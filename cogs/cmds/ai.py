import asyncio
import json
import os
import re
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord.ext import commands

from views.embeds import (
    BRAND_COLOR,
    err_embed,
    handle_common_error,
    info_embed,
    make_embed,
    ok_embed,
    send_usage,
    warn_embed,
)

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_CANDIDATE_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "config.json"
)

if os.path.exists(_CANDIDATE_CONFIG_PATH):
    CONFIG_PATH = os.path.abspath(_CANDIDATE_CONFIG_PATH)
else:
    CONFIG_PATH = os.path.abspath("config.json")

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

PROJECT_ROOT = os.path.dirname(CONFIG_PATH)
USAGE_FILE_PATH = os.path.join(PROJECT_ROOT, "key_usage.json")

BOT_PREFIX = config.get("PREFIX", "!")
OWNER_IDS = set(config.get("OWNER_IDS", []))

# --- API keys ----------------------------------------------------------
_raw_keys = config.get("AI_CHATBOT_KEYS")
if not _raw_keys:
    _legacy_key = config.get("AI_CHATBOT")
    if not _legacy_key:
        raise ValueError(
            "config.json needs either 'AI_CHATBOT_KEYS' (a list of OpenRouter API keys) "
            "or the legacy 'AI_CHATBOT' (a single key string)."
        )
    _raw_keys = [_legacy_key]
if isinstance(_raw_keys, str):
    _raw_keys = [_raw_keys]

OPENROUTER_API_KEYS = [k for k in _raw_keys if k]
if not OPENROUTER_API_KEYS:
    raise ValueError("No usable OpenRouter API keys found in config.json.")

# --- Account grouping for keys ------------------------------------------
_raw_key_accounts = config.get("AI_CHATBOT_KEY_ACCOUNTS")
if _raw_key_accounts:
    if len(_raw_key_accounts) != len(OPENROUTER_API_KEYS):
        raise ValueError(
            "'AI_CHATBOT_KEY_ACCOUNTS' must have exactly one entry per key in "
            "'AI_CHATBOT_KEYS' (same order, same length)."
        )
    KEY_ACCOUNT_IDS = [str(a) for a in _raw_key_accounts]
else:
    KEY_ACCOUNT_IDS = [f"key{i + 1}" for i in range(len(OPENROUTER_API_KEYS))]

# --- Per-key daily request limits ---------------------------------------
# OpenRouter's free-model daily cap is 50 requests/day per key by default,
# or 1000 requests/day once that key's account has ever purchased >= $10 of
# credits. Configure this per key in config.json, e.g.:
#   "AI_CHATBOT_KEY_DAILY_LIMITS": [1000, 50, 50]
# A single number applies to every key. If omitted, every key defaults to 50.
DEFAULT_FREE_DAILY_LIMIT = 50

_raw_key_limits = config.get("AI_CHATBOT_KEY_DAILY_LIMITS")
if _raw_key_limits is not None:
    if isinstance(_raw_key_limits, (int, float)):
        _raw_key_limits = [int(_raw_key_limits)] * len(OPENROUTER_API_KEYS)
    if len(_raw_key_limits) != len(OPENROUTER_API_KEYS):
        raise ValueError(
            "'AI_CHATBOT_KEY_DAILY_LIMITS' must have exactly one entry per key in "
            "'AI_CHATBOT_KEYS' (same order, same length), or be a single number."
        )
    KEY_DAILY_LIMITS = [int(x) for x in _raw_key_limits]
else:
    KEY_DAILY_LIMITS = [DEFAULT_FREE_DAILY_LIMIT] * len(OPENROUTER_API_KEYS)

# MongoDB location of the per-server AI channel whitelist (same DB as dex.py).
AI_DB_NAME = "utilities"
AI_CHANNELS_COLLECTION = "aichannels"

BUSY_REACTION = "⏳"
BUSY_DELETE_AFTER_SECONDS = 5

MAX_TOOL_ITERATIONS = 32       # limit for multi-step file edits
TOOL_CALL_MAX_TOKENS = 4096    # budget for reasoning/tools
CHAT_MAX_TOKENS = 1024
REQUEST_TIMEOUT_SECONDS = 30   # Increased to 30s to allow free models sufficient time

SUBTASK_MAX_ITERATIONS = 6
SUBTASK_MAX_TOKENS = 1024


# ---------------------------------------------------------------------------
# Persistent Key Usage Helpers
# ---------------------------------------------------------------------------

