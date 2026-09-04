import re
import discord

def parse_ids(text: str) -> list[int]:
    """Extracts numeric IDs from text or mentions separated by commas or whitespace."""
    if not text:
        return []
    return [int(x) for x in re.findall(r"\d+", text)]


class ConfirmReplaceView(discord.ui.View):
    def __init__(self, cog, config_type: str, entry: dict, main_message: discord.Message = None):
        super().__init__(timeout=60)
        self.cog = cog
        self.config_type = config_type
        self.entry = entry
        self.main_message = main_message

    @discord.ui.button(label="Confirm Replace", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = str(interaction.guild_id)
        await self.cog.collection.update_one(
            {"_id": guild_id},
            {"$set": {self.config_type: [self.entry]}},
            upsert=True,
        )

        if self.main_message:
            try:
                embed = await self.cog.build_config_embed(interaction.guild)
                await self.main_message.edit(embed=embed)
            except discord.HTTPException:
                pass

        target_str = f" target <@{self.entry['target']}>" if self.entry.get("target") else ""
        await interaction.response.edit_message(
            content=f"✅ Successfully replaced **{self.config_type}** configuration{target_str}.",
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ Replacement cancelled.", view=None)


class AddConfigModal(discord.ui.Modal):
    def __init__(self, cog, config_type: str):
        super().__init__(title=f"Add {config_type.capitalize()} Autolock")
        self.cog = cog
        self.config_type = config_type

        self.target_input = discord.ui.TextInput(
            label="Target Mention or Role/User ID",
            placeholder="e.g. @Role, @User, or 1234567890 (Optional for rare/regional)",
            required=(config_type == "user"),
        )
        self.add_item(self.target_input)

        self.restrict_input = discord.ui.TextInput(
            label="Restrict Channels / Categories",
            placeholder="Comma separated IDs or mentions (e.g. 101, 102, #spawns)",
            required=False,
        )
        self.add_item(self.restrict_input)

        self.exclude_input = discord.ui.TextInput(
            label="Exclude Channels / Categories",
            placeholder="Comma separated IDs or mentions (e.g. 201, 202, #lounge)",
            required=False,
        )
        self.add_item(self.exclude_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        target_ids = parse_ids(self.target_input.value)

        if self.config_type == "user" and not target_ids:
            return await interaction.response.send_message("❌ Invalid target ID or mention provided.", ephemeral=True)

        target_id = target_ids[0] if target_ids else None
        r_ids = parse_ids(self.restrict_input.value)
        x_ids = parse_ids(self.exclude_input.value)

        # Directly match category IDs against server categories
        category_ids = {cat.id for cat in guild.categories}

        r_cats = [i for i in r_ids if i in category_ids]
        r_chs = [i for i in r_ids if i not in category_ids]

        x_cats = [i for i in x_ids if i in category_ids]
        x_chs = [i for i in x_ids if i not in category_ids]

        entry = {
            "target": target_id,
            "restrict_categories": r_cats,
            "restrict_channels": r_chs,
            "exclude_categories": x_cats,
            "exclude_channels": x_chs,
        }

        guild_id = str(interaction.guild_id)
        guild_data = await self.cog._get_guild_data(guild_id)

        if self.config_type in ("rare", "regional") and guild_data.get(self.config_type):
            view = ConfirmReplaceView(self.cog, self.config_type, entry, main_message=interaction.message)
            return await interaction.response.send_message(
                f"⚠️ A **{self.config_type}** configuration already exists. Do you want to replace it?",
                view=view,
                ephemeral=True,
            )

        if self.config_type in ("rare", "regional"):
            await self.cog.collection.update_one(
                {"_id": guild_id},
                {"$set": {self.config_type: [entry]}},
                upsert=True,
            )
        else:
            await self.cog.collection.update_one(
                {"_id": guild_id},
                {"$push": {self.config_type: entry}},
                upsert=True,
            )

        embed = await self.cog.build_config_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed)
        target_msg = f" for <@{target_id}>" if target_id else ""
        await interaction.followup.send(f"✅ Added {self.config_type} entry{target_msg}.", ephemeral=True)


class RemoveConfigModal(discord.ui.Modal, title="Remove Autolock Entry"):
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
        self.target_input = discord.ui.TextInput(
            label="Config to Remove",
            placeholder="Type 'rare', 'regional', or a User/Role ID",
            required=True,
        )
        self.add_item(self.target_input)

    async def on_submit(self, interaction: discord.Interaction):
        val = self.target_input.value.strip().lower()
        guild_id = str(interaction.guild_id)

        if val in ("rare", "ra"):
            await self.cog.collection.update_one({"_id": guild_id}, {"$set": {"rare": []}}, upsert=True)
            msg = "✅ Cleared **rare** configuration."
        elif val in ("regional", "reg"):
            await self.cog.collection.update_one({"_id": guild_id}, {"$set": {"regional": []}}, upsert=True)
            msg = "✅ Cleared **regional** configuration."
        else:
            ids = parse_ids(val)
            if not ids:
                return await interaction.response.send_message("❌ Invalid entry or ID specified.", ephemeral=True)
            target_id = ids[0]
            guild_data = await self.cog._get_guild_data(guild_id)
            new_users = [e for e in guild_data.get("user", []) if e.get("target") != target_id]
            await self.cog.collection.update_one({"_id": guild_id}, {"$set": {"user": new_users}}, upsert=True)
            msg = f"✅ Removed <@{target_id}> from user autolocks."

        embed = await self.cog.build_config_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed)
        await interaction.followup.send(msg, ephemeral=True)


class SpawnsConfigView(discord.ui.View):
    def __init__(self, cog, author_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Only the command invoker can control this menu.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Add Rare", style=discord.ButtonStyle.primary, emoji="✨")
    async def add_rare(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddConfigModal(self.cog, "rare"))

    @discord.ui.button(label="Add Regional", style=discord.ButtonStyle.primary, emoji="🌍")
    async def add_regional(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddConfigModal(self.cog, "regional"))

    @discord.ui.button(label="Add User", style=discord.ButtonStyle.primary, emoji="👤")
    async def add_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddConfigModal(self.cog, "user"))

    @discord.ui.button(label="Remove Entry", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def remove_config(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RemoveConfigModal(self.cog))

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = await self.cog.build_config_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)