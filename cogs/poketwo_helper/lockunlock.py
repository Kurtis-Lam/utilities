import asyncio
import datetime
import time

import discord
from discord.ext import commands

from cogs.poketwo_helper.lockcommon import (
    UNLOCK_COLOR,
    WARN_COLOR,
    add_item_fields,
    already_locked_embed,
    already_unlocked_embed,
    can_unlock,
    format_elapsed,
    get_poketwo_target,
    is_locked_overwrite,
    locked_embed,
    now_unix,
    stamp,
    to_unix,
    unlock_denied_embed,
    unlocked_embed,
)
from views.common_views import EMBED_COLOR, error_embed
from views.embeds import handle_command_error
from views.loading import loading
from views.navigate import PaginatorView
from views.lockunlock_views import UnlockLayout, drop_auto_unlock, message_texts, poketwo_missing_embed

UNLOCK_BATCH_SIZE = 5
PROGRESS_EDIT_INTERVAL = 1.0


def _progress_embed(done: int, total: int, started: float, started_unix: int) -> discord.Embed:
    filled = int((done / total) * 10) if total else 10
    bar = "█" * filled + "░" * (10 - filled)
    embed = discord.Embed(
        title="🔓 Unlocking…",
        description=f"`{bar}` **{done}/{total}**",
        color=WARN_COLOR,
    )
    embed.add_field(name="⏱️ Elapsed", value=f"`{format_elapsed(time.monotonic() - started)}`", inline=True)
    return embed


NO_PINGS = discord.AllowedMentions.none()

# Lockstats paging: a field holds at most 1024 characters, an embed at most 6000 in total.
FIELD_LIMIT = 1000
FIELDS_PER_PAGE = 3


def _chunk_lines(items: list[str], sep: str, limit: int = FIELD_LIMIT) -> list[str]:
    """Join items with `sep` into as few strings of at most `limit` characters as possible."""
    chunks: list[str] = []
    current = ""
    for item in items:
        candidate = f"{current}{sep}{item}" if current else item
        if len(candidate) > limit and current:
            chunks.append(current)
            current = item
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _lockstats_pages(locked_lines: list[str], unlocked_mentions: list[str]) -> list[discord.Embed]:
    """Split the lock overview over as many embeds as needed (nothing is cut off)."""
    summary = f"🔒 **{len(locked_lines)}** locked  •  🔓 **{len(unlocked_mentions)}** unlocked"

    fields: list[tuple[str, str]] = []
    if locked_lines:
        for i, chunk in enumerate(_chunk_lines(locked_lines, "\n")):
            name = f"🔒 Locked ({len(locked_lines)})" if i == 0 else "🔒 Locked (continued)"
            fields.append((name, chunk))
    else:
        fields.append(("🔒 Locked (0)", "None"))

    if unlocked_mentions:
        for i, chunk in enumerate(_chunk_lines(unlocked_mentions, " ")):
            name = f"🔓 Unlocked ({len(unlocked_mentions)})" if i == 0 else "🔓 Unlocked (continued)"
            fields.append((name, chunk))
    else:
        fields.append(("🔓 Unlocked (0)", "None"))

    groups = [fields[i:i + FIELDS_PER_PAGE] for i in range(0, len(fields), FIELDS_PER_PAGE)]
    pages = []
    for number, group in enumerate(groups, start=1):
        embed = discord.Embed(title="📊 Locks", description=summary, color=EMBED_COLOR)
        for name, value in group:
            embed.add_field(name=name, value=value, inline=False)
        if len(groups) > 1:
            embed.set_footer(text=f"Page {number}/{len(groups)}")
        pages.append(embed)
    return pages


async def _disable_lock_message(channel, message_id: int | None, bot):
    """Fetch a stored lock message and grey out its Unlock button."""
    if not message_id:
        return
    try:
        msg = await channel.fetch_message(message_id)
        texts, accent = message_texts(msg)
        if not texts:
            return
        await msg.edit(view=UnlockLayout(bot, texts=drop_auto_unlock(texts), accent=accent, unlocked=True))
    except discord.HTTPException:
        pass


