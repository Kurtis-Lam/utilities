import asyncio
import datetime

import discord
from discord.ext import commands, tasks

from cogs.poketwo_helper.lockcommon import (
    DEFAULT_DELAY,
    SHORT_NAMES,
    UNLOCK_PRIORITY,
    WARN_COLOR,
    auto_unlocked_embed,
    get_poketwo_target,
    is_locked_overwrite,
    locked_embed,
    now_unix,
)
from cogs.poketwo_helper.lockunlock import NO_PINGS
from views.lockunlock_views import UnlockLayout

# How often the auto-unlock loop looks for locks whose timer has run out.
AUTO_UNLOCK_CHECK_SECONDS = 5


def _channel_in_whitelist(channel, whitelist: list) -> bool:
    """Empty whitelist == no channels will autolock. '*' == whole server."""
    if not whitelist:
        return False
    entries = {str(w) for w in whitelist}
    if "*" in entries:
        return True
    if str(channel.id) in entries:
        return True
    category_id = getattr(channel, "category_id", None)
    if category_id and str(category_id) in entries:
        return True
    return False


class AutoLock(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pending_locks = set()
        self._background_tasks = set()

    async def cog_load(self):
        self.auto_unlock_loop.start()

    async def cog_unload(self):
        self.auto_unlock_loop.cancel()

    # Shared Mongo client from main.py (no connection string lives in this file).
    @property
    def db(self):
        return self.bot.mongo_client["utilities"]

    @property
    def locks_collection(self):
        return self.db["locked_channels"]

    # Autolock's own on/off/delay/whitelist/restrict settings — both the guild-wide
    # ("--global") defaults and any per-channel overrides — live in AutoLockConfig.
    # We read through that cog rather than Mongo directly so every reader/writer
    # (this cog, .set, .toggle, the config UI) always agrees on the effective value.
    @property
    def config_cog(self):
        return self.bot.get_cog("AutoLockConfig")

    def _resolve_unlockers(self, cat_configs: dict[str, dict], active: list[str], category_users: dict):
        """
        Highest-priority active lock with 'restrict unlockers' ON (for this channel,
        after applying any channel override) decides who may unlock.
        Returns (allowed_user_ids | None, restricting_categories).
        None means anyone can unlock.
        """
        for tier in UNLOCK_PRIORITY:
            tier_cats = [
                c for c in tier
                if c in active and cat_configs.get(c, {}).get("restrict_unlockers", False)
            ]
            users = set()
            for c in tier_cats:
                users |= {int(u) for u in category_users.get(c, ())}
            if users:
                return sorted(users), tier_cats
        return None, []

    async def process_autolock(
        self,
        channel: discord.TextChannel,
        activated_categories: list[str],
        category_users: dict[str, set[int]] | None = None,
        pinged_users: set[int] | None = None,
        ping_url: str | None = None,
    ):
        """
        Called directly by Recognizer when a Pokémon is identified.

        activated_categories: categories that matched this spawn (re, sh, cl, tp, rp, rare, ...)
        category_users:       {"re": {uid, ...}, "sh": {...}, ...} users pinged per category
        pinged_users:         users actually @mentioned in the ping message (never role pings);
                              these are the people LockDM may DM once the channel is locked
        ping_url:             jump link of the ping message, included in those DMs
        """
        if channel.id in self.pending_locks:
            return

        config_cog = self.config_cog
        if not config_cog:
            print("AutoLock: AutoLockConfig cog is not loaded, skipping autolock check.")
            return

        category_users = category_users or {}

        # Effective config per category = guild-wide default with any channel-specific
        # override (set via .set/.toggle without --global) applied on top. Only
        # categories that are enabled *and* whose (guild-wide) whitelist covers this
        # channel take part.
        active = []
        cat_configs: dict[str, dict] = {}
        for cat in dict.fromkeys(activated_categories):
            cfg = await config_cog.get_category_config_channel(channel.guild.id, channel.id, cat)
            if cfg.get("enabled", False) and _channel_in_whitelist(channel, cfg.get("whitelist", [])):
                active.append(cat)
                cat_configs[cat] = cfg

        if not active:
            return

        # Delay: a category with delay OFF locks immediately; otherwise use its delay.
        # With several categories the shortest wait wins.
        delay = min(
            (
                cat_configs[cat].get("delay", DEFAULT_DELAY)
                if cat_configs[cat].get("delay_enabled", True)
                else 0
            )
            for cat in active
        )

        # Auto-unlock: with several categories the shortest configured lock time wins;
        # categories that have it switched off don't take part.
        locktimes = [
            int(cat_configs[cat]["locktime"])
            for cat in active
            if cat_configs[cat].get("locktime_enabled", False) and cat_configs[cat].get("locktime")
        ]
        locktime = min(locktimes) if locktimes else None

        allowed_unlockers, restrict_cats = self._resolve_unlockers(cat_configs, active, category_users)
        label = "/".join(SHORT_NAMES.get(c, c) for c in active)

        self.pending_locks.add(channel.id)
        try:
            if delay > 0:
                status_msg = await channel.send(
                    embed=discord.Embed(
                        title="⏳ Auto-Lock",
                        description=f"`{label}` matched. Locking <t:{now_unix() + delay}:R>.",
                        color=WARN_COLOR,
                    )
                )
                caught = await self._wait_for_catch(channel, delay)
                try:
                    await status_msg.delete()
                except discord.HTTPException:
                    pass
                if caught:
                    return

            await self._lock_channel(
                channel,
                active,
                allowed_unlockers,
                restrict_cats,
                locktime=locktime,
                pinged_users=pinged_users or set(),
                ping_url=ping_url,
            )
        finally:
            self.pending_locks.discard(channel.id)

    async def _wait_for_catch(self, channel: discord.TextChannel, delay: int) -> bool:
        """True if the Pokémon was caught before the delay ran out."""
        def poketwo_check(m: discord.Message) -> bool:
            return (
                m.author.id == self.bot.poketwo_id
                and m.channel.id == channel.id
                and m.content.startswith("Congratulations")
            )

        try:
            await self.bot.wait_for("message", check=poketwo_check, timeout=delay)
            return True
        except asyncio.TimeoutError:
            return False

    async def _lock_channel(
        self,
        channel: discord.TextChannel,
        active: list[str],
        allowed_unlockers: list[int] | None,
        restrict_cats: list[str],
        locktime: int | None = None,
        pinged_users: set[int] | None = None,
        ping_url: str | None = None,
    ):
        target = await get_poketwo_target(self.bot, channel.guild)
        if target is None:
            print(f"AutoLock: Poketwo not found in guild {channel.guild.id}, can't lock #{channel.id}")
            return

        locked_unix = now_unix()
        unlock_unix = locked_unix + locktime if locktime else None

        # Sync lock state to MongoDB *before* locking so /unlock checks (lockunlock.py,
        # the Unlock button) always see the restriction as soon as the channel is locked.
        # `unlock_at` is what the auto-unlock loop below watches (survives restarts).
        try:
            await self.locks_collection.update_one(
                {"_id": channel.id},
                {"$set": {
                    "guild_id": channel.guild.id,
                    "allowed_users": allowed_unlockers,   # None = anyone can unlock
                    "source": "autolock",
                    "categories": active,
                    "restricted_by": restrict_cats,
                    "locked_at": datetime.datetime.fromtimestamp(locked_unix, tz=datetime.timezone.utc),
                    "locktime": locktime,
                    "unlock_at": (
                        datetime.datetime.fromtimestamp(unlock_unix, tz=datetime.timezone.utc)
                        if unlock_unix
                        else None
                    ),
                }},
                upsert=True,
            )
        except Exception as e:
            print(f"AutoLock: failed to sync lock state to MongoDB: {e}")

        try:
            await channel.set_permissions(target, view_channel=False, send_messages=False)
        except discord.HTTPException as e:
            print(f"AutoLock: failed to lock #{channel.id}: {e}")
            try:
                await self.locks_collection.delete_one({"_id": channel.id})
            except Exception:
                pass
            return

        lock_embed = locked_embed(
            channel,
            trigger="/".join(SHORT_NAMES.get(c, c) for c in active),
            allowed_users=allowed_unlockers,
            restricted_by=restrict_cats,
            when=locked_unix,
            unlock_at=unlock_unix,
        )
        # The Unlock button lives inside the lock embed (shared with .lock). It is
        # persistent (registered by the LockUnlock cog), so it survives restarts.
        msg = None
        try:
            msg = await channel.send(
                view=UnlockLayout(self.bot, lock_embed),
                allowed_mentions=NO_PINGS,
            )
            # Remember the message so .unlock / the auto-unlock can edit it later.
            await self.locks_collection.update_one({"_id": channel.id}, {"$set": {"message_id": msg.id}})
        except discord.HTTPException as e:
            print(f"AutoLock: failed to send lock message in #{channel.id}: {e}")
        except Exception as e:
            print(f"AutoLock: failed to store lock message id: {e}")

        # DM the members who were pinged for this spawn (each one's /lockdm settings decide
        # whether and when). Runs in the background so a slow DM never delays anything.
        lockdm = self.bot.get_cog("LockDM")
        if lockdm and pinged_users:
            task = asyncio.create_task(
                lockdm.notify_autolock(
                    channel=channel,
                    user_ids=set(pinged_users),
                    categories=active,
                    locked_at=locked_unix,
                    unlock_at=unlock_unix,
                    ping_url=ping_url,
                    lock_url=msg.jump_url if msg else None,
                )
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    # --- Auto-unlock ------------------------------------------------------------

    @tasks.loop(seconds=AUTO_UNLOCK_CHECK_SECONDS)
    async def auto_unlock_loop(self):
        """Unlock every channel whose auto-unlock time has passed. Reads the due locks from
        MongoDB, so locks that expired while the bot was offline are handled on startup too."""
        try:
            due = await self.locks_collection.find(
                {"unlock_at": {"$lte": datetime.datetime.now(datetime.timezone.utc)}}
            ).to_list(length=None)
        except Exception as e:
            print(f"AutoLock: failed to query due auto-unlocks: {e}")
            return

        for doc in due:
            try:
                await self._auto_unlock(doc)
            except Exception as e:
                print(f"AutoLock: auto-unlock of #{doc.get('_id')} failed: {e}")

    @auto_unlock_loop.before_loop
    async def _before_auto_unlock_loop(self):
        await self.bot.wait_until_ready()

    async def _auto_unlock(self, doc: dict):
        channel_id = doc["_id"]
        # Only remove the lock doc we actually acted on (a newer lock must survive).
        same_lock = {"_id": channel_id, "unlock_at": doc["unlock_at"]}

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.NotFound:
                await self.locks_collection.delete_one(same_lock)  # channel is gone
                return
            except discord.HTTPException:
                return  # try again on the next tick

        poketwo = await get_poketwo_target(self.bot, channel.guild)
        if poketwo is None:
            await self.locks_collection.delete_one(same_lock)
            return

        if is_locked_overwrite(channel.overwrites_for(poketwo)):
            try:
                await channel.set_permissions(
                    poketwo,
                    overwrite=discord.PermissionOverwrite(read_messages=True, send_messages=True),
                    reason="Auto-unlock: lock time ran out",
                )
            except discord.HTTPException as e:
                print(f"AutoLock: can't auto-unlock #{channel_id}: {e}")
                # Stop retrying every few seconds; the lock stays until someone unlocks it.
                await self.locks_collection.update_one(same_lock, {"$set": {"unlock_at": None}})
                return

        await self.locks_collection.delete_one(same_lock)

        # Edit the lock message itself: "Unlocked — by Automatic".
        embed = auto_unlocked_embed(channel, lock_doc=doc, when=now_unix())
        message_id = doc.get("message_id")
        edited = False
        if message_id:
            try:
                msg = await channel.fetch_message(message_id)
                await msg.edit(view=UnlockLayout(self.bot, embed, unlocked=True), allowed_mentions=NO_PINGS)
                edited = True
            except discord.HTTPException:
                pass
        if not edited:
            try:
                await channel.send(embed=embed, allowed_mentions=NO_PINGS)
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoLock(bot))