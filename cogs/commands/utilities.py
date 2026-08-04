import asyncio
import re
import typing
import discord
from discord import app_commands
from discord.ext import commands

class Utilities(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="addprefix", description="Adds the # in front of all integers for Poketwo redirection")
    @commands.is_owner()
    async def addprefix(
        self, 
        ctx, 
        amt: int = commands.parameter(description="The maximum upper range integer to format (e.g., 5 prints #1 through #5).")
    ):
        prefixed_numbers = [f"#{i}" for i in range(1, amt + 1)]
        result = ' '.join(prefixed_numbers)
        formatted_result = f"```\n{result}\n```"
        await ctx.send(formatted_result)
    
    @commands.command(name="note", description="Notes a referral message on a specific channel")
    @commands.is_owner()
    async def note(self, ctx):
        if not ctx.message.reference:
            await ctx.send("Please reply to the message you want to note with `.note`")
            return
        
        try:
            replied_message = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            target_channel_id = 1458732659378229248
            target_channel = self.bot.get_channel(target_channel_id)

            if not target_channel:
                await ctx.send(f"Could not find the target channel with ID {target_channel_id}.")
                return

            original_content = replied_message.content
            jump_url = replied_message.jump_url
            final_content = f"{original_content}\n\n{jump_url}"
            files = []
            for attachment in replied_message.attachments:
                f = await attachment.to_file()
                files.append(f)
            embeds = replied_message.embeds
            await target_channel.send(content=final_content, files=files, embeds=embeds)
            await ctx.message.add_reaction("✅")

        except discord.Forbidden:
            await ctx.send("I do not have permission to send messages to the target channel.")
        except discord.HTTPException as e:
            await ctx.send(f"Failed to send message: {e}")
        except Exception as e:
            await ctx.send(f"An error occurred: {e}")

    @commands.command(name="remindme", aliases=["rm"], description="Sets a reminder.")
    @commands.is_owner()
    async def remindme(
        self, 
        ctx, 
        time: str = commands.parameter(description="The duration string before execution (e.g., 10s, 5m, 2h, 1d)."), 
        *, 
        message: str = commands.parameter(description="The text content you want to be reminded about.")
    ):
        time_pattern = re.compile(r"(\d+)([smhd])")
        match = time_pattern.match(time)

        if not match:
            return await ctx.send("Invalid time format. Use `<number><unit>` (e.g., `10s`, `5m`, `2h`, `1d`).")

        amount = int(match.group(1))
        unit = match.group(2)
        seconds = 0

        if unit == 's':
            seconds = amount
        elif unit == 'm':
            seconds = amount * 60
        elif unit == 'h':
            seconds = amount * 3600
        elif unit == 'd':
            seconds = amount * 86400
        
        if seconds > 2592000:
            return await ctx.send("Reminder cannot be longer than 30 days.")

        await ctx.send(f"I will remind you in **{time}**: {message}")
        await asyncio.sleep(seconds)
        await ctx.send(f"{ctx.author.mention} ⏰ **Reminder:** {message}")
            
async def setup(bot):
    await bot.add_cog(Utilities(bot))