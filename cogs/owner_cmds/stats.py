import asyncio
import os
import platform
from datetime import datetime, timezone

import discord
import psutil
from discord.ext import commands

from cogs.owner_cmds.utils import OwnerCog, get_dir_size


class Stats(OwnerCog):
    def __init__(self, bot: commands.Bot):
        super().__init__(bot)

    @commands.command(name="stats", aliases=["botinfo", "system", "info"])
    @commands.is_owner()
    async def stats(self, ctx: commands.Context):
        bot = self.bot
        now = datetime.now(timezone.utc)
        uptime = now - bot.start_time
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        days, hours = divmod(hours, 24)
        uptime_str = f"{days}d {hours}h {minutes}m {seconds}s"

        process = psutil.Process(os.getpid())
        proc_mem = process.memory_info().rss / (1024 ** 2)
        proc_cpu = process.cpu_percent(interval=None)

        sys_mem = psutil.virtual_memory()
        sys_cpu = psutil.cpu_percent(interval=None)
        cpu_cores = psutil.cpu_count(logical=True)

        bot_dir_bytes = await asyncio.to_thread(get_dir_size, ".")
        bot_dir_mb = bot_dir_bytes / (1024 ** 2)

        total_guilds = len(bot.guilds)
        total_users = sum(g.member_count or 0 for g in bot.guilds)
        total_channels = sum(len(g.channels) for g in bot.guilds)
        total_cogs = len(bot.cogs)
        total_commands = len(bot.commands)

        embed = discord.Embed(
            title=f"📊 {bot.user.name} Statistics",
            color=discord.Color.blurple(),
            timestamp=now,
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
            inline=True,
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
            inline=True,
        )
        embed.add_field(
            name="🖥️ Host System Hardware",
            value=(
                f"**CPU Usage:** `{sys_cpu:.1f}%` ({cpu_cores} Cores)\n"
                f"**RAM Usage:** `{sys_mem.used / (1024**3):.2f} / {sys_mem.total / (1024**3):.2f} GB` (`{sys_mem.percent}%`)"
            ),
            inline=False,
        )
        embed.add_field(
            name="⚙️ Environment",
            value=(
                f"**Python Version:** `v{platform.python_version()}`\n"
                f"**discord.py Version:** `v{discord.__version__}`\n"
                f"**Operating System:** `{platform.system()} {platform.release()} ({platform.machine()})`"
            ),
            inline=False,
        )
        embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(Stats(bot))