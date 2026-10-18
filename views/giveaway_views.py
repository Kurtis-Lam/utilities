from __future__ import annotations

import discord

ENTER_CUSTOM_ID = "giveaway:enter"


class LeaveView(discord.ui.View):
    """Ephemeral prompt shown to someone who presses Enter while already entered."""

    def __init__(self, cog, giveaway_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.giveaway_id = giveaway_id

    @discord.ui.button(label="Leave giveaway", style=discord.ButtonStyle.danger)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        result = await self.cog.collection.update_one(
            {"_id": self.giveaway_id, "ended": False},
            {"$pull": {"participants": interaction.user.id}},
        )
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        text = "You left the giveaway." if result.modified_count else "That giveaway is no longer running."
        await interaction.response.edit_message(content=text, view=self, allowed_mentions=discord.AllowedMentions.none())
        self.cog.schedule_refresh(self.giveaway_id)
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass


class EnterView(discord.ui.View):
    """Persistent enter button; the giveaway cog validates the expiry in MongoDB."""

    def __init__(self, cog, *, entries: int = 0, disabled: bool = False):
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
        self.add_item(button)

    async def _on_enter(self, interaction: discord.Interaction):
        await self.cog.handle_enter(interaction)
