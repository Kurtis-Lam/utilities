"""Custom help command.

Nothing here has to be edited when commands change: ``.help`` reads everything straight
from the loaded cogs.

* Command description    -> ``description="..."`` on the command decorator.
* Argument descriptions  -> ``commands.parameter(description="...")`` in the function signature
                            (or ``@app_commands.describe(...)``). For ``*args`` style commands that
                            can't use ``commands.parameter``: ``extras={"args": {"name": "..."}}``.
* Examples               -> ``USAGE_EXAMPLES`` in views/embeds.py (optional).
* Access level           -> detected from the command's checks: ``has_permissions`` = admin,
                            ``is_owner`` / ``OwnerCog`` = owner. Commands that check permissions inside
                            their body can be listed in ``ACCESS_OVERRIDES``.
* Overview grouping      -> ``HELP_CATEGORIES`` below (commands that aren't listed land in "Other").
                            Categories listed in ``POKETWO_CATEGORIES`` are shown in their own
                            "Poketwo Commands" section (user and admin commands together, admin-only
                            ones tagged ``(admin)``).

Usage:  .help                 User Commands, Admin Commands, Poketwo Commands and (owners only)
                              Owner Commands, each split into categories
        .help <command>       usage, aliases, arguments (+ descriptions), subcommands, examples.
                              Admin-only commands get a ``*`` after their name, owner-only ones ``***``.
                              Poketwo commands use ``(admin)`` instead of ``*`` for admin-only ones
        .help <cog>           the commands of one cog
        .help <group> <sub>   e.g. .help set lockdelay

Owner-only commands are invisible to everyone else: they are never listed, and asking for them
(``.help pull``) gets no response at all.
"""
import difflib
import re
import typing

import discord
from discord import app_commands
from discord.ext import commands

from cogs.owner_cmds.utils import OwnerCog
from views.embeds import BRAND_COLOR, USAGE_EXAMPLES, _param_descriptions, _usage_line, handle_common_error
from views.navigate import PaginatorView

PAGE_CHAR_LIMIT = 3800
FIELD_CHAR_LIMIT = 1000

USER, ADMIN, OWNER = 0, 1, 2   # access levels
POKETWO = 3                     # display section only (not an access level)
AI = 4                          # display section only (not an access level)

SECTIONS = {
    USER: ("", "User Commands"),
    ADMIN: ("", "Admin Commands"),
    POKETWO: ("", "Pokétwo Commands"),
    AI: ("", "AI Commands"),
    OWNER: ("", "Owner Commands"),
}
SECTION_ORDER = (USER, ADMIN, POKETWO, AI, OWNER)
MARKERS = {USER: "", ADMIN: "*", OWNER: "***"}
LEGEND = {ADMIN: "* Admin only", OWNER: "*** Owner only"}

# Admin-only marker used inside the Poketwo section / on Poketwo commands.
POKETWO_ADMIN_MARKER = "(admin)"
POKETWO_ADMIN_LEGEND = f"{POKETWO_ADMIN_MARKER} Admin only"

# Commands that never appear in .help (qualified names). Their subcommands are hidden too.
HELP_HIDDEN = {"grinder", "pause", "resume", "config grinder"}

# Show the Admin Commands section to everybody (False = only to people who can run them).
SHOW_ADMIN_COMMANDS_TO_EVERYONE = True

# Check function names (the part before ".<locals>") that make a command admin / owner only.
ADMIN_CHECKS = {"has_permissions", "has_guild_permissions"}
OWNER_CHECKS = {"is_owner", "is_bot_owner"}

# Commands that verify permissions inside their body, so no check can be detected on them.
ACCESS_OVERRIDES: dict[str, int] = {
    "config": ADMIN,
    "ai config": ADMIN,
    "ai enable": ADMIN,
    "ai disable": ADMIN,
    "ai add": ADMIN,
    "ai remove": ADMIN,
}

