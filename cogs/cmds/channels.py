import asyncio
import re
import typing
import discord
from discord import app_commands
from discord.ext import commands

# Import ConfirmView from views/common.py
from views.common_views import confirm, confirm_and_run
from views.embeds import handle_common_error, send_usage
from views.loading import loading
from views.prompts import ask, is_skip, one_line

def _normalize(text: str) -> str:
    """Lower-case and treat '-', '_' and repeated spaces as one space ("staff-area" == "Staff  Area")."""
    return " ".join(re.sub(r"[-_]+", " ", text).lower().split())


def find_category(guild: discord.Guild, query: str):
    """Find a category by ID, <#mention>, or name (spaces allowed, case-insensitive).

    Returns (category, matches): `category` is the single match or None; `matches` lists
    every candidate when the name is ambiguous (more than one category fits)."""
    query = query.strip().strip("\"'").strip()
    if not query:
        return None, []

    digits = re.fullmatch(r"<#(\d+)>|(\d{15,25})", query)
    if digits:
        channel = guild.get_channel(int(digits.group(1) or digits.group(2)))
        return (channel, []) if isinstance(channel, discord.CategoryChannel) else (None, [])

    wanted = _normalize(query)
    for matches in (
        [c for c in guild.categories if _normalize(c.name) == wanted],
        [c for c in guild.categories if _normalize(c.name).startswith(wanted)],
        [c for c in guild.categories if wanted in _normalize(c.name)],
    ):
        if len(matches) == 1:
            return matches[0], []
        if matches:
            return None, matches
    return None, []


