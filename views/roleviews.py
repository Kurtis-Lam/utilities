from __future__ import annotations

import discord

from views.embeds import info_embed, err_embed


class RoleChoiceView(discord.ui.View):
    """Paginated role picker used when a partial role name has multiple matches."""

    PAGE_SIZE = 20

    def __init__(self, *, author_id: int, roles: list[discord.Role], query: str, action_callback):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.roles = list(roles)
        self.query = query
        self.action_callback = action_callback
        self.page = 0
        self.message: discord.Message | None = None
        self._render()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.roles) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def prompt_embed(self) -> discord.Embed:
        start = self.page * self.PAGE_SIZE
        end = min(start + self.PAGE_SIZE, len(self.roles))
        return info_embed(
            "Multiple Roles Matched",
            f"Choose the role to apply for `{self.query[:80]}` using the buttons below.\n"
            f"Showing roles **{start + 1}–{end}** of **{len(self.roles)}**."
        )

    def _render(self):
        self.clear_items()
        start = self.page * self.PAGE_SIZE
        for index, role in enumerate(self.roles[start:start + self.PAGE_SIZE]):
            button = discord.ui.Button(
                label=role.name[:80],
                style=discord.ButtonStyle.primary,
                row=index // 5,
            )

            async def choose(interaction: discord.Interaction, selected_role=role):
                await interaction.response.defer()
                try:
                    result = await self.action_callback(interaction, selected_role)
                except Exception as exc:
                    print(f"Role selection failed: {exc}")
                    result = err_embed("Role update failed. Check my permissions, role hierarchy and database connection.")
                self.stop()
                for item in self.children:
                    item.disabled = True
                if interaction.message is not None:
                    await interaction.message.edit(embed=result, view=self)

            button.callback = choose
            self.add_item(button)

        if self.page_count > 1:
            previous = discord.ui.Button(label="Previous", emoji="◀️", style=discord.ButtonStyle.secondary, row=4, disabled=self.page == 0)
            next_button = discord.ui.Button(label="Next", emoji="▶️", style=discord.ButtonStyle.secondary, row=4, disabled=self.page >= self.page_count - 1)

            async def go_previous(interaction: discord.Interaction):
                self.page = max(0, self.page - 1)
                self._render()
                await interaction.response.edit_message(embed=self.prompt_embed(), view=self)

            async def go_next(interaction: discord.Interaction):
                self.page = min(self.page_count - 1, self.page + 1)
                self._render()
                await interaction.response.edit_message(embed=self.prompt_embed(), view=self)

            previous.callback = go_previous
            next_button.callback = go_next
            self.add_item(previous)
            self.add_item(next_button)

        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger, row=4)

        async def cancel_callback(interaction: discord.Interaction):
            self.stop()
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(embed=info_embed("Role Update Cancelled"), view=self)

        cancel.callback = cancel_callback
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(embed=err_embed("Only the administrator who started this role update can choose a role."), ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(embed=info_embed("Role Selection Expired"), view=self)
            except discord.HTTPException:
                pass
