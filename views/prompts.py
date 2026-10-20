"""
Small helpers for step-by-step "ask the user a question" flows.

Used by .cch / .ccat / .rch / .rcat when they are run without arguments:
the bot asks one question at a time and waits (default 30 seconds) for the
invoking user to answer in the same channel.

Also holds the helpers that let names contain spaces in prefix commands:
``raw_args`` (the text after the command name), ``split_name_and_target`` (a name followed by an
optional existing category / channel) and ``exact_category``.
"""
import asyncio
import re
import typing
from contextlib import suppress

import discord

from views.common_views import BaseLayout, container_from_embed

# Words that mean "leave this optional setting at its default".
SKIP_WORDS = {"skip", "none", "no", "n", "default", "current", "-"}
# Words that abort the whole flow.
CANCEL_WORDS = {"cancel", "stop", "exit", "quit"}

DEFAULT_TIMEOUT = 30.0


_QUOTED = re.compile(r"^([\"'\u201c\u2018])(.+?)[\"'\u201d\u2019](?:\s+(.*))?$", re.S)
_QUOTE_CHARS = "\"'\u201c\u201d\u2018\u2019"


def raw_args(ctx) -> str:
    """Everything the user typed after the command name (prefix commands only)."""
    raw = ctx.message.content[len(ctx.prefix):].lstrip()
    return raw[len(ctx.invoked_with):].strip()


def strip_quotes(text: str) -> str:
    return text.strip().strip(_QUOTE_CHARS).strip()


def normalize_name(text: str) -> str:
    """Lower-case and treat '-', '_' and repeated spaces as one space ("staff-area" == "Staff  Area")."""
    return " ".join(re.sub(r"[-_]+", " ", text).lower().split())


def exact_category(guild: discord.Guild, text: str) -> typing.Optional[discord.CategoryChannel]:
    """A category by <#mention>, ID or exact (case-insensitive) name. None when nothing matches."""
    text = strip_quotes(text)
    if not text:
        return None
    match = re.fullmatch(r"<#(\d+)>|(\d{15,25})", text)
    if match:
        channel = guild.get_channel(int(match.group(1) or match.group(2)))
        return channel if isinstance(channel, discord.CategoryChannel) else None
    wanted = normalize_name(text)
    return next((c for c in guild.categories if normalize_name(c.name) == wanted), None)


def split_name_and_target(text: str, resolver: typing.Callable[[str], typing.Any]) -> tuple[str, typing.Any, str]:
    """Splits ``<name with spaces> [target]``.

    The target is the longest run of trailing words that ``resolver`` accepts (it returns None for
    "not a target"); the name is everything before it and is never empty. A quoted name
    (``"My Name" target``) ends at the closing quote instead.

    Returns ``(name, target, unresolved)``: ``unresolved`` is only filled for a quoted name that is
    followed by text which is not a valid target.
    """
    text = text.strip()
    quoted = _QUOTED.match(text)
    if quoted:
        rest = (quoted.group(3) or "").strip()
        target = resolver(rest) if rest else None
        return quoted.group(2).strip(), target, ("" if target is not None else rest)

    words = text.split()
    for index in range(1, len(words)):
        target = resolver(" ".join(words[index:]))
        if target is not None:
            return " ".join(words[:index]), target, ""
    return text, None, ""


def one_line(text: str) -> str:
    """Returns plain text message."""
    return text


def is_skip(text: str) -> bool:
    return text.strip().lower() in SKIP_WORDS


def _static_panel(text: str, color: discord.Color | int = 0x0414C7) -> discord.ui.LayoutView:
    """A button-less message made of one embed-style container."""
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container_from_embed(discord.Embed(description=text, color=color)))
    return view


class PromptView(BaseLayout):
    """The question and its Cancel button, together inside one embed-style container."""

    def __init__(self, author_id: int, question: str = "", timeout: float = DEFAULT_TIMEOUT):
        super().__init__(author_id=author_id, timeout=timeout)
        self.cancelled = False
        self.question = question
        self.cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.red)
        self.cancel_button.callback = self.cancel
        embed = discord.Embed(
            description=f"{question}\n-# You have {int(timeout)} seconds\n-# Click below to abort.",
            color=0x0414C7,
        )
        self.add_item(container_from_embed(embed, discord.ui.ActionRow(self.cancel_button)))

    async def cancel(self, interaction: discord.Interaction):
        self.cancelled = True
        self.stop()
        await interaction.response.defer()

    async def on_timeout(self) -> None:
        # ask() edits the message itself once the wait is over.
        return None


async def ask(ctx, question: str, *, timeout: float = DEFAULT_TIMEOUT) -> discord.Message | None:
    """
    Asks `question` inside an embed with a Cancel button and waits for the user's reply message
    in the same channel or for them to click Cancel.

    Returns the user's reply message, or None if they timed out or cancelled.
    """
    view = PromptView(ctx.author.id, question, timeout=timeout)
    prompt = await ctx.reply(view=view, mention_author=False)
    view.message = prompt

    def check(m: discord.Message) -> bool:
        return m.author.id == ctx.author.id and m.channel.id == ctx.channel.id

    msg_task = asyncio.create_task(ctx.bot.wait_for("message", check=check))
    view_task = asyncio.create_task(view.wait())

    done, pending = await asyncio.wait(
        [msg_task, view_task],
        return_when=asyncio.FIRST_COMPLETED
    )

    for task in pending:
        task.cancel()

    if msg_task in done:
        with suppress(asyncio.CancelledError, Exception):
            reply = msg_task.result()
            if reply.content.strip().lower() in CANCEL_WORDS:
                with suppress(discord.HTTPException):
                    await prompt.edit(view=_static_panel("Cancelled."))
                return None
            with suppress(discord.HTTPException):
                await prompt.edit(view=_static_panel(question))
            return reply

    if view.cancelled:
        with suppress(discord.HTTPException):
            await prompt.edit(view=_static_panel("Cancelled."))
        return None

    with suppress(discord.HTTPException):
        await prompt.edit(view=_static_panel(f"⏱️ No reply within {int(timeout)} seconds. Cancelled."))
    return None
