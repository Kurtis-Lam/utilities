"""
Small helpers for step-by-step "ask the user a question" flows.

Used by .cch / .ccat / .rch / .rcat when they are run without arguments:
the bot asks one question at a time and waits (default 30 seconds) for the
invoking user to answer in the same channel.
"""
import asyncio
from typing import Optional

import discord

# Words that mean "leave this optional setting at its default".
SKIP_WORDS = {"skip", "none", "no", "n", "default", "current", "-"}
# Words that abort the whole flow.
CANCEL_WORDS = {"cancel", "stop", "exit", "quit"}

DEFAULT_TIMEOUT = 30.0


def one_line(text: str) -> str:
    """Returns plain text message."""
    return text


def is_skip(text: str) -> bool:
    return text.strip().lower() in SKIP_WORDS


class PromptView(discord.ui.View):
    def __init__(self, author_id: int, timeout: float = DEFAULT_TIMEOUT):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.cancelled = False

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This is not your prompt.", ephemeral=True)
            return
        self.cancelled = True
        self.stop()
        await interaction.response.defer()


async def ask(ctx, question: str, *, timeout: float = DEFAULT_TIMEOUT) -> Optional[discord.Message]:
    """
    Asks `question` as text with a Cancel button and waits for the user's reply message
    in the same channel or for them to click Cancel.

    Returns the user's reply message, or None if they timed out or cancelled.
    """
    view = PromptView(ctx.author.id, timeout=timeout)
    content = f"{question}\n-# You have {int(timeout)} seconds\n-# Click below to abort."
    prompt = await ctx.reply(content, view=view, mention_author=False)

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
        try:
            reply = msg_task.result()
            if reply.content.strip().lower() in CANCEL_WORDS:
                try:
                    await prompt.edit(content="❌ Cancelled.", view=None)
                except discord.HTTPException:
                    pass
                return None
            try:
                await prompt.edit(view=None)
            except discord.HTTPException:
                pass
            return reply
        except Exception:
            pass

    if view.cancelled:
        try:
            await prompt.edit(content="❌ Cancelled.", view=None)
        except discord.HTTPException:
            pass
        return None

    try:
        await prompt.edit(content=f"⏱️ No reply within {int(timeout)} seconds. Cancelled.", view=None)
    except discord.HTTPException:
        pass
    return None