import re

import discord
from discord import ui

from cogs.config import grinder as config
from views.common import (
    BaseView,
    ConfirmView,
    error_embed,
    extract_id,
    info_embed,
    make_embed,
    parse_indices,
    success_embed,
    themed,
    warning_embed,
)


# --- Small helpers --------------------------------------------------------------

def fmt_placeholder(val) -> str:
    s = str(val) if val is not None and str(val) != "" else "None"
    if len(s) > 45:
        s = s[:42] + "..."
    return f"Current: {s} • leave blank to keep"


async def _guild_only(interaction: discord.Interaction) -> bool:
    """Reply with a friendly error and return False when used outside a server."""
    if interaction.guild_id:
        return True
    await interaction.response.send_message(embed=error_embed("This can only be used inside a server."), ephemeral=True)
    return False


async def _edit_panel(interaction: discord.Interaction, embed: discord.Embed):
    """Refresh the panel message the modal was opened from (best effort)."""
    if interaction.message:
        try:
            await interaction.message.edit(embed=themed(embed))
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
            return await interaction.response.send_message(embed=error_embed("The token can't be empty."), ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        data = await config.get_global_data()

        if token in data["accounts"]:
            return await interaction.followup.send(
                embed=warning_embed("That token is already saved."), ephemeral=True
            )

        data["accounts"].append(token)
        await config.save_global_data(data)
        await _edit_panel(interaction, config.build_accounts_embed(data["accounts"]))

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
                embed=error_embed("Enter the account number as a digit, e.g. `1`."), ephemeral=True
            )
        number = nums[0]

        await interaction.response.defer(ephemeral=True)
        data = await config.get_global_data()

        if not (1 <= number <= len(data["accounts"])):
            return await interaction.followup.send(
                embed=error_embed(f"There's no account **#{number}**. You have {len(data['accounts'])} saved."),
                ephemeral=True,
            )

        data["accounts"].pop(number - 1)
        await config.save_global_data(data)
        await _edit_panel(interaction, config.build_accounts_embed(data["accounts"]))

        note = "\nAccounts after it have moved up by one number." if number <= len(data["accounts"]) else ""
        await interaction.followup.send(embed=success_embed(f"Account **#{number}** removed.{note}"), ephemeral=True)


