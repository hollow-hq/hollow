from typing import Optional, Callable

import discord
from discord import app_commands
from discord.ext import commands

from core.client.reduceslash import (
    NESTED_GROUPS,
    is_text_only,
    nested_group_aliases,
    nested_group_for,
    nested_group_names,
    text_only_reason,
)

# Hybrid commands that must become slash subcommands of a group (see todo.py).
# A command can be declared before or after its group, so both directions are kept:
#   _NESTED_WAITING[group_name] -> commands declared before the group existed
#   _GROUPS[group_name]         -> group objects created so far
_NESTED_WAITING: dict[str, list] = {}
_GROUPS: dict[str, object] = {}


async def _nested_group_help(ctx) -> None:
    """Callback used by the auto-created slash-only groups (`,mod` -> help)."""
    from core.context import hollowHelp

    command = ctx.command
    if command is not None and command.parent is not None:
        return await hollowHelp.send_group_help(ctx, command)
    return await hollowHelp.send_command_help(ctx, command)


def _ensure_nested_group(group_name: str):
    """Return the group hosting nested commands, creating a slash-only one if needed.

    Groups that no cog declares (``/moderation``, ``/music``, ``/information``) are
    created here. They are real prefix groups too, so `,moderation ban` and the
    hand-picked aliases (`,mod`) resolve, and the children stay reachable as
    `,ban` because the parent link only changes command routing.
    """
    group = _GROUPS.get(group_name)
    if group is not None:
        return group

    if group_name in _NESTED_WAITING or group_name not in NESTED_GROUPS:
        # A cog declares this group later in the module (e.g. /fun); let it do the work.
        return None

    description, group_help = NESTED_GROUPS[group_name]

    async def callback(cog_self, ctx, *args, **kwargs):
        return await _nested_group_help(ctx)

    callback.__name__ = group_name
    callback.__qualname__ = group_name
    callback.__doc__ = description

    aliases = [
        alias
        for alias in nested_group_aliases(group_name)
        if alias != group_name
    ]

    group = commands.hybrid_group(
        name=group_name,
        aliases=aliases,
        description=description,
        help=group_help or description,
        invoke_without_command=True,
        fallback_to_global=True,
    )(callback)
    group.fallback_to_global = False
    group._auto_nested_group = True
    group.cog = None
    _GROUPS[group_name] = group
    _AUTO_GROUPS[group_name] = group
    return group


def _attach_nested_command(group_name: str, cmd) -> None:
    """Move `cmd` under the `group_name` group for its slash version.

    The command keeps working as a top-level prefix command (`,ban`, `,dick`); only
    the application command is registered as `/moderation ban`, `/fun dick`, which
    costs no extra global command slot.
    """
    group = _ensure_nested_group(group_name)
    if group is None:
        _NESTED_WAITING.setdefault(group_name, []).append(cmd)
        return

    try:
        group.add_command(cmd)
    except Exception:
        # name clash inside the group -> keep it prefix-only instead of exploding
        safe = commands.hybrid_command(
            name=cmd.name,
            aliases=cmd.aliases,
            description=cmd.description or "",
            help=cmd.help or "",
            with_app_command=False,
        )(cmd.callback)
        safe._nested_conflict = True
        _NESTED_WAITING.setdefault(group_name, []).append(safe)


# Slash-only groups that had to be generated (mirrored onto the bot in setup_hook).
_AUTO_GROUPS: dict[str, object] = {}


def has_permissions(**permissions: bool):
    """Allow native Discord permissions or configured fake permissions."""
    if not permissions:
        raise TypeError("has_permissions requires at least one permission")

    invalid = set(permissions) - set(discord.Permissions.VALID_FLAGS)
    if invalid:
        raise TypeError(f"Unknown Discord permission(s): {', '.join(sorted(invalid))}")

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            return False

        if await ctx.bot.is_owner(ctx.author):
            return True

        native_permissions = ctx.author.guild_permissions
        if native_permissions.administrator or all(
            getattr(native_permissions, name) == expected
            for name, expected in permissions.items()
        ):
            return True

        try:
            cursor = await ctx.bot.db.execute(
                "SELECT permissions FROM fake_permission_config WHERE guild_id = ?",
                (ctx.guild.id,),
            )
            row = await cursor.fetchone()
        except Exception:
            return False

        if not row:
            return False

        import json

        try:
            grants = json.loads(row["permissions"] or "{}")
        except (TypeError, ValueError):
            return False

        granted = set()
        for role in ctx.author.roles:
            granted.update(grants.get(str(role.id), []))

        return "administrator" in granted or all(
            (name in granted) == expected
            for name, expected in permissions.items()
        )

    return commands.check(predicate)


