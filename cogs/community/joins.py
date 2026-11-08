from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from views.common_views import error_embed
from views.embeds import handle_command_error
from views.setup_wizard import SetupWizard, Step, start_wizard
from views.joins_views import (
    AutoroleConfigView,
    GreetConfigView,
    WelcomeConfigView,
    PLACEHOLDER_HELP,
    format_invite_line,
    make_member_welcome_embed,
    render_template,
    _delete_after,
)


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

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        await handle_command_error(ctx, error)

    async def _require_db(self, ctx: commands.Context) -> bool:
        if self.mongo_client is not None:
            return True
        await ctx.reply(embed=error_embed("MongoDB is not configured; join settings are unavailable."), mention_author=False)
        return False

    @commands.hybrid_group(
        name="joins",
        invoke_without_command=True,
        fallback="panel",
        description="Configure welcome messages, greet pings and autoroles.",
    )
    @app_commands.default_permissions(administrator=True)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def joinsconfig(self, ctx: commands.Context):
        if not await self._require_db(ctx):
            return
        config = await self.get_guild_config(ctx.guild.id)
        view = WelcomeConfigView(self.collection, config, author_id=ctx.author.id, guild=ctx.guild, member=ctx.author, cog=self)
        view.message = await ctx.reply(view=view, mention_author=False)

    def _protected_roles(self, config: dict) -> set[int]:
        return {int(r) for r in config.get("protected_autoroles", []) if str(r).isdigit()}

    def _setup_steps(self) -> list[Step]:
        has_channel = lambda v: bool(v.get("welcome_channel"))
        return [
            Step(
                key="welcome_channel",
                title="Welcome channel",
                prompt="Pick the channel where new members are welcomed. Use Clear to turn welcome messages off.",
                kind="channel",
                optional=True,
            ),
            Step(
                key="use_embed",
                title="Welcome style",
                prompt="Send the welcome as a styled embed? Choose No for a plain text message.",
                kind="toggle",
                when=has_channel,
            ),
            Step(
                key="welcome_embed_title",
                title="Embed title",
                prompt="The title of the welcome embed.",
                kind="text",
                max_length=256,
                placeholder="Welcome to {server}!",
                hint=f"Placeholders: {PLACEHOLDER_HELP}",
                when=lambda v: has_channel(v) and bool(v.get("use_embed")),
            ),
            Step(
                key="welcome_message",
                title="Welcome text",
                prompt="The message sent for each new member.",
                kind="paragraph",
                max_length=1000,
                placeholder="Welcome {mention} to {server}!",
                hint=f"Placeholders: {PLACEHOLDER_HELP}",
                when=lambda v: has_channel(v) and not v.get("use_embed"),
            ),
            Step(
                key="welcome_ping_member",
                title="Ping the member",
                prompt="Mention the new member in the welcome message?",
                kind="toggle",
                when=has_channel,
            ),
            Step(
                key="welcome_show_inviter",
                title="Show the inviter",
                prompt="Show who invited the member in the welcome embed?",
                kind="toggle",
                when=lambda v: has_channel(v) and bool(v.get("use_embed")),
            ),
            Step(
                key="greet_channels",
                title="Greet channels",
                prompt="Pick channels where new members get a quick ping that disappears right after, so those channels light up for them.",
                kind="channels",
                optional=True,
                maximum=10,
            ),
            Step(
                key="autoroles",
                title="Autoroles",
                prompt="Pick the roles every new member receives automatically.",
                kind="roles",
                optional=True,
                maximum=15,
                hint="Verification roles are protected and kept automatically. My highest role must be above these.",
            ),
        ]

    async def _finish_setup(self, interaction: discord.Interaction, values: dict) -> str:
        guild = interaction.guild
        config = await self.get_guild_config(guild.id)
        protected = self._protected_roles(config)

        chosen = [int(r) for r in (values.get("autoroles") or [])]
        me = guild.me
        for role_id in chosen:
            role = guild.get_role(role_id)
            if role is None or role.is_default():
                raise ValueError("One of the selected autoroles can't be used (it's @everyone or no longer exists).")
            if role.managed:
                raise ValueError(f"{role.name} is managed by an integration and can't be given automatically.")
            if me is not None and role >= me.top_role:
                raise ValueError(f"Move my highest role above {role.name} so I can assign it.")
        autoroles = list(dict.fromkeys(chosen + [int(r) for r in config.get("autoroles", []) if int(r) in protected]))

        channel_id = values.get("welcome_channel")
        update = {
            "welcome_channel": channel_id,
            "use_embed": bool(values.get("use_embed", True)),
            "welcome_embed_title": values.get("welcome_embed_title") or config.get("welcome_embed_title"),
            "welcome_message": values.get("welcome_message") or config.get("welcome_message"),
            "welcome_ping_member": bool(values.get("welcome_ping_member", True)),
            "welcome_show_inviter": bool(values.get("welcome_show_inviter", True)),
            "greet_channels": [int(c) for c in (values.get("greet_channels") or [])],
            "autoroles": autoroles,
        }
        await self.collection.update_one({"_id": guild.id}, {"$set": update}, upsert=True)

        lines = [
            f"**Welcome channel:** {f'<#{channel_id}>' if channel_id else 'off'}",
            f"**Style:** {'Embed' if update['use_embed'] else 'Plain text'}" if channel_id else "",
            f"**Greet channels:** {' '.join(f'<#{c}>' for c in update['greet_channels']) or 'none'}",
            f"**Autoroles:** {' '.join(f'<@&{r}>' for r in autoroles) or 'none'}",
            "-# Colors, images and extra embed fields are in `joins`.",
        ]
        return "\n".join(line for line in lines if line)

    @joinsconfig.command(name="setup", description="Guided joins setup. Run it again any time to change settings.")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def joins_setup(self, ctx: commands.Context):
        if not await self._require_db(ctx):
            return
        config = await self.get_guild_config(ctx.guild.id)
        protected = self._protected_roles(config)
        initial = {
            "welcome_channel": int(config["welcome_channel"]) if config.get("welcome_channel") else None,
            "use_embed": bool(config.get("use_embed", True)),
            "welcome_embed_title": config.get("welcome_embed_title") or "Welcome to {server}!",
            "welcome_message": config.get("welcome_message") or "Welcome {mention} to {server}!",
            "welcome_ping_member": bool(config.get("welcome_ping_member", True)),
            "welcome_show_inviter": bool(config.get("welcome_show_inviter", True)),
            "greet_channels": [int(c) for c in config.get("greet_channels", []) if str(c).isdigit()],
            "autoroles": [int(r) for r in config.get("autoroles", []) if str(r).isdigit() and int(r) not in protected],
        }
        wizard = SetupWizard(
            title="👋 Joins Setup",
            steps=self._setup_steps(),
            initial=initial,
            on_finish=self._finish_setup,
            author_id=ctx.author.id,
            guild=ctx.guild,
        )
        await start_wizard(ctx, wizard)

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
            "welcome_show_inviter": True,
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
                    invite_line = None
                    tracker = self.bot.get_cog("Invites")
                    if tracker is not None and config.get("welcome_show_inviter", True):
                        invite_line = format_invite_line(await tracker.process_join(member))
                    await channel.send(
                        content=member.mention if config.get("welcome_ping_member", True) else None,
                        embed=make_member_welcome_embed(config, member.guild, member, channel, invite_line),
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