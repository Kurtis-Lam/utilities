import discord
from discord.ext import commands
import json
import os
import asyncio

CONFIG_DIR = "data"
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

POKETWO_ID = 716390085896962058
SENSOR_IDS = [874910942490677270, 854233015475109888, 1250429544486273038]


class UnlockView(discord.ui.View):
    def __init__(self, poketwo_id: int, cog=None):
        super().__init__(timeout=None)
        self.poketwo_id = poketwo_id
        self.cog = cog

    @discord.ui.button(label="Unlock", style=discord.ButtonStyle.green, emoji="🔓")
    async def unlock(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        channel = interaction.channel

        # Fetch Pokétwo member
        poketwo = guild.get_member(self.poketwo_id)
        if not poketwo:
            try:
                poketwo = await guild.fetch_member(self.poketwo_id)
            except discord.NotFound:
                return await interaction.response.send_message(
                    "❌ Pokétwo member could not be found in this server.", ephemeral=True
                )

        # Restore permissions for Pokétwo
        await channel.set_permissions(poketwo, view_channel=True, send_messages=True)

        # Update button UI
        button.disabled = True
        button.label = "Unlocked"
        button.style = discord.ButtonStyle.secondary

        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"🔓 **{channel.mention}** was unlocked by {interaction.user.mention}."
        )

        # Remove lock tracking if cog reference exists
        if self.cog:
            self.cog.active_locks.pop(channel.id, None)


class AutoLock(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_locks = {}      # Stores channel.id -> lock_msg
        self.pending_locks = set()  # Stores channel.id currently in 15-second countdown

    def load_config(self) -> dict:
        """Loads configuration settings from JSON file."""
        if not os.path.exists(CONFIG_FILE):
            return {}
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _is_target_mentioned(self, message: discord.Message, target_id: int) -> bool:
        """Checks if a specific role or user ID is mentioned in the message or its embeds."""
        # Check standard Discord mention arrays
        if target_id in message.raw_role_mentions or target_id in message.raw_mentions:
            return True

        # Check raw text formatting variations (<@ID>, <@&ID>, <@!ID>)
        ping_patterns = [f"<@&{target_id}>", f"<@{target_id}>", f"<@!{target_id}>"]
        
        # Search plain content
        if any(pattern in message.content for pattern in ping_patterns):
            return True

        # Search inside embeds (title, description, fields)
        for embed in message.embeds:
            embed_text = ""
            if embed.title:
                embed_text += embed.title + " "
            if embed.description:
                embed_text += embed.description + " "
            for field in embed.fields:
                embed_text += f"{field.name} {field.value} "

            if any(pattern in embed_text for pattern in ping_patterns):
                return True

        return False

    def _check_target_triggered(self, message: discord.Message, guild_config: dict) -> bool:
        """
        Validates if the sensor message mentions a configured target (rare, regional, user)
        and complies with the allowed/excluded channel and category rules.
        """
        # Gather all configured entries for this server
        all_rules = (
            guild_config.get("rare", []) +
            guild_config.get("regional", []) +
            guild_config.get("user", [])
        )

        if not all_rules:
            return False

        channel_id = message.channel.id
        category_id = message.channel.category_id

        for entry in all_rules:
            target_id = entry.get("target")
            if not target_id:
                continue

            # 1. Check Channel / Category Restrictions
            restrict_ch = entry.get("restrict_channels", [])
            if restrict_ch and channel_id not in restrict_ch:
                continue

            restrict_cat = entry.get("restrict_categories", [])
            if restrict_cat and category_id not in restrict_cat:
                continue

            # 2. Check Channel / Category Exclusions
            exclude_ch = entry.get("exclude_channels", [])
            if exclude_ch and channel_id in exclude_ch:
                continue

            exclude_cat = entry.get("exclude_categories", [])
            if exclude_cat and category_id in exclude_cat:
                continue

            # 3. Check if target ID was mentioned in content/embeds
            if self._is_target_mentioned(message, target_id):
                return True

        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore DMs
        if not message.guild:
            return

        # Check if user typed .u or .unlock in a locked channel
        first_word = message.content.strip().split()[0].lower() if message.content.strip() else ""
        if first_word in [".u", ".unlock"] and message.channel.id in self.active_locks:
            lock_msg = self.active_locks.pop(message.channel.id, None)

            if lock_msg:
                try:
                    view = UnlockView(POKETWO_ID, cog=self)
                    for item in view.children:
                        if isinstance(item, discord.ui.Button):
                            item.disabled = True
                            item.label = "Unlocked"
                            item.style = discord.ButtonStyle.secondary
                    await lock_msg.edit(view=view)
                except discord.HTTPException:
                    pass

            return

        # Author MUST be one of the Sensor IDs
        if message.author.id not in SENSOR_IDS:
            return

        # Prevent duplicate countdowns in the same channel
        if message.channel.id in self.pending_locks:
            return

        # Check configuration and rules
        data = self.load_config()
        guild_config = data.get(str(message.guild.id))
        if not guild_config:
            return

        if not self._check_target_triggered(message, guild_config):
            return

        # Start 15-second countdown
        self.pending_locks.add(message.channel.id)

        warn_embed = discord.Embed(
            title="⏳ Auto-Lock Triggered",
            description="**Locking in 15 seconds...**",
            color=discord.Color.gold()
        )
        status_msg = await message.channel.send(embed=warn_embed)

        def poketwo_check(m: discord.Message):
            return (
                m.author.id == POKETWO_ID
                and m.channel.id == message.channel.id
                and m.content.startswith("Congratulations")
            )

        try:
            # Wait up to 15 seconds for Pokétwo "Congratulations" message
            await self.bot.wait_for("message", check=poketwo_check, timeout=15.0)

            # Catch detected -> Delete status message
            try:
                await status_msg.delete()
            except discord.HTTPException:
                pass

        except asyncio.TimeoutError:
            # 15 seconds elapsed with no catch -> Delete status message first
            try:
                await status_msg.delete()
            except discord.HTTPException:
                pass

            # Perform lock logic
            poketwo = message.guild.get_member(POKETWO_ID)
            if not poketwo:
                try:
                    poketwo = await message.guild.fetch_member(POKETWO_ID)
                except discord.NotFound:
                    poketwo = None

            if poketwo:
                await message.channel.set_permissions(poketwo, view_channel=False, send_messages=False)

                lock_embed = discord.Embed(
                    title="🔒 Channel Locked",
                    description="Use `.u` or the button to unlock!",
                    color=discord.Color.red()
                )
                lock_msg = await message.channel.send(
                    embed=lock_embed, 
                    view=UnlockView(POKETWO_ID, cog=self)
                )
                self.active_locks[message.channel.id] = lock_msg
            else:
                await message.channel.send("⚠️ Unable to lock channel: Pokétwo was not found in this server.")

        finally:
            self.pending_locks.remove(message.channel.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoLock(bot))
