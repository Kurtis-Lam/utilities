import discord
from discord.ext import commands

CATEGORY_ALIASES = {
    # Full & Short Aliases mapped to database category keys
    "rare": "rare", "ra": "rare", "rare-lock": "rare", "ra-lock": "rare",
    "regional": "regional", "reg": "regional", "regional-lock": "regional", "reg-lock": "regional",
    "gigantamax": "gigantamax", "gmax": "gigantamax", "gigantamax-lock": "gigantamax", "gmax-lock": "gigantamax",
    "paradox": "paradox", "para": "paradox", "paradox-lock": "paradox", "para-lock": "paradox",
    "eevos": "eevos", "eevos-lock": "eevos",
    "shinyhunt": "shinyhunt", "sh": "shinyhunt", "shinyhunt-lock": "shinyhunt", "sh-lock": "shinyhunt",
    "collection": "collection", "cl": "collection", "collection-lock": "collection", "cl-lock": "collection",
    "regionping": "regionping", "rp": "regionping", "regionping-lock": "regionping", "rp-lock": "regionping",
    "typeping": "typeping", "tp": "typeping", "typeping-lock": "typeping", "tp-lock": "typeping",
}

CATEGORY_NAMES = {
    "rare": "Rare Lock",
    "regional": "Regional Lock",
    "gigantamax": "Gigantamax Lock",
    "paradox": "Paradox Lock",
    "eevos": "Eevos Lock",
    "shinyhunt": "Shiny Hunt Lock",
    "collection": "Collection Lock",
    "regionping": "Region Ping Lock",
    "typeping": "Type Ping Lock",
}


class Toggle(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.group(name="toggle", invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    async def toggle_group(self, ctx: commands.Context):
        await ctx.send("⚠️ Usage: `.toggle <lock-name>` or `.toggle restrict-unlockers <category>`")

    @toggle_group.command(name="rare-lock", aliases=["ra-lock"])
    @commands.has_permissions(administrator=True)
    async def toggle_rare(self, ctx: commands.Context):
        await self._handle_toggle(ctx, "rare")

    @toggle_group.command(name="regional-lock", aliases=["reg-lock"])
    @commands.has_permissions(administrator=True)
    async def toggle_regional(self, ctx: commands.Context):
        await self._handle_toggle(ctx, "regional")

    @toggle_group.command(name="gigantamax-lock", aliases=["gmax-lock"])
    @commands.has_permissions(administrator=True)
    async def toggle_gmax(self, ctx: commands.Context):
        await self._handle_toggle(ctx, "gigantamax")

    @toggle_group.command(name="paradox-lock", aliases=["para-lock"])
    @commands.has_permissions(administrator=True)
    async def toggle_paradox(self, ctx: commands.Context):
        await self._handle_toggle(ctx, "paradox")

    @toggle_group.command(name="eevos-lock")
    @commands.has_permissions(administrator=True)
    async def toggle_eevos(self, ctx: commands.Context):
        await self._handle_toggle(ctx, "eevos")

    @toggle_group.command(name="shinyhunt-lock", aliases=["sh-lock"])
    @commands.has_permissions(administrator=True)
    async def toggle_shinyhunt(self, ctx: commands.Context):
        await self._handle_toggle(ctx, "shinyhunt")

    @toggle_group.command(name="collection-lock", aliases=["cl-lock"])
    @commands.has_permissions(administrator=True)
    async def toggle_collection(self, ctx: commands.Context):
        await self._handle_toggle(ctx, "collection")

    @toggle_group.command(name="regionping-lock", aliases=["rp-lock"])
    @commands.has_permissions(administrator=True)
    async def toggle_regionping(self, ctx: commands.Context):
        await self._handle_toggle(ctx, "regionping")

    @toggle_group.command(name="typeping-lock", aliases=["tp-lock"])
    @commands.has_permissions(administrator=True)
    async def toggle_typeping(self, ctx: commands.Context):
        await self._handle_toggle(ctx, "typeping")

    @toggle_group.command(name="restrict-unlockers", aliases=["restrict", "ru"])
    @commands.has_permissions(administrator=True)
    async def toggle_restrict_unlockers(self, ctx: commands.Context, category: str):
        cat_key = CATEGORY_ALIASES.get(category.lower())
        if not cat_key:
            return await ctx.send(
                "⚠️ Invalid category. Valid options: `rare` (`ra`), `regional` (`reg`), `gigantamax` (`gmax`), "
                "`paradox` (`para`), `eevos`, `shinyhunt` (`sh`), `collection` (`cl`), `regionping` (`rp`), `typeping` (`tp`)."
            )

        cog = self.bot.get_cog("AutoLockConfig")
        if not cog:
            return await ctx.send("⚠️ Internal error: `AutoLockConfig` cog is not loaded.")

        new_state = await cog.toggle_restrict(ctx.guild.id, cat_key)
        display_name = CATEGORY_NAMES.get(cat_key, cat_key.capitalize())
        status_str = "Enabled ✅" if new_state else "Disabled ❌"
        await ctx.send(f"🔒 Restrict Unlockers for **{display_name}** is now **{status_str}**.")

    async def _handle_toggle(self, ctx: commands.Context, category: str):
        cog = self.bot.get_cog("AutoLockConfig")
        if not cog:
            return await ctx.send("⚠️ Internal error: `AutoLockConfig` cog is not loaded.")

        new_state = await cog.toggle_lock(ctx.guild.id, category)
        display_name = CATEGORY_NAMES.get(category, category.capitalize())
        status_str = "Enabled ✅" if new_state else "Disabled ❌"
        await ctx.send(f"🔒 **{display_name}** is now **{status_str}**.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Toggle(bot))