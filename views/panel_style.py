"""A clean, light layout for the public panels (ticket panel, verification panel).

Short bold title, a short description in normal text, an optional line of small print and the buttons
underneath, with a server icon on the right when there is one. Long descriptions are trimmed to the
first few lines so a panel never turns into a wall of text.
"""
from __future__ import annotations

import discord

from views.common_views import EMBED_COLOR

MAX_DESCRIPTION_CHARS = 400
MAX_DESCRIPTION_LINES = 4


def _short(text: str) -> str:
    lines = [line.rstrip() for line in (text or "").strip().splitlines() if line.strip()]
    text = "\n".join(lines[:MAX_DESCRIPTION_LINES])
    if len(text) > MAX_DESCRIPTION_CHARS:
        text = text[: MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"
    return text


def panel_container(
    title: str,
    description: str = "",
    *rows: discord.ui.Item,
    small: str = "",
    extra: str = "",
    thumbnail: str | None = None,
    color: discord.Color | int | None = None,
) -> discord.ui.Container:
    container = discord.ui.Container(accent_colour=color or EMBED_COLOR)
    head = f"## {title}" if title else ""
    body = _short(description)
    text = "\n".join(part for part in (head, body) if part) or "\u200b"
    if thumbnail:
        container.add_item(discord.ui.Section(discord.ui.TextDisplay(text), accessory=discord.ui.Thumbnail(thumbnail)))
    else:
        container.add_item(discord.ui.TextDisplay(text))
    if extra:
        container.add_item(discord.ui.TextDisplay(extra[:1500]))
    if small:
        container.add_item(discord.ui.TextDisplay(f"-# {small}"))
    if rows:
        container.add_item(discord.ui.Separator(visible=False))
        for row in rows:
            container.add_item(row)
    return container