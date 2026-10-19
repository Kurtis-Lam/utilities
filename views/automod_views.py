from __future__ import annotations

import discord

from views.common_views import EmbedLayout

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


class AutomodPanelView(EmbedLayout):
    """Admin panel: the embed with its buttons inside it.

    Buttons: send the warning message, switch between kick and ban and, for bot owners only,
    reset the stored logs of a server (asks for the server ID in a popup).
    """

    def __init__(
        self,
        cog,
        *,
        embed: discord.Embed,
        author_id: int,
        action: str,
        has_channel: bool,
        is_owner: bool = False,
    ):
        super().__init__(embed, author_id=author_id, timeout=180)
        self.cog = cog

        self.send_button = discord.ui.Button(
            label="Send Warning Message",
            style=discord.ButtonStyle.danger,
            emoji="⚠️",
            disabled=not has_channel,
        )
        self.send_button.callback = self._on_send

        self.toggle_button = discord.ui.Button(
            label=f"Action: {ACTION_WORDS[action]['noun'][:-1]}",
            style=discord.ButtonStyle.secondary,
        )
        self.toggle_button.callback = self._on_toggle

        self.reset_button = None
        if is_owner:
            self.reset_button = discord.ui.Button(
                label="Reset Logs",
                style=discord.ButtonStyle.secondary,
                emoji="🧹",
            )
            self.reset_button.callback = self._on_reset

        self.render()

    def rows(self):
        row = [self.send_button, self.toggle_button]
        if self.reset_button is not None:
            row.append(self.reset_button)
        return [row]

    async def _on_send(self, interaction: discord.Interaction):
        await self.cog.panel_send_warning(interaction)

    async def _on_toggle(self, interaction: discord.Interaction):
        await self.cog.panel_toggle_action(interaction, self)

    async def _on_reset(self, interaction: discord.Interaction):
        if not await self.cog.bot.is_owner(interaction.user):
            return await interaction.response.send_message(
                embed=discord.Embed(title="❌ Only the bot owner can reset logs.", color=discord.Color.red()),
                ephemeral=True,
            )
        await interaction.response.send_modal(ResetLogsModal(self.cog, self))


class ResetLogsModal(discord.ui.Modal, title="Reset Automod Logs"):
    server_id = discord.ui.TextInput(
        label="Server ID (leave blank for this server)",
        placeholder="e.g. 123456789012345678",
        required=False,
        max_length=25,
    )

    def __init__(self, cog, panel: AutomodPanelView):
        super().__init__()
        self.cog = cog
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.panel_reset_logs(interaction, self.server_id.value, self.panel)