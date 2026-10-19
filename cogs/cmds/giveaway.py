"""Giveaways (administrators only).

Commands (all hybrid: work as ``.giveaway ...`` and as ``/giveaway ...``):

* ``giveaway create <duration> <winners> <prize> [channel]``  start a giveaway
* ``giveaway end <giveaway>``                                  end one right now
* ``giveaway list``                                            active / ended giveaways
* ``giveaway participants <giveaway>``                         who entered
* ``giveaway reroll <giveaway> [winners]``                     pick new winners

Everything is stored in MongoDB (``utilities.giveaways``), so giveaways survive restarts:
a background loop ends every giveaway whose time is up (also ones that expired while the bot
was offline) and the "Enter" button keeps working because it is a persistent view.
"""
import asyncio
import random
import re
import time
import typing

import discord
from discord import app_commands
from discord.ext import commands, tasks
from pymongo import ReturnDocument

from views.embeds import BRAND_COLOR, err_embed, handle_command_error, info_embed, ok_embed, send_usage, warn_embed
from views.loading import loading
from views.navigate import PaginatorView
from views.giveaway_views import EnterView, LeaveView
from views.common_views import EMBED_COLOR, make_embed

ENTER_CUSTOM_ID = "giveaway:enter"

MIN_SECONDS = 10
MAX_SECONDS = 30 * 86400
MAX_WINNERS = 20
MAX_PRIZE_LENGTH = 200
LIST_PAGE_SIZE = 6
PARTICIPANTS_PAGE_SIZE = 20

_DURATION_FULL = re.compile(r"^(?:\d+[smhdw])+$")
_DURATION_PART = re.compile(r"(\d+)([smhdw])")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_ID_PATTERN = re.compile(r"\d{15,25}")


def parse_duration(text: str) -> typing.Optional[int]:
    """'30m' / '2h' / '1d12h' -> seconds, or None when the format is invalid."""
    text = (text or "").strip().lower().replace(" ", "")
    if not _DURATION_FULL.match(text):
        return None
    return sum(int(number) * _DURATION_UNITS[unit] for number, unit in _DURATION_PART.findall(text))


def format_seconds(seconds: int) -> str:
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        amount, seconds = divmod(seconds, size)
        if amount:
            parts.append(f"{amount}{unit}")
    return " ".join(parts) or "0s"


def parse_giveaway_id(reference: str) -> typing.Optional[int]:
    """Message ID, or a message link (the message ID is the last number in it)."""
    found = _ID_PATTERN.findall(reference or "")
    return int(found[-1]) if found else None


def entry_count(doc: dict) -> int:
    if "entry_count" in doc:
        return int(doc["entry_count"])
    return len(doc.get("participants") or [])


def build_embed(doc: dict, *, ended: bool = False) -> discord.Embed:
    ends = int(doc["ends_at"])
    entries = entry_count(doc)
    winners = doc.get("winner_ids") or []

    if not ended:
        embed = discord.Embed(
            title=f"🎉 {doc['prize']}",
            description=(
                "Press the button below to enter!\n\n"
                f"**Ends:** <t:{ends}:R> (<t:{ends}:f>)\n"
                f"**Hosted by:** <@{doc['host_id']}>\n"
                f"**Winners:** {doc['winners']}\n"
                f"**Entries:** {entries}"
            ),
            color=BRAND_COLOR,
        )
        embed.set_footer(text=f"ID: {doc['_id']}")
        return embed

    winner_text = ", ".join(f"<@{user_id}>" for user_id in winners) if winners else "No valid entries"
    embed = discord.Embed(
        title=f"🎉 {doc['prize']}",
        description=(
            "**This giveaway has ended.**\n\n"
            f"**Winner{'s' if len(winners) != 1 else ''}:** {winner_text}\n"
            f"**Hosted by:** <@{doc['host_id']}>\n"
            f"**Ended:** <t:{int(doc.get('ended_at') or ends)}:R>\n"
            f"**Entries:** {entries}"
        ),
        color=discord.Color.dark_grey(),
    )
    embed.set_footer(text=f"ID: {doc['_id']}")
    return embed


