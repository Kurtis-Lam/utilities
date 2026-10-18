from __future__ import annotations

import io
import math
import random
import string
import time

import discord
from PIL import Image, ImageDraw, ImageFont
from discord.ui import Button, ChannelSelect, Modal, RoleSelect, TextInput

from views.common_views import EmbedLayout, error_embed, make_embed, success_embed

# Characters that are difficult to confuse in small or distorted text are excluded.
CAPTCHA_ALPHABET = "abcdefghjkmnprstuvwxyz"


def generate_captcha_text(length: int) -> str:
    length = max(3, min(12, int(length)))
    return "".join(random.SystemRandom().choice(CAPTCHA_ALPHABET) for _ in range(length))


def make_captcha_image(text: str) -> discord.File:
    rng = random.SystemRandom()
    width, height = 620, 170
    image = Image.new("RGB", (width, height), (246, 248, 255))
    draw = ImageDraw.Draw(image)
    cell = (width - 48) / max(1, len(text))
    font_size = max(34, min(80, int(cell * 0.72)))
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except OSError:
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", font_size)
        except OSError:
            font = ImageFont.load_default()

    # Keep the background distortion subtle enough that every letter remains readable.
    for _ in range(12):
        x1, y1 = rng.randrange(width), rng.randrange(height)
        x2 = min(width - 1, max(0, x1 + rng.randint(-150, 150)))
        y2 = min(height - 1, max(0, y1 + rng.randint(-35, 35)))
        draw.line((x1, y1, x2, y2), fill=(rng.randrange(155, 215), rng.randrange(155, 215), rng.randrange(155, 215)), width=1)
    for _ in range(100):
        x, y = rng.randrange(width), rng.randrange(height)
        draw.point((x, y), fill=(rng.randrange(115, 210), rng.randrange(115, 210), rng.randrange(115, 210)))
    for phase in (0.0, 2.2):
        points = [
            (x, int(height / 2 + math.sin(x / rng.randint(45, 65) + phase) * rng.randint(9, 20)))
            for x in range(0, width, 10)
        ]
        draw.line(points, fill=(145, 160, 195), width=1)

    resampling = getattr(Image, "Resampling", Image)
    for index, char in enumerate(text):
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1), (0, 0, 0, 0)))
        try:
            bounds = probe.textbbox((0, 0), char, font=font, stroke_width=1)
        except Exception:
            bounds = (0, 0, font_size, font_size)
        glyph_width = max(1, bounds[2] - bounds[0])
        glyph_height = max(1, bounds[3] - bounds[1])
        layer = Image.new("RGBA", (glyph_width + 18, glyph_height + 18), (0, 0, 0, 0))
        layer_draw = ImageDraw.Draw(layer)
        try:
            layer_draw.text((9 - bounds[0], 9 - bounds[1]), char, font=font, fill=(18, 28, 78, 255), stroke_width=1, stroke_fill=(18, 28, 78, 255))
        except TypeError:
            layer_draw.text((9 - bounds[0], 9 - bounds[1]), char, font=font, fill=(18, 28, 78, 255))
        rotated = layer.rotate(rng.randint(-13, 13), resample=resampling.BICUBIC, expand=True)
        center_x = 24 + (index + 0.5) * cell
        x = int(center_x - rotated.width / 2)
        y = int(height / 2 - rotated.height / 2 + rng.randint(-13, 13))
        image.paste(rotated, (x, y), rotated)

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return discord.File(output, filename="verification.png")


