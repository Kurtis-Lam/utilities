import discord

from views.common import BaseView

MAX_OPTIONS = 25  # Discord's hard limit for a select menu


class CategorySelectView(BaseView):
    """Dropdown that lets the invoker pick a server category.

    After ``await view.wait()``, ``view.selected_category`` holds the chosen
    ``CategoryChannel`` (or ``None`` if nothing was picked / it timed out).
    ``view.truncated`` is True when the server has more than 25 categories and
    only the first 25 could be listed.
    """

    def __init__(self, author: discord.Member | discord.User, categories: list):
        super().__init__(author_id=author.id, timeout=30.0)
        self.author = author
        self.selected_category: discord.CategoryChannel | None = None
        self.truncated = len(categories) > MAX_OPTIONS

        options = [
            discord.SelectOption(
                label=cat.name[:100],
                value=str(cat.id),
                description=f"ID: {cat.id}",
                emoji="📁",
            )
            for cat in categories[:MAX_OPTIONS]
        ]

        if options:
            self.select = discord.ui.Select(placeholder="📁 Choose a category…", options=options)
        else:
            # A select menu can't be empty, so show a disabled placeholder instead.
            self.select = discord.ui.Select(
                placeholder="No categories found in this server",
                options=[discord.SelectOption(label="No categories", value="0")],
                disabled=True,
            )
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, interaction: discord.Interaction):
        cat_id = int(self.select.values[0])
        self.selected_category = interaction.guild.get_channel(cat_id)

        # Lock the menu and show what was picked so the choice is obvious.
        chosen = self.selected_category.name if self.selected_category else "Unknown"
        self.select.placeholder = f"✅ Selected: {chosen}"[:150]
        self.disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()