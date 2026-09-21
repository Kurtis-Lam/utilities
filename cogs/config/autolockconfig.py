import re

import discord
from discord.ext import commands

from views.autolockview import (
    AutoLockMainView,
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    CATEGORY_DESCRIPTIONS,
    ROLE_CATEGORIES,
    ROLE_COMMAND_HINTS,
    RESTRICT_CATEGORIES,
)
from .base import config_group

DEFAULT_DELAY = 10

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
        "delay": DEFAULT_DELAY,
        "delay_enabled": True,  # True = wait `delay` seconds, False = lock immediately
        "whitelist": [],
        "restrict_unlockers": False,
    }


def _delay_text(cfg: dict) -> str:
    if cfg.get("delay_enabled", True):
        return f"`{cfg.get('delay', DEFAULT_DELAY)}s`"
    return "`Off` (locks instantly)"


@config_group.command(
    name="autolock",
    aliases=["a", "al"],
    description="Configure autolock behavior for res/sh/cl/tp/rp and rare/regional/gmax/paradox/eevos locks.",
)
@commands.has_permissions(administrator=True)
async def autolockconfig(ctx: commands.Context):
    cog = ctx.bot.get_cog("AutoLockConfig")

    if cog is None:
        return await ctx.send("⚠️ Internal error: `AutoLockConfig` cog is not loaded.")

    embed = await cog.build_main_embed(ctx.guild)
    view = AutoLockMainView(cog, guild_id=ctx.guild.id, author_id=ctx.author.id)
    await ctx.send(embed=embed, view=view)


@autolockconfig.error
async def autolockconfig_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.MissingPermissions):
        pass
    else:
        await ctx.send(f"⚠️ Internal error: `{error}`")


