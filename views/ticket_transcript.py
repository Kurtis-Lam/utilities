"""Discord-style HTML transcripts for support tickets.

``build_ticket_transcript`` reads a ticket channel and returns a single, self-contained
``.html`` file that looks like the Discord client: avatars, role colours, bot tags, replies,
markdown, embeds, attachments, stickers, reactions, buttons, day dividers and a dark/light
theme switch.  The channel itself is never shown (it is deleted right after the transcript is
made); the header shows the ticket number, ID, category, people involved and who deleted it.
"""
from __future__ import annotations

import base64
import html
import io
import mimetypes
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone

import discord

MAX_MESSAGES = 5000
MAX_INLINE_IMAGE_BYTES = 3 * 1024 * 1024
INLINE_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/avif"}
GROUP_WINDOW_SECONDS = 7 * 60


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------

def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _time12(dt: datetime) -> str:
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def _time12_seconds(dt: datetime) -> str:
    return f"{dt.hour % 12 or 12}:{dt.minute:02d}:{dt.second:02d} {'AM' if dt.hour < 12 else 'PM'}"


def _date_short(dt: datetime) -> str:
    return f"{dt.month:02d}/{dt.day:02d}/{dt.year}"


def _date_long(dt: datetime) -> str:
    return f"{dt.strftime('%B')} {dt.day}, {dt.year}"


def _stamp(dt: datetime) -> str:
    dt = _utc(dt)
    return f"{_date_short(dt)} {_time12(dt)}"


def _full_stamp(dt: datetime) -> str:
    dt = _utc(dt)
    return f"{dt.strftime('%A')}, {_date_long(dt)} {_time12(dt)} UTC"


