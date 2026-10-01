import json
import re
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.client.commands import has_permissions, hybrid_command, hybrid_group
from core.client.tagscript import TagScriptParser
from core.client.EmbedBuilder import build_kwargs
from core.config import COLORS
from core.context import hollowHelp


# Trigger tokens used to match messages. A plain literal performs a
# case-insensitive substring search across the message content. A
# regex trigger is delimited with slashes: /pattern/flags .
TRIGGER_REGEX = re.compile(r"^/(.+)/([gimsuy]*)$")


class Automation(commands.Cog):
    """Automation tools: autorole, autoreact and autoresponder triggers."""

    def __init__(self, bot):
        self.bot = bot

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetchone(self, query: str, *args) -> Optional[Any]:
        cursor = await self.bot.db.execute(query, args)
        return await cursor.fetchone()

    # ----- Autorole helpers ----- #

    async def _get_autorole(self, guild_id: int) -> dict[str, list[int]]:
        row = await self._fetchone(
            "SELECT everyone, humans, bots FROM autorole_config WHERE guild_id = ?",
            guild_id,
        )
        if not row:
            return {"everyone": [], "humans": [], "bots": []}
        return {
            key: json.loads(row[key] or "[]")
            for key in ("everyone", "humans", "bots")
        }

    async def _save_autorole_kind(
        self, guild_id: int, kind: str, roles: list[int]
    ) -> None:
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO autorole_config (guild_id) VALUES (?)",
            (guild_id,),
        )
        await self.bot.db.execute(
            f"UPDATE autorole_config SET {kind} = ? WHERE guild_id = ?",
            (json.dumps(roles), guild_id),
        )
        await self.bot.db.commit()

    async def _autorole_modify(
        self,
        ctx: commands.Context,
        kind: str,
        role: discord.Role,
        *,
        add: bool,
    ) -> None:
        if role.is_default() or role.managed:
            return await ctx.warn("That role cannot be used for autoroles.")
        if role >= ctx.guild.me.top_role:
            return await ctx.deny(
                f"I cannot manage {role.mention} because it is higher than my highest role."
            )
        config = await self._get_autorole(ctx.guild.id)
        values = config[kind]
        if add:
            if role.id in values:
                return await ctx.warn(f"{role.mention} is already an autorole.")
            values.append(role.id)
        else:
            if role.id not in values:
                return await ctx.warn(f"{role.mention} is not an autorole.")
            values.remove(role.id)
        await self._save_autorole_kind(ctx.guild.id, kind, values)
        action = "added to" if add else "removed from"
        await ctx.approve(f"{role.mention} has been {action} the **autorole** list.")

    async def _autorole_list(
        self, ctx: commands.Context, kind: str, title: str
    ) -> None:
        config = await self._get_autorole(ctx.guild.id)
        ids = config[kind]
        if not ids:
            return await ctx.warn(f"No **{kind}** autoroles configured.")
        lines: list[str] = []
        stale: list[int] = []
        for rid in ids:
            role = ctx.guild.get_role(rid)
            if role is None:
                stale.append(rid)
                continue
            lines.append(f"{role.mention} (`{role.id}`)")
        if stale:
            for rid in stale:
                ids.remove(rid)
            await self._save_autorole_kind(ctx.guild.id, kind, ids)
        await ctx.embed(
            title=title,
            description="\n".join(lines)[:4096],
            color=COLORS.neutral,
        )

    async def _autorole_reset(
        self, ctx: commands.Context, kind: str, label: str
    ) -> None:
        config = await self._get_autorole(ctx.guild.id)
        if not config[kind]:
            return await ctx.warn(f"No **{label}** autoroles configured.")
        await self._save_autorole_kind(ctx.guild.id, kind, [])
        await ctx.approve(f"All **{label}** autoroles have been reset.")
