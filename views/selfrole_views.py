from __future__ import annotations

import re

import discord

from views.common_views import error_embed, make_embed, success_embed


_ID_RE = re.compile(r"\d{15,25}")


def selfrole_panel_embed(config: dict, guild: discord.Guild | None) -> discord.Embed:
    embed = make_embed(
        str(config.get("title", "Choose Your Roles"))[:256],
        str(config.get("description", "Use the options below to manage your server roles."))[:4000],
    )
    mappings = config.get("roles", [])
    lines = []
    for item in mappings[:25]:
        role_id = int(item.get("role_id", 0) or 0)
        role = guild.get_role(role_id) if guild else None
        role_label = role.mention if role else f"Deleted role ({role_id})"
        lines.append(f"{item.get('emoji', '▫️')}  {role_label}")
    if not lines:
        embed.add_field(name="Available roles", value="*No roles configured.*", inline=False)
    else:
        for index in range(0, len(lines), 10):
            field_name = "Available roles" if index == 0 else f"Available roles ({index + 1}–{min(index + 10, len(lines))})"
            embed.add_field(name=field_name, value="\n".join(lines[index:index + 10]), inline=False)
    mode = str(config.get("mode", "buttons")).lower()
    embed.set_footer(text="Click a button to toggle a role." if mode == "buttons" else "React with an emoji to get its role; remove your reaction to remove it.")
    return embed


class SelfRolePanelView(discord.ui.View):
    """Persistent button panel for self-assigned roles."""

    def __init__(self, cog, guild_id: int, mappings: list[dict], guild: discord.Guild | None = None):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = int(guild_id)
        for index, mapping in enumerate(mappings[:25]):
            role_id = int(mapping.get("role_id", 0) or 0)
            role = guild.get_role(role_id) if guild else None
            button = discord.ui.Button(
                label=(role.name if role else mapping.get("role_name") or f"Role {role_id}")[:80],
                emoji=mapping.get("emoji") or None,
                style=discord.ButtonStyle.primary,
                custom_id=f"selfrole:{self.guild_id}:{role_id}",
                row=index // 5,
            )

            async def toggle(interaction: discord.Interaction, selected_role_id=role_id):
                if interaction.guild_id != self.guild_id or not isinstance(interaction.user, discord.Member):
                    return await interaction.response.send_message(embed=error_embed("Use this role panel in its server."), ephemeral=True)
                await interaction.response.defer(ephemeral=True, thinking=True)
                result = await self.cog.toggle_selfrole(
                    interaction.user,
                    selected_role_id,
                    panel_message_id=interaction.message.id if interaction.message else None,
                )
                await interaction.followup.send(embed=result, ephemeral=True)

            button.callback = toggle
            self.add_item(button)


