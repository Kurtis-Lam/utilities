import asyncio
import base64
import re
import time
from typing import Optional
import discord
from discord.ext import commands
import firebase_admin
from firebase_admin import credentials, db

from views.common import ConfirmView
from views.grinderview import AccountsView, ConfigView, GrinderLogsView

# --- CONSTANTS ---
VALID_MODES = ["autocatch", "spam", "dotcatch", "commaedit", "periodicmsg"]

MODE_PARAMS_INFO = {
    "autocatch": {"required": ["Account Index"], "optional": ["Target ID", "Pokemons", "Datafile"]},
    "spam": {"required": ["Account Index", "Target / Channel ID"], "optional": []},
    "dotcatch": {"required": ["Account Index", "Channel ID"], "optional": ["Pokemons", "Datafile"]},
    "commaedit": {"required": ["Account Index", "Channel ID"], "optional": ["Pokemons", "Datafile"]},
    "periodicmsg": {"required": ["Account Index", "Channel ID", "Message"], "optional": ["Time 1 (Delay)", "Time 2 (Interval)"]},
}

# --- FIREBASE SETUP ---
if not firebase_admin._apps:
    cred = credentials.Certificate("firebase_key.json")
    firebase_admin.initialize_app(cred, {
        'databaseURL': 'https://versatile-kl-default-rtdb.firebaseio.com/'
    })

ref = db.reference("grinder")


# --- HELPER: TIME PARSER ---
def parse_duration(time_str: str):
    """Parses time strings like 30s, 4m, 2h, 1d into seconds."""
    match = re.match(r"^(\d+)([s m h d])$", time_str.lower())
    if not match:
        return None
    
    amount, unit = int(match.group(1)), match.group(2)
    units = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    return amount * units[unit]


# --- HELPER: PARSE MULTIPLE INDICES & DURATION ---
def parse_indices_and_duration(raw_str: str):
    """
    Parses arguments for .r / .p commands.
    Extracts numerical indices and duration string.
    e.g. "1, 2, 3 9m" -> ([1, 2, 3], "9m")
         "1, 7"       -> ([1, 7], None)
    """
    if not raw_str or not raw_str.strip():
        return [], None

    tokens = [t.strip() for t in raw_str.replace(',', ' ').split() if t.strip()]
    indices = []
    duration = None

    for token in tokens:
        if parse_duration(token) is not None and duration is None:
            duration = token
        elif token.isdigit():
            indices.append(int(token))

    return indices, duration


# --- HELPER: PARSE TARGET ASPECTS FOR .GC DISPLAY ---
def parse_target_aspects(mode: str, target: str) -> list[tuple[str, str]]:
    """
    Parses target arguments into individual labeled aspects.
    Supports integers for target IDs, json for datafiles, and words for pokemons.
    """
    if not target:
        return []

    mode_lower = mode.lower()

    if "=" in target or ":" in target:
        subparts = [p.strip() for p in re.split(r'[,;]', target) if p.strip()]
        if all("=" in p or ":" in p for p in subparts):
            aspects = []
            for p in subparts:
                sep = "=" if "=" in p else ":"
                k, v = p.split(sep, 1)
                aspects.append((k.strip().capitalize(), v.strip()))
            return aspects

    if mode_lower in ["autocatch", "dotcatch", "commaedit"]:
        parts = [p.strip() for p in target.split(',') if p.strip()]
        chids = []
        pokes = []
        datafile = ""

        for p in parts:
            if p.lower().endswith(".json"):
                if not datafile:
                    datafile = p
            elif p.isdigit():
                chids.append(p)
            else:
                pokes.append(p)

        aspects = []
        if chids:
            aspects.append(("Target ID", ", ".join(chids)))
        if pokes:
            aspects.append(("Pokemons", ", ".join(pokes)))
        if datafile:
            aspects.append(("Datafile", datafile))
        return aspects

    elif mode_lower == "periodicmsg":
        parts = [p.strip() for p in re.split(r'[,; ]', target) if p.strip()]
        labels = ["Chid", "Message", "Time1", "Time2"]
        aspects = []
        for i, part in enumerate(parts):
            lbl = labels[i] if i < len(labels) else f"Arg{i+1}"
            aspects.append((lbl, part))
        return aspects

    else:
        parts = [p.strip() for p in target.split() if p.strip()]
        aspects = []
        for i, part in enumerate(parts):
            aspects.append((f"Arg{i+1}", part))
        return aspects


