import asyncio
import re

import certifi
import discord
from discord.ext import commands
import motor.motor_asyncio

MONGO_URI = "mongodb+srv://KurtisLam:CsHLOnDqihiU5uYG@cluster0.7rwx3oc.mongodb.net/?appName=Cluster0"

POKETWO_ID = 716390085896962058
SENSOR_IDS = {874910942490677270, 854233015475109888, 1250429544486273038}

DEFAULT_DELAY = 10

# Rare/Regional/Gmax/Paradox/Eevos are role-based: the role is whatever's
# configured in PokePings via .rarerole/.regionalrole/.gigantamaxrole/
# .paradoxrole/.eeveeevolutions (stored in the `pings` collection, key
# matches the category name below). Autolock only reads it.
ROLE_CATEGORIES = ("rare", "regional", "gmax", "paradox", "eevos")

# Sh/Cl/Tp/Rp are personal ping-subscription based (PokePings' .sh/.cl/.tp/.rp
# commands), matched by message content since they aren't tied to a single
# server role. tp = Type Ping, rp = Regional Ping (NOT the same as the
# role-based "regional" lock above — this is the personal .rp opt-in list).
RESTRICT_CATEGORIES = {"sh", "cl", "tp", "rp"}

# ---------------------------------------------------------------------------
# NOTE: The exact wording your ping-bot(s) use for these notifications isn't
# something we had a sample of, so these regexes are reasonable best guesses.
# Tweak them to match your server's real ping messages — that's the only
# thing you should need to change to get detection matching your setup.
# ---------------------------------------------------------------------------
TRIGGER_PATTERNS = {
    "sh": re.compile(r"\bshiny\s*hunt(ed|ing)?\b", re.IGNORECASE),
    "cl": re.compile(r"\bcollect(ed|ion)?\s*ping(ed)?\b", re.IGNORECASE),
    "tp": re.compile(r"\btype\s*ping(ed)?\b", re.IGNORECASE),
    "rp": re.compile(r"\bregional?\s*ping(ed)?\b", re.IGNORECASE),
}


async def get_poketwo_target(guild: discord.Guild):
    """Retrieve Poketwo Member object via cache or fetch."""
    return guild.get_member(POKETWO_ID) or await guild.fetch_member(POKETWO_ID)


def _channel_in_whitelist(channel, whitelist: list) -> bool:
    """Empty whitelist == applies to every channel. Non-empty == only listed
    channels/categories are eligible."""
    if not whitelist:
        return True
    if channel.id in whitelist:
        return True
    category_id = getattr(channel, "category_id", None)
    if category_id and category_id in whitelist:
        return True
    return False


def _detect_categories(message: discord.Message, autolock_doc: dict, ping_roles: dict) -> list[str]:
    """Returns the list of lock categories activated by this message.

    `autolock_doc` holds per-category {delay, whitelist, restrict_unlockers}.
    `ping_roles` is the `roles` sub-document from PokePings (`pings`
    collection), e.g. {"rare": "123...", "gmax": "456...", ...}.
    """
    activated = []
    content = message.content or ""

    for cat in ROLE_CATEGORIES:
        role_id_raw = ping_roles.get(cat)
        if not role_id_raw:
            continue
        try:
            role_id = int(role_id_raw)
        except (TypeError, ValueError):
            continue

        if any(r.id == role_id for r in message.role_mentions):
            cfg = autolock_doc.get(cat, {})
            if _channel_in_whitelist(message.channel, cfg.get("whitelist", [])):
                activated.append(cat)

    for cat, pattern in TRIGGER_PATTERNS.items():
        cfg = autolock_doc.get(cat, {})
        if pattern.search(content) and _channel_in_whitelist(message.channel, cfg.get("whitelist", [])):
            activated.append(cat)

    return activated


class AutoLockUnlockView(discord.ui.View):
    def __init__(self, cog=None, allowed_unlockers: set[int] | None = None):
        super().__init__(timeout=None)
        self.cog = cog
        # None == anyone can unlock. A set of IDs == only those users can unlock.
        self.allowed_unlockers = allowed_unlockers

    @discord.ui.button(label="Unlock", style=discord.ButtonStyle.green, emoji="🔓")
    async def unlock(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.allowed_unlockers is not None and interaction.user.id not in self.allowed_unlockers:
            return await interaction.response.send_message(
                "⚠️ Only the user(s) who triggered this lock can unlock this channel.", ephemeral=True
            )

        target = await get_poketwo_target(interaction.guild)
        await interaction.channel.set_permissions(target, view_channel=True, send_messages=True)

        button.disabled = True
        button.label = "Unlocked"
        button.style = discord.ButtonStyle.secondary

        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"🔓 **{interaction.channel.mention}** was unlocked by {interaction.user.mention}."
        )

        if self.cog:
            self.cog.active_locks.pop(interaction.channel.id, None)


