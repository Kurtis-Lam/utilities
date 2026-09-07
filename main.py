import asyncio
import aiohttp
import discord
import gc
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta, timezone

TOKEN = "MTQ3NTY3MjIwNzU0NjkwODcxMg.GkSu5B.z-SyH2cS3KuFIHBIFimvA3-qen6IrCigJQHqpY"
OWNERS = {1250429544486273038, 1281560553130692618, 1528374615720591381, 1432984051341459527, 1432983193681920014}

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.guilds = True
INTENTS.members = True

class Utilities(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=self.get_prefix_with_space, 
            owner_ids=OWNERS, 
            intents=INTENTS,
            case_insensitive=True
        )
        self.active_predictions = {}
        self.session = None

        self.cogs_dict = {
            "cmds": ["categories", "channels", "members", "messages", "ping", "roles", "utilities"],
            "config": ["base"],
            "poketwo": ["afk", "autolock", "catches", "dex", "fled", "hintsolver", "lockunlock", "pings", "recognizer"]
        }

    async def get_prefix_with_space(self, bot, message):
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

@bot.command(name="reload")
@commands.is_owner()
async def reload(ctx: commands.Context, cog_name: str = None):
    if cog_name:
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
            gc.collect()
            await msg.edit(content=f"✅ Reloaded `{cog_name}`.")
        except Exception as e:
            await msg.edit(content=f"❌ Failed to reload `{cog_name}`: `{e}`")
        return

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
            gc.collect()

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