class Channels(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_command_error(self, ctx, error):
        if not await handle_common_error(ctx, error):
            raise error

    async def confirm_action(self, ctx, prompt: str) -> bool:
        return await confirm(
            ctx,
            prompt,
            title="⚠️ Confirm",
            cancel_text="❌ Cancelled",
            timeout_text="⏱️ Timed out",
        )

    @commands.hybrid_command(aliases=["cch"], name="createchannel", description="Creates a new text channel.", with_app_command=True)
    @app_commands.describe(
        name="The name of the new channel. Leave blank and I'll ask you step by step.",
        category="The category to create the channel in. Defaults to the current category.",
        preaction="Automatically lock or hide the channel upon creation."
    )
    @commands.has_permissions(manage_channels=True)
    async def createchannel(
        self,
        ctx,
        name: typing.Optional[str] = commands.parameter(default=None, description="The name of the new channel (leave blank to be asked)."),
        category: typing.Optional[discord.CategoryChannel] = commands.parameter(default=None, description="The category to create the channel in. Defaults to current category."),
        preaction: typing.Optional[typing.Literal["prelock", "prehide", "both"]] = commands.parameter(default=None, description="Apply prelock, prehide, or both upon creation.")
    ):
        prelock = False
        prehide = False

        if ctx.interaction is None:
            content = ctx.message.content.lower()

            # Check and parse flags safely from the raw message content for prefix commands
            if "--prelock" in content:
                prelock = True
            if "--prehide" in content:
                prehide = True

            # Remove flags from the parsed name in case they were captured in quotes or text
            name = re.sub(r'(?i)--prelock', '', name or "").strip()
            name = re.sub(r'(?i)--prehide', '', name).strip()
        else:
            name = (name or "").strip()
            if preaction in ("prelock", "both"):
                prelock = True
            if preaction in ("prehide", "both"):
                prehide = True

        # No name given -> ask the questions one by one (30 seconds each)
        if not name:
            reply = await ask(ctx, "What do you want the **channel name** to be?")
            if reply is None:
                return
            name = reply.content.strip()
            if not name:
                return await ctx.reply(one_line("❌ The channel name can't be empty. Cancelled."), mention_author=False)

            reply = await ask(
                ctx,
                "Which **category** should it be created in? Mention / ID / name it, "
                "or type `skip` to use this channel's category."
            )
            if reply is None:
                return
            if not is_skip(reply.content):
                try:
                    category = await commands.CategoryChannelConverter().convert(ctx, reply.content.strip())
                except commands.BadArgument:
                    return await ctx.reply(one_line("❌ Couldn't find that category. Cancelled."), mention_author=False)

            if not (prelock or prehide):
                reply = await ask(ctx, "Which **preaction** do you want? `prelock`, `prehide`, `both`, or `none`.")
                if reply is None:
                    return
                choice = reply.content.strip().lower()
                if choice in ("prelock", "lock"):
                    prelock = True
                elif choice in ("prehide", "hide"):
                    prehide = True
                elif choice == "both":
                    prelock = prehide = True
                elif not is_skip(choice):
                    return await ctx.reply(one_line("❌ Invalid preaction. Cancelled."), mention_author=False)

        # If no category was provided, default to the current channel's category
        if category is None and getattr(ctx.channel, "category", None) is not None:
            category = ctx.channel.category

        overwrites = {}

        if prelock or prehide:
            # If a category is provided or inferred, clone its overwrites so the channel still syncs
            if category:
                overwrites = {target: overwrite for target, overwrite in category.overwrites.items()}

            default_overwrite = overwrites.get(ctx.guild.default_role, discord.PermissionOverwrite())

            if prelock:
                default_overwrite.send_messages = False
            if prehide:
                default_overwrite.view_channel = False

            overwrites[ctx.guild.default_role] = default_overwrite

            # Ensure the bot keeps channel permissions
            if ctx.guild.me not in overwrites:
                overwrites[ctx.guild.me] = discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)

            new_channel = await ctx.guild.create_text_channel(
                name=name,
                category=category,
                overwrites=overwrites,
                reason=f"Created by {ctx.author}"
            )
        else:
            new_channel = await ctx.guild.create_text_channel(
                name=name,
                category=category,
                reason=f"Created by {ctx.author}"
            )

        # Format the success message
        status_flags = []
        if prelock: status_flags.append("🔒 Locked")
        if prehide: status_flags.append("🙈 Hidden")
        status_text = f" ({', '.join(status_flags)})" if status_flags else ""

        embed = discord.Embed(
            title="✨ Channel Created",
            description=f"Successfully created channel {new_channel.mention}{status_text}.",
            color=discord.Color.green()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(aliases=["dch"], name="deletechannel", description="Deletes a text channel.", with_app_command=True)
    @app_commands.describe(
        channel="The channel to delete. Defaults to current channel."
    )
    @commands.has_permissions(manage_channels=True)
    async def deletechannel(
        self,
        ctx,
        channel: typing.Optional[discord.TextChannel] = commands.parameter(
            default=None,
            description="The channel to delete (mention or ID). Defaults to the current channel.",
        ),
    ):
        target = channel or ctx.channel
        target_name = target.name

        async def perform_delete(_interaction):
            await target.delete(reason=f"Deleted by {ctx.author}")
            return discord.Embed(
                title="🗑️ Channel Deleted",
                description=f"Successfully deleted `{target_name}`.",
                color=discord.Color.red(),
            )

        await confirm_and_run(
            ctx,
            f"Delete {target.mention}? This cannot be undone.",
            perform_delete,
            title="⚠️ Confirm",
            cancel_text="❌ Cancelled",
            timeout_text="⏱️ Timed out",
            failure_text="❌ Channel deletion failed. Check the bot's permissions and confirm the channel still exists.",
        )

    @commands.hybrid_command(aliases=["rch"], name="renamechannel", description="Renames a text channel.", with_app_command=True)
    @app_commands.describe(
        new_name="The new name for the channel. Leave blank and I'll ask you step by step.",
        channel="The channel to rename. Defaults to the current channel."
    )
    @commands.has_permissions(manage_channels=True)
    async def renamechannel(
        self,
        ctx,
        new_name: typing.Optional[str] = commands.parameter(default=None, description="The new channel name. Leave blank and I'll ask you step by step."),
        channel: typing.Optional[discord.TextChannel] = commands.parameter(default=None, description="The channel to rename (mention or ID). Defaults to the current channel.")
    ):
        target_channel = channel or ctx.channel

        # No name given -> ask the questions one by one (30 seconds each)
        if not new_name:
            reply = await ask(
                ctx,
                "Which **channel** do you want to rename? Mention / ID it, or type `skip` for this channel."
            )
            if reply is None:
                return
            if not is_skip(reply.content):
                try:
                    target_channel = await commands.TextChannelConverter().convert(ctx, reply.content.strip())
                except commands.BadArgument:
                    return await ctx.reply(one_line("❌ Couldn't find that channel. Cancelled."), mention_author=False)

            reply = await ask(ctx, f"What do you want the **new name** for {target_channel.mention} to be?")
            if reply is None:
                return
            new_name = reply.content.strip()
            if not new_name:
                return await ctx.reply(one_line("❌ The new name can't be empty. Cancelled."), mention_author=False)

        old_name = target_channel.name

        await target_channel.edit(name=new_name, reason=f"Renamed by {ctx.author}")

        embed = discord.Embed(
            title="✏️ Channel Renamed",
            description=f"Successfully renamed {target_channel.mention} from `{old_name}` to `{new_name}`.",
            color=discord.Color.green()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(aliases=["movech"], name="movechannel", description="Moves the current channel to another category.", with_app_command=True)
    @app_commands.describe(
        category="The ID or name of the category to move this channel to (names can have spaces)."
    )
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    @commands.guild_only()
    @loading
    async def movechannel(
        self,
        ctx,
        *,
        category: str = commands.parameter(description="The ID or name of the category to move this channel to (names can have spaces).")
    ):
        channel = ctx.channel
        if not isinstance(channel, discord.abc.GuildChannel):
            return await ctx.reply(
                embed=discord.Embed(description="❌ This channel can't be moved to a category.", color=discord.Color.red()),
                mention_author=False,
            )

        target, matches = find_category(ctx.guild, category)
        if target is None:
            if matches:
                names = ", ".join(f"**{c.name}** (`{c.id}`)" for c in matches[:10])
                text = f"❌ More than one category matches `{category}`: {names}. Use the ID instead."
            else:
                text = f"❌ Couldn't find a category matching `{category}`."
            return await ctx.reply(embed=discord.Embed(description=text, color=discord.Color.red()), mention_author=False)

        old = channel.category
        if old is not None and old.id == target.id:
            return await ctx.reply(
                embed=discord.Embed(description=f"ℹ️ {channel.mention} is already in **{target.name}**.", color=discord.Color.gold()),
                mention_author=False,
            )

        try:
            await channel.edit(category=target, reason=f"Moved by {ctx.author}")
        except discord.HTTPException as e:
            return await ctx.reply(
                embed=discord.Embed(description=f"❌ Couldn't move the channel: {e.text or e}", color=discord.Color.red()),
                mention_author=False,
            )

        embed = discord.Embed(
            title="📦 Channel Moved",
            description=f"Moved {channel.mention} from **{old.name if old else 'no category'}** to **{target.name}**.",
            color=discord.Color.green()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(aliases=["lch"], name="lockchannel", description="Locks a text channel for a user or role.", with_app_command=True)
    @app_commands.describe(
        target="The member or role to lock. Defaults to everyone.",
        channel="The channel to lock. Defaults to current channel."
    )
    @commands.has_permissions(manage_channels=True)
    async def lockchannel(
        self, 
        ctx, 
        target: typing.Optional[typing.Union[discord.Member, discord.Role]] = commands.parameter(default=None, description="The member or role to lock (mention or ID). Defaults to everyone."),
        channel: typing.Optional[discord.TextChannel] = commands.parameter(default=None, description="The channel to lock (mention or ID). Defaults to the current channel.")
    ):
        target_channel = channel or ctx.channel
        role_or_member = target or ctx.guild.default_role
        await target_channel.set_permissions(role_or_member, send_messages=False)
        
        embed = discord.Embed(
            description=f"🔒 {target_channel.mention} has been locked for {role_or_member.mention}.",
            color=discord.Color.dark_grey()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(aliases=["uch"], name="unlockchannel", description="Unlocks a text channel for a user or role.", with_app_command=True)
    @app_commands.describe(
        target="The member or role to unlock. Defaults to everyone.",
        channel="The channel to unlock. Defaults to current channel."
    )
    @commands.has_permissions(manage_channels=True)
    async def unlockchannel(
        self, 
        ctx, 
        target: typing.Optional[typing.Union[discord.Member, discord.Role]] = commands.parameter(default=None, description="The member or role to unlock (mention or ID). Defaults to everyone."),
        channel: typing.Optional[discord.TextChannel] = commands.parameter(default=None, description="The channel to unlock (mention or ID). Defaults to the current channel.")
    ):
        target_channel = channel or ctx.channel
        role_or_member = target or ctx.guild.default_role
        await target_channel.set_permissions(role_or_member, send_messages=True)
        
        embed = discord.Embed(
            description=f"🔓 {target_channel.mention} has been unlocked for {role_or_member.mention}.",
            color=discord.Color.green()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(aliases=["hch"], name="hidechannel", description="Hides a text channel from a user or role.", with_app_command=True)
    @app_commands.describe(
        target="The member or role to hide the channel from. Defaults to everyone.",
        channel="The channel to hide. Defaults to the current channel."
    )
    @commands.has_permissions(manage_channels=True)
    async def hidechannel(
        self, 
        ctx, 
        target: typing.Optional[typing.Union[discord.Member, discord.Role]] = commands.parameter(default=None, description="The member or role to hide the channel from (mention or ID). Defaults to everyone."),
        channel: typing.Optional[discord.TextChannel] = commands.parameter(default=None, description="The channel to hide (mention or ID). Defaults to the current channel.")
    ):
        target_channel = channel or ctx.channel
        role_or_member = target or ctx.guild.default_role
        await target_channel.set_permissions(role_or_member, view_channel=False)
        
        embed = discord.Embed(
            description=f"🙈 {target_channel.mention} is now hidden from {role_or_member.mention}.",
            color=discord.Color.dark_grey()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(aliases=["uhch"], name="unhidechannel", description="Unhides a text channel for a user or role.", with_app_command=True)
    @app_commands.describe(
        target="The member or role to unhide the channel for. Defaults to everyone.",
        channel="The channel to unhide. Defaults to the current channel."
    )
    @commands.has_permissions(manage_channels=True)
    async def unhidechannel(
        self, 
        ctx, 
        target: typing.Optional[typing.Union[discord.Member, discord.Role]] = commands.parameter(default=None, description="The member or role to unhide the channel for (mention or ID). Defaults to everyone."),
        channel: typing.Optional[discord.TextChannel] = commands.parameter(default=None, description="The channel to unhide (mention or ID). Defaults to the current channel.")
    ):
        target_channel = channel or ctx.channel
        role_or_member = target or ctx.guild.default_role
        await target_channel.set_permissions(role_or_member, view_channel=True)
        
        embed = discord.Embed(
            description=f"👁️ {target_channel.mention} is now visible to {role_or_member.mention}.",
            color=discord.Color.green()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="nuke", description="Nukes a text channel by cloning and replacing it.", with_app_command=True)
    @app_commands.describe(
        channel="The channel to nuke. Defaults to the current channel."
    )
    @commands.has_permissions(manage_channels=True)
    async def nuke(
        self, 
        ctx, 
        channel: typing.Optional[discord.TextChannel] = commands.parameter(default=None, description="The channel to nuke (mention or ID). Defaults to the current channel.")
    ):
        target = channel or ctx.channel
        confirmed = await self.confirm_action(ctx, f"Nuke {target.mention}? This cannot be undone.")
        if not confirmed: 
            return

        pos = target.position
        category = target.category
        new_channel = await target.clone(reason="Channel Nuked")
        await target.delete()
        await new_channel.edit(position=pos, category=category)
        
        embed = discord.Embed(
            title="💥 Channel Nuked",
            description=f"Nuked by {ctx.author.mention}",
            color=discord.Color.red()
        )
        if target != ctx.channel:
            # The confirmation message survived: edit it to show what happened.
            await confirmed.show(embed)
        else:
            await new_channel.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Channels(bot))