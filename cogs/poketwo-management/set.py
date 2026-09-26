import re

import discord
from discord.ext import commands

from views.autolockview import (
    CATEGORY_DESCRIPTIONS,
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    RESTRICT_CATEGORIES,
    ROLE_CATEGORIES,
)

# --- category helpers ---------------------------------------------------------
# "re" (reserves) is the same key the PokePings cog and the Recognizer use.
# Everything falls back gracefully if views/autolockview.py doesn't know "re" yet.

_EXTRA_LABELS = {"re": "Reserves"}

# Reserves first, then whatever order the view already uses.
ALL_CATEGORIES = tuple(CATEGORY_ORDER) if "re" in CATEGORY_ORDER else ("re", *CATEGORY_ORDER)

# Locks that can restrict who may unlock (they ping specific users, not a role).
UNLOCK_RESTRICTABLE = frozenset(RESTRICT_CATEGORIES) | {"re"}

CATEGORY_NAMES = {
    "re": "Reserves Lock",
    "sh": "Shiny Hunt Lock",
    "cl": "Collection Lock",
    "tp": "Type Ping Lock",
    "rp": "Region Ping Lock",
    "rare": "Rare Lock",
    "regional": "Regional Lock",
    "gmax": "Gigantamax Lock",
    "paradox": "Paradox Lock",
    "eevos": "Eeveelutions Lock",
}

# category key -> accepted names (without the "lock" suffix; see resolve_category)
CATEGORY_ALIASES = {
    "re": ("res", "reserve", "reserves"),
    "sh": ("sh", "shiny", "shinyhunt", "shinyhunts"),
    "cl": ("cl", "collection", "collections"),
    "tp": ("tp", "typeping", "typepings", "type", "types"),
    "rp": ("rp", "regionping", "regionpings", "region", "regions"),
    "rare": ("ra", "rare"),
    "regional": ("reg", "regional"),
    "gmax": ("gmax", "gigantamax"),
    "paradox": ("para", "paradox"),
    "eevos": ("eevos", "eevo", "eeveelution", "eeveelutions", "eeveeevolutions"),
}

_ALIAS_LOOKUP = {alias: cat for cat, aliases in CATEGORY_ALIASES.items() for alias in aliases}
_ALIAS_LOOKUP.update({cat: cat for cat in CATEGORY_ALIASES if cat != "re"})


def _label(category: str) -> str:
    return CATEGORY_LABELS.get(category) or _EXTRA_LABELS.get(category) or category.capitalize()


def _default_category() -> dict:
    return {
        "enabled": False,
        "delay": 10,
        "delay_enabled": True,  # True = wait `delay` seconds, False = lock immediately
        "whitelist": [],
        "restrict_unlockers": False,
    }


