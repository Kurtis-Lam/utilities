import asyncio
import re

import discord

from views.common import (
    BaseView,
    error_embed,
    success_embed,
    themed,
    warning_embed,
)

# --- Shared UI constants ------------------------------------------------------

CATEGORY_LABELS = {
    "re": "Res Lock",
    "sh": "Sh Lock",
    "cl": "Cl Lock",
    "rp": "Rp Lock",
    "tp": "Tp Lock",
    "rare": "Rare Lock",
    "regional": "Regional Lock",
    "gmax": "Gmax Lock",
    "paradox": "Paradox Lock",
    "eevos": "Eevos Lock",
}

ROW_LAYOUT = (
    ("re", "sh", "cl"),
    ("rp", "tp"),
    ("rare", "regional"),
    ("gmax", "paradox", "eevos"),
)
CATEGORY_ORDER = tuple(cat for row in ROW_LAYOUT for cat in row)
ALL_CATEGORIES = CATEGORY_ORDER

MIN_DELAY = 1
MAX_DELAY = 600

ROLE_CATEGORIES = {"rare", "regional", "gmax", "paradox", "eevos"}
RESTRICT_CATEGORIES = {"re", "sh", "cl", "tp", "rp"}

ROLE_COMMAND_HINTS = {
    "rare": "`.set rarerole` (alias `.set rarole`)",
    "regional": "`.set regionalrole` (alias `.set regrole`)",
    "gmax": "`.set gigantamaxrole` (alias `.set gmaxrole`)",
    "paradox": "`.set paradoxrole` (alias `.set pararole`)",
    "eevos": "`.set eeveelutionsrole` (alias `.set eevosroles`)",
}

CATEGORY_DESCRIPTIONS = {
    "re": "Triggers on Reserves (`.re`) notifications. Highest unlock priority (res > sh > cl > others).",
    "tp": "Triggers on Type Ping (`.tp`) notifications.",
    "rp": "Triggers on Regional Ping (`.rp`) notifications.",
    "sh": "Triggers on Shiny Hunt (`.sh`) notifications.",
    "cl": "Triggers on Collection (`.cl`) notifications.",
}

LEGEND = "Green = lock on  •  Grey = lock off"


# --- Page builders ---------------------------------------------------------------

async def _build_category_page(cog, guild: discord.Guild, guild_id: int, author_id: int, category: str):
    """(embed, view) for a category's config page, always built from a fresh
    read of the config so the on/off button colors match reality."""
    cfg = await cog.get_category_config(guild_id, category)
    embed = themed(await cog.build_category_embed(guild, category))
    view = CategoryConfigView(cog, guild_id=guild_id, author_id=author_id, category=category, cfg=cfg)
    return embed, view


async def build_main_page(cog, guild: discord.Guild, guild_id: int, author_id: int):
    """(embed, view) for the `.c a` landing page."""
    results = await asyncio.gather(
        *(cog.get_category_config(guild_id, cat) for cat in CATEGORY_ORDER),
        return_exceptions=True,
    )
    states = {
        cat: bool(cfg.get("enabled", False))
        for cat, cfg in zip(CATEGORY_ORDER, results)
        if isinstance(cfg, dict)
    }
    embed = themed(await cog.build_main_embed(guild))
    if states and not embed.footer.text:
        embed.set_footer(text=LEGEND)
    view = AutoLockMainView(cog, guild_id=guild_id, author_id=author_id, states=states or None)
    return embed, view


def _parse_whitelist_items(raw: str) -> list[str]:
    items = [i.strip() for i in raw.split(",") if i.strip()]
    valid = []
    for item in items:
        if item == "*":
            valid.append("*")
        else:
            clean_id = re.sub(r"\D", "", item)
            if clean_id or item.isdigit():
                valid.append(item)
    return valid


# --- Modals ------------------------------------------------------------------

class DelayModal(discord.ui.Modal, title="Set Lock Delay"):
    delay_seconds = discord.ui.TextInput(
        label="Delay in seconds",
        placeholder=f"A whole number from {MIN_DELAY} to {MAX_DELAY}, e.g. 15",
        required=True,
        max_length=5,
    )

    def __init__(self, cog, guild_id: int, category: str, parent_view: "CategoryConfigView"):
        super().__init__()
        self.cog = cog
        self.guild_id = guild_id
        self.category = category
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.delay_seconds.value).strip()
        if not raw.isdigit():
            return await interaction.response.send_message(
                embed=error_embed("Please enter a whole number of seconds."), ephemeral=True
            )

        delay = int(raw)
        if not (MIN_DELAY <= delay <= MAX_DELAY):
            return await interaction.response.send_message(
                embed=error_embed(
                    f"Delay must be between `{MIN_DELAY}` and `{MAX_DELAY}` seconds.\n"
                    "To lock instantly, press **Turn Delay Off** instead."
                ),
                ephemeral=True,
            )

        await self.cog.set_delay(self.guild_id, self.category, delay)
        cfg = await self.cog.get_category_config(self.guild_id, self.category)

        await self.parent_view.reload(interaction)
        note = "" if cfg.get("delay_enabled", True) else "\nThe delay is currently **off**, so this lock still fires instantly."
        await interaction.followup.send(embed=success_embed(f"Lock delay set to `{delay}s`.{note}"), ephemeral=True)


