"""Compatibility loader for installations whose config.json still lists grinder_config.

The actual owner-only .grinder command lives in cogs.owner_cmds.grinder.
"""
from discord.ext import commands


async def setup(bot: commands.Bot):
    extension = "cogs.owner_cmds.grinder"
    if extension not in bot.extensions:
        await bot.load_extension(extension)
