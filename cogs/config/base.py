from discord.ext import commands


async def setup(bot: commands.Bot):
    extensions = (
        "cogs.config.autolock",
        "cogs.config.joins",
        "cogs.config.spawns",
        "cogs.config.timer",
        "cogs.config.tickets",
        "cogs.config.verify",
        "cogs.owner_cmds.grinder",
    )
    for extension in extensions:
        if extension in bot.extensions:
            continue
        await bot.load_extension(extension)
