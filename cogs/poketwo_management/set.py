import discord
from discord.ext import commands

from cogs.poketwo_helper.lockcommon import (
    MAX_LOCKTIME,
    MIN_LOCKTIME,
    format_duration,
    parse_duration,
)
from views.autolock_views import whitelist_result_text
from views.embeds import (
    err_embed,
    handle_command_error,
    info_embed,
    ok_embed,
    send_usage,
    warn_embed,
)


ADD_WORDS = {"add", "a"}
REMOVE_WORDS = {"remove", "r"}
CLEAR_WORDS = {"clear", "reset"}
ON_WORDS = {"on", "enable", "enabled", "true"}
OFF_WORDS = {"off", "disable", "disabled", "false"}


class Set(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_command_error(self, ctx: commands.Context, error):
        # Missing/invalid arguments -> "how to use" embed, other errors -> embeds
        await handle_command_error(ctx, error)

    # --- group command ---------------------------------------------------------
    @commands.group(
        name="set",
        invoke_without_command=True,
        description="Configure lock delays, lock times, whitelists, standards and category ping roles.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_group(self, ctx: commands.Context):
        await send_usage(ctx)

    # --- .set lockdelay <seconds> <lock...> [--global] ------------------------------------
    @set_group.command(
        name="lockdelay",
        aliases=["ld", "delay", "lock-delay"],
        usage="<seconds> <lock...> [--global]",
        description="Set a lock's delay in seconds.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_lockdelay(self, ctx: commands.Context, seconds: str = None, *locks_and_flags: str):
        # Parse arguments to check for --global flag
        locks = []
        global_flag = False

        for arg in locks_and_flags:
            if arg.lower() == "--global":
                global_flag = True
            else:
                locks.append(arg)

        if seconds is None:
            return await send_usage(ctx, title="Missing arg: `seconds`")
        if not locks:
            return await send_usage(ctx, title="Missing arg: `lock`")

        try:
            delay = int(seconds.lower().rstrip("s"))
        except ValueError:
            return await ctx.reply(embed=err_embed(f"Invalid seconds: {seconds}"), mention_author=False)

        if not (1 <= delay <= 600):
            return await ctx.reply(embed=err_embed("Delay must be 1-600s."), mention_author=False)

        cog = self.bot.get_cog("AutoLockConfig")
        if not cog:
            return await ctx.reply(embed=err_embed("AutoLockConfig is not loaded.", emoji="⚠️"), mention_author=False)

        if any(l.lower() == "all" for l in locks):
            cats = list(cog.all_categories())
            unknown = []
        else:
            cats, unknown = [], []
            for tok in locks:
                cat = cog.resolve_category(tok)
                if cat is None:
                    unknown.append(tok)
                elif cat not in cats:
                    cats.append(cat)

        if not cats:
            return await ctx.reply(embed=err_embed(f"Unknown lock(s): {', '.join(unknown)}"), mention_author=False)

        lines = []
        for cat in cats:
            if global_flag:
                # Apply to whole server (guild level)
                await cog.set_delay(ctx.guild.id, cat, delay)
                scope = "whole server"
            else:
                # Apply to current channel only
                await cog.set_delay_channel(ctx.guild.id, ctx.channel.id, cat, delay)
                scope = f"{ctx.channel.mention}"

            line = f"⏱️ **{cog.display_name(cat)}** → **{delay}s** ({scope})"
            if not global_flag:
                cfg_doc = await cog.get_guild_config(ctx.guild.id)
                if not (cfg_doc.get(cat) or {}).get("delay_enabled", True):
                    line += " — delay is off"
            lines.append(line)

        embed = ok_embed("Delay Updated", "\n".join(lines)[:4096])
        if unknown:
            embed.add_field(
                name="⚠️ Unknown Lock(s)",
                value=", ".join(f"`{u}`" for u in unknown)[:1024],
                inline=False,
            )
        await ctx.reply(embed=embed, mention_author=False)

    # --- shared helpers for the commands below -----------------------------------

    async def _cog(self, ctx: commands.Context):
        cog = self.bot.get_cog("AutoLockConfig")
        if not cog:
            await ctx.reply(embed=err_embed("AutoLockConfig is not loaded.", emoji="⚠️"), mention_author=False)
        return cog

    @staticmethod
    def _split_flags(args) -> tuple[list[str], bool]:
        global_flag = any(a.lower() == "--global" for a in args)
        return [a for a in args if a.lower() != "--global"], global_flag

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

    async def _whitelist_action(self, ctx, cog, category: str, label: str, action: str, items: list[str]):
        """add / remove / clear on one whitelist (a lock's, or the standard one)."""
        guild = ctx.guild
        if action in CLEAR_WORDS:
            count = await cog.clear_whitelist(guild.id, category)
            embed = ok_embed(f"{label} Whitelist Cleared", f"Removed **{count}** entr{'y' if count == 1 else 'ies'}.") \
                if count else warn_embed(f"{label} Whitelist", "It was already empty.")
            return await ctx.reply(embed=embed, mention_author=False)

        if not items:
            return await send_usage(ctx, title="Missing arg: `channels`")

        raw = ",".join(items)
        if action in ADD_WORDS:
            added, already, invalid = await cog.add_whitelist(guild, category, raw)
            text = whitelist_result_text(cog, guild, added=added, already=already, invalid=invalid)
            embed = ok_embed(f"{label} Whitelist Updated", text) if added else warn_embed(f"{label} Whitelist", text)
        else:
            removed, not_found = await cog.remove_whitelist(guild, category, raw)
            text = whitelist_result_text(cog, guild, removed=removed, not_found=not_found)
            embed = ok_embed(f"{label} Whitelist Updated", text) if removed else warn_embed(f"{label} Whitelist", text)
        await ctx.reply(embed=embed, mention_author=False)

    # --- .set locktime <time|on|off> <lock...> [--global] -----------------------------

    @set_group.command(
        name="locktime",
        aliases=["lt", "locktimer", "lock-time", "autounlock"],
        usage="<time|on|off> <lock...> [--global]",
        description="Set how long a lock stays locked before it auto-unlocks.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_locktime(self, ctx: commands.Context, value: str = None, *locks_and_flags: str):
        locks, global_flag = self._split_flags(locks_and_flags)
        if value is None:
            return await send_usage(ctx, title="Missing arg: `time`")
        if not locks:
            return await send_usage(ctx, title="Missing arg: `lock`")

        cog = await self._cog(ctx)
        if not cog:
            return

        word = value.lower()
        seconds = None
        if word not in ON_WORDS | OFF_WORDS:
            seconds = parse_duration(value)
            error = self._check_locktime(seconds)
            if error:
                return await ctx.reply(embed=err_embed(error), mention_author=False)

        cats, unknown = self._resolve_locks(cog, locks)
        if not cats:
            return await ctx.reply(embed=err_embed(f"Unknown lock(s): {', '.join(unknown)}"), mention_author=False)

        scope = "whole server" if global_flag else ctx.channel.mention
        lines = []
        for cat in cats:
            if seconds is not None:
                if global_flag:
                    await cog.set_locktime(ctx.guild.id, cat, seconds)
                else:
                    await cog.set_locktime_channel(ctx.guild.id, ctx.channel.id, cat, seconds)
                lines.append(f"⌛ **{cog.display_name(cat)}** → **{format_duration(seconds)}** ({scope}, auto-unlock on)")
            else:
                on = word in ON_WORDS
                if global_flag:
                    await cog.set_locktime_enabled(ctx.guild.id, cat, on)
                else:
                    await cog.set_locktime_enabled_channel(ctx.guild.id, ctx.channel.id, cat, on)
                lines.append(f"⌛ **{cog.display_name(cat)}** auto-unlock: **{'On ✅' if on else 'Off ❌'}** ({scope})")

        embed = ok_embed("Lock Time Updated", "\n".join(lines)[:4096], emoji="⌛")
        if unknown:
            embed.add_field(name="⚠️ Unknown Lock(s)", value=", ".join(f"`{u}`" for u in unknown)[:1024], inline=False)
        await ctx.reply(embed=embed, mention_author=False)

    # --- .set whitelist <lock...> <add|remove|clear> [channels...] ---------------------

    @set_group.command(
        name="whitelist",
        aliases=["wl"],
        usage="<lock...> <add|a|remove|r|clear|reset> [channels...]",
        description="Add / remove / clear the channels a lock may autolock in (server-wide).",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_whitelist(self, ctx: commands.Context, *args: str):
        actions = ADD_WORDS | REMOVE_WORDS | CLEAR_WORDS
        idx = next((i for i, a in enumerate(args) if a.lower() in actions), None)
        if idx is None:
            return await send_usage(ctx, title="Missing arg: `add|remove|clear`")
        if idx == 0:
            return await send_usage(ctx, title="Missing arg: `lock`")

        cog = await self._cog(ctx)
        if not cog:
            return

        cats, unknown = self._resolve_locks(cog, args[:idx])
        if not cats:
            return await ctx.reply(embed=err_embed(f"Unknown lock(s): {', '.join(unknown)}"), mention_author=False)

        action = args[idx].lower()
        for cat in cats:
            await self._whitelist_action(ctx, cog, cat, cog.display_name(cat), action, list(args[idx + 1:]))
        if unknown:
            await ctx.reply(embed=warn_embed("Unknown Lock(s)", ", ".join(f"`{u}`" for u in unknown)), mention_author=False)

    # --- .set use-standard <lock...> ----------------------------------------------------

    @set_group.command(
        name="use-standard",
        aliases=["usestandard", "standard"],
        usage="<lock...>",
        description="Copy the standard delay, lock time and whitelist onto a lock (server-wide).",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_use_standard(self, ctx: commands.Context, *locks: str):
        if not locks:
            return await send_usage(ctx, title="Missing arg: `lock`")
        cog = await self._cog(ctx)
        if not cog:
            return

        cats, unknown = self._resolve_locks(cog, locks)
        if not cats:
            return await ctx.reply(embed=err_embed(f"Unknown lock(s): {', '.join(unknown)}"), mention_author=False)

        lines = []
        for cat in cats:
            applied = await cog.use_standard(ctx.guild.id, cat)
            delay, delay_on = applied["delay"]
            locktime, locktime_on = applied["locktime"]
            lines.append(
                f"⭐ **{cog.display_name(cat)}** — delay `{delay}s`{'' if delay_on else ' (off)'}, "
                f"lock time `{format_duration(locktime)}`{'' if locktime_on else ' (off)'}, "
                f"whitelist `{len(applied['whitelist'])}`"
            )
        embed = ok_embed("Standard Applied", "\n".join(lines)[:4096], emoji="⭐")
        if unknown:
            embed.add_field(name="⚠️ Unknown Lock(s)", value=", ".join(f"`{u}`" for u in unknown)[:1024], inline=False)
        await ctx.reply(embed=embed, mention_author=False)

    # --- standard settings ---------------------------------------------------------------

    @set_group.command(
        name="standard-lockdelay",
        aliases=["standard-delay", "standard-ld"],
        usage="<seconds|on|off>",
        description="Set the standard lock delay.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_standard_lockdelay(self, ctx: commands.Context, value: str = None):
        if value is None:
            return await send_usage(ctx, title="Missing arg: `seconds`")
        cog = await self._cog(ctx)
        if not cog:
            return

        word = value.lower()
        if word in ON_WORDS | OFF_WORDS:
            on = word in ON_WORDS
            await cog.set_delay_enabled(ctx.guild.id, "standard", on)
            return await ctx.reply(
                embed=ok_embed("Standard Lock Delay", f"Delay is now **{'On ✅' if on else 'Off ❌'}**.", emoji="⏱️"),
                mention_author=False,
            )
        try:
            delay = int(word.rstrip("s"))
        except ValueError:
            return await ctx.reply(embed=err_embed(f"Invalid seconds: {value}"), mention_author=False)
        if not (1 <= delay <= 600):
            return await ctx.reply(embed=err_embed("Delay must be 1-600s."), mention_author=False)

        await cog.set_delay(ctx.guild.id, "standard", delay)
        await ctx.reply(embed=ok_embed("Standard Lock Delay", f"Set to **{delay}s**.", emoji="⏱️"), mention_author=False)

    @set_group.command(
        name="standard-locktime",
        aliases=["standard-lt"],
        usage="<time>",
        description="Set the standard lock time (how long before auto-unlock).",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_standard_locktime(self, ctx: commands.Context, value: str = None):
        if value is None:
            return await send_usage(ctx, title="Missing arg: `time`")
        cog = await self._cog(ctx)
        if not cog:
            return
        seconds = parse_duration(value)
        error = self._check_locktime(seconds)
        if error:
            return await ctx.reply(embed=err_embed(error), mention_author=False)
        await cog.set_locktime(ctx.guild.id, "standard", seconds)
        await ctx.reply(
            embed=ok_embed("Standard Lock Time", f"Set to **{format_duration(seconds)}** (timer on).", emoji="⌛"),
            mention_author=False,
        )

    @set_group.command(
        name="standard-locktimer",
        aliases=["standard-timer"],
        usage="<on|off|time>",
        description="Turn the standard lock timer (auto-unlock) on or off, or give it a time.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_standard_locktimer(self, ctx: commands.Context, value: str = None):
        if value is None:
            return await send_usage(ctx, title="Missing arg: `on|off`")
        cog = await self._cog(ctx)
        if not cog:
            return
        word = value.lower()
        if word in ON_WORDS | OFF_WORDS:
            on = word in ON_WORDS
            await cog.set_locktime_enabled(ctx.guild.id, "standard", on)
            return await ctx.reply(
                embed=ok_embed("Standard Lock Timer", f"Auto-unlock is now **{'On ✅' if on else 'Off ❌'}**.", emoji="⏳"),
                mention_author=False,
            )
        seconds = parse_duration(value)
        error = self._check_locktime(seconds)
        if error:
            return await ctx.reply(embed=err_embed(error), mention_author=False)
        await cog.set_locktime(ctx.guild.id, "standard", seconds)
        await ctx.reply(
            embed=ok_embed("Standard Lock Timer", f"On, set to **{format_duration(seconds)}**.", emoji="⏳"),
            mention_author=False,
        )

    @set_group.command(
        name="standard-whitelist",
        aliases=["standard-wl"],
        usage="<add|a|remove|r|clear|reset> [channels...]",
        description="Add / remove / clear the standard whitelist.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_standard_whitelist(self, ctx: commands.Context, action: str = None, *items: str):
        if action is None or action.lower() not in ADD_WORDS | REMOVE_WORDS | CLEAR_WORDS:
            return await send_usage(ctx, title="Missing arg: `add|remove|clear`")
        cog = await self._cog(ctx)
        if not cog:
            return
        await self._whitelist_action(ctx, cog, "standard", "Standard", action.lower(), list(items))

    # --- role setters ----------------------------------------------------------
    # Roles live in the PokePings data (utilities.pings -> roles.<key>), which is
    # what the Recognizer reads when it pings, so we write through PokePings.

    async def _set_role(self, ctx: commands.Context, key: str, label: str, role: discord.Role | None):
        pings = self.bot.get_cog("PokePings")
        if not pings:
            return await ctx.reply(embed=err_embed("PokePings is not loaded.", emoji="⚠️"), mention_author=False)

        g_id = str(ctx.guild.id)
        old_id = await pings.get_guild_role(g_id, key)

        if role is None:
            if old_id:
                return await ctx.reply(embed=info_embed(f"{label} Role", f"<@&{old_id}>", emoji="🏷️"), mention_author=False)
            return await ctx.reply(embed=warn_embed(f"{label} Role", "Not set."), mention_author=False)

        if old_id and str(old_id) == str(role.id):
            return await ctx.reply(embed=info_embed("No Change", f"Already {role.mention}."), mention_author=False)

        await pings.set_guild_role(g_id, key, str(role.id))

        if old_id:
            await ctx.reply(embed=ok_embed(
                f"{label} Role Replaced", f"<@&{old_id}> → {role.mention}"), mention_author=False)
        else:
            await ctx.reply(embed=ok_embed(f"{label} Role Set", role.mention), mention_author=False)

    @set_group.command(
        name="rarerole", aliases=["rarole"], usage="[@role]",
        description="Set or view the Rare ping role.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_rarerole(self, ctx: commands.Context, role: discord.Role = None):
        await self._set_role(ctx, "rare", "Rare", role)

    @set_group.command(
        name="regionalrole", aliases=["regrole"], usage="[@role]",
        description="Set or view the Regional ping role.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_regionalrole(self, ctx: commands.Context, role: discord.Role = None):
        await self._set_role(ctx, "regional", "Regional", role)

    @set_group.command(
        name="gigantamaxrole", aliases=["gmaxrole"], usage="[@role]",
        description="Set or view the Gigantamax ping role.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_gigantamaxrole(self, ctx: commands.Context, role: discord.Role = None):
        await self._set_role(ctx, "gmax", "Gigantamax", role)

    @set_group.command(
        name="paradoxrole", aliases=["pararole"], usage="[@role]",
        description="Set or view the Paradox ping role.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_paradoxrole(self, ctx: commands.Context, role: discord.Role = None):
        await self._set_role(ctx, "paradox", "Paradox", role)

    @set_group.command(
        name="eeveelutionsrole", aliases=["eevosroles", "eevosrole"], usage="[@role]",
        description="Set or view the Eeveelutions ping role.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_eeveelutionsrole(self, ctx: commands.Context, role: discord.Role = None):
        await self._set_role(ctx, "eevos", "Eeveelutions", role)


async def setup(bot: commands.Bot):
    await bot.add_cog(Set(bot))