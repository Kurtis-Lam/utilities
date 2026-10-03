import discord

try:
    from views.common import warning_embed
except ImportError:
    def warning_embed(description: str) -> discord.Embed:
        return discord.Embed(title="⚠️ Warning", description=description, color=discord.Color.gold())


class PaginatorView(discord.ui.View):
    """
    Interactive pagination view for browsing through a list of discord.Embed pages.
    Supports left/right arrow navigation, user interaction checks, and button disable on timeout.
    """

    def __init__(
        self,
        pages: list[discord.Embed],
        user_id: int | None = None,
        timeout: float = 180.0,
        show_first_last: bool = False,
    ):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.user_id = user_id
        self.current_page = 0
        self.message: discord.Message | None = None

        if show_first_last and len(pages) > 2:
            self.first_button = discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                emoji="⏮️",
                custom_id="nav_first",
                disabled=True,
            )
            self.first_button.callback = self._on_first_click
            self.add_item(self.first_button)

        self.prev_button = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            emoji="◀️",
            custom_id="nav_prev",
            disabled=True,
        )
        self.prev_button.callback = self._on_prev_click
        self.add_item(self.prev_button)

        self.next_button = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            emoji="▶️",
            custom_id="nav_next",
            disabled=(len(pages) <= 1),
        )
        self.next_button.callback = self._on_next_click
        self.add_item(self.next_button)

        if show_first_last and len(pages) > 2:
            self.last_button = discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                emoji="⏭️",
                custom_id="nav_last",
                disabled=(len(pages) <= 1),
            )
            self.last_button.callback = self._on_last_click
            self.add_item(self.last_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.user_id is not None and interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=warning_embed("Not your menu."),
                ephemeral=True,
            )
            return False
        return True

    def _update_buttons(self):
        is_first = self.current_page == 0
        is_last = self.current_page == len(self.pages) - 1

        self.prev_button.disabled = is_first
        self.next_button.disabled = is_last

        if hasattr(self, "first_button"):
            self.first_button.disabled = is_first
        if hasattr(self, "last_button"):
            self.last_button.disabled = is_last

    async def _on_first_click(self, interaction: discord.Interaction):
        self.current_page = 0
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    async def _on_prev_click(self, interaction: discord.Interaction):
        if self.current_page > 0:
            self.current_page -= 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    async def _on_next_click(self, interaction: discord.Interaction):
        if self.current_page < len(self.pages) - 1:
            self.current_page += 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    async def _on_last_click(self, interaction: discord.Interaction):
        self.current_page = len(self.pages) - 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    async def on_timeout(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass


# Aliases for compatibility across different import naming conventions
PaginatedView = PaginatorView
NavigationView = PaginatorView
NavigateView = PaginatorView
Paginator = PaginatorView