class Set(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # --- group command ---------------------------------------------------------
    @commands.group(name="set", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_group(self, ctx: commands.Context):
        help_msg = (
            "⚙️ **Set Commands Help & Examples**\n\n"
            "**⏱️ Lock Delay**\n"
            "Set how many seconds to wait before auto-locking a category.\n"
            "• **One lock:** `.set lockdelay 15 sh`\n"
            "• **Multiple locks:** `.set lockdelay 15 sh cl tp`\n"
            "• **All locks at once:** `.set lockdelay 15 all`\n\n"
            "**🏷️ Category Ping Roles**\n"
            "Assign or check the ping role for specific categories. Mention a role to set it, or leave it blank to view current settings.\n"
            "• `.set rarerole @Rare Ping` *(Alias: `.set rarole`)*\n"
            "• `.set regionalrole @Regional Ping` *(Alias: `.set regrole`)*\n"
            "• `.set gigantamaxrole @GMax Ping` *(Alias: `.set gmaxrole`)*\n"
            "• `.set paradoxrole @Paradox Ping` *(Alias: `.set pararole`)*\n"
            "• `.set eeveelutionsrole @Eevee Ping` *(Alias: `.set eevosrole`)*"
        )
        await ctx.send(help_msg)

    # --- .set lockdelay <seconds> <lock...> ------------------------------------
    @set_group.command(name="lockdelay", aliases=["ld", "delay", "lock-delay"])
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_lockdelay(self, ctx: commands.Context, seconds: str = None, *locks: str):
        if seconds is None or not locks:
            return await ctx.send(
                "⚠️ **Usage:** `.set lockdelay <seconds> <lock>`\n\n"
                "**Examples:**\n"
                "• `.set lockdelay 15 sh` *(Sets Shiny Hunt delay to 15s)*\n"
                "• `.set lockdelay 15 sh cl tp` *(Sets Shiny, Collection, and Type Ping delays)*\n"
                "• `.set lockdelay 15 all` *(Sets all delays to 15s)*"
            )

        try:
            delay = int(seconds.lower().rstrip("s"))
        except ValueError:
            return await ctx.send(f"⚠️ `{seconds}` isn't a valid number of seconds.")

        if not (1 <= delay <= 600):
            return await ctx.send(
                f"⚠️ Delay must be between **1** and **600** seconds. To lock instantly, turn the delay off with `.toggle lockdelay <lock>`."
            )

        cog = self.bot.get_cog("AutoLockConfig")
        if not cog:
            return await ctx.send("⚠️ Internal error: `AutoLockConfig` cog is not loaded.")

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
            return await ctx.send(f"⚠️ Unknown lock(s): {', '.join(f'`{u}`' for u in unknown)}")

        cfg_doc = await cog.get_guild_config(ctx.guild.id)
        lines = []
        for cat in cats:
            await cog.set_delay(ctx.guild.id, cat, delay)
            line = f"⏱️ **{cog.display_name(cat)}** delay set to **{delay}s**."
            if not (cfg_doc.get(cat) or {}).get("delay_enabled", True):
                line += f" (Delay is currently **off**, so it still locks immediately. Turn it on with `.toggle lockdelay {cat}`.)"
            lines.append(line)

        if unknown:
            lines.append(f"⚠️ Unknown lock(s): {', '.join(f'`{u}`' for u in unknown)}")

        await ctx.send("\n".join(lines))

    # --- role setters ----------------------------------------------------------
    # Roles live in the PokePings data (utilities.pings -> roles.<key>), which is
    # what the Recognizer reads when it pings, so we write through PokePings.

    async def _set_role(self, ctx: commands.Context, key: str, label: str, role: discord.Role | None):
        pings = self.bot.get_cog("PokePings")
        if not pings:
            return await ctx.send("⚠️ Internal error: `PokePings` cog is not loaded.")

        g_id = str(ctx.guild.id)
        old_id = await pings.get_guild_role(g_id, key)

        if role is None:
            if old_id:
                return await ctx.send(
                    f"Current **{label}** role: <@&{old_id}>\n-# Set a new one with the command + a role mention.",
                    allowed_mentions=discord.AllowedMentions.none()
                )
            return await ctx.send(f"No role configured for **{label}**.", allowed_mentions=discord.AllowedMentions.none())

        if old_id and str(old_id) == str(role.id):
            return await ctx.send(f"**{label}** role is already {role.mention}.", allowed_mentions=discord.AllowedMentions.none())

        await pings.set_guild_role(g_id, key, str(role.id))

        if old_id:
            await ctx.send(
                f"✅ **{label}** role replaced: <@&{old_id}> → {role.mention}",
                allowed_mentions=discord.AllowedMentions.none()
            )
        else:
            await ctx.send(f"✅ **{label}** role set to {role.mention}", allowed_mentions=discord.AllowedMentions.none())

    @set_group.command(name="rarerole", aliases=["rarole"])
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_rarerole(self, ctx: commands.Context, role: discord.Role = None):
        await self._set_role(ctx, "rare", "Rare", role)

    @set_group.command(name="regionalrole", aliases=["regrole"])
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_regionalrole(self, ctx: commands.Context, role: discord.Role = None):
        await self._set_role(ctx, "regional", "Regional", role)

    @set_group.command(name="gigantamaxrole", aliases=["gmaxrole"])
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_gigantamaxrole(self, ctx: commands.Context, role: discord.Role = None):
        await self._set_role(ctx, "gmax", "Gigantamax", role)

    @set_group.command(name="paradoxrole", aliases=["pararole"])
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_paradoxrole(self, ctx: commands.Context, role: discord.Role = None):
        await self._set_role(ctx, "paradox", "Paradox", role)

    @set_group.command(name="eeveelutionsrole", aliases=["eevosroles", "eevosrole"])
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_eeveelutionsrole(self, ctx: commands.Context, role: discord.Role = None):
        await self._set_role(ctx, "eevos", "Eeveelutions", role)


async def setup(bot: commands.Bot):
    await bot.add_cog(Set(bot))