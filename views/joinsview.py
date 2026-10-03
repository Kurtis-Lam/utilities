import discord
from discord.ui import Button, ChannelSelect, Modal, RoleSelect, TextInput

from views.common import EmbedLayout, make_embed, success_embed

TOTAL_PAGES = 3
PLACEHOLDER_HELP = "{mention}  {username}  {display_name}  {server}  {membercount}"


# --- Embeds ----------------------------------------------------------------------

def _render_preview(template: str, guild: discord.Guild | None, member: discord.abc.User | None) -> str:
    """Fill the placeholders with real values so admins can see the result."""
    values = {
        "{mention}": member.mention if member else "@user",
        "{username}": member.name if member else "user",
        "{display_name}": getattr(member, "display_name", None) or (member.name if member else "user"),
        "{server}": guild.name if guild else "Server",
        "{membercount}": str(guild.member_count) if guild and guild.member_count else "0",
    }
    for key, val in values.items():
        template = template.replace(key, val)
    return template


def get_welcome_embed(
    config: dict,
    guild: discord.Guild | None = None,
    member: discord.abc.User | None = None,
) -> discord.Embed:
    embed = make_embed(
        "👋 Welcome",
        "Greets new members in a channel.",
    )

    channel_id = config.get("welcome_channel")
    embed.add_field(name="📍 Channel", value=f"<#{channel_id}>" if channel_id else "*Not set*", inline=True)
    embed.add_field(
        name="🎨 Format",
        value="Embed" if config.get("use_embed", True) else "Plain text",
        inline=True,
    )

    message = config.get("welcome_message")
    if message:
        shown = message if len(message) <= 900 else message[:897] + "…"
        embed.add_field(name="📝 Message", value=f"```\n{shown}\n```", inline=False)
        if guild or member:
            preview = _render_preview(message, guild, member)
            if len(preview) > 1000:
                preview = preview[:997] + "…"
            embed.add_field(name="👀 Preview", value=preview, inline=False)
    else:
        embed.add_field(name="📝 Message", value="*Not set*", inline=False)

    embed.set_footer(text=f"Page 1/{TOTAL_PAGES} • {PLACEHOLDER_HELP}")
    return embed


def get_greet_embed(config: dict) -> discord.Embed:
    embed = make_embed(
        "👻 Greet Pings",
        "Pings new members in these channels, then deletes the ping.",
    )
    channels = config.get("greet_channels", [])
    embed.add_field(
        name=f"📍 Channels ({len(channels)})",
        value="\n".join(f"<#{c}>" for c in channels) if channels else "*None set*",
        inline=False,
    )
    embed.set_footer(text=f"Page 2/{TOTAL_PAGES} • Clear the selection to turn off")
    return embed


def get_autorole_embed(config: dict) -> discord.Embed:
    embed = make_embed(
        "🛡️ Autoroles",
        "Given to every new member.",
    )
    roles = config.get("autoroles", [])
    embed.add_field(
        name=f"🏷️ Roles ({len(roles)})",
        value="\n".join(f"<@&{r}>" for r in roles) if roles else "*None set*",
        inline=False,
    )
    embed.set_footer(text=f"Page 3/{TOTAL_PAGES} • My role must be above these")
    return embed


# --- Modal -----------------------------------------------------------------------

class WelcomeMessageModal(Modal, title="Edit Welcome Message"):
    message_input = TextInput(
        label="Welcome Message",
        style=discord.TextStyle.paragraph,
        placeholder="Welcome {mention} to {server}! We now have {membercount} members.",
        required=True,
        max_length=2000,
    )

    def __init__(self, collection, guild_id, view_instance):
        super().__init__()
        self.collection = collection
        self.guild_id = guild_id
        self.view_instance = view_instance

    async def on_submit(self, interaction: discord.Interaction):
        new_message = self.message_input.value
        await self.collection.update_one(
            {"_id": self.guild_id}, {"$set": {"welcome_message": new_message}}, upsert=True
        )
        self.view_instance.config["welcome_message"] = new_message
        await self.view_instance.push(
            interaction, get_welcome_embed(self.view_instance.config, interaction.guild, interaction.user)
        )
        await interaction.followup.send(embed=success_embed("Welcome message updated."), ephemeral=True)


# --- Views -----------------------------------------------------------------------

