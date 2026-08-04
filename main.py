import asyncio
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta, timezone

TOKEN = "MTQ3NTY3MjIwNzU0NjkwODcxMg.Gxo4Zx.bdnqQA1JUmPV2nwEN0Ksy9l0c0dBhjjVvhWGAg"
OWNERS = {1250429544486273038, 1432984051341459527}
TARGET_AUTHOR_ID = 716390085896962058

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.guilds = True
INTENTS.members = True

# --- BOT MAIN CLASS ---
class PokeBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=".", owner_ids=OWNERS, intents=INTENTS)
        self.active_predictions = {}
        self.session = None  # <--- 2. Initialize attribute

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()  # <--- 3. Create persistent HTTP session

        cogs_dict = {
            "commands": ["categories", "channels", "members", "messages", "ping", "roles", "utilities"],
            "poketwo": ["autolock", "catches", "fled", "lockconfig", "lockunlock", "pname", "recognize"]
        }
        for category, cogs in cogs_dict.items():
            category_path = f'cogs.{category}'
            for cog in cogs:
                try:
                    await self.load_extension(f'{category_path}.{cog.lower()}')
                    print(f'✅ {cog.upper()} cog loaded.')
                except Exception as e:
                    print(f'❌ Failed to load {cog.upper()} cog: {e}')
        
        await self.tree.sync()
        print("Application commands synced successfully!")

    async def close(self):
        # <--- 4. Gracefully close session when bot shuts down
        if self.session:
            await self.session.close()
        await super().close()


bot = PokeBot()

@bot.command(name="checkflee")
@commands.has_permissions(manage_messages=True)
async def checkflee(ctx):
    poketwo_id = 716390085896962058
    failed_channels = []

    for channel in ctx.guild.text_channels:
        # Check bot permissions for the channel first
        permissions = channel.permissions_for(ctx.guild.me)
        if not (permissions.read_messages and permissions.read_message_history):
            continue

        try:
            async for message in channel.history(limit=25):
                if message.author.id == poketwo_id:
                    if not message.content.startswith("Congratulations"):
                        failed_channels.append(channel.mention)
                    break
        except discord.HTTPException:
            continue

    if failed_channels:
        channels_str = ", ".join(failed_channels)
        await ctx.send(f"❌ Poketwo did not congratulate in the following channels: {channels_str}")
    else:
        await ctx.send("✅ All scanned channels have a 'Congratulations' message from Poketwo as their last Poketwo message.")


@bot.event
async def on_ready():
    print(f'We have logged in as {bot.user}')

try:
    bot.run(TOKEN)
except discord.HTTPException as e:
    if e.status == 429:
        print("The Discord servers denied the connection for making too many requests")
    else:
        raise e