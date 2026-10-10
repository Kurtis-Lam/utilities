from typing import Optional

import discord
from discord.ext import commands

from views.common_views import error_embed, success_embed
from views.embeds import BRAND_COLOR


def build_status_embed(channel_id: Optional[str]) -> discord.Embed:
    if channel_id:
        description = f"Starboard Channel is currently set to <#{channel_id}>"
    else:
        description = "No channel configured"
    return discord.Embed(title="Starboard", description=description, color=BRAND_COLOR)


class ChannelSelectView(discord.ui.View):
    """Ephemeral view: pick a channel from a dropdown, then confirm."""

    def __init__(self, cog: "Starboard", parent: "StarboardView"):
        super().__init__(timeout=120)
        self.cog = cog
        self.parent = parent
        self.selected: Optional[discord.abc.GuildChannel] = None

    def build_embed(self) -> discord.Embed:
        if self.selected:
            description = (
                f"Selected: <#{self.selected.id}>\n\n"
                "Press **Confirm** to set it as the starboard channel."
            )
        else:
            description = "Choose the channel notable catches should be posted in."
        return discord.Embed(
            title="Configure Starboard", description=description, color=BRAND_COLOR
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                embed=error_embed("Only administrators can set the starboard channel."),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="Select a channel...",
        min_values=1,
        max_values=1,
    )
    async def channel_select(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ):
        self.selected = select.values[0]
        self.confirm.disabled = False
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, disabled=True)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.guild.get_channel(self.selected.id)
        if channel is None:
            return await interaction.response.send_message(
                embed=error_embed("I can't see that channel."), ephemeral=True
            )

        perms = channel.permissions_for(interaction.guild.me)
        missing = [
            name
            for name, ok in (
                ("View Channel", perms.view_channel),
                ("Send Messages", perms.send_messages),
                ("Embed Links", perms.embed_links),
            )
            if not ok
        ]
        if missing:
            return await interaction.response.send_message(
                embed=error_embed(
                    f"I'm missing **{', '.join(missing)}** in {channel.mention}. "
                    "Fix the permissions or pick another channel."
                ),
                ephemeral=True,
            )

        await self.cog.starboard_collection.update_one(
            {"guild_id": str(interaction.guild.id)},
            {"$set": {"channel_id": str(channel.id)}},
            upsert=True,
        )
        self.stop()
        await interaction.response.edit_message(
            embed=success_embed(f"Starboard channel set to {channel.mention}."),
            view=None,
        )
        await self.parent.refresh(interaction.guild)


class StarboardView(discord.ui.View):
    def __init__(self, cog: "Starboard", ctx: commands.Context):
        super().__init__(timeout=180)
        self.cog = cog
        self.ctx = ctx
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                embed=error_embed("This menu isn't yours. Run `.starboard` yourself."),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Configure", style=discord.ButtonStyle.primary, emoji="⚙️")
    async def configure(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                embed=error_embed("Only administrators can set the starboard channel."),
                ephemeral=True,
            )
        view = ChannelSelectView(self.cog, self)
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True
        )

    async def refresh(self, guild: discord.Guild):
        if self.message is None:
            return
        config = await self.cog.starboard_collection.find_one({"guild_id": str(guild.id)})
        embed = build_status_embed(config.get("channel_id") if config else None)
        try:
            await self.message.edit(embed=embed, view=self)
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def build_jump_view(jump_url: str) -> discord.ui.View:
    """Single link button that sends the reader to the original catch message."""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Jump to Message", url=jump_url))
    return view