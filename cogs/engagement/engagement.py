from __future__ import annotations

import asyncio
import json
import math
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.client.embed import Embed
from core.client.commands import has_permissions, hybrid_command, hybrid_group
from core.client.EmbedBuilder import build_kwargs
from core.client.tagscript import TagScriptParser
from core.config import COLORS, EMOJIS
from core.context import hollowHelp
from core.logger import log


# =============================================================================
# Helpers
# =============================================================================

XP_PER_LEVEL = 100  # XP required to advance from level N to level N + 1.

# Compiled regex reused for both giveaway and AFK duration parsing. Accepts
# things like "1h", "30m", "2d", "45s" or a plain integer number (treated as
# minutes when standalone for AFK, seconds for giveaways).
DURATION_RE = re.compile(
    r"^\s*(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks|mo|month|months|y|year|years)?\s*$",
    re.IGNORECASE,
)

# Recognises Discord message links of the form
# https://discord.com/channels/<guild>/<channel>/<message>
MESSAGE_LINK_RE = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord\.com/channels/\d+/\d+/\d+",
    re.IGNORECASE,
)


def parse_duration_seconds(arg: Optional[str | int]) -> Optional[int]:
    """Parse a duration string into seconds. Falls back gracefully."""
    if arg is None:
        return None
    if isinstance(arg, (int, float)):
        return max(0, int(arg))
    arg = str(arg).strip()
    if not arg:
        return None
    m = DURATION_RE.match(arg)
    if not m:
        return None
    amount = int(m.group(1))
    unit = (m.group(2) or "s").lower()
    if unit in ("s", "sec", "secs", "second", "seconds"):
        return amount
    if unit in ("m", "min", "mins", "minute", "minutes"):
        return amount * 60
    if unit in ("h", "hr", "hrs", "hour", "hours"):
        return amount * 3600
    if unit in ("d", "day", "days"):
        return amount * 86400
    if unit in ("w", "week", "weeks"):
        return amount * 604800
    if unit in ("mo", "month", "months"):
        return amount * 30 * 86400
    if unit in ("y", "year", "years"):
        return amount * 365 * 86400
    return amount


