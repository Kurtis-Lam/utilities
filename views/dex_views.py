import io
from typing import Optional

import discord

from views.common_views import BaseLayout, container_from_embed, error_embed


class ShinyView(BaseLayout):
    """Sparkles toggle inside the dex embed: red = normal sprite, green = shiny sprite."""

    def __init__(self, author_id: int, embed: discord.Embed, normal: Optional[tuple], shiny: tuple):
        super().__init__(author_id=author_id, timeout=180)
        self.embed = embed
        self.normal = normal  # (filename, bytes) or None
        self.shiny = shiny  # (filename, bytes)
        self.showing_shiny = False
        self.button = discord.ui.Button(emoji="✨", style=discord.ButtonStyle.danger)
        self.button.callback = self.sparkles
        self.render()

    def render(self) -> None:
        self.clear_items()
        current = self.shiny if self.showing_shiny else self.normal
        image = f"attachment://{current[0]}" if current else None
        self.add_item(
            container_from_embed(self.embed, discord.ui.ActionRow(self.button), image=image)
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                embed=error_embed("This button isn't yours. Run the command yourself."),
                ephemeral=True,
            )
            return False
        if self.message is None:
            self.message = interaction.message
        return True

    async def sparkles(self, interaction: discord.Interaction):
        self.showing_shiny = not self.showing_shiny
        self.button.style = (
            discord.ButtonStyle.success if self.showing_shiny else discord.ButtonStyle.danger
        )
        current = self.shiny if self.showing_shiny else self.normal
        attachments = [discord.File(fp=io.BytesIO(current[1]), filename=current[0])] if current else []
        self.render()
        await interaction.response.edit_message(attachments=attachments, view=self)
