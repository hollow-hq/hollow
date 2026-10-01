"""Jail — role-stripping alternative to timeouts for hollow.

Tables (created idempotently here *and* mirrored in core/schema.sql):

* ``jail_config``     - per-guild jail role / channel / log channel / defaults
* ``jails``           - one active row per jailed member (saved roles as JSON)
* ``jail_history``    - audit trail of jail / unjail / expiry actions
* ``jail_immune``     - users or roles excluded from jailing
* ``schema_version``  - per-cog forward-only migration marker

Listeners: ``on_member_join``, ``on_member_update``, ``on_guild_channel_create``,
``on_guild_channel_delete``, ``on_guild_role_delete``, ``on_guild_remove``.
Tasks: ``_expiry_loop`` (30s, releases expired sentences and catches up on
jails that expired while the bot was offline).
Persistent views: none (all confirmations are ephemeral button flows, rebuilt
per interaction, so nothing needs re-registration after restarts).

Env keys: none. Intents: members + guilds + voice_states. Bot permissions:
Manage Roles, Manage Channels, plus Connect for the voice kick-out.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import timedelta
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.client.commands import has_permissions, hybrid_group
from core.client.embed import Embed
from core.client.tagscript import TagScriptParser
from core.config import COLORS, EMOJIS
from core.logger import log
from core.client.locks import KeyedLockManager

from cogs.moderation.moderation import ConfirmView, can_moderate, human_duration, parse_duration

LOGGER = logging.getLogger("hollow.jail")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jail_config (
    guild_id INTEGER PRIMARY KEY,
    jail_role_id INTEGER,
    jail_channel_id INTEGER,
    log_channel_id INTEGER,
    default_duration TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    dm_message TEXT,
    release_message TEXT,
    remove_roles_mode TEXT NOT NULL DEFAULT 'all',
    auto_reapply INTEGER NOT NULL DEFAULT 1,
    allow_bot_targets INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jails (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    saved_role_ids TEXT NOT NULL DEFAULT '[]',
    reason TEXT,
    moderator_id INTEGER,
    jailed_at INTEGER NOT NULL,
    expires_at INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS jails_expiry_idx ON jails (active, expires_at);
CREATE TABLE IF NOT EXISTS jail_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER,
    action TEXT NOT NULL,
    reason TEXT,
    duration TEXT,
    roles_saved TEXT NOT NULL DEFAULT '[]',
    roles_restored TEXT NOT NULL DEFAULT '[]',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS jail_history_idx ON jail_history (guild_id, user_id, created_at);
CREATE TABLE IF NOT EXISTS jail_immune (
    guild_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    target_type TEXT NOT NULL,
    PRIMARY KEY (guild_id, target_id, target_type)
);
CREATE TABLE IF NOT EXISTS schema_version (
    cog TEXT PRIMARY KEY,
    version INTEGER NOT NULL
);
"""

SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# Pure logic (unit-tested in tests/moderation/test_jail.py)
# --------------------------------------------------------------------------- #

def strip_candidates(
    roles: list[dict[str, Any]], jail_role_id: int, bot_top_position: int
) -> list[int]:
    """Role IDs that should be removed when jailing.

    ``roles`` is a list of dicts with keys ``id``, ``managed``, ``is_default``,
    ``position`` (computed from real ``discord.Role`` objects by the caller).
    Skips the jail role itself, @everyone, managed/booster roles and any role
    at or above the bot's top role.
    """
    kept: list[int] = []
    for role in roles:
        if role["id"] == jail_role_id or role["is_default"]:
            continue
        if role.get("managed"):
            continue
        if role["position"] >= bot_top_position:
            continue
        kept.append(role["id"])
    return kept


def restore_split(
    saved_ids: list[int],
    guild_roles: dict[int, dict[str, Any]],
    bot_top_position: int,
) -> tuple[list[int], list[tuple[int, str]]]:
    """Split saved role IDs into restorable vs skipped (with a reason each).

    ``guild_roles`` maps role id -> the same dict shape as :func:`strip_candidates`.
    Roles that no longer exist, became managed, or sit at/above the bot's top
    role are reported instead of silently dropped.
    """
    restorable: list[int] = []
    skipped: list[tuple[int, str]] = []
    for role_id in saved_ids:
        role = guild_roles.get(role_id)
        if role is None:
            skipped.append((role_id, "deleted"))
        elif role.get("managed"):
            skipped.append((role_id, "managed"))
        elif role["position"] >= bot_top_position:
            skipped.append((role_id, "too-high"))
        else:
            restorable.append(role_id)
    return restorable, skipped


def saved_roles_snapshot(member: discord.Member, jail_role_id: int, bot_top_position: int) -> list[int]:
    """Build the strip list from a live member (thin wrapper over the pure fn)."""
    roles = [
        {
            "id": role.id,
            "managed": role.managed,
            "is_default": role.is_default(),
            "position": role.position,
        }
        for role in member.roles
    ]
    return strip_candidates(roles, jail_role_id, bot_top_position)


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #


