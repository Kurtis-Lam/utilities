import asyncio
import discord
from discord.ext import commands

class WildPokemonDetector(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.target_user_id = 716390085896962058
        self.target_channel_id = 1527623924811300967

    @commands.Cog.listener()
    async def on_message(self, message):
        # Check if the message is from the specified user
        if message.author.id != self.target_user_id:
            return

        # Check if the message contains embeds
        if message.embeds:
            for embed in message.embeds:
                # Check if the embed title starts with "Wild"
                if embed.title and embed.title.startswith("Wild"):
                    # Get the target channel to send the message link
                    channel = self.bot.get_channel(self.target_channel_id)
                    if channel:
                        await channel.send(
                            f"<@&1529318601444692102> Wild Pokémon fled! Message link: {message.jump_url}"
                        )
                    break

    @commands.command(name="checkflee")
    @commands.has_permissions(manage_messages=True)
    async def checkflee(self, ctx):
        failed_channels = []

        for channel in ctx.guild.text_channels:
            # Check bot permissions for the channel first
            permissions = channel.permissions_for(ctx.guild.me)
            if not (
                permissions.read_messages and permissions.read_message_history
            ):
                continue

            try:
                async for message in channel.history(limit=25):
                    if message.author.id == self.target_user_id:
                        if not message.content.startswith("Congratulations"):
                            failed_channels.append(channel.mention)
                        break
            except discord.HTTPException:
                continue

        if failed_channels:
            channels_str = ", ".join(failed_channels)
            await ctx.send(
                f"❌ Poketwo did not congratulate in the following channels: {channels_str}"
            )
        else:
            await ctx.send(
                "✅ All scanned channels have a 'Congratulations' message from Poketwo as their last Poketwo message."
            )


async def setup(bot):
    await bot.add_cog(WildPokemonDetector(bot))