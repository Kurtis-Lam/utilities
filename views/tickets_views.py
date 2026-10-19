from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import discord
from discord.ui import Button, Modal, Select, TextInput

from views.common_views import EmbedLayout, error_embed, make_embed, success_embed


def ticket_config_embed(config: dict, guild: discord.Guild | None) -> discord.Embed:
    category_id = config.get("default_category_id")
    log_id = config.get("log_channel_id")
    staff = config.get("staff_role_ids", [])
    ping = config.get("ping_role_ids", [])
    options = config.get("options", [])
    panels = config.get("panels", [])
    embed = make_embed("🎫 Ticket System Configuration", "Configure panels, ticket options, channel permissions, support roles, messages, transcripts and close controls.")
    embed.add_field(name="Category", value=f"<#{category_id}>" if category_id else "*Not set*", inline=True)
    embed.add_field(name="Log channel", value=f"<#{log_id}>" if log_id else "*Not set*", inline=True)
    embed.add_field(name="Max open", value=str(config.get("max_open_tickets", 3)), inline=True)
    option_lines = [f"{o.get('emoji') or '▫️'} `{o.get('key', '?')}` — **{o.get('label', 'Untitled')}**" for o in options[:12]]
    if len(options) > 12:
        option_lines.append(f"…and {len(options) - 12} more")
    embed.add_field(name=f"Creation options ({len(options)}/25)", value="\n".join(option_lines) if option_lines else "*No options configured yet*", inline=False)
    embed.add_field(name="Support roles", value=" ".join(f"<@&{r}>" for r in staff) if staff else "*None*", inline=True)
    embed.add_field(name="Pinged roles", value=" ".join(f"<@&{r}>" for r in ping) if ping else "*None*", inline=True)
    on_off = lambda value: "On" if value else "Off"
    settings = (
        f"Panels: {len(panels)} • Opener close: {on_off(config.get('allow_user_close', True))} • "
        f"Claiming: {on_off(config.get('allow_claim', True))} • Transcripts: {on_off(config.get('transcripts_enabled', True))}"
    )
    embed.add_field(name="Settings", value=settings, inline=False)
    embed.set_footer(text=f"Settings for {guild.name if guild else 'this server'} • IDs may be entered as raw IDs or channel/role mentions")
    return embed


def _ids(raw: str) -> list[int]:
    seen = []
    for value in re.findall(r"\d{15,25}", raw or ""):
        number = int(value)
        if number not in seen:
            seen.append(number)
    return seen


def _field(value: str, *, label: str, max_length: int = 4000, required: bool = False, style=discord.TextStyle.short, placeholder: str = ""):
    return TextInput(label=label, style=style, required=required, max_length=max_length, default=value[:max_length] if value else None, placeholder=placeholder[:100])


