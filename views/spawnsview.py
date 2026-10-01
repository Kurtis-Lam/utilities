import re

import discord

from views.common import (
    BaseView,
    error_embed,
    success_embed,
    themed,
    warning_embed,
)


def parse_ids(text: str) -> list[int]:
    """Extracts numeric IDs from text or mentions separated by commas or whitespace."""
    if not text:
        return []
    return [int(x) for x in re.findall(r"\d+", text)]


def mention_target(guild: discord.Guild | None, target_id: int | None) -> str:
    """Role IDs become role mentions, everything else is treated as a user."""
    if not target_id:
        return "*Everyone*"
    if guild and guild.get_role(target_id):
        return f"<@&{target_id}>"
    return f"<@{target_id}>"


def describe_entry(guild: discord.Guild | None, entry: dict) -> str:
    """Short, readable summary of an autolock entry."""
    lines = [f"**Target:** {mention_target(guild, entry.get('target'))}"]
    for label, key in (
        ("Restricted to categories", "restrict_categories"),
        ("Restricted to channels", "restrict_channels"),
        ("Excluded categories", "exclude_categories"),
        ("Excluded channels", "exclude_channels"),
    ):
        ids = entry.get(key) or []
        if ids:
            lines.append(f"**{label}:** " + ", ".join(f"<#{i}>" for i in ids))
    if len(lines) == 1:
        lines.append("*No channel restrictions or exclusions.*")
    return "\n".join(lines)


# --- Confirm replace -----------------------------------------------------------

