import asyncio
import base64
import re
import time
from typing import Optional
import discord
from discord.ext import commands
import firebase_admin
from firebase_admin import credentials, db

from views.common import ConfirmLayout, error_embed, first_text, make_embed, success_embed
from views.embeds import handle_command_error, send_usage
from views.grinderview import ConfigView
from .base import config_group

# --- CONSTANTS ---
VALID_MODES = ["autocatch", "spam", "dotcatch", "commaedit", "periodicmsg"]

MODE_PARAMS_INFO = {
    "autocatch": {"required": ["Account Index"], "optional": ["Target ID"]},
    "spam": {"required": ["Account Index", "Target / Channel ID"], "optional": []},
    "dotcatch": {"required": ["Account Index", "Channel ID"], "optional": []},
    "commaedit": {"required": ["Account Index", "Channel ID"], "optional": []},
    "periodicmsg": {"required": ["Account Index", "Channel ID", "Message"], "optional": ["Time 1 (Delay)", "Time 2 (Interval)"]},
}

# --- FIREBASE SETUP ---
if not firebase_admin._apps:
    cred = credentials.Certificate("firebase_key.json")
    firebase_admin.initialize_app(cred, {
        'databaseURL': 'https://versatile-kl-default-rtdb.firebaseio.com/'
    })

ref = db.reference("grinder")


# --- HELPER: FIREBASE LIST SANITIZER ---
def _ensure_list(data) -> list:
    if data is None:
        return []
    if isinstance(data, list):
        return [x for x in data if x is not None]
    if isinstance(data, dict):
        sorted_keys = sorted(data.keys(), key=lambda k: int(k) if str(k).isdigit() else k)
        return [data[k] for k in sorted_keys if data[k] is not None]
    return []


# --- HELPER: TIME PARSER ---
def parse_duration(time_str: str):
    if not time_str or not isinstance(time_str, str):
        return None
    match = re.match(r"^(\d+)([smhd])$", time_str.strip().lower())
    if not match:
        return None
    
    amount, unit = int(match.group(1)), match.group(2)
    units = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    return amount * units[unit]


# --- HELPER: PARSE MULTIPLE INDICES & DURATION ---
def parse_indices_and_duration(raw_str: str):
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


# --- HELPER: FORMAT ID WITH NAME (CHANNELS & GUILDS) ---
def format_id_with_name(id_str: str, guild: Optional[discord.Guild] = None) -> str:
    if not id_str or not id_str.isdigit():
        return id_str

    target_id = int(id_str)

    if guild:
        # Check channel in current guild
        channel = guild.get_channel(target_id)
        if channel:
            return f"{id_str} [{channel.name}]"
        
        # Check if ID matches current guild itself
        if guild.id == target_id:
            return f"{id_str} [{guild.name}]"

        # Check in bot's cached guilds via guild._state
        if guild._state and hasattr(guild._state, "_get_guild"):
            g = guild._state._get_guild(target_id)
            if g:
                g_chan = g.get_channel(target_id)
                if g_chan:
                    return f"{id_str} [{g_chan.name}]"
                return f"{id_str} [{g.name}]"

    return id_str


