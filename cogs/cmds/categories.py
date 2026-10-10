import asyncio
import re
import typing
import discord
from discord import app_commands
from discord.ext import commands

# Imports from views directory
from views.categories_views import CategorySelectView
from views.common_views import confirm
from views.embeds import handle_common_error, send_usage
from views.prompts import ask, is_skip, one_line


class Categories(commands.Cog):

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
            timeout_text="⏱️ Timeout\nYou took too long to reply. Action cancelled.",
        )

    async def _resolve_member_or_role(self, ctx, text: str):
        for converter in (commands.MemberConverter(), commands.RoleConverter()):
            try:
                return await converter.convert(ctx, text)
            except commands.BadArgument:
                continue
        return None

    async def prompt_category_selection(self, ctx, categories):
        view = CategorySelectView(ctx.author, categories)
        embed = discord.Embed(
            title="🔍 Pick a category",
            color=discord.Color.gold(),
        )
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        await view.wait()

        try:
            await msg.delete()
        except discord.HTTPException:
            pass

        return view.selected_category

    async def get_target_category(
        self, ctx, query
    ) -> typing.Optional[discord.CategoryChannel]:
        if not query:
            return ctx.channel.category

        if isinstance(query, discord.CategoryChannel):
            return query

        query_str = str(query).replace("-", " ").lower()

        if query_str.startswith("<#") and query_str.endswith(">"):
            try:
                channel_id = int(query_str[2:-1].replace("!", ""))
                chan = ctx.guild.get_channel(channel_id)
                if isinstance(chan, discord.CategoryChannel):
                    return chan
            except ValueError:
                pass
        elif query_str.isdigit():
            chan = ctx.guild.get_channel(int(query_str))
            if isinstance(chan, discord.CategoryChannel):
                return chan

        matching_categories = [
            cat
            for cat in ctx.guild.categories
            if cat.name.lower() == query_str
        ]

        if len(matching_categories) == 1:
            return matching_categories[0]
        elif len(matching_categories) > 1:
            return await self.prompt_category_selection(
                ctx, matching_categories
            )

        return None

    @commands.hybrid_command(
        aliases=["ccat"],
        name="createcategory",
        description="Creates a new category.",
        with_app_command=True,
    )
    @app_commands.describe(
        name="The name of the category to create. Leave blank and I'll ask you step by step.",
        user=(
            "Optional member or role to restrict this category's visibility to."
        ),
        preaction="Automatically lock or hide the category upon creation.",
    )
    @commands.has_permissions(manage_channels=True)
    async def createcategory(
        self,
        ctx,
        name: typing.Optional[str] = commands.parameter(
            default=None,
            description="The name of the category to create (leave blank to be asked).",
        ),
        user: typing.Optional[typing.Union[discord.Member, discord.Role]] = commands.parameter(
            default=None,
            description=(
                "Optional member or role to restrict this category's visibility to."
            ),
        ),
        preaction: typing.Optional[typing.Literal["prelock", "prehide", "both"]] = commands.parameter(
            default=None,
            description="Apply prelock, prehide, or both upon creation.",
        ),
    ):
        prelock = False
        prehide = False

        if ctx.interaction is None:
            content = ctx.message.content
            parts = content.split(maxsplit=1)
            args_str = parts[1] if len(parts) > 1 else ""

            # Consume --prelock flag
            if "--prelock" in args_str.lower():
                prelock = True
                args_str = re.sub(r"(?i)--prelock", "", args_str)

            # Consume --prehide flag
            if "--prehide" in args_str.lower():
                prehide = True
                args_str = re.sub(r"(?i)--prehide", "", args_str)

            # Extract user / role mention if present
            if ctx.message.mentions:
                user = ctx.message.mentions[-1]
                args_str = re.sub(rf"<@!?{user.id}>", "", args_str)
            elif ctx.message.role_mentions:
                user = ctx.message.role_mentions[-1]
                args_str = re.sub(rf"<@&{user.id}>", "", args_str)

            name = args_str.replace("-", " ").strip()
        else:
            name = (name or "").replace("-", " ").strip()
            if preaction in ("prelock", "both"):
                prelock = True
            if preaction in ("prehide", "both"):
                prehide = True

        # No name given -> ask the questions one by one (30 seconds each)
        if not name:
            reply = await ask(ctx, "What do you want the **category name** to be?")
            if reply is None:
                return
            name = reply.content.strip().replace("-", " ")
            if not name:
                return await ctx.reply(
                    one_line("❌ The category name can't be empty. Cancelled."), mention_author=False
                )

            reply = await ask(
                ctx,
                "Which **user or role** should this category be restricted to "
                "(only they can see it)? Mention / ID / name them, or type `skip` for no restriction.",
            )
            if reply is None:
                return
            if not is_skip(reply.content):
                user = await self._resolve_member_or_role(ctx, reply.content.strip())
                if user is None:
                    return await ctx.reply(
                        one_line("❌ Couldn't find that user or role. Cancelled."), mention_author=False
                    )

            if not (prelock or prehide):
                reply = await ask(
                    ctx,
                    "Which **preaction** do you want? `prelock`, `prehide`, `both`, or `none`.",
                )
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
                    return await ctx.reply(
                        one_line("❌ Invalid preaction. Cancelled."), mention_author=False
                    )

        overwrites = {}

        # Set specific user permissions if provided
        if user:
            overwrites = {
                ctx.guild.default_role: discord.PermissionOverwrite(
                    read_messages=False, connect=False
                ),
                ctx.guild.me: discord.PermissionOverwrite(
                    read_messages=True,
                    send_messages=True,
                    manage_channels=True,
                ),
                user: discord.PermissionOverwrite(
                    read_messages=True, send_messages=True
                ),
            }

        # Apply prelock / prehide overrides to default role
        if prelock or prehide:
            default_overwrite = overwrites.get(
                ctx.guild.default_role, discord.PermissionOverwrite()
            )
            if prelock:
                default_overwrite.send_messages = False
                default_overwrite.connect = False
            if prehide:
                default_overwrite.view_channel = False

            overwrites[ctx.guild.default_role] = default_overwrite

            # Ensure bot keeps permissions to avoid locking itself out
            if ctx.guild.me not in overwrites:
                overwrites[ctx.guild.me] = discord.PermissionOverwrite(
                    read_messages=True,
                    send_messages=True,
                    manage_channels=True,
                )

        category = await ctx.guild.create_category(
            name=name, overwrites=overwrites
        )

        status_flags = []
        if prelock:
            status_flags.append("🔒 Locked")
        if prehide:
            status_flags.append("🙈 Hidden")
        status_text = f" ({', '.join(status_flags)})" if status_flags else ""

        embed = discord.Embed(
            title="✅ Category Created",
            description=(
                f"Category **{category.name}** has been successfully"
                f" created{status_text}."
            ),
            color=discord.Color.green(),
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(
        aliases=["dcat"],
        name="deletecategory",
        with_app_command=True,
        description="Deletes a category and all channels inside it.",
    )
    @app_commands.describe(
        category=(
            "The category to delete. Defaults to the current category."
        )
    )
    @commands.has_permissions(manage_channels=True)
    async def deletecategory(
        self,
        ctx,
        category: typing.Optional[str] = commands.parameter(
            default=None,
            description=(
                "The category to delete. Defaults to the current category."
            ),
        ),
    ):
        target_category = await self.get_target_category(ctx, category)
        if not target_category:
            embed = discord.Embed(
                title="❌ Error",
                description="Could not find a category to delete.",
                color=discord.Color.red(),
            )
            return await ctx.reply(embed=embed, mention_author=False)

        confirmed = await self.confirm_action(
            ctx,
            f"Delete category **{target_category.name}** and all its channels?",
        )
        if not confirmed:
            return

        for channel in target_category.channels:
            await channel.delete()
        await target_category.delete()

        try:
            embed = discord.Embed(
                title="✅ Category Deleted",
                description=(
                    f"Category **{target_category.name}** and all its channels"
                    " have been deleted."
                ),
                color=discord.Color.red(),
            )
            await confirmed.show(embed)
        except discord.NotFound:
            pass

    @commands.hybrid_command(
        aliases=["rcat"],
        name="renamecategory",
        with_app_command=True,
        description="Renames a category.",
    )
    @app_commands.describe(
        name="The new name for the category. Leave blank and I'll ask you step by step.",
        category=(
            "The category to rename. Defaults to the current channel's category."
        ),
    )
    @commands.has_permissions(manage_channels=True)
    async def renamecategory(
        self,
        ctx,
        name: typing.Optional[str] = commands.parameter(
            default=None,
            description="The new name for the category (leave blank to be asked).",
        ),
        category: typing.Optional[str] = commands.parameter(
            default=None,
            description=(
                "The category to rename. Defaults to the current channel's"
                " category."
            ),
        ),
    ):
        # No name given -> ask the questions one by one (30 seconds each)
        if not name:
            reply = await ask(
                ctx,
                "Which **category** do you want to rename? Mention / ID / name it, "
                "or type `skip` for this channel's category.",
            )
            if reply is None:
                return
            category = None if is_skip(reply.content) else reply.content.strip()

            target_category = await self.get_target_category(ctx, category)
            if not target_category:
                return await ctx.reply(
                    one_line("❌ Couldn't find that category. Cancelled."), mention_author=False
                )

            reply = await ask(
                ctx,
                f"What do you want the **new name** for **{target_category.name}** to be?",
            )
            if reply is None:
                return
            name = reply.content.strip()
            if not name:
                return await ctx.reply(
                    one_line("❌ The new name can't be empty. Cancelled."), mention_author=False
                )
        else:
            target_category = await self.get_target_category(ctx, category)
        if not target_category:
            embed = discord.Embed(
                title="❌ Error",
                description="Could not find a category to rename.",
                color=discord.Color.red(),
            )
            return await ctx.reply(embed=embed, mention_author=False)

        new_name = name.replace("-", " ")
        old_name = target_category.name
        await target_category.edit(name=new_name)

        embed = discord.Embed(
            title="✅ Category Renamed",
            description=(
                f"Category **{old_name}** has been renamed to"
                f" **{target_category.name}**."
            ),
            color=discord.Color.green(),
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(
        aliases=["lcat"],
        name="lockcategory",
        with_app_command=True,
        description="Locks a category and syncs its channels.",
    )
    @app_commands.describe(
        target="The member or role to lock. Defaults to everyone.",
        category=(
            "The category to lock. Defaults to current channel's category."
        ),
    )
    @commands.has_permissions(manage_channels=True)
    async def lockcategory(
        self,
        ctx,
        target: typing.Optional[
            typing.Union[discord.Member, discord.Role]
        ] = commands.parameter(
            default=None,
            description="The member or role to lock. Defaults to everyone.",
        ),
        category: typing.Optional[str] = commands.parameter(
            default=None,
            description=(
                "The category to lock. Defaults to current channel's category."
            ),
        ),
    ):
        target_category = await self.get_target_category(ctx, category)
        if not target_category:
            embed = discord.Embed(
                description="❌ Could not find a category to lock.",
                color=discord.Color.red(),
            )
            return await ctx.reply(embed=embed, mention_author=False)

        role_or_member = target or ctx.guild.default_role
        await target_category.set_permissions(
            role_or_member, send_messages=False, connect=False
        )

        for channel in target_category.channels:
            await channel.edit(sync_permissions=True)

        embed = discord.Embed(
            description=(
                f"🔒 Category **{target_category.name}** has been locked for"
                f" {role_or_member.mention}."
            ),
            color=discord.Color.dark_grey(),
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(
        aliases=["ucat"],
        name="unlockcategory",
        with_app_command=True,
        description="Unlocks a category and syncs its channels.",
    )
    @app_commands.describe(
        target="The member or role to unlock. Defaults to everyone.",
        category=(
            "The category to unlock. Defaults to current channel's category."
        ),
    )
    @commands.has_permissions(manage_channels=True)
    async def unlockcategory(
        self,
        ctx,
        target: typing.Optional[
            typing.Union[discord.Member, discord.Role]
        ] = commands.parameter(
            default=None,
            description="The member or role to unlock. Defaults to everyone.",
        ),
        category: typing.Optional[str] = commands.parameter(
            default=None,
            description=(
                "The category to unlock. Defaults to current channel's category."
            ),
        ),
    ):
        target_category = await self.get_target_category(ctx, category)
        if not target_category:
            embed = discord.Embed(
                description="❌ Could not find a category to unlock.",
                color=discord.Color.red(),
            )
            return await ctx.reply(embed=embed, mention_author=False)

        role_or_member = target or ctx.guild.default_role
        await target_category.set_permissions(
            role_or_member, send_messages=True, connect=True
        )

        for channel in target_category.channels:
            await channel.edit(sync_permissions=True)

        embed = discord.Embed(
            description=(
                f"🔓 Category **{target_category.name}** has been unlocked for"
                f" {role_or_member.mention}."
            ),
            color=discord.Color.green(),
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(
        aliases=["hcat"],
        name="hidecategory",
        with_app_command=True,
        description="Hides a category and syncs its channels.",
    )
    @app_commands.describe(
        target="The member or role to hide from. Defaults to everyone.",
        category=(
            "The category to hide. Defaults to current channel's category."
        ),
    )
    @commands.has_permissions(manage_channels=True)
    async def hidecategory(
        self,
        ctx,
        target: typing.Optional[
            typing.Union[discord.Member, discord.Role]
        ] = commands.parameter(
            default=None,
            description="The member or role to hide from. Defaults to everyone.",
        ),
        category: typing.Optional[str] = commands.parameter(
            default=None,
            description=(
                "The category to hide. Defaults to current channel's category."
            ),
        ),
    ):
        target_category = await self.get_target_category(ctx, category)
        if not target_category:
            embed = discord.Embed(
                description="❌ Could not find a category to hide.",
                color=discord.Color.red(),
            )
            return await ctx.reply(embed=embed, mention_author=False)

        role_or_member = target or ctx.guild.default_role
        await target_category.set_permissions(
            role_or_member, view_channel=False
        )

        for channel in target_category.channels:
            await channel.edit(sync_permissions=True)

        embed = discord.Embed(
            description=(
                f"🙈 Category **{target_category.name}** is now hidden from"
                f" {role_or_member.mention}."
            ),
            color=discord.Color.dark_grey(),
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(
        aliases=["uhcat"],
        name="unhidecategory",
        with_app_command=True,
        description="Unhides a category and syncs its channels.",
    )
    @app_commands.describe(
        target="The member or role to unhide for. Defaults to everyone.",
        category=(
            "The category to unhide. Defaults to current channel's category."
        ),
    )
    @commands.has_permissions(manage_channels=True)
    async def unhidecategory(
        self,
        ctx,
        target: typing.Optional[
            typing.Union[discord.Member, discord.Role]
        ] = commands.parameter(
            default=None,
            description="The member or role to unhide for. Defaults to everyone.",
        ),
        category: typing.Optional[str] = commands.parameter(
            default=None,
            description=(
                "The category to unhide. Defaults to current channel's category."
            ),
        ),
    ):
        target_category = await self.get_target_category(ctx, category)
        if not target_category:
            embed = discord.Embed(
                description="❌ Could not find a category to unhide.",
                color=discord.Color.red(),
            )
            return await ctx.reply(embed=embed, mention_author=False)

        role_or_member = target or ctx.guild.default_role
        await target_category.set_permissions(
            role_or_member, view_channel=True
        )

        for channel in target_category.channels:
            await channel.edit(sync_permissions=True)

        embed = discord.Embed(
            description=(
                f"👁️ Category **{target_category.name}** is now visible to"
                f" {role_or_member.mention}."
            ),
            color=discord.Color.green(),
        )
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot):
    await bot.add_cog(Categories(bot))