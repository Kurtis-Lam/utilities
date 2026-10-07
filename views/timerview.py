import asyncio

import discord

from views.autolock_views import _parse_whitelist_items
from views.common import EmbedLayout, error_embed, success_embed

# --- Shared UI constants ------------------------------------------------------

# Priority order: if a spawn matches several categories, the first one that is
# enabled / whitelisted / has a non-AFK hunter gets the timer.
TIMER_CATEGORIES = ("sh", "cl", "rp", "tp")

TIMER_LABELS = {
    "sh": "SH Timer",
    "cl": "CL Timer",
    "rp": "RP Timer",
    "tp": "TP Timer",
}

# Text used inside the timer embed / steal warning.
TIMER_NAMES = {
    "sh": "Shiny Hunt",
    "cl": "Collection",
    "rp": "Region Ping",
    "tp": "Type Ping",
}

TIMER_DESCRIPTIONS = {
    "sh": "Shiny hunt (`.sh`) timer.",
    "cl": "Collection (`.cl`) timer.",
    "rp": "Regional pings (`.rp`) timer.",
    "tp": "Type pings (`.tp`) timer.",
}

ROW_LAYOUT = (("sh", "cl"), ("rp", "tp"))

DEFAULT_SECONDS = 15
MIN_SECONDS = 1
MAX_SECONDS = 600

LEGEND = "Green = on  •  Grey = off"


# --- Page builders ------------------------------------------------------------

async def _build_category_page(cog, guild: discord.Guild, guild_id: int, author_id: int, category: str):
    cfg = await cog.get_category_config(guild_id, category)
    embed = await cog.build_category_embed(guild, category)
    return TimerCategoryView(cog, guild_id=guild_id, author_id=author_id, category=category, cfg=cfg, embed=embed)


async def build_main_page(cog, guild: discord.Guild, guild_id: int, author_id: int):
    results = await asyncio.gather(
        *(cog.get_category_config(guild_id, cat) for cat in TIMER_CATEGORIES),
        return_exceptions=True,
    )
    states = {
        cat: bool(cfg.get("enabled", False))
        for cat, cfg in zip(TIMER_CATEGORIES, results)
        if isinstance(cfg, dict)
    }
    embed = await cog.build_main_embed(guild)
    if states and not embed.footer.text:
        embed.set_footer(text=LEGEND)
    return TimerMainView(cog, guild_id=guild_id, author_id=author_id, states=states or None, embed=embed)


# --- Modals -------------------------------------------------------------------

class SecondsModal(discord.ui.Modal, title="Set Timer Seconds"):
    seconds = discord.ui.TextInput(
        label="Timer length in seconds",
        placeholder=f"{MIN_SECONDS}-{MAX_SECONDS}, e.g. 15",
        required=True,
        max_length=5,
    )

    def __init__(self, cog, guild_id: int, category: str, parent_view: "TimerCategoryView"):
        super().__init__()
        self.cog = cog
        self.guild_id = guild_id
        self.category = category
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.seconds.value).strip()
        if not raw.isdigit():
            return await interaction.response.send_message(
                embed=error_embed("Enter whole seconds."), ephemeral=True
            )

        value = int(raw)
        if not (MIN_SECONDS <= value <= MAX_SECONDS):
            return await interaction.response.send_message(
                embed=error_embed(f"Timer must be {MIN_SECONDS}-{MAX_SECONDS}s."),
                ephemeral=True,
            )

        await self.cog.set_seconds(self.guild_id, self.category, value)
        await self.parent_view.reload(interaction)
        await interaction.followup.send(
            embed=success_embed(f"Timer set to `{value}s`."), ephemeral=True
        )