class WhitelistModal(discord.ui.Modal):
    target_input = discord.ui.TextInput(
        label="Channel / Category / Index / *",
        placeholder="Channel mention, ID, list number, or * for all (comma separated)",
        required=True,
    )

    def __init__(self, cog, guild_id: int, category: str, parent_view: "CategoryConfigView", action: str):
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
                embed=error_embed("Couldn't find a valid channel, category ID, list number, or `*` in that."),
                ephemeral=True,
            )

        if self.action == "add":
            await self.cog.add_whitelist(self.guild_id, self.category, raw_val)
            verb = "added to"
        else:
            await self.cog.remove_whitelist(interaction.guild, self.category, raw_val)
            verb = "removed from"

        await self.parent_view.reload(interaction)
        await interaction.followup.send(
            embed=success_embed(f"`{raw_val}` {verb} the whitelist."), ephemeral=True
        )


# --- Views -----------------------------------------------------------------------

class CategoryConfigView(BaseView):
    def __init__(self, cog, guild_id: int, author_id: int, category: str, cfg: dict):
        super().__init__(author_id=author_id, timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.category = category

        # Row 0: schedule + whitelist
        self.add_item(self._make_button("Set Delay", discord.ButtonStyle.blurple, self._set_delay, row=0, emoji="⏱️"))
        self.add_item(self._make_button("Add Whitelist", discord.ButtonStyle.green, self._add_whitelist, row=0, emoji="➕"))
        self.add_item(self._make_button("Remove Whitelist", discord.ButtonStyle.red, self._remove_whitelist, row=0, emoji="➖"))

        # Row 1: on/off switches
        is_enabled = cfg.get("enabled", False)
        self.add_item(self._make_button(
            "Turn Lock Off" if is_enabled else "Turn Lock On",
            discord.ButtonStyle.red if is_enabled else discord.ButtonStyle.green,
            self._toggle_lock,
            row=1,
            emoji="🔒" if is_enabled else "🔓",
        ))

        delay_on = cfg.get("delay_enabled", True)
        self.add_item(self._make_button(
            "Turn Delay Off" if delay_on else "Turn Delay On",
            discord.ButtonStyle.red if delay_on else discord.ButtonStyle.green,
            self._toggle_delay,
            row=1,
            emoji="⏲️",
        ))

        if category in RESTRICT_CATEGORIES:
            restrict_on = cfg.get("restrict_unlockers", True)
            self.add_item(self._make_button(
                "Turn Restrict Off" if restrict_on else "Turn Restrict On",
                discord.ButtonStyle.red if restrict_on else discord.ButtonStyle.green,
                self._toggle_restrict,
                row=1,
                emoji="🛡️",
            ))

        # Row 2: navigation
        self.add_item(self._make_button("Back", discord.ButtonStyle.gray, self._back, row=2, emoji="◀️"))

    def _make_button(self, label, style, callback, row=0, emoji=None):
        button = discord.ui.Button(label=label, style=style, row=row, emoji=emoji)
        button.callback = callback
        return button

    async def reload(self, interaction: discord.Interaction):
        embed, view = await _build_category_page(
            self.cog, interaction.guild, self.guild_id, self.author_id, self.category
        )
        await self.swap(interaction, embed=embed, view=view)

    async def _set_delay(self, interaction: discord.Interaction):
        await interaction.response.send_modal(DelayModal(self.cog, self.guild_id, self.category, self))

    async def _add_whitelist(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            WhitelistModal(self.cog, self.guild_id, self.category, self, action="add")
        )

    async def _remove_whitelist(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            WhitelistModal(self.cog, self.guild_id, self.category, self, action="remove")
        )

    async def _toggle_lock(self, interaction: discord.Interaction):
        await self.cog.toggle_lock(self.guild_id, self.category)
        await self.reload(interaction)

    async def _toggle_delay(self, interaction: discord.Interaction):
        await self.cog.toggle_delay(self.guild_id, self.category)
        await self.reload(interaction)

    async def _toggle_restrict(self, interaction: discord.Interaction):
        await self.cog.toggle_restrict(self.guild_id, self.category)
        await self.reload(interaction)

    async def _back(self, interaction: discord.Interaction):
        embed, view = await build_main_page(self.cog, interaction.guild, self.guild_id, self.author_id)
        await self.swap(interaction, embed=embed, view=view)


class AutoLockMainView(BaseView):
    def __init__(self, cog, guild_id: int, author_id: int, states: dict | None = None):
        super().__init__(author_id=author_id, timeout=180)
        self.cog = cog
        self.guild_id = guild_id

        for row_index, row in enumerate(ROW_LAYOUT):
            for cat in row:
                if states is None:
                    style = discord.ButtonStyle.blurple
                else:
                    style = discord.ButtonStyle.green if states.get(cat) else discord.ButtonStyle.gray
                button = discord.ui.Button(label=CATEGORY_LABELS[cat], style=style, row=row_index)
                button.callback = self._make_callback(cat)
                self.add_item(button)

    def _make_callback(self, category: str):
        async def callback(interaction: discord.Interaction):
            embed, view = await _build_category_page(
                self.cog, interaction.guild, self.guild_id, self.author_id, category
            )
            await self.swap(interaction, embed=embed, view=view)

        return callback