# --- HELPERS FOR CONFIG EDITING ---
def get_config_details(cfg: dict) -> dict:
    """Extracts existing parameter field values for a configuration dictionary."""
    mode = cfg.get("mode", "").lower()
    target_str = cfg.get("target", "")
    acc_idx = cfg.get("accIndex", 1)

    aspects_list = parse_target_aspects(mode, target_str)
    aspects = {k.lower(): v for k, v in aspects_list}

    details = {
        "accIndex": acc_idx,
        "chid": aspects.get("target id", aspects.get("chid", "")),
        "pokemons": aspects.get("pokemons", ""),
        "datafile": aspects.get("datafile", ""),
        "message": aspects.get("message", ""),
        "time1": aspects.get("time1", ""),
        "time2": aspects.get("time2", ""),
        "target": target_str
    }

    if mode == "spam" and not details["chid"]:
        details["chid"] = target_str
    elif mode == "periodicmsg" and not details["message"] and "args" in aspects:
        details["message"] = aspects["args"]

    return details


def build_target_string_for_mode(mode: str, fields: dict) -> tuple[str, bool]:
    """Reconstructs the formatted target string and xnon boolean from field values."""
    mode_lower = mode.lower()

    if mode_lower in ["autocatch", "dotcatch", "commaedit"]:
        target_ids = fields.get("target id", fields.get("chid", "")).strip()
        pokes = fields.get("pokemons", "").strip()
        datafile = fields.get("datafile", "").strip()

        target_ids_clean = ", ".join([p.strip() for p in target_ids.split(",") if p.strip()])
        pokes_clean = ", ".join([p.strip() for p in pokes.split(",") if p.strip()])

        parts = []
        if target_ids_clean:
            parts.append(target_ids_clean)
        if pokes_clean:
            parts.append(pokes_clean)
        if datafile:
            parts.append(datafile)

        return ", ".join(parts), False

    elif mode_lower == "periodicmsg":
        chid = fields.get("chid", "").strip()
        msg = fields.get("message", "").strip()
        t1 = fields.get("time1", "").strip()
        t2 = fields.get("time2", "").strip()

        parts = [chid, msg]
        if t1:
            parts.append(t1)
        if t2:
            parts.append(t2)
        return " ; ".join([p for p in parts if p]), False

    elif mode_lower == "spam":
        target = fields.get("chid", fields.get("target", "")).strip()
        return target, False

    else:
        target = fields.get("target", "").strip()
        return target, False


# --- HELPER: DECODE DISCORD USER ID FROM TOKEN ---
def get_user_id_from_token(token: str):
    """Extracts the Discord User ID from a token's base64-encoded first segment."""
    try:
        part = token.split('.')[0]
        padded = part + '=' * (-len(part) % 4)
        decoded = base64.b64decode(padded).decode('utf-8')
        return int(decoded) if decoded.isdigit() else None
    except Exception:
        return None


# --- HELPER: CHECK CHANNEL ALLOWED ---
def is_channel_allowed(target_str: str, channel_id: int) -> bool:
    """Checks if a given channel_id is allowed based on target_str channel IDs."""
    if not target_str:
        return True
    channel_ids = re.findall(r'\d+', target_str)
    if not channel_ids:
        return True
    return str(channel_id) in channel_ids


# --- FIREBASE HELPERS ---
async def get_global_data():
    """Fetches globally synced accounts and guilds."""
    data = await asyncio.to_thread(ref.get) or {}
    return {
        "accounts": data.get("accounts", []),
        "guilds": data.get("guilds", [])
    }


async def get_autocatch_status():
    """Fetches real-time autocatch pairs and current catcher turn from Firebase."""
    data = await asyncio.to_thread(ref.child("autocatch").get) or {}
    return {
        "pairs": data.get("pairs", []),
        "current_catcher": data.get("current_catcher", {})
    }


