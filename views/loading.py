"""Shared "⏳ Loading..." support for slow commands.

Put ``@loading`` directly above the command function (below ``@commands.command`` /
``@commands.hybrid_command`` and any ``@commands.has_permissions`` style checks):

    @commands.command(name="example")
    @loading
    async def example(self, ctx): ...

What it does
* Prefix commands: instantly replies ``⏳ Loading...`` (no ping). The command's *first*
  ``ctx.reply`` / ``ctx.send`` then **edits that message** into the real answer, so the
  command code needs no changes. When the answer can't be an edit (it has files, uses a
  Components V2 layout, or has content that must ping someone) the loading message is
  deleted and the answer is sent normally.
* Slash commands: the interaction is deferred (Discord's own "thinking..." state).
* If the command ends (or fails) without answering, the loading message is deleted so a
  stale ``⏳ Loading...`` is never left behind.
"""
from __future__ import annotations

import functools
from contextlib import suppress

import discord
from discord.ext import commands

LOADING_TEXT = "⏳ Loading..."

_NO_PINGS = discord.AllowedMentions.none()
# Keyword arguments an edit of the loading message can carry.
_EDITABLE_KWARGS = {"embed", "embeds", "view", "allowed_mentions", "mention_author", "delete_after"}


class Loading:
    """Async context manager behind ``@loading``."""

    def __init__(self, ctx: commands.Context, text: str = LOADING_TEXT):
        self.ctx = ctx
        self.text = text
        self.message: discord.Message | None = None
        self._consumed = False
        self._patched = False

    async def __aenter__(self) -> "Loading":
        ctx = self.ctx
        if ctx.interaction is not None:
            with suppress(discord.HTTPException):
                await ctx.defer()
            return self

        try:
            self.message = await ctx.reply(self.text, mention_author=False)
        except discord.HTTPException:
            # The invoking message is gone: fall back to a plain message.
            with suppress(discord.HTTPException):
                self.message = await ctx.channel.send(self.text)

        if self.message is not None:
            original_reply, original_send = ctx.reply, ctx.send

            async def reply(content=None, **kwargs):
                return await self._respond(original_reply, content, kwargs)

            async def send(content=None, **kwargs):
                return await self._respond(original_send, content, kwargs)

            ctx.reply, ctx.send = reply, send
            self._patched = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._patched:
            # Remove the instance-level overrides -> back to Context.reply / Context.send.
            self.ctx.__dict__.pop("reply", None)
            self.ctx.__dict__.pop("send", None)
            self._patched = False
        if self.message is not None and not self._consumed:
            await self._remove()
        return False

    async def _remove(self) -> None:
        message, self.message = self.message, None
        if message is not None:
            with suppress(discord.HTTPException):
                await message.delete()

    @staticmethod
    def _can_edit(content, kwargs: dict) -> bool:
        if not set(kwargs) <= _EDITABLE_KWARGS:
            return False  # files, polls, references, ... need a fresh message
        if isinstance(kwargs.get("view"), discord.ui.LayoutView):
            return False  # a classic message can't become a Components V2 one
        if isinstance(content, str) and ("<@" in content or "@everyone" in content or "@here" in content):
            return False  # a fresh message is what reliably notifies people
        return True

    async def _respond(self, original, content, kwargs: dict):
        message = self.message
        if message is None or self._consumed:
            return await original(content, **kwargs)
        self._consumed = True

        if self._can_edit(content, kwargs):
            payload = {
                "content": content,
                "view": kwargs.get("view"),
                "allowed_mentions": kwargs.get("allowed_mentions") or _NO_PINGS,
            }
            for key in ("embed", "embeds"):
                if key in kwargs:
                    payload[key] = kwargs[key]
            try:
                edited = await message.edit(**payload)
            except discord.HTTPException:
                pass
            else:
                delay = kwargs.get("delete_after")
                if delay is not None:
                    with suppress(discord.HTTPException):
                        await edited.delete(delay=delay)
                return edited

        await self._remove()
        return await original(content, **kwargs)


def loading(func=None, *, text: str = LOADING_TEXT):
    """Decorator: show ``⏳ Loading...`` while a command works, then replace it with the answer."""

    def decorate(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            ctx = next((a for a in args[:2] if isinstance(a, commands.Context)), None)
            if ctx is None:
                return await fn(*args, **kwargs)
            async with Loading(ctx, text):
                return await fn(*args, **kwargs)

        return wrapper

    return decorate(func) if func is not None else decorate
