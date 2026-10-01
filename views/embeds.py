"""
views/embeds.py

Shared embed helpers used by every cog in cogs/cmds/.

- BRAND_COLOR (#0414c7) is used for AI output and "how to use" embeds.
- ok_embed / err_embed / warn_embed / info_embed build consistent embeds with emojis.
- handle_common_error() turns the usual command errors into embeds, and shows a
  usage guide when a command is used without its required arguments.
"""
import traceback

import discord
from discord.ext import commands

BRAND_COLOR = discord.Color(0x0414C7)
SUCCESS_COLOR = discord.Color.green()
ERROR_COLOR = discord.Color.red()
WARN_COLOR = discord.Color.gold()


def make_embed(title, description=None, *, color=BRAND_COLOR, emoji=None) -> discord.Embed:
    if emoji and title:
        title = f"{emoji} {title}"
    return discord.Embed(title=title, description=description, color=color)


def ok_embed(title, description=None, *, emoji="✅", color=SUCCESS_COLOR):
    return make_embed(title, description, color=color, emoji=emoji)


def err_embed(title="Error", description=None, *, emoji="❌", color=ERROR_COLOR):
    return make_embed(title, description, color=color, emoji=emoji)


def warn_embed(title, description=None, *, emoji="⚠️", color=WARN_COLOR):
    return make_embed(title, description, color=color, emoji=emoji)


def info_embed(title, description=None, *, emoji="ℹ️", color=BRAND_COLOR):
    return make_embed(title, description, color=color, emoji=emoji)


# ---------------------------------------------------------------------------
# Usage examples shown in "how to use" embeds. {p} is replaced by the prefix.
# Keys are command qualified names.
# ---------------------------------------------------------------------------
USAGE_EXAMPLES = {
    # channels
    "createchannel": [
        "{p}createchannel general-chat",
        "{p}cch general-chat \"Staff Area\"",
        "{p}cch secret-chat --prelock --prehide",
    ],
    "renamechannel": ["{p}renamechannel new-name", "{p}rch new-name #old-channel"],
    # categories
    "createcategory": [
        "{p}createcategory Staff Area",
        "{p}ccat Staff Area --prelock",
        "{p}ccat Private @user --prehide",
    ],
    "renamecategory": ["{p}renamecategory new-name", "{p}rcat new-name old-category"],
    # members
    "kick": ["{p}kick @user Spamming"],
    "ban": ["{p}ban @user Breaking the rules"],
    "nick": ["{p}nick @user Cool Name", "{p}nick @user reset"],
    "mute": ["{p}mute @user", "{p}mute @user 10m", "{p}mute @user 2h Spamming"],
    "unmute": ["{p}unmute @user"],
    # roles
    "giverole": ["{p}giverole @user @role", "{p}gr @user @role"],
    "removerole": ["{p}removerole @user @role", "{p}rr @user @role"],
    "setperms": [
        "{p}setperms admin @role @user",
        "{p}setperms member @role",
        "{p}setperms blocked @user",
    ],
    "createrole": [
        "{p}createrole VIP",
        "{p}cr VIP red",
        "{p}cr Mods light-blue admin @user",
        "{p}cr Staff #FF0000 basic @user1 @user2",
    ],
    "deleterole": ["{p}deleterole @role", "{p}dr @role"],
    # messages
    "purge": ["{p}purge 10", "{p}purge 25 @user", "{p}purge *"],
    "echo": ["{p}echo Hello everyone!"],
    "stick": ["{p}stick Please read the rules!"],
    # utilities
    "addprefix": ["{p}addprefix 10"],
    "remind": ["{p}remind 10m Take a break", "{p}rm 2h Check the oven", "{p}rm 1d Pay rent"],
    "reminders remove": ["{p}reminders remove a1b2c3", "{p}reminders r a1b2c3"],
    # config
    "config": [
        "{p}config autolock",
        "{p}c a",
        "{p}c spawns",
        "{p}c joins",
    ],
    # grinder
    "edit": [
        "{p}edit @user datafile1",
        "{p}edit 123456789012345678 file1 file2.json",
    ],
    "syncguilds": [
        "{p}syncguilds 123456789012345678 987654321098765432",
        "{p}sg 123456789012345678 987654321098765432",
    ],
    "pause": ["{p}pause", "{p}pause 1 2 3", "{p}pause 1 2h", "{p}p 4 30m"],
    "resume": ["{p}resume", "{p}resume 1 2", "{p}r 3"],
    # poketwo-management: set
    "set": [
        "{p}set lockdelay 15 sh",
        "{p}set rarerole @Rare Ping",
    ],
    "set lockdelay": [
        "{p}set lockdelay 15 sh",
        "{p}set lockdelay 15 sh cl tp",
        "{p}set lockdelay 15 all",
        "{p}set lockdelay 15 sh --global",
    ],
    "set rarerole": ["{p}set rarerole @Rare Ping", "{p}set rarerole"],
    "set regionalrole": ["{p}set regionalrole @Regional Ping", "{p}set regionalrole"],
    "set gigantamaxrole": ["{p}set gigantamaxrole @GMax Ping", "{p}set gigantamaxrole"],
    "set paradoxrole": ["{p}set paradoxrole @Paradox Ping", "{p}set paradoxrole"],
    "set eeveelutionsrole": ["{p}set eeveelutionsrole @Eevee Ping", "{p}set eeveelutionsrole"],
    # poketwo-management: toggle
    "toggle": [
        "{p}toggle shlock",
        "{p}toggle shlock --global",
        "{p}toggle lockdelay sh cl",
    ],
    "toggle lockdelay": [
        "{p}toggle lockdelay sh",
        "{p}toggle lockdelay sh cl",
        "{p}toggle lockdelay sh cl --global",
    ],
    "toggle restrictunlockers": [
        "{p}toggle restrictunlockers res",
        "{p}toggle restuls res sh",
        "{p}toggle restrictunlockers res --global",
    ],
    # poketwo-management: settings
    "channelsettings": ["{p}channelsettings", "{p}chsettings"],
    # poketwo-utils
    "dex": ["{p}dex pikachu", "{p}dex 25", "{p}pokedex #25"],
    "extract": ["Reply to a Pokétwo embed with: {p}extract", "Reply to a Pokétwo embed with: {p}ex"],
    "checkflee": ["{p}checkflee"],
}


