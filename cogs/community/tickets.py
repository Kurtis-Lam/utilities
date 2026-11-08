from __future__ import annotations

import asyncio
import io
import re
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from views.common_views import ConfirmLayout, error_embed, make_embed, success_embed
from views.embeds import handle_command_error
from views.ticket_transcript import build_ticket_transcript
from views.setup_wizard import SetupWizard, Step, start_wizard
from views.tickets_views import (
    OTHER_KEY,
    TicketControlView,
    TicketDashboardView,
    TicketOptionSelectView,
    TicketOtherReasonModal,
    TicketPanelView,
    other_option,
)


def _defaults(guild_id: int) -> dict:
    return {
        "_id": str(guild_id),
        "panel_title": "Need help?",
        "panel_description": "Open a private ticket and our team will reply soon.",
        "panel_button_label": "Create Ticket",
        "panel_button_emoji": "🎫",
        "panel_options_mode": "create",
        "allow_other_option": True,
        "choose_option_message": "Choose the category that best matches your request.",
        "default_category_id": None,
        "staff_role_ids": [],
        "ping_role_ids": [],
        "log_channel_id": None,
        "max_open_tickets": 3,
        "channel_name_template": "ticket-{username}-{number}",
        "open_message": "Hello {user}, the team will be with you shortly.\n**Reason:** {reason}",
        "close_message": "Ticket closed by {user}.",
        "reopen_message": "Ticket reopened by {user}.",
        "claim_message": "Ticket claimed by {user}.",
        "closed_topic_suffix": "(closed)",
        "allow_user_close": True,
        "allow_claim": True,
        "transcripts_enabled": True,
        "delete_after_close_minutes": 0,
        "options": [
            {"key": "general", "label": "General Support", "description": "Ask a general question or request help.", "emoji": "🎫", "category_id": None, "ping_role_ids": [], "open_message": ""}
        ],
        "panels": [],
    }


def _format_ticket(template: str, *, user: discord.abc.User, guild: discord.Guild, channel=None,
                   reason: str = "", option: str = "", staff: str = "", ticket_id: int | None = None) -> str:
    values = {
        "{user}": user.mention,
        "{username}": user.name,
        "{display_name}": getattr(user, "display_name", user.name),
        "{user_id}": str(user.id),
        "{guild}": guild.name,
        "{guild_id}": str(guild.id),
        "{ticket}": channel.mention if channel else "ticket",
        "{ticket_id}": str(ticket_id or (channel.id if channel else "")),
        "{reason}": reason,
        "{option}": option,
        "{staff}": staff,
    }
    for key, value in values.items():
        template = template.replace(key, value)
    return template


def _channel_slug(template: str, user: discord.abc.User, number: int, option_label: str) -> str:
    values = {
        "{username}": user.name.lower(),
        "{display_name}": getattr(user, "display_name", user.name).lower(),
        "{user_id}": str(user.id),
        "{number}": f"{number:04d}",
        "{option}": option_label.lower(),
    }
    name = template
    for key, value in values.items():
        name = name.replace(key, value)
    name = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return (name or f"ticket-{user.id}")[:95]


def _option_lines(options: list[dict]) -> str:
    lines = []
    for option in options:
        label = str(option.get("label", "Ticket"))
        emoji = option.get("emoji") or ""
        description = str(option.get("description") or "")
        line = f"{emoji} {label}".strip()
        lines.append(f"{line} | {description}" if description else line)
    return "\n".join(lines)


def _parse_option_lines(text: str) -> list[dict]:
    """``emoji Label | description`` per line -> option dicts (emoji and description are optional)."""
    parsed = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        head, _, description = line.partition("|")
        head, description = head.strip(), description.strip()
        emoji = None
        first, _, rest = head.partition(" ")
        if rest.strip() and (re.fullmatch(r"<a?:\w{2,32}:\d{15,25}>", first) or not any(c.isascii() and c.isalnum() for c in first)):
            emoji, head = first, rest.strip()
        if not head:
            continue
        parsed.append({"label": head[:80], "description": description[:100], "emoji": emoji})
    return parsed


def _validate_options(text: str) -> str | None:
    parsed = _parse_option_lines(text)
    if not parsed:
        return "Add at least one ticket type, one per line."
    if len(parsed) > 24:
        return "Discord supports up to 24 ticket types."
    return None