def _size_text(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _css_color(colour) -> str | None:
    value = getattr(colour, "value", colour)
    if not value:
        return None
    return f"#{int(value):06x}"


def _safe_href(url: str) -> str:
    return _esc(str(url).replace('"', "%22"))


def _is_emoji_only(text: str) -> bool:
    stripped = re.sub(r"<a?:\w{1,32}:\d{15,25}>", "", text or "").strip()
    remainder = re.sub(r"\s+", "", stripped)
    if len(text or "") > 400 or not (text or "").strip():
        return False
    if not remainder:
        return bool(re.search(r"<a?:\w{1,32}:\d{15,25}>", text or ""))
    if len(remainder) > 30:
        return False
    return all(unicodedata.category(ch) in ("So", "Sk", "Mn", "Me", "Cf") for ch in remainder)


# --------------------------------------------------------------------------------------
# Rendering context (name / role / channel lookups)
# --------------------------------------------------------------------------------------

class _Context:
    def __init__(self, guild: discord.Guild | None, channel_id: int, users: dict[int, object], inline_images: dict[int, str]):
        self.guild = guild
        self.channel_id = channel_id
        self.users = users
        self.inline_images = inline_images

    def user_name(self, user_id: int) -> str:
        user = self.users.get(user_id)
        if user is None and self.guild is not None:
            user = self.guild.get_member(user_id)
        if user is None:
            return "Unknown User"
        return getattr(user, "display_name", None) or getattr(user, "name", "Unknown User")

    def role(self, role_id: int) -> tuple[str, str | None]:
        role = self.guild.get_role(role_id) if self.guild is not None else None
        if role is None:
            return "deleted-role", None
        return role.name, _css_color(role.color)

    def channel_name(self, channel_id: int) -> str:
        if channel_id == self.channel_id:
            return "ticket"
        channel = self.guild.get_channel(channel_id) if self.guild is not None else None
        return getattr(channel, "name", None) or "deleted-channel"


# --------------------------------------------------------------------------------------
# Discord markdown -> HTML
# --------------------------------------------------------------------------------------

_CODEBLOCK_RE = re.compile(r"```(?:([A-Za-z0-9_+#.\-]+)\n)?(.*?)```", re.S)
_INLINE_CODE_RE = re.compile(r"``(.+?)``|`([^`\n]+?)`", re.S)
_MASKED_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_SUPPRESSED_LINK_RE = re.compile(r"&lt;(https?://(?:(?!&gt;)\S)+?)&gt;")
_BARE_URL_RE = re.compile(r"https?://(?:(?!&lt;|&gt;)[^\s\x00\"])+")
_TOKEN_RE = re.compile(r"\x00(\d+)\x00")


def _link_html(url: str, label: str | None = None) -> str:
    """``url`` is a raw URL; ``label`` is already-escaped HTML (defaults to the escaped URL)."""
    return f'<a href="{_safe_href(url)}" target="_blank" rel="noopener noreferrer">{label if label is not None else _esc(url)}</a>'


def _emoji_img(name: str, emoji_id: int, animated: bool, cls: str = "emoji") -> str:
    ext = "gif" if animated else "webp"
    return f'<img class="{cls}" src="https://cdn.discordapp.com/emojis/{emoji_id}.{ext}?size=64&amp;quality=lossless" alt=":{_esc(name)}:" title=":{_esc(name)}:" loading="lazy">'


def _format_timestamp(value: int, style: str | None) -> str:
    try:
        dt = datetime.fromtimestamp(int(value), timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "Invalid Date"
    style = style or "f"
    if style == "t":
        return _time12(dt)
    if style == "T":
        return _time12_seconds(dt)
    if style == "d":
        return _date_short(dt)
    if style == "D":
        return _date_long(dt)
    if style == "F":
        return f"{dt.strftime('%A')}, {_date_long(dt)} {_time12(dt)}"
    return f"{_date_long(dt)} {_time12(dt)}"


def _blocks(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    run: list[str] = []
    quote: list[str] = []

    def flush_run():
        if run:
            out.append("<br>".join(run))
            run.clear()

    def flush_quote():
        if quote:
            out.append("<blockquote>" + "<br>".join(quote) + "</blockquote>")
            quote.clear()

    for index, line in enumerate(lines):
        if line.startswith("&gt;&gt;&gt; "):
            flush_run()
            flush_quote()
            rest = [line[len("&gt;&gt;&gt; "):]] + lines[index + 1:]
            out.append("<blockquote>" + "<br>".join(rest) + "</blockquote>")
            return "".join(out)
        quoted = re.match(r"^&gt; (.*)$", line)
        if quoted:
            flush_run()
            quote.append(quoted.group(1))
            continue
        flush_quote()
        header = re.match(r"^(#{1,3}) (.+)$", line)
        if header:
            flush_run()
            out.append(f'<div class="h{len(header.group(1))}">{header.group(2)}</div>')
            continue
        subtext = re.match(r"^-# (.+)$", line)
        if subtext:
            flush_run()
            out.append(f'<div class="subtext">{subtext.group(1)}</div>')
            continue
        bullet = re.match(r"^\s*[-*] (.+)$", line)
        if bullet:
            flush_run()
            out.append(f'<div class="li"><span class="dot">•</span><span>{bullet.group(1)}</span></div>')
            continue
        numbered = re.match(r"^\s*(\d{1,3})\. (.+)$", line)
        if numbered:
            flush_run()
            out.append(f'<div class="li"><span class="dot">{numbered.group(1)}.</span><span>{numbered.group(2)}</span></div>')
            continue
        run.append(line)
    flush_run()
    flush_quote()
    return "".join(out)


def render_markdown(raw: str | None, ctx: _Context, *, jumbo: bool = False) -> str:
    if not raw:
        return ""
    stash: list[str] = []

    def keep(markup: str) -> str:
        stash.append(markup)
        return f"\x00{len(stash) - 1}\x00"

    text = html.escape(raw.replace("\x00", ""), quote=False)

    text = _CODEBLOCK_RE.sub(lambda m: keep(f'<pre class="codeblock"><code>{m.group(2).strip(chr(10))}</code></pre>'), text)
    text = _INLINE_CODE_RE.sub(lambda m: keep(f'<code class="inline">{m.group(1) or m.group(2)}</code>'), text)
    text = re.sub(r"\\([\\*_~|`#\-\[\]()])", lambda m: keep(m.group(1)), text)

    text = _MASKED_LINK_RE.sub(lambda m: keep(_link_html(html.unescape(m.group(2)), m.group(1))), text)
    text = _SUPPRESSED_LINK_RE.sub(lambda m: keep(_link_html(html.unescape(m.group(1)))), text)

    def bare(match: re.Match) -> str:
        url = match.group(0)
        trailing = ""
        while url and (url[-1] in ".,;:!?'" or (url[-1] == ")" and url.count("(") < url.count(")"))):
            trailing = url[-1] + trailing
            url = url[:-1]
        return keep(_link_html(html.unescape(url), url)) + trailing

    text = _BARE_URL_RE.sub(bare, text)

    text = re.sub(r"&lt;@!?(\d+)&gt;", lambda m: keep(f'<span class="mention">@{_esc(ctx.user_name(int(m.group(1))))}</span>'), text)

    def role_mention(match: re.Match) -> str:
        name, color = ctx.role(int(match.group(1)))
        style = f' style="color:{color};background:{color}26"' if color else ""
        return keep(f'<span class="mention"{style}>@{_esc(name)}</span>')

    text = re.sub(r"&lt;@&amp;(\d+)&gt;", role_mention, text)
    text = re.sub(r"&lt;#(\d+)&gt;", lambda m: keep(f'<span class="mention">#{_esc(ctx.channel_name(int(m.group(1))))}</span>'), text)
    text = re.sub(r"&lt;/([\w\- ]{1,32}):(\d+)&gt;", lambda m: keep(f'<span class="mention">/{_esc(m.group(1))}</span>'), text)
    text = re.sub(
        r"&lt;(a?):(\w{1,32}):(\d{15,25})&gt;",
        lambda m: keep(_emoji_img(m.group(2), int(m.group(3)), bool(m.group(1)), "emoji jumbo" if jumbo else "emoji")),
        text,
    )
    text = re.sub(r"&lt;t:(-?\d+)(?::([tTdDfFR]))?&gt;", lambda m: keep(f'<span class="mention">{_esc(_format_timestamp(int(m.group(1)), m.group(2)))}</span>'), text)
    text = re.sub(r"(?<![\w])@(everyone|here)\b", lambda m: keep(f'<span class="mention">@{m.group(1)}</span>'), text)

    text = re.sub(r"\|\|(.+?)\|\|", r'<span class="spoiler">\1</span>', text, flags=re.S)
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", text, flags=re.S)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text, flags=re.S)
    text = re.sub(r"__(.+?)__", r"<u>\1</u>", text, flags=re.S)
    text = re.sub(r"(?<![\*\w])\*(?![\s\*])(.+?)(?<![\s\*])\*(?![\*\w])", r"<em>\1</em>", text)
    text = re.sub(r"(?<![\w_])_(?![\s_])(.+?)(?<![\s_])_(?![\w_])", r"<em>\1</em>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.S)

    text = _blocks(text)

    for _ in range(5):
        if "\x00" not in text:
            break
        text = _TOKEN_RE.sub(lambda m: stash[int(m.group(1))], text)
    return text


# --------------------------------------------------------------------------------------
# Message pieces
# --------------------------------------------------------------------------------------

def _avatar_url(user) -> str:
    try:
        return str(user.display_avatar.replace(size=64, static_format="webp"))
    except Exception:
        return "https://cdn.discordapp.com/embed/avatars/0.png"


def _author_color(user) -> str | None:
    return _css_color(getattr(user, "color", None))


def _emoji_html(emoji, cls: str = "emoji") -> str:
    if emoji is None:
        return ""
    emoji_id = getattr(emoji, "id", None)
    if emoji_id:
        return _emoji_img(getattr(emoji, "name", "emoji") or "emoji", int(emoji_id), bool(getattr(emoji, "animated", False)), cls)
    return f'<span class="uemoji">{_esc(getattr(emoji, "name", None) or str(emoji))}</span>'


def _is_image(attachment) -> bool:
    content_type = (getattr(attachment, "content_type", None) or mimetypes.guess_type(attachment.filename)[0] or "").split(";")[0].lower()
    return content_type.startswith("image/") and content_type != "image/svg+xml"


def _file_icon(name: str, content_type: str) -> str:
    lowered = name.lower()
    if content_type.startswith("video/") or lowered.endswith((".mp4", ".mov", ".webm", ".mkv")):
        return "🎞️"
    if content_type.startswith("audio/") or lowered.endswith((".mp3", ".wav", ".ogg", ".flac", ".m4a")):
        return "🎵"
    if lowered.endswith((".zip", ".rar", ".7z", ".tar", ".gz")):
        return "🗜️"
    if lowered.endswith((".pdf", ".doc", ".docx", ".txt", ".md", ".rtf")):
        return "📄"
    return "📎"


def _render_attachment(attachment, ctx: _Context) -> str:
    name = attachment.filename or "file"
    content_type = (getattr(attachment, "content_type", None) or mimetypes.guess_type(name)[0] or "").split(";")[0].lower()
    spoiler = name.startswith("SPOILER_")
    if _is_image(attachment):
        source = ctx.inline_images.get(attachment.id) or attachment.url
        cls = "att-image spoiler-img" if spoiler else "att-image"
        alt = _esc(attachment.description or name)
        return f'<div class="attachment"><img class="{cls}" src="{_esc(source)}" alt="{alt}" loading="lazy"></div>'
    return (
        '<div class="attachment file">'
        f'<div class="file-icon">{_file_icon(name, content_type)}</div>'
        f'<div class="file-meta"><a href="{_safe_href(attachment.url)}" target="_blank" rel="noopener noreferrer">{_esc(name)}</a>'
        f'<div class="file-size">{_size_text(getattr(attachment, "size", 0))}</div></div></div>'
    )


def _render_embed(embed: discord.Embed, ctx: _Context) -> str:
    kind = getattr(embed, "type", "rich") or "rich"
    if kind == "image":
        source = getattr(embed.thumbnail, "url", None) or embed.url
        if source:
            return f'<div class="attachment"><img class="att-image" src="{_esc(source)}" alt="" loading="lazy"></div>'
    if kind == "gifv":
        video = getattr(embed.video, "url", None)
        poster = getattr(embed.thumbnail, "url", None)
        if video:
            return f'<div class="attachment"><video class="att-image" src="{_esc(video)}" poster="{_esc(poster or "")}" autoplay loop muted playsinline></video></div>'

    color = _css_color(embed.color)
    style = f' style="border-left-color:{color}"' if color else ""
    parts: list[str] = []
    provider = getattr(embed.provider, "name", None)
    if provider and kind != "rich":
        parts.append(f'<div class="embed-provider">{_esc(provider)}</div>')
    if embed.author and embed.author.name:
        icon = f'<img class="embed-author-icon" src="{_esc(embed.author.icon_url)}" alt="" loading="lazy">' if embed.author.icon_url else ""
        name = _esc(embed.author.name)
        if embed.author.url:
            name = _link_html(embed.author.url, name)
        parts.append(f'<div class="embed-author">{icon}<span>{name}</span></div>')
    if embed.title:
        title = render_markdown(embed.title, ctx)
        if embed.url:
            title = _link_html(embed.url, title)
        parts.append(f'<div class="embed-title">{title}</div>')
    if embed.description:
        parts.append(f'<div class="embed-description">{render_markdown(embed.description, ctx)}</div>')

    rows: list[list] = []
    current: list = []
    for field in embed.fields:
        if field.inline:
            current.append(field)
            if len(current) == 3:
                rows.append(current)
                current = []
        else:
            if current:
                rows.append(current)
                current = []
            rows.append([field])
    if current:
        rows.append(current)
    if rows:
        cells = []
        for row in rows:
            span = 12 // len(row)
            for field in row:
                cells.append(
                    f'<div class="embed-field" style="grid-column:span {span}">'
                    f'<div class="embed-field-name">{render_markdown(field.name, ctx)}</div>'
                    f'<div class="embed-field-value">{render_markdown(field.value, ctx)}</div></div>'
                )
        parts.append(f'<div class="embed-fields">{"".join(cells)}</div>')

    if embed.image and embed.image.url:
        parts.append(f'<div class="embed-image"><img src="{_esc(embed.image.url)}" alt="" loading="lazy"></div>')
    elif kind in ("video", "gifv") and embed.thumbnail and embed.thumbnail.url:
        parts.append(f'<div class="embed-image"><img src="{_esc(embed.thumbnail.url)}" alt="" loading="lazy"></div>')

    footer_bits = []
    if embed.footer and embed.footer.text:
        icon = f'<img class="embed-footer-icon" src="{_esc(embed.footer.icon_url)}" alt="" loading="lazy">' if embed.footer.icon_url else ""
        footer_bits.append(f"{icon}<span>{_esc(embed.footer.text)}</span>")
    if embed.timestamp:
        footer_bits.append(f'<span title="{_esc(_full_stamp(embed.timestamp))}">{_esc(_stamp(embed.timestamp))}</span>')
    if footer_bits:
        parts.append(f'<div class="embed-footer">{"<span class=sep>•</span>".join(footer_bits)}</div>')

    if not parts and not (embed.thumbnail and embed.thumbnail.url):
        return ""
    thumb = ""
    has_thumb = bool(embed.thumbnail and embed.thumbnail.url and kind not in ("video", "gifv"))
    if has_thumb:
        thumb = f'<img class="embed-thumb" src="{_esc(embed.thumbnail.url)}" alt="" loading="lazy">'
    return f'<div class="embed{" has-thumb" if has_thumb else ""}"{style}><div class="embed-body">{"".join(parts)}</div>{thumb}</div>'


def _render_component(component, ctx: _Context) -> str:
    name = type(component).__name__
    try:
        if name == "ActionRow":
            inner = "".join(_render_component(child, ctx) for child in getattr(component, "children", []))
            return f'<div class="action-row">{inner}</div>' if inner else ""
        if name == "Button":
            style = getattr(getattr(component, "style", None), "name", "secondary")
            style = {"blurple": "primary", "grey": "secondary", "gray": "secondary", "green": "success", "red": "danger", "url": "link"}.get(style, style)
            emoji = _emoji_html(getattr(component, "emoji", None))
            label = _esc(component.label) if getattr(component, "label", None) else ""
            inner = f"{emoji}{'<span>' + label + '</span>' if label else ''}"
            disabled = " disabled" if getattr(component, "disabled", False) else ""
            if style == "link" and getattr(component, "url", None):
                return f'<a class="btn btn-link{disabled}" href="{_safe_href(component.url)}" target="_blank" rel="noopener noreferrer">{inner}<span class="ext">↗</span></a>'
            return f'<span class="btn btn-{_esc(style)}{disabled}">{inner}</span>'
        if name in ("SelectMenu", "Select", "StringSelect", "UserSelect", "RoleSelect", "MentionableSelect", "ChannelSelect"):
            placeholder = _esc(getattr(component, "placeholder", None) or "Make a selection")
            disabled = " disabled" if getattr(component, "disabled", False) else ""
            return f'<div class="select{disabled}"><span>{placeholder}</span><span class="chev">⌄</span></div>'
        if name == "TextDisplay":
            return f'<div class="v2-text">{render_markdown(getattr(component, "content", ""), ctx)}</div>'
        if name == "Separator":
            return '<hr class="v2-sep">'
        if name in ("Container", "Section"):
            inner = "".join(_render_component(child, ctx) for child in getattr(component, "children", []))
            accessory = getattr(component, "accessory", None)
            if accessory is not None:
                inner += _render_component(accessory, ctx)
            colour = _css_color(getattr(component, "accent_colour", None) or getattr(component, "accent_color", None))
            style = f' style="border-left-color:{colour}"' if colour else ""
            return f'<div class="v2-container"{style}>{inner}</div>' if inner else ""
    except Exception:
        return ""
    return ""


def _reply_snippet(target, ctx: _Context) -> str:
    content = (getattr(target, "content", "") or "").strip()
    if content:
        content = re.sub(r"```.*?```", "[code block]", content, flags=re.S)
        content = re.sub(r"\s+", " ", content)
        if len(content) > 120:
            content = content[:117] + "…"
        return render_markdown(content, ctx)
    if getattr(target, "attachments", None):
        return '<em class="faded">Click to see attachment</em>'
    if getattr(target, "embeds", None):
        return '<em class="faded">Click to see embed</em>'
    return '<em class="faded">Click to see message</em>'


def _render_reply(message, by_id: dict[int, object], ctx: _Context) -> str:
    reference = getattr(message, "reference", None)
    if reference is None or getattr(reference, "message_id", None) is None:
        return ""
    target = by_id.get(reference.message_id)
    if target is None:
        resolved = getattr(reference, "resolved", None) or getattr(reference, "cached_message", None)
        target = resolved if hasattr(resolved, "author") else None
    if target is None:
        return '<div class="reply"><span class="reply-spine"></span><span class="reply-name faded">Original message was deleted</span></div>'
    author = target.author
    color = _author_color(author)
    style = f' style="color:{color}"' if color else ""
    bot = '<span class="tag">APP</span>' if getattr(author, "bot", False) else ""
    return (
        f'<a class="reply" href="#m-{target.id}"><span class="reply-spine"></span>'
        f'<img class="reply-avatar" src="{_esc(_avatar_url(author))}" alt="">{bot}'
        f'<span class="reply-name"{style}>@{_esc(getattr(author, "display_name", author.name))}</span>'
        f'<span class="reply-text">{_reply_snippet(target, ctx)}</span></a>'
    )


def _render_message(message, previous, by_id: dict[int, object], ctx: _Context) -> str:
    author = message.author
    created = _utc(message.created_at)
    time_title = _esc(_full_stamp(created))
    message_type = getattr(message, "type", None)
    type_name = getattr(message_type, "name", "default")

    if type_name not in ("default", "reply", "chat_input_command", "context_menu_command"):
        try:
            system_text = message.system_content or ""
        except Exception:
            system_text = ""
        if not system_text:
            return ""
        return (
            f'<div class="msg system" id="m-{message.id}"><div class="system-icon">➜</div>'
            f'<div class="system-text">{_esc(system_text)} <span class="ts" title="{time_title}">{_esc(_stamp(created))}</span></div></div>'
        )

    reply_html = _render_reply(message, by_id, ctx) if type_name == "reply" or getattr(message, "reference", None) else ""
    grouped = False
    if previous is not None and not reply_html:
        prev_type = getattr(getattr(previous, "type", None), "name", "default")
        same_author = previous.author.id == author.id
        close_in_time = (created - _utc(previous.created_at)).total_seconds() < GROUP_WINDOW_SECONDS
        same_day = _utc(previous.created_at).date() == created.date()
        grouped = same_author and close_in_time and same_day and prev_type in ("default", "reply", "chat_input_command", "context_menu_command")

    body: list[str] = []
    content = message.content or ""
    if content:
        rendered = render_markdown(content, ctx, jumbo=_is_emoji_only(content))
        edited = ""
        if getattr(message, "edited_at", None):
            edited = f' <span class="edited" title="{_esc(_full_stamp(message.edited_at))}">(edited)</span>'
        body.append(f'<div class="content">{rendered}{edited}</div>')
    elif getattr(message, "edited_at", None) and not (message.embeds or message.attachments):
        body.append('<div class="content"><span class="edited">(edited)</span></div>')

    for snapshot in getattr(message, "message_snapshots", None) or []:
        try:
            inner = []
            if getattr(snapshot, "content", ""):
                inner.append(f'<div class="content">{render_markdown(snapshot.content, ctx)}</div>')
            inner.extend(_render_attachment(a, ctx) for a in getattr(snapshot, "attachments", []))
            inner.extend(_render_embed(e, ctx) for e in getattr(snapshot, "embeds", []))
            body.append(f'<div class="forward"><div class="forward-label">↪ Forwarded</div>{"".join(inner)}</div>')
        except Exception:
            continue

    for attachment in message.attachments:
        body.append(_render_attachment(attachment, ctx))
    for embed in message.embeds:
        try:
            body.append(_render_embed(embed, ctx))
        except Exception:
            continue
    for sticker in getattr(message, "stickers", []) or []:
        fmt = getattr(getattr(sticker, "format", None), "name", "")
        if fmt == "lottie":
            body.append(f'<div class="sticker-fallback">🏷️ Sticker: {_esc(sticker.name)}</div>')
        else:
            body.append(f'<div class="sticker"><img src="{_esc(sticker.url)}" alt="{_esc(sticker.name)}" title="{_esc(sticker.name)}" loading="lazy"></div>')
    for component in getattr(message, "components", []) or []:
        rendered_component = _render_component(component, ctx)
        if rendered_component:
            body.append(rendered_component)
    if message.reactions:
        pills = []
        for reaction in message.reactions:
            pills.append(f'<div class="reaction">{_emoji_html(reaction.emoji, "emoji reaction-emoji")}<span class="count">{reaction.count}</span></div>')
        body.append(f'<div class="reactions">{"".join(pills)}</div>')

    body_html = "".join(body)
    if grouped:
        return (
            f'<div class="msg grouped" id="m-{message.id}"><span class="gutter-time" title="{time_title}">{_esc(_time12(created))}</span>'
            f'<div class="msg-main">{body_html}</div></div>'
        )

    color = _author_color(author)
    name_style = f' style="color:{color}"' if color else ""
    bot = '<span class="tag">APP</span>' if getattr(author, "bot", False) else ""
    pin = '<span class="pin" title="Pinned">📌</span>' if getattr(message, "pinned", False) else ""
    display = _esc(getattr(author, "display_name", None) or author.name)
    return (
        f'<div class="msg first" id="m-{message.id}">{reply_html}'
        f'<img class="avatar" src="{_esc(_avatar_url(author))}" alt="" loading="lazy">'
        f'<div class="msg-main"><div class="msg-header"><span class="author"{name_style}>{display}</span>{bot}'
        f'<span class="ts" title="{time_title}">{_esc(_stamp(created))}</span>{pin}</div>{body_html}</div></div>'
    )


# --------------------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------------------

_CSS = """
:root{--bg:#1e1f22;--panel:#313338;--panel2:#2b2d31;--panel3:#232428;--text:#dbdee1;--muted:#949ba4;--header:#f2f3f5;--link:#00a8fc;--hover:#2e3035;--border:#3f4147;--mention-bg:rgba(88,101,242,.3);--mention-text:#c9cdfb;--mention-hover:#5865f2;--code-bg:#1e1f22;--code-border:#1a1b1e;--quote:#4e5058;--embed-default:#1e1f22;--reaction-bg:#2b2d31;--reaction-border:#3f4147;--spoiler:#1e1f22;--spoiler-open:rgba(255,255,255,.1);--shadow:0 8px 24px rgba(0,0,0,.35)}
:root[data-theme=light]{--bg:#e3e5e8;--panel:#ffffff;--panel2:#f2f3f5;--panel3:#ebedef;--text:#313338;--muted:#5c5e66;--header:#060607;--link:#006ce7;--hover:#f7f7f8;--border:#d7d9dc;--mention-bg:rgba(88,101,242,.15);--mention-text:#4752c4;--mention-hover:#5865f2;--code-bg:#f2f3f5;--code-border:#e3e5e8;--quote:#c4c9ce;--embed-default:#e3e5e8;--reaction-bg:#f2f3f5;--reaction-border:#e3e5e8;--spoiler:#c4c9ce;--spoiler-open:rgba(0,0,0,.08);--shadow:0 8px 24px rgba(0,0,0,.12)}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--text);font-family:"gg sans","Noto Sans","Helvetica Neue",Helvetica,Arial,"Segoe UI",sans-serif,"Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji";font-size:16px;line-height:1.375rem;-webkit-font-smoothing:antialiased}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
.topbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 20px;background:var(--panel2);border-bottom:1px solid var(--border);box-shadow:0 1px 0 rgba(0,0,0,.1)}
.brand{display:flex;align-items:center;gap:12px;min-width:0}
.brand img,.brand .gicon{width:36px;height:36px;border-radius:12px;flex:none;object-fit:cover;background:#5865f2;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700}
.brand .gname{font-weight:700;color:var(--header);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.brand .gsub{font-size:12px;color:var(--muted);line-height:1rem}
.toggle{border:0;border-radius:8px;padding:7px 12px;background:var(--panel3);color:var(--text);cursor:pointer;font:inherit;font-size:13px;font-weight:600}
.toggle:hover{filter:brightness(1.15)}
.wrap{max-width:1040px;margin:24px auto 48px;padding:0 16px}
.summary{background:var(--panel2);border:1px solid var(--border);border-radius:12px;padding:20px 22px;margin-bottom:18px;box-shadow:var(--shadow)}
.summary h1{margin:0 0 2px;font-size:22px;line-height:1.75rem;color:var(--header)}
.summary .lead{color:var(--muted);font-size:14px;margin-bottom:16px}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.fact{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:10px 12px;min-width:0}
.fact .k{font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);margin-bottom:3px}
.fact .v{font-size:14px;color:var(--header);word-break:break-word;display:flex;align-items:center;gap:8px}
.fact .v img{width:22px;height:22px;border-radius:50%;flex:none}
.fact.danger{border-color:rgba(218,55,60,.55)}
.fact.danger .k{color:#f23f42}
.people{margin-top:16px}
.people .k{font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{display:flex;align-items:center;gap:8px;background:var(--panel);border:1px solid var(--border);border-radius:999px;padding:4px 12px 4px 4px;font-size:13px;color:var(--header)}
.chip img{width:24px;height:24px;border-radius:50%}
.chip .n{color:var(--muted);font-size:12px}
.chat{background:var(--panel);border-radius:12px;border:1px solid var(--border);padding:12px 0 16px;box-shadow:var(--shadow)}
.chat-head{display:flex;align-items:center;gap:8px;padding:2px 16px 12px;margin-bottom:4px;border-bottom:1px solid var(--border);color:var(--header);font-weight:700}
.chat-head .hash{color:var(--muted);font-size:22px}
.chat-head .sub{color:var(--muted);font-weight:400;font-size:13px;margin-left:auto}
.divider{display:flex;align-items:center;margin:22px 16px 6px;color:var(--muted);font-size:12px;font-weight:600;line-height:1.3}
.divider:before,.divider:after{content:"";flex:1;height:1px;background:var(--border)}
.divider span{padding:0 8px}
.omitted{margin:8px 16px;padding:10px 12px;border-radius:8px;background:var(--panel2);color:var(--muted);font-size:13px;text-align:center}
.msg{position:relative;padding:2px 48px 2px 72px;min-height:22px}
.msg:hover{background:var(--hover)}
.msg.first{margin-top:17px;padding-top:2px}
.msg.grouped{padding-top:1px}
.msg:target{background:rgba(250,166,26,.12);box-shadow:inset 2px 0 0 #faa61a}
.avatar{position:absolute;left:16px;top:4px;width:40px;height:40px;border-radius:50%;object-fit:cover;background:var(--panel2)}
.gutter-time{position:absolute;left:0;width:72px;text-align:center;font-size:11px;color:var(--muted);line-height:22px;visibility:hidden;user-select:none}
.msg:hover .gutter-time{visibility:visible}
.msg-header{display:flex;align-items:baseline;flex-wrap:wrap;gap:0 6px;line-height:1.375rem}
.author{font-weight:500;color:var(--header);cursor:default}
.author:hover{text-decoration:underline}
.tag{display:inline-flex;align-items:center;height:15px;padding:0 5px;border-radius:4px;background:#5865f2;color:#fff;font-size:10px;font-weight:600;letter-spacing:.02em;line-height:15px;position:relative;top:-1px;text-transform:uppercase}
.ts{color:var(--muted);font-size:12px;font-weight:500}
.pin{font-size:11px}
.content{white-space:normal;word-wrap:break-word;overflow-wrap:anywhere;color:var(--text)}
.content strong,.embed strong{color:var(--header)}
.edited{color:var(--muted);font-size:10px}
.h1{font-size:1.5rem;font-weight:700;line-height:1.75rem;margin:8px 0 4px;color:var(--header)}
.h2{font-size:1.25rem;font-weight:700;line-height:1.5rem;margin:8px 0 4px;color:var(--header)}
.h3{font-size:1rem;font-weight:700;margin:8px 0 4px;color:var(--header)}
.subtext{font-size:.75rem;color:var(--muted)}
.li{display:flex;gap:8px;margin-left:8px}
.li .dot{color:var(--muted);flex:none}
blockquote{margin:2px 0;padding:0 8px 0 12px;border-left:4px solid var(--quote);border-radius:2px}
code.inline{background:var(--code-bg);border:1px solid var(--code-border);border-radius:4px;padding:1px 5px;font-family:Consolas,"Andale Mono WT","Andale Mono","Lucida Console",Monaco,monospace;font-size:.875rem;white-space:pre-wrap}
pre.codeblock{margin:6px 0;max-width:90%;background:var(--code-bg);border:1px solid var(--code-border);border-radius:4px;padding:7px 9px;overflow-x:auto;font-size:.875rem;line-height:1.125rem}
pre.codeblock code{font-family:Consolas,"Andale Mono WT","Andale Mono","Lucida Console",Monaco,monospace;white-space:pre}
.mention{display:inline-block;padding:0 2px;border-radius:3px;background:var(--mention-bg);color:var(--mention-text);font-weight:500;cursor:default;transition:background .1s,color .1s}
.mention:hover{background:var(--mention-hover);color:#fff}
.spoiler{background:var(--spoiler);color:transparent;border-radius:3px;cursor:pointer;padding:0 2px;user-select:none}
.spoiler *{visibility:hidden}
.spoiler.revealed{background:var(--spoiler-open);color:inherit;user-select:text}
.spoiler.revealed *{visibility:visible}
.emoji{width:22px;height:22px;object-fit:contain;vertical-align:-.4em;display:inline-block}
.emoji.jumbo{width:48px;height:48px;vertical-align:-.9em}
.uemoji{font-size:1.2em;line-height:1}
.reply{position:relative;display:flex;align-items:center;gap:4px;margin:0 0 3px;max-width:100%;font-size:.875rem;color:var(--muted);text-decoration:none!important;white-space:nowrap;overflow:hidden;line-height:1.1rem}
.reply:hover .reply-text{color:var(--header)}
.reply-spine{position:absolute;left:-36px;top:50%;width:33px;height:10px;border-left:2px solid var(--quote);border-top:2px solid var(--quote);border-top-left-radius:6px;margin-top:-1px;pointer-events:none}
.reply-avatar{width:16px;height:16px;border-radius:50%;flex:none;margin-left:2px}
.reply-name{font-weight:500;flex:none;color:var(--header)}
.reply-text{overflow:hidden;text-overflow:ellipsis;color:var(--muted)}
.reply-text *{display:inline!important;margin:0!important;font-size:inherit!important;border:0!important}
.reply-text pre,.reply-text blockquote{display:inline}
.faded{font-style:italic;color:var(--muted)}
.attachment{margin-top:4px;max-width:100%}
.att-image{display:block;max-width:min(100%,520px);max-height:350px;border-radius:8px;object-fit:contain;background:var(--panel2)}
.spoiler-img{filter:blur(44px);cursor:pointer}
.spoiler-img.revealed{filter:none}
.attachment.file{display:flex;align-items:center;gap:12px;max-width:432px;padding:10px 12px;background:var(--panel2);border:1px solid var(--border);border-radius:8px}
.file-icon{font-size:30px;line-height:1}
.file-meta{min-width:0}
.file-meta a{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.file-size{font-size:12px;color:var(--muted)}
.sticker img{width:160px;height:160px;object-fit:contain;margin-top:4px;display:block}
.sticker-fallback{display:inline-block;margin-top:4px;padding:6px 10px;background:var(--panel2);border-radius:8px;color:var(--muted);font-size:13px}
.embed{position:relative;display:grid;grid-template-columns:minmax(0,1fr);max-width:520px;margin-top:6px;padding:8px 16px 16px 12px;background:var(--panel2);border:1px solid var(--border);border-left:4px solid var(--embed-default);border-radius:4px}
.embed.has-thumb{grid-template-columns:minmax(0,1fr) auto;column-gap:16px}
.embed-body{min-width:0;display:flex;flex-direction:column;gap:8px;padding-top:8px}
.embed-thumb{grid-column:2;grid-row:1;margin-top:8px;max-width:80px;max-height:80px;border-radius:4px;object-fit:cover;justify-self:end}
.embed-provider{font-size:12px;color:var(--muted)}
.embed-author{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:600;color:var(--header)}
.embed-author a{color:var(--header)}
.embed-author-icon,.embed-footer-icon{width:24px;height:24px;border-radius:50%;object-fit:cover}
.embed-footer-icon{width:20px;height:20px}
.embed-title{font-size:16px;font-weight:700;color:var(--header);line-height:1.25rem}
.embed-title a{color:var(--link)}
.embed-description{font-size:14px;line-height:1.125rem}
.embed-fields{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:8px}
.embed-field{min-width:0}
.embed-field-name{font-size:14px;font-weight:700;color:var(--header);margin-bottom:2px}
.embed-field-value{font-size:14px;line-height:1.125rem}
.embed-image img{display:block;max-width:100%;max-height:300px;border-radius:4px}
.embed-footer{display:flex;align-items:center;flex-wrap:wrap;gap:4px 8px;font-size:12px;color:var(--muted);font-weight:500}
.embed-footer span{display:inline-flex;align-items:center;gap:8px}
.embed-footer .sep{color:var(--muted)}
.action-row{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.btn{display:inline-flex;align-items:center;gap:6px;height:32px;padding:2px 16px;border-radius:3px;font-size:14px;font-weight:500;color:#fff;cursor:default;text-decoration:none!important;user-select:none}
.btn .emoji{width:18px;height:18px;vertical-align:middle}
.btn-primary{background:#5865f2}.btn-secondary{background:#4e5058}.btn-success{background:#248046}.btn-danger{background:#da373c}.btn-link{background:#4e5058}
:root[data-theme=light] .btn-secondary,:root[data-theme=light] .btn-link{background:#6d6f78}
.btn.disabled{opacity:.5}
.btn .ext{font-size:12px;opacity:.8}
.select{display:flex;align-items:center;justify-content:space-between;min-width:260px;max-width:400px;padding:8px 12px;margin-top:8px;border-radius:4px;background:var(--panel3);border:1px solid var(--border);color:var(--muted);font-size:14px}
.select.disabled{opacity:.5}
.v2-container{margin-top:8px;max-width:520px;padding:12px 16px;border-radius:8px;background:var(--panel2);border:1px solid var(--border);border-left:4px solid var(--embed-default)}
.v2-sep{border:0;border-top:1px solid var(--border);margin:8px 0}
.forward{margin-top:4px;padding-left:12px;border-left:4px solid var(--quote)}
.forward-label{font-size:12px;color:var(--muted);margin-bottom:2px}
.reactions{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
.reaction{display:flex;align-items:center;gap:6px;padding:2px 8px;height:24px;border-radius:8px;background:var(--reaction-bg);border:1px solid var(--reaction-border);font-size:14px}
.reaction .reaction-emoji{width:16px;height:16px;vertical-align:middle}
.reaction .uemoji{font-size:16px}
.reaction .count{color:var(--muted);font-weight:600;font-size:13px}
.msg.system{display:flex;gap:12px;align-items:flex-start;padding-left:20px;margin-top:6px}
.system-icon{width:40px;text-align:center;color:#23a559;font-size:18px;flex:none}
.system-text{color:var(--muted);font-size:15px}
.foot{margin-top:18px;text-align:center;color:var(--muted);font-size:12px}
@media (max-width:640px){.msg{padding-right:12px;padding-left:64px}.avatar{left:10px}.gutter-time{width:64px}.wrap{padding:0 8px}.reply-spine{left:-30px;width:27px}.summary{padding:16px}}
@media print{.topbar .toggle{display:none}.topbar{position:static}body{background:#fff}}
"""

_SCRIPT = """
(function(){
  var root=document.documentElement,btn=document.getElementById('theme');
  function label(){btn.textContent=root.getAttribute('data-theme')==='light'?'🌙 Dark mode':'☀️ Light mode';}
  try{var saved=localStorage.getItem('ticket-transcript-theme');if(saved){root.setAttribute('data-theme',saved);}}catch(e){}
  label();
  document.addEventListener('click',function(e){
    var t=e.target.closest&&e.target.closest('.spoiler,.spoiler-img');
    if(t){t.classList.toggle('revealed');}
  });
  btn.addEventListener('click',function(){
    var next=root.getAttribute('data-theme')==='light'?'dark':'light';
    root.setAttribute('data-theme',next);
    try{localStorage.setItem('ticket-transcript-theme',next);}catch(e){}
    label();
  });
})();
"""


def _fact(key: str, value_html: str, *, danger: bool = False) -> str:
    return f'<div class="fact{" danger" if danger else ""}"><div class="k">{_esc(key)}</div><div class="v">{value_html}</div></div>'


def _person_html(name: str, avatar: str | None, user_id: int | None = None) -> str:
    image = f'<img src="{_esc(avatar)}" alt="">' if avatar else ""
    suffix = f' <span class="n" style="color:var(--muted);font-size:12px">({user_id})</span>' if user_id else ""
    return f"{image}<span>{_esc(name)}{suffix}</span>"


def _render_page(
    *,
    guild: discord.Guild | None,
    ticket: dict,
    messages: list,
    omitted: int,
    deleted_by: str,
    deleted_at: datetime,
    ctx: _Context,
    warning: str | None,
) -> str:
    by_id = {m.id: m for m in messages}
    counts: Counter[int] = Counter()
    people: dict[int, object] = {}
    for message in messages:
        counts[message.author.id] += 1
        people.setdefault(message.author.id, message.author)

    label = str(ticket.get("option_label") or "Support Ticket")
    number = ticket.get("ticket_number")
    number_text = f"#{int(number):04d}" if isinstance(number, int) or str(number).isdigit() else None
    title = f"{label} — Ticket {number_text}" if number_text else label
    guild_name = guild.name if guild else "Server"

    guild_icon = ""
    if guild is not None and getattr(guild, "icon", None):
        try:
            guild_icon = f'<img src="{_esc(guild.icon.replace(size=64, static_format="webp"))}" alt="">'
        except Exception:
            guild_icon = ""
    if not guild_icon:
        guild_icon = f'<div class="gicon">{_esc(guild_name[:1].upper() or "S")}</div>'

    opener_id = int(ticket.get("opener_id", 0) or 0)
    opener = people.get(opener_id) or (guild.get_member(opener_id) if guild is not None else None)
    opener_name = getattr(opener, "display_name", None) or str(ticket.get("opener_name") or "Unknown User")
    opener_avatar = _avatar_url(opener) if opener is not None else None

    facts = []
    if number_text:
        facts.append(_fact("Ticket", _esc(number_text)))
    facts.append(_fact("Ticket ID", _esc(ticket.get("channel_id") or ticket.get("_id") or "—")))
    facts.append(_fact("Category", _esc(label)))
    facts.append(_fact("Created by", _person_html(opener_name, opener_avatar, opener_id or None)))
    claimed_by = ticket.get("claimed_by")
    if claimed_by:
        claimer = people.get(int(claimed_by)) or (guild.get_member(int(claimed_by)) if guild is not None else None)
        claimer_name = getattr(claimer, "display_name", None) or str(ticket.get("claimed_by_name") or "Unknown User")
        facts.append(_fact("Claimed by", _person_html(claimer_name, _avatar_url(claimer) if claimer is not None else None, int(claimed_by))))
    else:
        facts.append(_fact("Claimed by", '<span style="color:var(--muted)">Nobody</span>'))
    opened_at = ticket.get("opened_at")
    if opened_at:
        facts.append(_fact("Opened", _esc(_stamp(datetime.fromtimestamp(int(opened_at), timezone.utc)) + " UTC")))
    if ticket.get("closed") and ticket.get("closed_by"):
        closer_name = str(ticket.get("closed_by_name") or f"User {ticket['closed_by']}")
        closer = people.get(int(ticket["closed_by"])) or (guild.get_member(int(ticket["closed_by"])) if guild is not None else None)
        if closer is not None:
            closer_name = getattr(closer, "display_name", closer_name)
        facts.append(_fact("Closed by", _person_html(closer_name, _avatar_url(closer) if closer is not None else None, int(ticket["closed_by"]))))
    if ticket.get("closed_at"):
        facts.append(_fact("Closed", _esc(_stamp(datetime.fromtimestamp(int(ticket["closed_at"]), timezone.utc)) + " UTC")))
    facts.append(_fact("Deleted by", _esc(deleted_by), danger=True))
    facts.append(_fact("Deleted", _esc(_stamp(deleted_at) + " UTC"), danger=True))
    facts.append(_fact("Messages", _esc(f"{len(messages) + omitted:,}")))

    chips = []
    for user_id, count in counts.most_common(24):
        user = people[user_id]
        name = getattr(user, "display_name", None) or user.name
        tag = ' <span class="tag">APP</span>' if getattr(user, "bot", False) else ""
        chips.append(f'<div class="chip"><img src="{_esc(_avatar_url(user))}" alt=""><span>{_esc(name)}{tag}</span><span class="n">{count}</span></div>')
    people_html = f'<div class="people"><div class="k">Participants</div><div class="chips">{"".join(chips)}</div></div>' if chips else ""

    rows: list[str] = []
    if omitted:
        rows.append(f'<div class="omitted">{omitted:,} earlier message(s) were left out to keep this file under the upload limit.</div>')
    if warning:
        rows.append(f'<div class="omitted">{_esc(warning)}</div>')
    previous = None
    last_day = None
    for message in messages:
        try:
            day = _utc(message.created_at).date()
            if day != last_day:
                rows.append(f'<div class="divider"><span>{_esc(_date_long(_utc(message.created_at)))}</span></div>')
                last_day = day
                previous = None
            piece = _render_message(message, previous, by_id, ctx)
        except Exception:
            piece = '<div class="msg first"><div class="content faded">This message could not be rendered.</div></div>'
        if piece:
            rows.append(piece)
        previous = message
    if not messages:
        rows.append('<div class="omitted">There were no messages in this ticket.</div>')

    generated = datetime.now(timezone.utc)
    return f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} · Transcript</title>
<style>{_CSS}</style>
</head>
<body>
<div class="topbar">
  <div class="brand">{guild_icon}<div><div class="gname">{_esc(guild_name)}</div><div class="gsub">Ticket transcript</div></div></div>
  <button class="toggle" id="theme" type="button">☀️ Light mode</button>
</div>
<div class="wrap">
  <section class="summary">
    <h1>🎫 {_esc(title)}</h1>
    <div class="lead">Archived when the ticket was deleted. All times are shown in UTC.</div>
    <div class="facts">{"".join(facts)}</div>
    {people_html}
  </section>
  <section class="chat">
    <div class="chat-head"><span class="hash">🎫</span><span>{_esc(label)}</span><span class="sub">{len(messages) + omitted:,} messages</span></div>
    {"".join(rows)}
  </section>
  <div class="foot">Generated {_esc(_stamp(generated))} UTC · {_esc(guild_name)}</div>
</div>
<script>{_SCRIPT}</script>
</body>
</html>"""


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------

async def _collect_inline_images(messages: list, budget: int) -> dict[int, str]:
    images: dict[int, str] = {}
    used = 0
    for message in messages:
        for attachment in message.attachments:
            if not _is_image(attachment):
                continue
            size = int(getattr(attachment, "size", 0) or 0)
            content_type = (getattr(attachment, "content_type", None) or mimetypes.guess_type(attachment.filename)[0] or "image/png").split(";")[0].lower()
            if content_type not in INLINE_IMAGE_TYPES or size > MAX_INLINE_IMAGE_BYTES or used + size > budget:
                continue
            try:
                raw = await attachment.read()
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                continue
            used += len(raw)
            images[attachment.id] = f"data:{content_type};base64,{base64.b64encode(raw).decode('ascii')}"
    return images


async def build_ticket_transcript(
    channel: discord.TextChannel,
    ticket: dict,
    *,
    deleted_by: str,
    deleted_at: datetime | None = None,
) -> tuple[discord.File, int]:
    """Return ``(file, message_count)`` for the ticket channel as a standalone HTML page."""
    guild = channel.guild
    deleted_at = deleted_at or datetime.now(timezone.utc)
    warning = None
    messages: list = []
    try:
        async for message in channel.history(limit=MAX_MESSAGES, oldest_first=True):
            messages.append(message)
    except discord.HTTPException as exc:
        warning = f"Some messages could not be retrieved: {exc}"

    users: dict[int, object] = {}
    for message in messages:
        users.setdefault(message.author.id, message.author)
        for mentioned in message.mentions:
            users.setdefault(mentioned.id, mentioned)

    try:
        limit = int(getattr(guild, "filesize_limit", 10 * 1024 * 1024))
    except Exception:
        limit = 10 * 1024 * 1024
    cap = max(limit - 96 * 1024, 512 * 1024)
    inline_budget = int(limit * 0.55 / 1.34)
    inline_images = await _collect_inline_images(messages, inline_budget)

    def render(shown: list, omitted: int, images: dict[int, str]) -> bytes:
        ctx = _Context(guild, channel.id, users, images)
        page = _render_page(guild=guild, ticket=ticket, messages=shown, omitted=omitted, deleted_by=deleted_by, deleted_at=deleted_at, ctx=ctx, warning=warning)
        return page.encode("utf-8", errors="replace")

    shown, omitted = messages, 0
    body = render(shown, omitted, inline_images)
    if len(body) > cap and inline_images:
        body = render(shown, omitted, {})
    while len(body) > cap and len(shown) > 50:
        drop = max(len(shown) // 4, 1)
        shown = shown[drop:]
        omitted += drop
        body = render(shown, omitted, {})

    number = ticket.get("ticket_number")
    stem = f"ticket-{int(number):04d}" if str(number).isdigit() else f"ticket-{channel.id}"
    return discord.File(io.BytesIO(body), filename=f"{stem}-transcript.html"), len(messages)