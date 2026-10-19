"""Invite tracker.

* ``invites``          show how many people you invited (total, still here, left, rejoins)
* ``invites @user``    administrators only: show the invites of someone else

How it works
------------
The cog keeps a snapshot of every invite's use counter per server. When someone joins, a fresh
snapshot is taken and compared with the old one: the invite whose counter went up is the one that
was used (an invite that vanished because it hit its max uses is detected too, and so is the
vanity URL). The result is stored in MongoDB (``utilities.invite_members``), one document per
member, so leaves and rejoins can be counted later.

* Rejoins never add a new invite for the inviter, they are counted separately.
* Bots are ignored.
* The bot needs the Manage Server permission to read invites.

The welcome message (``cogs/community/joins.py``) asks this cog who invited the new member through
``process_join`` and shows "Invited by ..." when the admins left that option on.
"""
from __future__ import annotations

import asyncio
import time
import typing
from dataclasses import dataclass

import discord
from discord.ext import commands

from views.embeds import err_embed, handle_command_error, info_embed

VANITY_KEY = "vanity"
RESULT_TIMEOUT = 8.0
_NO_PINGS = discord.AllowedMentions.none()


@dataclass
class InviteStats:
    total: int = 0
    present: int = 0
    rejoins: int = 0

    @property
    def left(self) -> int:
        return max(0, self.total - self.present)


@dataclass
class JoinInfo:
    kind: str  # "invite", "vanity" or "unknown"
    inviter_id: int | None = None
    code: str | None = None
    rejoin: bool = False
    stats: InviteStats | None = None


