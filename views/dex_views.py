import io
from typing import Optional

import discord

from views.embeds import err_embed


class ShinyView(discord.ui.View):
    """Sparkles toggle: red = normal sprite, green = shiny sprite."""

    def __init__(self, author_id: int, embed: discord.Embed, normal: Optional[tuple], shiny: tuple):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.embed = embed
        self.normal = normal  # (filename, bytes) or None
        self.shiny = shiny  # (filename, bytes)
        self.showing_shiny = False
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                embed=err_embed("This button isn't yours. Run the command yourself."),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(emoji="✨", style=discord.ButtonStyle.danger)
    async def sparkles(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.showing_shiny = not self.showing_shiny
        button.style = (
            discord.ButtonStyle.success if self.showing_shiny else discord.ButtonStyle.danger
        )
        current = self.shiny if self.showing_shiny else self.normal
        if current:
            filename, data = current
            self.embed.set_thumbnail(url=f"attachment://{filename}")
            attachments = [discord.File(fp=io.BytesIO(data), filename=filename)]
        else:
            self.embed.set_thumbnail(url=None)
            attachments = []
        await interaction.response.edit_message(
            embed=self.embed, attachments=attachments, view=self
        )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass