import asyncio
import datetime

import discord
from discord.ext import commands, tasks

from cogs.poketwo_helper.lockcommon import LOCK_COLOR, SHORT_NAMES, WARN_COLOR, stamp
from views.embeds import handle_command_error
from views.lockdm_views import build_lockdm_page

DEFAULT_SETTINGS = {
    "enabled": True,          # master switch
    "on_lock": True,          # DM when the channel is autolocked
    "before_unlock": None,    # seconds before auto-unlock to DM, None = off
}

REMINDER_CHECK_SECONDS = 5
DM_PAUSE = 0.3   # small gap between DMs so a big ping list never hits a rate limit


def _merge(doc: dict | None) -> dict:
    settings = dict(DEFAULT_SETTINGS)
    if doc:
        settings.update({k: doc[k] for k in DEFAULT_SETTINGS if k in doc})
    return settings


def _links(ping_url: str | None, lock_url: str | None) -> str:
    links = []
    if ping_url:
        links.append(f"[Jump to the ping]({ping_url})")
    if lock_url:
        links.append(f"[Jump to the lock message]({lock_url})")
    return " • ".join(links)


class LockDM(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # --- Mongo ------------------------------------------------------------------

    @property
    def db(self):
        return self.bot.mongo_client["utilities"]

    @property
    def settings_collection(self):
        return self.db["lockdm"]

    @property
    def reminders_collection(self):
        return self.db["lockdm_reminders"]

    @property
    def locks_collection(self):
        return self.db["locked_channels"]

    @property
    def prefix(self) -> str:
        return getattr(self.bot, "prefix_str", ".")

    async def cog_load(self):
        try:
            await self.reminders_collection.create_index("at")
        except Exception as e:
            print(f"LockDM: could not create reminder index: {e}")
        self.reminder_loop.start()

    async def cog_unload(self):
        self.reminder_loop.cancel()

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        await handle_command_error(ctx, error)

    # --- settings ---------------------------------------------------------------

    @staticmethod
    def _key(guild_id: int, user_id: int) -> str:
        return f"{guild_id}:{user_id}"

    async def get_settings(self, guild_id: int, user_id: int) -> dict:
        doc = await self.settings_collection.find_one({"_id": self._key(guild_id, user_id)})
        return _merge(doc)

    async def update_settings(self, guild_id: int, user_id: int, **fields) -> None:
        fields = {k: v for k, v in fields.items() if k in DEFAULT_SETTINGS}
        await self.settings_collection.update_one(
            {"_id": self._key(guild_id, user_id)},
            {"$set": {**fields, "guild_id": guild_id, "user_id": user_id}},
            upsert=True,
        )

    async def get_unlock_limit(self, guild_id: int) -> int | None:
        """Shortest auto-unlock time (seconds) admins configured here; None = no auto-unlock,
        in which case members can only choose "DM when autolocked"."""
        config = self.bot.get_cog("AutoLockConfig")
        if config is None:
            return None
        return await config.get_min_locktime(guild_id)

    # --- sending ----------------------------------------------------------------

    async def _send_dm(self, user_id: int, embed: discord.Embed) -> bool:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(embed=embed)
            return True
        except discord.HTTPException:
            return False  # DMs closed, user gone, ... nothing to do

    def _lock_embed(self, channel, categories, locked_at, unlock_at, ping_url, lock_url) -> discord.Embed:
        label = "/".join(SHORT_NAMES.get(c, c) for c in categories)
        embed = discord.Embed(
            title="🔒 Channel Locked",
            description=f"{channel.mention} in **{channel.guild.name}** was autolocked (`{label}`).",
            color=LOCK_COLOR,
        )
        embed.add_field(name="🕒 Locked", value=stamp(locked_at), inline=True)
        if unlock_at:
            embed.add_field(
                name="⏳ Unlocks Automatically",
                value=f"{stamp(unlock_at)} (<t:{unlock_at}:t>)",
                inline=True,
            )
        links = _links(ping_url, lock_url)
        if links:
            embed.add_field(name="🔗 Links", value=links, inline=False)
        embed.set_footer(text=f"Run {self.prefix}lockdm in {channel.guild.name} to change these DMs.")
        return embed

    async def notify_autolock(
        self,
        *,
        channel: discord.abc.GuildChannel,
        user_ids: set[int],
        categories: list[str],
        locked_at: int,
        unlock_at: int | None,
        ping_url: str | None,
        lock_url: str | None,
    ) -> None:
        """Called by AutoLock (in the background) right after it locked ``channel``.

        ``user_ids`` are the members @mentioned in the ping message. Each member's own
        settings decide whether they get a DM now and/or a reminder before the unlock.
        """
        try:
            guild = channel.guild
            docs = await self.settings_collection.find(
                {"guild_id": guild.id, "user_id": {"$in": list(user_ids)}}
            ).to_list(length=None)
            stored = {doc["user_id"]: doc for doc in docs}

            duration = (unlock_at - locked_at) if unlock_at else None
            to_dm: list[int] = []
            reminders: list[dict] = []

            for user_id in user_ids:
                settings = _merge(stored.get(user_id))
                if not settings["enabled"]:
                    continue
                if settings["on_lock"]:
                    to_dm.append(user_id)

                before = settings["before_unlock"]
                # The reminder has to land after the lock itself, so it must be shorter than
                # this lock's duration (members can't set it longer than the shortest one).
                if unlock_at and before and before < duration:
                    reminders.append({
                        "guild_id": guild.id,
                        "channel_id": channel.id,
                        "user_id": user_id,
                        "at": datetime.datetime.fromtimestamp(unlock_at - before, tz=datetime.timezone.utc),
                        "unlock_at": datetime.datetime.fromtimestamp(unlock_at, tz=datetime.timezone.utc),
                        "ping_url": ping_url,
                        "lock_url": lock_url,
                    })

            # Reminders first: that's one quick insert, the DMs below can take a while.
            if reminders:
                await self.reminders_collection.insert_many(reminders)

            if to_dm:
                embed = self._lock_embed(channel, categories, locked_at, unlock_at, ping_url, lock_url)
                for user_id in to_dm:
                    await self._send_dm(user_id, embed)
                    await asyncio.sleep(DM_PAUSE)
        except Exception as e:
            print(f"LockDM: failed to notify for #{getattr(channel, 'id', '?')}: {e}")

    # --- "before unlock" reminders ------------------------------------------------

    @tasks.loop(seconds=REMINDER_CHECK_SECONDS)
    async def reminder_loop(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        while True:
            try:
                # Claim one due reminder (atomic, so it can never be sent twice).
                doc = await self.reminders_collection.find_one_and_delete({"at": {"$lte": now}})
            except Exception as e:
                print(f"LockDM: failed to read due reminders: {e}")
                return
            if doc is None:
                return
            try:
                await self._send_reminder(doc)
            except Exception as e:
                print(f"LockDM: failed to send a reminder: {e}")

    @reminder_loop.before_loop
    async def _before_reminder_loop(self):
        await self.bot.wait_until_ready()

    async def _send_reminder(self, doc: dict) -> None:
        # Drop it when that lock is gone (unlocked by hand) or was replaced by a newer lock.
        still_locked = await self.locks_collection.find_one(
            {"_id": doc["channel_id"], "unlock_at": doc["unlock_at"]}
        )
        if not still_locked:
            return

        settings = await self.get_settings(doc["guild_id"], doc["user_id"])
        if not settings["enabled"]:
            return

        guild = self.bot.get_guild(doc["guild_id"])
        guild_name = guild.name if guild else "the server"
        unlock_unix = int(doc["unlock_at"].replace(tzinfo=datetime.timezone.utc).timestamp())

        embed = discord.Embed(
            title="⏳ Channel Unlocking Soon",
            description=f"<#{doc['channel_id']}> in **{guild_name}** unlocks automatically {stamp(unlock_unix)}.",
            color=WARN_COLOR,
        )
        links = _links(doc.get("ping_url"), doc.get("lock_url"))
        if links:
            embed.add_field(name="🔗 Links", value=links, inline=False)
        embed.set_footer(text=f"Run {self.prefix}lockdm in {guild_name} to change these DMs.")
        await self._send_dm(doc["user_id"], embed)

    # --- .lockdm -------------------------------------------------------------------

    @commands.command(
        name="lockdm",
        aliases=["ldm"],
        description="See and change your lock DM settings.",
    )
    @commands.guild_only()
    async def lockdm(self, ctx: commands.Context):
        view = await build_lockdm_page(self, ctx.guild, ctx.author.id)
        view.message = await ctx.reply(view=view, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(LockDM(bot))