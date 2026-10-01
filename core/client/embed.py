from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional, Union, List, Sequence
from datetime import datetime

import discord
from discord import MediaGalleryItem
from discord.utils import MISSING
from discord.ui import LayoutView, Container, TextDisplay, Section, Thumbnail, MediaGallery, Separator, ActionRow

__all__ = ("Embed", "patch_send", "unpatch_send")

TEXT_DISPLAY_LIMIT = 4000  # Discord caps TextDisplay content at 4000 chars


def _chunk_text(text: str, limit: int = TEXT_DISPLAY_LIMIT) -> List[str]:
    """Split text into <=limit chunks, preferring newline boundaries so no
    TextDisplay exceeds Discord's 4000-char content cap."""
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    for line in text.split("\n"):
        while len(line) > limit:  # oversized single line: hard-split
            chunks.append(line[:limit])
            line = line[limit:]
        add = len(line) + (1 if current else 0)
        if current and current_len + add > limit:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += len(line) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append("\n".join(current))
    return [c for c in chunks if c] or ["\u200b"]


class Embed:
    """
    Drop-in replacement for ``discord.Embed`` that builds a Components V2
    ``LayoutView`` (via ``Container`` + ``TextDisplay``/``Section``/``Thumbnail``/
    ``MediaGallery``) instead of a legacy embed object.

    The public API mirrors ``discord.Embed`` as closely as possible so that
    existing call sites (``Embed(...).set_author(...).add_field(...)``) work
    without modification. Unsupported legacy features (author/footer icons,
    field inlining) are accepted for signature compatibility but not rendered.

    Use ``to_container()`` to get the root ``Container`` or ``to_view()`` to get
    a ready-to-send ``LayoutView`` (optionally with a message-level ``content``
    ``TextDisplay`` placed before the container).
    """

    __slots__ = (
        "_title",
        "_description",
        "_url",
        "_timestamp",
        "_color",
        "_author_name",
        "_author_icon_url",
        "_author_url",
        "_footer_text",
        "_footer_icon_url",
        "_thumbnail_url",
        "_image_url",
        "_fields",
    )

    def __init__(
        self,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        color: Optional[Union[int, discord.Color, discord.Colour]] = None,
        colour: Optional[Union[int, discord.Color, discord.Colour]] = None,
        url: Optional[str] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        self._title = title
        self._description = description
        self._url = url
        self._timestamp = timestamp

        # Normalize color/colour (accept both spellings, int or discord.Color)
        raw_color = color if color is not None else colour
        if raw_color is None:
            self._color: Optional[discord.Colour] = None
        elif isinstance(raw_color, (discord.Color, discord.Colour)):
            self._color = discord.Colour(raw_color.value)
        elif isinstance(raw_color, int):
            self._color = discord.Colour(raw_color)
        else:
            raise TypeError(f"color must be int or discord.Color, not {type(raw_color).__name__}")

        # Author
        self._author_name: Optional[str] = None
        self._author_icon_url: Optional[str] = None
        self._author_url: Optional[str] = None

        # Footer
        self._footer_text: Optional[str] = None
        self._footer_icon_url: Optional[str] = None

        # Media
        self._thumbnail_url: Optional[str] = None
        self._image_url: Optional[str] = None

        # Fields
        self._fields: List[dict] = []  # each: {"name": str, "value": str, "inline": bool}

    # -------------------------------------------------------------------------
    # Fluent setters (return self for chaining)
    # -------------------------------------------------------------------------

    def set_author(
        self,
        *,
        name: Optional[str] = None,
        icon_url: Optional[str] = None,
        url: Optional[str] = None,
    ) -> Embed:
        self._author_name = name
        self._author_icon_url = icon_url  # accepted for compat, not rendered
        self._author_url = url
        return self

    def remove_author(self) -> Embed:
        self._author_name = None
        self._author_icon_url = None
        self._author_url = None
        return self

    def set_footer(
        self,
        *,
        text: Optional[str] = None,
        icon_url: Optional[str] = None,
    ) -> Embed:
        self._footer_text = text
        self._footer_icon_url = icon_url  # accepted for compat, not rendered
        return self

    def remove_footer(self) -> Embed:
        self._footer_text = None
        self._footer_icon_url = None
        return self

    def set_thumbnail(self, *, url: Optional[str] = None) -> Embed:
        self._thumbnail_url = url
        return self

    def set_image(self, *, url: Optional[str] = None) -> Embed:
        self._image_url = url
        return self

    def add_field(
        self,
        *,
        name: str,
        value: str,
        inline: bool = True,
    ) -> Embed:
        self._fields.append({"name": name, "value": value, "inline": inline})
        return self

    def insert_field_at(
        self,
        index: int,
        *,
        name: str,
        value: str,
        inline: bool = True,
    ) -> Embed:
        self._fields.insert(index, {"name": name, "value": value, "inline": inline})
        return self

    def set_field_at(
        self,
        index: int,
        *,
        name: str,
        value: str,
        inline: bool = True,
    ) -> Embed:
        if 0 <= index < len(self._fields):
            self._fields[index] = {"name": name, "value": value, "inline": inline}
        return self

    def remove_field(self, index: int) -> Embed:
        if 0 <= index < len(self._fields):
            self._fields.pop(index)
        return self

    def clear_fields(self) -> Embed:
        self._fields.clear()
        return self

    # -------------------------------------------------------------------------
    # Properties (read-only, for introspection/compat)
    # -------------------------------------------------------------------------

    @property
    def title(self) -> Optional[str]:
        return self._title

    @title.setter
    def title(self, value: Optional[str]) -> None:
        self._title = value

    @property
    def description(self) -> Optional[str]:
        return self._description

    @description.setter
    def description(self, value: Optional[str]) -> None:
        self._description = value

    @property
    def url(self) -> Optional[str]:
        return self._url

    @url.setter
    def url(self, value: Optional[str]) -> None:
        self._url = value

    @property
    def timestamp(self) -> Optional[datetime]:
        return self._timestamp

    @timestamp.setter
    def timestamp(self, value: Optional[datetime]) -> None:
        self._timestamp = value

    @property
    def color(self) -> Optional[discord.Colour]:
        return self._color

    @color.setter
    def color(self, value: Optional[Union[int, discord.Color, discord.Colour]]) -> None:
        if value is None:
            self._color = None
        elif isinstance(value, (discord.Color, discord.Colour)):
            self._color = discord.Colour(value.value)
        elif isinstance(value, int):
            self._color = discord.Colour(value)
        else:
            raise TypeError(f"color must be int or discord.Color, not {type(value).__name__}")

    @property
    def colour(self) -> Optional[discord.Colour]:
        return self._color

    @colour.setter
    def colour(self, value: Optional[Union[int, discord.Color, discord.Colour]]) -> None:
        self.color = value

    @property
    def author(self) -> Optional[SimpleNamespace]:
        # Attribute-style proxy (mirrors discord.Embed.author.name / .icon_url).
        if self._author_name is None:
            return None
        return SimpleNamespace(
            name=self._author_name,
            icon_url=self._author_icon_url,
            url=self._author_url,
        )

    @property
    def footer(self) -> Optional[SimpleNamespace]:
        if self._footer_text is None:
            return None
        return SimpleNamespace(
            text=self._footer_text,
            icon_url=self._footer_icon_url,
        )

    @property
    def thumbnail(self) -> Optional[SimpleNamespace]:
        if self._thumbnail_url is None:
            return None
        return SimpleNamespace(url=self._thumbnail_url)

    @property
    def image(self) -> Optional[SimpleNamespace]:
        if self._image_url is None:
            return None
        return SimpleNamespace(url=self._image_url)

    @property
    def fields(self) -> List[SimpleNamespace]:
        return [
            SimpleNamespace(name=f["name"], value=f["value"], inline=f["inline"])
            for f in self._fields
        ]

    @classmethod
    def from_dict(cls, data: dict) -> Embed:
        """Build an Embed from a ``discord.Embed.to_dict()``-shaped mapping
        (mirrors ``discord.Embed.from_dict`` for compat with EmbedBuilder)."""
        data = dict(data or {})
        color = data.get("color")
        embed = cls(
            title=data.get("title"),
            description=data.get("description"),
            color=color,
            url=data.get("url"),
            timestamp=data.get("timestamp"),
        )
        author = data.get("author") or {}
        if author.get("name") or author.get("icon_url") or author.get("url"):
            embed.set_author(
                name=author.get("name"),
                icon_url=author.get("icon_url"),
                url=author.get("url"),
            )
        footer = data.get("footer") or {}
        if footer.get("text") or footer.get("icon_url"):
            embed.set_footer(text=footer.get("text"), icon_url=footer.get("icon_url"))
        thumbnail = data.get("thumbnail") or {}
        if thumbnail.get("url"):
            embed.set_thumbnail(url=thumbnail["url"])
        image = data.get("image") or {}
        if image.get("url"):
            embed.set_image(url=image["url"])
        for f in data.get("fields") or []:
            embed.add_field(
                name=f.get("name", "\u200b"),
                value=f.get("value", "\u200b"),
                inline=f.get("inline", True),
            )
        return embed

    # -------------------------------------------------------------------------
    # Conversion to Components V2
    # -------------------------------------------------------------------------

    def _build_text_items(self) -> List[TextDisplay]:
        """Build TextDisplay items for title, description, author, fields, footer."""
        items: List[TextDisplay] = []

        # Author -> bold TextDisplay
        if self._author_name:
            items.append(TextDisplay(f"**{self._author_name}**"))

        # Title -> H2 markdown, optionally linked
        if self._title:
            title_text = f"## {self._title}"
            if self._url:
                title_text = f"## [{self._title}]({self._url})"
            items.append(TextDisplay(title_text))

        # Description -> plain TextDisplay(s), chunked to the 4000-char cap
        if self._description:
            for chunk in _chunk_text(self._description):
                items.append(TextDisplay(chunk))

        # Fields -> block fields as **name**\nvalue; inline fields with a
        # single-line value as compact `**name:** value` one-liners (the
        # closest Components V2 gets to the old side-by-side inline row).
        # Long values are chunked so no TextDisplay exceeds the 4000-char cap.
        for field in self._fields:
            name, value, inline = field["name"], field["value"], field["inline"]
            one_liner = f"**{name}:** {value}"
            if inline and "\n" not in value.strip() and len(one_liner) <= TEXT_DISPLAY_LIMIT:
                items.append(TextDisplay(one_liner))
            else:
                header = f"**{name}**\n"
                # Reserve header room so even the first chunk stays in-cap.
                for i, chunk in enumerate(_chunk_text(value, TEXT_DISPLAY_LIMIT - len(header))):
                    items.append(TextDisplay(header + chunk if i == 0 else chunk))

        # Footer -> -# subtext markdown
        if self._footer_text:
            items.append(TextDisplay(f"-# {self._footer_text}"))

        return items

    def _build_main_section(self) -> Optional[Section]:
        """Build the main Section with the first text chunk + thumbnail.

        Returns None if there's no thumbnail (Section requires an accessory).
        Overflow text beyond Section's 3-child limit is handled by
        :meth:`to_container`, which appends it directly to the Container.
        """
        text_items = self._build_text_items()

        # If no text content at all, we still need a placeholder for the Section
        if not text_items:
            text_items = [TextDisplay("\u200b")]  # zero-width space

        # Thumbnail must be passed as accessory in constructor.
        # Section caps at 3 text children -> only the first chunk goes in.
        if self._thumbnail_url:
            accessory = Thumbnail(media=self._thumbnail_url)
            return Section(*text_items[:3], accessory=accessory)
        return None

    def _overflow_text_items(self) -> List[TextDisplay]:
        """Text items that didn't fit in the thumbnail Section (3-child cap)."""
        if not self._thumbnail_url:
            return []
        text_items = self._build_text_items()
        if not text_items:
            text_items = [TextDisplay("\u200b")]
        return text_items[3:]

    def to_container(self) -> Container:
        """
        Convert this Embed to a ``discord.ui.Container`` (the Components V2
        equivalent of an embed box).
        """
        container_items: List[Any] = []

        # Main section (text + optional thumbnail) or text items directly
        main_section = self._build_main_section()
        if main_section is not None:
            container_items.append(main_section)
            container_items.extend(self._overflow_text_items())
        else:
            # No thumbnail - add text items directly to container
            text_items = self._build_text_items()
            if not text_items:
                text_items = [TextDisplay("\u200b")]
            container_items.extend(text_items)

        # Image -> MediaGallery (after main content)
        if self._image_url:
            container_items.append(
                MediaGallery(MediaGalleryItem(media=self._image_url))
            )

        # Create container with accent color if set
        if self._color is not None:
            return Container(*container_items, accent_colour=self._color)
        return Container(*container_items)

    def to_view(self, *, content: Optional[str] = None) -> LayoutView:
        """
        Convert this Embed to a ``discord.ui.LayoutView`` ready to send.

        If ``content`` is provided, it is placed as a ``TextDisplay`` *before*
        the container (message-level content), not inside the container.
        """
        view = LayoutView()

        # Message-level content goes outside the container
        if content:
            view.add_item(TextDisplay(content))

        # Add the container
        view.add_item(self.to_container())

        return view

    def to_classic_embed(self) -> "discord.Embed":
        """
        Convert this V2 Embed into a real ``discord.Embed`` so the message can
        go through the legacy non-V2 path. Used by the paginator because
        ``interaction.response.edit_message`` with a LayoutView-only payload
        triggers Discord's ``50006 — Cannot send an empty message`` error,
        while the same content rendered as a classic embed + plain ``View``
        works fine.
        """
        real = discord.Embed(
            title=self._title,
            url=self._url,
            description=self._description,
            colour=self._color,
            timestamp=self._timestamp,
        )
        if self._author_name:
            real.set_author(
                name=self._author_name,
                icon_url=self._author_icon_url,
                url=self._author_url,
            )
        if self._footer_text:
            real.set_footer(
                text=self._footer_text,
                icon_url=self._footer_icon_url,
            )
        if self._thumbnail_url:
            real.set_thumbnail(url=self._thumbnail_url)
        if self._image_url:
            real.set_image(url=self._image_url)
        for f in self._fields:
            inline = bool(f.get("inline", True))
            real.add_field(name=f["name"], value=f["value"], inline=inline)
        return real



# -----------------------------------------------------------------------------
# Monkey-patching send/edit methods to auto-convert our Embed -> LayoutView
# -----------------------------------------------------------------------------

_original_methods: dict = {}
_patched = False


def _is_our_embed(obj: Any) -> bool:
    """Check if an object is our Embed shim (not a real discord.Embed)."""
    return isinstance(obj, Embed)


_MERGED_VIEW_ATTR = "__hollow_merged_view__"


_INTERACTIVE_TYPES = None  # resolved lazily (see _interactive_types())


def _interactive_types() -> tuple:
    global _INTERACTIVE_TYPES
    if _INTERACTIVE_TYPES is None:
        _INTERACTIVE_TYPES = (
            discord.ui.Button,
            discord.ui.Select,
            discord.ui.ChannelSelect,
            discord.ui.RoleSelect,
            discord.ui.UserSelect,
            discord.ui.MentionableSelect,
        )
    return _INTERACTIVE_TYPES


def _iter_interactive_items(root: Any):
    """Yield Button/Select leaf items from a View, LayoutView, Container,
    ActionRow or Section (walking nested structures).

    This matters because callbacks re-send ``view=self.view`` where
    ``self.view`` is already the merged LayoutView — the merge must unwrap
    the buttons/selects from inside the old Container instead of treating
    the Container itself as a row item.
    """
    from discord.ui import ActionRow

    stack = list(getattr(root, "children", []) or [])
    while stack:
        item = stack.pop(0)
        if isinstance(item, _interactive_types()):
            yield item
        elif isinstance(item, ActionRow):
            stack.extend(item.children)
        elif isinstance(item, Container):
            stack.extend(item.children)
        elif isinstance(item, Section):
            acc = getattr(item, "accessory", None)
            if acc is not None:
                stack.append(acc)
            for c in getattr(item, "children", []) or []:
                if isinstance(c, _interactive_types()):
                    stack.append(c)
        # TextDisplay, MediaGallery, Separator, Thumbnail, File: ignored


_XRYPTON_ROOT_ATTR = "__hollow_root_view__"
_STATE_DENYLIST = frozenset({
        "_children", "_view", "_parent", "_timeout_task", "_stopped",
        _MERGED_VIEW_ATTR, _XRYPTON_ROOT_ATTR,
    })


def _merge_view_into_layout(layout_view: LayoutView, interactive_view: discord.ui.View) -> LayoutView:
    """
    Merge an interactive View's items into a LayoutView as ActionRows placed
    *inside* the Container (so buttons/selects render inside the accent box,
    matching the requested help-menu look).

    Preserves:
    - All button/select items with their callbacks (bound to original view)
    - Row grouping (max 5 buttons per row, 1 select per row)
    - timeout, on_timeout, interaction_check, on_error (delegated to original view)
    - custom_id for persistent views
    - instance state (e.g. Paginator.pages/current) copied onto the layout so
      ``self.view.<attr>`` inside Button-subclass callbacks keeps working
    """
    from discord.ui import ActionRow

    # Check if already merged
    if getattr(interactive_view, _MERGED_VIEW_ATTR, None) is layout_view:
        return layout_view

    # Root original view (unwrap chained layouts: L2 merges L1 which merged V).
    # Method delegation always targets the root so timeout/checks/errors behave.
    root_view = getattr(interactive_view, _XRYPTON_ROOT_ATTR, interactive_view)

    # Carry instance state onto the layout so ``self.view.pages`` /
    # ``self.view.current`` / ``self.view.author`` etc. keep working inside
    # Button-subclass callbacks (where ``self.view`` is now this layout).
    try:
        for key, value in vars(interactive_view).items():
            if key in _STATE_DENYLIST:
                continue
            try:
                setattr(layout_view, key, value)
            except Exception:
                pass
    except TypeError:
        pass
    setattr(layout_view, _XRYPTON_ROOT_ATTR, root_view)

    # Delegate view-level behavior to the root original view.
    layout_view.timeout = interactive_view.timeout

    # Wrap callbacks to delegate to root view
    original_on_timeout = root_view.on_timeout
    original_interaction_check = root_view.interaction_check
    original_on_error = root_view.on_error

    async def delegated_on_timeout() -> None:
        try:
            await original_on_timeout()
        finally:
            # Release any ``await view.wait()`` on the original view (e.g.
            # ConfirmView) and stop listening on both views.
            try:
                root_view.stop()
            except Exception:
                pass
            try:
                layout_view.stop()
            except Exception:
                pass

    async def delegated_interaction_check(interaction: discord.Interaction) -> bool:
        return await original_interaction_check(interaction)

    async def delegated_on_error(interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await original_on_error(interaction, error, item)

    layout_view.on_timeout = delegated_on_timeout
    layout_view.interaction_check = delegated_interaction_check
    layout_view.on_error = delegated_on_error

    # If the original view stops itself (e.g. ConfirmView.confirm calls
    # self.stop()), also stop the layout so it stops listening.
    try:
        orig_stop = root_view.stop

        def _stop_both() -> None:
            try:
                orig_stop()
            finally:
                try:
                    layout_view.stop()
                except Exception:
                    pass

        root_view.stop = _stop_both  # type: ignore[method-assign]
    except Exception:
        pass

    # Find the target Container (last one wins for multi-container views).
    # ActionRows go *inside* it so interactive components sit within the box.
    containers = [c for c in layout_view.children if isinstance(c, Container)]
    if not containers:
        raise ValueError("Cannot merge interactive view: LayoutView has no Container")
    target: Container = containers[-1]

    # Collect items grouped by row (unwrapping nested LayoutViews/Containers
    # so re-edits with view=self.view keep working instead of nesting).
    rows: dict[int, list[discord.ui.Item]] = {}
    for item in _iter_interactive_items(interactive_view):
        row = getattr(item, "row", 0) or 0
        if row not in rows:
            rows[row] = []
        rows[row].append(item)

    # Build ActionRows from grouped items
    for row_idx in sorted(rows.keys()):
        items = rows[row_idx]

        # Check for selects (they can't share rows)
        selects = [i for i in items if isinstance(i, (discord.ui.Select, discord.ui.ChannelSelect,
                                                        discord.ui.RoleSelect, discord.ui.UserSelect,
                                                        discord.ui.MentionableSelect))]
        buttons = [i for i in items if isinstance(i, discord.ui.Button)]

        if len(selects) > 1:
            raise ValueError(f"Row {row_idx}: Cannot have more than 1 select menu per row")

        if selects and buttons:
            raise ValueError(f"Row {row_idx}: Select menus cannot share a row with buttons")

        if len(buttons) > 5:
            raise ValueError(f"Row {row_idx}: Cannot have more than 5 buttons per row (got {len(buttons)})")

        # Create ActionRow and add items (Separator keeps spacing inside Container)
        action_row = ActionRow()
        for item in items:
            action_row.add_item(item)

        target.add_item(Separator(spacing=discord.SeparatorSpacing.small))
        target.add_item(action_row)

    # Validate total component count (Discord limit: 40 components, 5 action rows max)
    def _count_action_rows() -> int:
        n = 0
        for child in layout_view.children:
            if isinstance(child, ActionRow):
                n += 1
            elif isinstance(child, Container):
                n += sum(1 for c in child.children if isinstance(c, ActionRow))
        return n

    action_row_count = _count_action_rows()
    if action_row_count > 5:
        raise ValueError(f"Action rows ({action_row_count}) exceeds Discord's limit of 5")

    # Mark as merged to avoid double-merging on edits
    setattr(interactive_view, _MERGED_VIEW_ATTR, layout_view)

    return layout_view


def _shim_from_real_embed(real: discord.Embed) -> Embed:
    """Convert a real ``discord.Embed`` into our shim so it can be merged
    into a Container with its interactive view (buttons inside the box)."""
    shim = Embed(
        title=getattr(real, "title", None),
        description=getattr(real, "description", None),
        color=getattr(real, "colour", None),
        url=getattr(real, "url", None),
        timestamp=getattr(real, "timestamp", None),
    )
    author = getattr(real, "author", None)
    if author is not None and getattr(author, "name", None):
        shim.set_author(name=author.name)
    footer = getattr(real, "footer", None)
    if footer is not None and getattr(footer, "text", None):
        shim.set_footer(text=footer.text)
    thumbnail = getattr(real, "thumbnail", None)
    if thumbnail is not None and getattr(thumbnail, "url", None):
        shim.set_thumbnail(url=thumbnail.url)
    image = getattr(real, "image", None)
    if image is not None and getattr(image, "url", None):
        shim.set_image(url=image.url)
    for f in getattr(real, "fields", []) or []:
        shim.add_field(name=f.name, value=f.value, inline=getattr(f, "inline", True))
    return shim


def _first_embed(embeds) -> Any:
    """Return the first entry of an ``embeds=`` sequence, or None."""
    if isinstance(embeds, Sequence) and len(embeds) > 0:
        return embeds[0]
    return None


def _convert_embeds_in_kwargs(kwargs: dict) -> dict:
    """
    Convert our Embed instances in kwargs to LayoutView/components.
    Handles both `embed=` and `embeds=` (plural).
    Also merges any `view=` (interactive View) into the resulting LayoutView,
    with ActionRows placed *inside* the Container.
    Real discord.Embed WITHOUT a view still passes through untouched; real
    discord.Embed WITH a view is converted too so buttons land inside the box.

    A View that sets ``_classic_passthrough = True`` is an opt-out: the
    caller has already produced a *classic* ``discord.Embed`` to go with
    it and does NOT want the V2 merge to happen on this edit (Discord's
    validators reject V2 messages with thin payloads, and our paginator
    flips pages by re-attaching the same View).
    """
    new_kwargs = dict(kwargs)

    view_val = new_kwargs.get("view")
    if view_val is not None and getattr(view_val, "_classic_passthrough", False):
        # Opt-out of the V2 merge, but our Embed shim has no ``to_dict`` and
        # would explode inside discord.py's handle_message_parameters. Downgrade
        # it to a real discord.Embed first.
        if _is_our_embed(new_kwargs.get("embed")):
            new_kwargs["embed"] = new_kwargs["embed"].to_classic_embed()
        elif _is_our_embed(_first_embed(new_kwargs.get("embeds"))):
            new_kwargs["embeds"] = [
                e.to_classic_embed() if _is_our_embed(e) else e
                for e in new_kwargs["embeds"]
            ]
        return new_kwargs

    will_convert_single = "embed" in new_kwargs and _is_our_embed(new_kwargs["embed"])
    embeds_val = new_kwargs.get("embeds")
    will_convert_list = _is_our_embed(_first_embed(embeds_val))
    # Real embed + interactive view (e.g. ban ConfirmView): convert as well
    # so the buttons render inside the Container instead of below it.
    real_embed = new_kwargs.get("embed")
    view_val = new_kwargs.get("view")
    # Note: LayoutView is NOT a discord.ui.View subclass in this discord.py
    # version, so check both — callbacks re-send view=self.view where self.view
    # is already a merged LayoutView.
    has_interactive_view = (
        view_val is not None
        and isinstance(view_val, (discord.ui.View, LayoutView))
        and not _is_our_embed(view_val)
    )
    will_convert_real = (
        not will_convert_single
        and isinstance(real_embed, discord.Embed)
        and has_interactive_view
    )
    if not (will_convert_single or will_convert_list or will_convert_real):
        return new_kwargs

    # Extract interactive view if present (and not our Embed shim)
    interactive_view = None
    if "view" in new_kwargs:
        view = new_kwargs["view"]
        if view is not None and isinstance(view, (discord.ui.View, LayoutView)) and not _is_our_embed(view):
            interactive_view = new_kwargs.pop("view")

    # Handle single embed=
    if will_convert_single:
        embed = new_kwargs.pop("embed")
        content = new_kwargs.pop("content", None)
        layout_view = embed.to_view(content=content)

        # Merge interactive view if present
        if interactive_view:
            _merge_view_into_layout(layout_view, interactive_view)

        new_kwargs["view"] = layout_view

    elif will_convert_real:
        real = new_kwargs.pop("embed")
        content = new_kwargs.pop("content", None)
        layout_view = _shim_from_real_embed(real).to_view(content=content)

        if interactive_view:
            _merge_view_into_layout(layout_view, interactive_view)

        new_kwargs["view"] = layout_view

    # Handle embeds= (list)
    if will_convert_list:
        embeds = new_kwargs.pop("embeds")
        content = new_kwargs.pop("content", None)
        # For multiple embeds, create a view with multiple containers
        view = LayoutView()
        if content:
            view.add_item(TextDisplay(content))
        for e in embeds:
            if _is_our_embed(e):
                view.add_item(e.to_container())
            else:
                # Mixed real embeds + our embeds - not supported
                raise TypeError(
                    "Cannot mix real discord.Embed with hollow Embed in embeds="
                )

        # Merge interactive view if present
        if interactive_view:
            _merge_view_into_layout(view, interactive_view)

        new_kwargs["view"] = view

    return new_kwargs


def _make_send_wrapper(original_method, method_name: str, *, cls=None):
    """
    Create a wrapper that converts our ``Embed`` -> ``LayoutView`` before
    forwarding to the original ``send`` / ``edit_message`` / ``Message.edit``
    / ``WebhookMessage.edit`` etc.

    For ``Message.edit`` we additionally pass an explicit
    ``suppress=MISSING`` so that discord.py does NOT carry forward
    ``self.flags.value`` from the cached message — this avoids the
    ``50035 - The 'embeds' field cannot be used when using
    MessageFlags.IS_COMPONENTS_V2`` rejection that fires when the cached
    message was originally a Components V2 message and we now want to
    drop down to a classic embed + plain ``View`` edit.
    """
    # Only ``Message.edit`` carries forward ``self.flags.value`` in its
    # default signature (because ``suppress=False`` is the default, the
    # ``if suppress is not MISSING:`` branch runs and copies the cached
    # flags). ``WebhookMessage.edit`` and ``InteractionMessage.edit`` do
    # not have this exact behaviour and we only want to fix the catch that
    # bot code is hitting, so we only intervene on ``cls is discord.Message``.
    is_message_edit = (
        cls is not None
        and cls is discord.Message
        and method_name == "edit"
    )

    async def wrapper(*args, **kwargs):
        new_kwargs = _convert_embeds_in_kwargs(kwargs)
        if is_message_edit and "suppress" not in new_kwargs:
            # Force discord.py to leave ``flags = MISSING`` instead of
            # inheriting the cached message's flags (which include the V2
            # bit if the message was originally a V2 LayoutView).
            new_kwargs["suppress"] = MISSING
        return await original_method(*args, **new_kwargs)

    wrapper.__name__ = method_name
    wrapper.__qualname__ = method_name
    return wrapper


def patch_send() -> None:
    """
    Monkey-patch common send/edit methods to automatically convert
    hollow Embed instances to LayoutView.

    Patched targets:
    - discord.abc.Messageable.send
    - discord.InteractionResponse.send_message
    - discord.InteractionResponse.edit_message
    - discord.Webhook.send
    - discord.Webhook.edit_message
    - discord.Message.edit
    - discord.Interaction.followup.send
    - discord.Interaction.followup.edit_message
    """
    global _patched, _original_methods

    if _patched:
        return

    targets = [
        (discord.abc.Messageable, "send"),
        (discord.InteractionResponse, "send_message"),
        (discord.InteractionResponse, "edit_message"),
        (discord.Webhook, "send"),
        (discord.Webhook, "edit_message"),
        (discord.Message, "edit"),
        (discord.WebhookMessage, "edit"),
        (discord.InteractionMessage, "edit"),
    ]

    # Also patch followup if available
    if hasattr(discord, "Interaction"):
        # discord.Interaction.followup is a property returning Webhook
        # The Webhook.send/edit_message are already patched above
        pass

    for cls, method_name in targets:
        if hasattr(cls, method_name):
            original = getattr(cls, method_name)
            _original_methods[(cls, method_name)] = original
            setattr(cls, method_name, _make_send_wrapper(original, method_name, cls=cls))

    _patched = True


def unpatch_send() -> None:
    """Restore all monkey-patched methods to their original implementations."""
    global _patched, _original_methods

    if not _patched:
        return

    for (cls, method_name), original in _original_methods.items():
        setattr(cls, method_name, original)

    _original_methods.clear()
    _patched = False


# Backwards-compat alias (some codebases use Colour vs Color)
try:
    from discord import Colour  # noqa: F401
except ImportError:
    pass