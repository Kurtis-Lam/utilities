import asyncio
import re
import typing
import discord
from discord import app_commands
from discord.ext import commands

# Imports from views directory
from views.categoriesview import CategorySelectView
from views.common import ConfirmView


class Categories(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            perms = ", ".join([f"`{p}`" for p in error.missing_permissions])
            embed = discord.Embed(
                title="❌ Missing Permissions",
                description=(
                    "You lack the required permissions to use this command:"
                    f" {perms}"
                ),
                color=discord.Color.red(),
            )
            await ctx.send(embed=embed)
        elif isinstance(error, commands.BotMissingPermissions):
            perms = ", ".join([f"`{p}`" for p in error.missing_permissions])
            embed = discord.Embed(
                title="❌ Bot Missing Permissions",
                description=(
                    f"I am missing permissions to do this. Please give me: {perms}"
                ),
                color=discord.Color.red(),
            )
            await ctx.send(embed=embed)
        else:
            raise error

    async def confirm_action(self, ctx, prompt: str) -> bool:
        view = ConfirmView(ctx.author)
        embed = discord.Embed(
            title="⚠️ Confirmation Required",
            description=f"{prompt}\n\nClick a button below to confirm or cancel.",
            color=discord.Color.gold(),
        )
        msg = await ctx.send(embed=embed, view=view)
        await view.wait()

        if view.value is True:
            try:
                await msg.edit(view=None)
            except discord.HTTPException:
                pass
            return True
        elif view.value is False:
            cancel_embed = discord.Embed(
                title="❌ Action Cancelled", color=discord.Color.red()
            )
            try:
                await msg.edit(embed=cancel_embed, view=None)
            except discord.HTTPException:
                pass
            return False
        else:
            timeout_embed = discord.Embed(
                title="⏱️ Timeout",
                description="You took too long to reply. Action cancelled.",
                color=discord.Color.red(),
            )
            try:
                await msg.edit(embed=timeout_embed, view=None)
            except discord.HTTPException:
                pass
            return False

    async def prompt_category_selection(self, ctx, categories):
        view = CategorySelectView(ctx.author, categories)
        embed = discord.Embed(
            title="🔍 Multiple Categories Found",
            description=(
                "Found multiple categories matching that name. Please select"
                " one from the dropdown below:"
            ),
            color=discord.Color.gold(),
        )
        msg = await ctx.send(embed=embed, view=view)
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
        name="The name of the category to create.",
        user=(
            "Optional member to restrict this category's visibility to."
        ),
        preaction="Automatically lock or hide the category upon creation.",
    )
    @commands.has_permissions(manage_channels=True)
    async def createcategory(
        self,
        ctx,
        name: str = commands.parameter(
            description="The name of the category to create."
        ),
        user: typing.Optional[discord.Member] = commands.parameter(
            default=None,
            description=(
                "Optional member to restrict this category's visibility to."
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

            # Extract user mention if present
            if ctx.message.mentions:
                user = ctx.message.mentions[-1]
                args_str = re.sub(rf"<@!?{user.id}>", "", args_str)

            name = args_str.replace("-", " ").strip()
            if not name:
                embed = discord.Embed(
                    title="❌ Error",
                    description="Please provide a name for the category.",
                    color=discord.Color.red(),
                )
                return await ctx.send(embed=embed)
        else:
            name = name.replace("-", " ")
            if preaction in ("prelock", "both"):
                prelock = True
            if preaction in ("prehide", "both"):
                prehide = True

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
        await ctx.send(embed=embed)

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
            return await ctx.send(embed=embed)

        confirmed = await self.confirm_action(
            ctx,
            f"Are you sure you want to delete the category **{target_category.name}** and ALL channels inside it?",
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
            await ctx.send(embed=embed)
        except discord.NotFound:
            pass

    @commands.hybrid_command(
        aliases=["rcat"],
        name="renamecategory",
        with_app_command=True,
        description="Renames a category.",
    )
    @app_commands.describe(
        name="The new name for the category.",
        category=(
            "The category to rename. Defaults to the current channel's category."
        ),
    )
    @commands.has_permissions(manage_channels=True)
    async def renamecategory(
        self,
        ctx,
        name: str = commands.parameter(
            description="The new name for the category."
        ),
        category: typing.Optional[str] = commands.parameter(
            default=None,
            description=(
                "The category to rename. Defaults to the current channel's"
                " category."
            ),
        ),
    ):
        target_category = await self.get_target_category(ctx, category)
        if not target_category:
            embed = discord.Embed(
                title="❌ Error",
                description="Could not find a category to rename.",
                color=discord.Color.red(),
            )
            return await ctx.send(embed=embed)

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
        await ctx.send(embed=embed)

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
                title="❌ Error",
                description="Could not find a category to lock.",
                color=discord.Color.red(),
            )
            return await ctx.send(embed=embed)

        role_or_member = target or ctx.guild.default_role
        await target_category.set_permissions(
            role_or_member, send_messages=False, connect=False
        )

        for channel in target_category.channels:
            await channel.edit(sync_permissions=True)

        embed = discord.Embed(
            title="🔒 Category Locked",
            description=(
                f"Category **{target_category.name}** has been locked and"
                f" synced for {role_or_member.mention}."
            ),
            color=discord.Color.dark_grey(),
        )
        await ctx.send(embed=embed)

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
                title="❌ Error",
                description="Could not find a category to unlock.",
                color=discord.Color.red(),
            )
            return await ctx.send(embed=embed)

        role_or_member = target or ctx.guild.default_role
        await target_category.set_permissions(
            role_or_member, send_messages=True, connect=True
        )

        for channel in target_category.channels:
            await channel.edit(sync_permissions=True)

        embed = discord.Embed(
            title="🔓 Category Unlocked",
            description=(
                f"Category **{target_category.name}** has been unlocked and"
                f" synced for {role_or_member.mention}."
            ),
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)

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
                title="❌ Error",
                description="Could not find a category to hide.",
                color=discord.Color.red(),
            )
            return await ctx.send(embed=embed)

        role_or_member = target or ctx.guild.default_role
        await target_category.set_permissions(
            role_or_member, view_channel=False
        )

        for channel in target_category.channels:
            await channel.edit(sync_permissions=True)

        embed = discord.Embed(
            title="🙈 Category Hidden",
            description=(
                f"Category **{target_category.name}** is now hidden and synced"
                f" from {role_or_member.mention}."
            ),
            color=discord.Color.dark_grey(),
        )
        await ctx.send(embed=embed)

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
                title="❌ Error",
                description="Could not find a category to unhide.",
                color=discord.Color.red(),
            )
            return await ctx.send(embed=embed)

        role_or_member = target or ctx.guild.default_role
        await target_category.set_permissions(
            role_or_member, view_channel=True
        )

        for channel in target_category.channels:
            await channel.edit(sync_permissions=True)

        embed = discord.Embed(
            title="👁️ Category Unhidden",
            description=(
                f"Category **{target_category.name}** is now visible and"
                f" synced for {role_or_member.mention}."
            ),
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Categories(bot))