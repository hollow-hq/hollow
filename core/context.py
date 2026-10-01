from typing import Any, Dict, List, Optional, TYPE_CHECKING, Union
from contextlib import suppress
from logging import getLogger

import discord
from discord import ButtonStyle
from discord.ext import commands
from discord.ui import (
    Button,
    Section,
    Select,
    View,
)
from core.client.embed import Embed
from core.config import COLORS, EMOJIS
if TYPE_CHECKING:
    from core.hollow import hollow

log = getLogger("hollow/context")

# --------------------------------------------------------------------------- #
#  ANSI helpers                                                               #
# --------------------------------------------------------------------------- #
_RESET = "\033[0m"


def _ansi(value: str, *codes: int) -> str:
    """Wrap *value* with the given ANSI escape codes (renders in ```ansi blocks)."""
    prefix = "".join(f"\033[{code}m" for code in codes)
    return f"{prefix}{value}{_RESET}"


def _find_select(root: Any, custom_id: str) -> Optional[Select]:
    """
    Locate a Select matching ``custom_id`` anywhere inside ``root``.

    The bot's edit-wrapper (see ``core/client/embed.py``) turns a plain
    ``discord.ui.View`` into a ``LayoutView(Container(..., ActionRow(Select)))``
    on every message edit. After that, ``self.view.children[0]`` is no longer
    the Select itself, it's the wrapping Container. Helpers in this module
    previously assumed the Select was always at index 0, which caused
    ``discord.errors.HTTPException: Invalid Form Body ... type must be one of
    (2, 3, 5, 6, 7, 8).`` when a Container was added to an ActionRow and
    re-sent to Discord.
    """
    stack: List[Any] = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, Select):
            if node.custom_id == custom_id:
                return node
            continue
        if isinstance(node, Section):
            acc = getattr(node, "accessory", None)
            if acc is not None:
                stack.append(acc)
            for child in getattr(node, "children", []) or []:
                if isinstance(child, Select):
                    stack.append(child)
            continue
        # Treat anything else as a container that may hold nested children
        # (LayoutView, Container, ActionRow, plain View, ...).
        for child in getattr(node, "children", []) or []:
            stack.append(child)
    return None


def _resolve_select(view: Any, custom_id: str = "HELP:COG_SELECT") -> Optional[Select]:
    """
    Public-ish variant of ``_find_select`` that also accepts a ``Select``
    passed directly, and never raises on a non-Select root.
    """
    if isinstance(view, Select):
        return view if view.custom_id == custom_id else None
    return _find_select(view, custom_id)


def _swap_paginator_content(view: Any, new_index: int) -> None:  # noqa: D401 - retained for future use
    """
    Body-swap helper for LayoutView-based paginators.

    Kept around in case a future paginator wants V2 again, but the
    current ``HelpPaginator`` uses the legacy classic embed + plain
    View pipeline so this is not called from anywhere. The classic
    paginator just swaps ``view.embed`` and re-emits the same ``View``,
    so no body-tree mutation is needed.
    """