class TimerWhitelistModal(discord.ui.Modal):
    target_input = discord.ui.TextInput(
        label="Channel / Category / Index / *",
        placeholder="Mention, ID, number or *, comma separated",
        required=True,
    )

    def __init__(self, cog, guild_id: int, category: str, parent_view: "TimerCategoryView", action: str):
        super().__init__(title=f"{'Add To' if action == 'add' else 'Remove From'} Whitelist")
        self.cog = cog
        self.guild_id = guild_id
        self.category = category
        self.parent_view = parent_view
        self.action = action

    async def on_submit(self, interaction: discord.Interaction):
        raw_val = str(self.target_input.value).strip()

        if not _parse_whitelist_items(raw_val):
            return await interaction.response.send_message(
                embed=error_embed("No valid channel, ID, number or `*`."),
                ephemeral=True,
            )

        if self.action == "add":
            await self.cog.add_whitelist(interaction.guild, self.category, raw_val)
            verb = "added to"
        else:
            await self.cog.remove_whitelist(interaction.guild, self.category, raw_val)
            verb = "removed from"

        await self.parent_view.reload(interaction)
        await interaction.followup.send(
            embed=success_embed(f"`{raw_val}` {verb} the whitelist."), ephemeral=True
        )


# --- Views --------------------------------------------------------------------

class TimerCategoryView(EmbedLayout):
    def __init__(self, cog, guild_id: int, author_id: int, category: str, cfg: dict, embed: discord.Embed | None = None):
        super().__init__(embed, author_id=author_id, timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.category = category

        self.schedule_row = [
            self._make_button("Set Seconds", discord.ButtonStyle.blurple, self._set_seconds, emoji="⏱️"),
            self._make_button("Add Whitelist", discord.ButtonStyle.green, self._add_whitelist, emoji="➕"),
            self._make_button("Remove Whitelist", discord.ButtonStyle.red, self._remove_whitelist, emoji="➖"),
        ]

        is_enabled = cfg.get("enabled", False)
        self.switch_row = [
            self._make_button(
                "Turn Timer Off" if is_enabled else "Turn Timer On",
                discord.ButtonStyle.red if is_enabled else discord.ButtonStyle.green,
                self._toggle_timer,
                emoji="⏲️",
            )
        ]

        self.nav_row = [self._make_button("Back", discord.ButtonStyle.gray, self._back, emoji="◀️")]
        self.render()

    def rows(self):
        return [self.schedule_row, self.switch_row, self.nav_row]

    def _make_button(self, label, style, callback, emoji=None):
        button = discord.ui.Button(label=label, style=style, emoji=emoji)
        button.callback = callback
        return button

    async def reload(self, interaction: discord.Interaction):
        view = await _build_category_page(
            self.cog, interaction.guild, self.guild_id, self.author_id, self.category
        )
        await self.swap(interaction, view=view)

    async def _set_seconds(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SecondsModal(self.cog, self.guild_id, self.category, self))

    async def _add_whitelist(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            TimerWhitelistModal(self.cog, self.guild_id, self.category, self, action="add")
        )

    async def _remove_whitelist(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            TimerWhitelistModal(self.cog, self.guild_id, self.category, self, action="remove")
        )

    async def _toggle_timer(self, interaction: discord.Interaction):
        await self.cog.toggle_timer(self.guild_id, self.category)
        await self.reload(interaction)

    async def _back(self, interaction: discord.Interaction):
        view = await build_main_page(self.cog, interaction.guild, self.guild_id, self.author_id)
        await self.swap(interaction, view=view)


class TimerMainView(EmbedLayout):
    def __init__(self, cog, guild_id: int, author_id: int, states: dict | None = None, embed: discord.Embed | None = None):
        super().__init__(embed, author_id=author_id, timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.button_rows: list[list[discord.ui.Button]] = []

        for row in ROW_LAYOUT:
            buttons = []
            for cat in row:
                if states is None:
                    style = discord.ButtonStyle.blurple
                else:
                    style = discord.ButtonStyle.green if states.get(cat) else discord.ButtonStyle.gray
                button = discord.ui.Button(label=TIMER_LABELS[cat], style=style)
                button.callback = self._make_callback(cat)
                buttons.append(button)
            self.button_rows.append(buttons)
        self.render()

    def rows(self):
        return self.button_rows

    def _make_callback(self, category: str):
        async def callback(interaction: discord.Interaction):
            view = await _build_category_page(
                self.cog, interaction.guild, self.guild_id, self.author_id, category
            )
            await self.swap(interaction, view=view)

        return callback