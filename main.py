import asyncio
import aiohttp
import discord
import gc
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta, timezone

# Remember to keep your bot token secret!
TOKEN = "MTMyNzQ4MDgyODc0MTE2MTA3NA.GWPTNO.VttPjVzEFtwUW_6N00NCJUgRCinBm2FsCVcYrg"
OWNERS = {1250429544486273038, 1281560553130692618, 1528374615720591381, 1432984051341459527, 1432983193681920014}

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.guilds = True
INTENTS.members = True

# --- BOT MAIN CLASS ---
class Utilities(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=self.get_prefix_with_space, 
            owner_ids=OWNERS, 
            intents=INTENTS,
            case_insensitive=True  # <--- This makes all commands case-insensitive
        )
        self.active_predictions = {}
        self.session = None

        # Stored cogs dictionary as an instance attribute for easy access across the bot
        self.cogs_dict = {
            "cmds": ["categories", "channels", "joins", "members", "messages", "ping", "roles", "utilities"],
            "poketwo": ["dex", "fled", "hintsolver", "lockunlock", "pings", "recognizer", "spawnsconfig"],
            "grinder": ["grinder"]
        }

    async def get_prefix_with_space(self, bot, message):
        # This allows both '.' and '. ' (with a space) alongside bot mentions
        return commands.when_mentioned_or('.', '. ')(bot, message)

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()

        for category, cogs in self.cogs_dict.items():
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
        if self.session:
            await self.session.close()
        await super().close()

bot = Utilities()

@bot.event
async def on_ready():
    print(f'We have logged in as {bot.user}')

# --- OWNER COMMANDS ---
@bot.command(name="reload")
@commands.is_owner()
async def reload(ctx: commands.Context, cog_name: str = None):
    if cog_name:
        # Search for matching cog in cogs_dict
        target_ext = None
        for category, cogs in bot.cogs_dict.items():
            if cog_name.lower() in [c.lower() for c in cogs]:
                target_ext = f'cogs.{category}.{cog_name.lower()}'
                break
        
        if not target_ext:
            await ctx.send(f"❌ Cog `{cog_name}` not found.")
            return

        msg = await ctx.send(f"🔄 Reloading `{cog_name}`...")
        try:
            await bot.reload_extension(target_ext)
            gc.collect()  # Force garbage collection to free memory immediately
            await msg.edit(content=f"✅ Reloaded `{cog_name}`.")
        except Exception as e:
            await msg.edit(content=f"❌ Failed to reload `{cog_name}`: `{e}`")
        return

    # Fallback to reloading all cogs sequentially with live status edits
    reloaded, failed = [], []
    msg = await ctx.send("🔄 Starting reload process...")

    for category, cogs in bot.cogs_dict.items():
        for cog in cogs:
            await msg.edit(content=f"🔄 Reloading `{cog}`...")
            ext = f'cogs.{category}.{cog.lower()}'
            try:
                await bot.reload_extension(ext)
                reloaded.append(cog)
            except Exception as e:
                failed.append(f"`{cog}`: {e}")
            gc.collect()  # Clean up memory after each cog

    final_msg = f"🔄 **Reload Complete**\n✅ Successfully reloaded **{len(reloaded)}** cogs."
    if failed:
        final_msg += f"\n❌ **Failed ({len(failed)}):**\n" + "\n".join(failed)
    
    await msg.edit(content=final_msg)

@reload.error
async def reload_cogs_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.NotOwner):
        print("nah")

try:
    bot.run(TOKEN)
except discord.HTTPException as e:
    if e.status == 429:
        print("The Discord servers denied the connection for making too many requests")
    else:
        raise e