async def save_global_data(data):
    """Saves globally synced accounts and guilds."""
    await asyncio.to_thread(ref.child("accounts").set, data.get("accounts", []))
    await asyncio.to_thread(ref.child("guilds").set, data.get("guilds", []))


async def get_guild_configs(guild_id: str):
    """Fetches configs specific to a guild."""
    configs = await asyncio.to_thread(ref.child("configs").child(str(guild_id)).get) or []
    return configs


async def save_guild_configs(guild_id: str, configs: list):
    """Saves configs specific to a guild."""
    await asyncio.to_thread(ref.child("configs").child(str(guild_id)).set, configs)


async def get_guild_excludes(guild_id: str):
    """Fetches standalone excludes specific to a guild from Firebase."""
    excludes = await asyncio.to_thread(ref.child("excludes").child(str(guild_id)).get) or []
    return excludes


async def save_guild_excludes(guild_id: str, excludes: list):
    """Saves standalone excludes specific to a guild in Firebase."""
    await asyncio.to_thread(ref.child("excludes").child(str(guild_id)).set, excludes)


async def get_guild_detector_bots(guild_id: str):
    """Fetches detector bots IDs for a specific guild."""
    bots = await asyncio.to_thread(ref.child("detector_bots").child(str(guild_id)).get) or []
    return bots


async def save_guild_detector_bots(guild_id: str, bots: list):
    """Saves detector bots IDs to a specific guild."""
    await asyncio.to_thread(ref.child("detector_bots").child(str(guild_id)).set, bots)


async def get_guild_logs(guild_id: str):
    """Fetches log channel configs specific to a guild."""
    logs = await asyncio.to_thread(ref.child("logs").child(str(guild_id)).get) or {}
    return logs


async def save_guild_log(guild_id: str, log_type: str, channel_id: str):
    """Saves or overwrites a log channel config for a specific log type in a guild."""
    await asyncio.to_thread(ref.child("logs").child(str(guild_id)).child(log_type).set, channel_id)


async def save_guild_logs(guild_id: str, logs: dict):
    """Saves or overwrites all log channel configs for a specific guild."""
    await asyncio.to_thread(ref.child("logs").child(str(guild_id)).set, logs)


# --- HELPER: CHUNK EMBED FIELDS ---
def add_chunked_field(embed: discord.Embed, title: str, lines: list[str]):
    """Splits formatted lines across multiple embed fields to respect Discord's 1024-character limit per field."""
    if not lines:
        return

    chunk = []
    chunk_len = 0
    part = 1

    for line in lines:
        if chunk_len + len(line) + 1 > 1000:
            field_title = f"{title} (Part {part})" if part > 1 else title
            embed.add_field(name=field_title, value="\n".join(chunk), inline=False)
            chunk = [line]
            chunk_len = len(line)
            part += 1
        else:
            chunk.append(line)
            chunk_len += len(line) + 1

    if chunk:
        field_title = f"{title} (Part {part})" if part > 1 else title
        embed.add_field(name=field_title, value="\n".join(chunk), inline=False)


# --- EMBED BUILDERS ---
def build_accounts_embed(accounts: list):
    embed = discord.Embed(title="⚙️ Grinder Accounts (Global)", color=discord.Color.blue())
    if not accounts:
        embed.description = "No accounts stored."
    else:
        desc = ""
        for idx, token in enumerate(accounts, 1):
            masked_token = token[:10] + "..." if len(token) > 10 else token
            uid = get_user_id_from_token(token)
            member_mention = f"<@{uid}>" if uid else "*Unknown Member*"
            desc += f"**#{idx}**: {member_mention} — `{masked_token}`\n"
        embed.description = desc
    return embed