class EditAccountModal(ui.Modal, title="Edit Account Token"):
    index = ui.TextInput(label="Account Number", placeholder="e.g. 1", required=True)
    new_token = ui.TextInput(label="New Token", placeholder="Paste the new token here", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        nums = parse_indices(self.index.value)
        if not nums:
            return await interaction.response.send_message(
                embed=error_embed("Enter the account number as a digit, e.g. `1`."), ephemeral=True
            )
        number = nums[0]

        await interaction.response.defer(ephemeral=True)
        data = await config.get_global_data()

        if not (1 <= number <= len(data["accounts"])):
            return await interaction.followup.send(
                embed=error_embed(f"There's no account **#{number}**. You have {len(data['accounts'])} saved."),
                ephemeral=True,
            )

        data["accounts"][number - 1] = self.new_token.value.strip()
        await config.save_global_data(data)
        await _edit_panel(interaction, config.build_accounts_embed(data["accounts"]))

        await interaction.followup.send(embed=success_embed(f"Account **#{number}** token updated."), ephemeral=True)


class AccountsView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    # --- ROW 0: NAVIGATION ---
    @ui.button(label="📋 Configs", style=discord.ButtonStyle.grey, custom_id="acc_nav_cfg", row=0)
    async def nav_configs(self, interaction: discord.Interaction, button: ui.Button):
        if not await _guild_only(interaction):
            return
        configs = await config.get_guild_configs(str(interaction.guild_id))
        g_data = await config.get_global_data()
        embed = await config.build_mode_configs_embed(interaction.guild, configs, g_data.get("accounts", []))
        await interaction.response.edit_message(embed=themed(embed), view=ConfigView(page="modes"))

    @ui.button(label="📜 Logs", style=discord.ButtonStyle.grey, custom_id="acc_nav_logs", row=0)
    async def nav_logs(self, interaction: discord.Interaction, button: ui.Button):
        if not await _guild_only(interaction):
            return
        logs = await config.get_guild_logs(str(interaction.guild_id))
        embed = config.build_logs_embed(interaction.guild, logs)
        await interaction.response.edit_message(embed=themed(embed), view=GrinderLogsView())

    # --- ROW 1: ACTIONS ---
    @ui.button(label="➕ Add Account", style=discord.ButtonStyle.green, custom_id="acc_add", row=1)
    async def add_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AddAccountModal())

    @ui.button(label="✏️ Edit Account", style=discord.ButtonStyle.blurple, custom_id="acc_edit", row=1)
    async def edit_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(EditAccountModal())

    @ui.button(label="🗑️ Delete Account", style=discord.ButtonStyle.red, custom_id="acc_del", row=1)
    async def del_btn(self, interaction: discord.Interaction, button: ui.Button):
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
                embed=error_embed(f"Unknown mode `{mode_val}`.\nChoose one of: {modes}"), ephemeral=True
            )

        acc_indices = parse_indices(self.acc_index.value)
        if not acc_indices:
            return await interaction.response.send_message(
                embed=error_embed("Account number must be one or more digits, e.g. `1, 2`."), ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        g_data = await config.get_global_data()
        total_accounts = len(g_data.get("accounts", []))
        missing = [a for a in acc_indices if a > total_accounts]
        if missing:
            return await interaction.followup.send(
                embed=error_embed(
                    f"Account {_fmt_nums(missing)} doesn't exist. You have **{total_accounts}** saved account(s)."
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


class DynamicConfigEditModal(ui.Modal):
    def __init__(self, config_idx: int, cfg: dict, parent_view: "SequentialEditView"):
        mode = cfg.get("mode", "").lower()
        super().__init__(title=f"Edit Config #{config_idx} · {mode.upper()}"[:45])

        self.config_idx = config_idx
        self.cfg = cfg
        self.parent_view = parent_view
        self.mode = mode
        self.details = config.get_config_details(cfg)
        self.inputs: dict[str, ui.TextInput] = {}

        def add(key: str, label: str, current, *, paragraph: bool = False):
            field = ui.TextInput(
                label=label,
                placeholder=fmt_placeholder(current),
                style=discord.TextStyle.paragraph if paragraph else discord.TextStyle.short,
                required=False,
            )
            self.inputs[key] = field
            self.add_item(field)

        add("accIndex", "Account Number", self.details.get("accIndex", 1))

        if mode in ["autocatch", "dotcatch", "commaedit"]:
            add("chid", "Channel ID(s)", self.details.get("chid"))
            add("pokemons", "Pokemon (comma separated)", self.details.get("pokemons"))
            add("datafile", "Data File", self.details.get("datafile"))

        elif mode == "periodicmsg":
            add("chid", "Channel ID", self.details.get("chid"))
            add("message", "Message", self.details.get("message"), paragraph=True)
            add("time1", "Start Delay (Time 1)", self.details.get("time1"))
            add("time2", "Interval (Time 2)", self.details.get("time2"))

        elif mode == "spam":
            add("chid", "Target / Channel ID", self.details.get("chid") or self.details.get("target"))

        else:
            add("target", "Target / Arguments", self.details.get("target"))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        updated_fields = {}
        for key, text_input in self.inputs.items():
            val = text_input.value.strip()
            if val:
                if key == "accIndex":
                    updated_fields["accIndex"] = int(val) if val.isdigit() else self.details.get("accIndex", 1)
                else:
                    updated_fields[key] = val
            else:
                if key == "accIndex":
                    updated_fields["accIndex"] = self.details.get("accIndex", 1)
                else:
                    updated_fields[key] = self.details.get(key, "")

        new_target, new_xnon = config.build_target_string_for_mode(self.mode, updated_fields)

        guild_id = str(interaction.guild_id)
        configs = await config.get_guild_configs(guild_id)

        idx_in_list = self.config_idx - 1
        if 0 <= idx_in_list < len(configs):
            configs[idx_in_list]["accIndex"] = updated_fields.get("accIndex", 1)
            configs[idx_in_list]["target"] = new_target
            configs[idx_in_list]["xnon"] = new_xnon

            await config.save_guild_configs(guild_id, configs)
            await config.refresh_config_embed(interaction)

        await self.parent_view.advance(interaction, self.config_idx)


def _edit_step_embed(indices: list[int], step: int, mode: str, saved: int | None = None) -> discord.Embed:
    info = config.MODE_PARAMS_INFO.get(mode, {"required": [], "optional": []})
    lead = f"✅ Config **#{saved}** saved.\n\n" if saved else ""
    embed = make_embed(
        "✏️ Edit Configuration",
        f"{lead}Up next: config **#{indices[step]}** · mode `{mode}`",
    )
    embed.add_field(name="Progress", value=f"Step {step + 1} of {len(indices)}", inline=True)
    embed.add_field(
        name="Parameters",
        value=f"{len(info['required'])} required · {len(info['optional'])} optional",
        inline=True,
    )
    embed.set_footer(text="Press the button to open the form. Blank fields keep their current value.")
    return embed


class SequentialEditView(BaseView):
    def __init__(self, indices: list[int], guild_id: str, current_step: int = 0):
        super().__init__(timeout=180)
        self.indices = indices
        self.guild_id = guild_id
        self.current_step = current_step
        self.update_button()

    def update_button(self):
        self.clear_items()
        if self.current_step >= len(self.indices):
            return

        cfg_idx = self.indices[self.current_step]
        edit = ui.Button(
            label=f"Edit Config #{cfg_idx}",
            emoji="✏️",
            style=discord.ButtonStyle.blurple,
            custom_id=f"seq_edit_{cfg_idx}_{self.current_step}",
        )
        edit.callback = self.on_button_click
        self.add_item(edit)

        if self.current_step + 1 < len(self.indices):
            skip = ui.Button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary)
            skip.callback = self.on_skip
            self.add_item(skip)

        cancel = ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self.on_cancel
        self.add_item(cancel)

    async def _mode_of(self, cfg_idx: int) -> str:
        configs = await config.get_guild_configs(self.guild_id)
        if 1 <= cfg_idx <= len(configs):
            return configs[cfg_idx - 1].get("mode", "unknown")
        return "unknown"

    async def on_button_click(self, interaction: discord.Interaction):
        configs = await config.get_guild_configs(self.guild_id)
        cfg_idx = self.indices[self.current_step]

        if 1 <= cfg_idx <= len(configs):
            await interaction.response.send_modal(DynamicConfigEditModal(cfg_idx, configs[cfg_idx - 1], self))
        else:
            await interaction.response.send_message(
                embed=error_embed(f"Config #{cfg_idx} no longer exists."), ephemeral=True
            )

    async def on_skip(self, interaction: discord.Interaction):
        skipped = self.indices[self.current_step]
        next_step = self.current_step + 1
        mode = await self._mode_of(self.indices[next_step])
        embed = _edit_step_embed(self.indices, next_step, mode)
        embed.description = f"⏭️ Skipped config **#{skipped}**.\n\n" + embed.description
        self.superseded = True
        await interaction.response.edit_message(
            embed=themed(embed),
            view=SequentialEditView(self.indices, self.guild_id, current_step=next_step),
        )

    async def on_cancel(self, interaction: discord.Interaction):
        self.superseded = True
        await interaction.response.edit_message(embed=info_embed("Editing cancelled."), view=None)

    async def advance(self, interaction: discord.Interaction, completed_idx: int):
        next_step = self.current_step + 1
        if next_step < len(self.indices):
            mode = await self._mode_of(self.indices[next_step])
            await interaction.followup.send(
                embed=_edit_step_embed(self.indices, next_step, mode, saved=completed_idx),
                view=SequentialEditView(self.indices, self.guild_id, current_step=next_step),
                ephemeral=True,
            )
        else:
            done = ", ".join(f"#{i}" for i in self.indices)
            await interaction.followup.send(
                embed=success_embed(f"All done! Updated configuration(s): {done}"), ephemeral=True
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
                embed=error_embed("Enter one or more config numbers, e.g. `1, 2`."), ephemeral=True
            )

        guild_id = str(interaction.guild_id)
        configs = await config.get_guild_configs(guild_id)

        invalid = [i for i in indices if i > len(configs)]
        if invalid:
            return await interaction.response.send_message(
                embed=error_embed(
                    f"Config {_fmt_nums(invalid)} doesn't exist. This server has **{len(configs)}** configuration(s)."
                ),
                ephemeral=True,
            )

        first_mode = configs[indices[0] - 1].get("mode", "").lower()
        await interaction.response.send_message(
            embed=_edit_step_embed(indices, 0, first_mode),
            view=SequentialEditView(indices, guild_id),
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
                embed=error_embed("Enter one or more config numbers, e.g. `1, 2`."), ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        configs = await config.get_guild_configs(guild_id)

        removed = [n for n in numbers if n <= len(configs)]
        for n in sorted(removed, reverse=True):
            configs.pop(n - 1)

        if not removed:
            return await interaction.followup.send(
                embed=error_embed(f"None of those exist. This server has **{len(configs)}** configuration(s)."),
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
            return await interaction.response.send_message(embed=error_embed("Enter at least one Pokemon name."), ephemeral=True)

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
            return await interaction.response.send_message(embed=error_embed("Enter at least one Pokemon name."), ephemeral=True)

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
                embed=error_embed("Enter a valid numeric bot ID or mention."), ephemeral=True
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
                embed=error_embed("Enter one or more bot numbers, e.g. `1, 2`."), ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        bots = await config.get_guild_detector_bots(guild_id)

        removed = [n for n in numbers if n <= len(bots)]
        for n in sorted(removed, reverse=True):
            bots.pop(n - 1)

        if not removed:
            return await interaction.followup.send(
                embed=error_embed(f"None of those exist. There are **{len(bots)}** detector bot(s)."), ephemeral=True
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


class ConfigView(ui.View):
    def __init__(self, page="modes"):
        super().__init__(timeout=None)
        self.page = page
        self.reset_noun = RESET_NOUNS.get(page, "configurations")

        # Highlight the active tab
        self.nav_modes.style = discord.ButtonStyle.blurple if page == "modes" else discord.ButtonStyle.grey
        self.nav_autocatch.style = discord.ButtonStyle.blurple if page == "autocatch" else discord.ButtonStyle.grey
        self.nav_excludes.style = discord.ButtonStyle.blurple if page == "excludes" else discord.ButtonStyle.grey
        self.nav_bots.style = discord.ButtonStyle.blurple if page == "detector_bots" else discord.ButtonStyle.grey

        self.reset_all.label = f"💥 Reset All {self.reset_noun.title()}"

        # Only keep the buttons that make sense for this page
        if page == "autocatch":
            self.remove_item(self.edit_cfg)
        elif page not in ["modes", "autocatch"]:
            self.remove_item(self.add_cfg)
            self.remove_item(self.edit_cfg)
            self.remove_item(self.rem_cfg)

        if page != "excludes":
            self.remove_item(self.add_ex)
            self.remove_item(self.rem_ex)
        if page != "detector_bots":
            self.remove_item(self.add_bot)
            self.remove_item(self.rem_bot)

    async def change_page(self, interaction: discord.Interaction, new_page: str):
        guild_id = str(interaction.guild_id)
        g_data = await config.get_global_data()
        accounts = g_data.get("accounts", [])

        if new_page == "accounts":
            embed = config.build_accounts_embed(accounts)
            await interaction.response.edit_message(embed=themed(embed), view=AccountsView())
            return
        elif new_page == "logs":
            logs = await config.get_guild_logs(guild_id)
            embed = config.build_logs_embed(interaction.guild, logs)
            await interaction.response.edit_message(embed=themed(embed), view=GrinderLogsView())
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

        await interaction.response.edit_message(embed=themed(embed), view=ConfigView(page=new_page))

    # --- ROW 0: SECTION NAVIGATION ---
    @ui.button(label="⚙️ Accounts", style=discord.ButtonStyle.grey, custom_id="cfg_nav_accounts", row=0)
    async def nav_accounts(self, interaction: discord.Interaction, button: ui.Button):
        await self.change_page(interaction, "accounts")

    @ui.button(label="📜 Logs", style=discord.ButtonStyle.grey, custom_id="cfg_nav_logs", row=0)
    async def nav_logs(self, interaction: discord.Interaction, button: ui.Button):
        if await _guild_only(interaction):
            await self.change_page(interaction, "logs")

    # --- ROW 1: CONFIG TABS ---
    @ui.button(label="🎛️ Modes", style=discord.ButtonStyle.grey, custom_id="cfg_nav_modes", row=1)
    async def nav_modes(self, interaction: discord.Interaction, button: ui.Button):
        if await _guild_only(interaction):
            await self.change_page(interaction, "modes")

    @ui.button(label="🎯 AutoCatch", style=discord.ButtonStyle.grey, custom_id="cfg_nav_autocatch", row=1)
    async def nav_autocatch(self, interaction: discord.Interaction, button: ui.Button):
        if await _guild_only(interaction):
            await self.change_page(interaction, "autocatch")

    @ui.button(label="🚫 Excludes", style=discord.ButtonStyle.grey, custom_id="cfg_nav_excludes", row=1)
    async def nav_excludes(self, interaction: discord.Interaction, button: ui.Button):
        if await _guild_only(interaction):
            await self.change_page(interaction, "excludes")

    @ui.button(label="🤖 Bots", style=discord.ButtonStyle.grey, custom_id="cfg_nav_bots", row=1)
    async def nav_bots(self, interaction: discord.Interaction, button: ui.Button):
        if await _guild_only(interaction):
            await self.change_page(interaction, "detector_bots")

    # --- ROW 2: CONFIG ACTIONS ---
    @ui.button(label="➕ Add Config", style=discord.ButtonStyle.green, custom_id="cfg_add", row=2)
    async def add_cfg(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AddConfigModal())

    @ui.button(label="✏️ Edit Config", style=discord.ButtonStyle.blurple, custom_id="cfg_edit", row=2)
    async def edit_cfg(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(PromptEditConfigModal())

    @ui.button(label="🗑️ Remove Config", style=discord.ButtonStyle.red, custom_id="cfg_rem", row=2)
    async def rem_cfg(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(RemoveConfigModal())

    # --- ROW 3: EXCLUDE & BOT ACTIONS ---
    @ui.button(label="➕ Add Excludes", style=discord.ButtonStyle.green, custom_id="cfg_add_ex", row=3)
    async def add_ex(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AddExcludeModal())

    @ui.button(label="🗑️ Remove Excludes", style=discord.ButtonStyle.red, custom_id="cfg_rem_ex", row=3)
    async def rem_ex(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(RemoveExcludeModal())

    @ui.button(label="➕ Add Bot", style=discord.ButtonStyle.green, custom_id="cfg_add_bot", row=3)
    async def add_bot(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AddBotModal())

    @ui.button(label="🗑️ Remove Bot", style=discord.ButtonStyle.red, custom_id="cfg_rem_bot", row=3)
    async def rem_bot(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(RemoveBotModal())

    # --- ROW 4: DANGER ZONE (kept apart so it's hard to hit by accident) ---
    @ui.button(label="💥 Reset All", style=discord.ButtonStyle.danger, custom_id="cfg_reset_all", row=4)
    async def reset_all(self, interaction: discord.Interaction, button: ui.Button):
        if not await _guild_only(interaction):
            return

        confirm_view = ConfirmView(
            author=interaction.user, confirm_label=f"Yes, reset all {self.reset_noun}", cancel_label="Cancel"
        )
        await interaction.response.send_message(
            embed=warning_embed(
                f"This permanently removes **all {self.reset_noun}** for this server and can't be undone.",
                title="Reset everything?",
            ),
            view=confirm_view,
            ephemeral=True,
        )

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
            result = success_embed(f"All {self.reset_noun} have been reset.")
        elif confirm_view.value is False:
            result = info_embed("Reset cancelled. Nothing was changed.")
        else:
            result = info_embed("Reset timed out. Nothing was changed.")

        try:
            await interaction.edit_original_response(embed=themed(result), view=None)
        except discord.HTTPException:
            await interaction.followup.send(embed=themed(result), ephemeral=True)


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
                embed=error_embed("Enter a numeric channel ID or a #channel mention."), ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)

        await config.save_guild_log(guild_id, self.log_type, channel_id)

        logs = await config.get_guild_logs(guild_id)
        if interaction.guild:
            await _edit_panel(interaction, config.build_logs_embed(interaction.guild, logs))

        await interaction.followup.send(
            embed=success_embed(f"**{self.log_type.capitalize()}** logs will now go to <#{channel_id}>."),
            ephemeral=True,
        )


class GrinderLogsView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    # --- ROW 0: NAVIGATION ---
    @ui.button(label="📋 Configs", style=discord.ButtonStyle.grey, custom_id="log_nav_cfg", row=0)
    async def nav_configs(self, interaction: discord.Interaction, button: ui.Button):
        if not await _guild_only(interaction):
            return
        configs = await config.get_guild_configs(str(interaction.guild_id))
        g_data = await config.get_global_data()
        embed = await config.build_mode_configs_embed(interaction.guild, configs, g_data.get("accounts", []))
        await interaction.response.edit_message(embed=themed(embed), view=ConfigView(page="modes"))

    @ui.button(label="⚙️ Accounts", style=discord.ButtonStyle.grey, custom_id="log_nav_acc", row=0)
    async def nav_accounts(self, interaction: discord.Interaction, button: ui.Button):
        data = await config.get_global_data()
        embed = config.build_accounts_embed(data.get("accounts", []))
        await interaction.response.edit_message(embed=themed(embed), view=AccountsView())

    # --- ROW 1: SET LOG CHANNELS ---
    @ui.button(label="🔔 Set Alerts", style=discord.ButtonStyle.blurple, custom_id="log_alerts", row=1)
    async def set_alerts(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(SetLogModal("alerts"))

    @ui.button(label="🎯 Set Autocatch", style=discord.ButtonStyle.blurple, custom_id="log_autocatch", row=1)
    async def set_autocatch(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(SetLogModal("autocatch"))

    @ui.button(label="🔀 Set Switch", style=discord.ButtonStyle.blurple, custom_id="log_switch", row=1)
    async def set_switch(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(SetLogModal("switch"))