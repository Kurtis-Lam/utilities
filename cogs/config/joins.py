from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from views.common_views import error_embed
from views.embeds import handle_command_error
from views.joins_views import (
    AutoroleConfigView,
    GreetConfigView,
    WelcomeConfigView,
    make_member_welcome_embed,
    render_template,
    _delete_after,
)


@app_commands.default_permissions(administrator=True)
@commands.hybrid_command(name="joins", description="Configure welcome messages, greet pings and autoroles.")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def joinsconfig(ctx: commands.Context):
    cog = ctx.bot.get_cog("Joins")
    if not cog:
        return await ctx.reply(embed=error_embed("Joins module unavailable."), mention_author=False)
    config = await cog.get_guild_config(ctx.guild.id)
    view = WelcomeConfigView(cog.collection, config, author_id=ctx.author.id, guild=ctx.guild, member=ctx.author, cog=cog)
    view.message = await ctx.reply(view=view, mention_author=False)


@joinsconfig.error
async def joinsconfig_error(ctx: commands.Context, error: Exception):
    await handle_command_error(ctx, error)


class Joins(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def mongo_client(self):
        return self.bot.mongo_client

    @property
    def db(self):
        return self.mongo_client["utilities"]

    @property
    def collection(self):
        return self.db["joins_config"]

    async def cog_load(self):
        if self.mongo_client is not None:
            try:
                await self.mongo_client.admin.command("ping")
            except Exception as exc:
                print(f"Joins Cog: MongoDB warmup failed: {exc}")

    async def get_guild_config(self, guild_id: int) -> dict:
        defaults = {
            "_id": guild_id,
            "welcome_channel": None,
            "welcome_message": "Welcome {mention} to {server}!",
            "use_embed": True,
            "welcome_embed_title": "Welcome to {server}!",
            "welcome_embed_color": "0414C7",
            "welcome_author_name": "{username} joined!",
            "welcome_author_icon_url": "{avatar}",
            "welcome_footer": "Welcome to {server}!",
            "welcome_footer_icon_url": "",
            "welcome_thumbnail_url": "{avatar}",
            "welcome_image_url": "",
            "welcome_embed_fields": [],
            "welcome_show_timestamp": True,
            "welcome_ping_member": True,
            "greet_channels": [],
            "greet_delete_delay": 1.0,
            "autoroles": [],
            "protected_autoroles": [],
        }
        config = await self.collection.find_one({"_id": guild_id})
        if not config:
            config = defaults.copy()
            await self.collection.update_one({"_id": guild_id}, {"$setOnInsert": config}, upsert=True)
        else:
            missing = {key: value for key, value in defaults.items() if key not in config}
            if missing:
                await self.collection.update_one({"_id": guild_id}, {"$set": missing}, upsert=True)
                config.update(missing)
        return config

    async def ensure_protected_autorole(self, guild_id: int, role_id: int):
        await self.collection.update_one(
            {"_id": guild_id},
            {"$addToSet": {"autoroles": role_id, "protected_autoroles": role_id}},
            upsert=True,
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        config = await self.get_guild_config(member.guild.id)
        welcome_channel_id = config.get("welcome_channel")
        channel = member.guild.get_channel(int(welcome_channel_id)) if welcome_channel_id else None
        if channel:
            try:
                if config.get("use_embed", True):
                    await channel.send(
                        content=member.mention if config.get("welcome_ping_member", True) else None,
                        embed=make_member_welcome_embed(config, member.guild, member, channel),
                        allowed_mentions=discord.AllowedMentions(users=[member] if config.get("welcome_ping_member", True) else [], roles=False, everyone=False, replied_user=False),
                    )
                else:
                    content = render_template(str(config.get("welcome_message", "Welcome {mention}!")), member.guild, member, channel)
                    await channel.send(
                        content=content,
                        allowed_mentions=discord.AllowedMentions(users=[member] if config.get("welcome_ping_member", True) else [], roles=False, everyone=False, replied_user=False),
                    )
            except (discord.Forbidden, discord.HTTPException) as exc:
                print(f"Joins Cog: could not send welcome in guild {member.guild.id}: {exc}")

        for channel_id in config.get("greet_channels", []):
            greet_channel = member.guild.get_channel(int(channel_id))
            if not greet_channel:
                continue
            try:
                message = await greet_channel.send(
                    member.mention,
                    allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False, replied_user=False),
                )
                delay = max(0.1, min(30.0, float(config.get("greet_delete_delay", 1.0))))
                import asyncio
                asyncio.create_task(_delete_after(message, delay))
            except (discord.Forbidden, discord.HTTPException, ValueError):
                pass

        role_ids = list(dict.fromkeys(int(r) for r in config.get("autoroles", []) if str(r).isdigit()))
        protected = {int(r) for r in config.get("protected_autoroles", []) if str(r).isdigit()}
        if member.bot:
            role_ids = [role_id for role_id in role_ids if role_id not in protected]
        roles = [member.guild.get_role(role_id) for role_id in role_ids]
        roles = [role for role in roles if role and not role.is_default()]
        if roles:
            try:
                await member.add_roles(*roles, reason="Automatic role assignment on join")
            except (discord.Forbidden, discord.HTTPException):
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Joins(bot))
    if bot.get_command("joins") is None:
        bot.add_command(joinsconfig)