async def build_mode_configs_embed(guild: discord.Guild, configs: list, accounts: list):
    embed = discord.Embed(title=f"📋 Mode Configurations — {guild.name}", color=discord.Color.gold())
    if not configs:
        embed.description = "No configurations found for this server."
        return embed

    mode_groups = {m: [] for m in VALID_MODES}
    other_configs = []

    for idx, cfg in enumerate(configs, 1):
        mode = cfg.get("mode", "").lower()
        if mode in mode_groups:
            mode_groups[mode].append((idx, cfg))
        else:
            other_configs.append((idx, cfg))

    current_time = time.time()

    for mode in VALID_MODES:
        cfgs = mode_groups[mode]
        if not cfgs:
            continue

        lines = []
        for idx, cfg in cfgs:
            acc_idx = cfg.get("accIndex", 0)
            user_mention = "*Unknown Member*"
            display_name = ""

            if 1 <= acc_idx <= len(accounts):
                tok = accounts[acc_idx - 1]
                uid = get_user_id_from_token(tok)
                if uid:
                    user_mention = f"<@{uid}>"
                    member = guild.get_member(uid)
                    if member:
                        display_name = f" ({member.display_name})"

            line = f"`[#{idx}]` {user_mention}{display_name}"

            paused = cfg.get("paused", False)
            pause_until = cfg.get("pauseUntil")
            if paused and pause_until:
                if current_time < pause_until:
                    line += f" | ⏸️ **PAUSED** (<t:{int(pause_until)}:R>)"
                else:
                    line += " | ⏸️ **PAUSED** (Expired)"
            elif paused:
                line += " | ⏸️ **PAUSED**"

            target_str = cfg.get('target', '')
            aspects = parse_target_aspects(mode, target_str)
            for label, val in aspects:
                if val:
                    line += f"\n  {label}: `{val}`"

            lines.append(line)

        add_chunked_field(embed, f"⚙️ {mode.upper()}", lines)

    if other_configs:
        lines = []
        for idx, cfg in other_configs:
            acc_idx = cfg.get("accIndex", 0)
            user_mention = "*Unknown Member*"
            display_name = ""

            if 1 <= acc_idx <= len(accounts):
                tok = accounts[acc_idx - 1]
                uid = get_user_id_from_token(tok)
                if uid:
                    user_mention = f"<@{uid}>"
                    member = guild.get_member(uid)
                    if member:
                        display_name = f" ({member.display_name})"

            cfg_mode = cfg.get('mode', 'unknown')
            line = f"`[#{idx}]` {cfg_mode} | {user_mention}{display_name}"

            paused = cfg.get("paused", False)
            pause_until = cfg.get("pauseUntil")
            if paused and pause_until:
                if current_time < pause_until:
                    line += f" | ⏸️ **PAUSED** (<t:{int(pause_until)}:R>)"
                else:
                    line += " | ⏸️ **PAUSED** (Expired)"
            elif paused:
                line += " | ⏸️ **PAUSED**"

            target_str = cfg.get('target', '')
            aspects = parse_target_aspects(cfg_mode, target_str)
            for label, val in aspects:
                if val:
                    line += f"\n  {label}: `{val}`"

            lines.append(line)

        add_chunked_field(embed, "⚙️ Custom Modes", lines)

    return embed


async def build_autocatch_configs_embed(guild: discord.Guild, autocatch_data: dict, accounts: list):
    embed = discord.Embed(title=f"🎯 AutoCatch Configurations — {guild.name}", color=discord.Color.green())
    
    pairs = autocatch_data.get("pairs", [])
    current_catcher_info = autocatch_data.get("current_catcher", {})

    current_acc_idx = current_catcher_info.get("accIndex")
    if current_acc_idx and 1 <= current_acc_idx <= len(accounts):
        c_tok = accounts[current_acc_idx - 1]
        c_uid = get_user_id_from_token(c_tok)
        c_mention = f"<@{c_uid}>" if c_uid else "*Unknown Member*"
        current_catcher_str = f"**Acc #{current_acc_idx}** ({c_mention})"
    else:
        current_catcher_str = "*None / Waiting for spawn*"

    pairs_str_list = []
    if pairs:
        for p_idx, pair in enumerate(pairs, 1):
            pair_members = []
            for p_acc in pair:
                if 1 <= p_acc <= len(accounts):
                    p_tok = accounts[p_acc - 1]
                    p_uid = get_user_id_from_token(p_tok)
                    p_m = f"<@{p_uid}>" if p_uid else f"Acc #{p_acc}"
                    pair_members.append(f"Acc #{p_acc} ({p_m})")
                else:
                    pair_members.append(f"Acc #{p_acc}")
            pairs_str_list.append(f"• **Pair #{p_idx}:** {' & '.join(pair_members)}")
        pairs_fmt = "\n".join(pairs_str_list)
    else:
        pairs_fmt = "*No active pairs configured*"

    embed.add_field(name="🎯 Current Catcher Turn", value=current_catcher_str, inline=False)
    embed.add_field(name="👥 Active Catch Pairs", value=pairs_fmt, inline=False)
    return embed


