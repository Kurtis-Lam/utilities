import json
import os
from collections import defaultdict
import aiohttp
import discord
from discord.ext import commands

# Path to config.json (adjust depth if needed)
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config.json")

# Fallback to root directory if path above doesn't exist
if not os.path.exists(CONFIG_PATH):
    CONFIG_PATH = "config.json"

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

# Load values matching your config.json keys
BOT_PREFIX = config.get("PREFIX", "!")
AI_CHANNEL_ID = int(config["AI_CHATBOT_CHANNELID"])
OPENROUTER_API_KEY = config["AI_CHATBOT"]
OWNER_IDS = set(config.get("OWNER_IDS", []))  # Optional list of user IDs in config.json


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
        self.api_key = OPENROUTER_API_KEY
        self.current_model = "deepseek/deepseek-v4-flash-vision-exp"
        # Memory store: channel_id -> list of message dicts
        self.history = defaultdict(list)
        self.max_history = 10  # Store up to 10 back-and-forth messages

    def _is_safe_path(self, target_path: str) -> bool:
        """Security check to ensure code modifications stay inside the project directory."""
        root_dir = os.path.abspath(os.getcwd())
        abs_target = os.path.abspath(target_path)
        return abs_target.startswith(root_dir)

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
            capabilities.append(
                "DEVELOPER / OWNER MODE ENABLED:\n"
                "The user messaging you is the owner/developer of this bot. "
                "You have access to tools (`read_file`, `write_file`, `list_files`, `reload_extension`) "
                "which allow you to inspect, modify, and reload your own code when requested."
            )

        return "\n".join(capabilities)

    def get_owner_tools(self) -> list[dict]:
        """Tool definitions passed to OpenRouter for code self-editing."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": "Lists files and subdirectories in the bot codebase.",
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
                            "file_path": {"type": "string", "description": "Relative file path."}
                        },
                        "required": ["file_path"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Modifies or creates a source code file in the bot project.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "Relative file path."},
                            "content": {"type": "string", "description": "Full new code/content for the file."}
                        },
                        "required": ["file_path", "content"]
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
                            "extension_name": {"type": "string", "description": "Cog module path (e.g. 'cogs.ai_chat')."}
                        },
                        "required": ["extension_name"]
                    }
                }
            }
        ]

    async def execute_tool_call(self, tool_call: dict) -> str:
        """Executes the tool call requested by OpenRouter and returns the result string."""
        fn_name = tool_call["function"]["name"]
        try:
            args = json.loads(tool_call["function"].get("arguments", "{}"))
        except json.JSONDecodeError:
            return "Error: Invalid JSON arguments passed."

        if fn_name == "list_files":
            path = args.get("path", ".")
            if not self._is_safe_path(path):
                return "Error: Permission denied (Path outside working directory)."
            try:
                files = os.listdir(path)
                return json.dumps(files, indent=2)
            except Exception as e:
                return f"Error listing directory: {e}"

        elif fn_name == "read_file":
            path = args.get("file_path", "")
            if not self._is_safe_path(path):
                return "Error: Permission denied (Path outside working directory)."
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                return f"Error reading file '{path}': {e}"

        elif fn_name == "write_file":
            path = args.get("file_path", "")
            content = args.get("content", "")
            if not self._is_safe_path(path):
                return "Error: Permission denied (Path outside working directory)."
            try:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                return f"Successfully updated/written file: {path}"
            except Exception as e:
                return f"Error writing file '{path}': {e}"

        elif fn_name == "reload_extension":
            ext = args.get("extension_name", "")
            try:
                await self.bot.reload_extension(ext)
                return f"Successfully reloaded extension '{ext}'!"
            except Exception as e:
                return f"Error reloading extension '{ext}': {e}"

        return f"Unknown tool function: {fn_name}"

    @commands.command(name="reset", help="Clears the AI chatbot memory for this channel.")
    async def reset_memory(self, ctx: commands.Context):
        """Resets stored conversation history for the channel."""
        if ctx.channel.id != AI_CHANNEL_ID:
            await ctx.send("The AI Chatbot is not active in this channel.")
            return

        if ctx.channel.id in self.history:
            self.history[ctx.channel.id].clear()
            await ctx.send("🧹 Conversation memory has been cleared!")
        else:
            await ctx.send("Memory is already empty.")

    @commands.command(name="ai-info", help="Displays current active AI model and API key usage details.")
    async def ai_info(self, ctx: commands.Context):
        """Fetches AI model information and spending/usage stats from OpenRouter."""
        headers = {"Authorization": f"Bearer {self.api_key}"}
        timeout = aiohttp.ClientTimeout(total=15)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self.key_info_url, headers=headers) as resp:
                    if resp.status == 200:
                        res = await resp.json()
                        data = res.get("data", {})

                        label = data.get("label", "Unnamed Key")
                        usage_usd = data.get("usage", 0.0)
                        limit = data.get("limit")
                        limit_remaining = data.get("limit_remaining")
                        limit_reset = data.get("limit_reset")
                        is_free_tier = data.get("is_free_tier", False)

                        limit_str = f"${limit:.4f}" if limit is not None else "Unlimited"
                        remaining_str = f"${limit_remaining:.4f}" if limit_remaining is not None else "Unlimited"
                        reset_str = limit_reset.title() if limit_reset else "No automatic reset"

                        embed = discord.Embed(
                            title="🤖 AI Chatbot Information & Usage",
                            color=discord.Color.blue()
                        )
                        embed.add_field(name="Active Model", value=f"`{self.current_model}`", inline=False)
                        embed.add_field(name="Key Label", value=f"`{label}`", inline=False)
                        embed.add_field(name="Total Usage", value=f"`${usage_usd:.4f}` USD", inline=True)
                        embed.add_field(name="Credit Limit", value=f"`{limit_str}`", inline=True)
                        embed.add_field(name="Remaining Budget", value=f"`{remaining_str}`", inline=True)
                        embed.add_field(name="Limit Reset Policy", value=f"`{reset_str}`", inline=True)
                        embed.add_field(name="Free Tier Account", value=f"`{is_free_tier}`", inline=True)

                        await ctx.send(embed=embed)
                    else:
                        error_text = await resp.text()
                        print(f"OpenRouter Usage Error ({resp.status}): {error_text}")
                        await ctx.send(f"❌ Failed to retrieve API usage details (Status {resp.status}).")

        except Exception as e:
            print(f"Usage Exception: {e}")
            await ctx.send("❌ Encountered a network error while fetching AI details.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore messages from bots or outside the configured AI channel
        if message.author.bot or message.channel.id != AI_CHANNEL_ID:
            return

        # Ignore messages starting with the command prefix so commands process normally
        if message.content.startswith(BOT_PREFIX):
            return

        async with message.channel.typing():
            # Check if sender is bot owner
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

            # Build user content payload (Handling Text + Images)
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

            channel_id = message.channel.id
            self.history[channel_id].append(user_message_dict)

            # Keep recent history within limits
            self.history[channel_id] = self.history[channel_id][-self.max_history:]

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://your-site-or-repo.com",
                "X-Title": "Utilities Discord Bot Assistant"
            }

            timeout = aiohttp.ClientTimeout(total=90)

            # Function calling loop (allows model to call tools up to 5 iterations)
            current_payload_messages = [{"role": "system", "content": system_instruction}] + self.history[channel_id]

            for _ in range(5):
                payload = {
                    "model": self.current_model,
                    "messages": current_payload_messages,
                    "max_tokens": 1500
                }

                if is_owner:
                    payload["tools"] = self.get_owner_tools()

                try:
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.post(self.openrouter_url, headers=headers, json=payload) as resp:
                            if resp.status != 200:
                                error_text = await resp.text()
                                print(f"OpenRouter Error ({resp.status}): {error_text}")
                                await message.reply("Sorry, I had trouble generating a response.")
                                return

                            data = await resp.json()
                            choice_message = data['choices'][0]['message']

                            # Check if the AI wants to execute code-editing tools
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
                                # Loop back to let model process tool outputs and provide final response
                                continue

                            # Standard text response
                            reply_text = choice_message.get('content', '')
                            if reply_text:
                                self.history[channel_id].append({"role": "assistant", "content": reply_text})

                                chunks = split_message(reply_text)
                                for i, chunk in enumerate(chunks):
                                    if i == 0:
                                        await message.reply(chunk)
                                    else:
                                        await message.channel.send(chunk)
                            break

                except Exception as e:
                    await message.reply("Sorry, I encountered a network issue.")
                    print(f"Request Exception: {e}")
                    break


async def setup(bot):
    await bot.add_cog(AIChat(bot))