# --- HELPER: PARSE TARGET ASPECTS FOR DISPLAY ---
def parse_target_aspects(mode: str, target: str, guild: Optional[discord.Guild] = None) -> list[tuple[str, str]]:
    if not target:
        return []

    mode_lower = mode.lower()

    # 1) Key-Value style targets ("chid=123, pokes=abc")
    if "=" in target or ":" in target:
        subparts = [p.strip() for p in re.split(r'[,;]', target) if p.strip()]
        if all("=" in p or ":" in p for p in subparts):
            aspects = []
            for p in subparts:
                sep = "=" if "=" in p else ":"
                k, v = p.split(sep, 1)
                key_clean = k.strip()
                val_clean = v.strip()
                formatted_tokens = []
                for token in val_clean.replace(',', ' ').split():
                    formatted_tokens.append(format_id_with_name(token, guild))
                aspects.append((key_clean.capitalize(), ", ".join(formatted_tokens) if formatted_tokens else val_clean))
            return aspects

    # 2) autocatch, dotcatch, commaedit
    if mode_lower in ["autocatch", "dotcatch", "commaedit"]:
        parts = [p.strip() for p in target.split(',') if p.strip()]
        aspects = []
        for i, p in enumerate(parts):
            tokens = p.split()
            fmt_tokens = [format_id_with_name(tok, guild) for tok in tokens]
            val = ", ".join(fmt_tokens)
            if i == 0:
                aspects.append(("Chid", val))
            elif i == 1:
                aspects.append(("Pokemons", val))
            elif i == 2:
                aspects.append(("Datafile", val))
            else:
                aspects.append((f"Arg{i+1}", val))
        return aspects

    # 3) periodicmsg
    elif mode_lower == "periodicmsg":
        parts = [p.strip() for p in re.split(r'[,;]', target) if p.strip()]
        labels = ["Chid", "Message", "Time1", "Time2"]
        aspects = []
        for i, part in enumerate(parts):
            lbl = labels[i] if i < len(labels) else f"Arg{i+1}"
            if i == 0 or lbl.lower() in ["chid", "channel"]:
                tokens = part.split()
                fmt_tokens = [format_id_with_name(tok, guild) for tok in tokens]
                val = ", ".join(fmt_tokens)
            else:
                val = part
            aspects.append((lbl, val))
        return aspects

    # 4) spam
    elif mode_lower == "spam":
        parts = [p.strip() for p in re.split(r'[,;]', target) if p.strip()]
        if not parts:
            parts = [p.strip() for p in target.split() if p.strip()]
        aspects = []
        for i, part in enumerate(parts):
            lbl = "Chid" if i == 0 else f"Arg{i+1}"
            tokens = part.split()
            fmt_tokens = [format_id_with_name(tok, guild) for tok in tokens]
            aspects.append((lbl, ", ".join(fmt_tokens)))
        return aspects

    # 5) Custom / Other modes
    else:
        parts = [p.strip() for p in re.split(r'[,;]', target) if p.strip()]
        if not parts:
            parts = [p.strip() for p in target.split() if p.strip()]
        aspects = []
        for i, part in enumerate(parts):
            tokens = part.split()
            fmt_tokens = [format_id_with_name(tok, guild) for tok in tokens]
            aspects.append((f"Arg{i+1}", ", ".join(fmt_tokens)))
        return aspects


