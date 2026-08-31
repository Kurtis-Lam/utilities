import asyncio
import certifi
import discord
from discord.ext import commands
import motor.motor_asyncio 

MONGO_URI = "mongodb+srv://KurtisLam:CsHLOnDqihiU5uYG@cluster0.7rwx3oc.mongodb.net/?appName=Cluster0" 
POKETWO_ID = 716390085896962058
SENSOR_IDS = {874910942490677270, 854233015475109888, 1250429544486273038}

async def get_poketwo_target(guild: discord.Guild):
    """Retrieve Poketwo Member object via cache or fetch."""
    return guild.get_member(POKETWO_ID) or await guild.fetch_member(POKETWO_ID)


class UnlockView(discord.ui.View):
    def __init__(self, cog=None):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Unlock", style=discord.ButtonStyle.green, emoji="🔓")
    async def unlock(self, interaction: discord.Interaction, button: discord.ui.Button):
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
        self.active_locks = {}      # Stores channel.id -> lock_msg
        self.pending_locks = set()  # Stores channel.id in 15s countdown
        # Async Motor Client matching lockconfig.py[cite: 1]
        self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())
        self.db = self.mongo_client["utilities"]
        self.collection = self.db["config"]

    async def cog_load(self):
        """Warms up connection to MongoDB on boot."""
        try:
            await self.mongo_client.admin.command('ping')
        except Exception as e:
            print(f"AutoLock Cog: MongoDB warmup failed: {e}")

    async def _get_guild_data(self, guild_id: str) -> dict:
        """Fetches server configuration directly from MongoDB asynchronously[cite: 1]."""
        doc = await self.collection.find_one({"_id": guild_id})
        if not doc:
            doc = {"_id": guild_id, "rare": [], "regional": [], "user": []}
        return doc

    def _is_target_mentioned(self, content: str, target_id: int) -> bool:
        """Fast string check for mentions without list allocation overhead."""
        tid = str(target_id)
        return f"<@{tid}>" in content or f"<@&{tid}>" in content or f"<@!{tid}>" in content

    async def _check_target_triggered(self, message: discord.Message, guild_config: dict) -> bool:
        channel_id = message.channel.id
        category_id = message.channel.category_id
        content = message.content

        for key in ("rare", "regional", "user"):
            for entry in guild_config.get(key, []):
                target_id = entry.get("target")
                if not target_id:
                    continue

                restrict_ch = entry.get("restrict_channels")
                if restrict_ch and channel_id not in restrict_ch:
                    continue

                restrict_cat = entry.get("restrict_categories")
                if restrict_cat and category_id not in restrict_cat:
                    continue

                exclude_ch = entry.get("exclude_channels")
                if exclude_ch and channel_id in exclude_ch:
                    continue

                exclude_cat = entry.get("exclude_categories")
                if exclude_cat and category_id in exclude_cat:
                    continue

                if self._is_target_mentioned(content, target_id):
                    return True

        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild:
            return

        content = message.content.strip()

        # Check for .u or .unlock command
        if content and content.split(maxsplit=1)[0].lower() in (".u", ".unlock"):
            if message.channel.id in self.active_locks:
                lock_msg = self.active_locks.pop(message.channel.id, None)
                if lock_msg:
                    try:
                        view = UnlockView(cog=self)
                        view.children[0].disabled = True
                        view.children[0].label = "Unlocked"
                        view.children[0].style = discord.ButtonStyle.secondary
                        await lock_msg.edit(view=view)
                    except discord.HTTPException:
                        pass
            return

        # O(1) lookup for sensor ID
        if message.author.id not in SENSOR_IDS:
            return

        # Prevent duplicate countdowns
        if message.channel.id in self.pending_locks:
            return

        # Fetch configuration asynchronously from MongoDB for this guild
        guild_id = str(message.guild.id)
        guild_config = await self._get_guild_data(guild_id)
        if not guild_config or not await self._check_target_triggered(message, guild_config):
            return

        # Start countdown
        self.pending_locks.add(message.channel.id)

        status_msg = await message.channel.send("⏳ **Auto-Lock Triggered:** Locking in 15 seconds...")

        def poketwo_check(m: discord.Message) -> bool:
            return (
                m.author.id == POKETWO_ID
                and m.channel.id == message.channel.id
                and m.content.startswith("Congratulations")
            )

        try:
            await self.bot.wait_for("message", check=poketwo_check, timeout=15.0)
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
            await message.channel.set_permissions(
                target, view_channel=False, send_messages=False
            )

            lock_embed = discord.Embed(
                title="🔒 Channel Locked",
                description="Use `.u` or the button to unlock!",
                color=discord.Color.red(),
            )
            lock_msg = await message.channel.send(
                embed=lock_embed, view=UnlockView(cog=self)
            )
            self.active_locks[message.channel.id] = lock_msg

        finally:
            self.pending_locks.remove(message.channel.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoLock(bot))