class hollowHelp:
    @staticmethod
    def command_description(command: commands.Command) -> str:
        return command.help or getattr(command, "description", None) or "No description"

    @staticmethod
    def command_permission(command: commands.Command) -> str:
        found: List[str] = []

        def _register(name: str) -> None:
            label = name.replace("_", " ").title()
            if label not in found:
                found.append(label)

        def _collect(value: Any) -> None:
            if isinstance(value, dict):
                for name, allowed in value.items():
                    if allowed and name in discord.Permissions.VALID_FLAGS:
                        _register(name)
            elif isinstance(value, discord.Permissions):
                for name, allowed in value:
                    if allowed and name in discord.Permissions.VALID_FLAGS:
                        _register(name)

        for check in getattr(command, "checks", []) or []:
            for cell in getattr(check, "__closure__", None) or ():
                try:
                    _collect(cell.cell_contents)
                except ValueError:
                    continue
        return ", ".join(found) if found else "N/A"

    @staticmethod
    def collect_commands(cog: commands.Cog) -> List[commands.Command]:
        """Return every leaf command of a cog with its full qualified path (no duplicate group shells)."""
        leaves = []
        for cmd in cog.walk_commands():
            if isinstance(cmd, commands.Group) and cmd.commands:
                continue  # group shells are implied by their subcommands
            leaves.append(cmd)
        return sorted(leaves, key=lambda c: c.qualified_name)

    @staticmethod
    def build_help_embed(bot: "hollow", cog_name: Optional[str] = None) -> Embed:
        embed = Embed(
            title="Help",
            description="Select a plugin from the dropdown below to view its commands.",
            color=COLORS.neutral,
        )

        if cog_name and cog_name != "Index":
            cog = next(
                (c for c in bot.cogs.values() if c.qualified_name.lower() == cog_name.lower()),
                None,
            )
            if cog is None:
                embed.description = f"No plugin found for **{cog_name}**"
                return embed

            commands_list = hollowHelp.collect_commands(cog)
            if commands_list:
                embed.title = f"{cog.qualified_name} Commands"
                lines = [
                    f"`{cmd.qualified_name}` - {hollowHelp.command_description(cmd)}"
                    for cmd in commands_list
                ]
                embed.description = "\n".join(lines)[:4096]
            else:
                embed.description = f"No commands found for **{cog_name}**"
        else:
            embed.title = "Help Index"
            embed.description = "Browse all available plugins from the dropdown below."
            for cog in bot.cogs.values():
                if cog.qualified_name == "HelpCog":
                    continue
                cmd_count = len(hollowHelp.collect_commands(cog))
                embed.add_field(
                    name=f"• {cog.qualified_name}",
                    value=f"_{cmd_count} command{'s' if cmd_count != 1 else ''}_",
                    inline=True,
                )

        total_commands = sum(len(hollowHelp.collect_commands(cog)) for name, cog in bot.cogs.items() if name != "HelpCog")
        embed.set_footer(text=f"{total_commands} total commands")
        return embed

    @staticmethod
    def build_help_embeds_paginated(bot: "hollow", cog_name: str) -> List[Embed]:
        """Build paginated help embeds for a cog with many commands."""
        cog = next(
            (c for c in bot.cogs.values() if c.qualified_name.lower() == cog_name.lower()),
            None,
        )
        if cog is None:
            return [Embed(description=f"No plugin found for **{cog_name}**", color=COLORS.warn)]

        commands_list = hollowHelp.collect_commands(cog)
        if not commands_list:
            return [Embed(description=f"No commands found for **{cog_name}**", color=COLORS.neutral)]

        lines = [
            f"`{cmd.qualified_name}` - {hollowHelp.command_description(cmd)}"
            for cmd in commands_list
        ]

        embeds = []
        current_lines = []
        current_length = 0
        page_num = 1

        for line in lines:
            line_length = len(line) + 1
            if current_length + line_length > 3500 and current_lines:
                embed = Embed(
                    title=f"{cog.qualified_name} Commands",
                    description="\n".join(current_lines),
                    color=COLORS.neutral,
                )
                embed.set_footer(text=f"Page {page_num} • {len(commands_list)} commands")
                embeds.append(embed)
                current_lines = []
                current_length = 0
                page_num += 1
            current_lines.append(line)
            current_length += line_length

        if current_lines:
            embed = Embed(
                title=f"{cog.qualified_name} Commands",
                description="\n".join(current_lines),
                color=COLORS.neutral,
            )
            embed.set_footer(text=f"Page {page_num} • {len(commands_list)} commands")
            embeds.append(embed)

        return embeds

    @staticmethod
    async def send_bot_help(ctx: commands.Context) -> discord.Message:
        bot: hollow = ctx.bot

        initial_cogs = [name for name in bot.cogs.keys() if name != "HelpCog"]
        total_commands = sum(len(list(cog.walk_commands())) for cog in bot.cogs.values())
        # total_commands = sum(
        #     len(hollowHelp.collect_commands(cog))
        #     for name, cog in bot.cogs.items()
        #     if name != "HelpCog"
        # )
        total_categories = len(initial_cogs)
        thumbnail = "https://i.ibb.co.com/nNQZ7M3p/c0d5042b6d5202a834a47838ac450129.png"
        embed = (
            Embed(
                color=COLORS.neutral,
                description=(
                    "## hollow Help\n"
                    "Welcome to **hollow**, a free all-in one bot for every community.\n"
                    f"> {total_commands} commands from {total_categories} categories\n"
                ),
            )
            .set_thumbnail(url=thumbnail)
            .set_footer(
                text="Choose a plugin from the dropdown below.",
            )
        )
        view = HelpView(ctx, bot, initial_cogs, None)

        return await ctx.send(embed=embed, view=view)

    @staticmethod
    async def send_command_help(ctx: commands.Context, command: commands.Command) -> discord.Message:
        author = ctx.author
        author_name = getattr(author, "display_name", None) or getattr(author, "name", "N/A")
        author_avatar = getattr(getattr(author, "display_avatar", None), "url", None)
        module = getattr(command, "cog_name", None) or getattr(command, "cog", None)
        module = module if isinstance(module, str) else (module.qualified_name if module else "N/A")

        parent = f"{command.parent.name} " if command.parent else ""
        syntax = f",{parent}{command.name} {command.signature}".strip()
        example = getattr(command, "_example", None) or f",{parent}{command.name}"
        aliases = ", ".join(command.aliases) if command.aliases else "N/A"
        parameters = command.signature or "N/A"

        embed = (
            Embed(
                title=f"Command: {command.qualified_name}",
                description=hollowHelp.command_description(command),
                color=COLORS.neutral,
            )
            .set_author(
                name=author_name,
                icon_url=author_avatar,
            )
            .set_footer(
                text=f"Page 1/1 (1 entries) \u2219 Module: {module}",
            )
            .add_field(
                name="Aliases",
                value=aliases,
                inline=True,
            )
            .add_field(
                name="Parameters",
                value=parameters,
                inline=True,
            )
            .add_field(
                name="Permissions",
                value=f"{EMOJIS.WARN} {hollowHelp.command_permission(command)}",
                inline=True,
            )
            .add_field(
                name="Usage",
                value=(
                    f"```ansi\n"
                    f"{_ansi('Syntax:', 32)} {_ansi(syntax, 34)}\n"
                    f"{_ansi('Example:', 32)} {_ansi(example, 90)}\n"
                    f"```"
                ),
                inline=False,
            )
        )

        return await ctx.send(embed=embed)

    @staticmethod
    async def send_group_help(ctx: commands.Context, group: commands.Group) -> discord.Message:
        if not group.commands:
            return await hollowHelp.send_command_help(ctx, group)

        author = ctx.author
        author_name = getattr(author, "display_name", None) or getattr(author, "name", "N/A")
        author_avatar = getattr(getattr(author, "display_avatar", None), "url", None)
        module = getattr(group, "cog_name", None) or getattr(group, "cog", None)
        module = module if isinstance(module, str) else (module.qualified_name if module else "N/A")

        subcommands = list(group.commands)
        total = len(subcommands)

        pages = []
        for index, subcommand in enumerate(subcommands, start=1):
            parent = f"{group.name} " if subcommand.parent else ""
            syntax = f",{parent}{subcommand.name} {subcommand.signature}".strip()
            example = getattr(subcommand, "_example", None) or f",{parent}{subcommand.name}"
            aliases = ", ".join(subcommand.aliases) if subcommand.aliases else "N/A"
            parameters = subcommand.signature or "N/A"

            embed = (
                Embed(
                    title=f"Group Command: {subcommand.qualified_name}",
                    description=hollowHelp.command_description(subcommand),
                    color=COLORS.neutral,
                )
                .set_author(
                    name=author_name,
                    icon_url=author_avatar,
                )
                .set_footer(
                    text=f"Page {index}/{total} ({total} entries) ∙ Module: {module}",
                )
                .add_field(
                    name="Aliases",
                    value=aliases,
                    inline=True,
                )
                .add_field(
                    name="Parameters",
                    value=parameters,
                    inline=True,
                )
                .add_field(
                    name="Permission",
                    value=f"{EMOJIS.WARN} {hollowHelp.command_permission(group)}",
                    inline=True,
                )
                .add_field(
                    name="Usage",
                    value=(
                        f"```ansi\n"
                        f"{_ansi('Syntax:', 32)} {_ansi(syntax, 34)}\n"
                        f"{_ansi('Example:', 32)} {_ansi(example, 90)}\n"
                        f"```"
                    ),
                    inline=False,
                )
            )
            pages.append(embed)

        return await ctx.paginate(pages)


