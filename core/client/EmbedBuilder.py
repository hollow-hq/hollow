"""
Embed script - lets users script Discord embeds with a small tag syntax.

Example:
    {embed}$v{title: Hello}$v{description: Line 1\nLine 2}$v{url: https://title.example}
    $v{author: Author && https://icon.url && https://author.link}
    $v{field: Field && Hello! inline}$v{footer: ok && https://icon.url}
    $v{color: #ff0000}$v{timestamp}

Tags (separated by `$v`):
    {title: text}
    {description: text}
    {url: https://...}                       -> makes the title clickable
    {author: name && icon_url && link}       -> icon_url and link are optional
    {field: name && value [inline]}          -> "inline" at the end (or as 3rd part) = inline field
    {footer: text && icon_url}               -> icon_url is optional
    {color: #rrggbb}                         -> #000000 = bot default (COLORS.neutral)
    {thumbnail: https://...}
    {image: https://...}
    {timestamp}                              -> adds a timestamp to the footer

Use "\\n" inside any text for a newline.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Union

import discord
from discord.ext import commands

from core.config import COLORS

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

HEADER = "{embed}"
SEPARATOR = "$v"

# {key} or {key: value}  (value may contain newlines / braces)
_TAG_RE = re.compile(r"^\{\s*([a-zA-Z_]+)\s*(?::\s*(.*?))?\s*\}$", re.DOTALL)
_HEX_RE = re.compile(r"^[0-9a-f]{6}$")
_INLINE_RE = re.compile(r"\s+inline$", re.IGNORECASE)

# Discord embed limits
LIMITS = {
    "title": 256,
    "description": 4096,
    "field_name": 256,
    "field_value": 1024,
    "fields": 25,
    "footer": 2048,
    "author": 256,
    "total": 6000,
}

Target = Union[commands.Context, discord.Interaction, discord.abc.Messageable]


class EmbedSyntaxError(ValueError):
    """Raised when the embed code is invalid. The message is user-friendly."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _text(value: str) -> str:
    """Turn the literal sequence \\n into a real newline."""
    return value.replace("\\n", "\n")


def _split(value: str) -> list[str]:
    """Split a tag value on '&&' and strip every part."""
    return [part.strip() for part in value.split("&&")]


def _check_len(label: str, value: str, limit: int) -> None:
    if len(value) > limit:
        raise EmbedSyntaxError(f"`{label}` is too long ({len(value)}/{limit} characters).")


def _check_url(label: str, value: str) -> str:
    if not re.match(r"^https?://\S+$", value):
        raise EmbedSyntaxError(f"`{label}` must be a valid URL starting with http:// or https://.")
    return value


def _require(tag: str, value: Optional[str]) -> str:
    if value is None or not value.strip():
        raise EmbedSyntaxError(f"`{{{tag}}}` needs a value, e.g. `{{{tag}: something}}`.")
    return value.strip()


def _parse_color(value: str) -> Union[int, discord.Color]:
    raw = value.strip().lower().removeprefix("#").removeprefix("0x")
    if not _HEX_RE.match(raw):
        raise EmbedSyntaxError("`color` must be a hex code like `#ff0000`.")
    if raw == "000000":
        return COLORS.neutral  # #000000 = bot default color
    return int(raw, 16)


