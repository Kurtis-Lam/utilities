from __future__ import annotations

import discord

from views.common_views import BaseLayout, container_from_embed

ENTER_CUSTOM_ID = "giveaway:enter"


class LeaveView(BaseLayout):
    """Ephemeral prompt shown to someone who presses Enter while already entered."""

    def __init__(self, cog, giveaway_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.giveaway_id = giveaway_id
        self.leave_button = discord.ui.Button(label="Leave giveaway", style=discord.ButtonStyle.danger)
        self.leave_button.callback = self.leave
        embed = discord.Embed(description="You're already in this giveaway.", color=0x0414C7)
        self.add_item(container_from_embed(embed, discord.ui.ActionRow(self.leave_button)))

    async def leave(self, interaction: discord.Interaction):
        result = await self.cog.collection.update_one(
            {"_id": self.giveaway_id, "ended": False},
            {"$pull": {"participants": interaction.user.id}},
        )
        text = "You left the giveaway." if result.modified_count else "❌ That giveaway is no longer running."
        self.clear_items()
        self.add_item(container_from_embed(discord.Embed(description=text, color=0x0414C7)))
        await interaction.response.edit_message(view=self, allowed_mentions=discord.AllowedMentions.none())
        self.cog.schedule_refresh(self.giveaway_id)
        self.stop()


class EnterView(discord.ui.LayoutView):
    """Persistent enter button inside the giveaway embed; the giveaway cog validates the expiry in MongoDB."""

    def __init__(
        self,
        cog,
        embed: discord.Embed | None = None,
        *,
        entries: int = 0,
        disabled: bool = False,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        label = f"Enter ({entries})" if entries else "Enter"
        button = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label=label,
            emoji="🎉",
            custom_id=ENTER_CUSTOM_ID,
            disabled=disabled,
        )
        button.callback = self._on_enter
        self.add_item(
            container_from_embed(embed or discord.Embed(color=0x0414C7), discord.ui.ActionRow(button))
        )

    async def _on_enter(self, interaction: discord.Interaction):
        await self.cog.handle_enter(interaction)
