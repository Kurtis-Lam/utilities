import re

import discord

# --- Shared constants -------------------------------------------------------

CATEGORY_LABELS = {
    "rare": "Rare Lock",
    "regional": "Regional Lock",
    "gmax": "Gmax Lock",
    "paradox": "Paradox Lock",
    "eevos": "Eevos Lock",
    "sh": "Sh Lock",
    "cl": "Cl Lock",
    "tp": "Tp Lock",
    "rp": "Rp Lock",
}
CATEGORY_ORDER = ("rare", "regional", "gmax", "paradox", "eevos", "sh", "cl", "tp", "rp")

# Rare/Regional/Gmax/Paradox/Eevos are role-based, but the ROLE ITSELF is
# managed entirely by the PokePings cog (.rarerole, .regionalrole,
# .gigantamaxrole, .paradoxrole, .eeveeevolutions) — autolock only reads it,
# it never sets it. Sh/Cl/Tp/Rp instead get "restrict unlockers".
ROLE_CATEGORIES = {"rare", "regional", "gmax", "paradox", "eevos"}
RESTRICT_CATEGORIES = {"sh", "cl", "tp", "rp"}

# Shown in the category embed so admins know where to actually set the role.
ROLE_COMMAND_HINTS = {
    "rare": "`.rarerole` (alias `.rarole`)",
    "regional": "`.regionalrole` (alias `.regrole`)",
    "gmax": "`.gigantamaxrole` (alias `.gmaxrole`)",
    "paradox": "`.paradoxrole` (alias `.pararole`)",
    "eevos": "`.eeveeevolutions` (alias `.eevosrole`)",
}

# tp = Type Ping (the .tp opt-in list), rp = Regional Ping (the .rp opt-in
# list) — both are personal ping subscriptions from PokePings, distinct from
# the role-based Rare/Regional/Gmax/Paradox/Eevos locks above.
CATEGORY_DESCRIPTIONS = {
    "tp": "Triggers on Type Ping (`.tp`) notifications.",
    "rp": "Triggers on Regional Ping (`.rp`) notifications.",
    "sh": "Triggers on Shiny Hunt (`.sh`) notifications.",
    "cl": "Triggers on Collection (`.cl`) notifications.",
}


# --- Modals ------------------------------------------------------------------

class DelayModal(discord.ui.Modal, title="Set Lock Delay"):
    delay_seconds = discord.ui.TextInput(
        label="Delay (seconds)",
        placeholder="e.g. 10",
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

        delay = max(0, int(raw))
        await self.cog.set_delay(self.guild_id, self.category, delay)

        embed = await self.cog.build_category_embed(interaction.guild, self.category)
        view = CategoryConfigView(
            self.cog, guild_id=self.guild_id, author_id=interaction.user.id, category=self.category
        )
        try:
            await self.parent_message.edit(embed=embed, view=view)
        except discord.HTTPException:
            pass
        await interaction.response.send_message(f"✅ Lock delay set to `{delay}s`.", ephemeral=True)


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

        embed = await self.cog.build_category_embed(interaction.guild, self.category)
        view = CategoryConfigView(
            self.cog, guild_id=self.guild_id, author_id=interaction.user.id, category=self.category
        )
        try:
            await self.parent_message.edit(embed=embed, view=view)
        except discord.HTTPException:
            pass

        await interaction.response.send_message(f"✅ Input `{raw_val}` {verb} the whitelist.", ephemeral=True)


# --- Views ---------------------------------------------------------------------

class CategoryConfigView(discord.ui.View):
    """Config page for a single category. No role-setting here — roles for
    rare/regional/gmax/paradox/eevos are managed via PokePings' own commands
    and are only ever *read* by autolock."""

    def __init__(self, cog, guild_id: int, author_id: int, category: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.author_id = author_id
        self.category = category

        self.add_item(self._make_button("Set Delay", discord.ButtonStyle.blurple, self._set_delay))
        self.add_item(self._make_button("Add Whitelist", discord.ButtonStyle.green, self._add_whitelist))
        self.add_item(self._make_button("Remove Whitelist", discord.ButtonStyle.red, self._remove_whitelist))

        if category in RESTRICT_CATEGORIES:
            self.add_item(
                self._make_button("Toggle Restrict Unlockers", discord.ButtonStyle.gray, self._toggle_restrict)
            )

        self.add_item(self._make_button("Back", discord.ButtonStyle.gray, self._back, row=1))

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

    async def _toggle_restrict(self, interaction: discord.Interaction):
        await self.cog.toggle_restrict(self.guild_id, self.category)
        embed = await self.cog.build_category_embed(interaction.guild, self.category)
        view = CategoryConfigView(
            self.cog, guild_id=self.guild_id, author_id=self.author_id, category=self.category
        )
        await interaction.response.edit_message(embed=embed, view=view)

    async def _back(self, interaction: discord.Interaction):
        embed = await self.cog.build_main_embed(interaction.guild)
        view = AutoLockMainView(self.cog, guild_id=self.guild_id, author_id=self.author_id)
        await interaction.response.edit_message(embed=embed, view=view)


class AutoLockMainView(discord.ui.View):
    """Landing page: one button per lock category, 3 per row."""

    def __init__(self, cog, guild_id: int, author_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.author_id = author_id

        for i, cat in enumerate(CATEGORY_ORDER):
            row = i // 3
            button = discord.ui.Button(label=CATEGORY_LABELS[cat], style=discord.ButtonStyle.blurple, row=row)
            button.callback = self._make_callback(cat)
            self.add_item(button)

    def _make_callback(self, category: str):
        async def callback(interaction: discord.Interaction):
            embed = await self.cog.build_category_embed(interaction.guild, category)
            view = CategoryConfigView(
                self.cog, guild_id=self.guild_id, author_id=self.author_id, category=category
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