# ===================================================================
# Overview categories: category title -> top-level command names.
# A category shows up under User / Admin / Owner Commands depending on each command's access
# level, so "Message" can list `snipe` for everyone and `purge` for admins. Subcommands follow
# their parent's category (e.g. `reserves add` is listed under Pokétwo Pings).
# ===================================================================
HELP_CATEGORIES: dict[str, list[str]] = {
    "General": ["help", "ping"],
    "Category": [
        "createcategory", "deletecategory", "renamecategory", "lockcategory",
        "unlockcategory", "hidecategory", "unhidecategory",
    ],
    "Channel": [
        "createchannel", "deletechannel", "renamechannel", "movechannel", "lockchannel",
        "unlockchannel", "hidechannel", "unhidechannel", "nuke",
    ],
    "Role": ["createrole", "deleterole", "giverole", "removerole", "setperms"],
    "Member": [
        "membercount", "whois", "kick", "ban", "nick", "mute", "unmute", "muterole",
        "warn", "warns", "kicked", "banned", "mutelogs", "invites",
    ],
    "Message": [
        "snipe", "messages", "timediff", "purge", "forward", "pin", "unpin", "pins",
        "echo", "stick", "unstick", "sticks",
    ],
    "Giveaway": ["giveaway"],
    "AI": ["ai"],
    "Configurations": ["config"],
    "Pokétwo Pings": ["sh", "cl", "tp", "rp", "reserves"],
    "Pokétwo Helper": [
        "afk", "catches", "catchleaderboard", "catcheslink", "lock", "unlock", "lockstats",
        "unlockallchannels", "autolockdm", "starboard", "rec",
    ],
    "Pokétwo Management": ["set", "toggle", "settings", "channelsettings", "serversettings"],
    "Pokétwo Utils": ["catchtime", "dex", "extract"],
    "Utilities": ["remind", "timer", "reminders"],
    "Grinder": ["syncguilds"],
    "Bot": ["stats", "gitstats", "pull", "reload", "restart", "sync", "clearautomodlogs"],
}
OTHER_CATEGORY = "Other"

# Categories that are shown in the "Poketwo Commands" section instead of User / Admin.
POKETWO_CATEGORIES = {"Pokétwo Pings", "Pokétwo Helper", "Pokétwo Management", "Pokétwo Utils"}

# Categories shown in their own section where every subcommand is listed individually and
# admin-only ones are tagged "(admin)", just like the Poketwo section.
AI_CATEGORIES = {"AI"}

CATEGORY_OF = {name: title for title, names in HELP_CATEGORIES.items() for name in names}
CATEGORY_ORDER = {title: index for index, title in enumerate(HELP_CATEGORIES)}
COMMAND_ORDER = {name: index for index, name in enumerate(CATEGORY_OF)}


def access_level(cmd: commands.Command) -> int:
    """USER, ADMIN or OWNER. A subcommand is at least as restricted as its parent group."""
    level = USER
    for check in cmd.checks:
        kind = getattr(check, "__qualname__", "").split(".")[0]
        if kind in OWNER_CHECKS:
            level = OWNER
        elif kind in ADMIN_CHECKS:
            level = max(level, ADMIN)
    if isinstance(cmd.cog, OwnerCog):
        level = OWNER
    level = max(level, ACCESS_OVERRIDES.get(cmd.qualified_name, USER))
    if cmd.parent is not None:
        level = max(level, access_level(cmd.parent))
    return level


def _in_poketwo(cmd: commands.Command) -> bool:
    """True if the command belongs to one of the Poketwo categories."""
    root = cmd.root_parent or cmd
    return CATEGORY_OF.get(root.name) in POKETWO_CATEGORIES


def _in_ai(cmd: commands.Command) -> bool:
    """True if the command belongs to one of the AI categories."""
    root = cmd.root_parent or cmd
    return CATEGORY_OF.get(root.name) in AI_CATEGORIES


def section_of(cmd: commands.Command, level: int) -> int:
    """Overview section: owner commands always stay under Owner, Poketwo and AI commands go to their own section."""
    if level == OWNER:
        return OWNER
    if _in_ai(cmd):
        return AI
    return POKETWO if _in_poketwo(cmd) else level


def _tag(cmd: commands.Command, level: int, *, md: bool = False) -> str:
    """Access marker of a command: '(admin)' for admin-only Poketwo commands, else '', '*' or '***'."""
    if level == ADMIN and (_in_poketwo(cmd) or _in_ai(cmd)):
        return POKETWO_ADMIN_MARKER
    return _marker_md(level) if md else _marker(level)


def _legend(cmd: commands.Command, level: int) -> str:
    """Legend text matching `_tag`."""
    if level == ADMIN and (_in_poketwo(cmd) or _in_ai(cmd)):
        return POKETWO_ADMIN_LEGEND
    return LEGEND[level]


def _marker(level: int) -> str:
    """Plain-text marker for titles: '' / '*' / '***'."""
    return MARKERS[level]


