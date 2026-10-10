"""
views/embeds.py

Shared embed helpers used by every cog in cogs/cmds/.

- BRAND_COLOR (#0414c7) is used for AI output and "how to use" embeds.
- ok_embed / err_embed / warn_embed / info_embed build consistent embeds with emojis.
- handle_common_error() turns the usual command errors into embeds, and shows a
  compact usage embed when a command is used without its required arguments.
- Usage embeds are intentionally tiny: just one usage line (aliases written
  inline, e.g. `.createcategory|ccat`) and an "Example" button *inside the embed*
  that opens an ephemeral message with examples and argument descriptions.
- commands_usage_embed() builds a plain usage embed listing several commands
  (no Example button), e.g. for `.settings`.
- Missing arguments show the title "Missing arg: `name`" plus that usage line.
"""
import traceback
import typing

import discord
from discord.ext import commands

from views.common_views import BaseLayout, container_from_embed, one_line_embed

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
    "forward": [
        "{p}forward 123456789012345678 987654321098765432",
        "{p}fwd 123456789012345678 #channel",
        "Reply to a message with: {p}fwd #channel",
    ],
    "echo": ["{p}echo Hello everyone!"],
    "stick": ["{p}stick Please read the rules!"],
    # utilities
    "addprefix": ["{p}addprefix 10"],
    "remind": ["{p}remind 10m Take a break", "{p}rm 2h Check the oven", "{p}rm 1d Pay rent"],
    "reminders remove": ["{p}reminders remove a1b2c3", "{p}reminders r a1b2c3"],
    # config
    "config": [
        "{p}config autolock",
        "{p}c al",
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
        "{p}set eeveelutionsrole @Eevee Ping",
        "{p}set gmax @GMax Ping",
        "{p}set para 123456789012345678",
        "{p}set rarerole @Rare Ping",
        "{p}set reg @Regional Ping",
        "{p}set lockdelay 15 sh",
        "{p}set ld 15 sh cl --global",
        "{p}set ld 20 --standard",
        "{p}set shtimer 20",
        "{p}set locktime 2h sh",
        "{p}set lt 30m sh cl --global",
        "{p}set lt 1h --stan",
        "{p}set whitelist add #chat #bots sh",
        "{p}set wl r #chat sh cl",
        "{p}set wl add #chat --standard",
        "{p}set restrictunlockers true sh",
        "{p}set ru f sh cl --global",
        "{p}set ru t --standard",
        "{p}set use-standard sh cl",
    ],
    "set lockdelay": [
        "{p}set lockdelay 15 sh",
        "{p}set ld 15 sh cl tp",
        "{p}set lockdelay 15",
        "{p}set lockdelay 15 sh --global",
        "{p}set lockdelay 20 --standard",
        "{p}set lockdelay 20 --stan",
    ],
    "set locktime": [
        "{p}set locktime 2h sh",
        "{p}set lt 30m sh cl --global",
        "{p}set locktime 2h",
        "{p}set locktime off sh",
        "{p}set locktime 2h --standard",
        "{p}set locktime 2h --stan",
    ],
    "set whitelist": [
        "{p}set whitelist add #chat #bots sh",
        "{p}set wl a 123456789012345678 sh cl",
        "{p}set wl r #chat sh cl",
        "{p}set whitelist add #chat",
        "{p}set whitelist add #chat #bots --standard",
        "{p}set whitelist remove #chat --stan",
    ],
    "set restrictunlockers": [
        "{p}set restrictunlockers true sh",
        "{p}set ru f sh cl",
        "{p}set restrict-unlockers t",
        "{p}set ru false sh --global",
        "{p}set ru t --standard",
    ],
    "set use-standard": ["{p}set use-standard sh", "{p}set use-standard sh cl tp"],
    "set shtimer": ["{p}set shtimer 15", "{p}set shtimer 30s"],
    "set cltimer": ["{p}set cltimer 15", "{p}set cltimer 30s"],
    "set rptimer": ["{p}set rptimer 15", "{p}set rptimer 30s"],
    "set tptimer": ["{p}set tptimer 15", "{p}set tptimer 30s"],
    "set rarerole": ["{p}set rarerole @Rare Ping", "{p}set rare 123456789012345678"],
    "set regionalrole": ["{p}set regionalrole @Regional Ping", "{p}set reg @Regional Ping"],
    "set gigantamaxrole": ["{p}set gigantamaxrole @GMax Ping", "{p}set gmax @GMax Ping"],
    "set paradoxrole": ["{p}set paradoxrole @Paradox Ping", "{p}set para @Paradox Ping"],
    "set eeveelutionsrole": ["{p}set eeveelutionsrole @Eevee Ping", "{p}set eevos @Eevee Ping"],
    # poketwo-management: toggle
    "toggle": [
        "{p}toggle sh",
        "{p}toggle sh cl --global",
        "{p}toggle naming",
        "{p}toggle naming --global",
        "{p}toggle locktime sh cl",
        "{p}toggle locktime sh --global",
        "{p}toggle locktime --standard",
        "{p}toggle lockdelay sh",
        "{p}toggle lockdelay sh cl --global",
        "{p}toggle lockdelay --standard",
        "{p}toggle restrictunlockers sh",
        "{p}toggle ru sh cl --global",
        "{p}toggle ru --standard",
    ],
    "toggle naming": ["{p}toggle naming", "{p}toggle naming --global"],
    "toggle lockdelay": [
        "{p}toggle lockdelay sh",
        "{p}toggle ld sh cl",
        "{p}toggle lockdelay sh cl --global",
        "{p}toggle lockdelay --standard",
    ],
    "toggle locktime": [
        "{p}toggle locktime sh",
        "{p}toggle lt sh cl --global",
        "{p}toggle locktime --standard",
    ],
    "toggle restrictunlockers": [
        "{p}toggle restrictunlockers res",
        "{p}toggle ru res sh",
        "{p}toggle restrict-unlockers res --global",
        "{p}toggle ru --standard",
    ],
    # poketwo-helper
    "autolockdm": ["{p}autolockdm"],
    # poketwo-management: settings
    "channelsettings": ["{p}channelsettings", "{p}chsettings"],
    # poketwo-utils
    "dex": ["{p}dex pikachu", "{p}dex 25", "{p}pokedex #25"],
    "extract": ["Reply to a Pokétwo embed with: {p}extract", "Reply to a Pokétwo embed with: {p}ex"],
    "checkflee": ["{p}checkflee"],
}


