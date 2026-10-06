import asyncio
import re
import secrets
import time
import typing
import discord
from discord import app_commands
from discord.ext import commands

# Import ConfirmView from views/common.py
from views.common import confirm
from views.embeds import handle_common_error, ok_embed, err_embed, warn_embed, info_embed, BRAND_COLOR
from views.navigate import PaginatorView

MAX_REASON_LENGTH = 500
DEFAULT_REASON = "No reason provided"


class Members(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # Retrieves MongoDB instance dynamically from main.py's bot.mongo_client
    @property
    def mongo_client(self):
        return self.bot.mongo_client

    @property
    def db(self):
        return self.mongo_client["utilities"]

    @property
    def settings_collection(self):
        return self.db["guild_settings"]

    @property
    def warns_collection(self):
        return self.db["warns"]

    @property
    def modlogs_collection(self):
        """Kick / ban / mute history: one document per action."""
        return self.db["modlogs"]

    async def cog_load(self):
        """Warms up the database connection and makes sure the warn indexes exist."""
        try:
            await self.mongo_client.admin.command("ping")
            await self.warns_collection.create_index([("guild_id", 1), ("user_id", 1)])
            await self.warns_collection.create_index([("guild_id", 1), ("id", 1)], unique=True)
            await self.modlogs_collection.create_index(
                [("guild_id", 1), ("user_id", 1), ("action", 1), ("created_at", -1)]
            )
        except Exception as e:
            print(f"Members Cog: MongoDB warmup failed: {e}")

    async def cog_command_error(self, ctx, error):
        if not await handle_common_error(ctx, error):
            raise error

    async def confirm_action(self, ctx, prompt: str) -> bool:
        return await confirm(
            ctx,
            prompt,
            title="⚠️ Confirmation Required",
            cancel_text="❌ Action Cancelled",
            timeout_text="⏱️ Timeout\nYou took too long to reply. Action cancelled.",
        )

    def parse_time(self, time_str: str) -> int:
        match = re.match(r"^(\d+)([smhd])$", time_str.lower())
        if not match:
            return None
        amount, unit = int(match.group(1)), match.group(2)
        multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        return amount * multipliers[unit]

    # ------------------------------------------------------------------
    # User Info (.whois)
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="whois",
        aliases=["userinfo", "ui"],
        with_app_command=True,
        description="Displays information about a server member or user."
    )
    @app_commands.describe(target="The member or user ID to look up.")
    @commands.guild_only()
    async def whois(
        self,
        ctx: commands.Context,
        target: typing.Union[discord.Member, discord.User] = None,
    ):
        if target is None:
            target = ctx.author

        if isinstance(target, int):
            member = ctx.guild.get_member(target)
            if member:
                target = member
            else:
                try:
                    target = await self.bot.fetch_user(target)
                except discord.NotFound:
                    return await ctx.reply(
                        embed=err_embed("User Not Found", "No user found with that ID."),
                        mention_author=False
                    )
                except discord.HTTPException as e:
                    return await ctx.reply(
                        embed=err_embed("Error", f"Failed to fetch user: `{e}`"),
                        mention_author=False
                    )

        embed = discord.Embed(
            title=target.name,
            color=BRAND_COLOR,
        )

        avatar_url = target.avatar.url if target.avatar else target.default_avatar.url
        embed.set_thumbnail(url=avatar_url)

        embed.add_field(name="User ID", value=f"`{target.id}`", inline=True)
        embed.add_field(name="Display Name", value=f"{target.display_name}", inline=True)

        created_ts = int(target.created_at.timestamp())
        embed.add_field(
            name="Account Created At",
            value=f"<t:{created_ts}:F> (<t:{created_ts}:R>)",
            inline=False,
        )

        if isinstance(target, discord.Member):
            if target.joined_at:
                joined_ts = int(target.joined_at.timestamp())
                joined_val = f"<t:{joined_ts}:F> (<t:{joined_ts}:R>)"
            else:
                joined_val = "Unknown"

            embed.add_field(
                name="Joined Server At",
                value=joined_val,
                inline=False,
            )

            roles = [role.mention for role in reversed(target.roles) if not role.is_default()]
            roles_str = ", ".join(roles) if roles else "None"

            embed.add_field(
                name=f"Roles [{len(roles)}]",
                value=roles_str[:1024],
                inline=False,
            )
        else:
            embed.add_field(
                name="Joined Server At",
                value="Not a member of this server",
                inline=False,
            )

        await ctx.reply(embed=embed, mention_author=False)

    # ------------------------------------------------------------------
    # Mute role (stored per server in MongoDB, set with .muterole)
    # ------------------------------------------------------------------

    async def get_mute_role(self, guild: discord.Guild) -> typing.Optional[discord.Role]:
        doc = await self.settings_collection.find_one({"_id": guild.id})
        role_id = doc.get("mute_role_id") if doc else None
        return guild.get_role(role_id) if role_id else None

    async def require_mute_role(self, ctx) -> typing.Optional[discord.Role]:
        """Returns the configured mute role, or sends an error and returns None."""
        role = await self.get_mute_role(ctx.guild)
        if role is None:
            await ctx.reply(embed=err_embed(
                "No Mute Role",
                f"An admin must set one first: `{ctx.clean_prefix}muterole @role`"
            ), mention_author=False)
        return role

    @commands.hybrid_command(name="muterole", with_app_command=True, description="Shows or sets the role used for mutes.")
    @app_commands.describe(
        role="The role to use for mutes (mention or ID). Leave blank to view the current one."
    )
    @commands.has_permissions(manage_roles=True)
    async def muterole(self, ctx, role: discord.Role = None):
        if role is None:
            current = await self.get_mute_role(ctx.guild)
            if current:
                return await ctx.reply(embed=info_embed("Mute Role", current.mention, emoji="🔇"), mention_author=False)
            return await ctx.reply(embed=info_embed(
                "Mute Role", f"Not set. Use `{ctx.clean_prefix}muterole @role`", emoji="🔇"
            ), mention_author=False)

        if not ctx.author.guild_permissions.administrator:
            return await ctx.reply(embed=err_embed(
                "Missing Permissions", "Only administrators can set the mute role."
            ), mention_author=False)
        if role.is_default() or role.managed:
            return await ctx.reply(embed=err_embed(
                "Invalid Role", "That role can't be used as a mute role."
            ), mention_author=False)
        if role >= ctx.guild.me.top_role:
            return await ctx.reply(embed=err_embed(
                "Role Too High", "That role is higher than or equal to my highest role."
            ), mention_author=False)

        await self.settings_collection.update_one(
            {"_id": ctx.guild.id},
            {"$set": {"mute_role_id": role.id}},
            upsert=True
        )
        await ctx.reply(embed=ok_embed("Mute Role Set", role.mention, emoji="🔇"), mention_author=False)

    # ------------------------------------------------------------------
    # Kick / ban / nick
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="kick", with_app_command=True, description="Removes a target user from the server.")
    @app_commands.describe(
        user="The server member to kick.",
        reason="The reason behind removing this user."
    )
    @commands.has_permissions(kick_members=True)
    async def kick(
        self, 
        ctx, 
        user: discord.Member, 
        *, 
        reason: str = "No reason provided"
    ):
        if user.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            embed = discord.Embed(title="❌ Action Denied", description="You cannot kick someone with a role higher than or equal to yours.", color=discord.Color.red())
            return await ctx.reply(embed=embed, mention_author=False)

        dm_embed = discord.Embed(title=f"You have been kicked from {ctx.guild.name}", color=discord.Color.orange())
        dm_embed.add_field(name="Reason", value=reason)

        try:
            await user.send(embed=dm_embed)
        except discord.HTTPException:
            pass

        await user.kick(reason=reason)
        await self._log_action(ctx, user, "kick", reason)
        
        success_embed = discord.Embed(
            title="👢 Member Kicked",
            description=f"**{user.name}** has been kicked from the server.",
            color=discord.Color.orange()
        )
        success_embed.add_field(name="Reason", value=reason, inline=False)
        await ctx.reply(embed=success_embed, mention_author=False)

    @commands.hybrid_command(name="ban", with_app_command=True, description="Bans a member from the server permanently.")
    @app_commands.describe(
        user="The server member to ban.",
        reason="The reason behind banning this user."
    )
    @commands.has_permissions(ban_members=True)
    async def ban(
        self, 
        ctx, 
        user: discord.Member, 
        *, 
        reason: str = "No reason provided"
    ):
        if user.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            embed = discord.Embed(title="❌ Action Denied", description="You cannot ban someone with a role higher than or equal to yours.", color=discord.Color.red())
            return await ctx.reply(embed=embed, mention_author=False)

        confirmed = await self.confirm_action(ctx, f"Are you sure you want to ban **{user}**? This cannot be undone easily.")
        if not confirmed:
            return

        dm_embed = discord.Embed(title=f"You have been banned from {ctx.guild.name}", color=discord.Color.red())
        dm_embed.add_field(name="Reason", value=reason)

        try:
            await user.send(embed=dm_embed)
        except discord.HTTPException:
            pass

        await user.ban(reason=reason)
        await self._log_action(ctx, user, "ban", reason)
        
        success_embed = discord.Embed(
            title="🔨 Member Banned",
            description=f"**{user.name}** has been banned from the server.",
            color=discord.Color.red()
        )
        success_embed.add_field(name="Reason", value=reason, inline=False)
        await ctx.reply(embed=success_embed, mention_author=False)

    @commands.hybrid_command(name="nick", with_app_command=True, description="Changes the nickname of a target server member.")
    @app_commands.describe(
        user="The server member whose nickname to change.",
        new_name="The new nickname string (leave blank/none to reset)."
    )
    @commands.has_permissions(manage_nicknames=True)
    @commands.bot_has_permissions(manage_nicknames=True)
    async def nick(
        self, 
        ctx, 
        user: discord.Member, 
        *, 
        new_name: str
    ):
        if user.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            embed = discord.Embed(title="❌ Action Denied", description="You cannot change the nickname of someone with a role higher than or equal to yours.", color=discord.Color.red())
            return await ctx.reply(embed=embed, mention_author=False)
            
        if user.top_role >= ctx.guild.me.top_role:
            embed = discord.Embed(title="❌ Action Denied", description="I cannot change this user's nickname because their top role is higher than or equal to mine.", color=discord.Color.red())
            return await ctx.reply(embed=embed, mention_author=False)

        old_name = user.display_name
        try:
            target_nick = None if new_name.lower() in ["none", "reset", ""] else new_name
            await user.edit(nick=target_nick, reason=f"Changed by {ctx.author} ({ctx.author.id})")
            
            if target_nick is None:
                embed = discord.Embed(
                    title="✏️ Nickname Reset",
                    description=f"Successfully reset the nickname for **{user.name}** (was `{old_name}`).",
                    color=discord.Color.blue()
                )
            else:
                embed = discord.Embed(
                    title="✏️ Nickname Changed",
                    description=f"Successfully changed nickname for **{user.name}** from `{old_name}` to `{target_nick}`.",
                    color=discord.Color.blue()
                )
            await ctx.reply(embed=embed, mention_author=False)
        except discord.Forbidden:
            embed = discord.Embed(title="❌ Error", description="I lack the permissions to change this user's nickname.", color=discord.Color.red())
            await ctx.reply(embed=embed, mention_author=False)
        except discord.HTTPException as e:
            embed = discord.Embed(title="❌ Error", description=f"Failed to change nickname due to an error: {e}", color=discord.Color.red())
            await ctx.reply(embed=embed, mention_author=False)

    # ------------------------------------------------------------------
    # Mute / unmute (uses the role configured with .muterole)
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="mute", with_app_command=True, description="Mutes a user with the server's mute role for a set duration.")
    @app_commands.describe(
        user="The server member to mute.",
        duration="Duration of the mute (e.g. 30s, 10m, 2h, 1d). Leave blank for permanent.",
        reason="The reason for muting."
    )
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def mute(
        self,
        ctx,
        user: discord.Member,
        duration: typing.Optional[str] = None,
        *,
        reason: str = "No reason provided"
    ):
        if user.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            embed = discord.Embed(title="❌ Action Denied", description="You cannot mute someone with a role higher than or equal to yours.", color=discord.Color.red())
            return await ctx.reply(embed=embed, mention_author=False)

        muted_role = await self.require_mute_role(ctx)
        if not muted_role:
            return

        if muted_role >= ctx.guild.me.top_role:
            return await ctx.reply(embed=err_embed(
                "Role Too High", "The mute role is higher than or equal to my highest role."
            ), mention_author=False)

        seconds = self.parse_time(duration) if duration else None
        if duration and seconds is None:
            embed = discord.Embed(title="❌ Invalid Duration", description="Please use a valid time format like `30s`, `10m`, `2h`, or `1d`.", color=discord.Color.red())
            return await ctx.reply(embed=embed, mention_author=False)

        if muted_role in user.roles:
            return await ctx.reply(embed=err_embed("Already Muted", f"{user.mention} is already muted."), mention_author=False)

        try:
            await user.add_roles(muted_role, reason=f"Muted by {ctx.author}: {reason}")
        except discord.HTTPException:
            embed = discord.Embed(title="❌ Error", description="Failed to assign the muted role to the user.", color=discord.Color.red())
            return await ctx.reply(embed=embed, mention_author=False)

        await self._log_action(ctx, user, "mute", reason, duration)

        dm_embed = discord.Embed(title=f"You have been muted in {ctx.guild.name}", color=discord.Color.dark_grey())
        dm_embed.add_field(name="Reason", value=reason)
        if duration:
            dm_embed.add_field(name="Duration", value=duration)
        try:
            await user.send(embed=dm_embed)
        except discord.HTTPException:
            pass

        embed = discord.Embed(
            title="🔇 Member Muted",
            description=f"**{user.mention}** has been muted globally.",
            color=discord.Color.dark_grey()
        )
        embed.add_field(name="Reason", value=reason, inline=False)
        if duration:
            embed.add_field(name="Duration", value=duration, inline=False)
        await ctx.reply(embed=embed, mention_author=False)

        if seconds:
            await asyncio.sleep(seconds)
            if muted_role in user.roles:
                try:
                    await user.remove_roles(muted_role, reason="Mute duration expired.")
                except discord.HTTPException:
                    return
                unmute_dm = discord.Embed(title=f"You have been unmuted in {ctx.guild.name}", color=discord.Color.green())
                try:
                    await user.send(embed=unmute_dm)
                except discord.HTTPException:
                    pass

    @commands.hybrid_command(name="unmute", with_app_command=True, description="Unmutes a user by removing the mute role.")
    @app_commands.describe(
        user="The server member to unmute."
    )
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def unmute(
        self,
        ctx,
        user: discord.Member
    ):
        muted_role = await self.require_mute_role(ctx)
        if not muted_role:
            return

        if muted_role not in user.roles:
            embed = discord.Embed(title="❌ Error", description="This user is not currently muted.", color=discord.Color.red())
            return await ctx.reply(embed=embed, mention_author=False)

        await user.remove_roles(muted_role, reason=f"Unmuted by {ctx.author}")

        dm_embed = discord.Embed(title=f"You have been unmuted in {ctx.guild.name}", color=discord.Color.green())
        try:
            await user.send(embed=dm_embed)
        except discord.HTTPException:
            pass

        embed = discord.Embed(
            title="🔊 Member Unmuted",
            description=f"**{user.mention}** has been unmuted.",
            color=discord.Color.green()
        )
        await ctx.reply(embed=embed, mention_author=False)

    # ------------------------------------------------------------------
    # Warns (saved in MongoDB, each warn has a unique ID)
    # ------------------------------------------------------------------

    async def _new_warn_id(self, guild_id: int) -> str:
        while True:
            warn_id = secrets.token_hex(3)
            if not await self.warns_collection.find_one({"guild_id": guild_id, "id": warn_id}):
                return warn_id

    async def _remove_warn(self, ctx, user: discord.User, warn_id: str):
        warn_id = warn_id.strip("` ").lower()
        result = await self.warns_collection.delete_one(
            {"guild_id": ctx.guild.id, "user_id": user.id, "id": warn_id}
        )
        if result.deleted_count:
            await ctx.reply(embed=ok_embed("Warn Removed", f"Removed `{warn_id}` from {user.mention}.", emoji="🗑️"), mention_author=False)
        else:
            await ctx.reply(embed=err_embed("Warn Not Found", f"{user.mention} has no warn with ID `{warn_id}`."), mention_author=False)

    @commands.hybrid_group(
        name="warn",
        fallback="add",
        invoke_without_command=True,
        with_app_command=True,
        description="Warns a member and saves it."
    )
    @app_commands.describe(
        user="The server member to warn.",
        reason="The reason for the warn (optional)."
    )
    @commands.has_permissions(manage_messages=True)
    async def warn(
        self,
        ctx,
        user: discord.Member,
        *,
        reason: str = "No reason provided"
    ):
        if user.bot:
            return await ctx.reply(embed=err_embed("Action Denied", "You cannot warn a bot."), mention_author=False)
        if user == ctx.author:
            return await ctx.reply(embed=err_embed("Action Denied", "You cannot warn yourself."), mention_author=False)
        if user.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.reply(embed=err_embed(
                "Action Denied", "You cannot warn someone with a role higher than or equal to yours."
            ), mention_author=False)

        reason = reason.strip()[:MAX_REASON_LENGTH] or "No reason provided"
        warn_id = await self._new_warn_id(ctx.guild.id)

        await self.warns_collection.insert_one({
            "id": warn_id,
            "guild_id": ctx.guild.id,
            "user_id": user.id,
            "moderator_id": ctx.author.id,
            "reason": reason,
            "created_at": time.time(),
        })
        total = await self.warns_collection.count_documents(
            {"guild_id": ctx.guild.id, "user_id": user.id}
        )

        dm_embed = warn_embed(f"You were warned in {ctx.guild.name}", f"**Reason:** {reason}")
        try:
            await user.send(embed=dm_embed)
        except discord.HTTPException:
            pass

        await ctx.reply(embed=warn_embed(
            "Member Warned",
            f"{user.mention} warned. ID `{warn_id}` · Total **{total}**\n**Reason:** {reason}"
        ), mention_author=False)

    @warn.command(name="remove", aliases=["r"], description="Removes a warn by its ID.")
    @app_commands.describe(
        user="The member (mention or ID) the warn belongs to.",
        warn_id="The ID of the warn to remove."
    )
    @commands.has_permissions(administrator=True)
    async def warn_remove(self, ctx, user: discord.User, warn_id: str):
        await self._remove_warn(ctx, user, warn_id)

    @commands.hybrid_group(
        name="warns",
        fallback="view",
        invoke_without_command=True,
        with_app_command=True,
        description="Shows a user's warns."
    )
    @app_commands.describe(user="The member (mention or ID) to look up.")
    @commands.has_permissions(administrator=True)
    async def warns(self, ctx, user: discord.User):
        docs = await self.warns_collection.find(
            {"guild_id": ctx.guild.id, "user_id": user.id}
        ).sort("created_at", -1).to_list(length=None)

        if not docs:
            return await ctx.reply(embed=info_embed("No Warns", f"{user.mention} has no warns.", emoji="📭"), mention_author=False)

        lines = []
        used = 0
        for doc in docs:
            line = (
                f"`{doc['id']}` — {doc['reason']} · "
                f"<@{doc['moderator_id']}> · <t:{int(doc['created_at'])}:R>"
            )
            if used + len(line) > 3800:
                lines.append(f"*…and {len(docs) - len(lines)} more*")
                break
            lines.append(line)
            used += len(line) + 1

        await ctx.reply(embed=warn_embed(
            f"Warns for {user.name} ({len(docs)})", "\n".join(lines)
        ), mention_author=False)

    @warns.command(name="remove", aliases=["r"], description="Removes a warn by its ID.")
    @app_commands.describe(
        user="The member (mention or ID) the warn belongs to.",
        warn_id="The ID of the warn to remove."
    )
    @commands.has_permissions(administrator=True)
    async def warns_remove(self, ctx, user: discord.User, warn_id: str):
        await self._remove_warn(ctx, user, warn_id)


    # ------------------------------------------------------------------
    # Kick / ban / mute history (saved in MongoDB)
    # ------------------------------------------------------------------

    async def _log_action(self, ctx, user: discord.abc.User, action: str, reason: str, duration: typing.Optional[str] = None):
        """Saves who did what to whom, when, and why. The reason is only stored if one was given."""
        provided = reason.strip()[:MAX_REASON_LENGTH] if reason and reason != DEFAULT_REASON else None
        doc = {
            "guild_id": ctx.guild.id,
            "user_id": user.id,
            "moderator_id": ctx.author.id,
            "action": action,
            "reason": provided,
            "created_at": time.time(),
        }
        if duration:
            doc["duration"] = duration
        try:
            await self.modlogs_collection.insert_one(doc)
        except Exception as e:
            print(f"Members Cog: failed to save {action} log: {e}")

    async def _show_logs(self, ctx, user: discord.User, action: str, title: str, noun: str, color: discord.Color, per_page: int = 5):
        docs = await self.modlogs_collection.find(
            {"guild_id": ctx.guild.id, "user_id": user.id, "action": action}
        ).sort("created_at", -1).to_list(length=None)

        if not docs:
            return await ctx.reply(embed=info_embed(f"No {noun.title()} Records", f"{user.mention} has no recorded {noun}s.", emoji="📭"), mention_author=False)

        entries = []
        for n, d in enumerate(docs, 1):
            ts = int(d["created_at"])
            lines = [
                f"**#{n}** · <t:{ts}:F> (<t:{ts}:R>)",
                f"**By:** <@{d['moderator_id']}>",
                f"**Reason:** {d.get('reason') or DEFAULT_REASON}",
            ]
            if d.get("duration"):
                lines.append(f"**Duration:** {d['duration']}")
            entries.append("\n".join(lines))

        chunks = [entries[i:i + per_page] for i in range(0, len(entries), per_page)]
        pages = []
        for idx, chunk in enumerate(chunks, 1):
            embed = discord.Embed(
                title=f"{title} — {user} ({len(docs)})",
                description="\n\n".join(chunk),
                color=color,
            )
            embed.set_footer(text=f"Page {idx} out of {len(chunks)}")
            pages.append(embed)

        if len(pages) == 1:
            return await ctx.reply(embed=pages[0], mention_author=False)

        view = PaginatorView(pages, user_id=ctx.author.id)
        view.message = await ctx.reply(embed=pages[0], view=view, mention_author=False)

    @commands.hybrid_command(name="kicked", with_app_command=True, description="Shows when, by whom and why a user was kicked.")
    @app_commands.describe(user="The user (mention or ID) to look up.")
    @commands.has_permissions(administrator=True)
    async def kicked(self, ctx, user: discord.User):
        await self._show_logs(ctx, user, "kick", "👢 Kick History", "kick", discord.Color.orange())

    @commands.hybrid_command(name="banned", with_app_command=True, description="Shows when, by whom and why a user was banned.")
    @app_commands.describe(user="The user (mention or ID) to look up.")
    @commands.has_permissions(administrator=True)
    async def banned(self, ctx, user: discord.User):
        await self._show_logs(ctx, user, "ban", "🔨 Ban History", "ban", discord.Color.red())

    @commands.hybrid_command(name="mutelogs", with_app_command=True, description="Shows a user's mute history (when, by whom, why, and for how long).")
    @app_commands.describe(user="The user (mention or ID) to look up.")
    @commands.has_permissions(administrator=True)
    async def mutelogs(self, ctx, user: discord.User):
        await self._show_logs(ctx, user, "mute", "🔇 Mute History", "mute", discord.Color.dark_grey())


async def setup(bot):
    await bot.add_cog(Members(bot))