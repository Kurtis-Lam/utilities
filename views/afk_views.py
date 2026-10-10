import discord

from views.common_views import EMBED_COLOR, warning_embed

# Requires discord.py >= 2.6 (Components V2: LayoutView, Container, Section, ...)

CATEGORIES = [
    ("sh", "Shiny Hunt"),
    ("cl", "Collection"),
    ("tp", "Type Ping"),
    ("rp", "Region Ping"),
]


class PingToggleButton(discord.ui.Button):
    """On/Off button shown to the right of each category."""

    def __init__(self, afk_view: "AFKView", ping_key: str):
        is_active = afk_view.settings.get(ping_key, False)
        super().__init__(
            label="On" if is_active else "Off",
            style=discord.ButtonStyle.green if is_active else discord.ButtonStyle.red,
        )
        self.afk_view = afk_view
        self.ping_key = ping_key

    async def callback(self, interaction: discord.Interaction):
        view = self.afk_view
        new_value = not view.settings.get(self.ping_key, False)
        view.settings[self.ping_key] = new_value

        doc_id = f"{interaction.guild_id}_{interaction.user.id}"
        await view.cog.afk_settings.update_one(
            {"_id": doc_id},
            {
                "$set": {
                    "guild_id": interaction.guild_id,
                    "user_id": interaction.user.id,
                    f"allowed_pings.{self.ping_key}": new_value,
                }
            },
            upsert=True,
        )

        view.render()
        await interaction.response.edit_message(view=view)


class AFKToggleButton(discord.ui.Button):
    def __init__(self, afk_view: "AFKView"):
        is_afk = afk_view.is_afk
        super().__init__(
            label="Remove AFK" if is_afk else "Set AFK",
            style=discord.ButtonStyle.red if is_afk else discord.ButtonStyle.green,
        )
        self.afk_view = afk_view

    async def callback(self, interaction: discord.Interaction):
        view = self.afk_view
        view.is_afk = not view.is_afk

        if view.is_afk:
            await view.cog.afk_collection.update_one(
                {"_id": interaction.user.id},
                {"$set": {"afk": True}},
                upsert=True,
            )
        else:
            await view.cog.afk_collection.delete_one({"_id": interaction.user.id})

        view.render()
        await interaction.response.edit_message(view=view)


class TurnAllButton(discord.ui.Button):
    def __init__(self, afk_view: "AFKView", turn_on: bool):
        super().__init__(
            label="Turn All On" if turn_on else "Turn All Off",
            style=discord.ButtonStyle.grey,
        )
        self.afk_view = afk_view
        self.turn_on = turn_on

    async def callback(self, interaction: discord.Interaction):
        view = self.afk_view
        for key, _ in CATEGORIES:
            view.settings[key] = self.turn_on

        doc_id = f"{interaction.guild_id}_{interaction.user.id}"
        await view.cog.afk_settings.update_one(
            {"_id": doc_id},
            {
                "$set": {
                    "guild_id": interaction.guild_id,
                    "user_id": interaction.user.id,
                    "allowed_pings": view.settings,
                }
            },
            upsert=True,
        )

        view.render()
        await interaction.response.edit_message(view=view)


class AFKView(discord.ui.LayoutView):
    def __init__(self, cog, user_id: int, is_afk: bool, settings: dict):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id
        self.is_afk = is_afk
        self.settings = dict(settings)
        self.render()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=warning_embed("Not your menu."), ephemeral=True
            )
            return False
        return True

    def render(self):
        """Rebuild the whole container from the current state."""
        self.clear_items()

        status = "🌙 **AFK Status:** `AFK`" if self.is_afk else "☀️ **AFK Status:** `Active`"

        container = discord.ui.Container(accent_colour=EMBED_COLOR)
        container.add_item(discord.ui.TextDisplay(f"## ⚙️ AFK & Ping Settings\n{status}"))
        container.add_item(discord.ui.Separator())

        # One section per category: text on the left, button on the right
        for key, display_name in CATEGORIES:
            is_on = self.settings.get(key, False)
            icon = "✅" if is_on else "❌"
            subtext = "Receiving pings" if is_on else "Ignored"
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(f"**{display_name}** {icon}\n{subtext}"),
                    accessory=PingToggleButton(self, key),
                )
            )

        container.add_item(discord.ui.Separator())

        # Global actions, also inside the container
        container.add_item(
            discord.ui.ActionRow(
                AFKToggleButton(self),
                TurnAllButton(self, turn_on=True),
                TurnAllButton(self, turn_on=False),
            )
        )

        self.add_item(container)