class AutoLock(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # channel_id -> {"message": discord.Message, "allowed_unlockers": set[int] | None}
        self.active_locks = {}
        self.pending_locks = set()
        self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())
        self.db = self.mongo_client["utilities"]
        self.collection = self.db["autolock"]
        # Same collection PokePings writes rare/regional/gmax/paradox/eevos
        # role IDs to. We only ever read from it.
        self.pings_collection = self.db["pings"]

    async def cog_load(self):
        try:
            await self.mongo_client.admin.command("ping")
        except Exception as e:
            print(f"AutoLock Cog: MongoDB warmup failed: {e}")

    async def _get_guild_config(self, guild_id: int) -> dict:
        doc = await self.collection.find_one({"_id": str(guild_id)})
        return doc or {}

    async def _get_ping_roles(self, guild_id: int) -> dict:
        doc = await self.pings_collection.find_one({"_id": str(guild_id)})
        return (doc or {}).get("roles", {})

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild:
            return

        content = message.content.strip()

        if content and content.split(maxsplit=1)[0].lower() in (".u", ".unlock"):
            await self._handle_unlock_command(message)
            return

        if message.author.id not in SENSOR_IDS or message.channel.id in self.pending_locks:
            return

        autolock_doc = await self._get_guild_config(message.guild.id)
        if not autolock_doc:
            return  # Feature not configured yet for this guild.

        ping_roles = await self._get_ping_roles(message.guild.id)

        activated = _detect_categories(message, autolock_doc, ping_roles)
        if not activated:
            return

        # If multiple categories fire on the same message, lock on the
        # shortest configured delay among them.
        delay = min(autolock_doc.get(cat, {}).get("delay", DEFAULT_DELAY) for cat in activated)

        # If any matched sh/cl/tp/rp category has restrict-unlockers enabled,
        # that overrides the "anyone can unlock" default from
        # rare/regional/gmax/paradox/eevos.
        restrict_cats = [
            cat
            for cat in activated
            if cat in RESTRICT_CATEGORIES and autolock_doc.get(cat, {}).get("restrict_unlockers", False)
        ]

        allowed_unlockers = None
        if restrict_cats:
            mentioned_ids = {u.id for u in message.mentions}
            # Only actually restrict if someone was mentioned to restrict to —
            # otherwise fall back to "anyone can unlock" so the channel never
            # becomes permanently unlockable by no one.
            if mentioned_ids:
                allowed_unlockers = mentioned_ids

        self.pending_locks.add(message.channel.id)
        status_msg = await message.channel.send(
            f"⏳ **Auto-Lock Triggered** (`{'/'.join(activated)}`): Locking in {delay} seconds..."
        )

        def poketwo_check(m: discord.Message) -> bool:
            return (
                m.author.id == POKETWO_ID
                and m.channel.id == message.channel.id
                and m.content.startswith("Congratulations")
            )

        try:
            await self.bot.wait_for("message", check=poketwo_check, timeout=delay)
            try:
                await status_msg.delete()
            except discord.HTTPException:
                pass
        except asyncio.TimeoutError:
            try:
                await status_msg.delete()
            except discord.HTTPException:
                pass

            target = await get_poketwo_target(message.guild)
            await message.channel.set_permissions(target, view_channel=False, send_messages=False)

            if allowed_unlockers is not None:
                mentions = ", ".join(f"<@{uid}>" for uid in allowed_unlockers)
                description = (
                    f"Only {mentions} can unlock this channel (`{'/'.join(restrict_cats)}` lock)."
                )
            else:
                description = "Use `.u` or the button to unlock!"

            lock_embed = discord.Embed(
                title="🔒 Channel Locked",
                description=description,
                color=discord.Color.red(),
            )
            lock_msg = await message.channel.send(
                embed=lock_embed, view=AutoLockUnlockView(cog=self, allowed_unlockers=allowed_unlockers)
            )
            self.active_locks[message.channel.id] = {
                "message": lock_msg,
                "allowed_unlockers": allowed_unlockers,
            }
        finally:
            self.pending_locks.discard(message.channel.id)

    async def _handle_unlock_command(self, message: discord.Message):
        lock_data = self.active_locks.get(message.channel.id)
        if not lock_data:
            return

        allowed = lock_data["allowed_unlockers"]
        if allowed is not None and message.author.id not in allowed:
            # Silently ignore — only whitelisted unlockers may use the command.
            return

        await self._perform_unlock(message.channel, message.author, lock_data)

    async def _perform_unlock(self, channel: discord.TextChannel, unlocker: discord.Member, lock_data: dict):
        target = await get_poketwo_target(channel.guild)
        await channel.set_permissions(target, view_channel=True, send_messages=True)

        lock_msg = lock_data.get("message")
        if lock_msg:
            try:
                view = AutoLockUnlockView(cog=self, allowed_unlockers=lock_data["allowed_unlockers"])
                view.children[0].disabled = True
                view.children[0].label = "Unlocked"
                view.children[0].style = discord.ButtonStyle.secondary
                await lock_msg.edit(view=view)
            except discord.HTTPException:
                pass

        self.active_locks.pop(channel.id, None)
        await channel.send(f"🔓 **{channel.mention}** was unlocked by {unlocker.mention}")


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoLock(bot))