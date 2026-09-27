import asyncio
import json
import os
import re
import sys
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord.ext import commands

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
AI_CHATBOT_CHANNELID = int(config["AI_CHATBOT_CHANNELID"])
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
    """Loads daily key usage from disk if valid for today (UTC)."""
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
    """Saves daily key usage to disk."""
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
    """Splits a long message string into chunks under Discord's character limit."""
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
    """Pulls <think>/<thinking>/<reasoning> tagged spans out of `content`.

    Returns a tuple of (thinking_text, remaining_text), both stripped.
    thinking_text is the concatenation of every tagged span found (empty
    string if none). remaining_text is `content` with those spans removed.
    """
    if not content:
        return "", ""

    found = [m.group(2).strip() for m in _THINK_TAG_RE.finditer(content) if m.group(2).strip()]
    cleaned = _THINK_TAG_RE.sub("", content).strip()
    return "\n\n".join(found), cleaned


DISCORD_MESSAGE_LIMIT = 2000


class StatusUpdater:
    """Manages a single live progress message showing bot execution steps."""

    def __init__(self, first_message: discord.Message):
        self.channel = first_message.channel
        self.messages: list[discord.Message] = [first_message]
        self.current_text = first_message.content or ""

    async def append(self, text: str):
        """Appends subtext lines to the Discord progress message."""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()] or [text.strip()]
        for line in lines:
            if not line:
                continue
            subtext_line = f"-# {line}"
            candidate = f"{self.current_text}\n{subtext_line}" if self.current_text else subtext_line

            if len(candidate) <= DISCORD_MESSAGE_LIMIT:
                self.current_text = candidate
                try:
                    await self.messages[-1].edit(content=self.current_text)
                except discord.HTTPException:
                    pass
            else:
                try:
                    new_msg = await self.channel.send(subtext_line)
                    self.messages.append(new_msg)
                    self.current_text = subtext_line
                except discord.HTTPException:
                    pass

    async def append_block(self, text: str):
        """Appends a preformatted block (e.g. a fenced ```code``` block) verbatim,
        without the per-line '-# ' subtext prefix or line-splitting used by
        `append`. This keeps multi-line fenced content (like a collapsible /
        downloadable thinking transcript) intact as one continuous block.
        Oversized blocks are automatically split into multiple fenced chunks.
        """
        if len(text) > DISCORD_MESSAGE_LIMIT:
            inner_limit = DISCORD_MESSAGE_LIMIT - 8  # leave room for ``` fences
            raw = text.strip("`\n")
            for i in range(0, len(raw), inner_limit):
                await self.append_block(f"```\n{raw[i:i + inner_limit]}\n```")
            return

        candidate = f"{self.current_text}\n{text}" if self.current_text else text
        if len(candidate) <= DISCORD_MESSAGE_LIMIT:
            self.current_text = candidate
            try:
                await self.messages[-1].edit(content=self.current_text)
            except discord.HTTPException:
                pass
        else:
            try:
                new_msg = await self.channel.send(text)
                self.messages.append(new_msg)
                self.current_text = text
            except discord.HTTPException:
                pass


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
        self.history = defaultdict(list)
        self.max_history = 10
        self._channel_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

        self.last_usage_reset_date = datetime.now(timezone.utc).date()
        self.key_usage_today = load_key_usage()
        self.key_rate_limit_snapshot: dict[str, dict] = {}
        self.session: aiohttp.ClientSession | None = None

    async def cog_load(self):
        """Initializes a shared HTTP session when the cog is loaded."""
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        """Closes the shared HTTP session when the cog is unloaded."""
        if self.session and not self.session.closed:
            await self.session.close()

    def _check_daily_reset(self):
        """Resets key usage counts at midnight UTC."""
        today = datetime.now(timezone.utc).date()
        if today != self.last_usage_reset_date:
            self.key_usage_today.clear()
            self.last_usage_reset_date = today
            save_key_usage(self.key_usage_today)

    def _record_key_use(self, key: str):
        """Increments daily request count for a specific key."""
        self._check_daily_reset()
        self.key_usage_today[key] += 1
        save_key_usage(self.key_usage_today)

    def _key_has_quota(self, key: str) -> bool:
        """Returns False once a key has hit its configured daily request limit."""
        self._check_daily_reset()
        limit = self.key_daily_limit.get(key, DEFAULT_FREE_DAILY_LIMIT)
        return self.key_usage_today[key] < limit

    def _record_rate_limit_headers(self, key: str, headers) -> None:
        """Stores OpenRouter x-ratelimit headers."""
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
        """Resolves a relative path against PROJECT_ROOT."""
        return os.path.abspath(os.path.join(PROJECT_ROOT, relative_path))

    def _is_safe_path(self, target_path: str) -> bool:
        """Security check ensuring execution remains inside PROJECT_ROOT."""
        abs_target = self._resolve_path(target_path)
        root = os.path.abspath(PROJECT_ROOT)
        return abs_target == root or abs_target.startswith(root + os.sep)

    # -- API key pool & rotation ---------------------------------------------

    def _next_worker_key(self) -> str:
        """Round-robins worker keys for parallel tasks."""
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
        """Sends one chat-completions request using optimized free fallback models."""
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
        """Rotates through every configured key before giving up.

        Keys that have already hit their configured daily limit are tried last
        (as a safety-net fallback only, in case local usage tracking drifted
        from OpenRouter's real count), so a key being "used up" simply causes
        the bot to move on to the next key instead of erroring out.
        """
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
        """Inspects cogs and commands to build system context."""
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

    @commands.command(name="reset", help="Clears the AI chatbot memory for this channel.")
    async def reset_memory(self, ctx: commands.Context):
        if ctx.channel.id != AI_CHATBOT_CHANNELID:
            await ctx.send("The AI Chatbot is not active in this channel.")
            return

        if ctx.channel.id in self.history:
            self.history[ctx.channel.id].clear()
            await ctx.send("🧹 Conversation memory has been cleared!")
        else:
            await ctx.send("Memory is already empty.")

    @commands.command(name="memory", help="Displays the AI chatbot's current conversation memory for this channel.")
    async def show_memory(self, ctx: commands.Context):
        if ctx.channel.id != AI_CHATBOT_CHANNELID:
            await ctx.send("The AI Chatbot is not active in this channel.")
            return

        history = self.history.get(ctx.channel.id)
        if not history:
            await ctx.send("🧠 Memory is currently empty.")
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

        lines = [f"🧠 **Current memory for this channel** — `{len(history)}` message(s) stored:\n"]
        for i, msg in enumerate(history, start=1):
            role = msg.get("role", "unknown")
            lines.append(f"**[{i}] {role}:** {_content_to_str(msg.get('content'))}")

        text = "\n\n".join(lines)
        for chunk in split_message(text):
            await ctx.send(chunk)

    @commands.command(name="ai-info", help="Displays model status, and daily usage grouped by OpenRouter account.")
    @commands.is_owner()
    async def ai_info(self, ctx: commands.Context):
        self._check_daily_reset()
        timeout = aiohttp.ClientTimeout(total=15)

        account_groups: dict[str, list[int]] = defaultdict(list)
        for idx, key in enumerate(self.api_keys):
            account_groups[self.key_account_id[key]].append(idx)

        total_accounts = len(account_groups)

        embed = discord.Embed(title="🤖 AI Key Pool & Usage Status", color=discord.Color.blue())
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

    @ai_info.error
    async def ai_info_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.NotOwner):
            await ctx.send("❌ Only the bot owner can use this command.")

    # -- main listener -------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.channel.id != AI_CHATBOT_CHANNELID:
            return

        if message.content.startswith(BOT_PREFIX):
            return

        channel_id = message.channel.id
        lock = self._channel_locks[channel_id]

        async with lock:
            try:
                await self._handle_ai_message(message)
            except Exception:
                tb = traceback.format_exc()
                print(f"Unhandled exception in AIChat.on_message:\n{tb}")
                try:
                    await message.reply(
                        "❌ Something went wrong handling that message. Check console logs."
                    )
                except Exception:
                    pass

    async def _handle_ai_message(self, message: discord.Message):
        channel_id = message.channel.id

        status_msg = await message.reply("🔄 Requesting response from OpenRouter...")
        status = StatusUpdater(status_msg)

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

            self.history[channel_id].append(user_message_dict)
            self.history[channel_id] = self.history[channel_id][-self.max_history:]

            current_payload_messages = [{"role": "system", "content": system_instruction}] + self.history[channel_id]

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

                if iteration == 0:
                    await status.append("Requesting response from OpenRouter...")
                else:
                    await status.append(f"Continuing (step {iteration + 1})...")

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
                    await status.append(f"Failed: {e}")
                    await message.reply(f"❌ {e}")
                    reply_sent = True
                    break

                if "choices" not in data or not data["choices"]:
                    print(f"OpenRouter returned no choices: {data}")
                    await status.append("Failed: empty response from OpenRouter.")
                    await message.reply("Sorry, I got an empty response from the AI backend.")
                    reply_sent = True
                    break

                choice = data["choices"][0]
                choice_message = choice["message"]
                finish_reason = choice.get("finish_reason")

                # Some models put reasoning in a dedicated field; others embed
                # it inline in `content` via <think>/<thinking> tags. Pull
                # both out so `content` only ever holds the real output.
                raw_content = choice_message.get("content") or ""
                inline_thinking, cleaned_content = extract_inline_thinking(raw_content)
                choice_message["content"] = cleaned_content

                field_thinking = (choice_message.get("reasoning") or choice_message.get("thinking") or "").strip()
                thinking_text = "\n\n".join(t for t in (field_thinking, inline_thinking) if t)

                if thinking_text:
                    await status.append("Thinking...")
                    await status.append_block(f"```\n{thinking_text}\n```")

                if finish_reason == "length":
                    if choice_message.get("tool_calls"):
                        await status.append("Cut off before finishing.")
                        await message.reply(
                            "⚠️ The AI's response was cut off before it finished. Try again with a narrower request."
                        )
                    else:
                        await status.append("Ran out of tokens.")
                        await message.reply(
                            "⚠️ The AI ran out of tokens before it could act on your request."
                        )
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

                        if fn_name == "delegate_subtasks":
                            try:
                                n_tasks = len(json.loads(tool_call["function"].get("arguments", "{}")).get("tasks", []))
                            except Exception:
                                n_tasks = "?"
                            await status.append(f"Delegating {n_tasks} sub-agent(s) to run in parallel...")
                        else:
                            await status.append(f"Calling tool `{fn_name}`...")

                        if fn_name in MUTATING_TOOLS:
                            mutation_happened = True

                        tool_result = await self.execute_tool_call(tool_call)

                        if fn_name == "delegate_subtasks":
                            await status.append("Sub-agents finished.")
                        else:
                            await status.append(f"`{fn_name}` finished.")

                        current_payload_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "content": tool_result
                        })
                    continue

                # `content` has already had any inline <think> tags stripped
                # above, so this is purely the model's actual answer, never
                # its reasoning. If it's empty, the "no text response" path
                # below handles it rather than dumping raw thoughts on the user.
                reply_text = choice_message.get("content") or ""

                if mutation_happened:
                    await status.append("Generating summary of changes...")
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
                            summary_thinking, summary_cleaned = extract_inline_thinking(
                                summary_msg.get("content") or ""
                            )
                            summary_field_thinking = (
                                summary_msg.get("reasoning") or summary_msg.get("thinking") or ""
                            ).strip()
                            summary_all_thinking = "\n\n".join(
                                t for t in (summary_field_thinking, summary_thinking) if t
                            )
                            if summary_all_thinking:
                                await status.append("Thinking...")
                                await status.append_block(f"```\n{summary_all_thinking}\n```")

                            forced_summary = summary_cleaned.strip()
                            if forced_summary:
                                reply_text = forced_summary
                    except Exception as e:
                        print(f"Failed to retrieve summary: {e}")

                if reply_text.strip():
                    self.history[channel_id].append({"role": "assistant", "content": reply_text})
                    await status.append("Done.")
                    chunks = split_message(reply_text)
                    for i, chunk in enumerate(chunks):
                        if i == 0:
                            await message.reply(chunk)
                        else:
                            await message.channel.send(chunk)
                else:
                    await status.append("Done, but no text summary was generated.")
                    await message.reply("⚠️ Action completed, but no text response was returned.")

                reply_sent = True
                break
            else:
                await status.append("Hit tool call iteration limit.")
                await message.reply("⚠️ Task hit tool call iteration limits.")
                reply_sent = True

            if not reply_sent:
                await message.reply("❌ Something unexpected happened and no response was generated.")


async def setup(bot):
    await bot.add_cog(AIChat(bot))