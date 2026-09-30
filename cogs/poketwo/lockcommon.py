import re

import discord

POKETWO_ID = 716390085896962058

# Default number of seconds a delay-enabled lock waits before actually
# locking. Single source of truth so autolockconfig.py (which writes it)
# and autolock.py (which has a fallback read of it) never disagree.
DEFAULT_DELAY = 15

# Full display names, e.g. for embeds and confirmation messages.
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

# Short labels used in compact lock/status messages, e.g. "sh/cl".
SHORT_NAMES = {"re": "res"}

# category key -> accepted user-typed aliases (without a "lock" suffix;
# resolve_category() also strips a trailing "lock" before checking here).
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
_ALIAS_LOOKUP.update({cat: cat for cat in CATEGORY_ALIASES})


def resolve_category(token: str) -> str | None:
    t = re.sub(r"[\s_\-]+", "", token.lower())
    if t in _ALIAS_LOOKUP:
        return _ALIAS_LOOKUP[t]
    if t.endswith("lock"):
        return _ALIAS_LOOKUP.get(t[:-4])
    return None


def display_name(category: str) -> str:
    return CATEGORY_NAMES.get(category, category.capitalize())


# Who may unlock when several restricted locks fire on the same channel at
# once. Earlier tier wins; categories inside the same tuple are equal (their
# allowed-user sets are merged). Role-based locks (rare/regional/gmax/
# paradox/eevos) ping a role, not specific users, so they never appear here
# and can never restrict who's allowed to unlock.
UNLOCK_PRIORITY = (("re",), ("sh",), ("cl",), ("tp", "rp"))


async def get_poketwo_target(guild: discord.Guild):
    member = guild.get_member(POKETWO_ID)
    if member:
        return member
    try:
        return await guild.fetch_member(POKETWO_ID)
    except discord.HTTPException:
        return None


def can_unlock(lock_doc: dict | None, member: discord.Member) -> bool:
    if not lock_doc:
        return True
    allowed = lock_doc.get("allowed_users")
    if allowed is None:
        return True
    return member.id in allowed or member.guild_permissions.administrator


def unlock_denied_message(lock_doc: dict) -> str:
    allowed = lock_doc.get("allowed_users") or []
    mentions = ", ".join(f"<@{uid}>" for uid in allowed)
    return f"⚠️ Only {mentions} (or a server admin) can unlock this channel."