def _param_descriptions(command) -> dict:
    """Collect per-argument descriptions from commands.parameter / app_commands.describe."""
    out = {}
    app_cmd = getattr(command, "app_command", None)
    app_params = getattr(app_cmd, "_params", None) or {}
    for name, param in command.clean_params.items():
        desc = getattr(param, "description", None)
        if not isinstance(desc, str) or not desc.strip() or desc == "…":
            ap = app_params.get(name)
            desc = getattr(ap, "description", None)
        if isinstance(desc, str) and desc.strip() and desc != "…":
            out[name] = desc.strip()
    return out


def usage_embed(ctx, note: str = None) -> discord.Embed:
    """Builds a '#0414c7' embed explaining how to use ctx.command."""
    cmd = ctx.command
    prefix = ctx.clean_prefix
    qname = cmd.qualified_name

    embed = discord.Embed(
        title=f"📖 How to use `{prefix}{qname}`",
        description=(f"❌ {note}\n\n" if note else "") + (cmd.description or cmd.help or "No description provided."),
        color=BRAND_COLOR,
    )

    signature = f"{prefix}{qname} {cmd.signature}".strip()
    embed.add_field(name="📝 Usage", value=f"`{signature}`", inline=False)

    if cmd.aliases:
        # Subcommand aliases belong to the parent, e.g. "reminders r"
        parent = f"{cmd.parent.qualified_name} " if cmd.parent else ""
        alias_text = ", ".join(f"`{prefix}{parent}{a}`" for a in cmd.aliases)
        embed.add_field(name="🏷️ Aliases", value=alias_text, inline=False)

    descriptions = _param_descriptions(cmd)
    if descriptions:
        lines = []
        for name, param in cmd.clean_params.items():
            if name not in descriptions:
                continue
            shown = getattr(param, "displayed_name", None) or name
            tag = "required" if param.required else "optional"
            lines.append(f"• `{shown}` *({tag})* — {descriptions[name]}")
        if lines:
            embed.add_field(name="📋 Arguments", value="\n".join(lines)[:1024], inline=False)

    examples = USAGE_EXAMPLES.get(qname)
    if examples:
        embed.add_field(
            name="💡 Examples",
            value="\n".join(f"`{e.format(p=prefix)}`" for e in examples)[:1024],
            inline=False,
        )

    embed.set_footer(text="<required>  [optional]")
    return embed


