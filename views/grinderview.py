import re

import discord
from discord import ui

from cogs.config import grinder as config
from views.common import (
    ConfirmLayout,
    EmbedLayout,
    error_embed,
    extract_id,
    info_embed,
    make_embed,
    parse_indices,
    success_embed,
    warning_embed,
)


# --- Small helpers --------------------------------------------------------------

def fmt_placeholder(val) -> str:
    s = " ".join(str(val).split()) if val is not None and str(val) != "" else "None"
    if len(s) > 73:
        s = s[:70] + "..."
    return f"Current: {s} • blank = keep"


async def _guild_only(interaction: discord.Interaction) -> bool:
    """Reply with a friendly error and return False when used outside a server."""
    if interaction.guild_id:
        return True
    await interaction.response.send_message(embed=error_embed("Server only."), ephemeral=True)
    return False


async def _edit_panel(interaction: discord.Interaction, view: discord.ui.LayoutView):
    """Refresh the panel message the modal was opened from (best effort)."""
    if interaction.message:
        try:
            await interaction.message.edit(view=view)
        except discord.HTTPException:
            pass


def _fmt_nums(nums) -> str:
    return ", ".join(f"#{n}" for n in nums)


# =============================================================================
#  ACCOUNTS
# =============================================================================

class AddAccountModal(ui.Modal, title="Add Account"):
    token = ui.TextInput(label="Account Token", placeholder="Paste the account token here", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        token = self.token.value.strip()
        if not token:
            return await interaction.response.send_message(embed=error_embed("Token is empty."), ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        data = await config.get_global_data()

        if token in data["accounts"]:
            return await interaction.followup.send(
                embed=warning_embed("That token is already saved."), ephemeral=True
            )

        data["accounts"].append(token)
        await config.save_global_data(data)
        await _edit_panel(interaction, AccountsView(embed=config.build_accounts_embed(data["accounts"])))

        uid = config.get_user_id_from_token(token)
        mention = f"<@{uid}>" if uid else "Unknown Member"
        await interaction.followup.send(
            embed=success_embed(f"Account **#{len(data['accounts'])}** ({mention}) added."), ephemeral=True
        )


class DeleteAccountModal(ui.Modal, title="Delete Account"):
    index = ui.TextInput(label="Account Number", placeholder="e.g. 1", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        nums = parse_indices(self.index.value)
        if not nums:
            return await interaction.response.send_message(
                embed=error_embed("Enter an account number."), ephemeral=True
            )
        number = nums[0]

        await interaction.response.defer(ephemeral=True)
        data = await config.get_global_data()

        if not (1 <= number <= len(data["accounts"])):
            return await interaction.followup.send(
                embed=error_embed(f"No account #{number}."),
                ephemeral=True,
            )

        data["accounts"].pop(number - 1)
        await config.save_global_data(data)
        await _edit_panel(interaction, AccountsView(embed=config.build_accounts_embed(data["accounts"])))

        note = "\nAccounts after it have moved up by one number." if number <= len(data["accounts"]) else ""
        await interaction.followup.send(embed=success_embed(f"Account **#{number}** removed.{note}"), ephemeral=True)


class EditAccountModal(ui.Modal, title="Edit Account Token"):
    index = ui.TextInput(label="Account Number", placeholder="e.g. 1", required=True)
    new_token = ui.TextInput(label="New Token", placeholder="Paste the new token here", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        nums = parse_indices(self.index.value)
        if not nums:
            return await interaction.response.send_message(
                embed=error_embed("Enter an account number."), ephemeral=True
            )
        number = nums[0]

        await interaction.response.defer(ephemeral=True)
        data = await config.get_global_data()

        if not (1 <= number <= len(data["accounts"])):
            return await interaction.followup.send(
                embed=error_embed(f"No account #{number}."),
                ephemeral=True,
            )

        data["accounts"][number - 1] = self.new_token.value.strip()
        await config.save_global_data(data)
        await _edit_panel(interaction, AccountsView(embed=config.build_accounts_embed(data["accounts"])))

        await interaction.followup.send(embed=success_embed(f"Account **#{number}** token updated."), ephemeral=True)


class AccountsView(EmbedLayout):
    def __init__(self, embed: discord.Embed | None = None):
        super().__init__(embed, timeout=None)

        self.nav_configs_btn = ui.Button(label="📋 Configs", style=discord.ButtonStyle.grey, custom_id="acc_nav_cfg")
        self.nav_configs_btn.callback = self.nav_configs
        self.nav_logs_btn = ui.Button(label="📜 Logs", style=discord.ButtonStyle.grey, custom_id="acc_nav_logs")
        self.nav_logs_btn.callback = self.nav_logs
        self.add_btn = ui.Button(label="➕ Add Account", style=discord.ButtonStyle.green, custom_id="acc_add")
        self.add_btn.callback = self.add_account
        self.edit_btn = ui.Button(label="✏️ Edit Account", style=discord.ButtonStyle.blurple, custom_id="acc_edit")
        self.edit_btn.callback = self.edit_account
        self.del_btn = ui.Button(label="🗑️ Delete Account", style=discord.ButtonStyle.red, custom_id="acc_del")
        self.del_btn.callback = self.delete_account
        self.render()

    def rows(self):
        return [
            [self.nav_configs_btn, self.nav_logs_btn],
            [self.add_btn, self.edit_btn, self.del_btn],
        ]

    async def nav_configs(self, interaction: discord.Interaction):
        if not await _guild_only(interaction):
            return
        configs = await config.get_guild_configs(str(interaction.guild_id))
        g_data = await config.get_global_data()
        embed = await config.build_mode_configs_embed(interaction.guild, configs, g_data.get("accounts", []))
        await interaction.response.edit_message(view=ConfigView(page="modes", embed=embed))

    async def nav_logs(self, interaction: discord.Interaction):
        if not await _guild_only(interaction):
            return
        logs = await config.get_guild_logs(str(interaction.guild_id))
        embed = config.build_logs_embed(interaction.guild, logs)
        await interaction.response.edit_message(view=GrinderLogsView(embed=embed))

    async def add_account(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddAccountModal())

    async def edit_account(self, interaction: discord.Interaction):
        await interaction.response.send_modal(EditAccountModal())

    async def delete_account(self, interaction: discord.Interaction):
        await interaction.response.send_modal(DeleteAccountModal())


# =============================================================================
#  CONFIGS
# =============================================================================

class AddConfigModal(ui.Modal, title="Add Configuration"):
    mode = ui.TextInput(
        label="Mode",
        placeholder="autocatch, spam, dotcatch, commaedit or periodicmsg",
        required=True,
    )
    acc_index = ui.TextInput(
        label="Account Number(s)",
        placeholder="e.g. 1 or 1, 2, 3",
        required=True,
    )
    target = ui.TextInput(
        label="Target / Arguments / Channel IDs",
        placeholder="chid1, pikachu test.json  —or—  chid; msg; t1; t2",
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await _guild_only(interaction):
            return

        mode_val = self.mode.value.strip().lower()
        if mode_val not in config.VALID_MODES:
            modes = ", ".join(f"`{m}`" for m in config.VALID_MODES)
            return await interaction.response.send_message(
                embed=error_embed(f"Unknown mode {mode_val}. Use: {modes}"), ephemeral=True
            )

        acc_indices = parse_indices(self.acc_index.value)
        if not acc_indices:
            return await interaction.response.send_message(
                embed=error_embed("Enter account number(s), e.g. 1, 2."), ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        g_data = await config.get_global_data()
        total_accounts = len(g_data.get("accounts", []))
        missing = [a for a in acc_indices if a > total_accounts]
        if missing:
            return await interaction.followup.send(
                embed=error_embed(
                    f"No account {_fmt_nums(missing)}. You have {total_accounts}."
                ),
                ephemeral=True,
            )

        guild_id = str(interaction.guild_id)
        configs = await config.get_guild_configs(guild_id)

        raw_target = self.target.value.strip() if self.target.value else ""

        if mode_val in ["spam", "autocatch", "periodicmsg"] or not raw_target:
            targets_list = [raw_target]
        else:
            targets_list = [t.strip() for t in raw_target.split(",") if t.strip()]

        added_count = 0
        for idx in acc_indices:
            for tgt in targets_list:
                configs.append({
                    "mode": mode_val,
                    "accIndex": idx,
                    "target": tgt.strip(),
                    "paused": False,
                    "pauseUntil": None,
                })
                added_count += 1

        await config.save_guild_configs(guild_id, configs)
        await config.refresh_config_embed(interaction)

        await interaction.followup.send(
            embed=success_embed(
                f"Added **{added_count}** configuration(s).\n"
                f"Mode: `{mode_val}`  •  Accounts: {_fmt_nums(acc_indices)}"
            ),
            ephemeral=True,
        )


def _code(val, limit: int = 300) -> str:
    s = str(val) if val not in (None, "") else "None"
    s = s.replace("`", "'")
    return f"`{s[:limit - 3] + '...' if len(s) > limit else s}`"


class DynamicConfigEditModal(ui.Modal):
    """One input per variable the config has. Blank = keep the current value."""

    def __init__(self, config_idx: int, cfg: dict, fields: list, parent_view: "SequentialEditView"):
        mode = cfg.get("mode", "").lower()
        super().__init__(title=f"Edit Config #{config_idx} · {mode.upper()}"[:45])

        self.config_idx = config_idx
        self.mode = mode
        self.parent_view = parent_view
        self.inputs: dict[str, ui.TextInput] = {}

        for f in fields:
            field = ui.TextInput(
                label=f.label[:45],
                placeholder=fmt_placeholder(f.value),
                style=discord.TextStyle.paragraph if f.paragraph else discord.TextStyle.short,
                required=False,
            )
            self.inputs[f.key] = field
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guild_id = str(interaction.guild_id)
        configs = await config.get_guild_configs(guild_id)
        idx_in_list = self.config_idx - 1
        if not (0 <= idx_in_list < len(configs)):
            return await interaction.followup.send(
                embed=error_embed(f"Config #{self.config_idx} no longer exists."), ephemeral=True
            )

        cfg = configs[idx_in_list]
        accounts = (await config.get_global_data()).get("accounts", [])
        current = {f.key: f.value for f in config.get_editable_fields(cfg, accounts)}

        new = dict(current)
        for key, text_input in self.inputs.items():
            val = text_input.value.strip()
            if val:
                new[key] = val

        # validation
        acc_number = None
        if new.get("accIndex") != current.get("accIndex"):
            val = new["accIndex"]
            if not (val.isdigit() and 1 <= int(val) <= len(accounts)):
                return await interaction.followup.send(
                    embed=error_embed(f"Account must be a number from 1 to {len(accounts)}."), ephemeral=True
                )
            acc_number = int(val)
        if self.mode == "periodicmsg" and new.get("time2") and not new.get("time1"):
            return await interaction.followup.send(
                embed=error_embed("Time 2 needs a Time 1 (start delay) too."), ephemeral=True
            )

        changed = new != current
        if changed:
            cfg["target"] = config.build_target_from_values(self.mode, new)
            if acc_number is not None:
                cfg["accIndex"] = acc_number
                if "token" in cfg:
                    cfg["token"] = accounts[acc_number - 1]
            await config.save_guild_configs(guild_id, configs)
            await config.refresh_config_embed(interaction, override_page="modes", message=self.parent_view.panel)

        await self.parent_view.advance(interaction, self.config_idx, changed)


async def _build_step_embed(
    guild_id: str, indices: list[int], step: int, prefix: str = ""
) -> discord.Embed:
    """Shows the config about to be edited: how many variables it has and each current value."""
    cfg_idx = indices[step]
    configs = await config.get_guild_configs(guild_id)

    if not (1 <= cfg_idx <= len(configs)):
        embed = make_embed("✏️ Edit Configuration", f"{prefix}Config **#{cfg_idx}** no longer exists.")
        embed.add_field(name="Progress", value=f"Step {step + 1} of {len(indices)}", inline=True)
        return embed

    cfg = configs[cfg_idx - 1]
    accounts = (await config.get_global_data()).get("accounts", [])
    fields = config.get_editable_fields(cfg, accounts)
    mode = cfg.get("mode", "unknown")

    lines = [f"**{f.label}:** {_code(f.value)}" for f in fields]
    embed = make_embed(
        "✏️ Edit Configuration",
        f"{prefix}Config **#{cfg_idx}** · mode `{mode}` · **{len(fields)}** variable(s)\n\n"
        + "\n".join(lines),
    )
    embed.add_field(name="Progress", value=f"Step {step + 1} of {len(indices)}", inline=True)
    embed.set_footer(text="Press the button, then fill in only what you want to change. Blank keeps the current value.")
    return embed


class SequentialEditView(EmbedLayout):
    def __init__(
        self,
        indices: list[int],
        guild_id: str,
        current_step: int = 0,
        embed: discord.Embed | None = None,
        panel: discord.Message | None = None,
    ):
        super().__init__(embed, timeout=180)
        self.indices = indices
        self.guild_id = guild_id
        self.current_step = current_step
        self.panel = panel  # the main config panel message, refreshed after each save
        self.finished = False
        self.update_button()

    def update_button(self):
        self.buttons: list[ui.Button] = []
        if self.current_step < len(self.indices):
            cfg_idx = self.indices[self.current_step]
            edit = ui.Button(
                label=f"Edit Config #{cfg_idx}",
                emoji="✏️",
                style=discord.ButtonStyle.blurple,
                custom_id=f"seq_edit_{cfg_idx}_{self.current_step}",
            )
            edit.callback = self.on_button_click
            self.buttons.append(edit)

            if self.current_step + 1 < len(self.indices):
                skip = ui.Button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary)
                skip.callback = self.on_skip
                self.buttons.append(skip)

            cancel = ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
            cancel.callback = self.on_cancel
            self.buttons.append(cancel)
        self.render()

    def rows(self):
        return [] if self.finished else [self.buttons]

    async def on_button_click(self, interaction: discord.Interaction):
        configs = await config.get_guild_configs(self.guild_id)
        cfg_idx = self.indices[self.current_step]

        if 1 <= cfg_idx <= len(configs):
            cfg = configs[cfg_idx - 1]
            accounts = (await config.get_global_data()).get("accounts", [])
            fields = config.get_editable_fields(cfg, accounts)
            await interaction.response.send_modal(DynamicConfigEditModal(cfg_idx, cfg, fields, self))
        else:
            await interaction.response.send_message(
                embed=error_embed(f"Config #{cfg_idx} no longer exists."), ephemeral=True
            )

    async def on_skip(self, interaction: discord.Interaction):
        skipped = self.indices[self.current_step]
        next_step = self.current_step + 1
        embed = await _build_step_embed(
            self.guild_id, self.indices, next_step, prefix=f"⏭️ Skipped config **#{skipped}**.\n\n"
        )
        self.superseded = True
        await interaction.response.edit_message(
            view=SequentialEditView(
                self.indices, self.guild_id, current_step=next_step, embed=embed, panel=self.panel
            ),
        )

    async def on_cancel(self, interaction: discord.Interaction):
        self.superseded = True
        self.finished = True
        await self.push(interaction, info_embed("Editing cancelled."))

    async def advance(self, interaction: discord.Interaction, completed_idx: int, changed: bool = True):
        note = (
            f"✅ Config **#{completed_idx}** saved.\n\n"
            if changed else f"↩️ Config **#{completed_idx}** unchanged.\n\n"
        )
        next_step = self.current_step + 1
        if next_step < len(self.indices):
            embed = await _build_step_embed(self.guild_id, self.indices, next_step, prefix=note)
            await interaction.followup.send(
                view=SequentialEditView(
                    self.indices, self.guild_id, current_step=next_step, embed=embed, panel=self.panel
                ),
                ephemeral=True,
            )
        else:
            done = ", ".join(f"#{i}" for i in self.indices)
            await interaction.followup.send(
                embed=success_embed(f"{note}All done! Went through configuration(s): {done}"), ephemeral=True
            )


class PromptEditConfigModal(ui.Modal, title="Edit Configuration"):
    index = ui.TextInput(
        label="Config Number(s)",
        placeholder="e.g. 1 or 1, 2, 3",
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await _guild_only(interaction):
            return

        indices = parse_indices(self.index.value)
        if not indices:
            return await interaction.response.send_message(
                embed=error_embed("Enter config number(s)."), ephemeral=True
            )

        guild_id = str(interaction.guild_id)
        configs = await config.get_guild_configs(guild_id)

        invalid = [i for i in indices if i > len(configs)]
        if invalid:
            return await interaction.response.send_message(
                embed=error_embed(
                    f"No config {_fmt_nums(invalid)}. This server has {len(configs)}."
                ),
                ephemeral=True,
            )

        embed = await _build_step_embed(guild_id, indices, 0)
        await interaction.response.send_message(
            view=SequentialEditView(indices, guild_id, embed=embed, panel=interaction.message),
            ephemeral=True,
        )


class RemoveConfigModal(ui.Modal, title="Remove Configuration"):
    index = ui.TextInput(label="Config Number(s)", placeholder="e.g. 1 or 1, 2, 3", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not await _guild_only(interaction):
            return

        numbers = parse_indices(self.index.value)
        if not numbers:
            return await interaction.response.send_message(
                embed=error_embed("Enter config number(s)."), ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        configs = await config.get_guild_configs(guild_id)

        removed = [n for n in numbers if n <= len(configs)]
        for n in sorted(removed, reverse=True):
            configs.pop(n - 1)

        if not removed:
            return await interaction.followup.send(
                embed=error_embed(f"None exist. This server has {len(configs)}."),
                ephemeral=True,
            )

        await config.save_guild_configs(guild_id, configs)
        await config.refresh_config_embed(interaction)
        await interaction.followup.send(
            embed=success_embed(f"Removed **{len(removed)}** config(s): {_fmt_nums(sorted(removed))}"),
            ephemeral=True,
        )


# =============================================================================
#  EXCLUDES
# =============================================================================

class AddExcludeModal(ui.Modal, title="Add Excludes"):
    pokemon = ui.TextInput(
        label="Pokemon Name(s)",
        placeholder="pikachu, gimmighoul, raikou --xnon",
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await _guild_only(interaction):
            return

        items = [i.strip() for i in self.pokemon.value.strip().split(",") if i.strip()]
        if not items:
            return await interaction.response.send_message(embed=error_embed("Enter a Pokemon name."), ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)

        excludes = await config.get_guild_excludes(guild_id)
        added_names = []

        for item in items:
            is_xnon = "--xnon" in item.lower()
            pname = re.sub(r"(?i)\s*--xnon\s*", "", item).strip()
            if pname:
                excludes.append({"name": pname, "xnon": is_xnon})
                added_names.append(f"`{pname}{' (--xnon)' if is_xnon else ''}`")

        if not added_names:
            return await interaction.followup.send(embed=error_embed("No valid Pokemon names found."), ephemeral=True)

        await config.save_guild_excludes(guild_id, excludes)
        await config.refresh_config_embed(interaction)
        await interaction.followup.send(
            embed=success_embed(f"Added to excludes: {', '.join(added_names)}"), ephemeral=True
        )


class RemoveExcludeModal(ui.Modal, title="Remove Excludes"):
    pokemon = ui.TextInput(label="Pokemon Name(s)", placeholder="pikachu, gimmighoul, raikou", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not await _guild_only(interaction):
            return

        targets = {i.strip().lower() for i in self.pokemon.value.strip().split(",") if i.strip()}
        if not targets:
            return await interaction.response.send_message(embed=error_embed("Enter a Pokemon name."), ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)

        excludes = await config.get_guild_excludes(guild_id)
        kept = [
            ex for ex in excludes
            if (ex.get("name") if isinstance(ex, dict) else str(ex)).lower() not in targets
        ]
        removed = len(excludes) - len(kept)

        if removed == 0:
            return await interaction.followup.send(
                embed=warning_embed("None of those Pokemon were in the excludes list."), ephemeral=True
            )

        await config.save_guild_excludes(guild_id, kept)
        await config.refresh_config_embed(interaction)
        await interaction.followup.send(embed=success_embed(f"Removed **{removed}** exclude(s)."), ephemeral=True)


# =============================================================================
#  DETECTOR BOTS
# =============================================================================

class AddBotModal(ui.Modal, title="Add Detector Bot"):
    bot_id = ui.TextInput(label="Bot User ID", placeholder="123456789012345678 or a @mention", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not await _guild_only(interaction):
            return

        bot_id_val = extract_id(self.bot_id.value)
        if not bot_id_val:
            return await interaction.response.send_message(
                embed=error_embed("Invalid bot ID."), ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        bots = await config.get_guild_detector_bots(guild_id)

        if bot_id_val in bots:
            return await interaction.followup.send(
                embed=warning_embed(f"<@{bot_id_val}> is already a detector bot."), ephemeral=True
            )

        bots.append(bot_id_val)
        await config.save_guild_detector_bots(guild_id, bots)
        await config.refresh_config_embed(interaction, override_page="detector_bots")
        await interaction.followup.send(embed=success_embed(f"Added detector bot <@{bot_id_val}>."), ephemeral=True)


class RemoveBotModal(ui.Modal, title="Remove Detector Bot"):
    index = ui.TextInput(label="Bot Number(s)", placeholder="e.g. 1 or 1, 2", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not await _guild_only(interaction):
            return

        numbers = parse_indices(self.index.value)
        if not numbers:
            return await interaction.response.send_message(
                embed=error_embed("Enter bot number(s)."), ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        bots = await config.get_guild_detector_bots(guild_id)

        removed = [n for n in numbers if n <= len(bots)]
        for n in sorted(removed, reverse=True):
            bots.pop(n - 1)

        if not removed:
            return await interaction.followup.send(
                embed=error_embed(f"None exist. There are {len(bots)}."), ephemeral=True
            )

        await config.save_guild_detector_bots(guild_id, bots)
        await config.refresh_config_embed(interaction, override_page="detector_bots")
        await interaction.followup.send(embed=success_embed(f"Removed **{len(removed)}** detector bot(s)."), ephemeral=True)


# =============================================================================
#  CONFIG VIEW (Modes / AutoCatch / Excludes / Bots)
# =============================================================================

RESET_NOUNS = {
    "modes": "configurations",
    "autocatch": "configurations",
    "excludes": "excludes",
    "detector_bots": "detector bots",
}


class ConfigView(EmbedLayout):
    def __init__(self, page="modes", embed: discord.Embed | None = None):
        super().__init__(embed, timeout=None)
        self.page = page
        self.reset_noun = RESET_NOUNS.get(page, "configurations")

        def active(name: str) -> discord.ButtonStyle:
            return discord.ButtonStyle.blurple if page == name else discord.ButtonStyle.grey

        def button(label, style, custom_id, callback):
            item = ui.Button(label=label, style=style, custom_id=custom_id)
            item.callback = callback
            return item

        self.nav_accounts = button("⚙️ Accounts", discord.ButtonStyle.grey, "cfg_nav_accounts", self.on_nav_accounts)
        self.nav_logs = button("📜 Logs", discord.ButtonStyle.grey, "cfg_nav_logs", self.on_nav_logs)
        self.nav_modes = button("🎛️ Modes", active("modes"), "cfg_nav_modes", self.on_nav_modes)
        self.nav_autocatch = button("🎯 AutoCatch", active("autocatch"), "cfg_nav_autocatch", self.on_nav_autocatch)
        self.nav_excludes = button("🚫 Excludes", active("excludes"), "cfg_nav_excludes", self.on_nav_excludes)
        self.nav_bots = button("🤖 Bots", active("detector_bots"), "cfg_nav_bots", self.on_nav_bots)

        self.add_cfg = button("➕ Add Config", discord.ButtonStyle.green, "cfg_add", self.on_add_cfg)
        self.edit_cfg = button("✏️ Edit Config", discord.ButtonStyle.blurple, "cfg_edit", self.on_edit_cfg)
        self.rem_cfg = button("🗑️ Remove Config", discord.ButtonStyle.red, "cfg_rem", self.on_rem_cfg)

        self.add_ex = button("➕ Add Excludes", discord.ButtonStyle.green, "cfg_add_ex", self.on_add_ex)
        self.rem_ex = button("🗑️ Remove Excludes", discord.ButtonStyle.red, "cfg_rem_ex", self.on_rem_ex)
        self.add_bot = button("➕ Add Bot", discord.ButtonStyle.green, "cfg_add_bot", self.on_add_bot)
        self.rem_bot = button("🗑️ Remove Bot", discord.ButtonStyle.red, "cfg_rem_bot", self.on_rem_bot)

        self.reset_all = button(
            f"💥 Reset All {self.reset_noun.title()}", discord.ButtonStyle.danger, "cfg_reset_all", self.on_reset_all
        )
        self.render()

    def rows(self):
        page = self.page
        rows = [
            [self.nav_accounts, self.nav_logs],
            [self.nav_modes, self.nav_autocatch, self.nav_excludes, self.nav_bots],
        ]

        if page == "autocatch":
            rows.append([self.add_cfg, self.rem_cfg])
        elif page == "modes":
            rows.append([self.add_cfg, self.edit_cfg, self.rem_cfg])

        if page == "excludes":
            rows.append([self.add_ex, self.rem_ex])
        if page == "detector_bots":
            rows.append([self.add_bot, self.rem_bot])

        rows.append([self.reset_all])
        return rows

    async def change_page(self, interaction: discord.Interaction, new_page: str):
        guild_id = str(interaction.guild_id)
        g_data = await config.get_global_data()
        accounts = g_data.get("accounts", [])

        if new_page == "accounts":
            embed = config.build_accounts_embed(accounts)
            await interaction.response.edit_message(view=AccountsView(embed=embed))
            return
        elif new_page == "logs":
            logs = await config.get_guild_logs(guild_id)
            embed = config.build_logs_embed(interaction.guild, logs)
            await interaction.response.edit_message(view=GrinderLogsView(embed=embed))
            return

        if new_page == "autocatch":
            autocatch_data = await config.get_autocatch_status()
            embed = await config.build_autocatch_configs_embed(interaction.guild, autocatch_data, accounts)
        elif new_page == "excludes":
            excludes = await config.get_guild_excludes(guild_id)
            embed = await config.build_excludes_configs_embed(interaction.guild, excludes)
        elif new_page == "detector_bots":
            bots = await config.get_guild_detector_bots(guild_id)
            embed = await config.build_detector_bots_embed(interaction.guild, bots)
        else:
            new_page = "modes"
            configs = await config.get_guild_configs(guild_id)
            embed = await config.build_mode_configs_embed(interaction.guild, configs, accounts)

        await interaction.response.edit_message(view=ConfigView(page=new_page, embed=embed))

    async def on_nav_accounts(self, interaction: discord.Interaction):
        await self.change_page(interaction, "accounts")

    async def on_nav_logs(self, interaction: discord.Interaction):
        if await _guild_only(interaction):
            await self.change_page(interaction, "logs")

    async def on_nav_modes(self, interaction: discord.Interaction):
        if await _guild_only(interaction):
            await self.change_page(interaction, "modes")

    async def on_nav_autocatch(self, interaction: discord.Interaction):
        if await _guild_only(interaction):
            await self.change_page(interaction, "autocatch")

    async def on_nav_excludes(self, interaction: discord.Interaction):
        if await _guild_only(interaction):
            await self.change_page(interaction, "excludes")

    async def on_nav_bots(self, interaction: discord.Interaction):
        if await _guild_only(interaction):
            await self.change_page(interaction, "detector_bots")

    async def on_add_cfg(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddConfigModal())

    async def on_edit_cfg(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PromptEditConfigModal())

    async def on_rem_cfg(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RemoveConfigModal())

    async def on_add_ex(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddExcludeModal())

    async def on_rem_ex(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RemoveExcludeModal())

    async def on_add_bot(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddBotModal())

    async def on_rem_bot(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RemoveBotModal())

    async def on_reset_all(self, interaction: discord.Interaction):
        if not await _guild_only(interaction):
            return

        confirm_view = ConfirmLayout(
            interaction.user,
            f"All {self.reset_noun} will be removed. This can't be undone.",
            title="⚠️ Reset?",
            confirm_label=f"Reset all {self.reset_noun}",
            cancel_label="Cancel",
            cancel_text="Cancelled.",
            timeout_text="Timed out.",
        )
        await interaction.response.send_message(view=confirm_view, ephemeral=True)
        confirm_view.message = await interaction.original_response()

        await confirm_view.wait()

        if confirm_view.value is True:
            guild_id = str(interaction.guild_id)
            if self.page in ["modes", "autocatch"]:
                await config.save_guild_configs(guild_id, [])
            elif self.page == "excludes":
                await config.save_guild_excludes(guild_id, [])
            elif self.page == "detector_bots":
                await config.save_guild_detector_bots(guild_id, [])

            await config.refresh_config_embed(interaction, override_page=self.page)
            await confirm_view.show(success_embed(f"All {self.reset_noun} have been reset."))


# =============================================================================
#  LOGS
# =============================================================================

class SetLogModal(ui.Modal):
    channel_input = ui.TextInput(
        label="Log Channel",
        placeholder="Channel ID or #mention, e.g. 123456789012345678",
        required=True,
    )

    def __init__(self, log_type: str):
        self.log_type = log_type
        super().__init__(title=f"Set {log_type.capitalize()} Log Channel")

    async def on_submit(self, interaction: discord.Interaction):
        if not await _guild_only(interaction):
            return

        channel_id = extract_id(self.channel_input.value)
        if not channel_id:
            return await interaction.response.send_message(
                embed=error_embed("Invalid channel."), ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)

        await config.save_guild_log(guild_id, self.log_type, channel_id)

        logs = await config.get_guild_logs(guild_id)
        if interaction.guild:
            await _edit_panel(interaction, GrinderLogsView(embed=config.build_logs_embed(interaction.guild, logs)))

        await interaction.followup.send(
            embed=success_embed(f"**{self.log_type.capitalize()}** logs will now go to <#{channel_id}>."),
            ephemeral=True,
        )


class GrinderLogsView(EmbedLayout):
    def __init__(self, embed: discord.Embed | None = None):
        super().__init__(embed, timeout=None)

        self.nav_configs_btn = ui.Button(label="📋 Configs", style=discord.ButtonStyle.grey, custom_id="log_nav_cfg")
        self.nav_configs_btn.callback = self.nav_configs
        self.nav_accounts_btn = ui.Button(label="⚙️ Accounts", style=discord.ButtonStyle.grey, custom_id="log_nav_acc")
        self.nav_accounts_btn.callback = self.nav_accounts
        self.alerts_btn = ui.Button(label="🔔 Set Alerts", style=discord.ButtonStyle.blurple, custom_id="log_alerts")
        self.alerts_btn.callback = self.set_alerts
        self.autocatch_btn = ui.Button(label="🎯 Set Autocatch", style=discord.ButtonStyle.blurple, custom_id="log_autocatch")
        self.autocatch_btn.callback = self.set_autocatch
        self.switch_btn = ui.Button(label="🔀 Set Switch", style=discord.ButtonStyle.blurple, custom_id="log_switch")
        self.switch_btn.callback = self.set_switch
        self.render()

    def rows(self):
        return [
            [self.nav_configs_btn, self.nav_accounts_btn],
            [self.alerts_btn, self.autocatch_btn, self.switch_btn],
        ]

    async def nav_configs(self, interaction: discord.Interaction):
        if not await _guild_only(interaction):
            return
        configs = await config.get_guild_configs(str(interaction.guild_id))
        g_data = await config.get_global_data()
        embed = await config.build_mode_configs_embed(interaction.guild, configs, g_data.get("accounts", []))
        await interaction.response.edit_message(view=ConfigView(page="modes", embed=embed))

    async def nav_accounts(self, interaction: discord.Interaction):
        data = await config.get_global_data()
        embed = config.build_accounts_embed(data.get("accounts", []))
        await interaction.response.edit_message(view=AccountsView(embed=embed))

    async def set_alerts(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SetLogModal("alerts"))

    async def set_autocatch(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SetLogModal("autocatch"))

    async def set_switch(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SetLogModal("switch"))