class ConfirmReplaceView(BaseView):
    def __init__(self, cog, config_type: str, entry: dict, main_message: discord.Message = None, author_id: int | None = None):
        super().__init__(author_id=author_id, timeout=60)
        self.cog = cog
        self.config_type = config_type
        self.entry = entry
        self.main_message = main_message

    @discord.ui.button(label="Replace", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = str(interaction.guild_id)
        await self.cog.collection.update_one(
            {"_id": guild_id},
            {"$set": {self.config_type: [self.entry]}},
            upsert=True,
        )

        if self.main_message:
            try:
                embed = themed(await self.cog.build_config_embed(interaction.guild))
                await self.main_message.edit(embed=embed)
            except discord.HTTPException:
                pass

        self.superseded = True
        embed = success_embed(f"Replaced the **{self.config_type}** configuration.")
        embed.description += "\n\n" + describe_entry(interaction.guild, self.entry)
        await interaction.response.edit_message(content=None, embed=themed(embed), view=None)
        self.stop()

    @discord.ui.button(label="Keep Existing", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.superseded = True
        await interaction.response.edit_message(
            content=None,
            embed=themed(warning_embed("Cancelled. Your existing configuration was not changed.")),
            view=None,
        )
        self.stop()


# --- Modals --------------------------------------------------------------------

class AddConfigModal(discord.ui.Modal):
    def __init__(self, cog, config_type: str):
        super().__init__(title=f"Add {config_type.capitalize()} Autolock")
        self.cog = cog
        self.config_type = config_type

        self.target_input = discord.ui.TextInput(
            label="Target (role or user)",
            placeholder="@Role, @User or ID" + ("" if config_type == "user" else " — optional"),
            required=(config_type == "user"),
        )
        self.add_item(self.target_input)

        self.restrict_input = discord.ui.TextInput(
            label="Only these channels / categories",
            placeholder="Optional. IDs or mentions, separated by commas",
            required=False,
        )
        self.add_item(self.restrict_input)

        self.exclude_input = discord.ui.TextInput(
            label="Skip these channels / categories",
            placeholder="Optional. IDs or mentions, separated by commas",
            required=False,
        )
        self.add_item(self.exclude_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        target_ids = parse_ids(self.target_input.value)

        if self.config_type == "user" and not target_ids:
            return await interaction.response.send_message(
                embed=error_embed("That doesn't look like a valid user or role. Use a mention or an ID."),
                ephemeral=True,
            )

        target_id = target_ids[0] if target_ids else None
        r_ids = parse_ids(self.restrict_input.value)
        x_ids = parse_ids(self.exclude_input.value)

        # Directly match category IDs against server categories
        category_ids = {cat.id for cat in guild.categories}

        entry = {
            "target": target_id,
            "restrict_categories": [i for i in r_ids if i in category_ids],
            "restrict_channels": [i for i in r_ids if i not in category_ids],
            "exclude_categories": [i for i in x_ids if i in category_ids],
            "exclude_channels": [i for i in x_ids if i not in category_ids],
        }

        guild_id = str(interaction.guild_id)
        guild_data = await self.cog._get_guild_data(guild_id)

        if self.config_type in ("rare", "regional") and guild_data.get(self.config_type):
            view = ConfirmReplaceView(
                self.cog,
                self.config_type,
                entry,
                main_message=interaction.message,
                author_id=interaction.user.id,
            )
            embed = warning_embed(
                f"A **{self.config_type}** configuration already exists. "
                "Only one can be active, so adding this will replace it.",
                title="Replace existing configuration?",
            )
            embed.add_field(name="New configuration", value=describe_entry(guild, entry), inline=False)
            return await interaction.response.send_message(embed=themed(embed), view=view, ephemeral=True)

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

        embed = themed(await self.cog.build_config_embed(interaction.guild))
        await interaction.response.edit_message(embed=embed)

        confirm = success_embed(f"Added a **{self.config_type}** autolock entry.")
        confirm.description += "\n\n" + describe_entry(guild, entry)
        await interaction.followup.send(embed=themed(confirm), ephemeral=True)


class RemoveConfigModal(discord.ui.Modal, title="Remove Autolock Entry"):
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
        self.target_input = discord.ui.TextInput(
            label="What do you want to remove?",
            placeholder="rare, regional, or a user / role ID",
            required=True,
        )
        self.add_item(self.target_input)

    async def on_submit(self, interaction: discord.Interaction):
        val = self.target_input.value.strip().lower()
        guild_id = str(interaction.guild_id)

        if val in ("rare", "ra"):
            await self.cog.collection.update_one({"_id": guild_id}, {"$set": {"rare": []}}, upsert=True)
            msg = "Cleared the **rare** configuration."
        elif val in ("regional", "reg"):
            await self.cog.collection.update_one({"_id": guild_id}, {"$set": {"regional": []}}, upsert=True)
            msg = "Cleared the **regional** configuration."
        else:
            ids = parse_ids(val)
            if not ids:
                return await interaction.response.send_message(
                    embed=error_embed("Type `rare`, `regional`, or a valid user / role ID."),
                    ephemeral=True,
                )
            target_id = ids[0]
            guild_data = await self.cog._get_guild_data(guild_id)
            current = guild_data.get("user", [])
            new_users = [e for e in current if e.get("target") != target_id]

            if len(new_users) == len(current):
                return await interaction.response.send_message(
                    embed=warning_embed(f"No user autolock found for {mention_target(interaction.guild, target_id)}."),
                    ephemeral=True,
                )

            await self.cog.collection.update_one({"_id": guild_id}, {"$set": {"user": new_users}}, upsert=True)
            msg = f"Removed {mention_target(interaction.guild, target_id)} from user autolocks."

        embed = themed(await self.cog.build_config_embed(interaction.guild))
        await interaction.response.edit_message(embed=embed)
        await interaction.followup.send(embed=success_embed(msg), ephemeral=True)


# --- Main view -----------------------------------------------------------------

class SpawnsConfigView(BaseView):
    def __init__(self, cog, author_id: int):
        super().__init__(author_id=author_id, timeout=180)
        self.cog = cog

    # Row 0: add entries
    @discord.ui.button(label="Add Rare", style=discord.ButtonStyle.primary, emoji="✨", row=0)
    async def add_rare(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddConfigModal(self.cog, "rare"))

    @discord.ui.button(label="Add Regional", style=discord.ButtonStyle.primary, emoji="🌍", row=0)
    async def add_regional(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddConfigModal(self.cog, "regional"))

    @discord.ui.button(label="Add User", style=discord.ButtonStyle.primary, emoji="👤", row=0)
    async def add_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddConfigModal(self.cog, "user"))

    # Row 1: manage
    @discord.ui.button(label="Remove Entry", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def remove_config(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RemoveConfigModal(self.cog))

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄", row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = themed(await self.cog.build_config_embed(interaction.guild))
        await interaction.response.edit_message(embed=embed, view=self)