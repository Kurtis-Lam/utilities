import discord
from discord.ext import commands

from cogs.poketwo_helper.lockcommon import format_duration
from views.embeds import (
    err_embed,
    handle_command_error,
    ok_embed,
    send_usage,
    warn_embed,
)


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

    # --- .toggle <lock> --------------------------------------------------------
    # Unknown sub-command names fall through to this callback, so `.toggle shlock`,
    # `.toggle cllock`, `.toggle reslock`, ... are all handled here.

    @commands.group(
        name="toggle",
        invoke_without_command=True,
        usage="<lock...|naming> [--global]",
        description="Toggle locks, Pokémon naming, delays and restrictions.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def toggle_group(self, ctx: commands.Context, lock: str = None, *rest: str):
        if lock is None:
            return await send_usage(ctx)

        # Check for --global flag in the remaining arguments
        global_flag = any(a.lower() == "--global" for a in rest) or lock.lower() == "--global"
        rest = tuple(a for a in rest if a.lower() != "--global")
        locks = [lock] + list(rest) if lock.lower() != "--global" else list(rest)

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
        if naming_requested:
            naming_state = (
                await cog.toggle_naming(ctx.guild.id)
                if global_flag
                else await cog.toggle_naming_channel(ctx.guild.id, ctx.channel.id)
            )
            status = "Enabled ✅" if naming_state else "Disabled ❌"
            scope = "whole server" if global_flag else ctx.channel.mention
            lines = [f"✨ **Pokémon naming**: **{status}** ({scope})"]
        else:
            lines = []

        if not cats and not naming_requested:
            return await ctx.reply(embed=err_embed(f"Unknown lock(s): {', '.join(unknown)}"), mention_author=False)

        scope = "whole server" if global_flag else ctx.channel.mention

        for cat in cats:
            if global_flag:
                # Toggle for the whole server (guild level)
                new_state = await cog.toggle_lock(ctx.guild.id, cat)
            else:
                # Toggle for current channel only
                new_state = await cog.toggle_lock_channel(ctx.guild.id, ctx.channel.id, cat)

            status = "Enabled ✅" if new_state else "Disabled ❌"
            lines.append(f"🔒 **{cog.display_name(cat)}**: **{status}** ({scope})")

        title = "Settings Toggled" if naming_requested else "Lock Toggled"
        embed = ok_embed(title, "\n".join(lines)[:4096])
        if unknown:
            embed.add_field(
                name="⚠️ Unknown Lock(s)",
                value=", ".join(f"`{u}`" for u in unknown)[:1024],
                inline=False,
            )
        await ctx.reply(embed=embed, mention_author=False)

    # --- .toggle lockdelay <lock...> -------------------------------------------

    @toggle_group.command(
        name="lockdelay",
        aliases=["ld", "delay", "lock-delay"],
        usage="<lock...> [--global]",
        description="Toggle a lock's delay.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def toggle_lockdelay(self, ctx: commands.Context, *locks_and_flags: str):
        # Check for --global flag
        locks = []
        global_flag = False

        for arg in locks_and_flags:
            if arg.lower() == "--global":
                global_flag = True
            else:
                locks.append(arg)

        if not locks:
            return await send_usage(ctx, title="Missing arg: `lock`")

        cog = await self._config_cog(ctx)
        if not cog:
            return

        cats, unknown = self._resolve_all(cog, locks)
        if not cats:
            return await ctx.reply(embed=err_embed(f"Unknown lock(s): {', '.join(unknown)}"), mention_author=False)

        lines = []
        for cat in cats:
            if global_flag:
                # Toggle delay for whole server (guild level)
                new_state = await cog.toggle_delay(ctx.guild.id, cat)
                cfg = await cog.get_category_config(ctx.guild.id, cat)
            else:
                # Toggle delay for current channel only
                new_state = await cog.toggle_delay_channel(ctx.guild.id, ctx.channel.id, cat)
                cfg = await cog.get_category_config_channel(ctx.guild.id, ctx.channel.id, cat)

            state = f"**On ✅** (`{cfg.get('delay', 15)}s`)" if new_state else "**Off ❌**"
            scope = "whole server" if global_flag else ctx.channel.mention
            lines.append(f"⏱️ **{cog.display_name(cat)}** delay: {state} ({scope})")

        embed = ok_embed("Lock Delay Toggled", "\n".join(lines)[:4096], emoji="⏱️")
        if unknown:
            embed.add_field(
                name="⚠️ Unknown Lock(s)",
                value=", ".join(f"`{u}`" for u in unknown)[:1024],
                inline=False,
            )
        await ctx.reply(embed=embed, mention_author=False)

    # --- .toggle locktime <lock...> --------------------------------------------

    @toggle_group.command(
        name="locktime",
        aliases=["lt", "locktimer", "lock-time", "autounlock"],
        usage="<lock...> [--global]",
        description="Toggle a lock's auto-unlock timer.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def toggle_locktime(self, ctx: commands.Context, *locks_and_flags: str):
        locks = [a for a in locks_and_flags if a.lower() != "--global"]
        global_flag = len(locks) != len(locks_and_flags)

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
        if unknown:
            embed.add_field(name="⚠️ Unknown Lock(s)", value=", ".join(f"`{u}`" for u in unknown)[:1024], inline=False)
        await ctx.reply(embed=embed, mention_author=False)

    # --- .toggle standard-lockdelay / standard-locktimer -----------------------

    @toggle_group.command(
        name="standard-lockdelay",
        aliases=["standard-delay", "standard-ld"],
        description="Toggle the standard lock delay on/off.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def toggle_standard_lockdelay(self, ctx: commands.Context):
        cog = await self._config_cog(ctx)
        if not cog:
            return
        new_state = await cog.toggle_delay(ctx.guild.id, "standard")
        std = await cog.get_standard(ctx.guild.id)
        state = f"**On ✅** (`{std.get('delay', 15)}s`)" if new_state else "**Off ❌**"
        await ctx.reply(embed=ok_embed("Standard Lock Delay Toggled", f"Delay: {state}", emoji="⏱️"), mention_author=False)

    @toggle_group.command(
        name="standard-locktimer",
        aliases=["standard-timer", "standard-locktime"],
        description="Toggle the standard lock timer (auto-unlock) on/off.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def toggle_standard_locktimer(self, ctx: commands.Context):
        cog = await self._config_cog(ctx)
        if not cog:
            return
        new_state = await cog.toggle_locktime(ctx.guild.id, "standard")
        std = await cog.get_standard(ctx.guild.id)
        state = f"**On ✅** (`{format_duration(std.get('locktime', 3600))}`)" if new_state else "**Off ❌**"
        await ctx.reply(embed=ok_embed("Standard Lock Timer Toggled", f"Auto-unlock: {state}", emoji="⏳"), mention_author=False)

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
        # Check for --global flag
        locks = []
        global_flag = False

        for arg in locks_and_flags:
            if arg.lower() == "--global":
                global_flag = True
            else:
                locks.append(arg)

        if not locks:
            return await send_usage(ctx, title="Missing arg: `lock`")

        cog = await self._config_cog(ctx)
        if not cog:
            return

        cats, unknown = self._resolve_all(cog, locks)
        if not cats:
            return await ctx.reply(embed=err_embed(f"Unknown lock(s): {', '.join(unknown)}"), mention_author=False)

        lines = []
        changed = 0
        for cat in cats:
            if not cog.can_restrict(cat):
                lines.append(f"⚠️ **{cog.display_name(cat)}** can't be restricted.")
                continue
            if global_flag:
                # Toggle restrict for whole server (guild level)
                new_state = await cog.toggle_restrict(ctx.guild.id, cat)
            else:
                # Toggle restrict for current channel only
                new_state = await cog.toggle_restrict_channel(ctx.guild.id, ctx.channel.id, cat)

            state = "**On ✅**" if new_state else "**Off ❌**"
            scope = "whole server" if global_flag else ctx.channel.mention
            lines.append(f"🔐 **{cog.display_name(cat)}** restrict: {state} ({scope})")
            changed += 1

        description = "\n".join(lines)[:4096]
        if changed:
            embed = ok_embed("Restrict Toggled", description, emoji="🔐")
        else:
            embed = warn_embed("Nothing Changed", description)
        if unknown:
            embed.add_field(
                name="⚠️ Unknown Lock(s)",
                value=", ".join(f"`{u}`" for u in unknown)[:1024],
                inline=False,
            )
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(Toggle(bot))