import discord
from discord.ext import commands

USAGE = (
    "⚠️ **Usage**\n"
    "`.toggle <lock>` — turn a lock on/off **for the current channel**\n"
    "`.toggle lockdelay <lock> [<lock> ...]` — delay on (waits) / off (locks immediately) **for the current channel**\n"
    "`.toggle restrictunlockers <lock> [<lock> ...]` — only the pinged user(s) can unlock on/off "
    "(`res`, `sh`, `cl`, `tp`, `rp` only) **for the current channel**\n\n"
    "**Global flag:** Append `--global` to apply the setting to the whole server instead of just the current channel.\n"
    "Examples:\n"
    "• `.toggle shlock --global` — toggle Shiny Hunt lock globally\n"
    "• `.toggle lockdelay sh cl --global` — toggle lock delay globally for Shiny and Collection\n"
    "• `.toggle restrictunlockers res --global` — restrict unlockers globally\n\n"
    "**Locks:** `reslock`, `shlock`, `cllock`, `tplock`, `rplock`, `ralock`, `reglock`, "
    "`gmaxlock`, `paralock`, `eevoslock`\n"
    "-# Long names work too, e.g. `shinyhuntlock`, `collectionlock`, `typepingslock`, `regionpingslock`, "
    "`rarelock`, `regionallock`, `gigantamaxlock`, `paradoxlock`, `eeveelutionslock`, `reserveslock`."
)


class Toggle(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _config_cog(self, ctx: commands.Context):
        cog = self.bot.get_cog("AutoLockConfig")
        if not cog:
            await ctx.send("⚠️ Internal error: `AutoLockConfig` cog is not loaded.")
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

    @commands.group(name="toggle", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def toggle_group(self, ctx: commands.Context, lock: str = None, *rest: str):
        if lock is None:
            return await ctx.send(USAGE)

        # Check for --global flag in the remaining arguments
        global_flag = "--global" in rest
        if global_flag:
            rest = tuple(a for a in rest if a != "--global")
            # re-parse: lock is the first arg, rest are additional locks
            locks = [lock] + list(rest)
        else:
            locks = [lock] + list(rest)

        cog = await self._config_cog(ctx)
        if not cog:
            return

        # Resolve all categories
        cats, unknown = [], []
        for tok in locks:
            cat = cog.resolve_category(tok)
            if cat is None:
                unknown.append(tok)
            elif cat not in cats:
                cats.append(cat)

        if not cats and not unknown:
            return await ctx.send(f"⚠️ Unknown lock(s): {', '.join(f'`{u}`' for u in locks)}")

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

        if unknown:
            lines.append(f"⚠️ Unknown lock(s): {', '.join(f'`{u}`' for u in unknown)}")

        await ctx.send("\n".join(lines))

    # --- .toggle lockdelay <lock...> -------------------------------------------

    @toggle_group.command(name="lockdelay", aliases=["ld", "delay", "lock-delay"])
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
            return await ctx.send("⚠️ Usage: `.toggle lockdelay <lock> [<lock> ...]` [--global] (e.g. `.toggle lockdelay sh cl` or `.toggle lockdelay sh cl --global`)")

        cog = await self._config_cog(ctx)
        if not cog:
            return

        cats, unknown = self._resolve_all(cog, locks)
        lines = []
        for cat in cats:
            if global_flag:
                # Toggle delay for whole server (guild level)
                new_state = await cog.toggle_delay(ctx.guild.id, cat)
            else:
                # Toggle delay for current channel only
                new_state = await cog.toggle_delay_channel(ctx.guild.id, ctx.channel.id, cat)
            
            if global_flag:
                cfg = await cog.get_category_config(ctx.guild.id, cat)
            else:
                cfg = await cog.get_category_config_channel(ctx.guild.id, ctx.channel.id, cat)
            
            if new_state:
                state = f"**On ✅** (waits `{cfg.get('delay', 10)}s` before locking)"
            else:
                state = "**Off ❌** (locks immediately)"
            scope = "whole server" if global_flag else ctx.channel.mention
            lines.append(f"⏱️ Lock delay for **{cog.display_name(cat)}** is now {state} in {scope}.")

        if unknown:
            lines.append(f"⚠️ Unknown lock(s): {', '.join(f'`{u}`' for u in unknown)}")

        await ctx.send("\n".join(lines))

    # --- .toggle restrictunlockers <lock...> -----------------------------------

    @toggle_group.command(
        name="restrictunlockers",
        aliases=["restuls", "restrict-unlockers", "restrict", "ru"],
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
            return await ctx.send(
                "⚠️ Usage: `.toggle restrictunlockers <lock> [<lock> ...]` [--global] (e.g. `.toggle restuls res sh` or `.toggle restuls res sh --global`)"
            )

        cog = await self._config_cog(ctx)
        if not cog:
            return

        cats, unknown = self._resolve_all(cog, locks)
        lines = []
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

        if unknown:
            lines.append(f"⚠️ Unknown lock(s): {', '.join(f'`{u}`' for u in unknown)}")

        await ctx.send("\n".join(lines))


async def setup(bot: commands.Bot):
    await bot.add_cog(Toggle(bot))
