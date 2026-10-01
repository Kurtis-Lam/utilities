import discord
from discord.ext import commands

from views.embeds import (
    err_embed,
    handle_command_error,
    make_embed,
    ok_embed,
    send_usage,
    warn_embed,
)


def _toggle_usage_embed(p: str) -> discord.Embed:
    """'#0414c7' how-to-use embed for `.toggle` (shown when used with no arguments)."""
    embed = make_embed(
        "How to use `{0}toggle`".format(p),
        "Turn locks, lock delays and unlocker restrictions on/off.\n"
        "By default changes apply to **the current channel**.",
        emoji="📖",
    )
    embed.add_field(
        name="📝 Usage",
        value=(
            f"`{p}toggle <lock>` — turn a lock on/off\n"
            f"`{p}toggle lockdelay <lock> [<lock> ...]` — delay on (waits) / off (locks immediately)\n"
            f"`{p}toggle restrictunlockers <lock> [<lock> ...]` — only the pinged user(s) can unlock on/off "
            "(`res`, `sh`, `cl`, `tp`, `rp` only)"
        ),
        inline=False,
    )
    embed.add_field(
        name="🌐 Global Flag",
        value="Append `--global` at the end to apply the setting to the **whole server** instead of just the current channel.",
        inline=False,
    )
    embed.add_field(
        name="💡 Examples",
        value=(
            f"`{p}toggle shlock`\n"
            f"`{p}toggle shlock --global`\n"
            f"`{p}toggle lockdelay sh cl --global`\n"
            f"`{p}toggle restrictunlockers res --global`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔒 Locks",
        value=(
            "`reslock`, `shlock`, `cllock`, `tplock`, `rplock`, `ralock`, `reglock`, "
            "`gmaxlock`, `paralock`, `eevoslock`\n"
            "-# Long names work too, e.g. `shinyhuntlock`, `collectionlock`, `typepingslock`, `regionpingslock`, "
            "`rarelock`, `regionallock`, `gigantamaxlock`, `paradoxlock`, `eeveelutionslock`, `reserveslock`."
        ),
        inline=False,
    )
    embed.set_footer(text="<required>  [optional]")
    return embed


class Toggle(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_command_error(self, ctx: commands.Context, error):
        await handle_command_error(ctx, error)

    async def _config_cog(self, ctx: commands.Context):
        cog = self.bot.get_cog("AutoLockConfig")
        if not cog:
            await ctx.send(embed=err_embed(
                "Internal Error", "`AutoLockConfig` cog is not loaded.", emoji="⚠️"))
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
        usage="<lock...> [--global]",
        description="Turn locks, lock delays and unlocker restrictions on/off.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def toggle_group(self, ctx: commands.Context, lock: str = None, *rest: str):
        if lock is None:
            return await ctx.send(embed=_toggle_usage_embed(ctx.clean_prefix))

        # Check for --global flag in the remaining arguments
        global_flag = any(a.lower() == "--global" for a in rest) or lock.lower() == "--global"
        rest = tuple(a for a in rest if a.lower() != "--global")
        locks = [lock] + list(rest) if lock.lower() != "--global" else list(rest)

        if not locks:
            return await ctx.send(embed=_toggle_usage_embed(ctx.clean_prefix))

        cog = await self._config_cog(ctx)
        if not cog:
            return

        # Resolve all categories
        cats, unknown = self._resolve_all(cog, locks)

        if not cats:
            return await ctx.send(embed=err_embed(
                "Unknown Lock",
                f"Unknown lock(s): {', '.join(f'`{u}`' for u in unknown)}\n"
                f"-# Use `{ctx.clean_prefix}toggle` to see all valid locks."))

        scope = "whole server" if global_flag else ctx.channel.mention

        lines = []
        for cat in cats:
            if global_flag:
                # Toggle for the whole server (guild level)
                new_state = await cog.toggle_lock(ctx.guild.id, cat)
            else:
                # Toggle for current channel only
                new_state = await cog.toggle_lock_channel(ctx.guild.id, ctx.channel.id, cat)

            status = "Enabled ✅" if new_state else "Disabled ❌"
            lines.append(f"🔒 **{cog.display_name(cat)}** is now **{status}** for {scope}.")

        embed = ok_embed("Lock Toggled", "\n".join(lines)[:4096])
        if unknown:
            embed.add_field(
                name="⚠️ Unknown Lock(s)",
                value=", ".join(f"`{u}`" for u in unknown)[:1024],
                inline=False,
            )
        await ctx.send(embed=embed)

    # --- .toggle lockdelay <lock...> -------------------------------------------

    @toggle_group.command(
        name="lockdelay",
        aliases=["ld", "delay", "lock-delay"],
        usage="<lock...> [--global]",
        description="Turn the lock delay on (waits) or off (locks immediately).",
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
            return await send_usage(ctx, note="Missing required argument: `lock`")

        cog = await self._config_cog(ctx)
        if not cog:
            return

        cats, unknown = self._resolve_all(cog, locks)
        if not cats:
            return await ctx.send(embed=err_embed(
                "Unknown Lock", f"Unknown lock(s): {', '.join(f'`{u}`' for u in unknown)}"))

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

            if new_state:
                state = f"**On ✅** (waits `{cfg.get('delay', 15)}s` before locking)"
            else:
                state = "**Off ❌** (locks immediately)"
            scope = "whole server" if global_flag else ctx.channel.mention
            lines.append(f"⏱️ Lock delay for **{cog.display_name(cat)}** is now {state} in {scope}.")

        embed = ok_embed("Lock Delay Toggled", "\n".join(lines)[:4096], emoji="⏱️")
        if unknown:
            embed.add_field(
                name="⚠️ Unknown Lock(s)",
                value=", ".join(f"`{u}`" for u in unknown)[:1024],
                inline=False,
            )
        await ctx.send(embed=embed)

    # --- .toggle restrictunlockers <lock...> -----------------------------------

    @toggle_group.command(
        name="restrictunlockers",
        aliases=["restuls", "restrict-unlockers", "restrict", "ru"],
        usage="<lock...> [--global]",
        description="Restrict unlocking to only the pinged user(s) (res, sh, cl, tp, rp only).",
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
            return await send_usage(ctx, note="Missing required argument: `lock`")

        cog = await self._config_cog(ctx)
        if not cog:
            return

        cats, unknown = self._resolve_all(cog, locks)
        if not cats:
            return await ctx.send(embed=err_embed(
                "Unknown Lock", f"Unknown lock(s): {', '.join(f'`{u}`' for u in unknown)}"))

        lines = []
        changed = 0
        for cat in cats:
            if not cog.can_restrict(cat):
                lines.append(
                    f"⚠️ **{cog.display_name(cat)}** can't restrict unlockers "
                    "(it pings a role, not specific users)."
                )
                continue
            if global_flag:
                # Toggle restrict for whole server (guild level)
                new_state = await cog.toggle_restrict(ctx.guild.id, cat)
            else:
                # Toggle restrict for current channel only
                new_state = await cog.toggle_restrict_channel(ctx.guild.id, ctx.channel.id, cat)

            state = "**Enabled ✅** (only the pinged user(s) can unlock)" if new_state else "**Disabled ❌** (anyone can unlock)"
            scope = "whole server" if global_flag else ctx.channel.mention
            lines.append(f"🔐 Restrict Unlockers for **{cog.display_name(cat)}** is now {state} in {scope}.")
            changed += 1

        description = "\n".join(lines)[:4096]
        if changed:
            embed = ok_embed("Restrict Unlockers Toggled", description, emoji="🔐")
        else:
            embed = warn_embed("Nothing Changed", description)
        if unknown:
            embed.add_field(
                name="⚠️ Unknown Lock(s)",
                value=", ".join(f"`{u}`" for u in unknown)[:1024],
                inline=False,
            )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Toggle(bot))