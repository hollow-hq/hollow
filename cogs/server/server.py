import json
import random
import re
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.client.embed import Embed
from core.client.tagscript import TagScriptParser
from core.client.EmbedBuilder import build_kwargs
from core.client.commands import has_permissions, hybrid_command, hybrid_group
from core.config import COLORS
from core.context import hollowHelp

DEFAULT_MESSAGE = "Welcome {user.mention} to **{guild.name}**! You are our {user.join_position_suffix} member."
DEFAULT_LEAVE_MESSAGE = "{user.name} has left **{guild.name}**. We now have {guild.count} members."
DEFAULT_BOOST_MESSAGE = "{user.mention} just **boosted** **{guild.name}**! The server now has **{guild.boost_count}** boost(s). 🎉"
MAX_BOOSTER_LIMIT = 25
MAX_SHARE_LIMIT = 25
ROLE_ICON_MAX_BYTES = 256 * 1024
LOG_CHANNELS = (
    "message-logs",
    "member-logs",
    "server-logs",
    "voice-logs",
    "moderation-logs",
)
LOG_TYPES = {
    "message": "message-logs",
    "member": "member-logs",
    "server": "server-logs",
    "voice": "voice-logs",
    "moderation": "moderation-logs",
}
FAKE_PERMISSION_NAMES = {
    permission: permission.replace("_", " ").title()
    for permission in discord.Permissions.VALID_FLAGS
}

VARIABLES_FIELDS = lambda: [
    ("User Variables", ", ".join(sorted(TagScriptParser.USER_VARS))),
    ("Guild Variables", ", ".join(sorted(TagScriptParser.GUILD_VARS))),
    ("Channel Variables", ", ".join(sorted(TagScriptParser.CHANNEL_VARS))),
    ("Date Variables", ", ".join(sorted(TagScriptParser.DATE_VARS))),
    ("Time Variables", ", ".join(sorted(TagScriptParser.TIME_VARS))),
    (
        "Embed Syntax",
        "`{embed}` then `$v` separated parts: `title:`, `description:`, `color:`, "
        "`image:`, `thumbnail:`, `author:`, `field:`, `footer:`, `content:`, `button:`",
    ),
]


