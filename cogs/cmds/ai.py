import asyncio
import json
import os
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
# OpenRouter's /api/v1/key endpoint does not expose any cross-key "account id" —
# label/usage/limit are all per-key, not per-account — so keys belonging to the
# same OpenRouter account can't be auto-detected reliably. Group them explicitly:
#
#   "AI_CHATBOT_KEY_ACCOUNTS": ["accountA", "accountA", "accountB", "accountB", "accountB"]
#
# One entry per key in AI_CHATBOT_KEYS, same order/length. Keys sharing the same
# string are treated as being on the same OpenRouter account (and therefore
# sharing a single 50/day free-tier limit). If omitted, every key is treated as
# its own separate account.
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

MAX_TOOL_ITERATIONS = 32       # limit for multi-step file edits
TOOL_CALL_MAX_TOKENS = 2048    # budget for reasoning/tools
CHAT_MAX_TOKENS = 1024
REQUEST_TIMEOUT_SECONDS = 180  # timeout for OpenRouter responses

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


class AIChat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.openrouter_url = "https://openrouter.ai/api/v1/chat/completions"
        self.key_info_url = "https://openrouter.ai/api/v1/key"

        self.api_keys = OPENROUTER_API_KEYS
        self.primary_key = self.api_keys[0]
        self.worker_keys = self.api_keys[1:] if len(self.api_keys) > 1 else self.api_keys
        self._worker_key_cycle_idx = 0

        # Maps each raw key string -> the account id it belongs to (see
        # AI_CHATBOT_KEY_ACCOUNTS above). Used to group usage in `ai-info`.
        self.key_account_id: dict[str, str] = dict(zip(self.api_keys, KEY_ACCOUNT_IDS))

        self.current_model = "openrouter/free"
        self.history = defaultdict(list)
        self.max_history = 10
        self._channel_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

        # Persistent daily usage tracking per key (LOCAL ESTIMATE ONLY — see
        # key_rate_limit_snapshot below for the authoritative server-side number)
        self.last_usage_reset_date = datetime.now(timezone.utc).date()
        self.key_usage_today = load_key_usage()

        # Most recent x-ratelimit-* headers OpenRouter returned for each key,
        # IF that key has ever hit an error response. OpenRouter only sends
        # these headers on errors (never on a normal 200 OK), so this is NOT
        # a live quota snapshot — it's just "state at the last time this key
        # got rate-limited." ai_info() uses the live `free_model_daily_requests`
        # field from GET /api/v1/key instead; this dict is only a fallback for
        # when that live lookup fails. Not persisted to disk.
        self.key_rate_limit_snapshot: dict[str, dict] = {}

    def _check_daily_reset(self):
        """Resets key usage counts at midnight UTC."""
        today = datetime.now(timezone.utc).date()
        if today != self.last_usage_reset_date:
            self.key_usage_today.clear()
            self.last_usage_reset_date = today
            save_key_usage(self.key_usage_today)

    def _record_key_use(self, key: str):
        """Increments the daily request count for the specified key and persists it.
        NOTE: this is only a local estimate of requests sent by THIS bot process —
        see _record_rate_limit_headers for OpenRouter's actual server-side count."""
        self._check_daily_reset()
        self.key_usage_today[key] += 1
        save_key_usage(self.key_usage_today)

    def _record_rate_limit_headers(self, key: str, headers) -> None:
        """Stores OpenRouter's own x-ratelimit-* response headers for this key,
        when present.

        NOTE: per OpenRouter's docs, these headers are ONLY ever included on
        error responses (e.g. a 429) — a normal 200 OK never carries them. So
        this snapshot only updates at the moment a key gets rate-limited, and
        then stays frozen (stale) until another 429 happens; it can't be used
        as a live "quota remaining" indicator. For that, ai_info() below uses
        the `free_model_daily_requests` field from GET /api/v1/key instead,
        which is fresh on every call regardless of errors. This snapshot is
        kept only as a "last time this key was rate-limited" fallback."""
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
        """Resolves a user/model-supplied relative path against PROJECT_ROOT."""
        return os.path.abspath(os.path.join(PROJECT_ROOT, relative_path))

    def _is_safe_path(self, target_path: str) -> bool:
        """Security check to ensure code modifications stay inside the project directory."""
        abs_target = self._resolve_path(target_path)
        root = os.path.abspath(PROJECT_ROOT)
        return abs_target == root or abs_target.startswith(root + os.sep)

    # -- API key pool & rotation ---------------------------------------------

    def _next_worker_key(self) -> str:
        """Round-robins across configured worker keys for parallel subtasks."""
        pool = self.worker_keys or [self.primary_key]
        key = pool[self._worker_key_cycle_idx % len(pool)]
        self._worker_key_cycle_idx += 1
        return key

    # -- low-level OpenRouter call --------------------------------------------

    async def _call_openrouter(
        self, key: str, messages: list, tools: list | None, max_tokens: int
    ) -> dict:
        """Sends one chat-completions request using a specific key."""
        self._record_key_use(key)
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://your-site-or-repo.com",
            "X-Title": "Utilities Discord Bot Assistant",
        }
        payload = {
            "model": self.current_model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools

        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.openrouter_url, headers=headers, json=payload) as resp:
                    self._record_rate_limit_headers(key, resp.headers)
                    if resp.status != 200:
                        error_text = await resp.text()
                        raise RuntimeError(f"OpenRouter error ({resp.status}): {error_text}")
                    return await resp.json()
        except asyncio.TimeoutError as e:
            raise RuntimeError(
                f"OpenRouter didn't respond within {REQUEST_TIMEOUT_SECONDS}s "
                "(the model may be overloaded or slow right now — try again)."
            ) from e

    async def _call_openrouter_with_rotation(
        self, messages: list, tools: list | None, max_tokens: int
    ) -> dict:
        """Sends chat request. If a 429 Rate Limit error occurs, automatically rotates
        through all configured API keys with backoff delay until a working key is found."""
        start_key = self.primary_key
        start_idx = self.api_keys.index(start_key) if start_key in self.api_keys else 0
        n_keys = len(self.api_keys)

        last_error = None
        for offset in range(n_keys):
            key_idx = (start_idx + offset) % n_keys
            key = self.api_keys[key_idx]

            try:
                data = await self._call_openrouter(key, messages, tools, max_tokens)
                self.primary_key = key
                return data
            except RuntimeError as e:
                last_error = e
                if "429" in str(e) and n_keys > 1:
                    next_key_num = ((key_idx + 1) % n_keys) + 1
                    backoff_sec = 2 * (offset + 1)
                    print(
                        f"[Key Rotation] Key #{key_idx + 1} hit 429 rate limit. "
                        f"Waiting {backoff_sec}s before rotating to key #{next_key_num}..."
                    )
                    await asyncio.sleep(backoff_sec)
                    continue
                raise e

        raise last_error

    # -- prompt / tool definitions ------------------------------------------

    def generate_bot_capabilities_prompt(self, is_owner: bool) -> str:
        """Inspects all cogs and commands dynamically to build system context."""
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
                "Guidelines for self-editing tasks:\n"
                "- All paths are relative to the project root (where config.json lives).\n"
                "- MANDATORY: After completing all file edits, deletions, or extension reloads, "
                "you MUST send the user a plain text summary detailing what changes you made."
            )

        return "\n".join(capabilities)

    def get_owner_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": "Lists files and subdirectories in the bot codebase, relative to the project root.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative directory path (default is '.')."}
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
                    "description": "Runs one or more independent, READ-ONLY investigation subtasks in parallel.",
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
            if not self._is_safe_path(rel_path):
                return "Error: Permission denied (path outside project directory)."
            try:
                full_path = self._resolve_path(rel_path)
                files = os.listdir(full_path)
                return json.dumps(files, indent=2)
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
                    sub_messages, self.get_worker_tools(), SUBTASK_MAX_TOKENS
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

    @commands.command(name="ai-info", help="Displays model status, and daily usage grouped by OpenRouter account.")
    @commands.is_owner()
    async def ai_info(self, ctx: commands.Context):
        """Fetches OpenRouter key metadata, groups keys that belong to the same
        account (per AI_CHATBOT_KEY_ACCOUNTS in config.json), and shows each
        account's shared 50/day free-tier usage plus a per-key breakdown."""
        self._check_daily_reset()
        timeout = aiohttp.ClientTimeout(total=15)

        # Group key indices by account id, preserving key order within a group.
        account_groups: dict[str, list[int]] = defaultdict(list)
        for idx, key in enumerate(self.api_keys):
            account_groups[self.key_account_id[key]].append(idx)

        embed = discord.Embed(title="🤖 AI Key Pool & Usage Status", color=discord.Color.blue())
        embed.add_field(name="Active Model", value=f"`{self.current_model}`", inline=False)
        embed.add_field(
            name="Configured Key Pool",
            value=f"`{len(self.api_keys)}` total API key(s) across `{len(account_groups)}` account(s)",
            inline=False,
        )

        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Fetch per-key info once up front so we can compute account totals.
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

            for acct_id, indices in account_groups.items():
                # Live, always-current free-model daily quota for this account,
                # straight from GET /api/v1/key's `free_model_daily_requests`
                # field (fetched fresh above, just now, as part of this command).
                # This is the correct source for the shared 50(or 1000)/day cap:
                # unlike x-ratelimit-* headers (which OpenRouter only sends on
                # error responses, never on a normal 200 OK — so a header-based
                # snapshot goes stale the moment nothing has errored recently),
                # this field is populated on every single call, success or not.
                # Since the cap is shared per account, every key on the account
                # should report the same numbers here; take the first that
                # answered successfully.
                fmdr = None
                for i in indices:
                    info = key_info[self.api_keys[i]]
                    if "_error" not in info and info.get("free_model_daily_requests"):
                        fmdr = info["free_model_daily_requests"]
                        break

                # The free-model daily cap always resets at the next UTC
                # midnight; render that as a Discord relative timestamp
                # (renders client-side as "in X hours", localized automatically).
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
                        "**Live Quota (from OpenRouter):** unavailable right now "
                        "(key lookup failed for every key on this account — see per-key errors below)"
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
                        key_requests = self.key_usage_today[key]
                        key_items.append(f"• Key #{i + 1}{role_tag} — `{label}` — `{key_requests}` reqs today")

                for i in range(0, len(key_items), 2):
                    lines.append(" | ".join(key_items[i:i + 2]))

                embed.add_field(
                    name=f"📦 Account: {acct_id}  ({len(indices)} key{'s' if len(indices) != 1 else ''})",
                    value="\n".join(lines),
                    inline=False,
                )

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

            for iteration in range(MAX_TOOL_ITERATIONS):
                tools_for_call = self.get_owner_tools() if is_owner else None
                max_tokens_for_call = TOOL_CALL_MAX_TOKENS if is_owner else CHAT_MAX_TOKENS

                try:
                    data = await self._call_openrouter_with_rotation(
                        current_payload_messages, tools_for_call, max_tokens_for_call
                    )
                except RuntimeError as e:
                    print(f"OpenRouter request failed: {e}")
                    await message.reply(f"❌ {e}")
                    reply_sent = True
                    break

                if "choices" not in data or not data["choices"]:
                    print(f"OpenRouter returned no choices: {data}")
                    await message.reply("Sorry, I got an empty response from the AI backend.")
                    reply_sent = True
                    break

                choice = data["choices"][0]
                choice_message = choice["message"]
                finish_reason = choice.get("finish_reason")

                if finish_reason == "length":
                    if choice_message.get("tool_calls"):
                        await message.reply(
                            "⚠️ The AI's response was cut off before it finished. Try again with a narrower request."
                        )
                    else:
                        await message.reply(
                            "⚠️ The AI ran out of tokens before it could act on your request."
                        )
                    reply_sent = True
                    break

                tool_calls = choice_message.get("tool_calls")
                if tool_calls and is_owner:
                    current_payload_messages.append(choice_message)
                    for tool_call in tool_calls:
                        tool_result = await self.execute_tool_call(tool_call)
                        current_payload_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "content": tool_result
                        })
                    continue

                reply_text = (
                    choice_message.get("content")
                    or choice_message.get("reasoning")
                    or choice_message.get("thinking")
                    or ""
                )

                if not reply_text.strip() and current_payload_messages and current_payload_messages[-1].get("role") == "tool":
                    current_payload_messages.append({
                        "role": "user",
                        "content": "All tool actions are completed. Please provide a brief plain text summary of what changes were made."
                    })
                    try:
                        summary_data = await self._call_openrouter_with_rotation(
                            current_payload_messages, None, CHAT_MAX_TOKENS
                        )
                        if "choices" in summary_data and summary_data["choices"]:
                            summary_msg = summary_data["choices"][0]["message"]
                            reply_text = (
                                summary_msg.get("content")
                                or summary_msg.get("reasoning")
                                or summary_msg.get("thinking")
                                or ""
                            )
                    except Exception as e:
                        print(f"Failed to retrieve fallback summary: {e}")

                if reply_text.strip():
                    self.history[channel_id].append({"role": "assistant", "content": reply_text})

                    chunks = split_message(reply_text)
                    for i, chunk in enumerate(chunks):
                        if i == 0:
                            await message.reply(chunk)
                        else:
                            await message.channel.send(chunk)
                else:
                    await message.reply("⚠️ Action completed, but no text summary was generated by the AI.")

                reply_sent = True
                break
            else:
                print(f"AIChat: hit MAX_TOOL_ITERATIONS ({MAX_TOOL_ITERATIONS}) in channel {channel_id}.")
                await message.reply("⚠️ Task hit tool call iteration limits.")
                reply_sent = True

            if not reply_sent:
                await message.reply("❌ Something unexpected happened and no response was generated.")


async def setup(bot):
    await bot.add_cog(AIChat(bot))