class TicketPanelView(discord.ui.View):
    """Persistent panel button. The message ID maps the interaction to its settings."""

    def __init__(self, cog, button_label: str = "Create Ticket"):
        super().__init__(timeout=None)
        self.cog = cog
        button = Button(label=(button_label or "Create Ticket")[:80], emoji="🎫", style=discord.ButtonStyle.primary, custom_id="tickets:create")
        button.callback = self.create_ticket_callback
        self.add_item(button)

    async def create_ticket_callback(self, interaction: discord.Interaction):
        if interaction.guild is None or interaction.message is None:
            return await interaction.response.send_message(embed=error_embed("Use this panel inside the server."), ephemeral=True)
        if not self.cog._db_ready():
            return await interaction.response.send_message(embed=error_embed("Ticket storage is unavailable. Please contact an administrator."), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        config = await self.cog.get_config(interaction.guild.id)
        panel = next((p for p in config.get("panels", []) if int(p.get("message_id", 0)) == interaction.message.id), None)
        if panel is None:
            return await interaction.followup.send(embed=error_embed("This ticket panel is no longer configured. Ask an administrator to repost it."), ephemeral=True)
        options = config.get("options", [])
        if not options:
            return await interaction.followup.send(embed=error_embed("No ticket options have been configured yet."), ephemeral=True)
        view = TicketOptionSelectView(self.cog, config, panel)
        embed = make_embed("🎫 Create a Ticket", str(config.get("choose_option_message", "Choose the category that best matches your request.")))
        try:
            view.message = await interaction.followup.send(embed=embed, view=view, ephemeral=True, wait=True)
        except discord.HTTPException:
            pass


class TicketOptionSelect(Select):
    def __init__(self, parent_view):
        self.parent_view = parent_view
        choices = parent_view.config.get("options", [])[:25]
        options = [
            discord.SelectOption(
                label=str(option.get("label", "Ticket option"))[:100],
                value=str(option.get("key", "general"))[:100],
                description=str(option.get("description", "Create a ticket"))[:100] or None,
                emoji=option.get("emoji") or None,
            )
            for option in choices
        ]
        super().__init__(placeholder="Select a ticket type…", min_values=1, max_values=1, options=options, custom_id="tickets:choose_option")

    async def callback(self, interaction: discord.Interaction):
        await self.parent_view.choose(interaction, self.values[0])


class TicketOptionSelectView(discord.ui.View):
    def __init__(self, cog, config: dict, panel: dict):
        super().__init__(timeout=120)
        self.cog, self.config, self.panel = cog, config, panel
        self.message: discord.Message | None = None
        self.add_item(TicketOptionSelect(self))

    async def choose(self, interaction: discord.Interaction, option_key: str):
        try:
            await self.cog.create_ticket(interaction, option_key, self.config, self.panel)
        finally:
            for item in self.children:
                if hasattr(item, "disabled"):
                    item.disabled = True
            if self.message is not None:
                try:
                    await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    pass
            self.stop()

    async def on_timeout(self):
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass


class TicketControlView(discord.ui.View):
    """Persistent claim, close, reopen and delete controls for ticket channels.

    Modes: ``open`` (Claim + Close), ``claimed`` (Close only), ``closed`` (Reopen + Delete)
    and ``all`` (every button; only used to register the persistent callbacks).
    """

    def __init__(self, cog, mode: str = "open"):
        super().__init__(timeout=None)
        self.cog = cog
        if mode == "open":
            self.remove_item(self.reopen)
            self.remove_item(self.delete)
        elif mode == "claimed":
            self.remove_item(self.claim)
            self.remove_item(self.reopen)
            self.remove_item(self.delete)
        elif mode == "closed":
            self.remove_item(self.claim)
            self.remove_item(self.close)

    @discord.ui.button(label="Claim", emoji="🖐️", style=discord.ButtonStyle.primary, custom_id="tickets:claim")
    async def claim(self, interaction: discord.Interaction, button: Button):
        await self.cog.handle_ticket_control(interaction, "claim")

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="tickets:close")
    async def close(self, interaction: discord.Interaction, button: Button):
        await self.cog.handle_ticket_control(interaction, "close")

    @discord.ui.button(label="Reopen", emoji="🔓", style=discord.ButtonStyle.success, custom_id="tickets:reopen")
    async def reopen(self, interaction: discord.Interaction, button: Button):
        await self.cog.handle_ticket_control(interaction, "reopen")

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.secondary, custom_id="tickets:delete")
    async def delete(self, interaction: discord.Interaction, button: Button):
        await self.cog.handle_ticket_control(interaction, "delete")