class HelpSelect(Select):
    def __init__(self, bot: "hollow", cog_names: List[str], current: Optional[str]):
        options = [
            discord.SelectOption(
                label="Index",
                value="Index",
                default=(current == "Index"),
            )
        ]
        for name in cog_names:
            options.append(
                discord.SelectOption(
                    label=name,
                    value=name,
                    default=(name == current),
                )
            )
        super().__init__(
            placeholder="Select a plugin...",
            options=options,
            custom_id="HELP:COG_SELECT",
            row=0,
        )
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        # Acknowledge immediately so Discord never shows "did not respond"
        await interaction.response.defer()
        # Explicit allowed_mentions to suppress the @everyone ping that
        # would otherwise fire on edits when a description contains
        # `@everyone` (Discord defaults to everyone=True on edits unless told otherwise).
        no_ping = discord.AllowedMentions(
            users=True, roles=False, everyone=False, replied_user=False
        )
        try:
            selected = self.values[0]

            # self.view may be a LayoutView container now (after the edit-wrapper
            # merged the original HelpView), so search the merged tree for the
            # actual Select menu rather than assuming children[0] is it.
            cog_select = _resolve_select(self.view)
            if cog_select is not None:
                for option in cog_select.options:
                    option.default = option.value == selected

            if selected == "Index":
                embed = hollowHelp.build_help_embed(self.bot, selected)
                # Classic embed (not the V2 shim) so the wrapper leaves it as
                # a normal message; ``self.view`` is the original ``View``.
                await interaction.message.edit(
                    embed=embed.to_classic_embed(),
                    view=self.view,
                    allowed_mentions=no_ping,
                )
            else:
                embeds = hollowHelp.build_help_embeds_paginated(self.bot, selected)
                if len(embeds) == 1:
                    await interaction.message.edit(
                        embed=embeds[0].to_classic_embed(),
                        view=self.view,
                        allowed_mentions=no_ping,
                    )
                else:
                    # Resolve the Select from the merged view (the dropdown
                    # may live inside an ActionRow / Container after the
                    # edit-wrapper stitched the original HelpView into a
                    # LayoutView).
                    cog_select = _resolve_select(self.view) or self.view
                    view = HelpPaginator(interaction, embeds, self.bot, cog_select)
                    # ``interaction.message.edit`` carried forward the
                    # cached message's ``flags.components_v2`` bit from the
                    # original V2 Index page, and Discord rejects the
                    # combination ``embeds + flags.components_v2`` with
                    # ``400 Invalid Form Body`` even when our new view has
                    # no V2 components at all. Instead of fighting that
                    # path, drop the old message and resend the paginator
                    # under a fresh flagless state: ``channel.send`` does
                    # not inherit ``self.flags`` from any cached message.
                    try:
                        await interaction.message.delete()
                    except discord.HTTPException:
                        pass
                    await interaction.channel.send(
                        embed=view.embed,
                        view=view,
                        allowed_mentions=no_ping,
                    )
        except Exception:
            import traceback

            traceback.print_exc()
            try:
                await interaction.message.edit(
                    embed=Embed(
                        description="⚠️ Something went wrong while loading that plugin's commands.",
                        color=COLORS.warn,
                    ),
                    view=self.view,
                    allowed_mentions=no_ping,
                )
            except discord.HTTPException:
                pass


