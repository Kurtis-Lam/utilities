import asyncio
import discord
from discord.ext import commands

class Ping(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.latencies = []
        self.editing = False
        self.current_channel = None
        self.current_message = None
        self.task = None 

    @commands.hybrid_command(name="ping", description="Check the bot's latency", with_app_command = True)
    async def ping(self, ctx):
        msg = await ctx.send("Pong!")
        latency = self.bot.latency * 1000
        await msg.edit(content = f"Pong! **{latency:.2f} ms**")

async def setup(bot):
    await bot.add_cog(Ping(bot))