class TicketDashboardView(EmbedLayout):
    def __init__(self, cog, config: dict, guild: discord.Guild, author_id: int):
        super().__init__(ticket_config_embed(config, guild), author_id=author_id, timeout=300)
        self.cog, self.config, self.guild = cog, config, guild
        self.base_btn = Button(label="Panel & Category", emoji="🗂️", style=discord.ButtonStyle.primary)
        self.base_btn.callback = self.base_settings
        self.roles_btn = Button(label="Support & Ping Roles", emoji="🛡️", style=discord.ButtonStyle.primary)
        self.roles_btn.callback = self.role_settings
        self.messages_btn = Button(label="Ticket Messages", emoji="💬", style=discord.ButtonStyle.primary)
        self.messages_btn.callback = self.message_settings
        self.security_btn = Button(label="Limits & Controls", emoji="⚙️", style=discord.ButtonStyle.secondary)
        self.security_btn.callback = self.security_settings
        self.add_option_btn = Button(label="Add Option", emoji="➕", style=discord.ButtonStyle.success)
        self.add_option_btn.callback = self.add_option
        self.edit_option_btn = Button(label="Edit Option", emoji="✏️", style=discord.ButtonStyle.secondary)
        self.edit_option_btn.callback = self.edit_option
        self.remove_option_btn = Button(label="Remove Option", emoji="🗑️", style=discord.ButtonStyle.danger)
        self.remove_option_btn.callback = self.remove_option
        self.post_panel_btn = Button(label="Post Panel", emoji="📨", style=discord.ButtonStyle.success)
        self.post_panel_btn.callback = self.post_panel
        self.refresh_btn = Button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary)
        self.refresh_btn.callback = self.refresh
        self.render()

    def rows(self):
        return [
            [self.base_btn, self.roles_btn, self.messages_btn],
            [self.security_btn, self.add_option_btn, self.edit_option_btn, self.remove_option_btn],
            [self.post_panel_btn, self.refresh_btn],
        ]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.author_id is not None and interaction.user.id != self.author_id:
            await interaction.response.send_message(embed=error_embed("This configuration panel belongs to another administrator."), ephemeral=True)
            return False
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(embed=error_embed("Administrator permission is required."), ephemeral=True)
            return False
        return await super().interaction_check(interaction)

    async def reload_config(self):
        self.config = await self.cog.get_config(self.guild.id)
        self.set_embed(ticket_config_embed(self.config, self.guild))

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.reload_config()
        await interaction.edit_original_response(view=self)

    async def refresh_message(self):
        if self.message is None:
            return
        await self.reload_config()
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass

    async def base_settings(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TicketBaseSettingsModal(self))

    async def role_settings(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TicketRoleSettingsModal(self))

    async def message_settings(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TicketMessageSettingsModal(self))

    async def security_settings(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TicketSecuritySettingsModal(self))

    async def add_option(self, interaction: discord.Interaction):
        if len(self.config.get("options", [])) >= 25:
            return await interaction.response.send_message(embed=error_embed("Discord panels support up to 25 ticket options. Remove one before adding another."), ephemeral=True)
        await interaction.response.send_modal(TicketOptionModal(self.cog, self.guild.id, self, mode="add"))

    async def edit_option(self, interaction: discord.Interaction):
        options = self.config.get("options", [])
        if not options:
            return await interaction.response.send_message(embed=error_embed("Add an option first."), ephemeral=True)
        await interaction.response.send_message(embed=make_embed("Edit Ticket Option", "Choose the option to edit."), view=TicketOptionActionView(self.cog, self.guild.id, self, options, "edit"), ephemeral=True)

    async def remove_option(self, interaction: discord.Interaction):
        options = self.config.get("options", [])
        if not options:
            return await interaction.response.send_message(embed=error_embed("There are no ticket options to remove."), ephemeral=True)
        await interaction.response.send_message(embed=make_embed("Remove Ticket Option", "Choose the option to remove."), view=TicketOptionActionView(self.cog, self.guild.id, self, options, "remove"), ephemeral=True)

    async def post_panel(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PostTicketPanelModal(self))

    async def on_timeout(self):
        self.disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass


class TicketBaseSettingsModal(Modal, title="Ticket Panel & Category"):
    panel_title = TextInput(label="Default panel title", required=True, max_length=256)
    panel_description = TextInput(label="Default panel description", style=discord.TextStyle.paragraph, required=True, max_length=3000)
    category_id = TextInput(label="Default category ID (blank to clear)", required=False, max_length=25)
    channel_template = TextInput(label="Ticket channel name template", required=True, max_length=80, placeholder="ticket-{username}-{number}")
    choose_message = TextInput(label="Ephemeral option prompt", required=True, max_length=1000)

    def __init__(self, dashboard: TicketDashboardView):
        super().__init__()
        self.dashboard = dashboard
        c = dashboard.config
        self.panel_title.default = str(c.get("panel_title", "Need help?"))[:256]
        self.panel_description.default = str(c.get("panel_description", "Choose a category below to open a private support ticket."))[:3000]
        self.category_id.default = str(c.get("default_category_id") or "")
        self.channel_template.default = str(c.get("channel_name_template", "ticket-{username}-{number}"))[:80]
        self.choose_message.default = str(c.get("choose_option_message", "Choose the category that best matches your request."))[:1000]

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.category_id.value.strip()
        ids = _ids(raw)
        if raw and not ids:
            return await interaction.response.send_message(embed=error_embed("Enter a category ID, mention, or leave it blank."), ephemeral=True)
        category = interaction.guild.get_channel(ids[0]) if ids and interaction.guild else None
        if category is not None and not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message(embed=error_embed("The default destination must be a category channel."), ephemeral=True)
        values = {
            "panel_title": self.panel_title.value,
            "panel_description": self.panel_description.value,
            "default_category_id": ids[0] if ids else None,
            "channel_name_template": self.channel_template.value,
            "choose_option_message": self.choose_message.value,
        }
        await self.dashboard.cog.save_config_fields(self.dashboard.guild.id, values)
        await interaction.response.send_message(embed=success_embed("Ticket panel and category settings saved."), ephemeral=True)
        await self.dashboard.refresh_message()