class HelpView(View):
    """
    The dropdown-only View rendered with the help message.

    Does NOT set ``_classic_passthrough``: the first send goes through
    hollow Embed -> LayoutView conversion in ``core.client.embed`` so the
    selector is rendered inside a Components V2 Container. Once the user
    clicks the dropdown, the callback sends a classic embed so the
    message does not have to remain on V2.
    """

    def __init__(self, ctx: commands.Context, bot: "hollow", cog_names: List[str], current: Optional[str]):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.add_item(HelpSelect(bot, cog_names, current))

    async def interaction_check(self, interaction: discord.Interaction[discord.Client]) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.warn("You're not the **author** of this menu!")
            return False
        return True


class PaginatorButton(Button):
    def __init__(self, style: ButtonStyle, emoji: str, custom_id: str = None, row: int = 0):
        # An empty string is not a valid emoji for Discord's API; treat it as "no emoji".
        super().__init__(style=style, custom_id=custom_id, emoji=emoji or None, row=row)

    async def callback(self, interaction: discord.Interaction):
        if self.custom_id == "previous":
            return await self.previous(interaction)
        if self.custom_id == "next":
            return await self.next(interaction)
        if self.custom_id == "pages":
            return await self.pages(interaction)
        if self.custom_id == "cancel":
            return await self.cancel(interaction)

    async def previous(self, interaction: discord.Interaction):
        try:
            if self.view.current == 0:
                self.view.set_page(len(self.view.pages) - 1)
            else:
                self.view.set_page(self.view.current - 1)
            # Classic-mode edit: the embed carries the page, the View
            # carries the row of buttons + the cog dropdown. Re-using
            # ``self.view`` keeps button registration valid.
            await interaction.response.edit_message(
                embed=self.view.embed, view=self.view
            )
        except Exception as exc:
            log.exception("Paginator previous failed: %s", exc)
            try:
                await interaction.response.send_message(
                    f"⚠️ Could not flip page: {exc}", ephemeral=True
                )
            except Exception:
                pass

    async def next(self, interaction: discord.Interaction):
        try:
            if self.view.current == len(self.view.pages) - 1:
                self.view.set_page(0)
            else:
                self.view.set_page(self.view.current + 1)
            await interaction.response.edit_message(
                embed=self.view.embed, view=self.view
            )
        except Exception as exc:
            log.exception("Paginator next failed: %s", exc)
            try:
                await interaction.response.send_message(
                    f"⚠️ Could not flip page: {exc}", ephemeral=True
                )
            except Exception:
                pass

    async def cancel(self, interaction: discord.Interaction):
        try:
            self.view.stop()
            await interaction.message.delete()
        except discord.HTTPException:
            # Message may already be gone (deleted by another handler,
            # timed-out, etc.). ACK so Discord doesn't show stale UI.
            with suppress(Exception):
                await interaction.response.send_message("Cancelled.", ephemeral=True)

    async def pages(self, interaction: discord.Interaction):
        try:
            await interaction.response.send_modal(PagesModal(self.view))
        except Exception as exc:
            log.exception("Paginator pages failed: %s", exc)
            try:
                await interaction.response.send_message(
                    f"⚠️ Could not open page picker: {exc}", ephemeral=True
                )
            except Exception:
                pass


