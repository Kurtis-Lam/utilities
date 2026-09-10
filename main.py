import asyncio
import aiohttp
import discord
import gc
import os
import platform
import sys
import psutil
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta, timezone

import certifi
import motor.motor_asyncio

TOKEN = "MTQ3NTY3MjIwNzU0NjkwODcxMg.GkSu5B.z-SyH2cS3KuFIHBIFimvA3-qen6IrCigJQHqpY"
OWNERS = {1250429544486273038, 1281560553130692618, 1528374615720591381, 1432984051341459527, 1432983193681920014}
MONGO_URI = "mongodb+srv://KurtisLam:CsHLOnDqihiU5uYG@cluster0.7rwx3oc.mongodb.net/?appName=Cluster0"

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
            case_insensitive=True,
            chunk_guilds_at_startup=False,                       # Stops bot from downloading full guild caches
            member_cache_flags=discord.MemberCacheFlags.none()    # Zero member cache footprint
        )
        self.start_time = datetime.now(timezone.utc)
        self.active_predictions = {}
        self.session = None

        # Shared MongoDB Client for all cogs
        self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
            MONGO_URI,
            tlsCAFile=certifi.where(),
            maxPoolSize=2,
            minPoolSize=0,
            serverSelectionTimeoutMS=5000
        )

        self.cogs_dict = {
            "cmds": ["categories", "channels", "members", "messages", "ping", "roles", "utilities"],
            "config": ["base"],
            "poketwo": ["afk", "autolock", "lockunlock", "pings", "recognizer"],
            "poketwo-utils": ["dex", "extract", "fled", "hintsolver"],
            "toggles": ["autolocktoggle"]
        }

    async def get_prefix_with_space(self, bot, message):
        return commands.when_mentioned_or('.', '. ')(bot, message)

    async def setup_hook(self):
        # Optimized session with reduced connections
        connector = aiohttp.TCPConnector(limit=5, enable_cleanup_closed=True)
        self.session = aiohttp.ClientSession(connector=connector)

        for category, cogs in self.cogs_dict.items():
            category_path = f'cogs.{category}'
            for cog in cogs:
                try:
                    await self.load_extension(f'{category_path}.{cog.lower()}')
                    print(f'✅ {cog.upper()} cog loaded.')
                except Exception as e:
                    print(f'❌ Failed to load {cog.upper()} cog: {e}')
        
        # Free memory immediately after loading extensions
        gc.collect()

    async def close(self):
        if self.session:
            await self.session.close()
        if self.mongo_client:
            self.mongo_client.close()
        await super().close()

bot = Utilities()

@bot.event
async def on_ready():
    print(f'We have logged in as {bot.user}')

import os
import platform
import asyncio
import psutil
import discord
from datetime import datetime, timezone
from discord.ext import commands

def get_dir_size(path: str = ".") -> int:
    """Recursively calculate directory size in bytes."""
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            filepath = os.path.join(root, f)
            if not os.path.islink(filepath):
                try:
                    total += os.path.getsize(filepath)
                except OSError:
                    pass
    return total

@bot.command(name="stats", aliases=["botinfo", "system", "info"])
async def stats(ctx: commands.Context):
    # Calculate Uptime
    now = datetime.now(timezone.utc)
    uptime = now - bot.start_time
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    days, hours = divmod(hours, 24)
    uptime_str = f"{days}d {hours}h {minutes}m {seconds}s"

    # Process Metrics
    process = psutil.Process(os.getpid())
    proc_mem = process.memory_info().rss / (1024 ** 2)  # Convert to MB
    proc_cpu = process.cpu_percent(interval=None)

    # System Metrics
    sys_mem = psutil.virtual_memory()
    sys_cpu = psutil.cpu_percent(interval=None)
    cpu_cores = psutil.cpu_count(logical=True)
    
    # Bot Directory Storage Calculation (Non-blocking)
    bot_dir_bytes = await asyncio.to_thread(get_dir_size, ".")
    bot_dir_mb = bot_dir_bytes / (1024 ** 2)

    # Discord Entity Counts
    total_guilds = len(bot.guilds)
    total_users = sum(g.member_count or 0 for g in bot.guilds)
    total_channels = sum(len(g.channels) for g in bot.guilds)
    total_cogs = len(bot.cogs)
    total_commands = len(bot.commands)

    embed = discord.Embed(
        title=f"📊 {bot.user.name} Statistics",
        color=discord.Color.blurple(),
        timestamp=now
    )
    if bot.user.display_avatar:
        embed.set_thumbnail(url=bot.user.display_avatar.url)

    embed.add_field(
        name="🤖 Bot Metrics",
        value=(
            f"**Latency:** `{round(bot.latency * 1000, 2)} ms`\n"
            f"**Uptime:** `{uptime_str}`\n"
            f"**Guilds:** `{total_guilds:,}`\n"
            f"**Users:** `{total_users:,}`\n"
            f"**Channels:** `{total_channels:,}`\n"
            f"**Commands:** `{total_commands}`\n"
            f"**Loaded Cogs:** `{total_cogs}`"
        ),
        inline=True
    )

    embed.add_field(
        name="⚡ Process Hardware",
        value=(
            f"**CPU Usage:** `{proc_cpu:.1f}%`\n"
            f"**RAM Usage:** `{proc_mem:.2f} MB`\n"
            f"**Bot Directory:** `{bot_dir_mb:.2f} MB`\n"
            f"**Threads:** `{process.num_threads()}`\n"
            f"**Async Tasks:** `{len(asyncio.all_tasks())}`\n"
            f"**HTTP Session:** `{'Active' if bot.session and not bot.session.closed else 'Closed'}`"
        ),
        inline=True
    )

    embed.add_field(
        name="🖥️ Host System Hardware",
        value=(
            f"**CPU Usage:** `{sys_cpu:.1f}%` ({cpu_cores} Cores)\n"
            f"**RAM Usage:** `{sys_mem.used / (1024**3):.2f} / {sys_mem.total / (1024**3):.2f} GB` (`{sys_mem.percent}%`)"
        ),
        inline=False
    )

    embed.add_field(
        name="⚙️ Environment",
        value=(
            f"**Python Version:** `v{platform.python_version()}`\n"
            f"**discord.py Version:** `v{discord.__version__}`\n"
            f"**Operating System:** `{platform.system()} {platform.release()} ({platform.machine()})`"
        ),
        inline=False
    )

    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)

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