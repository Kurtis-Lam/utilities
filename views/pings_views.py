from typing import List

import discord

from views.common_views import BaseView, EMBED_COLOR, make_embed, warning_embed

# --- Navigation View Integration ----------------------------------------------
_NavViewClass = None
try:
    import views.navigate as nav_module
    for attr in ["PaginatorView", "PaginatedView", "Paginator", "NavigationView", "NavigateView"]:
        if hasattr(nav_module, attr):
            _NavViewClass = getattr(nav_module, attr)
            break
except ImportError:
    pass


class DefaultPaginatorView(discord.ui.View):
    """Fallback interactive pagination view with left and right arrow buttons."""

    def __init__(self, pages: List[discord.Embed], user_id: int, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.user_id = user_id
        self.current_page = 0

        self.prev_button = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            emoji="◀️",
            custom_id="nav_prev",
            disabled=True
        )
        self.next_button = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            emoji="▶️",
            custom_id="nav_next",
            disabled=(len(pages) <= 1)
        )

        self.prev_button.callback = self.on_prev_click
        self.next_button.callback = self.on_next_click

        self.add_item(self.prev_button)
        self.add_item(self.next_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=warning_embed("Not your menu."),
                ephemeral=True
            )
            return False
        return True

    def _update_buttons(self):
        self.prev_button.disabled = (self.current_page == 0)
        self.next_button.disabled = (self.current_page == len(self.pages) - 1)

    async def on_prev_click(self, interaction: discord.Interaction):
        if self.current_page > 0:
            self.current_page -= 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    async def on_next_click(self, interaction: discord.Interaction):
        if self.current_page < len(self.pages) - 1:
            self.current_page += 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)


def create_paginator_view(pages: List[discord.Embed], user_id: int) -> discord.ui.View:
    if _NavViewClass is not None:
        try:
            return _NavViewClass(pages=pages, user_id=user_id)
        except TypeError:
            try:
                return _NavViewClass(pages, user_id)
            except TypeError:
                try:
                    return _NavViewClass(pages)
                except Exception:
                    pass
    return DefaultPaginatorView(pages, user_id)


TYPES = [
    "Normal", "Fire", "Water", "Grass", "Electric", "Ice",
    "Fighting", "Poison", "Ground", "Flying", "Psychic", "Bug",
    "Rock", "Ghost", "Dragon", "Steel", "Dark", "Fairy"
]

REGIONS = [
    "Kanto", "Johto", "Hoenn", "Sinnoh", "Unova",
    "Kalos", "Alola", "Galar", "Hisui", "Paldea"
]

EXTRA_RP_CATEGORIES = ["Gmax", "Paradox", "Eevos"]


# --- UI Components for Type Pings ---

class TypePingSelect(discord.ui.Select):
    def __init__(self, user_types: list):
        options = [
            discord.SelectOption(
                label=t,
                value=t,
                default=(t in user_types)
            )
            for t in TYPES
        ]
        super().__init__(
            placeholder="Select types to toggle...",
            min_values=0,
            max_values=len(TYPES),
            options=options,
            custom_id="tp_select"
        )

    async def callback(self, interaction: discord.Interaction):
        view: TypePingView = self.view
        if interaction.user.id != view.user_id:
            return await interaction.response.send_message(
                embed=warning_embed("Not your menu."), ephemeral=True
            )

        g_id = str(interaction.guild_id)
        u_id = str(interaction.user.id)

        new_types = self.values
        await view.cog.set_ping_data(g_id, "tp", u_id, new_types)

        for option in self.options:
            option.default = option.value in new_types

        embed = view.make_embed(new_types)
        await interaction.response.edit_message(embed=embed, view=view)


class TypePingView(discord.ui.View):
    def __init__(self, cog: "PokePings", user_id: int, user_types: list):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id
        self.add_item(TypePingSelect(user_types))

    def make_embed(self, user_types: list) -> discord.Embed:
        embed = discord.Embed(title="⚡ Type Pings", color=EMBED_COLOR)
        embed.description = "\n".join(
            f"{'✅' if t in user_types else '❌'} **{t}**" for t in TYPES
        )
        return embed


# --- UI Components for Region & Special Category Pings ---

