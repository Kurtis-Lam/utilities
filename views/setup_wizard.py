"""Step-by-step setup wizard used by ``automod setup``, ``joins setup``, ``tickets setup`` and ``verify setup``.

A wizard is a list of ``Step`` objects. Each step asks one question and shows the current value, so
running a setup command again later simply walks through the same questions with the saved answers
already filled in (press Next / Skip to keep them, or change only what you want).

Controls per step (all inside one container message):

* channel / role pickers and Yes-No / choice buttons move on as soon as you pick something
* text and number steps open a small popup
* Back, Skip (optional steps), Clear (optional steps with a value), Next and Cancel

The last page is a review of every answer with a Save button; ``on_finish(interaction, values)`` does the
actual saving and returns the text to show when it is done (raise ``ValueError`` to show a problem and stay
on the review page).
"""
from __future__ import annotations

import typing
from dataclasses import dataclass, field

import discord
from discord.ui import Button, ChannelSelect, Modal, RoleSelect, TextInput

from views.common_views import BaseLayout, EMBED_COLOR, error_embed

OnFinish = typing.Callable[[discord.Interaction, dict], typing.Awaitable[str]]

KINDS = {"channel", "channels", "role", "roles", "text", "paragraph", "number", "toggle", "choice"}


@dataclass
class Step:
    key: str
    title: str
    prompt: str
    kind: str
    optional: bool = False
    default: typing.Any = None
    choices: list[tuple[str, str]] = field(default_factory=list)
    channel_types: list[discord.ChannelType] = field(default_factory=lambda: [discord.ChannelType.text])
    minimum: int = 1
    maximum: int = 25
    max_length: int = 1000
    placeholder: str = ""
    hint: str = ""
    when: typing.Callable[[dict], bool] | None = None
    validate: typing.Callable[[typing.Any], str | None] | None = None
    show: typing.Callable[[typing.Any], str] | None = None

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"Unknown step kind: {self.kind}")


def _empty(value: typing.Any) -> bool:
    return value is None or value == "" or value == []


def display_value(step: Step, value: typing.Any) -> str:
    if step.show is not None:
        return step.show(value)
    if _empty(value):
        return "*Not set*"
    if step.kind == "channel":
        return f"<#{value}>"
    if step.kind == "channels":
        return " ".join(f"<#{v}>" for v in value)
    if step.kind == "role":
        return f"<@&{value}>"
    if step.kind == "roles":
        return " ".join(f"<@&{v}>" for v in value)
    if step.kind == "toggle":
        return "Yes" if value else "No"
    if step.kind == "choice":
        return dict(step.choices).get(value, str(value))
    if step.kind == "paragraph":
        text = str(value)
        return text if len(text) <= 300 else text[:299] + "…"
    return str(value)


