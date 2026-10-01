"""Ticket system.

A full support-ticket flow:

* members open a **private** text channel (visible only to the opener, the
  configured support role(s) and administrators) from a panel button or from
  ``,ticket open``
* support staff claim, rename, add/remove members and close tickets
* closing a ticket saves a transcript (HTML or TXT) to the log channel and
  deletes the channel

Every subcommand is a hybrid command, so it works both as a slash command
(``/ticket open``) and as a prefix command (``,ticket open``). Running the
group on its own prints the help paginator through ``hollowHelp``.
"""

from __future__ import annotations

import html
import io
import json
import time
from typing import Any, Optional

import discord
from discord.ext import commands, tasks

from core.client.commands import has_permissions, hybrid_group
from core.client.embed import Embed
from core.config import COLORS
from core.context import hollowHelp

# Channel name template — supports {user}, {id} and {reason}.
DEFAULT_NAME = "ticket-{user}"
MAX_TRANSCRIPT_MESSAGES = 3000


# ============================================================================ #
# Helpers
# ============================================================================ #


def _default_config() -> dict[str, Any]:
    return {
        "guild_id": None,
        "category_id": None,
        "log_channel_id": None,
        "support_roles": "[]",
        "panel_title": "Support Tickets",
        "panel_description": (
            "Need help? Press the button below to open a private ticket channel.\n"
            "A member of the support team will be with you shortly."
        ),
        "panel_emoji": "\U0001f3e1",
        "button_label": "Open a Ticket",
        "max_open": 1,
        "transcript_format": "html",
        "delete_on_close": 1,
        "dm_on_close": 1,
    }


def _json_list(raw: Any) -> list[int]:
    try:
        return [int(x) for x in json.loads(raw or "[]")]
    except (TypeError, ValueError):
        return []