# ----- Autoreact helpers ----- #

    async def _autoreact_rows(self, guild_id: int) -> list[Any]:
        cursor = await self.bot.db.execute(
            "SELECT id, trigger, emoji, exclusive_channels, exclusive_roles "
            "FROM autoreact_config WHERE guild_id = ?",
            (guild_id,),
        )
        return await cursor.fetchall()

    async def _autoreact_find(
        self, guild_id: int, trigger: str
    ) -> Optional[Any]:
        return await self._fetchone(
            "SELECT id, trigger, emoji, exclusive_channels, exclusive_roles "
            "FROM autoreact_config WHERE guild_id = ? AND trigger = ?",
            guild_id,
            trigger,
        )

    async def _autoreact_save_exclusive(
        self,
        ctx: commands.Context,
        row_id: int,
        column: str,
        target_id: int,
        *,
        add: bool,
    ) -> None:
        row = await self._fetchone(
            f"SELECT {column} FROM autoreact_config WHERE id = ?", row_id
        )
        if not row:
            return await ctx.warn("That autoreaction could not be found.")
        try:
            values = json.loads(row[column] or "[]")
        except (TypeError, ValueError):
            values = []
        if add:
            if target_id in values:
                return await ctx.warn("That entry is already in the exclusive list.")
            values.append(target_id)
        else:
            if target_id not in values:
                return await ctx.warn("That entry is not in the exclusive list.")
            values.remove(target_id)
        await self.bot.db.execute(
            f"UPDATE autoreact_config SET {column} = ? WHERE id = ?",
            (json.dumps(values), row_id),
        )
        await self.bot.db.commit()
        await ctx.approve("Autoreaction exclusivity has been updated.")

    # ----- Trigger (autoresponder) helpers ----- #

    async def _trigger_rows(self, guild_id: int) -> list[Any]:
        cursor = await self.bot.db.execute(
            "SELECT id, trigger, response, exclusive_channels, exclusive_roles "
            "FROM trigger_config WHERE guild_id = ?",
            (guild_id,),
        )
        return await cursor.fetchall()

    async def _trigger_find(
        self, guild_id: int, trigger: str
    ) -> Optional[Any]:
        return await self._fetchone(
            "SELECT id, trigger, response, exclusive_channels, exclusive_roles "
            "FROM trigger_config WHERE guild_id = ? AND trigger = ?",
            guild_id,
            trigger,
        )

    async def _trigger_save_exclusive(
        self,
        ctx: commands.Context,
        row_id: int,
        column: str,
        target_id: int,
        *,
        add: bool,
    ) -> None:
        row = await self._fetchone(
            f"SELECT {column} FROM trigger_config WHERE id = ?", row_id
        )
        if not row:
            return await ctx.warn("That trigger could not be found.")
        try:
            values = json.loads(row[column] or "[]")
        except (TypeError, ValueError):
            values = []
        if add:
            if target_id in values:
                return await ctx.warn("That entry is already in the exclusive list.")
            values.append(target_id)
        else:
            if target_id not in values:
                return await ctx.warn("That entry is not in the exclusive list.")
            values.remove(target_id)
        await self.bot.db.execute(
            f"UPDATE trigger_config SET {column} = ? WHERE id = ?",
            (json.dumps(values), row_id),
        )
        await self.bot.db.commit()
        await ctx.approve("Trigger exclusivity has been updated.")

    # ----- TagScript rendering ----- #

    async def _parse_tagscript(
        self,
        raw: str,
        author: discord.abc.User,
        guild: Optional[discord.Guild],
        channel: Optional[discord.abc.GuildChannel],
    ) -> str:
        try:
            return await TagScriptParser.parse(raw, author, guild, channel, self.bot)
        except Exception:
            return raw

    async def _render_response(
        self,
        raw: str,
        author: discord.abc.User,
        channel: discord.abc.Messageable,
    ) -> dict[str, Any]:
        """Run TagScript + the embed script builder, returning send kwargs."""
        guild = getattr(channel, "guild", None)
        parsed = await self._parse_tagscript(
            raw, author, guild, channel if isinstance(channel, discord.abc.GuildChannel) else None
        )
        return build_kwargs(parsed)
