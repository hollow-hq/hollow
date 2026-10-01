"""Keyed asyncio lock manager.

A tiny helper that hands out one ``asyncio.Lock`` per composite key, e.g.
``locks.get(guild_id, user_id)``. Used by cogs that mutate per-user state
(jail, economy) so double-clicks, spam and racing tasks can never corrupt
state (double-spend, double-claim, role save/restore races).

The lock table is intentionally left to grow: keys are bounded by
(guilds x members), which is tiny compared to memory budgets, and the
``asyncio.Lock`` objects are ~100 bytes each. Call :meth:`drop` when a key's
lifetime ends (e.g. a guild is removed) to keep long-running processes tidy.
"""

from __future__ import annotations

import asyncio
from typing import Tuple


class KeyedLockManager:
    """Hands out one ``asyncio.Lock`` per composite key."""

    __slots__ = ("_locks",)

    def __init__(self) -> None:
        self._locks: dict[Tuple, asyncio.Lock] = {}

    def get(self, *key) -> asyncio.Lock:
        """Return (creating if needed) the lock for ``*key``."""
        key = tuple(key)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def drop(self, *key) -> None:
        """Remove the lock for ``*key`` (only safe when it is not held)."""
        self._locks.pop(tuple(key), None)

    def drop_guild(self, guild_id: int) -> None:
        """Drop every lock keyed with ``guild_id`` as its first element."""
        for key in [k for k in self._locks if k and k[0] == guild_id]:
            self._locks.pop(key, None)

    def locked(self, *key) -> bool:
        """Whether the lock for ``*key`` is currently held."""
        lock = self._locks.get(tuple(key))
        return lock.locked() if lock else False
