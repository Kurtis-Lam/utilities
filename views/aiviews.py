"""
Views for the AI cog.

* `.ai config` (server administrators): Add / Remove / Clear buttons for the AI channel whitelist.
* `.ai info`   (bot owner only): Add / Edit / Delete buttons for the OpenRouter API key pool.

The views are deliberately thin: all database / config work lives in the AIChat cog
(`add_ai_channels`, `add_api_key`, `edit_api_key`, `delete_api_key`, ...),
so this file only handles the UI. The cog raises ValueError for user-facing problems.
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


# ===========================================================================
# `.ai info` — API key management (bot owner only)
# ===========================================================================

def _error_embed(text: str) -> discord.Embed:
    return discord.Embed(description=f"❌ {text}", color=discord.Color.red())


class AddKeyModal(discord.ui.Modal):
    def __init__(self, info_view: "AIInfoView"):
        super().__init__(title="Add API key")
        self.info_view = info_view
        self.key_input = discord.ui.TextInput(
            label="OpenRouter API key",
            placeholder="sk-or-v1-...",
            required=True,
            max_length=200,
        )
        self.account_input = discord.ui.TextInput(
            label="Account name",
            placeholder="Keys with the same account name are grouped together",
            required=True,
            max_length=50,
        )
        self.add_item(self.key_input)
        self.add_item(self.account_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cog = self.info_view.cog
        try:
            position = await cog.add_api_key(str(self.key_input.value), str(self.account_input.value))
        except ValueError as e:
            await interaction.followup.send(embed=_error_embed(str(e)), ephemeral=True)
            return
        except Exception as e:
            print(f"AIInfoView: error adding key: {e}")
            await interaction.followup.send(embed=_error_embed("Couldn't save the key (check console logs)."), ephemeral=True)
            return

        account = cog._clean_account_name(str(self.account_input.value))
        await interaction.followup.send(
            embed=discord.Embed(description=f"✅ Added **Key #{position}** to account **{account}**.", color=BRAND_COLOR),
            ephemeral=True,
        )
        await self.info_view.refresh_message()

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"AddKeyModal error: {error}")
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send(embed=_error_embed("Something went wrong. Please try again."), ephemeral=True)


class EditKeyModal(discord.ui.Modal):
    """Both fields are optional: leaving one blank keeps its current value."""

    def __init__(self, info_view: "AIInfoView", key: str, position: int, account: str):
        super().__init__(title=f"Edit Key #{position}")
        self.info_view = info_view
        self.key = key
        self.position = position
        self.key_input = discord.ui.TextInput(
            label="New API key (blank = keep current)",
            placeholder=f"Current key ends in ...{key[-4:]}",
            required=False,
            max_length=200,
        )
        self.account_input = discord.ui.TextInput(
            label="New account name (blank = keep current)",
            placeholder=f"Current: {account}"[:100],
            required=False,
            max_length=50,
        )
        self.add_item(self.key_input)
        self.add_item(self.account_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cog = self.info_view.cog
        try:
            changed = await cog.edit_api_key(self.key, str(self.key_input.value), str(self.account_input.value))
        except ValueError as e:
            await interaction.followup.send(embed=_error_embed(str(e)), ephemeral=True)
            return
        except Exception as e:
            print(f"AIInfoView: error editing key: {e}")
            await interaction.followup.send(embed=_error_embed("Couldn't save the changes (check console logs)."), ephemeral=True)
            return

        if not changed:
            await interaction.followup.send(
                embed=discord.Embed(description="⚠️ Nothing was changed.", color=BRAND_COLOR), ephemeral=True
            )
            return

        await interaction.followup.send(
            embed=discord.Embed(
                description=f"✅ Updated {' and '.join(changed)} for **Key #{self.position}**.", color=BRAND_COLOR
            ),
            ephemeral=True,
        )
        await self.info_view.refresh_message()

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"EditKeyModal error: {error}")
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send(embed=_error_embed("Something went wrong. Please try again."), ephemeral=True)


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, info_view: "AIInfoView", key: str, position: int, account: str):
        super().__init__(timeout=30)
        self.info_view = info_view
        self.key = key
        self.position = position
        self.account = account

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="⏳ Deleting key...", view=None)
        try:
            await self.info_view.cog.delete_api_key(self.key)
        except ValueError as e:
            await interaction.edit_original_response(content=f"❌ {e}")
            return
        except Exception as e:
            print(f"AIInfoView: error deleting key: {e}")
            await interaction.edit_original_response(content="❌ Couldn't delete the key (check console logs).")
            return

        await interaction.edit_original_response(
            content=f"✅ Deleted **Key #{self.position}** (account **{self.account}**, ended in `...{self.key[-4:]}`)."
        )
        await self.info_view.refresh_message()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)


class KeyPickerView(discord.ui.View):
    """Ephemeral dropdown to choose which key to edit or delete. Keys are never shown in full."""

    MAX_OPTIONS = 25  # Discord's select-menu limit

    def __init__(self, info_view: "AIInfoView", action: str):
        super().__init__(timeout=60)
        self.info_view = info_view
        self.action = action
        cog = info_view.cog
        self.keys = list(cog.api_keys)[: self.MAX_OPTIONS]

        options = []
        for i, key in enumerate(self.keys):
            account = cog.key_account_id.get(key, "unknown")
            primary = " (primary)" if key == cog.primary_key else ""
            options.append(
                discord.SelectOption(
                    label=f"Key #{i + 1} — {account}"[:100],
                    description=f"Ends in ...{key[-4:]}{primary}"[:100],
                    value=str(i),
                )
            )
        self.select = discord.ui.Select(
            placeholder=f"Choose a key to {action}...", options=options, min_values=1, max_values=1
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        index = int(self.select.values[0])
        key = self.keys[index]
        cog = self.info_view.cog
        if key not in cog.api_keys:
            await interaction.response.edit_message(content="❌ That key no longer exists.", view=None)
            return
        account = cog.key_account_id.get(key, "unknown")
        position = index + 1

        if self.action == "edit":
            await interaction.response.send_modal(EditKeyModal(self.info_view, key, position, account))
        else:
            await interaction.response.edit_message(
                content=(
                    f"⚠️ Delete **Key #{position}** (account **{account}**, ends in `...{key[-4:]}`)?\n"
                    "This can't be undone."
                ),
                view=ConfirmDeleteView(self.info_view, key, position, account),
            )


class AIInfoView(BaseLayout):
    """Add / Edit / Delete / Refresh buttons shown inside the `.ai info` container (bot owner only)."""

    def __init__(self, cog, author_id: int, embed: discord.Embed, timeout: float = 180.0):
        super().__init__(author_id=author_id, timeout=timeout)
        self.cog = cog
        self.add_button = discord.ui.Button(label="Add Key", emoji="➕", style=discord.ButtonStyle.success)
        self.edit_button = discord.ui.Button(label="Edit Key", emoji="✏️", style=discord.ButtonStyle.primary)
        self.delete_button = discord.ui.Button(label="Delete Key", emoji="🗑️", style=discord.ButtonStyle.danger)
        self.refresh_button = discord.ui.Button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary)
        self.add_button.callback = self._on_add
        self.edit_button.callback = self._on_edit
        self.delete_button.callback = self._on_delete
        self.refresh_button.callback = self._on_refresh
        self.render(embed)

    def render(self, embed: discord.Embed) -> None:
        self.clear_items()
        self.add_item(
            container_from_embed(
                embed,
                discord.ui.ActionRow(self.add_button, self.edit_button, self.delete_button, self.refresh_button),
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False
        if not await self.cog.is_owner_user(interaction.user):
            await interaction.response.send_message(
                embed=discord.Embed(description="🚫 Only the bot owner can manage API keys.", color=discord.Color.red()),
                ephemeral=True,
            )
            return False
        return True

    async def refresh_message(self) -> None:
        """Rebuilds the info embed (live usage included) and redraws the main message in place."""
        message = getattr(self, "message", None)
        if message is None:
            return
        try:
            self.render(await self.cog.build_info_embed())
            await message.edit(view=self)
        except Exception as e:
            print(f"AIInfoView: failed to refresh info message: {e}")

    # -- buttons ------------------------------------------------------------

    async def _on_add(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddKeyModal(self))

    async def _open_picker(self, interaction: discord.Interaction, action: str):
        await interaction.response.send_message(
            f"Which key do you want to {action}?", view=KeyPickerView(self, action), ephemeral=True
        )

    async def _on_edit(self, interaction: discord.Interaction):
        await self._open_picker(interaction, "edit")

    async def _on_delete(self, interaction: discord.Interaction):
        await self._open_picker(interaction, "delete")

    async def _on_refresh(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.refresh_message()