async def build_excludes_configs_embed(guild: discord.Guild, excludes: list):
    embed = discord.Embed(title=f"🚫 Excludes Configurations — {guild.name}", color=discord.Color.red())
    if not excludes:
        embed.description = "No excludes configured for this server."
        return embed

    ex_strs = []
    for ex in excludes:
        if isinstance(ex, dict):
            ex_name = ex.get("name", "")
            is_xnon = bool(ex.get("xnon"))
            ex_strs.append(f"• `{ex_name}` — `--xnon`: `{is_xnon}`")
        else:
            ex_strs.append(f"• `{ex}` — `--xnon`: `False`")

    add_chunked_field(embed, "🚫 Excluded Pokemon", ex_strs)

    return embed


async def build_detector_bots_embed(guild: discord.Guild, bots: list):
    embed = discord.Embed(title=f"🤖 Detector Bots — {guild.name}", color=discord.Color.dark_theme())
    if not bots:
        embed.description = "No detector bots configured for this server."
        return embed

    bot_strs = []
    for idx, bot_id in enumerate(bots, 1):
        bot_strs.append(f"`[#{idx}]` <@{bot_id}> (`{bot_id}`)")

    add_chunked_field(embed, "🤖 Assigned Detector Bots", bot_strs)
    return embed


def build_logs_embed(guild: discord.Guild, logs: dict):
    embed = discord.Embed(
        title=f"📜 Grinder Log Channels — {guild.name}",
        color=discord.Color.purple()
    )

    alerts_ch = logs.get("alerts")
    autocatch_ch = logs.get("autocatch")
    switch_ch = logs.get("switch")

    alerts_str = f"<#{alerts_ch}> (`{alerts_ch}`)" if alerts_ch else "*Not set*"
    autocatch_str = f"<#{autocatch_ch}> (`{autocatch_ch}`)" if autocatch_ch else "*Not set*"
    switch_str = f"<#{switch_ch}> (`{switch_ch}`)" if switch_ch else "*Not set*"

    embed.add_field(name="🚨 Alerts Log", value=alerts_str, inline=False)
    embed.add_field(name="🎯 Autocatch Log", value=autocatch_str, inline=False)
    embed.add_field(name="🔀 Switch Log", value=switch_str, inline=False)

    return embed


# --- HELPER TO REFRESH CONFIG EMBEDS LIVE ---
async def refresh_config_embed(interaction: discord.Interaction, override_page: str = None):
    if interaction.message and interaction.guild and interaction.message.embeds:
        guild_id = str(interaction.guild_id)
        current_title = interaction.message.embeds[0].title or ""

        page = override_page
        if not page:
            if "AutoCatch" in current_title:
                page = "autocatch"
            elif "Excludes" in current_title:
                page = "excludes"
            elif "Detector Bots" in current_title:
                page = "detector_bots"
            elif "Mode Configurations" in current_title:
                page = "modes"

        if page == "autocatch":
            autocatch_data = await get_autocatch_status()
            g_data = await get_global_data()
            accounts = g_data.get("accounts", [])
            embed = await build_autocatch_configs_embed(interaction.guild, autocatch_data, accounts)
            await interaction.message.edit(embed=embed)
        elif page == "excludes":
            excludes = await get_guild_excludes(guild_id)
            embed = await build_excludes_configs_embed(interaction.guild, excludes)
            await interaction.message.edit(embed=embed)
        elif page == "detector_bots":
            bots = await get_guild_detector_bots(guild_id)
            embed = await build_detector_bots_embed(interaction.guild, bots)
            await interaction.message.edit(embed=embed)
        elif page == "modes":
            configs = await get_guild_configs(guild_id)
            g_data = await get_global_data()
            accounts = g_data.get("accounts", [])
            embed = await build_mode_configs_embed(interaction.guild, configs, accounts)
            await interaction.message.edit(embed=embed)