def _marker_md(level: int) -> str:
    """Marker escaped for markdown descriptions."""
    return MARKERS[level].replace("*", "\\*")


def _is_hidden(cmd: commands.Command) -> bool:
    while cmd is not None:
        if cmd.hidden or cmd.qualified_name in HELP_HIDDEN:
            return True
        cmd = cmd.parent
    return False


def _pretty(name: str) -> str:
    """'LockUnlock' -> 'Lock Unlock'."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)


def _short(text: str | None, limit: int = 110) -> str:
    """First line of a description, shortened."""
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _chunk(lines: list[str], limit: int) -> list[str]:
    """Join lines with newlines into chunks of at most `limit` characters."""
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def _can_use(command: commands.Command, ctx: commands.Context) -> bool:
    try:
        return await command.can_run(ctx)
    except commands.CommandError:
        return False
    except Exception:
        return False


class Help(commands.Cog):
    """The help command."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # Make sure the built-in help is gone even if main.py still has it enabled.
        if self.bot.get_command("help") is not None and self.bot.help_command is not None:
            self.bot.help_command = None

    async def cog_command_error(self, ctx, error):
        if not await handle_common_error(ctx, error):
            raise error

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    def _find_cog(self, query: str) -> typing.Optional[commands.Cog]:
        wanted = re.sub(r"[\s_-]+", "", query).lower()
        for name, cog in self.bot.cogs.items():
            if re.sub(r"[\s_-]+", "", name).lower() == wanted:
                return cog
        return None

    def _all_names(self, owner: bool) -> list[str]:
        names: list[str] = []
        for cmd in self.bot.walk_commands():
            if _is_hidden(cmd) or (access_level(cmd) == OWNER and not owner):
                continue
            names.append(cmd.qualified_name)
            names.extend(f"{cmd.parent.qualified_name} {alias}" if cmd.parent else alias for alias in cmd.aliases)
        return names

    async def _visible(self, cmd: commands.Command, ctx: commands.Context, level: int, owner: bool) -> bool:
        """Owner commands only for owners; admin commands for everyone (or only for people who
        can run them); everything else only if the invoker can run it."""
        if level == OWNER:
            return owner
        if level == ADMIN and SHOW_ADMIN_COMMANDS_TO_EVERYONE:
            return True
        return await _can_use(cmd, ctx)

    @staticmethod
    def _error(text: str) -> discord.Embed:
        return discord.Embed(description=f"❌ {text}", color=discord.Color.red())

    # ------------------------------------------------------------------
    # Embeds
    # ------------------------------------------------------------------

    async def _overview_pages(self, ctx: commands.Context, owner: bool) -> list[discord.Embed]:
        prefix = ctx.clean_prefix
        grouped: dict[int, dict[str, list[commands.Command]]] = {section: {} for section in SECTION_ORDER}

        for cmd in self.bot.walk_commands():
            if _is_hidden(cmd):
                continue
            level = access_level(cmd)
            if _in_ai(cmd):
                if cmd.parent is None:
                    continue  # the AI group itself is replaced by its subcommands
            elif cmd.parent is not None and level == access_level(cmd.parent):
                continue  # already covered by its parent group
            if not await self._visible(cmd, ctx, level, owner):
                continue
            root = cmd.root_parent or cmd
            section = section_of(cmd, level)
            grouped[section].setdefault(CATEGORY_OF.get(root.name, OTHER_CATEGORY), []).append(cmd)

        blocks: list[tuple[str, list[str]]] = []
        for section in SECTION_ORDER:
            categories = grouped[section]
            if not categories:
                continue
            emoji, title = SECTIONS[section]
            lines = []
            for category in sorted(categories, key=lambda c: CATEGORY_ORDER.get(c, len(CATEGORY_ORDER))):
                cmds = sorted(
                    categories[category],
                    key=lambda c: (COMMAND_ORDER.get((c.root_parent or c).name, 10**6), access_level(c), c.qualified_name),
                )
                names = []
                for c in cmds:
                    entry = f"`{prefix}{c.qualified_name}`"
                    if section in (POKETWO, AI) and access_level(c) == ADMIN:
                        entry += f" {POKETWO_ADMIN_MARKER}"
                    names.append(entry)
                # Inside the Poketwo section the "Pokétwo " prefix is redundant.
                shown = category.removeprefix("Pokétwo ") if section == POKETWO else category
                lines.append(f"**{shown}:** {', '.join(names)}")
            blocks.append((f"## {emoji} {title}", lines))

        if not blocks:
            return [discord.Embed(title="📖 Help", description="No commands available.", color=BRAND_COLOR)]

        pages: list[list[str]] = []
        current: list[str] = []
        size = 0
        for header, lines in blocks:
            for index, line in enumerate(lines):
                extra = ([""] if current else []) + [header] if index == 0 else []
                cost = sum(len(item) + 1 for item in extra) + len(line) + 1
                if current and size + cost > PAGE_CHAR_LIMIT:
                    pages.append(current)
                    current, size = [], 0
                    extra = [header if index == 0 else f"{header} (continued)"]
                    cost = sum(len(item) + 1 for item in extra) + len(line) + 1
                current.extend(extra + [line])
                size += cost
        if current:
            pages.append(current)

        intro = f"Use `{prefix}help <command>` for details, arguments and examples."
        embeds = []
        for number, page in enumerate(pages, start=1):
            body = "\n".join(page)
            embed = discord.Embed(
                title="📖 Help",
                description=f"{intro}\n\n{body}" if number == 1 else body,
                color=BRAND_COLOR,
            )
            if len(pages) > 1:
                embed.set_footer(text=f"Page {number}/{len(pages)}")
            embeds.append(embed)
        return embeds

    async def _cog_pages(self, ctx: commands.Context, cog: commands.Cog, owner: bool) -> typing.Optional[list[discord.Embed]]:
        """None means "stay silent": the cog only holds owner commands and the invoker isn't an owner."""
        prefix = ctx.clean_prefix
        lines = []
        legends: dict[str, None] = {}
        hidden_owner_commands = False
        for cmd in sorted(cog.get_commands(), key=lambda c: c.name):
            if _is_hidden(cmd):
                continue
            level = access_level(cmd)
            if level == OWNER and not owner:
                hidden_owner_commands = True
                continue
            if not await self._visible(cmd, ctx, level, owner):
                continue
            aliases = f" (`{'`, `'.join(cmd.aliases)}`)" if cmd.aliases else ""
            marker = f" {_tag(cmd, level, md=True)}" if level != USER else ""
            if level != USER:
                legends[_legend(cmd, level)] = None
            summary = _short(cmd.description or cmd.short_doc)
            lines.append(f"**{prefix}{cmd.name}**{marker}{aliases}" + (f" — {summary}" if summary else ""))

        title = f"📖 {_pretty(cog.qualified_name)}"
        if not lines:
            if hidden_owner_commands:
                return None
            return [discord.Embed(title=title, description="No commands available.", color=BRAND_COLOR)]
        legend = " • ".join(legends)
        footer = f"Use {prefix}help <command> for details."
        return self._text_pages(title, lines, footer=f"{footer} • {legend}" if legend else footer)

    @staticmethod
    def _text_pages(title: str, lines: list[str], *, footer: str | None = None, intro: str = "") -> list[discord.Embed]:
        chunks = _chunk(lines, PAGE_CHAR_LIMIT)
        pages = []
        for number, chunk in enumerate(chunks, start=1):
            embed = discord.Embed(title=title, description=f"{intro}\n\n{chunk}" if intro and number == 1 else chunk, color=BRAND_COLOR)
            notes = [footer] if footer else []
            if len(chunks) > 1:
                notes.append(f"Page {number}/{len(chunks)}")
            if notes:
                embed.set_footer(text=" • ".join(notes))
            pages.append(embed)
        return pages

    async def _command_pages(self, ctx: commands.Context, cmd: commands.Command, owner: bool) -> list[discord.Embed]:
        prefix = ctx.clean_prefix
        descriptions = _param_descriptions(cmd)
        level = access_level(cmd)

        tag = _tag(cmd, level)
        tag = f" {tag}" if tag == POKETWO_ADMIN_MARKER else tag
        embed = discord.Embed(title=f"{prefix}{cmd.qualified_name}{tag}", color=BRAND_COLOR)
        summary = (cmd.description or cmd.help or "").strip()
        body = [summary] if summary else []
        if not await _can_use(cmd, ctx):
            body.append("🔒 You can't use this command.")
        embed.description = "\n".join(body) or None

        embed.add_field(name="Usage", value=f"`{_usage_line(cmd, prefix)}`", inline=False)

        if cmd.aliases:
            parent = f"{cmd.parent.qualified_name} " if cmd.parent else ""
            embed.add_field(
                name="Aliases",
                value=" ".join(f"`{prefix}{parent}{alias}`" for alias in cmd.aliases),
                inline=False,
            )

        argument_lines = []
        for name, param in cmd.clean_params.items():
            shown = getattr(param, "displayed_name", None) or name
            tag = "required" if param.required else "optional"
            text = descriptions.get(name, "No description.")
            argument_lines.append(f"`{shown}` *({tag})* — {text}")
        if argument_lines:
            for index, chunk in enumerate(_chunk(argument_lines, FIELD_CHAR_LIMIT)):
                embed.add_field(name="Arguments" if index == 0 else "Arguments (continued)", value=chunk, inline=False)

        examples = list(USAGE_EXAMPLES.get(cmd.qualified_name, []))
        if examples:
            text = "\n".join(f"`{example.format(p=prefix)}`" for example in examples)
            embed.add_field(name="Examples", value=text[:1024], inline=False)

        subcommands = []
        used_legends: dict[str, None] = {}
        if isinstance(cmd, commands.Group):
            for sub in sorted(cmd.commands, key=lambda c: c.name):
                if _is_hidden(sub):
                    continue
                sub_level = access_level(sub)
                if not await self._visible(sub, ctx, sub_level, owner):
                    continue
                marker = ""
                if sub_level != level:
                    marker = f" {_tag(sub, sub_level, md=True)}"
                    used_legends[_legend(sub, sub_level)] = None
                subs_summary = _short(sub.description or sub.short_doc)
                subcommands.append(f"`{_usage_line(sub, prefix)}`{marker}" + (f"\n{subs_summary}" if subs_summary else ""))

        notes = []
        if argument_lines or not subcommands:
            notes.append("<required> [optional]")
        if level != USER:
            notes.append(_legend(cmd, level))
        if notes:
            embed.set_footer(text=" • ".join(notes))

        if not subcommands:
            return [embed]

        # Subcommands can be many: first page = the command, following pages = its subcommands.
        sub_footer = " • ".join(
            [f"Use {prefix}help {cmd.qualified_name} <subcommand> for details.", *used_legends]
        )
        pages = [embed]
        pages.extend(self._text_pages(f"{prefix}{cmd.qualified_name} — subcommands", subcommands, footer=sub_footer))
        for number, page in enumerate(pages, start=1):
            existing = page.footer.text if page.footer and page.footer.text else ""
            page.set_footer(text=f"{existing} • Page {number}/{len(pages)}" if existing else f"Page {number}/{len(pages)}")
        return pages

    # ------------------------------------------------------------------
    # Command
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="help", description="Shows all commands, or details and arguments of one command.")
    @app_commands.describe(query="A command (e.g. ccat, set lockdelay) or a cog name. Leave blank to list every command.")
    async def help(
        self,
        ctx: commands.Context,
        *,
        query: typing.Optional[str] = commands.parameter(
            default=None,
            description="A command (e.g. ccat, set lockdelay) or a cog name. Leave blank to list every command.",
        ),
    ):
        query = (query or "").strip().removeprefix(ctx.clean_prefix).strip()
        owner = await self.bot.is_owner(ctx.author)

        if not query:
            pages = await self._overview_pages(ctx, owner)
        else:
            command = self.bot.get_command(query)
            if command is not None and not _is_hidden(command):
                if access_level(command) == OWNER and not owner:
                    return
                pages = await self._command_pages(ctx, command, owner)
            else:
                cog = self._find_cog(query)
                if cog is not None:
                    pages = await self._cog_pages(ctx, cog, owner)
                    if pages is None:
                        return
                else:
                    close = difflib.get_close_matches(query.lower(), self._all_names(owner), n=3, cutoff=0.6)
                    hint = f" Did you mean {', '.join(f'`{ctx.clean_prefix}{name}`' for name in close)}?" if close else ""
                    return await ctx.reply(
                        embed=self._error(f"No command or cog called `{query}`.{hint}"),
                        mention_author=False,
                    )

        if len(pages) == 1:
            return await ctx.reply(embed=pages[0], mention_author=False)

        view = PaginatorView(pages, user_id=ctx.author.id)
        view.message = await ctx.reply(embed=pages[0], view=view, mention_author=False)


async def setup(bot):
    await bot.add_cog(Help(bot))