class _ValueModal(Modal):
    def __init__(self, wizard: "SetupWizard", step: Step):
        super().__init__(title=step.title[:45])
        self.wizard, self.step = wizard, step
        current = wizard.values.get(step.key)
        self.field = TextInput(
            label=step.title[:45],
            style=discord.TextStyle.paragraph if step.kind == "paragraph" else discord.TextStyle.short,
            required=not step.optional,
            max_length=step.max_length if step.kind != "number" else 6,
            default=None if _empty(current) else str(current)[:step.max_length],
            placeholder=step.placeholder[:100] or None,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.field.value.strip()
        step = self.step
        value: typing.Any = raw
        if not raw:
            value = None
        elif step.kind == "number":
            try:
                value = int(raw)
            except ValueError:
                return await interaction.response.send_message(embed=error_embed("Enter a whole number."), ephemeral=True)
            if not step.minimum <= value <= step.maximum:
                return await interaction.response.send_message(
                    embed=error_embed(f"Enter a number from {step.minimum} to {step.maximum}."), ephemeral=True
                )
        if step.validate is not None and value is not None:
            problem = step.validate(value)
            if problem:
                return await interaction.response.send_message(embed=error_embed(problem), ephemeral=True)
        self.wizard.values[step.key] = value
        await self.wizard.advance(interaction)


class SetupWizard(BaseLayout):
    def __init__(
        self,
        *,
        title: str,
        steps: list[Step],
        initial: dict,
        on_finish: OnFinish,
        author_id: int,
        guild: discord.Guild,
        timeout: float = 600,
    ):
        super().__init__(author_id=author_id, timeout=timeout)
        self.title = title
        self.steps = steps
        self.on_finish = on_finish
        self.guild = guild
        self.values: dict[str, typing.Any] = {
            step.key: initial.get(step.key, step.default) for step in steps
        }
        self.index = 0
        self.review = False
        self.render()

    # -- state ---------------------------------------------------------------

    def active_steps(self) -> list[Step]:
        return [s for s in self.steps if s.when is None or s.when(self.values)]

    def current(self) -> Step:
        steps = self.active_steps()
        self.index = max(0, min(self.index, len(steps) - 1))
        return steps[self.index]

    def satisfied(self, step: Step) -> bool:
        return step.optional or not _empty(self.values.get(step.key))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms is None or not perms.administrator:
            await interaction.response.send_message(embed=error_embed("Administrator permission is required."), ephemeral=True)
            return False
        return True

    # -- rendering -----------------------------------------------------------

    def render(self) -> None:
        self.clear_items()
        steps = self.active_steps()
        container = discord.ui.Container(accent_colour=EMBED_COLOR)

        if self.review:
            lines = [f"**{s.title}**\n{display_value(s, self.values.get(s.key))}" for s in steps]
            text = "\n".join(lines)
            if len(text) > 3300:
                text = text[:3299] + "…"
            container.add_item(discord.ui.TextDisplay(f"## {self.title}\n-# Review • nothing is saved until you press Save"))
            container.add_item(discord.ui.TextDisplay(text))
            container.add_item(discord.ui.Separator())
            back = Button(label="Back", emoji="◀️", style=discord.ButtonStyle.secondary)
            back.callback = self._back
            save = Button(label="Save", emoji="✅", style=discord.ButtonStyle.success)
            save.callback = self._save
            container.add_item(discord.ui.ActionRow(back, save, self._cancel_button()))
            self.add_item(container)
            return

        step = self.current()
        total = len(steps)
        dots = "".join("●" if i <= self.index else "○" for i in range(total))
        container.add_item(
            discord.ui.TextDisplay(f"## {self.title}\n-# Step {self.index + 1} of {total} • {dots}")
        )
        body = f"### {step.title}\n{step.prompt}"
        if step.hint:
            body += f"\n-# {step.hint}"
        container.add_item(discord.ui.TextDisplay(body))
        container.add_item(discord.ui.TextDisplay(f"**Current:** {display_value(step, self.values.get(step.key))}"))
        container.add_item(discord.ui.Separator())

        for row in self._input_rows(step):
            container.add_item(row)

        value = self.values.get(step.key)
        back = Button(label="Back", emoji="◀️", style=discord.ButtonStyle.secondary, disabled=self.index == 0)
        back.callback = self._back
        nav: list[discord.ui.Item] = [back]
        if step.optional:
            skip = Button(label="Skip", style=discord.ButtonStyle.secondary)
            skip.callback = self._skip
            nav.append(skip)
            if not _empty(value):
                clear = Button(label="Clear", emoji="🧹", style=discord.ButtonStyle.secondary)
                clear.callback = self._clear
                nav.append(clear)
        last = self.index == total - 1
        nxt = Button(
            label="Review" if last else "Next",
            emoji="📋" if last else "▶️",
            style=discord.ButtonStyle.primary,
            disabled=not self.satisfied(step),
        )
        nxt.callback = self._next
        nav.append(nxt)
        nav.append(self._cancel_button())
        container.add_item(discord.ui.ActionRow(*nav))
        self.add_item(container)

    def _cancel_button(self) -> Button:
        cancel = Button(label="Cancel", style=discord.ButtonStyle.danger)
        cancel.callback = self._cancel
        return cancel

    def _input_rows(self, step: Step) -> list[discord.ui.Item]:
        kind = step.kind
        value = self.values.get(step.key)
        if kind in {"channel", "channels"}:
            multi = kind == "channels"
            select = ChannelSelect(
                channel_types=step.channel_types,
                placeholder=step.placeholder or ("Choose channels" if multi else "Choose a channel"),
                min_values=1,
                max_values=min(step.maximum, 25) if multi else 1,
            )
            select.callback = self._make_select_callback(step, select, multi)
            return [discord.ui.ActionRow(select)]
        if kind in {"role", "roles"}:
            multi = kind == "roles"
            select = RoleSelect(
                placeholder=step.placeholder or ("Choose roles" if multi else "Choose a role"),
                min_values=1,
                max_values=min(step.maximum, 25) if multi else 1,
            )
            select.callback = self._make_select_callback(step, select, multi)
            return [discord.ui.ActionRow(select)]
        if kind in {"text", "paragraph", "number"}:
            edit = Button(label="Edit", emoji="✏️", style=discord.ButtonStyle.primary)

            async def open_modal(interaction: discord.Interaction, step=step):
                await interaction.response.send_modal(_ValueModal(self, step))

            edit.callback = open_modal
            return [discord.ui.ActionRow(edit)]
        if kind == "toggle":
            buttons = []
            for label, flag in (("Yes", True), ("No", False)):
                selected = value is not None and bool(value) == flag
                button = Button(
                    label=label,
                    style=discord.ButtonStyle.success if selected and flag else (
                        discord.ButtonStyle.danger if selected else discord.ButtonStyle.secondary
                    ),
                )
                button.callback = self._make_value_callback(step, flag)
                buttons.append(button)
            return [discord.ui.ActionRow(*buttons)]
        if kind == "choice":
            buttons = []
            for key, label in step.choices[:5]:
                button = Button(
                    label=label,
                    style=discord.ButtonStyle.primary if value == key else discord.ButtonStyle.secondary,
                )
                button.callback = self._make_value_callback(step, key)
                buttons.append(button)
            return [discord.ui.ActionRow(*buttons)]
        return []

    # -- callbacks -----------------------------------------------------------

    def _make_select_callback(self, step: Step, select: discord.ui.Select, multi: bool):
        async def callback(interaction: discord.Interaction):
            ids = [item.id for item in select.values]
            self.values[step.key] = ids if multi else ids[0]
            if multi:
                self.render()
                await interaction.response.edit_message(view=self)
            else:
                await self.advance(interaction)

        return callback

    def _make_value_callback(self, step: Step, value: typing.Any):
        async def callback(interaction: discord.Interaction):
            self.values[step.key] = value
            await self.advance(interaction)

        return callback

    async def advance(self, interaction: discord.Interaction) -> None:
        steps = self.active_steps()
        if self.index >= len(steps) - 1:
            self.review = True
        else:
            self.index += 1
        self.render()
        await self._edit(interaction)

    async def _edit(self, interaction: discord.Interaction) -> None:
        if interaction.response.is_done():
            if self.message is not None:
                await self.message.edit(view=self)
            else:
                await interaction.edit_original_response(view=self)
        else:
            await interaction.response.edit_message(view=self)

    async def _next(self, interaction: discord.Interaction):
        await self.advance(interaction)

    async def _skip(self, interaction: discord.Interaction):
        await self.advance(interaction)

    async def _clear(self, interaction: discord.Interaction):
        self.values[self.current().key] = None if self.current().kind not in {"channels", "roles"} else []
        self.render()
        await interaction.response.edit_message(view=self)

    async def _back(self, interaction: discord.Interaction):
        if self.review:
            self.review = False
        else:
            self.index = max(0, self.index - 1)
        self.render()
        await interaction.response.edit_message(view=self)

    async def _cancel(self, interaction: discord.Interaction):
        self.stop()
        self.superseded = True
        await interaction.response.edit_message(view=_done_view("Setup cancelled", "Nothing was changed."))

    async def _save(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            result = await self.on_finish(interaction, dict(self.values))
        except ValueError as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(embed=error_embed(f"Discord rejected a change: {exc.text or exc}"), ephemeral=True)
            return
        self.stop()
        self.superseded = True
        await interaction.edit_original_response(view=_done_view(f"{self.title} saved", result))


def _done_view(title: str, text: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=EMBED_COLOR)
    container.add_item(discord.ui.TextDisplay(f"## {title}"))
    if text:
        container.add_item(discord.ui.TextDisplay(text[:3900]))
    view.add_item(container)
    return view


async def start_wizard(ctx, wizard: SetupWizard) -> None:
    wizard.message = await ctx.reply(view=wizard, mention_author=False)