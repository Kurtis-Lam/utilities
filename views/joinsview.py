import discord
from discord.ui import View, Button, Modal, TextInput, ChannelSelect, RoleSelect

def get_welcome_embed(config: dict) -> discord.Embed:
    embed = discord.Embed(title="👋 Welcome Configuration (Page 1/3)", color=discord.Color.blue())
    channel_str = f"<#{config['welcome_channel']}>" if config.get('welcome_channel') else "Not Set"
    embed.add_field(name="Welcome Channel", value=channel_str, inline=False)
    embed.add_field(name="Use Embed", value="✅ Yes" if config.get('use_embed') else "❌ No", inline=False)
    embed.add_field(name="Current Message", value=f"```\n{config.get('welcome_message', 'Not set')}\n```", inline=False)
    embed.set_footer(text="Placeholders: {mention}, {username}, {display_name}, {server}, {membercount}")
    return embed

def get_greet_embed(config: dict) -> discord.Embed:
    embed = discord.Embed(title="👻 Greet/Ghost-Ping Configuration (Page 2/3)", color=discord.Color.purple())
    channels = config.get('greet_channels', [])
    channels_str = ", ".join([f"<#{c}>" for c in channels]) if channels else "None Set"
    
    embed.description = "When a user joins, the bot will ping them in these channels and delete the message immediately."
    embed.add_field(name="Greet Channels", value=channels_str, inline=False)
    return embed

def get_autorole_embed(config: dict) -> discord.Embed:
    embed = discord.Embed(title="🛡️ Autorole Configuration (Page 3/3)", color=discord.Color.gold())
    roles = config.get('autoroles', [])
    roles_str = ", ".join([f"<@&{r}>" for r in roles]) if roles else "None Set"
    
    embed.description = "When a user joins the server, the bot will automatically assign these roles to them."
    embed.add_field(name="Assigned Autoroles", value=roles_str, inline=False)
    return embed

class WelcomeMessageModal(Modal, title="Set Welcome Message"):
    message_input = TextInput(
        label="Welcome Message",
        style=discord.TextStyle.paragraph,
        placeholder="Welcome {mention} to {server}! We now have {membercount} members.",
        required=True,
        max_length=2000
    )

    def __init__(self, collection, guild_id, view_instance):
        super().__init__()
        self.collection = collection
        self.guild_id = guild_id
        self.view_instance = view_instance

    async def on_submit(self, interaction: discord.Interaction):
        new_message = self.message_input.value
        await self.collection.update_one({"_id": self.guild_id}, {"$set": {"welcome_message": new_message}}, upsert=True)
        self.view_instance.config["welcome_message"] = new_message
        await interaction.response.edit_message(embed=get_welcome_embed(self.view_instance.config), view=self.view_instance)

class WelcomeConfigView(View):
    def __init__(self, collection, config: dict):
        super().__init__(timeout=180)
        self.collection = collection
        self.config = config
        self.guild_id = config["_id"]

    @discord.ui.select(cls=ChannelSelect, channel_types=[discord.ChannelType.text], placeholder="Select a Welcome Channel")
    async def select_welcome_channel(self, interaction: discord.Interaction, select: ChannelSelect):
        channel_id = select.values[0].id
        self.config["welcome_channel"] = channel_id
        await self.collection.update_one({"_id": self.guild_id}, {"$set": {"welcome_channel": channel_id}}, upsert=True)
        await interaction.response.edit_message(embed=get_welcome_embed(self.config), view=self)

    @discord.ui.button(label="Edit Message", style=discord.ButtonStyle.primary, row=1)
    async def edit_message_btn(self, interaction: discord.Interaction, button: Button):
        modal = WelcomeMessageModal(self.collection, self.guild_id, self)
        modal.message_input.default = self.config.get("welcome_message", "")
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Toggle Embed/Text", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_embed_btn(self, interaction: discord.Interaction, button: Button):
        new_val = not self.config.get("use_embed", True)
        self.config["use_embed"] = new_val
        await self.collection.update_one({"_id": self.guild_id}, {"$set": {"use_embed": new_val}}, upsert=True)
        await interaction.response.edit_message(embed=get_welcome_embed(self.config), view=self)

    @discord.ui.button(label="Next Page ➡️ (Greets)", style=discord.ButtonStyle.success, row=2)
    async def next_page_btn(self, interaction: discord.Interaction, button: Button):
        view = GreetConfigView(self.collection, self.config)
        await interaction.response.edit_message(embed=get_greet_embed(self.config), view=view)


class GreetConfigView(View):
    def __init__(self, collection, config: dict):
        super().__init__(timeout=180)
        self.collection = collection
        self.config = config
        self.guild_id = config["_id"]

    @discord.ui.select(
        cls=ChannelSelect, 
        channel_types=[discord.ChannelType.text], 
        placeholder="Select channels for ghost pings", 
        max_values=10
    )
    async def select_greet_channels(self, interaction: discord.Interaction, select: ChannelSelect):
        channel_ids = [channel.id for channel in select.values]
        self.config["greet_channels"] = channel_ids
        await self.collection.update_one({"_id": self.guild_id}, {"$set": {"greet_channels": channel_ids}}, upsert=True)
        await interaction.response.edit_message(embed=get_greet_embed(self.config), view=self)

    @discord.ui.button(label="⬅️ Prev Page (Welcome)", style=discord.ButtonStyle.secondary, row=1)
    async def prev_page_btn(self, interaction: discord.Interaction, button: Button):
        view = WelcomeConfigView(self.collection, self.config)
        await interaction.response.edit_message(embed=get_welcome_embed(self.config), view=view)

    @discord.ui.button(label="Next Page ➡️ (Autoroles)", style=discord.ButtonStyle.success, row=1)
    async def next_page_btn(self, interaction: discord.Interaction, button: Button):
        view = AutoroleConfigView(self.collection, self.config)
        await interaction.response.edit_message(embed=get_autorole_embed(self.config), view=view)


class AutoroleConfigView(View):
    def __init__(self, collection, config: dict):
        super().__init__(timeout=180)
        self.collection = collection
        self.config = config
        self.guild_id = config["_id"]

    @discord.ui.select(
        cls=RoleSelect,
        placeholder="Select roles to automatically assign",
        max_values=10
    )
    async def select_autoroles(self, interaction: discord.Interaction, select: RoleSelect):
        role_ids = [role.id for role in select.values]
        self.config["autoroles"] = role_ids
        await self.collection.update_one({"_id": self.guild_id}, {"$set": {"autoroles": role_ids}}, upsert=True)
        await interaction.response.edit_message(embed=get_autorole_embed(self.config), view=self)

    @discord.ui.button(label="⬅️ Prev Page (Greets)", style=discord.ButtonStyle.secondary, row=1)
    async def prev_page_btn(self, interaction: discord.Interaction, button: Button):
        view = GreetConfigView(self.collection, self.config)
        await interaction.response.edit_message(embed=get_greet_embed(self.config), view=view)