class TicketRoleSettingsModal(Modal, title="Ticket Roles & Logs"):
    staff_roles = TextInput(label="Support role IDs / mentions (comma separated)", style=discord.TextStyle.paragraph, required=False, max_length=1000)
    ping_roles = TextInput(label="Roles to ping on ticket creation", style=discord.TextStyle.paragraph, required=False, max_length=1000)
    log_channel = TextInput(label="Transcript/log channel ID (blank to clear)", required=False, max_length=25)

    def __init__(self, dashboard: TicketDashboardView):
        super().__init__()
        self.dashboard = dashboard
        c = dashboard.config
        self.staff_roles.default = ", ".join(str(x) for x in c.get("staff_role_ids", []))[:1000]
        self.ping_roles.default = ", ".join(str(x) for x in c.get("ping_role_ids", []))[:1000]
        self.log_channel.default = str(c.get("log_channel_id") or "")

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        staff_ids = _ids(self.staff_roles.value)
        ping_ids = _ids(self.ping_roles.value)
        all_ids = set(staff_ids + ping_ids)
        missing = [rid for rid in all_ids if guild is None or guild.get_role(rid) is None]
        if missing:
            return await interaction.response.send_message(embed=error_embed("Unknown role ID(s): " + ", ".join(map(str, missing))), ephemeral=True)
        log_raw = self.log_channel.value.strip()
        log_ids = _ids(log_raw)
        if log_raw and not log_ids:
            return await interaction.response.send_message(embed=error_embed("Log channel must be a channel ID/mention or blank."), ephemeral=True)
        log_channel = guild.get_channel(log_ids[0]) if log_ids and guild else None
        if log_ids and not isinstance(log_channel, discord.TextChannel):
            return await interaction.response.send_message(embed=error_embed("Log channel must be a text-based channel."), ephemeral=True)
        await self.dashboard.cog.save_config_fields(self.dashboard.guild.id, {
            "staff_role_ids": staff_ids,
            "ping_role_ids": ping_ids,
            "log_channel_id": log_ids[0] if log_ids else None,
        })
        await interaction.response.send_message(embed=success_embed("Support roles, ping roles, and log channel saved."), ephemeral=True)
        await self.dashboard.refresh_message()


class TicketMessageSettingsModal(Modal, title="Ticket Messages"):
    open_message = TextInput(label="Ticket opening embed description", style=discord.TextStyle.paragraph, required=True, max_length=3000)
    close_message = TextInput(label="Close message template", style=discord.TextStyle.paragraph, required=True, max_length=1000)
    reopen_message = TextInput(label="Reopen message template", style=discord.TextStyle.paragraph, required=True, max_length=1000)
    claim_message = TextInput(label="Claim message template", required=True, max_length=1000)
    closed_topic = TextInput(label="Closed channel topic suffix", required=False, max_length=200)

    def __init__(self, dashboard: TicketDashboardView):
        super().__init__()
        self.dashboard = dashboard
        c = dashboard.config
        self.open_message.default = str(c.get("open_message", "Hello {user}, the team will be with you shortly.\n**Reason:** {reason}"))[:3000]
        self.close_message.default = str(c.get("close_message", "Ticket closed by {user}."))[:1000]
        self.reopen_message.default = str(c.get("reopen_message", "Ticket reopened by {user}."))[:1000]
        self.claim_message.default = str(c.get("claim_message", "Ticket claimed by {user}."))[:1000]
        self.closed_topic.default = str(c.get("closed_topic_suffix", "(closed)"))[:200]

    async def on_submit(self, interaction: discord.Interaction):
        await self.dashboard.cog.save_config_fields(self.dashboard.guild.id, {
            "open_message": self.open_message.value,
            "close_message": self.close_message.value,
            "reopen_message": self.reopen_message.value,
            "claim_message": self.claim_message.value,
            "closed_topic_suffix": self.closed_topic.value,
        })
        await interaction.response.send_message(embed=success_embed("Ticket message templates saved."), ephemeral=True)
        await self.dashboard.refresh_message()


