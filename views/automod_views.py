from __future__ import annotations

import discord

from views.common_views import BaseView

AUTOMOD_LOG_CUSTOM_ID = "automod:log"
DEFAULT_AVATAR_URL = "https://cdn.discordapp.com/embed/avatars/0.png"
WARNING_RED = discord.Color(0xE02B2B)

ACTION_WORDS = {
    "kick": {"verb": "kicked", "noun": "Kicks"},
    "ban": {"verb": "banned", "noun": "Bans"},
}


class AutomodWarningView(discord.ui.LayoutView):
    """The red warning message: bot avatar on the right, counter button inside the container.

    Persistent: the cog registers one instance at startup and the button is matched by its
    custom_id, so it keeps working after restarts. Anyone can press it.
    """

    def __init__(self, cog, *, action: str = "kick", count: int = 0, avatar_url: str | None = None):
        super().__init__(timeout=None)
        self.cog = cog
        words = ACTION_WORDS.get(action, ACTION_WORDS["kick"])

        text = discord.ui.TextDisplay(
            "## 🚫 Do not send messages here\n"
            f"Anyone who sends a message in this channel will be automatically **{words['verb']}**."
        )
        section = discord.ui.Section(text, accessory=discord.ui.Thumbnail(avatar_url or DEFAULT_AVATAR_URL))

        self.log_button = discord.ui.Button(
            label=f"{words['noun']}: {count:,}",
            style=discord.ButtonStyle.danger,
            custom_id=AUTOMOD_LOG_CUSTOM_ID,
        )
        self.log_button.callback = self._on_log

        container = discord.ui.Container(accent_colour=WARNING_RED)
        container.add_item(section)
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(self.log_button))
        self.add_item(container)

    async def _on_log(self, interaction: discord.Interaction):
        await self.cog.handle_log(interaction)


class AutomodPanelView(BaseView):
    """Admin panel: send the warning message and switch between kick and ban."""

    def __init__(self, cog, *, author_id: int, action: str, has_channel: bool):
        super().__init__(author_id=author_id, timeout=180)
        self.cog = cog
        self.send_button.disabled = not has_channel
        self.toggle_button.label = f"Action: {ACTION_WORDS[action]['noun'][:-1]}"

    @discord.ui.button(label="Send Warning Message", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def send_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.panel_send_warning(interaction)

    @discord.ui.button(label="Action: Kick", style=discord.ButtonStyle.secondary)
    async def toggle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.panel_toggle_action(interaction, self)