class Invites(commands.Cog):
    """Tracks who invited whom."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cache: dict[int, dict[str, tuple[int, int, int | None]] | None] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._tasks: dict[tuple[int, int], asyncio.Task] = {}
        self._warm_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    @property
    def members_col(self):
        return self.bot.mongo_client["utilities"]["invite_members"]

    def _db_ready(self) -> bool:
        return self.bot.mongo_client is not None

    async def cog_load(self):
        if not self._db_ready():
            print("Invites Cog: MongoDB is not configured, the invite tracker is disabled.")
            return
        try:
            await self.members_col.create_index([("guild_id", 1), ("user_id", 1)], unique=True)
            await self.members_col.create_index([("guild_id", 1), ("inviter_id", 1)])
        except Exception as e:
            print(f"Invites Cog: could not create indexes: {e}")
        self._warm_task = asyncio.create_task(self._warm_cache())

    def cog_unload(self):
        if self._warm_task:
            self._warm_task.cancel()
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        await handle_command_error(ctx, error)

    async def _warm_cache(self):
        await self.bot.wait_until_ready()
        for guild in list(self.bot.guilds):
            await self._refresh_snapshot(guild)
            await asyncio.sleep(0.5)

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    async def _snapshot(self, guild: discord.Guild) -> dict[str, tuple[int, int, int | None]] | None:
        """code -> (uses, max_uses, inviter_id). None when the bot may not read invites."""
        if not guild.me or not guild.me.guild_permissions.manage_guild:
            return None
        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            return None

        snap = {
            invite.code: (invite.uses or 0, invite.max_uses or 0, invite.inviter.id if invite.inviter else None)
            for invite in invites
        }
        if "VANITY_URL" in guild.features:
            try:
                vanity = await guild.vanity_invite()
                if vanity is not None:
                    snap[VANITY_KEY] = (vanity.uses or 0, 0, None)
            except (discord.Forbidden, discord.HTTPException):
                pass
        return snap

    async def _refresh_snapshot(self, guild: discord.Guild):
        async with self._lock(guild.id):
            self._cache[guild.id] = await self._snapshot(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self._refresh_snapshot(guild)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        self._cache.pop(guild.id, None)
        self._locks.pop(guild.id, None)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        if invite.guild is None:
            return
        cached = self._cache.get(invite.guild.id)
        if cached is not None:
            cached[invite.code] = (invite.uses or 0, invite.max_uses or 0, invite.inviter.id if invite.inviter else None)

    # ------------------------------------------------------------------
    # Join / leave tracking
    # ------------------------------------------------------------------

    def _detect(self, before: dict | None, after: dict | None) -> tuple[str, int | None, str | None]:
        if before is None or after is None:
            return "unknown", None, None

        used = [code for code, (uses, _max, _inv) in after.items() if uses > before.get(code, (0, 0, None))[0]]
        if not used:
            # An invite that reached its max uses is deleted by Discord, so it is missing in `after`.
            used = [
                code for code, (uses, max_uses, _inv) in before.items()
                if code not in after and max_uses and uses + 1 >= max_uses
            ]
        if len(used) != 1:
            return "unknown", None, None

        code = used[0]
        if code == VANITY_KEY:
            return "vanity", None, VANITY_KEY
        inviter_id = (after.get(code) or before.get(code))[2]
        return "invite", inviter_id, code

    async def process_join(self, member: discord.Member) -> JoinInfo | None:
        """Work out (once) who invited ``member``. Safe to call from several cogs at the same time."""
        if member.bot or not self._db_ready():
            return None
        key = (member.guild.id, member.id)
        task = self._tasks.get(key)
        if task is None:
            task = asyncio.create_task(self._process_join(member))
            self._tasks[key] = task
            task.add_done_callback(
                lambda _t, k=key: asyncio.get_running_loop().call_later(60, self._tasks.pop, k, None)
            )
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=RESULT_TIMEOUT)
        except asyncio.TimeoutError:
            return None

    async def _process_join(self, member: discord.Member) -> JoinInfo | None:
        guild = member.guild
        try:
            async with self._lock(guild.id):
                before = self._cache.get(guild.id)
                after = await self._snapshot(guild)
                self._cache[guild.id] = after
                kind, inviter_id, code = self._detect(before, after)

            now = int(time.time())
            record = await self.members_col.find_one({"guild_id": guild.id, "user_id": member.id})
            rejoin = record is not None
            if record is None:
                await self.members_col.insert_one({
                    "guild_id": guild.id,
                    "user_id": member.id,
                    "inviter_id": inviter_id,
                    "code": code,
                    "in_server": True,
                    "join_count": 1,
                    "first_joined": now,
                    "last_joined": now,
                })
            else:
                changes = {"in_server": True, "last_joined": now}
                if record.get("inviter_id") is None and inviter_id is not None:
                    changes["inviter_id"] = inviter_id
                    changes["code"] = code
                await self.members_col.update_one(
                    {"_id": record["_id"]}, {"$set": changes, "$inc": {"join_count": 1}}
                )

            stats = await self.get_stats(guild.id, inviter_id) if inviter_id else None
            return JoinInfo(kind=kind, inviter_id=inviter_id, code=code, rejoin=rejoin, stats=stats)
        except Exception as e:
            print(f"Invites Cog: could not process join of {member.id} in {guild.id}: {e}")
            return None

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self.process_join(member)

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent):
        if not self._db_ready() or payload.user.bot:
            return
        self._tasks.pop((payload.guild_id, payload.user.id), None)
        try:
            await self.members_col.update_one(
                {"guild_id": payload.guild_id, "user_id": payload.user.id},
                {"$set": {"in_server": False, "left_at": int(time.time())}},
            )
        except Exception as e:
            print(f"Invites Cog: could not mark {payload.user.id} as left in {payload.guild_id}: {e}")

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    async def get_stats(self, guild_id: int, user_id: int) -> InviteStats:
        pipeline = [
            {"$match": {"guild_id": guild_id, "inviter_id": user_id}},
            {"$group": {
                "_id": None,
                "total": {"$sum": 1},
                "present": {"$sum": {"$cond": ["$in_server", 1, 0]}},
                "rejoins": {"$sum": {"$subtract": ["$join_count", 1]}},
            }},
        ]
        docs = await self.members_col.aggregate(pipeline).to_list(length=1)
        if not docs:
            return InviteStats()
        doc = docs[0]
        return InviteStats(total=doc["total"], present=doc["present"], rejoins=doc["rejoins"])

    # ------------------------------------------------------------------
    # Command
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="invites",
        aliases=["inv"],
        usage="[member]",
        description="Shows how many people you invited (administrators can look up other members).",
        extras={"args": {"member": "Administrators only: the member to look up."}},
    )
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def invites(
        self,
        ctx: commands.Context,
        member: typing.Optional[typing.Union[discord.Member, discord.User]] = None,
    ):
        if not self._db_ready():
            return await ctx.reply(embed=err_embed("MongoDB isn't configured, the invite tracker is unavailable.", emoji="⚠️"), mention_author=False)

        target = member or ctx.author
        if target.id != ctx.author.id and not ctx.author.guild_permissions.administrator:
            return await ctx.reply(embed=err_embed("Only administrators can look at other members' invites."), mention_author=False)

        stats, record = await asyncio.gather(
            self.get_stats(ctx.guild.id, target.id),
            self.members_col.find_one({"guild_id": ctx.guild.id, "user_id": target.id}),
        )

        name = getattr(target, "display_name", None) or target.name
        embed = info_embed(f"{name}'s Invites", f"{target.mention} has invited **{stats.total:,}** member{'s' if stats.total != 1 else ''}.", emoji="📨")
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="✅ Still here", value=f"{stats.present:,}", inline=True)
        embed.add_field(name="🚪 Left", value=f"{stats.left:,}", inline=True)
        embed.add_field(name="🔁 Rejoins", value=f"{stats.rejoins:,}", inline=True)

        if record and record.get("inviter_id"):
            embed.add_field(name="📥 Invited by", value=f"<@{record['inviter_id']}>", inline=False)
        elif record and record.get("code") == VANITY_KEY:
            embed.add_field(name="📥 Joined via", value="The server's vanity URL", inline=False)

        if ctx.guild.id in self._cache and self._cache[ctx.guild.id] is None:
            embed.set_footer(text="I need the Manage Server permission to track new invites.")
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=_NO_PINGS)


async def setup(bot: commands.Bot):
    await bot.add_cog(Invites(bot))