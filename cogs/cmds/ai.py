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
        self.current_model = "meta-llama/llama-3.3-70b-instruct"
        # Memory store: channel_id -> list of message dicts
        self.history = defaultdict(list)
        self.max_history = 10  # Store up to 10 back-and-forth messages

    def generate_bot_capabilities_prompt(self) -> str:
        """Inspects all cogs and commands dynamically to build system context."""
        capabilities = ["Here is a summary of commands and capabilities you can explain to users:\n"]

        for cog_name, cog in self.bot.cogs.items():
            capabilities.append(f"### Cog: {cog_name}")
            for cmd in cog.get_commands():
                if cmd.hidden:
                    continue
                
                desc = cmd.help or cmd.brief or "No description provided."
                signature = f"{BOT_PREFIX}{cmd.name} {cmd.signature}".strip()
                capabilities.append(f"- `{signature}`: {desc}")
            capabilities.append("")

        return "\n".join(capabilities)

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
        headers = {
            "Authorization": f"Bearer {self.api_key}"
        }

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

                        # Parse limits formatting
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

        # Ignore messages starting with the command prefix so commands like !reset and !ai-info process normally
        if message.content.startswith(BOT_PREFIX):
            return

        async with message.channel.typing():
            capabilities_info = self.generate_bot_capabilities_prompt()

            system_instruction = (
                "You are an assistant bot in a Discord server. "
                "Answer user questions accurately and help them navigate server features.\n\n"
                f"{capabilities_info}"
            )

            # Append new user message to local history
            channel_id = message.channel.id
            self.history[channel_id].append({"role": "user", "content": message.content})

            # Keep only the last N messages to fit within token limits
            self.history[channel_id] = self.history[channel_id][-self.max_history:]

            # Combine system instruction with conversation history
            payload_messages = [{"role": "system", "content": system_instruction}] + self.history[channel_id]

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://your-site-or-repo.com",
                "X-Title": "Discord Bot Assistant"
            }

            payload = {
                "model": self.current_model,
                "messages": payload_messages
            }

            timeout = aiohttp.ClientTimeout(total=60)

            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(self.openrouter_url, headers=headers, json=payload) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            reply_text = data['choices'][0]['message']['content']
                            
                            # Append assistant response to history
                            self.history[channel_id].append({"role": "assistant", "content": reply_text})

                            chunks = split_message(reply_text)
                            for i, chunk in enumerate(chunks):
                                if i == 0:
                                    await message.reply(chunk)
                                else:
                                    await message.channel.send(chunk)
                        else:
                            error_text = await resp.text()
                            print(f"OpenRouter Error ({resp.status}): {error_text}")
                            await message.reply("Sorry, I had trouble generating a response.")

            except Exception as e:
                await message.reply("Sorry, I encountered a network issue.")
                print(f"Request Exception: {e}")

async def setup(bot):
    await bot.add_cog(AIChat(bot))