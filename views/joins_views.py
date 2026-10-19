from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlparse

import discord
from discord.ui import Button, ChannelSelect, Modal, RoleSelect, TextInput

from views.common_views import EMBED_COLOR, EmbedLayout, make_embed, success_embed, error_embed

TOTAL_PAGES = 3
PLACEHOLDER_HELP = "{mention} {username} {display_name} {server} {membercount} {user_id} {server_id} {avatar} {server_icon} {channel} {created_at}"


async def _delete_after(message: discord.Message, delay: float):
    await asyncio.sleep(max(0.1, min(30.0, float(delay))))
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


def render_template(template: str, guild: discord.Guild | None, member: discord.abc.User | None,
                    channel: discord.abc.GuildChannel | None = None) -> str:
    created_at = getattr(member, "created_at", None)
    values = {
        "{mention}": member.mention if member else "@user",
        "{username}": member.name if member else "user",
        "{display_name}": getattr(member, "display_name", None) or (member.name if member else "user"),
        "{server}": guild.name if guild else "Server",
        "{membercount}": str(guild.member_count or 0) if guild else "0",
        "{user_id}": str(member.id) if member else "0",
        "{server_id}": str(guild.id) if guild else "0",
        "{avatar}": str(member.display_avatar.url) if member else "",
        "{server_icon}": str(guild.icon.url) if guild and guild.icon else "",
        "{channel}": getattr(channel, "mention", "") if channel else "",
        "{created_at}": f"<t:{int(created_at.timestamp())}:D>" if created_at else "",
    }
    for key, value in values.items():
        template = template.replace(key, value)
    return template


def _render_preview(template: str, guild: discord.Guild | None, member: discord.abc.User | None) -> str:
    return render_template(template, guild, member)


def _color_from_config(config: dict) -> discord.Color:
    raw = str(config.get("welcome_embed_color", "0414C7")).strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}", raw):
        return discord.Color(int(raw, 16))
    return EMBED_COLOR


def format_invite_line(info) -> str | None:
    """One line for the welcome embed from a ``JoinInfo`` (cogs/cmds/invites.py). None = nothing to show."""
    if info is None:
        return None
    if info.kind == "vanity":
        return "Joined using the server's vanity URL."
    if info.kind == "invite" and info.inviter_id:
        total = info.stats.total if info.stats else 0
        return f"Invited by <@{info.inviter_id}>, who now has **{total:,}** invite{'s' if total != 1 else ''}."
    return "I couldn't work out who invited them."


def make_member_welcome_embed(
    config: dict,
    guild: discord.Guild | None,
    member: discord.abc.User | None,
    channel: discord.abc.GuildChannel | None = None,
    invite_line: str | None = None,
) -> discord.Embed:
    description = render_template(
        str(config.get("welcome_message", "Welcome {mention} to {server}!")), guild, member, channel
    )
    if invite_line and config.get("welcome_show_inviter", True):
        description = f"{description}\n\n{invite_line}" if description else invite_line
    title_template = str(config.get("welcome_embed_title", "Welcome to {server}!"))
    title = render_template(title_template, guild, member, channel).strip() or None
    embed = discord.Embed(title=title, description=description or None, color=_color_from_config(config))

    author_name_template = str(config.get("welcome_author_name", "{username} joined!"))
    author_name = render_template(author_name_template, guild, member, channel).strip()
    author_icon = render_template(str(config.get("welcome_author_icon_url", "{avatar}")), guild, member, channel).strip()
    if author_name:
        try:
            embed.set_author(name=author_name[:256], icon_url=author_icon if _is_http_url(author_icon) else None)
        except (TypeError, ValueError):
            embed.set_author(name=author_name[:256])

    thumbnail = render_template(str(config.get("welcome_thumbnail_url", "{avatar}")), guild, member, channel).strip()
    image = render_template(str(config.get("welcome_image_url", "")), guild, member, channel).strip()
    if _is_http_url(thumbnail):
        embed.set_thumbnail(url=thumbnail)
    if _is_http_url(image):
        embed.set_image(url=image)

    footer = render_template(str(config.get("welcome_footer", "Welcome to {server}!")), guild, member, channel).strip()
    footer_icon = render_template(str(config.get("welcome_footer_icon_url", "")), guild, member, channel).strip()
    if footer:
        embed.set_footer(text=footer[:2048], icon_url=footer_icon if _is_http_url(footer_icon) else None)

    for field in config.get("welcome_embed_fields", [])[:25]:
        if not isinstance(field, dict):
            continue
        name = render_template(str(field.get("name", "Field")), guild, member, channel).strip() or "Field"
        value = render_template(str(field.get("value", "—")), guild, member, channel).strip() or "—"
        embed.add_field(name=name[:256], value=value[:1024], inline=bool(field.get("inline", False)))
    if config.get("welcome_show_timestamp", True):
        embed.timestamp = discord.utils.utcnow()
    return embed