class SelfRoleDashboardView(discord.ui.View):
    def __init__(self, cog, guild: discord.Guild, author_id: int, config: dict):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.author_id = author_id
        self.config = config
        self.message: discord.Message | None = None

        configure = discord.ui.Button(label="Configure & Publish", emoji="⚙️", style=discord.ButtonStyle.primary)
        configure.callback = self.configure
        refresh = discord.ui.Button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary)
        refresh.callback = self.refresh
        self.add_item(configure)
        self.add_item(refresh)

    def embed(self) -> discord.Embed:
        channel_id = self.config.get("channel_id")
        message_id = self.config.get("message_id")
        mode = str(self.config.get("mode", "buttons")).title()
        lines = []
        for item in self.config.get("roles", [])[:25]:
            role = self.guild.get_role(int(item.get("role_id", 0) or 0))
            lines.append(f"{item.get('emoji', '▫️')} {role.mention if role else '`Deleted role`'}")
        embed = make_embed("Self-Role Configuration", "Configure one role panel per server. Each emoji is mapped to one role.")
        embed.add_field(name="Panel channel", value=f"<#{channel_id}>" if channel_id else "*Not set*", inline=True)
        embed.add_field(name="Display mode", value=mode, inline=True)
        embed.add_field(name="Panel message", value=f"`{message_id}`" if message_id else "*Not published*", inline=True)
        if not lines:
            embed.add_field(name="Role mappings (0)", value="*No mappings configured.*", inline=False)
        else:
            for index in range(0, len(lines), 10):
                field_name = f"Role mappings ({len(self.config.get('roles', []))})" if index == 0 else f"More mappings ({index + 1}–{min(index + 10, len(lines))})"
                embed.add_field(name=field_name, value="\n".join(lines[index:index + 10]), inline=False)
        embed.set_footer(text="Buttons send private confirmations. Reaction panels toggle roles without posting public messages.")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(embed=error_embed("Only the administrator who opened this panel can configure it."), ephemeral=True)
            return False
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(embed=error_embed("Administrator permission is required."), ephemeral=True)
            return False
        return True

    async def configure(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SelfRoleConfigModal(self))

    async def refresh(self, interaction: discord.Interaction):
        self.config = await self.cog.get_selfrole_config(self.guild.id)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def refresh_message(self):
        if self.message is None:
            return
        self.config = await self.cog.get_selfrole_config(self.guild.id)
        try:
            await self.message.edit(embed=self.embed(), view=self)
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class SelfRoleConfigModal(discord.ui.Modal, title="Configure Self-Role Panel"):
    channel_id = discord.ui.TextInput(label="Panel text channel ID / mention", required=True, max_length=25)
    title_input = discord.ui.TextInput(label="Embed title", required=True, max_length=256)
    description_input = discord.ui.TextInput(label="Embed description", style=discord.TextStyle.paragraph, required=True, max_length=3000)
    mode_input = discord.ui.TextInput(label="Display mode: buttons or reactions", required=True, max_length=12, placeholder="buttons")
    mappings_input = discord.ui.TextInput(
        label="Emoji | role ID (one mapping per line)",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=4000,
        placeholder="🎮 | 123456789012345678\n✨ | 234567890123456789",
    )

    def __init__(self, dashboard: SelfRoleDashboardView):
        super().__init__()
        self.dashboard = dashboard
        config = dashboard.config
        self.channel_id.default = str(config.get("channel_id") or dashboard.guild.system_channel_id or "")[:25]
        self.title_input.default = str(config.get("title", "Choose Your Roles"))[:256]
        self.description_input.default = str(config.get("description", "Use the options below to manage your server roles."))[:3000]
        self.mode_input.default = str(config.get("mode", "buttons"))
        self.mappings_input.default = "\n".join(
            f"{item.get('emoji', '')} | {item.get('role_id', '')}"
            for item in config.get("roles", [])
        )[:4000] or None

    async def on_submit(self, interaction: discord.Interaction):
        raw_channel = self.channel_id.value.strip()
        channel_ids = _ID_RE.findall(raw_channel)
        channel = interaction.guild.get_channel(int(channel_ids[0])) if channel_ids and interaction.guild else None
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(embed=error_embed("Choose a text channel ID or mention."), ephemeral=True)

        mode = self.mode_input.value.strip().lower()
        if mode not in {"buttons", "reactions"}:
            return await interaction.response.send_message(embed=error_embed("Display mode must be `buttons` or `reactions`."), ephemeral=True)

        bot_member = interaction.guild.me if interaction.guild else None
        if bot_member is None:
            return await interaction.response.send_message(embed=error_embed("I could not verify my server permissions. Try again shortly."), ephemeral=True)
        channel_permissions = channel.permissions_for(bot_member)
        if not channel_permissions.view_channel or not channel_permissions.send_messages or not channel_permissions.embed_links:
            return await interaction.response.send_message(embed=error_embed("I need View Channel, Send Messages and Embed Links permission in the selected channel."), ephemeral=True)
        if mode == "reactions" and not channel_permissions.add_reactions:
            return await interaction.response.send_message(embed=error_embed("Reaction mode requires Add Reactions permission in the selected channel."), ephemeral=True)

        mappings = []
        seen_roles = set()
        seen_emojis = set()
        for line_number, line in enumerate(self.mappings_input.value.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            if "|" in line:
                emoji_text, role_text = (part.strip() for part in line.split("|", 1))
            elif "=" in line:
                emoji_text, role_text = (part.strip() for part in line.split("=", 1))
            else:
                return await interaction.response.send_message(embed=error_embed(f"Mapping on line {line_number} must use `emoji | role ID`."), ephemeral=True)
            role_ids = _ID_RE.findall(role_text)
            if not emoji_text or not role_ids:
                return await interaction.response.send_message(embed=error_embed(f"Mapping on line {line_number} needs an emoji and a role ID/mention."), ephemeral=True)
            try:
                emoji = discord.PartialEmoji.from_str(emoji_text)
            except (TypeError, ValueError):
                return await interaction.response.send_message(embed=error_embed(f"The emoji on line {line_number} is invalid."), ephemeral=True)
            emoji_value = str(emoji)
            if emoji.id is not None and interaction.guild.get_emoji(emoji.id) is None and not channel_permissions.use_external_emojis:
                return await interaction.response.send_message(embed=error_embed(f"The custom emoji on line {line_number} is external; grant Use External Emojis or use a server emoji."), ephemeral=True)
            role_id = int(role_ids[0])
            role = interaction.guild.get_role(role_id)
            if role is None or role.is_default() or role.managed:
                return await interaction.response.send_message(embed=error_embed(f"The role on line {line_number} is unavailable or cannot be self-assigned."), ephemeral=True)
            if bot_member is None or role >= bot_member.top_role:
                return await interaction.response.send_message(embed=error_embed(f"Move my bot role above **{role.name}** before configuring it."), ephemeral=True)
            if role_id in seen_roles:
                return await interaction.response.send_message(embed=error_embed(f"Role **{role.name}** is mapped more than once."), ephemeral=True)
            if emoji_value in seen_emojis:
                return await interaction.response.send_message(embed=error_embed(f"Emoji `{emoji_value}` is mapped more than once."), ephemeral=True)
            seen_roles.add(role_id)
            seen_emojis.add(emoji_value)
            mappings.append({"emoji": emoji_value, "role_id": role_id, "role_name": role.name})

        limit = 25 if mode == "buttons" else 20
        if not mappings:
            return await interaction.response.send_message(embed=error_embed("Add at least one emoji-to-role mapping."), ephemeral=True)
        if len(mappings) > limit:
            return await interaction.response.send_message(embed=error_embed(f"{mode.title()} mode supports up to {limit} role mappings."), ephemeral=True)

        config = {
            "_id": interaction.guild.id,
            "channel_id": channel.id,
            "title": self.title_input.value.strip() or "Choose Your Roles",
            "description": self.description_input.value.strip(),
            "mode": mode,
            "roles": mappings,
            "message_id": self.dashboard.config.get("message_id"),
            "previous_channel_id": self.dashboard.config.get("channel_id"),
        }
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            message = await self.dashboard.cog.publish_selfrole_panel(interaction.guild, config)
        except (discord.HTTPException, ValueError) as exc:
            return await interaction.followup.send(embed=error_embed(f"Could not publish the self-role panel: {exc}"), ephemeral=True)
        await interaction.followup.send(embed=success_embed(f"Self-role panel published in {channel.mention}.\n[Jump to panel]({message.jump_url})"), ephemeral=True)
        self.dashboard.config = await self.dashboard.cog.get_selfrole_config(interaction.guild.id)
        await self.dashboard.refresh_message()