async def group_usage_embed(ctx, note: str = None) -> discord.Embed:
    """Builds a '#0414c7' embed listing the subcommands of ctx.command (a command group).

    Only subcommands the invoker is actually allowed to run are listed."""
    group = ctx.command
    prefix = ctx.clean_prefix
    qname = group.qualified_name

    embed = discord.Embed(
        title=f"📖 How to use `{prefix}{qname}`",
        description=(f"❌ {note}\n\n" if note else "")
        + (group.description or group.help or "Pick one of the subcommands below."),
        color=BRAND_COLOR,
    )
    embed.add_field(name="📝 Usage", value=f"`{prefix}{qname} <subcommand>`", inline=False)

    if group.aliases:
        alias_text = ", ".join(f"`{prefix}{a}`" for a in group.aliases)
        embed.add_field(name="🏷️ Aliases", value=alias_text, inline=False)

    lines = []
    for sub in sorted(group.commands, key=lambda c: c.name):
        if sub.hidden:
            continue
        try:
            if not await sub.can_run(ctx):
                continue
        except commands.CommandError:
            continue
        sub_aliases = ", ".join("`" + a + "`" for a in sub.aliases)
        alias_text = f" *(aliases: {sub_aliases})*" if sub_aliases else ""
        summary = sub.description or sub.short_doc or "No description provided."
        lines.append(f"• `{prefix}{sub.qualified_name}`{alias_text}\n   {summary}")
    embed.add_field(
        name="📂 Subcommands",
        value="\n".join(lines)[:1024] if lines else "*No subcommands are available to you.*",
        inline=False,
    )

    examples = USAGE_EXAMPLES.get(qname)
    if examples:
        embed.add_field(
            name="💡 Examples",
            value="\n".join(f"`{e.format(p=prefix)}`" for e in examples)[:1024],
            inline=False,
        )

    embed.set_footer(text="<required>  [optional]")
    return embed


async def send_usage(ctx, note: str = None):
    """Send the '#0414c7' how-to-use embed for ctx.command (works for groups too)."""
    if isinstance(ctx.command, commands.Group):
        embed = await group_usage_embed(ctx, note=note)
    else:
        embed = usage_embed(ctx, note=note)
    await ctx.send(embed=embed)


async def handle_common_error(ctx, error) -> bool:
    """Sends an embed for common errors. Returns True if handled, False otherwise."""
    if isinstance(error, commands.MissingRequiredArgument):
        await send_usage(ctx, note=f"Missing required argument: `{error.param.name}`")
    elif isinstance(error, commands.UserInputError):
        await send_usage(ctx, note=str(error) or "Invalid argument provided.")
    elif isinstance(error, commands.MissingPermissions):
        perms = ", ".join(f"`{p.replace('_', ' ').title()}`" for p in error.missing_permissions)
        await ctx.send(embed=err_embed(
            "Missing Permissions", f"You lack the required permissions to use this command: {perms}"))
    elif isinstance(error, commands.BotMissingPermissions):
        perms = ", ".join(f"`{p.replace('_', ' ').title()}`" for p in error.missing_permissions)
        await ctx.send(embed=err_embed(
            "Bot Missing Permissions", f"I am missing permissions to do this. Please give me: {perms}"))
    elif isinstance(error, commands.NotOwner):
        await ctx.send(embed=err_embed("Owner Only", "Only the bot owner can use this command.", emoji="🚫"))
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.send(embed=err_embed("Server Only", "This command can only be used in a server.", emoji="🚫"))
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(embed=warn_embed(
            "Slow Down", f"Try again in **{error.retry_after:.1f}s**.", emoji="⏳"))
    elif isinstance(error, commands.CheckFailure):
        await ctx.send(embed=err_embed("Check Failed", "You can't use this command here.", emoji="🚫"))
    elif isinstance(error, commands.CommandInvokeError) and isinstance(error.original, discord.Forbidden):
        await ctx.send(embed=err_embed(
            "Forbidden", "Discord refused that action. Check my permissions and role position."))
    else:
        return False
    return True


async def handle_command_error(ctx, error) -> None:
    """One-stop error handler: common errors become embeds (missing arguments show
    the how-to-use embed); anything else becomes an 'Internal Error' embed."""
    if await handle_common_error(ctx, error):
        return
    original = getattr(error, "original", error)
    traceback.print_exception(type(original), original, original.__traceback__)
    await ctx.send(embed=err_embed("Internal Error", f"Something went wrong: `{original}`", emoji="⚠️"))