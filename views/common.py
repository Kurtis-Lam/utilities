import discord

class ConfirmView(discord.ui.View):

    def __init__(self, author: discord.Member | discord.User):
        super().__init__(timeout=30.0)
        self.author = author
        self.value: bool | None = None

    @discord.ui.button(
        label="Confirm", style=discord.ButtonStyle.green, emoji="✅"
    )
    async def confirm_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user != self.author:
            return await interaction.response.send_message(
                "You cannot use this confirmation.", ephemeral=True
            )
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(
        label="Cancel", style=discord.ButtonStyle.red, emoji="❌"
    )
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user != self.author:
            return await interaction.response.send_message(
                "You cannot use this confirmation.", ephemeral=True
            )
        self.value = False
        await interaction.response.defer()
        self.stop()

    async def on_timeout(self):
        self.value = None
        self.stop()