class WelcomeConfigView(EmbedLayout):
    def __init__(
        self,
        collection,
        config: dict,
        author_id: int | None = None,
        guild: discord.Guild | None = None,
        member: discord.abc.User | None = None,
    ):
        super().__init__(get_welcome_embed(config, guild, member), author_id=author_id, timeout=180)
        self.collection = collection
        self.config = config
        self.guild_id = config["_id"]

        self.channel_select = ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder="📍 Welcome channel",
        )
        self.channel_select.callback = self.select_welcome_channel

        self.edit_message_btn = Button(label="Edit Message", emoji="📝", style=discord.ButtonStyle.primary)
        self.edit_message_btn.callback = self.edit_message_cb

        self.toggle_embed_btn = Button(label="Switch Format", style=discord.ButtonStyle.secondary)
        self.toggle_embed_btn.callback = self.toggle_embed_cb

        self.next_page_btn = Button(label="Next: Greets", emoji="▶️", style=discord.ButtonStyle.success)
        self.next_page_btn.callback = self.next_page_cb

        self._sync_toggle()
        self.render()

    def rows(self):
        return [
            [self.channel_select],
            [self.edit_message_btn, self.toggle_embed_btn],
            [self.next_page_btn],
        ]

    def _sync_toggle(self):
        use_embed = self.config.get("use_embed", True)
        self.toggle_embed_btn.label = "Switch to Plain Text" if use_embed else "Switch to Embed"
        self.toggle_embed_btn.emoji = "💬" if use_embed else "🖼️"

    async def select_welcome_channel(self, interaction: discord.Interaction):
        channel_id = self.channel_select.values[0].id
        self.config["welcome_channel"] = channel_id
        await self.collection.update_one(
            {"_id": self.guild_id}, {"$set": {"welcome_channel": channel_id}}, upsert=True
        )
        await self.push(interaction, get_welcome_embed(self.config, interaction.guild, interaction.user))

    async def edit_message_cb(self, interaction: discord.Interaction):
        modal = WelcomeMessageModal(self.collection, self.guild_id, self)
        modal.message_input.default = self.config.get("welcome_message", "")
        await interaction.response.send_modal(modal)

    async def toggle_embed_cb(self, interaction: discord.Interaction):
        new_val = not self.config.get("use_embed", True)
        self.config["use_embed"] = new_val
        await self.collection.update_one({"_id": self.guild_id}, {"$set": {"use_embed": new_val}}, upsert=True)
        self._sync_toggle()
        await self.push(interaction, get_welcome_embed(self.config, interaction.guild, interaction.user))

    async def next_page_cb(self, interaction: discord.Interaction):
        view = GreetConfigView(self.collection, self.config, self.author_id)
        await self.swap(interaction, view=view)


class GreetConfigView(EmbedLayout):
    def __init__(self, collection, config: dict, author_id: int | None = None):
        super().__init__(get_greet_embed(config), author_id=author_id, timeout=180)
        self.collection = collection
        self.config = config
        self.guild_id = config["_id"]

        self.channel_select = ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder="📍 Greet channels (max 10)",
            min_values=0,
            max_values=10,
        )
        self.channel_select.callback = self.select_greet_channels

        self.prev_page_btn = Button(label="Back: Welcome", emoji="◀️", style=discord.ButtonStyle.secondary)
        self.prev_page_btn.callback = self.prev_page_cb

        self.next_page_btn = Button(label="Next: Autoroles", emoji="▶️", style=discord.ButtonStyle.success)
        self.next_page_btn.callback = self.next_page_cb
        self.render()

    def rows(self):
        return [[self.channel_select], [self.prev_page_btn, self.next_page_btn]]

    async def select_greet_channels(self, interaction: discord.Interaction):
        channel_ids = [channel.id for channel in self.channel_select.values]
        self.config["greet_channels"] = channel_ids
        await self.collection.update_one(
            {"_id": self.guild_id}, {"$set": {"greet_channels": channel_ids}}, upsert=True
        )
        await self.push(interaction, get_greet_embed(self.config))

    async def prev_page_cb(self, interaction: discord.Interaction):
        view = WelcomeConfigView(
            self.collection, self.config, self.author_id, interaction.guild, interaction.user
        )
        await self.swap(interaction, view=view)

    async def next_page_cb(self, interaction: discord.Interaction):
        view = AutoroleConfigView(self.collection, self.config, self.author_id)
        await self.swap(interaction, view=view)


class AutoroleConfigView(EmbedLayout):
    def __init__(self, collection, config: dict, author_id: int | None = None):
        super().__init__(get_autorole_embed(config), author_id=author_id, timeout=180)
        self.collection = collection
        self.config = config
        self.guild_id = config["_id"]

        self.role_select = RoleSelect(
            placeholder="🏷️ Autoroles (max 10)",
            min_values=0,
            max_values=10,
        )
        self.role_select.callback = self.select_autoroles

        self.prev_page_btn = Button(label="Back: Greets", emoji="◀️", style=discord.ButtonStyle.secondary)
        self.prev_page_btn.callback = self.prev_page_cb
        self.render()

    def rows(self):
        return [[self.role_select], [self.prev_page_btn]]

    async def select_autoroles(self, interaction: discord.Interaction):
        role_ids = [role.id for role in self.role_select.values]
        self.config["autoroles"] = role_ids
        await self.collection.update_one({"_id": self.guild_id}, {"$set": {"autoroles": role_ids}}, upsert=True)
        await self.push(interaction, get_autorole_embed(self.config))

    async def prev_page_cb(self, interaction: discord.Interaction):
        view = GreetConfigView(self.collection, self.config, self.author_id)
        await self.swap(interaction, view=view)