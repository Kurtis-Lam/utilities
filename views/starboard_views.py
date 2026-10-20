from typing import Optional

import discord
from discord.ext import commands

from views.common_views import BaseLayout, container_from_embed, error_embed, success_embed
from views.embeds import BRAND_COLOR


def build_status_embed(channel_id: Optional[str]) -> discord.Embed:
    if channel_id:
        description = f"Starboard Channel is currently set to <#{channel_id}>"
    else:
        description = "No channel configured"
    return discord.Embed(title="Starboard", description=description, color=BRAND_COLOR)


class ChannelSelectView(BaseLayout):
    """Ephemeral panel: pick a channel from a dropdown, then confirm (both inside the embed)."""

    def __init__(self, cog: "Starboard", parent: "StarboardView"):
        super().__init__(timeout=120)
        self.cog = cog
        self.parent = parent
        self.selected: Optional[discord.abc.GuildChannel] = None

        self.select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder="Select a channel...",
            min_values=1,
            max_values=1,
        )
        self.select.callback = self.channel_select
        self.confirm_button = discord.ui.Button(
            label="Confirm", style=discord.ButtonStyle.success, disabled=True
        )
        self.confirm_button.callback = self.confirm
        self.render()

    def render(self) -> None:
        self.clear_items()
        self.add_item(
            container_from_embed(
                self.build_embed(),
                discord.ui.ActionRow(self.select),
                discord.ui.ActionRow(self.confirm_button),
            )
        )

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
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms is None or not perms.administrator:
            await interaction.response.send_message(
                embed=error_embed("Only administrators can set the starboard channel."),
                ephemeral=True,
            )
            return False
        return True

    async def channel_select(self, interaction: discord.Interaction):
        self.selected = self.select.values[0]
        self.confirm_button.disabled = False
        self.render()
        await interaction.response.edit_message(view=self)

    async def confirm(self, interaction: discord.Interaction):
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
        self.clear_items()
        self.add_item(container_from_embed(success_embed(f"Starboard channel set to {channel.mention}.")))
        await interaction.response.edit_message(view=self)
        await self.parent.refresh(interaction.guild)


class StarboardView(BaseLayout):
    """Starboard status with its Configure button inside the embed."""

    def __init__(self, cog: "Starboard", ctx: commands.Context, embed: Optional[discord.Embed] = None):
        super().__init__(author_id=ctx.author.id, timeout=180)
        self.cog = cog
        self.ctx = ctx
        self.embed = embed or build_status_embed(None)
        self.configure_button = discord.ui.Button(
            label="Configure", style=discord.ButtonStyle.primary, emoji="⚙️"
        )
        self.configure_button.callback = self.configure
        self.render()

    def render(self) -> None:
        self.clear_items()
        self.add_item(container_from_embed(self.embed, discord.ui.ActionRow(self.configure_button)))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                embed=error_embed("This menu isn't yours. Run `.starboard` yourself."),
                ephemeral=True,
            )
            return False
        if self.message is None:
            self.message = interaction.message
        return True

    async def configure(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                embed=error_embed("Only administrators can set the starboard channel."),
                ephemeral=True,
            )
        view = ChannelSelectView(self.cog, self)
        await interaction.response.send_message(view=view, ephemeral=True)

    async def refresh(self, guild: discord.Guild):
        if self.message is None:
            return
        config = await self.cog.starboard_collection.find_one({"guild_id": str(guild.id)})
        self.embed = build_status_embed(config.get("channel_id") if config else None)
        self.render()
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass


def build_jump_layout(embed: discord.Embed, jump_url: str, image: Optional[str] = None) -> discord.ui.LayoutView:
    """The catch embed with a single 'Jump to Message' link button inside it."""
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        container_from_embed(
            embed,
            discord.ui.ActionRow(discord.ui.Button(label="Jump to Message", url=jump_url)),
            image=image,
        )
    )
    return view
