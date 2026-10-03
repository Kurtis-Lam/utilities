"""
Views for `.ai config`: Add / Remove / Clear buttons for the AI channel whitelist.

The view is deliberately thin: all database work lives in the AIChat cog
(`add_ai_channels`, `remove_ai_channels`, `clear_ai_channels`,
`build_config_embed`), so this file only handles the UI.
"""
import re
from typing import Optional

import discord

from views.common import BaseLayout, container_from_embed
from views.embeds import BRAND_COLOR

_MENTION_RE = re.compile(r"^<#(\d+)>$")


def parse_channels(
    guild: discord.Guild, raw: str, *, allow_unknown_ids: bool = False
) -> tuple[list[int], list[str]]:
    """
    Turns text like `general, 123456789012345678, #bot-chat, <#987654321>`
    into (channel_ids, unrecognised_tokens).

    Tokens are split on commas / whitespace. Each one may be a channel name,
    a raw ID or a <#mention>. With `allow_unknown_ids` (used when removing),
    raw IDs of channels that no longer exist are still accepted so stale
    entries can be cleaned out.
    """
    ids: list[int] = []
    invalid: list[str] = []
    seen: set[int] = set()

    for token in re.split(r"[,\s]+", raw.strip()):
        if not token:
            continue

        channel_id: Optional[int] = None
        mention = _MENTION_RE.match(token)
        if mention:
            channel_id = int(mention.group(1))
        elif token.isdigit():
            channel_id = int(token)
        else:
            wanted = token.lstrip("#").lower()
            found = discord.utils.find(lambda c: c.name.lower() == wanted, guild.text_channels)
            if found:
                channel_id = found.id

        if channel_id is None:
            invalid.append(token)
            continue
        if not allow_unknown_ids and guild.get_channel_or_thread(channel_id) is None:
            invalid.append(token)
            continue

        if channel_id not in seen:
            seen.add(channel_id)
            ids.append(channel_id)

    return ids, invalid


class ChannelModal(discord.ui.Modal):
    channels = discord.ui.TextInput(
        label="Channels (separate with commas)",
        style=discord.TextStyle.paragraph,
        placeholder="general, bot-chat, 123456789012345678",
        required=True,
        max_length=1000,
    )

    def __init__(self, config_view: "AIConfigView", mode: str):
        super().__init__(title="Add AI channels" if mode == "add" else "Remove AI channels")
        self.config_view = config_view
        self.mode = mode

    async def on_submit(self, interaction: discord.Interaction):
        await self.config_view.handle_submit(interaction, self.mode, str(self.channels.value))

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"AIConfigView modal error: {error}")
        text = "❌ Something went wrong. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(embed=discord.Embed(description=text, color=discord.Color.red()), ephemeral=True)
        else:
            await interaction.response.send_message(embed=discord.Embed(description=text, color=discord.Color.red()), ephemeral=True)


class AIConfigView(BaseLayout):
    """Add / Remove / Clear buttons shown inside the `.ai config` container (administrators only)."""

    def __init__(self, cog, author_id: int, guild: discord.Guild, embed: discord.Embed, timeout: float = 180.0):
        super().__init__(author_id=author_id, timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.add_button = discord.ui.Button(label="Add", emoji="➕", style=discord.ButtonStyle.success)
        self.remove_button = discord.ui.Button(label="Remove", emoji="➖", style=discord.ButtonStyle.secondary)
        self.clear_button = discord.ui.Button(label="Clear", emoji="🗑️", style=discord.ButtonStyle.danger)
        self.add_button.callback = self._on_add
        self.remove_button.callback = self._on_remove
        self.clear_button.callback = self._on_clear
        self.render(embed)

    def render(self, embed: discord.Embed) -> None:
        self.clear_items()
        self.add_item(
            container_from_embed(
                embed,
                discord.ui.ActionRow(self.add_button, self.remove_button, self.clear_button),
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms is None or not perms.administrator:
            await interaction.response.send_message(
                embed=discord.Embed(description="🚫 Only administrators can change AI channels.", color=discord.Color.red()),
                ephemeral=True,
            )
            return False
        return True

    # -- shared -----------------------------------------------------------

    async def _refresh(self, interaction: discord.Interaction, result_text: str):
        """Redraws the config embed in place, then privately tells the user what happened."""
        embed = await self.cog.build_config_embed(self.guild)
        self.render(embed)
        result = discord.Embed(description=result_text, color=BRAND_COLOR)
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            await interaction.response.send_message(embed=result, ephemeral=True)
            return
        await interaction.followup.send(embed=result, ephemeral=True)

    async def handle_submit(self, interaction: discord.Interaction, mode: str, raw: str):
        ids, invalid = parse_channels(self.guild, raw, allow_unknown_ids=(mode == "remove"))
        lines: list[str] = []

        try:
            if ids and mode == "add":
                added, already = await self.cog.add_ai_channels(self.guild.id, ids)
                if added:
                    lines.append("✅ **Added:** " + ", ".join(f"<#{i}>" for i in added))
                if already:
                    lines.append("⚠️ **Already enabled:** " + ", ".join(f"<#{i}>" for i in already))
            elif ids and mode == "remove":
                removed, missing = await self.cog.remove_ai_channels(self.guild.id, ids)
                if removed:
                    lines.append("✅ **Removed:** " + ", ".join(f"<#{i}>" for i in removed))
                if missing:
                    lines.append("⚠️ **Wasn't enabled:** " + ", ".join(f"<#{i}>" for i in missing))
        except Exception as e:
            print(f"AIConfigView: database error in {mode}: {e}")
            lines = ["❌ Couldn't save the AI channels right now. Please try again in a moment."]

        if invalid:
            cleaned = ", ".join(f"`{t.replace('`', '')}`" for t in invalid)
            lines.append(f"❓ **Couldn't find:** {cleaned}")
        if not lines:
            lines.append("⚠️ No channels were provided.")

        await self._refresh(interaction, "\n".join(lines))

    # -- buttons ------------------------------------------------------------

    async def _on_add(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ChannelModal(self, "add"))

    async def _on_remove(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ChannelModal(self, "remove"))

    async def _on_clear(self, interaction: discord.Interaction):
        try:
            count = await self.cog.clear_ai_channels(self.guild.id)
        except Exception as e:
            print(f"AIConfigView: database error in clear: {e}")
            await interaction.response.send_message(
                embed=discord.Embed(description="❌ Couldn't clear the AI channels right now.", color=discord.Color.red()),
                ephemeral=True,
            )
            return
        text = f"🗑️ Cleared **{count}** AI channel(s)." if count else "⚠️ There are no AI channels to clear."
        await self._refresh(interaction, text)