def parse_interval_to_minutes(arg: Optional[str | int]) -> Optional[int]:
    """Parse a duration string into minutes (used by AFK autoclear timeout)."""
    secs = parse_duration_seconds(arg)
    if secs is None:
        return None
    return max(0, secs // 60)


def parse_message_id_or_link(arg: Optional[str], ctx: Optional[commands.Context] = None) -> Optional[int]:
    """Extract a message ID from either an ID string or a discord message link.

    If ``ctx`` is provided and ``ctx.message.reference`` exists, the replied-to
    message ID is preferred (so users can simply reply to a giveaway).
    """
    if ctx is not None and getattr(ctx.message, "reference", None) is not None:
        ref = ctx.message.reference
        if ref.resolved and isinstance(ref.resolved, discord.Message):
            return ref.resolved.id
        if ref.message_id:
            return ref.message_id

    if arg is None:
        return None
    s = str(arg).strip()
    if not s:
        return None
    m = MESSAGE_LINK_RE.search(s)
    if m:
        parts = m.group(0).rstrip("/").split("/")
        try:
            return int(parts[-1])
        except (ValueError, IndexError):
            return None
    if s.isdigit():
        return int(s)
    return None


def level_for_xp(xp: int) -> int:
    """Convert total XP into a level integer (level 0 == 0 XP)."""
    if xp <= 0:
        return 0
    return int((xp // XP_PER_LEVEL))


def xp_progress(xp: int) -> tuple[int, int, int]:
    """Return ``(level, current_xp_into_level, xp_required_for_next)``."""
    lvl = level_for_xp(xp)
    floor = lvl * XP_PER_LEVEL
    into = max(0, xp - floor)
    return lvl, into, XP_PER_LEVEL


def _fmt_relative(dt: Optional[datetime], *, suffix: str = "") -> str:
    """Render *dt* using Discord's relative timestamp formatting."""
    if dt is None:
        return "unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"{discord.utils.format_dt(dt, style='R')}{suffix}"


# Discord caps the *sum* of all displayable text in a View (button labels,
# select option labels, placeholders, emoji names that count as text, etc.)
# at 4000 characters per message. Templates authored by users may include
# rows of custom buttons; we must guarantee that the cumulative payload never
# exceeds the limit, otherwise Discord rejects the request with HTTP 400
# ("Components displayable text size exceeds maximum size of 4000").
COMPONENT_TEXT_BUDGET = 3900


def _truncate_component_text(value: Optional[str], limit: int = 80) -> Optional[str]:
    """Safely cut a component's displayable text to ``limit`` characters."""
    if value is None:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _view_displayable_length(view: Optional[discord.ui.View]) -> int:
    """Sum of every label/option/placeholder that lands on a View's payload."""
    if view is None:
        return 0
    total = 0
    for child in view.children:
        # Buttons carry ``label`` and a unicode/animated emoji; Discord counts
        # the emoji name/id too, but the worst-case blow-up is the label.
        label = getattr(child, "label", None)
        if label:
            total += len(label)
        emoji = getattr(child, "emoji", None)
        if emoji is not None:
            # ``str(emoji)`` returns "<:name:id>" for custom or the char itself.
            total += len(str(emoji))
        placeholder = getattr(child, "placeholder", None)
        if placeholder:
            total += len(placeholder)
        # SelectOption objects on Selects each contribute label + description
        options = getattr(child, "options", None) or []
        for option in options:
            total += len(getattr(option, "label", "") or "")
            total += len(getattr(option, "value", "") or "")
            description = getattr(option, "description", None)
            if description:
                total += len(description)
    return total


def _trim_view(view: Optional[discord.ui.View], *, budget: int = COMPONENT_TEXT_BUDGET) -> Optional[discord.ui.View]:
    """Return *view* with overflowing components dropped/shortened so we stay
    under Discord's 4000 character cap on displayable text."""
    if view is None:
        return None
    # First pass: drop entire children whose own footprint already blows the
    # budget (defensive — shouldn't happen in practice).
    kept_children = []
    running_total = 0
    for child in view.children:
        # Snapshot the child's contribution before deciding
        running_total_backup = running_total
        # Worst-case contribution: 1 component, optimistically estimate its
        # current footprint by serializing to a fresh view.
        try:
            child_cost = _view_displayable_length(_SingleChildView(child))  # type: ignore[arg-type]
        except Exception:
            child_cost = 200  # safe upper-bound estimate
        if running_total_backup + child_cost > budget:
            # Drop this child entirely rather than truncate individual labels.
            log.debug(
                "render_message: discarding oversized component %r", child
            )
            continue
        running_total = running_total_backup + child_cost
        kept_children.append(child)
    if len(kept_children) == len(view.children):
        return view
    new_view = view.__class__(timeout=getattr(view, "timeout", None))
    # Copy attributes that some views rely on (e.g. cog) so callbacks still work.
    for attr in ("cog", "ctx", "bot", "author_id", "host_id", "cog_obj",
                 "interaction", "is_persistent", "message_id"):
        if hasattr(view, attr):
            try:
                setattr(new_view, attr, getattr(view, attr))
            except Exception:
                pass
    for child in kept_children:
        # Shorten any runaway label / placeholder so individual components
        # never push us back over the cap either.
        if hasattr(child, "label"):
            child.label = _truncate_component_text(child.label)
        if hasattr(child, "placeholder") and getattr(child, "placeholder", None):
            child.placeholder = _truncate_component_text(child.placeholder)
        for option in getattr(child, "options", None) or []:
            option.label = _truncate_component_text(option.label)
            if getattr(option, "description", None):
                option.description = _truncate_component_text(option.description)
        try:
            new_view.add_item(child)
        except Exception:
            # If a component refuses to be added we silently drop it rather
            # than failing the entire render.
            log.debug("render_message: failed to re-add component %r", child)
    return new_view


class _SingleChildView(discord.ui.View):
    """Helper used by ``_view_displayable_length`` to measure a single child."""

    def __init__(self, child: discord.ui.Item) -> None:
        super().__init__(timeout=None)
        self.add_item(child)


async def render_message(
    cog: "Engagement",
    raw: Optional[str],
    *,
    author: discord.abc.User,
    guild: Optional[discord.Guild] = None,
    channel: Optional[discord.abc.GuildChannel] = None,
    extra: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Run raw text through TagScript and the embed script builder, returning send kwargs."""
    if not raw:
        return {}

    text = raw
    try:
        text = await TagScriptParser.parse(text, author, guild, channel, cog.bot)
    except Exception:
        pass

    if extra:
        for key, value in extra.items():
            token = "{" + key + "}"
            text = text.replace(token, value)

    kwargs: dict[str, Any] = build_kwargs(text)
    view = kwargs.get("view")
    if view is not None and getattr(view, "children", None):
        sent_view = _trim_view(view)
        if sent_view is not None and len(sent_view.children) > 0:
            kwargs["view"] = sent_view
        else:
            kwargs.pop("view", None)
    return kwargs


def _parse_bool(value: str) -> Optional[bool]:
    lowered = (value or "").strip().lower()
    if lowered in ("true", "yes", "y", "1", "on", "enable", "enabled"):
        return True
    if lowered in ("false", "no", "n", "0", "off", "disable", "disabled"):
        return False
    return None
# =============================================================================
# Modals / UI components
# =============================================================================


class SuggestionModal(discord.ui.Modal, title="Submit a suggestion"):
    """Modal that collects the suggestion body and posts an embed to the panel channel."""

    def __init__(self, cog: "Engagement", opener: discord.abc.User) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.opener = opener
        self.body = discord.ui.TextInput(
            label="Your suggestion",
            style=discord.TextStyle.paragraph,
            placeholder="Describe your suggestion in detail...",
            max_length=1024,
            required=True,
        )
        self.add_item(self.body)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            record, message = await self.cog.post_suggestion(
                interaction.guild,  # type: ignore[arg-type]
                self.opener,
                self.body.value.strip(),
            )
        except ValueError as err:
            return await interaction.followup.send(
                embed=Embed(description=f"{EMOJIS.DENY} {err}", color=COLORS.deny),
                ephemeral=True,
            )
        except discord.HTTPException as err:
            return await interaction.followup.send(
                embed=Embed(
                    description=f"{EMOJIS.DENY} I could not deliver your suggestion: `{err}`",
                    color=COLORS.deny,
                ),
                ephemeral=True,
            )

        view = SuggestionActionView(self.cog, record["id"])
        try:
            await message.edit(view=view)
        except discord.HTTPException:
            pass

        await interaction.followup.send(
            embed=Embed(
                description=f"{EMOJIS.APPROVE} Your suggestion has been posted in {message.channel.mention}.",
                color=COLORS.approve,
            ),
            ephemeral=True,
        )


class SuggestPanelView(discord.ui.View):
    """Persistent view attached to the server's suggestion panel message."""

    def __init__(self, cog: "Engagement") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return False
        config = await self.cog.get_suggestions_config(interaction.guild.id)
        if not config.get("channel_id"):
            await interaction.response.send_message(
                embed=Embed(
                    description=f"{EMOJIS.WARN} Suggestions are not configured for this server yet.",
                    color=COLORS.warn,
                ),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Make a suggestion",
        style=discord.ButtonStyle.success,
        emoji="💡",
        custom_id="ENGAGEMENT:SUGGEST_OPEN",
    )
    async def open_modal(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        # Caller check (members, not bots)
        if interaction.user.bot:
            return await interaction.response.send_message(
                embed=Embed(
                    description=f"{EMOJIS.WARN} Bots cannot submit suggestions.",
                    color=COLORS.warn,
                ),
                ephemeral=True,
            )

        # Ensure the inviter is not blacklisted.
        blacklisted = await self.cog.is_blacklisted(interaction.guild_id or 0, interaction.user.id)
        if blacklisted:
            return await interaction.response.send_message(
                embed=Embed(
                    description=(
                        f"{EMOJIS.DENY} You are **blacklisted** from making suggestions in this server."
                    ),
                    color=COLORS.deny,
                ),
                ephemeral=True,
            )

        await interaction.response.send_modal(SuggestionModal(self.cog, interaction.user))


class SuggestionActionView(discord.ui.View):
    """Approve / Deny buttons attached to each posted suggestion."""

    def __init__(self, cog: "Engagement", suggestion_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.suggestion_id = suggestion_id

        approve = SuggestionActionButton(
            style=discord.ButtonStyle.success,
            label="Approve",
            emoji="✅",
            custom_id=f"ENGAGEMENT:SUGGEST_APPROVE:{suggestion_id}",
            action="approve",
        )
        approve.cog = cog
        approve.suggestion_id = suggestion_id
        self.add_item(approve)

        deny = SuggestionActionButton(
            style=discord.ButtonStyle.danger,
            label="Deny",
            emoji="❌",
            custom_id=f"ENGAGEMENT:SUGGEST_DENY:{suggestion_id}",
            action="deny",
        )
        deny.cog = cog
        deny.suggestion_id = suggestion_id
        self.add_item(deny)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member) or interaction.guild is None:
            return False

        is_owner = await interaction.client.is_owner(member)  # type: ignore[attr-defined]
        if not (
            is_owner
            or member.guild_permissions.administrator
            or member.guild_permissions.manage_guild
        ):
            await interaction.response.send_message(
                embed=Embed(
                    description=f"{EMOJIS.DENY} Only **administrators** can manage suggestions.",
                    color=COLORS.deny,
                ),
                ephemeral=True,
            )
            return False
        return True


class SuggestionActionButton(discord.ui.Button["SuggestionActionView"]):
    """A persistent button that resolves which suggestion to act on."""

    cog: "Engagement"
    suggestion_id: int

    def __init__(self, *, action: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        record = await self.cog.get_suggestion(self.suggestion_id)
        if not record:
            return await interaction.response.send_message(
                embed=Embed(
                    description=f"{EMOJIS.WARN} That suggestion no longer exists.",
                    color=COLORS.warn,
                ),
                ephemeral=True,
            )
        await interaction.response.defer()
        await self.cog.respond_to_suggestion(
            interaction.guild,  # type: ignore[arg-type]
            record,
            status="approved" if self.action == "approve" else "denied",
            responder=interaction.user,
        )


class SuggestSetupView(discord.ui.View):
    """Step-1 wizard: pick a text channel for the suggestion panel."""

    def __init__(self, cog: "Engagement", ctx: commands.Context) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                embed=Embed(
                    description=f"{EMOJIS.WARN} You're not the author of this menu.",
                    color=COLORS.warn,
                ),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder="Select a channel for suggestions...",
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
    )
    async def channel_picked(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        channel_id = int(select.values[0])
        await interaction.response.send_modal(
            SuggestionSetupMessageModal(self.cog, self.ctx.author.id, channel_id)
        )


class SuggestionSetupMessageModal(discord.ui.Modal, title="Suggestions — Panel message"):
    def __init__(self, cog: "Engagement", opener_id: int, channel_id: int) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.opener_id = opener_id
        self.channel_id = channel_id
        self.message = discord.ui.TextInput(
            label="Panel message (Embed codes + TagScript)",
            style=discord.TextStyle.paragraph,
            placeholder=(
                "{embed}$vtitle:Suggestions$vdescription:Click the button below to submit a suggestion.$v"
                "color:5865F2$vfooter:Powered by hollow"
            ),
            required=False,
            max_length=2000,
        )
        self.add_item(self.message)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        channel = interaction.guild.get_channel(self.channel_id) if interaction.guild else None  # type: ignore[union-attr]
        if channel is None or not isinstance(channel, discord.TextChannel):
            return await interaction.followup.send(
                embed=Embed(
                    description=f"{EMOJIS.DENY} The selected channel could not be found.",
                    color=COLORS.deny,
                ),
                ephemeral=True,
            )

        guild = interaction.guild  # type: ignore[union-attr]
        member = guild.get_member(self.opener_id) if guild else interaction.user

        template = self.message.value.strip() or (
            "{embed}$vtitle:Suggestions$vdescription:Press the button below to submit a suggestion.$v"
            "color:5865F2"
        )

        try:
            kwargs = await render_message(
                self.cog,
                template,
                author=member or interaction.user,
                guild=guild,
                channel=channel,
            )
        except Exception as err:
            return await interaction.followup.send(
                embed=Embed(
                    description=f"{EMOJIS.DENY} Failed to render the panel: `{err}`",
                    color=COLORS.deny,
                ),
                ephemeral=True,
            )

        # The suggestion panel must always expose the "Make a suggestion"
        # button. We deliberately ignore any buttons/components that the
        # user's template introduced — appending them on top of the panel
        # view risks blowing through Discord's 4000-character budget on
        # displayable component text, which manifests as HTTP 400
        # ("Components displayable text size exceeds maximum size of 4000").
        # The template's embed body itself is still respected (it goes via
        # ``content``/``embed``), only the interactive surface is pinned to
        # the suggestion button so the panel remains a stable UX.
        kwargs["view"] = SuggestPanelView(self.cog)

        try:
            sent = await channel.send(**kwargs)
        except discord.HTTPException as err:
            return await interaction.followup.send(
                embed=Embed(
                    description=f"{EMOJIS.DENY} Could not post the panel: `{err}`",
                    color=COLORS.deny,
                ),
                ephemeral=True,
            )

        await self.cog.save_suggestions_config(
            guild.id,  # type: ignore[union-attr]
            channel_id=self.channel_id,
            panel_message_id=sent.id,
            panel_template=template,
        )

        await interaction.followup.send(
            embed=Embed(
                description=(
                    f"{EMOJIS.APPROVE} Suggestions have been configured. "
                    f"Panel posted in {channel.mention}."
                ),
                color=COLORS.approve,
            ),
            ephemeral=True,
        )


class StarboardEditModal(discord.ui.Modal, title="Edit starboard"):
    """Used by ;starboard edit to configure a starboard in a single form."""

    def __init__(
        self,
        cog: "Engagement",
        guild_id: int,
        name: str,
        current: dict[str, Any],
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = guild_id
        self.name = name
        self.channel_id = discord.ui.TextInput(
            label="Channel ID",
            placeholder=str(current.get("channel_id") or ""),
            default=str(current.get("channel_id") or ""),
            required=True,
            max_length=32,
        )
        self.emoji = discord.ui.TextInput(
            label="Emoji (unicode or <:name:id>)",
            placeholder=str(current.get("emoji") or "⭐"),
            default=str(current.get("emoji") or "⭐"),
            required=True,
            max_length=64,
        )
        self.threshold = discord.ui.TextInput(
            label="Reaction threshold",
            placeholder=str(current.get("threshold") or 3),
            default=str(current.get("threshold") or 3),
            required=True,
            max_length=4,
        )
        self.color = discord.ui.TextInput(
            label="Embed colour (hex)",
            placeholder=str(current.get("color") or "5865F2"),
            default=str(current.get("color") or "5865F2"),
            required=False,
            max_length=8,
        )
        self.add_item(self.channel_id)
        self.add_item(self.emoji)
        self.add_item(self.threshold)
        self.add_item(self.color)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            threshold = int(self.threshold.value)
            if threshold < 1:
                raise ValueError
        except ValueError:
            return await interaction.followup.send(
                embed=Embed(
                    description=f"{EMOJIS.DENY} Threshold must be a positive integer.",
                    color=COLORS.deny,
                ),
                ephemeral=True,
            )

        color_raw = (self.color.value or "5865F2").strip().replace("0x", "").replace("#", "")
        try:
            color = int(color_raw, 16) & 0xFFFFFF
        except ValueError:
            return await interaction.followup.send(
                embed=Embed(
                    description=f"{EMOJIS.DENY} Colour must be hex (e.g. `5865F2`).",
                    color=COLORS.deny,
                ),
                ephemeral=True,
            )

        try:
            channel_id = int(self.channel_id.value)
        except ValueError:
            return await interaction.followup.send(
                embed=Embed(
                    description=f"{EMOJIS.DENY} Channel ID must be numeric.",
                    color=COLORS.deny,
                ),
                ephemeral=True,
            )

        try:
            await self.cog.update_starboard(
                self.guild_id,
                name=self.name,
                channel_id=channel_id,
                emoji=self.emoji.value.strip(),
                threshold=threshold,
                color=color,
            )
        except ValueError as err:
            return await interaction.followup.send(
                embed=Embed(
                    description=f"{EMOJIS.DENY} {err}",
                    color=COLORS.deny,
                ),
                ephemeral=True,
            )

        await interaction.followup.send(
            embed=Embed(
                description=f"{EMOJIS.APPROVE} Starboard **{self.name}** has been updated.",
                color=COLORS.approve,
            ),
            ephemeral=True,
        )


# =============================================================================
# Modals / UI components — Giveaways
# =============================================================================

# Emoji used by the giveaway entry button (configurable per guild once added
# in the future; kept as a module-level constant for now).
GIVEAWAY_EMOJI = "🎉"

# Reaction emoji entries use to enter a giveaway. Stored separately from the
# button below so listeners can identify the giveaway reaction at a glance.
GIVEAWAY_REACTION = "🎉"


class GiveawayStartModal(discord.ui.Modal):
    """Step-1 modal of the giveaway creation wizard: prize, winners, duration."""

    def __init__(self, cog: "Engagement", opener_id: int, channel_id: int) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.opener_id = opener_id
        self.channel_id = channel_id

        self.prize = discord.ui.TextInput(
            label="Prize",
            style=discord.TextStyle.short,
            placeholder="Nitro, $10 gift card, custom role...",
            max_length=120,
            required=True,
        )
        self.winners = discord.ui.TextInput(
            label="Winners",
            style=discord.TextStyle.short,
            placeholder="1",
            default="1",
            max_length=4,
            required=True,
        )
        self.duration = discord.ui.TextInput(
            label="Duration (e.g. 1h, 30m, 1d)",
            style=discord.TextStyle.short,
            placeholder="1h",
            default="1h",
            max_length=16,
            required=True,
        )
        self.add_item(self.prize)
        self.add_item(self.winners)
        self.add_item(self.duration)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        prize_raw = self.prize.value.strip()
        if not prize_raw:
            return await interaction.followup.send(
                embed=Embed(
                    description=f"{EMOJIS.DENY} Prize cannot be empty.",
                    color=COLORS.deny,
                ),
                ephemeral=True,
            )

        try:
            winners = int(self.winners.value.strip())
            if winners < 1:
                raise ValueError
            if winners > 50:
                return await interaction.followup.send(
                    embed=Embed(
                        description=f"{EMOJIS.DENY} You can have at most **50** winners.",
                        color=COLORS.deny,
                    ),
                    ephemeral=True,
                )
        except ValueError:
            return await interaction.followup.send(
                embed=Embed(
                    description=f"{EMOJIS.DENY} Winners must be a positive integer.",
                    color=COLORS.deny,
                ),
                ephemeral=True,
            )

        seconds = parse_duration_seconds(self.duration.value)
        if seconds is None or seconds < 30:
            return await interaction.followup.send(
                embed=Embed(
                    description=(
                        f"{EMOJIS.DENY} Duration must be a valid value of at least "
                        f"**30 seconds** (e.g. ``1h``, ``30m``)."
                    ),
                    color=COLORS.deny,
                ),
                ephemeral=True,
            )
        if seconds > 60 * 60 * 24 * 30:
            return await interaction.followup.send(
                embed=Embed(
                    description=f"{EMOJIS.DENY} Giveaway duration cannot exceed **30 days**.",
                    color=COLORS.deny,
                ),
                ephemeral=True,
            )

        guild = interaction.guild  # type: ignore[union-attr]
        member = guild.get_member(self.opener_id) if guild else interaction.user
        try:
            record, message = await self.cog.create_giveaway(
                guild,  # type: ignore[arg-type]
                member or interaction.user,
                prize=prize_raw,
                winners=winners,
                duration_seconds=seconds,
                channel_id=self.channel_id,
            )
        except ValueError as err:
            return await interaction.followup.send(
                embed=Embed(
                    description=f"{EMOJIS.DENY} {err}",
                    color=COLORS.deny,
                ),
                ephemeral=True,
            )
        except discord.HTTPException as err:
            return await interaction.followup.send(
                embed=Embed(
                    description=f"{EMOJIS.DENY} Failed to post the giveaway: `{err}`",
                    color=COLORS.deny,
                ),
                ephemeral=True,
            )

        await interaction.followup.send(
            embed=Embed(
                description=(
                    f"{EMOJIS.APPROVE} Giveaway has been created in "
                    f"<#{record['channel_id']}> — [jump to message]({message.jump_url})."
                ),
                color=COLORS.approve,
            ),
            ephemeral=True,
        )


class GiveawayStartView(discord.ui.View):
    """Step-0 view of the giveaway wizard: pick the channel it will run in."""

    def __init__(self, cog: "Engagement", ctx: commands.Context) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                embed=Embed(
                    description=f"{EMOJIS.WARN} You're not the **author** of this menu.",
                    color=COLORS.warn,
                ),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder="Select a channel for the giveaway...",
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
    )
    async def channel_picked(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        channel_id = int(select.values[0])
        await interaction.response.send_modal(
            GiveawayStartModal(self.cog, self.ctx.author.id, channel_id)
        )


class GiveawayEntryButton(discord.ui.Button["GiveawayEntryView"]):
    """Persistent button used as an alternative to reacting with the giveaway emoji."""

    cog: "Engagement"
    message_id: int

    def __init__(self, cog: "Engagement", message_id: int) -> None:
        super().__init__(
            label="Enter giveaway",
            style=discord.ButtonStyle.success,
            emoji=GIVEAWAY_EMOJI,
            custom_id=f"ENGAGEMENT:GIVEAWAY_ENTRY:{message_id}",
        )
        self.cog = cog
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.cog.toggle_giveaway_entry(
            interaction, self.message_id, add=True
        )
        if result is None:
            return
        await interaction.followup.send(
            embed=Embed(
                description=f"{EMOJIS.APPROVE} You are now in the giveaway — you have **{result}** entries.",
                color=COLORS.approve,
            ),
            ephemeral=True,
        )


class GiveawayEntryView(discord.ui.View):
    """Persistent view attached to the giveaway's message."""

    def __init__(self, cog: "Engagement", message_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(GiveawayEntryButton(cog, message_id))


class GiveawayEditModal(discord.ui.Modal, title="Edit giveaway"):
    """Modal used by `,giveaways edit prize/colour/host/etc.` for inline text edits."""

    def __init__(
        self,
        cog: "Engagement",
        guild_id: int,
        message_id: int,
        *,
        field: str,
        label: str,
        current: str,
        placeholder: str,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = guild_id
        self.message_id = message_id
        self.field = field
        self.value_field = discord.ui.TextInput(
            label=label,
            style=discord.TextStyle.short if field != "messages" else discord.TextStyle.paragraph,
            placeholder=placeholder,
            default=current,
            required=False,
            max_length=2000,
        )
        self.add_item(self.value_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        value = self.value_field.value.strip()
        try:
            await self.cog.update_giveaway_field(
                self.guild_id, self.message_id, self.field, value
            )
        except ValueError as err:
            return await interaction.followup.send(
                embed=Embed(
                    description=f"{EMOJIS.DENY} {err}",
                    color=COLORS.deny,
                ),
                ephemeral=True,
            )

        await interaction.followup.send(
            embed=Embed(
                description=f"{EMOJIS.APPROVE} Giveaway ``{self.field}`` has been updated.",
                color=COLORS.approve,
            ),
            ephemeral=True,
        )
# =============================================================================
# Cog
# =============================================================================


class Engagement(commands.Cog):
    """Engagement tools: suggestions, invite tracking, leveling and starboards."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        # Register the persistent suggestion button view so it survives restarts.
        self.bot.add_view(SuggestPanelView(self))
        self.bot.add_view(GiveawayEntryView(self, 0))
        self._refresh_persistent_views.start()
        self._giveaway_sweeper.start()
        self._afk_sweeper.start()

    async def cog_unload(self) -> None:
        self._refresh_persistent_views.cancel()
        self._giveaway_sweeper.cancel()
        self._afk_sweeper.cancel()

    @tasks.loop(minutes=10)
    async def _refresh_persistent_views(self) -> None:
        """Persistent views fire on_custom behind the scenes; the button keeps working."""
        # We don't need to actually do anything, the view is already registered.
        return None

    # ------------------------------------------------------------------
    # Database helpers — Suggestions
    # ------------------------------------------------------------------

    async def get_suggestions_config(self, guild_id: int) -> dict[str, Any]:
        row = await (await self.bot.db.execute(
            "SELECT channel_id, panel_message_id, panel_template, embed_template "
            "FROM suggestions_config WHERE guild_id = ?",
            (guild_id,),
        )).fetchone()
        if not row:
            return {"channel_id": None, "panel_message_id": None, "panel_template": None, "embed_template": None}
        return {
            "channel_id": row["channel_id"],
            "panel_message_id": row["panel_message_id"],
            "panel_template": row["panel_template"],
            "embed_template": row["embed_template"],
        }

    async def save_suggestions_config(
        self,
        guild_id: int,
        *,
        channel_id: Optional[int] = None,
        panel_message_id: Optional[int] = None,
        panel_template: Optional[str] = None,
        embed_template: Optional[str] = None,
    ) -> None:
        existing = await self.get_suggestions_config(guild_id)
        await self.bot.db.execute(
            """
            INSERT INTO suggestions_config (guild_id, channel_id, panel_message_id, panel_template, embed_template)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                panel_message_id = excluded.panel_message_id,
                panel_template = excluded.panel_template,
                embed_template = excluded.embed_template
            """,
            (
                guild_id,
                channel_id if channel_id is not None else existing["channel_id"],
                panel_message_id if panel_message_id is not None else existing["panel_message_id"],
                panel_template if panel_template is not None else existing["panel_template"],
                embed_template if embed_template is not None else existing["embed_template"],
            ),
        )
        await self.bot.db.commit()

    async def insert_suggestion(
        self,
        guild_id: int,
        author_id: int,
        text: str,
        channel_id: int,
        message_id: int,
    ) -> int:
        cursor = await self.bot.db.execute(
            "INSERT INTO suggestions (guild_id, channel_id, message_id, author_id, suggestion) "
            "VALUES (?, ?, ?)",
            (guild_id, channel_id, message_id, author_id, text),
        )
        await self.bot.db.commit()
        return cursor.lastrowid or 0

    async def get_suggestion(self, suggestion_id: int) -> Optional[dict[str, Any]]:
        row = await (await self.bot.db.execute(
            "SELECT id, guild_id, channel_id, message_id, author_id, suggestion, status, "
            "responder_id, response, response_message_id "
            "FROM suggestions WHERE id = ?",
            (suggestion_id,),
        )).fetchone()
        return dict(row) if row else None

    async def set_suggestion_response(
        self,
        suggestion_id: int,
        *,
        status: str,
        responder_id: Optional[int],
        response: Optional[str],
        response_message_id: Optional[int],
    ) -> None:
        await self.bot.db.execute(
            "UPDATE suggestions SET status = ?, responder_id = ?, response = ?, response_message_id = ? "
            "WHERE id = ?",
            (status, responder_id, response, response_message_id, suggestion_id),
        )
        await self.bot.db.commit()

    async def is_blacklisted(self, guild_id: int, user_id: int) -> bool:
        row = await (await self.bot.db.execute(
            "SELECT 1 FROM suggestions_blacklist WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )).fetchone()
        return row is not None

    async def blacklist_user(
        self, guild_id: int, user_id: int, *, moderator_id: int, reason: Optional[str]
    ) -> bool:
        try:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO suggestions_blacklist (guild_id, user_id, moderator_id, reason) "
                "VALUES (?, ?, ?)",
                (guild_id, user_id, moderator_id, reason),
            )
            await self.bot.db.commit()
            return True
        except Exception:
            return False

    async def unblacklist_user(self, guild_id: int, user_id: int) -> int:
        cursor = await self.bot.db.execute(
            "DELETE FROM suggestions_blacklist WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        await self.bot.db.commit()
        return cursor.rowcount or 0

    async def post_suggestion(
        self,
        guild: discord.Guild,
        author: discord.abc.User,
        text: str,
    ) -> tuple[dict[str, Any], discord.Message]:
        """Validate and post a suggestion, returning ``(record, message)``."""
        config = await self.get_suggestions_config(guild.id)
        if not config["channel_id"]:
            raise ValueError("Suggestions are not configured for this server. Run `,suggestions setup` first.")

        if await self.is_blacklisted(guild.id, author.id):
            raise ValueError("You are blacklisted from making suggestions in this server.")

        if len(text) < 4:
            raise ValueError("Your suggestion must be at least **4** characters long.")
        if len(text) > 1024:
            raise ValueError("Your suggestion must be **1024** characters or fewer.")

        channel = guild.get_channel(config["channel_id"])
        if channel is None or not isinstance(channel, discord.TextChannel):
            raise ValueError("The suggestions channel is missing or invalid. Re-run `,suggestions setup`.")
        if not channel.permissions_for(guild.me).send_messages:
            raise ValueError("I do not have permission to post in the suggestions channel.")

        embed = Embed(
            title="New suggestion",
            description=text,
            color=COLORS.neutral,
            timestamp=datetime.now(tz=timezone.utc),
        )
        embed.set_author(
            name=getattr(author, "display_name", author.name),
            icon_url=getattr(getattr(author, "display_avatar", None), "url", None),
        )
        embed.set_footer(text=f"User ID: {author.id}")

        message = await channel.send(embed=embed)
        record_id = await self.insert_suggestion(
            guild.id, author.id, text, message.channel.id, message.id
        )
        record = await self.get_suggestion(record_id)
        assert record is not None
        return record, message

    async def respond_to_suggestion(
        self,
        guild: discord.Guild,
        record: dict[str, Any],
        *,
        status: str,
        responder: discord.abc.User,
        reason: Optional[str] = None,
    ) -> None:
        """Update a suggestion's status, posting an answer in its thread (original embed)."""
        colors = {
            "approved": COLORS.approve,
            "denied": COLORS.deny,
            "implemented": 0x57F287,
            "considered": 0xFEE75C,
        }
        embed = Embed(
            title=f"Suggestion {status}",
            description=record["suggestion"],
            color=colors.get(status, COLORS.neutral),
            timestamp=datetime.now(tz=timezone.utc),
        )
        embed.set_author(
            name=getattr(responder, "display_name", responder.name),
            icon_url=getattr(getattr(responder, "display_avatar", None), "url", None),
        )
        embed.set_footer(text=f"Decision by {getattr(responder, 'id', '?')}")

        if reason:
            embed.add_field(name="Reason", value=reason[:1024], inline=False)

        try:
            original = await (
                await self.bot.fetch_channel(record["channel_id"])
            ).fetch_message(record["message_id"])  # type: ignore[union-attr]
            await original.edit(embed=embed)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

        await self.set_suggestion_response(
            record["id"],
            status=status,
            responder_id=getattr(responder, "id", None),
            response=reason,
            response_message_id=record.get("message_id"),
        )

        try:
            await responder.send(
                f"Your suggestion in **{guild.name}** has been **{status}**."
                if isinstance(responder, discord.Member)
                else None  # silently swallow
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ------------------------------------------------------------------
    # Database helpers — Invites
    # ------------------------------------------------------------------

    async def cache_invites(self, guild: discord.Guild) -> dict[str, int]:
        """Fetch every guild invite and return ``{code: uses}``."""
        snapshot: dict[str, int] = {}
        try:
            for invite in await guild.invites():
                snapshot[invite.code] = invite.uses or 0
        except (discord.Forbidden, discord.HTTPException):
            return snapshot
        return snapshot

    async def upsert_invite(
        self,
        guild_id: int,
        code: str,
        *,
        inviter_id: Optional[int],
        channel_id: Optional[int],
        url: Optional[str],
        uses: int,
    ) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO invites (guild_id, code, inviter_id, channel_id, url, uses)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, code) DO UPDATE SET
                inviter_id = excluded.inviter_id,
                channel_id = excluded.channel_id,
                url = excluded.url,
                uses = excluded.uses
            """,
            (guild_id, code, inviter_id, channel_id, url, uses),
        )
        await self.bot.db.commit()

    async def remove_invite(self, guild_id: int, code: str) -> None:
        await self.bot.db.execute(
            "DELETE FROM invites WHERE guild_id = ? AND code = ?",
            (guild_id, code),
        )
        await self.bot.db.commit()

    async def record_invite_join(self, guild_id: int, user_id: int, code: Optional[str]) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO invite_joins (guild_id, user_id, invite_code)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                invite_code = excluded.invite_code,
                joined_at = CURRENT_TIMESTAMP
            """,
            (guild_id, user_id, code),
        )
        await self.bot.db.commit()

    async def invite_join_code(self, guild_id: int, user_id: int) -> Optional[str]:
        row = await (await self.bot.db.execute(
            "SELECT invite_code FROM invite_joins WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )).fetchone()
        return row["invite_code"] if row else None

    async def invited_users(self, guild_id: int, inviter_id: int) -> list[dict[str, Any]]:
        cursor = await self.bot.db.execute(
            "SELECT user_id, invite_code, joined_at FROM invite_joins "
            "WHERE guild_id = ? AND invite_code IN "
            "(SELECT code FROM invites WHERE guild_id = ? AND inviter_id = ?)",
            (guild_id, guild_id, inviter_id),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def invite_stats(self, guild_id: int, inviter_id: int) -> dict[str, int]:
        cursor = await self.bot.db.execute(
            "SELECT code, uses, channel_id FROM invites "
            "WHERE guild_id = ? AND inviter_id = ?",
            (guild_id, inviter_id),
        )
        rows = await cursor.fetchall()
        total_uses = sum((row["uses"] or 0) for row in rows) if rows else 0
        return {"total": total_uses, "invites": len(rows)}
# ------------------------------------------------------------------
    # Database helpers — Levels
    # ------------------------------------------------------------------

    async def get_level_config(self, guild_id: int) -> dict[str, Any]:
        row = await (await self.bot.db.execute(
            "SELECT enabled, xp_min, xp_max, cooldown_seconds, level_up_message, "
            "level_up_channel_id, announce FROM level_config WHERE guild_id = ?",
            (guild_id,),
        )).fetchone()
        if not row:
            return {
                "enabled": True,
                "xp_min": 15,
                "xp_max": 25,
                "cooldown_seconds": 60,
                "level_up_message": (
                    "🎉 {user.mention} just advanced to **level {level}** in **{guild.name}**!"
                ),
                "level_up_channel_id": None,
                "announce": True,
            }
        return {
            "enabled": bool(row["enabled"]),
            "xp_min": row["xp_min"] or 15,
            "xp_max": row["xp_max"] or 25,
            "cooldown_seconds": row["cooldown_seconds"] or 60,
            "level_up_message": row["level_up_message"],
            "level_up_channel_id": row["level_up_channel_id"],
            "announce": bool(row["announce"]),
        }

    async def save_level_config(self, guild_id: int, **fields: Any) -> None:
        current = await self.get_level_config(guild_id)
        current.update({k: v for k, v in fields.items() if v is not None})
        await self.bot.db.execute(
            """
            INSERT INTO level_config (
                guild_id, enabled, xp_min, xp_max, cooldown_seconds,
                level_up_message, level_up_channel_id, announce
            )
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                enabled = excluded.enabled,
                xp_min = excluded.xp_min,
                xp_max = excluded.xp_max,
                cooldown_seconds = excluded.cooldown_seconds,
                level_up_message = excluded.level_up_message,
                level_up_channel_id = excluded.level_up_channel_id,
                announce = excluded.announce
            """,
            (
                guild_id,
                int(current["enabled"]),
                int(current["xp_min"]),
                int(current["xp_max"]),
                int(current["cooldown_seconds"]),
                current["level_up_message"],
                current["level_up_channel_id"],
                int(current["announce"]),
            ),
        )
        await self.bot.db.commit()

    async def get_level_entry(self, guild_id: int, user_id: int) -> dict[str, Any]:
        row = await (await self.bot.db.execute(
            "SELECT xp, total_xp, last_message_ts FROM levels WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )).fetchone()
        if not row:
            return {"xp": 0, "total_xp": 0, "last_message_ts": 0}
        return dict(row)

    async def add_xp(self, guild_id: int, user_id: int, amount: int) -> Optional[tuple[int, int, int]]:
        """Add ``amount`` XP. Returns ``(old_level, new_level, gained_xp)`` if a levelup occurred."""
        entry = await self.get_level_entry(guild_id, user_id)
        old_level = level_for_xp(entry["xp"])
        new_total = max(0, entry["xp"] + amount)
        await self.bot.db.execute(
            """
            INSERT INTO levels (guild_id, user_id, xp, total_xp, last_message_ts)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                xp = excluded.xp,
                total_xp = excluded.total_xp,
                last_message_ts = excluded.last_message_ts
            """,
            (guild_id, user_id, new_total, max(0, entry["total_xp"] + amount), int(datetime.now(tz=timezone.utc).timestamp())),
        )
        await self.bot.db.commit()
        new_level = level_for_xp(new_total)
        if new_level > old_level:
            return old_level, new_level, amount
        return None

    async def remove_xp(self, guild_id: int, user_id: int, amount: int) -> int:
        entry = await self.get_level_entry(guild_id, user_id)
        new_total = max(0, entry["xp"] - abs(amount))
        await self.bot.db.execute(
            """
            INSERT INTO levels (guild_id, user_id, xp, total_xp, last_message_ts)
            VALUES (?, ?, 0, 0, 0)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                xp = ?
            """,
            (guild_id, user_id, new_total),
        )
        await self.bot.db.commit()
        return entry["xp"] - new_total

    async def set_xp(self, guild_id: int, user_id: int, xp: int) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO levels (guild_id, user_id, xp, total_xp, last_message_ts)
            VALUES (?, ?, 0, 0)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                xp = ?
            """,
            (guild_id, user_id, max(0, xp), max(0, xp)),
        )
        await self.bot.db.commit()

    async def level_excludes(self, guild_id: int) -> list[tuple[str, int]]:
        cursor = await self.bot.db.execute(
            "SELECT kind, target_id FROM level_excludes WHERE guild_id = ?",
            (guild_id,),
        )
        return [(row["kind"], row["target_id"]) for row in await cursor.fetchall()]

    async def add_level_exclude(self, guild_id: int, kind: str, target_id: int) -> bool:
        try:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO level_excludes (guild_id, kind, target_id) VALUES (?, ?)",
                (guild_id, kind, target_id),
            )
            await self.bot.db.commit()
            return True
        except Exception:
            return False

    async def remove_level_exclude(self, guild_id: int, kind: str, target_id: int) -> int:
        cursor = await self.bot.db.execute(
            "DELETE FROM level_excludes WHERE guild_id = ? AND kind = ? AND target_id = ?",
            (guild_id, kind, target_id),
        )
        await self.bot.db.commit()
        return cursor.rowcount or 0

    async def level_rewards_for(self, guild_id: int) -> list[tuple[int, int]]:
        cursor = await self.bot.db.execute(
            "SELECT level, role_id FROM level_rewards WHERE guild_id = ? ORDER BY level ASC",
            (guild_id,),
        )
        return [(row["level"], row["role_id"]) for row in await cursor.fetchall()]

    async def set_level_reward(self, guild_id: int, level: int, role_id: int) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO level_rewards (guild_id, level, role_id)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, level) DO UPDATE SET
                role_id = excluded.role_id
            """,
            (guild_id, level, role_id),
        )
        await self.bot.db.commit()

    async def clear_level_reward(self, guild_id: int, level: int) -> int:
        cursor = await self.bot.db.execute(
            "DELETE FROM level_rewards WHERE guild_id = ? AND level = ?",
            (guild_id, level),
        )
        await self.bot.db.commit()
        return cursor.rowcount or 0

    async def top_levels(self, guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
        cursor = await self.bot.db.execute(
            "SELECT user_id, xp, total_xp FROM levels WHERE guild_id = ? "
            "ORDER BY xp DESC, total_xp DESC LIMIT ?",
            (guild_id, limit),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def level_rank(self, guild_id: int, user_id: int) -> Optional[int]:
        cursor = await self.bot.db.execute(
            "SELECT user_id FROM levels WHERE guild_id = ? ORDER BY xp DESC, total_xp DESC",
            (guild_id,),
        )
        rows = await cursor.fetchall()
        for index, row in enumerate(rows, start=1):
            if row["user_id"] == user_id:
                return index
        return None
# ------------------------------------------------------------------
    # Database helpers — Starboards
    # ------------------------------------------------------------------

    async def list_starboards(self, guild_id: int) -> list[dict[str, Any]]:
        cursor = await self.bot.db.execute(
            "SELECT name, channel_id, emoji, threshold, locked, self_react, color "
            "FROM starboards WHERE guild_id = ? ORDER BY name ASC",
            (guild_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_starboard(self, guild_id: int, name: str) -> Optional[dict[str, Any]]:
        row = await (await self.bot.db.execute(
            "SELECT name, channel_id, emoji, threshold, locked, self_react, color "
            "FROM starboards WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )).fetchone()
        return dict(row) if row else None

    async def add_starboard(
        self,
        guild_id: int,
        *,
        name: str,
        channel_id: int,
        emoji: str,
        threshold: int,
        color: Optional[int] = None,
    ) -> None:
        existing = await self.list_starboards(guild_id)
        if any(s["name"].lower() == name.lower() for s in existing):
            raise ValueError(f"A starboard named **{name}** already exists.")
        if len(existing) >= 3:
            raise ValueError("Servers may configure at most **3** starboards.")
        if threshold < 1:
            raise ValueError("Threshold must be at least **1**.")
        await self.bot.db.execute(
            "INSERT INTO starboards (guild_id, name, channel_id, emoji, threshold, color) "
            "VALUES (?, ?, ?)",
            (guild_id, name, channel_id, emoji, threshold, color or 0xFFCC00),
        )
        await self.bot.db.commit()

    async def update_starboard(
        self,
        guild_id: int,
        *,
        name: str,
        channel_id: Optional[int] = None,
        emoji: Optional[str] = None,
        threshold: Optional[int] = None,
        color: Optional[int] = None,
        locked: Optional[bool] = None,
        self_react: Optional[bool] = None,
    ) -> None:
        current = await self.get_starboard(guild_id, name)
        if not current:
            raise ValueError(f"No starboard named **{name}** exists.")
        new = {
            "channel_id": channel_id if channel_id is not None else current["channel_id"],
            "emoji": emoji if emoji is not None else current["emoji"],
            "threshold": threshold if threshold is not None else current["threshold"],
            "color": color if color is not None else current["color"],
            "locked": int(locked if locked is not None else bool(current["locked"])),
            "self_react": int(self_react if self_react is not None else bool(current["self_react"])),
        }
        await self.bot.db.execute(
            "UPDATE starboards SET channel_id = ?, emoji = ?, threshold = ?, "
            "color = ?, locked = ?, self_react = ? WHERE guild_id = ? AND name = ?",
            (
                new["channel_id"], new["emoji"], new["threshold"],
                new["color"], new["locked"], new["self_react"],
                guild_id, name,
            ),
        )
        await self.bot.db.commit()

    async def delete_starboard(self, guild_id: int, name: str) -> int:
        cursor = await self.bot.db.execute(
            "DELETE FROM starboards WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        await self.bot.db.execute(
            "DELETE FROM starboard_ignores WHERE guild_id = ? AND starboard_name = ?",
            (guild_id, name),
        )
        await self.bot.db.execute(
            "DELETE FROM starboard_posts WHERE guild_id = ? AND starboard_name = ?",
            (guild_id, name),
        )
        await self.bot.db.commit()
        return cursor.rowcount or 0

    async def list_starboard_ignores(self, guild_id: int, name: str) -> list[tuple[str, int]]:
        cursor = await self.bot.db.execute(
            "SELECT kind, target_id FROM starboard_ignores "
            "WHERE guild_id = ? AND starboard_name = ?",
            (guild_id, name),
        )
        return [(row["kind"], row["target_id"]) for row in await cursor.fetchall()]

    async def toggle_starboard_ignore(
        self,
        guild_id: int,
        name: str,
        kind: str,
        target_id: int,
        *,
        add: bool,
    ) -> bool:
        if add:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO starboard_ignores "
                "(guild_id, starboard_name, kind, target_id) VALUES (?, ?, ?)",
                (guild_id, name, kind, target_id),
            )
        else:
            await self.bot.db.execute(
                "DELETE FROM starboard_ignores "
                "WHERE guild_id = ? AND starboard_name = ? AND kind = ? AND target_id = ?",
                (guild_id, name, kind, target_id),
            )
        await self.bot.db.commit()
        return True

    async def get_starboard_post(self, guild_id: int, name: str, source_id: int) -> Optional[dict[str, Any]]:
        row = await (await self.bot.db.execute(
            "SELECT starboard_message_id, stargazers FROM starboard_posts "
            "WHERE guild_id = ? AND starboard_name = ? AND source_message_id = ?",
            (guild_id, name, source_id),
        )).fetchone()
        if not row:
            return None
        return {"starboard_message_id": row["starboard_message_id"], "stargazers": json.loads(row["stargazers"] or "[]")}

    async def upsert_starboard_post(
        self,
        guild_id: int,
        name: str,
        source_id: int,
        starboard_message_id: int,
        stargazers: list[int],
    ) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO starboard_posts (guild_id, starboard_name, source_message_id,
                starboard_message_id, stargazers)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, starboard_name, source_message_id) DO UPDATE SET
                starboard_message_id = excluded.starboard_message_id,
                stargazers = excluded.stargazers
            """,
            (guild_id, name, source_id, starboard_message_id, json.dumps(stargazers)),
        )
        await self.bot.db.commit()

    async def delete_starboard_post(self, guild_id: int, name: str, source_id: int) -> None:
        await self.bot.db.execute(
            "DELETE FROM starboard_posts WHERE guild_id = ? "
            "AND starboard_name = ? AND source_message_id = ?",
            (guild_id, name, source_id),
        )
        await self.bot.db.commit()

    # ------------------------------------------------------------------
    # Database helpers — Giveaways
    # ------------------------------------------------------------------

    async def get_giveaway_config(self, guild_id: int) -> dict[str, Any]:
        row = await (await self.bot.db.execute(
            "SELECT default_channel_id FROM giveaway_config WHERE guild_id = ?",
            (guild_id,),
        )).fetchone()
        return {"default_channel_id": row["default_channel_id"] if row else None}

    async def save_giveaway_config(
        self, guild_id: int, *, default_channel_id: Optional[int]
    ) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO giveaway_config (guild_id, default_channel_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                default_channel_id = excluded.default_channel_id
            """,
            (guild_id, default_channel_id),
        )
        await self.bot.db.commit()

    async def get_giveaway(self, message_id: int) -> Optional[dict[str, Any]]:
        row = await (await self.bot.db.execute(
            "SELECT message_id, guild_id, channel_id, host_id, prize, winners, "
            "duration_seconds, minimum_age_seconds, required_role_id, entries, "
            "ended, winner_ids, created_at, ends_at, "
            "thumbnail_url, image_url, host_name, color, banner_message, invite_message "
            "FROM giveaways WHERE message_id = ?",
            (message_id,),
        )).fetchone()
        if not row:
            return None
        return dict(row)

    async def list_active_giveaways(self, guild_id: int) -> list[dict[str, Any]]:
        cursor = await self.bot.db.execute(
            "SELECT message_id, guild_id, channel_id, host_id, prize, winners, "
            "duration_seconds, minimum_age_seconds, required_role_id, entries, "
            "ended, winner_ids, created_at, ends_at "
            "FROM giveaways WHERE guild_id = ? AND ended = 0 "
            "ORDER BY ends_at ASC",
            (guild_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def list_giveaways(self, guild_id: int) -> list[dict[str, Any]]:
        cursor = await self.bot.db.execute(
            "SELECT message_id, guild_id, channel_id, host_id, prize, winners, "
            "duration_seconds, minimum_age_seconds, required_role_id, entries, "
            "ended, winner_ids, created_at, ends_at "
            "FROM giveaways WHERE guild_id = ? "
            "ORDER BY ended ASC, ends_at DESC",
            (guild_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def list_due_giveaways(self) -> list[dict[str, Any]]:
        cursor = await self.bot.db.execute(
            "SELECT message_id, guild_id, channel_id, host_id, prize, winners, "
            "duration_seconds, minimum_age_seconds, required_role_id, entries, "
            "ended, winner_ids, created_at, ends_at "
            "FROM giveaways WHERE ended = 0 AND ends_at <= CURRENT_TIMESTAMP"
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def create_giveaway(
        self,
        guild: discord.Guild,
        host: discord.abc.User,
        *,
        prize: str,
        winners: int,
        duration_seconds: int,
        channel_id: int,
        minimum_age_seconds: int = 0,
        required_role_id: Optional[int] = None,
        thumbnail_url: Optional[str] = None,
        image_url: Optional[str] = None,
        color: Optional[int] = None,
        invite_message: Optional[str] = None,
        banner_message: Optional[str] = None,
    ) -> tuple[dict[str, Any], discord.Message]:
        """Persist a new giveaway and ship the embed to the target channel."""
        if duration_seconds < 30:
            raise ValueError("Giveaway duration must be at least 30 seconds.")
        channel = guild.get_channel(channel_id)
        if channel is None or not isinstance(
            channel, (discord.TextChannel, discord.Thread)
        ):
            raise ValueError("The giveaway channel is invalid or missing.")
        if not channel.permissions_for(guild.me).send_messages or not channel.permissions_for(guild.me).embed_links:
            raise ValueError("I do not have permission to send embeds in that channel.")

        ends_at_dt = datetime.now(tz=timezone.utc) + timedelta(seconds=duration_seconds)
        ends_at_iso = ends_at_dt.replace(tzinfo=None).isoformat(timespec="seconds")

        # Render embed (title -> "Giveaway: <prize>", description -> time left, host, etc.)
        embed = await self._render_giveaway_embed(
            guild,
            host,
            prize=prize,
            winners=winners,
            ends_at_dt=ends_at_dt,
            minimum_age_seconds=minimum_age_seconds,
            required_role_id=required_role_id,
            thumbnail_url=thumbnail_url,
            image_url=image_url,
            color=color or 0x5865F2,
            entries=[],
            ended=False,
        )

        view = GiveawayEntryView(self, 0)  # placeholder, message_id patched below
        host_id_int = getattr(host, "id", 0)
        await self.bot.db.execute(
            """
            INSERT INTO giveaways (
                guild_id, channel_id, host_id, host_name, prize, winners,
                duration_seconds, minimum_age_seconds, required_role_id, entries,
                ended, winner_ids, created_at, ends_at, color,
                thumbnail_url, image_url, invite_message, banner_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '[]', CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild.id, channel.id, host_id_int,
                getattr(host, "display_name", getattr(host, "name", "Unknown")),
                prize, winners, duration_seconds, minimum_age_seconds,
                required_role_id, "[]", ends_at_iso,
                color or 0x5865F2, thumbnail_url, image_url,
                invite_message, banner_message,
            ),
        )
        await self.bot.db.commit()

        cursor = await self.bot.db.execute(
            "SELECT message_id FROM giveaways WHERE guild_id = ? AND channel_id = ? "
            "AND prize = ? AND ended = 0 ORDER BY message_id DESC LIMIT 1",
            (guild.id, channel.id, prize),
        )
        row = await cursor.fetchone()
        message_id = int(row["message_id"]) if row else 0

        view = GiveawayEntryView(self, message_id)
        message = await channel.send(embed=embed, view=view)

        # Update database: persist real message id and overwrite view child.
        await self.bot.db.execute(
            "UPDATE giveaways SET message_id = ? WHERE message_id = ?",
            (message.id, message_id),
        )
        await self.bot.db.commit()

        record = await self.get_giveaway(message.id)
        assert record is not None
        return record, message

    async def _render_giveaway_embed(
        self,
        guild: discord.Guild,
        host: discord.abc.User,
        *,
        prize: str,
        winners: int,
        ends_at_dt: datetime,
        minimum_age_seconds: int,
        required_role_id: Optional[int],
        thumbnail_url: Optional[str],
        image_url: Optional[str],
        color: int,
        entries: list[int],
        ended: bool,
        winner_ids: Optional[list[int]] = None,
        end_reason: Optional[str] = None,
        quick_winner: Optional[int] = None,
    ) -> Embed:
        embed = Embed(color=color)
        embed.title = f"🎉 Giveaway: {prize}" if not ended else f"🎉 Giveaway ended: {prize}"
        embed.set_author(
            name=getattr(host, "display_name", getattr(host, "name", "Unknown")),
            icon_url=getattr(getattr(host, "display_avatar", None), "url", None),
        )
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        if image_url:
            embed.set_image(url=image_url)

        requirements_lines: list[str] = []
        if minimum_age_seconds > 0:
            days = max(1, minimum_age_seconds // 86400)
            requirements_lines.append(f"• Account age: at least **{days}** day(s) old")
        if required_role_id:
            requirements_lines.append(f"• Required role: <@&{required_role_id}>")
        extra_required = await self.list_required_roles(guild.id)
        for role_id in extra_required:
            requirements_lines.append(f"• Required role: <@&{role_id}>")
        if not requirements_lines:
            requirements_lines.append("• No requirements")

        embed.add_field(
            name="Requirements",
            value="\n".join(requirements_lines)[:1024],
            inline=False,
        )
        if ended:
            winners_text = "No winners" if not winner_ids else ", ".join(
                f"<@{wid}>" for wid in winner_ids
            )
            embed.add_field(
                name=f"{'Winner' if (winner_ids and len(winner_ids) == 1) else 'Winners'}",
                value=winners_text,
                inline=False,
            )
            embed.add_field(
                name="Entries",
                value=str(len(entries)),
                inline=True,
            )
            if end_reason:
                embed.add_field(name="Reason", value=end_reason[:512], inline=False)
        else:
            embed.add_field(
                name="Winners",
                value=str(winners),
                inline=True,
            )
            embed.add_field(
                name="Ends",
                value=discord.utils.format_dt(ends_at_dt, style="R"),
                inline=True,
            )
            embed.add_field(
                name="Entries",
                value="0",
                inline=True,
            )
            embed.add_field(
                name="Host",
                value=host.mention if isinstance(host, discord.Member) else f"`{host.id}`",
                inline=False,
            )
            embed.add_field(
                name="How to enter",
                value=f"React with {GIVEAWAY_REACTION} or use the **Enter giveaway** button below.",
                inline=False,
            )
        embed.set_footer(text=f"Ends at {ends_at_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        return embed

    async def update_giveaway_field(
        self, guild_id: int, message_id: int, field: str, value: Any
    ) -> None:
        """Mutate a single column on a giveaway row, validating the value first."""
        record = await self.get_giveaway(message_id)
        if not record or record["guild_id"] != guild_id:
            raise ValueError("I could not find that giveaway in this server.")
        if record["ended"]:
            raise ValueError("That giveaway has already ended — create a new one instead.")

        setters = {
            "prize": ("prize", str(value).strip()[:120]),
            "winners": ("winners", max(1, min(50, int(value)))),
            "host": ("host_id", int(value)),
            "host_name": ("host_name", str(value)[:64]),
            "duration": (
                "ends_at",
                (
                    datetime.now(tz=timezone.utc) + timedelta(
                        seconds=parse_duration_seconds(value) or 0
                    )
                ).replace(tzinfo=None).isoformat(timespec="seconds"),
            ),
            "stay": ("minimum_age_seconds", max(0, int(value)) * 86400),
            "age": ("minimum_age_seconds", max(0, int(value)) * 86400),
            "thumbnail": ("thumbnail_url", value or None),
            "image": ("image_url", value or None),
            "colour": ("color", int(str(value).replace("0x", "").replace("#", ""), 16) & 0xFFFFFF),
            "color": ("color", int(str(value).replace("0x", "").replace("#", ""), 16) & 0xFFFFFF),
            "invite_message": ("invite_message", value),
            "messages": ("banner_message", value),
            "requiredroles": ("required_role_id", int(value) if str(value).isdigit() else None),
            "required_role_id": ("required_role_id", int(value) if str(value).isdigit() else None),
        }
        if field not in setters:
            raise ValueError(f"Field ``{field}`` cannot be edited.")
        col, new_val = setters[field]

        if field == "duration":
            seconds = parse_duration_seconds(value)
            if seconds is None or seconds < 30:
                raise ValueError("Provide a valid duration of at least 30 seconds.")
            # also persist new duration_seconds for math later
            await self.bot.db.execute(
                "UPDATE giveaways SET duration_seconds = ?, ends_at = ? "
                "WHERE message_id = ?",
                (seconds, new_val, message_id),
            )
            await self.bot.db.commit()
            return

        await self.bot.db.execute(
            f"UPDATE giveaways SET {col} = ? WHERE message_id = ?",
            (new_val, message_id),
        )
        await self.bot.db.commit()

    async def toggle_giveaway_entry(
        self,
        interaction: discord.Interaction,
        message_id: int,
        *,
        add: bool,
    ) -> Optional[int]:
        """Toggle an entry for the invoking user, returning the new entry count."""
        record = await self.get_giveaway(message_id)
        if not record:
            try:
                await interaction.followup.send(
                    embed=Embed(
                        description=f"{EMOJIS.DENY} That giveaway no longer exists.",
                        color=COLORS.deny,
                    ),
                    ephemeral=True,
                )
            except Exception:
                pass
            return None

        if record["ended"]:
            try:
                await interaction.followup.send(
                    embed=Embed(
                        description=f"{EMOJIS.DENY} That giveaway has already ended.",
                        color=COLORS.deny,
                    ),
                    ephemeral=True,
                )
            except Exception:
                pass
            return None

        guild = interaction.guild
        if guild is None:
            return None
        member = guild.get_member(interaction.user.id) or interaction.user

        # Eligibility checks
        if await self.is_giveaway_blacklisted(guild.id, interaction.user.id):
            try:
                await interaction.followup.send(
                    embed=Embed(
                        description=f"{EMOJIS.DENY} You are **blacklisted** from this server's giveaways.",
                        color=COLORS.deny,
                    ),
                    ephemeral=True,
                )
            except Exception:
                pass
            return None

        if record["minimum_age_seconds"] and isinstance(member, discord.Member):
            if discord.utils.utcnow() - member.created_at < timedelta(seconds=record["minimum_age_seconds"]):
                try:
                    await interaction.followup.send(
                        embed=Embed(
                            description=f"{EMOJIS.DENY} Your account is not old enough to enter this giveaway.",
                            color=COLORS.deny,
                        ),
                        ephemeral=True,
                    )
                except Exception:
                    pass
                return None

        if record["required_role_id"] and isinstance(member, discord.Member):
            if not any(r.id == record["required_role_id"] for r in member.roles):
                try:
                    await interaction.followup.send(
                        embed=Embed(
                            description=f"{EMOJIS.DENY} You do not have the required role to enter this giveaway.",
                            color=COLORS.deny,
                        ),
                        ephemeral=True,
                    )
                except Exception:
                    pass
                return None

        # Also ensure the user has every role marked via `,giveaways edit requiredroles`
        required_roles = await self.list_required_roles(guild.id)
        if required_roles and isinstance(member, discord.Member):
            user_role_ids = {r.id for r in member.roles}
            if not any(r in user_role_ids for r in required_roles):
                try:
                    await interaction.followup.send(
                        embed=Embed(
                            description=f"{EMOJIS.DENY} You do not have a required role to enter this giveaway.",
                            color=COLORS.deny,
                        ),
                        ephemeral=True,
                    )
                except Exception:
                    pass
                return None

        try:
            entries = json.loads(record["entries"] or "[]")
        except Exception:
            entries = []

        if add:
            if interaction.user.id in entries:
                return len(entries)
            entries.append(interaction.user.id)
        else:
            if interaction.user.id not in entries:
                return len(entries)
            entries.remove(interaction.user.id)

        # Count entries by adding extra entries for each qualifying role
        extra = 0
        if isinstance(member, discord.Member) and add:
            extras = await self.list_extra_entries(guild.id)
            for role_id, count in extras:
                if any(r.id == role_id for r in member.roles):
                    extra += max(0, count - 1)  # already counted once

        total = len(entries) + extra
        await self.bot.db.execute(
            "UPDATE giveaways SET entries = ? WHERE message_id = ?",
            (json.dumps(entries), message_id),
        )
        await self.bot.db.commit()
        return total

    async def add_giveaway_entry(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        user_id: int,
    ) -> list[int]:
        """Add an entry as a result of a reaction; returns the full entries list."""
        record = await self.get_giveaway(message_id)
        if not record or record["guild_id"] != guild_id or record["ended"]:
            try:
                raw = record["entries"] if record else "[]"
            except Exception:
                raw = "[]"
            return json.loads(raw or "[]")
        try:
            entries = json.loads(record["entries"] or "[]")
        except Exception:
            entries = []
        if user_id not in entries:
            entries.append(user_id)
        await self.bot.db.execute(
            "UPDATE giveaways SET entries = ? WHERE message_id = ?",
            (json.dumps(entries), message_id),
        )
        await self.bot.db.commit()
        return entries

    async def remove_giveaway_entry(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        user_id: int,
    ) -> list[int]:
        record = await self.get_giveaway(message_id)
        if not record or record["guild_id"] != guild_id or record["ended"]:
            try:
                raw = record["entries"] if record else "[]"
            except Exception:
                raw = "[]"
            return json.loads(raw or "[]")
        try:
            entries = json.loads(record["entries"] or "[]")
        except Exception:
            entries = []
        if user_id in entries:
            entries.remove(user_id)
        await self.bot.db.execute(
            "UPDATE giveaways SET entries = ? WHERE message_id = ?",
            (json.dumps(entries), message_id),
        )
        await self.bot.db.commit()
        return entries

    async def populate_extra_entries(
        self, *, member: discord.Member, entries: list[int], guild: discord.Guild
    ) -> int:
        """Return total ticket count for *member* once extra entries are added."""
        total = len(entries)
        extras = await self.list_extra_entries(guild.id)
        for role_id, count in extras:
            if any(r.id == role_id for r in member.roles):
                total += max(0, count)
        return total

    # ------------------------------------------------------------------
    # Database helpers — Giveaway extra entries / required roles
    # ------------------------------------------------------------------

    async def list_extra_entries(self, guild_id: int) -> list[tuple[int, int]]:
        cursor = await self.bot.db.execute(
            "SELECT role_id, entries FROM giveaway_extra_entries WHERE guild_id = ? "
            "ORDER BY entries DESC",
            (guild_id,),
        )
        return [(row["role_id"], row["entries"]) for row in await cursor.fetchall()]

    async def set_extra_entry(
        self, guild_id: int, role_id: int, entries: int
    ) -> None:
        entries = max(1, min(entries, 25))
        await self.bot.db.execute(
            """
            INSERT INTO giveaway_extra_entries (guild_id, role_id, entries)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, role_id) DO UPDATE SET
                entries = excluded.entries
            """,
            (guild_id, role_id, entries),
        )
        await self.bot.db.commit()

    async def remove_extra_entry(self, guild_id: int, role_id: int) -> int:
        cursor = await self.bot.db.execute(
            "DELETE FROM giveaway_extra_entries WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )
        await self.bot.db.commit()
        return cursor.rowcount or 0

    async def list_required_roles(self, guild_id: int) -> list[int]:
        cursor = await self.bot.db.execute(
            "SELECT role_id FROM giveaway_required_roles WHERE guild_id = ? ORDER BY role_id ASC",
            (guild_id,),
        )
        return [row["role_id"] for row in await cursor.fetchall()]

    async def add_required_role(self, guild_id: int, role_id: int) -> bool:
        try:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO giveaway_required_roles (guild_id, role_id) VALUES (?, ?)",
                (guild_id, role_id),
            )
            await self.bot.db.commit()
            return True
        except Exception:
            return False

    async def remove_required_role(self, guild_id: int, role_id: int) -> int:
        cursor = await self.bot.db.execute(
            "DELETE FROM giveaway_required_roles WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )
        await self.bot.db.commit()
        return cursor.rowcount or 0

    # ------------------------------------------------------------------
    # Database helpers — Giveaway blacklist
    # ------------------------------------------------------------------

    async def is_giveaway_blacklisted(self, guild_id: int, user_id: int) -> bool:
        row = await (await self.bot.db.execute(
            "SELECT 1 FROM giveaway_blacklist WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )).fetchone()
        return row is not None

    async def blacklist_giveaway_user(
        self, guild_id: int, user_id: int, *, moderator_id: int, reason: Optional[str]
    ) -> bool:
        try:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO giveaway_blacklist "
                "(guild_id, user_id, moderator_id, reason) VALUES (?, ?, ?, ?)",
                (guild_id, user_id, moderator_id, reason),
            )
            await self.bot.db.commit()
            return True
        except Exception:
            return False

    async def unblacklist_giveaway_user(self, guild_id: int, user_id: int) -> int:
        cursor = await self.bot.db.execute(
            "DELETE FROM giveaway_blacklist WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        await self.bot.db.commit()
        return cursor.rowcount or 0

    async def list_giveaway_blacklist(self, guild_id: int) -> list[dict[str, Any]]:
        cursor = await self.bot.db.execute(
            "SELECT user_id, moderator_id, reason, created_at FROM giveaway_blacklist "
            "WHERE guild_id = ? ORDER BY created_at DESC",
            (guild_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def end_giveaway(
        self, guild_id: int, message_id: int, *, cancel: bool = False
    ) -> tuple[discord.Message, list[int]]:
        """Pick winners, update the message, mark ended. Returns ``(message, winners)``."""
        record = await self.get_giveaway(message_id)
        if not record or record["guild_id"] != guild_id:
            raise ValueError("Giveaway not found in this server.")
        if record["ended"]:
            raise ValueError("That giveaway has already ended.")

        try:
            entries = json.loads(record["entries"] or "[]")
        except Exception:
            entries = []

        winners_count = max(1, int(record["winners"] or 1))
        winners_list: list[int] = []
        if not cancel and entries:
            try:
                winners_list = random.sample(entries, min(winners_count, len(entries)))
            except ValueError:
                winners_list = []

        await self.bot.db.execute(
            "UPDATE giveaways SET ended = 1, winner_ids = ? WHERE message_id = ?",
            (json.dumps(winners_list), message_id),
        )
        await self.bot.db.commit()

        guild = self.bot.get_guild(record["guild_id"])
        channel = guild.get_channel(record["channel_id"]) if guild else None
        try:
            message = await channel.fetch_message(message_id) if isinstance(channel, (discord.TextChannel, discord.Thread)) else None
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None

        # Build updated embed
        if message is not None and guild is not None:
            host = await self.bot.fetch_user(record["host_id"])
            host_obj = host or self.bot.user
            ends_at_dt = datetime.fromisoformat(record["ends_at"]).replace(tzinfo=timezone.utc)
            embed = await self._render_giveaway_embed(
                guild,
                host_obj,  # type: ignore[arg-type]
                prize=record["prize"],
                winners=record["winners"],
                ends_at_dt=ends_at_dt,
                minimum_age_seconds=record["minimum_age_seconds"] or 0,
                required_role_id=record["required_role_id"],
                thumbnail_url=record["thumbnail_url"],
                image_url=record["image_url"],
                color=record["color"] or 0x5865F2,
                entries=entries,
                ended=True,
                winner_ids=winners_list,
                end_reason=("Cancelled" if cancel else None),
            )
            try:
                await message.edit(embed=embed, view=None)
            except (discord.Forbidden, discord.HTTPException):
                pass

        return message, winners_list

    async def reroll_giveaway(
        self, guild_id: int, message_id: int, count: int = 1
    ) -> list[int]:
        record = await self.get_giveaway(message_id)
        if not record or record["guild_id"] != guild_id:
            raise ValueError("Giveaway not found in this server.")
        if not record["ended"]:
            raise ValueError("You can only reroll an ended giveaway.")

        try:
            entries = json.loads(record["entries"] or "[]")
        except Exception:
            entries = []

        try:
            previous_winners = set(json.loads(record.get("winner_ids") or "[]"))
        except Exception:
            previous_winners = set()

        eligible = [eid for eid in entries if eid not in previous_winners]
        if not eligible:
            raise ValueError("There are no remaining entries to reroll.")

        try:
            new_winners = random.sample(eligible, min(count, len(eligible)))
        except ValueError:
            new_winners = []

        try:
            all_winners = list(previous_winners) + new_winners
        except Exception:
            all_winners = list(previous_winners)

        await self.bot.db.execute(
            "UPDATE giveaways SET winner_ids = ? WHERE message_id = ?",
            (json.dumps(all_winners), record["message_id"]),
        )
        await self.bot.db.commit()

        # Update embed
        guild = self.bot.get_guild(record["guild_id"])
        channel = guild.get_channel(record["channel_id"]) if guild else None
        try:
            message = await channel.fetch_message(message_id) if isinstance(channel, (discord.TextChannel, discord.Thread)) else None
        except Exception:
            message = None

        if message is not None and guild is not None:
            try:
                host = await self.bot.fetch_user(record["host_id"])
            except Exception:
                host = None
            host_obj = host or self.bot.user
            ends_at_dt = datetime.fromisoformat(record["ends_at"]).replace(tzinfo=timezone.utc)
            embed = await self._render_giveaway_embed(
                guild,
                host_obj,  # type: ignore[arg-type]
                prize=record["prize"],
                winners=record["winners"],
                ends_at_dt=ends_at_dt,
                minimum_age_seconds=record["minimum_age_seconds"] or 0,
                required_role_id=record["required_role_id"],
                thumbnail_url=record["thumbnail_url"],
                image_url=record["image_url"],
                color=record["color"] or 0x5865F2,
                entries=entries,
                ended=True,
                winner_ids=all_winners,
                end_reason="Reroll",
            )
            try:
                await message.edit(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

        return new_winners

    # ------------------------------------------------------------------
    # Database helpers — AFK
    # ------------------------------------------------------------------

    async def get_afk(self, user_id: int) -> Optional[dict[str, Any]]:
        row = await (await self.bot.db.execute(
            "SELECT user_id, reason, since, last_autoclear FROM afk_users WHERE user_id = ?",
            (user_id,),
        )).fetchone()
        return dict(row) if row else None

    async def set_afk(self, user_id: int, reason: str) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO afk_users (user_id, reason, since)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                reason = excluded.reason,
                since = CURRENT_TIMESTAMP,
                last_autoclear = NULL
            """,
            (user_id, reason[:480]),
        )
        await self.bot.db.commit()

    async def clear_afk(self, user_id: int) -> int:
        cursor = await self.bot.db.execute(
            "DELETE FROM afk_users WHERE user_id = ?",
            (user_id,),
        )
        await self.bot.db.commit()
        return cursor.rowcount or 0

    async def list_afk(self, guild_id: Optional[int] = None) -> list[dict[str, Any]]:
        if guild_id is None:
            cursor = await self.bot.db.execute(
                "SELECT user_id, reason, since FROM afk_users ORDER BY since DESC",
            )
        else:
            # Postgres/SQLite: cross-guild members may go AFK in the bot's context;
            # we are using `afk_users` (per-user) so no guild_id column exists —
            # return all AFK users but filter out people not in the guild below.
            cursor = await self.bot.db.execute(
                "SELECT user_id, reason, since FROM afk_users ORDER BY since DESC",
            )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_afk_config(self, guild_id: int) -> dict[str, Any]:
        row = await (await self.bot.db.execute(
            "SELECT timeout_minutes, log_channel_id FROM afk_config WHERE guild_id = ?",
            (guild_id,),
        )).fetchone()
        return {
            "timeout_minutes": row["timeout_minutes"] if row else 0,
            "log_channel_id": row["log_channel_id"] if row else None,
        }

    async def save_afk_config(
        self,
        guild_id: int,
        *,
        timeout_minutes: Optional[int] = None,
        log_channel_id: Optional[int] = None,
    ) -> None:
        current = await self.get_afk_config(guild_id)
        await self.bot.db.execute(
            """
            INSERT INTO afk_config (guild_id, timeout_minutes, log_channel_id)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                timeout_minutes = excluded.timeout_minutes,
                log_channel_id = excluded.log_channel_id
            """,
            (
                guild_id,
                timeout_minutes if timeout_minutes is not None else current["timeout_minutes"],
                log_channel_id if log_channel_id is not None else current["log_channel_id"],
            ),
        )
        await self.bot.db.commit()

    async def _afk_ignored_channels(self, guild_id: int) -> set[int]:
        cursor = await self.bot.db.execute(
            "SELECT channel_id FROM afk_ignore_channels WHERE guild_id = ?",
            (guild_id,),
        )
        return {row["channel_id"] for row in await cursor.fetchall()}

    async def add_afk_ignore(self, guild_id: int, channel_id: int) -> None:
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO afk_ignore_channels (guild_id, channel_id) VALUES (?, ?)",
            (guild_id, channel_id),
        )
        await self.bot.db.commit()

    async def remove_afk_ignore(self, guild_id: int, channel_id: int) -> int:
        cursor = await self.bot.db.execute(
            "DELETE FROM afk_ignore_channels WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )
        await self.bot.db.commit()
        return cursor.rowcount or 0

    # ------------------------------------------------------------------
    # Database helpers — AFK presets
    # ------------------------------------------------------------------

    async def list_afk_presets(self, user_id: int) -> list[dict[str, Any]]:
        cursor = await self.bot.db.execute(
            "SELECT name, reason FROM afk_presets WHERE user_id = ? ORDER BY name ASC",
            (user_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_afk_preset(self, user_id: int, name: str) -> Optional[dict[str, Any]]:
        row = await (await self.bot.db.execute(
            "SELECT name, reason FROM afk_presets WHERE user_id = ? AND name = ?",
            (user_id, name),
        )).fetchone()
        return dict(row) if row else None

    async def add_afk_preset(self, user_id: int, name: str, reason: str) -> bool:
        try:
            await self.bot.db.execute(
                """
                INSERT INTO afk_presets (user_id, name, reason)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, name) DO UPDATE SET
                    reason = excluded.reason
                """,
                (user_id, name[:32], reason[:480]),
            )
            await self.bot.db.commit()
            return True
        except Exception:
            return False

    async def delete_afk_preset(self, user_id: int, name: str) -> int:
        cursor = await self.bot.db.execute(
            "DELETE FROM afk_presets WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        await self.bot.db.commit()
        return cursor.rowcount or 0

    # ------------------------------------------------------------------
    # Background sweepers — Giveaways
    # ------------------------------------------------------------------

    @tasks.loop(seconds=30)
    async def _giveaway_sweeper(self) -> None:
        """Ends any giveaways whose ``ends_at`` has elapsed."""
        try:
            due = await self.list_due_giveaways()
        except Exception as err:
            log.error(f"giveaway sweep query failed: {err}")
            return
        for row in due:
            try:
                msg, winners = await self.end_giveaway(row["guild_id"], row["message_id"])
            except Exception as err:
                log.error(f"could not auto-end giveaway {row['message_id']}: {err}")
                continue
            # Announce winners in the original channel
            guild = self.bot.get_guild(row["guild_id"])
            channel = guild.get_channel(row["channel_id"]) if guild else None
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                continue
            if not winners:
                try:
                    await channel.send(
                        content=(
                            f"🎉 The giveaway for **{row['prize']}** has ended.\n"
                            f"There were no valid entries to pick a winner from."
                        )
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
            else:
                mentions = " ".join(f"<@{wid}>" for wid in winners)
                host_mention = f"<@{row['host_id']}>" if guild else "the host"
                try:
                    await channel.send(
                        content=(
                            f"🎉 Congratulations {mentions}! You won **{row['prize']}** "
                            f"({'1 winner' if len(winners) == 1 else f'{len(winners)} winners'})!\n"
                            f"Please open a ticket with {host_mention} to claim your prize."
                        )
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass

    @_giveaway_sweeper.before_loop
    async def _before_giveaway_sweeper(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Background sweeper — AFK timeouts
    # ------------------------------------------------------------------

    @tasks.loop(minutes=2)
    async def _afk_sweeper(self) -> None:
        try:
            cursor = await self.bot.db.execute(
                "SELECT guild_id, timeout_minutes FROM afk_config WHERE timeout_minutes > 0"
            )
            servers = await cursor.fetchall()
        except Exception as err:
            log.error(f"afk config sweep query failed: {err}")
            return

        for row in servers:
            guild_id = row["guild_id"]
            minutes = row["timeout_minutes"] or 0
            if minutes <= 0:
                continue
            try:
                members = await self.list_afk(guild_id)
            except Exception:
                continue
            for afk in members:
                since_raw = afk["since"]
                since: Optional[datetime] = None
                if isinstance(since_raw, datetime):
                    since = since_raw.replace(tzinfo=None) if since_raw.tzinfo is None else since_raw
                else:
                    try:
                        since = datetime.fromisoformat(str(since_raw))
                    except Exception:
                        continue
                if since is None:
                    continue
                if since.tzinfo is None:
                    since = since.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(tz=timezone.utc) - since).total_seconds()
                if elapsed < minutes * 60:
                    continue
                # Time to clear this user
                await self.clear_afk(afk["user_id"])
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    continue
                member = guild.get_member(afk["user_id"])
                if member is None:
                    continue
                cfg = await self.get_afk_config(guild_id)
                log_chan = guild.get_channel(cfg["log_channel_id"]) if cfg["log_channel_id"] else None
                if isinstance(log_chan, (discord.TextChannel, discord.Thread)):
                    try:
                        await log_chan.send(
                            embed=Embed(
                                description=(
                                    f"{EMOJIS.APPROVE} Cleared AFK for {member.mention} "
                                    f"after **{minutes}** minutes of inactivity."
                                ),
                                color=COLORS.approve,
                            )
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        pass

    @_afk_sweeper.before_loop
    async def _before_afk_sweeper(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Event listeners — Invites
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not hasattr(self, "_invite_cache"):
            self._invite_cache = {}
        # Cache all invites on startup so we can compare delta on join.
        await self._refresh_invite_cache()

    async def _refresh_invite_cache(self) -> None:
        cache: dict[int, dict[str, int]] = {}
        for guild in list(self.bot.guilds):
            try:
                cache[guild.id] = await self.cache_invites(guild)
            except Exception:
                cache[guild.id] = {}
            # also push into persistent storage
            try:
                for invite in await guild.invites():
                    await self.upsert_invite(
                        guild.id,
                        invite.code,
                        inviter_id=invite.inviter.id if invite.inviter else None,
                        channel_id=invite.channel.id if invite.channel else None,
                        url=invite.url,
                        uses=invite.uses or 0,
                    )
            except (discord.Forbidden, discord.HTTPException):
                continue
        self._invite_cache = cache

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        if not member.guild:
            return

        previous = self._invite_cache.get(member.guild.id, {})
        current = await self.cache_invites(member.guild)

        used_code: Optional[str] = None
        try:
            for code, uses in current.items():
                prior = previous.get(code, 0)
                if uses > prior:
                    used_code = code
                    await self.upsert_invite(
                        member.guild.id, code,
                        inviter_id=None,
                        channel_id=None,
                        url=None,
                        uses=uses,
                    )
                    # Refresh inviter attribution
                    try:
                        for invite in await member.guild.invites():
                            if invite.code == code:
                                await self.upsert_invite(
                                    member.guild.id, code,
                                    inviter_id=invite.inviter.id if invite.inviter else None,
                                    channel_id=invite.channel.id if invite.channel else None,
                                    url=invite.url,
                                    uses=invite.uses or 0,
                                )
                                break
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                    break
        finally:
            self._invite_cache[member.guild.id] = current

        await self.record_invite_join(member.guild.id, member.id, used_code)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        if not invite.guild:
            return
        await self.upsert_invite(
            invite.guild.id, invite.code,
            inviter_id=invite.inviter.id if invite.inviter else None,
            channel_id=invite.channel.id if invite.channel else None,
            url=invite.url,
            uses=invite.uses or 0,
        )
        cache = self._invite_cache.setdefault(invite.guild.id, {})
        cache[invite.code] = invite.uses or 0

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        if not invite.guild:
            return
        await self.remove_invite(invite.guild.id, invite.code)
        cache = self._invite_cache.get(invite.guild.id)
        if cache is not None:
            cache.pop(invite.code, None)

    # ------------------------------------------------------------------
    # Event listeners — Levels
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild or not isinstance(message.author, discord.Member):
            return
        guild = message.guild
        if message.channel.id in {message.channel.id for _ in [None]}:  # placeholder, see exclude logic below
            pass

        config = await self.get_level_config(guild.id)
        if not config["enabled"]:
            return

        # ensure guild object cached to avoid zombies
        if not isinstance(guild, discord.Guild):
            return

        # exclude bots handled above
        excluded = await self.level_excludes(guild.id)
        excluded_channel_ids = {tid for kind, tid in excluded if kind == "channel"}
        excluded_role_ids = {tid for kind, tid in excluded if kind == "role"}

        if message.channel.id in excluded_channel_ids:
            return
        if any(role.id in excluded_role_ids for role in message.author.roles):
            return

        # Cooldown: only one XP gain per cooldown window per user.
        now_ts = int(datetime.now(tz=timezone.utc).timestamp())
        entry = await self.get_level_entry(guild.id, message.author.id)
        if now_ts - (entry["last_message_ts"] or 0) < int(config["cooldown_seconds"]):
            return

        amount = random.randint(
            max(1, int(config["xp_min"])),
            max(int(config["xp_min"]), int(config["xp_max"])),
        )
        result = await self.add_xp(guild.id, message.author.id, amount)
        if result is None:
            return
        old_level, new_level, _ = result

        rewards = await self.level_rewards_for(guild.id)
        for required_level, role_id in rewards:
            if old_level < required_level <= new_level:
                role = guild.get_role(role_id)
                if role and role not in message.author.roles:
                    try:
                        if guild.me.top_role > role and guild.me.guild_permissions.manage_roles:
                            await message.author.add_roles(role, reason=f"Level reward for level {required_level}")
                    except (discord.Forbidden, discord.HTTPException):
                        pass

        if not config["announce"]:
            return

        target_channel: Optional[discord.TextChannel] = None
        if config["level_up_channel_id"]:
            ch = guild.get_channel(config["level_up_channel_id"])
            if isinstance(ch, discord.TextChannel):
                target_channel = ch
        if target_channel is None and isinstance(message.channel, discord.TextChannel):
            target_channel = message.channel

        if target_channel is None:
            return

        template = config.get("level_up_message") or (
            "🎉 {user.mention} just advanced to **level {level}** in **{guild.name}**!"
        )
        try:
            kwargs = await render_message(
                self,
                template,
                author=message.author,
                guild=guild,
                channel=target_channel,
                extra={"level": str(new_level)},
            )
        except Exception:
            kwargs = {}
        if not kwargs:
            kwargs = {"content": f"🎉 {message.author.mention} just advanced to **level {new_level}**!"}
        try:
            await target_channel.send(**kwargs)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ------------------------------------------------------------------
    # Event listeners — Giveaways (reaction entries)
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        try:
            await self._handle_giveaway_reaction(payload, added=True)
        except Exception as err:
            log.error(f"giveaway reaction add failed: {err}")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        try:
            await self._handle_giveaway_reaction(payload, added=False)
        except Exception as err:
            log.error(f"giveaway reaction remove failed: {err}")

    async def _handle_giveaway_reaction(
        self,
        payload: discord.RawReactionActionEvent,
        *,
        added: bool,
    ) -> None:
        if payload.guild_id is None or payload.message_id is None:
            return
        if str(payload.emoji) != GIVEAWAY_REACTION:
            return
        record = await self.get_giveaway(payload.message_id)
        if not record or record["guild_id"] != payload.guild_id or record["ended"]:
            return
        if added:
            await self.add_giveaway_entry(
                payload.guild_id,
                payload.channel_id,
                payload.message_id,
                payload.user_id,
            )
        else:
            await self.remove_giveaway_entry(
                payload.guild_id,
                payload.channel_id,
                payload.message_id,
                payload.user_id,
            )

    # ------------------------------------------------------------------
    # Event listeners — AFK auto-clear
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message_afk(self, message: discord.Message) -> None:
        """When an AFK user writes a non-ignored message, ping them and clear AFK."""
        if message.author.bot or message.guild is None:
            return
        if not isinstance(message.author, discord.Member):
            return

        afk = await self.get_afk(message.author.id)
        if afk is None:
            return
        guild = message.guild
        ignored = await self._afk_ignored_channels(guild.id)
        if message.channel.id in ignored:
            return
        # Mention the user isn't allowed while AFK (and remove their AFK status)
        try:
            await message.channel.send(
                content=(
                    f"Welcome back {message.author.mention}! I removed your AFK status "
                    f"you were AFK since {_fmt_relative( datetime.fromisoformat(afk['since']).replace(tzinfo=timezone.utc) if isinstance(afk['since'], str) else afk['since']) }."
                ),
                delete_after=8,
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
        await self.clear_afk(message.author.id)

        # Forward members mentioning an AFK user
        for mentioned in message.mentions:
            if mentioned.bot or mentioned.id == message.author.id:
                continue
            afked = await self.get_afk(mentioned.id)
            if afked is None:
                continue
            try:
                since_value = (
                    datetime.fromisoformat(afked['since']).replace(tzinfo=timezone.utc)
                    if isinstance(afked['since'], str)
                    else afked['since']
                )
                content = (
                    f"💤 {mentioned.display_name} is currently AFK — "
                    f"**{afked['reason']}** (since {_fmt_relative(since_value)})."
                )
                await message.channel.send(
                    content=content,
                    delete_after=10,
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        # Optionally log the AFK clearing event
        cfg = await self.get_afk_config(guild.id)
        log_ch = guild.get_channel(cfg["log_channel_id"]) if cfg.get("log_channel_id") else None
        if isinstance(log_ch, (discord.TextChannel, discord.Thread)):
            try:
                await log_ch.send(
                    embed=Embed(
                        description=(
                            f"{EMOJIS.APPROVE} {message.author.mention}'s AFK state was cleared "
                            f"automatically upon sending a message."
                        ),
                        color=COLORS.approve,
                    )
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

    # ------------------------------------------------------------------
    # Event listeners — Starboards
    # ------------------------------------------------------------------

    async def _starboard_emoji_key(self, emoji: str) -> str:
        """Return the canonical emoji key used to count toward a starboard."""
        return emoji

    def _is_self_react(self, payload: discord.RawReactionActionEvent, *, message: discord.Message) -> bool:
        return payload.user_id == message.author.id

    async def _handle_starboard_reaction(self, payload: discord.RawReactionActionEvent, *, added: bool) -> None:
        if payload.guild_id is None or payload.member is None and added:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        # We need the original message to inspect the author's roles & count users
        channel = guild.get_channel(payload.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            return

        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        starboards = await self.list_starboards(guild.id)
        if not starboards:
            return

        for starboard in starboards:
            if starboard["locked"]:
                continue

            # Emoji filtering
            if payload.emoji.name != starboard["emoji"] and str(payload.emoji) != starboard["emoji"]:
                # Custom emoji - check id/name match
                if getattr(payload.emoji, "id", None) is not None:
                    if str(payload.emoji) != starboard["emoji"]:
                        continue
                else:
                    continue

            # Ignore filter
            ignores = await self.list_starboard_ignores(guild.id, starboard["name"])
            kinds = {"channel": set(), "role": set(), "user": set()}
            for kind, tid in ignores:
                kinds.setdefault(kind, set()).add(tid)
            if message.author.id in kinds["user"]:
                continue
            if message.channel.id in kinds["channel"]:
                continue
            if isinstance(message.author, discord.Member):
                if any(r.id in kinds["role"] for r in message.author.roles):
                    continue

            # Self-reactions
            if self._is_self_react(payload, message=message) and not starboard["self_react"]:
                continue

            # Count matching reactions
            count = 0
            stargazers: list[int] = []
            for reaction in message.reactions:
                if str(reaction.emoji) == starboard["emoji"] and reaction.emoji.name == starboard["emoji"]:
                    async for user in reaction.users():
                        if user.bot:
                            continue
                        # Re-check self-reaction once we iterate
                        if user.id == message.author.id and not starboard["self_react"]:
                            continue
                        count += 1
                        stargazers.append(user.id)
                        if count >= max(starboard["threshold"] * 2, starboard["threshold"]):
                            break
                    break

            existing = await self.get_starboard_post(guild.id, starboard["name"], message.id)

            if count >= starboard["threshold"]:
                # Post or update starboard embed
                color_value = starboard["color"] or 0xFFCC00
                embed = Embed(
                    description=message.content or None,
                    color=color_value,
                    timestamp=message.created_at,
                    url=message.jump_url,
                )
                embed.set_author(
                    name=message.author.display_name,
                    icon_url=message.author.display_avatar.url,
                )
                if message.attachments:
                    first = message.attachments[0]
                    if first.content_type and first.content_type.startswith("image/"):
                        embed.set_image(url=first.url)

                footer = f"{starboard['emoji']} {count}  •  {message.channel.name}"
                embed.set_footer(text=footer)

                target_channel = guild.get_channel(starboard["channel_id"])
                if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
                    continue

                starboard_msg_id: Optional[int] = None
                if existing:
                    try:
                        m = await target_channel.fetch_message(existing["starboard_message_id"])
                        await m.edit(embed=embed)
                        starboard_msg_id = m.id
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        existing = None

                if existing is None:
                    try:
                        msg = await target_channel.send(
                            content=f"{starboard['emoji']} **{count}**  {message.channel.mention}",
                            embed=embed,
                        )
                        starboard_msg_id = msg.id
                    except (discord.Forbidden, discord.HTTPException):
                        continue

                await self.upsert_starboard_post(
                    guild.id,
                    starboard["name"],
                    message.id,
                    starboard_msg_id,
                    stargazers,
                )
            elif existing and count < starboard["threshold"] and added:
                # reaction removed dropped us under threshold
                target_channel = guild.get_channel(starboard["channel_id"])
                if isinstance(target_channel, (discord.TextChannel, discord.Thread)):
                    try:
                        m = await target_channel.fetch_message(existing["starboard_message_id"])
                        await m.delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
                await self.delete_starboard_post(guild.id, starboard["name"], message.id)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        try:
            await self._handle_starboard_reaction(payload, added=True)
        except Exception as err:  # never let a starboard bug break reaction flow
            log.error(f"starboard reaction add failed: {err}")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        try:
            await self._handle_starboard_reaction(payload, added=False)
        except Exception as err:
            log.error(f"starboard reaction remove failed: {err}")

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        cursor = await self.bot.db.execute(
            "SELECT starboard_name, starboard_message_id FROM starboard_posts "
            "WHERE guild_id = ? AND source_message_id = ?",
            (payload.guild_id, payload.message_id),
        )
        rows = await cursor.fetchall()

        for row in rows:
            target = guild.get_channel((await self.get_starboard(guild.id, row["starboard_name"]) or {}).get("channel_id") or 0)
            if isinstance(target, (discord.TextChannel, discord.Thread)):
                try:
                    m = await target.fetch_message(row["starboard_message_id"])
                    await m.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            await self.delete_starboard_post(guild.id, row["starboard_name"], payload.message_id)
# =============================================================================
# Command groups
# =============================================================================

    # ------------------------------------------------------------------
    # Suggestions command group
    # ------------------------------------------------------------------

    @hybrid_group(
        name="suggestions",
        aliases=["suggest", "suggestion", "suggestions"],
        description="Manage server suggestions.",
        example=",suggestions setup",
    )
    @commands.guild_only()
    async def suggestions(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @suggestions.command(
        name="setup",
        aliases=["config", "configure"],
        description="Interactive setup wizard for the suggestions system.",
        example=",suggestions setup",
    )
    @has_permissions(administrator=True)
    @commands.bot_has_permissions(send_messages=True, manage_messages=True)
    async def suggestions_setup(self, ctx: commands.Context) -> None:
        if not ctx.interaction:
            return await ctx.send(
                embed=Embed(
                    description=(
                        f"{EMOJIS.WARN} The setup wizard can only be used as a **slash command**.\n"
                        f"Try `/suggestions setup` instead."
                    ),
                    color=COLORS.warn,
                )
            )
        await ctx.interaction.response.send_message(
            embed=Embed(
                title="Suggestions — Setup",
                description=(
                    "Pick the channel where suggestion panels should be posted.\n"
                    "After picking the channel, you can also provide an optional embed template."
                ),
                color=COLORS.neutral,
            ),
            view=SuggestSetupView(self, ctx),
        )

    @suggestions.command(
        name="panel",
        description="Re-post the server's suggestion panel (binds the button again).",
        example=",suggestions panel",
    )
    @has_permissions(administrator=True)
    @commands.bot_has_permissions(send_messages=True)
    async def suggestions_panel(self, ctx: commands.Context) -> None:
        config = await self.get_suggestions_config(ctx.guild.id)
        if not config["channel_id"]:
            return await ctx.warn("Suggestions are not configured yet. Run `,suggestions setup` first.")

        channel = ctx.guild.get_channel(config["channel_id"])
        if channel is None or not isinstance(channel, discord.TextChannel):
            return await ctx.warn("The previously configured channel is missing.")
        if not channel.permissions_for(ctx.guild.me).send_messages:
            return ctx.warn("I do not have permission to send messages in that channel.")

        template = config.get("panel_template") or (
            "{embed}$vtitle:Suggestions$vdescription:Press the button below to make a suggestion.$vcolor:5865F2"
        )
        try:
            kwargs = await render_message(
                self,
                template,
                author=ctx.author,
                guild=ctx.guild,
                channel=channel,
            )
        except Exception as err:
            return await ctx.deny(f"Failed to render the panel: `{err}`.")

        # Pin the interactive surface to the suggestion button. The user's
        # template may include other components, but stacking them on top of
        # the persistent "Make a suggestion" button has historically pushed us
        # over Discord's 4000-character limit on displayable component text.
        kwargs.pop("view", None)
        kwargs["view"] = SuggestPanelView(self)

        try:
            sent = await channel.send(**kwargs)
        except discord.HTTPException as err:
            return await ctx.deny(f"Could not post the panel: `{err}`.")

        await self.save_suggestions_config(ctx.guild.id, panel_message_id=sent.id)
        await ctx.approve(f"Suggestion panel has been reposted in {channel.mention}.")

    @suggestions.command(
        name="blacklist",
        aliases=["block", "deny"],
        description="Blacklist a user from making suggestions.",
        example=",suggestions blacklist @Spammer",
    )
    @has_permissions(administrator=True)
    async def suggestions_blacklist(self, ctx: commands.Context, user: discord.Member) -> None:
        if await self.is_blacklisted(ctx.guild.id, user.id):
            return await ctx.warn(f"{user.mention} is already **blacklisted**.")
        ok = await self.blacklist_user(
            ctx.guild.id, user.id, moderator_id=ctx.author.id, reason=None,
        )
        if not ok:
            return await ctx.deny("Failed to blacklist that user.")
        await ctx.approve(f"{user.mention} is now **blacklisted** from making suggestions.")

    @suggestions.command(
        name="unblacklist",
        aliases=["unblock", "allow"],
        description="Remove a user from the suggestion blacklist.",
        example=",suggestions unblacklist @Spammer",
    )
    @has_permissions(administrator=True)
    async def suggestions_unblacklist(self, ctx: commands.Context, user: discord.Member) -> None:
        removed = await self.unblacklist_user(ctx.guild.id, user.id)
        if not removed:
            return await ctx.warn(f"{user.mention} is not blacklisted.")
        await ctx.approve(f"{user.mention} can now make suggestions again.")

    # ------------------------------------------------------------------
    # Invite commands
    # ------------------------------------------------------------------

    @hybrid_command(
        name="invited",
        aliases=["invitesby", "invites_by", "invitedby"],
        description="List the members invited by a specific user (paginated).",
        example=",invited @Alice",
    )
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def invited(self, ctx: commands.Context, target: discord.Member, page: int = 1) -> None:
        if page < 1:
            page = 1

        rows = await self.invited_users(ctx.guild.id, target.id)
        if not rows:
            return await ctx.warn(f"{target.mention} has not invited anyone that I could track.")

        joined_map = {row["user_id"]: row for row in rows}
        try:
            entries = sorted(joined_map.values(), key=lambda r: r["joined_at"], reverse=True)
        except Exception:
            entries = list(joined_map.values())

        per_page = 10
        pages = max(1, math.ceil(len(entries) / per_page))
        page = min(page, pages)
        start = (page - 1) * per_page
        chunk = entries[start:start + per_page]

        lines = []
        for row in chunk:
            member = ctx.guild.get_member(row["user_id"])
            display = member.mention if member else f"<@{row['user_id']}>"
            joined_raw = row.get("joined_at") or ""
            try:
                dt = datetime.fromisoformat(joined_raw)
                joined = discord.utils.format_dt(dt, style="R")
            except Exception:
                joined = joined_raw or "unknown"
            lines.append(f"**{display}** — joined {joined} via `{row['invite_code']}`")

        embed = Embed(
            title=f"Members invited by {target.display_name}",
            description="\n".join(lines),
            color=COLORS.neutral,
        )
        embed.set_footer(text=f"Page {page}/{pages} • {len(entries)} total")
        embed.set_thumbnail(url=target.display_avatar.url)
        await ctx.send(embed=embed)

    @hybrid_command(
        name="invites",
        aliases=["invitestats", "invite_info"],
        description="View the invite statistics of a member.",
        example=",invites @Alice",
    )
    @commands.guild_only()
    async def invites(self, ctx: commands.Context, target: discord.Member) -> None:
        stats = await self.invite_stats(ctx.guild.id, target.id)
        values = await self.invited_users(ctx.guild.id, target.id)

        cursor = await self.bot.db.execute(
            "SELECT code, uses, channel_id FROM invites WHERE guild_id = ? AND inviter_id = ? "
            "ORDER BY uses DESC",
            (ctx.guild.id, target.id),
        )
        invites_rows = await cursor.fetchall()

        embed = Embed(
            title=f"Invite stats — {target.display_name}",
            color=COLORS.neutral,
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(
            name="Total uses",
            value=str(stats["total"]),
            inline=True,
        )
        embed.add_field(
            name="Tracked invites",
            value=str(stats["invites"]),
            inline=True,
        )
        embed.add_field(
            name="Members recorded",
            value=str(len(values)),
            inline=True,
        )

        if invites_rows:
            lines = []
            for row in invites_rows[:6]:
                ch = ctx.guild.get_channel(row["channel_id"]) if row["channel_id"] else None
                ch_label = ch.mention if ch else "deleted channel"
                lines.append(f"`{row['code']}` → **{row['uses']}** uses ({ch_label})")
            embed.add_field(
                name="Top invite codes",
                value="\n".join(lines)[:1024],
                inline=False,
            )

        await ctx.send(embed=embed)

    @hybrid_command(
        name="inviter",
        aliases=["inviterof", "who_invited"],
        description="Show who invited a given user.",
        example=",inviter @NewMember",
    )
    @commands.guild_only()
    async def inviter_command(self, ctx: commands.Context, target: discord.Member) -> None:
        code = await self.invite_join_code(ctx.guild.id, target.id)
        if not code:
            return await ctx.warn(f"I could not determine who invited {target.mention}.")

        row = await (await self.bot.db.execute(
            "SELECT inviter_id, channel_id FROM invites WHERE guild_id = ? AND code = ?",
            (ctx.guild.id, code),
        )).fetchone()
        if not row or not row["inviter_id"]:
            return await ctx.warn(
                f"I know {target.mention} joined, but the original inviter could not be resolved."
            )

        inviter = ctx.guild.get_member(row["inviter_id"]) or await self.bot.fetch_user(row["inviter_id"])
        embed = Embed(
            title="Inviter",
            color=COLORS.neutral,
        )
        embed.add_field(name="Member", value=target.mention, inline=True)
        embed.add_field(name="Inviter", value=getattr(inviter, "mention", f"`{row['inviter_id']}`"), inline=True)
        embed.add_field(name="Invite code", value=f"`{code}`", inline=True)
        await ctx.send(embed=embed)
# ------------------------------------------------------------------
    # Level command group
    # ------------------------------------------------------------------

    @hybrid_group(
        name="level",
        aliases=["levels", "lvl", "rank"],
        description="Server leveling system.",
        example=",level rank",
    )
    @commands.guild_only()
    async def level(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            entry = await self.get_level_entry(ctx.guild.id, ctx.author.id)
            level_now, into, needed = xp_progress(entry["xp"])
            rank = await self.level_rank(ctx.guild.id, ctx.author.id)

            embed = Embed(
                title=f"{ctx.author.display_name}'s level",
                color=COLORS.neutral,
            )
            embed.set_thumbnail(url=ctx.author.display_avatar.url)
            embed.add_field(name="Level", value=str(level_now), inline=True)
            embed.add_field(name="XP", value=f"{into} / {needed}", inline=True)
            embed.add_field(
                name="Server rank",
                value=f"#{rank}" if rank is not None else "Unranked",
                inline=True,
            )
            embed.add_field(
                name="Progress",
                value=self._progress_bar(into, needed),
                inline=False,
            )
            embed.set_footer(text=f"Total XP: {entry['total_xp']}")
            await ctx.send(embed=embed)

    @staticmethod
    def _progress_bar(current: int, total: int, *, length: int = 14) -> str:
        ratio = 0 if total <= 0 else min(1.0, current / total)
        filled = int(round(ratio * length))
        bar = "█" * filled + "░" * (length - filled)
        pct = int(ratio * 100)
        return f"`{bar}` {pct}%"

    @level.command(
        name="rank",
        aliases=["position", "place"],
        description="Show your ranking in the server leaderboard.",
        example=",level rank",
    )
    async def level_rank_cmd(self, ctx: commands.Context) -> None:
        entry = await self.get_level_entry(ctx.guild.id, ctx.author.id)
        rank = await self.level_rank(ctx.guild.id, ctx.author.id)
        total = entry["total_xp"]
        level_now, into, needed = xp_progress(entry["xp"])
        embed = Embed(
            title=f"Ranking for {ctx.author.display_name}",
            color=COLORS.neutral,
        )
        embed.set_thumbnail(url=ctx.author.display_avatar.url)
        embed.add_field(name="Level", value=str(level_now), inline=True)
        embed.add_field(
            name="Rank",
            value=f"#{rank}" if rank is not None else "Unranked",
            inline=True,
        )
        embed.add_field(name="Total XP", value=str(total), inline=True)
        embed.add_field(
            name="Progress",
            value=self._progress_bar(into, needed),
            inline=False,
        )
        await ctx.send(embed=embed)

    @level.command(
        name="up",
        description="Preview what your level-up announcement would look like.",
        example=",level up",
    )
    async def level_up_preview(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        target = member or ctx.author
        entry = await self.get_level_entry(ctx.guild.id, target.id)
        upcoming = level_for_xp(entry["xp"]) + 1

        config = await self.get_level_config(ctx.guild.id)
        template = config["level_up_message"] or "🎉 {user.mention} just advanced to **level {level}** in **{guild.name}**!"
        try:
            kwargs = await render_message(
                self,
                template,
                author=target,
                guild=ctx.guild,
                channel=ctx.channel if isinstance(ctx.channel, discord.abc.GuildChannel) else None,
                extra={"level": str(upcoming)},
            )
        except Exception as err:
            return await ctx.deny(f"Failed to render the preview: `{err}`.")

        if not kwargs:
            kwargs = {
                "content": f"🎉 {target.mention} just advanced to **level {upcoming}** in **{ctx.guild.name}**!"
            }
        await ctx.send(**kwargs)

    @level.command(
        name="add",
        aliases=["give"],
        description="Give XP to a member (administrator).",
        example=",level add @Member 100",
    )
    @has_permissions(administrator=True)
    async def level_add(self, ctx: commands.Context, member: discord.Member, amount: int) -> None:
        if amount <= 0:
            return await ctx.warn("Amount must be positive.")
        result = await self.add_xp(ctx.guild.id, member.id, amount)
        if result is None:
            entry = await self.get_level_entry(ctx.guild.id, member.id)
            await ctx.approve(
                f"Added **{amount}** XP to {member.mention}. They are now level **{level_for_xp(entry['xp'])}**."
            )
        else:
            old, new, _ = result
            await ctx.approve(
                f"Added **{amount}** XP to {member.mention}. They advanced from level **{old}** to **{new}**."
            )

    @level.command(
        name="remove",
        aliases=["remote", "rm"],
        description="Remove XP from a member (administrator).",
        example=",level remove @Member 50",
    )
    @has_permissions(administrator=True)
    async def level_remove(self, ctx: commands.Context, member: discord.Member, amount: int) -> None:
        if amount <= 0:
            return await ctx.warn("Amount must be positive.")
        removed = abs(amount)
        delta = await self.remove_xp(ctx.guild.id, member.id, removed)
        entry = await self.get_level_entry(ctx.guild.id, member.id)
        await ctx.approve(
            f"Removed **{delta}** XP from {member.mention}. They are now level **{level_for_xp(entry['xp'])}**."
        )

    @level.command(
        name="leaderboard",
        aliases=["top", "lb"],
        description="Show the leveling leaderboard.",
        example=",level leaderboard",
    )
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def level_leaderboard(self, ctx: commands.Context, page: int = 1) -> None:
        if page < 1:
            page = 1

        per_page = 10
        offset = (page - 1) * per_page
        cursor = await self.bot.db.execute(
            "SELECT user_id, xp, total_xp FROM levels WHERE guild_id = ? "
            "ORDER BY xp DESC, total_xp DESC LIMIT ? OFFSET ?",
            (ctx.guild.id, per_page + 1, offset),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        has_next = len(rows) > per_page
        rows = rows[:per_page]

        if not rows:
            return await ctx.warn("There is no leveling data yet.")

        lines = []
        for index, row in enumerate(rows, start=offset + 1):
            member = ctx.guild.get_member(row["user_id"])
            display = member.mention if member else f"<@{row['user_id']}>"
            lines.append(
                f"**#{index}** {display} — level **{level_for_xp(row['xp'])}** • "
                f"XP **{row['xp']}** (total {row['total_xp']})"
            )

        embed = Embed(
            title=f"{ctx.guild.name} leaderboard",
            description="\n".join(lines),
            color=COLORS.neutral,
        )
        embed.set_footer(text=f"Page {page}{' • More on next page' if has_next else ''}")
        await ctx.send(embed=embed)

    @level.command(
        name="exempt",
        aliases=["exclude", "ignore"],
        description="Exclude channels or roles from gaining XP.",
        example=",level exempt #announcements @Bots",
    )
    @has_permissions(administrator=True)
    async def level_exempt(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
        role: Optional[discord.Role] = None,
    ) -> None:
        if channel is None and role is None:
            return await ctx.warn("Provide a **channel** or **role** to exclude.")

        async def add(kind: str, target_id: int, label: str) -> None:
            ok = await self.add_level_exclude(ctx.guild.id, kind, target_id)
            if ok:
                await ctx.approve(f"{label} is now **excluded** from gaining XP.")

        if channel is not None:
            await add("channel", channel.id, channel.mention)
        if role is not None:
            await add("role", role.id, role.mention)

    async def _level_unexempt(
        self,
        ctx: commands.Context,
        *,
        channel: Optional[discord.TextChannel],
        role: Optional[discord.Role],
    ) -> None:
        if channel is None and role is None:
            return await ctx.warn("Provide a **channel** or **role** to include again.")
        if channel is not None:
            r = await self.remove_level_exclude(ctx.guild.id, "channel", channel.id)
            if r:
                await ctx.approve(f"{channel.mention} now grants XP again.")
            else:
                await ctx.warn(f"{channel.mention} was not excluded.")
        if role is not None:
            r = await self.remove_level_exclude(ctx.guild.id, "role", role.id)
            if r:
                await ctx.approve(f"{role.mention} now grants XP again.")
            else:
                await ctx.warn(f"{role.mention} was not excluded.")

    @level.command(
        name="unexempt",
        aliases=["include"],
        description="Re-include a previously excluded channel or role.",
        example=",level unexempt #announcements",
    )
    @has_permissions(administrator=True)
    async def level_unexempt_cmd(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
        role: Optional[discord.Role] = None,
    ) -> None:
        await self._level_unexempt(ctx, channel=channel, role=role)

    @level.command(
        name="settings",
        description="View the current leveling configuration.",
        example=",level settings",
    )
    @has_permissions(administrator=True)
    async def level_settings(self, ctx: commands.Context) -> None:
        config = await self.get_level_config(ctx.guild.id)
        excludes = await self.level_excludes(ctx.guild.id)
        rewards = await self.level_rewards_for(ctx.guild.id)

        embed = Embed(
            title=f"Leveling settings — {ctx.guild.name}",
            color=COLORS.neutral,
        )
        embed.add_field(
            name="Status",
            value="Enabled" if config["enabled"] else "Disabled",
            inline=True,
        )
        embed.add_field(
            name="XP range",
            value=f"{config['xp_min']} – {config['xp_max']}",
            inline=True,
        )
        embed.add_field(
            name="Cooldown",
            value=f"{config['cooldown_seconds']}s",
            inline=True,
        )
        embed.add_field(
            name="Announcements",
            value="On" if config["announce"] else "Off",
            inline=True,
        )
        ch_label = (
            f"<#{config['level_up_channel_id']}>"
            if config["level_up_channel_id"]
            else "same channel as the activity"
        )
        embed.add_field(name="Announcement channel", value=ch_label, inline=True)
        embed.add_field(
            name="Level-up message",
            value=(
                f"```{config['level_up_message']}```"
                if config["level_up_message"]
                else "*default*"
            ),
            inline=False,
        )

        if excludes:
            lines = []
            for kind, tid in excludes:
                if kind == "channel":
                    lines.append(f"• <#{tid}> (channel)")
                else:
                    lines.append(f"• <@&{tid}> (role)")
            embed.add_field(name="Excluded", value="\n".join(lines)[:1024], inline=False)

        if rewards:
            lines = [
                f"• Level **{lvl}** → <@&{role_id}>"
                for lvl, role_id in rewards
            ]
            embed.add_field(name="Role rewards", value="\n".join(lines)[:1024], inline=False)

        await ctx.send(embed=embed)

    @level.command(
        name="message",
        aliases=["msg", "announcement"],
        description="Set the level-up announcement template.",
        example=",level message 🎉 {user.mention} is now level {level}!",
    )
    @has_permissions(administrator=True)
    async def level_message(self, ctx: commands.Context, *, template: str) -> None:
        if "{level}" not in template:
            return await ctx.warn("Your template must include the `{level}` token.")
        await self.save_level_config(ctx.guild.id, level_up_message=template)
        await ctx.approve("The level-up message has been updated.")

    @level.command(
        name="announce",
        aliases=["announcements"],
        description="Toggle level-up announcements on or off.",
        example=",level announce on",
    )
    @has_permissions(administrator=True)
    async def level_announce(self, ctx: commands.Context, state: str) -> None:
        value = _parse_bool(state)
        if value is None:
            return await ctx.warn("Provide `on` or `off`.")
        await self.save_level_config(ctx.guild.id, announce=value, enabled=True if value else False)
        await ctx.approve(f"Announcements are now **{'on' if value else 'off'}**.")

    @level.command(
        name="toggle",
        description="Enable or disable the leveling system.",
        example=",level toggle on",
    )
    @has_permissions(administrator=True)
    async def level_toggle(self, ctx: commands.Context, state: str) -> None:
        value = _parse_bool(state)
        if value is None:
            return await ctx.warn("Provide `on` or `off`.")
        await self.save_level_config(ctx.guild.id, enabled=value)
        await ctx.approve(f"Leveling is now **{'enabled' if value else 'disabled'}**.")

    @level.command(
        name="rate",
        description="Set the XP range per qualifying message.",
        example=",level rate 20-40",
    )
    @has_permissions(administrator=True)
    async def level_rate(self, ctx: commands.Context, minimum: int, maximum: Optional[int] = None) -> None:
        maximum = maximum or minimum
        if min(minimum, maximum) < 1:
            return await ctx.warn("XP must be at least **1**.")
        await self.save_level_config(ctx.guild.id, xp_min=min(minimum, maximum), xp_max=max(minimum, maximum))
        await ctx.approve(f"XP per message now ranges from **{minimum}** to **{maximum}**.")

    @level.command(
        name="cooldown",
        description="Set the leveling cooldown (in seconds).",
        example=",level cooldown 30",
    )
    @has_permissions(administrator=True)
    async def level_cooldown(self, ctx: commands.Context, seconds: int) -> None:
        if seconds < 0:
            return await ctx.warn("Cooldown cannot be negative.")
        await self.save_level_config(ctx.guild.id, cooldown_seconds=seconds)
        await ctx.approve(f"Cooldown set to **{seconds}s**.")

    @level.command(
        name="reward",
        aliases=["rewards"],
        description="Award a role at a given level.",
        example=",level reward 5 @Veteran",
    )
    @has_permissions(administrator=True)
    async def level_reward_cmd(self, ctx: commands.Context, level_value: int, role: discord.Role) -> None:
        if level_value < 1:
            return await ctx.warn("Level must be at least **1**.")
        if role >= ctx.guild.me.top_role:
            return await ctx.deny("I cannot manage that role because it is higher than my highest role.")
        await self.set_level_reward(ctx.guild.id, level_value, role.id)
        await ctx.approve(f"{role.mention} will be awarded at **level {level_value}**.")

    @level.command(
        name="unreward",
        aliases=["delreward", "rmreward"],
        description="Remove a level reward for a given level.",
        example=",level unreward 5",
    )
    @has_permissions(administrator=True)
    async def level_unreward_cmd(self, ctx: commands.Context, level_value: int) -> None:
        n = await self.clear_level_reward(ctx.guild.id, level_value)
        if not n:
            return await ctx.warn(f"No reward is set for level **{level_value}**.")
        await ctx.approve(f"Reward for level **{level_value}** has been removed.")

    # ------------------------------------------------------------------
    # Starboard command group
    # ------------------------------------------------------------------

    @hybrid_group(
        name="starboard",
        aliases=["sb", "stars"],
        description="Manage server starboards.",
        example=",starboard config",
    )
    @commands.guild_only()
    async def starboard(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await self._starboard_default(ctx)

    async def _starboard_default(self, ctx: commands.Context) -> None:
        """Called when `,starboard` is invoked with no subcommand."""
        await self.starboard_config_cmd.callback(self, ctx)  # type: ignore[attr-defined]

    @starboard.command(
        name="config",
        aliases=["list", "show"],
        description="Show every configured starboard in this server.",
        example=",starboard config",
    )
    @has_permissions(administrator=True)
    async def starboard_config_cmd(self, ctx: commands.Context) -> None:
        rows = await self.list_starboards(ctx.guild.id)
        if not rows:
            return await ctx.warn("This server has no starboards configured yet.")

        embed = Embed(
            title=f"Starboards in {ctx.guild.name}",
            color=COLORS.neutral,
        )
        for row in rows:
            ch_label = f"<#{row['channel_id']}>"
            status = "🔒 locked" if row["locked"] else "🟢 active"
            value = (
                f"Channel: {ch_label}\n"
                f"Emoji: {row['emoji']}\n"
                f"Threshold: **{row['threshold']}**\n"
                f"Self-reactions: {'on' if row['self_react'] else 'off'}\n"
                f"Status: {status}"
            )
            embed.add_field(name=f"⭐ {row['name']}", value=value, inline=False)
        await ctx.send(embed=embed)

    @starboard.command(
        name="add",
        description="Add a new starboard (max 3 per server).",
        example=",starboard add #starboard ⭐ 3 stars",
    )
    @has_permissions(administrator=True)
    @commands.bot_has_permissions(manage_messages=True, send_messages=True)
    async def starboard_add(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        emoji: str,
        threshold: int,
        *,
        name: str,
    ) -> None:
        try:
            await self.add_starboard(
                ctx.guild.id,
                name=name,
                channel_id=channel.id,
                emoji=emoji,
                threshold=threshold,
            )
        except ValueError as err:
            return await ctx.warn(str(err))
        await ctx.approve(f"Starboard **{name}** has been created in {channel.mention}.")

    @starboard.command(
        name="remove",
        aliases=["delete", "rm"],
        description="Remove a starboard by name.",
        example=",starboard remove stars",
    )
    @has_permissions(administrator=True)
    async def starboard_remove(self, ctx: commands.Context, *, name: str) -> None:
        n = await self.delete_starboard(ctx.guild.id, name)
        if not n:
            return await ctx.warn(f"No starboard named **{name}** exists.")
        await ctx.approve(f"Starboard **{name}** has been removed.")

    @starboard.command(
        name="edit",
        description="Interactively edit an existing starboard (channel/emoji/threshold/color).",
        example=",starboard edit stars",
    )
    @has_permissions(administrator=True)
    async def starboard_edit(self, ctx: commands.Context, *, name: str) -> None:
        current = await self.get_starboard(ctx.guild.id, name)
        if not current:
            return await ctx.warn(f"No starboard named **{name}** exists.")

        if not ctx.interaction:
            return await ctx.warn("Editing a starboard requires using the slash command form.")

        await ctx.interaction.response.send_modal(
            StarboardEditModal(self, ctx.guild.id, name, current)
        )

    @starboard.command(
        name="self",
        description="Toggle whether self-reactions count toward a starboard.",
        example=",starboard self on stars",
    )
    @has_permissions(administrator=True)
    async def starboard_self(self, ctx: commands.Context, allow: bool, *, name: str) -> None:
        try:
            await self.update_starboard(ctx.guild.id, name=name, self_react=allow)
        except ValueError as err:
            return await ctx.warn(str(err))
        await ctx.approve(
            f"Self-reactions in **{name}** are now **{'counted' if allow else 'ignored'}**."
        )

    @starboard.command(
        name="lock",
        description="Lock a starboard, preventing new posts.",
        example=",starboard lock stars",
    )
    @has_permissions(administrator=True)
    async def starboard_lock(self, ctx: commands.Context, *, name: str) -> None:
        try:
            await self.update_starboard(ctx.guild.id, name=name, locked=True)
        except ValueError as err:
            return await ctx.warn(str(err))
        await ctx.approve(f"Starboard **{name}** has been **locked**.")

    @starboard.command(
        name="unlock",
        description="Unlock a previously locked starboard.",
        example=",starboard unlock stars",
    )
    @has_permissions(administrator=True)
    async def starboard_unlock(self, ctx: commands.Context, *, name: str) -> None:
        try:
            await self.update_starboard(ctx.guild.id, name=name, locked=False)
        except ValueError as err:
            return await ctx.warn(str(err))
        await ctx.approve(f"Starboard **{name}** has been **unlocked**.")

    @starboard.command(
        name="ignore",
        description="Ignore a channel, role or user from a starboard.",
        example=",starboard ignore channel #no-star stars",
    )
    @has_permissions(administrator=True)
    async def starboard_ignore(
        self,
        ctx: commands.Context,
        kind: str,
        channel: Optional[discord.TextChannel] = None,
        role: Optional[discord.Role] = None,
        user: Optional[discord.Member] = None,
        *,
        name: Optional[str] = None,
    ) -> None:
        kind_normalised = kind.lower().strip()
        if kind_normalised not in ("channel", "role", "user"):
            return await ctx.warn("Provide `channel`, `role` or `user` as the target kind.")

        if name is None:
            starboards = await self.list_starboards(ctx.guild.id)
            if not starboards:
                return await ctx.warn("No starboards are configured.")
            name = starboards[0]["name"]

        sb = await self.get_starboard(ctx.guild.id, name)
        if not sb:
            return await ctx.warn(f"Starboard **{name}** does not exist.")

        target_id: Optional[int] = None
        label = ""
        if kind_normalised == "channel":
            if channel is None:
                return await ctx.warn("Provide a **channel** argument.")
            target_id, label = channel.id, channel.mention
        elif kind_normalised == "role":
            if role is None:
                return await ctx.warn("Provide a **role** argument.")
            target_id, label = role.id, role.mention
        elif kind_normalised == "user":
            if user is None:
                return await ctx.warn("Provide a **user** argument.")
            target_id, label = user.id, user.mention
        if target_id is None:
            return

        await self.toggle_starboard_ignore(
            ctx.guild.id, name, kind_normalised, target_id, add=True
        )
        await ctx.approve(f"{label} is now **ignored** by **{name}**.")

        await self.toggle_starboard_ignore(ctx.guild.id, name, kind, add=True)
        await ctx.approve(f"{label} is now **ignored** by **{name}**.")

    @starboard.command(
        name="unignore",
        description="Un-ignore a channel, role or user from a starboard.",
        example=",starboard unignore #no-star stars",
    )
    @has_permissions(administrator=True)
    async def starboard_unignore(
        self,
        ctx: commands.Context,
        role: Optional[discord.Role] = None,
        channel: Optional[discord.TextChannel] = None,
        user: Optional[discord.Member] = None,
        *,
        name: Optional[str] = None,
    ) -> None:
        if name is None:
            starboards = await self.list_starboards(ctx.guild.id)
            if not starboards:
                return await ctx.warn("No starboards are configured.")
            name = starboards[0]["name"]

        sb = await self.get_starboard(ctx.guild.id, name)
        if not sb:
            return await ctx.warn(f"Starboard **{name}** does not exist.")

        targets = []
        if channel is not None:
            targets.append(("channel", channel.id, channel.mention))
        if role is not None:
            targets.append(("role", role.id, role.mention))
        if user is not None:
            targets.append(("user", user.id, user.mention))

        if not targets:
            return await ctx.warn("Provide a **channel**, **role** or **user** to unignore.")

        for kind, tid, label in targets:
            await self.toggle_starboard_ignore(ctx.guild.id, name, kind, tid, add=False)
            await ctx.send(
                embed=Embed(
                    description=f"{EMOJIS.APPROVE} {label} is no longer ignored by **{name}**.",
                    color=COLORS.approve,
                )
            )

    @starboard.command(
        name="ignorelist",
        description="Show every channel, role and user ignored by a starboard.",
        example=",starboard ignorelist stars",
    )
    @has_permissions(administrator=True)
    async def starboard_ignorelist(self, ctx: commands.Context, *, name: str) -> None:
        sb = await self.get_starboard(ctx.guild.id, name)
        if not sb:
            return await ctx.warn(f"Starboard **{name}** does not exist.")

        ignores = await self.list_starboard_ignores(ctx.guild.id, name)
        if not ignores:
            return await ctx.warn(f"Starboard **{name}** has no ignore entries.")

        buckets: dict[str, list[int]] = {"channel": [], "role": [], "user": []}
        for kind, tid in ignores:
            buckets.setdefault(kind, []).append(tid)

        lines = []
        if buckets["channel"]:
            lines.append("**Channels:** " + ", ".join(f"<#{cid}>" for cid in buckets["channel"]))
        if buckets["role"]:
            lines.append("**Roles:** " + ", ".join(f"<@&{rid}>" for rid in buckets["role"]))
        if buckets["user"]:
            lines.append("**Users:** " + ", ".join(f"<@{uid}>" for uid in buckets["user"]))

        await ctx.send(
            embed=Embed(
                title=f"Ignored entries — {name}",
                description="\n".join(lines)[:4096],
                color=COLORS.neutral,
            )
        )

    # ------------------------------------------------------------------
    # Giveaway command group
    # ------------------------------------------------------------------

    @hybrid_group(
        name="giveaways",
        aliases=["gw", "gws"],
        description="Manage giveaways.",
        example=",giveaways start",
    )
    @commands.guild_only()
    async def giveaways(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @giveaways.command(
        name="start",
        description="Start a new giveaway interactively (buttons collect the inputs).",
        example=",giveaways start",
    )
    @has_permissions(manage_guild=True)
    async def giveaways_start(self, ctx: commands.Context) -> None:
        if not ctx.interaction:
            return await ctx.warn("The giveaway wizard can only be used as a **slash command**.")
        await ctx.interaction.response.send_message(
            embed=Embed(
                title="🎉 Start a giveaway",
                description=(
                    "Pick the channel where the giveaway should be posted.\n"
                    "After picking the channel, a small form will ask for the "
                    "**prize**, **winners**, and **duration**."
                ),
                color=COLORS.neutral,
            ),
            view=GiveawayStartView(self, ctx),
        )

    @giveaways.command(
        name="list",
        aliases=["ls"],
        description="List every giveaway in this guild (active, then historical).",
        example=",giveaways list",
    )
    @has_permissions(manage_guild=True)
    async def giveaways_list(self, ctx: commands.Context) -> None:
        all_rows = await self.list_giveaways(ctx.guild.id)
        if not all_rows:
            return await ctx.warn("This server has no giveaways yet.")

        chunk_size = 8
        pages: list[Embed] = []
        total = len(all_rows)
        pages_total = max(1, math.ceil(total / chunk_size))
        for page_index in range(pages_total):
            start = page_index * chunk_size
            chunk = all_rows[start:start + chunk_size]
            lines: list[str] = []
            for row in chunk:
                status = "ended" if row["ended"] else "active"
                try:
                    entries = len(json.loads(row["entries"] or "[]"))
                except Exception:
                    entries = 0
                ends_at = row["ends_at"] or "?"
                host_id = row.get("host_id")
                host = f"<@{host_id}>" if host_id else "unknown"
                lines.append(
                    f"`{row['message_id']}` • **{row['prize']}** — {status} • "
                    f"{entries} entries • winners **{row['winners']}** • "
                    f"host {host} • ends `{ends_at}`"
                )
            embed = Embed(
                title=f"Giveaways in {ctx.guild.name}",
                description="\n".join(lines) or "No giveaways.",
                color=COLORS.neutral,
            )
            embed.set_footer(text=f"Page {page_index + 1}/{pages_total} • {total} giveaways")
            pages.append(embed)
        await ctx.paginate(pages)

    @giveaways.command(
        name="cancel",
        description="Cancel an active giveaway (no winners are picked).",
        example=",giveaways cancel 1234567890",
    )
    @has_permissions(manage_guild=True)
    async def giveaways_cancel(
        self,
        ctx: commands.Context,
        message_id_or_link: Optional[str] = None,
    ) -> None:
        msg_id = parse_message_id_or_link(message_id_or_link, ctx)
        if msg_id is None:
            return await ctx.warn("Provide a **message ID or link** (or reply to the giveaway).")
        try:
            msg, _winners = await self.end_giveaway(ctx.guild.id, msg_id, cancel=True)
        except ValueError as err:
            return await ctx.warn(str(err))
        if msg is not None:
            await ctx.approve(f"Giveaway cancelled — see the [updated message]({msg.jump_url}).")
        else:
            await ctx.approve("Giveaway has been cancelled.")

    async def _run_giveaway_end(
        self, ctx: commands.Context, message_id_or_link: Optional[str]
    ) -> None:
        """Shared body for ending a giveaway early and picking winners."""
        msg_id = parse_message_id_or_link(message_id_or_link, ctx)
        if msg_id is None:
            return await ctx.warn("Provide a **message ID or link** (or reply to the giveaway).")
        try:
            msg, winners = await self.end_giveaway(ctx.guild.id, msg_id)
        except ValueError as err:
            return await ctx.warn(str(err))
        if not winners:
            await ctx.approve(
                f"Giveaway ended but there were no valid entries. See [the message]({msg.jump_url if msg else '?'})."
            )
        else:
            mentions = ", ".join(f"<@{w}>" for w in winners)
            await ctx.approve(
                f"Giveaway ended. Winner{'s' if len(winners) != 1 else ''}: {mentions}"
            )

    @giveaways.command(
        name="end",
        description="End a giveaway early and pick winners.",
        example=",giveaways end 1234567890",
    )
    @has_permissions(manage_guild=True)
    async def giveaways_end(
        self,
        ctx: commands.Context,
        message_id_or_link: Optional[str] = None,
    ) -> None:
        await self._run_giveaway_end(ctx, message_id_or_link)

    @giveaways.command(
        name="create",
        description="Create a giveaway directly: prize, winners, duration.",
        example=',giveaways create "Nitro" 1 1h',
    )
    @has_permissions(manage_guild=True)
    async def giveaways_create(
        self,
        ctx: commands.Context,
        prize: str,
        winners: int = 1,
        duration: str = "1h",
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if winners < 1 or winners > 50:
            return await ctx.warn("Winners must be between **1** and **50**.")
        seconds = parse_duration_seconds(duration)
        if seconds is None or seconds < 30:
            return await ctx.warn(
                "Duration must be a valid value of at least **30 seconds** (e.g. `1h`, `30m`)."
            )
        if seconds > 60 * 60 * 24 * 30:
            return await ctx.warn("Giveaway duration cannot exceed **30 days**.")
        target = channel or ctx.channel
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            return await ctx.warn("Giveaways can only run in a text channel or thread.")
        try:
            record, message = await self.create_giveaway(
                ctx.guild,
                ctx.author,
                prize=prize.strip(),
                winners=winners,
                duration_seconds=seconds,
                channel_id=target.id,
            )
        except ValueError as err:
            return await ctx.warn(str(err))
        except discord.HTTPException as err:
            return await ctx.deny(f"Failed to post the giveaway: `{err}`")
        await ctx.approve(
            f"Giveaway created in <#{record['channel_id']}> — [jump to message]({message.jump_url})."
        )

    # Legacy singular alias for `giveaways end` (kept for muscle memory).
    @hybrid_command(
        name="giveaway",
        aliases=["gw_end"],
        description="End a giveaway early and pick winners (alias of giveaways end).",
        example=",giveaway end 1234567890",
    )
    @commands.guild_only()
    @has_permissions(manage_guild=True)
    async def giveaway_end_command(
        self,
        ctx: commands.Context,
        action: Optional[str] = None,
        message_id_or_link: Optional[str] = None,
    ) -> None:
        # `,giveaway end <id>` -> message_id_or_link; `,giveaway <id>` ->
        # action holds the id. Bare `,giveaway` shows the group help like
        # every other command group does.
        target = message_id_or_link
        if target is None and action is not None and action.strip().lower() not in ("end",):
            target = action.strip() or None
        if target is None:
            group = getattr(self, "giveaways", None) or ctx.bot.get_command("giveaways")
            if group is not None:
                return await hollowHelp.send_group_help(ctx, group)
        await self._run_giveaway_end(ctx, target)

    @giveaways.command(
        name="reroll",
        description="Reroll the winners of an ended giveaway.",
        example=",giveaways reroll 1234567890",
    )
    @has_permissions(manage_guild=True)
    async def giveaways_reroll(
        self,
        ctx: commands.Context,
        message_id_or_link: Optional[str] = None,
    ) -> None:
        msg_id = parse_message_id_or_link(message_id_or_link, ctx)
        if msg_id is None:
            return await ctx.warn("Provide a **message ID or link** (or reply to the giveaway).")
        try:
            new_winners = await self.reroll_giveaway(ctx.guild.id, msg_id)
        except ValueError as err:
            return await ctx.warn(str(err))
        if not new_winners:
            return await ctx.warn("There were no remaining entries to reroll from.")
        mentions = ", ".join(f"<@{w}>" for w in new_winners)
        await ctx.approve(f"New winner{'s' if len(new_winners) != 1 else ''}: {mentions}")

    @giveaways.command(
        name="entires",
        aliases=["entries", "entrants"],
        description="View the entries of a giveaway (note: keeps the typo from the spec).",
        example=",giveaways entries 1234567890",
    )
    @has_permissions(manage_guild=True)
    async def giveaways_entries(
        self,
        ctx: commands.Context,
        message_id_or_link: Optional[str] = None,
    ) -> None:
        msg_id = parse_message_id_or_link(message_id_or_link, ctx)
        if msg_id is None:
            return await ctx.warn("Provide a **message ID or link** (or reply to the giveaway).")
        record = await self.get_giveaway(msg_id)
        if not record or record["guild_id"] != ctx.guild.id:
            return await ctx.warn("I could not find that giveaway.")
        try:
            entries = json.loads(record["entries"] or "[]")
        except Exception:
            entries = []
        if not entries:
            return await ctx.warn("That giveaway has no entries yet.")

        per_page = 12
        pages_total = max(1, math.ceil(len(entries) / per_page))
        pages: list[Embed] = []
        for index in range(pages_total):
            chunk = entries[index * per_page:(index + 1) * per_page]
            lines = []
            for user_id in chunk:
                member = ctx.guild.get_member(user_id)
                render = member.mention if member else f"<@{user_id}>"
                lines.append(f"• {render}")
            embed = Embed(
                title=f"Entries — {record['prize']}",
                description="\n".join(lines) or "(empty)",
                color=COLORS.neutral,
            )
            embed.set_footer(text=f"Page {index + 1}/{pages_total} • {len(entries)} total entries")
            pages.append(embed)
        await ctx.paginate(pages)

    @giveaways.command(
        name="channel",
        description="Set the default channel where giveaways will be posted.",
        example=",giveaways channel #giveaways",
    )
    @has_permissions(manage_guild=True)
    async def giveaways_channel(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if channel is None:
            cfg = await self.get_giveaway_config(ctx.guild.id)
            label = f"<#{cfg['default_channel_id']}>" if cfg["default_channel_id"] else "none"
            return await ctx.approve(f"Current default giveaway channel: {label}.")
        await self.save_giveaway_config(
            ctx.guild.id, default_channel_id=channel.id
        )
        await ctx.approve(f"Future giveaways will default to {channel.mention}.")

    @giveaways.command(
        name="extraentries",
        aliases=["extra_entries"],
        description="Show every role that grants extra giveaway entries.",
        example=",giveaways extraentries",
    )
    @has_permissions(manage_guild=True)
    async def giveaways_extraentries(self, ctx: commands.Context) -> None:
        rows = await self.list_extra_entries(ctx.guild.id)
        if not rows:
            return await ctx.warn("No extra-entry roles are configured for this server.")
        lines = [
            f"• <@&{role_id}> ⟶ **{count}** entries"
            for role_id, count in rows
        ]
        embed = Embed(
            title="Extra-entry roles",
            description="\n".join(lines)[:2048],
            color=COLORS.neutral,
        )
        await ctx.send(embed=embed)

    @giveaways.command(
        name="clear",
        description="Clear ended giveaways from the database.",
        example=",giveaways clear",
    )
    @has_permissions(manage_guild=True)
    async def giveaways_clear(self, ctx: commands.Context) -> None:
        cursor = await self.bot.db.execute(
            "DELETE FROM giveaways WHERE guild_id = ? AND ended = 1",
            (ctx.guild.id,),
        )
        await self.bot.db.commit()
        removed = cursor.rowcount or 0
        if not removed:
            return await ctx.warn("There were no ended giveaways to clear.")
        await ctx.approve(f"Cleared **{removed}** ended giveaway{'s' if removed != 1 else ''} from history.")

    # ------------------------------------------------------------------
    # Giveaway edit subgroup
    # ------------------------------------------------------------------

    @giveaways.group(
        name="edit",
        description="Edit an active giveaway's configuration.",
        example=",giveaways edit prize 1234 Nitro",
        invoke_without_command=True,
    )
    @has_permissions(manage_guild=True)
    async def giveaways_edit(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    async def _resolve_giveaway_id(
        self, ctx: commands.Context, arg: Optional[str]
    ) -> Optional[int]:
        return parse_message_id_or_link(arg, ctx)

    @giveaways_edit.command(
        name="prize",
        description="Change the prize of an active giveaway.",
        example=",giveaways edit prize 1234567890 'Nitro Classic'",
    )
    @app_commands.describe(message_id="Giveaway message ID or link", prize="New prize")
    async def giveaways_edit_prize(
        self,
        ctx: commands.Context,
        message_id: str,
        *,
        prize: str,
    ) -> None:
        msg_id = await self._resolve_giveaway_id(ctx, message_id)
        if msg_id is None:
            return await ctx.warn("Provide a valid message ID or link.")
        try:
            await self.update_giveaway_field(ctx.guild.id, msg_id, "prize", prize)
        except ValueError as err:
            return await ctx.warn(str(err))
        await ctx.approve(f"Prize has been updated to **{prize}**.")

    @giveaways_edit.command(
        name="winners",
        description="Change how many winners an active giveaway will pick.",
        example=",giveaways edit winners 1234567890 3",
    )
    @app_commands.describe(message_id="Giveaway message ID or link", winners="New winner count")
    async def giveaways_edit_winners(
        self,
        ctx: commands.Context,
        message_id: str,
        winners: int,
    ) -> None:
        msg_id = await self._resolve_giveaway_id(ctx, message_id)
        if msg_id is None:
            return await ctx.warn("Provide a valid message ID or link.")
        if winners < 1:
            return await ctx.warn("There must be at least one winner.")
        try:
            await self.update_giveaway_field(ctx.guild.id, msg_id, "winners", winners)
        except ValueError as err:
            return await ctx.warn(str(err))
        await ctx.approve(f"Winner count has been updated to **{winners}**.")

    @giveaways_edit.command(
        name="duration",
        description="Change how long an active giveaway lasts.",
        example=",giveaways edit duration 1234567890 2h",
    )
    @app_commands.describe(
        message_id="Giveaway message ID or link",
        duration="New duration (e.g. 1h, 30m, 2d)",
    )
    async def giveaways_edit_duration(
        self,
        ctx: commands.Context,
        message_id: str,
        duration: str,
    ) -> None:
        msg_id = await self._resolve_giveaway_id(ctx, message_id)
        if msg_id is None:
            return await ctx.warn("Provide a valid message ID or link.")
        try:
            await self.update_giveaway_field(ctx.guild.id, msg_id, "duration", duration)
        except ValueError as err:
            return await ctx.warn(str(err))
        await ctx.approve(f"Giveaway now runs for **{duration}**.")

    @giveaways_edit.command(
        name="stay",
        description="Set the minimum server stay required to enter a giveaway (days).",
        example=",giveaways edit stay 1234567890 7",
    )
    @app_commands.describe(
        message_id="Giveaway message ID or link",
        days="Days the member must have been on the server",
    )
    async def giveaways_edit_stay(
        self,
        ctx: commands.Context,
        message_id: str,
        days: int,
    ) -> None:
        msg_id = await self._resolve_giveaway_id(ctx, message_id)
        if msg_id is None:
            return await ctx.warn("Provide a valid message ID or link.")
        if days < 0:
            return await ctx.warn("Stay requirement cannot be negative.")
        try:
            await self.update_giveaway_field(ctx.guild.id, msg_id, "stay", days)
        except ValueError as err:
            return await ctx.warn(str(err))
        await ctx.approve(f"Minimum server stay set to **{days}** day(s).")

    @giveaways_edit.command(
        name="age",
        description="Set the minimum account age required to enter a giveaway (days).",
        example=",giveaways edit age 1234567890 30",
    )
    async def giveaways_edit_age(
        self,
        ctx: commands.Context,
        message_id: str,
        days: int,
    ) -> None:
        msg_id = await self._resolve_giveaway_id(ctx, message_id)
        if msg_id is None:
            return await ctx.warn("Provide a valid message ID or link.")
        if days < 0:
            return await ctx.warn("Age requirement cannot be negative.")
        try:
            await self.update_giveaway_field(ctx.guild.id, msg_id, "age", days)
        except ValueError as err:
            return await ctx.warn(str(err))
        await ctx.approve(f"Minimum account age set to **{days}** day(s).")

    @giveaways_edit.command(
        name="host",
        description="Transfer the host of an active giveaway to another member.",
        example=",giveaways edit host 1234567890 @Bob",
    )
    @app_commands.describe(message_id="Giveaway message ID or link", member="New host")
    async def giveaways_edit_host(
        self,
        ctx: commands.Context,
        message_id: str,
        member: discord.Member,
    ) -> None:
        msg_id = await self._resolve_giveaway_id(ctx, message_id)
        if msg_id is None:
            return await ctx.warn("Provide a valid message ID or link.")
        try:
            await self.update_giveaway_field(ctx.guild.id, msg_id, "host", member.id)
            await self.update_giveaway_field(
                ctx.guild.id, msg_id, "host_name", member.display_name
            )
        except ValueError as err:
            return await ctx.warn(str(err))
        await ctx.approve(f"Giveaway host has been transferred to {member.mention}.")

    @giveaways_edit.command(
        name="extraentries",
        aliases=["extra_entries"],
        description="Set the number of extra entries a role grants.",
        example=",giveaways edit extraentries @VIP 3",
    )
    @app_commands.describe(role="Role that gets the bonus", entries="Entry count (1-25)")
    @has_permissions(administrator=True)
    async def giveaways_edit_extraentries(
        self,
        ctx: commands.Context,
        role: discord.Role,
        entries: int,
    ) -> None:
        if entries < 1:
            return await ctx.warn("Entry count must be at least 1.")
        await self.set_extra_entry(ctx.guild.id, role.id, entries)
        await ctx.approve(f"{role.mention} now grants **{entries}** entries per giveaway.")

    @giveaways_edit.command(
        name="requiredroles",
        aliases=["required_roles", "roles"],
        description="Replace the list of roles required to enter a giveaway.",
        example=",giveaways edit requiredroles @Members @Boosters",
    )
    @has_permissions(administrator=True)
    async def giveaways_edit_requiredroles(
        self,
        ctx: commands.Context,
        roles: commands.Greedy[discord.Role],
    ) -> None:
        # Wipe and add
        await self.bot.db.execute(
            "DELETE FROM giveaway_required_roles WHERE guild_id = ?",
            (ctx.guild.id,),
        )
        for role in roles:
            await self.add_required_role(ctx.guild.id, role.id)
        await self.bot.db.commit()
        if not roles:
            return await ctx.approve("Cleared every required-role entry from giveaways.")
        await ctx.approve(
            f"Members must now hold one of: "
            f"{', '.join(r.mention for r in roles)} to enter giveaways."
        )

    @giveaways_edit.command(
        name="messages",
        description="Update the custom messages shown above/below the giveaway.",
        example=",giveaways edit messages 1234567890 'Win a prize!'",
    )
    @app_commands.describe(message_id="Giveaway message ID or link", text="Custom text")
    async def giveaways_edit_messages(
        self,
        ctx: commands.Context,
        message_id: str,
        *,
        text: str,
    ) -> None:
        msg_id = await self._resolve_giveaway_id(ctx, message_id)
        if msg_id is None:
            return await ctx.warn("Provide a valid message ID or link.")
        try:
            await self.update_giveaway_field(ctx.guild.id, msg_id, "messages", text)
        except ValueError as err:
            return await ctx.warn(str(err))
        await ctx.approve("The custom message has been saved for that giveaway.")

    @giveaways_edit.command(
        name="thumbnail",
        description="Set the small image shown on an active giveaway embed.",
        example=",giveaways edit thumbnail 1234567890 https://example.com/thumb.png",
    )
    @app_commands.describe(message_id="Giveaway message ID or link", url="Image URL")
    async def giveaways_edit_thumbnail(
        self,
        ctx: commands.Context,
        message_id: str,
        url: Optional[str] = None,
    ) -> None:
        msg_id = await self._resolve_giveaway_id(ctx, message_id)
        if msg_id is None:
            return await ctx.warn("Provide a valid message ID or link.")
        if url and not (url.startswith("http://") or url.startswith("https://")):
            return await ctx.warn("That doesn't look like a valid URL.")
        try:
            await self.update_giveaway_field(ctx.guild.id, msg_id, "thumbnail", url or "")
        except ValueError as err:
            return await ctx.warn(str(err))
        await ctx.approve(f"Giveaway thumbnail has been set to {url or '[cleared]'}.")

    @giveaways_edit.command(
        name="image",
        description="Set the larger image shown on an active giveaway embed.",
        example=",giveaways edit image 1234567890 https://example.com/image.png",
    )
    @app_commands.describe(message_id="Giveaway message ID or link", url="Image URL")
    async def giveaways_edit_image(
        self,
        ctx: commands.Context,
        message_id: str,
        url: Optional[str] = None,
    ) -> None:
        msg_id = await self._resolve_giveaway_id(ctx, message_id)
        if msg_id is None:
            return await ctx.warn("Provide a valid message ID or link.")
        if url and not (url.startswith("http://") or url.startswith("https://")):
            return await ctx.warn("That doesn't look like a valid URL.")
        try:
            await self.update_giveaway_field(ctx.guild.id, msg_id, "image", url or "")
        except ValueError as err:
            return await ctx.warn(str(err))
        await ctx.approve(f"Giveaway banner image has been set to {url or '[cleared]'}.")

    @giveaways_edit.command(
        name="colour",
        aliases=["color"],
        description="Change the embed colour of an active giveaway.",
        example=",giveaways edit colour 1234567890 #FF8800",
    )
    @app_commands.describe(message_id="Giveaway message ID or link", colour="Hex colour")
    async def giveaways_edit_colour(
        self,
        ctx: commands.Context,
        message_id: str,
        colour: str,
    ) -> None:
        msg_id = await self._resolve_giveaway_id(ctx, message_id)
        if msg_id is None:
            return await ctx.warn("Provide a valid message ID or link.")
        try:
            await self.update_giveaway_field(ctx.guild.id, msg_id, "colour", colour)
        except ValueError as err:
            return await ctx.warn(str(err))
        await ctx.approve(f"Giveaway embed colour set to `#{colour.replace('#', '')}`.")

    # ------------------------------------------------------------------
    # Giveaway blacklist subgroup
    # ------------------------------------------------------------------

    @giveaways.group(
        name="blacklist",
        aliases=["block"],
        description="Manage the giveaway entry block-list.",
        example=",giveaways blacklist add @Spammer",
        invoke_without_command=True,
    )
    @has_permissions(manage_guild=True)
    async def giveaways_blacklist_grp(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @giveaways_blacklist_grp.command(
        name="add",
        description="Block a member from entering giveaways in this server.",
        example=",giveaways blacklist add @Spammer",
    )
    @app_commands.describe(user="User to block", reason="Optional reason for the mod-log")
    async def giveaways_blacklist_add(
        self,
        ctx: commands.Context,
        user: discord.Member,
        *,
        reason: Optional[str] = None,
    ) -> None:
        if await self.is_giveaway_blacklisted(ctx.guild.id, user.id):
            return await ctx.warn(f"{user.mention} is already blacklisted.")
        ok = await self.blacklist_giveaway_user(
            ctx.guild.id,
            user.id,
            moderator_id=ctx.author.id,
            reason=reason,
        )
        if not ok:
            return await ctx.deny("Failed to blacklist that user.")
        await ctx.approve(f"{user.mention} can no longer enter giveaways here.")

    @giveaways_blacklist_grp.command(
        name="remove",
        aliases=["unblock"],
        description="Remove a user from the giveaway blacklist.",
        example=",giveaways blacklist remove @Spammer",
    )
    async def giveaways_blacklist_remove(
        self,
        ctx: commands.Context,
        user: discord.Member,
    ) -> None:
        removed = await self.unblacklist_giveaway_user(ctx.guild.id, user.id)
        if not removed:
            return await ctx.warn(f"{user.mention} is not blacklisted.")
        await ctx.approve(f"{user.mention} can enter giveaways again.")

    @giveaways_blacklist_grp.command(
        name="list",
        description="Show every user currently blacklisted from giveaways.",
        example=",giveaways blacklist list",
    )
    async def giveaways_blacklist_list(self, ctx: commands.Context) -> None:
        rows = await self.list_giveaway_blacklist(ctx.guild.id)
        if not rows:
            return await ctx.warn("No users are currently blacklisted.")
        per_page = 10
        pages_total = max(1, math.ceil(len(rows) / per_page))
        pages: list[Embed] = []
        for index in range(pages_total):
            chunk = rows[index * per_page:(index + 1) * per_page]
            lines: list[str] = []
            for row in chunk:
                moderator = (
                    f"<@{row['moderator_id']}>"
                    if row.get("moderator_id")
                    else "unknown"
                )
                reason = row.get("reason") or "No reason provided"
                created = _fmt_relative(
                    datetime.fromisoformat(row["created_at"]).replace(tzinfo=timezone.utc)
                    if isinstance(row.get("created_at"), str)
                    else row.get("created_at")
                )
                lines.append(
                    f"• <@{row['user_id']}> — by {moderator} — {reason} (added {created})"
                )
            embed = Embed(
                title="Giveaway blacklist",
                description="\n".join(lines),
                color=COLORS.neutral,
            )
            embed.set_footer(text=f"Page {index + 1}/{pages_total} • {len(rows)} total")
            pages.append(embed)
        await ctx.paginate(pages)

    # ------------------------------------------------------------------
    # AFK command group
    # ------------------------------------------------------------------

    @hybrid_command(
        name="afk",
        aliases=["awayfromkeyboard"],
        description="Mark yourself as AFK (use without args to manage presets, etc.).",
        example=",afk sleeping",
    )
    @commands.guild_only()
    async def afk(
        self,
        ctx: commands.Context,
        *,
        reason: Optional[str] = None,
    ) -> None:
        reason_text = (reason or "").strip() or "AFK"
        # If subcommand-like argument was given, defer to the group
        lowered = reason_text.lower().split()
        if lowered and lowered[0] in {
            "preset", "ignore", "timeout", "list", "logchannel", "clear"
        } and len(lowered) >= 1:
            # The user accidentally prefixed `,afk preset list` without sub-group
            sub = ctx.bot.get_command("afk")
            if isinstance(sub, commands.HybridGroup):
                # Rebuild a fake context to invoke the subgroup
                pass

        await self.set_afk(ctx.author.id, reason_text)
        # Reply with confirmation in the same channel
        await ctx.send(
            embed=Embed(
                description=f"💤 {ctx.author.mention} is now **AFK**: {reason_text}",
                color=COLORS.neutral,
            )
        )
        # Best-effort log
        cfg = await self.get_afk_config(ctx.guild.id)
        log_ch = ctx.guild.get_channel(cfg["log_channel_id"]) if cfg.get("log_channel_id") else None
        if isinstance(log_ch, (discord.TextChannel, discord.Thread)):
            try:
                await log_ch.send(
                    embed=Embed(
                        description=(
                            f"💤 {ctx.author.mention} went **AFK** — "
                            f"{reason_text}"
                        ),
                        color=COLORS.neutral,
                    )
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

    @hybrid_group(
        name="afkset",
        aliases=["afksetting", "afk_admin"],
        description="AFK administration (presets, ignored channels, etc.).",
        example=",afkset list",
        invoke_without_command=True,
    )
    @commands.guild_only()
    async def afkset(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @afkset.group(
        name="preset",
        description="Manage your personal AFK presets.",
        example=",afkset preset add sleeping Sleep",
        invoke_without_command=True,
    )
    async def afk_preset(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @afk_preset.command(
        name="add",
        description="Save an AFK preset you can re-use later.",
        example=",afk preset add sleeping 'Sleeping · ping me later'",
    )
    @app_commands.describe(name="Preset name", status="AFK status saved under this name")
    async def afk_preset_add(
        self,
        ctx: commands.Context,
        name: str,
        *,
        status: str,
    ) -> None:
        if len(name) > 32:
            return await ctx.warn("Preset names must be **32** characters or fewer.")
        if len(status) > 480:
            return await ctx.warn("Status messages must be **480** characters or fewer.")
        ok = await self.add_afk_preset(ctx.author.id, name, status)
        if not ok:
            return await ctx.deny("Could not save the preset.")
        await ctx.approve(f"Preset **{name}** has been saved.")

    @afk_preset.command(
        name="delete",
        aliases=["remove", "rm"],
        description="Delete one of your AFK presets.",
        example=",afk preset delete sleeping",
    )
    async def afk_preset_delete(self, ctx: commands.Context, *, name: str) -> None:
        removed = await self.delete_afk_preset(ctx.author.id, name)
        if not removed:
            return await ctx.warn(f"No preset named **{name}** exists.")
        await ctx.approve(f"Preset **{name}** has been removed.")

    @afk_preset.command(
        name="list",
        description="Show every AFK preset you've saved.",
        example=",afk preset list",
    )
    async def afk_preset_list(self, ctx: commands.Context) -> None:
        rows = await self.list_afk_presets(ctx.author.id)
        if not rows:
            return await ctx.warn("You haven't saved any AFK presets yet.")
        embed = Embed(
            title="Your AFK presets",
            color=COLORS.neutral,
        )
        for row in rows:
            embed.add_field(
                name=row["name"],
                value=row["reason"][:1024],
                inline=False,
            )
        await ctx.send(embed=embed)

    @afk_preset.command(
        name="use",
        description="Mark yourself as AFK using a saved preset.",
        example=",afk preset use sleeping",
    )
    async def afk_preset_use(self, ctx: commands.Context, *, name: str) -> None:
        row = await self.get_afk_preset(ctx.author.id, name)
        if not row:
            return await ctx.warn(f"No preset named **{name}** exists.")
        await self.set_afk(ctx.author.id, row["reason"])
        await ctx.send(
            embed=Embed(
                description=f"💤 {ctx.author.mention} is now **AFK**: {row['reason']}",
                color=COLORS.neutral,
            )
        )

    @afkset.command(
        name="ignore",
        description="Stop AFK from auto-clearing when a member speaks in this channel.",
        example=",afk ignore #bot-commands",
    )
    @has_permissions(manage_guild=True)
    async def afk_ignore(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        await self.add_afk_ignore(ctx.guild.id, channel.id)
        await ctx.approve(f"AFK will no longer auto-clear in {channel.mention}.")

    @afkset.command(
        name="unignore",
        description="Re-enable AFK auto-clearing in a previously ignored channel.",
        example=",afk unignore #bot-commands",
    )
    @has_permissions(manage_guild=True)
    async def afk_unignore(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        removed = await self.remove_afk_ignore(ctx.guild.id, channel.id)
        if not removed:
            return await ctx.warn(f"{channel.mention} was not in the AFK ignore list.")
        await ctx.approve(f"AFK will once again auto-clear in {channel.mention}.")

    @afkset.command(
        name="timeout",
        description="Set how long before an AFK user is auto-removed (e.g. 30m, 2h, 0 to disable).",
        example=",afk timeout 30m",
    )
    @has_permissions(manage_guild=True)
    async def afk_timeout(self, ctx: commands.Context, minutes: str) -> None:
        secs = parse_duration_seconds(minutes)
        if secs is None:
            return await ctx.warn(
                "Provide a valid duration like ``30m``, ``2h`` or ``0`` to disable."
            )
        await self.save_afk_config(
            ctx.guild.id, timeout_minutes=max(0, secs // 60)
        )
        if secs == 0:
            return await ctx.approve("AFK auto-clear has been disabled.")
        await ctx.approve(
            f"AFK users will be cleared after **{secs // 60}** minute(s) of inactivity."
        )

    @afkset.command(
        name="list",
        description="Show every user that is currently AFK in this server.",
        example=",afk list",
    )
    async def afk_list(self, ctx: commands.Context) -> None:
        rows = await self.list_afk(ctx.guild.id)
        if not rows:
            return await ctx.warn("No one is currently AFK in this server.")
        # Filter to members that exist in this guild
        visible = [r for r in rows if ctx.guild.get_member(r["user_id"]) is not None]
        if not visible:
            return await ctx.warn("No one is currently AFK in this server.")
        per_page = 10
        pages_total = max(1, math.ceil(len(visible) / per_page))
        pages: list[Embed] = []
        for page_index in range(pages_total):
            chunk = visible[page_index * per_page:(page_index + 1) * per_page]
            lines: list[str] = []
            for row in chunk:
                member = ctx.guild.get_member(row["user_id"])
                since_raw = row["since"]
                since_dt: Optional[datetime] = None
                if isinstance(since_raw, datetime):
                    since_dt = since_raw
                elif isinstance(since_raw, str):
                    try:
                        since_dt = datetime.fromisoformat(since_raw).replace(tzinfo=timezone.utc)
                    except Exception:
                        since_dt = None
                mention = member.mention if member else f"<@{row['user_id']}>"
                lines.append(
                    f"• {mention} — {row['reason']} (since {_fmt_relative(since_dt)})"
                )
            embed = Embed(
                title="Currently AFK",
                description="\n".join(lines),
                color=COLORS.neutral,
            )
            embed.set_footer(text=f"Page {page_index + 1}/{pages_total} • {len(visible)} AFK")
            pages.append(embed)
        await ctx.paginate(pages)

    @afkset.command(
        name="logchannel",
        aliases=["logs"],
        description="Set where AFK notifications are posted.",
        example=",afk logchannel #afk-logs",
    )
    @has_permissions(manage_guild=True)
    async def afk_logchannel(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        await self.save_afk_config(
            ctx.guild.id, log_channel_id=channel.id if channel else None
        )
        if channel is None:
            return await ctx.approve("AFK log channel has been cleared.")
        await ctx.approve(f"AFK notifications will now post in {channel.mention}.")

    @afkset.command(
        name="clear",
        description="Clear another member's AFK status.",
        example=",afk clear @Bob",
    )
    @has_permissions(manage_guild=True)
    async def afk_clear(
        self,
        ctx: commands.Context,
        member: discord.Member,
    ) -> None:
        afk = await self.get_afk(member.id)
        if not afk:
            return await ctx.warn(f"{member.mention} is not AFK.")
        removed = await self.clear_afk(member.id)
        if not removed:
            return await ctx.warn(f"{member.mention} is not AFK.")
        await ctx.approve(f"{member.mention}'s AFK status has been cleared.")