def _option_key(label: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:30] or "option"
    key, n = base, 2
    while key in taken or key == OTHER_KEY:
        key, n = f"{base}_{n}", n + 1
    taken.add(key)
    return key


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._delete_tasks: dict[int, asyncio.Task] = {}
        self._background_tasks: set[asyncio.Task] = set()

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        await handle_command_error(ctx, error)

    async def _require_db(self, ctx: commands.Context) -> bool:
        if self._db_ready():
            return True
        await ctx.reply(embed=error_embed("MongoDB is not configured; ticket settings are unavailable."), mention_author=False)
        return False

    @commands.hybrid_group(
        name="tickets",
        aliases=["ticket"],
        invoke_without_command=True,
        fallback="panel",
        description="Configure and publish support ticket panels.",
    )
    @app_commands.default_permissions(administrator=True)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def tickets_command(self, ctx: commands.Context):
        if not await self._require_db(ctx):
            return
        config = await self.get_config(ctx.guild.id)
        view = TicketDashboardView(self, config, ctx.guild, ctx.author.id)
        # EmbedLayout renders its embed inside a Components V2 container; do not pass embed= separately.
        view.message = await ctx.reply(view=view, mention_author=False)

    async def publish_panel(self, guild: discord.Guild, channel: discord.TextChannel, config: dict) -> discord.Message:
        """Post a ticket panel built from the saved settings and remember it so its buttons keep working."""
        title = str(config.get("panel_title", "Need help?"))
        description = str(config.get("panel_description", ""))
        label = str(config.get("panel_button_label", "Create Ticket"))
        emoji = config.get("panel_button_emoji", "🎫") or None
        options_mode = config.get("panel_options_mode", "create")
        options = list(config.get("options", []))
        allow_other = bool(config.get("allow_other_option", True))
        view = TicketPanelView(
            self, label, emoji, make_embed(title, description),
            options_mode=options_mode, options=options, allow_other_option=allow_other,
            thumbnail=guild.icon.url if guild.icon else None,
        )
        message = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        panels = list(config.get("panels", []))
        panels.append({
            "channel_id": channel.id,
            "message_id": message.id,
            "title": title,
            "description": description,
            "button_label": label,
            "button_emoji": emoji,
            "options_mode": options_mode,
            "options": options,
            "allow_other_option": allow_other,
            "created_at": int(time.time()),
        })
        await self.save_config_fields(guild.id, {"panels": panels})
        return message

    def _setup_steps(self) -> list[Step]:
        return [
            Step(
                key="panel_title",
                title="Panel title",
                prompt="The short heading members see on the ticket panel.",
                kind="text",
                max_length=100,
                placeholder="Need help?",
            ),
            Step(
                key="panel_description",
                title="Panel text",
                prompt="One or two short lines explaining what tickets are for.",
                kind="paragraph",
                max_length=300,
                placeholder="Open a private ticket and our team will reply soon.",
                hint="Short text looks best on the panel.",
            ),
            Step(
                key="options_text",
                title="Ticket types",
                prompt="What can members open a ticket for? One type per line.",
                kind="paragraph",
                max_length=1500,
                placeholder="🎫 General Support | Ask a question",
                hint="Format: emoji Label | short description (emoji and description are optional). Up to 24 types.",
                validate=_validate_options,
                show=lambda v: "*Not set*" if not v else "\n".join(f"• {line}" for line in str(v).splitlines() if line.strip())[:300],
            ),
            Step(
                key="panel_options_mode",
                title="How members pick a type",
                prompt="One **Create Ticket** button that opens a private picker, or a separate button for each type directly on the panel?",
                kind="choice",
                choices=[("create", "One button"), ("inline", "Button per type")],
            ),
            Step(
                key="panel_button_label",
                title="Button label",
                prompt="The text on the Create Ticket button.",
                kind="text",
                max_length=40,
                placeholder="Create Ticket",
                when=lambda v: v.get("panel_options_mode") != "inline",
            ),
            Step(
                key="default_category_id",
                title="Ticket category",
                prompt="New tickets are created inside this category.",
                kind="channel",
                optional=True,
                channel_types=[discord.ChannelType.category],
                placeholder="Choose a category",
                hint="Skip to create tickets at the top of the channel list.",
            ),
            Step(
                key="staff_role_ids",
                title="Support roles",
                prompt="Which roles can see and answer tickets? They are pinged when a ticket opens.",
                kind="roles",
                optional=True,
                maximum=10,
            ),
            Step(
                key="log_channel_id",
                title="Log channel",
                prompt="Where should closed-ticket transcripts and logs go?",
                kind="channel",
                optional=True,
            ),
            Step(
                key="max_open_tickets",
                title="Open tickets per member",
                prompt="How many tickets can one member have open at once? Use 0 for no limit.",
                kind="number",
                minimum=0,
                maximum=25,
            ),
            Step(
                key="transcripts_enabled",
                title="Transcripts",
                prompt="Save a transcript when a ticket is closed?",
                kind="toggle",
            ),
            Step(
                key="panel_channel",
                title="Post the panel",
                prompt="Pick a channel and I'll post the panel there when you save.",
                kind="channel",
                optional=True,
                hint="Skip to save the settings only. You can post a panel later with the dashboard.",
            ),
        ]

    async def _finish_setup(self, interaction: discord.Interaction, values: dict) -> str:
        guild = interaction.guild
        config = await self.get_config(guild.id)

        category_id = values.get("default_category_id")
        if category_id and not isinstance(guild.get_channel(int(category_id)), discord.CategoryChannel):
            raise ValueError("The selected ticket category no longer exists.")
        staff_ids = [int(r) for r in (values.get("staff_role_ids") or [])]
        if any(guild.get_role(r) is None or guild.get_role(r).is_default() for r in staff_ids):
            raise ValueError("One of the support roles can't be used (it's @everyone or no longer exists).")

        parsed = _parse_option_lines(values.get("options_text") or "")
        limit = 24 if config.get("allow_other_option", True) else 25
        if not parsed or len(parsed) > limit:
            raise ValueError(f"Add between 1 and {limit} ticket types.")
        existing = {str(o.get("label", "")).casefold(): o for o in config.get("options", [])}
        taken = {str(o.get("key")) for o in config.get("options", [])}
        options = []
        for item in parsed:
            old = existing.get(item["label"].casefold())
            options.append({
                "key": old["key"] if old else _option_key(item["label"], taken),
                "label": item["label"],
                "description": item["description"],
                "emoji": item["emoji"],
                "category_id": old.get("category_id") if old else None,
                "ping_role_ids": old.get("ping_role_ids", []) if old else [],
                "open_message": old.get("open_message", "") if old else "",
            })

        mode = values.get("panel_options_mode") or "create"
        if mode == "inline" and len(options) + (1 if config.get("allow_other_option", True) else 0) > 25:
            raise ValueError("A panel with a button per type holds up to 25 buttons. Remove a type or turn off the Other option.")

        fields = {
            "panel_title": values.get("panel_title") or config.get("panel_title"),
            "panel_description": values.get("panel_description") or config.get("panel_description"),
            "options": options,
            "panel_options_mode": mode,
            "panel_button_label": values.get("panel_button_label") or config.get("panel_button_label", "Create Ticket"),
            "default_category_id": int(category_id) if category_id else None,
            "staff_role_ids": staff_ids,
            "log_channel_id": int(values["log_channel_id"]) if values.get("log_channel_id") else None,
            "max_open_tickets": int(values.get("max_open_tickets") or 0),
            "transcripts_enabled": bool(values.get("transcripts_enabled", True)),
        }
        await self.save_config_fields(guild.id, fields)
        config.update(fields)

        lines = [
            f"**Ticket types:** {len(options)}",
            f"**Category:** {f'<#{category_id}>' if category_id else 'none'}",
            f"**Support roles:** {' '.join(f'<@&{r}>' for r in staff_ids) or 'none'}",
            f"**Log channel:** {'<#' + str(fields['log_channel_id']) + '>' if fields['log_channel_id'] else 'none'}",
            f"**Open tickets per member:** {fields['max_open_tickets'] or 'unlimited'}",
        ]
        panel_channel = guild.get_channel(int(values["panel_channel"])) if values.get("panel_channel") else None
        if panel_channel is not None:
            if not isinstance(panel_channel, discord.TextChannel):
                raise ValueError("The panel channel must be a text channel.")
            message = await self.publish_panel(guild, panel_channel, config)
            lines.append(f"**Panel:** posted in {panel_channel.mention} ([jump]({message.jump_url}))")
        else:
            lines.append("**Panel:** not posted this time")
        lines.append("-# Panels that are already posted keep their old text. Post a new one to use the changes.")
        return "\n".join(lines)

    @tickets_command.command(name="setup", description="Guided ticket setup. Run it again any time to change settings.")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def tickets_setup(self, ctx: commands.Context):
        if not await self._require_db(ctx):
            return
        config = await self.get_config(ctx.guild.id)
        initial = {
            "panel_title": config.get("panel_title", "Need help?"),
            "panel_description": config.get("panel_description", ""),
            "options_text": _option_lines(config.get("options", [])),
            "panel_options_mode": config.get("panel_options_mode", "create"),
            "panel_button_label": config.get("panel_button_label", "Create Ticket"),
            "default_category_id": int(config["default_category_id"]) if config.get("default_category_id") else None,
            "staff_role_ids": [int(r) for r in config.get("staff_role_ids", []) if str(r).isdigit()],
            "log_channel_id": int(config["log_channel_id"]) if config.get("log_channel_id") else None,
            "max_open_tickets": int(config.get("max_open_tickets", 3) or 0),
            "transcripts_enabled": bool(config.get("transcripts_enabled", True)),
            "panel_channel": None,
        }
        wizard = SetupWizard(
            title="🎫 Ticket Setup",
            steps=self._setup_steps(),
            initial=initial,
            on_finish=self._finish_setup,
            author_id=ctx.author.id,
            guild=ctx.guild,
        )
        await start_wizard(ctx, wizard)

    @commands.command(name="ticketsetup", hidden=True)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def ticketsetup_alias(self, ctx: commands.Context):
        await self.tickets_setup.callback(self, ctx)

    @property
    def config_collection(self):
        return self.bot.mongo_client["utilities"]["tickets_config"]

    @property
    def tickets_collection(self):
        return self.bot.mongo_client["utilities"]["tickets"]

    async def cog_load(self):
        self.bot.add_view(TicketPanelView(self))
        self.bot.add_view(TicketControlView(self, mode="all"))
        if self.bot.mongo_client is None:
            print("Tickets Cog: MongoDB is not configured; ticket creation will be unavailable.")
            return
        try:
            await self.tickets_collection.create_index([("guild_id", 1), ("opener_id", 1), ("closed", 1)])
            await self.tickets_collection.create_index([("closed", 1), ("closed_at", 1)])
            panel_configs = await self.config_collection.find({"panels.0": {"$exists": True}}).to_list(length=None)
            for config in panel_configs:
                for panel in config.get("panels", []):
                    message_id = int(panel.get("message_id", 0) or 0)
                    if not message_id:
                        continue
                    options_mode = panel.get("options_mode", config.get("panel_options_mode", "create"))
                    panel_view = TicketPanelView(
                        self,
                        panel.get("button_label", "Create Ticket"),
                        panel.get("button_emoji", "🎫"),
                        make_embed(panel.get("title", config.get("panel_title", "Need help?")), panel.get("description", config.get("panel_description", ""))),
                        options_mode=options_mode,
                        options=panel.get("options", config.get("options", [])),
                        allow_other_option=panel.get("allow_other_option", config.get("allow_other_option", True)),
                    )
                    self.bot.add_view(panel_view, message_id=message_id)
        except Exception as exc:
            print(f"Tickets Cog: could not initialise indexes or persistent panels: {exc}")

    async def cog_unload(self):
        for task in self._delete_tasks.values():
            task.cancel()
        self._delete_tasks.clear()
        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()

    def _edit_channel_soon(self, channel: discord.TextChannel, **kwargs):
        """Rename/retopic a channel without blocking ticket messages.

        Discord limits channel renames to 2 per 10 minutes, so a close/reopen/close sequence
        would otherwise stall for minutes before the next ticket message could be sent.
        """
        async def runner():
            try:
                await asyncio.wait_for(channel.edit(**kwargs), timeout=20)
            except (asyncio.TimeoutError, discord.HTTPException):
                pass

        task = asyncio.create_task(runner())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def get_config(self, guild_id: int) -> dict:
        if not self._db_ready():
            raise RuntimeError("MongoDB is not configured; ticket settings are unavailable.")
        defaults = _defaults(guild_id)
        config = await self.config_collection.find_one({"_id": str(guild_id)})
        if config is None:
            await self.config_collection.update_one({"_id": str(guild_id)}, {"$setOnInsert": defaults}, upsert=True)
            return defaults
        missing = {key: value for key, value in defaults.items() if key not in config}
        if missing:
            await self.config_collection.update_one({"_id": str(guild_id)}, {"$set": missing}, upsert=True)
            config.update(missing)
        return config

    async def save_config_fields(self, guild_id: int, values: dict):
        if not self._db_ready():
            raise RuntimeError("MongoDB is not configured; ticket settings cannot be saved.")
        await self.config_collection.update_one({"_id": str(guild_id)}, {"$set": values}, upsert=True)

    def _db_ready(self) -> bool:
        return self.bot.mongo_client is not None

    async def handle_inline_option(self, interaction: discord.Interaction, option_key: str):
        if interaction.guild is None or interaction.message is None:
            return await interaction.response.send_message(embed=error_embed("Use this panel inside the server."), ephemeral=True)
        if not self._db_ready():
            return await interaction.response.send_message(embed=error_embed("Ticket storage is unavailable. Please contact an administrator."), ephemeral=True)
        config = await self.get_config(interaction.guild.id)
        panel = next((p for p in config.get("panels", []) if int(p.get("message_id", 0)) == interaction.message.id), None)
        if panel is None:
            return await interaction.response.send_message(embed=error_embed("This ticket panel is no longer configured. Ask an administrator to repost it."), ephemeral=True)
        if option_key == OTHER_KEY:
            if not config.get("allow_other_option", True):
                return await interaction.response.send_message(embed=error_embed("The Other option is currently disabled."), ephemeral=True)
            selector = TicketOptionSelectView(self, config, panel, interaction.user.id)
            return await interaction.response.send_modal(TicketOtherReasonModal(selector))
        await self.create_ticket(interaction, option_key, config, panel)

    async def create_ticket(self, interaction: discord.Interaction, option_key: str, config: dict, panel: dict, reason: str | None = None):
        if interaction.guild is None or interaction.user.bot:
            return await interaction.response.send_message(embed=error_embed("Tickets can only be created by server members."), ephemeral=True)
        if not self._db_ready():
            return await interaction.response.send_message(embed=error_embed("MongoDB is not configured; ticket creation is unavailable."), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        config = await self.get_config(guild.id)
        if option_key == OTHER_KEY:
            if not config.get("allow_other_option", True):
                return await interaction.followup.send(embed=error_embed("The Other option is currently disabled. Open the panel again."), ephemeral=True)
            option = other_option()
            reason_text = (reason or "").strip() or "No reason provided"
        else:
            option = next((item for item in config.get("options", []) if item.get("key") == option_key), None)
            if option is None:
                return await interaction.followup.send(embed=error_embed("That ticket option was removed. Open the panel again."), ephemeral=True)
            reason_text = str(option.get("label", "Ticket"))
        max_open = int(config.get("max_open_tickets", 3) or 0)
        if max_open > 0:
            count = await self.tickets_collection.count_documents({"guild_id": guild.id, "opener_id": interaction.user.id, "closed": False})
            if count >= max_open:
                return await interaction.followup.send(embed=error_embed(f"You already have the maximum of `{max_open}` open ticket(s)."), ephemeral=True)

        category_id = option.get("category_id") or config.get("default_category_id")
        category = guild.get_channel(int(category_id)) if category_id else None
        if category_id and not isinstance(category, discord.CategoryChannel):
            return await interaction.followup.send(embed=error_embed("The configured ticket category no longer exists. Ask an administrator to update it."), ephemeral=True)

        support_ids = [int(x) for x in config.get("staff_role_ids", []) if str(x).isdigit()]
        ping_ids = list(dict.fromkeys(support_ids + [int(x) for x in config.get("ping_role_ids", []) if str(x).isdigit()] + [int(x) for x in option.get("ping_role_ids", []) if str(x).isdigit()]))
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
        }
        for role_id in support_ids:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True, attach_files=True, embed_links=True)
        bot_member = guild.get_member(self.bot.user.id) if self.bot.user else None
        if bot_member is None and self.bot.user:
            try:
                bot_member = await guild.fetch_member(self.bot.user.id)
            except discord.HTTPException:
                bot_member = None
        if bot_member is not None:
            overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_messages=True, attach_files=True, embed_links=True)

        ticket_number = await self.tickets_collection.count_documents({"guild_id": guild.id}) + 1
        channel_name = _channel_slug(str(config.get("channel_name_template", "ticket-{username}-{number}")), interaction.user, ticket_number, str(option.get("label", "ticket")))
        topic = f"Ticket creator ID: {interaction.user.id} | Option: {option.get('label', 'Ticket')} | Opened: {int(time.time())}"
        try:
            channel = await guild.create_text_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
                topic=topic[:1024],
                reason=f"Ticket opened by {interaction.user} ({interaction.user.id})",
            )
        except discord.HTTPException as exc:
            return await interaction.followup.send(embed=error_embed(f"Could not create the ticket channel: {exc}"), ephemeral=True)

        record = {
            "_id": channel.id,
            "guild_id": guild.id,
            "channel_id": channel.id,
            "opener_id": interaction.user.id,
            "opener_name": str(interaction.user),
            "option_key": option_key,
            "option_label": str(option.get("label", "Ticket")),
            "ticket_number": ticket_number,
            "reason": reason_text,
            "panel_message_id": int(panel.get("message_id", 0)),
            "opened_at": int(time.time()),
            "closed": False,
            "claimed_by": None,
            "control_message_id": None,
        }
        try:
            await self.tickets_collection.insert_one(record)
        except Exception as exc:
            try:
                await channel.delete(reason="Could not persist ticket record")
            except discord.HTTPException:
                pass
            print(f"Tickets Cog: could not save ticket {channel.id}: {exc}")
            return await interaction.followup.send(embed=error_embed("Ticket creation failed while saving its record. Please try again."), ephemeral=True)

        opening_template = str(option.get("open_message") or config.get("open_message", "Hello {user}, the team will be with you shortly.\n**Reason:** {reason}"))
        description = _format_ticket(opening_template, user=interaction.user, guild=guild, channel=channel, reason=reason_text, option=str(option.get("label", "Ticket")), ticket_id=channel.id)
        embed = make_embed(f"{option.get('emoji') or '🎫'} {option.get('label', 'Ticket')}", description)
        embed.add_field(name="Created by", value=interaction.user.mention, inline=True)
        embed.add_field(name="Ticket ID", value=f"`{channel.id}`", inline=True)
        if option_key == OTHER_KEY and "{reason}" not in opening_template:
            embed.add_field(name="Reason", value=reason_text[:1024], inline=False)
        role_mentions = [f"<@&{role_id}>" for role_id in ping_ids if guild.get_role(role_id)]
        allowed_mentions = discord.AllowedMentions(users=[interaction.user], roles=[discord.Object(id=role_id) for role_id in ping_ids if guild.get_role(role_id)], everyone=False, replied_user=False)
        try:
            lead = " ".join([interaction.user.mention, *role_mentions])
            control_message = await channel.send(
                view=TicketControlView(self, embed=embed, mode="open", lead=lead),
                allowed_mentions=allowed_mentions,
            )
            await self.tickets_collection.update_one(
                {"_id": channel.id},
                {"$set": {"control_message_id": control_message.id, "control_embed": embed.to_dict(), "control_lead": lead}},
            )
        except discord.HTTPException as exc:
            print(f"Tickets Cog: ticket {channel.id} created but opening message failed: {exc}")
        await interaction.followup.send(
            embed=success_embed(f"Your ticket has been created: {channel.mention}"),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def _is_staff(self, member: discord.Member, config: dict) -> bool:
        if member.guild_permissions.administrator or member.guild_permissions.manage_guild or member.guild_permissions.manage_channels:
            return True
        role_ids = {int(x) for x in config.get("staff_role_ids", []) if str(x).isdigit()}
        return any(role.id in role_ids for role in member.roles)

    async def handle_ticket_control(self, interaction: discord.Interaction, action: str):
        channel = interaction.channel
        if interaction.guild is None or not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(embed=error_embed("These controls only work in ticket text channels."), ephemeral=True)
        if not self._db_ready():
            return await interaction.response.send_message(embed=error_embed("Ticket storage is unavailable."), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ticket = await self.tickets_collection.find_one({"_id": channel.id, "guild_id": interaction.guild.id})
        if ticket is None:
            return await interaction.followup.send(embed=error_embed("This channel is not registered as a ticket."), ephemeral=True)
        config = await self.get_config(interaction.guild.id)
        member = interaction.user if isinstance(interaction.user, discord.Member) else await interaction.guild.fetch_member(interaction.user.id)
        staff = self._is_staff(member, config)
        opener = member.id == int(ticket.get("opener_id", 0))

        if action == "claim":
            if not config.get("allow_claim", True):
                return await interaction.followup.send(embed=error_embed("Ticket claiming is disabled by an administrator."), ephemeral=True)
            if not staff:
                return await interaction.followup.send(embed=error_embed("Only support staff can claim tickets."), ephemeral=True)
            if ticket.get("claimed_by"):
                return await interaction.followup.send(embed=error_embed(f"This ticket is already claimed by <@{ticket['claimed_by']}>."), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            claim_result = await self.tickets_collection.update_one(
                {"_id": channel.id, "claimed_by": None},
                {"$set": {"claimed_by": member.id, "claimed_by_name": member.display_name, "claimed_at": int(time.time())}},
            )
            if claim_result.modified_count == 0:
                latest = await self.tickets_collection.find_one({"_id": channel.id}) or {}
                owner = f"<@{latest['claimed_by']}>" if latest.get("claimed_by") else "another staff member"
                return await interaction.followup.send(embed=error_embed(f"This ticket is already claimed by {owner}."), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            await self._mark_claimed(interaction.message, member, ticket)
            message = _format_ticket(str(config.get("claim_message", "Ticket claimed by {user}.")), user=member, guild=interaction.guild, channel=channel, option=ticket.get("option_label", "Ticket"), ticket_id=channel.id)
            await channel.send(embed=make_embed("Ticket Claimed", message), allowed_mentions=discord.AllowedMentions.none())
            return await interaction.followup.send(embed=success_embed("You claimed this ticket."), ephemeral=True)

        if action == "close":
            if ticket.get("closed"):
                return await interaction.followup.send(embed=error_embed("This ticket is already closed."), ephemeral=True)
            if opener and not config.get("allow_user_close", True) and not staff:
                return await interaction.followup.send(embed=error_embed("Only support staff can close tickets in this server."), ephemeral=True)
            if not opener and not staff:
                return await interaction.followup.send(embed=error_embed("Only the ticket creator or support staff can close this ticket."), ephemeral=True)
            confirm = ConfirmLayout(
                member,
                "Are you sure you want to close this ticket?\nThe ticket creator will lose access to the channel. Support staff can reopen or delete it afterwards.",
                title="🔒 Close this ticket?",
                confirm_label="Close Ticket",
                cancel_label="Cancel",
                cancel_text="❌ Ticket close cancelled",
                timeout=60.0,
            )
            try:
                confirm.message = await interaction.followup.send(view=confirm, ephemeral=True, wait=True)
            except discord.HTTPException as exc:
                print(f"Tickets Cog: could not send close confirmation for {channel.id}: {exc}")
                return
            await confirm.wait()
            if not confirm:
                return
            latest = await self.tickets_collection.find_one({"_id": channel.id, "guild_id": interaction.guild.id})
            if latest is None or latest.get("closed"):
                return await confirm.show(error_embed("This ticket is already closed."))
            await confirm.show(success_embed("Closing ticket…"))
            await self._close_ticket(channel, latest, config, member)
            return

        if action == "reopen":
            if not staff:
                return await interaction.followup.send(embed=error_embed("Only support staff can reopen tickets."), ephemeral=True)
            if not ticket.get("closed"):
                return await interaction.followup.send(embed=error_embed("This ticket is already open."), ephemeral=True)
            opener_member = interaction.guild.get_member(int(ticket["opener_id"]))
            if opener_member is None:
                try:
                    opener_member = await interaction.guild.fetch_member(int(ticket["opener_id"]))
                except discord.HTTPException:
                    opener_member = None
            if opener_member:
                overwrite = channel.overwrites_for(opener_member)
                overwrite.view_channel = True
                overwrite.send_messages = True
                overwrite.read_message_history = True
                await channel.set_permissions(opener_member, overwrite=overwrite, reason="Ticket reopened")
            pending_delete = self._delete_tasks.pop(channel.id, None)
            if pending_delete is not None:
                pending_delete.cancel()
            await self.tickets_collection.update_one({"_id": channel.id}, {"$set": {"closed": False, "reopened_at": int(time.time())}, "$unset": {"closed_at": "", "closed_by": "", "closed_by_name": ""}})
            message = _format_ticket(str(config.get("reopen_message", "Ticket reopened by {user}.")), user=member, guild=interaction.guild, channel=channel, option=ticket.get("option_label", "Ticket"), ticket_id=channel.id)
            await channel.send(view=TicketControlView(self, embed=make_embed("🔓 Ticket Reopened", message), mode="reopened"), allowed_mentions=discord.AllowedMentions.none())
            self._edit_channel_soon(
                channel,
                name=channel.name.removeprefix("closed-")[:100],
                topic=(channel.topic or "").replace(str(config.get("closed_topic_suffix", "(closed)")), "").strip()[:1024],
                reason=f"Ticket reopened by {member}",
            )
            return await interaction.followup.send(embed=success_embed("Ticket reopened."), ephemeral=True)

        if action == "delete":
            if not staff:
                return await interaction.followup.send(embed=error_embed("Only support staff can delete tickets."), ephemeral=True)
            if not ticket.get("closed"):
                return await interaction.followup.send(embed=error_embed("Close this ticket before deleting it."), ephemeral=True)
            await interaction.followup.send(embed=success_embed("Deleting closed ticket…"), ephemeral=True)
            await self._delete_ticket(channel, ticket, config, deleter=member, interaction=interaction)
            return

    async def _mark_claimed(self, message: discord.Message | None, member: discord.Member, ticket: dict):
        if message is None:
            return
        try:
            stored = ticket.get("control_embed")
            if stored:
                embed = discord.Embed.from_dict(stored)
            elif message.embeds:
                embed = discord.Embed.from_dict(message.embeds[0].to_dict())
            else:
                embed = make_embed("🎫 Ticket")
            for index in reversed(range(len(embed.fields))):
                if embed.fields[index].name == "Claimed by":
                    embed.remove_field(index)
            embed.add_field(name="Claimed by", value=member.mention, inline=False)
            kwargs = {
                "view": TicketControlView(self, embed=embed, mode="claimed", lead=ticket.get("control_lead")),
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if not getattr(message.flags, "components_v2", False):
                kwargs.update(content=None, embeds=[])
            await message.edit(**kwargs)
        except (discord.HTTPException, TypeError, ValueError) as exc:
            print(f"Tickets Cog: could not update claimed ticket message {message.id}: {exc}")

    async def _close_ticket(self, channel: discord.TextChannel, ticket: dict, config: dict, closer: discord.Member):
        guild = channel.guild
        opener = guild.get_member(int(ticket["opener_id"]))
        if opener is None:
            try:
                opener = await guild.fetch_member(int(ticket["opener_id"]))
            except discord.HTTPException:
                opener = None
        if opener:
            overwrite = channel.overwrites_for(opener)
            overwrite.view_channel = False
            overwrite.send_messages = False
            overwrite.read_message_history = False
            try:
                await channel.set_permissions(opener, overwrite=overwrite, reason=f"Ticket closed by {closer}")
            except discord.HTTPException:
                pass
        await self.tickets_collection.update_one({"_id": channel.id}, {"$set": {"closed": True, "closed_at": int(time.time()), "closed_by": closer.id, "closed_by_name": closer.display_name}})
        close_text = _format_ticket(str(config.get("close_message", "Ticket closed by {user}.")), user=closer, guild=guild, channel=channel, option=ticket.get("option_label", "Ticket"), ticket_id=channel.id)
        await channel.send(view=TicketControlView(self, embed=make_embed("🔒 Ticket Closed", close_text), mode="closed"), allowed_mentions=discord.AllowedMentions.none())
        suffix = str(config.get("closed_topic_suffix", "(closed)")).strip()
        topic = (channel.topic or "").replace(suffix, "").strip()
        if suffix:
            topic = f"{topic} {suffix}".strip()
        name = channel.name if channel.name.startswith("closed-") else f"closed-{channel.name}"
        self._edit_channel_soon(channel, name=name[:100], topic=topic[:1024], reason=f"Ticket closed by {closer}")
        delay_minutes = int(config.get("delete_after_close_minutes", 0) or 0)
        if delay_minutes > 0:
            task = asyncio.create_task(self._delete_channel_after(channel.id, delay_minutes * 60))
            old = self._delete_tasks.pop(channel.id, None)
            if old:
                old.cancel()
            self._delete_tasks[channel.id] = task
            task.add_done_callback(lambda _task, channel_id=channel.id: self._delete_tasks.pop(channel_id, None))

    async def _delete_ticket(self, channel: discord.TextChannel, ticket: dict, config: dict, *, deleter: discord.Member | None = None, interaction: discord.Interaction | None = None) -> bool:
        guild = channel.guild
        deleted_at = datetime.now(timezone.utc)
        pending = self._delete_tasks.pop(channel.id, None)
        if pending is not None and pending is not asyncio.current_task():
            pending.cancel()
        if deleter is not None:
            deleted_by_text = f"{deleter.display_name} ({deleter.id})"
            deleted_by_mention = deleter.mention
            reason = f"Ticket deleted by {deleter} ({deleter.id})"
        else:
            deleted_by_text = deleted_by_mention = "Automatic deletion (close timer)"
            reason = "Ticket auto-deleted after configured close delay"

        transcript_bytes: bytes | None = None
        transcript_name = "transcript.html"
        message_count = 0
        if config.get("transcripts_enabled", True):
            try:
                built, message_count = await build_ticket_transcript(channel, ticket, deleted_by=deleted_by_text, deleted_at=deleted_at)
                transcript_bytes, transcript_name = built.fp.getvalue(), built.filename
            except Exception as exc:
                print(f"Tickets Cog: could not build transcript for {channel.id}: {exc}")

        def transcript_file() -> discord.File:
            return discord.File(io.BytesIO(transcript_bytes or b""), filename=transcript_name)

        try:
            await channel.delete(reason=reason)
        except discord.HTTPException as exc:
            print(f"Tickets Cog: could not delete ticket {channel.id}: {exc}")
            if interaction is not None:
                try:
                    await interaction.followup.send(embed=error_embed(f"Could not delete this ticket: {exc}"), ephemeral=True)
                except discord.HTTPException:
                    pass
            return False

        try:
            await self.tickets_collection.update_one(
                {"_id": channel.id},
                {"$set": {"closed": True, "deleted": True, "deleted_at": int(deleted_at.timestamp()), "deleted_by": deleter.id if deleter else None}},
            )
        except Exception as exc:
            print(f"Tickets Cog: could not mark ticket {channel.id} deleted: {exc}")

        number = ticket.get("ticket_number")
        lines = []
        if number:
            lines.append(f"**Ticket:** #{int(number):04d}")
        lines.extend([
            f"**Option:** {ticket.get('option_label', 'Ticket')}",
            f"**Ticket ID:** `{channel.id}`",
            f"**Creator:** <@{ticket['opener_id']}>",
        ])
        if ticket.get("claimed_by"):
            lines.append(f"**Claimed by:** <@{ticket['claimed_by']}>")
        if ticket.get("closed_by"):
            lines.append(f"**Closed by:** <@{ticket['closed_by']}>")
        lines.append(f"**Deleted by:** {deleted_by_mention}")
        if transcript_bytes is not None:
            lines.append(f"**Messages archived:** {message_count}")
        log_embed = make_embed("🗑️ Ticket Deleted", "\n".join(lines))

        log_channel = guild.get_channel(int(config["log_channel_id"])) if config.get("log_channel_id") else None
        sent = False
        if log_channel and isinstance(log_channel, (discord.TextChannel, discord.Thread)):
            try:
                kwargs = {"embed": log_embed, "allowed_mentions": discord.AllowedMentions.none()}
                if transcript_bytes is not None:
                    kwargs["file"] = transcript_file()
                await log_channel.send(**kwargs)
                sent = True
            except discord.HTTPException as exc:
                print(f"Tickets Cog: could not log deleted ticket {channel.id}: {exc}")
        if not sent and transcript_bytes is not None and interaction is not None:
            try:
                await interaction.followup.send(
                    embed=make_embed("📄 Ticket Transcript", "No log channel could be used, so the transcript is attached here for you."),
                    file=transcript_file(),
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
        return True

    async def _delete_channel_after(self, channel_id: int, seconds: int):
        await asyncio.sleep(seconds)
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            ticket = await self.tickets_collection.find_one({"_id": channel_id})
            if ticket is None or not ticket.get("closed"):
                return
            config = await self.get_config(channel.guild.id)
            await self._delete_ticket(channel, ticket, config, deleter=None)
        except Exception as exc:
            print(f"Tickets Cog: auto-delete failed for {channel_id}: {exc}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))