def load_key_usage() -> defaultdict:
    if os.path.exists(USAGE_FILE_PATH):
        try:
            with open(USAGE_FILE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("date") == datetime.now(timezone.utc).date().isoformat():
                    return defaultdict(int, data.get("usage", {}))
        except Exception:
            pass
    return defaultdict(int)


def save_key_usage(usage_dict: dict):
    try:
        data = {
            "date": datetime.now(timezone.utc).date().isoformat(),
            "usage": dict(usage_dict),
        }
        with open(USAGE_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def split_message(text: str, limit: int = 2000) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = text.rfind(" ", 0, limit)
        if split_at == -1:
            split_at = limit

        chunks.append(text[:split_at].strip())
        text = text[split_at:].lstrip()

    if text:
        chunks.append(text)

    return chunks


# Some free/fallback models (e.g. DeepSeek-style) don't use a separate
# "reasoning"/"thinking" field — they embed their chain-of-thought directly
# inline in `content` using tags like <think>...</think>. Left alone, that
# text leaks straight into the final Discord reply and makes the bot look
# like it "keeps talking" after it's actually done. This strips any such
# tags out of a content string and returns (thinking_text, cleaned_text).
_THINK_TAG_RE = re.compile(r"<(think|thinking|reasoning)>(.*?)</\1>", re.IGNORECASE | re.DOTALL)


def extract_inline_thinking(content: str) -> tuple[str, str]:
    if not content:
        return "", ""

    found = [m.group(2).strip() for m in _THINK_TAG_RE.finditer(content) if m.group(2).strip()]
    cleaned = _THINK_TAG_RE.sub("", content).strip()
    return "\n\n".join(found), cleaned


# Embed descriptions can hold 4096 chars; stay a little under it.
EMBED_DESCRIPTION_LIMIT = 4000


def format_duration(seconds: float) -> str:
    seconds = max(1, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m {secs}s"


class ThinkingStatus:
    """
    Shows a single #0414c7 embed while the AI works:
      - while running:  "Thinking..." + a live Discord relative timestamp (<t:...:R>)
      - when finished:  "Thought for N seconds" + a Discord timestamp of when it finished
    The model's reasoning / tool-call steps are never displayed.
    """

    def __init__(self, source_message: discord.Message):
        self.source = source_message
        self.status_msg: discord.Message | None = None
        self.started_at = time.time()

    async def start(self):
        embed = discord.Embed(
            title="🧠 Thinking...",
            description=f"⏳ Started <t:{int(self.started_at)}:R>",
            color=BRAND_COLOR,
        )
        try:
            self.status_msg = await self.source.reply(embed=embed, mention_author=False)
        except discord.HTTPException:
            self.status_msg = None

    async def finish(self, success: bool = True):
        if not self.status_msg:
            return
        now = time.time()
        duration = format_duration(now - self.started_at)
        if success:
            embed = discord.Embed(
                title=f"✅ Thought for {duration}",
                description=f"🕒 Finished <t:{int(now)}:T> (<t:{int(now)}:R>)",
                color=BRAND_COLOR,
            )
        else:
            embed = discord.Embed(
                title=f"❌ Stopped after {duration}",
                description=f"🕒 Ended <t:{int(now)}:T> (<t:{int(now)}:R>)",
                color=BRAND_COLOR,
            )
        try:
            await self.status_msg.edit(embed=embed)
        except discord.HTTPException:
            pass


def is_bot_owner():
    """Owner-only check: Discord application owner OR an id listed in config OWNER_IDS."""

    async def predicate(ctx: commands.Context) -> bool:
        if await ctx.bot.is_owner(ctx.author) or ctx.author.id in OWNER_IDS:
            return True
        raise commands.NotOwner("You do not own this bot.")

    return commands.check(predicate)


class AIChat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.openrouter_url = "https://openrouter.ai/api/v1/chat/completions"
        self.key_info_url = "https://openrouter.ai/api/v1/key"

        self.api_keys = OPENROUTER_API_KEYS
        self.primary_key = self.api_keys[0]
        self.worker_keys = self.api_keys[1:] if len(self.api_keys) > 1 else self.api_keys
        self._worker_key_cycle_idx = 0

        self.key_account_id: dict[str, str] = dict(zip(self.api_keys, KEY_ACCOUNT_IDS))
        self.key_daily_limit: dict[str, int] = dict(zip(self.api_keys, KEY_DAILY_LIMITS))

        self.current_model = "openrouter/free"
        # Memory is keyed by (channel_id, user_id): every user gets their own
        # memory in every channel, and resetting only touches one such slot.
        self.history: dict[tuple[int, int], list] = defaultdict(list)
        self.max_history = 10

        # guild_id -> set of whitelisted channel ids (mirror of MongoDB).
        self.ai_channels: dict[int, set[int]] = {}
        # Users with a request currently in flight (one request at a time).
        self._active_users: set[int] = set()
        self._bg_tasks: set[asyncio.Task] = set()

        self.last_usage_reset_date = datetime.now(timezone.utc).date()
        self.key_usage_today = load_key_usage()
        self.key_rate_limit_snapshot: dict[str, dict] = {}
        self.session: aiohttp.ClientSession | None = None

    # Retrieves MongoDB instance dynamically from main.py's bot.mongo_client
    @property
    def mongo_client(self):
        return self.bot.mongo_client

    @property
    def channels_collection(self):
        return self.mongo_client[AI_DB_NAME][AI_CHANNELS_COLLECTION]

    async def cog_load(self):
        self.session = aiohttp.ClientSession()
        try:
            await self.mongo_client.admin.command("ping")
            await self.load_ai_channels()
        except Exception as e:
            print(f"AI Cog: MongoDB warmup or AI channel loading failed: {e}")

    async def load_ai_channels(self):
        """Loads every server's whitelisted AI channels from MongoDB into memory."""
        loaded: dict[int, set[int]] = {}
        async for doc in self.channels_collection.find({}):
            try:
                loaded[int(doc["_id"])] = {int(c) for c in doc.get("channels", [])}
            except (KeyError, TypeError, ValueError):
                continue
        self.ai_channels = loaded

    async def _fetch_guild_channels(self, guild_id: int) -> list[int]:
        """Reads one server's whitelist straight from MongoDB (source of truth) and refreshes the cache."""
        doc = await self.channels_collection.find_one({"_id": guild_id})
        ids = [int(c) for c in doc.get("channels", [])] if doc else []
        self.ai_channels[guild_id] = set(ids)
        return ids

    def _is_ai_channel(self, channel) -> bool:
        guild = getattr(channel, "guild", None)
        return bool(guild) and channel.id in self.ai_channels.get(guild.id, ())

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def cog_command_error(self, ctx, error):
        if not await handle_common_error(ctx, error):
            raise error

    def _check_daily_reset(self):
        today = datetime.now(timezone.utc).date()
        if today != self.last_usage_reset_date:
            self.key_usage_today.clear()
            self.last_usage_reset_date = today
            save_key_usage(self.key_usage_today)

    def _record_key_use(self, key: str):
        self._check_daily_reset()
        self.key_usage_today[key] += 1
        save_key_usage(self.key_usage_today)

    def _key_has_quota(self, key: str) -> bool:
        self._check_daily_reset()
        limit = self.key_daily_limit.get(key, DEFAULT_FREE_DAILY_LIMIT)
        return self.key_usage_today[key] < limit

    def _record_rate_limit_headers(self, key: str, headers) -> None:
        limit = headers.get("x-ratelimit-limit")
        remaining = headers.get("x-ratelimit-remaining")
        reset = headers.get("x-ratelimit-reset")
        if limit is None and remaining is None:
            return
        self.key_rate_limit_snapshot[key] = {
            "limit": limit,
            "remaining": remaining,
            "reset": reset,
            "observed_at": datetime.now(timezone.utc),
        }

    # -- path safety -------------------------------------------------------

    def _resolve_path(self, relative_path: str) -> str:
        return os.path.abspath(os.path.join(PROJECT_ROOT, relative_path))

    def _is_safe_path(self, target_path: str) -> bool:
        abs_target = self._resolve_path(target_path)
        root = os.path.abspath(PROJECT_ROOT)
        return abs_target == root or abs_target.startswith(root + os.sep)

    # -- API key pool & rotation ---------------------------------------------

    def _next_worker_key(self) -> str:
        pool = self.worker_keys or [self.primary_key]
        key = pool[self._worker_key_cycle_idx % len(pool)]
        self._worker_key_cycle_idx += 1
        return key

    # -- low-level OpenRouter call --------------------------------------------

    async def _call_openrouter(
        self,
        key: str,
        messages: list,
        tools: list | None,
        max_tokens: int,
        reasoning_effort: str | None = None,
    ) -> dict:
        self._record_key_use(key)
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://your-site-or-repo.com",
            "X-Title": "Utilities Discord Bot Assistant",
        }
        
        # Uses lightweight, high-availability free models as fallbacks
        payload = {
            "models": [
                self.current_model,
                "google/gemini-2.0-flash-lite-001:free",
                "meta-llama/llama-3.1-8b-instruct:free",
            ],
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
        if reasoning_effort:
            payload["reasoning"] = {"effort": reasoning_effort}

        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS, sock_read=REQUEST_TIMEOUT_SECONDS)
        session = self.session if self.session and not self.session.closed else aiohttp.ClientSession()
        close_session = session != self.session

        try:
            async with session.post(self.openrouter_url, headers=headers, json=payload, timeout=timeout) as resp:
                self._record_rate_limit_headers(key, resp.headers)
                if resp.status != 200:
                    error_text = await resp.text()
                    raise RuntimeError(f"OpenRouter error ({resp.status}): {error_text}")

                result = await resp.json()
                if "choices" not in result and "error" in result:
                    err = result["error"]
                    err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    err_code = err.get("code", "") if isinstance(err, dict) else ""
                    raise RuntimeError(f"OpenRouter error ({err_code}): {err_msg}")
                return result
        except asyncio.TimeoutError as e:
            raise RuntimeError(
                f"OpenRouter didn't respond within {REQUEST_TIMEOUT_SECONDS}s "
                "(the model may be overloaded or slow right now — try again)."
            ) from e
        finally:
            if close_session:
                await session.close()

    async def _call_openrouter_with_rotation(
        self,
        messages: list,
        tools: list | None,
        max_tokens: int,
        start_key: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict:
        start_key = start_key or self.api_keys[0]
        start_idx = self.api_keys.index(start_key) if start_key in self.api_keys else 0
        n_keys = len(self.api_keys)

        # Explicit client errors that switching keys can never fix.
        NON_RETRYABLE_CODES = ("400", "404")

        # Try keys with remaining quota first (in round-robin order starting at
        # start_idx), then fall back to quota-exhausted keys as a last resort.
        ordered = [(start_idx + offset) % n_keys for offset in range(n_keys)]
        attempt_order = [i for i in ordered if self._key_has_quota(self.api_keys[i])]
        attempt_order += [i for i in ordered if i not in attempt_order]

        last_error = None
        for attempt_num, key_idx in enumerate(attempt_order):
            key = self.api_keys[key_idx]
            is_last_attempt = attempt_num == len(attempt_order) - 1

            if not self._key_has_quota(key) and not is_last_attempt:
                print(
                    f"[Key Rotation] Key #{key_idx + 1} is at its daily limit "
                    f"({self.key_usage_today[key]}/{self.key_daily_limit.get(key, DEFAULT_FREE_DAILY_LIMIT)}); skipping."
                )
                continue

            try:
                data = await self._call_openrouter(key, messages, tools, max_tokens, reasoning_effort)
                self.primary_key = key
                return data
            except RuntimeError as e:
                last_error = e
                err_str = str(e)
                is_non_retryable = any(code in err_str for code in NON_RETRYABLE_CODES)
                retryable = not is_non_retryable

                if retryable and not is_last_attempt:
                    next_key_idx = attempt_order[attempt_num + 1]
                    print(
                        f"[Key Rotation] Key #{key_idx + 1} failed ({err_str}). "
                        f"Switching to key #{next_key_idx + 1}..."
                    )
                    continue
                raise e

        raise last_error or RuntimeError("No OpenRouter API keys are configured.")

    # -- prompt / tool definitions ------------------------------------------

    def generate_bot_capabilities_prompt(self, is_owner: bool) -> str:
        capabilities = ["Here is a summary of available server commands you can explain to users:\n"]

        for cog_name, cog in self.bot.cogs.items():
            capabilities.append(f"### Cog: {cog_name}")
            for cmd in cog.get_commands():
                if cmd.hidden:
                    continue

                desc = cmd.help or cmd.brief or "No description provided."
                signature = f"{BOT_PREFIX}{cmd.name} {cmd.signature}".strip()
                capabilities.append(f"- `{signature}`: {desc}")
            capabilities.append("")

        if is_owner:
            worker_note = f"{len(self.api_keys)} total API keys loaded"
            capabilities.append(
                "DEVELOPER / OWNER MODE ENABLED:\n"
                "The user messaging you is the owner/developer of this bot. "
                "You have access to tools (`list_files`, `read_file`, `write_file`, `delete_file`, "
                "`load_extension`, `unload_extension`, `reload_extension`, `restart_bot`, "
                "`delegate_subtasks`) which let you inspect, modify, and reload/restart your own "
                f"code when requested. ({worker_note}.)\n\n"
                "STRICT RULES — every tool call is a separate request against a very tight daily "
                "budget shared across all API keys, so wasted round-trips directly cost the user "
                "usable requests for the rest of the day:\n"
                "- NO PREAMBLE, NO MONOLOGUE. Never narrate your plan, restate the request back, "
                "or explain what you're 'going to do next.' Go straight to calling tools or "
                "writing the final summary. No filler text before your first tool call.\n"
                "- MINIMIZE ROUND-TRIPS:\n"
                "  - `list_files` is recursive (default depth 4) — call it ONCE near the project "
                "root to see the whole tree. Never call it again unless the tree actually changed.\n"
                "  - Read each relevant file AT MOST ONCE. Never re-read a file already shown "
                "earlier in this conversation — that content is still in context.\n"
                "  - Before reading anything, decide the full list of files you'll likely need and "
                "read only those. Do not explore speculatively or 'double check' by re-reading.\n"
                "  - As soon as you have enough information, make the edit. Don't keep gathering "
                "more context 'to be sure.'\n"
                "- ACT, DON'T ASK. Make the edit directly with `write_file` rather than describing "
                "what you would change.\n"
                "- MANDATORY: After completing any file edits, deletions, or extension reloads, "
                "send ONE short, plain-text summary of exactly what changed. No other commentary.\n"
                "- Only use `delegate_subtasks` for genuinely independent, unrelated parts of a "
                "request — never as a substitute for reading a file yourself."
            )

        return "\n".join(capabilities)

    def get_owner_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": (
                        "Recursively lists files and subdirectories under a path in the bot codebase, "
                        "relative to the project root, as a flat list of relative paths."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative directory path (default is '.')."},
                            "max_depth": {
                                "type": "integer",
                                "description": "How many directory levels deep to recurse (default 4, max 8)."
                            }
                        }
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Reads the content of a file in the bot project directory.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "Relative file path from project root."}
                        },
                        "required": ["file_path"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Creates or overwrites a source code file in the bot project with the given content.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "Relative file path from project root."},
                            "content": {"type": "string", "description": "Full new code/content for the file."}
                        },
                        "required": ["file_path", "content"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_file",
                    "description": "Deletes a file from the bot project directory.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "Relative file path from project root."}
                        },
                        "required": ["file_path"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "load_extension",
                    "description": "Loads a cog/extension that is not currently loaded.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "extension_name": {"type": "string", "description": "Dotted module path (e.g. 'cogs.afk')."}
                        },
                        "required": ["extension_name"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "unload_extension",
                    "description": "Unloads a currently loaded cog/extension.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "extension_name": {"type": "string", "description": "Dotted module path (e.g. 'cogs.setafk')."}
                        },
                        "required": ["extension_name"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "reload_extension",
                    "description": "Reloads a bot cog/extension dynamically so changes take effect immediately.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "extension_name": {"type": "string", "description": "Dotted module path (e.g. 'cogs.afk')."}
                        },
                        "required": ["extension_name"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "restart_bot",
                    "description": "Restarts the entire bot process.",
                    "parameters": {"type": "object", "properties": {}}
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "delegate_subtasks",
                    "description": (
                        "Splits a task into independent, READ-ONLY investigation subtasks and runs ALL of "
                        "them concurrently (in parallel), each with its own sub-agent and API key."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "tasks": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "instructions": {"type": "string"}
                                    },
                                    "required": ["id", "instructions"]
                                }
                            }
                        },
                        "required": ["tasks"]
                    }
                }
            }
        ]

    def get_worker_tools(self) -> list[dict]:
        read_only_names = {"list_files", "read_file"}
        return [t for t in self.get_owner_tools() if t["function"]["name"] in read_only_names]

    # -- tool execution -------------------------------------------------------

    async def execute_tool_call(self, tool_call: dict) -> str:
        fn_name = tool_call["function"]["name"]
        try:
            args = json.loads(tool_call["function"].get("arguments", "{}"))
        except json.JSONDecodeError as e:
            return f"Error: Invalid JSON arguments passed ({e})."

        if fn_name == "list_files":
            rel_path = args.get("path", ".")
            try:
                max_depth = min(int(args.get("max_depth", 4) or 4), 8)
            except (TypeError, ValueError):
                max_depth = 4
            if not self._is_safe_path(rel_path):
                return "Error: Permission denied (path outside project directory)."
            try:
                full_path = self._resolve_path(rel_path)
                IGNORE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}
                results = []

                def _walk(current: str, depth: int):
                    try:
                        entries = sorted(os.listdir(current))
                    except Exception:
                        return
                    for entry in entries:
                        if entry in IGNORE_DIRS:
                            continue
                        entry_full = os.path.join(current, entry)
                        entry_rel = os.path.relpath(entry_full, full_path)
                        if os.path.isdir(entry_full):
                            results.append(entry_rel + "/")
                            if depth < max_depth:
                                _walk(entry_full, depth + 1)
                        else:
                            results.append(entry_rel)

                _walk(full_path, 1)
                return json.dumps(results, indent=2)
            except Exception as e:
                return f"Error listing directory: {e}"

        elif fn_name == "read_file":
            rel_path = args.get("file_path", "")
            if not self._is_safe_path(rel_path):
                return "Error: Permission denied (path outside project directory)."
            try:
                full_path = self._resolve_path(rel_path)
                with open(full_path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                return f"Error reading file '{rel_path}': {e}"

        elif fn_name == "write_file":
            rel_path = args.get("file_path", "")
            content = args.get("content", "")
            if not self._is_safe_path(rel_path):
                return "Error: Permission denied (path outside project directory)."
            try:
                full_path = self._resolve_path(rel_path)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(content)
                return f"Successfully wrote file: {rel_path}"
            except Exception as e:
                return f"Error writing file '{rel_path}': {e}"

        elif fn_name == "delete_file":
            rel_path = args.get("file_path", "")
            if not self._is_safe_path(rel_path):
                return "Error: Permission denied (path outside project directory)."
            try:
                full_path = self._resolve_path(rel_path)
                os.remove(full_path)
                return f"Successfully deleted file: {rel_path}"
            except Exception as e:
                return f"Error deleting file '{rel_path}': {e}"

        elif fn_name == "load_extension":
            ext = args.get("extension_name", "")
            try:
                await self.bot.load_extension(ext)
                return f"Successfully loaded extension '{ext}'!"
            except Exception as e:
                return f"Error loading extension '{ext}': {e}"

        elif fn_name == "unload_extension":
            ext = args.get("extension_name", "")
            try:
                await self.bot.unload_extension(ext)
                return f"Successfully unloaded extension '{ext}'!"
            except Exception as e:
                return f"Error unloading extension '{ext}': {e}"

        elif fn_name == "reload_extension":
            ext = args.get("extension_name", "")
            try:
                await self.bot.reload_extension(ext)
                return f"Successfully reloaded extension '{ext}'!"
            except Exception as e:
                return f"Error reloading extension '{ext}': {e}"

        elif fn_name == "restart_bot":
            async def _delayed_restart():
                await asyncio.sleep(2)
                os.execv(sys.executable, [sys.executable] + sys.argv)

            asyncio.create_task(_delayed_restart())
            return "Restart scheduled in ~2 seconds."

        elif fn_name == "delegate_subtasks":
            tasks = args.get("tasks", [])
            if not isinstance(tasks, list) or not tasks:
                return "Error: 'tasks' must be a non-empty list."

            valid_tasks = [t for t in tasks if isinstance(t, dict) and t.get("instructions")]
            if not valid_tasks:
                return "Error: no valid tasks provided."

            coros = [
                self._run_subtask(
                    str(t.get("id", f"task{i}")), t["instructions"], self._next_worker_key()
                )
                for i, t in enumerate(valid_tasks)
            ]
            results = await asyncio.gather(*coros)

            combined = "\n\n".join(
                f"### Subtask '{t.get('id', f'task{i}')}' result:\n{res}"
                for i, (t, res) in enumerate(zip(valid_tasks, results))
            )
            return combined

        return f"Unknown tool function: {fn_name}"

    async def _run_subtask(self, task_id: str, instructions: str, key: str) -> str:
        sub_messages = [
            {
                "role": "system",
                "content": (
                    "You are a sub-agent helping a primary assistant investigate part of a task. "
                    "You may only use list_files and read_file. Investigate and reply with a plain-text summary."
                ),
            },
            {"role": "user", "content": instructions},
        ]

        for _ in range(SUBTASK_MAX_ITERATIONS):
            try:
                data = await self._call_openrouter_with_rotation(
                    sub_messages, self.get_worker_tools(), SUBTASK_MAX_TOKENS, start_key=key
                )
            except RuntimeError as e:
                return f"[subtask failed: {e}]"

            if "choices" not in data or not data["choices"]:
                return "[subtask failed: empty response from OpenRouter]"

            choice_message = data["choices"][0]["message"]
            tool_calls = choice_message.get("tool_calls")

            if tool_calls:
                sub_messages.append(choice_message)
                for tool_call in tool_calls:
                    result = await self.execute_tool_call(tool_call)
                    sub_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": result,
                    })
                continue

            content = (choice_message.get("content") or choice_message.get("reasoning") or "").strip()
            return content or "[subtask returned no text]"

        return f"[subtask hit iteration limit ({SUBTASK_MAX_ITERATIONS})]"

    # -- commands -------------------------------------------------------

    @staticmethod
    def _unique_mentioned_channels(ctx: commands.Context) -> list:
        """Channel mentions from the invoking message, de-duplicated, in order, same server only."""
        seen, result = set(), []
        for ch in ctx.message.channel_mentions:
            if ch.id in seen or getattr(ch, "guild", None) != ctx.guild:
                continue
            seen.add(ch.id)
            result.append(ch)
        return result

    async def _require_admin(self, ctx: commands.Context) -> bool:
        """Sends an error and returns False unless this is a server and the author is an administrator."""
        if ctx.guild is None:
            await ctx.reply(embed=err_embed("Server Only", "This command can only be used in a server.", emoji="🚫"))
            return False
        if not ctx.author.guild_permissions.administrator:
            await ctx.reply(embed=err_embed(
                "Missing Permissions",
                "Only **server administrators** can add or remove AI channels.",
                emoji="🚫",
            ))
            return False
        return True

    @commands.group(
        name="ai",
        invoke_without_command=True,
        description="Learn how to use the AI chatbot and its commands.",
    )
    async def ai_group(self, ctx: commands.Context):
        p = ctx.clean_prefix
        embed = make_embed(
            title="🤖 AI Chatbot — How to Use",
            description=(
                "Just type a message in any AI-enabled channel and I'll reply. "
                "Use the commands below to manage it."
            ),
        )
        embed.add_field(
            name="💬 Chatting",
            value=(
                "Send a normal message (no prefix) in an AI channel. "
                "You can attach images too.\n"
                f"-# Messages starting with `{p}` are treated as commands, not AI prompts."
            ),
            inline=False,
        )
        embed.add_field(
            name="⚙️ Managing AI channels *(administrators only)*",
            value=(
                f"`{p}ai-config` — see where the AI is enabled (anyone can view)\n"
                f"`{p}ai add <#channel> [#channel ...]` — enable the AI in channel(s)\n"
                f"`{p}ai remove <#channel> [#channel ...]` — disable the AI in channel(s)\n"
                f"-# Aliases: `{p}ai a` = add, `{p}ai r` = remove"
            ),
            inline=False,
        )
        embed.add_field(
            name="🧠 Memory",
            value=(
                f"`{p}ai memory` — view what I remember from your chat with me in this channel\n"
                f"`{p}ai reset` — clear your memory in this channel\n"
                "-# Memory is separate for every channel **and** every user. "
                "Resetting only clears yours, and only in the channel you run it in."
            ),
            inline=False,
        )
        embed.add_field(
            name="⏳ One request at a time",
            value=(
                "If I'm still answering you, new messages get a ⏳ reaction and are "
                f"deleted after {BUSY_DELETE_AFTER_SECONDS} seconds. Wait for my reply first."
            ),
            inline=False,
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.command(name="ai-config", description="See which channels the AI chatbot is enabled in.")
    async def ai_config(self, ctx: commands.Context):
        if ctx.guild is None:
            return await ctx.reply(embed=err_embed("Server Only", "This command can only be used in a server.", emoji="🚫"))

        try:
            channel_ids = await self._fetch_guild_channels(ctx.guild.id)
        except Exception as e:
            print(f"AI Cog: failed to fetch AI channels for guild {ctx.guild.id}: {e}")
            # Fall back to the in-memory copy so the command still works.
            channel_ids = sorted(self.ai_channels.get(ctx.guild.id, set()))

        p = ctx.clean_prefix
        if channel_ids:
            listing = "\n".join(f"• <#{cid}>" for cid in channel_ids)
            description = f"The AI chatbot is enabled in **{len(channel_ids)}** channel(s):\n\n{listing}"
        else:
            description = "The AI chatbot isn't enabled in any channel in this server yet."

        embed = make_embed(title="🤖 AI Configuration", description=description)
        embed.add_field(
            name="Manage *(administrators only)*",
            value=(
                f"`{p}ai add <#channel> [#channel ...]`\n"
                f"`{p}ai remove <#channel> [#channel ...]`\n"
                f"-# See `{p}ai` for the full guide."
            ),
            inline=False,
        )
        await ctx.reply(embed=embed, mention_author=False)

    @ai_group.command(
        name="add",
        aliases=["a"],
        usage="<#channel> [#channel ...]",
        description="Enable the AI chatbot in one or more channels (administrators only).",
    )
    async def ai_add(self, ctx: commands.Context, *, channels: str = None):
        if not await self._require_admin(ctx):
            return

        targets = self._unique_mentioned_channels(ctx)
        if not targets:
            return await send_usage(ctx, note="Mention at least one channel, e.g. `#general`.")

        try:
            existing = set(await self._fetch_guild_channels(ctx.guild.id))
            to_add = [c for c in targets if c.id not in existing]
            already = [c for c in targets if c.id in existing]

            if to_add:
                await self.channels_collection.update_one(
                    {"_id": ctx.guild.id},
                    {"$addToSet": {"channels": {"$each": [c.id for c in to_add]}}},
                    upsert=True,
                )
                self.ai_channels.setdefault(ctx.guild.id, set()).update(c.id for c in to_add)
        except Exception as e:
            print(f"AI Cog: failed to add AI channels for guild {ctx.guild.id}: {e}")
            return await ctx.reply(embed=err_embed(
                "Database Error", "Couldn't save the AI channels right now. Please try again in a moment."))

        lines = []
        if to_add:
            lines.append("✅ **Added:** " + ", ".join(c.mention for c in to_add))
        if already:
            lines.append("⚠️ **Already added:** " + ", ".join(c.mention for c in already))

        await ctx.reply(embed=make_embed(title="AI Channels Updated", description="\n".join(lines)),
                        mention_author=False)

    @ai_group.command(
        name="remove",
        aliases=["r"],
        usage="<#channel> [#channel ...]",
        description="Disable the AI chatbot in one or more channels (administrators only).",
    )
    async def ai_remove(self, ctx: commands.Context, *, channels: str = None):
        if not await self._require_admin(ctx):
            return

        targets = self._unique_mentioned_channels(ctx)
        if not targets:
            return await send_usage(ctx, note="Mention at least one channel, e.g. `#general`.")

        try:
            existing = set(await self._fetch_guild_channels(ctx.guild.id))
            to_remove = [c for c in targets if c.id in existing]
            not_added = [c for c in targets if c.id not in existing]

            if to_remove:
                await self.channels_collection.update_one(
                    {"_id": ctx.guild.id},
                    {"$pull": {"channels": {"$in": [c.id for c in to_remove]}}},
                )
                self.ai_channels.setdefault(ctx.guild.id, set()).difference_update(c.id for c in to_remove)
        except Exception as e:
            print(f"AI Cog: failed to remove AI channels for guild {ctx.guild.id}: {e}")
            return await ctx.reply(embed=err_embed(
                "Database Error", "Couldn't update the AI channels right now. Please try again in a moment."))

        lines = []
        if to_remove:
            lines.append("✅ **Removed:** " + ", ".join(c.mention for c in to_remove))
        if not_added:
            lines.append("⚠️ **Not added:** " + ", ".join(c.mention for c in not_added))

        await ctx.reply(embed=make_embed(title="AI Channels Updated", description="\n".join(lines)),
                        mention_author=False)

    @ai_group.command(name="reset", help="Clears your AI chatbot memory for this channel.")
    async def reset_memory(self, ctx: commands.Context):
        if not self._is_ai_channel(ctx.channel):
            await ctx.send(embed=err_embed("Wrong Channel", "The AI Chatbot is not active in this channel.", emoji="🚫"))
            return

        key = (ctx.channel.id, ctx.author.id)
        if self.history.get(key):
            self.history[key].clear()
            await ctx.send(embed=ok_embed("Memory Cleared", "Your memory in this channel has been cleared!", emoji="🧹", color=BRAND_COLOR))
        else:
            await ctx.send(embed=info_embed("Memory Empty", "Your memory in this channel is already empty.", emoji="🧠"))

    @ai_group.command(name="memory", help="Displays your AI chatbot memory for this channel.")
    async def show_memory(self, ctx: commands.Context):
        if not self._is_ai_channel(ctx.channel):
            await ctx.send(embed=err_embed("Wrong Channel", "The AI Chatbot is not active in this channel.", emoji="🚫"))
            return

        history = self.history.get((ctx.channel.id, ctx.author.id))
        if not history:
            await ctx.send(embed=info_embed("Memory Empty", "Your memory in this channel is currently empty.", emoji="🧠"))
            return

        def _content_to_str(content) -> str:
            if isinstance(content, list):
                parts = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "image_url":
                        url = (block.get("image_url") or {}).get("url", "")
                        parts.append(f"[image: {url}]")
                return "\n".join(p for p in parts if p) or "[empty]"
            return content or "[empty]"

        lines = []
        for i, msg in enumerate(history, start=1):
            role = msg.get("role", "unknown")
            lines.append(f"**[{i}] {role}:** {_content_to_str(msg.get('content'))}")

        text = "\n\n".join(lines)
        for idx, chunk in enumerate(split_message(text, EMBED_DESCRIPTION_LIMIT)):
            embed = discord.Embed(description=chunk, color=BRAND_COLOR)
            if idx == 0:
                embed.title = f"🧠 Your Memory (this channel) — {len(history)} message(s) stored"
            await ctx.send(embed=embed)

    @commands.command(name="ai-info", help="Owner only: displays model status, and daily usage grouped by OpenRouter account.")
    @is_bot_owner()
    async def ai_info(self, ctx: commands.Context):
        self._check_daily_reset()
        timeout = aiohttp.ClientTimeout(total=15)

        account_groups: dict[str, list[int]] = defaultdict(list)
        for idx, key in enumerate(self.api_keys):
            account_groups[self.key_account_id[key]].append(idx)

        total_accounts = len(account_groups)

        embed = discord.Embed(title="🤖 AI Key Pool & Usage Status", color=BRAND_COLOR)
        embed.add_field(name="Active Model", value=f"`{self.current_model}`", inline=False)
        embed.add_field(
            name="Configured Key Pool",
            value=f"`{len(self.api_keys)}` total API key(s) across `{total_accounts}` account(s)",
            inline=False,
        )

        session = self.session if self.session and not self.session.closed else aiohttp.ClientSession(timeout=timeout)
        close_session = session != self.session

        try:
            # Fetch each key's real quota from OpenRouter first — the daily
            # limit varies per account (e.g. 50 vs 1000 once $10+ has been
            # spent), so totals below are summed from live data rather than
            # assumed/configured defaults.
            key_info: dict[str, dict] = {}
            for key in self.api_keys:
                headers = {"Authorization": f"Bearer {key}"}
                try:
                    async with session.get(self.key_info_url, headers=headers) as resp:
                        if resp.status == 200:
                            res = await resp.json()
                            key_info[key] = res.get("data", {})
                        else:
                            key_info[key] = {"_error": f"HTTP {resp.status}"}
                except Exception as e:
                    key_info[key] = {"_error": str(e)}

            total_usage_live = 0
            total_limit_live = 0
            unavailable_keys = 0
            for key in self.api_keys:
                info = key_info[key]
                fmdr = info.get("free_model_daily_requests") if "_error" not in info else None
                if fmdr and fmdr.get("limit") is not None:
                    total_usage_live += fmdr.get("used") or 0
                    total_limit_live += fmdr.get("limit") or 0
                else:
                    # Fall back to the locally-tracked count/configured limit
                    # only for keys OpenRouter didn't return live data for.
                    unavailable_keys += 1
                    total_usage_live += self.key_usage_today[key]
                    total_limit_live += self.key_daily_limit.get(key, DEFAULT_FREE_DAILY_LIMIT)

            usage_note = (
                f" (`{unavailable_keys}` key(s) unreachable — using local estimate for those)"
                if unavailable_keys else ""
            )
            embed.add_field(
                name="Total Daily Usage",
                value=f"`{total_usage_live} / {total_limit_live}` requests used today across all keys{usage_note}",
                inline=False,
            )

            for acct_id, indices in account_groups.items():
                fmdr = None
                for i in indices:
                    info = key_info[self.api_keys[i]]
                    if "_error" not in info and info.get("free_model_daily_requests"):
                        fmdr = info["free_model_daily_requests"]
                        break

                now_utc = datetime.now(timezone.utc)
                next_reset = datetime.combine(
                    now_utc.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
                )
                reset_ts = int(next_reset.timestamp())

                if fmdr is not None:
                    used = fmdr.get("used")
                    limit = fmdr.get("limit")
                    live_quota_line = (
                        f"**Quota:** `{used} / {limit}` — "
                        f"resets <t:{reset_ts}:R>"
                    )
                else:
                    live_quota_line = (
                        "**Live Quota (from OpenRouter):** unavailable right now"
                    )

                lines = [live_quota_line]

                key_items = []
                for i in indices:
                    key = self.api_keys[i]
                    data = key_info[key]
                    role_tag = " 🟢 Primary" if key == self.primary_key else ""

                    if "_error" in data:
                        key_items.append(f"• Key #{i + 1}{role_tag} — ❌ {data['_error']}")
                    else:
                        label = data.get("label", "Unnamed Key")
                        key_items.append(f"• Key #{i + 1}{role_tag} — `{label}`")

                for i in range(0, len(key_items), 2):
                    lines.append(" | ".join(key_items[i:i + 2]))

                embed.add_field(
                    name=f"📦 Account: {acct_id}  ({len(indices)} key{'s' if len(indices) != 1 else ''})",
                    value="\n".join(lines),
                    inline=False,
                )
        finally:
            if close_session:
                await session.close()

        await ctx.send(embed=embed)

    # -- main listener -------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        if not self._is_ai_channel(message.channel):
            return

        if message.content.startswith(BOT_PREFIX):
            return

        # Rate limit: one in-flight request per user. The check and the add
        # below have no `await` between them, so two rapid messages can't both slip through.
        user_id = message.author.id
        if user_id in self._active_users:
            task = asyncio.create_task(self._reject_busy(message))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
            return
        self._active_users.add(user_id)

        try:
            await self._handle_ai_message(message)
        except Exception:
            tb = traceback.format_exc()
            print(f"Unhandled exception in AIChat.on_message:\n{tb}")
            try:
                await message.reply(
                    embed=err_embed(
                        "Something Went Wrong",
                        "Something went wrong handling that message. Check console logs.",
                    )
                )
            except Exception:
                pass
        finally:
            self._active_users.discard(user_id)

    async def _reject_busy(self, message: discord.Message):
        """React with an hourglass, then delete the too-early message after a few seconds."""
        try:
            await message.add_reaction(BUSY_REACTION)
        except discord.HTTPException:
            pass
        await asyncio.sleep(BUSY_DELETE_AFTER_SECONDS)
        try:
            await message.delete()
        except discord.HTTPException:
            pass  # already deleted, or the bot lacks Manage Messages

    async def _send_result(self, message: discord.Message, text: str):
        """Sends the final answer as #0414c7 embed(s). Only the result is shown."""
        chunks = split_message(text, EMBED_DESCRIPTION_LIMIT)
        for i, chunk in enumerate(chunks):
            embed = discord.Embed(description=chunk, color=BRAND_COLOR)
            if i == 0:
                await message.reply(embed=embed)
            else:
                await message.channel.send(embed=embed)

    async def _handle_ai_message(self, message: discord.Message):
        memory_key = (message.channel.id, message.author.id)

        # Single "Thinking..." embed with a live Discord timestamp; edited to
        # "Thought for N seconds" when done. No reasoning/steps are displayed.
        status = ThinkingStatus(message)
        await status.start()

        async with message.channel.typing():
            is_owner = (await self.bot.is_owner(message.author)) or (message.author.id in OWNER_IDS)
            capabilities_info = self.generate_bot_capabilities_prompt(is_owner)

            system_instruction = (
                "You are Utilities, a dedicated Discord bot for server management and Pokétwo assistance. "
                "Your main capabilities include:\n"
                "- Server moderation and utility operations.\n"
                "- Pokétwo assistance: automatically identifying and naming spawned Pokémon.\n"
                "- Supporting shiny hunting, Pokémon collection tracking, and rare/regional spawn pings.\n\n"
                "Answer user questions accurately, maintain a friendly and helpful tone, and guide users "
                "on how to navigate server features and bot commands.\n\n"
                "USAGE IS LIMITED: this bot runs on a shared daily request budget, so any extended "
                "internal reasoning/thinking before your answer costs real quota. For simple, "
                "direct questions, answer immediately with no preliminary reasoning at all. Only "
                "think step-by-step first for genuinely complex, ambiguous, or multi-part requests "
                "(e.g. multi-file code changes) where it actually changes the quality of the answer.\n\n"
                f"{capabilities_info}"
            )

            image_urls = [
                att.url for att in message.attachments
                if (att.content_type and att.content_type.startswith("image/"))
                or att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
            ]

            if image_urls:
                user_content = []
                text_prompt = message.content.strip() if message.content else "Please look at the attached image(s)."
                user_content.append({"type": "text", "text": text_prompt})

                for url in image_urls:
                    user_content.append({"type": "image_url", "image_url": {"url": url}})

                user_message_dict = {"role": "user", "content": user_content}
            else:
                user_message_dict = {"role": "user", "content": message.content}

            self.history[memory_key].append(user_message_dict)
            self.history[memory_key] = self.history[memory_key][-self.max_history:]

            current_payload_messages = [{"role": "system", "content": system_instruction}] + self.history[memory_key]

            reply_sent = False
            mutation_happened = False
            MUTATING_TOOLS = {
                "write_file", "delete_file",
                "load_extension", "unload_extension", "reload_extension",
                "restart_bot",
            }

            for iteration in range(MAX_TOOL_ITERATIONS):
                tools_for_call = self.get_owner_tools() if is_owner else None
                max_tokens_for_call = TOOL_CALL_MAX_TOKENS if is_owner else CHAT_MAX_TOKENS

                try:
                    data = await self._call_openrouter_with_rotation(
                        current_payload_messages,
                        tools_for_call,
                        max_tokens_for_call,
                        start_key=self.primary_key,
                        reasoning_effort=None,
                    )
                except RuntimeError as e:
                    print(f"OpenRouter request failed: {e}")
                    await status.finish(success=False)
                    await message.reply(embed=err_embed("Request Failed", str(e)))
                    reply_sent = True
                    break

                if "choices" not in data or not data["choices"]:
                    print(f"OpenRouter returned no choices: {data}")
                    await status.finish(success=False)
                    await message.reply(embed=err_embed("Empty Response", "Sorry, I got an empty response from the AI backend."))
                    reply_sent = True
                    break

                choice = data["choices"][0]
                choice_message = choice["message"]
                finish_reason = choice.get("finish_reason")

                # Models may embed reasoning inline in `content` via <think> tags,
                # or in a dedicated `reasoning`/`thinking` field. It is stripped
                # and discarded here so only the real answer is ever shown.
                raw_content = choice_message.get("content") or ""
                _discarded_thinking, cleaned_content = extract_inline_thinking(raw_content)
                choice_message["content"] = cleaned_content

                if finish_reason == "length":
                    await status.finish(success=False)
                    if choice_message.get("tool_calls"):
                        await message.reply(embed=warn_embed(
                            "Response Cut Off",
                            "The AI's response was cut off before it finished. Try again with a narrower request."
                        ))
                    else:
                        await message.reply(embed=warn_embed(
                            "Out of Tokens",
                            "The AI ran out of tokens before it could act on your request."
                        ))
                    reply_sent = True
                    break

                tool_calls = choice_message.get("tool_calls")
                if tool_calls and is_owner:
                    trimmed_message = {
                        "role": choice_message.get("role", "assistant"),
                        "content": choice_message.get("content") or "",
                        "tool_calls": tool_calls,
                    }
                    current_payload_messages.append(trimmed_message)
                    for tool_call in tool_calls:
                        fn_name = tool_call["function"]["name"]

                        if fn_name in MUTATING_TOOLS:
                            mutation_happened = True

                        tool_result = await self.execute_tool_call(tool_call)

                        current_payload_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "content": tool_result
                        })
                    continue

                # `content` has already had any inline <think> tags stripped
                # above, so this is purely the model's actual answer.
                reply_text = choice_message.get("content") or ""

                if mutation_happened:
                    current_payload_messages.append({
                        "role": "user",
                        "content": (
                            "You modified the bot's files/extensions during this task. "
                            "Respond with ONLY a concise plain-text summary of exactly what changed and why."
                        )
                    })
                    try:
                        summary_data = await self._call_openrouter_with_rotation(
                            current_payload_messages, None, CHAT_MAX_TOKENS
                        )
                        if "choices" in summary_data and summary_data["choices"]:
                            summary_msg = summary_data["choices"][0]["message"]
                            _summary_thinking, summary_cleaned = extract_inline_thinking(
                                summary_msg.get("content") or ""
                            )
                            forced_summary = summary_cleaned.strip()
                            if forced_summary:
                                reply_text = forced_summary
                    except Exception as e:
                        print(f"Failed to retrieve summary: {e}")

                if reply_text.strip():
                    self.history[memory_key].append({"role": "assistant", "content": reply_text})
                    await status.finish(success=True)
                    await self._send_result(message, reply_text)
                else:
                    await status.finish(success=False)
                    await message.reply(embed=warn_embed(
                        "No Response",
                        "Action completed, but no text response was returned."
                    ))

                reply_sent = True
                break
            else:
                await status.finish(success=False)
                await message.reply(embed=warn_embed(
                    "Iteration Limit",
                    "Task hit tool call iteration limits."
                ))
                reply_sent = True

            if not reply_sent:
                await status.finish(success=False)
                await message.reply(embed=err_embed(
                    "Unexpected Error",
                    "Something unexpected happened and no response was generated."
                ))


async def setup(bot):
    await bot.add_cog(AIChat(bot))