class PagesModal(discord.ui.Modal, title="Select Page"):
    def __init__(self, view):
        super().__init__()
        self.view = view
        self.selector = discord.ui.TextInput(
            label="Page",
            placeholder="5",
            custom_id="PAGINATOR:PAGES",
            style=discord.TextStyle.short,
            min_length=1,
            max_length=3,
            required=True,
            row=0,
        )
        self.add_item(self.selector)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            page = int(self.selector.value)
        except ValueError:
            await interaction.warn("Please provide a valid page number.")
            return
        if page < 1 or page > len(self.view.pages):
            await interaction.warn("Please provide a valid page number.")
            return
        try:
            self.view.set_page(page - 1)
            await interaction.response.edit_message(
                embed=self.view.embed, view=self.view
            )
        except Exception as exc:
            log.exception("Paginator pages-modal submit failed: %s", exc)
            try:
                await interaction.response.send_message(
                    f"⚠️ Could not switch pages: {exc}", ephemeral=True
                )
            except Exception:
                pass


class Paginator(View):
    # Keep pagination on the classic embed/View path. Without this sentinel
    # the global embed wrapper converts the paginator into a LayoutView, and
    # the button callback then sees ``self.view`` as that LayoutView rather
    # than this paginator (which has no set_page method).
    _classic_passthrough = True

    def __init__(
        self,
        ctx,
        pages: list[Embed],
        current: int = 0,
    ):
        self.ctx = ctx
        self.pages = pages
        self.current = current
        self.embed = pages[current].to_classic_embed() if hasattr(pages[current], "to_classic_embed") else pages[current]
        super().__init__(timeout=10)

        self.add_item(
            PaginatorButton(
                style=discord.ButtonStyle.blurple,
                custom_id="previous",
                emoji=EMOJIS.PREVIOUS,
            )
        )
        self.add_item(
            PaginatorButton(
                style=discord.ButtonStyle.blurple,
                custom_id="next",
                emoji=EMOJIS.NEXT,
            )
        )
        self.add_item(
            PaginatorButton(
                style=discord.ButtonStyle.grey,
                custom_id="pages",
                emoji=EMOJIS.NAVIGATE,
            )
        )
        self.add_item(
            PaginatorButton(
                style=discord.ButtonStyle.danger,
                custom_id="cancel",
                emoji=EMOJIS.CANCEL,
            )
        )

    def set_page(self, index: int) -> discord.Embed:
        if not 0 <= index < len(self.pages):
            raise IndexError(f"Paginator index {index} out of range")
        self.current = index
        page = self.pages[index]
        self.embed = page.to_classic_embed() if hasattr(page, "to_classic_embed") else page
        return self.embed

    async def interaction_check(
        self, interaction: discord.Interaction[discord.Client]
    ) -> bool:
        if interaction.user.id != getattr(self.ctx, "author", interaction.user).id:
            await interaction.warn("You're not the **author** of this embed!")
            return False
        return True


