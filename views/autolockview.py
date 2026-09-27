import re

import discord

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

# Button layout for the `.c a` main page — exactly the rows requested:
#   row 1: res, sh, cl
#   row 2: rp, tp
#   row 3: rare, regional
#   row 4: gmax, paradox, eevos
# CATEGORY_ORDER is just this flattened, and is also the order everything
# else (embeds, .chsettings, .set/.toggle "all") iterates categories in.
ROW_LAYOUT = (
    ("re", "sh", "cl"),
    ("rp", "tp"),
    ("rare", "regional"),
    ("gmax", "paradox", "eevos"),
)
CATEGORY_ORDER = tuple(cat for row in ROW_LAYOUT for cat in row)
ALL_CATEGORIES = CATEGORY_ORDER

# Same limits as `.set lockdelay`. To lock instantly, turn the delay OFF instead.
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


# --- small shared helper -------------------------------------------------------

async def _build_category_page(cog, guild: discord.Guild, guild_id: int, author_id: int, category: str):
    """(embed, view) for a category's config page, always built from a fresh
    read of the config so the on/off button colors match reality."""
    cfg = await cog.get_category_config(guild_id, category)
    embed = await cog.build_category_embed(guild, category)
    view = CategoryConfigView(cog, guild_id=guild_id, author_id=author_id, category=category, cfg=cfg)
    return embed, view


# --- Modals ------------------------------------------------------------------

class DelayModal(discord.ui.Modal, title="Set Lock Delay"):
    delay_seconds = discord.ui.TextInput(
        label="Delay (seconds)",
        placeholder=f"e.g. 15 ({MIN_DELAY}-{MAX_DELAY})",
        required=True,
        max_length=5,
    )

    def __init__(self, cog, guild_id: int, category: str, parent_message: discord.Message):
        super().__init__()
        self.cog = cog
        self.guild_id = guild_id
        self.category = category
        self.parent_message = parent_message

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.delay_seconds.value).strip()
        if not raw.isdigit():
            return await interaction.response.send_message(
                "⚠️ Please enter a whole number of seconds.", ephemeral=True
            )

        delay = int(raw)
        if not (MIN_DELAY <= delay <= MAX_DELAY):
            return await interaction.response.send_message(
                f"⚠️ Delay must be between `{MIN_DELAY}` and `{MAX_DELAY}` seconds. "
                "To lock instantly, use **Turn Delay Off** instead.",
                ephemeral=True,
            )

        await self.cog.set_delay(self.guild_id, self.category, delay)
        cfg = await self.cog.get_category_config(self.guild_id, self.category)

        embed, view = await _build_category_page(
            self.cog, interaction.guild, self.guild_id, interaction.user.id, self.category
        )
        try:
            await self.parent_message.edit(embed=embed, view=view)
        except discord.HTTPException:
            pass
        note = "" if cfg.get("delay_enabled", True) else " (Delay is currently **off**, so this lock still fires instantly.)"
        await interaction.response.send_message(f"✅ Lock delay set to `{delay}s`.{note}", ephemeral=True)


class WhitelistModal(discord.ui.Modal):
    target_input = discord.ui.TextInput(
        label="Channel / Category ID / Index / *",
        placeholder="e.g. *, #channel, 123456789, or 1, 2",
        required=True,
    )

    def __init__(self, cog, guild_id: int, category: str, parent_message: discord.Message, action: str):
        super().__init__(title=f"{'Add To' if action == 'add' else 'Remove From'} Whitelist")
        self.cog = cog
        self.guild_id = guild_id
        self.category = category
        self.parent_message = parent_message
        self.action = action

    async def on_submit(self, interaction: discord.Interaction):
        raw_val = str(self.target_input.value).strip()
        items = [i.strip() for i in raw_val.split(",") if i.strip()]

        valid_items = []
        for item in items:
            if item == "*":
                valid_items.append("*")
            else:
                clean_id = re.sub(r"\D", "", item)
                if clean_id or item.isdigit():
                    valid_items.append(item)

        if not valid_items:
            return await interaction.response.send_message(
                "⚠️ Could not parse a valid channel/category ID, index, or `*`.", ephemeral=True
            )

        if self.action == "add":
            await self.cog.add_whitelist(self.guild_id, self.category, raw_val)
            verb = "added to"
        else:
            await self.cog.remove_whitelist(interaction.guild, self.category, raw_val)
            verb = "removed from"

        embed, view = await _build_category_page(
            self.cog, interaction.guild, self.guild_id, interaction.user.id, self.category
        )
        try:
            await self.parent_message.edit(embed=embed, view=view)
        except discord.HTTPException:
            pass

        await interaction.response.send_message(f"✅ Input `{raw_val}` {verb} the whitelist.", ephemeral=True)


# --- Views ---------------------------------------------------------------------

