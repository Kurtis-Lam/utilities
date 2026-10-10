import asyncio
import re

import discord

from cogs.poketwo_helper.lockcommon import (
    DEFAULT_DELAY,
    DEFAULT_LOCKTIME,
    MAX_LOCKTIME,
    MIN_LOCKTIME,
    format_duration,
    parse_duration,
)
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
    std = await cog.get_standard(guild_id)
    embed = await cog.build_category_embed(guild, category)
    return CategoryConfigView(
        cog, guild_id=guild_id, author_id=author_id, category=category, cfg=cfg, embed=embed, std=std
    )


async def build_standard_page(cog, guild: discord.Guild, guild_id: int, author_id: int):
    """The ⭐ Standard settings page (reached from the `.c a` landing page)."""
    cfg = await cog.get_standard(guild_id)
    embed = await cog.build_standard_embed(guild)
    return StandardConfigView(cog, guild_id=guild_id, author_id=author_id, cfg=cfg, embed=embed)


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


def whitelist_result_text(cog, guild: discord.Guild, added=(), already=(), invalid=(), removed=(), not_found=()) -> str:
    """One readable summary of what an add / remove did (shared by modals and commands)."""
    def fmt(items):
        return ", ".join(cog.format_whitelist_item(guild, i) for i in items)

    lines = []
    if added:
        lines.append(f"➕ Added: {fmt(added)}")
    if removed:
        lines.append(f"➖ Removed: {fmt(removed)}")
    if already:
        lines.append(f"ℹ️ Already listed: {fmt(already)}")
    if invalid or not_found:
        bad = list(invalid) + list(not_found)
        lines.append("❓ Not found: " + ", ".join(f"`{b}`" for b in bad))
    return "\n".join(lines) or "Nothing changed."


def _parse_whitelist_items(raw: str) -> list[str]:
    items = [i.strip() for i in re.split(r"[,\s]+", raw) if i.strip()]
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


class LocktimeModal(discord.ui.Modal, title="Set Lock Time"):
    locktime_input = discord.ui.TextInput(
        label="Auto-unlock after",
        placeholder="e.g. 30m, 2h, 1h30m, 1d (a bare number = minutes)",
        required=True,
        max_length=20,
    )

    def __init__(self, cog, guild_id: int, category: str, parent_view):
        super().__init__()
        self.cog = cog
        self.guild_id = guild_id
        self.category = category
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        seconds = parse_duration(str(self.locktime_input.value))
        if seconds is None:
            return await interaction.response.send_message(
                embed=error_embed("Use a time like `30m`, `2h` or `1h30m`."), ephemeral=True
            )
        if not (MIN_LOCKTIME <= seconds <= MAX_LOCKTIME):
            return await interaction.response.send_message(
                embed=error_embed(
                    f"Lock time must be between {format_duration(MIN_LOCKTIME)} and {format_duration(MAX_LOCKTIME)}."
                ),
                ephemeral=True,
            )

        await self.cog.set_locktime(self.guild_id, self.category, seconds)
        await self.parent_view.reload(interaction)
        await interaction.followup.send(
            embed=success_embed(f"Lock time set to `{format_duration(seconds)}` (auto-unlock is on)."),
            ephemeral=True,
        )


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
            added, already, invalid = await self.cog.add_whitelist(interaction.guild, self.category, raw_val)
            text = whitelist_result_text(self.cog, interaction.guild, added=added, already=already, invalid=invalid)
            changed = bool(added)
        else:
            removed, not_found = await self.cog.remove_whitelist(interaction.guild, self.category, raw_val)
            text = whitelist_result_text(self.cog, interaction.guild, removed=removed, not_found=not_found)
            changed = bool(removed)

        await self.parent_view.reload(interaction)
        embed = success_embed(text) if changed else warning_embed(text)
        await interaction.followup.send(embed=embed, ephemeral=True)


# --- Views -----------------------------------------------------------------------

def _make_button(label, style, callback, emoji=None):
    button = discord.ui.Button(label=label[:80], style=style, emoji=emoji)
    button.callback = callback
    return button


def _on_off_button(label: str, is_on: bool, callback, emoji=None):
    """Red 'Turn X Off' when it's on, green 'Turn X On' when it's off."""
    return _make_button(
        f"Turn {label} {'Off' if is_on else 'On'}",
        discord.ButtonStyle.red if is_on else discord.ButtonStyle.green,
        callback,
        emoji=emoji,
    )


