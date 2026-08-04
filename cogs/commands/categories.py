import asyncio
import typing
import discord
from discord import app_commands
from discord.ext import commands

class Categories(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            perms = ", ".join([f"`{p}`" for p in error.missing_permissions])
            embed = discord.Embed(
                title="❌ Missing Permissions",
                description=f"You lack the required permissions to use this command: {perms}",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
        elif isinstance(error, commands.BotMissingPermissions):
            perms = ", ".join([f"`{p}`" for p in error.missing_permissions])
            embed = discord.Embed(
                title="❌ Bot Missing Permissions",
                description=f"I am missing permissions to do this. Please give me: {perms}",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
        else:
            raise error

    async def confirm_action(self, ctx, prompt: str) -> bool:
        embed = discord.Embed(
            title="⚠️ Confirmation Required",
            description=f"{prompt}\n\nType `y` to confirm or `n` to cancel.",
            color=discord.Color.gold()
        )
        await ctx.send(embed=embed)
        
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() in ['y', 'n']
        
        try:
            msg = await self.bot.wait_for('message', check=check, timeout=30.0)
            if msg.content.lower() == 'y':
                return True
            else:
                cancel_embed = discord.Embed(title="❌ Action Cancelled", color=discord.Color.red())
                await ctx.send(embed=cancel_embed)
                return False
        except asyncio.TimeoutError:
            timeout_embed = discord.Embed(title="⏱️ Timeout", description="You took too long to reply. Action cancelled.", color=discord.Color.red())
            await ctx.send(embed=timeout_embed)
            return False

    @commands.hybrid_command(aliases=["ccat"], name="createcategory", description="Creates a new category.", with_app_command=True)
    @commands.has_permissions(manage_channels=True)
    async def createcategory(
        self, 
        ctx, 
        name: str = commands.parameter(description="The name of the category to create."), 
        user: typing.Optional[discord.Member] = commands.parameter(default = None, description="Optional member to restrict this category's visibility to.")
    ):
        overwrites = {}
        if user:
            overwrites = {
                ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False, connect=False),
                ctx.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
                user: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }

        category = await ctx.guild.create_category(name=name, overwrites=overwrites)
        
        embed = discord.Embed(
            title="✅ Category Created",
            description=f"Category **{category.name}** has been successfully created.",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(aliases=["dcat"], name="deletecategory", with_app_command=True, description="Deletes a category and all channels inside it.")
    @commands.has_permissions(manage_channels=True)
    async def deletecategory(
        self, 
        ctx, 
        category: typing.Optional[discord.CategoryChannel] = commands.parameter(default = None, description = "The category to delete. Defaults to the current category.")
    ):
        target_category = category or ctx.channel.category
        if not target_category:
            embed = discord.Embed(title="❌ Error", description="Could not find a category to delete.", color=discord.Color.red())
            return await ctx.send(embed=embed)

        confirmed = await self.confirm_action(ctx, f"Are you sure you want to delete the category **{target_category.name}** and ALL channels inside it?")
        if not confirmed: return

        for channel in target_category.channels:
            await channel.delete()
        await target_category.delete()
        
        try:
            embed = discord.Embed(
                title="✅ Category Deleted",
                description=f"Category **{target_category.name}** and all its channels have been deleted.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
        except discord.NotFound:
            pass

    @commands.hybrid_command(aliases=["rcat"], name="renamecategory", with_app_command=True, description="Renames a category.")
    @commands.has_permissions(manage_channels=True)
    async def renamecategory(
        self,
        ctx,
        name: str = commands.parameter(description = "The new name for the category."),
        category: typing.Optional[discord.CategoryChannel] = commands.parameter(default = None, description = "The category to rename. Defaults to the current channel's category.")
    ):
        target_category = category or ctx.channel.category
        if not target_category:
            embed = discord.Embed(title="❌ Error", description="Could not find a category to rename.", color=discord.Color.red())
            return await ctx.send(embed=embed)

        old_name = target_category.name
        await target_category.edit(name=name)
        
        embed = discord.Embed(
            title="✅ Category Renamed",
            description=f"Category **{old_name}** has been renamed to **{target_category.name}**.",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(aliases=["lcat"], name="lockcategory", with_app_command=True, description="Locks a category and syncs its channels.")
    @commands.has_permissions(manage_channels=True)
    async def lockcategory(
        self, 
        ctx, 
        target: typing.Optional[typing.Union[discord.Member, discord.Role]] = commands.parameter(default = None, description = "The member or role to lock. Defaults to everyone."), 
        category: typing.Optional[discord.CategoryChannel] = commands.parameter(default = None, description = "The category to lock. Defaults to current channel's category.")
    ):
        target_category = category or ctx.channel.category
        if not target_category:
            embed = discord.Embed(title="❌ Error", description="Could not find a category to lock.", color=discord.Color.red())
            return await ctx.send(embed=embed)
            
        role_or_member = target or ctx.guild.default_role
        await target_category.set_permissions(role_or_member, send_messages=False, connect=False)
        
        for channel in target_category.channels:
            await channel.edit(sync_permissions=True)
            
        embed = discord.Embed(
            title="🔒 Category Locked",
            description=f"Category **{target_category.name}** has been locked and synced for {role_or_member.mention}.",
            color=discord.Color.dark_grey()
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(aliases=["ucat"], name="unlockcategory", with_app_command=True, description="Unlocks a category and syncs its channels.")
    @commands.has_permissions(manage_channels=True)
    async def unlockcategory(
        self, 
        ctx, 
        target: typing.Optional[typing.Union[discord.Member, discord.Role]] = commands.parameter(default = None, description = "The member or role to unlock. Defaults to everyone."), 
        category: typing.Optional[discord.CategoryChannel] = commands.parameter(default = None, description = "The category to unlock. Defaults to current channel's category.")
    ):
        target_category = category or ctx.channel.category
        if not target_category:
            embed = discord.Embed(title="❌ Error", description="Could not find a category to unlock.", color=discord.Color.red())
            return await ctx.send(embed=embed)
            
        role_or_member = target or ctx.guild.default_role
        await target_category.set_permissions(role_or_member, send_messages=True, connect=True)
        
        for channel in target_category.channels:
            await channel.edit(sync_permissions=True)
            
        embed = discord.Embed(
            title="🔓 Category Unlocked",
            description=f"Category **{target_category.name}** has been unlocked and synced for {role_or_member.mention}.",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(aliases=["hcat"], name="hidecategory", with_app_command=True, description="Hides a category and syncs its channels.")
    @commands.has_permissions(manage_channels=True)
    async def hidecategory(
        self, 
        ctx, 
        target: typing.Optional[typing.Union[discord.Member, discord.Role]] = commands.parameter(default = None, description = "The member or role to hide from. Defaults to everyone."), 
        category: typing.Optional[discord.CategoryChannel] = commands.parameter(default = None, description = "The category to hide. Defaults to current channel's category.")
    ):
        target_category = category or ctx.channel.category
        if not target_category:
            embed = discord.Embed(title="❌ Error", description="Could not find a category to hide.", color=discord.Color.red())
            return await ctx.send(embed=embed)
            
        role_or_member = target or ctx.guild.default_role
        await target_category.set_permissions(role_or_member, view_channel=False)
        
        for channel in target_category.channels:
            await channel.edit(sync_permissions=True)
            
        embed = discord.Embed(
            title="🙈 Category Hidden",
            description=f"Category **{target_category.name}** is now hidden and synced from {role_or_member.mention}.",
            color=discord.Color.dark_grey()
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(aliases=["uhcat"], name="unhidecategory", with_app_command=True, description="Unhides a category and syncs its channels.")
    @commands.has_permissions(manage_channels=True)
    async def unhidecategory(
        self, 
        ctx, 
        target: typing.Optional[typing.Union[discord.Member, discord.Role]] = commands.parameter(default = None, description = "The member or role to unhide for. Defaults to everyone."), 
        category: typing.Optional[discord.CategoryChannel] = commands.parameter(default = None, description = "The category to unhide. Defaults to current channel's category.")
    ):
        target_category = category or ctx.channel.category
        if not target_category:
            embed = discord.Embed(title="❌ Error", description="Could not find a category to unhide.", color=discord.Color.red())
            return await ctx.send(embed=embed)
            
        role_or_member = target or ctx.guild.default_role
        await target_category.set_permissions(role_or_member, view_channel=True)
        
        for channel in target_category.channels:
            await channel.edit(sync_permissions=True)
            
        embed = discord.Embed(
            title="👁️ Category Unhidden",
            description=f"Category **{target_category.name}** is now visible and synced for {role_or_member.mention}.",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(Categories(bot))