class AutoLockConfig(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Shared Mongo client from main.py (no connection string lives in this file).
    @property
    def mongo_client(self):
        return self.bot.mongo_client

    @property
    def db(self):
        return self.bot.mongo_client["utilities"]

    @property
    def collection(self):
        return self.db["autolock"]

    @property
    def pings_collection(self):
        return self.db["pings"]

    # --- category resolution (used by .toggle / .set) --------------------------

    @staticmethod
    def resolve_category(token: str) -> str | None:
        """'shlock' / 'sh-lock' / 'shinyhunt' / 'res' -> 'sh' / 'sh' / 'sh' / 're'. None if unknown."""
        t = re.sub(r"[\s_\-]+", "", token.lower())
        if t in _ALIAS_LOOKUP:
            return _ALIAS_LOOKUP[t]
        if t.endswith("lock"):
            return _ALIAS_LOOKUP.get(t[:-4])
        return None

    @staticmethod
    def display_name(category: str) -> str:
        return CATEGORY_NAMES.get(category, category.capitalize())

    @staticmethod
    def can_restrict(category: str) -> bool:
        return category in UNLOCK_RESTRICTABLE

    @staticmethod
    def all_categories() -> tuple:
        return ALL_CATEGORIES

    # --- storage helpers (autolock settings) ----------------------------------

    async def get_guild_config(self, guild_id: int) -> dict:
        gid = str(guild_id)
        doc = await self.collection.find_one({"_id": gid})

        if not doc:
            doc = {"_id": gid, **{cat: _default_category() for cat in ALL_CATEGORIES}}
            await self.collection.insert_one(doc)
            return doc

        needs_update = False
        for cat in ALL_CATEGORIES:
            if not isinstance(doc.get(cat), dict):
                doc[cat] = _default_category()
                needs_update = True
            else:
                for key, val in _default_category().items():
                    if key not in doc[cat]:
                        doc[cat][key] = val
                        needs_update = True
                if doc[cat].pop("role", None) is not None:
                    needs_update = True

        if needs_update:
            await self.collection.update_one(
                {"_id": gid}, {"$set": {cat: doc[cat] for cat in ALL_CATEGORIES}}, upsert=True
            )

        return doc

    async def get_category_config(self, guild_id: int, category: str) -> dict:
        doc = await self.get_guild_config(guild_id)
        return doc.get(category, _default_category())

    async def toggle_lock(self, guild_id: int, category: str) -> bool:
        cfg = await self.get_category_config(guild_id, category)
        new_val = not cfg.get("enabled", False)
        await self.collection.update_one(
            {"_id": str(guild_id)},
            {"$set": {f"{category}.enabled": new_val}},
            upsert=True,
        )
        return new_val

    async def set_delay(self, guild_id: int, category: str, delay: int):
        await self.get_guild_config(guild_id)
        await self.collection.update_one(
            {"_id": str(guild_id)}, {"$set": {f"{category}.delay": delay}}, upsert=True
        )

    async def toggle_delay(self, guild_id: int, category: str) -> bool:
        """True = the lock waits `delay` seconds first. False = it locks immediately."""
        cfg = await self.get_category_config(guild_id, category)
        new_val = not cfg.get("delay_enabled", True)
        await self.collection.update_one(
            {"_id": str(guild_id)},
            {"$set": {f"{category}.delay_enabled": new_val}},
            upsert=True,
        )
        return new_val

    async def toggle_restrict(self, guild_id: int, category: str) -> bool:
        cfg = await self.get_category_config(guild_id, category)
        new_val = not cfg.get("restrict_unlockers", False)
        await self.collection.update_one(
            {"_id": str(guild_id)},
            {"$set": {f"{category}.restrict_unlockers": new_val}},
            upsert=True,
        )
        return new_val

    async def add_whitelist(self, guild: discord.Guild | int, category: str, raw_input: str):
        if isinstance(guild, int):
            guild_obj = self.bot.get_guild(guild)
        else:
            guild_obj = guild

        if not guild_obj:
            return

        cfg = await self.get_category_config(guild_obj.id, category)
        current = cfg.get("whitelist", [])

        items = [i.strip() for i in str(raw_input).split(",") if i.strip()]
        updated = False

        for item in items:
            if item == "*":
                if "*" not in current:
                    current.append("*")
                    updated = True
                continue

            clean_id = re.sub(r"\D", "", item)
            target_channel = None

            if clean_id:
                target_channel = guild_obj.get_channel(int(clean_id))

            if target_channel:
                if target_channel.id not in current:
                    current.append(target_channel.id)
                    updated = True
                continue

            search_name = item.lower().lstrip("#")

            matched_channel = discord.utils.find(
                lambda c: c.name.lower() == search_name,
                guild_obj.channels
            )

            if matched_channel and matched_channel.id not in current:
                current.append(matched_channel.id)
                updated = True

        if updated:
            await self.collection.update_one(
                {"_id": str(guild_obj.id)},
                {"$set": {f"{category}.whitelist": current}},
                upsert=True,
            )

    async def remove_whitelist(self, guild: discord.Guild, category: str, raw_input: str):
        guild_id = guild.id
        cfg = await self.get_category_config(guild_id, category)
        current = cfg.get("whitelist", [])

        if not current:
            return

        items = [i.strip() for i in str(raw_input).split(",") if i.strip()]
        ordered = self._get_ordered_whitelist(guild, current)
        to_remove = set()
        clear_all = False

        for item in items:
            if item == "*":
                clear_all = True
                break

            if item.isdigit():
                val = int(item)
                if 1 <= val <= len(ordered):
                    idx_target = ordered[val - 1]
                    to_remove.add(idx_target)
                    to_remove.add(str(idx_target))

                to_remove.add(val)
                to_remove.add(str(val))

            clean_id = re.sub(r"\D", "", item)
            if clean_id:
                to_remove.add(int(clean_id))
                to_remove.add(clean_id)

            search_name = item.lower().lstrip("#")
            matched_channel = discord.utils.find(
                lambda c: c.name.lower() == search_name,
                guild.channels
            )
            if matched_channel:
                to_remove.add(matched_channel.id)
                to_remove.add(str(matched_channel.id))

        if clear_all:
            new_whitelist = []
        else:
            new_whitelist = [
                x for x in current
                if x not in to_remove and str(x) not in to_remove
            ]

        await self.collection.update_one(
            {"_id": str(guild_id)},
            {"$set": {f"{category}.whitelist": new_whitelist}},
            upsert=True,
        )

    # --- reading roles from PokePings (never written here) --------------------
    # Roles are written by the PokePings cog (see .set rarerole etc. in autolockset.py).

    async def get_ping_role(self, guild: discord.Guild, category: str) -> discord.Role | None:
        doc = await self.pings_collection.find_one({"_id": str(guild.id)})
        role_id = (doc or {}).get("roles", {}).get(category)
        if not role_id:
            return None
        try:
            return guild.get_role(int(role_id))
        except (TypeError, ValueError):
            return None

    # --- embed builders & ordering helpers ------------------------------------

    def _get_ordered_whitelist(self, guild: discord.Guild, whitelist: list) -> list:
        if not whitelist:
            return []

        categories = []
        channels = []
        others = []

        for item in whitelist:
            if str(item) == "*":
                others.append("*")
                continue
            try:
                wid = int(item)
            except (ValueError, TypeError):
                others.append(item)
                continue

            ch = guild.get_channel(wid)
            if ch is None:
                others.append(wid)
            elif isinstance(ch, discord.CategoryChannel):
                categories.append((ch.position, ch.name, wid))
            else:
                channels.append((ch.position, ch.name, wid))

        categories.sort(key=lambda x: (x[0], x[1]))
        channels.sort(key=lambda x: (x[0], x[1]))

        return [cat[2] for cat in categories] + [ch[2] for ch in channels] + others

    def _format_whitelist(self, guild: discord.Guild, whitelist: list) -> str:
        if not whitelist:
            return "None (no whitelist channels configured)"

        ordered = self._get_ordered_whitelist(guild, whitelist)
        lines = []

        for idx, item in enumerate(ordered, start=1):
            if str(item) == "*":
                lines.append(f"`{idx}.` 🌐 Whole Server (`*`)")
                continue

            ch = guild.get_channel(item) if isinstance(item, int) or str(item).isdigit() else None
            if ch is None:
                lines.append(f"`{idx}.` `{item}` (unknown/deleted)")
            elif isinstance(ch, discord.CategoryChannel):
                lines.append(f"`{idx}.` 📁 {ch.name}")
            else:
                lines.append(f"`{idx}.` {ch.mention}")

        return "\n".join(lines)

    async def build_main_embed(self, guild: discord.Guild) -> discord.Embed:
        doc = await self.get_guild_config(guild.id)

        embed = discord.Embed(
            title=f"🔒 AutoLock Configuration — {guild.name}",
            description="Select a category below to configure its lock behavior.",
            color=discord.Color.blurple(),
        )

        for cat in ALL_CATEGORIES:
            cfg = doc.get(cat, _default_category())
            is_enabled = cfg.get("enabled", False)
            status_icon = "✅" if is_enabled else "❌"
            wl_count = len(cfg.get("whitelist", []))

            lines = [
                f"Delay: {_delay_text(cfg)}",
                f"Whitelist: `{wl_count}` entr{'y' if wl_count == 1 else 'ies'}",
            ]
            if cat in ROLE_CATEGORIES:
                role = await self.get_ping_role(guild, cat)
                lines.append(f"Role: {role.mention if role else 'Not set'}")
            if cat in UNLOCK_RESTRICTABLE:
                lines.append(f"Restrict Unlockers: `{cfg.get('restrict_unlockers', False)}`")

            embed.add_field(name=f"{_label(cat)} {status_icon}", value="\n".join(lines), inline=True)

        return embed

    async def build_category_embed(self, guild: discord.Guild, category: str) -> discord.Embed:
        cfg = await self.get_category_config(guild.id, category)
        is_enabled = cfg.get("enabled", False)
        status_str = "Enabled ✅" if is_enabled else "Disabled ❌"
        delay_on = cfg.get("delay_enabled", True)

        embed = discord.Embed(
            title=f"⚙️ {_label(category)} Settings — {guild.name}",
            color=discord.Color.blurple(),
        )

        if category in CATEGORY_DESCRIPTIONS:
            embed.description = CATEGORY_DESCRIPTIONS[category]
        elif category == "re":
            embed.description = (
                "Locks the channel when a Pokémon someone has reserved spawns. "
                "Highest unlock priority (Reserves > Shiny Hunt > Collection > everything else)."
            )

        embed.add_field(name="Lock Status", value=f"`{status_str}`", inline=False)
        embed.add_field(
            name="Lock Delay",
            value=(
                f"`{cfg.get('delay', DEFAULT_DELAY)}` seconds — "
                + ("On ✅ (waits before locking)" if delay_on else "Off ❌ (locks immediately)")
            ),
            inline=False,
        )
        embed.add_field(
            name="Whitelist",
            value=self._format_whitelist(guild, cfg.get("whitelist", [])),
            inline=False,
        )

        if category in ROLE_CATEGORIES:
            role = await self.get_ping_role(guild, category)
            embed.add_field(
                name="Ping Role (read-only here)",
                value=(
                    f"{role.mention if role else 'Not set'}\n"
                    f"-# Set with {ROLE_COMMAND_HINTS[category]}, not here."
                ),
                inline=False,
            )

        if category in UNLOCK_RESTRICTABLE:
            embed.add_field(
                name="Restrict Unlockers",
                value=(
                    f"`{cfg.get('restrict_unlockers', False)}` — when enabled, only the user(s) "
                    "pinged for this lock may unlock the channel (server admins can always unlock). "
                    "If several restricted locks trigger at once, Reserves > Shiny Hunt > Collection > others."
                ),
                inline=False,
            )

        return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoLockConfig(bot))