class Jail(commands.Cog):
    """Role-stripping jail with saved-role restore, expiry and rejoin protection."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.locks = KeyedLockManager()
        self._syncing: set[int] = set()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def cog_load(self) -> None:
        db = self.bot.db
        await db.executescript(SCHEMA_SQL)
        await self._migrate(db)
        await db.commit()
        LOGGER.info("jail tables ready (schema v%s)", SCHEMA_VERSION)
        self._expiry_loop.start()

    async def cog_unload(self) -> None:
        self._expiry_loop.cancel()

    async def _migrate(self, db) -> None:
        """Forward-only migration; current version is 1."""
        cur = await db.execute("SELECT version FROM schema_version WHERE cog = 'jail'")
        row = await cur.fetchone()
        current = row["version"] if row else 0
        # v1 -> base schema (nothing extra to do; tables above cover it)
        if current < SCHEMA_VERSION:
            await db.execute(
                "INSERT INTO schema_version (cog, version) VALUES ('jail', ?) "
                "ON CONFLICT(cog) DO UPDATE SET version = excluded.version",
                (SCHEMA_VERSION,),
            )

    async def _ensure_wal(self) -> None:
        try:
            cur = await self.bot.db.execute("PRAGMA journal_mode")
            row = await cur.fetchone()
            if not row or str(row[0]).lower() != "wal":
                await self.bot.db.execute("PRAGMA journal_mode=WAL")
        except Exception:
            LOGGER.debug("could not check WAL mode", exc_info=True)

    # ------------------------------------------------------------------ #
    # Config helpers
    # ------------------------------------------------------------------ #

    async def get_config(self, guild_id: int) -> dict[str, Any]:
        cur = await self.bot.db.execute("SELECT * FROM jail_config WHERE guild_id = ?", (guild_id,))
        row = await cur.fetchone()
        if row:
            return dict(row)
        return {
            "guild_id": guild_id,
            "jail_role_id": None,
            "jail_channel_id": None,
            "log_channel_id": None,
            "default_duration": None,
            "enabled": 1,
            "dm_message": None,
            "release_message": None,
            "remove_roles_mode": "all",
            "auto_reapply": 1,
            "allow_bot_targets": 0,
        }

    async def _save_config_field(self, guild_id: int, field: str, value: Any) -> None:
        config = await self.get_config(guild_id)
        config[field] = value
        await self.bot.db.execute(
            """
            INSERT INTO jail_config (
                guild_id, jail_role_id, jail_channel_id, log_channel_id,
                default_duration, enabled, dm_message, release_message,
                remove_roles_mode, auto_reapply, allow_bot_targets
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                jail_role_id = excluded.jail_role_id,
                jail_channel_id = excluded.jail_channel_id,
                log_channel_id = excluded.log_channel_id,
                default_duration = excluded.default_duration,
                enabled = excluded.enabled,
                dm_message = excluded.dm_message,
                release_message = excluded.release_message,
                remove_roles_mode = excluded.remove_roles_mode,
                auto_reapply = excluded.auto_reapply,
                allow_bot_targets = excluded.allow_bot_targets
            """,
            (
                guild_id,
                config["jail_role_id"],
                config["jail_channel_id"],
                config["log_channel_id"],
                config["default_duration"],
                int(bool(config["enabled"])),
                config["dm_message"],
                config["release_message"],
                config["remove_roles_mode"],
                int(bool(config["auto_reapply"])),
                int(bool(config["allow_bot_targets"])),
            ),
        )
        await self.bot.db.commit()

    async def _active_jail(self, guild_id: int, user_id: int) -> Optional[Any]:
        cur = await self.bot.db.execute(
            "SELECT * FROM jails WHERE guild_id = ? AND user_id = ? AND active = 1",
            (guild_id, user_id),
        )
        return await cur.fetchone()

    # ------------------------------------------------------------------ #
    # Logging
    # ------------------------------------------------------------------ #

    async def _log_action(
        self,
        guild: discord.Guild,
        user_id: int,
        action: str,
        moderator_id: Optional[int],
        reason: Optional[str],
        duration: Optional[str] = None,
        roles: Optional[list[int]] = None,
    ) -> None:
        try:
            await self.bot.db.execute(
                "INSERT INTO jail_history (guild_id, user_id, moderator_id, action, reason, duration, roles_saved, roles_restored, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    guild.id,
                    user_id,
                    moderator_id,
                    action,
                    reason,
                    duration,
                    json.dumps(roles or []),
                    json.dumps(roles or []),
                    int(time.time()),
                ),
            )
            await self.bot.db.execute(
                "INSERT INTO mod_history (guild_id, user_id, action, moderator_id, reason) VALUES (?, ?, ?, ?, ?)",
                (guild.id, user_id, f"jail-{action}", moderator_id, reason),
            )
            await self.bot.db.commit()
        except Exception:
            LOGGER.exception("failed to write jail history (guild=%s user=%s)", guild.id, user_id)

        config = await self.get_config(guild.id)
        channel = guild.get_channel(int(config["log_channel_id"])) if config.get("log_channel_id") else None
        if not isinstance(channel, discord.TextChannel):
            return
        embed = Embed(
            title=f"Jail: {action}",
            description=(
                f"**Member:** <@{user_id}> (`{user_id}`)\n"
                f"**Moderator:** {f'<@{moderator_id}>' if moderator_id else '*system*'}\n"
                f"**Reason:** {discord.utils.escape_markdown(reason or 'Not provided')[:500]}"
                + (f"\n**Duration:** {duration}" if duration else "")
            ),
            color=COLORS.warn if action in ("jail", "extend") else COLORS.approve,
        )
        if roles:
            embed.add_field(
                name="Roles saved/restored",
                value=", ".join(f"<@&{r}>" for r in roles[:25])[:1024] or "none",
                inline=False,
            )
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass

    async def _dm_member(self, member: discord.Member, template: Optional[str], fallback: str) -> None:
        text = template or fallback
        try:
            rendered = await TagScriptParser.parse(text, member, member.guild, None, self.bot)
        except Exception:
            rendered = text
        try:
            await member.send(embed=Embed(description=rendered[:4000], color=COLORS.neutral))
        except (discord.Forbidden, discord.HTTPException):
            pass  # DMs closed — fail silently by design

    # ------------------------------------------------------------------ #
    # Permission helpers
    # ------------------------------------------------------------------ #

    async def _has_perm(self, ctx: commands.Context, **perms: bool) -> bool:
        predicate = has_permissions(**perms)
        try:
            return await predicate.predicate(ctx)
        except Exception:
            return False

    def _bot_top_position(self, guild: discord.Guild) -> int:
        top = guild.me.top_role if guild.me else None
        return top.position if top else 0

    def _check_bot_perms(self, ctx_or_guild, config: dict[str, Any]) -> Optional[str]:
        guild = ctx_or_guild.guild if hasattr(ctx_or_guild, "guild") else ctx_or_guild
        me = guild.me
        if me is None:
            return "I am not fully loaded yet."
        needed = []
        if not me.guild_permissions.manage_roles:
            needed.append("Manage Roles")
        if not me.guild_permissions.manage_channels:
            needed.append("Manage Channels")
        if config.get("jail_role_id") and not me.guild_permissions.manage_roles:
            needed.append("Manage Roles (to apply the jail role)")
        if needed:
            return f"I'm missing: **{', '.join(needed)}**."
        if config.get("jail_role_id"):
            role = guild.get_role(int(config["jail_role_id"]))
            if role is None:
                return "The configured jail role no longer exists — run `,jail repair`."
            if role >= guild.me.top_role:
                return "The jail role is **above my top role** — move it below me."
        return None

    # ------------------------------------------------------------------ #
    # Core jail / unjail
    # ------------------------------------------------------------------ #

    async def _jail_member(
        self,
        ctx: commands.Context,
        member: discord.Member,
        duration: Optional[str],
        reason: Optional[str],
    ) -> None:
        guild = ctx.guild
        config = await self.get_config(guild.id)

        if not int(config.get("enabled", 1)):
            return await ctx.deny("The jail system is currently **disabled** (`,jail toggle`).")
        if not await self._has_perm(ctx, moderate_members=True):
            return await ctx.deny("You need the **Moderate Members** permission to jail.")

        if not config.get("jail_role_id"):
            return await ctx.deny("No jail role configured — run `,jail setup` or `,jail role <role>`.")

        bot_error = self._check_bot_perms(guild, config)
        if bot_error:
            return await ctx.deny(bot_error)

        if member.bot and not int(config.get("allow_bot_targets", 0)):
            return await ctx.deny("Jailing bots is disabled — allow it with `,jail settings`.")
        if member.id == ctx.author.id:
            return await ctx.deny("You cannot jail yourself.")
        if member.id == guild.owner_id:
            return await ctx.deny("You cannot jail the **server owner**.")
        if await self.bot.is_owner(member):
            return await ctx.deny("You cannot jail a bot owner.")

        ok, msg = can_moderate(ctx, member)
        if not ok:
            return await ctx.deny(msg)

        # immunity (user or any of their roles)
        immune = await self._is_immune(guild.id, member)
        if immune:
            return await ctx.deny("That member or one of their roles is **immune** to jailing.")

        jail_role = guild.get_role(int(config["jail_role_id"]))
        if jail_role is None:
            return await ctx.deny("The jail role was deleted — run `,jail repair`.")

        td = parse_duration(duration) if duration else None
        if duration and td is None:
            return await ctx.warn("Invalid duration. Use formats like `30m`, `2h`, `1d`, or leave it out for indefinite.")
        if td is None and config.get("default_duration"):
            td = parse_duration(str(config["default_duration"]))

        actual_reason = reason or "No reason provided"

        expires_at = int(time.time()) + int(td.total_seconds()) if td else None

        async with self.locks.get(guild.id, member.id):
            if await self._active_jail(guild.id, member.id):
                return await ctx.deny("That member is **already jailed**. Use `,jail extend` to lengthen the sentence.")

            # 1) PERSIST FIRST so a crash mid-action is recoverable.
            saved_ids = saved_roles_snapshot(member, jail_role.id, self._bot_top_position(guild))
            await self.bot.db.execute(
                "INSERT INTO jails (guild_id, user_id, saved_role_ids, reason, moderator_id, jailed_at, expires_at, active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (guild.id, member.id, json.dumps(saved_ids), actual_reason, ctx.author.id, int(time.time()), expires_at),
            )
            await self.bot.db.commit()

            # 2) Apply on Discord, rolling back what we can on failure.
            stripped: list[int] = []
            partial_failure: Optional[str] = None
            try:
                roles_to_remove = [guild.get_role(rid) for rid in saved_ids]
                for role in roles_to_remove:
                    if role is None:
                        continue
                    try:
                        await member.remove_roles(role, reason=f"Jailed by {ctx.author} (ID: {ctx.author.id})")
                        stripped.append(role.id)
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        partial_failure = f"failed removing <@&{role.id}> (`{exc}`)"
                        break
                if partial_failure is None:
                    try:
                        await member.add_roles(jail_role, reason=f"Jailed by {ctx.author} (ID: {ctx.author.id})")
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        partial_failure = f"failed adding the jail role (`{exc}`)"
            except Exception:
                LOGGER.exception("unexpected jail failure guild=%s member=%s", guild.id, member.id)
                partial_failure = "unexpected error"

            if partial_failure:
                # Roll back: restore what we stripped, drop the DB row.
                await self._restore_roles_quiet(guild, member, stripped)
                await self.bot.db.execute(
                    "DELETE FROM jails WHERE guild_id = ? AND user_id = ? AND active = 1",
                    (guild.id, member.id),
                )
                await self.bot.db.commit()
                return await ctx.deny(f"Jail failed partway and was rolled back: {partial_failure}")

        # 3) Voice disconnect + DM + log.
        try:
            if member.voice:
                await member.move_to(None, reason="Jailed")
        except (discord.Forbidden, discord.HTTPException):
            pass

        await self._dm_member(
            member,
            config.get("dm_message"),
            f"You have been jailed in **{guild.name}**.\nReason: {actual_reason}"
            + (f"\nDuration: {human_duration(td)}" if td else "\nYou will remain jailed until a staff member releases you."),
        )
        await self._log_action(
            guild, member.id, "jail", ctx.author.id, actual_reason,
            human_duration(td) if td else "indefinite", saved_ids,
        )
        await ctx.approve(
            f"**{member}** has been jailed."
            + (f" **Duration:** {human_duration(td)} — they will be released {discord.utils.format_dt(discord.utils.utcnow() + td, style='R')}." if td else "")
            + f" **Reason:** {actual_reason}"
        )

    async def _restore_roles_quiet(self, guild: discord.Guild, member: discord.Member, role_ids: list[int]) -> None:
        for role_id in role_ids:
            role = guild.get_role(role_id)
            if role is None:
                continue
            try:
                await member.add_roles(role, reason="Jail rollback")
            except (discord.Forbidden, discord.HTTPException):
                pass

    async def _is_immune(self, guild_id: int, member: discord.Member) -> bool:
        cur = await self.bot.db.execute(
            "SELECT 1 FROM jail_immune WHERE guild_id = ? AND (target_id = ? OR target_id IN "
            f"({', '.join('?' * max(len(member.roles) - 1, 0)) or 'NULL'}))",
            (guild_id, member.id, *[r.id for r in member.roles[1:]]),
        )
        return await cur.fetchone() is not None

    async def _do_unjail(
        self,
        guild: discord.Guild,
        member: Optional[discord.Member],
        row: Any,
        reason: str,
        moderator_id: Optional[int],
        via: str = "manual",
    ) -> tuple[bool, str, list[int]]:
        """Release a jailed member. Idempotent: re-running on an inactive row is a no-op.

        Returns (success, message, restored_role_ids).
        """
        async with self.locks.get(guild.id, row["user_id"]):
            current = await self._active_jail(guild.id, row["user_id"])
            if not current:
                return False, "not-jailed", []

            saved_ids = json.loads(current["saved_role_ids"] or "[]")
            config = await self.get_config(guild.id)
            bot_top = self._bot_top_position(guild)

            guild_roles: dict[int, dict[str, Any]] = {
                role.id: {
                    "id": role.id,
                    "managed": role.managed,
                    "is_default": role.is_default(),
                    "position": role.position,
                }
                for role in guild.roles
            }
            restorable, skipped = restore_split(saved_ids, guild_roles, bot_top)

            target = member or guild.get_member(row["user_id"])
            restored: list[int] = []
            if target:
                for role_id in restorable:
                    role = guild.get_role(role_id)
                    if role is None:
                        continue
                    try:
                        await target.add_roles(role, reason=f"Unjailed ({via})")
                        restored.append(role_id)
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        skipped.append((role_id, f"failed ({exc})"))
                # Remove the jail role last so the member regains access cleanly.
                if config.get("jail_role_id"):
                    jail_role = guild.get_role(int(config["jail_role_id"]))
                    if jail_role and jail_role in target.roles:
                        try:
                            await target.remove_roles(jail_role, reason=f"Unjailed ({via})")
                        except (discord.Forbidden, discord.HTTPException) as exc:
                            skipped.append((jail_role.id, f"jail-role remove failed ({exc})"))
            else:
                skipped.append((0, "member left the guild"))

            await self.bot.db.execute(
                "UPDATE jails SET active = 0 WHERE guild_id = ? AND user_id = ?",
                (guild.id, row["user_id"]),
            )
            await self.bot.db.commit()

            await self._log_action(
                guild, row["user_id"], "unjail" if via == "manual" else f"unjail ({via})",
                moderator_id, reason, None, restored,
            )

            if target and via in ("manual", "expired"):
                await self._dm_member(
                    target,
                    config.get("release_message"),
                    f"You have been released from jail in **{guild.name}**."
                    + (f"\nReason: {reason}" if reason else ""),
                )

            detail = f"Restored {len(restored)} role(s)."
            if skipped:
                detail += " Skipped: " + ", ".join(
                    (f"<@&{rid}>" if rid else "member") + f" ({why})" for rid, why in skipped[:10]
                )
            return True, detail, restored

    # ------------------------------------------------------------------ #
    # Expiry task
    # ------------------------------------------------------------------ #

    @tasks.loop(seconds=30)
    async def _expiry_loop(self) -> None:
        now = int(time.time())
        try:
            cur = await self.bot.db.execute(
                "SELECT * FROM jails WHERE active = 1 AND expires_at IS NOT NULL AND expires_at <= ?",
                (now,),
            )
            rows = await cur.fetchall()
        except Exception:
            LOGGER.exception("expiry loop query failed")
            return
        for row in rows:
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None:
                # Guild gone — deactivate so the loop doesn't retry forever.
                try:
                    await self.bot.db.execute(
                        "UPDATE jails SET active = 0 WHERE guild_id = ? AND user_id = ?",
                        (row["guild_id"], row["user_id"]),
                    )
                    await self.bot.db.commit()
                except Exception:
                    LOGGER.exception("failed to deactivate orphan jail row")
                continue
            member = guild.get_member(row["user_id"])
            try:
                ok, detail, _ = await self._do_unjail(
                    guild, member, row, "Sentence expired", None, via="expired"
                )
                if ok:
                    log.success(f"auto-released user {row['user_id']} in guild {row['guild_id']}", name="Jail")
            except Exception:
                LOGGER.exception(
                    "expiry release failed guild=%s user=%s (continuing loop)",
                    row["guild_id"], row["user_id"],
                )

    @_expiry_loop.before_loop
    async def _before_expiry(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    # Listeners
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Re-apply the jail role to a jailed member who left and rejoined."""
        row = await self._active_jail(member.guild.id, member.id)
        if not row:
            return
        config = await self.get_config(member.guild.id)
        if not config.get("jail_role_id"):
            return
        jail_role = member.guild.get_role(int(config["jail_role_id"]))
        if jail_role is None:
            return
        try:
            await member.add_roles(jail_role, reason="Jail sentence persists across rejoin")
            await self._log_action(member.guild, member.id, "rejail (rejoin)", None, "Member rejoined while jailed")
        except (discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.warning("rejoin rejail failed guild=%s user=%s: %s", member.guild.id, member.id, exc)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Detect manual jail-role removal and re-apply it (configurable)."""
        config = await self.get_config(after.guild.id)
        if not int(config.get("auto_reapply", 1)) or not config.get("jail_role_id"):
            return
        jail_role = after.guild.get_role(int(config["jail_role_id"]))
        if jail_role is None:
            return
        if jail_role in before.roles and jail_role not in after.roles:
            row = await self._active_jail(after.guild.id, after.id)
            if row:
                async with self.locks.get(after.guild.id, after.id):
                    row = await self._active_jail(after.guild.id, after.id)
                    if row and jail_role not in after.roles:
                        try:
                            await after.add_roles(jail_role, reason="Jail role manually removed — re-applied")
                            await self._log_action(after.guild, after.id, "rejail (role restore)", None, "Jail role was manually removed")
                        except (discord.Forbidden, discord.HTTPException):
                            pass

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        """New channels deny the jail role by default."""
        config = await self.get_config(channel.guild.id)
        if not config.get("jail_role_id") or channel.id == config.get("jail_channel_id"):
            return
        role = channel.guild.get_role(int(config["jail_role_id"]))
        if role is None:
            return
        try:
            await channel.set_permissions(role, view_channel=False, send_messages=False, connect=False, reason="Jail: deny new channel")
        except (discord.Forbidden, discord.HTTPException):
            pass

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        config = await self.get_config(channel.guild.id)
        if config.get("jail_channel_id") == channel.id:
            await self._save_config_field(channel.guild.id, "jail_channel_id", None)
            await self._notify_staff(channel.guild, "The jail channel was **deleted**. Run `,jail setup` or `,jail channel <channel>` to repair.")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        config = await self.get_config(role.guild.id)
        if config.get("jail_role_id") == role.id:
            await self._save_config_field(role.guild.id, "jail_role_id", None)
            await self._save_config_field(role.guild.id, "enabled", 0)
            await self._notify_staff(role.guild, "The jail role was **deleted** — the jail system has been **disabled**. Run `,jail setup` to repair.")

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        try:
            await self.bot.db.execute("DELETE FROM jail_config WHERE guild_id = ?", (guild.id,))
            await self.bot.db.execute("DELETE FROM jails WHERE guild_id = ?", (guild.id,))
            await self.bot.db.execute("DELETE FROM jail_history WHERE guild_id = ?", (guild.id,))
            await self.bot.db.execute("DELETE FROM jail_immune WHERE guild_id = ?", (guild.id,))
            await self.bot.db.commit()
            self.locks.drop_guild(guild.id)
        except Exception:
            LOGGER.exception("guild cleanup failed for %s", guild.id)

    async def _notify_staff(self, guild: discord.Guild, message: str) -> None:
        config = await self.get_config(guild.id)
        channel = guild.get_channel(int(config["log_channel_id"])) if config.get("log_channel_id") else guild.system_channel
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(embed=Embed(description=f"{EMOJIS.WARN} {message}", color=COLORS.warn))
            except (discord.Forbidden, discord.HTTPException):
                pass

    # ------------------------------------------------------------------ #
    # Overwrite sync
    # ------------------------------------------------------------------ #

    async def _sync_overwrites(self, guild: discord.Guild, jail_role: discord.Role, jail_channel_id: Optional[int], progress: Optional[discord.Message] = None) -> tuple[int, int]:
        """Deny the jail role on every channel except the jail channel. Rate-limit friendly."""
        channels = [c for c in guild.channels if c.id != jail_channel_id]
        done = 0
        failed = 0
        sem = asyncio.Semaphore(5)

        async def apply(chan) -> None:
            nonlocal done, failed
            async with sem:
                try:
                    await chan.set_permissions(jail_role, view_channel=False, send_messages=False, connect=False, reason="Jail sync")
                    done += 1
                except (discord.Forbidden, discord.HTTPException):
                    failed += 1

        chunk = 20
        for i in range(0, len(channels), chunk):
            await asyncio.gather(*(apply(c) for c in channels[i : i + chunk]))
            if progress and i + chunk < len(channels):
                try:
                    await progress.edit(
                        embed=Embed(
                            description=f"Syncing overwrites… **{done}/{len(channels)}** channels done.",
                            color=COLORS.neutral,
                        )
                    )
                except discord.HTTPException:
                    pass
        return done, failed

    # ------------------------------------------------------------------ #
    # Top-level: ,jail <user>, ,unjail, ,unjailall
    # ------------------------------------------------------------------ #

    @hybrid_group(
        name="jail",
        description="Jail a member or configure the jail system",
        invoke_without_command=True,
    )
    @app_commands.describe(user="Member to jail", duration="Sentence length like 30m, 2h, 1d", reason="Reason for jailing")
    @commands.guild_only()
    async def jail(
        self,
        ctx: commands.Context,
        user: Optional[discord.Member] = None,
        duration: Optional[str] = None,
        *,
        reason: Optional[str] = None,
    ) -> None:
        """,jail <user> [duration] [reason] — jail a member (with confirmation)."""
        if user is None:
            # Bare invocation is handled by the hybrid_group wrapper (group help).
            return
        await self._jail_member(ctx, user, duration, reason)

    async def _confirm_and_run(self, ctx: commands.Context, embed: Embed, coro_fn) -> None:
        """Standard ban/kick-style confirmation flow."""
        view = ConfirmView(ctx.author)
        message = await ctx.send(embed=embed, view=view)
        await view.wait()
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        if not view.value:
            return await ctx.warn("Cancelled.")
        await coro_fn()

    @commands.hybrid_command(
        name="unjail",
        aliases=["release"],
        description="Release a jailed member and restore their roles",
    )
    @app_commands.describe(user="Member to release", reason="Reason for releasing")
    @commands.guild_only()
    async def unjail(self, ctx: commands.Context, user: discord.Member, *, reason: Optional[str] = None) -> None:
        """Release a member and restore their saved roles."""
        row = await self._active_jail(ctx.guild.id, user.id)
        if not row:
            return await ctx.deny("That member is not jailed.")
        bot_error = self._check_bot_perms(ctx.guild, await self.get_config(ctx.guild.id))
        if bot_error:
            return await ctx.deny(bot_error)
        await self._confirm_and_run(
            ctx,
            Embed(
                title="Confirm Unjail",
                description=f"Release **{user}** and restore their roles?" + (f"\n**Reason:** {reason}" if reason else ""),
                color=COLORS.warn,
            ),
            lambda: self._finish_unjail(ctx, user, row, reason),
        )

    async def _finish_unjail(self, ctx: commands.Context, user: discord.Member, row: Any, reason: Optional[str]) -> None:
        ok, detail, _ = await self._do_unjail(ctx.guild, user, row, reason or "No reason provided", ctx.author.id)
        if not ok:
            return await ctx.warn("That member is no longer jailed.")
        await ctx.approve(f"**{user}** has been released. {detail}")

    @commands.hybrid_command(name="unjailall", description="Release everyone currently jailed (with confirmation)")
    @commands.guild_only()
    @has_permissions(administrator=True)
    async def unjailall(self, ctx: commands.Context) -> None:
        """Release every currently jailed member in this server."""
        cur = await self.bot.db.execute("SELECT * FROM jails WHERE guild_id = ? AND active = 1", (ctx.guild.id,))
        rows = await cur.fetchall()
        if not rows:
            return await ctx.warn("Nobody is currently jailed.")
        await self._confirm_and_run(
            ctx,
            Embed(title="Confirm Unjailall", description=f"Release **{len(rows)}** jailed member(s)?", color=COLORS.deny),
            lambda: self._finish_unjailall(ctx, rows),
        )

    async def _finish_unjailall(self, ctx: commands.Context, rows: list[Any]) -> None:
        released = 0
        for row in rows:
            member = ctx.guild.get_member(row["user_id"])
            try:
                ok, _, _ = await self._do_unjail(ctx.guild, member, row, f"Mass release by {ctx.author}", ctx.author.id)
                if ok:
                    released += 1
            except Exception:
                LOGGER.exception("unjailall failed for user %s", row["user_id"])
        await ctx.approve(f"Released **{released}/{len(rows)}** member(s).")

    # ------------------------------------------------------------------ #
    # Setup & config
    # ------------------------------------------------------------------ #

    @jail.command(name="setup", description="Create the jail role and channel and configure permissions")
    @commands.guild_only()
    @has_permissions(administrator=True)
    @commands.bot_has_permissions(manage_roles=True, manage_channels=True)
    async def jail_setup(self, ctx: commands.Context) -> None:
        """Create the jail role, jail channel and deny overwrites across the server."""
        guild = ctx.guild
        config = await self.get_config(guild.id)
        if config.get("jail_role_id"):
            return await ctx.warn("The jail system is already set up. Use `,jail reset` first if you want to redo it.")

        status = await ctx.approve("Setting up the jail system… this may take a moment.")

        try:
            jail_role = await guild.create_role(name="Jailed", color=discord.Color.dark_grey(), reason=f"Jail setup by {ctx.author}")
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False, send_messages=False),
                jail_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_roles=True),
            }
            jail_channel = await guild.create_text_channel("jail", overwrites=overwrites, reason=f"Jail setup by {ctx.author}")
        except discord.Forbidden:
            return await status.edit(embed=Embed(description=f"{ctx.author.mention}: I need **Manage Roles** and **Manage Channels**.", color=COLORS.deny))
        except discord.HTTPException as exc:
            return await status.edit(embed=Embed(description=f"{ctx.author.mention}: setup failed: `{exc}`", color=COLORS.deny))

        await self._save_config_field(guild.id, "jail_role_id", jail_role.id)
        await self._save_config_field(guild.id, "jail_channel_id", jail_channel.id)

        try:
            await status.edit(embed=Embed(description=f"Role and channel created. Applying overwrites to **{len(guild.channels)}** channels…", color=COLORS.neutral))
            done, failed = await self._sync_overwrites(guild, jail_role, jail_channel.id, progress=status)
        except Exception:
            LOGGER.exception("jail setup sync failed guild=%s", guild.id)
            done = failed = 0

        await self._log_action(guild, 0, "setup", ctx.author.id, f"Jail role {jail_role.name} + channel created")
        await status.edit(
            embed=Embed(
                title="Jail setup complete",
                description=(
                    f"**Jail role:** {jail_role.mention}\n"
                    f"**Jail channel:** {jail_channel.mention}\n"
                    f"**Overwrites applied:** {done} ok / {failed} failed\n\n"
                    f"Set a log channel with `,jail logchannel <channel>` and a default duration with `,jail duration <time>`."
                ),
                color=COLORS.approve,
            )
        )

    @jail.command(name="role", description="Set an existing role as the jail role")
    @commands.guild_only()
    @has_permissions(administrator=True)
    async def jail_role(self, ctx: commands.Context, role: discord.Role) -> None:
        """Use an existing role as the jail role."""
        if role.is_default() or role.managed:
            return await ctx.warn("That role cannot be used (managed or @everyone).")
        if role >= ctx.guild.me.top_role:
            return await ctx.warn("That role is **above my top role** — I couldn't assign it.")
        await self._save_config_field(ctx.guild.id, "jail_role_id", role.id)
        await ctx.approve(f"{role.mention} is now the jail role. Run `,jail sync` to fix channel permissions.")

    @jail.command(name="channel", description="Set the jail channel")
    @commands.guild_only()
    @has_permissions(administrator=True)
    async def jail_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set an existing channel as the jail channel."""
        await self._save_config_field(ctx.guild.id, "jail_channel_id", channel.id)
        await ctx.approve(f"{channel.mention} is now the jail channel.")

    @jail.command(name="logchannel", description="Set the jail log channel")
    @commands.guild_only()
    @has_permissions(administrator=True)
    async def jail_logchannel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set the channel where jail actions are logged."""
        await self._save_config_field(ctx.guild.id, "log_channel_id", channel.id)
        await ctx.approve(f"Jail actions will be logged in {channel.mention}.")

    @jail.command(name="duration", description="Set the default sentence length")
    @commands.guild_only()
    @has_permissions(administrator=True)
    async def jail_duration(self, ctx: commands.Context, duration: str) -> None:
        """Set the default sentence when none is given (empty/`none` to clear)."""
        if duration.lower() in ("none", "0", "indefinite"):
            await self._save_config_field(ctx.guild.id, "default_duration", None)
            return await ctx.approve("Default duration cleared — jails without a duration are indefinite.")
        td = parse_duration(duration)
        if not td:
            return await ctx.warn("Invalid duration. Use formats like `30m`, `2h`, `1d`.")
        await self._save_config_field(ctx.guild.id, "default_duration", duration)
        await ctx.approve(f"Default jail duration set to **{human_duration(td)}**.")

    @jail.command(name="message", description="Set the DM sent when a member is jailed")
    @commands.guild_only()
    @has_permissions(administrator=True)
    async def jail_message(self, ctx: commands.Context, *, message: str) -> None:
        """Set the jail DM (Tagscript supported, e.g. {user} {guild.name})."""
        if len(message) > 2000:
            return await ctx.warn("Message too long (2000 max).")
        await self._save_config_field(ctx.guild.id, "dm_message", message)
        await ctx.approve(f"Jail DM set:\n>>> {discord.utils.escape_markdown(message[:500])}")

    @jail.command(name="releasemessage", description="Set the DM sent when a member is released")
    @commands.guild_only()
    @has_permissions(administrator=True)
    async def jail_releasemessage(self, ctx: commands.Context, *, message: str) -> None:
        """Set the release DM (Tagscript supported)."""
        if len(message) > 2000:
            return await ctx.warn("Message too long (2000 max).")
        await self._save_config_field(ctx.guild.id, "release_message", message)
        await ctx.approve(f"Release DM set:\n>>> {discord.utils.escape_markdown(message[:500])}")

    @jail.command(name="toggle", description="Enable or disable the jail system")
    @commands.guild_only()
    @has_permissions(administrator=True)
    async def jail_toggle(self, ctx: commands.Context) -> None:
        """Enable or disable jailing in this server."""
        config = await self.get_config(ctx.guild.id)
        new_state = 0 if int(config.get("enabled", 1)) else 1
        await self._save_config_field(ctx.guild.id, "enabled", new_state)
        await ctx.approve("The jail system is now **enabled**." if new_state else "The jail system is now **disabled**. Existing sentences continue counting down.")

    # ------------------------------------------------------------------ #
    # Information & management
    # ------------------------------------------------------------------ #

    @jail.command(name="list", description="List currently jailed members")
    @commands.guild_only()
    @has_permissions(moderate_members=True)
    async def jail_list(self, ctx: commands.Context) -> None:
        """List every currently jailed member with time remaining (paginated)."""
        cur = await self.bot.db.execute(
            "SELECT * FROM jails WHERE guild_id = ? AND active = 1 ORDER BY jailed_at DESC",
            (ctx.guild.id,),
        )
        rows = await cur.fetchall()
        if not rows:
            return await ctx.warn("Nobody is currently jailed.")
        now = int(time.time())
        lines = []
        for row in rows:
            expiry = "indefinite" if not row["expires_at"] else human_duration(timedelta(seconds=max(row["expires_at"] - now, 0)))
            moderator = f"<@{row['moderator_id']}>" if row["moderator_id"] else "system"
            lines.append(f"<@{row['user_id']}> — **{expiry}** — by {moderator} — {discord.utils.escape_markdown((row['reason'] or '')[:60])}")
        embeds = []
        for i in range(0, len(lines), 15):
            embeds.append(
                Embed(
                    title=f"Jailed members ({len(rows)})",
                    description="\n".join(lines[i : i + 15]),
                    color=COLORS.neutral,
                )
            )
        await ctx.paginate(embeds)

    @jail.command(name="info", description="Details for a jailed member")
    @commands.guild_only()
    @has_permissions(moderate_members=True)
    async def jail_info(self, ctx: commands.Context, user: discord.Member) -> None:
        """Reason, moderator, expiry and saved roles for a jailed member."""
        row = await self._active_jail(ctx.guild.id, user.id)
        if not row:
            return await ctx.deny(f"**{user}** is not jailed.")
        now = int(time.time())
        expires = (
            f"{discord.utils.format_dt(discord.utils.utcfromtimestamp(row['expires_at']), style='F')} "
            f"({human_duration(timedelta(seconds=max(row['expires_at'] - now, 0)))} left)"
            if row["expires_at"] else "indefinite"
        )
        saved = json.loads(row["saved_role_ids"] or "[]")
        embed = (
            Embed(title=f"Jail: {user}", color=COLORS.warn)
            .add_field(name="Reason", value=discord.utils.escape_markdown(row["reason"] or "N/A")[:1024])
            .add_field(name="Moderator", value=f"<@{row['moderator_id']}>" if row["moderator_id"] else "system")
            .add_field(name="Jailed", value=f"<t:{row['jailed_at']}:R>")
            .add_field(name="Expires", value=expires)
            .add_field(name="Saved roles", value=", ".join(f"<@&{r}>" for r in saved[:25]) or "none")
        )
        await ctx.send(embed=embed)

    @jail.command(name="history", description="A member's full jail history")
    @commands.guild_only()
    @has_permissions(moderate_members=True)
    async def jail_history(self, ctx: commands.Context, user: discord.Member) -> None:
        """Every jail action recorded for a member."""
        cur = await self.bot.db.execute(
            "SELECT * FROM jail_history WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC LIMIT 50",
            (ctx.guild.id, user.id),
        )
        rows = await cur.fetchall()
        if not rows:
            return await ctx.warn(f"No jail history for **{user}**.")
        lines = []
        for r in rows:
            moderator = f"<@{r['moderator_id']}>" if r["moderator_id"] else "system"
            lines.append(
                f"`<t:{r['created_at']}:R>` **{r['action']}** — "
                f"{discord.utils.escape_markdown((r['reason'] or '')[:60])} — by {moderator}"
            )
        embeds = [Embed(title=f"Jail history: {user}", description="\n".join(lines[i : i + 15]), color=COLORS.neutral) for i in range(0, len(lines), 15)]
        await ctx.paginate(embeds)

    @jail.command(name="extend", description="Extend an active sentence")
    @commands.guild_only()
    @has_permissions(moderate_members=True)
    async def jail_extend(self, ctx: commands.Context, user: discord.Member, duration: str) -> None:
        """Lengthen a member's active jail sentence."""
        td = parse_duration(duration)
        if not td:
            return await ctx.warn("Invalid duration. Use formats like `30m`, `2h`, `1d`.")
        row = await self._active_jail(ctx.guild.id, user.id)
        if not row:
            return await ctx.deny("That member is not jailed.")
        if not row["expires_at"]:
            return await ctx.warn("That sentence is **indefinite** — nothing to extend.")
        new_expiry = max(row["expires_at"], int(time.time())) + int(td.total_seconds())
        async with self.locks.get(ctx.guild.id, user.id):
            await self.bot.db.execute("UPDATE jails SET expires_at = ? WHERE guild_id = ? AND user_id = ? AND active = 1", (new_expiry, ctx.guild.id, user.id))
            await self.bot.db.commit()
        await self._log_action(ctx.guild, user.id, "extend", ctx.author.id, row["reason"], f"+{human_duration(td)}")
        await ctx.approve(f"Sentence for **{user}** extended by **{human_duration(td)}** — now ends {discord.utils.format_dt(discord.utils.utcfromtimestamp(new_expiry), style='R')}.")

    @jail.command(name="reduce", description="Shorten an active sentence")
    @commands.guild_only()
    @has_permissions(moderate_members=True)
    async def jail_reduce(self, ctx: commands.Context, user: discord.Member, duration: str) -> None:
        """Shorten a member's active jail sentence."""
        td = parse_duration(duration)
        if not td:
            return await ctx.warn("Invalid duration. Use formats like `30m`, `2h`, `1d`.")
        row = await self._active_jail(ctx.guild.id, user.id)
        if not row:
            return await ctx.deny("That member is not jailed.")
        if not row["expires_at"]:
            return await ctx.warn("That sentence is **indefinite** — use `,unjail` to release them.")
        new_expiry = row["expires_at"] - int(td.total_seconds())
        now = int(time.time())
        async with self.locks.get(ctx.guild.id, user.id):
            if new_expiry <= now:
                # Shortened past the end — release immediately.
                ok, detail, _ = await self._do_unjail(ctx.guild, user, row, f"Reduced below expiry by {ctx.author}", ctx.author.id)
                if ok:
                    return await ctx.approve(f"**{user}** released early. {detail}")
                return await ctx.warn("Release failed.")
            await self.bot.db.execute("UPDATE jails SET expires_at = ? WHERE guild_id = ? AND user_id = ? AND active = 1", (new_expiry, ctx.guild.id, user.id))
            await self.bot.db.commit()
        await self._log_action(ctx.guild, user.id, "reduce", ctx.author.id, row["reason"], f"-{human_duration(td)}")
        await ctx.approve(f"Sentence for **{user}** reduced by **{human_duration(td)}** — now ends {discord.utils.format_dt(discord.utils.utcfromtimestamp(new_expiry), style='R')}.")

    @jail.command(name="reason", description="Edit the reason for an active jail")
    @commands.guild_only()
    @has_permissions(moderate_members=True)
    async def jail_reason(self, ctx: commands.Context, user: discord.Member, *, reason: str) -> None:
        """Change the recorded reason for an active sentence."""
        row = await self._active_jail(ctx.guild.id, user.id)
        if not row:
            return await ctx.deny("That member is not jailed.")
        await self.bot.db.execute("UPDATE jails SET reason = ? WHERE guild_id = ? AND user_id = ? AND active = 1", (reason[:500], ctx.guild.id, user.id))
        await self.bot.db.commit()
        await self._log_action(ctx.guild, user.id, "reason-edit", ctx.author.id, reason)
        await ctx.approve(f"Reason for **{user}**'s jail updated.")

    @jail.command(name="sync", description="Re-apply jail channel permission overwrites")
    @commands.guild_only()
    @has_permissions(administrator=True)
    @commands.bot_has_permissions(manage_roles=True, manage_channels=True)
    async def jail_sync(self, ctx: commands.Context) -> None:
        """Repair drifted overwrites across every channel."""
        config = await self.get_config(ctx.guild.id)
        if not config.get("jail_role_id"):
            return await ctx.deny("No jail role configured.")
        role = ctx.guild.get_role(int(config["jail_role_id"]))
        if role is None:
            return await ctx.deny("The jail role no longer exists — run `,jail repair`.")
        if ctx.guild.id in self._syncing:
            return await ctx.warn("A sync is already running.")
        self._syncing.add(ctx.guild.id)
        try:
            status = await ctx.approve("Syncing channel overwrites…")
            done, failed = await self._sync_overwrites(ctx.guild, role, config.get("jail_channel_id"), progress=status)
            await status.edit(embed=Embed(description=f"Overwrite sync finished: **{done}** ok, **{failed}** failed.", color=COLORS.approve))
        finally:
            self._syncing.discard(ctx.guild.id)

    @jail.command(name="repair", description="Detect and fix jail inconsistencies")
    @commands.guild_only()
    @has_permissions(administrator=True)
    async def jail_repair(self, ctx: commands.Context) -> None:
        """Detect missing roles, orphaned rows and drifted overwrites."""
        guild = ctx.guild
        config = await self.get_config(guild.id)
        findings: list[str] = []

        cur = await self.bot.db.execute("SELECT * FROM jails WHERE guild_id = ? AND active = 1", (guild.id,))
        rows = await cur.fetchall()
        for row in rows:
            member = guild.get_member(row["user_id"])
            if member is None:
                await self.bot.db.execute("UPDATE jails SET active = 0 WHERE guild_id = ? AND user_id = ?", (guild.id, row["user_id"]))
                findings.append(f"Deactivated jail row for a member who left (`{row['user_id']}`).")
                continue
            if config.get("jail_role_id"):
                jail_role = guild.get_role(int(config["jail_role_id"]))
                if jail_role and jail_role not in member.roles:
                    try:
                        await member.add_roles(jail_role, reason="Jail repair")
                        findings.append(f"Re-applied the jail role to <@{row['user_id']}>.")
                    except (discord.Forbidden, discord.HTTPException):
                        findings.append(f"Could not re-apply the jail role to <@{row['user_id']}> (permissions?).")
        await self.bot.db.commit()

        if config.get("jail_role_id") and guild.get_role(int(config["jail_role_id"])) is None:
            findings.append("The jail role is missing — run `,jail setup` or `,jail role <role>`.")
        if config.get("jail_channel_id") and guild.get_channel(int(config["jail_channel_id"])) is None:
            findings.append("The jail channel is missing — run `,jail channel <channel>`.")

        if not findings:
            return await ctx.approve("No issues found — the jail system looks healthy.")
        await ctx.send(embed=Embed(title="Jail repair report", description="\n".join(f"• {f}" for f in findings)[:4000], color=COLORS.warn))

    @jail.command(name="settings", description="View the current jail configuration")
    @commands.guild_only()
    @has_permissions(administrator=True)
    async def jail_settings(self, ctx: commands.Context) -> None:
        """View the current jail configuration."""
        config = await self.get_config(ctx.guild.id)
        role = ctx.guild.get_role(int(config["jail_role_id"])) if config.get("jail_role_id") else None
        channel = ctx.guild.get_channel(int(config["jail_channel_id"])) if config.get("jail_channel_id") else None
        log_ch = ctx.guild.get_channel(int(config["log_channel_id"])) if config.get("log_channel_id") else None
        cur = await self.bot.db.execute("SELECT COUNT(*) AS c FROM jails WHERE guild_id = ? AND active = 1", (ctx.guild.id,))
        active = (await cur.fetchone())["c"]
        await ctx.send(
            embed=(
                Embed(title="Jail settings", color=COLORS.neutral)
                .add_field(name="Enabled", value="yes" if int(config.get("enabled", 1)) else "no")
                .add_field(name="Jail role", value=role.mention if role else "not set")
                .add_field(name="Jail channel", value=channel.mention if channel else "not set")
                .add_field(name="Log channel", value=log_ch.mention if log_ch else "not set")
                .add_field(name="Default duration", value=config.get("default_duration") or "indefinite")
                .add_field(name="Re-apply on removal", value="yes" if int(config.get("auto_reapply", 1)) else "no")
                .add_field(name="Currently jailed", value=str(active))
            )
        )

    @jail.command(name="reset", description="Remove jail configuration and release all members")
    @commands.guild_only()
    @has_permissions(administrator=True)
    async def jail_reset(self, ctx: commands.Context) -> None:
        """Wipe jail configuration and release everyone (admin, with confirmation)."""
        await self._confirm_and_run(
            ctx,
            Embed(
                title="Confirm Jail Reset",
                description="Delete the jail configuration and **release every jailed member**? This cannot be undone.",
                color=COLORS.deny,
            ),
            lambda: self._finish_reset(ctx),
        )

    async def _finish_reset(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        cur = await self.bot.db.execute("SELECT * FROM jails WHERE guild_id = ? AND active = 1", (guild.id,))
        rows = await cur.fetchall()
        for row in rows:
            member = guild.get_member(row["user_id"])
            try:
                await self._do_unjail(guild, member, row, "Jail system reset", ctx.author.id)
            except Exception:
                LOGGER.exception("reset unjail failed user=%s", row["user_id"])
        config = await self.get_config(guild.id)
        if config.get("jail_role_id"):
            role = guild.get_role(int(config["jail_role_id"]))
            if role and not role.managed:
                try:
                    await role.delete(reason=f"Jail reset by {ctx.author}")
                except (discord.Forbidden, discord.HTTPException):
                    pass
        await self.bot.db.execute("DELETE FROM jail_config WHERE guild_id = ?", (guild.id,))
        await self.bot.db.execute("DELETE FROM jail_immune WHERE guild_id = ?", (guild.id,))
        await self.bot.db.commit()
        self.locks.drop_guild(guild.id)
        await ctx.approve("Jail configuration wiped and everyone released.")

    # ------------------------------------------------------------------ #
    # Immunity
    # ------------------------------------------------------------------ #

    @jail.group(name="immune", description="Manage jail immunity", invoke_without_command=True)
    @commands.guild_only()
    @has_permissions(administrator=True)
    async def jail_immune(self, ctx: commands.Context) -> None:
        """Show the immunity list (bare invocation)."""
        if ctx.invoked_subcommand is None:
            cur = await self.bot.db.execute("SELECT target_id, target_type FROM jail_immune WHERE guild_id = ?", (ctx.guild.id,))
            rows = await cur.fetchall()
            if not rows:
                return await ctx.warn("No immune users or roles. Add one with `,jail immune add <target>`.")
            lines = [f"**{r['target_type']}** — {'<@' + str(r['target_id']) + '>' if r['target_type'] == 'user' else '<@&' + str(r['target_id']) + '>'}" for r in rows]
            await ctx.send(embed=Embed(title="Jail immunity", description="\n".join(lines)[:4000], color=COLORS.neutral))

    @jail_immune.command(name="add", description="Make a user or role immune to jailing")
    @app_commands.describe(target="A user or role to protect")
    async def jail_immune_add(self, ctx: commands.Context, target: discord.Member | discord.Role) -> None:
        """Add a user or role to the immunity list."""
        target_type = "user" if isinstance(target, discord.Member) else "role"
        if isinstance(target, discord.Member) and target.id == ctx.guild.owner_id:
            return await ctx.warn("The owner is always immune — no need to add them.")
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO jail_immune (guild_id, target_id, target_type) VALUES (?, ?, ?)",
            (ctx.guild.id, target.id, target_type),
        )
        await self.bot.db.commit()
        await ctx.approve(f"{target.mention} is now immune to jailing.")

    @jail_immune.command(name="remove", description="Remove a user or role from the immunity list")
    @app_commands.describe(target="The user or role to unprotect")
    async def jail_immune_remove(self, ctx: commands.Context, target: discord.Member | discord.Role) -> None:
        """Remove a user or role from the immunity list."""
        cur = await self.bot.db.execute(
            "DELETE FROM jail_immune WHERE guild_id = ? AND target_id = ?",
            (ctx.guild.id, target.id),
        )
        await self.bot.db.commit()
        if cur.rowcount == 0:
            return await ctx.warn(f"{target.mention} is not on the immunity list.")
        await ctx.approve(f"{target.mention} is no longer immune to jailing.")

    @jail_immune.command(name="list", description="List immune users and roles")
    async def jail_immune_list(self, ctx: commands.Context) -> None:
        """List immune users and roles."""
        cur = await self.bot.db.execute("SELECT target_id, target_type FROM jail_immune WHERE guild_id = ?", (ctx.guild.id,))
        rows = await cur.fetchall()
        if not rows:
            return await ctx.warn("The immunity list is empty.")
        lines = [f"**{r['target_type']}** — {'<@' + str(r['target_id']) + '>' if r['target_type'] == 'user' else '<@&' + str(r['target_id']) + '>'}" for r in rows]
        await ctx.send(embed=Embed(title="Jail immunity", description="\n".join(lines)[:4000], color=COLORS.neutral))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Jail(bot))