# ------------------------------------------------------------------ #
    # Commands: autorole
    # ------------------------------------------------------------------ #

    @hybrid_group(
        name="autorole",
        aliases=["ar", "autor", "arole"],
        description="Manage roles that are automatically given to new members.",
        example=",autorole add @Member",
    )
    @commands.guild_only()
    @has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def autorole(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @autorole.command(
        name="add",
        description="Add a role to be given to every new member.",
        example=",autorole add @Member",
    )
    @app_commands.describe(role="Role to give to every new member")
    async def autorole_add(self, ctx: commands.Context, role: discord.Role):
        await self._autorole_modify(ctx, "everyone", role, add=True)

    @autorole.command(
        name="remove",
        aliases=["delete", "rm"],
        description="Remove a role from the everyone autorole list.",
        example=",autorole remove @Member",
    )
    @app_commands.describe(role="Role to remove from the everyone autorole list")
    async def autorole_remove(self, ctx: commands.Context, role: discord.Role):
        await self._autorole_modify(ctx, "everyone", role, add=False)

    @autorole.command(
        name="list",
        description="List every role given to all new members.",
        example=",autorole list",
    )
    async def autorole_list(self, ctx: commands.Context):
        await self._autorole_list(ctx, "everyone", "Autorole (everyone)")

    @autorole.command(
        name="reset",
        description="Clear every autorole assigned to everyone.",
        example=",autorole reset",
    )
    async def autorole_reset(self, ctx: commands.Context):
        await self._autorole_reset(ctx, "everyone", "everyone")

    # autorole humans subgroup
    @autorole.group(
        name="humans",
        aliases=["users", "members"],
        description="Manage autoroles given only to human members.",
        example=",autorole humans add @Member",
    )
    async def autorole_humans(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @autorole_humans.command(
        name="add",
        description="Add a role to be given to human members only.",
        example=",autorole humans add @Member",
    )
    @app_commands.describe(role="Role to give to human members only")
    async def autorole_humans_add(self, ctx: commands.Context, role: discord.Role):
        await self._autorole_modify(ctx, "humans", role, add=True)

    @autorole_humans.command(
        name="remove",
        aliases=["delete", "rm"],
        description="Remove a role from the humans autorole list.",
        example=",autorole humans remove @Member",
    )
    @app_commands.describe(role="Role to remove from the humans autorole list")
    async def autorole_humans_remove(self, ctx: commands.Context, role: discord.Role):
        await self._autorole_modify(ctx, "humans", role, add=False)

    @autorole_humans.command(
        name="list",
        description="List every role given to humans only.",
        example=",autorole humans list",
    )
    async def autorole_humans_list(self, ctx: commands.Context):
        await self._autorole_list(ctx, "humans", "Autorole (humans)")

    @autorole_humans.command(
        name="reset",
        description="Clear every autorole assigned to humans only.",
        example=",autorole humans reset",
    )
    async def autorole_humans_reset(self, ctx: commands.Context):
        await self._autorole_reset(ctx, "humans", "humans")

    # autorole bots subgroup
    @autorole.group(
        name="bots",
        aliases=["clankers"],
        description="Manage autoroles given only to bot members.",
        example=",autorole bots add @Bot",
    )
    async def autorole_bots(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @autorole_bots.command(
        name="add",
        description="Add a role to be given to bot members only.",
        example=",autorole bots add @Bot",
    )
    @app_commands.describe(role="Role to give to bot members only")
    async def autorole_bots_add(self, ctx: commands.Context, role: discord.Role):
        await self._autorole_modify(ctx, "bots", role, add=True)

    @autorole_bots.command(
        name="remove",
        aliases=["delete", "rm"],
        description="Remove a role from the bots autorole list.",
        example=",autorole bots remove @Bot",
    )
    @app_commands.describe(role="Role to remove from the bots autorole list")
    async def autorole_bots_remove(self, ctx: commands.Context, role: discord.Role):
        await self._autorole_modify(ctx, "bots", role, add=False)

    @autorole_bots.command(
        name="list",
        description="List every role given to bots only.",
        example=",autorole bots list",
    )
    async def autorole_bots_list(self, ctx: commands.Context):
        await self._autorole_list(ctx, "bots", "Autorole (bots)")

    @autorole_bots.command(
        name="reset",
        description="Clear every autorole assigned to bots only.",
        example=",autorole bots reset",
    )
    async def autorole_bots_reset(self, ctx: commands.Context):
        await self._autorole_reset(ctx, "bots", "bots")
# ------------------------------------------------------------------ #
    # Commands: autoreact
    # ------------------------------------------------------------------ #

    @hybrid_group(
        name="autoreact",
        aliases=["reaction", "reactions"],
        description="Automatically react when a trigger phrase is used.",
        example=",autoreact add hello 👋",
    )
    @commands.guild_only()
    @has_permissions(manage_guild=True)
    async def autoreact(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @autoreact.command(
        name="add",
        description="Add a new autoreaction trigger.",
        example=",autoreact add hello 👋",
    )
    @app_commands.describe(
        message="Text the user must send to trigger the reaction (use /regex/ for regex)",
        emoji="The emoji to react with (custom emoji also supported)",
    )
    async def autoreact_add(
        self, ctx: commands.Context, message: str, *, emoji: str
    ):
        if not emoji:
            return await ctx.warn("Provide a valid emoji to react with.")
        try:
            await self._validate_emoji(ctx, emoji)
        except ValueError as e:
            return await ctx.warn(str(e))

        existing = await self._autoreact_find(ctx.guild.id, message)
        if existing:
            return await ctx.warn("That trigger already exists. Use **edit** to update it instead.")

        await self.bot.db.execute(
            "INSERT INTO autoreact_config (guild_id, trigger, emoji) VALUES (?, ?, ?)",
            (ctx.guild.id, message, emoji),
        )
        await self.bot.db.commit()
        await ctx.approve(f"Autoreaction added. Reacting with {emoji} on `{message}`.")

    @autoreact.command(
        name="remove",
        aliases=["delete", "rm"],
        description="Remove an autoreaction trigger.",
        example=",autoreact remove hello",
    )
    @app_commands.describe(message="The trigger text to remove")
    async def autoreact_remove(self, ctx: commands.Context, *, message: str):
        row = await self._autoreact_find(ctx.guild.id, message)
        if not row:
            return await ctx.warn("No autoreaction was found for that trigger.")
        await self.bot.db.execute(
            "DELETE FROM autoreact_config WHERE id = ?", (row["id"],)
        )
        await self.bot.db.commit()
        await ctx.approve(f"Autoreaction for `{message}` has been removed.")

    @autoreact.command(
        name="edit",
        description="Edit an existing autoreaction (change trigger or emoji).",
        example=",autoreact edit hello 👀",
    )
    @app_commands.describe(
        message="The existing trigger, with optional new message and emoji at the end",
        emoji="A new emoji to react with",
    )
    async def autoreact_edit(
        self,
        ctx: commands.Context,
        message: Optional[str] = None,
        *,
        emoji: Optional[str] = None,
    ):
        # The hybrid signature allows two positional arguments. We accept
        # either: ,autoreact edit <message> <emoji>  (explicit form)
        # or:      ,autoreact edit              (interactive)
        if message is None:
            return await hollowHelp.send_command_help(ctx, ctx.command)

        # Resolve trigger: if user provides only the trigger and no emoji,
        # show prompt-like error
        if emoji is None:
            row = await self._autoreact_find(ctx.guild.id, message)
            if not row:
                return await ctx.warn("Provide both a trigger and a new emoji, or just a trigger to look it up.")
            return await ctx.warn(
                f"Current emoji for `{message}` is `{row['emoji']}`. "
                "Provide a new emoji to update it: `,autoreact edit <trigger> <emoji>`."
            )
        try:
            await self._validate_emoji(ctx, emoji)
        except ValueError as e:
            return await ctx.warn(str(e))

        row = await self._autoreact_find(ctx.guild.id, message)
        if not row:
            return await ctx.warn("That autoreaction does not exist.")
        await self.bot.db.execute(
            "UPDATE autoreact_config SET emoji = ? WHERE id = ?",
            (emoji, row["id"]),
        )
        await self.bot.db.commit()
        await ctx.approve(f"Autoreaction updated. `{message}` now reacts with {emoji}.")

    @autoreact.command(
        name="reset",
        description="Clear every autoreaction configured in this server.",
        example=",autoreact reset",
    )
    async def autoreact_reset(self, ctx: commands.Context):
        rows = await self._autoreact_rows(ctx.guild.id)
        if not rows:
            return await ctx.warn("There are no autoreactions to reset.")
        await self.bot.db.execute(
            "DELETE FROM autoreact_config WHERE guild_id = ?", (ctx.guild.id,)
        )
        await self.bot.db.commit()
        await ctx.approve(f"All **{len(rows)}** autoreactions have been removed.")

    @autoreact.command(
        name="list",
        description="List every autoreaction trigger.",
        example=",autoreact list",
    )
    async def autoreact_list(self, ctx: commands.Context):
        rows = await self._autoreact_rows(ctx.guild.id)
        if not rows:
            return await ctx.warn("No autoreactions configured.")
        lines: list[str] = []
        for r in rows:
            try:
                channels = json.loads(r["exclusive_channels"] or "[]")
                roles = json.loads(r["exclusive_roles"] or "[]")
            except (TypeError, ValueError):
                channels, roles = [], []
            exclusivity: list[str] = []
            if channels:
                exclusivity.append(
                    "channels: " + ", ".join(f"<#{cid}>" for cid in channels)
                )
            if roles:
                exclusivity.append(
                    "roles: " + ", ".join(f"<@&{rid}>" for rid in roles)
                )
            flag = f" _({'; '.join(exclusivity)})_" if exclusivity else ""
            lines.append(
                f"`{r['trigger']}` → {r['emoji']}{flag}"
            )
        await ctx.embed(
            title="Autoreactions",
            description="\n".join(lines)[:4096],
            color=COLORS.neutral,
        )

    # autoreact exclusive subgroup
    @autoreact.group(
        name="exclusive",
        description="Make an autoreaction only fire for a specific channel or role.",
        example=",autoreact exclusive channel #general hello",
    )
    async def autoreact_exclusive(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @autoreact_exclusive.command(
        name="channel",
        description="Make an autoreaction only fire in a specific channel.",
        example=",autoreact exclusive channel #general hello",
    )
    @app_commands.describe(
        channel="Channel the trigger is restricted to",
        message="Trigger text of the autoreaction",
    )
    async def autoreact_exclusive_channel(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        *,
        message: str,
    ):
        row = await self._autoreact_find(ctx.guild.id, message)
        if not row:
            return await ctx.warn("No autoreaction was found for that trigger.")
        await self._autoreact_save_exclusive(
            ctx, row["id"], "exclusive_channels", channel.id, add=True
        )

    @autoreact_exclusive.command(
        name="role",
        description="Make an autoreaction only fire for members with a specific role.",
        example=",autoreact exclusive role @VIP hello",
    )
    @app_commands.describe(
        role="Role required to trigger the autoreaction",
        message="Trigger text of the autoreaction",
    )
    async def autoreact_exclusive_role(
        self,
        ctx: commands.Context,
        role: discord.Role,
        *,
        message: str,
    ):
        row = await self._autoreact_find(ctx.guild.id, message)
        if not row:
            return await ctx.warn("No autoreaction was found for that trigger.")
        await self._autoreact_save_exclusive(
            ctx, row["id"], "exclusive_roles", role.id, add=True
        )

    @autoreact_exclusive.command(
        name="list",
        description="Show configured exclusivity for every autoreaction.",
        example=",autoreact exclusive list",
    )
    async def autoreact_exclusive_list(self, ctx: commands.Context):
        rows = await self._autoreact_rows(ctx.guild.id)
        if not rows:
            return await ctx.warn("No autoreactions configured.")
        lines: list[str] = []
        for r in rows:
            try:
                channels = json.loads(r["exclusive_channels"] or "[]")
                roles = json.loads(r["exclusive_roles"] or "[]")
            except (TypeError, ValueError):
                channels, roles = [], []
            if not channels and not roles:
                lines.append(f"`{r['trigger']}` → {r['emoji']} _(no exclusivity)_")
                continue
            parts: list[str] = []
            if channels:
                parts.append("channels: " + ", ".join(f"<#{cid}>" for cid in channels))
            if roles:
                parts.append("roles: " + ", ".join(f"<@&{rid}>" for rid in roles))
            lines.append(f"`{r['trigger']}` → {r['emoji']} _({'; '.join(parts)})_")
        await ctx.embed(
            title="Autoreaction Exclusivity",
            description="\n".join(lines)[:4096],
            color=COLORS.neutral,
        )

    async def _validate_emoji(self, ctx: commands.Context, emoji: str) -> None:
        """Raise ValueError if emoji cannot be used in this guild."""
        if not emoji:
            raise ValueError("Provide a valid emoji to react with.")
        # discord.PartialEmoji accepts a custom emoji string like "<:name:id>"
        # as well as unicode emoji.
        try:
            partial = discord.PartialEmoji.from_str(emoji)
        except Exception as e:
            raise ValueError(f"That is not a valid emoji: `{e}`")
        if partial.is_custom_emoji() and partial.id is not None:
            # The custom emoji must come from this guild to be usable.
            if ctx.guild and ctx.guild.get_emoji(partial.id) is None:
                raise ValueError(
                    "I can only react with custom emojis that belong to this server."
                )
# ------------------------------------------------------------------ #
    # Commands: trigger (autoresponder)
    # ------------------------------------------------------------------ #

    @hybrid_group(
        name="trigger",
        aliases=["autorespond", "ares", "arespond"],
        description="Send an automatic reply when a phrase is detected.",
        example=",trigger add hello Hello {user.mention}!",
    )
    @commands.guild_only()
    @has_permissions(manage_guild=True)
    @commands.bot_has_permissions(send_messages=True)
    async def trigger(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @trigger.command(
        name="add",
        description="Create a new autoresponder trigger.",
        example=",trigger add hello Hello {user.mention}!",
    )
    @app_commands.describe(
        message="Text the user must send (use /regex/ for regex match)",
        respond="Message to send back (supports TagScript + embed script)",
    )
    async def trigger_add(self, ctx: commands.Context, message: str, *, respond: str):
        if not respond:
            return await ctx.warn("Provide a response message.")
        existing = await self._trigger_find(ctx.guild.id, message)
        if existing:
            return await ctx.warn(
                "That trigger already exists. Use **rename** to update the response."
            )
        await self.bot.db.execute(
            "INSERT INTO trigger_config (guild_id, trigger, response) VALUES (?, ?, ?)",
            (ctx.guild.id, message, respond),
        )
        await self.bot.db.commit()
        await ctx.approve(
            f"Autoresponder added: replying to `{message}` with the configured message."
        )

    @trigger.command(
        name="remove",
        aliases=["delete", "rm"],
        description="Delete an autoresponder trigger.",
        example=",trigger remove hello",
    )
    @app_commands.describe(message="The trigger text to remove")
    async def trigger_remove(self, ctx: commands.Context, *, message: str):
        row = await self._trigger_find(ctx.guild.id, message)
        if not row:
            return await ctx.warn("No autoresponder was found for that trigger.")
        await self.bot.db.execute(
            "DELETE FROM trigger_config WHERE id = ?", (row["id"],)
        )
        await self.bot.db.commit()
        await ctx.approve(f"Autoresponder for `{message}` has been removed.")

    @trigger.command(
        name="list",
        description="List every autoresponder trigger.",
        example=",trigger list",
    )
    async def trigger_list(self, ctx: commands.Context):
        rows = await self._trigger_rows(ctx.guild.id)
        if not rows:
            return await ctx.warn("No autoresponders configured.")
        lines: list[str] = []
        for r in rows:
            try:
                channels = json.loads(r["exclusive_channels"] or "[]")
                roles = json.loads(r["exclusive_roles"] or "[]")
            except (TypeError, ValueError):
                channels, roles = [], []
            exclusivity: list[str] = []
            if channels:
                exclusivity.append(
                    "channels: " + ", ".join(f"<#{cid}>" for cid in channels)
                )
            if roles:
                exclusivity.append(
                    "roles: " + ", ".join(f"<@&{rid}>" for rid in roles)
                )
            flag = f" _({'; '.join(exclusivity)})_" if exclusivity else ""
            response = (r["response"] or "").replace("\n", " ")
            if len(response) > 80:
                response = response[:77] + "..."
            lines.append(f"`{r['trigger']}` → `{response}`{flag}")
        await ctx.embed(
            title="Autoresponders",
            description="\n".join(lines)[:4096],
            color=COLORS.neutral,
        )

    @trigger.command(
        name="reset",
        description="Delete every autoresponder in this server.",
        example=",trigger reset",
    )
    async def trigger_reset(self, ctx: commands.Context):
        rows = await self._trigger_rows(ctx.guild.id)
        if not rows:
            return await ctx.warn("There are no autoresponders to reset.")
        await self.bot.db.execute(
            "DELETE FROM trigger_config WHERE guild_id = ?", (ctx.guild.id,)
        )
        await self.bot.db.commit()
        await ctx.approve(f"All **{len(rows)}** autoresponders have been removed.")

    @trigger.command(
        name="rename",
        aliases=["edit"],
        description="Replace the response of an existing autoresponder.",
        example=",trigger rename hello Hello {user.mention}!",
    )
    @app_commands.describe(
        message="Existing trigger text",
        new_respond="New response message",
    )
    async def trigger_rename(
        self, ctx: commands.Context, message: str, *, new_respond: str
    ):
        if not new_respond:
            return await ctx.warn("Provide a new response message.")
        row = await self._trigger_find(ctx.guild.id, message)
        if not row:
            return await ctx.warn("No autoresponder was found for that trigger.")
        await self.bot.db.execute(
            "UPDATE trigger_config SET response = ? WHERE id = ?",
            (new_respond, row["id"]),
        )
        await self.bot.db.commit()
        await ctx.approve(
            f"Autoresponder response updated for `{message}`."
        )

    # trigger exclusive subgroup
    @trigger.group(
        name="exclusive",
        aliases=["exc"],
        description="Restrict an autoresponder to specific channels or roles.",
        example=",trigger exclusive channeladd hello #general",
    )
    async def trigger_exclusive(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @trigger_exclusive.command(
        name="channeladd",
        aliases=["cadd"],
        description="Restrict a trigger to a specific channel.",
        example=",trigger exclusive channeladd hello #general",
    )
    @app_commands.describe(
        message="Trigger text to restrict",
        channel="Channel where the trigger is allowed",
    )
    async def trigger_exclusive_channeladd(
        self,
        ctx: commands.Context,
        message: str,
        channel: discord.TextChannel,
    ):
        row = await self._trigger_find(ctx.guild.id, message)
        if not row:
            return await ctx.warn("No autoresponder was found for that trigger.")
        await self._trigger_save_exclusive(
            ctx, row["id"], "exclusive_channels", channel.id, add=True
        )

    @trigger_exclusive.command(
        name="channelremove",
        aliases=["cremove", "cdel", "crremove"],
        description="Remove a channel restriction from a trigger.",
        example=",trigger exclusive channelremove hello",
    )
    @app_commands.describe(
        message="Trigger text to update",
        channel="Channel to remove from the exclusivity list",
    )
    async def trigger_exclusive_channelremove(
        self,
        ctx: commands.Context,
        message: str,
        channel: discord.TextChannel,
    ):
        row = await self._trigger_find(ctx.guild.id, message)
        if not row:
            return await ctx.warn("No autoresponder was found for that trigger.")
        await self._trigger_save_exclusive(
            ctx, row["id"], "exclusive_channels", channel.id, add=False
        )

    @trigger_exclusive.command(
        name="channellist",
        aliases=["clist", "channels"],
        description="Show channel restrictions for every autoresponder.",
        example=",trigger exclusive channellist",
    )
    async def trigger_exclusive_channellist(self, ctx: commands.Context):
        rows = await self._trigger_rows(ctx.guild.id)
        if not rows:
            return await ctx.warn("No autoresponders configured.")
        lines: list[str] = []
        for r in rows:
            try:
                channels = json.loads(r["exclusive_channels"] or "[]")
            except (TypeError, ValueError):
                channels = []
            if not channels:
                lines.append(f"`{r['trigger']}` _(no channel restrictions)_")
            else:
                lines.append(
                    f"`{r['trigger']}` → " + ", ".join(f"<#{cid}>" for cid in channels)
                )
        await ctx.embed(
            title="Trigger Channel Exclusivity",
            description="\n".join(lines)[:4096],
            color=COLORS.neutral,
        )

    @trigger_exclusive.command(
        name="roleadd",
        aliases=["radd"],
        description="Restrict a trigger to a specific role.",
        example=",trigger exclusive roleadd hello @VIP",
    )
    @app_commands.describe(
        message="Trigger text to restrict",
        role="Role required to invoke the autoresponder",
    )
    async def trigger_exclusive_roleadd(
        self, ctx: commands.Context, message: str, role: discord.Role
    ):
        row = await self._trigger_find(ctx.guild.id, message)
        if not row:
            return await ctx.warn("No autoresponder was found for that trigger.")
        await self._trigger_save_exclusive(
            ctx, row["id"], "exclusive_roles", role.id, add=True
        )

    @trigger_exclusive.command(
        name="roleremove",
        aliases=["rremove", "rdel"],
        description="Remove a role restriction from a trigger.",
        example=",trigger exclusive roleremove hello",
    )
    @app_commands.describe(
        message="Trigger text to update",
        role="Role to remove from the exclusivity list",
    )
    async def trigger_exclusive_roleremove(
        self, ctx: commands.Context, message: str, role: discord.Role
    ):
        row = await self._trigger_find(ctx.guild.id, message)
        if not row:
            return await ctx.warn("No autoresponder was found for that trigger.")
        await self._trigger_save_exclusive(
            ctx, row["id"], "exclusive_roles", role.id, add=False
        )

    @trigger_exclusive.command(
        name="rolelist",
        aliases=["rlist", "roles"],
        description="Show role restrictions for every autoresponder.",
        example=",trigger exclusive rolelist",
    )
    async def trigger_exclusive_rolelist(self, ctx: commands.Context):
        rows = await self._trigger_rows(ctx.guild.id)
        if not rows:
            return await ctx.warn("No autoresponders configured.")
        lines: list[str] = []
        for r in rows:
            try:
                roles = json.loads(r["exclusive_roles"] or "[]")
            except (TypeError, ValueError):
                roles = []
            if not roles:
                lines.append(f"`{r['trigger']}` _(no role restrictions)_")
            else:
                lines.append(
                    f"`{r['trigger']}` → " + ", ".join(f"<@&{rid}>" for rid in roles)
                )
        await ctx.embed(
            title="Trigger Role Exclusivity",
            description="\n".join(lines)[:4096],
            color=COLORS.neutral,
        )
# ------------------------------------------------------------------ #
    # Listeners
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        config = await self._get_autorole(member.guild.id)
        if not (config["everyone"] or (member.bot and config["bots"]) or (not member.bot and config["humans"])):
            return
        roles_to_add: list[int] = []
        roles_to_add.extend(config["everyone"])
        if member.bot:
            roles_to_add.extend(config["bots"])
        else:
            roles_to_add.extend(config["humans"])
        # Deduplicate and validate against bot's role hierarchy
        seen: set[int] = set()
        valid_role_ids: list[int] = []
        for rid in roles_to_add:
            if rid in seen:
                continue
            role = member.guild.get_role(rid)
            if role is None or role >= member.guild.me.top_role:
                continue
            valid_role_ids.append(rid)
            seen.add(rid)
        if not valid_role_ids:
            return
        try:
            await member.add_roles(
                *[discord.Object(id=rid) for rid in valid_role_ids],
                reason="Autorole on member join",
            )
        except (discord.Forbidden, discord.HTTPException):
            return

    def _build_matcher(self, trigger: str):
        """Return a callable (content) -> bool for the given trigger string."""
        match = TRIGGER_REGEX.match(trigger)
        if match:
            pattern, flags = match.groups()
            flag = 0
            for ch in flags or "":
                flag |= getattr(re, ch, 0)
            try:
                compiled = re.compile(pattern, flag)
            except re.error:
                # invalid regex → match nothing
                return lambda _c: False
            return lambda content: bool(compiled.search(content))
        lowered = trigger.lower()
        return lambda content: lowered in (content or "").lower()

    def _exclusive_passes(
        self,
        raw_channels: Optional[str],
        raw_roles: Optional[str],
        channel_id: int,
        member_role_ids: set[int],
    ) -> bool:
        """Return True if the message passes the (possibly empty) exclusivity filter."""
        try:
            channels: list[int] = json.loads(raw_channels or "[]") if raw_channels else []
            roles: list[int] = json.loads(raw_roles or "[]") if raw_roles else []
        except (TypeError, ValueError):
            channels, roles = [], []
        if channels and channel_id not in channels:
            return False
        if roles and not (set(roles) & member_role_ids):
            return False
        return True

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        # Don't react to commands or our own responses
        if message.content.startswith(tuple(await self.bot.get_prefix(message))):
            return

        guild = message.guild
        member_role_ids = {role.id for role in message.author.roles}  # type: ignore[arg-type]

        # --- Autoreact ---
        try:
            cursor = await self.bot.db.execute(
                "SELECT trigger, emoji, exclusive_channels, exclusive_roles "
                "FROM autoreact_config WHERE guild_id = ?",
                (guild.id,),
            )
            rows = await cursor.fetchall()
        except Exception:
            rows = []
        for row in rows:
            if not self._exclusive_passes(
                row["exclusive_channels"], row["exclusive_roles"],
                message.channel.id, member_role_ids,
            ):
                continue
            matcher = self._build_matcher(row["trigger"])
            if not matcher(message.content or ""):
                continue
            emoji = row["emoji"]
            try:
                await message.add_reaction(emoji)
            except (discord.HTTPException, discord.Forbidden):
                continue

        # --- Triggers (autoresponder) ---
        try:
            cursor = await self.bot.db.execute(
                "SELECT trigger, response, exclusive_channels, exclusive_roles "
                "FROM trigger_config WHERE guild_id = ?",
                (guild.id,),
            )
            rows = await cursor.fetchall()
        except Exception:
            rows = []
        for row in rows:
            if not self._exclusive_passes(
                row["exclusive_channels"], row["exclusive_roles"],
                message.channel.id, member_role_ids,
            ):
                continue
            matcher = self._build_matcher(row["trigger"])
            if not matcher(message.content or ""):
                continue
            try:
                kwargs = await self._render_response(
                    row["response"], message.author, message.channel
                )
            except Exception:
                continue
            try:
                await message.channel.send(**kwargs)
            except (discord.Forbidden, discord.HTTPException):
                continue
            # Only one autoresponder fires per message to avoid spam.
            break
