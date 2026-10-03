import datetime
import re
import time

import discord

from views.common import one_line_embed

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
    if lock_doc.get("source") == "manual":
        return True
    allowed = lock_doc.get("allowed_users")
    if allowed is None:
        return True
    return member.id in allowed or member.guild_permissions.administrator


def unlock_denied_message(lock_doc: dict) -> str:
    allowed = lock_doc.get("allowed_users") or []
    mentions = ", ".join(f"<@{uid}>" for uid in allowed)
    return f"⚠️ Only {mentions} or an admin can unlock."


# --- Embed styling shared by .lock / .unlock / .uac / .lockstats and AutoLock ---

LOCK_COLOR = discord.Color.red()
UNLOCK_COLOR = discord.Color.green()
WARN_COLOR = discord.Color.gold()


def now_unix() -> int:
    return int(time.time())


def to_unix(value) -> int | None:
    """Mongo datetime (naive means UTC) -> unix seconds. None if missing or invalid."""
    if not isinstance(value, datetime.datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return int(value.timestamp())


def format_elapsed(seconds: float) -> str:
    """12.4 -> '12.4s', 125 -> '2m 5s', 3725 -> '1h 2m 5s'."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f}s"
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def is_locked_overwrite(overwrite: discord.PermissionOverwrite) -> bool:
    """A channel counts as locked for Poketwo when it can't view or can't send."""
    return overwrite.send_messages is False or overwrite.view_channel is False


def stamp(unix: int) -> str:
    """Live relative time, e.g. '2 minutes ago'."""
    return f"<t:{unix}:R>"


def who_can_unlock_text(allowed_users) -> str:
    if allowed_users is None:
        return "Anyone (`.u` or the button)."
    if not allowed_users:
        return "Server admins only."
    mentions = ", ".join(f"<@{uid}>" for uid in allowed_users)
    return f"{mentions} or an admin."


def _categories_label(categories) -> str:
    return "/".join(SHORT_NAMES.get(c, c) for c in (categories or []))


def locked_embed(
    channel,
    *,
    locked_by=None,
    trigger: str | None = None,
    allowed_users=None,
    restricted_by=None,
    when: int | None = None,
) -> discord.Embed:
    when = when or now_unix()
    embed = discord.Embed(
        title="🔒 Channel Locked",
        color=LOCK_COLOR,
    )
    embed.add_field(name="🕒 Locked At", value=stamp(when), inline=True)
    if locked_by is not None:
        embed.add_field(name="👤 Locked By", value=locked_by.mention, inline=True)
    elif trigger:
        embed.add_field(name="🤖 Triggered By", value=f"Auto-lock (`{trigger}`)", inline=True)

    text = who_can_unlock_text(allowed_users)
    if restricted_by:
        text += f"\n-# Restricted by `{_categories_label(restricted_by)}`."
    embed.add_field(name="🔐 Who Can Unlock", value=text, inline=False)
    return embed


def unlocked_embed(channel, *, unlocked_by, locked_at: int | None = None, when: int | None = None) -> discord.Embed:
    when = when or now_unix()
    embed = discord.Embed(
        title="🔓 Channel Unlocked",
        color=UNLOCK_COLOR,
    )
    embed.add_field(name="🕒 Unlocked At", value=stamp(when), inline=True)
    embed.add_field(name="👤 Unlocked By", value=unlocked_by.mention, inline=True)
    return embed


def already_locked_embed(channel, lock_doc: dict | None = None) -> discord.Embed:
    embed = discord.Embed(
        title="🔒 Already Locked",
        description=channel.mention,
        color=WARN_COLOR,
    )
    if lock_doc:
        locked_at = to_unix(lock_doc.get("locked_at"))
        if locked_at:
            embed.add_field(name="🕒 Locked At", value=stamp(locked_at), inline=True)
        if lock_doc.get("locked_by"):
            embed.add_field(name="👤 Locked By", value=f"<@{lock_doc['locked_by']}>", inline=True)
        elif lock_doc.get("source") == "autolock":
            embed.add_field(
                name="🤖 Triggered By",
                value=f"Auto-lock (`{_categories_label(lock_doc.get('categories'))}`)",
                inline=True,
            )
        embed.add_field(name="🔐 Who Can Unlock", value=who_can_unlock_text(lock_doc.get("allowed_users")), inline=False)
    return embed


def already_unlocked_embed(channel) -> discord.Embed:
    return discord.Embed(
        title="🔓 Already Unlocked",
        description=channel.mention,
        color=WARN_COLOR,
    )


def unlock_denied_embed(channel, lock_doc: dict) -> discord.Embed:
    """One-line error: who is allowed to unlock."""
    allowed = lock_doc.get("allowed_users")
    if not allowed:
        text = "Only admins can unlock."
    else:
        text = "Only " + ", ".join(f"<@{uid}>" for uid in allowed) + " or an admin can unlock."
    return one_line_embed(text, "⛔", LOCK_COLOR)


# --- Field helpers that stay inside Discord's 1024-char field limit ---

def chunk_items(items: list[str], sep: str = " ", limit: int = 1000, max_chunks: int = 2):
    """Join items into <= limit-char chunks. Returns (chunks, hidden_count)."""
    chunks, current, length, used = [], [], 0, 0
    for item in items:
        extra = len(item) + len(sep)
        if current and length + extra > limit:
            chunks.append(sep.join(current))
            current, length = [], 0
            if len(chunks) >= max_chunks:
                break
        current.append(item)
        length += extra
        used += 1
    else:
        if current:
            chunks.append(sep.join(current))
    return chunks, len(items) - used


def add_item_fields(embed: discord.Embed, name: str, items: list[str], sep: str = " ", max_chunks: int = 2) -> None:
    if not items:
        return
    chunks, hidden = chunk_items(items, sep=sep, max_chunks=max_chunks)
    for index, chunk in enumerate(chunks, start=1):
        title = name if len(chunks) == 1 else f"{name} (part {index})"
        if hidden and index == len(chunks):
            chunk += f"\n…and {hidden} more"
        embed.add_field(name=title, value=chunk, inline=False)