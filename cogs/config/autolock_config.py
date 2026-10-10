import re

import discord
from discord.ext import commands

from views.autolock_views import (
    build_main_page,
    CATEGORY_LABELS,
    ALL_CATEGORIES,
    CATEGORY_DESCRIPTIONS,
    ROLE_CATEGORIES,
    ROLE_COMMAND_HINTS,
    RESTRICT_CATEGORIES,
)
from cogs.poketwo_helper.lockcommon import (
    DEFAULT_DELAY,
    DEFAULT_LOCKTIME,
    format_duration,
    resolve_category as _resolve_category,
    display_name as _display_name,
)
from views.common_views import error_embed
from views.embeds import handle_command_error
from .baseconfigs import config_group


# Pseudo-category used to store the server's "standard" settings. It lives in the same
# guild document and has the same field names as a real category (delay, delay_enabled,
# locktime, locktime_enabled, whitelist), so every setter below works on it unchanged.
STANDARD = "standard"


def _default_category(category: str) -> dict:
    return {
        "enabled": False,
        "delay": DEFAULT_DELAY,
        "delay_enabled": True,  # True = wait `delay` seconds, False = lock immediately
        "locktime": DEFAULT_LOCKTIME,   # seconds the channel stays locked before auto-unlock
        "locktime_enabled": False,      # True = auto-unlock after `locktime`, False = stay locked
        "whitelist": [],
        "restrict_unlockers": category in RESTRICT_CATEGORIES,
    }


def _default_standard() -> dict:
    return {
        "delay": DEFAULT_DELAY,
        "delay_enabled": True,
        "locktime": DEFAULT_LOCKTIME,
        "locktime_enabled": False,
        "whitelist": [],
    }


# What "use standard" can copy onto a lock.
STANDARD_PARTS = ("delay", "locktime", "whitelist")


def _label(category: str) -> str:
    return CATEGORY_LABELS.get(category, category.capitalize())


@config_group.command(
    name="autolock",
    aliases=["al"],
    description="Configure autolocks.",
)
@commands.has_permissions(administrator=True)
async def autolockconfig(ctx: commands.Context):
    cog = ctx.bot.get_cog("AutoLockConfig")

    if cog is None:
        return await ctx.reply(embed=error_embed("AutoLockConfig is not loaded."), mention_author=False)

    # build_main_page reads every category so the buttons are green (on) / grey (off).
    view = await build_main_page(cog, ctx.guild, ctx.guild.id, ctx.author.id)
    view.message = await ctx.reply(view=view, mention_author=False)


