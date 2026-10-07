import asyncio
import re

import discord

from views.common_views import (
    EmbedLayout,
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
    "re": "Reserves (`.re`) pings. Top unlock priority.",
    "tp": "Type pings (`.tp`).",
    "rp": "Regional pings (`.rp`).",
    "sh": "Shiny hunt (`.sh`) pings.",
    "cl": "Collection (`.cl`) pings.",
}

LEGEND = "Green = on  •  Grey = off"


# --- Page builders ---------------------------------------------------------------

async def _build_category_page(cog, guild: discord.Guild, guild_id: int, author_id: int, category: str):
    """The category's config page as a view (container + buttons), always built
    from a fresh read of the config so the on/off button colors match reality."""
    cfg = await cog.get_category_config(guild_id, category)
    embed = await cog.build_category_embed(guild, category)
    return CategoryConfigView(cog, guild_id=guild_id, author_id=author_id, category=category, cfg=cfg, embed=embed)


async def build_main_page(cog, guild: discord.Guild, guild_id: int, author_id: int):
    """The `.c a` landing page as a view (container + buttons)."""
    results = await asyncio.gather(
        *(cog.get_category_config(guild_id, cat) for cat in CATEGORY_ORDER),
        return_exceptions=True,
    )
    states = {
        cat: bool(cfg.get("enabled", False))
        for cat, cfg in zip(CATEGORY_ORDER, results)
        if isinstance(cfg, dict)
    }
    embed = await cog.build_main_embed(guild)
    if states and not embed.footer.text:
        embed.set_footer(text=LEGEND)
    return AutoLockMainView(cog, guild_id=guild_id, author_id=author_id, states=states or None, embed=embed)


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
        placeholder=f"{MIN_DELAY}-{MAX_DELAY}, e.g. 15",
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
                embed=error_embed("Enter whole seconds."), ephemeral=True
            )

        delay = int(raw)
        if not (MIN_DELAY <= delay <= MAX_DELAY):
            return await interaction.response.send_message(
                embed=error_embed(f"Delay must be {MIN_DELAY}-{MAX_DELAY}s."),
                ephemeral=True,
            )

        await self.cog.set_delay(self.guild_id, self.category, delay)
        cfg = await self.cog.get_category_config(self.guild_id, self.category)

        await self.parent_view.reload(interaction)
        note = "" if cfg.get("delay_enabled", True) else " (delay is off)"
        await interaction.followup.send(embed=success_embed(f"Lock delay set to `{delay}s`.{note}"), ephemeral=True)


class WhitelistModal(discord.ui.Modal):
    target_input = discord.ui.TextInput(
        label="Channel / Category / Index / *",
        placeholder="Mention, ID, number or *, comma separated",
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
                embed=error_embed("No valid channel, ID, number or `*`."),
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

class CategoryConfigView(EmbedLayout):
    def __init__(self, cog, guild_id: int, author_id: int, category: str, cfg: dict, embed: discord.Embed | None = None):
        super().__init__(embed, author_id=author_id, timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.category = category

        self.schedule_row = [
            self._make_button("Set Delay", discord.ButtonStyle.blurple, self._set_delay, emoji="⏱️"),
            self._make_button("Add Whitelist", discord.ButtonStyle.green, self._add_whitelist, emoji="➕"),
            self._make_button("Remove Whitelist", discord.ButtonStyle.red, self._remove_whitelist, emoji="➖"),
        ]

        is_enabled = cfg.get("enabled", False)
        self.switch_row = [
            self._make_button(
                "Turn Lock Off" if is_enabled else "Turn Lock On",
                discord.ButtonStyle.red if is_enabled else discord.ButtonStyle.green,
                self._toggle_lock,
                emoji="🔒" if is_enabled else "🔓",
            )
        ]

        delay_on = cfg.get("delay_enabled", True)
        self.switch_row.append(self._make_button(
            "Turn Delay Off" if delay_on else "Turn Delay On",
            discord.ButtonStyle.red if delay_on else discord.ButtonStyle.green,
            self._toggle_delay,
            emoji="⏲️",
        ))

        if category in RESTRICT_CATEGORIES:
            restrict_on = cfg.get("restrict_unlockers", True)
            self.switch_row.append(self._make_button(
                "Turn Restrict Off" if restrict_on else "Turn Restrict On",
                discord.ButtonStyle.red if restrict_on else discord.ButtonStyle.green,
                self._toggle_restrict,
                emoji="🛡️",
            ))

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
        view = await build_main_page(self.cog, interaction.guild, self.guild_id, self.author_id)
        await self.swap(interaction, view=view)


class AutoLockMainView(EmbedLayout):
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
                button = discord.ui.Button(label=CATEGORY_LABELS[cat], style=style)
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