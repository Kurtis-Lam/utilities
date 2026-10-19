from discord.ext import commands

from views.embeds import handle_command_error, send_usage


@commands.hybrid_group(
    name="config",
    aliases=["c"],
    invoke_without_command=True,
    description="Open the config menus.",
)
async def config_group(ctx: commands.Context):
    if ctx.invoked_subcommand is None:
        passed = getattr(ctx, "subcommand_passed", None)
        note = f"Unknown subcommand `{passed}`." if passed else None
        await send_usage(ctx, note=note)


@config_group.error
async def config_group_error(ctx: commands.Context, error: Exception):
    await handle_command_error(ctx, error)