class TicketSecuritySettingsModal(Modal, title="Ticket Limits & Controls"):
    max_open = TextInput(label="Max open tickets per member (0 = unlimited)", required=True, max_length=3)
    allow_user_close = TextInput(label="Allow ticket creator to close? (true/false)", required=True, max_length=5)
    allow_claim = TextInput(label="Enable ticket claim button? (true/false)", required=True, max_length=5)
    transcripts = TextInput(label="Transcript on ticket delete? (true/false)", required=True, max_length=5)
    deletion_delay = TextInput(label="Delete closed ticket after min (0 = never)", required=True, max_length=4)

    def __init__(self, dashboard: TicketDashboardView):
        super().__init__()
        self.dashboard = dashboard
        c = dashboard.config
        self.max_open.default = str(c.get("max_open_tickets", 3))
        self.allow_user_close.default = str(bool(c.get("allow_user_close", True))).lower()
        self.allow_claim.default = str(bool(c.get("allow_claim", True))).lower()
        self.transcripts.default = str(bool(c.get("transcripts_enabled", True))).lower()
        self.deletion_delay.default = str(c.get("delete_after_close_minutes", 0))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            max_open = int(self.max_open.value.strip())
            delete_delay = int(self.deletion_delay.value.strip())
            if max_open < 0 or not 0 <= delete_delay <= 10080:
                raise ValueError("limits out of range")
            parse_bool = lambda value: value.strip().lower() in ("true", "yes", "1", "on") if value.strip().lower() in ("true", "false", "yes", "no", "1", "0", "on", "off") else None
            allow_close, allow_claim, transcripts = map(parse_bool, (self.allow_user_close.value, self.allow_claim.value, self.transcripts.value))
            if None in (allow_close, allow_claim, transcripts):
                raise ValueError("boolean values must be true or false")
        except ValueError as exc:
            return await interaction.response.send_message(embed=error_embed(f"Invalid setting: {exc}."), ephemeral=True)
        await self.dashboard.cog.save_config_fields(self.dashboard.guild.id, {
            "max_open_tickets": max_open,
            "allow_user_close": allow_close,
            "allow_claim": allow_claim,
            "transcripts_enabled": transcripts,
            "delete_after_close_minutes": delete_delay,
        })
        await interaction.response.send_message(embed=success_embed("Ticket limits and controls saved."), ephemeral=True)
        await self.dashboard.refresh_message()


