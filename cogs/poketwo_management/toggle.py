import discord
from discord.ext import commands

from cogs.poketwo_helper.lockcommon import format_duration, split_scope_flags
from views.embeds import (
    err_embed,
    handle_command_error,
    ok_embed,
    send_usage,
    warn_embed,
)

STANDARD = "standard"


class Toggle(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_command_error(self, ctx: commands.Context, error):
        await handle_command_error(ctx, error)

    async def _config_cog(self, ctx: commands.Context):
        cog = self.bot.get_cog("AutoLockConfig")
        if not cog:
            await ctx.reply(embed=err_embed("AutoLockConfig is not loaded.", emoji="⚠️"), mention_author=False)
        return cog

    @staticmethod
    def _resolve_all(cog, tokens):
        """Split tokens into (unique valid category keys, unknown tokens), keeping order."""
        cats, unknown = [], []
        for tok in tokens:
            cat = cog.resolve_category(tok)
            if cat is None:
                unknown.append(tok)
            elif cat not in cats:
                cats.append(cat)
        return cats, unknown

    @staticmethod
    async def _flag_error(ctx, locks, is_global: bool, is_standard: bool, *, allow_standard: bool) -> bool:
        """Replies and returns True when the flag combination is not allowed."""
        message = None
        if is_global and is_standard:
            message = "`--global` and `--standard` can't be used together."
        elif is_standard and not allow_standard:
            message = "`--standard` can only be used with lockdelay, locktime and whitelist."
        elif is_standard and locks:
            message = "`--standard` toggles the standard values, so don't list locks with it."
        if message:
            await ctx.reply(embed=err_embed(message), mention_author=False)
            return True
        return False

    @staticmethod
    def _unknown_field(embed: discord.Embed, unknown: list[str]) -> None:
        if unknown:
            embed.add_field(
                name="⚠️ Unknown Lock(s)",
                value=", ".join(f"`{u}`" for u in unknown)[:1024],
                inline=False,
            )

    async def _naming_line(self, ctx: commands.Context, cog, is_global: bool) -> str:
        naming_state = (
            await cog.toggle_naming(ctx.guild.id)
            if is_global
            else await cog.toggle_naming_channel(ctx.guild.id, ctx.channel.id)
        )
        status = "Enabled ✅" if naming_state else "Disabled ❌"
        scope = "whole server" if is_global else ctx.channel.mention
        return f"✨ **Pokémon naming**: **{status}** ({scope})"

    # --- .toggle {lock} [--global] ---------------------------------------------
    # Unknown sub-command names fall through to this callback, so `.toggle shlock`,
    # `.toggle cllock`, `.toggle reslock`, ... are all handled here.

    @commands.group(
        name="toggle",
        invoke_without_command=True,
        usage="<lock...> [--global]",
        description="Toggle locks, Pokémon naming, delays and restrictions.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def toggle_group(self, ctx: commands.Context, *args: str):
        locks, global_flag, standard_flag = split_scope_flags(args)
        if await self._flag_error(ctx, locks, global_flag, standard_flag, allow_standard=False):
            return
        if not locks:
            return await send_usage(ctx)

        cog = await self._config_cog(ctx)
        if not cog:
            return

        naming_requested = any(token.lower() == "naming" for token in locks)
        # Keep "naming" separate from autolock categories while allowing it to be
        # toggled alongside lock categories in one command.
        cats, unknown = self._resolve_all(
            cog, [token for token in locks if token.lower() != "naming"]
        )
        if not cats and not naming_requested:
            return await ctx.reply(embed=err_embed(f"Unknown lock(s): {', '.join(unknown)}"), mention_author=False)

        lines = []
        if naming_requested:
            lines.append(await self._naming_line(ctx, cog, global_flag))

        scope = "whole server" if global_flag else ctx.channel.mention

        for cat in cats:
            if global_flag:
                new_state = await cog.toggle_lock(ctx.guild.id, cat)
            else:
                new_state = await cog.toggle_lock_channel(ctx.guild.id, ctx.channel.id, cat)

            status = "Enabled ✅" if new_state else "Disabled ❌"
            lines.append(f"🔒 **{cog.display_name(cat)}**: **{status}** ({scope})")

        title = "Settings Toggled" if naming_requested else "Lock Toggled"
        embed = ok_embed(title, "\n".join(lines)[:4096])
        self._unknown_field(embed, unknown)
        await ctx.reply(embed=embed, mention_author=False)

    # --- .toggle naming [--global] ---------------------------------------------

    @toggle_group.command(
        name="naming",
        usage="[--global]",
        description="Toggle Pokémon naming for this channel (or the whole server).",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def toggle_naming(self, ctx: commands.Context, *flags: str):
        extra, global_flag, standard_flag = split_scope_flags(flags)
        if await self._flag_error(ctx, extra, global_flag, standard_flag, allow_standard=False):
            return
        if extra:
            return await send_usage(ctx)

        cog = await self._config_cog(ctx)
        if not cog:
            return
        line = await self._naming_line(ctx, cog, global_flag)
        await ctx.reply(embed=ok_embed("Naming Toggled", line, emoji="✨"), mention_author=False)

    # --- .toggle lockdelay|lock-delay|delay|ld {lock(s)} [--global|--standard] -----

    @toggle_group.command(
        name="lockdelay",
        aliases=["lock-delay", "delay", "ld"],
        usage="<lock(s)...> [--global|--standard]",
        description="Toggle a lock's delay.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def toggle_lockdelay(self, ctx: commands.Context, *args: str):
        locks, global_flag, standard_flag = split_scope_flags(args)
        if await self._flag_error(ctx, locks, global_flag, standard_flag, allow_standard=True):
            return
        if not locks and not standard_flag:
            return await send_usage(ctx, title="Missing arg: `lock`")

        cog = await self._config_cog(ctx)
        if not cog:
            return

        if standard_flag:
            new_state = await cog.toggle_delay(ctx.guild.id, STANDARD)
            std = await cog.get_standard(ctx.guild.id)
            state = f"**On ✅** (`{std.get('delay', 15)}s`)" if new_state else "**Off ❌**"
            return await ctx.reply(
                embed=ok_embed("Standard Lock Delay Toggled", f"Delay: {state}", emoji="⏱️"),
                mention_author=False,
            )

        cats, unknown = self._resolve_all(cog, locks)
        if not cats:
            return await ctx.reply(embed=err_embed(f"Unknown lock(s): {', '.join(unknown)}"), mention_author=False)

        scope = "whole server" if global_flag else ctx.channel.mention
        lines = []
        for cat in cats:
            if global_flag:
                new_state = await cog.toggle_delay(ctx.guild.id, cat)
                cfg = await cog.get_category_config(ctx.guild.id, cat)
            else:
                new_state = await cog.toggle_delay_channel(ctx.guild.id, ctx.channel.id, cat)
                cfg = await cog.get_category_config_channel(ctx.guild.id, ctx.channel.id, cat)

            state = f"**On ✅** (`{cfg.get('delay', 15)}s`)" if new_state else "**Off ❌**"
            lines.append(f"⏱️ **{cog.display_name(cat)}** delay: {state} ({scope})")

        embed = ok_embed("Lock Delay Toggled", "\n".join(lines)[:4096], emoji="⏱️")
        self._unknown_field(embed, unknown)
        await ctx.reply(embed=embed, mention_author=False)

    # --- .toggle locktime|lock-time|time|lt {lock(s)} [--global|--standard] --------

    @toggle_group.command(
        name="locktime",
        aliases=["lock-time", "time", "lt"],
        usage="<lock(s)...> [--global|--standard]",
        description="Toggle a lock's auto-unlock timer.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def toggle_locktime(self, ctx: commands.Context, *args: str):
        locks, global_flag, standard_flag = split_scope_flags(args)
        if await self._flag_error(ctx, locks, global_flag, standard_flag, allow_standard=True):
            return
        if not locks and not standard_flag:
            return await send_usage(ctx, title="Missing arg: `lock`")

        cog = await self._config_cog(ctx)
        if not cog:
            return

        if standard_flag:
            new_state = await cog.toggle_locktime(ctx.guild.id, STANDARD)
            std = await cog.get_standard(ctx.guild.id)
            state = f"**On ✅** (`{format_duration(std.get('locktime', 3600))}`)" if new_state else "**Off ❌**"
            return await ctx.reply(
                embed=ok_embed("Standard Lock Time Toggled", f"Auto-unlock: {state}", emoji="⌛"),
                mention_author=False,
            )

        cats, unknown = self._resolve_all(cog, locks)
        if not cats:
            return await ctx.reply(embed=err_embed(f"Unknown lock(s): {', '.join(unknown)}"), mention_author=False)

        scope = "whole server" if global_flag else ctx.channel.mention
        lines = []
        for cat in cats:
            if global_flag:
                new_state = await cog.toggle_locktime(ctx.guild.id, cat)
                cfg = await cog.get_category_config(ctx.guild.id, cat)
            else:
                new_state = await cog.toggle_locktime_channel(ctx.guild.id, ctx.channel.id, cat)
                cfg = await cog.get_category_config_channel(ctx.guild.id, ctx.channel.id, cat)
            state = f"**On ✅** (`{format_duration(cfg.get('locktime', 3600))}`)" if new_state else "**Off ❌**"
            lines.append(f"⌛ **{cog.display_name(cat)}** auto-unlock: {state} ({scope})")

        embed = ok_embed("Lock Time Toggled", "\n".join(lines)[:4096], emoji="⌛")
        self._unknown_field(embed, unknown)
        await ctx.reply(embed=embed, mention_author=False)

    # --- .toggle restrictunlockers <lock...> -----------------------------------

    @toggle_group.command(
        name="restrictunlockers",
        aliases=["restuls", "restrict-unlockers", "restrict", "ru"],
        usage="<lock...> [--global]",
        description="Only pinged users can unlock (res/sh/cl/tp/rp).",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def toggle_restrictunlockers(self, ctx: commands.Context, *locks_and_flags: str):
        locks, global_flag, standard_flag = split_scope_flags(locks_and_flags)
        if await self._flag_error(ctx, locks, global_flag, standard_flag, allow_standard=False):
            return
        if not locks:
            return await send_usage(ctx, title="Missing arg: `lock`")

        cog = await self._config_cog(ctx)
        if not cog:
            return

        cats, unknown = self._resolve_all(cog, locks)
        if not cats:
            return await ctx.reply(embed=err_embed(f"Unknown lock(s): {', '.join(unknown)}"), mention_author=False)

        scope = "whole server" if global_flag else ctx.channel.mention
        lines = []
        changed = 0
        for cat in cats:
            if not cog.can_restrict(cat):
                lines.append(f"⚠️ **{cog.display_name(cat)}** can't be restricted.")
                continue
            if global_flag:
                new_state = await cog.toggle_restrict(ctx.guild.id, cat)
            else:
                new_state = await cog.toggle_restrict_channel(ctx.guild.id, ctx.channel.id, cat)

            state = "**On ✅**" if new_state else "**Off ❌**"
            lines.append(f"🔐 **{cog.display_name(cat)}** restrict: {state} ({scope})")
            changed += 1

        description = "\n".join(lines)[:4096]
        if changed:
            embed = ok_embed("Restrict Toggled", description, emoji="🔐")
        else:
            embed = warn_embed("Nothing Changed", description)
        self._unknown_field(embed, unknown)
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(Toggle(bot))