"""
Views for `.ai config`: Add / Remove / Clear buttons for the AI channel whitelist.

The view is deliberately thin: all database work lives in the AIChat cog
(`add_ai_channels`, `remove_ai_channels`, `clear_ai_channels`,
`build_config_embed`), so this file only handles the UI.
"""
import re
from typing import Optional

import discord

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


class AIConfigView(discord.ui.View):
    """Buttons shown under `.ai config` (administrators only)."""

    def __init__(self, cog, author_id: int, guild: discord.Guild, timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.author_id = author_id
        self.guild = guild
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                embed=discord.Embed(description="⚠️ Not your menu.", color=discord.Color.gold()),
                ephemeral=True,
            )
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
        result = discord.Embed(description=result_text, color=BRAND_COLOR)
        try:
            await interaction.response.edit_message(embed=embed, view=self)
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

    @discord.ui.button(label="Add", emoji="➕", style=discord.ButtonStyle.success)
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ChannelModal(self, "add"))

    @discord.ui.button(label="Remove", emoji="➖", style=discord.ButtonStyle.secondary)
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ChannelModal(self, "remove"))

    @discord.ui.button(label="Clear", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def clear_button(self, interaction: discord.Interaction, button: discord.ui.Button):
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

    async def on_timeout(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass