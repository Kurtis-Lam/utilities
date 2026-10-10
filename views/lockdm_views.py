"""The ``.autolockdm`` page: each member's own lock-DM settings, edited with buttons.

* master switch   – DMs on / off (this is the "status" shown at the top)
* on autolock     – DM me the moment a channel I was pinged in gets autolocked
* before unlock   – DM me N minutes/hours before that channel unlocks automatically.
                    Only offered when admins configured an auto-unlock time, and N must
                    be shorter than the shortest one.
"""
from __future__ import annotations

import discord

from cogs.poketwo_helper.lockcommon import MIN_BEFORE_UNLOCK, format_duration, parse_duration
from views.common_views import EmbedLayout, error_embed, success_embed

TITLE = "🔔 Autolock DMs"


def build_lockdm_embed(guild: discord.Guild, settings: dict, limit: int | None) -> discord.Embed:
    enabled = settings["enabled"]
    before = settings["before_unlock"]

    embed = discord.Embed(
        title=f"{TITLE} — {guild.name}",
        description=(
            "Get a DM (with a link) when a channel you were pinged in is autolocked.\n"
            f"**Status: {'On ✅' if enabled else 'Off ❌'}**"
        ),
        color=discord.Color.green() if enabled else discord.Color.red(),
    )
    embed.add_field(
        name="DM when autolocked",
        value="✅ On" if settings["on_lock"] else "❌ Off",
        inline=False,
    )

    if limit is None:
        before_text = "Unavailable — admins haven't set up an auto-unlock time."
    elif before:
        before_text = f"✅ `{format_duration(before)}` before the channel unlocks"
        if before >= limit:
            before_text += f"\n-# Currently ignored: it must be shorter than `{format_duration(limit)}`."
    else:
        before_text = f"❌ Off\n-# Can be set up to just under `{format_duration(limit)}`."
    embed.add_field(name="DM before auto-unlock", value=before_text, inline=False)

    if not enabled:
        embed.set_footer(text="DMs are off — you won't get any, whatever is set above.")
    return embed


async def build_lockdm_page(cog, guild: discord.Guild, user_id: int) -> "LockDMView":
    """Always built from a fresh read, so the buttons match what is stored."""
    settings = await cog.get_settings(guild.id, user_id)
    limit = await cog.get_unlock_limit(guild.id)
    embed = build_lockdm_embed(guild, settings, limit)
    return LockDMView(cog, guild_id=guild.id, user_id=user_id, settings=settings, limit=limit, embed=embed)


class BeforeUnlockModal(discord.ui.Modal, title="DM Before Auto-Unlock"):
    time_input = discord.ui.TextInput(
        label="How long before the unlock?",
        placeholder="e.g. 10m, 1h (a bare number = minutes)",
        required=True,
        max_length=20,
    )

    def __init__(self, view: "LockDMView"):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        cog, guild_id, user_id = self.view.cog, self.view.guild_id, self.view.user_id

        # Re-check against the live admin config: it may have changed since the page was drawn.
        limit = await cog.get_unlock_limit(guild_id)
        if limit is None:
            return await interaction.response.send_message(
                embed=error_embed("Admins haven't set up an auto-unlock time, so this isn't available."),
                ephemeral=True,
            )

        seconds = parse_duration(str(self.time_input.value))
        if seconds is None:
            return await interaction.response.send_message(
                embed=error_embed("Use a time like `10m` or `1h`."), ephemeral=True
            )
        if seconds < MIN_BEFORE_UNLOCK:
            return await interaction.response.send_message(
                embed=error_embed(f"Pick at least {format_duration(MIN_BEFORE_UNLOCK)}."), ephemeral=True
            )
        if seconds >= limit:
            return await interaction.response.send_message(
                embed=error_embed(
                    f"Must be shorter than the auto-unlock time ({format_duration(limit)})."
                ),
                ephemeral=True,
            )

        await cog.update_settings(guild_id, user_id, before_unlock=seconds)
        await self.view.reload(interaction)
        await interaction.followup.send(
            embed=success_embed(f"You'll get a DM `{format_duration(seconds)}` before a channel auto-unlocks."),
            ephemeral=True,
        )


class LockDMView(EmbedLayout):
    def __init__(
        self,
        cog,
        guild_id: int,
        user_id: int,
        settings: dict,
        limit: int | None,
        embed: discord.Embed,
    ):
        super().__init__(embed, author_id=user_id, timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id

        enabled = settings["enabled"]
        self.master_row = [
            self._button(
                "Turn DMs Off" if enabled else "Turn DMs On",
                discord.ButtonStyle.red if enabled else discord.ButtonStyle.green,
                self._toggle_master,
                emoji="🔕" if enabled else "🔔",
            )
        ]

        on_lock = settings["on_lock"]
        self.option_row = [
            self._button(
                f"DM On Autolock: {'On' if on_lock else 'Off'}",
                discord.ButtonStyle.green if on_lock else discord.ButtonStyle.gray,
                self._toggle_on_lock,
                emoji="🔒",
            )
        ]

        # "Before unlock" only exists when admins configured an auto-unlock time.
        available = limit is not None
        before = settings["before_unlock"]
        set_button = self._button(
            "Set DM Before Unlock" if not before else "Change DM Before Unlock",
            discord.ButtonStyle.blurple,
            self._set_before,
            emoji="⏳",
        )
        set_button.disabled = not available
        self.option_row.append(set_button)

        if before:
            self.option_row.append(
                self._button("Clear Before Unlock", discord.ButtonStyle.red, self._clear_before, emoji="🧹")
            )

        self.render()

    def rows(self):
        return [self.master_row, self.option_row]

    @staticmethod
    def _button(label, style, callback, emoji=None) -> discord.ui.Button:
        button = discord.ui.Button(label=label, style=style, emoji=emoji)
        button.callback = callback
        return button

    async def reload(self, interaction: discord.Interaction):
        guild = interaction.guild
        view = await build_lockdm_page(self.cog, guild, self.user_id)
        await self.swap(interaction, view=view)

    async def _toggle_master(self, interaction: discord.Interaction):
        settings = await self.cog.get_settings(self.guild_id, self.user_id)
        await self.cog.update_settings(self.guild_id, self.user_id, enabled=not settings["enabled"])
        await self.reload(interaction)

    async def _toggle_on_lock(self, interaction: discord.Interaction):
        settings = await self.cog.get_settings(self.guild_id, self.user_id)
        await self.cog.update_settings(self.guild_id, self.user_id, on_lock=not settings["on_lock"])
        await self.reload(interaction)

    async def _set_before(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BeforeUnlockModal(self))

    async def _clear_before(self, interaction: discord.Interaction):
        await self.cog.update_settings(self.guild_id, self.user_id, before_unlock=None)
        await self.reload(interaction)