@autolockconfig.error
async def autolockconfig_error(ctx: commands.Context, error: Exception):
    await handle_command_error(ctx, error)


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
        return _resolve_category(token)

    @staticmethod
    def display_name(category: str) -> str:
        return _display_name(category)

    @staticmethod
    def can_restrict(category: str) -> bool:
        return category in RESTRICT_CATEGORIES

    @staticmethod
    def all_categories() -> tuple:
        return ALL_CATEGORIES

    # --- storage helpers (guild-wide / "--global" autolock settings) -----------

    async def get_guild_config(self, guild_id: int) -> dict:
        gid = str(guild_id)
        doc = await self.collection.find_one({"_id": gid})

        if not doc:
            doc = {
                "_id": gid,
                **{cat: _default_category(cat) for cat in ALL_CATEGORIES},
                STANDARD: _default_standard(),
            }
            await self.collection.insert_one(doc)
            return doc

        needs_update = False
        if not isinstance(doc.get(STANDARD), dict):
            doc[STANDARD] = _default_standard()
            await self.collection.update_one({"_id": gid}, {"$set": {STANDARD: doc[STANDARD]}}, upsert=True)
        else:
            missing = {k: v for k, v in _default_standard().items() if k not in doc[STANDARD]}
            if missing:
                doc[STANDARD].update(missing)
                await self.collection.update_one(
                    {"_id": gid}, {"$set": {f"{STANDARD}.{k}": v for k, v in missing.items()}}, upsert=True
                )

        for cat in ALL_CATEGORIES:
            if not isinstance(doc.get(cat), dict):
                doc[cat] = _default_category(cat)
                needs_update = True
            else:
                for key, val in _default_category(cat).items():
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
        """Guild-wide config of a lock. ``category="standard"`` returns the standard settings."""
        doc = await self.get_guild_config(guild_id)
        if category == STANDARD:
            return doc.get(STANDARD, _default_standard())
        return doc.get(category, _default_category(category))

    async def get_standard(self, guild_id: int) -> dict:
        return await self.get_category_config(guild_id, STANDARD)

    async def get_naming_enabled(self, guild_id: int, channel_id: int | None = None) -> bool:
        """Return whether Pokémon naming is enabled, applying a channel override if present."""
        doc = await self.get_guild_config(guild_id)
        enabled = doc.get("naming_enabled", True)
        if channel_id is not None:
            channels = doc.get("channels")
            channel_config = channels.get(str(channel_id)) if isinstance(channels, dict) else None
            if isinstance(channel_config, dict) and isinstance(channel_config.get("naming_enabled"), bool):
                enabled = channel_config["naming_enabled"]
        return bool(enabled)

    async def toggle_naming(self, guild_id: int) -> bool:
        """Toggle the server-wide Pokémon naming default."""
        doc = await self.get_guild_config(guild_id)
        new_value = not doc.get("naming_enabled", True)
        await self.collection.update_one(
            {"_id": str(guild_id)},
            {"$set": {"naming_enabled": new_value}},
            upsert=True,
        )
        return new_value

    async def toggle_naming_channel(self, guild_id: int, channel_id: int) -> bool:
        """Toggle Pokémon naming for a channel, overriding the server default."""
        new_value = not await self.get_naming_enabled(guild_id, channel_id)
        await self.collection.update_one(
            {"_id": str(guild_id)},
            {"$set": {f"channels.{channel_id}.naming_enabled": new_value}},
            upsert=True,
        )
        return new_value

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

    async def set_locktime(self, guild_id: int, category: str, seconds: int):
        """Set how long the channel stays locked. Also switches the auto-unlock timer on."""
        await self.get_guild_config(guild_id)
        await self.collection.update_one(
            {"_id": str(guild_id)},
            {"$set": {f"{category}.locktime": seconds, f"{category}.locktime_enabled": True}},
            upsert=True,
        )

    async def set_locktime_enabled(self, guild_id: int, category: str, value: bool):
        await self.get_guild_config(guild_id)
        await self.collection.update_one(
            {"_id": str(guild_id)}, {"$set": {f"{category}.locktime_enabled": bool(value)}}, upsert=True
        )

    async def toggle_locktime(self, guild_id: int, category: str) -> bool:
        """True = the channel auto-unlocks after `locktime`. False = it stays locked."""
        cfg = await self.get_category_config(guild_id, category)
        new_val = not cfg.get("locktime_enabled", False)
        await self.set_locktime_enabled(guild_id, category, new_val)
        return new_val

    async def set_delay_enabled(self, guild_id: int, category: str, value: bool):
        await self.get_guild_config(guild_id)
        await self.collection.update_one(
            {"_id": str(guild_id)}, {"$set": {f"{category}.delay_enabled": bool(value)}}, upsert=True
        )

    async def toggle_restrict(self, guild_id: int, category: str) -> bool:
        cfg = await self.get_category_config(guild_id, category)
        new_val = not cfg.get("restrict_unlockers", False)
        await self.collection.update_one(
            {"_id": str(guild_id)},
            {"$set": {f"{category}.restrict_unlockers": new_val}},
            upsert=True,
        )
        return new_val

    # --- storage helpers (per-channel overrides, i.e. non "--global" commands) -
    #
    # `.set` / `.toggle` without `--global` should only affect the channel the
    # command was run in, without touching (or requiring) the guild-wide default.
    # Overrides live under doc["channels"][str(channel_id)][category] and only
    # ever contain the specific keys that were overridden; anything not present
    # there falls back to the guild-wide category config. Whitelist is not
    # overridable per channel — it's always guild-wide, same as before.

    async def get_channel_overrides(self, guild_id: int, channel_id: int) -> dict:
        """Raw override dict for one channel, e.g. {"sh": {"enabled": True}}. {} if none set."""
        doc = await self.get_guild_config(guild_id)
        channels = doc.get("channels")
        if not isinstance(channels, dict):
            return {}
        entry = channels.get(str(channel_id))
        return entry if isinstance(entry, dict) else {}

    async def get_category_config_channel(self, guild_id: int, channel_id: int, category: str) -> dict:
        """Effective config for `category` in this channel: guild default + channel override."""
        base = dict(await self.get_category_config(guild_id, category))
        overrides = await self.get_channel_overrides(guild_id, channel_id)
        cat_override = overrides.get(category)
        if isinstance(cat_override, dict):
            base.update(cat_override)
        return base

    # Alias kept for readability at call sites that just want "what should
    # actually happen in this channel right now" (e.g. the AutoLock cog).
    async def get_effective_category_config(self, guild_id: int, channel_id: int, category: str) -> dict:
        return await self.get_category_config_channel(guild_id, channel_id, category)

    async def _set_channel_field(self, guild_id: int, channel_id: int, category: str, field: str, value):
        await self.get_guild_config(guild_id)  # make sure the guild doc exists first
        await self.collection.update_one(
            {"_id": str(guild_id)},
            {"$set": {f"channels.{channel_id}.{category}.{field}": value}},
            upsert=True,
        )

    async def set_delay_channel(self, guild_id: int, channel_id: int, category: str, delay: int):
        await self._set_channel_field(guild_id, channel_id, category, "delay", delay)

    async def set_locktime_channel(self, guild_id: int, channel_id: int, category: str, seconds: int):
        await self._set_channel_field(guild_id, channel_id, category, "locktime", seconds)
        await self._set_channel_field(guild_id, channel_id, category, "locktime_enabled", True)

    async def set_locktime_enabled_channel(self, guild_id: int, channel_id: int, category: str, value: bool):
        await self._set_channel_field(guild_id, channel_id, category, "locktime_enabled", bool(value))

    async def toggle_locktime_channel(self, guild_id: int, channel_id: int, category: str) -> bool:
        cfg = await self.get_category_config_channel(guild_id, channel_id, category)
        new_val = not cfg.get("locktime_enabled", False)
        await self._set_channel_field(guild_id, channel_id, category, "locktime_enabled", new_val)
        return new_val

    async def toggle_lock_channel(self, guild_id: int, channel_id: int, category: str) -> bool:
        cfg = await self.get_category_config_channel(guild_id, channel_id, category)
        new_val = not cfg.get("enabled", False)
        await self._set_channel_field(guild_id, channel_id, category, "enabled", new_val)
        return new_val

    async def toggle_delay_channel(self, guild_id: int, channel_id: int, category: str) -> bool:
        cfg = await self.get_category_config_channel(guild_id, channel_id, category)
        new_val = not cfg.get("delay_enabled", True)
        await self._set_channel_field(guild_id, channel_id, category, "delay_enabled", new_val)
        return new_val

    async def toggle_restrict_channel(self, guild_id: int, channel_id: int, category: str) -> bool:
        cfg = await self.get_category_config_channel(guild_id, channel_id, category)
        new_val = not cfg.get("restrict_unlockers", False)
        await self._set_channel_field(guild_id, channel_id, category, "restrict_unlockers", new_val)
        return new_val

    @staticmethod
    def split_items(raw_input) -> list[str]:
        """'#a, #b 123' -> ['#a', '#b', '123'] (commas and/or spaces both separate items)."""
        return [i for i in re.split(r"[,\s]+", str(raw_input)) if i]

    def format_whitelist_item(self, guild: discord.Guild, item) -> str:
        if str(item) == "*":
            return "🌐 Whole Server (`*`)"
        ch = guild.get_channel(int(item)) if str(item).isdigit() else None
        if ch is None:
            return f"`{item}` (deleted)"
        if isinstance(ch, discord.CategoryChannel):
            return f"📁 {ch.name}"
        return ch.mention

    async def add_whitelist(self, guild: discord.Guild | int, category: str, raw_input: str):
        """Add channels / categories / `*` to a lock's whitelist (``category="standard"`` works too).

        Returns ``(added, already, invalid)`` — lists of whitelist entries (ids or `*`) and
        of the raw inputs that couldn't be resolved.
        """
        guild_obj = self.bot.get_guild(guild) if isinstance(guild, int) else guild
        if not guild_obj:
            return [], [], []

        cfg = await self.get_category_config(guild_obj.id, category)
        current = list(cfg.get("whitelist", []))

        added, already, invalid = [], [], []

        for item in self.split_items(raw_input):
            if item == "*":
                if "*" in current:
                    already.append("*")
                else:
                    current.append("*")
                    added.append("*")
                continue

            clean_id = re.sub(r"\D", "", item)
            target_channel = guild_obj.get_channel(int(clean_id)) if clean_id else None

            if target_channel is None:
                search_name = item.lower().lstrip("#")
                target_channel = discord.utils.find(
                    lambda c: c.name.lower() == search_name, guild_obj.channels
                )

            if target_channel is None:
                invalid.append(item)
            elif target_channel.id in current:
                already.append(target_channel.id)
            else:
                current.append(target_channel.id)
                added.append(target_channel.id)

        if added:
            await self.collection.update_one(
                {"_id": str(guild_obj.id)},
                {"$set": {f"{category}.whitelist": current}},
                upsert=True,
            )
        return added, already, invalid

    async def remove_whitelist(self, guild: discord.Guild, category: str, raw_input: str):
        """Remove entries by channel mention / id / name / list number / `*` (= everything).

        Returns ``(removed, not_found)`` — removed whitelist entries and unmatched raw inputs.
        """
        guild_id = guild.id
        cfg = await self.get_category_config(guild_id, category)
        current = list(cfg.get("whitelist", []))

        if not current:
            return [], self.split_items(raw_input)

        ordered = self._get_ordered_whitelist(guild, current)
        to_remove: set = set()
        not_found: list[str] = []
        clear_all = False

        for item in self.split_items(raw_input):
            if item == "*":
                clear_all = True
                break

            targets: set = set()

            if item.isdigit():
                val = int(item)
                if 1 <= val <= len(ordered):
                    targets.add(ordered[val - 1])
                    targets.add(str(ordered[val - 1]))
                targets.add(val)
                targets.add(str(val))

            clean_id = re.sub(r"\D", "", item)
            if clean_id:
                targets.add(int(clean_id))
                targets.add(clean_id)

            search_name = item.lower().lstrip("#")
            matched_channel = discord.utils.find(
                lambda c: c.name.lower() == search_name, guild.channels
            )
            if matched_channel:
                targets.add(matched_channel.id)
                targets.add(str(matched_channel.id))

            if any(x in targets or str(x) in targets for x in current):
                to_remove |= targets
            else:
                not_found.append(item)

        if clear_all:
            removed, new_whitelist = list(current), []
        else:
            removed = [x for x in current if x in to_remove or str(x) in to_remove]
            new_whitelist = [x for x in current if x not in removed]

        if removed:
            await self.collection.update_one(
                {"_id": str(guild_id)},
                {"$set": {f"{category}.whitelist": new_whitelist}},
                upsert=True,
            )
        return removed, not_found

    async def clear_whitelist(self, guild_id: int, category: str) -> int:
        """Empty a lock's whitelist. Returns how many entries were removed."""
        cfg = await self.get_category_config(guild_id, category)
        count = len(cfg.get("whitelist", []))
        await self.collection.update_one(
            {"_id": str(guild_id)},
            {"$set": {f"{category}.whitelist": []}},
            upsert=True,
        )
        return count

    # --- "standard" settings ----------------------------------------------------
    #
    # The server keeps one set of standard values (delay, locktime, whitelist). A lock's
    # page / `.set use-standard` can copy them onto that lock in one go.

    async def use_standard(self, guild_id: int, category: str, parts=STANDARD_PARTS) -> dict:
        """Copy the standard ``parts`` onto a lock (guild level). Returns what was applied."""
        std = await self.get_standard(guild_id)
        fields: dict = {}
        applied: dict = {}

        if "delay" in parts:
            fields[f"{category}.delay"] = std.get("delay", DEFAULT_DELAY)
            fields[f"{category}.delay_enabled"] = std.get("delay_enabled", True)
            applied["delay"] = (std.get("delay", DEFAULT_DELAY), std.get("delay_enabled", True))
        if "locktime" in parts:
            fields[f"{category}.locktime"] = std.get("locktime", DEFAULT_LOCKTIME)
            fields[f"{category}.locktime_enabled"] = std.get("locktime_enabled", False)
            applied["locktime"] = (std.get("locktime", DEFAULT_LOCKTIME), std.get("locktime_enabled", False))
        if "whitelist" in parts:
            fields[f"{category}.whitelist"] = list(std.get("whitelist", []))
            applied["whitelist"] = list(std.get("whitelist", []))

        if fields:
            await self.get_guild_config(guild_id)
            await self.collection.update_one({"_id": str(guild_id)}, {"$set": fields}, upsert=True)
        return applied

    async def get_min_locktime(self, guild_id: int) -> int | None:
        """Shortest auto-unlock time (seconds) any enabled lock in this server can use,
        counting channel overrides. None when no lock auto-unlocks (LockDM uses this to
        decide whether "DM before unlock" can be offered, and how long it may be)."""
        doc = await self.get_guild_config(guild_id)
        channels = doc.get("channels") if isinstance(doc.get("channels"), dict) else {}
        values = []
        for cat in ALL_CATEGORIES:
            base = doc.get(cat, _default_category(cat))
            variants = [base]
            for entry in channels.values():
                override = entry.get(cat) if isinstance(entry, dict) else None
                if isinstance(override, dict) and override:
                    variants.append({**base, **override})
            for cfg in variants:
                if cfg.get("enabled") and cfg.get("locktime_enabled") and cfg.get("locktime"):
                    values.append(int(cfg["locktime"]))
        return min(values) if values else None

    # --- reading roles from PokePings (never written here) --------------------
    # Roles are written by the PokePings cog (see .set rarerole etc. in set.py).

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
            return "None"

        ordered = self._get_ordered_whitelist(guild, whitelist)
        lines = []

        for idx, item in enumerate(ordered, start=1):
            if str(item) == "*":
                lines.append(f"`{idx}.` 🌐 Whole Server (`*`)")
                continue

            ch = guild.get_channel(item) if isinstance(item, int) or str(item).isdigit() else None
            if ch is None:
                lines.append(f"`{idx}.` `{item}` (deleted)")
            elif isinstance(ch, discord.CategoryChannel):
                lines.append(f"`{idx}.` 📁 {ch.name}")
            else:
                lines.append(f"`{idx}.` {ch.mention}")

        return "\n".join(lines)

    def _count_channel_overrides(self, doc: dict, category: str) -> int:
        """How many channels have at least one overridden field for this category."""
        channels = doc.get("channels")
        if not isinstance(channels, dict):
            return 0
        return sum(
            1
            for entry in channels.values()
            if isinstance(entry, dict) and isinstance(entry.get(category), dict) and entry[category]
        )

    async def build_main_embed(self, guild: discord.Guild) -> discord.Embed:
        """Landing page: one line per lock, on or off. Details live inside each lock's page."""
        doc = await self.get_guild_config(guild.id)

        lines = []
        for cat in ALL_CATEGORIES:
            cfg = doc.get(cat, _default_category(cat))
            is_enabled = cfg.get("enabled", False)
            lines.append(f"{'✅' if is_enabled else '❌'} **{_label(cat)}** — {'On' if is_enabled else 'Off'}")

        std = doc.get(STANDARD, _default_standard())
        return discord.Embed(
            title=f"🔒 AutoLock — {guild.name}",
            description=(
                "Pick a lock to see its settings.\n\n"
                + "\n".join(lines)
                + f"\n\n⭐ **Standard** — {self._standard_summary(std)}"
            ),
            color=discord.Color.blurple(),
        )

    @staticmethod
    def _standard_summary(std: dict) -> str:
        delay = f"`{std.get('delay', DEFAULT_DELAY)}s`" + ("" if std.get("delay_enabled", True) else " (off)")
        locktime = f"`{format_duration(std.get('locktime', DEFAULT_LOCKTIME))}`" + (
            "" if std.get("locktime_enabled", False) else " (off)"
        )
        return f"Delay {delay} • Lock time {locktime} • Whitelist `{len(std.get('whitelist', []))}`"

    async def build_standard_embed(self, guild: discord.Guild) -> discord.Embed:
        std = await self.get_standard(guild.id)
        delay_on = std.get("delay_enabled", True)
        locktime_on = std.get("locktime_enabled", False)

        embed = discord.Embed(
            title=f"⭐ Standard Settings — {guild.name}",
            description=(
                "Your server's standard values. Press **Use Standard** on a lock's page "
                "(or run `.set use-standard <lock>`) to copy them onto that lock."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Standard Lock Delay",
            value=f"`{std.get('delay', DEFAULT_DELAY)}s` — " + ("On ✅" if delay_on else "Off ❌"),
            inline=False,
        )
        embed.add_field(
            name="Standard Lock Time (auto-unlock)",
            value=f"`{format_duration(std.get('locktime', DEFAULT_LOCKTIME))}` — "
            + ("On ✅" if locktime_on else "Off ❌"),
            inline=False,
        )
        embed.add_field(
            name="Standard Whitelist",
            value=self._format_whitelist(guild, std.get("whitelist", [])),
            inline=False,
        )
        return embed

    async def build_category_embed(self, guild: discord.Guild, category: str) -> discord.Embed:
        doc = await self.get_guild_config(guild.id)
        cfg = doc.get(category, _default_category(category))
        is_enabled = cfg.get("enabled", False)
        status_str = "Enabled ✅" if is_enabled else "Disabled ❌"
        delay_on = cfg.get("delay_enabled", True)

        embed = discord.Embed(
            title=f"⚙️ {_label(category)} — {guild.name}",
            color=discord.Color.blurple(),
        )

        if category in CATEGORY_DESCRIPTIONS:
            embed.description = CATEGORY_DESCRIPTIONS[category]

        embed.add_field(name="Lock Status", value=f"`{status_str}`", inline=False)
        embed.add_field(
            name="Lock Delay",
            value=f"`{cfg.get('delay', DEFAULT_DELAY)}s` — " + ("On ✅" if delay_on else "Off ❌"),
            inline=False,
        )
        locktime_on = cfg.get("locktime_enabled", False)
        embed.add_field(
            name="Lock Time (auto-unlock)",
            value=f"`{format_duration(cfg.get('locktime', DEFAULT_LOCKTIME))}` — "
            + ("On ✅" if locktime_on else "Off ❌"),
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
                name="Ping Role",
                value=(
                    f"{role.mention if role else 'Not set'}\n"
                    f"-# Set with {ROLE_COMMAND_HINTS[category]}"
                ),
                inline=False,
            )
            embed.add_field(
                name="Who Can Unlock",
                value="Anyone (role ping).",
                inline=False,
            )

        if category in RESTRICT_CATEGORIES:
            embed.add_field(
                name="Restrict Unlockers",
                value=f"`{cfg.get('restrict_unlockers', True)}` — only pinged user(s) can unlock (admins always can).",
                inline=False,
            )

        override_count = self._count_channel_overrides(doc, category)
        embed.add_field(
            name="Overrides",
            value=f"`{override_count}` channel(s)." if override_count else "None.",
            inline=False,
        )
        embed.add_field(
            name="Standard",
            value=self._standard_summary(doc.get(STANDARD, _default_standard())),
            inline=False,
        )

        return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoLockConfig(bot))