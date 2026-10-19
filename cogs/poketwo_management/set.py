import re

import discord
from discord import app_commands
from discord.ext import commands

from cogs.poketwo_helper.lockcommon import (
    STANDARD_FLAGS,
    MAX_LOCKTIME,
    MIN_LOCKTIME,
    format_duration,
    parse_duration,
    split_scope_flags,
)
from views.autolock_views import MAX_DELAY, MIN_DELAY, RESTRICT_CATEGORIES, whitelist_result_text
from views.embeds import (
    err_embed,
    handle_command_error,
    info_embed,
    ok_embed,
    send_usage,
    warn_embed,
)
from views.loading import loading
from views.timer_views import MAX_SECONDS, MIN_SECONDS, TIMER_LABELS


ADD_WORDS = {"add", "a"}
REMOVE_WORDS = {"remove", "r"}
CLEAR_WORDS = {"clear", "reset"}
ON_WORDS = {"on", "enable", "enabled", "true"}
OFF_WORDS = {"off", "disable", "disabled", "false"}
TRUE_WORDS = {"true", "t", "yes", "y", "on", "enable", "enabled"}
FALSE_WORDS = {"false", "f", "no", "n", "off", "disable", "disabled"}

# Only these accept --standard / --stan; every other .set command rejects the flag.
STANDARD_COMMANDS = "lockdelay, locktime, whitelist, restrictunlockers and the timers (shtimer, cltimer, rptimer, tptimer)"
NO_STANDARD_COMMANDS = {
    "set rarerole", "set regionalrole", "set gigantamaxrole", "set paradoxrole", "set eeveelutionsrole",
    "set use-standard",
}

STANDARD = "standard"

# `.set use-standard shtimer` etc.
TIMER_TOKENS = {"shtimer": "sh", "cltimer": "cl", "rptimer": "rp", "tptimer": "tp"}

# A whitelist item: channel mention, channel id (or list index when removing), "*" or "#name".
CHANNEL_TOKEN = re.compile(r"^(<#\d+>|\d+|\*|#\S+)$")


