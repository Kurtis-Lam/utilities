import discord

from views.common_views import BaseLayout, container_from_embed


class PaginatorView(BaseLayout):
    """
    Interactive pagination for browsing through a list of discord.Embed pages.

    The page and its arrow buttons live together inside one embed-style container
    (Components V2), so send it with ``await ctx.reply(view=view, mention_author=False)``
    and set ``view.message`` to the returned message.
    Supports left/right (and optionally first/last) navigation, user checks and button disable on timeout.
    """

    def __init__(
        self,
        pages: list[discord.Embed],
        user_id: int | None = None,
        timeout: float = 180.0,
        show_first_last: bool = False,
    ):
        super().__init__(author_id=user_id, timeout=timeout)
        self.pages = pages
        self.user_id = user_id
        self.current_page = 0

        self.first_button: discord.ui.Button | None = None
        self.last_button: discord.ui.Button | None = None

        if show_first_last and len(pages) > 2:
            self.first_button = discord.ui.Button(
                style=discord.ButtonStyle.secondary, emoji="⏮️", disabled=True
            )
            self.first_button.callback = self._on_first_click

        self.prev_button = discord.ui.Button(
            style=discord.ButtonStyle.secondary, emoji="◀️", disabled=True
        )
        self.prev_button.callback = self._on_prev_click

        self.next_button = discord.ui.Button(
            style=discord.ButtonStyle.secondary, emoji="▶️", disabled=(len(pages) <= 1)
        )
        self.next_button.callback = self._on_next_click

        if show_first_last and len(pages) > 2:
            self.last_button = discord.ui.Button(
                style=discord.ButtonStyle.secondary, emoji="⏭️", disabled=(len(pages) <= 1)
            )
            self.last_button.callback = self._on_last_click

        self.render()

    def render(self) -> None:
        self.clear_items()
        buttons = [b for b in (self.first_button, self.prev_button, self.next_button, self.last_button) if b]
        self.add_item(container_from_embed(self.pages[self.current_page], discord.ui.ActionRow(*buttons)))

    def _update_buttons(self) -> None:
        is_first = self.current_page == 0
        is_last = self.current_page == len(self.pages) - 1

        self.prev_button.disabled = is_first
        self.next_button.disabled = is_last
        if self.first_button is not None:
            self.first_button.disabled = is_first
        if self.last_button is not None:
            self.last_button.disabled = is_last

    async def _go_to(self, interaction: discord.Interaction, page: int) -> None:
        self.current_page = max(0, min(page, len(self.pages) - 1))
        self._update_buttons()
        self.render()
        await interaction.response.edit_message(view=self)

    async def _on_first_click(self, interaction: discord.Interaction):
        await self._go_to(interaction, 0)

    async def _on_prev_click(self, interaction: discord.Interaction):
        await self._go_to(interaction, self.current_page - 1)

    async def _on_next_click(self, interaction: discord.Interaction):
        await self._go_to(interaction, self.current_page + 1)

    async def _on_last_click(self, interaction: discord.Interaction):
        await self._go_to(interaction, len(self.pages) - 1)


# Aliases for compatibility across different import naming conventions
PaginatedView = PaginatorView
NavigationView = PaginatorView
NavigateView = PaginatorView
Paginator = PaginatorView