class TicketOptionModal(Modal, title="Ticket Option"):
    option_label = TextInput(label="Option name", required=True, max_length=100)
    option_emoji = TextInput(label="Emoji (blank = none)", required=False, max_length=64, placeholder="🎫  or a custom emoji like <:name:id>")
    option_description = TextInput(label="Option description", required=True, max_length=100, placeholder="Describe what this ticket is for")
    category_id = TextInput(label="Destination category ID (blank = default)", required=False, max_length=25)
    ping_roles = TextInput(label="Extra roles to ping (comma separated IDs)", required=False, max_length=500)

    def __init__(self, cog, guild_id: int, dashboard: TicketDashboardView, mode: str, option: dict | None = None):
        super().__init__(title="Add Ticket Option" if mode == "add" else "Edit Ticket Option")
        self.cog, self.guild_id, self.dashboard, self.mode = cog, guild_id, dashboard, mode
        self.option_key = option.get("key") if option else None
        option = option or {}
        self.existing_option = option
        self.option_label.default = str(option.get("label", ""))[:100] or None
        self.option_emoji.default = str(option.get("emoji") or "")[:64] or None
        self.option_description.default = str(option.get("description", ""))[:100] or None
        self.category_id.default = str(option.get("category_id") or "")
        self.ping_roles.default = ", ".join(map(str, option.get("ping_role_ids", [])))[:500]

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        category_raw = self.category_id.value.strip()
        category_ids = _ids(category_raw)
        if category_raw and not category_ids:
            return await interaction.response.send_message(embed=error_embed("Category must be an ID/mention or blank."), ephemeral=True)
        category = guild.get_channel(category_ids[0]) if guild and category_ids else None
        if category_ids and not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message(embed=error_embed("That destination is not a category."), ephemeral=True)
        pings = _ids(self.ping_roles.value)
        missing = [rid for rid in pings if guild is None or guild.get_role(rid) is None]
        if missing:
            return await interaction.response.send_message(embed=error_embed("Unknown role ID(s): " + ", ".join(map(str, missing))), ephemeral=True)

        label = self.option_label.value.strip()
        emoji = self.option_emoji.value.strip() or None
        config = await self.cog.get_config(self.guild_id)
        options = list(config.get("options", []))
        key = self.option_key or re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "option"
        existing = next((i for i, item in enumerate(options) if item.get("key") == key), None)
        if self.mode == "add":
            if len(options) >= 25:
                return await interaction.response.send_message(embed=error_embed("Discord supports up to 25 ticket options. Remove an option before adding another."), ephemeral=True)
            if existing is not None:
                key = f"{key}-{int(time.time()) % 100000}"
        elif existing is None:
            return await interaction.response.send_message(embed=error_embed("That option no longer exists. Refresh the dashboard."), ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        probe = None
        if emoji:
            probe_view = discord.ui.View(timeout=60)
            try:
                probe_view.add_item(Select(placeholder="Previewing emoji…", options=[discord.SelectOption(label=label[:100] or "Preview", value="preview", emoji=emoji)]))
                probe = await interaction.followup.send(content="Checking that emoji…", view=probe_view, ephemeral=True, wait=True)
            except (discord.HTTPException, ValueError, TypeError):
                return await interaction.followup.send(
                    embed=error_embed("Discord did not accept that emoji. Use a standard emoji like 🎫, or a custom emoji from a server the bot is in, like <:name:id>."),
                    ephemeral=True,
                )
            finally:
                probe_view.stop()

        updated = {
            "key": key,
            "label": label,
            "description": self.option_description.value.strip(),
            "category_id": category_ids[0] if category_ids else None,
            "ping_role_ids": pings,
            "open_message": str(self.existing_option.get("open_message", "")) if self.mode == "edit" else "",
            "emoji": emoji,
        }
        config = await self.cog.get_config(self.guild_id)
        options = list(config.get("options", []))
        index = next((i for i, item in enumerate(options) if item.get("key") == key), None)
        if self.mode == "add" and index is None:
            options.append(updated)
        elif index is not None and self.mode == "edit":
            options[index] = updated
        else:
            return await interaction.followup.send(embed=error_embed("Could not save that option. Refresh the dashboard and try again."), ephemeral=True)
        await self.cog.save_config_fields(self.guild_id, {"options": options})

        success = success_embed(f"Ticket option {emoji + ' ' if emoji else ''}**{label}** saved.\nUse the button below to set a custom opening message for this option.")
        follow_up = TicketOpeningMessageView(self.cog, self.guild_id, key, self.dashboard)
        if probe is not None:
            try:
                await probe.edit(content=None, embed=success, view=follow_up)
                follow_up.message = probe
            except discord.HTTPException:
                follow_up.message = await interaction.followup.send(embed=success, view=follow_up, ephemeral=True, wait=True)
        else:
            follow_up.message = await interaction.followup.send(embed=success, view=follow_up, ephemeral=True, wait=True)
        await self.dashboard.refresh_message()


class TicketOpeningMessageModal(Modal, title="Custom Opening Message"):
    opening_message = TextInput(
        label="Opening message (blank = server default)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
        placeholder="Placeholders: {user} {username} {option} {reason}",
    )

    def __init__(self, cog, guild_id: int, option_key: str, dashboard: TicketDashboardView, current: str = ""):
        super().__init__()
        self.cog, self.guild_id, self.option_key, self.dashboard = cog, guild_id, option_key, dashboard
        self.opening_message.default = (current or "")[:1000] or None

    async def on_submit(self, interaction: discord.Interaction):
        config = await self.cog.get_config(self.guild_id)
        options = list(config.get("options", []))
        index = next((i for i, item in enumerate(options) if item.get("key") == self.option_key), None)
        if index is None:
            return await interaction.response.send_message(embed=error_embed("That option no longer exists."), ephemeral=True)
        options[index] = {**options[index], "open_message": self.opening_message.value}
        await self.cog.save_config_fields(self.guild_id, {"options": options})
        await interaction.response.send_message(embed=success_embed("Custom opening message saved."), ephemeral=True)
        await self.dashboard.refresh_message()


class TicketOpeningMessageView(discord.ui.View):
    def __init__(self, cog, guild_id: int, option_key: str, dashboard: TicketDashboardView):
        super().__init__(timeout=300)
        self.cog, self.guild_id, self.option_key, self.dashboard = cog, guild_id, option_key, dashboard
        self.message: discord.Message | None = None
        button = Button(label="Custom Opening Message", emoji="📝", style=discord.ButtonStyle.secondary)
        button.callback = self.open_modal
        self.add_item(button)

    async def open_modal(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(embed=error_embed("Administrator permission is required."), ephemeral=True)
        config = await self.cog.get_config(self.guild_id)
        option = next((o for o in config.get("options", []) if o.get("key") == self.option_key), None)
        if option is None:
            return await interaction.response.send_message(embed=error_embed("That option no longer exists."), ephemeral=True)
        await interaction.response.send_modal(TicketOpeningMessageModal(self.cog, self.guild_id, self.option_key, self.dashboard, str(option.get("open_message", ""))))

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


class TicketOptionActionView(discord.ui.View):
    def __init__(self, cog, guild_id: int, dashboard: TicketDashboardView, options: list[dict], action: str):
        super().__init__(timeout=90)
        self.cog, self.guild_id, self.dashboard, self.action = cog, guild_id, dashboard, action
        self.select = Select(
            placeholder="Choose a ticket option…",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=str(o.get("label", "Option"))[:100], value=str(o.get("key", "general"))[:100], emoji=o.get("emoji") or None) for o in options[:25]],
        )
        self.select.callback = self.selected
        self.add_item(self.select)

    async def selected(self, interaction: discord.Interaction):
        key = self.select.values[0]
        config = await self.cog.get_config(self.guild_id)
        options = list(config.get("options", []))
        option = next((o for o in options if o.get("key") == key), None)
        if option is None:
            return await interaction.response.send_message(embed=error_embed("That option no longer exists."), ephemeral=True)
        if self.action == "edit":
            await interaction.response.send_modal(TicketOptionModal(self.cog, self.guild_id, self.dashboard, "edit", option))
        else:
            options = [o for o in options if o.get("key") != key]
            await self.cog.save_config_fields(self.guild_id, {"options": options})
            await interaction.response.send_message(embed=success_embed(f"Removed ticket option **{option.get('label', key)}**."), ephemeral=True)
            await self.dashboard.refresh_message()
        self.stop()

    async def on_timeout(self):
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True


