import discord
from discord.ext import commands

USAGE = (
    "⚠️ **Usage**\n"
    "`.toggle <lock>` — turn a lock on/off\n"
    "`.toggle lockdelay <lock> [<lock> ...]` — delay on (waits) / off (locks immediately)\n"
    "`.toggle restrictunlockers <lock> [<lock> ...]` — only the pinged user(s) can unlock on/off "
    "(`res`, `sh`, `cl`, `tp`, `rp` only)\n\n"
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
    async def toggle_group(self, ctx: commands.Context, lock: str = None, *_):
        if lock is None:
            return await ctx.send(USAGE)

        cog = await self._config_cog(ctx)
        if not cog:
            return

        category = cog.resolve_category(lock)
        if category is None:
            return await ctx.send(f"⚠️ Unknown lock `{lock}`.\n\n{USAGE}")

        new_state = await cog.toggle_lock(ctx.guild.id, category)
        status = "Enabled ✅" if new_state else "Disabled ❌"
        await ctx.send(f"🔒 **{cog.display_name(category)}** is now **{status}**.")

    # --- .toggle lockdelay <lock...> -------------------------------------------

    @toggle_group.command(name="lockdelay", aliases=["ld", "delay", "lock-delay"])
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def toggle_lockdelay(self, ctx: commands.Context, *locks: str):
        if not locks:
            return await ctx.send("⚠️ Usage: `.toggle lockdelay <lock> [<lock> ...]` (e.g. `.toggle lockdelay sh cl`)")

        cog = await self._config_cog(ctx)
        if not cog:
            return

        cats, unknown = self._resolve_all(cog, locks)
        lines = []
        for cat in cats:
            new_state = await cog.toggle_delay(ctx.guild.id, cat)
            cfg = await cog.get_category_config(ctx.guild.id, cat)
            if new_state:
                state = f"**On ✅** (waits `{cfg.get('delay', 10)}s` before locking)"
            else:
                state = "**Off ❌** (locks immediately)"
            lines.append(f"⏱️ Lock delay for **{cog.display_name(cat)}** is now {state}.")

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
    async def toggle_restrictunlockers(self, ctx: commands.Context, *locks: str):
        if not locks:
            return await ctx.send(
                "⚠️ Usage: `.toggle restrictunlockers <lock> [<lock> ...]` (e.g. `.toggle restuls res sh`)"
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
            new_state = await cog.toggle_restrict(ctx.guild.id, cat)
            state = "**Enabled ✅** (only the pinged user(s) can unlock)" if new_state else "**Disabled ❌** (anyone can unlock)"
            lines.append(f"🔐 Restrict Unlockers for **{cog.display_name(cat)}** is now {state}.")

        if unknown:
            lines.append(f"⚠️ Unknown lock(s): {', '.join(f'`{u}`' for u in unknown)}")

        await ctx.send("\n".join(lines))


async def setup(bot: commands.Bot):
    await bot.add_cog(Toggle(bot))