# --- COG DEFINITION ---
class GrinderCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="grindaccounts", aliases=["ga"])
    @commands.is_owner()
    async def grindaccounts(self, ctx: commands.Context):
        data = await get_global_data()
        accs = data.get("accounts", [])
        
        embed = build_accounts_embed(accs)
        await ctx.send(embed=embed, view=AccountsView())

    @commands.command(name="grindconfig", aliases=["gc"])
    @commands.is_owner()
    async def grindconfig(self, ctx: commands.Context):
        if not ctx.guild:
            await ctx.send("❌ This command must be used within a server.")
            return

        guild_id = str(ctx.guild.id)
        configs = await get_guild_configs(guild_id)
        g_data = await get_global_data()
        accounts = g_data.get("accounts", [])

        embed = await build_mode_configs_embed(ctx.guild, configs, accounts)
        await ctx.send(embed=embed, view=ConfigView(page="modes"))

    @commands.command(name="grinderlogs", aliases=["gl"])
    @commands.is_owner()
    async def grinderlogs(self, ctx: commands.Context):
        if not ctx.guild:
            await ctx.send("❌ This command must be used within a server.")
            return

        guild_id = str(ctx.guild.id)
        logs = await get_guild_logs(guild_id)

        embed = build_logs_embed(ctx.guild, logs)
        await ctx.send(embed=embed, view=GrinderLogsView())

    @commands.command(name="edit")
    @commands.is_owner()
    async def edit(self, ctx: commands.Context, user: discord.User, *, filenames: str):
        if not ctx.guild:
            await ctx.send("❌ This command must be used within a server.")
            return

        g_data = await get_global_data()
        accounts = g_data.get("accounts", [])

        user_acc_indices = [
            idx for idx, token in enumerate(accounts, 1)
            if get_user_id_from_token(token) == user.id
        ]

        if not user_acc_indices:
            await ctx.send(f"❌ No registered accounts found for {user.mention}.")
            return

        guild_id = str(ctx.guild.id)
        configs = await get_guild_configs(guild_id)

        if not configs:
            await ctx.send("❌ No configurations found for this server.")
            return

        raw_files = [f.strip() for f in filenames.replace(',', ' ').split() if f.strip()]
        if not raw_files:
            await ctx.send("❌ Please provide at least one datafile name.")
            return

        combined_files = " ".join([
            f if f.lower().endswith(".json") else f"{f}.json"
            for f in raw_files
        ])

        updated_count = 0
        for cfg in configs:
            if cfg.get("mode", "").lower() == "autocatch" and cfg.get("accIndex") in user_acc_indices:
                details = get_config_details(cfg)
                details["datafile"] = combined_files
                new_target, _ = build_target_string_for_mode("autocatch", details)
                cfg["target"] = new_target
                updated_count += 1

        if updated_count > 0:
            await save_guild_configs(guild_id, configs)
            await ctx.send(
                f"✅ Updated {updated_count} `autocatch` configuration(s) for {user.mention} to `{combined_files}`."
            )

    @commands.command(name="pause", aliases=["p"])
    @commands.is_owner()
    async def pause(self, ctx: commands.Context, *, raw_args: str = ""):
        if not ctx.guild:
            await ctx.send("❌ This command must be used within a server.")
            return

        guild_id = str(ctx.guild.id)
        configs = await get_guild_configs(guild_id)

        if not configs:
            await ctx.send("❌ No configurations found for this server.")
            return

        indices, duration = parse_indices_and_duration(raw_args)

        pause_until = None
        if duration is not None:
            seconds = parse_duration(duration)
            if seconds is None:
                await ctx.send("❌ Invalid duration format! Use e.g. `30s`, `4m`, `2h`, `1d`.")
                return
            # Convert to milliseconds for Node.js Date.now() compatibility
            pause_until = int((time.time() + seconds) * 1000)

        # If no numerical indices provided, target ALL configurations in this server
        if not indices:
            target_indices = list(range(1, len(configs) + 1))
        else:
            target_indices = []
            for idx in indices:
                if 1 <= idx <= len(configs):
                    if idx not in target_indices:
                        target_indices.append(idx)
                else:
                    await ctx.send(f"❌ Invalid index `#{idx}`. Server has {len(configs)} configuration(s).")
                    return

        if not target_indices:
            await ctx.send("❌ No valid config indices specified.")
            return

        g_data = await get_global_data()
        accounts = g_data.get("accounts", [])

        if len(target_indices) > 1:
            lines = []
            for idx in target_indices:
                cfg = configs[idx - 1]
                acc_idx = cfg.get("accIndex", 0)
                user_mention = "*Unknown Member*"
                if 1 <= acc_idx <= len(accounts):
                    tok = accounts[acc_idx - 1]
                    uid = get_user_id_from_token(tok)
                    if uid:
                        user_mention = f"<@{uid}>"

                mode = cfg.get("mode", "unknown")
                arg = cfg.get("target") or "None"
                lines.append(f"`[#{idx}]` **Account:** {user_mention} (Acc #{acc_idx}) | **Mode:** `{mode}` | **Argument:** `{arg}`")

            embed = discord.Embed(
                title="⚠️ Confirm Pause Configurations",
                description=f"Are you sure you want to pause the following **{len(target_indices)}** configuration(s)?\n\n" + "\n".join(lines),
                color=discord.Color.orange()
            )
            if duration:
                embed.add_field(name="⏱️ Duration", value=f"**{duration}** (resumes <t:{int(pause_until / 1000)}:R>)", inline=False)

            confirm_view = ConfirmView(author=ctx.author)
            confirm_msg = await ctx.send(embed=embed, view=confirm_view)

            await confirm_view.wait()

            if confirm_view.value is True:
                for idx in target_indices:
                    configs[idx - 1]["paused"] = True
                    configs[idx - 1]["pauseUntil"] = pause_until

                await save_guild_configs(guild_id, configs)

                dur_str = f" for **{duration}** (resumes <t:{int(pause_until / 1000)}:R>)" if duration else " indefinitely"
                await confirm_msg.edit(content=f"⏸️ Paused **{len(target_indices)}** configuration(s){dur_str}.", embed=None, view=None)
            elif confirm_view.value is False:
                await confirm_msg.edit(content="❌ Pause cancelled.", embed=None, view=None)
            else:
                await confirm_msg.edit(content="⏰ Pause confirmation timed out.", embed=None, view=None)
            return

        idx = target_indices[0]
        cfg_idx = idx - 1
        configs[cfg_idx]["paused"] = True
        configs[cfg_idx]["pauseUntil"] = pause_until

        await save_guild_configs(guild_id, configs)

        mode = configs[cfg_idx].get("mode", "unknown")
        acc_idx = configs[cfg_idx].get("accIndex", 0)

        if duration:
            await ctx.send(
                f"⏸️ Paused Config **#{idx}** (`{mode}` | Acc #{acc_idx}) for **{duration}** "
                f"(resumes <t:{int(pause_until / 1000)}:R>)."
            )
        else:
            await ctx.send(f"⏸️ Paused Config **#{idx}** (`{mode}` | Acc #{acc_idx}) indefinitely.")

    @commands.command(name="resume", aliases=["r"])
    @commands.is_owner()
    async def resume(self, ctx: commands.Context, *, raw_args: str = ""):
        if not ctx.guild:
            await ctx.send("❌ This command must be used within a server.")
            return

        guild_id = str(ctx.guild.id)
        configs = await get_guild_configs(guild_id)

        if not configs:
            await ctx.send("❌ No configurations found for this server.")
            return

        indices, _ = parse_indices_and_duration(raw_args)

        if not indices:
            target_indices = list(range(1, len(configs) + 1))
        else:
            target_indices = []
            for idx in indices:
                if 1 <= idx <= len(configs):
                    if idx not in target_indices:
                        target_indices.append(idx)
                else:
                    await ctx.send(f"❌ Invalid index `#{idx}`. Server has {len(configs)} configuration(s).")
                    return

        if not target_indices:
            await ctx.send("❌ No valid config indices specified.")
            return

        g_data = await get_global_data()
        accounts = g_data.get("accounts", [])

        if len(target_indices) > 1:
            lines = []
            for idx in target_indices:
                cfg = configs[idx - 1]
                acc_idx = cfg.get("accIndex", 0)
                user_mention = "*Unknown Member*"
                if 1 <= acc_idx <= len(accounts):
                    tok = accounts[acc_idx - 1]
                    uid = get_user_id_from_token(tok)
                    if uid:
                        user_mention = f"<@{uid}>"

                mode = cfg.get("mode", "unknown")
                arg = cfg.get("target") or "None"
                lines.append(f"`[#{idx}]` **Account:** {user_mention} (Acc #{acc_idx}) | **Mode:** `{mode}` | **Argument:** `{arg}`")

            embed = discord.Embed(
                title="⚠️ Confirm Resume Configurations",
                description=f"Are you sure you want to resume the following **{len(target_indices)}** configuration(s)?\n\n" + "\n".join(lines),
                color=discord.Color.green()
            )

            confirm_view = ConfirmView(author=ctx.author)
            confirm_msg = await ctx.send(embed=embed, view=confirm_view)

            await confirm_view.wait()

            if confirm_view.value is True:
                for idx in target_indices:
                    configs[idx - 1]["paused"] = False
                    configs[idx - 1]["pauseUntil"] = None

                await save_guild_configs(guild_id, configs)

                await confirm_msg.edit(content=f"▶️ Resumed **{len(target_indices)}** configuration(s).", embed=None, view=None)
            elif confirm_view.value is False:
                await confirm_msg.edit(content="❌ Resume cancelled.", embed=None, view=None)
            else:
                await confirm_msg.edit(content="⏰ Resume confirmation timed out.", embed=None, view=None)
            return

        idx = target_indices[0]
        cfg_idx = idx - 1
        configs[cfg_idx]["paused"] = False
        configs[cfg_idx]["pauseUntil"] = None

        await save_guild_configs(guild_id, configs)

        mode = configs[cfg_idx].get("mode", "unknown")
        acc_idx = configs[cfg_idx].get("accIndex", 0)
        await ctx.send(f"▶️ Resumed Config **#{idx}** (`{mode}` | Acc #{acc_idx}).")

    @commands.command(name="syncguilds", aliases=["sg"])
    @commands.is_owner()
    async def syncguilds(self, ctx: commands.Context, from_guild: str, to_guild: str):
        from_id = re.sub(r'\D', '', str(from_guild))
        to_id = re.sub(r'\D', '', str(to_guild))

        if not from_id or not to_id:
            await ctx.send("❌ Please provide valid source and destination Guild IDs.")
            return

        if from_id == to_id:
            await ctx.send("❌ Source and destination Guild IDs cannot be the same.")
            return

        source_configs = await get_guild_configs(from_id)
        source_excludes = await get_guild_excludes(from_id)
        source_logs = await get_guild_logs(from_id)

        if not source_configs and not source_excludes and not source_logs:
            await ctx.send(f"❌ No configurations, excludes, or log channels found for source guild `{from_id}`.")
            return

        if source_configs:
            source_non_spam = [cfg for cfg in source_configs if cfg.get("mode", "").lower() != "spam"]
            target_configs = await get_guild_configs(to_id)
            target_spam = [cfg for cfg in target_configs if cfg.get("mode", "").lower() == "spam"]
            
            merged_configs = target_spam + source_non_spam
            await save_guild_configs(to_id, merged_configs)

        if source_excludes:
            await save_guild_excludes(to_id, source_excludes)

        if source_logs:
            await save_guild_logs(to_id, source_logs)

        await ctx.send(
            f"✅ Successfully synced mode configs (excluding SPAM), excludes, and logs from guild `{from_id}` to guild `{to_id}`!"
        )


# Extension setup entrypoint
async def setup(bot: commands.Bot):
    await bot.add_cog(GrinderCog(bot))