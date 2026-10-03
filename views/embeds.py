"""
views/embeds.py

Shared embed helpers used by every cog in cogs/cmds/.

- BRAND_COLOR (#0414c7) is used for AI output and "how to use" embeds.
- ok_embed / err_embed / warn_embed / info_embed build consistent embeds with emojis.
- handle_common_error() turns the usual command errors into embeds, and shows a
  compact usage embed when a command is used without its required arguments.
- Usage embeds are intentionally tiny: just one usage line (aliases written
  inline, e.g. `.createcategory|ccat`) and an "Example" button that opens an
  ephemeral message with examples and argument descriptions.
- Missing arguments show the title "Missing arg: `name`" plus that usage line.
"""
import traceback
import typing

import discord
from discord.ext import commands

from views.common import BaseView, one_line_embed

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
    """One-line error: if a description is given it becomes the title (the name is dropped)."""
    return one_line_embed(description or title, emoji, color)


def warn_embed(title, description=None, *, emoji="⚠️", color=WARN_COLOR):
    return make_embed(title, description, color=color, emoji=emoji)


def info_embed(title, description=None, *, emoji="ℹ️", color=BRAND_COLOR):
    return make_embed(title, description, color=color, emoji=emoji)


# ---------------------------------------------------------------------------
# Usage examples shown by the "Example" button. {p} is replaced by the prefix.
# Keys are command qualified names.
# ---------------------------------------------------------------------------
USAGE_EXAMPLES = {
    # ai
    "ai add": ["{p}ai add #chat", "{p}ai a #chat #bots"],
    "ai remove": ["{p}ai remove #chat", "{p}ai r #chat #bots"],
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
    "muterole": ["{p}muterole", "{p}muterole @Muted", "{p}muterole 123456789012345678"],
    "warn": [
        "{p}warn @user",
        "{p}warn @user Spamming",
        "{p}warn remove @user a1b2c3",
    ],
    "warn remove": ["{p}warn remove @user a1b2c3", "{p}warn r @user a1b2c3"],
    "warns": [
        "{p}warns @user",
        "{p}warns 123456789012345678",
        "{p}warns remove @user a1b2c3",
    ],
    "warns remove": ["{p}warns remove @user a1b2c3", "{p}warns r @user a1b2c3"],
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _command_label(cmd, prefix: str) -> str:
    """'.createcategory|ccat' / '.reminders remove|r' (aliases written inline)."""
    parent = f"{cmd.parent.qualified_name} " if cmd.parent else ""
    names = "|".join([cmd.name, *cmd.aliases])
    return f"{prefix}{parent}{names}"


def _usage_line(cmd, prefix: str) -> str:
    return f"{_command_label(cmd, prefix)} {cmd.signature}".strip()


def example_embed(ctx) -> typing.Optional[discord.Embed]:
    """Builds the ephemeral 'Example' message for ctx.command (None if there is nothing to show)."""
    cmd = ctx.command
    if cmd is None:
        return None
    prefix = ctx.clean_prefix

    examples = list(USAGE_EXAMPLES.get(cmd.qualified_name, []))
    if not examples and isinstance(cmd, commands.Group):
        for sub in sorted(cmd.commands, key=lambda c: c.name):
            examples.extend(USAGE_EXAMPLES.get(sub.qualified_name, []))

    sections = []
    if examples:
        sections.append("\n".join(f"`{e.format(p=prefix)}`" for e in examples))

    descriptions = _param_descriptions(cmd)
    if descriptions:
        lines = []
        for name, param in cmd.clean_params.items():
            if name not in descriptions:
                continue
            shown = getattr(param, "displayed_name", None) or name
            tag = "" if param.required else " *(optional)*"
            lines.append(f"`{shown}`{tag} — {descriptions[name]}")
        if lines:
            body = "\n".join(lines)
            sections.append(f"**Arguments**\n{body}" if examples else body)

    if not sections:
        return None
    return discord.Embed(
        title="Example" if examples else "Arguments",
        description="\n\n".join(sections)[:4096],
        color=BRAND_COLOR,
    )


class ExampleView(BaseView):
    """One 'Example' button. Pressing it sends the examples as an ephemeral message
    (anyone can press it; only the presser sees the reply)."""

    def __init__(self, embed: discord.Embed):
        super().__init__(timeout=120)
        self.example_embed = embed

    @discord.ui.button(label="Example", style=discord.ButtonStyle.secondary)
    async def example_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=self.example_embed, ephemeral=True)


