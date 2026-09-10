from discord.ext import commands

@commands.hybrid_group(name="config", aliases=["c"], invoke_without_command=True)
async def config_group(ctx: commands.Context):
    if ctx.invoked_subcommand is None:
        await ctx.send_help(ctx.command)

async def setup(bot: commands.Bot):
    await bot.load_extension("cogs.config.autolockconfig")
    await bot.load_extension("cogs.config.grinder")
    await bot.load_extension("cogs.config.joins")

    bot.add_command(config_group)