def _is_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def get_welcome_embed(
    config: dict,
    guild: discord.Guild | None = None,
    member: discord.abc.User | None = None,
) -> discord.Embed:
    embed = make_embed("👋 Welcome", "Configure the welcome message and run live tests.")
    channel_id = config.get("welcome_channel")
    embed.add_field(name="📍 Channel", value=f"<#{channel_id}>" if channel_id else "*Not set*", inline=True)
    embed.add_field(name="📄 Format", value="Embed" if config.get("use_embed", True) else "Plain text", inline=True)
    embed.add_field(name="🔔 Member ping", value="Enabled" if config.get("welcome_ping_member", True) else "Disabled", inline=True)
    embed.add_field(name="📨 Show inviter", value="On" if config.get("welcome_show_inviter", True) else "Off", inline=True)
    message = str(config.get("welcome_message", "Welcome {mention} to {server}!"))
    shown = message if len(message) <= 800 else message[:797] + "…"
    embed.add_field(name="📝 Message template", value=f"```\n{shown}\n```", inline=False)
    if guild or member:
        preview = _render_preview(message, guild, member)
        embed.add_field(name="👀 Preview", value=(preview[:1000] or "*Empty*"), inline=False)
    embed.add_field(name="🖼️ Title", value=str(config.get("welcome_embed_title", "Welcome to {server}!"))[:120], inline=False)
    embed.add_field(name="🎨 Colour", value=f"`#{str(config.get('welcome_embed_color', '0414C7')).lstrip('#')}`", inline=True)
    embed.add_field(name="🧩 Fields", value=f"`{len(config.get('welcome_embed_fields', []))}`", inline=True)
    embed.add_field(name="🕒 Timestamp", value="On" if config.get("welcome_show_timestamp", True) else "Off", inline=True)
    embed.set_footer(text=f"Page 1/{TOTAL_PAGES} • {PLACEHOLDER_HELP}")
    return embed


def get_greet_embed(config: dict) -> discord.Embed:
    embed = make_embed("👻 Greet Pings", "Pings new members in these channels, then deletes the ping.")
    channels = config.get("greet_channels", [])
    embed.add_field(
        name=f"📍 Channels ({len(channels)})",
        value="\n".join(f"<#{c}>" for c in channels) if channels else "*None set*",
        inline=False,
    )
    embed.add_field(name="⏱️ Delete delay", value=f"{float(config.get('greet_delete_delay', 1.0)):g} second(s)", inline=True)
    embed.set_footer(text=f"Page 2/{TOTAL_PAGES} • Clear the selection to turn off")
    return embed


def get_autorole_embed(config: dict) -> discord.Embed:
    embed = make_embed("🛡️ Autoroles", "Given to every new member. Verification roles are protected and cannot be removed here.")
    roles = config.get("autoroles", [])
    protected = {int(x) for x in config.get("protected_autoroles", []) if str(x).isdigit()}
    lines = [f"<@&{role_id}>" + (" 🔒" if role_id in protected else "") for role_id in roles]
    embed.add_field(name=f"🏷️ Roles ({len(roles)})", value="\n".join(lines) if lines else "*None set*", inline=False)
    embed.set_footer(text=f"Page 3/{TOTAL_PAGES} • The bot's highest role must be above these")
    return embed