class HelpPaginator(View):
    """
    Plain ``discord.ui.View``-based paginator. We deliberately do NOT
    inherit from LayoutView: editing a V2-only message with this loadout
    triggers Discord's ``50006 - Cannot send an empty message`` error,
    so we use a classic View + discord.Embed instead.
    """

    # Sentinel for ``core.client.embed._convert_embeds_in_kwargs`` so the
    # edit-wrapper that turns hollow Embeds into V2 LayoutViews leaves this
    # view alone on subsequent edits (the wrapper is fine for the *first*
    # message but kills every paginator flip afterwards).
    _classic_passthrough = True
    def __init__(
        self,
        interaction: discord.Interaction,
        pages: list[Embed],
        bot: "hollow",
        select_menu: Any,
        current: int = 0,
    ):
        self.interaction = interaction
        self.pages = pages
        self.current = current
        super().__init__(timeout=60)

        # Tolerate a raw Select, the original HelpView (plain View), OR the
        # merged LayoutView Discord actually sent. The edit-wrapper may have
        # nested the Select inside an ActionRow inside a Container, so we
        # have to look for it before treating ``select_menu`` as a row item.
        real_select = _resolve_select(select_menu)
        if real_select is None and isinstance(select_menu, Select):
            real_select = select_menu
        if real_select is None:
            raise ValueError(
                "HelpPaginator expected a Select menu, got "
                f"{type(select_menu).__name__}"
            )
        select_menu = real_select
        # Clear row attribute from select menu (it was set in HelpSelect)
        # and pin it on row 0 so it sits above the buttons on the message.
        select_menu.row = 0

        embed = pages[current]

        # Classic-mode pagination: store the page on a classic
        # ``discord.Embed`` so each ``previous/next`` edit just swaps the
        # embed and re-uses this View. We do NOT build any V2 Container,
        # TextDisplay, Section, or Separator here, because we now render
        # the body via the classic embed on the message itself.
        self.embed: discord.Embed = embed.to_classic_embed()

        # The button row sits below the Select.
        self.add_item(
            PaginatorButton(
                style=discord.ButtonStyle.blurple,
                custom_id="previous",
                emoji=EMOJIS.PREVIOUS,
                row=1,
            )
        )
        self.add_item(
            PaginatorButton(
                style=discord.ButtonStyle.blurple,
                custom_id="next",
                emoji=EMOJIS.NEXT,
                row=1,
            )
        )
        self.add_item(
            PaginatorButton(
                style=discord.ButtonStyle.grey,
                custom_id="pages",
                emoji=EMOJIS.NAVIGATE,
                row=1,
            )
        )
        self.add_item(
            PaginatorButton(
                style=discord.ButtonStyle.danger,
                custom_id="cancel",
                emoji=EMOJIS.CANCEL,
                row=1,
            )
        )

    @property
    def current_page(self) -> Embed:
        return self.pages[self.current]

    def set_page(self, index: int) -> discord.Embed:
        """
        Flip to ``pages[index]`` and rebuild the cached ``self.embed``
        (a classic ``discord.Embed`` built from that page). Returns the
        new embed so callers can pass it to ``Message.edit`` /
        ``InteractionResponse.edit_message`` directly.

        ``PaginatorButton.next/previous`` and ``PagesModal.on_submit``
        call this on every flip; without it they raise ``AttributeError``.
        """
        if not 0 <= index < len(self.pages):
            raise IndexError(
                f"HelpPaginator index {index} out of range "
                f"(0..{len(self.pages) - 1})"
            )
        self.current = index
        self.embed = self.pages[index].to_classic_embed()
        return self.embed

    async def interaction_check(
        self, interaction: discord.Interaction[discord.Client]
    ) -> bool:
        if interaction.user.id != self.interaction.user.id:
            await interaction.warn("You're not the **author** of this embed!")
            return False
        return True


