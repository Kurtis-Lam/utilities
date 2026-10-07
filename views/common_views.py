"""Shared UI building blocks for every view in the bot.

Everything visual lives here so the whole bot stays consistent:

* ``EMBED_COLOR``   – the one brand color (#0414c7) used by every embed.
* ``themed()``      – force that color onto an embed built elsewhere (cogs, config modules).
* ``*_embed()``     – small ready-made embeds for success / error / warning / info replies
  (errors are one line: the message is the title).
* ``BaseView``      – owner-only check + auto-disable on timeout + safe page swapping.
* ``ConfirmView``   – red Confirm / grey Cancel prompt, no emojis (same API as before).
* ``BaseLayout``    – owner-only check + auto-disable on timeout for Components V2 layouts.
* ``ConfirmLayout`` – Confirm / Cancel prompt with the buttons inside the embed-style container.
* ``confirm()``     – one-call helper around ``ConfirmLayout`` that returns True / False.
* ``container_from_embed()`` – turn an embed into a container, optionally with button rows inside.
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

# --- Components V2: buttons inside the embed-style container -----------------

def first_text(message: discord.Message | None) -> str:
    def walk(components) -> str:
        for comp in components or []:
            content = getattr(comp, "content", None)
            if isinstance(content, str) and content:
                return content
            found = walk(getattr(comp, "children", None))
            if found:
                return found
        return ""

    return walk(getattr(message, "components", None))


def container_from_embed(
    embed: discord.Embed,
    *rows: discord.ui.Item,
    color: discord.Color | int | None = None,
) -> discord.ui.Container:
    container = discord.ui.Container(accent_colour=color or embed.color or EMBED_COLOR)

    head: list[str] = []
    if embed.title:
        head.append(f"## {embed.title}")
    if embed.description:
        head.append(embed.description)
    body = [f"**{field.name}**\n{field.value}" for field in embed.fields]
    footer = f"-# {embed.footer.text}" if embed.footer and embed.footer.text else ""

    budget = 3900 - len(footer)
    texts: list[str] = []
    for text in ("\n".join(head), "\n\n".join(body)):
        if not text or budget <= 0:
            continue
        if len(text) > budget:
            text = text[: budget - 1] + "…"
        budget -= len(text)
        texts.append(text)
    if footer:
        texts.append(footer)
    if not texts:
        texts.append("\u200b")

    for text in texts:
        container.add_item(discord.ui.TextDisplay(text))
    if rows:
        container.add_item(discord.ui.Separator())
        for row in rows:
            container.add_item(row)
    return container


class BaseLayout(discord.ui.LayoutView):
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
        for child in self.walk_children():
            if hasattr(child, "disabled"):
                child.disabled = True  # type: ignore[attr-defined]

    async def on_timeout(self) -> None:
        if self.superseded or self.message is None:
            return
        self.disable_all()
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass

    async def swap(self, interaction: discord.Interaction, *, view: discord.ui.LayoutView) -> None:
        self.superseded = True
        if isinstance(view, BaseLayout):
            view.message = interaction.message
        await interaction.response.edit_message(view=view)


class EmbedLayout(BaseLayout):
    def __init__(
        self,
        embed: discord.Embed | None = None,
        *,
        author_id: int | None = None,
        timeout: float | None = 180,
    ):
        super().__init__(author_id=author_id, timeout=timeout)
        self.embed = themed(embed)

    def rows(self) -> list[list[discord.ui.Item]]:
        return []

    def render(self) -> None:
        self.clear_items()
        embed = self.embed or discord.Embed(color=EMBED_COLOR)
        action_rows = [discord.ui.ActionRow(*items) for items in self.rows() if items]
        self.add_item(container_from_embed(embed, *action_rows))

    def set_embed(self, embed: discord.Embed) -> None:
        self.embed = themed(embed)
        self.render()

    async def push(self, interaction: discord.Interaction, embed: discord.Embed | None = None) -> None:
        if embed is not None:
            self.set_embed(embed)
        else:
            self.render()
        await interaction.response.edit_message(view=self)


class ConfirmLayout(BaseLayout):
    def __init__(
        self,
        author: discord.Member | discord.User,
        prompt: str | None = None,
        *,
        embed: discord.Embed | None = None,
        title: str = "⚠️ Confirm",
        color: discord.Color | None = None,
        confirm_label: str = "Confirm",
        cancel_label: str = "Cancel",
        cancel_text: str = "❌ Cancelled",
        timeout_text: str = "⏱️ Timed out",
        timeout: float = 30.0,
    ):
        super().__init__(author_id=author.id, timeout=timeout)
        self.author = author
        self.value: bool | None = None
        self.cancel_text = cancel_text
        self.timeout_text = timeout_text
        self.embed = embed or discord.Embed(
            title=title,
            description=prompt,
            color=color or discord.Color.gold(),
        )
        self.confirm_button = discord.ui.Button(label=confirm_label, style=discord.ButtonStyle.danger)
        self.cancel_button = discord.ui.Button(label=cancel_label, style=discord.ButtonStyle.secondary)
        self.confirm_button.callback = self._on_confirm
        self.cancel_button.callback = self._on_cancel
        self.render()

    def render(self, state: str = "pending") -> None:
        self.clear_items()
        if state == "pending":
            container = container_from_embed(
                self.embed,
                discord.ui.ActionRow(self.confirm_button, self.cancel_button),
            )
        elif state == "confirmed":
            container = container_from_embed(self.embed)
        else:
            text = self.cancel_text if state == "cancelled" else self.timeout_text
            head, _, rest = text.partition("\n")
            body = f"## {head}" + (f"\n{rest}" if rest else "")
            container = discord.ui.Container(
                discord.ui.TextDisplay(body),
                accent_colour=discord.Color.red(),
            )
        self.add_item(container)

    async def send(self, ctx, *, reply: bool = True) -> discord.Message:
        if reply:
            self.message = await ctx.reply(view=self, mention_author=False)
        else:
            self.message = await ctx.send(view=self)
        return self.message

    async def show(self, embed: discord.Embed) -> None:
        self.clear_items()
        self.add_item(container_from_embed(embed))
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _finish(self, interaction: discord.Interaction, value: bool) -> None:
        self.value = value
        self.render("confirmed" if value else "cancelled")
        await interaction.response.edit_message(view=self)
        self.stop()

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        await self._finish(interaction, True)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await self._finish(interaction, False)

    async def on_timeout(self) -> None:
        self.value = None
        self.render("timeout")
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def confirm(ctx, prompt: str, **kwargs) -> bool:
    view = ConfirmLayout(ctx.author, prompt, **kwargs)
    await view.send(ctx)
    await view.wait()
    return view.value is True