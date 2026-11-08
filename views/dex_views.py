import io
from typing import Optional

import discord

from views.common_views import error_embed


class ShinyView(discord.ui.View):
    """Classic-embed shiny toggle, keeping the Pokédex's three-column field layout."""

    def __init__(self, author_id: int, embed: discord.Embed, normal: Optional[tuple], shiny: tuple):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.embed = embed.copy()
        self.normal = normal
        self.shiny = shiny
        self.showing_shiny = False
        self.button = discord.ui.Button(emoji="✨", style=discord.ButtonStyle.danger)
        self.button.callback = self.sparkles
        self.add_item(self.button)

    def render_embed(self) -> discord.Embed:
        rendered = self.embed.copy()
        current = self.shiny if self.showing_shiny else self.normal
        if current:
            rendered.set_thumbnail(url=f"attachment://{current[0]}")
        else:
            rendered.remove_thumbnail()
        return rendered

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
        await interaction.response.edit_message(
            embed=self.render_embed(),
            attachments=attachments,
            view=self,
        )

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