class LockUnlock(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def locks_collection(self):
        """Dynamically retrieves the collection from the shared main MongoDB client."""
        return self.bot.mongo_client["utilities"]["locked_channels"]

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        await handle_command_error(ctx, error)

    @commands.hybrid_command(aliases=["u"], name="unlock", description="Unlocks the current channel.")
    @commands.guild_only()
    async def unlock(self, ctx):
        poketwo, lock_doc = await asyncio.gather(
            get_poketwo_target(self.bot, ctx.guild),
            self.locks_collection.find_one({"_id": ctx.channel.id}),
        )

        if poketwo is None:
            return await ctx.reply(embed=poketwo_missing_embed(), mention_author=False)

        if not is_locked_overwrite(ctx.channel.overwrites_for(poketwo)):
            if lock_doc:
                await self.locks_collection.delete_one({"_id": ctx.channel.id})
                await _disable_lock_message(ctx.channel, lock_doc.get("message_id"), self.bot)
            return await ctx.reply(embed=already_unlocked_embed(ctx.channel), mention_author=False)

        if lock_doc and not can_unlock(lock_doc, ctx.author):
            return await ctx.reply(embed=unlock_denied_embed(ctx.channel, lock_doc), mention_author=False)

        permissions = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        try:
            await ctx.channel.set_permissions(poketwo, overwrite=permissions)
        except discord.HTTPException as e:
            return await ctx.reply(embed=error_embed(f"Couldn't unlock: {e}"), mention_author=False)

        if lock_doc:
            await self.locks_collection.delete_one({"_id": ctx.channel.id})
            await _disable_lock_message(ctx.channel, lock_doc.get("message_id"), self.bot)

        await ctx.reply(
            embed=unlocked_embed(
                ctx.channel,
                unlocked_by=ctx.author,
                locked_at=to_unix((lock_doc or {}).get("locked_at")),
            ), mention_author=False
        )

    @commands.hybrid_command(aliases=["l"], name="lock", description="Locks the current channel.")
    @commands.guild_only()
    async def lock(self, ctx):
        poketwo, existing = await asyncio.gather(
            get_poketwo_target(self.bot, ctx.guild),
            self.locks_collection.find_one({"_id": ctx.channel.id}),
        )

        if poketwo is None:
            return await ctx.reply(embed=poketwo_missing_embed(), mention_author=False)

        if is_locked_overwrite(ctx.channel.overwrites_for(poketwo)):
            return await ctx.reply(
                view=UnlockLayout(self.bot, already_locked_embed(ctx.channel, existing)),
                allowed_mentions=NO_PINGS, mention_author=False,
            )

        now = now_unix()
        permissions = discord.PermissionOverwrite(read_messages=False, send_messages=False)
        try:
            await ctx.channel.set_permissions(poketwo, overwrite=permissions)
        except discord.HTTPException as e:
            return await ctx.reply(embed=error_embed(f"Couldn't lock: {e}"), mention_author=False)

        msg = await ctx.reply(
            view=UnlockLayout(
                self.bot,
                locked_embed(ctx.channel, locked_by=ctx.author, allowed_users=None, when=now),
            ),
            allowed_mentions=NO_PINGS, mention_author=False,
        )

        await self.locks_collection.update_one(
            {"_id": ctx.channel.id},
            {"$set": {
                "allowed_users": None,
                "guild_id": ctx.guild.id,
                "source": "manual",
                "locked_by": ctx.author.id,
                "categories": [],
                "restricted_by": [],
                "locked_at": datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc),
                "message_id": msg.id,
            }},
            upsert=True,
        )

    @commands.hybrid_command(name="unlockallchannels", aliases=["uac"], description="Unlocks all channels in the server.")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def unlockallchannels(self, ctx):
        poketwo = await get_poketwo_target(self.bot, ctx.guild)
        if poketwo is None:
            return await ctx.reply(embed=poketwo_missing_embed(), mention_author=False)

        started = time.monotonic()
        started_unix = now_unix()

        await self.locks_collection.delete_many({"guild_id": ctx.guild.id})

        candidates = [
            ch for ch in ctx.guild.channels
            if isinstance(ch, (discord.TextChannel, discord.VoiceChannel, discord.ForumChannel))
            and poketwo in ch.overwrites
        ]
        locked = [ch for ch in candidates if is_locked_overwrite(ch.overwrites_for(poketwo))]
        locked_ids = {ch.id for ch in locked}
        already_unlocked = [ch for ch in candidates if ch.id not in locked_ids]

        if not locked:
            embed = discord.Embed(
                title="🔓 Already Unlocked",
                description="No locked channels.",
                color=WARN_COLOR,
            )
            add_item_fields(embed, f"ℹ️️ Already Unlocked ({len(already_unlocked)})", [c.mention for c in already_unlocked])
            return await ctx.reply(embed=embed, mention_author=False)

        total = len(locked)
        permissions = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        progress_msg = await ctx.reply(embed=_progress_embed(0, total, started, started_unix), mention_author=False)

        done = 0
        failed = []

        async def unlock_single(channel):
            nonlocal done
            try:
                await channel.set_permissions(poketwo, overwrite=permissions)
                done += 1
            except discord.HTTPException:
                failed.append(channel)

        last_edit = time.monotonic()
        for i in range(0, total, UNLOCK_BATCH_SIZE):
            chunk = locked[i:i + UNLOCK_BATCH_SIZE]
            await asyncio.gather(*(unlock_single(ch) for ch in chunk))

            now = time.monotonic()
            if i + UNLOCK_BATCH_SIZE < total and now - last_edit >= PROGRESS_EDIT_INTERVAL:
                last_edit = now
                try:
                    await progress_msg.edit(embed=_progress_embed(done, total, started, started_unix))
                except discord.HTTPException:
                    pass
            await asyncio.sleep(0.3)

        elapsed = time.monotonic() - started
        had_errors = bool(failed)
        embed = discord.Embed(
            title="⚠️ Unlocked with errors" if had_errors else "✅ Unlocked",
            description=f"**{done}/{total}** channels unlocked.",
            color=WARN_COLOR if had_errors else UNLOCK_COLOR,
        )
        embed.add_field(name="⏱️ Elapsed", value=f"`{format_elapsed(elapsed)}`", inline=True)
        embed.add_field(name="🕒 Finished", value=stamp(now_unix()), inline=True)
        add_item_fields(embed, f"⚠️ Couldn't Unlock ({len(failed)})", [c.mention for c in failed])
        add_item_fields(embed, f"ℹ️ Already Unlocked ({len(already_unlocked)})", [c.mention for c in already_unlocked])

        try:
            await progress_msg.edit(embed=embed)
        except discord.HTTPException:
            await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="lockstats", aliases=["ls"], description="Shows all locked and unlocked channels (admins only).")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @loading
    async def lockstats(self, ctx):
        poketwo = await get_poketwo_target(self.bot, ctx.guild)
        if poketwo is None:
            return await ctx.reply(embed=poketwo_missing_embed(), mention_author=False)

        docs = await self.locks_collection.find({"guild_id": ctx.guild.id}).to_list(length=None)
        locked_since = {doc["_id"]: to_unix(doc.get("locked_at")) for doc in docs}
        unlocks_at = {doc["_id"]: to_unix(doc.get("unlock_at")) for doc in docs}

        locked = []
        unlocked = []
        for channel in ctx.guild.text_channels:
            if is_locked_overwrite(channel.overwrites_for(poketwo)):
                locked.append(channel)
            else:
                unlocked.append(channel)

        locked_lines = []
        for channel in locked:
            since = locked_since.get(channel.id)
            line = f"{channel.mention} • <t:{since}:R>" if since else channel.mention
            unlock_at = unlocks_at.get(channel.id)
            if unlock_at:
                line += f" • unlocks <t:{unlock_at}:R>"
            locked_lines.append(line)

        pages = _lockstats_pages(locked_lines, [c.mention for c in unlocked])

        if len(pages) == 1:
            return await ctx.reply(embed=pages[0], mention_author=False)

        view = PaginatorView(pages, user_id=ctx.author.id)
        view.message = await ctx.reply(view=view, mention_author=False)


async def setup(bot):
    bot.add_view(UnlockLayout(bot))
    await bot.add_cog(LockUnlock(bot))