class CategoryConfigView(discord.ui.View):
    """
    Config page for a single category.

    `cfg` is that category's current effective (guild-wide) config. It's only
    used to decide button labels/colors: the on/off, delay, and restrict
    buttons always show what will happen if pressed as a plain "Turn On"
    (green) / "Turn Off" (red) rather than an ambiguous "Toggle" button.
    """

    def __init__(self, cog, guild_id: int, author_id: int, category: str, cfg: dict):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.author_id = author_id
        self.category = category

        # Row 0: configuration controls
        self.add_item(self._make_button("Set Delay", discord.ButtonStyle.blurple, self._set_delay, row=0))
        self.add_item(self._make_button("Add Whitelist", discord.ButtonStyle.green, self._add_whitelist, row=0))
        self.add_item(self._make_button("Remove Whitelist", discord.ButtonStyle.red, self._remove_whitelist, row=0))

        # Row 1: on/off switches, always showing the CURRENT state and the
        # color of the action pressing them will take (green = will turn on,
        # red = will turn off).
        is_enabled = cfg.get("enabled", False)
        self.add_item(self._make_button(
            "Turn Off" if is_enabled else "Turn On",
            discord.ButtonStyle.red if is_enabled else discord.ButtonStyle.green,
            self._toggle_lock,
            row=1,
        ))

        delay_on = cfg.get("delay_enabled", True)
        self.add_item(self._make_button(
            "Turn Delay Off" if delay_on else "Turn Delay On",
            discord.ButtonStyle.red if delay_on else discord.ButtonStyle.green,
            self._toggle_delay,
            row=1,
        ))

        if category in RESTRICT_CATEGORIES:
            restrict_on = cfg.get("restrict_unlockers", True)
            self.add_item(self._make_button(
                "Turn Restrict Off" if restrict_on else "Turn Restrict On",
                discord.ButtonStyle.red if restrict_on else discord.ButtonStyle.green,
                self._toggle_restrict,
                row=1,
            ))

        # Row 2: navigation
        self.add_item(self._make_button("Back", discord.ButtonStyle.gray, self._back, row=2))

    def _make_button(self, label, style, callback, row=0):
        button = discord.ui.Button(label=label, style=style, row=row)
        button.callback = callback
        return button

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "⚠️ Only the person who ran this command can use these controls.", ephemeral=True
            )
            return False
        return True

    async def _set_delay(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            DelayModal(self.cog, self.guild_id, self.category, interaction.message)
        )

    async def _add_whitelist(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            WhitelistModal(self.cog, self.guild_id, self.category, interaction.message, action="add")
        )

    async def _remove_whitelist(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            WhitelistModal(self.cog, self.guild_id, self.category, interaction.message, action="remove")
        )

    async def _toggle_lock(self, interaction: discord.Interaction):
        await self.cog.toggle_lock(self.guild_id, self.category)
        embed, view = await _build_category_page(
            self.cog, interaction.guild, self.guild_id, self.author_id, self.category
        )
        await interaction.response.edit_message(embed=embed, view=view)

    async def _toggle_delay(self, interaction: discord.Interaction):
        await self.cog.toggle_delay(self.guild_id, self.category)
        embed, view = await _build_category_page(
            self.cog, interaction.guild, self.guild_id, self.author_id, self.category
        )
        await interaction.response.edit_message(embed=embed, view=view)

    async def _toggle_restrict(self, interaction: discord.Interaction):
        await self.cog.toggle_restrict(self.guild_id, self.category)
        embed, view = await _build_category_page(
            self.cog, interaction.guild, self.guild_id, self.author_id, self.category
        )
        await interaction.response.edit_message(embed=embed, view=view)

    async def _back(self, interaction: discord.Interaction):
        embed = await self.cog.build_main_embed(interaction.guild)
        view = AutoLockMainView(self.cog, guild_id=self.guild_id, author_id=self.author_id)
        await interaction.response.edit_message(embed=embed, view=view)


class AutoLockMainView(discord.ui.View):
    """Landing page for `.c a`: one button per lock category, laid out per ROW_LAYOUT."""

    def __init__(self, cog, guild_id: int, author_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.author_id = author_id

        for row_index, row in enumerate(ROW_LAYOUT):
            for cat in row:
                button = discord.ui.Button(label=CATEGORY_LABELS[cat], style=discord.ButtonStyle.blurple, row=row_index)
                button.callback = self._make_callback(cat)
                self.add_item(button)

    def _make_callback(self, category: str):
        async def callback(interaction: discord.Interaction):
            embed, view = await _build_category_page(
                self.cog, interaction.guild, self.guild_id, self.author_id, category
            )
            await interaction.response.edit_message(embed=embed, view=view)

        return callback

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "⚠️ Only the person who ran this command can use these controls.", ephemeral=True
            )
            return False
        return True