class WelcomeMessageModal(Modal, title="Edit Welcome Message"):
    message_input = TextInput(
        label="Welcome text template",
        style=discord.TextStyle.paragraph,
        placeholder="Welcome {mention} to {server}! We now have {membercount} members.",
        required=True,
        max_length=2000,
    )

    def __init__(self, collection, guild_id, view_instance):
        super().__init__()
        self.collection, self.guild_id, self.view_instance = collection, guild_id, view_instance

    async def on_submit(self, interaction: discord.Interaction):
        value = self.message_input.value
        await self.collection.update_one({"_id": self.guild_id}, {"$set": {"welcome_message": value}}, upsert=True)
        self.view_instance.config["welcome_message"] = value
        await self.view_instance.push(interaction, get_welcome_embed(self.view_instance.config, interaction.guild, interaction.user))
        await interaction.followup.send(embed=success_embed("Welcome message updated."), ephemeral=True)


class WelcomeStyleModal(Modal, title="Welcome Embed Style"):
    title_input = TextInput(label="Embed title", required=False, max_length=256, placeholder="Welcome to {server}!")
    color_input = TextInput(label="Hex colour (without # is fine)", required=True, max_length=7, placeholder="0414C7")
    footer_input = TextInput(label="Footer text", required=False, max_length=2048, placeholder="Welcome to {server}!")
    thumbnail_input = TextInput(label="Thumbnail URL or {avatar}", required=False, max_length=400, placeholder="{avatar}")
    image_input = TextInput(label="Large image URL", required=False, max_length=400, placeholder="https://example.com/banner.png")

    def __init__(self, view_instance):
        super().__init__()
        self.view_instance = view_instance
        c = view_instance.config
        self.title_input.default = str(c.get("welcome_embed_title", "Welcome to {server}!"))[:256]
        self.color_input.default = str(c.get("welcome_embed_color", "0414C7"))[:7]
        self.footer_input.default = str(c.get("welcome_footer", "Welcome to {server}!"))[:2048]
        self.thumbnail_input.default = str(c.get("welcome_thumbnail_url", "{avatar}"))[:400]
        self.image_input.default = str(c.get("welcome_image_url", ""))[:400]

    async def on_submit(self, interaction: discord.Interaction):
        color = self.color_input.value.strip().lstrip("#")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", color):
            return await interaction.response.send_message(embed=error_embed("Colour must be six hexadecimal digits, such as `0414C7`."), ephemeral=True)
        for label, value in (("thumbnail", self.thumbnail_input.value.strip()), ("image", self.image_input.value.strip())):
            if value and value != "{avatar}" and not _is_http_url(value):
                return await interaction.response.send_message(embed=error_embed(f"{label.title()} must be an HTTP(S) URL or blank."), ephemeral=True)
        values = {
            "welcome_embed_title": self.title_input.value,
            "welcome_embed_color": color.upper(),
            "welcome_footer": self.footer_input.value,
            "welcome_thumbnail_url": self.thumbnail_input.value.strip(),
            "welcome_image_url": self.image_input.value.strip(),
        }
        await self.view_instance.save_values(interaction, values)
        await interaction.followup.send(embed=success_embed("Welcome embed style updated."), ephemeral=True)


