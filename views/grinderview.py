import re
import discord
from discord import ui

from views.common import ConfirmView
from cogs.grinder import grinder as config


def fmt_placeholder(val) -> str:
    s = str(val) if val is not None and str(val) != "" else "None"
    if len(s) > 45:
        s = s[:42] + "..."
    return f"Current: {s} (Leave blank to keep)"


# --- MODALS FOR ACCOUNTS ---
class AddAccountModal(ui.Modal, title="Add Account"):
    token = ui.TextInput(label="Account Token", placeholder="Paste account token here...", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        data = await config.get_global_data()
        data["accounts"].append(self.token.value.strip())
        await config.save_global_data(data)
        
        if interaction.message:
            await interaction.message.edit(embed=config.build_accounts_embed(data["accounts"]))

        uid = config.get_user_id_from_token(self.token.value.strip())
        mention = f"<@{uid}>" if uid else "Unknown Member"
        await interaction.followup.send(f"✅ Account #{len(data['accounts'])} ({mention}) added!", ephemeral=True)


class DeleteAccountModal(ui.Modal, title="Delete Account"):
    index = ui.TextInput(label="Account Index", placeholder="e.g. 1", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            idx = int(self.index.value.strip()) - 1
            await interaction.response.defer(ephemeral=True)
            data = await config.get_global_data()
            if 0 <= idx < len(data["accounts"]):
                data["accounts"].pop(idx)
                await config.save_global_data(data)

                if interaction.message:
                    await interaction.message.edit(embed=config.build_accounts_embed(data["accounts"]))

                await interaction.followup.send(f"🗑️ Account #{idx + 1} removed.", ephemeral=True)
            else:
                await interaction.followup.send("❌ Invalid index.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ Index must be a number.", ephemeral=True)


class EditAccountModal(ui.Modal, title="Edit Account Token"):
    index = ui.TextInput(label="Account Index", placeholder="e.g. 1", required=True)
    new_token = ui.TextInput(label="New Token", placeholder="Paste new token...", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            idx = int(self.index.value.strip()) - 1
            await interaction.response.defer(ephemeral=True)
            data = await config.get_global_data()
            if 0 <= idx < len(data["accounts"]):
                data["accounts"][idx] = self.new_token.value.strip()
                await config.save_global_data(data)

                if interaction.message:
                    await interaction.message.edit(embed=config.build_accounts_embed(data["accounts"]))

                await interaction.followup.send(f"✏️ Account #{idx + 1} token updated.", ephemeral=True)
            else:
                await interaction.followup.send("❌ Invalid index.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ Index must be a number.", ephemeral=True)


class AccountsView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Add Account", style=discord.ButtonStyle.green, custom_id="acc_add")
    async def add_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AddAccountModal())

    @ui.button(label="Delete Account", style=discord.ButtonStyle.red, custom_id="acc_del")
    async def del_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(DeleteAccountModal())

    @ui.button(label="Edit Account", style=discord.ButtonStyle.blurple, custom_id="acc_edit")
    async def edit_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(EditAccountModal())


# --- MODALS & VIEWS FOR CONFIG ---
class AddConfigModal(ui.Modal, title="Add Configuration"):
    mode = ui.TextInput(
        label="Mode", 
        placeholder="autocatch / spam / dotcatch / commaedit / periodicmsg", 
        required=True
    )
    acc_index = ui.TextInput(label="Account Index(es)", placeholder="e.g. 1, 2, 3 or 2, 4, 5", required=True)
    target = ui.TextInput(
        label="Target / Arguments / Channel IDs", 
        placeholder="chid1, pikachu test.json OR chid; msg; t1; t2", 
        required=False
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
            return

        mode_val = self.mode.value.strip().lower()
        if mode_val not in config.VALID_MODES:
            await interaction.response.send_message(
                f"❌ Invalid mode! Allowed modes: {', '.join(config.VALID_MODES)}", ephemeral=True
            )
            return

        raw_accs = [i.strip() for i in self.acc_index.value.split(',') if i.strip()]
        acc_indices = [int(a) for a in raw_accs if a.isdigit()]

        if not acc_indices:
            await interaction.response.send_message("❌ Account Index must contain valid number(s).", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        configs = await config.get_guild_configs(guild_id)

        raw_target = self.target.value.strip() if self.target.value else ""

        if mode_val in ["spam", "autocatch", "periodicmsg"] or not raw_target:
            targets_list = [raw_target]
        else:
            targets_list = [t.strip() for t in raw_target.split(',') if t.strip()]

        added_count = 0
        for idx in acc_indices:
            for tgt in targets_list:
                clean_target = tgt.strip()

                new_cfg = {
                    "mode": mode_val,
                    "accIndex": idx,
                    "target": clean_target,
                    "paused": False,
                    "pauseUntil": None
                }
                configs.append(new_cfg)
                added_count += 1

        await config.save_guild_configs(guild_id, configs)
        await config.refresh_config_embed(interaction)

        await interaction.followup.send(
            f"✅ Added {added_count} configuration(s) for `{mode_val}`!", 
            ephemeral=True
        )


class DynamicConfigEditModal(ui.Modal):
    def __init__(self, config_idx: int, cfg: dict, parent_view: 'SequentialEditView'):
        self.config_idx = config_idx
        self.cfg = cfg
        self.parent_view = parent_view
        self.mode = cfg.get("mode", "").lower()

        info = config.MODE_PARAMS_INFO.get(
            self.mode, 
            {"required": ["Account Index"], "optional": ["Target"]}
        )
        req_count = len(info["required"])
        opt_count = len(info["optional"])
        total_count = req_count + opt_count

        super().__init__(title=f"Config #{config_idx} ({self.mode.upper()}) [{total_count} Params]")

        self.details = config.get_config_details(cfg)
        self.inputs = {}

        curr_acc = self.details.get("accIndex", 1)
        self.inputs["accIndex"] = ui.TextInput(
            label="Account Index [Required]",
            placeholder=fmt_placeholder(curr_acc),
            required=False
        )
        self.add_item(self.inputs["accIndex"])

        if self.mode in ["autocatch", "dotcatch", "commaedit"]:
            self.inputs["chid"] = ui.TextInput(
                label="Target ID(s) [Optional]",
                placeholder=fmt_placeholder(self.details.get("chid")),
                required=False
            )
            self.add_item(self.inputs["chid"])

            self.inputs["pokemons"] = ui.TextInput(
                label="Pokemons (comma separated) [Optional]",
                placeholder=fmt_placeholder(self.details.get("pokemons")),
                required=False
            )
            self.add_item(self.inputs["pokemons"])

            self.inputs["datafile"] = ui.TextInput(
                label="Datafile [Optional]",
                placeholder=fmt_placeholder(self.details.get("datafile")),
                required=False
            )
            self.add_item(self.inputs["datafile"])

        elif self.mode == "periodicmsg":
            self.inputs["chid"] = ui.TextInput(
                label="Channel ID [Required]",
                placeholder=fmt_placeholder(self.details.get("chid")),
                required=False
            )
            self.add_item(self.inputs["chid"])

            self.inputs["message"] = ui.TextInput(
                label="Message [Required]",
                placeholder=fmt_placeholder(self.details.get("message")),
                style=discord.TextStyle.paragraph,
                required=False
            )
            self.add_item(self.inputs["message"])

            self.inputs["time1"] = ui.TextInput(
                label="Time 1 (Delay) [Optional]",
                placeholder=fmt_placeholder(self.details.get("time1")),
                required=False
            )
            self.add_item(self.inputs["time1"])

            self.inputs["time2"] = ui.TextInput(
                label="Time 2 (Interval) [Optional]",
                placeholder=fmt_placeholder(self.details.get("time2")),
                required=False
            )
            self.add_item(self.inputs["time2"])

        elif self.mode == "spam":
            curr_target = self.details.get("chid") or self.details.get("target")
            self.inputs["chid"] = ui.TextInput(
                label="Target / Channel ID [Required]",
                placeholder=fmt_placeholder(curr_target),
                required=False
            )
            self.add_item(self.inputs["chid"])

        else:
            self.inputs["target"] = ui.TextInput(
                label="Target / Arguments [Required]",
                placeholder=fmt_placeholder(self.details.get("target")),
                required=False
            )
            self.add_item(self.inputs["target"])

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        updated_fields = {}
        for key, text_input in self.inputs.items():
            val = text_input.value.strip()
            if val:
                if key == "accIndex":
                    if val.isdigit():
                        updated_fields["accIndex"] = int(val)
                    else:
                        updated_fields["accIndex"] = self.details.get("accIndex", 1)
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


class SequentialEditView(ui.View):
    def __init__(self, indices: list[int], guild_id: str, current_step: int = 0):
        super().__init__(timeout=180)
        self.indices = indices
        self.guild_id = guild_id
        self.current_step = current_step
        self.update_button()

    def update_button(self):
        self.clear_items()
        if self.current_step < len(self.indices):
            cfg_idx = self.indices[self.current_step]
            btn = ui.Button(
                label=f"Edit Config #{cfg_idx}",
                style=discord.ButtonStyle.blurple,
                custom_id=f"seq_edit_{cfg_idx}_{self.current_step}"
            )
            btn.callback = self.on_button_click
            self.add_item(btn)

    async def on_button_click(self, interaction: discord.Interaction):
        configs = await config.get_guild_configs(self.guild_id)
        cfg_idx = self.indices[self.current_step]

        if 1 <= cfg_idx <= len(configs):
            cfg = configs[cfg_idx - 1]
            modal = DynamicConfigEditModal(cfg_idx, cfg, self)
            await interaction.response.send_modal(modal)
        else:
            await interaction.response.send_message(
                f"❌ Config #{cfg_idx} no longer exists.", ephemeral=True
            )

    async def advance(self, interaction: discord.Interaction, completed_idx: int):
        next_step = self.current_step + 1
        if next_step < len(self.indices):
            next_idx = self.indices[next_step]
            configs = await config.get_guild_configs(self.guild_id)
            next_mode = configs[next_idx - 1].get("mode", "unknown") if 1 <= next_idx <= len(configs) else "unknown"

            next_view = SequentialEditView(self.indices, self.guild_id, current_step=next_step)
            await interaction.followup.send(
                content=(
                    f"✅ **Config #{completed_idx}** updated!\n"
                    f"👉 **Next:** Click the button below to edit **Config #{next_idx}** (`{next_mode}`)."
                ),
                view=next_view,
                ephemeral=True
            )
        else:
            edited_str = ", ".join(f"#{i}" for i in self.indices)
            await interaction.followup.send(
                content=f"🎉 All specified configuration(s) ({edited_str}) have been updated successfully!",
                ephemeral=True
            )


class PromptEditConfigModal(ui.Modal, title="Edit Configuration"):
    index = ui.TextInput(
        label="Config Index(es)", 
        placeholder="e.g. 1 or 1, 2, 3", 
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
            return

        raw_indices = [i.strip() for i in self.index.value.split(',') if i.strip()]
        indices = []
        for i in raw_indices:
            if i.isdigit() and int(i) not in indices:
                indices.append(int(i))

        if not indices:
            await interaction.response.send_message("❌ Please enter valid config index number(s).", ephemeral=True)
            return

        guild_id = str(interaction.guild_id)
        configs = await config.get_guild_configs(guild_id)

        invalid_indices = [idx for idx in indices if idx < 1 or idx > len(configs)]
        if invalid_indices:
            await interaction.response.send_message(
                f"❌ Invalid index(es): {', '.join(map(str, invalid_indices))}. Server has {len(configs)} configuration(s).",
                ephemeral=True
            )
            return

        seq_view = SequentialEditView(indices, guild_id)
        first_idx = indices[0]
        first_cfg = configs[first_idx - 1]
        first_mode = first_cfg.get("mode", "").lower()
        info = config.MODE_PARAMS_INFO.get(first_mode, {"required": [], "optional": []})
        req_count = len(info["required"])
        opt_count = len(info["optional"])

        msg_content = (
            f"⚙️ **Editing Configuration(s):** {', '.join(f'#{i}' for i in indices)}\n\n"
            f"Click below to edit **Config #{first_idx}** (`{first_mode}`):\n"
            f"• **Required parameters:** {req_count}\n"
            f"• **Optional parameters:** {opt_count}\n"
            f"• **Total parameters:** {req_count + opt_count}"
        )

        await interaction.response.send_message(
            content=msg_content,
            view=seq_view,
            ephemeral=True
        )


class RemoveConfigModal(ui.Modal, title="Remove Configuration"):
    index = ui.TextInput(label="Config Index(es)", placeholder="e.g. 1 or 1, 2, 3", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
            return

        raw_indices = [i.strip() for i in self.index.value.split(',') if i.strip()]
        indices_to_remove = [int(i) - 1 for i in raw_indices if i.isdigit()]

        if not indices_to_remove:
            await interaction.response.send_message("❌ Index must contain valid number(s).", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        configs = await config.get_guild_configs(guild_id)

        sorted_indices = sorted(list(set(indices_to_remove)), reverse=True)
        removed_count = 0

        for idx in sorted_indices:
            if 0 <= idx < len(configs):
                configs.pop(idx)
                removed_count += 1

        if removed_count > 0:
            await config.save_guild_configs(guild_id, configs)
            await config.refresh_config_embed(interaction)
            await interaction.followup.send(f"🗑️ Removed {removed_count} config(s).", ephemeral=True)
        else:
            await interaction.followup.send("❌ No valid config indices were found to remove.", ephemeral=True)


class AddExcludeModal(ui.Modal, title="Add Excludes"):
    pokemon = ui.TextInput(label="Pokemon Name(s)", placeholder="e.g. pikachu, gimmighoul, raikou --xnon", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
            return

        raw_input = self.pokemon.value.strip()
        items = [i.strip() for i in raw_input.split(',') if i.strip()]

        if not items:
            await interaction.response.send_message("❌ Invalid Pokemon name(s).", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)

        excludes = await config.get_guild_excludes(guild_id)
        added_names = []

        for item in items:
            is_xnon = "--xnon" in item.lower()
            pname = re.sub(r'(?i)\s*--xnon\s*', '', item).strip()
            if pname:
                excludes.append({"name": pname, "xnon": is_xnon})
                xnon_tag = " (--xnon)" if is_xnon else ""
                added_names.append(f"`{pname}{xnon_tag}`")

        if added_names:
            await config.save_guild_excludes(guild_id, excludes)
            await config.refresh_config_embed(interaction)
            await interaction.followup.send(f"✅ Saved {', '.join(added_names)} to excludes in Firebase!", ephemeral=True)
        else:
            await interaction.followup.send("❌ No valid Pokemon names provided.", ephemeral=True)


class RemoveExcludeModal(ui.Modal, title="Remove Excludes"):
    pokemon = ui.TextInput(label="Pokemon Name(s)", placeholder="e.g. pikachu, gimmighoul, raikou", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
            return

        raw_input = self.pokemon.value.strip()
        target_pnames = set(i.strip().lower() for i in raw_input.split(',') if i.strip())

        if not target_pnames:
            await interaction.response.send_message("❌ Please provide valid Pokemon name(s).", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)

        excludes = await config.get_guild_excludes(guild_id)
        new_excludes = [
            ex for ex in excludes 
            if (ex.get("name") if isinstance(ex, dict) else str(ex)).lower() not in target_pnames
        ]

        removed_count = len(excludes) - len(new_excludes)

        if removed_count > 0:
            await config.save_guild_excludes(guild_id, new_excludes)
            await config.refresh_config_embed(interaction)
            await interaction.followup.send(f"🗑️ Removed {removed_count} exclude item(s)!", ephemeral=True)
        else:
            await interaction.followup.send("❌ None of the specified excludes were found.", ephemeral=True)


class AddBotModal(ui.Modal, title="Add Detector Bot"):
    bot_id = ui.TextInput(label="Bot User ID", placeholder="e.g. 123456789012345678", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        bot_id_val = self.bot_id.value.strip()
        if not bot_id_val.isdigit():
            await interaction.response.send_message("❌ Must be a valid numeric ID.", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        bots = await config.get_guild_detector_bots(guild_id)
        
        if bot_id_val not in bots:
            bots.append(bot_id_val)
            await config.save_guild_detector_bots(guild_id, bots)
            
        await config.refresh_config_embed(interaction, override_page="detector_bots")
        await interaction.followup.send(f"✅ Added detector bot <@{bot_id_val}>.", ephemeral=True)


class RemoveBotModal(ui.Modal, title="Remove Detector Bot"):
    index = ui.TextInput(label="Bot Index(es)", placeholder="e.g. 1 or 1, 2", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        raw_indices = [int(i.strip()) - 1 for i in self.index.value.split(',') if i.strip().isdigit()]
        if not raw_indices:
            await interaction.response.send_message("❌ Please provide valid numeric index(es).", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        bots = await config.get_guild_detector_bots(guild_id)
        
        removed = 0
        for idx in sorted(set(raw_indices), reverse=True):
            if 0 <= idx < len(bots):
                bots.pop(idx)
                removed += 1
                
        if removed > 0:
            await config.save_guild_detector_bots(guild_id, bots)
            await config.refresh_config_embed(interaction, override_page="detector_bots")
            await interaction.followup.send(f"🗑️ Removed {removed} detector bot(s).", ephemeral=True)
        else:
            await interaction.followup.send("❌ Invalid indices.", ephemeral=True)


class ConfigView(ui.View):
    def __init__(self, page="modes"):
        super().__init__(timeout=None)
        self.page = page

        # Config tabs: selected tab is blue (blurple), unselected tabs are grey (grey)
        self.nav_modes.style = discord.ButtonStyle.blurple if page == "modes" else discord.ButtonStyle.grey
        self.nav_autocatch.style = discord.ButtonStyle.blurple if page == "autocatch" else discord.ButtonStyle.grey
        self.nav_excludes.style = discord.ButtonStyle.blurple if page == "excludes" else discord.ButtonStyle.grey
        self.nav_bots.style = discord.ButtonStyle.blurple if page == "detector_bots" else discord.ButtonStyle.grey
        
        # Determine Reset ALL label based on page
        if page in ["modes", "autocatch"]:
            self.reset_all.label = "Reset ALL Configs"
        elif page == "excludes":
            self.reset_all.label = "Reset ALL Excludes"
        elif page == "detector_bots":
            self.reset_all.label = "Reset ALL Bots"

        # Dynamically remove items not applicable to current page
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
        new_view = ConfigView(page=new_page)
        guild_id = str(interaction.guild_id)
        g_data = await config.get_global_data()
        accounts = g_data.get("accounts", [])

        if new_page == "modes":
            configs = await config.get_guild_configs(guild_id)
            embed = await config.build_mode_configs_embed(interaction.guild, configs, accounts)
        elif new_page == "autocatch":
            autocatch_data = await config.get_autocatch_status()
            embed = await config.build_autocatch_configs_embed(interaction.guild, autocatch_data, accounts)
        elif new_page == "excludes":
            excludes = await config.get_guild_excludes(guild_id)
            embed = await config.build_excludes_configs_embed(interaction.guild, excludes)
        elif new_page == "detector_bots":
            bots = await config.get_guild_detector_bots(guild_id)
            embed = await config.build_detector_bots_embed(interaction.guild, bots)

        await interaction.response.edit_message(embed=embed, view=new_view)

    # --- ROW 0: NAVIGATION BUTTONS ---
    @ui.button(label="Mode Configs", style=discord.ButtonStyle.grey, custom_id="cfg_nav_modes", row=0)
    async def nav_modes(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.guild:
            await self.change_page(interaction, "modes")

    @ui.button(label="AutoCatch Configs", style=discord.ButtonStyle.grey, custom_id="cfg_nav_autocatch", row=0)
    async def nav_autocatch(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.guild:
            await self.change_page(interaction, "autocatch")

    @ui.button(label="Excludes Configs", style=discord.ButtonStyle.grey, custom_id="cfg_nav_excludes", row=0)
    async def nav_excludes(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.guild:
            await self.change_page(interaction, "excludes")

    @ui.button(label="Detector Bots", style=discord.ButtonStyle.grey, custom_id="cfg_nav_bots", row=0)
    async def nav_bots(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.guild:
            await self.change_page(interaction, "detector_bots")

    # --- ROW 1: CONFIG ACTION BUTTONS ---
    @ui.button(label="Add Config", style=discord.ButtonStyle.green, custom_id="cfg_add", row=1)
    async def add_cfg(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AddConfigModal())

    @ui.button(label="Edit Config", style=discord.ButtonStyle.blurple, custom_id="cfg_edit", row=1)
    async def edit_cfg(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(PromptEditConfigModal())

    @ui.button(label="Remove Config", style=discord.ButtonStyle.red, custom_id="cfg_rem", row=1)
    async def rem_cfg(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(RemoveConfigModal())

    @ui.button(label="Reset ALL", style=discord.ButtonStyle.danger, custom_id="cfg_reset_all", row=1)
    async def reset_all(self, interaction: discord.Interaction, button: ui.Button):
        if not interaction.guild_id or not isinstance(interaction.user, (discord.Member, discord.User)):
            return

        confirm_view = ConfirmView(author=interaction.user)
        await interaction.response.send_message(
            f"⚠️ **Are you sure you want to {self.reset_all.label} for this server?**",
            view=confirm_view,
            ephemeral=True
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
            await interaction.followup.send(f"💥 {self.reset_all.label} successful!", ephemeral=True)
        elif confirm_view.value is False:
            await interaction.followup.send("❌ Reset cancelled.", ephemeral=True)
        else:
            await interaction.followup.send("⏰ Reset request timed out.", ephemeral=True)

    # --- ROW 2: EXCLUDE & BOT ACTION BUTTONS ---
    @ui.button(label="Add Excludes", style=discord.ButtonStyle.green, custom_id="cfg_add_ex", row=2)
    async def add_ex(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AddExcludeModal())

    @ui.button(label="Remove Excludes", style=discord.ButtonStyle.red, custom_id="cfg_rem_ex", row=2)
    async def rem_ex(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(RemoveExcludeModal())

    @ui.button(label="Add Bot", style=discord.ButtonStyle.green, custom_id="cfg_add_bot", row=2)
    async def add_bot(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AddBotModal())

    @ui.button(label="Remove Bot", style=discord.ButtonStyle.red, custom_id="cfg_rem_bot", row=2)
    async def rem_bot(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(RemoveBotModal())


# --- MODALS & VIEWS FOR LOGS ---
class SetLogModal(ui.Modal):
    channel_input = ui.TextInput(
        label="Channel ID", 
        placeholder="Paste numeric channel ID (e.g. 123456789012345678)", 
        required=True
    )

    def __init__(self, log_type: str):
        self.log_type = log_type
        super().__init__(title=f"Set {log_type.capitalize()} Log Channel")

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
            return

        raw_channel = self.channel_input.value.strip()

        if not raw_channel.isdigit():
            await interaction.response.send_message(
                "❌ Invalid input! Only numeric Channel IDs are allowed.",
                ephemeral=True
            )
            return

        channel_id = raw_channel

        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)

        await config.save_guild_log(guild_id, self.log_type, channel_id)

        logs = await config.get_guild_logs(guild_id)
        if interaction.message and interaction.guild:
            embed = config.build_logs_embed(interaction.guild, logs)
            await interaction.message.edit(embed=embed)

        await interaction.followup.send(
            f"✅ **{self.log_type.capitalize()}** log channel updated to <#{channel_id}> (`{channel_id}`).",
            ephemeral=True
        )


class GrinderLogsView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Set Alerts", style=discord.ButtonStyle.blurple, custom_id="log_alerts")
    async def set_alerts(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(SetLogModal("alerts"))

    @ui.button(label="Set Autocatch", style=discord.ButtonStyle.blurple, custom_id="log_autocatch")
    async def set_autocatch(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(SetLogModal("autocatch"))

    @ui.button(label="Set Switch", style=discord.ButtonStyle.blurple, custom_id="log_switch")
    async def set_switch(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(SetLogModal("switch"))