# ---------------------------------------------------------------------------
# Group overviews: what `.set` / `.toggle` (no subcommand) print. Hand-written so the list
# keeps this order and shows every alias. {} = required, [] = optional. {p} = the prefix.
# ---------------------------------------------------------------------------
USAGE_OVERVIEWS = {
    "set": [
        "{p}set eeveelutionsrole|eeveelutions|eevosrole|eevos {role}",
        "{p}set gigantamaxrole|gigantamax|gmaxrole|gmax {role}",
        "{p}set paradoxrole|paradox|pararole|para {role}",
        "{p}set rarerole|rare|rarole|ra {role}",
        "{p}set regionalrole|regional|regrole|reg {role}",
        "{p}set lockdelay|lock-delay|delay|ld {time} [lock] [--global|--standard]",
        "{p}set shtimer/cltimer/rptimer/tptimer {timer}",
        "{p}set locktime|lock-time|time|lt {time} [lock(s)] [--global|--standard]",
        "{p}set whitelist|wl {add|a|remove|r} {channel mention(s) / channel id(s)} [lock(s)] [--standard]",
        "{p}set restrictunlockers|restrict-unlockers|ru {true|false|t|f} [lock(s)] [--global|--standard]",
        "{p}set use-standard {lock(s)}",
    ],
    "toggle": [
        "{p}toggle {lock} [--global]",
        "{p}toggle naming [--global]",
        "{p}toggle locktime {lock(s)} [--global|--standard]",
        "{p}toggle lockdelay {lock(s)} [--global|--standard]",
        "{p}toggle restrictunlockers|restrict-unlockers|ru {lock(s)} [--global|--standard]",
    ],
}
USAGE_OVERVIEW_NOTES = {
    "set": "`{}` required • `[]` optional • `--standard` / `--stan` can't be combined with `--global`, "
           "and only works with lockdelay, locktime, whitelist and restrictunlockers.",
    "toggle": "`{}` required • `[]` optional • `--standard` / `--stan` can't be combined with `--global`, "
              "and only works with lockdelay, locktime and restrictunlockers.",
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


class ExampleLayout(BaseLayout):
    """The usage embed with one 'Example' button *inside* it (Components V2 container).
    Pressing it sends the examples as an ephemeral message (anyone can press it;
    only the presser sees the reply)."""

    def __init__(self, usage: discord.Embed, examples: discord.Embed):
        super().__init__(timeout=120)
        self.examples = examples

        button = discord.ui.Button(label=examples.title or "Example", style=discord.ButtonStyle.secondary)
        button.callback = self._on_press
        self.add_item(container_from_embed(usage, discord.ui.ActionRow(button)))

    async def _on_press(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=self.examples, ephemeral=True)


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

    overview = USAGE_OVERVIEWS.get(group.qualified_name)
    if overview:
        lines.append(USAGE_OVERVIEW_NOTES[group.qualified_name])
        lines.append("\n".join(f"`{line.replace('{p}', prefix)}`" for line in overview))
        return discord.Embed(
            title=title or "Usage",
            description="\n".join(lines)[:4096],
            color=BRAND_COLOR,
        )

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
    """Send the compact usage embed for ctx.command (works for groups too).
    When examples / argument descriptions exist, an 'Example' button is placed
    inside the embed itself."""
    cmd = ctx.command
    if isinstance(cmd, commands.Group) and (not cmd.clean_params or cmd.qualified_name in USAGE_OVERVIEWS):
        embed = await group_usage_embed(ctx, note=note, title=title)
    else:
        embed = usage_embed(ctx, note=note, title=title)

    examples = example_embed(ctx)
    if examples is None:
        return await ctx.reply(embed=embed, mention_author=False)

    view = ExampleLayout(embed, examples)
    msg = await ctx.reply(view=view, mention_author=False)
    view.message = msg
    return msg


def commands_usage_embed(ctx, *names: str, title: str = "Usage") -> discord.Embed:
    """Plain usage embed listing the usage line of each named command (no Example button).
    Unknown names are skipped."""
    prefix = ctx.clean_prefix
    lines = []
    for name in names:
        cmd = ctx.bot.get_command(name)
        if cmd is not None and not cmd.hidden:
            lines.append(f"`{_usage_line(cmd, prefix)}`")
    return discord.Embed(
        title=title,
        description="\n".join(lines) or "No commands available.",
        color=BRAND_COLOR,
    )


async def handle_common_error(ctx, error) -> bool:
    """Sends an embed for common errors. Returns True if handled, False otherwise."""
    if isinstance(error, commands.MissingRequiredArgument):
        await send_usage(ctx, title=f"Missing arg: `{error.param.name}`")
    elif isinstance(error, commands.UserInputError):
        await send_usage(ctx, note=str(error) or "Invalid argument provided.")
    elif isinstance(error, commands.MissingPermissions):
        perms = ", ".join(f"`{p.replace('_', ' ').title()}`" for p in error.missing_permissions)
        await ctx.reply(embed=err_embed(f"Missing permissions: {perms}"), mention_author=False)
    elif isinstance(error, commands.BotMissingPermissions):
        perms = ", ".join(f"`{p.replace('_', ' ').title()}`" for p in error.missing_permissions)
        await ctx.reply(embed=err_embed(f"I need: {perms}"), mention_author=False)
    elif isinstance(error, commands.NotOwner):
        await ctx.reply(embed=err_embed("Owner only.", emoji="🚫"), mention_author=False)
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.reply(embed=err_embed("Server only.", emoji="🚫"), mention_author=False)
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.reply(embed=warn_embed("Slow Down", f"Retry in **{error.retry_after:.1f}s**.", emoji="⏳"), mention_author=False)
    elif isinstance(error, commands.CheckFailure):
        await ctx.reply(embed=err_embed("You can't use this here.", emoji="🚫"), mention_author=False)
    elif isinstance(error, commands.CommandInvokeError) and isinstance(error.original, discord.Forbidden):
        await ctx.reply(embed=err_embed("Check my permissions and role position."), mention_author=False)
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
    await ctx.reply(embed=err_embed(f"Internal error: {original}", emoji="⚠️"), mention_author=False)