class WelcomeAdvancedModal(Modal, title="Advanced Welcome Options"):
    author_input = TextInput(label="Embed author name", required=False, max_length=256, placeholder="{username} joined!")
    author_icon_input = TextInput(label="Author icon URL or {avatar}", required=False, max_length=400, placeholder="{avatar}")
    footer_icon_input = TextInput(label="Footer icon URL", required=False, max_length=400, placeholder="https://example.com/icon.png")
    fields_input = TextInput(label="Embed fields JSON (list)", style=discord.TextStyle.paragraph, required=False, max_length=4000, placeholder='[{"name":"Member","value":"{mention}","inline":true}]')
    delete_delay_input = TextInput(label="Greet delete delay (seconds)", required=True, max_length=4, placeholder="1")

    def __init__(self, view_instance):
        super().__init__()
        self.view_instance = view_instance
        c = view_instance.config
        self.author_input.default = str(c.get("welcome_author_name", "{username} joined!"))[:256]
        self.author_icon_input.default = str(c.get("welcome_author_icon_url", "{avatar}"))[:400]
        self.footer_icon_input.default = str(c.get("welcome_footer_icon_url", ""))[:400]
        self.fields_input.default = json.dumps(c.get("welcome_embed_fields", []), ensure_ascii=False)[:4000]
        self.delete_delay_input.default = str(c.get("greet_delete_delay", 1.0))[:4]

    async def on_submit(self, interaction: discord.Interaction):
        author_icon = self.author_icon_input.value.strip()
        footer_icon = self.footer_icon_input.value.strip()
        for label, value in (("Author icon", author_icon), ("Footer icon", footer_icon)):
            if value and value != "{avatar}" and not _is_http_url(value):
                return await interaction.response.send_message(embed=error_embed(f"{label} must be an HTTP(S) URL, `{{avatar}}`, or blank."), ephemeral=True)
        try:
            fields = json.loads(self.fields_input.value.strip() or "[]")
            if not isinstance(fields, list) or len(fields) > 25:
                raise ValueError("Use a JSON list of no more than 25 fields.")
            sanitized = []
            for field in fields:
                if not isinstance(field, dict) or not str(field.get("name", "")).strip() or not str(field.get("value", "")).strip():
                    raise ValueError("Every field needs a non-empty name and value.")
                sanitized.append({"name": str(field["name"])[:256], "value": str(field["value"])[:1024], "inline": bool(field.get("inline", False))})
            base = self.view_instance.config
            total_text = (
                len(str(base.get("welcome_message", "")))
                + len(str(base.get("welcome_embed_title", "")))
                + len(self.author_input.value)
                + len(str(base.get("welcome_footer", "")))
                + sum(len(field["name"]) + len(field["value"]) for field in sanitized)
            )
            if total_text > 5700:
                raise ValueError("Welcome embed text is too long. Reduce the message, title, footer, or custom fields so the whole embed stays within Discord's character limit.")
            delay = float(self.delete_delay_input.value.strip())
            if not 0.1 <= delay <= 30:
                raise ValueError("Greet delete delay must be between 0.1 and 30 seconds.")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return await interaction.response.send_message(embed=error_embed(f"Invalid settings: {exc}"), ephemeral=True)
        await self.view_instance.save_values(interaction, {
            "welcome_author_name": self.author_input.value,
            "welcome_author_icon_url": author_icon,
            "welcome_footer_icon_url": footer_icon,
            "welcome_embed_fields": sanitized,
            "greet_delete_delay": delay,
        })
        await interaction.followup.send(embed=success_embed("Advanced welcome options saved."), ephemeral=True)