class _SettingsView(EmbedLayout):
    """Shared plumbing of the lock page and the standard page: the buttons that edit
    delay / lock time / whitelist behave identically on both (``self.category`` is a
    lock key or ``"standard"``)."""

    def __init__(self, cog, guild_id: int, author_id: int, category: str, embed: discord.Embed | None):
        super().__init__(embed, author_id=author_id, timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.category = category

    async def reload(self, interaction: discord.Interaction):
        raise NotImplementedError

    async def _set_delay(self, interaction: discord.Interaction):
        await interaction.response.send_modal(DelayModal(self.cog, self.guild_id, self.category, self))

    async def _set_locktime(self, interaction: discord.Interaction):
        await interaction.response.send_modal(LocktimeModal(self.cog, self.guild_id, self.category, self))

    async def _add_whitelist(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            WhitelistModal(self.cog, self.guild_id, self.category, self, action="add")
        )

    async def _remove_whitelist(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            WhitelistModal(self.cog, self.guild_id, self.category, self, action="remove")
        )

    async def _clear_whitelist(self, interaction: discord.Interaction):
        count = await self.cog.clear_whitelist(self.guild_id, self.category)
        await self.reload(interaction)
        text = f"Whitelist cleared (`{count}` removed)." if count else "The whitelist was already empty."
        await interaction.followup.send(
            embed=success_embed(text) if count else warning_embed(text), ephemeral=True
        )

    async def _toggle_delay(self, interaction: discord.Interaction):
        await self.cog.toggle_delay(self.guild_id, self.category)
        await self.reload(interaction)

    async def _toggle_locktime(self, interaction: discord.Interaction):
        await self.cog.toggle_locktime(self.guild_id, self.category)
        await self.reload(interaction)

    def _setting_row(self) -> list:
        return [
            _make_button("Set Delay", discord.ButtonStyle.blurple, self._set_delay, emoji="⏱️"),
            _make_button("Set Lock Time", discord.ButtonStyle.blurple, self._set_locktime, emoji="⌛"),
            _make_button("Add Whitelist", discord.ButtonStyle.green, self._add_whitelist, emoji="➕"),
            _make_button("Remove Whitelist", discord.ButtonStyle.red, self._remove_whitelist, emoji="➖"),
            _make_button("Clear Whitelist", discord.ButtonStyle.red, self._clear_whitelist, emoji="🧹"),
        ]


class CategoryConfigView(_SettingsView):
    def __init__(
        self,
        cog,
        guild_id: int,
        author_id: int,
        category: str,
        cfg: dict,
        embed: discord.Embed | None = None,
        std: dict | None = None,
    ):
        super().__init__(cog, guild_id, author_id, category, embed)
        std = std or {}

        self.schedule_row = self._setting_row()

        is_enabled = cfg.get("enabled", False)
        self.switch_row = [
            _make_button(
                "Turn Lock Off" if is_enabled else "Turn Lock On",
                discord.ButtonStyle.red if is_enabled else discord.ButtonStyle.green,
                self._toggle_lock,
                emoji="🔒" if is_enabled else "🔓",
            ),
            _on_off_button("Delay", cfg.get("delay_enabled", True), self._toggle_delay, emoji="⏲️"),
            _on_off_button("Lock Time", cfg.get("locktime_enabled", False), self._toggle_locktime, emoji="⏳"),
        ]

        if category in RESTRICT_CATEGORIES:
            self.switch_row.append(
                _on_off_button("Restrict", cfg.get("restrict_unlockers", True), self._toggle_restrict, emoji="🛡️")
            )

        std_delay = f"{std.get('delay', DEFAULT_DELAY)}s" + ("" if std.get("delay_enabled", True) else ", off")
        std_locktime = format_duration(std.get("locktime", DEFAULT_LOCKTIME)) + (
            "" if std.get("locktime_enabled", False) else ", off"
        )
        std_whitelist = len(std.get("whitelist", []))
        self.standard_row = [
            _make_button(f"Use Standard Delay ({std_delay})", discord.ButtonStyle.gray, self._use_std_delay, emoji="⭐"),
            _make_button(
                f"Use Standard Lock Time ({std_locktime})", discord.ButtonStyle.gray, self._use_std_locktime, emoji="⭐"
            ),
            _make_button(
                f"Use Standard Whitelist ({std_whitelist})", discord.ButtonStyle.gray, self._use_std_whitelist, emoji="⭐"
            ),
        ]

        self.nav_row = [_make_button("Back", discord.ButtonStyle.gray, self._back, emoji="◀️")]
        self.render()

    def rows(self):
        return [self.schedule_row, self.switch_row, self.standard_row, self.nav_row]

    async def reload(self, interaction: discord.Interaction):
        view = await _build_category_page(
            self.cog, interaction.guild, self.guild_id, self.author_id, self.category
        )
        await self.swap(interaction, view=view)

    async def _use_standard(self, interaction: discord.Interaction, part: str, text: str):
        await self.cog.use_standard(self.guild_id, self.category, (part,))
        await self.reload(interaction)
        await interaction.followup.send(embed=success_embed(text), ephemeral=True)

    async def _use_std_delay(self, interaction: discord.Interaction):
        std = await self.cog.get_standard(self.guild_id)
        state = "" if std.get("delay_enabled", True) else " (delay is off)"
        await self._use_standard(interaction, "delay", f"Lock delay set to the standard `{std.get('delay', DEFAULT_DELAY)}s`.{state}")

    async def _use_std_locktime(self, interaction: discord.Interaction):
        std = await self.cog.get_standard(self.guild_id)
        state = "" if std.get("locktime_enabled", False) else " (auto-unlock is off)"
        await self._use_standard(
            interaction,
            "locktime",
            f"Lock time set to the standard `{format_duration(std.get('locktime', DEFAULT_LOCKTIME))}`.{state}",
        )

    async def _use_std_whitelist(self, interaction: discord.Interaction):
        std = await self.cog.get_standard(self.guild_id)
        count = len(std.get("whitelist", []))
        await self._use_standard(interaction, "whitelist", f"Whitelist replaced with the standard one (`{count}` entries).")

    async def _toggle_lock(self, interaction: discord.Interaction):
        await self.cog.toggle_lock(self.guild_id, self.category)
        await self.reload(interaction)

    async def _toggle_restrict(self, interaction: discord.Interaction):
        await self.cog.toggle_restrict(self.guild_id, self.category)
        await self.reload(interaction)

    async def _back(self, interaction: discord.Interaction):
        view = await build_main_page(self.cog, interaction.guild, self.guild_id, self.author_id)
        await self.swap(interaction, view=view)


class StandardConfigView(_SettingsView):
    """⭐ The server's standard delay / lock time / whitelist."""

    def __init__(self, cog, guild_id: int, author_id: int, cfg: dict, embed: discord.Embed | None = None):
        super().__init__(cog, guild_id, author_id, "standard", embed)

        self.schedule_row = self._setting_row()
        self.switch_row = [
            _on_off_button("Delay", cfg.get("delay_enabled", True), self._toggle_delay, emoji="⏲️"),
            _on_off_button("Lock Timer", cfg.get("locktime_enabled", False), self._toggle_locktime, emoji="⏳"),
        ]
        self.nav_row = [_make_button("Back", discord.ButtonStyle.gray, self._back, emoji="◀️")]
        self.render()

    def rows(self):
        return [self.schedule_row, self.switch_row, self.nav_row]

    async def reload(self, interaction: discord.Interaction):
        view = await build_standard_page(self.cog, interaction.guild, self.guild_id, self.author_id)
        await self.swap(interaction, view=view)

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

        standard_button = discord.ui.Button(label="Standard", style=discord.ButtonStyle.blurple, emoji="⭐")
        standard_button.callback = self._open_standard
        self.button_rows.append([standard_button])
        self.render()

    def rows(self):
        return self.button_rows

    async def _open_standard(self, interaction: discord.Interaction):
        view = await build_standard_page(self.cog, interaction.guild, self.guild_id, self.author_id)
        await self.swap(interaction, view=view)

    def _make_callback(self, category: str):
        async def callback(interaction: discord.Interaction):
            view = await _build_category_page(
                self.cog, interaction.guild, self.guild_id, self.author_id, category
            )
            await self.swap(interaction, view=view)

        return callback