class CleanupRolesView(discord.ui.View):
    def __init__(self, cog, ctx, roles: list[discord.Role]):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx
        self.roles = roles

    @discord.ui.button(label="Delete empty roles", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete_roles(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message(embed=Embed(description=f"❌ {interaction.user.mention}: You're not the author of this menu!", color=COLORS.deny), ephemeral=True)
        deleted = 0
        failed = 0
        for role in self.roles:
            try:
                await role.delete(reason=f"Cleanup empty roles requested by {self.ctx.author}")
                deleted += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
        await interaction.response.edit_message(
            embed=Embed(description=f"✅ Deleted **{deleted}** empty roles." + (f" Failed: {failed}." if failed else ""), color=COLORS.neutral),
            view=None,
        )
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True


class Server(commands.Cog):
    """Server configuration: welcomer, leaver and logging."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self._sticky_last: dict[int, int] = {}

    async def get_config(self, guild_id: int, table: str = "welcome_config") -> dict:
        cursor = await self.bot.db.execute(
            f"SELECT channels, message, dm_enabled FROM {table} WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return {"channels": [], "message": None, "dm_enabled": False}
        return {
            "channels": json.loads(row["channels"] or "[]"),
            "message": row["message"],
            "dm_enabled": bool(row["dm_enabled"]) if "dm_enabled" in row.keys() else False,
        }

    async def set_config(self, guild_id: int, config: dict, table: str = "welcome_config") -> None:
        channels = json.dumps(config["channels"])
        message = config["message"]
        if table == "welcome_config":
            await self.bot.db.execute(
                """
                INSERT INTO welcome_config (guild_id, channels, message, dm_enabled)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channels = excluded.channels,
                    message = excluded.message,
                    dm_enabled = excluded.dm_enabled
                """,
                (guild_id, channels, message, int(config.get("dm_enabled", False))),
            )
        elif table == "leave_config":
            await self.bot.db.execute(
                """
                INSERT INTO leave_config (guild_id, channels, message)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channels = excluded.channels,
                    message = excluded.message
                """,
                (guild_id, channels, message),
            )
        else:
            raise ValueError(f"Unknown config table: {table}")
        await self.bot.db.commit()

    async def get_log_config(self, guild_id: int) -> dict:
        cursor = await self.bot.db.execute(
            "SELECT category_id, channels, enabled FROM log_config WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return {"category_id": None, "channels": {}, "enabled": False}
        return {
            "category_id": row["category_id"],
            "channels": json.loads(row["channels"] or "{}"),
            "enabled": bool(row["enabled"]),
        }

    async def set_log_config(self, guild_id: int, config: dict) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO log_config (guild_id, category_id, channels, enabled)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                category_id = excluded.category_id,
                channels = excluded.channels,
                enabled = excluded.enabled
            """,
            (
                guild_id,
                config.get("category_id"),
                json.dumps(config.get("channels", {})),
                int(config.get("enabled", True)),
            ),
        )
        await self.bot.db.commit()

    async def get_ignore_config(self, guild_id: int) -> dict:
        cursor = await self.bot.db.execute(
            "SELECT users, channels, roles FROM ignore_config WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return {"users": [], "channels": [], "roles": []}
        return {key: json.loads(row[key] or "[]") for key in ("users", "channels", "roles")}

    async def set_ignore_config(self, guild_id: int, config: dict) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO ignore_config (guild_id, users, channels, roles)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                users = excluded.users,
                channels = excluded.channels,
                roles = excluded.roles
            """,
            (
                guild_id,
                json.dumps(config["users"]),
                json.dumps(config["channels"]),
                json.dumps(config["roles"]),
            ),
        )
        await self.bot.db.commit()

    async def get_fake_permissions(self, guild_id: int) -> dict[str, list[str]]:
        cursor = await self.bot.db.execute(
            "SELECT permissions FROM fake_permission_config WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return {}
        try:
            return json.loads(row["permissions"] or "{}")
        except (TypeError, ValueError):
            return {}

    async def set_fake_permissions(self, guild_id: int, permissions: dict[str, list[str]]) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO fake_permission_config (guild_id, permissions)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET permissions = excluded.permissions
            """,
            (guild_id, json.dumps(permissions)),
        )
        await self.bot.db.commit()

    # ------------------------------------------------------------------ #
    # Helpers: sticky / webhook / moderation
    # ------------------------------------------------------------------ #

    def _generate_webhook_identifier(self, length: int = 8) -> str:
        return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))

    async def _get_sticky(self, channel_id: int) -> Optional[dict]:
        cursor = await self.bot.db.execute(
            "SELECT guild_id, channel_id, message, last_message_id, created_by FROM sticky_config WHERE channel_id = ?",
            (channel_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return dict(row)

    async def _set_sticky(self, guild_id: int, channel_id: int, message: str, author_id: int) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO sticky_config (guild_id, channel_id, message, created_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET message = excluded.message, created_by = excluded.created_by
            """,
            (guild_id, channel_id, message, author_id),
        )
        await self.bot.db.commit()

    async def _update_sticky_last(self, channel_id: int, last_message_id: Optional[int]) -> None:
        await self.bot.db.execute(
            "UPDATE sticky_config SET last_message_id = ? WHERE channel_id = ?",
            (last_message_id, channel_id),
        )
        await self.bot.db.commit()
        if last_message_id is None:
            self._sticky_last.pop(channel_id, None)
        else:
            self._sticky_last[channel_id] = last_message_id

    async def _remove_sticky(self, channel_id: int) -> None:
        await self.bot.db.execute("DELETE FROM sticky_config WHERE channel_id = ?", (channel_id,))
        await self.bot.db.commit()
        self._sticky_last.pop(channel_id, None)

    async def _resolve_channel_and_text(self, ctx: commands.Context, text: str) -> tuple[discord.TextChannel, str]:
        """Parse optional channel mention at start or end of text. Returns (channel, message)."""
        if not text:
            return ctx.channel, ""
        # try to detect channel mention/id at start or end
        # split preserving original
        parts = text.strip().split()
        if not parts:
            return ctx.channel, text
        # check first token is channel
        first = parts[0]
        last = parts[-1]
        # helper to try convert token to channel
        def try_channel(token: str) -> Optional[discord.TextChannel]:
            # mention like <#123>
            m = re.match(r"<#(\d+)>", token)
            if m:
                try:
                    cid = int(m.group(1))
                    ch = ctx.guild.get_channel(cid)
                    if isinstance(ch, discord.TextChannel):
                        return ch
                except Exception:
                    pass
            # raw id
            if token.isdigit():
                try:
                    ch = ctx.guild.get_channel(int(token))
                    if isinstance(ch, discord.TextChannel):
                        return ch
                except Exception:
                    pass
            # try converter heuristic: channel name with #?
            if token.startswith("#"):
                name = token[1:]
                ch = discord.utils.get(ctx.guild.text_channels, name=name)
                if ch:
                    return ch
            return None

        ch_first = try_channel(first)
        ch_last = try_channel(last)
        if ch_first and len(parts) > 1:
            # first token is channel, rest is message
            return ch_first, text[len(first):].strip()
        if ch_last and len(parts) > 1:
            return ch_last, text[: -len(last)].strip()
        # fallback: if text contains channel mention anywhere, extract? use converter attempt on whole text startswith?
        return ctx.channel, text

    async def _render_with_tagscript_embed(self, ctx: commands.Context, raw: str) -> dict:
        """Return send kwargs after TagScriptParser + the embed script builder."""
        # Use ctx.author as user for tagscript
        try:
            parsed = await TagScriptParser.parse(raw, ctx.author, ctx.guild, ctx.channel, self.bot)
        except Exception:
            parsed = raw
        return build_kwargs(parsed)

    async def _send_sticky(self, channel: discord.TextChannel, raw_message: str, author: discord.Member) -> Optional[discord.Message]:
        """Render and send sticky message, return sent message."""
        try:
            parsed = await TagScriptParser.parse(raw_message, author, channel.guild, channel, self.bot)
        except Exception:
            parsed = raw_message
        kwargs = build_kwargs(parsed)
        try:
            msg = await channel.send(**kwargs)
            await self._update_sticky_last(channel.id, msg.id)
            return msg
        except (discord.Forbidden, discord.HTTPException):
            return None

    async def _repost_sticky(self, channel: discord.TextChannel) -> None:
        row = await self._get_sticky(channel.id)
        if not row:
            return
        # delete previous sticky message if exists
        last_id = row["last_message_id"]
        if last_id:
            try:
                old = await channel.fetch_message(last_id)
                # only delete if it was our sticky (bot authored)
                if old.author.id == self.bot.user.id:
                    await old.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        # fetch author for rendering (fallback to bot user)
        guild = channel.guild
        author = guild.get_member(row["created_by"]) or guild.me
        await self._send_sticky(channel, row["message"], author)

    async def _parse_message_reference(self, ctx: commands.Context, reference: str) -> Optional[discord.Message]:
        """Parse message link or id to discord.Message."""
        reference = reference.strip()
        # link format: https://discord.com/channels/<guild>/<channel>/<msg>
        m = re.match(r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)", reference)
        if m:
            _, channel_id, message_id = m.groups()
            channel_id = int(channel_id)
            message_id = int(message_id)
            channel = ctx.guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except Exception:
                    return None
            try:
                return await channel.fetch_message(message_id)
            except Exception:
                return None
        # plain id
        if reference.isdigit():
            msg_id = int(reference)
            # try current channel first
            try:
                return await ctx.channel.fetch_message(msg_id)
            except Exception:
                pass
            # search across guild channels
            for ch in ctx.guild.text_channels:
                try:
                    return await ch.fetch_message(msg_id)
                except Exception:
                    continue
        return None

    async def should_ignore(self, guild: discord.Guild, member: Optional[discord.Member] = None,
                            channel: Optional[discord.abc.GuildChannel] = None) -> bool:
        config = await self.get_ignore_config(guild.id)
        if channel and channel.id in config["channels"]:
            return True
        if member:
            if member.id in config["users"]:
                return True
            if any(role.id in config["roles"] for role in member.roles):
                return True
        return False

    async def send_log(self, guild: discord.Guild, log_type: str, embed: Embed) -> None:
        config = await self.get_log_config(guild.id)
        if not config["enabled"]:
            return
        channel_id = config["channels"].get(LOG_TYPES[log_type])
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    def log_embed(self, title: str, description: str, *, color: Optional[discord.Color] = None) -> Embed:
        return Embed(title=title, description=description[:4096], color=color or COLORS.neutral)

    async def audit_actor(self, guild: discord.Guild, action: discord.AuditLogAction,
                          target_id: Optional[int] = None) -> Optional[discord.User]:
        try:
            async for entry in guild.audit_logs(limit=5, action=action, after=discord.utils.utcnow() - timedelta(seconds=15)):
                if target_id is None or getattr(entry.target, "id", None) == target_id:
                    return entry.user
        except (discord.Forbidden, discord.HTTPException):
            pass
        return None

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #

    async def render(self, member: discord.Member, raw: str):
        """Parse tagscript + embed syntax, return send kwargs."""
        parsed = await TagScriptParser.parse(raw, member, member.guild, None, self.bot)
        return build_kwargs(parsed)

    async def send_welcome(self, member: discord.Member) -> None:
        config = await self.get_config(member.guild.id)
        raw = config["message"] or DEFAULT_MESSAGE
        try:
            kwargs = await self.render(member, raw)
        except Exception:
            return

        for channel_id in config["channels"]:
            channel = member.guild.get_channel(channel_id)
            if channel is None:
                continue
            try:
                await channel.send(**kwargs)
            except discord.HTTPException:
                continue

        if config["dm_enabled"]:
            try:
                await member.send(**kwargs)
            except (discord.HTTPException, discord.Forbidden):
                pass

    async def send_leave(self, member: discord.Member) -> None:
        config = await self.get_config(member.guild.id, "leave_config")
        if not config["channels"]:
            return
        raw = config["message"] or DEFAULT_LEAVE_MESSAGE
        try:
            kwargs = await self.render(member, raw)
        except Exception:
            return

        for channel_id in config["channels"]:
            channel = member.guild.get_channel(channel_id)
            if channel is None:
                continue
            try:
                await channel.send(**kwargs)
            except discord.HTTPException:
                continue

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self.send_welcome(member)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await self.send_leave(member)

    # ------------------------------------------------------------------ #
    # Sticky repost listener
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # sticky repost - runs before the logging on_message handler
        # ignore bots and DMs
        if message.guild is None or message.author.bot:
            # still allow logging handler below to run (second listener)
            # we handle by not returning early for sticky only
            pass
        else:
            # do not trigger sticky on the sticky message itself
            row = None
            try:
                cursor = await self.bot.db.execute(
                    "SELECT last_message_id FROM sticky_config WHERE channel_id = ?", (message.channel.id,)
                )
                r = await cursor.fetchone()
                if r and r["last_message_id"] and r["last_message_id"] == message.id:
                    # this is our sticky message, ignore
                    pass
                else:
                    # check if channel has sticky
                    cursor2 = await self.bot.db.execute(
                        "SELECT guild_id FROM sticky_config WHERE channel_id = ?", (message.channel.id,)
                    )
                    has_sticky = await cursor2.fetchone()
                    if has_sticky:
                        # small delay to let other processing happen, then repost
                        # use background task to avoid blocking
                        self.bot.loop.create_task(self._delayed_sticky_repost(message.channel))
            except Exception:
                pass

    async def _delayed_sticky_repost(self, channel: discord.TextChannel):
        # slight delay so the triggering message is visible before sticky
        import asyncio
        await asyncio.sleep(1.2)
        try:
            await self._repost_sticky(channel)
        except Exception:
            pass

    # keep original special message logging as separate listener
    @commands.Cog.listener("on_message")
    async def on_message_log_special(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot or not self.is_special_message(message):
            return
        if await self.should_ignore(message.guild, message.author, message.channel):
            return
        details = []
        if message.attachments:
            details.append(f"Attachments: **{len(message.attachments)}**")
        if message.embeds:
            details.append(f"Embeds: **{len(message.embeds)}**")
        if message.stickers:
            details.append(f"Stickers: **{len(message.stickers)}**")
        if message.components:
            details.append("Components attached")
        if message.poll:
            details.append("Poll created")
        await self.send_log(message.guild, "message", self.log_embed(
            "Special Message Created",
            f"**Author:** {message.author.mention}\n**Channel:** {message.channel.mention}\n"
            f"**Message:** [Jump to message]({message.jump_url})\n" + "\n".join(details),
        ))

    # ------------------------------------------------------------------ #
    # Commands: logging
    # ------------------------------------------------------------------ #

    @commands.hybrid_group(
        name="log",
        aliases=["logging", "logs"],
        description="Manage server logging",
        example=",log aio",
        invoke_without_command=True,
    )
    @commands.guild_only()
    @has_permissions(manage_guild=True)
    async def log(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @log.command(name="aio", aliases=["setup"], description="Create and configure the logging channels", example=",log aio")
    @commands.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def log_aio(self, ctx: commands.Context):
        guild = ctx.guild
        config = await self.get_log_config(guild.id)
        category = guild.get_channel(config["category_id"]) if config["category_id"] else None
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }

        try:
            if not isinstance(category, discord.CategoryChannel):
                category = await guild.create_category("Logs", overwrites=overwrites, reason=f"Logging setup requested by {ctx.author}")
            else:
                await category.edit(overwrites=overwrites, reason=f"Logging setup requested by {ctx.author}")

            channels = {}
            for channel_name in LOG_CHANNELS:
                channel_id = config["channels"].get(channel_name)
                channel = guild.get_channel(channel_id) if channel_id else None
                if not isinstance(channel, discord.TextChannel) or channel.category_id != category.id:
                    channel = discord.utils.get(category.text_channels, name=channel_name)
                if channel is None:
                    channel = await guild.create_text_channel(channel_name, category=category, sync_permissions=True,
                                                             reason=f"Logging setup requested by {ctx.author}")
                channels[channel_name] = channel.id

            await category.edit(position=len(guild.categories) - 1, reason="Place logging category at the bottom")
            config.update({"category_id": category.id, "channels": channels, "enabled": True})
            await self.set_log_config(guild.id, config)
        except discord.Forbidden:
            return await ctx.deny("I need **Manage Channels** and permission to manage the logging category.")
        except discord.HTTPException as error:
            return await ctx.deny(f"Logging setup failed: `{error}`")

        await ctx.approve(f"Logging has been configured in **{category.name}**.")

    @log.group(name="ignore", description="Exclude users, channels, or roles from logs", example=",logs ignore user @user", invoke_without_command=True)
    async def log_ignore(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @log.group(name="unignore", description="Remove logging exclusions", example=",logs unignore user @user", invoke_without_command=True)
    async def log_unignore(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    async def update_ignored(self, ctx: commands.Context, key: str, object_id: int, add: bool) -> None:
        config = await self.get_ignore_config(ctx.guild.id)
        values = config[key]
        if add:
            if object_id in values:
                return await ctx.warn("That item is already ignored by logging.")
            values.append(object_id)
        else:
            if object_id not in values:
                return await ctx.warn("That item is not ignored by logging.")
            values.remove(object_id)
        await self.set_ignore_config(ctx.guild.id, config)
        await ctx.approve("Logging ignore settings have been updated.")

    @log_ignore.command(name="user", description="Ignore a user in logs", example=",logs ignore user @user")
    async def log_ignore_user(self, ctx: commands.Context, user: discord.Member):
        await self.update_ignored(ctx, "users", user.id, True)

    @log_ignore.command(name="channel", description="Ignore a channel in logs", example=",logs ignore channel #channel")
    async def log_ignore_channel(self, ctx: commands.Context, channel: discord.abc.GuildChannel):
        await self.update_ignored(ctx, "channels", channel.id, True)

    @log_ignore.command(name="role", description="Ignore members with a role in logs", example=",logs ignore role @role")
    async def log_ignore_role(self, ctx: commands.Context, role: discord.Role):
        await self.update_ignored(ctx, "roles", role.id, True)

    @log_unignore.command(name="user", description="Stop ignoring a user in logs", example=",logs unignore user @user")
    async def log_unignore_user(self, ctx: commands.Context, user: discord.Member):
        await self.update_ignored(ctx, "users", user.id, False)

    @log_unignore.command(name="channel", description="Stop ignoring a channel in logs", example=",logs unignore channel #channel")
    async def log_unignore_channel(self, ctx: commands.Context, channel: discord.abc.GuildChannel):
        await self.update_ignored(ctx, "channels", channel.id, False)

    @log_unignore.command(name="role", description="Stop ignoring a role in logs", example=",logs unignore role @role")
    async def log_unignore_role(self, ctx: commands.Context, role: discord.Role):
        await self.update_ignored(ctx, "roles", role.id, False)

    @log_ignore.command(name="list", description="View logging exclusions", example=",logs ignore list")
    async def log_ignore_list(self, ctx: commands.Context):
        config = await self.get_ignore_config(ctx.guild.id)
        lines = []
        for key, label in (("users", "Users"), ("channels", "Channels"), ("roles", "Roles")):
            ids = config[key]
            if ids:
                lines.append(f"**{label}:** " + ", ".join(f"<@&{item}" if key == "roles" else f"<#{item}>" if key == "channels" else f"<@{item}>" for item in ids))
        if not lines:
            return await ctx.warn("Nothing is currently ignored by logging.")
        await ctx.embed(title="Logging Ignores", description="\n".join(lines))

    # ------------------------------------------------------------------ #
    # Commands: fake permissions
    # ------------------------------------------------------------------ #

    @commands.hybrid_group(
        name="fakepermissions",
        aliases=["fp", "fakeperms", "fakeperm"],
        description="Manage permissions granted only within hollow",
        example=",fakeperms add @Moderator ban_members",
        invoke_without_command=True,
    )
    @commands.guild_only()
    @has_permissions(administrator=True)
    async def fakepermissions(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @fakepermissions.command(name="add", description="Give a role a fake permission", example=",fakeperms add @Moderator ban_members")
    async def fakepermissions_add(self, ctx: commands.Context, role: discord.Role, permission: str):
        permission = permission.lower().strip()
        if permission not in FAKE_PERMISSION_NAMES:
            return await ctx.warn("That is not a valid fake permission. Use `,fakeperms permissions` to view valid permissions.")
        if role.is_default() or role.managed:
            return await ctx.warn("Choose a regular server role.")

        config = await self.get_fake_permissions(ctx.guild.id)
        permissions = config.setdefault(str(role.id), [])
        if permission in permissions:
            return await ctx.warn(f"{role.mention} already has fake `{permission}`.")
        permissions.append(permission)
        permissions.sort()
        await self.set_fake_permissions(ctx.guild.id, config)
        await ctx.approve(f"Granted fake `{permission}` to {role.mention}.")

    @fakepermissions.command(name="remove", aliases=["delete", "rm"], description="Remove all fake permissions from a role", example=",fakeperms remove @Moderator")
    async def fakepermissions_remove(self, ctx: commands.Context, role: discord.Role):
        config = await self.get_fake_permissions(ctx.guild.id)
        if str(role.id) not in config:
            return await ctx.warn(f"{role.mention} has no fake permissions.")
        del config[str(role.id)]
        await self.set_fake_permissions(ctx.guild.id, config)
        await ctx.approve(f"Removed all fake permissions from {role.mention}.")

    @fakepermissions.command(name="reset", description="Reset every fake permission", example=",fakeperms reset")
    async def fakepermissions_reset(self, ctx: commands.Context):
        config = await self.get_fake_permissions(ctx.guild.id)
        if not config:
            return await ctx.warn("No fake permissions are configured.")
        await self.set_fake_permissions(ctx.guild.id, {})
        await ctx.approve("All fake permissions have been reset.")

    @fakepermissions.command(name="list", description="List all configured fake permissions", example=",fakeperms list")
    async def fakepermissions_list(self, ctx: commands.Context):
        config = await self.get_fake_permissions(ctx.guild.id)
        lines = []
        stale_role_ids = []
        for role_id, permissions in config.items():
            role = ctx.guild.get_role(int(role_id))
            if role is None:
                stale_role_ids.append(role_id)
                continue
            lines.append(f"{role.mention}: " + ", ".join(f"`{permission}`" for permission in permissions))
        if stale_role_ids:
            for role_id in stale_role_ids:
                config.pop(role_id, None)
            await self.set_fake_permissions(ctx.guild.id, config)
        if not lines:
            return await ctx.warn("No fake permissions are configured.")
        await ctx.embed(title="Fake Permissions", description="\n".join(lines)[:4096])

    @fakepermissions.command(name="permissions", aliases=["perms"], description="List valid fake permissions", example=",fakeperms permissions")
    async def fakepermissions_permissions(self, ctx: commands.Context):
        requested = "\n".join(f"`{permission}` - {name}" for permission, name in sorted(FAKE_PERMISSION_NAMES.items()))
        await ctx.embed(title="Valid Fake Permissions", description=requested[:4096])

    # ------------------------------------------------------------------ #
    # Commands: firstmessage
    # ------------------------------------------------------------------ #

    @hybrid_command(name="firstmessage", aliases=["firstmsg"], description="Give link to the first ever message in the channel", example=",firstmessage #general")
    @app_commands.describe(channel="Channel to find the first message in (defaults to current)")
    @commands.guild_only()
    async def firstmessage(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        channel = channel or ctx.channel
        if not isinstance(channel, discord.TextChannel):
            return await ctx.warn("First message can only be fetched from a text channel.")
        # Need read history
        if not channel.permissions_for(ctx.guild.me).read_message_history:
            return await ctx.deny(f"I need **Read Message History** in {channel.mention}.")
        # Inform user we are searching (channel with many messages may take a second)
        try:
            await ctx.channel.typing()
        except Exception:
            pass
        first: Optional[discord.Message] = None
        try:
            async for msg in channel.history(limit=1, oldest_first=True):
                first = msg
                break
        except discord.Forbidden:
            return await ctx.deny(f"I don't have permission to read history in {channel.mention}.")
        except discord.HTTPException as e:
            return await ctx.deny(f"Failed to fetch first message: `{e}`")
        if not first:
            return await ctx.warn(f"No messages found in {channel.mention}.")
        embed = Embed(
            title=f"First message in #{channel.name}",
            description=f"[Jump to message]({first.jump_url})\n\n**Author:** {first.author.mention} (`{first.author}`)\n**Content:** {first.content[:1500] or '*No text content*'}",
            color=COLORS.neutral,
            timestamp=first.created_at,
        )
        if first.attachments:
            embed.add_field(name="Attachments", value=f"{len(first.attachments)} file(s)", inline=True)
        embed.set_footer(text=f"Message ID: {first.id} • {discord.utils.format_dt(first.created_at, 'F')}")
        # Try to show author's avatar
        try:
            embed.set_thumbnail(url=first.author.display_avatar.url)
        except Exception:
            pass
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------ #
    # Logging listeners
    # ------------------------------------------------------------------ #

    def is_special_message(self, message: discord.Message) -> bool:
        return bool(message.attachments or message.embeds or message.stickers or message.components or message.poll)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot or await self.should_ignore(message.guild, message.author, message.channel):
            return
        if message.channel.category_id and message.channel.category_id == (await self.get_log_config(message.guild.id))["category_id"]:
            return
        actor = await self.audit_actor(message.guild, discord.AuditLogAction.message_delete, message.author.id)
        content = message.content or "*No text content*"
        description = f"**Author:** {message.author.mention}\n**Channel:** {message.channel.mention}\n**Content:** {content[:1800]}"
        if actor and actor.id != message.author.id:
            description += f"\n**Deleted by:** {actor.mention}"
        await self.send_log(message.guild, "message", self.log_embed("Message Deleted", description, color=discord.Color.red()))

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if not after.guild or after.author.bot or before.content == after.content:
            return
        if await self.should_ignore(after.guild, after.author, after.channel):
            return
        await self.send_log(after.guild, "message", self.log_embed(
            "Message Edited",
            f"**Author:** {after.author.mention}\n**Channel:** {after.channel.mention}\n"
            f"**Message:** [Jump to message]({after.jump_url})\n"
            f"**Before:** {before.content[:900] or '*No text content*'}\n"
            f"**After:** {after.content[:900] or '*No text content*'}",
            color=discord.Color.orange(),
        ))

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if not payload.guild_id or payload.member is None or payload.member.bot:
            return
        guild = self.bot.get_guild(payload.guild_id)
        channel = guild.get_channel(payload.channel_id) if guild else None
        if not guild or not isinstance(channel, discord.abc.GuildChannel) or await self.should_ignore(guild, payload.member, channel):
            return
        await self.send_log(guild, "message", self.log_embed(
            "Reaction Added",
            f"**User:** {payload.member.mention}\n**Channel:** {channel.mention}\n**Emoji:** {payload.emoji}\n"
            f"**Message:** https://discord.com/channels/{guild.id}/{payload.channel_id}/{payload.message_id}",
        ))

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if not payload.guild_id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        channel = guild.get_channel(payload.channel_id) if guild else None
        member = guild.get_member(payload.user_id) if guild else None
        if not guild or not member or member.bot or not isinstance(channel, discord.abc.GuildChannel) or await self.should_ignore(guild, member, channel):
            return
        await self.send_log(guild, "message", self.log_embed(
            "Reaction Removed",
            f"**User:** {member.mention}\n**Channel:** {channel.mention}\n**Emoji:** {payload.emoji}\n"
            f"**Message:** https://discord.com/channels/{guild.id}/{payload.channel_id}/{payload.message_id}",
        ))

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        # ---- boost transitions (notifications + role lifecycle) ----
        try:
            if not before.premium_since and after.premium_since:
                cfg = await self._get_boost_config(after.guild.id)
                if cfg["channel_id"] and cfg["message"]:
                    channel = after.guild.get_channel(cfg["channel_id"])
                    if isinstance(channel, discord.TextChannel):
                        try:
                            kwargs = await self.render(after, cfg["message"])
                            await channel.send(**kwargs)
                        except (discord.Forbidden, discord.HTTPException):
                            pass
                await self._maybe_give_default_booster_role(after)
            elif before.premium_since and not after.premium_since:
                await self._handle_unboost(after)
        except Exception:
            pass

        # ---- existing logging ----
        if await self.should_ignore(after.guild, after):
            return
        changes = []
        if before.nick != after.nick:
            changes.append(f"**Server name:** `{before.nick or before.name}` -> `{after.nick or after.name}`")
        if before.global_name != after.global_name:
            changes.append(f"**Global name:** `{before.global_name or before.name}` -> `{after.global_name or after.name}`")
        if before.display_avatar != after.display_avatar:
            changes.append("**Avatar:** updated")
        if before.banner != after.banner:
            changes.append("**Banner:** updated")
        if before.timed_out_until != after.timed_out_until:
            state = f"until {discord.utils.format_dt(after.timed_out_until, 'F')}" if after.timed_out_until else "removed"
            await self.send_log(after.guild, "moderation", self.log_embed("Member Timeout Updated", f"**Member:** {after.mention}\n**Timeout:** {state}", color=discord.Color.orange()))
        if changes:
            await self.send_log(after.guild, "member", self.log_embed("Member Updated", f"**Member:** {after.mention}\n" + "\n".join(changes)))

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        await self.send_log(channel.guild, "server", self.log_embed("Channel Created", f"**Channel:** {channel.mention}\n**Type:** {channel.type}"))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        await self.send_log(channel.guild, "server", self.log_embed("Channel Deleted", f"**Name:** `{channel.name}`\n**Type:** {channel.type}", color=discord.Color.red()))

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> None:
        if before.name == after.name and before.category_id == after.category_id and before.position == after.position:
            return
        await self.send_log(after.guild, "server", self.log_embed("Channel Updated", f"**Channel:** {after.mention}\n**Name:** `{before.name}` -> `{after.name}`"))

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        if before.name != after.name:
            await self.send_log(after, "server", self.log_embed("Server Updated", f"**Name:** `{before.name}` -> `{after.name}`"))

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        if before.channel == after.channel or await self.should_ignore(member.guild, member):
            return
        if before.channel is None:
            action = f"joined {after.channel.mention}"
        elif after.channel is None:
            action = f"left {before.channel.mention}"
        else:
            action = f"moved from {before.channel.mention} to {after.channel.mention}"
        await self.send_log(member.guild, "voice", self.log_embed("Voice State Updated", f"**Member:** {member.mention}\n**Action:** {action}"))

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        actor = await self.audit_actor(guild, discord.AuditLogAction.ban, user.id)
        description = f"**User:** {user.mention} (`{user.id}`)"
        if actor:
            description += f"\n**Banned by:** {actor.mention}"
        await self.send_log(guild, "moderation", self.log_embed("Member Banned", description, color=discord.Color.red()))

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        actor = await self.audit_actor(guild, discord.AuditLogAction.unban, user.id)
        description = f"**User:** {user.mention} (`{user.id}`)"
        if actor:
            description += f"\n**Unbanned by:** {actor.mention}"
        await self.send_log(guild, "moderation", self.log_embed("Member Unbanned", description, color=discord.Color.green()))

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]) -> None:
        if not messages or not messages[0].guild:
            return
        guild = messages[0].guild
        channel = messages[0].channel
        if channel.category_id and channel.category_id == (await self.get_log_config(guild.id))["category_id"]:
            return
        await self.send_log(guild, "moderation", self.log_embed("Messages Bulk Deleted", f"**Channel:** {channel.mention}\n**Count:** {len(messages)}", color=discord.Color.red()))

    # ------------------------------------------------------------------ #
    # Commands: welcome
    # ------------------------------------------------------------------ #

    @commands.hybrid_group(
        name="welcome",
        aliases=["welc", "join", "welcomer", "wl"],
        description="Manage the welcomer",
        example=",welcome",
        invoke_without_command=True,
    )
    @has_permissions(manage_guild=True)
    async def welcome(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @welcome.group(name="channel",
            aliases=["ch"],
            description="Manage welcome channels",
            example=",welcome channel set #general")
    @has_permissions(manage_guild=True)
    async def welcome_channel(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @welcome_channel.command(name="set",
            description="Set a welcome channel",
            example=",welcome channel set #welcome")
    @app_commands.describe(channel="The channel to send welcome messages in")
    @has_permissions(manage_guild=True)
    @commands.bot_has_permissions(send_messages=True)
    async def welcome_channel_set(self, ctx: commands.Context, *, channel: Optional[discord.TextChannel] = None):
        channel = channel or ctx.channel
        config = await self.get_config(ctx.guild.id)
        if channel.id in config["channels"]:
            return await ctx.warn(f"**{channel.mention}** is already a welcome channel.")
        config["channels"].append(channel.id)
        await self.set_config(ctx.guild.id, config)
        await ctx.approve(f"Welcome messages will now be sent in {channel.mention}.")

    @welcome_channel.command(name="remove",
            aliases=["delete", "rm"],
            description="Remove a welcome channel",
            example=",welcome channel remove #welcome")
    @app_commands.describe(channel="The welcome channel to remove")
    @has_permissions(manage_guild=True)
    async def welcome_channel_remove(self, ctx: commands.Context, *, channel: Optional[discord.TextChannel] = None):
        channel = channel or ctx.channel
        config = await self.get_config(ctx.guild.id)
        if channel.id not in config["channels"]:
            return await ctx.warn(f"**{channel.mention}** is not a welcome channel.")
        config["channels"].remove(channel.id)
        await self.set_config(ctx.guild.id, config)
        await ctx.approve(f"Removed {channel.mention} from the welcome channels.")

    @welcome_channel.command(name="list",
            description="List all welcome channels",
            example=",welcome channel list")
    @has_permissions(manage_guild=True)
    async def welcome_channel_list(self, ctx: commands.Context):
        config = await self.get_config(ctx.guild.id)
        if not config["channels"]:
            return await ctx.warn("No welcome channels have been set up yet.")
        channels = [
            f"{i}. {ctx.guild.get_channel(cid).mention if ctx.guild.get_channel(cid) else f'`deleted ({cid})`'}"
            for i, cid in enumerate(config["channels"], 1)
        ]
        await ctx.embed(title="Welcome Channels", description="\n".join(channels))

    @welcome.command(name="setup",
            aliases=["aio"],
            description="Setup the welcomer in an interactive wizard",
            example=",welcome setup")
    @has_permissions(manage_guild=True)
    async def welcome_setup(self, ctx: commands.Context):
        embed = Embed(
            title="Welcomer Setup",
            description=(
                "**Step 1.** Select your welcome channel below.\n"
                "**Step 2.** Set your welcome message.\n"
                "**Step 3.** Configure additional options."
            ),
            color=COLORS.neutral,
        )
        await ctx.send(embed=embed, view=Server.SetupView(self, ctx))

    @welcome.command(name="variables",
            aliases=["vars", "tags"],
            description="See the available welcome variables (tagscript)",
            example=",welcome variables")
    async def welcome_variables(self, ctx: commands.Context):
        await ctx.embed(
            title="Welcome Variables",
            fields=[{"name": n, "value": v[:1024] or "N/A"} for n, v in VARIABLES_FIELDS()],
        )

    @welcome.group(name="message",
            aliases=["msg"],
            description="Manage the welcome message",
            example=",welcome message set Welcome {user.mention}!")
    @has_permissions(manage_guild=True)
    async def welcome_message(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @welcome_message.command(name="preview",
            description="See how your welcome message looks",
            example=",welcome message preview")
    @has_permissions(manage_guild=True)
    async def welcome_message_preview(self, ctx: commands.Context):
        config = await self.get_config(ctx.guild.id)
        if not config["message"]:
            return await ctx.warn("No welcome message has been set yet.")
        try:
            kwargs = await self.render(ctx.author, config["message"])
        except Exception as e:
            return await ctx.deny(f"Failed to render the welcome message: `{e}`")
        await ctx.send(**kwargs)

    @welcome_message.command(name="set",
            description="Set a welcome message (Tagscript & embed script supported)",
            example=",welcome message set Welcome {user.mention} to **{guild.name}**!")
    @app_commands.describe(message="The welcome message (Tagscript/embed script supported)")
    @has_permissions(manage_guild=True)
    async def welcome_message_set(self, ctx: commands.Context, *, message: str):
        if len(message) > 4000:
            return await ctx.warn("The welcome message cannot be longer than **4000** characters.")
        try:
            await self.render(ctx.author, message)
        except Exception as e:
            return await ctx.deny(f"That message could not be parsed: `{e}`")
        config = await self.get_config(ctx.guild.id)
        config["message"] = message
        await self.set_config(ctx.guild.id, config)
        await ctx.approve("The welcome message has been **updated**.")

    @welcome_message.command(name="remove",
            aliases=["delete", "rm", "reset"],
            description="Remove the welcome message",
            example=",welcome message remove")
    @has_permissions(manage_guild=True)
    async def welcome_message_remove(self, ctx: commands.Context):
        config = await self.get_config(ctx.guild.id)
        if not config["message"]:
            return await ctx.warn("There is no welcome message set.")
        config["message"] = None
        await self.set_config(ctx.guild.id, config)
        await ctx.approve("The welcome message has been **removed**.")

    @welcome.group(name="dm",
            aliases=["dms"],
            description="Manage welcome DMs",
            example=",welcome dm enable")
    @has_permissions(manage_guild=True)
    async def welcome_dm(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @welcome_dm.command(name="status",
            description="Check if DM welcomes are enabled",
            example=",welcome dm status")
    @has_permissions(manage_guild=True)
    async def welcome_dm_status(self, ctx: commands.Context):
        config = await self.get_config(ctx.guild.id)
        state = "enabled" if config["dm_enabled"] else "disabled"
        await ctx.approve(f"Welcome DMs are currently **{state}**.")

    @welcome_dm.command(name="enable",
            aliases=["on"],
            description="Enable DM welcomes",
            example=",welcome dm enable")
    @has_permissions(manage_guild=True)
    async def welcome_dm_enable(self, ctx: commands.Context):
        config = await self.get_config(ctx.guild.id)
        if config["dm_enabled"]:
            return await ctx.warn("Welcome DMs are already **enabled**.")
        config["dm_enabled"] = True
        await self.set_config(ctx.guild.id, config)
        await ctx.approve("Welcome DMs have been **enabled**.")

    @welcome_dm.command(name="disable",
            aliases=["off"],
            description="Disable DM welcomes",
            example=",welcome dm disable")
    @has_permissions(manage_guild=True)
    async def welcome_dm_disable(self, ctx: commands.Context):
        config = await self.get_config(ctx.guild.id)
        if not config["dm_enabled"]:
            return await ctx.warn("Welcome DMs are already **disabled**.")
        config["dm_enabled"] = False
        await self.set_config(ctx.guild.id, config)
        await ctx.approve("Welcome DMs have been **disabled**.")

    # ------------------------------------------------------------------ #
    # Welcomer setup wizard
    # ------------------------------------------------------------------ #

    class SetupWizard(discord.ui.Modal, title="Welcomer Setup - Final Step"):
        """Step 3: additional config."""

        def __init__(self, cog, channel_id: int, message: str):
            super().__init__(timeout=180)
            self.cog = cog
            self.channel_id = channel_id
            self.message = message
            self.dm = discord.ui.TextInput(
                label="Enable DM welcomes? (yes/no)",
                placeholder="no",
                required=False,
                max_length=3,
            )
            self.add_item(self.dm)

        async def on_submit(self, interaction: discord.Interaction):
            config = await self.cog.get_config(interaction.guild_id)
            config["channels"] = [self.channel_id]
            config["message"] = self.message
            config["dm_enabled"] = self.dm.value.strip().lower() in ("y", "yes", "1", "true", "on")
            await self.cog.set_config(interaction.guild_id, config)
            await interaction.response.send_message(
                embed=Embed(
                    description="✅ Welcomer has been **set up** successfully!",
                    color=COLORS.neutral,
                ),
                ephemeral=True,
            )

    class MessageModal(discord.ui.Modal, title="Welcomer Setup - Message"):
        def __init__(self, cog, channel_id: int):
            super().__init__(timeout=300)
            self.cog = cog
            self.channel_id = channel_id
            self.message = discord.ui.TextInput(
                label="Welcome message (Tagscript/Embed supported)",
                style=discord.TextStyle.paragraph,
                default=DEFAULT_MESSAGE,
                max_length=2000,
            )
            self.add_item(self.message)

        async def on_submit(self, interaction: discord.Interaction):
            await interaction.response.send_modal(
                Server.SetupWizard(self.cog, self.channel_id, self.message.value)
            )

    class ChannelSelect(discord.ui.ChannelSelect):
        def __init__(self, cog, ctx):
            super().__init__(
                placeholder="Select your welcome channel...",
                channel_types=[discord.ChannelType.text],
            )
            self.cog = cog
            self.ctx = ctx

        async def callback(self, interaction: discord.Interaction):
            channel = interaction.data["values"][0]
            await interaction.response.send_modal(Server.MessageModal(self.cog, int(channel)))

    class SetupView(discord.ui.View):
        def __init__(self, cog, ctx):
            super().__init__(timeout=120)
            self.cog = cog
            self.ctx = ctx
            self.add_item(Server.ChannelSelect(cog, ctx))

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if interaction.user.id != self.ctx.author.id:
                await interaction.warn("You're not the **author** of this menu!")
                return False
            return True

    # ------------------------------------------------------------------ #
    # Leaver setup wizard
    # ------------------------------------------------------------------ #

    class LeaveSetupWizard(discord.ui.Modal, title="Leaver Setup - Message"):
        def __init__(self, cog, channel_id: int):
            super().__init__(timeout=300)
            self.cog = cog
            self.channel_id = channel_id
            self.message = discord.ui.TextInput(
                label="Leave message (Tagscript/Embed supported)",
                style=discord.TextStyle.paragraph,
                default=DEFAULT_LEAVE_MESSAGE,
                max_length=2000,
            )
            self.add_item(self.message)

        async def on_submit(self, interaction: discord.Interaction):
            config = await self.cog.get_config(interaction.guild_id, "leave_config")
            config["channels"] = [self.channel_id]
            config["message"] = self.message.value
            await self.cog.set_config(interaction.guild_id, config, "leave_config")
            await interaction.response.send_message(
                embed=Embed(
                    description="✅ Leaver has been **set up** successfully!",
                    color=COLORS.neutral,
                ),
                ephemeral=True,
            )

    class LeaveChannelSelect(discord.ui.ChannelSelect):
        def __init__(self, cog, ctx):
            super().__init__(
                placeholder="Select your leave channel...",
                channel_types=[discord.ChannelType.text],
            )
            self.cog = cog
            self.ctx = ctx

        async def callback(self, interaction: discord.Interaction):
            channel = interaction.data["values"][0]
            await interaction.response.send_modal(Server.LeaveSetupWizard(self.cog, int(channel)))

    class LeaveSetupView(discord.ui.View):
        def __init__(self, cog, ctx):
            super().__init__(timeout=120)
            self.cog = cog
            self.ctx = ctx
            self.add_item(Server.LeaveChannelSelect(cog, ctx))

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if interaction.user.id != self.ctx.author.id:
                await interaction.warn("You're not the **author** of this menu!")
                return False
            return True

    # ------------------------------------------------------------------ #
    # Commands: leaver
    # ------------------------------------------------------------------ #

    @commands.hybrid_group(
        name="leaver",
        aliases=["leave"],
        description="Manage the leaver",
        example=",leaver",
        invoke_without_command=True,
    )
    @has_permissions(manage_guild=True)
    async def leaver(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @leaver.group(name="channel",
            aliases=["ch"],
            description="Manage leave channels",
            example=",leaver channel set #goodbye")
    @has_permissions(manage_guild=True)
    async def leaver_channel(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @leaver_channel.command(name="set",
            description="Set a leave channel",
            example=",leaver channel set #goodbye")
    @app_commands.describe(channel="The channel to send leave messages in")
    @has_permissions(manage_guild=True)
    @commands.bot_has_permissions(send_messages=True)
    async def leaver_channel_set(self, ctx: commands.Context, *, channel: Optional[discord.TextChannel] = None):
        channel = channel or ctx.channel
        config = await self.get_config(ctx.guild.id, "leave_config")
        if channel.id in config["channels"]:
            return await ctx.warn(f"**{channel.mention}** is already a leave channel.")
        config["channels"].append(channel.id)
        await self.set_config(ctx.guild.id, config, "leave_config")
        await ctx.approve(f"Leave messages will now be sent in {channel.mention}.")

    @leaver_channel.command(name="remove",
            aliases=["delete", "rm"],
            description="Remove a leave channel",
            example=",leaver channel remove #goodbye")
    @app_commands.describe(channel="The leave channel to remove")
    @has_permissions(manage_guild=True)
    async def leaver_channel_remove(self, ctx: commands.Context, *, channel: Optional[discord.TextChannel] = None):
        channel = channel or ctx.channel
        config = await self.get_config(ctx.guild.id, "leave_config")
        if channel.id not in config["channels"]:
            return await ctx.warn(f"**{channel.mention}** is not a leave channel.")
        config["channels"].remove(channel.id)
        await self.set_config(ctx.guild.id, config, "leave_config")
        await ctx.approve(f"Removed {channel.mention} from the leave channels.")

    @leaver_channel.command(name="list",
            description="List all leave channels",
            example=",leaver channel list")
    @has_permissions(manage_guild=True)
    async def leaver_channel_list(self, ctx: commands.Context):
        config = await self.get_config(ctx.guild.id, "leave_config")
        if not config["channels"]:
            return await ctx.warn("No leave channels have been set up yet.")
        channels = [
            f"{i}. {ctx.guild.get_channel(cid).mention if ctx.guild.get_channel(cid) else f'`deleted ({cid})`'}"
            for i, cid in enumerate(config["channels"], 1)
        ]
        await ctx.embed(title="Leave Channels", description="\n".join(channels))

    @leaver.command(name="setup",
            aliases=["aio"],
            description="Setup the leaver in an interactive wizard",
            example=",leaver setup")
    @has_permissions(manage_guild=True)
    async def leaver_setup(self, ctx: commands.Context):
        embed = Embed(
            title="Leaver Setup",
            description=(
                "**Step 1.** Select your leave channel below.\n"
                "**Step 2.** Set your leave message."
            ),
            color=COLORS.neutral,
        )
        await ctx.send(embed=embed, view=Server.LeaveSetupView(self, ctx))

    @leaver.command(name="variables",
            aliases=["vars", "tags"],
            description="See the available leave variables (tagscript)",
            example=",leaver variables")
    async def leaver_variables(self, ctx: commands.Context):
        await ctx.embed(
            title="Leave Variables",
            fields=[{"name": n, "value": v[:1024] or "N/A"} for n, v in VARIABLES_FIELDS()],
        )

    @leaver.group(name="message",
            aliases=["msg"],
            description="Manage the leave message",
            example=",leaver message set {user.name} has left!")
    @has_permissions(manage_guild=True)
    async def leaver_message(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @leaver_message.command(name="preview",
            description="See how your leave message looks",
            example=",leaver message preview")
    @has_permissions(manage_guild=True)
    async def leaver_message_preview(self, ctx: commands.Context):
        config = await self.get_config(ctx.guild.id, "leave_config")
        if not config["message"]:
            return await ctx.warn("No leave message has been set yet.")
        try:
            kwargs = await self.render(ctx.author, config["message"])
        except Exception as e:
            return await ctx.deny(f"Failed to render the leave message: `{e}`")
        await ctx.send(**kwargs)

    @leaver_message.command(name="set",
            description="Set a leave message (Tagscript & embed script supported)",
            example=",leaver message set {user.name} has left **{guild.name}**!")
    @app_commands.describe(message="The leave message (Tagscript/embed script supported)")
    @has_permissions(manage_guild=True)
    async def leaver_message_set(self, ctx: commands.Context, *, message: str):
        if len(message) > 4000:
            return await ctx.warn("The leave message cannot be longer than **4000** characters.")
        try:
            await self.render(ctx.author, message)
        except Exception as e:
            return await ctx.deny(f"That message could not be parsed: `{e}`")
        config = await self.get_config(ctx.guild.id, "leave_config")
        config["message"] = message
        await self.set_config(ctx.guild.id, config, "leave_config")
        await ctx.approve("The leave message has been **updated**.")

    @leaver_message.command(name="remove",
            aliases=["delete", "rm", "reset"],
            description="Remove the leave message",
            example=",leaver message remove")
    @has_permissions(manage_guild=True)
    async def leaver_message_remove(self, ctx: commands.Context):
        config = await self.get_config(ctx.guild.id, "leave_config")
        if not config["message"]:
            return await ctx.warn("There is no leave message set.")
        config["message"] = None
        await self.set_config(ctx.guild.id, config, "leave_config")
        await ctx.approve("The leave message has been **removed**.")

    # ------------------------------------------------------------------ #
    # Commands: sticky
    # ------------------------------------------------------------------ #

    @commands.hybrid_group(
        name="sticky",
        description="Manage sticky messages",
        example=",sticky set Hello",
        invoke_without_command=True,
    )
    @commands.guild_only()
    async def sticky(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @sticky.command(name="set", description="Set a sticky message", example=",sticky set Hello world")
    @app_commands.describe(message="The sticky message (Tagscript & Embed supported)", channel="Channel to set sticky in (defaults to current)")
    @has_permissions(manage_messages=True)
    @commands.bot_has_permissions(send_messages=True, manage_messages=True)
    async def sticky_set(self, ctx: commands.Context, *, message: str, channel: Optional[discord.TextChannel] = None):
        # support: message may contain trailing channel mention - hybrid will pass message as whole if channel not supplied via slash
        # for prefix, allow ",sticky set hello #channel"
        if channel is None and message:
            # try to detect channel inside message tail
            maybe_channel, maybe_text = await self._resolve_channel_and_text(ctx, message)
            if maybe_channel.id != ctx.channel.id:
                channel = maybe_channel
                message = maybe_text
        channel = channel or ctx.channel
        if not isinstance(channel, discord.TextChannel):
            return await ctx.warn("Sticky can only be set in a text channel.")
        if len(message) > 4000:
            return await ctx.warn("Sticky message cannot be longer than **4000** characters.")
        # validate render
        try:
            parsed = await TagScriptParser.parse(message, ctx.author, ctx.guild, channel, self.bot)
            build_kwargs(parsed)
        except Exception as e:
            return await ctx.deny(f"That message could not be parsed: `{e}`")
        # delete previous sticky message if exists
        old = await self._get_sticky(channel.id)
        if old and old["last_message_id"]:
            try:
                m = await channel.fetch_message(old["last_message_id"])
                if m.author.id == self.bot.user.id:
                    await m.delete()
            except Exception:
                pass
        await self._set_sticky(ctx.guild.id, channel.id, message, ctx.author.id)
        # send new sticky immediately
        msg = await self._send_sticky(channel, message, ctx.author)
        if msg:
            await ctx.approve(f"Sticky set in {channel.mention}.")
        else:
            await ctx.approve(f"Sticky saved for {channel.mention} (failed to send, will retry on next message).")

    @sticky.command(name="list", description="List all sticky messages", example=",sticky list")
    @has_permissions(manage_messages=True)
    async def sticky_list(self, ctx: commands.Context):
        cursor = await self.bot.db.execute(
            "SELECT channel_id, message, last_message_id FROM sticky_config WHERE guild_id = ?", (ctx.guild.id,)
        )
        rows = await cursor.fetchall()
        if not rows:
            return await ctx.warn("No sticky messages are set.")
        lines = []
        for row in rows:
            ch = ctx.guild.get_channel(row["channel_id"])
            ch_mention = ch.mention if ch else f"`deleted ({row['channel_id']})`"
            preview = row["message"][:60].replace("\n", " ")
            if len(row["message"]) > 60:
                preview += "…"
            lines.append(f"{ch_mention} — `{preview}`")
        await ctx.embed(title="Sticky Messages", description="\n".join(lines)[:4096])

    @sticky.command(name="edit", description="Edit a sticky message", example=",sticky edit New content")
    @app_commands.describe(new_message="The new sticky message", channel="Channel of the sticky to edit")
    @has_permissions(manage_messages=True)
    @commands.bot_has_permissions(send_messages=True, manage_messages=True)
    async def sticky_edit(self, ctx: commands.Context, *, new_message: str, channel: Optional[discord.TextChannel] = None):
        if channel is None and new_message:
            maybe_channel, maybe_text = await self._resolve_channel_and_text(ctx, new_message)
            if maybe_channel.id != ctx.channel.id:
                channel = maybe_channel
                new_message = maybe_text
        channel = channel or ctx.channel
        row = await self._get_sticky(channel.id)
        if not row:
            return await ctx.warn(f"No sticky is set in {channel.mention}. Use `,sticky set` first.")
        if len(new_message) > 4000:
            return await ctx.warn("Sticky message cannot be longer than **4000** characters.")
        try:
            parsed = await TagScriptParser.parse(new_message, ctx.author, ctx.guild, channel, self.bot)
            build_kwargs(parsed)
        except Exception as e:
            return await ctx.deny(f"That message could not be parsed: `{e}`")
        await self.bot.db.execute(
            "UPDATE sticky_config SET message = ? WHERE channel_id = ?", (new_message, channel.id)
        )
        await self.bot.db.commit()
        # repost: delete old and send new
        if row["last_message_id"]:
            try:
                old = await channel.fetch_message(row["last_message_id"])
                if old.author.id == self.bot.user.id:
                    await old.delete()
            except Exception:
                pass
        msg = await self._send_sticky(channel, new_message, ctx.author)
        if msg:
            await ctx.approve(f"Sticky in {channel.mention} has been **updated**.")
        else:
            await ctx.approve(f"Sticky in {channel.mention} updated (will appear on next message).")

    @sticky.command(name="remove", aliases=["delete", "rm"], description="Remove a sticky message", example=",sticky remove")
    @app_commands.describe(channel="Channel to remove sticky from")
    @has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def sticky_remove(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        channel = channel or ctx.channel
        row = await self._get_sticky(channel.id)
        if not row:
            return await ctx.warn(f"No sticky is set in {channel.mention}.")
        # delete last sticky message
        if row["last_message_id"]:
            try:
                old = await channel.fetch_message(row["last_message_id"])
                if old.author.id == self.bot.user.id:
                    await old.delete()
            except Exception:
                pass
        await self._remove_sticky(channel.id)
        await ctx.approve(f"Sticky removed from {channel.mention}.")

    # ------------------------------------------------------------------ #
    # Commands: admin
    # ------------------------------------------------------------------ #

    @commands.hybrid_group(
        name="admin",
        description="Server administration utilities",
        example=",admin viewwarnings @user",
        invoke_without_command=True,
    )
    @commands.guild_only()
    @has_permissions(manage_messages=True)
    async def admin(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @admin.command(name="viewwarnings", description="View warnings about a user", example=",admin viewwarnings @user")
    @app_commands.describe(user="The user to view warnings for")
    @has_permissions(moderate_members=True)
    async def admin_viewwarnings(self, ctx: commands.Context, user: discord.Member):
        cursor = await self.bot.db.execute(
            "SELECT id, reason, moderator_id, created_at FROM warnings WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC",
            (ctx.guild.id, user.id),
        )
        rows = await cursor.fetchall()
        if not rows:
            return await ctx.warn(f"No warnings found for {user.mention}.")
        lines = []
        for r in rows:
            mod = f"<@{r['moderator_id']}>" if r["moderator_id"] else "Unknown"
            reason = r["reason"] or "No reason"
            created = r["created_at"]
            lines.append(f"`#{r['id']}` {reason} — by {mod} • {created}")
        embed = Embed(title=f"Warnings — {user}", description="\n".join(lines)[:4096], color=COLORS.neutral)
        embed.set_thumbnail(url=user.display_avatar.url)
        await ctx.send(embed=embed)

    # alias top-level commands: ,viewwarnings and ,warnings
    @hybrid_command(name="viewwarnings", aliases=["warnings"], description="View warnings about a user", example=",viewwarnings @user")
    @app_commands.describe(user="The user to view warnings for")
    @commands.guild_only()
    @has_permissions(moderate_members=True)
    async def viewwarnings(self, ctx: commands.Context, user: discord.Member):
        await self.admin_viewwarnings(ctx, user)

    @admin.group(name="notes", description="Keep notes about users", example=",admin notes list", invoke_without_command=True)
    @has_permissions(manage_messages=True)
    async def admin_notes(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @admin_notes.command(name="add", description="Keep a note about this user", example=",admin notes add @user Spamming")
    @app_commands.describe(user="The user to note", note="The note content")
    @has_permissions(manage_messages=True)
    async def admin_notes_add(self, ctx: commands.Context, user: discord.Member, *, note: Optional[str] = None):
        if not note:
            return await ctx.warn("Please provide a note. Example: `,admin notes add @user Spamming in general`")
        if len(note) > 2000:
            return await ctx.warn("Note cannot be longer than **2000** characters.")
        await self.bot.db.execute(
            "INSERT INTO admin_notes (guild_id, user_id, author_id, note) VALUES (?, ?, ?, ?)",
            (ctx.guild.id, user.id, ctx.author.id, note),
        )
        await self.bot.db.commit()
        await ctx.approve(f"Note added for {user.mention}.")

    @admin_notes.command(name="remove", aliases=["delete", "rm"], description="Remove a note about a user", example=",admin notes remove @user 1")
    @app_commands.describe(user="The user whose note to remove", index="Note index or ID to remove (use list to see IDs)")
    @has_permissions(manage_messages=True)
    async def admin_notes_remove(self, ctx: commands.Context, user: discord.Member, index: Optional[int] = None):
        if index is None:
            # remove last note for user if no index
            cursor = await self.bot.db.execute(
                "SELECT id FROM admin_notes WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1",
                (ctx.guild.id, user.id),
            )
            row = await cursor.fetchone()
            if not row:
                return await ctx.warn(f"No notes found for {user.mention}.")
            index = row["id"]
            # treat as ID
            await self.bot.db.execute("DELETE FROM admin_notes WHERE id = ? AND guild_id = ?", (index, ctx.guild.id))
            await self.bot.db.commit()
            return await ctx.approve(f"Removed latest note for {user.mention} (`#{index}`).")
        # try treat index as note ID first, fallback to positional index
        cursor = await self.bot.db.execute(
            "SELECT id FROM admin_notes WHERE guild_id = ? AND user_id = ? AND id = ?",
            (ctx.guild.id, user.id, index),
        )
        row = await cursor.fetchone()
        if row:
            await self.bot.db.execute("DELETE FROM admin_notes WHERE id = ?", (index,))
            await self.bot.db.commit()
            return await ctx.approve(f"Removed note `#{index}` for {user.mention}.")
        # treat as 1-based positional index
        cursor = await self.bot.db.execute(
            "SELECT id FROM admin_notes WHERE guild_id = ? AND user_id = ? ORDER BY id ASC",
            (ctx.guild.id, user.id),
        )
        rows = await cursor.fetchall()
        if not rows or index < 1 or index > len(rows):
            return await ctx.warn(f"Invalid index. {user.mention} has **{len(rows)}** note(s). Use `,admin notes list`.")
        note_id = rows[index - 1]["id"]
        await self.bot.db.execute("DELETE FROM admin_notes WHERE id = ?", (note_id,))
        await self.bot.db.commit()
        await ctx.approve(f"Removed note **{index}** (`#{note_id}`) for {user.mention}.")

    @admin_notes.command(name="list", description="List all notes", example=",admin notes list")
    @has_permissions(manage_messages=True)
    async def admin_notes_list(self, ctx: commands.Context):
        cursor = await self.bot.db.execute(
            "SELECT user_id, note, author_id, created_at, id FROM admin_notes WHERE guild_id = ? ORDER BY created_at DESC LIMIT 25",
            (ctx.guild.id,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return await ctx.warn("No notes have been saved yet.")
        lines = []
        for r in rows:
            user = ctx.guild.get_member(r["user_id"])
            user_str = user.mention if user else f"`{r['user_id']}`"
            preview = r["note"][:50].replace("\n", " ")
            if len(r["note"]) > 50:
                preview += "…"
            lines.append(f"`#{r['id']}` {user_str} — {preview} • <@{r['author_id']}>")
        await ctx.embed(title="Admin Notes (recent 25)", description="\n".join(lines)[:4096])

    @admin_notes.command(name="view", description="View notes about a user", example=",admin notes view @user")
    @app_commands.describe(user="The user whose notes to view")
    @has_permissions(manage_messages=True)
    async def admin_notes_view(self, ctx: commands.Context, user: discord.Member):
        cursor = await self.bot.db.execute(
            "SELECT id, note, author_id, created_at FROM admin_notes WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC",
            (ctx.guild.id, user.id),
        )
        rows = await cursor.fetchall()
        if not rows:
            return await ctx.warn(f"No notes found for {user.mention}.")
        embeds = []
        for r in rows:
            author = ctx.guild.get_member(r["author_id"]) or self.bot.get_user(r["author_id"])
            author_str = str(author) if author else f"ID {r['author_id']}"
            author_icon = author.display_avatar.url if author and getattr(author, "display_avatar", None) else None
            embed = Embed(
                title=f"Note #{r['id']} — {user}",
                description=r["note"][:4096],
                color=COLORS.neutral,
                timestamp=discord.utils.parse_time(r["created_at"]) if isinstance(r["created_at"], str) else None,
            )
            embed.set_footer(text=f"By {author_str} • {r['created_at']}", icon_url=author_icon)
            embed.set_thumbnail(url=user.display_avatar.url)
            embeds.append(embed)
        if len(embeds) == 1:
            await ctx.send(embed=embeds[0])
        else:
            await ctx.paginate(embeds)

    @admin.group(name="cleanup", description="Clean up unused roles, channels and bots", example=",admin cleanup roles", invoke_without_command=True)
    @has_permissions(manage_guild=True)
    async def admin_cleanup(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @admin_cleanup.command(name="channels", description="Find channels with no recent activity", example=",admin cleanup channels")
    @has_permissions(manage_guild=True)
    @commands.bot_has_permissions(read_message_history=True)
    async def admin_cleanup_channels(self, ctx: commands.Context):
        await ctx.send(embed=Embed(description="🔍 Scanning channels for inactivity…", color=COLORS.neutral))
        inactive = []
        # consider 30 days threshold
        threshold = datetime.now(timezone.utc) - timedelta(days=30)
        for channel in ctx.guild.text_channels:
            try:
                # try last_message_id / history
                if channel.last_message_id is None:
                    inactive.append(channel)
                    continue
                # attempt to fetch last message via history
                last = None
                async for msg in channel.history(limit=1):
                    last = msg
                    break
                if last is None:
                    inactive.append(channel)
                elif last.created_at < threshold:
                    inactive.append(channel)
            except (discord.Forbidden, discord.HTTPException):
                continue
        if not inactive:
            return await ctx.approve("No inactive channels found (all had activity in the last 30 days).")
        lines = [f"{ch.mention} — last activity: unknown/no messages" if not ch.last_message_id else f"{ch.mention} (id: {ch.id})" for ch in inactive[:25]]
        desc = "\n".join(lines)
        if len(inactive) > 25:
            desc += f"\n…and **{len(inactive)-25}** more."
        await ctx.embed(title=f"Inactive Channels ({len(inactive)})", description=desc[:4096])

    @admin_cleanup.command(name="roles", description="Find and remove roles with no members", example=",admin cleanup roles")
    @has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def admin_cleanup_roles(self, ctx: commands.Context):
        empty_roles = [r for r in ctx.guild.roles if len(r.members) == 0 and not r.is_default() and not r.managed and not r.is_premium_subscriber()]
        if not empty_roles:
            return await ctx.warn("No empty roles found.")
        lines = [f"{r.mention} — `{r.name}` ({r.id})" for r in empty_roles[:20]]
        desc = "\n".join(lines)
        if len(empty_roles) > 20:
            desc += f"\n…and **{len(empty_roles)-20}** more."
        embed = Embed(title=f"Empty Roles ({len(empty_roles)})", description=desc[:4096], color=COLORS.neutral)
        view = CleanupRolesView(self, ctx, empty_roles)
        await ctx.send(embed=embed, view=view)

    @admin_cleanup.command(name="duplicates", description="Find duplicate roles and channels", example=",admin cleanup duplicates")
    @has_permissions(manage_guild=True)
    async def admin_cleanup_duplicates(self, ctx: commands.Context):
        # duplicate roles by lower name
        seen_roles: dict[str, list[discord.Role]] = {}
        for r in ctx.guild.roles:
            if r.is_default():
                continue
            key = r.name.lower().strip()
            seen_roles.setdefault(key, []).append(r)
        dup_roles = {k: v for k, v in seen_roles.items() if len(v) > 1}
        seen_channels: dict[str, list[discord.abc.GuildChannel]] = {}
        for ch in ctx.guild.channels:
            key = ch.name.lower().strip()
            seen_channels.setdefault(key, []).append(ch)
        dup_channels = {k: v for k, v in seen_channels.items() if len(v) > 1}
        if not dup_roles and not dup_channels:
            return await ctx.approve("No duplicate roles or channels found.")
        embeds = []
        if dup_roles:
            lines = []
            for name, roles in list(dup_roles.items())[:15]:
                mentions = ", ".join(f"{r.mention} (`{r.id}`)" for r in roles)
                lines.append(f"**{name}**: {mentions}")
            embeds.append(Embed(title=f"Duplicate Roles ({len(dup_roles)})", description="\n".join(lines)[:4096], color=COLORS.neutral))
        if dup_channels:
            lines = []
            for name, channels in list(dup_channels.items())[:15]:
                mentions = ", ".join(f"{c.mention} (`{c.id}`)" for c in channels)
                lines.append(f"**{name}**: {mentions}")
            embeds.append(Embed(title=f"Duplicate Channels ({len(dup_channels)})", description="\n".join(lines)[:4096], color=COLORS.neutral))
        if len(embeds) == 1:
            await ctx.send(embed=embeds[0])
        else:
            await ctx.paginate(embeds)

    @admin_cleanup.command(name="bots", description="List all bots and what they can do", example=",admin cleanup bots")
    @has_permissions(manage_guild=True)
    async def admin_cleanup_bots(self, ctx: commands.Context):
        bots = [m for m in ctx.guild.members if m.bot]
        if not bots:
            return await ctx.warn("No bots found in this server.")
        lines = []
        for bot_member in bots[:25]:
            perms = []
            gp = bot_member.guild_permissions
            # highlight dangerous perms
            for perm, value in gp:
                if value:
                    perms.append(perm.replace("_", " ").title())
            # shorten
            perm_str = ", ".join(perms[:6]) if perms else "No special perms"
            if len(perms) > 6:
                perm_str += f" +{len(perms)-6} more"
            lines.append(f"{bot_member.mention} `{bot_member.name}` — {perm_str}")
        desc = "\n".join(lines)
        if len(bots) > 25:
            desc += f"\n…and **{len(bots)-25}** more bots."
        await ctx.embed(title=f"Bots ({len(bots)})", description=desc[:4096])

    @admin.command(name="history", description="View complete moderation history for a user", example=",admin history @user")
    @app_commands.describe(user="The user to view history for")
    @has_permissions(moderate_members=True)
    async def admin_history(self, ctx: commands.Context, user: discord.Member):
        # warnings
        cursor = await self.bot.db.execute(
            "SELECT reason, created_at, moderator_id FROM warnings WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC LIMIT 10",
            (ctx.guild.id, user.id),
        )
        warns = await cursor.fetchall()
        # mod_history
        cursor = await self.bot.db.execute(
            "SELECT action, reason, created_at, moderator_id FROM mod_history WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC LIMIT 10",
            (ctx.guild.id, user.id),
        )
        mods = await cursor.fetchall()
        # notes
        cursor = await self.bot.db.execute(
            "SELECT note, created_at, author_id FROM admin_notes WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC LIMIT 10",
            (ctx.guild.id, user.id),
        )
        notes = await cursor.fetchall()
        if not warns and not mods and not notes:
            return await ctx.warn(f"No moderation history found for {user.mention}.")
        embeds = []
        if warns:
            desc = "\n".join(f"• {r['reason'] or 'No reason'} — <@{r['moderator_id']}> • {r['created_at']}" for r in warns)
            embeds.append(Embed(title="Warnings", description=desc[:4096], color=COLORS.neutral))
        if mods:
            desc = "\n".join(f"• **{r['action']}** — {r['reason'] or 'No reason'} • <@{r['moderator_id']}> • {r['created_at']}" for r in mods)
            embeds.append(Embed(title="Mod Actions", description=desc[:4096], color=COLORS.warn))
        if notes:
            desc = "\n".join(f"• {r['note'][:80]} — <@{r['author_id']}> • {r['created_at']}" for r in notes)
            embeds.append(Embed(title="Notes", description=desc[:4096], color=COLORS.neutral))
        # audit fallback for kicks/bans/timeouts if DB empty
        if not mods:
            # try audit logs
            audit_lines = []
            for action, label in [(discord.AuditLogAction.kick, "Kick"), (discord.AuditLogAction.ban, "Ban"), (discord.AuditLogAction.member_update, "Timeout/Update")]:
                try:
                    async for entry in ctx.guild.audit_logs(limit=10, action=action):
                        if getattr(entry.target, "id", None) == user.id:
                            audit_lines.append(f"• **{label}** by {entry.user.mention} • {discord.utils.format_dt(entry.created_at, 'R')}")
                except Exception:
                    continue
            if audit_lines:
                embeds.append(Embed(title="Audit Log (recent)", description="\n".join(audit_lines[:10])[:4096], color=COLORS.neutral))
        if len(embeds) == 1:
            embeds[0].set_author(name=str(user), icon_url=user.display_avatar.url)
            await ctx.send(embed=embeds[0])
        else:
            for e in embeds:
                e.set_author(name=str(user), icon_url=user.display_avatar.url)
            await ctx.paginate(embeds)

    @admin.group(name="recent", description="View recent moderation actions", example=",admin recent bans", invoke_without_command=True)
    @has_permissions(moderate_members=True)
    async def admin_recent(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    async def _fetch_recent_audit(self, ctx: commands.Context, action: discord.AuditLogAction, title: str):
        entries = []
        try:
            async for entry in ctx.guild.audit_logs(limit=15, action=action):
                target = getattr(entry.target, "mention", str(getattr(entry.target, "id", "Unknown")))
                entries.append(f"{discord.utils.format_dt(entry.created_at, 'R')} — **{target}** by {entry.user.mention} `{entry.reason or 'No reason'}`")
        except discord.Forbidden:
            return await ctx.deny("I need **View Audit Log** permission.")
        except discord.HTTPException as e:
            return await ctx.deny(f"Failed to fetch audit logs: `{e}`")
        if not entries:
            return await ctx.warn(f"No recent {title.lower()} found.")
        await ctx.embed(title=f"Recent {title}", description="\n".join(entries)[:4096])

    @admin_recent.command(name="kicks", description="View recent kicks", example=",admin recent kicks")
    @has_permissions(moderate_members=True)
    async def admin_recent_kicks(self, ctx: commands.Context):
        await self._fetch_recent_audit(ctx, discord.AuditLogAction.kick, "Kicks")

    @admin_recent.command(name="bans", description="View recent bans", example=",admin recent bans")
    @has_permissions(ban_members=True)
    async def admin_recent_bans(self, ctx: commands.Context):
        await self._fetch_recent_audit(ctx, discord.AuditLogAction.ban, "Bans")

    @admin_recent.command(name="timeouts", description="View recent timeouts", example=",admin recent timeouts")
    @has_permissions(moderate_members=True)
    async def admin_recent_timeouts(self, ctx: commands.Context):
        # timeouts are member_update with timed_out_until
        entries = []
        try:
            async for entry in ctx.guild.audit_logs(limit=30, action=discord.AuditLogAction.member_update):
                # heuristic: check if timed out
                after = getattr(entry, "after", None)
                # fallback: include all member_update and label as timeout if possible
                target = getattr(entry.target, "mention", str(getattr(entry.target, "id", "Unknown")))
                entries.append(f"{discord.utils.format_dt(entry.created_at, 'R')} — **{target}** by {entry.user.mention}")
                if len(entries) >= 15:
                    break
        except discord.Forbidden:
            return await ctx.deny("I need **View Audit Log** permission.")
        except discord.HTTPException as e:
            return await ctx.deny(f"Failed to fetch audit logs: `{e}`")
        if not entries:
            return await ctx.warn("No recent timeouts found.")
        await ctx.embed(title="Recent Timeouts", description="\n".join(entries)[:4096])

    @admin.command(name="announce", description="Send an announcement to a channel", example=",admin announce Hello @everyone")
    @app_commands.describe(message="Announcement content (Tagscript & Embed supported)", channel="Channel to announce in")
    @has_permissions(manage_messages=True)
    @commands.bot_has_permissions(send_messages=True)
    async def admin_announce(self, ctx: commands.Context, *, message: str, channel: Optional[discord.TextChannel] = None):
        # detect channel trailing/leading for prefix usage
        if channel is None and message:
            maybe_channel, maybe_text = await self._resolve_channel_and_text(ctx, message)
            if maybe_channel.id != ctx.channel.id:
                channel = maybe_channel
                message = maybe_text
        channel = channel or ctx.channel
        if not message or len(message.strip()) == 0:
            return await ctx.warn("Please provide a message to announce.")
        if len(message) > 4000:
            return await ctx.warn("Announcement cannot be longer than **4000** characters.")
        kwargs = await self._render_with_tagscript_embed(ctx, message)
        try:
            await channel.send(**kwargs)
        except discord.Forbidden:
            return await ctx.deny(f"I don't have permission to send in {channel.mention}.")
        except discord.HTTPException as e:
            return await ctx.deny(f"Failed to send announcement: `{e}`")
        await ctx.approve(f"Announcement sent in {channel.mention}.")

    # ------------------------------------------------------------------ #
    # Commands: webhook
    # ------------------------------------------------------------------ #

    @commands.hybrid_group(
        name="webhook",
        description="Manage webhooks in your server",
        example=",webhook list",
        invoke_without_command=True,
    )
    @commands.guild_only()
    async def webhook(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @webhook.command(name="list", description="List all webhooks", example=",webhook list")
    @has_permissions(manage_webhooks=True)
    async def webhook_list(self, ctx: commands.Context):
        cursor = await self.bot.db.execute(
            "SELECT identifier, channel_id, name, locked, creator_id FROM webhook_store WHERE guild_id = ?", (ctx.guild.id,)
        )
        rows = await cursor.fetchall()
        # also fetch native webhooks to show? but store is managed list
        if not rows:
            # fallback: show discord native webhooks
            try:
                hooks = await ctx.guild.webhooks()
            except discord.Forbidden:
                return await ctx.warn("No hollow webhooks found and I cannot view server webhooks.")
            if not hooks:
                return await ctx.warn("No webhooks found.")
            lines = [f"{h.name} — <#{h.channel_id}> `ID:{h.id}`" for h in hooks[:20]]
            return await ctx.embed(title="Server Webhooks", description="\n".join(lines)[:4096])
        lines = []
        for r in rows:
            ch = ctx.guild.get_channel(r["channel_id"])
            ch_str = ch.mention if ch else f"`deleted ({r['channel_id']})`"
            lock = "🔒" if r["locked"] else "🔓"
            lines.append(f"`{r['identifier']}` {lock} **{r['name'] or 'webhook'}** — {ch_str} • <@{r['creator_id']}>")
        await ctx.embed(title="hollow Webhooks", description="\n".join(lines)[:4096])

    @webhook.command(name="create", description="Create a new webhook in the current channel", example=",webhook create MyHook")
    @app_commands.describe(name="Name for the webhook")
    @has_permissions(manage_webhooks=True)
    @commands.bot_has_permissions(manage_webhooks=True)
    async def webhook_create(self, ctx: commands.Context, *, name: str):
        name = name.strip()[:80] or "hollow"
        try:
            hook = await ctx.channel.create_webhook(name=name, reason=f"Webhook created by {ctx.author} via hollow")
        except discord.Forbidden:
            return await ctx.deny("I need **Manage Webhooks** permission to create webhooks.")
        except discord.HTTPException as e:
            return await ctx.deny(f"Failed to create webhook: `{e}`")
        identifier = self._generate_webhook_identifier()
        # ensure unique
        for _ in range(5):
            cursor = await self.bot.db.execute("SELECT identifier FROM webhook_store WHERE identifier = ?", (identifier,))
            if not await cursor.fetchone():
                break
            identifier = self._generate_webhook_identifier()
        await self.bot.db.execute(
            "INSERT INTO webhook_store (identifier, guild_id, channel_id, webhook_id, webhook_token, webhook_url, creator_id, name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (identifier, ctx.guild.id, ctx.channel.id, hook.id, hook.token, hook.url, ctx.author.id, name),
        )
        await self.bot.db.commit()
        await ctx.approve(f"Webhook **{name}** created in {ctx.channel.mention} — identifier: `{identifier}`\nUse `,webhook send {identifier} <message>` to send.")

    @webhook.command(name="recreate", description="Recreate a webhook from its identifier", example=",webhook recreate abc12345")
    @app_commands.describe(identifier="Webhook identifier")
    @has_permissions(manage_webhooks=True)
    @commands.bot_has_permissions(manage_webhooks=True)
    async def webhook_recreate(self, ctx: commands.Context, identifier: str):
        cursor = await self.bot.db.execute(
            "SELECT channel_id, name FROM webhook_store WHERE identifier = ? AND guild_id = ?", (identifier, ctx.guild.id)
        )
        row = await cursor.fetchone()
        if not row:
            return await ctx.warn(f"No webhook found with identifier `{identifier}`.")
        channel = ctx.guild.get_channel(row["channel_id"]) or ctx.channel
        if not isinstance(channel, discord.TextChannel):
            return await ctx.warn("Original channel not found, recreating in current channel.")
        try:
            hook = await channel.create_webhook(name=row["name"] or "hollow", reason=f"Recreated {identifier} by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException) as e:
            return await ctx.deny(f"Failed to recreate webhook: `{e}`")
        await self.bot.db.execute(
            "UPDATE webhook_store SET webhook_id = ?, webhook_token = ?, webhook_url = ?, channel_id = ? WHERE identifier = ?",
            (hook.id, hook.token, hook.url, channel.id, identifier),
        )
        await self.bot.db.commit()
        await ctx.approve(f"Webhook `{identifier}` recreated in {channel.mention}.")

    @webhook.command(name="lock", description="Lock your webhook from being used by others", example=",webhook lock abc12345")
    @app_commands.describe(identifier="Webhook identifier to lock")
    async def webhook_lock(self, ctx: commands.Context, identifier: str):
        cursor = await self.bot.db.execute(
            "SELECT creator_id, locked FROM webhook_store WHERE identifier = ? AND guild_id = ?", (identifier, ctx.guild.id)
        )
        row = await cursor.fetchone()
        if not row:
            return await ctx.warn(f"No webhook found with identifier `{identifier}`.")
        if row["creator_id"] != ctx.author.id and not ctx.author.guild_permissions.manage_guild:
            return await ctx.deny("Only the webhook owner or a server manager can lock it.")
        if row["locked"]:
            return await ctx.warn(f"Webhook `{identifier}` is already locked.")
        await self.bot.db.execute("UPDATE webhook_store SET locked = 1 WHERE identifier = ?", (identifier,))
        await self.bot.db.commit()
        await ctx.approve(f"Webhook `{identifier}` is now **locked**.")

    @webhook.command(name="unlock", description="Unlock your webhook", example=",webhook unlock abc12345")
    @app_commands.describe(identifier="Webhook identifier to unlock")
    async def webhook_unlock(self, ctx: commands.Context, identifier: str):
        cursor = await self.bot.db.execute(
            "SELECT creator_id, locked FROM webhook_store WHERE identifier = ? AND guild_id = ?", (identifier, ctx.guild.id)
        )
        row = await cursor.fetchone()
        if not row:
            return await ctx.warn(f"No webhook found with identifier `{identifier}`.")
        if row["creator_id"] != ctx.author.id and not ctx.author.guild_permissions.manage_guild:
            return await ctx.deny("Only the webhook owner or a server manager can unlock it.")
        if not row["locked"]:
            return await ctx.warn(f"Webhook `{identifier}` is not locked.")
        await self.bot.db.execute("UPDATE webhook_store SET locked = 0 WHERE identifier = ?", (identifier,))
        await self.bot.db.commit()
        await ctx.approve(f"Webhook `{identifier}` is now **unlocked**.")

    @webhook.command(name="send", description="Send a message using a webhook | Supports Tagscript and Embed", example=",webhook send abc123 Hello")
    @app_commands.describe(identifier="Webhook identifier", message="Message to send (Tagscript & Embed supported)")
    async def webhook_send(self, ctx: commands.Context, identifier: str, *, message: str):
        cursor = await self.bot.db.execute(
            "SELECT webhook_id, webhook_token, locked, creator_id, channel_id FROM webhook_store WHERE identifier = ? AND guild_id = ?", (identifier, ctx.guild.id)
        )
        row = await cursor.fetchone()
        if not row:
            return await ctx.warn(f"No webhook found with identifier `{identifier}`. Use `,webhook list` or `,webhook create`.")
        if row["locked"] and row["creator_id"] != ctx.author.id and not ctx.author.guild_permissions.manage_guild:
            return await ctx.deny("This webhook is **locked** by its owner.")
        # prepare content via tagscript+embed
        kwargs = await self._render_with_tagscript_embed(ctx, message)
        # ensure webhook still exists
        try:
            hook = discord.Webhook.from_url(f"https://discord.com/api/webhooks/{row['webhook_id']}/{row['webhook_token']}", client=self.bot)
            # adapt kwargs: webhook send uses content/embed/view but view not supported directly; convert view to components via embed?
            # For simplicity, use hook.send with whatever we have; discord.Webhook supports view via components in newer versions
            send_kwargs = {}
            if "content" in kwargs:
                send_kwargs["content"] = kwargs["content"]
            if "embed" in kwargs:
                send_kwargs["embed"] = kwargs["embed"]
            if "view" in kwargs:
                send_kwargs["view"] = kwargs["view"]
            send_kwargs["wait"] = True
            try:
                await hook.send(**send_kwargs)
            except discord.NotFound:
                return await ctx.deny("Webhook not found (deleted). Try `,webhook recreate` using the same identifier.")
            await ctx.approve(f"Message sent via webhook `{identifier}`.")
        except discord.Forbidden:
            await ctx.deny("I don't have permission to use that webhook.")
        except discord.HTTPException as e:
            await ctx.deny(f"Failed to send via webhook: `{e}`")
        except Exception as e:
            await ctx.deny(f"Webhook send failed: `{e}`")

    @webhook.command(name="edit", description="Edit a message sent by a webhook | Supports Tagscript and Embed", example=",webhook edit https://discord.com/channels/.../... New content")
    @app_commands.describe(message_link_or_id="Message link or ID to edit", message="New content (Tagscript & Embed supported)")
    @has_permissions(manage_messages=True)
    async def webhook_edit(self, ctx: commands.Context, message_link_or_id: str, *, message: Optional[str] = None):
        if not message:
            return await ctx.warn("Please provide the new message content.")
        target = await self._parse_message_reference(ctx, message_link_or_id)
        if not target:
            return await ctx.warn("Could not find that message. Provide a valid message link or ID.")
        # check if message is webhook authored - we try to find webhook store for its channel
        # allow edit if author is webhook or bot can edit
        kwargs = await self._render_with_tagscript_embed(ctx, message)
        # Attempt to edit via webhook if possible: find webhook that matches channel
        cursor = await self.bot.db.execute(
            "SELECT webhook_id, webhook_token FROM webhook_store WHERE guild_id = ? AND channel_id = ? LIMIT 1", (ctx.guild.id, target.channel.id)
        )
        row = await cursor.fetchone()
        # Prefer webhook edit if we have webhook for that channel and target was webhook message
        webhook_edited = False
        if row and target.webhook_id:
            try:
                hook = discord.Webhook.from_url(f"https://discord.com/api/webhooks/{row['webhook_id']}/{row['webhook_token']}", client=self.bot)
                edit_kwargs = {}
                if "content" in kwargs:
                    edit_kwargs["content"] = kwargs["content"]
                if "embed" in kwargs:
                    edit_kwargs["embed"] = kwargs["embed"]
                if "view" in kwargs:
                    edit_kwargs["view"] = kwargs["view"]
                # webhook edit requires message_id
                await hook.edit_message(target.id, **edit_kwargs)
                webhook_edited = True
            except Exception:
                webhook_edited = False
        if not webhook_edited:
            # fallback: try bot edit if we are author or have manage_messages
            try:
                edit_kwargs = {}
                if "content" in kwargs:
                    edit_kwargs["content"] = kwargs["content"]
                if "embed" in kwargs:
                    edit_kwargs["embed"] = kwargs["embed"]
                if "view" in kwargs:
                    edit_kwargs["view"] = kwargs["view"]
                # clear embed if content only?
                if "embed" not in edit_kwargs:
                    edit_kwargs["embed"] = None
                await target.edit(**edit_kwargs)
            except discord.Forbidden:
                return await ctx.deny("I don't have permission to edit that message.")
            except discord.HTTPException as e:
                return await ctx.deny(f"Failed to edit message: `{e}`")
        await ctx.approve(f"Message edited.")

    # ------------------------------------------------------------------ #
    # Helpers: boost / booster role
    # ------------------------------------------------------------------ #

    # ---- boost config ----
    DEFAULT_BOOST_CONFIG: dict = {"channel_id": None, "message": None}

    async def _get_boost_config(self, guild_id: int) -> dict:
        try:
            cursor = await self.bot.db.execute(
                "SELECT channel_id, message FROM boost_config WHERE guild_id = ?", (guild_id,)
            )
            row = await cursor.fetchone()
        except Exception:
            row = None
        if not row:
            return dict(self.DEFAULT_BOOST_CONFIG)
        return {"channel_id": row["channel_id"], "message": row["message"]}

    async def _set_boost_config(self, guild_id: int, channel_id: Optional[int], message: Optional[str]) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO boost_config (guild_id, channel_id, message)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                message = excluded.message
            """,
            (guild_id, channel_id, message),
        )
        await self.bot.db.commit()

    async def _clear_boost_config(self, guild_id: int) -> None:
        await self.bot.db.execute("DELETE FROM boost_config WHERE guild_id = ?", (guild_id,))
        await self.bot.db.commit()

    # ---- booster-role config ----
    DEFAULT_BOOSTER_ROLE_CONFIG: dict = {
        "base_role_id": None,
        "enabled": False,
        "hoist": False,
        "role_limit": 1,
        "share_limit": 3,
        "default_color": 0x99AAB5,
        "filtered_words": [],
    }

    async def _get_br_config(self, guild_id: int) -> dict:
        try:
            cursor = await self.bot.db.execute(
                """
                SELECT base_role_id, enabled, hoist, role_limit, share_limit,
                       default_color, filtered_words
                FROM booster_role_config WHERE guild_id = ?
                """,
                (guild_id,),
            )
            row = await cursor.fetchone()
        except Exception:
            row = None
        if not row:
            return dict(self.DEFAULT_BOOSTER_ROLE_CONFIG)
        try:
            filtered = json.loads(row["filtered_words"] or "[]")
            if not isinstance(filtered, list):
                filtered = []
        except (TypeError, ValueError):
            filtered = []
        return {
            "base_role_id": row["base_role_id"],
            "enabled": bool(row["enabled"]),
            "hoist": bool(row["hoist"]),
            "role_limit": row["role_limit"] or 1,
            "share_limit": row["share_limit"] or 3,
            "default_color": row["default_color"] if row["default_color"] is not None else 0x99AAB5,
            "filtered_words": [str(w) for w in filtered],
        }

    async def _set_br_config(self, guild_id: int, config: dict) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO booster_role_config (
                guild_id, base_role_id, enabled, hoist, role_limit,
                share_limit, default_color, filtered_words
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                base_role_id = excluded.base_role_id,
                enabled = excluded.enabled,
                hoist = excluded.hoist,
                role_limit = excluded.role_limit,
                share_limit = excluded.share_limit,
                default_color = excluded.default_color,
                filtered_words = excluded.filtered_words
            """,
            (
                guild_id,
                config.get("base_role_id"),
                int(bool(config.get("enabled"))),
                int(bool(config.get("hoist"))),
                max(1, int(config.get("role_limit") or 1)),
                max(0, int(config.get("share_limit") or 0)),
                int(config.get("default_color") or 0x99AAB5),
                json.dumps(config.get("filtered_words") or []),
            ),
        )
        await self.bot.db.commit()

    async def _user_br_roles(self, guild_id: int, user_id: int) -> list[int]:
        cursor = await self.bot.db.execute(
            "SELECT role_id FROM booster_roles WHERE guild_id = ? AND user_id = ? ORDER BY created_at ASC",
            (guild_id, user_id),
        )
        rows = await cursor.fetchall()
        return [row["role_id"] for row in rows]

    async def _count_br_roles(self, guild_id: int, user_id: int) -> int:
        cursor = await self.bot.db.execute(
            "SELECT COUNT(*) AS c FROM booster_roles WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cursor.fetchone()
        return int(row["c"] or 0)

    async def _delete_br_role(self, guild_id: int, role_id: int) -> None:
        await self.bot.db.execute(
            "DELETE FROM booster_roles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id)
        )
        await self.bot.db.execute(
            "DELETE FROM booster_role_shares WHERE guild_id = ? AND role_id = ?", (guild_id, role_id)
        )
        await self.bot.db.commit()

    async def _is_br_booster(self, member: discord.Member) -> bool:
        """Member actively boosting the guild."""
        return bool(member.guild_premium_subscriptions or member.premium_since is not None)

    @staticmethod
    def _parse_color(value: str) -> Optional[int]:
        s = value.strip().lstrip("#")
        # hex 3-char
        if len(s) == 3 and all(c in "0123456789abcdefABCDEF" for c in s):
            s = "".join(c * 2 for c in s)
        if len(s) != 6 or not all(c in "0123456789abcdefABCDEF" for c in s):
            return None
        try:
            value_int = int(s, 16)
            return discord.Color(value_int).value
        except ValueError:
            return None

    @staticmethod
    def _name_passes_filter(name: str, filtered_words: list[str]) -> bool:
        lowered = name.lower()
        return not any(word and word.lower() in lowered for word in filtered_words)

    async def _resolve_br_member(self, ctx: commands.Context, member_arg: Optional[discord.Member]) -> discord.Member:
        # boosts may target an offline/missing user; ctx.author is the default
        return member_arg or ctx.author

    async def _sync_booster_roles(self, guild: discord.Guild, base_role: Optional[discord.Role]) -> None:
        """Re-stack all tracked booster roles under the base role."""
        cursor = await self.bot.db.execute(
            "SELECT role_id FROM booster_roles WHERE guild_id = ?", (guild.id,)
        )
        rows = await cursor.fetchall()
        changed = 0
        for row in rows:
            role = guild.get_role(row["role_id"])
            if role is None:
                continue
            try:
                desired_position = (base_role.position - 1) if base_role else role.position
                # Move in safe batches without reordering other roles.
                if desired_position < 0:
                    desired_position = 0
                await role.edit(position=desired_position, reason="Booster role sync")
                changed += 1
            except (discord.Forbidden, discord.HTTPException):
                continue
        return changed

    # ------------------------------------------------------------------ #
    # Boost event-helpers (called from on_member_update above)
    # ------------------------------------------------------------------ #

    async def _maybe_give_default_booster_role(self, member: discord.Member) -> None:
        """If booster roles are enabled, but the user has none, give them a default."""
        try:
            cfg = await self._get_br_config(member.guild.id)
        except Exception:
            return
        if not cfg["enabled"]:
            return
        owned = await self._user_br_roles(member.guild.id, member.id)
        if owned:
            return
        # only auto-create if there is a default_color defined and limit >= 1
        if cfg["role_limit"] < 1:
            return
        # don't auto-create if user disabled (no base role set)
        if cfg["base_role_id"] is None:
            return
        base_role = member.guild.get_role(cfg["base_role_id"])
        if base_role is None:
            return
        if not member.guild.me.guild_permissions.manage_roles:
            return
        try:
            role = await member.guild.create_role(
                name=f"{member.display_name}'s role",
                color=discord.Color(cfg["default_color"]) if cfg["default_color"] else discord.Color.default(),
                reason=f"Auto-allocated booster role for {member}",
            )
            position = max(0, base_role.position - 1)
            try:
                await role.edit(position=position, hoist=cfg["hoist"])
            except (discord.Forbidden, discord.HTTPException):
                pass
            await self.bot.db.execute(
                "INSERT OR REPLACE INTO booster_roles (role_id, guild_id, user_id) VALUES (?, ?, ?)",
                (role.id, member.guild.id, member.id),
            )
            await self.bot.db.commit()
            try:
                await member.add_roles(role, reason="Auto-allocated booster role")
            except (discord.Forbidden, discord.HTTPException):
                pass
        except (discord.Forbidden, discord.HTTPException):
            return

    async def _handle_unboost(self, member: discord.Member) -> None:
        """When a member unboosts, clean up roles they own unless shared."""
        try:
            cfg = await self._get_br_config(member.guild.id)
        except Exception:
            return
        if not cfg["enabled"]:
            return
        owned = await self._user_br_roles(member.guild.id, member.id)
        for role_id in owned:
            # check if anyone else owns it via shares
            cursor = await self.bot.db.execute(
                "SELECT COUNT(*) AS c FROM booster_role_shares WHERE guild_id = ? AND role_id = ?",
                (member.guild.id, role_id),
            )
            row = await cursor.fetchone()
            shares = int(row["c"] or 0)
            role = member.guild.get_role(role_id)
            if shares == 0 and role is not None and member.guild.me.guild_permissions.manage_roles:
                try:
                    await role.delete(reason=f"Booster {member} unboosted")
                except (discord.Forbidden, discord.HTTPException):
                    pass
            else:
                # remove role from the unbooster, but keep the role itself
                try:
                    if role is not None:
                        await member.remove_roles(role, reason=f"{member} unboosted")
                except (discord.Forbidden, discord.HTTPException):
                    pass
            await self._delete_br_role(member.guild.id, role_id)
        # also drop their inbound shares
        await self.bot.db.execute(
            "DELETE FROM booster_role_shares WHERE guild_id = ? AND recipient_id = ?",
            (member.guild.id, member.id),
        )
        await self.bot.db.commit()

    # ------------------------------------------------------------------ #
    # Commands: boost
    # ------------------------------------------------------------------ #

    @commands.hybrid_group(
        name="boost",
        description="Manage boost notifications",
        example=",boost channel #boost",
        invoke_without_command=True,
    )
    @commands.guild_only()
    @has_permissions(manage_guild=True)
    async def boost(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @boost.group(name="channel", aliases=["ch"], description="Set the boost notification channel",
                 example=",boost channel #boost")
    @has_permissions(manage_guild=True)
    async def boost_channel(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @boost_channel.command(name="set", description="Set boost notification channel", example=",boost channel set #boost")
    @app_commands.describe(channel="Channel to send boost notifications in")
    @has_permissions(manage_guild=True)
    @commands.bot_has_permissions(send_messages=True)
    async def boost_channel_set(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        channel = channel or ctx.channel
        if not isinstance(channel, discord.TextChannel):
            return await ctx.warn("Boost notifications can only be sent in text channels.")
        cfg = await self._get_boost_config(ctx.guild.id)
        cfg["channel_id"] = channel.id
        await self._set_boost_config(ctx.guild.id, cfg["channel_id"], cfg["message"])
        await ctx.approve(f"Boost notifications will be sent in {channel.mention}.")

    @boost.group(name="message", aliases=["msg", "setmessage"], description="Set the boost message",
                 example=",boost message {user.mention} boosted **{guild.name}**!")
    @has_permissions(manage_guild=True)
    async def boost_message(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @boost_message.command(name="set", description="Set boost message (Tagscript & Embed supported)",
                           example=",boost message set Thank you {user.mention} for boosting! 🎉")
    @app_commands.describe(message="Boost message (Tagscript & Embed supported)")
    @has_permissions(manage_guild=True)
    async def boost_message_set(self, ctx: commands.Context, *, message: str):
        if len(message) > 4000:
            return await ctx.warn("Boost message cannot be longer than **4000** characters.")
        try:
            await self.render(ctx.author, message)
        except Exception as e:
            return await ctx.deny(f"That message could not be parsed: `{e}`")
        cfg = await self._get_boost_config(ctx.guild.id)
        cfg["message"] = message
        await self._set_boost_config(ctx.guild.id, cfg["channel_id"], cfg["message"])
        await ctx.approve("The boost message has been **updated**.")

    @boost_message.command(name="preview", description="Preview the boost message", example=",boost message preview")
    @has_permissions(manage_guild=True)
    async def boost_message_preview(self, ctx: commands.Context):
        cfg = await self._get_boost_config(ctx.guild.id)
        raw = cfg["message"] or DEFAULT_BOOST_MESSAGE
        try:
            kwargs = await self.render(ctx.author, raw)
        except Exception as e:
            return await ctx.deny(f"Failed to render: `{e}`")
        await ctx.send(**kwargs)

    @boost.command(name="remove", aliases=["del", "delete", "disable"], description="Remove boost notifications",
                   example=",boost remove")
    @has_permissions(manage_guild=True)
    async def boost_remove(self, ctx: commands.Context):
        await self._clear_boost_config(ctx.guild.id)
        await ctx.approve("Boost notifications have been **disabled**.")

    @boost.command(name="view", aliases=["see", "test"], description="View the current boost configuration",
                   example=",boost view")
    @has_permissions(manage_guild=True)
    async def boost_view(self, ctx: commands.Context):
        cfg = await self._get_boost_config(ctx.guild.id)
        channel = ctx.guild.get_channel(cfg["channel_id"]) if cfg["channel_id"] else None
        embed = Embed(title="Boost Configuration", color=COLORS.neutral)
        embed.add_field(
            name="Channel",
            value=channel.mention if channel else "Not set",
            inline=False,
        )
        embed.add_field(
            name="Message",
            value=(cfg["message"] or DEFAULT_BOOST_MESSAGE)[:1024] or "Not set",
            inline=False,
        )
        embed.set_footer(text="Use `,boost message preview` to see how it renders.")
        await ctx.send(embed=embed)

    @boost.command(name="variables", aliases=["vars", "tags"], description="List available boost variables",
                   example=",boost variables")
    async def boost_variables(self, ctx: commands.Context):
        await ctx.embed(
            title="Boost Variables",
            fields=[{"name": n, "value": v[:1024] or "N/A"} for n, v in VARIABLES_FIELDS()],
        )

    # ------------------------------------------------------------------ #
    # Commands: boosterrole  (alias: br, boosterroles)
    # ------------------------------------------------------------------ #

    @commands.hybrid_group(
        name="boosterrole",
        aliases=["br", "boosterroles"],
        description="Manage custom booster roles for boosters",
        example=",boosterrole setup",
        invoke_without_command=True,
    )
    @commands.guild_only()
    async def boosterrole(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            cfg = await self._get_br_config(ctx.guild.id)
            if not cfg["enabled"]:
                return await ctx.warn("Booster roles are not set up. Use `,boosterrole setup`.")
            await hollowHelp.send_group_help(ctx, ctx.command)

    # ---- setup / disable ----
    @boosterrole.command(name="setup", description="Enable custom booster roles", example=",boosterrole setup")
    @has_permissions(administrator=True)
    async def br_setup(self, ctx: commands.Context):
        cfg = await self._get_br_config(ctx.guild.id)
        if cfg["enabled"]:
            return await ctx.warn("Booster roles are already **enabled**. Use `,boosterrole disable` to turn them off.")
        cfg["enabled"] = True
        await self._set_br_config(ctx.guild.id, cfg)
        await ctx.approve(
            "Booster roles are now **enabled**. "
            "Set a base role with `,boosterrole base` and let boosters use `,boosterrole create`."
        )

    @boosterrole.command(name="disable", description="Disable booster roles and remove tracking",
                         example=",boosterrole disable")
    @has_permissions(administrator=True)
    async def br_disable(self, ctx: commands.Context):
        await self.bot.db.execute("DELETE FROM booster_role_config WHERE guild_id = ?", (ctx.guild.id,))
        await self.bot.db.execute("DELETE FROM booster_roles WHERE guild_id = ?", (ctx.guild.id,))
        await self.bot.db.execute("DELETE FROM booster_role_shares WHERE guild_id = ?", (ctx.guild.id,))
        await self.bot.db.commit()
        await ctx.approve("Booster roles have been **disabled** and all data cleared.")

    # ---- base role ----
    @boosterrole.command(name="base", description="Set the role that booster roles are created under",
                         example=",boosterrole base @Members")
    @app_commands.describe(role="The role booster roles will be created under")
    @has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def br_base(self, ctx: commands.Context, role: discord.Role):
        if role.is_default() or role.managed:
            return await ctx.warn("Choose a regular server role that is not managed by an integration.")
        cfg = await self._get_br_config(ctx.guild.id)
        cfg["base_role_id"] = role.id
        await self._set_br_config(ctx.guild.id, cfg)
        await ctx.approve(f"Booster roles will now be created under {role.mention}.")

    # ---- create ----
    @boosterrole.command(name="create", description="Create your booster role", example=",boosterrole create Pink 0xFF00FF")
    @app_commands.describe(name="Name for your booster role", color="Hex color (e.g. 0xFF00FF or FF00FF)")
    @commands.bot_has_permissions(manage_roles=True)
    async def br_create(self, ctx: commands.Context, name: str, color: Optional[str] = None):
        cfg = await self._get_br_config(ctx.guild.id)
        if not cfg["enabled"]:
            return await ctx.warn("Booster roles are not set up. Ask an admin to run `,boosterrole setup`.")
        if not await self._is_br_booster(ctx.author):
            return await ctx.warn("Only **boosters** can use this command.")
        name = (name or "").strip()
        if not name:
            return await ctx.warn("Please provide a name for your booster role.")
        if len(name) > 100:
            return await ctx.warn("Role name cannot exceed **100** characters.")
        if not self._name_passes_filter(name, cfg["filtered_words"]):
            return await ctx.warn("Your role name contains a filtered word. Pick a different name.")
        owned = await self._count_br_roles(ctx.guild.id, ctx.author.id)
        limit = max(1, cfg["role_limit"])
        if owned >= limit:
            return await ctx.warn(f"You already own **{owned}/{limit}** booster role(s). Remove one with `,boosterrole remove` first.")
        color_value = cfg["default_color"] if cfg["default_color"] else 0x99AAB5
        if color is not None:
            parsed_color = self._parse_color(color)
            if not parsed_color:
                return await ctx.warn("Invalid color. Use hex format like `0xFF00FF` or `FF00FF`.")
            color_value = parsed_color
        base_role = ctx.guild.get_role(cfg["base_role_id"]) if cfg["base_role_id"] else None
        try:
            new_role = await ctx.guild.create_role(
                name=name,
                color=discord.Color(color_value),
                hoist=cfg["hoist"],
                reason=f"Booster role created by {ctx.author}",
            )
        except discord.Forbidden:
            return await ctx.deny("I need **Manage Roles** to create booster roles.")
        except discord.HTTPException as e:
            return await ctx.deny(f"Failed to create role: `{e}`")
        if base_role is not None:
            try:
                position = max(1, base_role.position - 1)
                await new_role.edit(position=position)
            except (discord.Forbidden, discord.HTTPException):
                pass
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO booster_roles (role_id, guild_id, user_id) VALUES (?, ?, ?)",
            (new_role.id, ctx.guild.id, ctx.author.id),
        )
        await self.bot.db.commit()
        try:
            await ctx.author.add_roles(new_role, reason="Booster role created")
        except (discord.Forbidden, discord.HTTPException):
            pass
        await ctx.approve(f"Your booster role {new_role.mention} has been **created**.")

    # ---- remove ----
    @boosterrole.command(name="remove", aliases=["delete", "del"], description="Remove your booster role",
                         example=",boosterrole remove")
    @commands.bot_has_permissions(manage_roles=True)
    async def br_remove(self, ctx: commands.Context):
        cfg = await self._get_br_config(ctx.guild.id)
        if not cfg["enabled"]:
            return await ctx.warn("Booster roles are not set up.")
        owned_role_ids = await self._user_br_roles(ctx.guild.id, ctx.author.id)
        if not owned_role_ids:
            return await ctx.warn("You don't own any booster role.")
        for role_id in owned_role_ids:
            # check if shared with others; if so just remove from author and keep it
            cursor = await self.bot.db.execute(
                "SELECT COUNT(*) AS c FROM booster_role_shares WHERE guild_id = ? AND role_id = ?",
                (ctx.guild.id, role_id),
            )
            row = await cursor.fetchone()
            shares = int(row["c"] or 0)
            role = ctx.guild.get_role(role_id)
            try:
                if role is not None:
                    await ctx.author.remove_roles(role, reason="Removed booster role")
                if shares == 0 and role is not None:
                    await role.delete(reason=f"Booster role removed by {ctx.author}")
            except (discord.Forbidden, discord.HTTPException):
                continue
            await self._delete_br_role(ctx.guild.id, role_id)
        await ctx.approve("Your booster role(s) have been **removed**.")

    # ---- include ----
    @boosterrole.command(name="include", description="Associate an existing role as a member's booster role",
                         example=",boosterrole include @MyRole @member")
    @app_commands.describe(role="Existing role to use", member="Booster that will own it")
    @has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def br_include(self, ctx: commands.Context, role: discord.Role, member: discord.Member):
        if role.is_default() or role.managed or role.is_premium_subscriber():
            return await ctx.warn("Choose a regular server role that is not managed.")
        cfg = await self._get_br_config(ctx.guild.id)
        if not cfg["enabled"]:
            return await ctx.warn("Booster roles are not set up.")
        owned = await self._count_br_roles(ctx.guild.id, member.id)
        if owned >= cfg["role_limit"]:
            return await ctx.warn(f"{member.mention} already owns the maximum booster roles.")
        # ensure not already tracked
        cursor = await self.bot.db.execute(
            "SELECT 1 FROM booster_roles WHERE role_id = ? AND guild_id = ?", (role.id, ctx.guild.id)
        )
        if await cursor.fetchone():
            return await ctx.warn(f"{role.mention} is already registered as a booster role.")
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO booster_roles (role_id, guild_id, user_id) VALUES (?, ?, ?)",
            (role.id, ctx.guild.id, member.id),
        )
        await self.bot.db.commit()
        try:
            await member.add_roles(role, reason="Associated as booster role")
        except (discord.Forbidden, discord.HTTPException):
            pass
        await ctx.approve(f"{role.mention} has been associated as {member.mention}'s booster role.")

    # ---- name / color edit ----
    @boosterrole.command(name="name", description="Rename your booster role", example=",boosterrole name New Name")
    @commands.bot_has_permissions(manage_roles=True)
    async def br_name(self, ctx: commands.Context, *, name: str):
        cfg = await self._get_br_config(ctx.guild.id)
        if not cfg["enabled"]:
            return await ctx.warn("Booster roles are not set up.")
        if not await self._is_br_booster(ctx.author):
            return await ctx.warn("Only boosters can rename their booster role.")
        if not name.strip():
            return await ctx.warn("Provide a new name.")
        if len(name) > 100:
            return await ctx.warn("Role name cannot exceed **100** characters.")
        if not self._name_passes_filter(name, cfg["filtered_words"]):
            return await ctx.warn("That name contains a filtered word.")
        owned = await self._user_br_roles(ctx.guild.id, ctx.author.id)
        if not owned:
            return await ctx.warn("You don't own a booster role.")
        renamed = 0
        for role_id in owned:
            role = ctx.guild.get_role(role_id)
            if role is None:
                continue
            try:
                await role.edit(name=name, reason=f"Booster role renamed by {ctx.author}")
                renamed += 1
            except (discord.Forbidden, discord.HTTPException):
                continue
        if not renamed:
            return await ctx.deny("I couldn't rename your booster role. Check my permissions.")
        await ctx.approve(f"Renamed **{renamed}** booster role(s) to **{name}**.")

    # ---- color ----
    @boosterrole.command(name="color", description="Recolor your booster role", example=",boosterrole color 0xFF00FF")
    @commands.bot_has_permissions(manage_roles=True)
    async def br_color(self, ctx: commands.Context, color: str):
        cfg = await self._get_br_config(ctx.guild.id)
        if not cfg["enabled"]:
            return await ctx.warn("Booster roles are not set up.")
        if not await self._is_br_booster(ctx.author):
            return await ctx.warn("Only boosters can recolor their booster role.")
        parsed_color = self._parse_color(color)
        if not parsed_color:
            return await ctx.warn("Invalid color. Use hex format like `0xFF00FF` or `FF00FF`.")
        owned = await self._user_br_roles(ctx.guild.id, ctx.author.id)
        if not owned:
            return await ctx.warn("You don't own a booster role.")
        recolored = 0
        for role_id in owned:
            role = ctx.guild.get_role(role_id)
            if role is None:
                continue
            try:
                await role.edit(color=discord.Color(parsed_color), reason=f"Recolored by {ctx.author}")
                recolored += 1
            except (discord.Forbidden, discord.HTTPException):
                continue
        if not recolored:
            return await ctx.deny("I couldn't recolor your booster role.")
        await ctx.approve(f"Recolored **{recolored}** booster role(s).")

    # ---- icon ----
    @boosterrole.command(name="icon", description="Set your booster role icon", example=",boosterrole icon")
    @app_commands.describe(url="URL for the icon (optional)", file="Image attachment (optional)")
    @commands.bot_has_permissions(manage_roles=True)
    async def br_icon(self, ctx: commands.Context, url: Optional[str] = None, file: Optional[discord.Attachment] = None):
        cfg = await self._get_br_config(ctx.guild.id)
        if not cfg["enabled"]:
            return await ctx.warn("Booster roles are not set up.")
        if not await self._is_br_booster(ctx.author):
            return await ctx.warn("Only boosters can set their role icon.")
        owned = await self._user_br_roles(ctx.guild.id, ctx.author.id)
        if not owned:
            return await ctx.warn("You don't own a booster role.")
        asset: Optional[bytes] = None
        try:
            if file is not None:
                if file.size > ROLE_ICON_MAX_BYTES:
                    return await ctx.warn("Attachment too large (max **256KB**).")
                asset = await file.read()
            elif url:
                async with self.bot.session.get(url) as resp:
                    if resp.status != 200:
                        return await ctx.deny(f"Failed to fetch the image (HTTP {resp.status}).")
                    data = await resp.read()
                    if len(data) > ROLE_ICON_MAX_BYTES:
                        return await ctx.warn("Image too large (max **256KB**).")
                    asset = data
        except Exception as e:
            return await ctx.deny(f"Failed to download image: `{e}`")
        if asset is None:
            return await ctx.warn("Provide a URL or attach an image.")
        updated = 0
        for role_id in owned:
            role = ctx.guild.get_role(role_id)
            if role is None:
                continue
            try:
                await role.edit(display_icon=asset, reason=f"Icon set by {ctx.author}")
                updated += 1
            except discord.Forbidden:
                return await ctx.deny("I need **Manage Roles** to set role icons (requires boost tier 2).")
            except discord.HTTPException as e:
                return await ctx.deny(f"Failed to set icon: `{e}`")
        if not updated:
            return await ctx.deny("None of your booster roles could be updated.")
        await ctx.approve(f"Icon updated for **{updated}** booster role(s).")

    # ---- hoist toggle ----
    @boosterrole.command(name="hoist", description="Toggle whether booster roles are shown separately",
                         example=",boosterrole hoist")
    @has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def br_hoist(self, ctx: commands.Context):
        cfg = await self._get_br_config(ctx.guild.id)
        cfg["hoist"] = not cfg["hoist"]
        await self._set_br_config(ctx.guild.id, cfg)
        # also update existing roles
        cursor = await self.bot.db.execute(
            "SELECT role_id FROM booster_roles WHERE guild_id = ?", (ctx.guild.id,)
        )
        rows = await cursor.fetchall()
        updated = 0
        for row in rows:
            role = ctx.guild.get_role(row["role_id"])
            if role is None:
                continue
            try:
                await role.edit(hoist=cfg["hoist"], reason=f"Booster role hoist toggled by {ctx.author}")
                updated += 1
            except (discord.Forbidden, discord.HTTPException):
                continue
        await ctx.approve(
            f"Booster roles will now be displayed **{'separately' if cfg['hoist'] else 'inline'}**. "
            f"(Updated **{updated}** role(s))"
        )

    # ---- list ----
    @boosterrole.command(name="list", description="List every booster role", example=",boosterrole list")
    async def br_list(self, ctx: commands.Context):
        cursor = await self.bot.db.execute(
            """
            SELECT role_id, user_id FROM booster_roles WHERE guild_id = ?
            ORDER BY created_at ASC
            """,
            (ctx.guild.id,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return await ctx.warn("No booster roles have been created yet.")
        lines = []
        for row in rows:
            role = ctx.guild.get_role(row["role_id"])
            owner = ctx.guild.get_member(row["user_id"])
            if role is None:
                owner_fallback = f"`{row['user_id']}`"
                owner_str2 = owner.mention if owner else owner_fallback
                lines.append(f"`deleted role ({row['role_id']})` — owner: {owner_str2}")
                continue
            owner_str = owner.mention if owner else f"`{row['user_id']}`"
            lines.append(f"{role.mention} ({role.id}) — owner: {owner_str}")
        await ctx.embed(title=f"Booster Roles ({len(rows)})", description="\n".join(lines)[:4096])

    # ---- limit ----
    @boosterrole.command(name="limit", description="Set the maximum number of booster roles per booster",
                         example=",boosterrole limit 3")
    @app_commands.describe(amount="Maximum number of booster roles a single member can have")
    @has_permissions(manage_roles=True)
    async def br_limit(self, ctx: commands.Context, amount: int):
        if amount < 1 or amount > MAX_BOOSTER_LIMIT:
            return await ctx.warn(f"Limit must be between **1** and **{MAX_BOOSTER_LIMIT}**.")
        cfg = await self._get_br_config(ctx.guild.id)
        cfg["role_limit"] = amount
        await self._set_br_config(ctx.guild.id, cfg)
        await ctx.approve(f"Each booster may now own up to **{amount}** role(s).")

    # ---- sync ----
    @boosterrole.command(name="sync", description="Re-stack all booster roles under the base role",
                         example=",boosterrole sync")
    @has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def br_sync(self, ctx: commands.Context):
        cfg = await self._get_br_config(ctx.guild.id)
        base_role = ctx.guild.get_role(cfg["base_role_id"]) if cfg["base_role_id"] else None
        if base_role is None:
            return await ctx.warn("No base role configured. Set one with `,boosterrole base`.")
        try:
            changed = await self._sync_booster_roles(ctx.guild, base_role)
        except Exception:
            changed = 0
        if not changed:
            return await ctx.approve("All tracked booster roles are already correctly stacked.")
        await ctx.approve(f"Re-stacked **{changed}** booster role(s) under {base_role.mention}.")

    # ---- filter sub-group ----
    @boosterrole.group(name="filter", description="Manage filtered words for booster roles",
                       example=",boosterrole filter list", invoke_without_command=True)
    @has_permissions(manage_guild=True)
    async def br_filter(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @br_filter.command(name="add", description="Add a filtered word", example=",boosterrole filter add slur")
    @app_commands.describe(word="Word or substring to filter from booster-role names")
    @has_permissions(manage_guild=True)
    async def br_filter_add(self, ctx: commands.Context, *, word: str):
        cleaned = word.strip().lower()
        if not cleaned:
            return await ctx.warn("Provide a word to filter.")
        if len(cleaned) > 64:
            return await ctx.warn("Filtered words cannot exceed **64** characters.")
        cfg = await self._get_br_config(ctx.guild.id)
        if cleaned in [w.lower() for w in cfg["filtered_words"]]:
            return await ctx.warn("That word is already filtered.")
        cfg["filtered_words"].append(cleaned)
        await self._set_br_config(ctx.guild.id, cfg)
        await ctx.approve(f"Added `{cleaned}` to the booster-role name filter.")

    @br_filter.command(name="remove", aliases=["delete", "rm"], description="Remove a filtered word",
                       example=",boosterrole filter remove slur")
    @app_commands.describe(word="Filtered word to remove")
    @has_permissions(manage_guild=True)
    async def br_filter_remove(self, ctx: commands.Context, *, word: str):
        cfg = await self._get_br_config(ctx.guild.id)
        target = word.strip().lower()
        if target not in [w.lower() for w in cfg["filtered_words"]]:
            return await ctx.warn("That word isn't currently filtered.")
        cfg["filtered_words"] = [w for w in cfg["filtered_words"] if w.lower() != target]
        await self._set_br_config(ctx.guild.id, cfg)
        await ctx.approve(f"Removed `{target}` from the booster-role name filter.")

    @br_filter.command(name="list", description="List filtered words", example=",boosterrole filter list")
    @has_permissions(manage_guild=True)
    async def br_filter_list(self, ctx: commands.Context):
        cfg = await self._get_br_config(ctx.guild.id)
        if not cfg["filtered_words"]:
            return await ctx.warn("No filtered words configured.")
        await ctx.embed(title="Filtered Booster Role Words", description="\n".join(f"`{w}`" for w in cfg["filtered_words"])[:4096])

    # ---- share / shares sub-group ----
    @boosterrole.command(name="share", description="Share your booster role with another member",
                         example=",boosterrole share @member")
    @app_commands.describe(member="Member to share your booster role with")
    async def br_share(self, ctx: commands.Context, member: discord.Member):
        if member.bot:
            return await ctx.warn("You cannot share a booster role with a bot.")
        if member.id == ctx.author.id:
            return await ctx.warn("You already own your booster role.")
        cfg = await self._get_br_config(ctx.guild.id)
        if not cfg["enabled"]:
            return await ctx.warn("Booster roles are not set up.")
        if not await self._is_br_booster(ctx.author):
            return await ctx.warn("Only boosters can share booster roles.")
        owned = await self._user_br_roles(ctx.guild.id, ctx.author.id)
        if not owned:
            return await ctx.warn("You don't own a booster role to share.")
        if cfg["share_limit"] <= 0:
            return await ctx.warn("Sharing booster roles is currently disabled in this server.")
        # check share count already granted
        cursor = await self.bot.db.execute(
            "SELECT COUNT(*) AS c FROM booster_role_shares WHERE guild_id = ? AND sender_id = ?",
            (ctx.guild.id, ctx.author.id),
        )
        row = await cursor.fetchone()
        granted = int(row["c"] or 0)
        if granted >= cfg["share_limit"]:
            return await ctx.warn(f"You've already shared your role with the limit of **{cfg['share_limit']}** member(s).")
        role_id = owned[0]
        # ensure not already shared with that member
        cursor = await self.bot.db.execute(
            "SELECT 1 FROM booster_role_shares WHERE guild_id = ? AND role_id = ? AND recipient_id = ?",
            (ctx.guild.id, role_id, member.id),
        )
        if await cursor.fetchone():
            return await ctx.warn("That member already has access via a share.")
        await self.bot.db.execute(
            """
            INSERT INTO booster_role_shares (guild_id, role_id, sender_id, recipient_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, role_id, recipient_id) DO NOTHING
            """,
            (ctx.guild.id, role_id, ctx.author.id, member.id),
        )
        await self.bot.db.commit()
        role = ctx.guild.get_role(role_id)
        if role is not None:
            try:
                await member.add_roles(role, reason=f"Booster role shared by {ctx.author}")
            except (discord.Forbidden, discord.HTTPException) as e:
                return await ctx.warn(f"Share saved, but I couldn't add the role: `{e}`")
        await ctx.approve(f"Shared your booster role with {member.mention}.")

    @boosterrole.group(name="shares", description="Manage your booster role shares",
                       example=",boosterrole shares list", invoke_without_command=True)
    async def br_shares(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @br_shares.command(name="list", description="List members you've shared your booster role with",
                       example=",boosterrole shares list")
    async def br_shares_list(self, ctx: commands.Context):
        cursor = await self.bot.db.execute(
            """
            SELECT recipient_id, role_id FROM booster_role_shares
            WHERE guild_id = ? AND sender_id = ?
            ORDER BY created_at ASC
            """,
            (ctx.guild.id, ctx.author.id),
        )
        rows = await cursor.fetchall()
        if not rows:
            return await ctx.warn("You haven't shared your booster role with anyone.")
        lines = []
        for row in rows:
            member = ctx.guild.get_member(row["recipient_id"])
            role = ctx.guild.get_role(row["role_id"])
            if member is None:
                lines.append(f"`User {row['recipient_id']}`" + (f" — {role.mention}" if role else ""))
                continue
            lines.append(f"{member.mention} ({member})" + (f" — via {role.mention}" if role else ""))
        await ctx.embed(title=f"You shared your booster role with {len(rows)} member(s)",
                         description="\n".join(lines)[:4096])

    @br_shares.command(name="remove", description="Stop sharing your booster role with a member",
                       example=",boosterrole shares remove @member")
    @app_commands.describe(member="Member to stop sharing with")
    async def br_shares_remove(self, ctx: commands.Context, member: discord.Member):
        cursor = await self.bot.db.execute(
            "SELECT role_id FROM booster_role_shares WHERE guild_id = ? AND sender_id = ? AND recipient_id = ?",
            (ctx.guild.id, ctx.author.id, member.id),
        )
        rows = await cursor.fetchall()
        if not rows:
            return await ctx.warn(f"You haven't shared your booster role with {member.mention}.")
        for row in rows:
            await self.bot.db.execute(
                "DELETE FROM booster_role_shares WHERE guild_id = ? AND sender_id = ? AND recipient_id = ? AND role_id = ?",
                (ctx.guild.id, ctx.author.id, member.id, row["role_id"]),
            )
            role = ctx.guild.get_role(row["role_id"])
            if role is not None:
                try:
                    await member.remove_roles(role, reason=f"Booster role share ended by {ctx.author}")
                except (discord.Forbidden, discord.HTTPException):
                    pass
        await self.bot.db.commit()
        await ctx.approve(f"Stopped sharing your booster role with {member.mention}.")

    @br_shares.command(name="leave", description="Remove yourself from booster roles shared with you",
                       example=",boosterrole shares leave")
    async def br_shares_leave(self, ctx: commands.Context):
        cursor = await self.bot.db.execute(
            "SELECT role_id, sender_id FROM booster_role_shares WHERE guild_id = ? AND recipient_id = ?",
            (ctx.guild.id, ctx.author.id),
        )
        rows = await cursor.fetchall()
        if not rows:
            return await ctx.warn("No booster roles have been shared with you.")
        removed = 0
        for row in rows:
            role = ctx.guild.get_role(row["role_id"])
            try:
                if role is not None:
                    await ctx.author.remove_roles(role, reason="Recipient left shared booster role")
                removed += 1
            except (discord.Forbidden, discord.HTTPException):
                continue
        await self.bot.db.execute(
            "DELETE FROM booster_role_shares WHERE guild_id = ? AND recipient_id = ?",
            (ctx.guild.id, ctx.author.id),
        )
        await self.bot.db.commit()
        await ctx.approve(f"Removed **{removed}** shared booster role(s) from you.")

    @br_shares.command(name="max", description="Set the booster-role share limit",
                       example=",boosterrole shares max 5")
    @app_commands.describe(amount="Maximum number of members a booster can share with")
    @has_permissions(manage_guild=True)
    async def br_shares_max(self, ctx: commands.Context, amount: int):
        if amount < 0 or amount > MAX_SHARE_LIMIT:
            return await ctx.warn(f"Share limit must be between **0** and **{MAX_SHARE_LIMIT}**.")
        cfg = await self._get_br_config(ctx.guild.id)
        cfg["share_limit"] = amount
        await self._set_br_config(ctx.guild.id, cfg)
        if amount == 0:
            await ctx.approve("Booster-role sharing is now **disabled** in this server.")
        else:
            await ctx.approve(f"Each booster may now share with up to **{amount}** member(s).")


async def setup(bot):
    await bot.add_cog(Server(bot))