# --- HELPERS FOR CONFIG EDITING ---
def get_config_details(cfg: dict) -> dict:
    mode = cfg.get("mode", "").lower()
    target_str = cfg.get("target", "")
    acc_idx = cfg.get("accIndex", 1)

    aspects_list = parse_target_aspects(mode, target_str)
    aspects = {k.lower(): v for k, v in aspects_list}

    details = {
        "accIndex": acc_idx,
        "token": cfg.get("token", ""),
        "chid": aspects.get("chid", aspects.get("target id", "")),
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
    mode_lower = mode.lower()

    if mode_lower in ["autocatch", "dotcatch", "commaedit"]:
        target_ids = str(fields.get("chid", fields.get("target id", "")) or "").strip()
        pokes = str(fields.get("pokemons", "") or "").strip()
        datafile = str(fields.get("datafile", "") or "").strip()

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
        chid = str(fields.get("chid", "") or "").strip()
        msg = str(fields.get("message", "") or "").strip()
        t1 = str(fields.get("time1", "") or "").strip()
        t2 = str(fields.get("time2", "") or "").strip()

        parts = [chid, msg]
        if t1:
            parts.append(t1)
        if t2:
            parts.append(t2)
        return " ; ".join([p for p in parts if p]), False

    elif mode_lower == "spam":
        target = str(fields.get("chid", fields.get("target", "")) or "").strip()
        return target, False

    else:
        target = str(fields.get("target", "") or "").strip()
        return target, False


# --- HELPER: DECODE DISCORD USER ID FROM TOKEN ---
def get_user_id_from_token(token: str):
    if not token or not isinstance(token, str):
        return None
    try:
        part = token.split('.')[0]
        padded = part + '=' * (-len(part) % 4)
        try:
            decoded = base64.urlsafe_b64decode(padded.encode('utf-8')).decode('utf-8')
        except Exception:
            decoded = base64.b64decode(padded.encode('utf-8')).decode('utf-8')
        return int(decoded) if decoded.isdigit() else None
    except Exception:
        return None


def is_channel_allowed(target_str: str, channel_id: int) -> bool:
    if not target_str:
        return True
    channel_ids = re.findall(r'\d+', target_str)
    if not channel_ids:
        return True
    return str(channel_id) in channel_ids


# --- FIREBASE HELPERS ---
async def get_global_data():
    data = await asyncio.to_thread(ref.get) or {}
    if not isinstance(data, dict):
        data = {}
    return {
        "accounts": _ensure_list(data.get("accounts")),
        "guilds": _ensure_list(data.get("guilds"))
    }


async def get_autocatch_status():
    data = await asyncio.to_thread(ref.child("autocatch").get) or {}
    if not isinstance(data, dict):
        data = {}
    return {
        "pairs": _ensure_list(data.get("pairs")),
        "current_catcher": data.get("current_catcher", {}) if isinstance(data.get("current_catcher"), dict) else {}
    }


async def save_global_data(data):
    await asyncio.to_thread(ref.child("accounts").set, data.get("accounts", []))
    await asyncio.to_thread(ref.child("guilds").set, data.get("guilds", []))


async def get_guild_configs(guild_id: str):
    configs = await asyncio.to_thread(ref.child("configs").child(str(guild_id)).get)
    return _ensure_list(configs)


async def save_guild_configs(guild_id: str, configs: list):
    await asyncio.to_thread(ref.child("configs").child(str(guild_id)).set, configs)


async def get_guild_excludes(guild_id: str):
    excludes = await asyncio.to_thread(ref.child("excludes").child(str(guild_id)).get)
    return _ensure_list(excludes)


async def save_guild_excludes(guild_id: str, excludes: list):
    await asyncio.to_thread(ref.child("excludes").child(str(guild_id)).set, excludes)


async def get_guild_detector_bots(guild_id: str):
    bots = await asyncio.to_thread(ref.child("detector_bots").child(str(guild_id)).get)
    return _ensure_list(bots)


async def save_guild_detector_bots(guild_id: str, bots: list):
    await asyncio.to_thread(ref.child("detector_bots").child(str(guild_id)).set, bots)


async def get_guild_logs(guild_id: str):
    logs = await asyncio.to_thread(ref.child("logs").child(str(guild_id)).get) or {}
    return logs if isinstance(logs, dict) else {}


async def save_guild_log(guild_id: str, log_type: str, channel_id: str):
    await asyncio.to_thread(ref.child("logs").child(str(guild_id)).child(log_type).set, channel_id)


async def save_guild_logs(guild_id: str, logs: dict):
    await asyncio.to_thread(ref.child("logs").child(str(guild_id)).set, logs)


# --- HELPER TO CHUNK EMBED FIELDS ---
def add_chunked_field(embed: discord.Embed, title: str, lines: list[str]):
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
async def build_mode_configs_embed(guild: discord.Guild, configs: list, accounts: list):
    embed = discord.Embed(title=f"📋 Mode Configurations — {guild.name}", color=discord.Color.gold())
    if not configs:
        embed.description = "No configs in this server."
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
            tok = cfg.get("token")
            acc_idx = 0
            
            if tok and tok in accounts:
                acc_idx = accounts.index(tok) + 1
            elif not tok and "accIndex" in cfg:
                acc_idx = cfg["accIndex"]
                if 1 <= acc_idx <= len(accounts):
                    tok = accounts[acc_idx - 1]

            user_mention = "*Unknown Member*"
            display_name = ""

            if tok:
                uid = get_user_id_from_token(tok)
                if uid:
                    user_mention = f"<@{uid}>"
                    member = guild.get_member(uid)
                    if member:
                        display_name = f" ({member.display_name})"

            acc_str = f" (Acc #{acc_idx})" if acc_idx > 0 else " (Removed Acc)"
            line = f"`[#{idx}]` {user_mention}{display_name}{acc_str}"

            paused = cfg.get("paused", False)
            pause_until = cfg.get("pauseUntil")
            if paused and pause_until:
                pause_until_sec = int(pause_until / 1000) if pause_until > 1e11 else int(pause_until)
                if current_time < pause_until_sec:
                    line += f" | ⏸️ **Paused** (<t:{pause_until_sec}:R>)"
                else:
                    line += " | ⏸️ **Paused** (Expired)"
            elif paused:
                line += " | ⏸️ **Paused**"
            else:
                line += " | 🟢 **Active**"

            target_str = cfg.get('target', '')
            aspects = parse_target_aspects(mode, target_str, guild)
            for label, val in aspects:
                if val:
                    line += f"\n  {label}: `{val}`"

            lines.append(line)

        add_chunked_field(embed, f"⚙️ {mode.upper()}", lines)

    if other_configs:
        lines = []
        for idx, cfg in other_configs:
            tok = cfg.get("token")
            acc_idx = 0
            
            if tok and tok in accounts:
                acc_idx = accounts.index(tok) + 1
            elif not tok and "accIndex" in cfg:
                acc_idx = cfg["accIndex"]
                if 1 <= acc_idx <= len(accounts):
                    tok = accounts[acc_idx - 1]

            user_mention = "*Unknown Member*"
            display_name = ""

            if tok:
                uid = get_user_id_from_token(tok)
                if uid:
                    user_mention = f"<@{uid}>"
                    member = guild.get_member(uid)
                    if member:
                        display_name = f" ({member.display_name})"

            acc_str = f" (Acc #{acc_idx})" if acc_idx > 0 else " (Removed Acc)"
            cfg_mode = cfg.get('mode', 'unknown')
            line = f"`[#{idx}]` {cfg_mode} | {user_mention}{display_name}{acc_str}"

            paused = cfg.get("paused", False)
            pause_until = cfg.get("pauseUntil")
            if paused and pause_until:
                pause_until_sec = int(pause_until / 1000) if pause_until > 1e11 else int(pause_until)
                if current_time < pause_until_sec:
                    line += f" | ⏸️ **Paused** (<t:{pause_until_sec}:R>)"
                else:
                    line += " | ⏸️ **Paused** (Expired)"
            elif paused:
                line += " | ⏸️ **Paused**"
            else:
                line += " | 🟢 **Active**"

            target_str = cfg.get('target', '')
            aspects = parse_target_aspects(cfg_mode, target_str, guild)
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

    current_acc_idx = current_catcher_info.get("accIndex") if isinstance(current_catcher_info, dict) else None
    if current_acc_idx and isinstance(current_acc_idx, int) and 1 <= current_acc_idx <= len(accounts):
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
            if isinstance(pair, (list, tuple)):
                for p_acc in pair:
                    if isinstance(p_acc, int) and 1 <= p_acc <= len(accounts):
                        p_tok = accounts[p_acc - 1]
                        p_uid = get_user_id_from_token(p_tok)
                        p_m = f"<@{p_uid}>" if p_uid else f"Acc #{p_acc}"
                        pair_members.append(f"Acc #{p_acc} ({p_m})")
                    else:
                        pair_members.append(f"Acc #{p_acc}")
            pairs_str_list.append(f"• **Pair #{p_idx}:** {' & '.join(pair_members)}")
        pairs_fmt = "\n".join(pairs_str_list)
    else:
        pairs_fmt = "*None*"

    embed.add_field(name="🎯 Current Catcher Turn", value=current_catcher_str, inline=False)
    embed.add_field(name="👥 Active Catch Pairs", value=pairs_fmt, inline=False)
    return embed


async def build_excludes_configs_embed(guild: discord.Guild, excludes: list):
    embed = discord.Embed(title=f"🚫 Excludes Configurations — {guild.name}", color=discord.Color.red())
    if not excludes:
        embed.description = "No excludes."
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
        embed.description = "No detector bots."
        return embed

    bot_strs = []
    for idx, bot_id in enumerate(bots, 1):
        bot_strs.append(f"`[#{idx}]` <@{bot_id}> (`{bot_id}`)")

    add_chunked_field(embed, "🤖 Assigned Detector Bots", bot_strs)
    return embed


# --- HELPER TO REFRESH CONFIG EMBEDS LIVE ---
async def refresh_config_embed(interaction: discord.Interaction, override_page: str = None):
    message = interaction.message
    if not (message and interaction.guild):
        return

    current_title = first_text(message)
    if not (override_page or current_title):
        return

    guild_id = str(interaction.guild_id)

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

    embed = None
    if page == "autocatch":
        autocatch_data = await get_autocatch_status()
        g_data = await get_global_data()
        accounts = g_data.get("accounts", [])
        embed = await build_autocatch_configs_embed(interaction.guild, autocatch_data, accounts)
    elif page == "excludes":
        excludes = await get_guild_excludes(guild_id)
        embed = await build_excludes_configs_embed(interaction.guild, excludes)
    elif page == "detector_bots":
        bots = await get_guild_detector_bots(guild_id)
        embed = await build_detector_bots_embed(interaction.guild, bots)
    elif page == "modes":
        configs = await get_guild_configs(guild_id)
        g_data = await get_global_data()
        accounts = g_data.get("accounts", [])
        embed = await build_mode_configs_embed(interaction.guild, configs, accounts)

    if embed is not None:
        await message.edit(view=ConfigView(page=page, embed=embed))


# --- CONFIG GROUP SUBCOMMANDS ---
@config_group.group(name="grinder", aliases=["g", "grind"], invoke_without_command=True)
@commands.is_owner()
async def grindconfig(ctx: commands.Context):
    """Shows mode configurations for the current server."""
    if ctx.invoked_subcommand is None:
        if not ctx.guild:
            await ctx.send(embed=error_embed("Server only."))
            return

        guild_id = str(ctx.guild.id)
        configs = await get_guild_configs(guild_id)
        g_data = await get_global_data()
        accounts = g_data.get("accounts", [])

        embed = await build_mode_configs_embed(ctx.guild, configs, accounts)
        await ctx.send(view=ConfigView(page="modes", embed=embed))


@grindconfig.command(name="add", aliases=["a"])
@commands.is_owner()
async def grindconfig_add(
    ctx: commands.Context,
    mode: str = commands.parameter(description="Mode name (autocatch, spam, dotcatch, commaedit, periodicmsg)."),
    acc_idx: int = commands.parameter(description="Account index (1-based)."),
    *,
    args: str = commands.parameter(default="", description="Arguments separated by commas.")
):
    """Add a mode configuration: .c g a {mode} {account index} {args separated by commas}"""
    if not ctx.guild:
        await ctx.send(embed=error_embed("Server only."))
        return

    mode_lower = mode.lower()
    if mode_lower not in VALID_MODES:
        await ctx.send(embed=error_embed(
            f"Invalid mode `{mode}`. Valid modes are: {', '.join([f'`{m}`' for m in VALID_MODES])}"
        ))
        return

    g_data = await get_global_data()
    accounts = g_data.get("accounts", [])

    if not accounts:
        await ctx.send(embed=error_embed("No accounts registered in global data."))
        return

    if acc_idx < 1 or acc_idx > len(accounts):
        await ctx.send(embed=error_embed(
            f"Invalid account index #{acc_idx}. Valid account range is `1` to `{len(accounts)}`."
        ))
        return

    token = accounts[acc_idx - 1]
    arg_list = [a.strip() for a in args.split(",") if a.strip()]

    # Validate mode-specific required arguments
    if mode_lower == "spam" and not arg_list:
        await ctx.send(embed=error_embed("Missing required argument: Target / Channel ID for `spam` mode."))
        return
    elif mode_lower in ["dotcatch", "commaedit"] and not arg_list:
        await ctx.send(embed=error_embed(f"Missing required argument: Channel ID for `{mode_lower}` mode."))
        return
    elif mode_lower == "periodicmsg" and len(arg_list) < 2:
        await ctx.send(embed=error_embed(
            "Missing required arguments for `periodicmsg` mode. Format: `{channel_id}, {message}, [time1], [time2]`"
        ))
        return

    if mode_lower == "periodicmsg":
        target_str = " ; ".join(arg_list)
    else:
        target_str = ", ".join(arg_list)

    guild_id = str(ctx.guild.id)
    configs = await get_guild_configs(guild_id)

    new_cfg = {
        "mode": mode_lower,
        "accIndex": acc_idx,
        "token": token,
        "target": target_str,
        "paused": False
    }

    configs.append(new_cfg)
    await save_guild_configs(guild_id, configs)

    new_index = len(configs)
    user_uid = get_user_id_from_token(token)
    user_mention = f"<@{user_uid}>" if user_uid else f"Acc #{acc_idx}"

    desc = f"Added config `[#{new_index}]` for {user_mention} (Acc #{acc_idx})\n**Mode:** `{mode_lower}`"
    if target_str:
        desc += f"\n**Target/Args:** `{target_str}`"

    await ctx.send(embed=success_embed(desc))


@grindconfig.command(name="remove", aliases=["r"])
@commands.is_owner()
async def grindconfig_remove(
    ctx: commands.Context,
    index: int = commands.parameter(description="Index displayed in .c g.")
):
    """Remove a mode configuration: .c g r {index}"""
    if not ctx.guild:
        await ctx.send(embed=error_embed("Server only."))
        return

    guild_id = str(ctx.guild.id)
    configs = await get_guild_configs(guild_id)

    if not configs:
        await ctx.send(embed=error_embed("No configs in this server."))
        return

    if index < 1 or index > len(configs):
        await ctx.send(embed=error_embed(f"Invalid index `#{index}`. Server has `{len(configs)}` config(s)."))
        return

    removed_cfg = configs.pop(index - 1)
    await save_guild_configs(guild_id, configs)

    mode = removed_cfg.get("mode", "unknown")
    acc_idx = removed_cfg.get("accIndex", 0)
    tok = removed_cfg.get("token")

    g_data = await get_global_data()
    accounts = g_data.get("accounts", [])
    if tok and tok in accounts:
        acc_idx = accounts.index(tok) + 1

    user_uid = get_user_id_from_token(tok) if tok else None
    user_mention = f"<@{user_uid}>" if user_uid else (f"Acc #{acc_idx}" if acc_idx > 0 else "Unknown Acc")

    await ctx.send(embed=success_embed(
        f"Removed config `[#{index}]` (`{mode}` | {user_mention} | Acc #{acc_idx})."
    ))


async def _config_command_error(ctx: commands.Context, error: Exception):
    await handle_command_error(ctx, error)


grindconfig.error(_config_command_error)
grindconfig_add.error(_config_command_error)
grindconfig_remove.error(_config_command_error)


# --- COG DEFINITION ---
class GrinderCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        """Every command in this cog: embeds for errors, how-to-use embed for missing arguments."""
        await handle_command_error(ctx, error)

    @commands.group(name="grindconfig", aliases=["gc", "g"], invoke_without_command=True)
    @commands.is_owner()
    async def grindconfig_cog(self, ctx: commands.Context):
        """Top-level command alias for viewing server configs."""
        if ctx.invoked_subcommand is None:
            await grindconfig(ctx)

    @grindconfig_cog.command(name="add", aliases=["a"])
    @commands.is_owner()
    async def grindconfig_cog_add(
        self,
        ctx: commands.Context,
        mode: str = commands.parameter(description="Mode name."),
        acc_idx: int = commands.parameter(description="Account index."),
        *,
        args: str = commands.parameter(default="", description="Arguments separated by commas.")
    ):
        """Add a mode configuration."""
        await grindconfig_add(ctx, mode, acc_idx, args=args)

    @grindconfig_cog.command(name="remove", aliases=["r"])
    @commands.is_owner()
    async def grindconfig_cog_remove(
        self,
        ctx: commands.Context,
        index: int = commands.parameter(description="Index displayed in .c g.")
    ):
        """Remove a mode configuration by its index."""
        await grindconfig_remove(ctx, index)

    @commands.command(name="edit")
    @commands.is_owner()
    async def edit(
        self,
        ctx: commands.Context,
        user: discord.User = commands.parameter(description="Member (mention or ID)."),
        *,
        filenames: str = commands.parameter(description="Datafile name(s); .json is optional."),
    ):
        """Set datafile(s) for a member's autocatch configs."""
        if not ctx.guild:
            await ctx.send(embed=error_embed("Server only."))
            return

        g_data = await get_global_data()
        accounts = g_data.get("accounts", [])

        user_tokens = [
            token for token in accounts
            if get_user_id_from_token(token) == user.id
        ]

        if not user_tokens:
            await ctx.send(embed=error_embed(f"No accounts for {user.mention}."))
            return

        guild_id = str(ctx.guild.id)
        configs = await get_guild_configs(guild_id)

        if not configs:
            await ctx.send(embed=error_embed("No configs in this server."))
            return

        raw_files = [f.strip() for f in filenames.replace(',', ' ').split() if f.strip()]
        if not raw_files:
            await send_usage(ctx, note="Give a datafile name.")
            return

        combined_files = " ".join([
            f if f.lower().endswith(".json") else f"{f}.json"
            for f in raw_files
        ])

        updated_count = 0
        for cfg in configs:
            cfg_tok = cfg.get("token")
            if not cfg_tok and "accIndex" in cfg:
                acc_idx = cfg["accIndex"]
                if 1 <= acc_idx <= len(accounts):
                    cfg_tok = accounts[acc_idx - 1]

            if cfg.get("mode", "").lower() == "autocatch" and cfg_tok in user_tokens:
                details = get_config_details(cfg)
                details["datafile"] = combined_files
                new_target, _ = build_target_string_for_mode("autocatch", details)
                cfg["target"] = new_target
                updated_count += 1

        if updated_count > 0:
            await save_guild_configs(guild_id, configs)
            await ctx.send(embed=success_embed(
                f"Updated {updated_count} autocatch config(s) for {user.mention}: `{combined_files}`."
            ))
        else:
            await ctx.send(embed=error_embed(
                f"{user.mention} has no autocatch configs here."
            ))

    @commands.command(name="pause", aliases=["p"])
    @commands.is_owner()
    async def pause(
        self,
        ctx: commands.Context,
        *,
        raw_args: str = commands.parameter(default="", description="Config numbers and/or a duration (30s, 2h, 1d). Empty = all."),
    ):
        """Pause configs (all, or the numbers you list)."""
        if not ctx.guild:
            await ctx.send(embed=error_embed("Server only."))
            return

        guild_id = str(ctx.guild.id)
        configs = await get_guild_configs(guild_id)

        if not configs:
            await ctx.send(embed=error_embed("No configs in this server."))
            return

        indices, duration = parse_indices_and_duration(raw_args)

        pause_until = None
        if duration is not None:
            seconds = parse_duration(duration)
            if seconds is None:
                await send_usage(ctx, note="Invalid duration, e.g. `30s`, `2h`.")
                return
            pause_until = int((time.time() + seconds) * 1000)

        if not indices:
            target_indices = list(range(1, len(configs) + 1))
        else:
            target_indices = []
            for idx in indices:
                if 1 <= idx <= len(configs):
                    if idx not in target_indices:
                        target_indices.append(idx)
                else:
                    await ctx.send(embed=error_embed(f"Invalid index #{idx}. Max is {len(configs)}."))
                    return

        if not target_indices:
            await ctx.send(embed=error_embed("No valid configs."))
            return

        g_data = await get_global_data()
        accounts = g_data.get("accounts", [])

        if len(target_indices) > 1:
            lines = []
            for idx in target_indices:
                cfg = configs[idx - 1]
                tok = cfg.get("token")
                acc_idx = 0
                
                if tok and tok in accounts:
                    acc_idx = accounts.index(tok) + 1
                elif not tok and "accIndex" in cfg:
                    acc_idx = cfg["accIndex"]
                    if 1 <= acc_idx <= len(accounts):
                        tok = accounts[acc_idx - 1]

                user_mention = "*Unknown Member*"
                if tok:
                    uid = get_user_id_from_token(tok)
                    if uid:
                        user_mention = f"<@{uid}>"

                mode = cfg.get("mode", "unknown")
                arg = cfg.get("target") or "None"
                acc_str = f"Acc #{acc_idx}" if acc_idx > 0 else "Removed Acc"
                lines.append(f"`[#{idx}]` {user_mention} ({acc_str}) | `{mode}` | `{arg}`")

            embed = discord.Embed(
                title="⚠️ Pause",
                description=f"Pause **{len(target_indices)}** config(s)?\n\n" + "\n".join(lines),
                color=discord.Color.orange()
            )
            if duration:
                embed.add_field(name="⏱️ Duration", value=f"**{duration}** (resumes <t:{int(pause_until / 1000)}:R>)", inline=False)

            confirm_view = ConfirmLayout(
                ctx.author,
                embed=embed,
                cancel_text="❌ Cancelled.",
                timeout_text="⏰ Timed out.",
            )
            await confirm_view.send(ctx, reply=False)
            await confirm_view.wait()

            if confirm_view.value is True:
                for idx in target_indices:
                    configs[idx - 1]["paused"] = True
                    configs[idx - 1]["pauseUntil"] = pause_until

                await save_guild_configs(guild_id, configs)

                dur_str = f" for **{duration}** (resumes <t:{int(pause_until / 1000)}:R>)" if duration else " indefinitely"
                await confirm_view.show(make_embed(description=f"⏸️ Paused **{len(target_indices)}** configuration(s){dur_str}."))
            return

        idx = target_indices[0]
        cfg_idx = idx - 1
        configs[cfg_idx]["paused"] = True
        configs[cfg_idx]["pauseUntil"] = pause_until

        await save_guild_configs(guild_id, configs)

        mode = configs[cfg_idx].get("mode", "unknown")
        
        cfg = configs[cfg_idx]
        tok = cfg.get("token")
        acc_idx = 0
        if tok and tok in accounts:
            acc_idx = accounts.index(tok) + 1
        elif not tok and "accIndex" in cfg:
            acc_idx = cfg["accIndex"]
            if 1 <= acc_idx <= len(accounts):
                tok = accounts[acc_idx - 1]

        acc_str = f"Acc #{acc_idx}" if acc_idx > 0 else "Removed Acc"

        if duration:
            await ctx.send(embed=make_embed(description=(
                f"⏸ Paused Config **#{idx}** (`{mode}` | {acc_str}) for **{duration}** "
                f"(resumes <t:{int(pause_until / 1000)}:R>)."
            )))
        else:
            await ctx.send(embed=make_embed(
                description=f"⏸️ Paused Config **#{idx}** (`{mode}` | {acc_str}) indefinitely."
            ))

    @commands.command(name="resume", aliases=["r"])
    @commands.is_owner()
    async def resume(
        self,
        ctx: commands.Context,
        *,
        raw_args: str = commands.parameter(default="", description="Config numbers. Empty = all."),
    ):
        """Resume configs (all, or the numbers you list)."""
        if not ctx.guild:
            await ctx.send(embed=error_embed("Server only."))
            return

        guild_id = str(ctx.guild.id)
        configs = await get_guild_configs(guild_id)

        if not configs:
            await ctx.send(embed=error_embed("No configs in this server."))
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
                    await ctx.send(embed=error_embed(f"Invalid index #{idx}. Max is {len(configs)}."))
                    return

        if not target_indices:
            await ctx.send(embed=error_embed("No valid configs."))
            return

        g_data = await get_global_data()
        accounts = g_data.get("accounts", [])

        if len(target_indices) > 1:
            lines = []
            for idx in target_indices:
                cfg = configs[idx - 1]
                tok = cfg.get("token")
                acc_idx = 0
                
                if tok and tok in accounts:
                    acc_idx = accounts.index(tok) + 1
                elif not tok and "accIndex" in cfg:
                    acc_idx = cfg["accIndex"]
                    if 1 <= acc_idx <= len(accounts):
                        tok = accounts[acc_idx - 1]

                user_mention = "*Unknown Member*"
                if tok:
                    uid = get_user_id_from_token(tok)
                    if uid:
                        user_mention = f"<@{uid}>"

                mode = cfg.get("mode", "unknown")
                arg = cfg.get("target") or "None"
                acc_str = f"Acc #{acc_idx}" if acc_idx > 0 else "Removed Acc"
                lines.append(f"`[#{idx}]` {user_mention} ({acc_str}) | `{mode}` | `{arg}`")

            embed = discord.Embed(
                title="⚠️ Resume",
                description=f"Resume **{len(target_indices)}** config(s)?\n\n" + "\n".join(lines),
                color=discord.Color.green()
            )

            confirm_view = ConfirmLayout(
                ctx.author,
                embed=embed,
                cancel_text="❌ Cancelled.",
                timeout_text="⏰ Timed out.",
            )
            await confirm_view.send(ctx, reply=False)
            await confirm_view.wait()

            if confirm_view.value is True:
                for idx in target_indices:
                    configs[idx - 1]["paused"] = False
                    configs[idx - 1]["pauseUntil"] = None

                await save_guild_configs(guild_id, configs)

                await confirm_view.show(make_embed(description=f"▶️ Resumed **{len(target_indices)}** configuration(s)."))
            return

        idx = target_indices[0]
        cfg_idx = idx - 1
        configs[cfg_idx]["paused"] = False
        configs[cfg_idx]["pauseUntil"] = None

        await save_guild_configs(guild_id, configs)

        mode = configs[cfg_idx].get("mode", "unknown")
        
        cfg = configs[cfg_idx]
        tok = cfg.get("token")
        acc_idx = 0
        if tok and tok in accounts:
            acc_idx = accounts.index(tok) + 1
        elif not tok and "accIndex" in cfg:
            acc_idx = cfg["accIndex"]
            if 1 <= acc_idx <= len(accounts):
                tok = accounts[acc_idx - 1]

        acc_str = f"Acc #{acc_idx}" if acc_idx > 0 else "Removed Acc"
        await ctx.send(embed=make_embed(description=f"▶️ Resumed Config **#{idx}** (`{mode}` | {acc_str})."))

    @commands.command(name="syncguilds", aliases=["sg"])
    @commands.is_owner()
    async def syncguilds(
        self,
        ctx: commands.Context,
        from_guild: str = commands.parameter(description="Source server ID."),
        to_guild: str = commands.parameter(description="Target server ID."),
    ):
        """Copy configs (except SPAM), excludes and logs to another server."""
        from_id = re.sub(r'\D', '', str(from_guild))
        to_id = re.sub(r'\D', '', str(to_guild))

        if not from_id or not to_id:
            await send_usage(ctx, note="Give two valid server IDs.")
            return

        if from_id == to_id:
            await ctx.send(embed=error_embed("IDs must differ."))
            return

        source_configs = await get_guild_configs(from_id)
        source_excludes = await get_guild_excludes(from_id)
        source_logs = await get_guild_logs(from_id)

        if not source_configs and not source_excludes and not source_logs:
            await ctx.send(embed=error_embed(f"Nothing to sync from {from_id}."))
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

        await ctx.send(embed=success_embed(
            f"Synced configs (no SPAM), excludes and logs: `{from_id}` → `{to_id}`."
        ))


async def setup(bot: commands.Bot):
    await bot.add_cog(GrinderCog(bot))