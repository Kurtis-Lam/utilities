import discord


class CategorySelectView(discord.ui.View):

    def __init__(self, author: discord.Member | discord.User, categories: list):
        super().__init__(timeout=30.0)
        self.author = author
        self.selected_category: discord.CategoryChannel | None = None

        options = [
            discord.SelectOption(
                label=cat.name, value=str(cat.id), description=f"ID: {cat.id}"
            )
            for cat in categories[:25]
        ]
        self.select = discord.ui.Select(
            placeholder="Choose a category...", options=options
        )
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user != self.author:
            return await interaction.response.send_message(
                "You cannot use this selection.", ephemeral=True
            )
        cat_id = int(self.select.values[0])
        self.selected_category = interaction.guild.get_channel(cat_id)
        await interaction.response.defer()
        self.stop()

    async def on_timeout(self):
        self.stop()