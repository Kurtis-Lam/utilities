import asyncio
import datetime

import discord
from discord.ext import commands

from cogs.poketwo.lockcommon import (
    WARN_COLOR,
    already_unlocked_embed,
    is_locked_overwrite,
    locked_embed,
    now_unix,
    to_unix,
    unlock_denied_embed,
    unlocked_embed,
)
from views.common import error_embed

POKETWO_ID = 716390085896962058
DEFAULT_DELAY = 10

# Who may unlock when several restricted locks fire at once. Earlier tier wins;
# categories inside the same tuple are equal (their user sets are merged).
# Role-based locks (rare/regional/gmax/paradox/eevos) ping a role, not users,
# so they never restrict anything.
UNLOCK_PRIORITY = (("re",), ("sh",), ("cl",), ("tp", "rp"))

# Short labels for the status/lock messages
SHORT_NAMES = {"re": "res"}


async def get_poketwo_target(guild: discord.Guild):
    """Retrieve Poketwo Member object via cache or fetch. None if it isn't in the guild."""
    member = guild.get_member(POKETWO_ID)
    if member:
        return member
    try:
        return await guild.fetch_member(POKETWO_ID)
    except discord.HTTPException:
        return None


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


def can_unlock(lock_doc: dict | None, member: discord.Member) -> bool:
    """Same rule lockunlock.py uses: no doc / no restriction -> anyone; else listed users or admins."""
    if not lock_doc:
        return True
    allowed = lock_doc.get("allowed_users")
    if allowed is None:
        return True
    return member.id in allowed or member.guild_permissions.administrator


def unlock_denied_message(lock_doc: dict) -> str:
    allowed = lock_doc.get("allowed_users") or []
    mentions = ", ".join(f"<@{uid}>" for uid in allowed)
    return f"⚠️ Only {mentions} or an admin can unlock."


class AutoLockUnlockView(discord.ui.View):
    def __init__(self, cog=None):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Unlock", style=discord.ButtonStyle.green, emoji="🔓")
    async def unlock(self, interaction: discord.Interaction, button: discord.ui.Button):
        locks = self.cog.locks_collection if self.cog else None

        lock_doc = None
        if locks is not None:
            try:
                lock_doc = await locks.find_one({"_id": interaction.channel.id})
            except Exception as e:
                print(f"AutoLock: failed to read lock state: {e}")
                return await interaction.response.send_message(
                    embed=error_embed("Couldn't verify the lock. Try again."),
                    ephemeral=True,
                )

        target = await get_poketwo_target(interaction.guild)
        if target is None:
            return await interaction.response.send_message(
                embed=error_embed("Pokétwo isn't in this server."), ephemeral=True
            )

        # Someone already unlocked it (e.g. with .unlock): retire the button and say so
        if not is_locked_overwrite(interaction.channel.overwrites_for(target)):
            if locks is not None and lock_doc:
                await locks.delete_one({"_id": interaction.channel.id})
            button.disabled = True
            button.label = "Unlocked"
            button.style = discord.ButtonStyle.secondary
            await interaction.response.edit_message(view=self)
            return await interaction.followup.send(
                embed=already_unlocked_embed(interaction.channel), ephemeral=True
            )

        if not can_unlock(lock_doc, interaction.user):
            return await interaction.response.send_message(
                embed=unlock_denied_embed(interaction.channel, lock_doc), ephemeral=True
            )

        await interaction.channel.set_permissions(target, view_channel=True, send_messages=True)

        if locks is not None and lock_doc:
            await locks.delete_one({"_id": interaction.channel.id})

        button.disabled = True
        button.label = "Unlocked"
        button.style = discord.ButtonStyle.secondary

        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            embed=unlocked_embed(
                interaction.channel,
                unlocked_by=interaction.user,
                locked_at=to_unix((lock_doc or {}).get("locked_at")),
            )
        )


class AutoLock(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pending_locks = set()

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
    ):
        """
        Called directly by Recognizer when a Pokémon is identified.

        activated_categories: categories that matched this spawn (re, sh, cl, tp, rp, rare, ...)
        category_users:       {"re": {uid, ...}, "sh": {...}, ...} users pinged per category
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

            await self._lock_channel(channel, active, allowed_unlockers, restrict_cats)
        finally:
            self.pending_locks.discard(channel.id)

    async def _wait_for_catch(self, channel: discord.TextChannel, delay: int) -> bool:
        """True if the Pokémon was caught before the delay ran out."""
        def poketwo_check(m: discord.Message) -> bool:
            return (
                m.author.id == POKETWO_ID
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
    ):
        target = await get_poketwo_target(channel.guild)
        if target is None:
            print(f"AutoLock: Poketwo not found in guild {channel.guild.id}, can't lock #{channel.id}")
            return

        # Sync lock state to MongoDB *before* locking so /unlock checks (lockunlock.py,
        # the Unlock button) always see the restriction as soon as the channel is locked.
        try:
            await self.locks_collection.update_one(
                {"_id": channel.id},
                {"$set": {
                    "guild_id": channel.guild.id,
                    "allowed_users": allowed_unlockers,   # None = anyone can unlock
                    "source": "autolock",
                    "categories": active,
                    "restricted_by": restrict_cats,
                    "locked_at": datetime.datetime.now(datetime.timezone.utc),
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
            when=now_unix(),
        )
        await channel.send(embed=lock_embed, view=AutoLockUnlockView(cog=self))


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoLock(bot))