class Set(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_command_error(self, ctx: commands.Context, error):
        # Missing/invalid arguments -> "how to use" embed, other errors -> embeds
        await handle_command_error(ctx, error)

    async def cog_before_invoke(self, ctx: commands.Context):
        # --standard is only meaningful for lockdelay / locktime / whitelist / restrictunlockers.
        # Without this guard the timer / role / use-standard commands would silently ignore it.
        if ctx.interaction is None and ctx.command and ctx.command.qualified_name in NO_STANDARD_COMMANDS:
            if any(word.lower() in STANDARD_FLAGS for word in ctx.message.content.split()):
                raise commands.BadArgument(f"`--standard` can only be used with {STANDARD_COMMANDS}.")

    # --- group command ---------------------------------------------------------
    @commands.hybrid_group(
        name="set",
        invoke_without_command=True,
        description="Configure lock delays, lock times, whitelists, restrict unlockers, timers and ping roles.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_group(self, ctx: commands.Context):
        await send_usage(ctx)

    # --- shared helpers --------------------------------------------------------

    async def _cog(self, ctx: commands.Context):
        cog = self.bot.get_cog("AutoLockConfig")
        if not cog:
            await ctx.reply(embed=err_embed("AutoLockConfig is not loaded.", emoji="⚠️"), mention_author=False)
        return cog

    @staticmethod
    def _resolve_locks(cog, tokens) -> tuple[list[str], list[str]]:
        if any(t.lower() == "all" for t in tokens):
            return list(cog.all_categories()), []
        cats, unknown = [], []
        for tok in tokens:
            cat = cog.resolve_category(tok)
            if cat is None:
                unknown.append(tok)
            elif cat not in cats:
                cats.append(cat)
        return cats, unknown

    @staticmethod
    def _check_locktime(seconds):
        """Returns an error string, or None when the duration is usable."""
        if seconds is None:
            return "Use a time like `30m`, `2h` or `1h30m` (a bare number = minutes)."
        if not (MIN_LOCKTIME <= seconds <= MAX_LOCKTIME):
            return f"Lock time must be {format_duration(MIN_LOCKTIME)}-{format_duration(MAX_LOCKTIME)}."
        return None

    @staticmethod
    def _check_delay(value: str):
        """Returns (seconds, error). A bare number is seconds; `15s` / `1m` also work."""
        seconds = parse_duration(value, default_unit="s")
        if seconds is None:
            return None, f"Invalid time: {value} (use seconds, e.g. `15`)."
        if not (MIN_DELAY <= seconds <= MAX_DELAY):
            return None, f"Delay must be {MIN_DELAY}-{MAX_DELAY}s."
        return seconds, None

    async def _scope_error(self, ctx, locks, is_global: bool, is_standard: bool, *, allow_global: bool = True) -> bool:
        """Replies and returns True when the flag combination is not allowed."""
        message = None
        if is_global and is_standard:
            message = "`--global` and `--standard` can't be used together."
        elif is_global and not allow_global:
            message = "`--global` isn't used here: whitelists are always server-wide."
        elif is_standard and locks:
            message = "`--standard` sets the standard values, so don't list locks with it."
        if message:
            await ctx.reply(embed=err_embed(message), mention_author=False)
            return True
        return False

    async def _targets(self, ctx, cog, locks, is_standard: bool):
        """Locks a command applies to: the standard, the listed locks, or every lock when none
        are listed. Returns (cats, unknown), or None after replying with an error."""
        if is_standard:
            return [STANDARD], []
        if not locks:
            return list(cog.all_categories()), []
        cats, unknown = self._resolve_locks(cog, locks)
        if not cats:
            await ctx.reply(embed=err_embed(f"Unknown lock(s): {', '.join(unknown)}"), mention_author=False)
            return None
        return cats, unknown

    @staticmethod
    def _scope_text(ctx, is_global: bool, is_standard: bool) -> str:
        if is_standard:
            return "standard"
        return "whole server" if is_global else ctx.channel.mention

    @staticmethod
    async def _scope_config(cog, ctx, cat: str, is_global: bool, is_standard: bool) -> dict:
        if is_standard:
            return await cog.get_standard(ctx.guild.id)
        if is_global:
            return await cog.get_category_config(ctx.guild.id, cat)
        return await cog.get_category_config_channel(ctx.guild.id, ctx.channel.id, cat)

    @staticmethod
    def _unknown_field(embed: discord.Embed, unknown: list[str]) -> None:
        if unknown:
            embed.add_field(
                name="⚠️ Unknown Lock(s)",
                value=", ".join(f"`{u}`" for u in unknown)[:1024],
                inline=False,
            )

    # --- .set lockdelay|lock-delay|delay|ld {time} [lock] [--global|--standard] ----------

    @set_group.command(
        name="lockdelay",
        aliases=["lock-delay", "delay", "ld"],
        usage="<time> [lock...] [--global|--standard]",
        description="Set a lock's delay in seconds (no lock = all locks).",
        extras={"args": {"args": "Delay in seconds (e.g. 15 or 30s), then optional lock names to change (sh, cl, rp, tp, res or all; none = all locks), plus optional --global (whole server) or --standard (the standard value; no locks with it)."}},
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @loading
    @app_commands.describe(
        args='Delay in seconds, then optional locks (sh, cl, rp, tp, res, all) and --global or --standard.',
    )
    async def set_lockdelay(self, ctx: commands.Context, *, args: str = ""):
        tokens, is_global, is_standard = split_scope_flags(args.split())
        if not tokens:
            return await send_usage(ctx, title="Missing arg: `time`")
        value, locks = tokens[0], tokens[1:]

        if await self._scope_error(ctx, locks, is_global, is_standard):
            return
        delay, error = self._check_delay(value)
        if error:
            return await ctx.reply(embed=err_embed(error), mention_author=False)

        cog = await self._cog(ctx)
        if not cog:
            return
        targets = await self._targets(ctx, cog, locks, is_standard)
        if targets is None:
            return
        cats, unknown = targets

        scope = self._scope_text(ctx, is_global, is_standard)
        lines = []
        for cat in cats:
            if is_global or is_standard:
                await cog.set_delay(ctx.guild.id, cat, delay)
            else:
                await cog.set_delay_channel(ctx.guild.id, ctx.channel.id, cat, delay)

            line = f"⏱️ **{cog.display_name(cat)}** → **{delay}s** ({scope})"
            cfg = await self._scope_config(cog, ctx, cat, is_global, is_standard)
            if not cfg.get("delay_enabled", True):
                line += " — delay is off"
            lines.append(line)

        embed = ok_embed("Delay Updated", "\n".join(lines)[:4096], emoji="⏱️")
        self._unknown_field(embed, unknown)
        await ctx.reply(embed=embed, mention_author=False)

    # --- .set locktime|lock-time|time|lt {time} [lock(s)] [--global|--standard] ----------

    @set_group.command(
        name="locktime",
        aliases=["lock-time", "time", "lt"],
        usage="<time|on|off> [lock(s)...] [--global|--standard]",
        description="Set how long a lock stays locked before it auto-unlocks (no lock = all locks).",
        extras={"args": {"args": "Lock time (e.g. 30m, 2h, 1h30m; a bare number = minutes) or on/off to toggle auto-unlock, then optional lock names (sh, cl, rp, tp, res or all; none = all locks), plus optional --global or --standard."}},
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @loading
    @app_commands.describe(
        args='Time (30m, 2h, 1h30m) or on/off, then optional locks (or all) and --global or --standard.',
    )
    async def set_locktime(self, ctx: commands.Context, *, args: str = ""):
        tokens, is_global, is_standard = split_scope_flags(args.split())
        if not tokens:
            return await send_usage(ctx, title="Missing arg: `time`")
        value, locks = tokens[0], tokens[1:]

        if await self._scope_error(ctx, locks, is_global, is_standard):
            return

        word = value.lower()
        seconds = None
        if word not in ON_WORDS | OFF_WORDS:
            seconds = parse_duration(value)
            error = self._check_locktime(seconds)
            if error:
                return await ctx.reply(embed=err_embed(error), mention_author=False)

        cog = await self._cog(ctx)
        if not cog:
            return
        targets = await self._targets(ctx, cog, locks, is_standard)
        if targets is None:
            return
        cats, unknown = targets

        scope = self._scope_text(ctx, is_global, is_standard)
        lines = []
        for cat in cats:
            if seconds is not None:
                if is_global or is_standard:
                    await cog.set_locktime(ctx.guild.id, cat, seconds)
                else:
                    await cog.set_locktime_channel(ctx.guild.id, ctx.channel.id, cat, seconds)
                lines.append(f"⌛ **{cog.display_name(cat)}** → **{format_duration(seconds)}** ({scope}, auto-unlock on)")
            else:
                on = word in ON_WORDS
                if is_global or is_standard:
                    await cog.set_locktime_enabled(ctx.guild.id, cat, on)
                else:
                    await cog.set_locktime_enabled_channel(ctx.guild.id, ctx.channel.id, cat, on)
                lines.append(f"⌛ **{cog.display_name(cat)}** auto-unlock: **{'On ✅' if on else 'Off ❌'}** ({scope})")

        embed = ok_embed("Lock Time Updated", "\n".join(lines)[:4096], emoji="⌛")
        self._unknown_field(embed, unknown)
        await ctx.reply(embed=embed, mention_author=False)

    # --- .set whitelist|wl {add|a|remove|r} <channels> [lock(s)] [--standard] --------------

    @set_group.command(
        name="whitelist",
        aliases=["wl"],
        usage="<add|a|remove|r> <channels...> [lock(s)...] [--standard]",
        description="Add / remove the channels a lock may autolock in (server-wide; no lock = all locks).",
        extras={"args": {"args": "add/a, remove/r or clear/reset, then channels (#mentions, channel IDs, #name, * for all, or list numbers when removing), then optional lock names (sh, cl, rp, tp, res or all; none = all locks). --standard edits the standard whitelist."}},
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @loading
    @app_commands.describe(
        args='add / remove / clear, then channels (mentions, IDs or *), then optional locks (--standard too).',
    )
    async def set_whitelist(self, ctx: commands.Context, *, args: str = ""):
        tokens, is_global, is_standard = split_scope_flags(args.split())

        actions = ADD_WORDS | REMOVE_WORDS | CLEAR_WORDS
        if not tokens or tokens[0].lower() not in actions:
            return await send_usage(ctx, title="Missing arg: `add|remove`")
        action = tokens[0].lower()

        cog = await self._cog(ctx)
        if not cog:
            return

        locks, items, unknown = [], [], []
        for tok in tokens[1:]:
            if tok.lower() == "all" or cog.resolve_category(tok) is not None:
                locks.append(tok)
            elif CHANNEL_TOKEN.match(tok):
                items.append(tok)
            else:
                unknown.append(tok)

        if await self._scope_error(ctx, locks, is_global, is_standard, allow_global=False):
            return
        if unknown:
            return await ctx.reply(
                embed=err_embed(f"Unknown lock or channel: {', '.join(f'`{u}`' for u in unknown)}"),
                mention_author=False,
            )
        if action not in CLEAR_WORDS and not items:
            return await send_usage(ctx, title="Missing arg: `channels`")

        targets = await self._targets(ctx, cog, locks, is_standard)
        if targets is None:
            return
        cats, _ = targets

        guild = ctx.guild
        raw = ",".join(items)
        results: list[tuple[str, str]] = []
        changed = False
        for cat in cats:
            label = cog.display_name(cat)
            if action in CLEAR_WORDS:
                count = await cog.clear_whitelist(guild.id, cat)
                text = f"Removed **{count}** entr{'y' if count == 1 else 'ies'}." if count else "It was already empty."
                did = bool(count)
            elif action in ADD_WORDS:
                added, already, invalid = await cog.add_whitelist(guild, cat, raw)
                text = whitelist_result_text(cog, guild, added=added, already=already, invalid=invalid)
                did = bool(added)
            else:
                removed, not_found = await cog.remove_whitelist(guild, cat, raw)
                text = whitelist_result_text(cog, guild, removed=removed, not_found=not_found)
                did = bool(removed)
            changed = changed or did
            results.append((label, text))

        make = ok_embed if changed else warn_embed
        if len(results) == 1:
            label, text = results[0]
            embed = make(f"{label} Whitelist {'Updated' if changed else ''}".strip(), text[:4096])
        else:
            embed = make(f"Whitelist {'Updated' if changed else ''}".strip())
            for label, text in results:
                embed.add_field(name=label, value=text[:1024], inline=False)
        await ctx.reply(embed=embed, mention_author=False)

    # --- .set restrictunlockers|restrict-unlockers|ru {true|false|t|f} [lock(s)] [--global|--standard] ---

    @set_group.command(
        name="restrictunlockers",
        aliases=["restrict-unlockers", "ru"],
        usage="<true|false|t|f> [lock(s)...] [--global|--standard]",
        description="Only the pinged user(s) can unlock (res/sh/cl/tp/rp; no lock = all of them).",
        extras={"args": {"args": "true/t or false/f, then optional lock names (res, sh, cl, tp, rp or all; none = all of them), plus optional --global or --standard."}},
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @loading
    @app_commands.describe(
        args='true or false, then optional locks (res, sh, cl, tp, rp, all) and --global or --standard.',
    )
    async def set_restrictunlockers(self, ctx: commands.Context, *, args: str = ""):
        tokens, is_global, is_standard = split_scope_flags(args.split())
        if not tokens:
            return await send_usage(ctx, title="Missing arg: `true|false`")
        value, locks = tokens[0].lower(), tokens[1:]
        if value not in TRUE_WORDS | FALSE_WORDS:
            return await ctx.reply(embed=err_embed("Use `true`, `false`, `t` or `f`."), mention_author=False)
        on = value in TRUE_WORDS

        if await self._scope_error(ctx, locks, is_global, is_standard):
            return
        cog = await self._cog(ctx)
        if not cog:
            return

        scope = self._scope_text(ctx, is_global, is_standard)
        state = "**On ✅**" if on else "**Off ❌**"

        if is_standard:
            await cog.set_restrict(ctx.guild.id, STANDARD, on)
            return await ctx.reply(
                embed=ok_embed("Standard Restrict Updated", f"🛡️ Restrict unlockers: {state} (standard)", emoji="🛡️"),
                mention_author=False,
            )

        if locks:
            cats, unknown = self._resolve_locks(cog, locks)
            if not cats:
                return await ctx.reply(embed=err_embed(f"Unknown lock(s): {', '.join(unknown)}"), mention_author=False)
        else:
            cats, unknown = [c for c in cog.all_categories() if cog.can_restrict(c)], []

        lines, changed = [], 0
        for cat in cats:
            if not cog.can_restrict(cat):
                lines.append(f"⚠️ **{cog.display_name(cat)}** can't be restricted.")
                continue
            if is_global:
                await cog.set_restrict(ctx.guild.id, cat, on)
            else:
                await cog.set_restrict_channel(ctx.guild.id, ctx.channel.id, cat, on)
            lines.append(f"🛡️ **{cog.display_name(cat)}** restrict: {state} ({scope})")
            changed += 1

        description = "\n".join(lines)[:4096]
        embed = (
            ok_embed("Restrict Updated", description, emoji="🛡️")
            if changed
            else warn_embed("Nothing Changed", description)
        )
        self._unknown_field(embed, unknown)
        await ctx.reply(embed=embed, mention_author=False)

    # --- .set use-standard <lock...> ----------------------------------------------------

    @set_group.command(
        name="use-standard",
        aliases=["usestandard", "standard"],
        usage="<lock...>",
        description="Copy the standard delay, lock time, whitelist and restrict onto a lock (server-wide).",
        extras={"args": {"locks": "One or more lock names (sh, cl, rp, tp, res or all) to copy the standard delay, lock time, whitelist and restrict onto."}},
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @loading
    @app_commands.describe(
        locks='Locks to apply the standard to: sh, cl, rp, tp, res or all.',
    )
    async def set_use_standard(self, ctx: commands.Context, *, locks: str = ""):
        tokens = locks.split()
        if not tokens:
            return await send_usage(ctx, title="Missing arg: `lock`")
        cog = await self._cog(ctx)
        if not cog:
            return

        timer_cats = list(dict.fromkeys(TIMER_TOKENS[t.lower()] for t in tokens if t.lower() in TIMER_TOKENS))
        lock_tokens = [t for t in tokens if t.lower() not in TIMER_TOKENS]

        cats, unknown = self._resolve_locks(cog, lock_tokens) if lock_tokens else ([], [])
        if not cats and not timer_cats:
            return await ctx.reply(embed=err_embed(f"Unknown lock(s): {', '.join(unknown)}"), mention_author=False)

        lines = []
        for cat in cats:
            applied = await cog.use_standard(ctx.guild.id, cat)
            delay, delay_on = applied["delay"]
            locktime, locktime_on = applied["locktime"]
            line = (
                f"⭐ **{cog.display_name(cat)}** — delay `{delay}s`{'' if delay_on else ' (off)'}, "
                f"lock time `{format_duration(locktime)}`{'' if locktime_on else ' (off)'}, "
                f"whitelist `{len(applied['whitelist'])}`"
            )
            if "restrict" in applied:
                line += f", restrict {'on' if applied['restrict'] else 'off'}"
            lines.append(line)

        if timer_cats:
            timer = await self._timer_cog(ctx)
            if not timer:
                return
            for cat in timer_cats:
                applied = await timer.use_standard(ctx.guild.id, cat)
                lines.append(
                    f"⭐ **{TIMER_LABELS.get(cat, cat.upper())}** — `{applied['seconds']}s`, "
                    f"{'on' if applied['enabled'] else 'off'}"
                )
        embed = ok_embed("Standard Applied", "\n".join(lines)[:4096], emoji="⭐")
        self._unknown_field(embed, unknown)
        await ctx.reply(embed=embed, mention_author=False)

    # --- timers: .set shtimer|cltimer|rptimer|tptimer {time|on|off} [--global|--standard] ---
    # Same scopes as the locks: no flag = this channel only, --global = whole server,
    # --standard = the standard value. Timers live in the Timer cog (see `.c timer`).

    async def _timer_cog(self, ctx: commands.Context):
        timer = self.bot.get_cog("Timer")
        if not timer:
            await ctx.reply(embed=err_embed("Timer is not loaded.", emoji="⚠️"), mention_author=False)
        return timer

    async def _set_timer(self, ctx: commands.Context, category: str, args: str):
        tokens, is_global, is_standard = split_scope_flags(args.split())
        if not tokens:
            return await send_usage(ctx, title="Missing arg: `timer`")
        if len(tokens) > 1:
            return await ctx.reply(
                embed=err_embed("Give just one value: seconds (e.g. `15`) or `on` / `off`."), mention_author=False
            )
        if is_global and is_standard:
            return await ctx.reply(
                embed=err_embed("`--global` and `--standard` can't be used together."), mention_author=False
            )

        value = tokens[0].lower()
        seconds = None
        enabled = None
        if value in ON_WORDS:
            enabled = True
        elif value in OFF_WORDS:
            enabled = False
        else:
            seconds = parse_duration(value, default_unit="s")
            if seconds is None:
                return await ctx.reply(
                    embed=err_embed(f"Invalid timer: {tokens[0]} (use seconds, e.g. `15`, or `on` / `off`)."),
                    mention_author=False,
                )
            if not (MIN_SECONDS <= seconds <= MAX_SECONDS):
                return await ctx.reply(
                    embed=err_embed(f"Timer must be {MIN_SECONDS}-{MAX_SECONDS}s."), mention_author=False
                )

        timer = await self._timer_cog(ctx)
        if not timer:
            return

        gid, cid = ctx.guild.id, ctx.channel.id
        scope = "standard" if is_standard else ("whole server" if is_global else ctx.channel.mention)
        target = STANDARD if is_standard else category

        if seconds is not None:
            if is_global or is_standard:
                await timer.set_seconds(gid, target, seconds)
            else:
                await timer.set_seconds_channel(gid, cid, category, seconds)
        elif is_global or is_standard:
            await timer.set_enabled(gid, target, enabled)
        else:
            await timer.set_enabled_channel(gid, cid, category, enabled)

        if is_standard:
            cfg = await timer.get_standard(gid)
        elif is_global:
            cfg = await timer.get_category_config(gid, category)
        else:
            cfg = await timer.get_category_config_channel(gid, cid, category)

        label = TIMER_LABELS.get(category, category.upper())
        if is_standard:
            label = "Standard Timer"
        if seconds is not None:
            text = f"Set to **{seconds}s** ({scope})."
            if not cfg.get("enabled", False):
                text += f" It's currently off here — turn it on with `.toggle {category}timer`."
        else:
            text = f"**{'On ✅' if enabled else 'Off ❌'}** ({scope}) — `{cfg.get('seconds', 15)}s`."
        await ctx.reply(embed=ok_embed(f"{label} Updated", text, emoji="⏲️"), mention_author=False)

    @set_group.command(
        name="shtimer",
        usage="<time|on|off> [--global|--standard]",
        description="Set the SH timer length in seconds, or turn it on/off.",
        extras={"args": {"args": "Timer length in seconds (e.g. 15 or 30s) or on/off, plus optional --global (whole server) or --standard (the standard value). No flag = this channel only."}},
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @app_commands.describe(
        args='Timer length in seconds (e.g. 15 or 30s) or on/off, plus --global or --standard.',
    )
    async def set_shtimer(self, ctx: commands.Context, *, args: str = ""):
        await self._set_timer(ctx, "sh", args)

    @set_group.command(
        name="cltimer",
        usage="<time|on|off> [--global|--standard]",
        description="Set the CL timer length in seconds, or turn it on/off.",
        extras={"args": {"args": "Timer length in seconds (e.g. 15 or 30s) or on/off, plus optional --global (whole server) or --standard (the standard value). No flag = this channel only."}},
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @app_commands.describe(
        args='Timer length in seconds (e.g. 15 or 30s) or on/off, plus --global or --standard.',
    )
    async def set_cltimer(self, ctx: commands.Context, *, args: str = ""):
        await self._set_timer(ctx, "cl", args)

    @set_group.command(
        name="rptimer",
        usage="<time|on|off> [--global|--standard]",
        description="Set the RP timer length in seconds, or turn it on/off.",
        extras={"args": {"args": "Timer length in seconds (e.g. 15 or 30s) or on/off, plus optional --global (whole server) or --standard (the standard value). No flag = this channel only."}},
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @app_commands.describe(
        args='Timer length in seconds (e.g. 15 or 30s) or on/off, plus --global or --standard.',
    )
    async def set_rptimer(self, ctx: commands.Context, *, args: str = ""):
        await self._set_timer(ctx, "rp", args)

    @set_group.command(
        name="tptimer",
        usage="<time|on|off> [--global|--standard]",
        description="Set the TP timer length in seconds, or turn it on/off.",
        extras={"args": {"args": "Timer length in seconds (e.g. 15 or 30s) or on/off, plus optional --global (whole server) or --standard (the standard value). No flag = this channel only."}},
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @app_commands.describe(
        args='Timer length in seconds (e.g. 15 or 30s) or on/off, plus --global or --standard.',
    )
    async def set_tptimer(self, ctx: commands.Context, *, args: str = ""):
        await self._set_timer(ctx, "tp", args)


    # --- role setters ----------------------------------------------------------
    # Roles live in the PokePings data (utilities.pings -> roles.<key>), which is
    # what the Recognizer reads when it pings, so we write through PokePings.

    async def _set_role(self, ctx: commands.Context, key: str, label: str, role: discord.Role):
        pings = self.bot.get_cog("PokePings")
        if not pings:
            return await ctx.reply(embed=err_embed("PokePings is not loaded.", emoji="⚠️"), mention_author=False)

        g_id = str(ctx.guild.id)
        old_id = await pings.get_guild_role(g_id, key)

        if old_id and str(old_id) == str(role.id):
            return await ctx.reply(embed=info_embed("No Change", f"Already {role.mention}."), mention_author=False)

        await pings.set_guild_role(g_id, key, str(role.id))

        if old_id:
            await ctx.reply(embed=ok_embed(
                f"{label} Role Replaced", f"<@&{old_id}> → {role.mention}"), mention_author=False)
        else:
            await ctx.reply(embed=ok_embed(f"{label} Role Set", role.mention), mention_author=False)

    @set_group.command(
        name="rarerole", aliases=["rare", "rarole", "ra"], usage="<role>",
        description="Set the Rare ping role.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @app_commands.describe(
        role='The role to ping for Rare spawns (mention or ID).',
    )
    async def set_rarerole(
        self,
        ctx: commands.Context,
        role: discord.Role = commands.parameter(description="The role to ping for Rare spawns (mention or ID)."),
    ):
        await self._set_role(ctx, "rare", "Rare", role)

    @set_group.command(
        name="regionalrole", aliases=["regional", "regrole", "reg"], usage="<role>",
        description="Set the Regional ping role.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @app_commands.describe(
        role='The role to ping for Regional spawns (mention or ID).',
    )
    async def set_regionalrole(
        self,
        ctx: commands.Context,
        role: discord.Role = commands.parameter(description="The role to ping for Regional spawns (mention or ID)."),
    ):
        await self._set_role(ctx, "regional", "Regional", role)

    @set_group.command(
        name="gigantamaxrole", aliases=["gigantamax", "gmaxrole", "gmax"], usage="<role>",
        description="Set the Gigantamax ping role.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @app_commands.describe(
        role='The role to ping for Gigantamax spawns (mention or ID).',
    )
    async def set_gigantamaxrole(
        self,
        ctx: commands.Context,
        role: discord.Role = commands.parameter(description="The role to ping for Gigantamax spawns (mention or ID)."),
    ):
        await self._set_role(ctx, "gmax", "Gigantamax", role)

    @set_group.command(
        name="paradoxrole", aliases=["paradox", "pararole", "para"], usage="<role>",
        description="Set the Paradox ping role.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @app_commands.describe(
        role='The role to ping for Paradox spawns (mention or ID).',
    )
    async def set_paradoxrole(
        self,
        ctx: commands.Context,
        role: discord.Role = commands.parameter(description="The role to ping for Paradox spawns (mention or ID)."),
    ):
        await self._set_role(ctx, "paradox", "Paradox", role)

    @set_group.command(
        name="eeveelutionsrole", aliases=["eeveelutions", "eevosrole", "eevos"], usage="<role>",
        description="Set the Eeveelutions ping role.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @app_commands.describe(
        role='The role to ping for Eeveelutions spawns (mention or ID).',
    )
    async def set_eeveelutionsrole(
        self,
        ctx: commands.Context,
        role: discord.Role = commands.parameter(description="The role to ping for Eeveelutions spawns (mention or ID)."),
    ):
        await self._set_role(ctx, "eevos", "Eeveelutions", role)


async def setup(bot: commands.Bot):
    await bot.add_cog(Set(bot))