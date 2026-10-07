import asyncio
import datetime
import time

import discord
from discord.ext import commands

from cogs.poketwo-helper.lockcommon import (
    POKETWO_ID,
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
from views.common_views import EMBED_COLOR, container_from_embed, error_embed
from views.embeds import handle_command_error

UNLOCK_BATCH_SIZE = 5
PROGRESS_EDIT_INTERVAL = 1.0


def _poketwo_missing_embed() -> discord.Embed:
    return error_embed("Pokétwo isn't in this server.")


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


UNLOCK_CUSTOM_ID = "lockunlock:unlock_btn"
NO_PINGS = discord.AllowedMentions.none()


def _message_texts(message: discord.Message | None) -> tuple[list[str], discord.Color | int | None]:
    """Read back the text blocks (and accent color) of a Components V2 lock message,
    so the button can be retired without keeping the original embed around."""
    texts: list[str] = []
    accent = None

    def walk(components):
        nonlocal accent
        for comp in components or []:
            if accent is None:
                accent = getattr(comp, "accent_colour", None) or getattr(comp, "accent_color", None)
            content = getattr(comp, "content", None)
            if isinstance(content, str) and content:
                texts.append(content)
            walk(getattr(comp, "children", None))

    walk(getattr(message, "components", None))
    return texts, accent


class UnlockLayout(discord.ui.LayoutView):
    """The lock message: the embed-style container with the Unlock button *inside* it.

    Used by .lock and by AutoLock, so both look and behave the same.
    Pass ``embed`` to build a fresh message, or ``texts`` (+ ``accent``) to rebuild
    an existing one. ``unlocked=True`` renders the greyed-out "Unlocked" button.
    """

    def __init__(self, bot, embed: discord.Embed | None = None, *, texts=None, accent=None, unlocked: bool = False):
        super().__init__(timeout=None)
        self.bot = bot

        self.unlock_button = discord.ui.Button(
            label="Unlocked" if unlocked else "Unlock",
            style=discord.ButtonStyle.secondary if unlocked else discord.ButtonStyle.green,
            emoji=None if unlocked else "🔓",
            custom_id=UNLOCK_CUSTOM_ID,
            disabled=unlocked,
        )
        self.unlock_button.callback = self._on_unlock
        row = discord.ui.ActionRow(self.unlock_button)

        if embed is not None:
            container = container_from_embed(embed, row)
        else:
            container = discord.ui.Container(accent_colour=accent or EMBED_COLOR)
            for text in texts or ["\u200b"]:
                container.add_item(discord.ui.TextDisplay(text))
            container.add_item(discord.ui.Separator())
            container.add_item(row)
        self.add_item(container)

    async def _retire(self, interaction: discord.Interaction) -> None:
        """Grey out the button on the message that was clicked (acknowledges the interaction)."""
        texts, accent = _message_texts(interaction.message)
        view = UnlockLayout(self.bot, texts=texts, accent=accent, unlocked=True)
        try:
            await interaction.response.edit_message(view=view)
        except discord.HTTPException:
            # e.g. an old pre-V2 lock message that can't be converted; still acknowledge.
            if not interaction.response.is_done():
                await interaction.response.defer()

    async def _on_unlock(self, interaction: discord.Interaction):
        guild = interaction.guild
        channel = interaction.channel
        user = interaction.user

        if guild is None or channel is None:
            return

        locks_collection = self.bot.mongo_client["utilities"]["locked_channels"]

        poketwo, lock_doc = await asyncio.gather(
            get_poketwo_target(guild),
            locks_collection.find_one({"_id": channel.id}),
        )

        if poketwo is None:
            return await interaction.response.send_message(embed=_poketwo_missing_embed(), ephemeral=True)

        if not is_locked_overwrite(channel.overwrites_for(poketwo)):
            if lock_doc:
                await locks_collection.delete_one({"_id": channel.id})
            await self._retire(interaction)
            return await interaction.followup.send(embed=already_unlocked_embed(channel), ephemeral=True)

        if lock_doc and not can_unlock(lock_doc, user):
            return await interaction.response.send_message(embed=unlock_denied_embed(channel, lock_doc), ephemeral=True)

        permissions = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        try:
            await channel.set_permissions(poketwo, overwrite=permissions)
        except discord.HTTPException as e:
            return await interaction.response.send_message(embed=error_embed(f"Couldn't unlock: {e}"), ephemeral=True)

        if lock_doc:
            await locks_collection.delete_one({"_id": channel.id})

        await self._retire(interaction)
        await interaction.followup.send(
            embed=unlocked_embed(
                channel,
                unlocked_by=user,
                locked_at=to_unix((lock_doc or {}).get("locked_at")),
            )
        )


async def _disable_lock_message(channel, message_id: int | None, bot):
    """Fetch a stored lock message and grey out its Unlock button."""
    if not message_id:
        return
    try:
        msg = await channel.fetch_message(message_id)
        texts, accent = _message_texts(msg)
        if not texts:
            return
        await msg.edit(view=UnlockLayout(bot, texts=texts, accent=accent, unlocked=True))
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
            get_poketwo_target(ctx.guild),
            self.locks_collection.find_one({"_id": ctx.channel.id}),
        )

        if poketwo is None:
            return await ctx.reply(embed=_poketwo_missing_embed())

        if not is_locked_overwrite(ctx.channel.overwrites_for(poketwo)):
            if lock_doc:
                await self.locks_collection.delete_one({"_id": ctx.channel.id})
                await _disable_lock_message(ctx.channel, lock_doc.get("message_id"), self.bot)
            return await ctx.reply(embed=already_unlocked_embed(ctx.channel))

        if lock_doc and not can_unlock(lock_doc, ctx.author):
            return await ctx.reply(embed=unlock_denied_embed(ctx.channel, lock_doc))

        permissions = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        try:
            await ctx.channel.set_permissions(poketwo, overwrite=permissions)
        except discord.HTTPException as e:
            return await ctx.reply(embed=error_embed(f"Couldn't unlock: {e}"))

        if lock_doc:
            await self.locks_collection.delete_one({"_id": ctx.channel.id})
            await _disable_lock_message(ctx.channel, lock_doc.get("message_id"), self.bot)

        await ctx.reply(
            embed=unlocked_embed(
                ctx.channel,
                unlocked_by=ctx.author,
                locked_at=to_unix((lock_doc or {}).get("locked_at")),
            )
        )

    @commands.hybrid_command(aliases=["l"], name="lock", description="Locks the current channel.")
    @commands.guild_only()
    async def lock(self, ctx):
        poketwo, existing = await asyncio.gather(
            get_poketwo_target(ctx.guild),
            self.locks_collection.find_one({"_id": ctx.channel.id}),
        )

        if poketwo is None:
            return await ctx.reply(embed=_poketwo_missing_embed())

        if is_locked_overwrite(ctx.channel.overwrites_for(poketwo)):
            return await ctx.reply(
                view=UnlockLayout(self.bot, already_locked_embed(ctx.channel, existing)),
                allowed_mentions=NO_PINGS,
            )

        now = now_unix()
        permissions = discord.PermissionOverwrite(read_messages=False, send_messages=False)
        try:
            await ctx.channel.set_permissions(poketwo, overwrite=permissions)
        except discord.HTTPException as e:
            return await ctx.reply(embed=error_embed(f"Couldn't lock: {e}"))

        msg = await ctx.reply(
            view=UnlockLayout(
                self.bot,
                locked_embed(ctx.channel, locked_by=ctx.author, allowed_users=None, when=now),
            ),
            allowed_mentions=NO_PINGS,
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

    @commands.has_permissions(administrator=True)
    @commands.command(name="unlockallchannels", aliases=["uac"], description="Unlocks all channels in the server.")
    async def unlockallchannels(self, ctx):
        poketwo = await get_poketwo_target(ctx.guild)
        if poketwo is None:
            return await ctx.reply(embed=_poketwo_missing_embed())

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
            return await ctx.reply(embed=embed)

        total = len(locked)
        permissions = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        progress_msg = await ctx.reply(embed=_progress_embed(0, total, started, started_unix))

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
            await ctx.reply(embed=embed)

    @commands.hybrid_command(name="lockstats", aliases=["ls"], description="Shows all locked and unlocked channels.")
    @commands.guild_only()
    async def lockstats(self, ctx):
        poketwo = await get_poketwo_target(ctx.guild)
        if poketwo is None:
            return await ctx.reply(embed=_poketwo_missing_embed())

        docs = await self.locks_collection.find({"guild_id": ctx.guild.id}).to_list(length=None)
        locked_since = {doc["_id"]: to_unix(doc.get("locked_at")) for doc in docs}

        locked = []
        unlocked = []
        for channel in ctx.guild.text_channels:
            if is_locked_overwrite(channel.overwrites_for(poketwo)):
                locked.append(channel)
            else:
                unlocked.append(channel)

        embed = discord.Embed(
            title="📊 Locks",
            description=f"🔒 **{len(locked)}** locked  •  🔓 **{len(unlocked)}** unlocked",
            color=EMBED_COLOR,
        )

        locked_lines = []
        for channel in locked:
            since = locked_since.get(channel.id)
            locked_lines.append(f"{channel.mention} • <t:{since}:R>" if since else channel.mention)

        if locked:
            add_item_fields(embed, f"🔒 Locked ({len(locked)})", locked_lines, sep="\n")
        else:
            embed.add_field(name="🔒 Locked (0)", value="None", inline=False)

        if unlocked:
            add_item_fields(embed, f"🔓 Unlocked ({len(unlocked)})", [c.mention for c in unlocked])
        else:
            embed.add_field(name="🔓 Unlocked (0)", value="None", inline=False)

        await ctx.reply(embed=embed)


async def setup(bot):
    bot.add_view(UnlockLayout(bot))
    await bot.add_cog(LockUnlock(bot))