class Context(commands.Context):
    bot: "hollow"

    async def embed(self, **kwargs) -> discord.Message:
        return await self.send(**self.create(**kwargs))

    def create(self, **kwargs) -> Dict[str, Any]:
        view = View()

        for button in kwargs.get("buttons") or []:
            if not button or not button.get("label"):
                continue
            view.add_item(Button(
                label=button.get("label"),
                style=button.get("style") or ButtonStyle.secondary,
                emoji=button.get("emoji"),
                url=button.get("url"),
            ))

        embed = (
            Embed(
                url=kwargs.get("url"),
                description=kwargs.get("description"),
                title=kwargs.get("title"),
                color=kwargs.get("color") or COLORS.neutral,
                timestamp=kwargs.get("timestamp"),
            )
            .set_image(url=kwargs.get("image"))
            .set_thumbnail(url=kwargs.get("thumbnail"))
            .set_footer(
                text=kwargs.get("footer", {}).get("text"),
                icon_url=kwargs.get("footer", {}).get("icon_url"),
            )
            .set_author(
                name=kwargs.get("author", {}).get("name", ""),
                icon_url=kwargs.get("author", {}).get("icon_url", ""),
            )
        )

        for field in kwargs.get("fields") or []:
            if not field:
                continue
            embed.add_field(
                name=field.get("name"),
                value=field.get("value"),
                inline=field.get("inline", False),
            )

        return {
            "content": kwargs.get("content"),
            "embed": embed,
            "view": kwargs.get("view") or view,
            "delete_after": kwargs.get("delete_after"),
        }

    async def _config_emoji(self, column: str, fallback: str) -> str:
        try:
            cursor = await self.bot.db.execute(
                f"SELECT {column} FROM bot_config WHERE id = 1"
            )
            row = await cursor.fetchone()
            if row and row[column]:
                return row[column]
        except Exception:
            pass
        return fallback

    async def approve(self, message: str, **kwargs) -> discord.Message:
        emoji = await self._config_emoji("emoji_approve", EMOJIS.APPROVE)
        return await self.send(
            embed=Embed(
                color=COLORS.approve,
                description=f"{emoji} {self.author.mention}: {message}",
            ),
            **kwargs,
        )

    async def warn(self, message: str, **kwargs) -> discord.Message:
        emoji = await self._config_emoji("emoji_warn", EMOJIS.WARN)
        return await self.send(
            embed=Embed(
                color=COLORS.warn,
                description=f"{emoji} {self.author.mention}: {message}",
            ),
            **kwargs,
        )

    async def deny(self, message: str, **kwargs) -> discord.Message:
        emoji = await self._config_emoji("emoji_deny", EMOJIS.DENY)
        return await self.send(
            embed=Embed(
                color=COLORS.deny,
                description=f"{emoji} {self.author.mention}: {message}",
            ),
            **kwargs,
        )

    async def paginate(self, embeds: List[Union[Embed, discord.Embed]], **kwargs) -> discord.Message:
        if len(embeds) == 1:
            return await self.send(embed=embeds[0], **kwargs)
        paginator = Paginator(self, embeds)
        # The paginator opts out of Components V2 conversion so its View is
        # preserved for callbacks. Therefore the initial embed must also be a
        # real discord.Embed, not hollow's LayoutView-producing Embed shim.
        first = embeds[0]
        first = first.to_classic_embed() if isinstance(first, Embed) else first
        return await self.send(embed=first, view=paginator, **kwargs)