def example_view(ctx) -> typing.Optional[ExampleView]:
    embed = example_embed(ctx)
    return ExampleView(embed) if embed else None


# ---------------------------------------------------------------------------
# Usage embeds
# ---------------------------------------------------------------------------

def usage_embed(ctx, note: str = None, title: str = None) -> discord.Embed:
    """Compact '#0414c7' usage embed for ctx.command: optional short note + one usage line."""
    cmd = ctx.command
    prefix = ctx.clean_prefix

    lines = []
    if note:
        lines.append(note)
    lines.append(f"`{_usage_line(cmd, prefix)}`")

    return discord.Embed(
        title=title or "Usage",
        description="\n".join(lines),
        color=BRAND_COLOR,
    )


async def group_usage_embed(ctx, note: str = None, title: str = None) -> discord.Embed:
    """Compact '#0414c7' embed listing the subcommands of ctx.command (a command group).

    Only subcommands the invoker is actually allowed to run are listed."""
    group = ctx.command
    prefix = ctx.clean_prefix

    lines = []
    if note:
        lines.append(note)

    subs = []
    for sub in sorted(group.commands, key=lambda c: c.name):
        if sub.hidden:
            continue
        try:
            if not await sub.can_run(ctx):
                continue
        except commands.CommandError:
            continue
        subs.append(f"`{_usage_line(sub, prefix)}`")

    lines.append("\n".join(subs) if subs else f"`{_command_label(group, prefix)} <subcommand>`")

    return discord.Embed(
        title=title or "Usage",
        description="\n".join(lines)[:4096],
        color=BRAND_COLOR,
    )


async def send_usage(ctx, note: str = None, title: str = None):
    """Send the compact usage embed for ctx.command (works for groups too),
    with an 'Example' button when examples / argument descriptions exist."""
    cmd = ctx.command
    if isinstance(cmd, commands.Group) and not cmd.clean_params:
        embed = await group_usage_embed(ctx, note=note, title=title)
    else:
        embed = usage_embed(ctx, note=note, title=title)

    view = example_view(ctx)
    if view is None:
        return await ctx.send(embed=embed)

    msg = await ctx.send(embed=embed, view=view)
    view.message = msg
    return msg


async def handle_common_error(ctx, error) -> bool:
    """Sends an embed for common errors. Returns True if handled, False otherwise."""
    if isinstance(error, commands.MissingRequiredArgument):
        await send_usage(ctx, title=f"Missing arg: `{error.param.name}`")
    elif isinstance(error, commands.UserInputError):
        await send_usage(ctx, note=str(error) or "Invalid argument provided.")
    elif isinstance(error, commands.MissingPermissions):
        perms = ", ".join(f"`{p.replace('_', ' ').title()}`" for p in error.missing_permissions)
        await ctx.send(embed=err_embed(f"Missing permissions: {perms}"))
    elif isinstance(error, commands.BotMissingPermissions):
        perms = ", ".join(f"`{p.replace('_', ' ').title()}`" for p in error.missing_permissions)
        await ctx.send(embed=err_embed(f"I need: {perms}"))
    elif isinstance(error, commands.NotOwner):
        await ctx.send(embed=err_embed("Owner only.", emoji="🚫"))
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.send(embed=err_embed("Server only.", emoji="🚫"))
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(embed=warn_embed("Slow Down", f"Retry in **{error.retry_after:.1f}s**.", emoji="⏳"))
    elif isinstance(error, commands.CheckFailure):
        await ctx.send(embed=err_embed("You can't use this here.", emoji="🚫"))
    elif isinstance(error, commands.CommandInvokeError) and isinstance(error.original, discord.Forbidden):
        await ctx.send(embed=err_embed("Check my permissions and role position."))
    else:
        return False
    return True


async def handle_command_error(ctx, error) -> None:
    """One-stop error handler: common errors become embeds (missing arguments show
    the usage embed); anything else becomes an 'Internal Error' embed."""
    if await handle_common_error(ctx, error):
        return
    original = getattr(error, "original", error)
    traceback.print_exception(type(original), original, original.__traceback__)
    await ctx.send(embed=err_embed(f"Internal error: {original}", emoji="⚠️"))