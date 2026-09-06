import asyncio
import certifi
import discord
from discord.ext import commands
import motor.motor_asyncio

MONGO_URI = "mongodb+srv://KurtisLam:CsHLOnDqihiU5uYG@cluster0.7rwx3oc.mongodb.net/?appName=Cluster0"

POKETWO_ID = 716390085896962058
DEFAULT_DELAY = 10

ROLE_CATEGORIES = ("rare", "regional", "gmax", "paradox", "eevos")
RESTRICT_CATEGORIES = {"sh", "cl", "tp", "rp"}


async def get_poketwo_target(guild: discord.Guild):
    """Retrieve Poketwo Member object via cache or fetch."""
    return guild.get_member(POKETWO_ID) or await guild.fetch_member(POKETWO_ID)


def _channel_in_whitelist(channel, whitelist: list) -> bool:
    """Empty whitelist == applies to every channel. Non-empty == only listed channels/categories."""
    if not whitelist:
        return True
    if channel.id in whitelist:
        return True
    category_id = getattr(channel, "category_id", None)
    if category_id and category_id in whitelist:
        return True
    return False


class AutoLockUnlockView(discord.ui.View):
    def __init__(self, cog=None, allowed_unlockers: set[int] | None = None):
        super().__init__(timeout=None)
        self.cog = cog
        self.allowed_unlockers = allowed_unlockers  # None == anyone can unlock

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
        self.active_locks = {}
        self.pending_locks = set()
        self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())
        self.db = self.mongo_client["utilities"]
        self.collection = self.db["autolock"]

    async def cog_load(self):
        try:
            await self.mongo_client.admin.command("ping")
        except Exception as e:
            print(f"AutoLock Cog: MongoDB warmup failed: {e}")

    async def _get_guild_config(self, guild_id: int) -> dict:
        doc = await self.collection.find_one({"_id": str(guild_id)})
        return doc or {}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild:
            return

        content = message.content.strip()
        if content and content.split(maxsplit=1)[0].lower() in (".u", ".unlock"):
            await self._handle_unlock_command(message)

    async def process_autolock(
        self,
        channel: discord.TextChannel,
        activated_categories: list[str],
        pinged_user_ids: set[int],
    ):
        """Called directly by Recognizer when a Pokémon is identified."""
        if channel.id in self.pending_locks:
            return

        autolock_doc = await self._get_guild_config(channel.guild.id)
        if not autolock_doc:
            return

        # Check whitelist per category
        whitelisted_cats = [
            cat for cat in activated_categories
            if _channel_in_whitelist(channel, autolock_doc.get(cat, {}).get("whitelist", []))
        ]

        if not whitelisted_cats:
            return

        delay = min(autolock_doc.get(cat, {}).get("delay", DEFAULT_DELAY) for cat in whitelisted_cats)

        # Check if any active personal ping category requires unlock restriction
        restrict_cats = [
            cat for cat in whitelisted_cats
            if cat in RESTRICT_CATEGORIES and autolock_doc.get(cat, {}).get("restrict_unlockers", False)
        ]

        # Role locks (rare, regional, gmax, paradox, eevos) are unlockable by everyone (None)
        # unless overridden by a restricted personal ping (sh, cl, tp, rp) with pinged users
        allowed_unlockers = None
        if restrict_cats and pinged_user_ids:
            allowed_unlockers = pinged_user_ids

        self.pending_locks.add(channel.id)
        status_msg = await channel.send(
            f"⏳ **Auto-Lock Triggered** (`{'/'.join(whitelisted_cats)}`): Locking in {delay} seconds..."
        )

        def poketwo_check(m: discord.Message) -> bool:
            return (
                m.author.id == POKETWO_ID
                and m.channel.id == channel.id
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

            target = await get_poketwo_target(channel.guild)
            await channel.set_permissions(target, view_channel=False, send_messages=False)

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
            lock_msg = await channel.send(
                embed=lock_embed, view=AutoLockUnlockView(cog=self, allowed_unlockers=allowed_unlockers)
            )
            self.active_locks[channel.id] = {
                "message": lock_msg,
                "allowed_unlockers": allowed_unlockers,
            }
        finally:
            self.pending_locks.discard(channel.id)

    async def _handle_unlock_command(self, message: discord.Message):
        lock_data = self.active_locks.get(message.channel.id)
        if not lock_data:
            return

        allowed = lock_data["allowed_unlockers"]
        if allowed is not None and message.author.id not in allowed:
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