"""Central control over which hybrid commands are exposed as slash commands.

Discord allows only 100 application commands per application globally (and per
guild). `hollow` has well over 100 top-level commands, so a curated subset is
registered as slash commands and the rest stay prefix-only -- they keep working
with `,name` plus every alias, they just don't consume a global slash slot.

To change the policy, edit the sets below; no cog needs to be touched:

* ``TEXT_ONLY_COMMANDS`` - top-level commands registered prefix-only.
* ``PRIORITY_COMMANDS``  - protected names that are never demoted, useful when
  rewriting ``TEXT_ONLY_COMMANDS`` by hand.
* ``NESTED_COMMANDS``     - `command -> parent group` pairs. The command keeps its
  prefix invocation (`,dick`) but its slash version lives under the group
  (`/fun dick`), which costs zero extra global command slots.

Verify the result with ``python _count_commands.py`` (must stay <= 100).
"""

from typing import Optional, Set

# Discord hard limit for application commands (global registration).
GLOBAL_COMMAND_LIMIT = 100

# Commands that must never be removed from the slash list (protected).
PRIORITY_COMMANDS: Set[str] = {
    # help / meta
    "help",
    "invite",
    "support",
    "ping",
    "botinfo",
    # moderation
    "ban",
    "unban",
    "kick",
    "timeout",
    "untimeout",
    "warn",
    "unwarn",
    "purge",
    "lock",
    "unlock",
    # jail
    "jail",
    "unjail",
    # tickets
    "ticket",
    # frequently used lookups
    "serverinfo",
    "userinfo",
    "avatar",
    "servericon",
}

# Prefix-only (no slash) top-level commands, chosen as the lowest-value ones.
TEXT_ONLY_COMMANDS: Set[str] = {
    # fun (these keep their slash form as /fun <name>, see NESTED_COMMANDS)
    "image",
    "search",
    "steal",
    "urban",
    # information
    "channelinfo",
    "sticker",
    "spotify",
    "inviteinfo",
    # moderation (admin power-tools)
    "clearsnipe",
    "editsnipe",
    "reactsnipe",
    "snipe",
    "forcenick",
    "unforcenick",
    "nickname",
    "resetnickname",
    "imagemute",
    "unimagemute",
    "reactmute",
    "unreactmute",
    "resetwarns",
    "unbanall",
    "unmuteall",
    "unwarnall",
    "ghostping",
    # engagement
    "invited",
    # automation (keep only /trigger)
    "afkset",
    "antinuke",
    "autoreact",
    "autorole",
    # roleplay image actions
    "roleplay",
}

# Note: a few groups are declared with `commands.hybrid_group` directly (server,
# config) so they bypass this list and stay as slash commands on purpose.

# Command -> slash parent group. The prefix invocation stays untouched (`,dick`),
# only the slash version moves under the group, so it costs no global slot.
NESTED_COMMANDS = {
    # fun cog: plain commands become /fun <command>
    "ball": "fun",
    "coinflip": "fun",
    "dice": "fun",
    "rps": "fun",
    "humble": "fun",
    "howgay": "fun",
    "dick": "fun",
    "dihr": "fun",
    "image": "fun",
    "search": "fun",
    "steal": "fun",
    "urban": "fun",
    # moderation cog -> /moderation <command>
    "ban": "moderation",
    "unban": "moderation",
    "kick": "moderation",
    "timeout": "moderation",
    "untimeout": "moderation",
    "warn": "moderation",
    "unwarn": "moderation",
    "purge": "moderation",
    "lock": "moderation",
    "unlock": "moderation",
    # music cog -> /music <command>
    "play": "music",
    "skip": "music",
    "stop": "music",
    "queue": "music",
    "volume": "music",
    "repeat": "music",
    "connect": "music",
    "preset": "music",
    # information cog -> /information <command>
    "serverinfo": "information",
    "userinfo": "information",
    "avatar": "information",
    "banner": "information",
    "servericon": "information",
    "serverbanner": "information",
}

# Slash groups created on demand to host nested commands (the cog doesn't have to
# declare them). They only exist in the slash tree: every nested command still
# answers to its own prefix invocation (`,ban`, `,si`, ...).
NESTED_GROUPS = {
    "moderation": ("Moderation actions", "ban, kick, timeout, warns, purges and channel locks"),
    "music": ("Music player", "play, skip, queue, volume and audio presets"),
    "information": ("Information", "server, user and bot information lookups"),
}

# Prefix aliases kept working when a command becomes a nested subcommand. The
# aliases already declared by the command (`,b`, `,si`, `,pp`, ...) are untouched,
# these are the extra hand-picked ones.
NESTED_ALIASES: dict[str, list[str]] = {
    "moderation": ["mod", "moderation"],
    "music": ["music"],
    "information": ["info", "information"],
    "fun": ["fun"],
}


def nested_group_for(name: str) -> Optional[str]:
    """Slash parent group for `name`, or None when it should stay top-level."""
    return NESTED_COMMANDS.get((name or "").strip().casefold())


def nested_group_names() -> Set[str]:
    """Groups that need a slash-only counterpart (``fun`` already exists as one)."""
    return set(NESTED_COMMANDS.values())


def nested_group_aliases(group: str) -> list[str]:
    """Prefix aliases added to a nested slash group, if they are free."""
    return list(NESTED_ALIASES.get((group or "").strip().casefold(), []))


# prefix word -> nested group, so `,moderation ban` and `,mod ban` also work even
# though the group itself is not a real command object on the bot.
NESTED_GROUP_NAMES: dict[str, str] = {}
for _group, _aliases in NESTED_ALIASES.items():
    NESTED_GROUP_NAMES[_group.casefold()] = _group
    for _alias in _aliases:
        NESTED_GROUP_NAMES.setdefault(_alias.casefold(), _group)


def is_text_only(name: str) -> bool:
    """True if `name` must be registered as a prefix command only."""
    key = (name or "").strip().casefold()
    return key in TEXT_ONLY_COMMANDS and key not in PRIORITY_COMMANDS


def text_only_reason(name: str) -> Optional[str]:
    """Human readable reason used for logging (None if the command stays a slash)."""
    if not is_text_only(name):
        return None
    return (
        f"'{name}' is prefix-only (see core/client/todo.py TEXT_ONLY_COMMANDS) "
        f"to stay under Discord's {GLOBAL_COMMAND_LIMIT} global slash command limit"
    )