class Giveaway(commands.Cog):
    """Admin-only giveaways."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._refresh_tasks: dict[int, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    @property
    def collection(self):
        return self.bot.mongo_client["utilities"]["giveaways"]

    async def cog_load(self):
        if self.bot.mongo_client is None:
            print("Giveaway Cog: MongoDB is not configured, giveaways are disabled.")
            return
        try:
            await self.collection.create_index([("guild_id", 1), ("created_at", -1)])
            await self.collection.create_index([("ended", 1), ("ends_at", 1)])
        except Exception as e:
            print(f"Giveaway Cog: could not create indexes: {e}")
        self.bot.add_view(EnterView(self))
        self.check_giveaways.start()

    def cog_unload(self):
        self.check_giveaways.cancel()
        for task in self._refresh_tasks.values():
            task.cancel()
        self._refresh_tasks.clear()

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        if (
            isinstance(error, commands.ChannelNotFound)
            and ctx.command is not None
            and ctx.command.qualified_name == "giveaway create"
        ):
            await send_usage(ctx, note="Wrap a multi-word prize in quotes, e.g. `\"Discord Nitro\"`.")
            return
        await handle_command_error(ctx, error)

    # ------------------------------------------------------------------
    # Background loop: end giveaways whose time is up
    # ------------------------------------------------------------------

    @tasks.loop(seconds=10)
    async def check_giveaways(self):
        try:
            due = await self.collection.find(
                {"ended": False, "ends_at": {"$lte": time.time()}},
                {"_id": 1},
            ).to_list(length=None)
        except Exception as e:
            print(f"Giveaway Cog: failed to query due giveaways: {e}")
            return

        for doc in due:
            try:
                await self.end_giveaway(doc["_id"])
            except Exception as e:
                print(f"Giveaway Cog: failed to end giveaway {doc['_id']}: {e}")

    @check_giveaways.before_loop
    async def before_check_giveaways(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Entering / leaving
    # ------------------------------------------------------------------

    async def handle_enter(self, interaction: discord.Interaction):
        message = interaction.message
        doc = await self.collection.find_one({"_id": message.id}, {"participants": 0}) if message else None
        if doc is None or doc.get("ended") or doc["ends_at"] <= time.time():
            return await interaction.response.send_message(
                embed=err_embed("This giveaway has ended."), ephemeral=True
            )

        already = await self.collection.count_documents(
            {"_id": message.id, "participants": interaction.user.id}, limit=1
        )
        if already:
            leave_view = LeaveView(self, message.id)
            await interaction.response.send_message(
                content="You're already in this giveaway.",
                view=leave_view,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            try:
                leave_view.message = await interaction.original_response()
            except discord.HTTPException:
                pass
            return

        result = await self.collection.update_one(
            {"_id": message.id, "ended": False},
            {"$addToSet": {"participants": interaction.user.id}},
        )
        if not result.matched_count:
            return await interaction.response.send_message(
                embed=err_embed("This giveaway has ended."), ephemeral=True
            )
        await interaction.response.send_message(
            embed=ok_embed("You're in!", f"Good luck winning **{doc['prize']}**.", emoji="🎉"),
            ephemeral=True,
        )
        self.schedule_refresh(message.id)

    def schedule_refresh(self, giveaway_id: int):
        """Update the entry count on the giveaway message (several entries are merged into one edit)."""
        if giveaway_id in self._refresh_tasks:
            return
        task = asyncio.create_task(self._refresh_later(giveaway_id))
        self._refresh_tasks[giveaway_id] = task
        task.add_done_callback(lambda _t, gid=giveaway_id: self._refresh_tasks.pop(gid, None))

    async def _refresh_later(self, giveaway_id: int):
        await asyncio.sleep(3)
        doc = await self._get_with_count(giveaway_id)
        if doc is None or doc.get("ended"):
            return
        message = await self._partial_message(doc)
        if message is None:
            return
        try:
            await message.edit(
                embed=build_embed(doc),
                view=EnterView(self, entries=entry_count(doc)),
            )
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------
    # Core helpers
    # ------------------------------------------------------------------

    async def _get_with_count(self, giveaway_id: int) -> typing.Optional[dict]:
        pipeline = [
            {"$match": {"_id": giveaway_id}},
            {"$addFields": {"entry_count": {"$size": {"$ifNull": ["$participants", []]}}}},
            {"$project": {"participants": 0}},
        ]
        docs = await self.collection.aggregate(pipeline).to_list(length=1)
        return docs[0] if docs else None

    async def _partial_message(self, doc: dict) -> typing.Optional[discord.PartialMessage]:
        channel = self.bot.get_channel(doc["channel_id"])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(doc["channel_id"])
            except discord.HTTPException:
                return None
        return channel.get_partial_message(doc["_id"])

    async def _pick_winners(
        self,
        guild: typing.Optional[discord.Guild],
        participants: list[int],
        count: int,
        exclude: typing.Iterable[int] = (),
    ) -> list[int]:
        excluded = set(exclude)
        pool = [user_id for user_id in participants if user_id not in excluded]
        random.shuffle(pool)

        winners: list[int] = []
        for user_id in pool:
            if len(winners) >= count:
                break
            if guild is not None:
                member = guild.get_member(user_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(user_id)
                    except discord.HTTPException:
                        continue  # left the server
                if member.bot:
                    continue
            winners.append(user_id)
        return winners

    async def _announce(self, doc: dict, winners: list[int], *, rerolled: bool = False):
        channel = self.bot.get_channel(doc["channel_id"])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(doc["channel_id"])
            except discord.HTTPException:
                channel = None

        guild = self.bot.get_guild(doc.get("guild_id"))
        jump_url = f"https://discord.com/channels/{doc.get('guild_id', '@me')}/{doc['channel_id']}/{doc['_id']}"
        if winners:
            display_names = []
            for user_id in winners:
                user = self.bot.get_user(user_id)
                if user is None:
                    try:
                        user = await self.bot.fetch_user(user_id)
                    except discord.HTTPException:
                        user = None
                display_names.append(f"**{discord.utils.escape_markdown(user.display_name if isinstance(user, discord.Member) else user.name)}**" if user else f"user `{user_id}`")
            names = ", ".join(display_names)
            if rerolled:
                text = f"🎉 New winner{'s' if len(winners) != 1 else ''} for **{doc['prize']}**: {names}. Congratulations!"
            else:
                text = f"🎉 Congratulations to {names}! You won **{doc['prize']}**."
        else:
            text = f"😔 Nobody won **{doc['prize']}**: there were no valid entries."

        if channel is not None:
            reference = discord.MessageReference(
                message_id=doc["_id"],
                channel_id=doc["channel_id"],
                guild_id=doc.get("guild_id"),
                fail_if_not_exists=False,
            )
            try:
                await channel.send(text, reference=reference, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc:
                print(f"Giveaway Cog: could not announce giveaway {doc['_id']}: {exc}")

        if winners:
            guild_name = guild.name if guild else "the server"
            for user_id in winners:
                user = self.bot.get_user(user_id)
                if user is None:
                    try:
                        user = await self.bot.fetch_user(user_id)
                    except discord.HTTPException:
                        user = None
                if user is None:
                    continue
                dm_embed = make_embed(
                    "🎉 You won a giveaway!",
                    f"You won **{doc['prize']}** in **{guild_name}**.\n\n[View the giveaway message]({jump_url})",
                )
                try:
                    await user.send(embed=dm_embed, allowed_mentions=discord.AllowedMentions.none())
                except (discord.Forbidden, discord.HTTPException) as exc:
                    print(f"Giveaway Cog: could not DM winner {user_id} for giveaway {doc['_id']}: {exc}")

    async def end_giveaway(self, giveaway_id: int) -> typing.Optional[dict]:
        """Ends a running giveaway. Returns the final document, or None if it was not running."""
        claimed = await self.collection.find_one_and_update(
            {"_id": giveaway_id, "ended": False},
            {"$set": {"ended": True, "ended_at": int(time.time())}},
            return_document=ReturnDocument.AFTER,
        )
        if claimed is None:
            return None

        guild = self.bot.get_guild(claimed["guild_id"])
        winners = await self._pick_winners(guild, claimed.get("participants") or [], claimed["winners"])
        await self.collection.update_one({"_id": giveaway_id}, {"$set": {"winner_ids": winners}})

        doc = await self._get_with_count(giveaway_id) or claimed
        message = await self._partial_message(doc)
        if message is not None:
            try:
                await message.edit(embed=build_embed(doc, ended=True), view=EnterView(self, entries=0, disabled=True), allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass
        await self._announce(doc, winners)

        self._refresh_tasks.pop(giveaway_id, None)
        return doc

    async def _resolve(self, ctx: commands.Context, reference: str, *, need_ended: typing.Optional[bool] = None):
        """Giveaway document for a message ID / link given to a command, or None after replying."""
        giveaway_id = parse_giveaway_id(reference)
        if giveaway_id is None:
            await ctx.reply(embed=err_embed("Give a giveaway message ID or link."), mention_author=False)
            return None
        doc = await self._get_with_count(giveaway_id)
        if doc is None or doc.get("guild_id") != ctx.guild.id:
            await ctx.reply(embed=err_embed("No giveaway with that ID in this server."), mention_author=False)
            return None
        if need_ended is True and not doc.get("ended"):
            await ctx.reply(embed=err_embed("That giveaway is still running. End it first."), mention_author=False)
            return None
        if need_ended is False and doc.get("ended"):
            await ctx.reply(embed=err_embed("That giveaway has already ended."), mention_author=False)
            return None
        return doc

    def _db_ready(self) -> bool:
        return self.bot.mongo_client is not None

    async def _require_db(self, ctx: commands.Context) -> bool:
        if self._db_ready():
            return True
        await ctx.reply(embed=err_embed("MongoDB isn't configured, giveaways are unavailable.", emoji="⚠️"), mention_author=False)
        return False

    async def _autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
        ended: typing.Optional[bool],
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild is None or not self._db_ready():
            return []
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms is None or not perms.administrator:
            return []
        query: dict = {"guild_id": interaction.guild.id}
        if ended is not None:
            query["ended"] = ended
        try:
            docs = await self.collection.find(query, {"prize": 1}).sort("created_at", -1).to_list(length=50)
        except Exception:
            return []
        needle = (current or "").strip().lower()
        choices = []
        for doc in docs:
            label = f"{doc['prize'][:70]} • {doc['_id']}"
            if needle and needle not in label.lower():
                continue
            choices.append(app_commands.Choice(name=label[:100], value=str(doc["_id"])))
            if len(choices) >= 25:
                break
        return choices

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.hybrid_group(
        name="giveaway",
        aliases=["gw"],
        invoke_without_command=True,
        description="Create and manage giveaways (administrators only).",
    )
    @app_commands.default_permissions(administrator=True)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def giveaway(self, ctx: commands.Context):
        await send_usage(ctx)

    @giveaway.command(
        name="create",
        aliases=["start"],
        usage="<duration> <winners> <prize> [channel]",
        description="Creates a new giveaway.",
    )
    @app_commands.describe(
        duration="How long it runs, e.g. 30m, 2h, 1d12h (10 seconds to 30 days).",
        winners="How many winners to pick (1-20).",
        prize="What is being given away (use quotes for several words in text commands).",
        channel="Channel to host the giveaway in (defaults to this channel).",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def giveaway_create(
        self,
        ctx: commands.Context,
        duration: str = commands.parameter(description="How long it runs, e.g. 30m, 2h, 1d12h (10 seconds to 30 days)."),
        winners: int = commands.parameter(description="How many winners to pick (1-20)."),
        prize: str = commands.parameter(description="What is being given away (use quotes for several words)."),
        channel: typing.Optional[discord.TextChannel] = commands.parameter(
            default=None,
            description="Channel to host the giveaway in (defaults to this channel).",
        ),
    ):
        if not await self._require_db(ctx):
            return

        seconds = parse_duration(duration)
        if seconds is None:
            return await ctx.reply(
                embed=err_embed("Invalid duration. Use something like `30m`, `2h` or `1d12h`."), mention_author=False
            )
        if not (MIN_SECONDS <= seconds <= MAX_SECONDS):
            return await ctx.reply(
                embed=err_embed(f"Duration must be between {format_seconds(MIN_SECONDS)} and {format_seconds(MAX_SECONDS)}."),
                mention_author=False,
            )
        if not (1 <= winners <= MAX_WINNERS):
            return await ctx.reply(embed=err_embed(f"Winners must be between 1 and {MAX_WINNERS}."), mention_author=False)

        prize = prize.strip()
        if not prize:
            return await ctx.reply(embed=err_embed("Give a prize."), mention_author=False)
        if len(prize) > MAX_PRIZE_LENGTH:
            return await ctx.reply(
                embed=err_embed(f"The prize can be at most {MAX_PRIZE_LENGTH} characters."), mention_author=False
            )

        target = channel or ctx.channel
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            return await ctx.reply(embed=err_embed("Pick a text channel."), mention_author=False)
        perms = target.permissions_for(ctx.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await ctx.reply(
                embed=err_embed(f"I need View Channel, Send Messages and Embed Links in {target.mention}."),
                mention_author=False,
            )

        ends_at = int(time.time()) + seconds
        doc = {
            "guild_id": ctx.guild.id,
            "channel_id": target.id,
            "host_id": ctx.author.id,
            "prize": prize,
            "winners": winners,
            "ends_at": ends_at,
            "created_at": int(time.time()),
            "ended": False,
            "participants": [],
            "winner_ids": [],
        }
        doc["_id"] = 0  # placeholder for the embed, replaced by the real message ID below

        try:
            message = await target.send(embed=build_embed(doc), view=EnterView(self))
        except discord.HTTPException as e:
            return await ctx.reply(embed=err_embed(f"Couldn't post the giveaway: {e.text or e}"), mention_author=False)

        doc["_id"] = message.id
        try:
            await self.collection.insert_one(doc)
        except Exception as e:
            print(f"Giveaway Cog: failed to save giveaway {message.id}: {e}")
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            return await ctx.reply(embed=err_embed("Couldn't save the giveaway. Please try again.", emoji="⚠️"), mention_author=False)

        try:
            await message.edit(embed=build_embed(doc))  # footer now shows the real ID
        except discord.HTTPException:
            pass

        embed = ok_embed(
            "Giveaway Created",
            f"**{prize}** in {target.mention}\n"
            f"Ends <t:{ends_at}:R> • {winners} winner{'s' if winners != 1 else ''}\n"
            f"[Jump to giveaway]({message.jump_url}) • ID `{message.id}`",
            emoji="🎉",
        )
        await ctx.reply(embed=embed, mention_author=False)

    @giveaway.command(
        name="end",
        usage="<giveaway>",
        description="Ends a giveaway.",
    )
    @app_commands.describe(giveaway="The giveaway to end (pick from the list, or paste its message ID or link).")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def giveaway_end(
        self,
        ctx: commands.Context,
        giveaway: str = commands.parameter(description="The giveaway to end (message ID or link)."),
    ):
        if not await self._require_db(ctx):
            return
        doc = await self._resolve(ctx, giveaway, need_ended=False)
        if doc is None:
            return
        final = await self.end_giveaway(doc["_id"])
        if final is None:
            return await ctx.reply(embed=err_embed("That giveaway has already ended."), mention_author=False)

        winners = final.get("winner_ids") or []
        text = ", ".join(f"<@{user_id}>" for user_id in winners) if winners else "No valid entries."
        await ctx.reply(
            embed=ok_embed("Giveaway Ended", f"**{final['prize']}**\nWinners: {text}", emoji="🏁"),
            mention_author=False,
        )

    @giveaway_end.autocomplete("giveaway")
    async def giveaway_end_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._autocomplete(interaction, current, ended=False)

    @giveaway.command(
        name="list",
        description="Shows the list of active and ended giveaways.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @loading
    async def giveaway_list(self, ctx: commands.Context):
        if not await self._require_db(ctx):
            return
        pipeline = [
            {"$match": {"guild_id": ctx.guild.id}},
            {"$sort": {"created_at": -1}},
            {"$limit": 100},
            {"$addFields": {"entry_count": {"$size": {"$ifNull": ["$participants", []]}}}},
            {"$project": {"participants": 0}},
        ]
        docs = await self.collection.aggregate(pipeline).to_list(length=100)
        if not docs:
            return await ctx.reply(
                embed=info_embed("No Giveaways", "This server has no giveaways yet.", emoji="📭"), mention_author=False
            )

        active = [d for d in docs if not d.get("ended")]
        active.sort(key=lambda d: d["ends_at"])
        ended = [d for d in docs if d.get("ended")]

        lines: list[str] = []
        for doc in active + ended:
            link = f"https://discord.com/channels/{doc['guild_id']}/{doc['channel_id']}/{doc['_id']}"
            entries = entry_count(doc)
            if doc.get("ended"):
                status = f"🔴 Ended <t:{int(doc.get('ended_at') or doc['ends_at'])}:R>"
            else:
                status = f"🟢 Ends <t:{int(doc['ends_at'])}:R>"
            lines.append(
                f"**[{doc['prize'][:60]}]({link})**\n"
                f"{status} • {entries} entr{'y' if entries == 1 else 'ies'} • {doc['winners']} winner(s)\n"
                f"ID `{doc['_id']}`"
            )

        pages = []
        total = (len(lines) + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE
        for number in range(total):
            chunk = lines[number * LIST_PAGE_SIZE:(number + 1) * LIST_PAGE_SIZE]
            embed = discord.Embed(
                title=f"🎁 Giveaways ({len(active)} active, {len(ended)} ended)",
                description="\n\n".join(chunk),
                color=BRAND_COLOR,
            )
            embed.set_footer(text=f"Page {number + 1}/{total}")
            pages.append(embed)

        if len(pages) == 1:
            return await ctx.reply(embed=pages[0], mention_author=False)
        view = PaginatorView(pages, user_id=ctx.author.id)
        view.message = await ctx.reply(embed=pages[0], view=view, mention_author=False)

    @giveaway.command(
        name="participants",
        aliases=["entries"],
        usage="<giveaway>",
        description="Shows the list of participants in a giveaway.",
    )
    @app_commands.describe(giveaway="The giveaway to inspect (pick from the list, or paste its message ID or link).")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @loading
    async def giveaway_participants(
        self,
        ctx: commands.Context,
        giveaway: str = commands.parameter(description="The giveaway to inspect (message ID or link)."),
    ):
        if not await self._require_db(ctx):
            return
        doc = await self._resolve(ctx, giveaway)
        if doc is None:
            return
        full = await self.collection.find_one({"_id": doc["_id"]}, {"participants": 1})
        participants = (full or {}).get("participants") or []
        if not participants:
            return await ctx.reply(
                embed=info_embed("No Participants", f"Nobody has entered **{doc['prize']}** yet.", emoji="📭"),
                mention_author=False,
            )

        total = (len(participants) + PARTICIPANTS_PAGE_SIZE - 1) // PARTICIPANTS_PAGE_SIZE
        pages = []
        for number in range(total):
            start = number * PARTICIPANTS_PAGE_SIZE
            chunk = participants[start:start + PARTICIPANTS_PAGE_SIZE]
            embed = discord.Embed(
                title=f"👥 {doc['prize'][:200]}",
                description="\n".join(f"{start + i + 1}. <@{user_id}>" for i, user_id in enumerate(chunk)),
                color=BRAND_COLOR,
            )
            embed.set_footer(text=f"{len(participants)} participant(s) • Page {number + 1}/{total}")
            pages.append(embed)

        if len(pages) == 1:
            return await ctx.reply(embed=pages[0], mention_author=False)
        view = PaginatorView(pages, user_id=ctx.author.id)
        view.message = await ctx.reply(embed=pages[0], view=view, mention_author=False)

    @giveaway_participants.autocomplete("giveaway")
    async def giveaway_participants_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._autocomplete(interaction, current, ended=None)

    @giveaway.command(
        name="reroll",
        usage="<giveaway> [winners]",
        description="Rolls new winners for the specified giveaway.",
    )
    @app_commands.describe(
        giveaway="The ended giveaway to reroll (pick from the list, or paste its message ID or link).",
        winners="How many new winners to pick (1-20). Defaults to the original number.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def giveaway_reroll(
        self,
        ctx: commands.Context,
        giveaway: str = commands.parameter(description="The ended giveaway to reroll (message ID or link)."),
        winners: typing.Optional[int] = commands.parameter(
            default=None,
            description="How many new winners to pick (1-20). Defaults to the original number.",
        ),
    ):
        if not await self._require_db(ctx):
            return
        doc = await self._resolve(ctx, giveaway, need_ended=True)
        if doc is None:
            return

        count = winners if winners is not None else doc["winners"]
        if not (1 <= count <= MAX_WINNERS):
            return await ctx.reply(embed=err_embed(f"Winners must be between 1 and {MAX_WINNERS}."), mention_author=False)

        full = await self.collection.find_one({"_id": doc["_id"]}, {"participants": 1})
        participants = (full or {}).get("participants") or []
        previous = doc.get("winner_ids") or []

        # Prefer people who haven't won yet; fall back to everyone if that leaves nobody.
        new_winners = await self._pick_winners(ctx.guild, participants, count, exclude=previous)
        if not new_winners:
            new_winners = await self._pick_winners(ctx.guild, participants, count)
        if not new_winners:
            return await ctx.reply(
                embed=warn_embed("No Valid Entries", "There is nobody left to pick as a winner."), mention_author=False
            )

        await self.collection.update_one({"_id": doc["_id"]}, {"$set": {"winner_ids": new_winners}})
        doc["winner_ids"] = new_winners
        message = await self._partial_message(doc)
        if message is not None:
            try:
                await message.edit(embed=build_embed(doc, ended=True))
            except discord.HTTPException:
                pass
        await self._announce(doc, new_winners, rerolled=True)

        mentions = ", ".join(f"<@{user_id}>" for user_id in new_winners)
        await ctx.reply(
            embed=ok_embed("Giveaway Rerolled", f"**{doc['prize']}**\nNew winners: {mentions}", emoji="🔄"),
            mention_author=False,
        )

    @giveaway_reroll.autocomplete("giveaway")
    async def giveaway_reroll_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._autocomplete(interaction, current, ended=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Giveaway(bot))