class _TicketSelect(discord.ui.Select):
    """Select used by the setup wizard."""

    def __init__(self, placeholder: str, options: list[discord.SelectOption], custom_id: str):
        super().__init__(placeholder=placeholder, options=options, custom_id=custom_id, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:  # pragma: no cover
        # Routed centrally through ``Tickets.on_interaction`` so the wizard
        # works even after a restart (custom_ids are not stateful).
        return None


class TicketSetupView(discord.ui.View):
    """Step 1 of the wizard: pick the category that will hold ticket channels."""

    _classic_passthrough = True

    def __init__(self, categories: list[discord.CategoryChannel]):
        super().__init__(timeout=300)
        self.add_item(
            _TicketSelect(
                "Ticket category...",
                [discord.SelectOption(label=c.name, value=str(c.id)) for c in categories[:25]],
                "ticket:setup:category",
            )
        )


class TicketSetupRoleView(discord.ui.View):
    """Step 2 of the wizard: pick the support role(s)."""

    _classic_passthrough = True

    def __init__(self, roles: list[discord.Role], selected: list[int]):
        super().__init__(timeout=300)
        self.add_item(
            _TicketSelect(
                "Support role...",
                [
                    discord.SelectOption(label=r.name, value=str(r.id), default=r.id in selected)
                    for r in roles[:25]
                ],
                "ticket:setup:role",
            )
        )


class TicketSetupLogView(discord.ui.View):
    """Step 3 of the wizard: pick the transcript log channel."""

    _classic_passthrough = True

    def __init__(self, channels: list[discord.TextChannel], selected: Optional[int]):
        super().__init__(timeout=300)
        options = [discord.SelectOption(label="no log channel", value="none", default=selected is None)]
        options += [
            discord.SelectOption(label=c.name, value=str(c.id), default=c.id == selected)
            for c in channels[:24]
        ]
        self.add_item(_TicketSelect("Transcript log channel...", options, "ticket:setup:log"))


class TicketPanelModal(discord.ui.Modal, title="Ticket panel"):
    """Step 4 of the wizard: the embed shown on the ticket panel."""

    def __init__(self, cog: "Tickets"):
        super().__init__()
        self.cog = cog
        self.add_item(
            discord.ui.TextInput(
                label="Panel title",
                placeholder="Support Tickets",
                custom_id="title",
                required=True,
                max_length=256,
                style=discord.TextStyle.short,
            )
        )
        self.add_item(
            discord.ui.TextInput(
                label="Panel message",
                placeholder="Press the button below to open a ticket.",
                custom_id="description",
                required=True,
                max_length=1800,
                style=discord.TextStyle.paragraph,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cfg = await self.cog.get_config(interaction.guild_id)
        cfg["panel_title"] = self.title.value
        cfg["panel_description"] = self.description.value
        await self.cog.save_config(interaction.guild_id, cfg)
        roles = ", ".join(f"<@&{r}>" for r in _json_list(cfg.get("support_roles"))) or "none"
        await interaction.response.send_message(
            embed=Embed(
                title="Ticket system configured",
                description=(
                    f"**Category:** <#{cfg['category_id']}>\n"
                    f"**Support roles:** {roles}\n"
                    f"**Log channel:** {'<#' + str(cfg['log_channel_id']) + '>' if cfg['log_channel_id'] else 'none'}\n\n"
                    f"Run **,ticket panel** to post the panel."
                ),
                color=COLORS.approve,
            ),
            ephemeral=True,
        )


class TicketOpenView(discord.ui.View):
    """The panel posted by ``,ticket panel``."""

    _classic_passthrough = True

    def __init__(self, guild_id: int, label: str = "Open a Ticket"):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.add_item(
            discord.ui.Button(
                label=label,
                emoji="\U0001f3e1",
                style=discord.ButtonStyle.primary,
                custom_id=f"ticket:open:{guild_id}",
                row=0,
            )
        )


class TicketCloseView(discord.ui.View):
    """Close button rendered inside every open ticket channel."""

    _classic_passthrough = True

    def __init__(self, ticket_id: int):
        super().__init__(timeout=None)
        self.ticket_id = ticket_id
        self.add_item(
            discord.ui.Button(
                label="Close Ticket",
                emoji="\U0001f6d1",
                style=discord.ButtonStyle.danger,
                custom_id=f"ticket:close:{ticket_id}",
                row=0,
            )
        )


# ============================================================================ #
# Cog
# ============================================================================ #


class Tickets(commands.Cog):
    """Private support tickets with transcripts, staff roles and a panel."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        for guild in self.bot.guilds:
            self.bot.add_view(TicketOpenView(guild.id))
        self._restore_views.start()

    async def cog_unload(self) -> None:
        self._restore_views.cancel()

    @tasks.loop(minutes=30)
    async def _restore_views(self) -> None:
        """Re-register panel views for guilds that appeared after start-up."""
        known = {getattr(v, "guild_id", None) for v in self.bot.views}
        for guild in self.bot.guilds:
            if guild.id not in known:
                self.bot.add_view(TicketOpenView(guild.id))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """A ticket channel removed by hand leaves a stale open row behind."""
        if not isinstance(channel, discord.TextChannel):
            return
        await self.bot.db.execute(
            "UPDATE tickets SET status = 'deleted', closed_at = ? "
            "WHERE channel_id = ? AND status = 'open'",
            (int(time.time()), channel.id),
        )

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------

    async def get_config(self, guild_id: int) -> dict[str, Any]:
        row = await (
            await self.bot.db.execute(
                "SELECT * FROM tickets_config WHERE guild_id = ?", (guild_id,)
            )
        ).fetchone()
        if not row:
            return _default_config() | {"guild_id": guild_id}
        return dict(row)

    async def save_config(self, guild_id: int, config: dict[str, Any]) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO tickets_config (
                guild_id, category_id, log_channel_id, support_roles,
                panel_title, panel_description, panel_emoji, button_label,
                max_open, transcript_format, delete_on_close, dm_on_close
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                category_id = excluded.category_id,
                log_channel_id = excluded.log_channel_id,
                support_roles = excluded.support_roles,
                panel_title = excluded.panel_title,
                panel_description = excluded.panel_description,
                panel_emoji = excluded.panel_emoji,
                button_label = excluded.button_label,
                max_open = excluded.max_open,
                transcript_format = excluded.transcript_format,
                delete_on_close = excluded.delete_on_close,
                dm_on_close = excluded.dm_on_close
            """,
            (
                guild_id,
                config.get("category_id"),
                config.get("log_channel_id"),
                json.dumps(_json_list(config.get("support_roles"))),
                config.get("panel_title") or "Support Tickets",
                config.get("panel_description") or "Open a ticket to get help.",
                config.get("panel_emoji") or "\U0001f3e1",
                config.get("button_label") or "Open a Ticket",
                int(config.get("max_open") or 1),
                (config.get("transcript_format") or "html").lower(),
                int(bool(config.get("delete_on_close", 1))),
                int(bool(config.get("dm_on_close", 1))),
            ),
        )

    async def next_number(self, guild_id: int) -> int:
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO ticket_counters (guild_id, counter) VALUES (?, 0)", (guild_id,)
        )
        await self.bot.db.execute(
            "UPDATE ticket_counters SET counter = counter + 1 WHERE guild_id = ?", (guild_id,)
        )
        row = await (
            await self.bot.db.execute(
                "SELECT counter FROM ticket_counters WHERE guild_id = ?", (guild_id,)
            )
        ).fetchone()
        return int(row["counter"])

    async def get_ticket_by_channel(self, channel_id: int) -> Optional[Any]:
        return await (
            await self.bot.db.execute(
                "SELECT * FROM tickets WHERE channel_id = ? AND status = 'open'", (channel_id,)
            )
        ).fetchone()

    async def is_staff(self, guild: discord.Guild, member: discord.Member, config: dict[str, Any]) -> bool:
        """Support staff = configured support role(s), admins or the bot owner."""
        if member.guild_permissions.administrator or await self.bot.is_owner(member):
            return True
        roles = set(_json_list(config.get("support_roles")))
        return any(r.id in roles for r in member.roles)

    # ------------------------------------------------------------------
    # Ticket lifecycle
    # ------------------------------------------------------------------

    async def open_ticket(
        self,
        guild: discord.Guild,
        opener: discord.Member,
        config: dict[str, Any],
        reason: Optional[str] = None,
    ) -> tuple[Optional[discord.TextChannel], Optional[Any], Optional[str]]:
        """Create the private channel plus its DB row. Returns (channel, ticket, error)."""
        if not config.get("category_id"):
            return None, None, "The ticket system isn't set up yet — an admin needs to run `,ticket setup`."

        blacklisted = await (
            await self.bot.db.execute(
                "SELECT 1 FROM ticket_blacklist WHERE guild_id = ? AND user_id = ?",
                (guild.id, opener.id),
            )
        ).fetchone()
        if blacklisted:
            return None, None, "You are blacklisted from opening tickets in this server."

        open_row = await (
            await self.bot.db.execute(
                "SELECT COUNT(*) AS c FROM tickets WHERE guild_id = ? AND opener_id = ? AND status = 'open'",
                (guild.id, opener.id),
            )
        ).fetchone()
        limit = int(config.get("max_open") or 1)
        if int(open_row["c"]) >= limit:
            return None, None, f"You already have **{open_row['c']}** open ticket(s) — the limit is **{limit}**."

        number = await self.next_number(guild.id)
        reason = (reason or "No reason given").strip()[:200]

        category = guild.get_channel(int(config["category_id"]))
        if not isinstance(category, discord.CategoryChannel):
            return None, None, "The configured ticket category no longer exists."

        name = DEFAULT_NAME.format(user=opener.name, id=number, reason=reason)
        try:
            channel = await guild.create_text_channel(
                name=name[:100],
                category=category,
                topic=f"Ticket #{number} — opened by {opener} ({opener.id})\nReason: {reason}",
                overwrites={
                    guild.default_role: discord.PermissionOverwrite(
                        view_channel=False, send_messages=False, read_message_history=False
                    ),
                    opener: discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        attach_files=True,
                    ),
                },
            )
        except discord.Forbidden:
            return None, None, "I lack permission to create channels in that category."
        except discord.HTTPException as exc:
            return None, None, f"Could not create the ticket channel: {exc}"

        for role_id in _json_list(config.get("support_roles")):
            role = guild.get_role(role_id)
            if role is not None:
                await channel.set_permissions(
                    role, view_channel=True, send_messages=True, read_message_history=True
                )

        await self.bot.db.execute(
            "INSERT INTO tickets (guild_id, channel_id, number, opener_id, reason, status) "
            "VALUES (?, ?, ?, ?, ?, 'open')",
            (guild.id, channel.id, number, opener.id, reason),
        )
        ticket = await self.get_ticket_by_channel(channel.id)
        return channel, ticket, None

    async def build_transcript(
        self, channel: discord.TextChannel, ticket: Any, fmt: str
    ) -> tuple[discord.File, str]:
        """Render the channel history into a (file, plain-text) transcript."""
        stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        try:
            opener = await channel.guild.fetch_member(ticket["opener_id"])
        except discord.HTTPException:
            opener = None
        opener_tag = f"{opener} (`{ticket['opener_id']}`)" if opener else f"`{ticket['opener_id']}`"

        header = [
            f"Ticket #{ticket['number']}",
            f"Channel: {channel.name} (`{channel.id}`)",
            f"Guild: {channel.guild.name} (`{channel.guild.id}`)",
            f"Opened by: {opener_tag}",
            f"Reason: {ticket['reason']}",
            f"Generated: {stamp}",
        ]

        messages: list[discord.Message] = []
        async for message in channel.history(limit=MAX_TRANSCRIPT_MESSAGES, oldest_first=True):
            messages.append(message)

        lines: list[str] = []
        for message in messages:
            when = message.created_at.strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"[{when}] {message.author} ({message.author.id}): {message.content}".rstrip())
            for attachment in message.attachments:
                lines.append(f"    attachment: {attachment.url}")

        txt = "\n".join([*header, ""] + (lines or ["(no messages)"]))

        if fmt == "html":
            body = (
                "".join(
                    f"<div class='msg'><span class='meta'>"
                    f"[{html.escape(m.created_at.strftime('%Y-%m-%d %H:%M:%S'))}] "
                    f"<b>{html.escape(str(m.author))}</b> ({m.author.id})</span><br/>"
                    f"{html.escape(m.content) or '<i>(no text)</i>'}"
                    + "".join(
                        f"<br/><a href='{a.url}'>attachment: {html.escape(a.filename)}</a>"
                        for a in m.attachments
                    )
                    + "</div>"
                    for m in messages
                )
                or "<div class='msg'><i>(no messages)</i></div>"
            )
            document = (
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                f"<title>Ticket #{ticket['number']} transcript</title>"
                "<style>body{background:#1e1f22;color:#dbdee1;font-family:monospace}"
                ".msg{margin:8px 0;padding:6px;background:#2b2d31;border-radius:4px}"
                ".meta{color:#949ba4}</style></head><body>"
                f"<h2>{html.escape(chr(10).join(header))}</h2>{body}"
                "</body></html>"
            )
            data, extension = document.encode("utf-8"), "html"
        else:
            data, extension = txt.encode("utf-8"), "txt"

        return discord.File(io.BytesIO(data), filename=f"ticket-{ticket['number']}.{extension}"), txt

    async def close_ticket(
        self,
        channel: discord.TextChannel,
        ticket: Any,
        closer: discord.Member,
        reason: str,
    ) -> Optional[discord.Message]:
        config = await self.get_config(channel.guild.id)
        file, _ = await self.build_transcript(channel, ticket, config.get("transcript_format") or "html")

        embed = Embed(
            title=f"Ticket #{ticket['number']} closed",
            description=(
                f"**Channel:** `{channel.name}`\n"
                f"**Opened by:** <@{ticket['opener_id']}>\n"
                f"**Closed by:** {closer.mention}\n"
                f"**Reason:** {reason or 'Not provided'}"
            ),
            color=COLORS.neutral,
        )

        message: Optional[discord.Message] = None
        log_channel = (
            channel.guild.get_channel(int(config["log_channel_id"])) if config.get("log_channel_id") else None
        )
        if isinstance(log_channel, discord.TextChannel):
            try:
                message = await log_channel.send(embed=embed, file=file)
            except discord.Forbidden:
                pass

        await self.bot.db.execute(
            "UPDATE tickets SET status = 'closed', closed_by = ?, close_reason = ?, closed_at = ? WHERE id = ?",
            (closer.id, reason, int(time.time()), ticket["id"]),
        )

        if int(config.get("dm_on_close", 1)):
            try:
                opener = await channel.guild.fetch_member(ticket["opener_id"])
                if opener is not None:
                    await opener.send(
                        embed=Embed(
                            title="Your ticket was closed",
                            description=(
                                f"Ticket **#{ticket['number']}** in *{channel.guild.name}* was closed by "
                                f"{closer.mention}.\nReason: **{reason or 'Not provided'}**"
                            ),
                            color=COLORS.warn,
                        )
                    )
            except (discord.Forbidden, discord.HTTPException):
                pass

        if int(config.get("delete_on_close", 1)):
            try:
                await channel.delete(reason=f"Ticket #{ticket['number']} closed by {closer}")
            except discord.HTTPException:
                pass
        return message

    # ------------------------------------------------------------------
    # Panel + wizard interaction routing
    #
    # Every ticket custom_id is handled by one central listener so buttons
    # keep working after a restart (custom_ids are stateless strings).
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        if not custom_id.startswith("ticket:"):
            return

        if custom_id.startswith("ticket:open:"):
            await self.handle_open_button(interaction)
        elif custom_id.startswith("ticket:close:"):
            await self.handle_close_button(interaction)
        elif custom_id.startswith("ticket:setup:"):
            await self.handle_setup_step(interaction, (interaction.data.get("values") or ["none"])[0])

    def panel_embed(self, config: dict[str, Any]) -> discord.Embed:
        return Embed(
            title=config.get("panel_title") or "Support Tickets",
            description=config.get("panel_description") or "Open a ticket to get help.",
            color=COLORS.neutral,
        ).to_classic_embed()

    async def handle_open_button(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        config = await self.get_config(guild.id)
        channel, ticket, error = await self.open_ticket(guild, interaction.user, config)
        if error:
            return await interaction.followup.send(
                embed=Embed(description=error, color=COLORS.deny), ephemeral=True
            )

        try:
            await channel.send(
                embed=Embed(
                    title=f"Ticket #{ticket['number']}",
                    description=(
                        f"Welcome {interaction.user.mention} — a support member will be with you shortly.\n"
                        f"**Reason:** {ticket['reason']}"
                    ),
                    color=COLORS.approve,
                ).to_classic_embed(),
                view=TicketCloseView(ticket["id"]),
            )
        except discord.Forbidden:
            pass

        await interaction.followup.send(
            embed=Embed(description=f"Opened {channel.mention}.", color=COLORS.approve),
            ephemeral=True,
        )

    async def handle_close_button(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        ticket_id = int(interaction.data["custom_id"].split(":")[-1])
        row = await (
            await self.bot.db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        ).fetchone()
        if not row or row["status"] != "open":
            return await interaction.followup.send(
                embed=Embed(description="This ticket is no longer open.", color=COLORS.deny),
                ephemeral=True,
            )

        config = await self.get_config(interaction.guild.id)
        allowed = await self.is_staff(interaction.guild, interaction.user, config)
        if not allowed and interaction.user.id != row["opener_id"]:
            return await interaction.followup.send(
                embed=Embed(description="Only staff or the ticket owner can close this.", color=COLORS.deny),
                ephemeral=True,
            )

        channel = interaction.guild.get_channel(row["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return await interaction.followup.send(
                embed=Embed(description="That ticket channel no longer exists.", color=COLORS.deny),
                ephemeral=True,
            )

        await self.close_ticket(channel, row, interaction.user, "Closed with the close button")
        await interaction.followup.send(
            embed=Embed(description="Ticket closed and archived.", color=COLORS.approve),
            ephemeral=True,
        )

    async def handle_setup_step(self, interaction: discord.Interaction, value: str) -> None:
        custom_id = interaction.data["custom_id"]
        guild = interaction.guild
        config = await self.get_config(guild.id)

        if custom_id == "ticket:setup:category":
            config["category_id"] = int(value)
            await self.save_config(guild.id, config)
            roles = [r for r in guild.roles if not r.is_default() and r != guild.me.top_role]
            await interaction.response.send_message(
                embed=Embed(
                    description="Now pick the support role(s) that can see and manage every ticket.",
                    color=COLORS.neutral,
                ),
                view=TicketSetupRoleView(roles, _json_list(config.get("support_roles"))),
                ephemeral=True,
            )

        elif custom_id == "ticket:setup:role":
            config["support_roles"] = [int(x) for x in value.split(",")]
            await self.save_config(guild.id, config)
            channels = [c for c in guild.text_channels if c != interaction.channel]
            await interaction.response.send_message(
                embed=Embed(
                    description="Pick the channel transcripts get sent to, or choose `no log channel`.",
                    color=COLORS.neutral,
                ),
                view=TicketSetupLogView(channels, config.get("log_channel_id")),
                ephemeral=True,
            )

        elif custom_id == "ticket:setup:log":
            config["log_channel_id"] = None if value == "none" else int(value)
            await self.save_config(guild.id, config)
            await interaction.response.send_message(
                embed=Embed(description="Last step — set the panel title and message.", color=COLORS.neutral),
                ephemeral=True,
            )
            await interaction.followup.send_modal(TicketPanelModal(self))

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.hybrid_group(
        name="ticket",
        aliases=["tickets", "tk"],
        description="Support ticket system",
        invoke_without_command=True,
    )
    @commands.guild_only()
    async def ticket(self, ctx: commands.Context) -> None:
        """Open the ticket system help"""
        #"""Show the ticket system status and its subcommands."""
        # config = await self.get_config(ctx.guild.id)
        # row = await (
        #     await self.bot.db.execute(
        #         "SELECT COUNT(*) AS c FROM tickets WHERE guild_id = ? AND status = 'open'",
        #         (ctx.guild.id,),
        #     )
        # ).fetchone()

        # embed = (
        #     Embed(
        #         title="Ticket system",
        #         description=(
        #             "Open a ticket with **,ticket open** or the button on the server panel.\n"
        #             "Inside a ticket: `close`, `rename`, `add`, `remove`, `claim`, `unclaim`."
        #         ),
        #         color=COLORS.neutral,
        #     )
        #     .add_field(
        #         name="Status",
        #         value="Configured" if config.get("category_id") else "**Not configured** — run `,ticket setup`",
        #     )
        #     .add_field(name="Open tickets", value=str(row["c"]))
        #     .add_field(name="Limit per member", value=str(config.get("max_open") or 1))
        #     .add_field(
        #         name="Support roles",
        #         value=", ".join(f"<@&{r}>" for r in _json_list(config.get("support_roles"))) or "none",
        #     )
        # )
        # await ctx.send(embed=embed)
        await hollowHelp.send_group_help(ctx, ctx.command)

    @ticket.command(
        name="setup",
        description="Interactive ticket system setup wizard")
    @commands.guild_only()
    @has_permissions(administrator=True)
    async def ticket_setup(self, ctx: commands.Context) -> None:
        """Run the interactive wizard to configure the ticket system."""
        categories = list(ctx.guild.categories)[:25]
        if not categories:
            return await ctx.deny("This server has no categories — create one first.")
        await ctx.send(
            embed=Embed(description="Pick the category that will hold ticket channels.", color=COLORS.neutral),
            view=TicketSetupView(categories),
        )

    @ticket.command(
        name="panel",
        description="Post the ticket panel button")
    @commands.guild_only()
    @has_permissions(administrator=True)
    @commands.bot_has_permissions(send_messages=True)
    async def ticket_panel(self, ctx: commands.Context, channel: discord.TextChannel = None) -> None:
        """Post (or repost) the ticket-opening panel in a channel."""
        config = await self.get_config(ctx.guild.id)
        if not config.get("category_id"):
            return await ctx.deny("Run `,ticket setup` first.")
        target = channel or ctx.channel
        view = TicketOpenView(ctx.guild.id, config.get("button_label") or "Open a Ticket")
        self.bot.add_view(view)
        await target.send(embed=self.panel_embed(config), view=view)
        await ctx.approve(f"Panel posted in {target.mention}.")

    @ticket.command(
        name="open",
        description="Open a new ticket for yourself")
    @commands.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def ticket_open(self, ctx: commands.Context, reason: str = None) -> None:
        """Manually open a new ticket for yourself."""
        config = await self.get_config(ctx.guild.id)
        channel, ticket, error = await self.open_ticket(ctx.guild, ctx.author, config, reason)
        if error:
            return await ctx.deny(error)
        try:
            await channel.send(
                embed=Embed(
                    title=f"Ticket #{ticket['number']}",
                    description=f"**Reason:** {ticket['reason']}",
                    color=COLORS.approve,
                ).to_classic_embed(),
                view=TicketCloseView(ticket["id"]),
            )
        except discord.Forbidden:
            pass
        await ctx.approve(f"Opened {channel.mention}.")

    @ticket.command(
        name="close",
        description="Close the current ticket channel")
    @commands.guild_only()
    async def ticket_close(self, ctx: commands.Context, reason: str = None) -> None:
        """Close the ticket channel you are currently in."""
        ticket = await self.get_ticket_by_channel(ctx.channel.id)
        if not ticket:
            return await ctx.deny("This is not a ticket channel.")
        config = await self.get_config(ctx.guild.id)
        if not await self.is_staff(ctx.guild, ctx.author, config) and ctx.author.id != ticket["opener_id"]:
            return await ctx.deny("Only staff or the ticket owner can close this.")
        await ctx.approve("Closing the ticket…")
        await self.close_ticket(ctx.channel, ticket, ctx.author, reason or "Not provided")

    @ticket.command(
        name="add",
        description="Add a member to the current ticket")
    @commands.guild_only()
    @has_permissions(manage_channels=True)
    async def ticket_add(self, ctx: commands.Context, user: discord.Member) -> None:
        """Add a member to the current ticket channel."""
        ticket = await self.get_ticket_by_channel(ctx.channel.id)
        if not ticket:
            return await ctx.deny("This is not a ticket channel.")
        await ctx.channel.set_permissions(
            user,
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            reason=f"Added to ticket #{ticket['number']}",
        )
        await ctx.approve(f"{user.mention} was added to this ticket.")

    @ticket.command(
        name="remove",
        description="Remove a member from the current ticket")
    @commands.guild_only()
    @has_permissions(manage_channels=True)
    async def ticket_remove(self, ctx: commands.Context, user: discord.Member) -> None:
        """Remove a member from the current ticket channel."""
        ticket = await self.get_ticket_by_channel(ctx.channel.id)
        if not ticket:
            return await ctx.deny("This is not a ticket channel.")
        await ctx.channel.set_permissions(
            user, overwrite=None, reason=f"Removed from ticket #{ticket['number']}"
        )
        await ctx.approve(f"{user.mention} was removed from this ticket.")

    @ticket.command(
        name="rename",
        description="Rename the current ticket channel")
    @commands.guild_only()
    @has_permissions(manage_channels=True)
    async def ticket_rename(self, ctx: commands.Context, name: str) -> None:
        """Rename the current ticket channel."""
        ticket = await self.get_ticket_by_channel(ctx.channel.id)
        if not ticket:
            return await ctx.deny("This is not a ticket channel.")
        await ctx.channel.edit(name=name[:100])
        await ctx.approve(f"Channel renamed to **{name}**.")

    @ticket.command(
        name="claim",
        description="Claim the current ticket")
    @commands.guild_only()
    async def ticket_claim(self, ctx: commands.Context) -> None:
        """Claim the current ticket as the handling staff member."""
        ticket = await self.get_ticket_by_channel(ctx.channel.id)
        if not ticket:
            return await ctx.deny("This is not a ticket channel.")
        config = await self.get_config(ctx.guild.id)
        if not await self.is_staff(ctx.guild, ctx.author, config):
            return await ctx.deny("You are not support staff.")
        if ticket["staff_id"] and ticket["staff_id"] != ctx.author.id:
            staff = ctx.guild.get_member(ticket["staff_id"])
            return await ctx.deny(
                f"This ticket is already claimed by {staff.mention if staff else 'someone else'}."
            )
        await self.bot.db.execute("UPDATE tickets SET staff_id = ? WHERE id = ?", (ctx.author.id, ticket["id"]))
        await ctx.approve(f"{ctx.author.mention} claimed ticket #{ticket['number']}.")

    @ticket.command(
        name="unclaim",
        description="Release your claim on the current ticket")
    @commands.guild_only()
    async def ticket_unclaim(self, ctx: commands.Context) -> None:
        """Release your claim on the current ticket."""
        ticket = await self.get_ticket_by_channel(ctx.channel.id)
        if not ticket:
            return await ctx.deny("This is not a ticket channel.")
        if not ticket["staff_id"] or ticket["staff_id"] != ctx.author.id:
            return await ctx.deny("You are not the claimed handler of this ticket.")
        await self.bot.db.execute("UPDATE tickets SET staff_id = NULL WHERE id = ?", (ticket["id"],))
        await ctx.approve("You released this ticket.")

    @ticket.command(
        name="transcript",
        description="Generate a transcript of a ticket")
    @commands.guild_only()
    @has_permissions(manage_channels=True)
    async def ticket_transcript(self, ctx: commands.Context, ticket_id: int = None) -> None:
        """Generate an export of a ticket transcript (current channel or by id)."""
        config = await self.get_config(ctx.guild.id)
        if ticket_id:
            row = await (
                await self.bot.db.execute(
                    "SELECT * FROM tickets WHERE guild_id = ? AND number = ?", (ctx.guild.id, ticket_id)
                )
            ).fetchone()
        else:
            row = await self.get_ticket_by_channel(ctx.channel.id)
        if not row:
            return await ctx.deny("No matching ticket found.")
        channel = ctx.guild.get_channel(row["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return await ctx.deny("That ticket's channel no longer exists.")
        async with ctx.typing():
            file, _ = await self.build_transcript(channel, row, config.get("transcript_format") or "html")
        await ctx.send(
            embed=Embed(description=f"Transcript for ticket **#{row['number']}**.", color=COLORS.neutral),
            file=file,
        )

    @ticket.command(
        name="list",
        description="List all open tickets")
    @commands.guild_only()
    @has_permissions(manage_channels=True)
    async def ticket_list(self, ctx: commands.Context) -> None:
        """List every open ticket in this server."""
        rows = await (
            await self.bot.db.execute(
                "SELECT * FROM tickets WHERE guild_id = ? AND status = 'open' ORDER BY number DESC",
                (ctx.guild.id,),
            )
        ).fetchall()
        if not rows:
            return await ctx.approve("There are no open tickets.")

        lines = []
        for row in rows:
            staff = ctx.guild.get_member(row["staff_id"]) if row["staff_id"] else None
            handled = f" — {staff.mention}" if staff else ""
            channel = ctx.guild.get_channel(row["channel_id"])
            lines.append(
                f"`#{row['number']}` {channel.mention if channel else '*deleted*'} • "
                f"<@{row['opener_id']}>{handled} • {row['reason'][:60]}"
            )
        await ctx.send(
            embed=Embed(
                title=f"Open tickets ({len(rows)})",
                description="\n".join(lines)[:4000],
                color=COLORS.neutral,
            )
        )

    @ticket.command(
        name="blacklist",
        description="Block a user from opening tickets")
    @commands.guild_only()
    @has_permissions(manage_channels=True)
    async def ticket_blacklist(self, ctx: commands.Context, user: discord.Member = None) -> None:
        """Block a user from opening tickets. Run without a user to list the blacklist."""
        if user is None:
            rows = await (
                await self.bot.db.execute(
                    "SELECT user_id FROM ticket_blacklist WHERE guild_id = ? ORDER BY created_at DESC",
                    (ctx.guild.id,),
                )
            ).fetchall()
            if not rows:
                return await ctx.approve("The ticket blacklist is empty.")
            listing = "\n".join(f"<@{row['user_id']}>" for row in rows[:100])
            return await ctx.send(
                embed=Embed(title=f"Ticket blacklist ({len(rows)})", description=listing, color=COLORS.neutral)
            )

        await self.bot.db.execute(
            "INSERT OR REPLACE INTO ticket_blacklist (guild_id, user_id, moderator_id) VALUES (?, ?, ?)",
            (ctx.guild.id, user.id, ctx.author.id),
        )
        await ctx.approve(f"{user.mention} can no longer open tickets.")

    @ticket.command(
        name="unblacklist",
        description="Unblock a user from opening tickets")
    @commands.guild_only()
    @has_permissions(manage_channels=True)
    async def ticket_unblacklist(self, ctx: commands.Context, user: discord.Member) -> None:
        """Remove a user from the ticket blacklist."""
        cursor = await self.bot.db.execute(
            "DELETE FROM ticket_blacklist WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, user.id)
        )
        if not cursor.rowcount:
            return await ctx.deny(f"{user.mention} is not blacklisted.")
        await ctx.approve(f"{user.mention} may open tickets again.")

    @ticket.command(
        name="limit",
        description="Set the max open tickets per user")
    @commands.guild_only()
    @has_permissions(administrator=True)
    async def ticket_limit(self, ctx: commands.Context, amount: int) -> None:
        """Set the maximum number of tickets a member may have open at once."""
        if amount < 1 or amount > 25:
            return await ctx.deny("The limit must be between 1 and 25.")
        config = await self.get_config(ctx.guild.id)
        config["max_open"] = amount
        await self.save_config(ctx.guild.id, config)
        await ctx.approve(f"Members may now have up to **{amount}** open ticket(s).")

    @ticket.command(
        name="category",
        description="Set the category tickets are created under")
    @commands.guild_only()
    @has_permissions(administrator=True)
    async def ticket_category(self, ctx: commands.Context, category: discord.CategoryChannel) -> None:
        """Set/change the category that ticket channels are created in."""
        config = await self.get_config(ctx.guild.id)
        config["category_id"] = category.id
        await self.save_config(ctx.guild.id, config)
        await ctx.approve(f"Tickets will now be created under **{category.name}**.")

    @ticket.command(
        name="supportrole",
        description="Add a role that can view and manage all tickets")
    @commands.guild_only()
    @has_permissions(administrator=True)
    async def ticket_supportrole(self, ctx: commands.Context, role: discord.Role) -> None:
        """Add a support role. Pass the same role again to remove it."""
        config = await self.get_config(ctx.guild.id)
        roles = _json_list(config.get("support_roles"))
        if role.id in roles:
            roles.remove(role.id)
            action = "removed from"
        else:
            roles.append(role.id)
            action = "granted ticket access by"
        config["support_roles"] = roles
        await self.save_config(ctx.guild.id, config)
        await ctx.approve(f"{role.mention} was {action} the support roles.")


async def setup(bot) -> None:
    await bot.add_cog(Tickets(bot))
