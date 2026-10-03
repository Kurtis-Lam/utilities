import re

import discord

from views.common import (
    EmbedLayout,
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
        ("Only categories", "restrict_categories"),
        ("Only channels", "restrict_channels"),
        ("Skip categories", "exclude_categories"),
        ("Skip channels", "exclude_channels"),
    ):
        ids = entry.get(key) or []
        if ids:
            lines.append(f"**{label}:** " + ", ".join(f"<#{i}>" for i in ids))
    if len(lines) == 1:
        lines.append("*No restrictions.*")
    return "\n".join(lines)


# --- Confirm replace -----------------------------------------------------------

class ConfirmReplaceView(EmbedLayout):
    def __init__(
        self,
        cog,
        config_type: str,
        entry: dict,
        embed: discord.Embed,
        main_message: discord.Message = None,
        main_view: "SpawnsConfigView" = None,
        author_id: int | None = None,
    ):
        super().__init__(embed, author_id=author_id, timeout=60)
        self.cog = cog
        self.config_type = config_type
        self.entry = entry
        self.main_message = main_message
        self.main_view = main_view
        self.finished = False

        self.confirm_button = discord.ui.Button(label="Replace", style=discord.ButtonStyle.danger)
        self.confirm_button.callback = self.confirm
        self.cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        self.cancel_button.callback = self.cancel
        self.render()

    def rows(self):
        return [] if self.finished else [[self.confirm_button, self.cancel_button]]

    async def confirm(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild_id)
        await self.cog.collection.update_one(
            {"_id": guild_id},
            {"$set": {self.config_type: [self.entry]}},
            upsert=True,
        )

        if self.main_message and self.main_view:
            try:
                self.main_view.set_embed(await self.cog.build_config_embed(interaction.guild))
                await self.main_message.edit(view=self.main_view)
            except discord.HTTPException:
                pass

        self.superseded = True
        self.finished = True
        result = success_embed(f"Replaced **{self.config_type}**.")
        result.description += "\n\n" + describe_entry(interaction.guild, self.entry)
        await self.push(interaction, result)
        self.stop()

    async def cancel(self, interaction: discord.Interaction):
        self.superseded = True
        self.finished = True
        await self.push(interaction, warning_embed("Cancelled."))
        self.stop()


# --- Modals --------------------------------------------------------------------

class AddConfigModal(discord.ui.Modal):
    def __init__(self, cog, config_type: str, parent_view: "SpawnsConfigView"):
        super().__init__(title=f"Add {config_type.capitalize()} Autolock")
        self.cog = cog
        self.config_type = config_type
        self.parent_view = parent_view

        self.target_input = discord.ui.TextInput(
            label="Target (role or user)",
            placeholder="@Role, @User or ID" + ("" if config_type == "user" else " (optional)"),
            required=(config_type == "user"),
        )
        self.add_item(self.target_input)

        self.restrict_input = discord.ui.TextInput(
            label="Only in (IDs)",
            placeholder="Optional, comma separated",
            required=False,
        )
        self.add_item(self.restrict_input)

        self.exclude_input = discord.ui.TextInput(
            label="Skip (IDs)",
            placeholder="Optional, comma separated",
            required=False,
        )
        self.add_item(self.exclude_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        target_ids = parse_ids(self.target_input.value)

        if self.config_type == "user" and not target_ids:
            return await interaction.response.send_message(
                embed=error_embed("Invalid user or role."),
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
            embed = warning_embed(f"A **{self.config_type}** config exists. Replace it?")
            embed.add_field(name="New", value=describe_entry(guild, entry), inline=False)
            view = ConfirmReplaceView(
                self.cog,
                self.config_type,
                entry,
                embed,
                main_message=interaction.message,
                main_view=self.parent_view,
                author_id=interaction.user.id,
            )
            return await interaction.response.send_message(view=view, ephemeral=True)

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

        await self.parent_view.push(interaction, await self.cog.build_config_embed(interaction.guild))

        confirm = success_embed(f"Added **{self.config_type}** entry.")
        confirm.description += "\n\n" + describe_entry(guild, entry)
        await interaction.followup.send(embed=themed(confirm), ephemeral=True)


class RemoveConfigModal(discord.ui.Modal, title="Remove Autolock Entry"):
    def __init__(self, cog, parent_view: "SpawnsConfigView"):
        super().__init__()
        self.cog = cog
        self.parent_view = parent_view
        self.target_input = discord.ui.TextInput(
            label="Remove",
            placeholder="rare, regional or an ID",
            required=True,
        )
        self.add_item(self.target_input)

    async def on_submit(self, interaction: discord.Interaction):
        val = self.target_input.value.strip().lower()
        guild_id = str(interaction.guild_id)

        if val in ("rare", "ra"):
            await self.cog.collection.update_one({"_id": guild_id}, {"$set": {"rare": []}}, upsert=True)
            msg = "Cleared **rare**."
        elif val in ("regional", "reg"):
            await self.cog.collection.update_one({"_id": guild_id}, {"$set": {"regional": []}}, upsert=True)
            msg = "Cleared **regional**."
        else:
            ids = parse_ids(val)
            if not ids:
                return await interaction.response.send_message(
                    embed=error_embed("Enter rare, regional or an ID."),
                    ephemeral=True,
                )
            target_id = ids[0]
            guild_data = await self.cog._get_guild_data(guild_id)
            current = guild_data.get("user", [])
            new_users = [e for e in current if e.get("target") != target_id]

            if len(new_users) == len(current):
                return await interaction.response.send_message(
                    embed=warning_embed(f"No autolock for {mention_target(interaction.guild, target_id)}."),
                    ephemeral=True,
                )

            await self.cog.collection.update_one({"_id": guild_id}, {"$set": {"user": new_users}}, upsert=True)
            msg = f"Removed {mention_target(interaction.guild, target_id)}."

        await self.parent_view.push(interaction, await self.cog.build_config_embed(interaction.guild))
        await interaction.followup.send(embed=success_embed(msg), ephemeral=True)


# --- Main view -----------------------------------------------------------------

class SpawnsConfigView(EmbedLayout):
    def __init__(self, cog, author_id: int, embed: discord.Embed | None = None):
        super().__init__(embed, author_id=author_id, timeout=180)
        self.cog = cog

        self.add_rare_btn = discord.ui.Button(label="Add Rare", style=discord.ButtonStyle.primary, emoji="✨")
        self.add_rare_btn.callback = self.add_rare
        self.add_regional_btn = discord.ui.Button(label="Add Regional", style=discord.ButtonStyle.primary, emoji="🌍")
        self.add_regional_btn.callback = self.add_regional
        self.add_user_btn = discord.ui.Button(label="Add User", style=discord.ButtonStyle.primary, emoji="👤")
        self.add_user_btn.callback = self.add_user
        self.remove_btn = discord.ui.Button(label="Remove Entry", style=discord.ButtonStyle.danger, emoji="🗑️")
        self.remove_btn.callback = self.remove_config
        self.refresh_btn = discord.ui.Button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄")
        self.refresh_btn.callback = self.refresh
        self.render()

    def rows(self):
        return [
            [self.add_rare_btn, self.add_regional_btn, self.add_user_btn],
            [self.remove_btn, self.refresh_btn],
        ]

    async def add_rare(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddConfigModal(self.cog, "rare", self))

    async def add_regional(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddConfigModal(self.cog, "regional", self))

    async def add_user(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddConfigModal(self.cog, "user", self))

    async def remove_config(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RemoveConfigModal(self.cog, self))

    async def refresh(self, interaction: discord.Interaction):
        await self.push(interaction, await self.cog.build_config_embed(interaction.guild))