def _parse_field(value: str) -> tuple[str, str, bool]:
    parts = _split(value)
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise EmbedSyntaxError("`field` must look like `{field: Name && Value}` (add `inline` at the end for inline).")

    name, val, inline = parts[0], parts[1], False

    if len(parts) > 2 and parts[2].lower() == "inline":
        inline = True
    else:
        match = _INLINE_RE.search(val)
        if match:
            inline = True
            val = val[: match.start()].rstrip()

    if not val:
        raise EmbedSyntaxError("`field` value can't be empty.")
    return _text(name), _text(val), inline


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def to_object(code: str) -> discord.Embed:
    """
    Convert an embed script into a `discord.Embed`.

    Raises:
        EmbedSyntaxError: if the script is invalid (message is safe to show to users).
    """
    code = code.strip()
    if not code.lower().startswith(HEADER):
        raise EmbedSyntaxError(f"Embed code must start with `{HEADER}`.")

    embed = discord.Embed(color=COLORS.neutral)
    body = code[len(HEADER):]

    used_any = False

    for chunk in body.split(SEPARATOR):
        chunk = chunk.strip()
        if not chunk:
            continue

        match = _TAG_RE.match(chunk)
        if not match:
            raise EmbedSyntaxError(f"Invalid tag: `{chunk[:50]}`. Tags look like `{{name: value}}`.")

        tag, value = match.group(1).lower(), match.group(2)
        used_any = True

        if tag == "title":
            title = _text(_require(tag, value))
            _check_len("title", title, LIMITS["title"])
            embed.title = title

        elif tag == "description":
            description = _text(_require(tag, value))
            _check_len("description", description, LIMITS["description"])
            embed.description = description

        elif tag == "url":
            embed.url = _check_url("url", _require(tag, value))

        elif tag == "author":
            parts = _split(_require(tag, value))
            name = _text(parts[0])
            _check_len("author name", name, LIMITS["author"])
            icon = _check_url("author icon", parts[1]) if len(parts) > 1 and parts[1] else None
            link = _check_url("author link", parts[2]) if len(parts) > 2 and parts[2] else None
            embed.set_author(name=name, icon_url=icon, url=link)

        elif tag == "field":
            if len(embed.fields) >= LIMITS["fields"]:
                raise EmbedSyntaxError(f"An embed can't have more than {LIMITS['fields']} fields.")
            name, val, inline = _parse_field(_require(tag, value))
            _check_len("field name", name, LIMITS["field_name"])
            _check_len("field value", val, LIMITS["field_value"])
            embed.add_field(name=name, value=val, inline=inline)

        elif tag == "footer":
            parts = _split(_require(tag, value))
            text = _text(parts[0])
            _check_len("footer", text, LIMITS["footer"])
            icon = _check_url("footer icon", parts[1]) if len(parts) > 1 and parts[1] else None
            embed.set_footer(text=text, icon_url=icon)

        elif tag == "color":
            embed.color = _parse_color(_require(tag, value))

        elif tag == "thumbnail":
            embed.set_thumbnail(url=_check_url("thumbnail", _require(tag, value)))

        elif tag == "image":
            embed.set_image(url=_check_url("image", _require(tag, value)))

        elif tag == "timestamp":
            embed.timestamp = discord.utils.utcnow()

        else:
            raise EmbedSyntaxError(f"Unknown tag `{tag}`.")

    if not used_any:
        raise EmbedSyntaxError("Your embed is empty. Add at least one tag, e.g. `{title: Hello}`.")

    if len(embed) > LIMITS["total"]:
        raise EmbedSyntaxError(f"The embed is too big ({len(embed)}/{LIMITS['total']} total characters).")

    return embed


def build_kwargs(code: str, *, view: Optional[Any] = None) -> dict:
    """
    Convert an embed script into keyword arguments for a `send`/`edit` call.

    A script that doesn't start with `{embed}` is treated as plain text.
    """
    code = (code or "").strip()
    if not code:
        return {}

    if not code.lower().startswith(HEADER):
        return {"content": code, **( {"view": view} if view is not None else {}) }

    embed = to_object(code)
    kwargs: dict = {"embed": embed}
    if view is not None and getattr(view, "children", None):
        kwargs["view"] = view
    return kwargs


async def send_embed(
    target: Target,
    code: str,
    *,
    content: Optional[str] = None,
    ephemeral: bool = False,
    reply_on_error: bool = True,
) -> Optional[discord.Message]:
    """
    Build an embed from `code` and send it to `target`.

    `target` can be a commands.Context, a discord.Interaction, or any Messageable
    (TextChannel, DM, Member, ...).

    If the script is invalid and `reply_on_error` is True, the error is sent to the
    user and None is returned. If False, EmbedSyntaxError is raised instead.
    """
    try:
        embed = to_object(code)
    except EmbedSyntaxError as err:
        if not reply_on_error:
            raise
        await _send(target, content=f"❌ {err}", ephemeral=True)
        return None

    return await _send(target, content=content, embed=embed, ephemeral=ephemeral)


async def _send(
    target: Target,
    *,
    content: Optional[str] = None,
    embed: Optional[discord.Embed] = None,
    ephemeral: bool = False,
) -> Optional[discord.Message]:
    """Send to a Context, Interaction or Messageable."""
    kwargs = {"content": content, "embed": embed} if embed else {"content": content}
    allowed = discord.AllowedMentions.none()

    if isinstance(target, discord.Interaction):
        if target.response.is_done():
            return await target.followup.send(**kwargs, ephemeral=ephemeral, allowed_mentions=allowed, wait=True)
        await target.response.send_message(**kwargs, ephemeral=ephemeral, allowed_mentions=allowed)
        return await target.original_response()

    if isinstance(target, commands.Context):
        return await target.send(**kwargs, allowed_mentions=allowed)

    return await target.send(**kwargs, allowed_mentions=allowed)