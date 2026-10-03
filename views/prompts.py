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


def one_line(text: str, color: Optional[discord.Color] = None) -> discord.Embed:
    """A title-less, single-line embed (description only)."""
    return discord.Embed(description=text, color=color or discord.Color.red())


def is_skip(text: str) -> bool:
    return text.strip().lower() in SKIP_WORDS


async def ask(ctx, question: str, *, timeout: float = DEFAULT_TIMEOUT) -> Optional[discord.Message]:
    """
    Asks `question` as a reply to the user's command and waits for their next
    message in the same channel.

    Returns the user's reply message, or None if they timed out or typed
    `cancel` (the prompt is edited to say so, so the caller just needs to stop).
    """
    embed = discord.Embed(
        description=f"❓ {question}\n-# ⏱️ {int(timeout)} seconds to reply · type `cancel` to stop",
        color=discord.Color.gold(),
    )
    prompt = await ctx.reply(embed=embed, mention_author=False)

    def check(m: discord.Message) -> bool:
        return m.author.id == ctx.author.id and m.channel.id == ctx.channel.id

    try:
        reply = await ctx.bot.wait_for("message", check=check, timeout=timeout)
    except asyncio.TimeoutError:
        try:
            await prompt.edit(embed=one_line(f"⏱️ No reply within {int(timeout)} seconds. Cancelled."))
        except discord.HTTPException:
            pass
        return None

    if reply.content.strip().lower() in CANCEL_WORDS:
        try:
            await prompt.edit(embed=one_line("❌ Cancelled."))
        except discord.HTTPException:
            pass
        return None

    return reply