class PostTicketPanelModal(Modal, title="Publish Ticket Panel"):
    channel_id = TextInput(label="Text channel ID / mention", required=True, max_length=25)
    title_input = TextInput(label="This panel's title", required=True, max_length=256)
    description_input = TextInput(label="This panel's description", style=discord.TextStyle.paragraph, required=True, max_length=3000)
    button_label = TextInput(label="Create button label", required=True, max_length=80, placeholder="Create Ticket")

    def __init__(self, dashboard: TicketDashboardView):
        super().__init__()
        self.dashboard = dashboard
        c = dashboard.config
        self.title_input.default = str(c.get("panel_title", "Need help?"))[:256]
        self.description_input.default = str(c.get("panel_description", "Choose a category below to open a private support ticket."))[:3000]
        self.button_label.default = str(c.get("panel_button_label", "Create Ticket"))[:80]

    async def on_submit(self, interaction: discord.Interaction):
        channel_ids = _ids(self.channel_id.value)
        channel = interaction.guild.get_channel(channel_ids[0]) if channel_ids and interaction.guild else None
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(embed=error_embed("Choose a text channel ID or mention."), ephemeral=True)
        if not self.dashboard.config.get("options"):
            return await interaction.response.send_message(embed=error_embed("Add at least one ticket option before publishing a panel."), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        label = self.button_label.value.strip() or "Create Ticket"
        embed = make_embed(self.title_input.value, self.description_input.value)
        try:
            message = await channel.send(embed=embed, view=TicketPanelView(self.dashboard.cog, label), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            return await interaction.followup.send(embed=error_embed(f"Could not post the panel: {exc}"), ephemeral=True)
        config = await self.dashboard.cog.get_config(self.dashboard.guild.id)
        panels = list(config.get("panels", []))
        panels.append({"channel_id": channel.id, "message_id": message.id, "title": self.title_input.value, "description": self.description_input.value, "button_label": label, "created_at": int(time.time())})
        await self.dashboard.cog.save_config_fields(self.dashboard.guild.id, {"panels": panels, "panel_button_label": label})
        await interaction.followup.send(embed=success_embed(f"Ticket panel published in {channel.mention}.\n[Jump to panel]({message.jump_url})"), ephemeral=True)
        await self.dashboard.refresh_message()