def _clean_aliases(name: Optional[str], aliases: Optional[list]) -> list:
    """Strip duplicates/case variants and remove the primary name from aliases."""
    cleaned: list[str] = []
    seen: set[str] = set()
    name_key = name.casefold() if name else None
    for alias in aliases or []:
        if not isinstance(alias, str):
            continue
        key = alias.casefold()
        if key == name_key:
            continue
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(alias)
    return cleaned


def hybrid_command(
    name: Optional[str] = None,
    *,
    aliases: list = None,
    description: Optional[str] = None,
    example: Optional[str] = None,
    **kwargs,
):
    def decorator(func: Callable):
        effective_name = name or func.__name__

        nested_in = nested_group_for(effective_name)
        if nested_in:
            # Slash version becomes `/<group> <name>`; prefix `,name` untouched.
            cmd = commands.hybrid_command(
                name=effective_name,
                aliases=_clean_aliases(name, aliases),
                description=description or func.__doc__ or "",
                help=description or func.__doc__ or "",
                **kwargs,
            )(func)
            if example:
                cmd._example = example
            cmd._nested_in = nested_in
            _attach_nested_command(nested_in, cmd)
            return cmd

        if is_text_only(effective_name):
            cmd = commands.hybrid_command(
                name=effective_name,
                aliases=_clean_aliases(name, aliases),
                description=description or func.__doc__ or "",
                help=description or func.__doc__ or "",
                with_app_command=False,
                **kwargs,
            )(func)
            if example:
                cmd._example = example
            cmd._prefix_only_reason = text_only_reason(effective_name)
            return cmd

        cmd = commands.hybrid_command(
            name=name,
            aliases=_clean_aliases(name, aliases),
            description=description or func.__doc__ or "",
            help=description or func.__doc__ or "",
            **kwargs,
        )(func)

        if example:
            cmd._example = example

        return cmd

    return decorator


def hybrid_group(
    name: Optional[str] = None,
    *,
    aliases: list = None,
    description: Optional[str] = None,
    example: Optional[str] = None,
    **kwargs,
):
    def decorator(func: Callable):
        # Pop invoke_without_command if caller passed it explicitly to avoid
        # "multiple values for keyword argument" (wrapper already defaults to True).
        invoke_without_command = kwargs.pop("invoke_without_command", True)
        effective_name = name or func.__name__
        text_only = is_text_only(effective_name)
        if text_only and kwargs.get("with_app_command") is False:
            # caller already forced prefix-only, respect it
            text_only = False
        if text_only:
            kwargs["with_app_command"] = False
        cmd = commands.hybrid_group(
            name=name,
            aliases=_clean_aliases(name, aliases),
            description=description or func.__doc__ or "",
            help=description or func.__doc__ or "",
            invoke_without_command=invoke_without_command,
            **kwargs,
            )(func)

        _GROUPS[cmd.name] = cmd
        for pending in _NESTED_WAITING.pop(cmd.name, []):
            _attach_nested_command(cmd.name, pending)

        original_callback = cmd.callback

        async def group_callback(cog_self, ctx: commands.Context, *args, **kwargs):
            from core.context import hollowHelp

            # bare group invocation -> show group help via paginator
            if not args:
                return await hollowHelp.send_group_help(ctx, ctx.command)
            return await original_callback(cog_self, ctx, *args, **kwargs)

        group_callback.__qualname__ = func.__qualname__
        group_callback.__name__ = func.__name__
        cmd.callback = group_callback

        if example:
            cmd._example = example

        if text_only:
            cmd._prefix_only_reason = text_only_reason(effective_name)

        return cmd

    return decorator
