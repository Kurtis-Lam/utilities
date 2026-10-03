"""Shared UI building blocks for every view in the bot.

Everything visual lives here so the whole bot stays consistent:

* ``EMBED_COLOR``   – the one brand color (#0414c7) used by every embed.
* ``themed()``      – force that color onto an embed built elsewhere (cogs, config modules).
* ``*_embed()``     – small ready-made embeds for success / error / warning / info replies
  (errors are one line: the message is the title).
* ``BaseView``      – owner-only check + auto-disable on timeout + safe page swapping.
* ``ConfirmView``   – red Confirm / grey Cancel prompt, no emojis (same API as before).
"""
from __future__ import annotations

import re

import discord

# --- Theme ---------------------------------------------------------------------

EMBED_COLOR = discord.Color(0x0414C7)


def make_embed(
    title: str | None = None,
    description: str | None = None,
    *,
    footer: str | None = None,
    **kwargs,
) -> discord.Embed:
    """Create an embed that already uses the bot color."""
    embed = discord.Embed(title=title, description=description, color=EMBED_COLOR, **kwargs)
    if footer:
        embed.set_footer(text=footer)
    return embed


def themed(embed: discord.Embed | None) -> discord.Embed | None:
    """Apply the bot color to an embed that was built somewhere else."""
    if embed is not None:
        embed.color = EMBED_COLOR
    return embed


_MENTION = re.compile(r"<(?:@[!&]?|#)\d+>")


def one_line_embed(text: str, emoji: str = "", color: discord.Color = EMBED_COLOR) -> discord.Embed:
    """Embed that is a single line: the message is the *title*.

    Discord titles can't render mentions, bold or code, so markdown is stripped.
    Text with mentions or line breaks falls back to a one-line description
    (which does render them)."""
    prefix = f"{emoji} " if emoji else ""
    if _MENTION.search(text) or "\n" in text:
        return discord.Embed(description=f"{prefix}{text}", color=color)
    plain = text.replace("**", "").replace("`", "")
    return discord.Embed(title=f"{prefix}{plain}"[:256], color=color)


def success_embed(text: str) -> discord.Embed:
    return make_embed(description=f"✅ {text}")


def error_embed(text: str) -> discord.Embed:
    return one_line_embed(text, "❌")


def warning_embed(text: str, title: str | None = None) -> discord.Embed:
    return make_embed(title=title, description=f"⚠️ {text}")


def info_embed(text: str) -> discord.Embed:
    return make_embed(description=f"ℹ️ {text}")


# --- Input parsing helpers -----------------------------------------------------

def parse_indices(raw: str) -> list[int]:
    """'1, 2 3' -> [1, 2, 3]. Keeps order, drops duplicates and anything < 1."""
    seen: list[int] = []
    for token in re.findall(r"\d+", raw or ""):
        value = int(token)
        if value >= 1 and value not in seen:
            seen.append(value)
    return seen


def extract_id(raw: str) -> str | None:
    """Pull a Discord ID out of '123…', '<#123…>' or '<@123…>'."""
    match = re.search(r"\d{15,25}", raw or "")
    return match.group(0) if match else None


# --- Base view -----------------------------------------------------------------

class BaseView(discord.ui.View):
    """View with an optional owner lock and clean timeout behaviour.

    * ``author_id``  – if set, only that user may press the controls.
    * ``message``    – the message the view is attached to. It is filled in
      automatically on the first interaction (or set it yourself after sending).
      When the view times out the controls are greyed out instead of silently
      dying, which avoids the confusing "This interaction failed" message.
    * ``swap()``     – replace the current page with a new embed + view and make
      sure the old view's timeout can never overwrite the new page.
    """

    def __init__(self, *, author_id: int | None = None, timeout: float | None = 180):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.message: discord.Message | None = None
        self.superseded = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.author_id is not None and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                embed=warning_embed("Not your menu."),
                ephemeral=True,
            )
            return False
        if self.message is None:
            self.message = interaction.message
        return True

    def disable_all(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]

    async def on_timeout(self) -> None:
        if self.superseded or self.message is None:
            return
        self.disable_all()
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass

    async def swap(
        self,
        interaction: discord.Interaction,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> None:
        """Replace this page (embed + view) in place."""
        self.superseded = True
        if isinstance(view, BaseView):
            view.message = interaction.message
        await interaction.response.edit_message(embed=themed(embed), view=view)


# --- Confirm prompt ------------------------------------------------------------

class ConfirmView(BaseView):
    """Confirm / Cancel prompt. ``await view.wait()`` then read ``view.value``
    (True = confirmed, False = cancelled, None = timed out)."""

    def __init__(
        self,
        author: discord.Member | discord.User,
        *,
        confirm_label: str = "Confirm",
        cancel_label: str = "Cancel",
        timeout: float = 30.0,
    ):
        super().__init__(author_id=author.id, timeout=timeout)
        self.author = author
        self.value: bool | None = None
        self.confirm_button.label = confirm_label
        self.cancel_button.label = cancel_label

    async def _finish(self, interaction: discord.Interaction, value: bool) -> None:
        self.value = value
        self.disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, False)

    async def on_timeout(self) -> None:
        self.value = None
        await super().on_timeout()