class WelcomeConfigView(EmbedLayout):
    def __init__(self, collection, config: dict, author_id: int | None = None, guild=None, member=None, cog=None):
        super().__init__(get_welcome_embed(config, guild, member), author_id=author_id, timeout=240)
        self.collection, self.config, self.guild_id, self.cog = collection, config, config["_id"], cog
        self.channel_select = ChannelSelect(channel_types=[discord.ChannelType.text], placeholder="📍 Welcome channel")
        self.channel_select.callback = self.select_welcome_channel
        self.edit_message_btn = Button(label="Edit Message", emoji="📝", style=discord.ButtonStyle.primary)
        self.edit_message_btn.callback = self.edit_message_cb
        self.style_btn = Button(label="Embed Style", emoji="🎨", style=discord.ButtonStyle.primary)
        self.style_btn.callback = self.style_cb
        self.advanced_btn = Button(label="Advanced", emoji="🧩", style=discord.ButtonStyle.secondary)
        self.advanced_btn.callback = self.advanced_cb
        self.toggle_embed_btn = Button(label="Switch Format", style=discord.ButtonStyle.secondary)
        self.toggle_embed_btn.callback = self.toggle_embed_cb
        self.toggle_ping_btn = Button(label="Toggle Member Ping", emoji="🔔", style=discord.ButtonStyle.secondary)
        self.toggle_ping_btn.callback = self.toggle_ping_cb
        self.toggle_inviter_btn = Button(label="Toggle Inviter", emoji="📨", style=discord.ButtonStyle.secondary)
        self.toggle_inviter_btn.callback = self.toggle_inviter_cb
        self.test_welcome_btn = Button(label="Test Welcome", emoji="🧪", style=discord.ButtonStyle.success)
        self.test_welcome_btn.callback = self.test_welcome_cb
        self.test_greet_btn = Button(label="Test Greets", emoji="👻", style=discord.ButtonStyle.success)
        self.test_greet_btn.callback = self.test_greets_cb
        self.test_roles_btn = Button(label="Test Autoroles", emoji="🛡️", style=discord.ButtonStyle.success)
        self.test_roles_btn.callback = self.test_roles_cb
        self.next_page_btn = Button(label="Next: Greets", emoji="▶️", style=discord.ButtonStyle.success)
        self.next_page_btn.callback = self.next_page_cb
        self._sync_toggle()
        self.render()

    def rows(self):
        return [
            [self.channel_select],
            [self.edit_message_btn, self.style_btn, self.advanced_btn],
            [self.test_welcome_btn, self.test_greet_btn, self.test_roles_btn],
            [self.toggle_embed_btn, self.toggle_ping_btn, self.toggle_inviter_btn, self.next_page_btn],
        ]

    async def save_values(self, interaction: discord.Interaction, values: dict):
        await self.collection.update_one({"_id": self.guild_id}, {"$set": values}, upsert=True)
        self.config.update(values)
        self._sync_toggle()
        await self.push(interaction, get_welcome_embed(self.config, interaction.guild, interaction.user))

    def _sync_toggle(self):
        use_embed = self.config.get("use_embed", True)
        self.toggle_embed_btn.label = "Switch to Plain Text" if use_embed else "Switch to Embed"
        self.toggle_embed_btn.emoji = "💬" if use_embed else "🖼️"
        self.toggle_ping_btn.label = "Disable Member Ping" if self.config.get("welcome_ping_member", True) else "Enable Member Ping"
        self.toggle_inviter_btn.label = "Hide Inviter" if self.config.get("welcome_show_inviter", True) else "Show Inviter"

    async def select_welcome_channel(self, interaction: discord.Interaction):
        channel_id = self.channel_select.values[0].id
        self.config["welcome_channel"] = channel_id
        await self.collection.update_one({"_id": self.guild_id}, {"$set": {"welcome_channel": channel_id}}, upsert=True)
        await self.push(interaction, get_welcome_embed(self.config, interaction.guild, interaction.user))

    async def edit_message_cb(self, interaction: discord.Interaction):
        modal = WelcomeMessageModal(self.collection, self.guild_id, self)
        modal.message_input.default = str(self.config.get("welcome_message", ""))[:2000]
        await interaction.response.send_modal(modal)

    async def style_cb(self, interaction: discord.Interaction):
        await interaction.response.send_modal(WelcomeStyleModal(self))

    async def advanced_cb(self, interaction: discord.Interaction):
        await interaction.response.send_modal(WelcomeAdvancedModal(self))

    async def toggle_embed_cb(self, interaction: discord.Interaction):
        await self.save_values(interaction, {"use_embed": not self.config.get("use_embed", True)})

    async def toggle_ping_cb(self, interaction: discord.Interaction):
        await self.save_values(interaction, {"welcome_ping_member": not self.config.get("welcome_ping_member", True)})

    async def toggle_inviter_cb(self, interaction: discord.Interaction):
        await self.save_values(interaction, {"welcome_show_inviter": not self.config.get("welcome_show_inviter", True)})

    async def test_welcome_cb(self, interaction: discord.Interaction):
        guild = interaction.guild
        channel = guild.get_channel(self.config.get("welcome_channel")) if guild else None
        if channel is None:
            return await interaction.response.send_message(embed=error_embed("Set a welcome channel first."), ephemeral=True)
        try:
            if self.config.get("use_embed", True):
                await channel.send(
                    content=interaction.user.mention if self.config.get("welcome_ping_member", True) else None,
                    embed=make_member_welcome_embed(self.config, guild, interaction.user, channel, await self._test_invite_line(interaction)),
                    allowed_mentions=discord.AllowedMentions(
                        users=[interaction.user] if self.config.get("welcome_ping_member", True) else [],
                        roles=False, everyone=False, replied_user=False,
                    ),
                )
            else:
                content = render_template(str(self.config.get("welcome_message", "Welcome {mention} to {server}!")), guild, interaction.user, channel)
                await channel.send(
                    content=content,
                    allowed_mentions=discord.AllowedMentions(
                        users=[interaction.user] if self.config.get("welcome_ping_member", True) else [],
                        roles=False, everyone=False, replied_user=False,
                    ),
                )
        except discord.HTTPException as exc:
            return await interaction.response.send_message(embed=error_embed(f"Could not send the test: {exc}"), ephemeral=True)
        await interaction.response.send_message(embed=success_embed(f"Test welcome sent to {channel.mention}. Only you can be pinged by this test, according to the configured ping setting."), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def _test_invite_line(self, interaction: discord.Interaction) -> str | None:
        """Sample inviter line for the test welcome: the person pressing the button is the 'inviter'."""
        if not self.config.get("welcome_show_inviter", True) or interaction.guild is None:
            return None
        tracker = interaction.client.get_cog("Invites")
        if tracker is None:
            return None
        try:
            stats = await tracker.get_stats(interaction.guild.id, interaction.user.id)
        except Exception:
            return None
        from cogs.cmds.invites import JoinInfo
        return format_invite_line(JoinInfo(kind="invite", inviter_id=interaction.user.id, stats=stats))

    async def test_greets_cb(self, interaction: discord.Interaction):
        guild = interaction.guild
        sent, failed = 0, 0
        for channel_id in self.config.get("greet_channels", []):
            channel = guild.get_channel(int(channel_id)) if guild else None
            if channel is None:
                failed += 1
                continue
            try:
                message = await channel.send(
                    interaction.user.mention,
                    allowed_mentions=discord.AllowedMentions(users=[interaction.user], roles=False, everyone=False, replied_user=False),
                )
                asyncio.create_task(_delete_after(message, max(0.1, min(30.0, float(self.config.get("greet_delete_delay", 1.0))))))
                sent += 1
            except discord.HTTPException:
                failed += 1
        await interaction.response.send_message(embed=success_embed(f"Greet test finished: `{sent}` message(s) tested, `{failed}` failed. Only your account was eligible to be pinged; test pings were then deleted using the configured delay."), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def test_roles_cb(self, interaction: discord.Interaction):
        guild = interaction.guild
        roles = [guild.get_role(int(role_id)) for role_id in self.config.get("autoroles", [])] if guild else []
        roles = [role for role in roles if role]
        bot_member = guild.me if guild else None
        unassignable = [role.name for role in roles if bot_member and role >= bot_member.top_role]
        description = "Configured roles: " + (", ".join(role.mention for role in roles) or "none")
        description += "\n" + (f"⚠️ Bot role is not high enough for: {', '.join(unassignable)}" if unassignable else "✅ Bot role hierarchy looks valid for the configured roles.")
        await interaction.response.send_message(embed=make_embed("Autorole Test", description), ephemeral=True)

    async def next_page_cb(self, interaction: discord.Interaction):
        view = GreetConfigView(self.collection, self.config, self.author_id, cog=self.cog)
        await self.swap(interaction, view=view)


class GreetConfigView(EmbedLayout):
    def __init__(self, collection, config: dict, author_id: int | None = None, cog=None):
        super().__init__(get_greet_embed(config), author_id=author_id, timeout=240)
        self.collection, self.config, self.guild_id, self.cog = collection, config, config["_id"], cog
        self.channel_select = ChannelSelect(channel_types=[discord.ChannelType.text], placeholder="📍 Greet channels (max 10)", min_values=0, max_values=10)
        self.channel_select.callback = self.select_greet_channels
        self.test_btn = Button(label="Test Greets", emoji="🧪", style=discord.ButtonStyle.success)
        self.test_btn.callback = self.test_greets_cb
        self.prev_page_btn = Button(label="Back: Welcome", emoji="◀️", style=discord.ButtonStyle.secondary)
        self.prev_page_btn.callback = self.prev_page_cb
        self.next_page_btn = Button(label="Next: Autoroles", emoji="▶️", style=discord.ButtonStyle.success)
        self.next_page_btn.callback = self.next_page_cb
        self.render()

    def rows(self):
        return [[self.channel_select], [self.test_btn, self.prev_page_btn, self.next_page_btn]]

    async def select_greet_channels(self, interaction: discord.Interaction):
        channel_ids = [channel.id for channel in self.channel_select.values]
        self.config["greet_channels"] = channel_ids
        await self.collection.update_one({"_id": self.guild_id}, {"$set": {"greet_channels": channel_ids}}, upsert=True)
        await self.push(interaction, get_greet_embed(self.config))

    async def test_greets_cb(self, interaction: discord.Interaction):
        view = WelcomeConfigView(self.collection, self.config, self.author_id, interaction.guild, interaction.user, self.cog)
        view.message = interaction.message
        await view.test_greets_cb(interaction)

    async def prev_page_cb(self, interaction: discord.Interaction):
        view = WelcomeConfigView(self.collection, self.config, self.author_id, interaction.guild, interaction.user, self.cog)
        await self.swap(interaction, view=view)

    async def next_page_cb(self, interaction: discord.Interaction):
        view = AutoroleConfigView(self.collection, self.config, self.author_id, self.cog)
        await self.swap(interaction, view=view)


class AutoroleConfigView(EmbedLayout):
    def __init__(self, collection, config: dict, author_id: int | None = None, cog=None):
        super().__init__(get_autorole_embed(config), author_id=author_id, timeout=240)
        self.collection, self.config, self.guild_id, self.cog = collection, config, config["_id"], cog
        self.role_select = RoleSelect(placeholder="🏷️ Autoroles (max 10; protected roles stay)", min_values=0, max_values=10)
        self.role_select.callback = self.select_autoroles
        self.prev_page_btn = Button(label="Back: Greets", emoji="◀️", style=discord.ButtonStyle.secondary)
        self.prev_page_btn.callback = self.prev_page_cb
        self.render()

    def rows(self):
        return [[self.role_select], [self.prev_page_btn]]

    async def select_autoroles(self, interaction: discord.Interaction):
        selected = [role.id for role in self.role_select.values]
        protected = [int(x) for x in self.config.get("protected_autoroles", []) if str(x).isdigit()]
        role_ids = list(dict.fromkeys(protected + [role_id for role_id in selected if role_id not in protected]))[:10]
        self.config["autoroles"] = role_ids
        await self.collection.update_one({"_id": self.guild_id}, {"$set": {"autoroles": role_ids}}, upsert=True)
        await self.push(interaction, get_autorole_embed(self.config))

    async def prev_page_cb(self, interaction: discord.Interaction):
        view = GreetConfigView(self.collection, self.config, self.author_id, self.cog)
        await self.swap(interaction, view=view)