class RegionPingSelect(discord.ui.Select):
    def __init__(self, user_regions: list):
        all_items = REGIONS + EXTRA_RP_CATEGORIES
        options = [
            discord.SelectOption(
                label=item,
                value=item,
                default=(item in user_regions)
            )
            for item in all_items
        ]
        super().__init__(
            placeholder="Select regions/categories to toggle...",
            min_values=0,
            max_values=len(all_items),
            options=options,
            custom_id="rp_select"
        )

    async def callback(self, interaction: discord.Interaction):
        view: RegionPingView = self.view
        if interaction.user.id != view.user_id:
            return await interaction.response.send_message(
                embed=warning_embed("Not your menu."), ephemeral=True
            )

        g_id = str(interaction.guild_id)
        u_id = str(interaction.user.id)

        new_regions = self.values
        await view.cog.set_ping_data(g_id, "rp", u_id, new_regions)

        for option in self.options:
            option.default = option.value in new_regions

        embed = view.make_embed(new_regions)
        await interaction.response.edit_message(embed=embed, view=view)


class RegionPingView(discord.ui.View):
    def __init__(self, cog: "PokePings", user_id: int, user_regions: list):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id
        self.add_item(RegionPingSelect(user_regions))

    def make_embed(self, user_regions: list) -> discord.Embed:
        embed = discord.Embed(title="🌍 Region Pings", color=EMBED_COLOR)
        
        region_lines = [f"{'✅' if r in user_regions else '❌'} **{r}**" for r in REGIONS]
        extra_lines = [f"{'✅' if cat in user_regions else '❌'} **{cat}**" for cat in EXTRA_RP_CATEGORIES]

        embed.add_field(name="Regions", value="\n".join(region_lines), inline=True)
        embed.add_field(name="Special", value="\n".join(extra_lines), inline=True)
        return embed


# --- Shiny Hunt Variant Selector ----------------------------------------------

class ShinyHuntSelect(discord.ui.Select):
    def __init__(self, view: "ShinyHuntView"):
        self.sh_view = view
        super().__init__(
            placeholder="Choose the Pokémon variants to ping for...",
            min_values=1,
            max_values=min(view.page_size, len(view.variants)),
            options=[],
        )
        self.update_options()

    def update_options(self):
        page_variants = self.sh_view.page_variants
        self.options = [
            discord.SelectOption(
                label=name,
                value=name,
                default=name in self.sh_view.selected,
            )
            for name in page_variants
        ]
        self.max_values = len(page_variants)

    async def callback(self, interaction: discord.Interaction):
        view = self.sh_view
        page_names = set(view.page_variants)
        view.selected.difference_update(page_names)
        view.selected.update(self.values)
        self.update_options()
        await interaction.response.edit_message(embed=view.make_embed(), view=view)


class ShinyHuntView(BaseView):
    page_size = 25

    def __init__(self, cog: "PokePings", author_id: int, variants: list[str]):
        super().__init__(author_id=author_id, timeout=60)
        self.cog = cog
        self.variants = variants
        self.selected = set()
        self.page = 0
        self.selector = ShinyHuntSelect(self)
        self.add_item(self.selector)
        self.previous_button.disabled = True
        self.next_button.disabled = len(self.variants) <= self.page_size

    @property
    def page_variants(self) -> list[str]:
        start = self.page * self.page_size
        return self.variants[start:start + self.page_size]

    def make_embed(self) -> discord.Embed:
        pages = (len(self.variants) + self.page_size - 1) // self.page_size
        embed = make_embed(
            title="✨ Choose Shiny Hunt Variants",
            description=(
                f"Select one or more variants, then confirm. "
                f"({len(self.selected)} selected)\n"
                f"Page {self.page + 1}/{pages}"
            ),
        )
        return embed

    def _change_page(self, offset: int):
        self.page += offset
        self.selector.update_options()
        self.previous_button.disabled = self.page == 0
        self.next_button.disabled = (self.page + 1) * self.page_size >= len(self.variants)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=1)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._change_page(-1)
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=1)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._change_page(1)
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, row=1)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected:
            await interaction.response.send_message(
                embed=warning_embed("Select at least one Pokémon variant first."),
                ephemeral=True,
            )
            return

        variants = [name for name in self.variants if name in self.selected]
        await self.cog.set_ping_data(
            str(interaction.guild_id), "sh", str(interaction.user.id), variants
        )
        self.disable_all()
        embeds = self.cog._sh_confirmation_embeds(variants, "✨ Shiny Hunt Pings Updated")
        await interaction.response.edit_message(embeds=embeds[:10], view=None)
        self.stop()
        for start in range(10, len(embeds), 10):
            await interaction.followup.send(embeds=embeds[start:start + 10])