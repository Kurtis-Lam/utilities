import asyncio

import discord

from cogs.poketwo_helper.lockcommon import (
    already_unlocked_embed,
    can_unlock,
    get_poketwo_target,
    is_locked_overwrite,
    unlock_denied_embed,
    unlocked_embed,
    to_unix,
)
from views.common_views import EMBED_COLOR, container_from_embed, error_embed

UNLOCK_CUSTOM_ID = "lockunlock:unlock_btn"


def poketwo_missing_embed() -> discord.Embed:
    return error_embed("Pokétwo isn't in this server.")


def message_texts(message: discord.Message | None) -> tuple[list[str], discord.Color | int | None]:
    """Read back the text blocks (and accent color) of a Components V2 lock message,
    so the button can be retired without keeping the original embed around."""
    texts: list[str] = []
    accent = None

    def walk(components):
        nonlocal accent
        for comp in components or []:
            if accent is None:
                accent = getattr(comp, "accent_colour", None) or getattr(comp, "accent_color", None)
            content = getattr(comp, "content", None)
            if isinstance(content, str) and content:
                texts.append(content)
            walk(getattr(comp, "children", None))

    walk(getattr(message, "components", None))
    return texts, accent


class UnlockLayout(discord.ui.LayoutView):
    """The lock message: the embed-style container with the Unlock button *inside* it.

    Used by .lock and by AutoLock, so both look and behave the same.
    Pass ``embed`` to build a fresh message, or ``texts`` (+ ``accent``) to rebuild
    an existing one. ``unlocked=True`` renders the greyed-out "Unlocked" button.
    """

    def __init__(self, bot, embed: discord.Embed | None = None, *, texts=None, accent=None, unlocked: bool = False):
        super().__init__(timeout=None)
        self.bot = bot

        self.unlock_button = discord.ui.Button(
            label="Unlocked" if unlocked else "Unlock",
            style=discord.ButtonStyle.secondary if unlocked else discord.ButtonStyle.green,
            emoji=None if unlocked else "🔓",
            custom_id=UNLOCK_CUSTOM_ID,
            disabled=unlocked,
        )
        self.unlock_button.callback = self._on_unlock
        row = discord.ui.ActionRow(self.unlock_button)

        if embed is not None:
            container = container_from_embed(embed, row)
        else:
            container = discord.ui.Container(accent_colour=accent or EMBED_COLOR)
            for text in texts or ["\u200b"]:
                container.add_item(discord.ui.TextDisplay(text))
            container.add_item(discord.ui.Separator())
            container.add_item(row)
        self.add_item(container)

    async def _retire(self, interaction: discord.Interaction) -> None:
        """Grey out the button on the message that was clicked (acknowledges the interaction)."""
        texts, accent = message_texts(interaction.message)
        view = UnlockLayout(self.bot, texts=texts, accent=accent, unlocked=True)
        try:
            await interaction.response.edit_message(view=view)
        except discord.HTTPException:
            # e.g. an old pre-V2 lock message that can't be converted; still acknowledge.
            if not interaction.response.is_done():
                await interaction.response.defer()

    async def _on_unlock(self, interaction: discord.Interaction):
        guild = interaction.guild
        channel = interaction.channel
        user = interaction.user

        if guild is None or channel is None:
            return

        locks_collection = self.bot.mongo_client["utilities"]["locked_channels"]

        poketwo, lock_doc = await asyncio.gather(
            get_poketwo_target(self.bot, guild),
            locks_collection.find_one({"_id": channel.id}),
        )

        if poketwo is None:
            return await interaction.response.send_message(embed=poketwo_missing_embed(), ephemeral=True)

        if not is_locked_overwrite(channel.overwrites_for(poketwo)):
            if lock_doc:
                await locks_collection.delete_one({"_id": channel.id})
            await self._retire(interaction)
            return await interaction.followup.send(embed=already_unlocked_embed(channel), ephemeral=True)

        if lock_doc and not can_unlock(lock_doc, user):
            return await interaction.response.send_message(embed=unlock_denied_embed(channel, lock_doc), ephemeral=True)

        permissions = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        try:
            await channel.set_permissions(poketwo, overwrite=permissions)
        except discord.HTTPException as e:
            return await interaction.response.send_message(embed=error_embed(f"Couldn't unlock: {e}"), ephemeral=True)

        if lock_doc:
            await locks_collection.delete_one({"_id": channel.id})

        await self._retire(interaction)
        await interaction.followup.send(
            embed=unlocked_embed(
                channel,
                unlocked_by=user,
                locked_at=to_unix((lock_doc or {}).get("locked_at")),
            )
        )