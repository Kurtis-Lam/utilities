from typing import List

import discord

from views.common_views import BaseLayout, EMBED_COLOR, container_from_embed, error_embed, make_embed
from views.navigate import PaginatorView


def create_paginator_view(pages: List[discord.Embed], user_id: int) -> PaginatorView:
    """Pager whose arrow buttons sit inside the page's embed. Send it with ``ctx.reply(view=view)``."""
    return PaginatorView(pages, user_id=user_id)


def result_view(embeds: List[discord.Embed]) -> discord.ui.LayoutView:
    """A button-less message made of one embed-style container per embed."""
    view = discord.ui.LayoutView(timeout=None)
    for embed in embeds:
        view.add_item(container_from_embed(embed))
    return view


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
        )

    async def callback(self, interaction: discord.Interaction):
        view: TypePingView = self.view

        g_id = str(interaction.guild_id)
        u_id = str(interaction.user.id)

        new_types = self.values
        await view.cog.set_ping_data(g_id, "tp", u_id, new_types)

        for option in self.options:
            option.default = option.value in new_types

        view.embed = view.make_embed(new_types)
        view.render()
        await interaction.response.edit_message(view=view)


class TypePingView(BaseLayout):
    """The type ping list and its select menu together inside one embed-style container."""

    def __init__(self, cog: "PokePings", user_id: int, user_types: list):
        super().__init__(author_id=user_id, timeout=180)
        self.cog = cog
        self.user_id = user_id
        self.select = TypePingSelect(user_types)
        self.embed = self.make_embed(user_types)
        self.render()

    def render(self) -> None:
        self.clear_items()
        self.add_item(container_from_embed(self.embed, discord.ui.ActionRow(self.select)))

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
        )

    async def callback(self, interaction: discord.Interaction):
        view: RegionPingView = self.view

        g_id = str(interaction.guild_id)
        u_id = str(interaction.user.id)

        new_regions = self.values
        await view.cog.set_ping_data(g_id, "rp", u_id, new_regions)

        for option in self.options:
            option.default = option.value in new_regions

        view.embed = view.make_embed(new_regions)
        view.render()
        await interaction.response.edit_message(view=view)


class RegionPingView(BaseLayout):
    """The region ping list and its select menu together inside one embed-style container."""

    def __init__(self, cog: "PokePings", user_id: int, user_regions: list):
        super().__init__(author_id=user_id, timeout=180)
        self.cog = cog
        self.user_id = user_id
        self.select = RegionPingSelect(user_regions)
        self.embed = self.make_embed(user_regions)
        self.render()

    def render(self) -> None:
        self.clear_items()
        self.add_item(container_from_embed(self.embed, discord.ui.ActionRow(self.select)))

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
        view.render()
        await interaction.response.edit_message(view=view)


class ShinyHuntView(BaseLayout):
    """Variant picker: the select menu and the Previous / Next / Confirm buttons live inside the embed."""

    page_size = 25

    def __init__(self, cog: "PokePings", author_id: int, variants: list[str]):
        super().__init__(author_id=author_id, timeout=60)
        self.cog = cog
        self.variants = variants
        self.selected = set()
        self.page = 0
        self.selector = ShinyHuntSelect(self)

        self.previous_button = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary)
        self.next_button = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary)
        self.confirm_button = discord.ui.Button(label="Confirm", style=discord.ButtonStyle.success)
        self.previous_button.callback = self._on_previous
        self.next_button.callback = self._on_next
        self.confirm_button.callback = self._on_confirm
        self.previous_button.disabled = True
        self.next_button.disabled = len(self.variants) <= self.page_size
        self.render()

    def render(self) -> None:
        self.clear_items()
        self.add_item(
            container_from_embed(
                self.make_embed(),
                discord.ui.ActionRow(self.selector),
                discord.ui.ActionRow(self.previous_button, self.next_button, self.confirm_button),
            )
        )

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
        self.render()

    async def _on_previous(self, interaction: discord.Interaction):
        self._change_page(-1)
        await interaction.response.edit_message(view=self)

    async def _on_next(self, interaction: discord.Interaction):
        self._change_page(1)
        await interaction.response.edit_message(view=self)

    async def _on_confirm(self, interaction: discord.Interaction):
        if not self.selected:
            await interaction.response.send_message(
                embed=error_embed("Select at least one Pokémon variant first."),
                ephemeral=True,
            )
            return

        variants = [name for name in self.variants if name in self.selected]
        await self.cog.set_ping_data(
            str(interaction.guild_id), "sh", str(interaction.user.id), variants
        )
        embeds = self.cog._sh_confirmation_embeds(variants, "✨ Shiny Hunt Pings Updated")
        await interaction.response.edit_message(view=result_view(embeds[:10]))
        self.stop()
        for start in range(10, len(embeds), 10):
            await interaction.followup.send(view=result_view(embeds[start:start + 10]))