def verification_config_embed(config: dict, guild: discord.Guild | None) -> discord.Embed:
    channel_id = config.get("verify_channel_id")
    role_id = config.get("unverified_role_id")
    embed = make_embed("🛡️ Verification System", "Set the verification channel and role, configure the CAPTCHA, then post the persistent verification panel.")
    embed.add_field(name="Verification channel", value=f"<#{channel_id}>" if channel_id else "*Not set*", inline=True)
    embed.add_field(name="Unverified role", value=f"<@&{role_id}>" if role_id else "*Not set*", inline=True)
    embed.add_field(name="Code length", value=str(config.get("captcha_length", 6)), inline=True)
    embed.add_field(name="Attempts", value=str(config.get("max_attempts", 3)), inline=True)
    embed.add_field(name="Challenge expiry", value=f"{config.get('captcha_expiry_seconds', 180)} sec", inline=True)
    embed.add_field(name="Panel message", value="Configured" if config.get("verify_panel_message_id") else "Not posted", inline=True)
    embed.add_field(name="Panel title", value=str(config.get("panel_title", "Verify to access the server"))[:256], inline=False)
    embed.add_field(name="Prompt", value=str(config.get("prompt_description", "Complete this CAPTCHA to receive access to the rest of the server."))[:1000], inline=False)
    embed.set_footer(text=f"{guild.name if guild else 'This server'} • Verification role is protected in autoroles")
    return embed


class VerificationPanelView(discord.ui.View):
    def __init__(self, cog, button_label: str = "Verify"):
        super().__init__(timeout=None)
        self.cog = cog
        button = Button(label=(button_label or "Verify")[:80], style=discord.ButtonStyle.success, emoji="✅", custom_id="verify:start")
        button.callback = self.verify_callback
        self.add_item(button)

    async def verify_callback(self, interaction: discord.Interaction):
        await self.cog.start_captcha(interaction)


class CaptchaPromptView(discord.ui.View):
    def __init__(self, cog, expected: str, config: dict, user_id: int, expires_at: float):
        super().__init__(timeout=max(1, expires_at - time.time()))
        self.cog, self.expected, self.config = cog, expected, config
        self.user_id, self.expires_at = user_id, expires_at
        self.attempts = 0
        self.message: discord.Message | None = None
        self.enter_button = Button(label=str(config.get("enter_button_label", "Enter the text"))[:80], style=discord.ButtonStyle.primary, emoji="⌨️")
        self.enter_button.callback = self.open_modal
        self.add_item(self.enter_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=error_embed("This CAPTCHA belongs to another member."), ephemeral=True)
            return False
        if time.time() > self.expires_at:
            await interaction.response.send_message(embed=error_embed("This CAPTCHA expired. Press Verify again to get a new one."), ephemeral=True)
            self.stop()
            return False
        return True

    async def open_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CaptchaAnswerModal(self))

    async def on_timeout(self):
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass


