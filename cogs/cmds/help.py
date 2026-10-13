"""Custom help command.

Nothing here has to be edited when commands change: ``.help`` reads everything straight
from the loaded cogs.

* Command description    -> ``description="..."`` on the command decorator.
* Argument descriptions  -> ``commands.parameter(description="...")`` in the function signature
                            (or ``@app_commands.describe(...)``). For ``*args`` style commands that
                            can't use ``commands.parameter``: ``extras={"args": {"name": "..."}}``.
* Examples               -> ``USAGE_EXAMPLES`` in views/embeds.py (optional).

Usage:  .help                 every command you can use, grouped by cog
        .help <command>       usage, aliases, arguments (+ descriptions), subcommands, examples
        .help <cog>           the commands of one cog
        .help <group> <sub>   e.g. .help set lockdelay
"""
import difflib
import re
import typing

import discord
from discord.ext import commands

from views.embeds import BRAND_COLOR, USAGE_EXAMPLES, _param_descriptions, _usage_line, handle_common_error
from views.loading import loading
from views.navigate import PaginatorView

PAGE_CHAR_LIMIT = 3800
FIELD_CHAR_LIMIT = 1000
FIELDS_PER_PAGE = 6


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


def _chunk_words(words: list[str], limit: int) -> list[str]:
    """Join words with spaces into chunks of at most `limit` characters."""
    chunks: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if len(candidate) > limit and current:
            chunks.append(current)
            current = word
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

    def _all_names(self) -> list[str]:
        names: list[str] = []
        for cmd in self.bot.walk_commands():
            if cmd.hidden:
                continue
            names.append(cmd.qualified_name)
            names.extend(f"{cmd.parent.qualified_name} {alias}" if cmd.parent else alias for alias in cmd.aliases)
        return names

    @staticmethod
    def _error(text: str) -> discord.Embed:
        return discord.Embed(description=f"❌ {text}", color=discord.Color.red())

    # ------------------------------------------------------------------
    # Embeds
    # ------------------------------------------------------------------

    async def _overview_pages(self, ctx: commands.Context) -> list[discord.Embed]:
        prefix = ctx.clean_prefix
        sections: dict[str, list[str]] = {}

        for cmd in sorted(self.bot.commands, key=lambda c: c.name):
            if cmd.hidden or not await _can_use(cmd, ctx):
                continue
            cog_name = _pretty(cmd.cog.qualified_name) if cmd.cog else "Other"
            sections.setdefault(cog_name, []).append(f"`{prefix}{cmd.name}`")

        if not sections:
            return [discord.Embed(title="Help", description="No commands available.", color=BRAND_COLOR)]

        fields: list[tuple[str, str]] = []
        for cog_name in sorted(sections):
            parts = _chunk_words(sections[cog_name], FIELD_CHAR_LIMIT)
            for index, part in enumerate(parts):
                fields.append((cog_name if index == 0 else f"{cog_name} (continued)", part))

        groups: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] = []
        size = 0
        for field in fields:
            field_size = len(field[0]) + len(field[1])
            if current and (len(current) >= FIELDS_PER_PAGE or size + field_size > PAGE_CHAR_LIMIT):
                groups.append(current)
                current, size = [], 0
            current.append(field)
            size += field_size
        if current:
            groups.append(current)

        pages = []
        for number, group in enumerate(groups, start=1):
            embed = discord.Embed(
                title="📖 Help",
                description=f"Use `{prefix}help <command>` for details, arguments and examples.",
                color=BRAND_COLOR,
            )
            for name, value in group:
                embed.add_field(name=name, value=value, inline=False)
            if len(groups) > 1:
                embed.set_footer(text=f"Page {number}/{len(groups)}")
            pages.append(embed)
        return pages

    async def _cog_pages(self, ctx: commands.Context, cog: commands.Cog) -> list[discord.Embed]:
        prefix = ctx.clean_prefix
        lines = []
        for cmd in sorted(cog.get_commands(), key=lambda c: c.name):
            if cmd.hidden or not await _can_use(cmd, ctx):
                continue
            aliases = f" (`{'`, `'.join(cmd.aliases)}`)" if cmd.aliases else ""
            summary = _short(cmd.description or cmd.short_doc)
            lines.append(f"**{prefix}{cmd.name}**{aliases}" + (f" — {summary}" if summary else ""))

        title = f"📖 {_pretty(cog.qualified_name)}"
        if not lines:
            return [discord.Embed(title=title, description="No commands available.", color=BRAND_COLOR)]
        return self._text_pages(title, lines, footer=f"Use {prefix}help <command> for details.")

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

    async def _command_pages(self, ctx: commands.Context, cmd: commands.Command) -> list[discord.Embed]:
        prefix = ctx.clean_prefix
        descriptions = _param_descriptions(cmd)

        embed = discord.Embed(title=f"{prefix}{cmd.qualified_name}", color=BRAND_COLOR)
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
        if isinstance(cmd, commands.Group):
            for sub in sorted(cmd.commands, key=lambda c: c.name):
                if sub.hidden or not await _can_use(sub, ctx):
                    continue
                subs_summary = _short(sub.description or sub.short_doc)
                subcommands.append(f"`{_usage_line(sub, prefix)}`" + (f"\n{subs_summary}" if subs_summary else ""))

        if argument_lines or not subcommands:
            embed.set_footer(text="<required> [optional]")

        if not subcommands:
            return [embed]

        # Subcommands can be many: first page = the command, following pages = its subcommands.
        pages = [embed]
        pages.extend(
            self._text_pages(
                f"{prefix}{cmd.qualified_name} — subcommands",
                subcommands,
                footer=f"Use {prefix}help {cmd.qualified_name} <subcommand> for details.",
            )
        )
        for number, page in enumerate(pages, start=1):
            existing = page.footer.text if page.footer and page.footer.text else ""
            page.set_footer(text=f"{existing} • Page {number}/{len(pages)}" if existing else f"Page {number}/{len(pages)}")
        return pages

    # ------------------------------------------------------------------
    # Command
    # ------------------------------------------------------------------

    @commands.command(name="help", description="Shows all commands, or details and arguments of one command.")
    @loading
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

        if not query:
            pages = await self._overview_pages(ctx)
        else:
            command = self.bot.get_command(query)
            if command is not None and not command.hidden:
                pages = await self._command_pages(ctx, command)
            else:
                cog = self._find_cog(query)
                if cog is not None:
                    pages = await self._cog_pages(ctx, cog)
                else:
                    close = difflib.get_close_matches(query.lower(), self._all_names(), n=3, cutoff=0.6)
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