class CaptchaAnswerModal(Modal, title="Server Verification"):
    answer = TextInput(label="Type the lowercase letters shown", required=True, min_length=3, max_length=12, placeholder="lowercase letters only")

    def __init__(self, prompt_view: CaptchaPromptView):
        super().__init__()
        self.prompt_view = prompt_view
        self.answer.max_length = max(3, min(12, int(prompt_view.config.get("captcha_length", 6))))
        self.answer.min_length = self.answer.max_length

    async def on_submit(self, interaction: discord.Interaction):
        prompt = self.prompt_view
        if time.time() > prompt.expires_at:
            prompt.stop()
            return await interaction.response.send_message(embed=error_embed("This CAPTCHA expired. Press Verify again to get a new one."), ephemeral=True)
        supplied = self.answer.value.strip()
        if not supplied.isascii() or not supplied.isalpha() or supplied.lower() != supplied:
            prompt.attempts += 1
            return await self._wrong(interaction, "Use lowercase English letters only.")
        if supplied != prompt.expected:
            prompt.attempts += 1
            return await self._wrong(interaction, "That text does not match. Check the CAPTCHA and try again.")
        if interaction.guild is None:
            return await interaction.response.send_message(embed=error_embed("Verification must be completed in the server."), ephemeral=True)
        try:
            await prompt.cog.complete_verification(interaction.guild, interaction.user, prompt.config)
        except (discord.Forbidden, discord.HTTPException, ValueError) as exc:
            return await interaction.response.send_message(embed=error_embed(f"Could not finish verification: {exc}"), ephemeral=True)
        prompt.stop()
        if prompt.message is not None:
            try:
                await prompt.message.edit(view=None, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass
        message = str(prompt.config.get("success_message", "✅ Verification complete. You now have access to the server."))
        await interaction.response.send_message(embed=make_embed("✅ Verified", message), ephemeral=True)

    async def _wrong(self, interaction: discord.Interaction, message: str):
        prompt = self.prompt_view
        if prompt.attempts >= max(1, min(10, int(prompt.config.get("max_attempts", 3)))):
            prompt.stop()
            if prompt.message is not None:
                try:
                    await prompt.message.edit(view=None, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    pass
            text = str(prompt.config.get("failure_message", "Too many incorrect attempts. Press Verify again for a new CAPTCHA."))
        else:
            text = f"{message} Attempts remaining: `{max(0, int(prompt.config.get('max_attempts', 3)) - prompt.attempts)}`."
        await interaction.response.send_message(embed=error_embed(text), ephemeral=True)


class VerificationConfigView(EmbedLayout):
    def __init__(self, cog, config: dict, guild: discord.Guild, author_id: int):
        super().__init__(verification_config_embed(config, guild), author_id=author_id, timeout=300)
        self.cog, self.config, self.guild = cog, config, guild
        self.channel_select = ChannelSelect(channel_types=[discord.ChannelType.text], placeholder="Choose verification channel")
        self.channel_select.callback = self.select_channel
        self.role_select = RoleSelect(placeholder="Choose unverified role", min_values=1, max_values=1)
        self.role_select.callback = self.select_role
        self.create_role_btn = Button(label="Create Unverified Role", emoji="➕", style=discord.ButtonStyle.primary)
        self.create_role_btn.callback = self.create_role
        self.settings_btn = Button(label="CAPTCHA Settings", emoji="⚙️", style=discord.ButtonStyle.secondary)
        self.settings_btn.callback = self.settings
        self.messages_btn = Button(label="Messages", emoji="💬", style=discord.ButtonStyle.secondary)
        self.messages_btn.callback = self.messages
        self.labels_btn = Button(label="Button Labels", emoji="🏷️", style=discord.ButtonStyle.secondary)
        self.labels_btn.callback = self.button_labels
        self.apply_btn = Button(label="Apply All Permissions", emoji="🔐", style=discord.ButtonStyle.danger)
        self.apply_btn.callback = self.apply_permissions
        self.post_btn = Button(label="Post / Refresh Panel", emoji="📨", style=discord.ButtonStyle.success)
        self.post_btn.callback = self.post_panel
        self.test_btn = Button(label="Test CAPTCHA", emoji="🧪", style=discord.ButtonStyle.success)
        self.test_btn.callback = self.test_captcha
        self.render()

    def rows(self):
        return [
            [self.channel_select],
            [self.role_select],
            [self.create_role_btn, self.settings_btn, self.messages_btn, self.apply_btn],
            [self.labels_btn, self.post_btn, self.test_btn],
        ]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.author_id is not None and interaction.user.id != self.author_id:
            await interaction.response.send_message(embed=error_embed("This verification panel belongs to another administrator."), ephemeral=True)
            return False
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(embed=error_embed("Administrator permission is required."), ephemeral=True)
            return False
        return await super().interaction_check(interaction)

    async def refresh_message(self):
        self.config = await self.cog.get_config(self.guild.id)
        self.set_embed(verification_config_embed(self.config, self.guild))
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def select_channel(self, interaction: discord.Interaction):
        channel = self.channel_select.values[0]
        try:
            await self.cog.set_verification_channel(interaction.guild, channel)
        except (RuntimeError, ValueError, discord.Forbidden, discord.HTTPException) as exc:
            return await interaction.response.send_message(embed=error_embed(f"Could not set the verification channel: {exc}"), ephemeral=True)
        self.config = await self.cog.get_config(interaction.guild.id)
        self.set_embed(verification_config_embed(self.config, interaction.guild))
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(embed=success_embed(f"Verification channel set to {channel.mention}; channel permissions were updated."), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def select_role(self, interaction: discord.Interaction):
        role = self.role_select.values[0]
        try:
            await self.cog.set_unverified_role(interaction.guild, role)
        except (RuntimeError, ValueError, discord.Forbidden, discord.HTTPException) as exc:
            return await interaction.response.send_message(embed=error_embed(f"Could not set the unverified role: {exc}"), ephemeral=True)
        self.config = await self.cog.get_config(interaction.guild.id)
        self.set_embed(verification_config_embed(self.config, interaction.guild))
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(embed=success_embed(f"Unverified role set to {role.mention} and protected from autorole removal."), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def create_role(self, interaction: discord.Interaction):
        role = await self.cog.create_unverified_role(interaction.guild)
        if role is None:
            return await interaction.response.send_message(embed=error_embed("Could not create the Unverified role. Check the bot's Manage Roles permission and role hierarchy."), ephemeral=True)
        await self.refresh_message()
        await interaction.response.send_message(embed=success_embed(f"Created {role.mention} and applied it as the protected unverified role."), ephemeral=True)

    async def settings(self, interaction: discord.Interaction):
        await interaction.response.send_modal(VerificationCaptchaSettingsModal(self))

    async def messages(self, interaction: discord.Interaction):
        await interaction.response.send_modal(VerificationTextSettingsModal(self))

    async def button_labels(self, interaction: discord.Interaction):
        await interaction.response.send_modal(VerificationButtonLabelsModal(self))

    async def apply_permissions(self, interaction: discord.Interaction):
        config = await self.cog.get_config(interaction.guild.id)
        if not config.get("verify_channel_id") or not config.get("unverified_role_id"):
            return await interaction.response.send_message(embed=error_embed("Choose a verification channel and role first."), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.cog.apply_verification_permissions(interaction.guild, config)
        await self.refresh_message()
        await interaction.followup.send(embed=success_embed(f"Permissions applied to {result['updated']} category/channel(s)." + (f"\nWarnings: {result['failed']} channel update(s) failed; check bot permissions." if result['failed'] else "")), ephemeral=True)

    async def post_panel(self, interaction: discord.Interaction):
        config = await self.cog.get_config(interaction.guild.id)
        channel = interaction.guild.get_channel(int(config.get("verify_channel_id") or 0))
        if not isinstance(channel, (discord.TextChannel, discord.NewsChannel)):
            return await interaction.response.send_message(embed=error_embed("Set a text verification channel first."), ephemeral=True)
        message = await self.cog.post_verification_panel(interaction.guild, channel, config)
        if message is None:
            return await interaction.response.send_message(embed=error_embed("Could not post the verification panel. Check channel permissions."), ephemeral=True)
        await self.refresh_message()
        await interaction.response.send_message(embed=success_embed(f"Verification panel posted in {channel.mention}."), ephemeral=True)

    async def test_captcha(self, interaction: discord.Interaction):
        await self.cog.start_captcha(interaction, test_only=True)

    async def on_timeout(self):
        self.disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass


class VerificationCaptchaSettingsModal(Modal, title="CAPTCHA Settings"):
    code_length = TextInput(label="Code length (3–12 lowercase letters)", required=True, max_length=2)
    attempts = TextInput(label="Maximum attempts (1–10)", required=True, max_length=2)
    expiry = TextInput(label="CAPTCHA expiry in seconds (30–600)", required=True, max_length=3)
    panel_title = TextInput(label="Public verification panel title", required=True, max_length=256)
    prompt_title = TextInput(label="Ephemeral CAPTCHA title", required=True, max_length=256)

    def __init__(self, view: VerificationConfigView):
        super().__init__()
        self.view_instance = view
        c = view.config
        self.code_length.default = str(c.get("captcha_length", 6))
        self.attempts.default = str(c.get("max_attempts", 3))
        self.expiry.default = str(c.get("captcha_expiry_seconds", 180))
        self.panel_title.default = str(c.get("panel_title", "Verify to access the server"))[:256]
        self.prompt_title.default = str(c.get("captcha_title", "Complete verification"))[:256]

    async def on_submit(self, interaction: discord.Interaction):
        try:
            length, attempts, expiry = int(self.code_length.value), int(self.attempts.value), int(self.expiry.value)
            if not 3 <= length <= 12 or not 1 <= attempts <= 10 or not 30 <= expiry <= 600:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(embed=error_embed("Code length must be 3–12, attempts 1–10, and expiry 30–600 seconds."), ephemeral=True)
        await self.view_instance.cog.save_config_fields(self.view_instance.guild.id, {
            "captcha_length": length,
            "max_attempts": attempts,
            "captcha_expiry_seconds": expiry,
            "panel_title": self.panel_title.value,
            "captcha_title": self.prompt_title.value,
        })
        await interaction.response.send_message(embed=success_embed("CAPTCHA settings saved."), ephemeral=True)
        await self.view_instance.refresh_message()


class VerificationTextSettingsModal(Modal, title="Verification Messages & Labels"):
    panel_description = TextInput(label="Public panel description", style=discord.TextStyle.paragraph, required=True, max_length=3000)
    prompt_description = TextInput(label="Ephemeral CAPTCHA instructions", style=discord.TextStyle.paragraph, required=True, max_length=1000)
    success_message = TextInput(label="Success message", style=discord.TextStyle.paragraph, required=True, max_length=1000)
    failure_message = TextInput(label="Too-many-attempts message", style=discord.TextStyle.paragraph, required=True, max_length=1000)

    def __init__(self, view: VerificationConfigView):
        super().__init__()
        self.view_instance = view
        c = view.config
        self.panel_description.default = str(c.get("panel_description", "Press Verify and enter the lowercase letters shown in the CAPTCHA to unlock the server."))[:3000]
        self.prompt_description.default = str(c.get("prompt_description", "Enter the lowercase letters shown. Similar-looking characters are excluded."))[:1000]
        self.success_message.default = str(c.get("success_message", "Verification complete. You now have access to the server."))[:1000]
        self.failure_message.default = str(c.get("failure_message", "Too many incorrect attempts. Press Verify again for a new CAPTCHA."))[:1000]

    async def on_submit(self, interaction: discord.Interaction):
        await self.view_instance.cog.save_config_fields(self.view_instance.guild.id, {
            "panel_description": self.panel_description.value,
            "prompt_description": self.prompt_description.value,
            "success_message": self.success_message.value,
            "failure_message": self.failure_message.value,
        })
        await interaction.response.send_message(embed=success_embed("Verification messages and labels saved."), ephemeral=True)
        await self.view_instance.refresh_message()


class VerificationButtonLabelsModal(Modal, title="Verification Button Labels"):
    verify_label = TextInput(label="Public Verify button label", required=True, max_length=80)
    enter_label = TextInput(label="Ephemeral Enter button label", required=True, max_length=80)

    def __init__(self, view: VerificationConfigView):
        super().__init__()
        self.view_instance = view
        self.verify_label.default = str(view.config.get("verify_button_label", "Verify"))[:80]
        self.enter_label.default = str(view.config.get("enter_button_label", "Enter the text"))[:80]

    async def on_submit(self, interaction: discord.Interaction):
        await self.view_instance.cog.save_config_fields(self.view_instance.guild.id, {
            "verify_button_label": self.verify_label.value.strip() or "Verify",
            "enter_button_label": self.enter_label.value.strip() or "Enter the text",
        })
        await interaction.response.send_message(embed=success_embed("Verification button labels saved. Repost or refresh the public panel to apply its new label."), ephemeral=True)
        await self.view_instance.refresh_message()
