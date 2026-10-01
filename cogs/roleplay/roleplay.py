"""Roleplay commands.

Lets members act on one another with an anime GIF from the free
roleplay-gifs collection hosted on the GitHub raw CDN:
https://github.com/itsfizys/roleplay-gifs

Every action is available two ways:

* as a slash command  — ``/hug @user``
* as a prefix command — ``,hug @user``
* inside the group    — ``/roleplay hug @user`` / ``,roleplay hug @user``

The GIF index (index.json) is fetched once on ``cog_load`` and cached, with a
hardcoded fallback so the cog still works if GitHub is unreachable.
"""

from __future__ import annotations

import random
from typing import Optional

import aiohttp
import discord
from discord.ext import commands
from discord.ext.commands import command

from core.client.commands import hybrid_group
from core.config import COLORS

# GitHub raw CDN that hosts the roleplay-gifs collection.
INDEX_URL = "https://raw.githubusercontent.com/itsfizys/roleplay-gifs/main/index.json"
FALLBACK_BASE_URL = "https://raw.githubusercontent.com/itsfizys/roleplay-gifs/main"

# Seconds a member must wait before re-using the same action.
COOLDOWN = 3.0

# 25 curated actions, each mapped to its API category.
# "text" — rendered with {author}/{target} when a target is given
# "solo" — rendered with {author} when no target is given
# "verb" — short description used for the command's help text
ACTIONS: dict[str, dict[str, str]] = {
    "airkiss": {
        "emoji": "\U0001f618",
        "verb": "blows an air kiss at",
        "text": "{author} blows an air kiss at {target}",
        "solo": "{author} blows an air kiss into the void",
    },
    "bite": {
        "emoji": "\U0001f62d",
        "verb": "bites",
        "text": "{author} bites {target}",
        "solo": "{author} bites their own hand",
    },
    "blush": {
        "emoji": "\U0001f60a",
        "verb": "blushes at",
        "text": "{author} blushes at {target}",
        "solo": "{author} is blushing",
    },
    "cry": {
        "emoji": "\U0001f622",
        "verb": "cries because of",
        "text": "{author} cries because of {target}",
        "solo": "{author} is crying",
    },
    "cuddle": {
        "emoji": "\U0001f9d1\U0001f3fc",
        "verb": "cuddles",
        "text": "{author} cuddles {target}",
        "solo": "{author} cuddles a pillow",
    },
    "dance": {
        "emoji": "\U0001f483",
        "verb": "dances with",
        "text": "{author} dances with {target}",
        "solo": "{author} is dancing",
    },
    "facepalm": {
        "emoji": "\U0001f926",
        "verb": "facepalms at",
        "text": "{author} facepalms at {target}",
        "solo": "{author} facepalms",
    },
    "handhold": {
        "emoji": "\U0001f91d",
        "verb": "holds hands with",
        "text": "{author} holds hands with {target}",
        "solo": "{author} holds their own hand",
    },
    "headbang": {
        "emoji": "\U0001f642\U0001f4a5",
        "verb": "headbangs",
        "text": "{author} headbangs {target}",
        "solo": "{author} headbangs to the music",
    },
    "hug": {
        "emoji": "\U0001f917",
        "verb": "hugs",
        "text": "{author} hugs {target}",
        "solo": "{author} hugs themselves",
    },
    "kiss": {
        "emoji": "\U0001f48b",
        "verb": "kisses",
        "text": "{author} kisses {target}",
        "solo": "{author} blows a kiss to the air",
    },
    "laugh": {
        "emoji": "\U0001f606",
        "verb": "laughs at",
        "text": "{author} laughs at {target}",
        "solo": "{author} is laughing",
    },
    "lick": {
        "emoji": "\U0001f60c",
        "verb": "licks",
        "text": "{author} licks {target}",
        "solo": "{author} licks the screen",
    },
    "nom": {
        "emoji": "\U0001f35e",
        "verb": "noms on",
        "text": "{author} noms on {target}",
        "solo": "{author} noms on some food",
    },
    "nuzzle": {
        "emoji": "\U0001f431",
        "verb": "nuzzles",
        "text": "{author} nuzzles {target}",
        "solo": "{author} nuzzles a plushie",
    },
    "pat": {
        "emoji": "\U0001f44c",
        "verb": "pats",
        "text": "{author} pats {target}",
        "solo": "{author} pats the air",
    },
    "pinch": {
        "emoji": "\U0001f90e",
        "verb": "pinches",
        "text": "{author} pinches {target}",
        "solo": "{author} pinches their own cheek",
    },
    "poke": {
        "emoji": "\U0001f446",
        "verb": "pokes",
        "text": "{author} pokes {target}",
        "solo": "{author} pokes a hole in the air",
    },
    "punch": {
        "emoji": "\U0001f44a",
        "verb": "punches",
        "text": "{author} punches {target}",
        "solo": "{author} punches the air",
    },
    "slap": {
        "emoji": "\U0001f94e",
        "verb": "slaps",
        "text": "{author} slaps {target}",
        "solo": "{author} slaps themselves",
    },
    "sorry": {
        "emoji": "\U0001f647",
        "verb": "apologises to",
        "text": "{author} apologises to {target}",
        "solo": "{author} apologises to nobody",
    },
    "tickle": {
        "emoji": "\U0001f923",
        "verb": "tickles",
        "text": "{author} tickles {target}",
        "solo": "{author} tickles themselves",
    },
    "wave": {
        "emoji": "\U0001f44b",
        "verb": "waves at",
        "text": "{author} waves at {target}",
        "solo": "{author} waves at nobody",
    },
    "wink": {
        "emoji": "\U0001f609",
        "verb": "winks at",
        "text": "{author} winks at {target}",
        "solo": "{author} winks",
    },
}

# Used when index.json cannot be fetched at startup.
FALLBACK_COUNTS: dict[str, int] = {
    "airkiss": 6,
    "bite": 20,
    "blush": 41,
    "cry": 46,
    "cuddle": 29,
    "dance": 33,
    "facepalm": 5,
    "handhold": 10,
    "headbang": 8,
    "hug": 40,
    "kiss": 36,
    "laugh": 17,
    "lick": 14,
    "nom": 46,
    "nuzzle": 10,
    "pat": 27,
    "pinch": 11,
    "poke": 17,
    "punch": 15,
    "slap": 25,
    "sorry": 4,
    "tickle": 9,
    "wave": 22,
    "wink": 32,
}


class Roleplay(commands.Cog):
    """Interact with other members using anime roleplay GIFs."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.base_url = FALLBACK_BASE_URL
        self.counts: dict[str, int] = dict(FALLBACK_COUNTS)
        # (user_id, action) -> timestamp of last use
        self._cooldowns: dict[tuple[int, str], float] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        await self.refresh_index()

    async def cog_unload(self) -> None:
        self._cooldowns.clear()

    async def refresh_index(self) -> None:
        """Fetch index.json from the CDN and cache the category counts."""
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(INDEX_URL) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)

            categories = data.get("categories") or {}
            if not isinstance(categories, dict) or not categories:
                return
            self.base_url = data.get("base_url") or FALLBACK_BASE_URL
            self.counts = {
                str(k): int(v) for k, v in categories.items() if isinstance(v, int)
            }
        except Exception:
            # Keep the hardcoded fallback — the cog still works offline.
            pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _cooldown_remaining(self, user_id: int, action: str) -> float:
        now = discord.utils.utcnow().timestamp()
        last = self._cooldowns.get((user_id, action))
        if last is None:
            return 0.0
        return max(0.0, COOLDOWN - (now - last))

    def _mark_used(self, user_id: int, action: str) -> None:
        now = discord.utils.utcnow().timestamp()
        # Opportunistic cleanup so the dict cannot grow without bound.
        if len(self._cooldowns) > 2000:
            self._cooldowns = {
                key: ts for key, ts in self._cooldowns.items() if now - ts <= COOLDOWN
            }
        self._cooldowns[(user_id, action)] = now

    def gif_url(self, action: str) -> Optional[str]:
        """Build a random GIF URL for *action*, or None when it is unknown."""
        if action not in ACTIONS:
            return None
        count = self.counts.get(action) or FALLBACK_COUNTS.get(action, 0)
        if count < 1:
            return None
        n = random.randint(1, count)
        return f"{self.base_url}/{action}/{n}.gif"

    def build_embed(
        self,
        action: str,
        author: discord.abc.User,
        target: Optional[discord.abc.User] = None,
    ):
        """Build the embed sent for an action."""
        from core.client.embed import Embed

        entry = ACTIONS[action]
        author_name = getattr(author, "display_name", None) or author.name

        if target is None or target.id == author.id:
            description = entry["solo"].format(author=author_name)
        else:
            # Mention guild members so the ping actually works; fall back to the
            # display name for users outside this guild.
            try:
                target_name = target.mention
            except Exception:
                target_name = getattr(target, "display_name", None) or target.name
            description = entry["text"].format(author=author_name, target=target_name)

        embed = Embed(
            description=f"{entry['emoji']} {description}",
            color=COLORS.neutral,
        )
        url = self.gif_url(action)
        if url:
            embed.set_image(url=url)
        return embed

    async def perform(
        self,
        ctx: commands.Context,
        action: str,
        target: Optional[discord.User] = None,
    ) -> None:
        """Shared execution path for the slash, prefix and grouped forms."""
        if action not in ACTIONS:
            return await ctx.warn(f"Unknown action `{action}`.")

        if target is not None and target.bot:
            return await ctx.warn("Bots can't be roleplayed with.")

        remaining = self._cooldown_remaining(ctx.author.id, action)
        if remaining > 0:
            return await ctx.warn(f"Slow down — try again in {remaining:.1f}s.")

        self._mark_used(ctx.author.id, action)
        await ctx.send(embed=self.build_embed(action, ctx.author, target))

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    # Each action is exposed twice, so both surfaces stay in sync:
    #   * top-level hybrid command -> /hug @user   and   ,hug @user
    #   * sub-command of /roleplay  -> /roleplay hug @user

    @hybrid_group(
        name="roleplay",
        aliases=["rp"],
        description="Interact with other members using anime GIFs",
    )
    async def roleplay(self, ctx: commands.Context) -> None:
        """Pick an action, e.g. ``/roleplay hug @user``."""
        from core.context import hollowHelp

        await hollowHelp.send_group_help(ctx, ctx.command)

    @command(name="airkiss", description="blows an air kiss at someone", example=",airkiss @user")
    async def rp_airkiss(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """blows an air kiss at someone."""
        await self.perform(ctx, "airkiss", user)

    @roleplay.command(name="airkiss", description="blows an air kiss at someone")
    async def rp_group_airkiss(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """blows an air kiss at someone."""
        await self.perform(ctx, "airkiss", user)


    @command(name="bite", description="bites someone", example=",bite @user")
    async def rp_bite(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """bites someone."""
        await self.perform(ctx, "bite", user)

    @roleplay.command(name="bite", description="bites someone")
    async def rp_group_bite(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """bites someone."""
        await self.perform(ctx, "bite", user)


    @command(name="blush", description="blushes at someone", example=",blush @user")
    async def rp_blush(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """blushes at someone."""
        await self.perform(ctx, "blush", user)

    @roleplay.command(name="blush", description="blushes at someone")
    async def rp_group_blush(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """blushes at someone."""
        await self.perform(ctx, "blush", user)


    @command(name="cry", description="cries because of someone", example=",cry @user")
    async def rp_cry(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """cries because of someone."""
        await self.perform(ctx, "cry", user)

    @roleplay.command(name="cry", description="cries because of someone")
    async def rp_group_cry(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """cries because of someone."""
        await self.perform(ctx, "cry", user)


    @command(name="cuddle", description="cuddles someone", example=",cuddle @user")
    async def rp_cuddle(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """cuddles someone."""
        await self.perform(ctx, "cuddle", user)

    @roleplay.command(name="cuddle", description="cuddles someone")
    async def rp_group_cuddle(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """cuddles someone."""
        await self.perform(ctx, "cuddle", user)


    @command(name="dance", description="dances with someone", example=",dance @user")
    async def rp_dance(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """dances with someone."""
        await self.perform(ctx, "dance", user)

    @roleplay.command(name="dance", description="dances with someone")
    async def rp_group_dance(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """dances with someone."""
        await self.perform(ctx, "dance", user)


    @command(name="facepalm", description="facepalms at someone", example=",facepalm @user")
    async def rp_facepalm(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """facepalms at someone."""
        await self.perform(ctx, "facepalm", user)

    @roleplay.command(name="facepalm", description="facepalms at someone")
    async def rp_group_facepalm(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """facepalms at someone."""
        await self.perform(ctx, "facepalm", user)


    @command(name="handhold", description="holds hands with someone", example=",handhold @user")
    async def rp_handhold(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """holds hands with someone."""
        await self.perform(ctx, "handhold", user)

    @roleplay.command(name="handhold", description="holds hands with someone")
    async def rp_group_handhold(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """holds hands with someone."""
        await self.perform(ctx, "handhold", user)


    @command(name="headbang", description="headbangs someone", example=",headbang @user")
    async def rp_headbang(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """headbangs someone."""
        await self.perform(ctx, "headbang", user)

    @roleplay.command(name="headbang", description="headbangs someone")
    async def rp_group_headbang(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """headbangs someone."""
        await self.perform(ctx, "headbang", user)


    @command(name="hug", description="hugs someone", example=",hug @user")
    async def rp_hug(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """hugs someone."""
        await self.perform(ctx, "hug", user)

    @roleplay.command(name="hug", description="hugs someone")
    async def rp_group_hug(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """hugs someone."""
        await self.perform(ctx, "hug", user)


    @command(name="kiss", description="kisses someone", example=",kiss @user")
    async def rp_kiss(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """kisses someone."""
        await self.perform(ctx, "kiss", user)

    @roleplay.command(name="kiss", description="kisses someone")
    async def rp_group_kiss(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """kisses someone."""
        await self.perform(ctx, "kiss", user)


    @command(name="laugh", description="laughs at someone", example=",laugh @user")
    async def rp_laugh(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """laughs at someone."""
        await self.perform(ctx, "laugh", user)

    @roleplay.command(name="laugh", description="laughs at someone")
    async def rp_group_laugh(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """laughs at someone."""
        await self.perform(ctx, "laugh", user)


    @command(name="lick", description="licks someone", example=",lick @user")
    async def rp_lick(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """licks someone."""
        await self.perform(ctx, "lick", user)

    @roleplay.command(name="lick", description="licks someone")
    async def rp_group_lick(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """licks someone."""
        await self.perform(ctx, "lick", user)


    @command(name="nom", description="noms on someone", example=",nom @user")
    async def rp_nom(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """noms on someone."""
        await self.perform(ctx, "nom", user)

    @roleplay.command(name="nom", description="noms on someone")
    async def rp_group_nom(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """noms on someone."""
        await self.perform(ctx, "nom", user)


    @command(name="nuzzle", description="nuzzles someone", example=",nuzzle @user")
    async def rp_nuzzle(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """nuzzles someone."""
        await self.perform(ctx, "nuzzle", user)

    @roleplay.command(name="nuzzle", description="nuzzles someone")
    async def rp_group_nuzzle(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """nuzzles someone."""
        await self.perform(ctx, "nuzzle", user)


    @command(name="pat", description="pats someone", example=",pat @user")
    async def rp_pat(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """pats someone."""
        await self.perform(ctx, "pat", user)

    @roleplay.command(name="pat", description="pats someone")
    async def rp_group_pat(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """pats someone."""
        await self.perform(ctx, "pat", user)


    @command(name="pinch", description="pinches someone", example=",pinch @user")
    async def rp_pinch(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """pinches someone."""
        await self.perform(ctx, "pinch", user)

    @roleplay.command(name="pinch", description="pinches someone")
    async def rp_group_pinch(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """pinches someone."""
        await self.perform(ctx, "pinch", user)


    @command(name="poke", description="pokes someone", example=",poke @user")
    async def rp_poke(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """pokes someone."""
        await self.perform(ctx, "poke", user)

    @roleplay.command(name="poke", description="pokes someone")
    async def rp_group_poke(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """pokes someone."""
        await self.perform(ctx, "poke", user)


    @command(name="punch", description="punches someone", example=",punch @user")
    async def rp_punch(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """punches someone."""
        await self.perform(ctx, "punch", user)

    @roleplay.command(name="punch", description="punches someone")
    async def rp_group_punch(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """punches someone."""
        await self.perform(ctx, "punch", user)


    @command(name="slap", description="slaps someone", example=",slap @user")
    async def rp_slap(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """slaps someone."""
        await self.perform(ctx, "slap", user)

    @roleplay.command(name="slap", description="slaps someone")
    async def rp_group_slap(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """slaps someone."""
        await self.perform(ctx, "slap", user)


    @command(name="sorry", description="apologises to someone", example=",sorry @user")
    async def rp_sorry(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """apologises to someone."""
        await self.perform(ctx, "sorry", user)

    @roleplay.command(name="sorry", description="apologises to someone")
    async def rp_group_sorry(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """apologises to someone."""
        await self.perform(ctx, "sorry", user)


    @command(name="tickle", description="tickles someone", example=",tickle @user")
    async def rp_tickle(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """tickles someone."""
        await self.perform(ctx, "tickle", user)

    @roleplay.command(name="tickle", description="tickles someone")
    async def rp_group_tickle(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """tickles someone."""
        await self.perform(ctx, "tickle", user)


    @command(name="wave", description="waves at someone", example=",wave @user")
    async def rp_wave(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """waves at someone."""
        await self.perform(ctx, "wave", user)

    @roleplay.command(name="wave", description="waves at someone")
    async def rp_group_wave(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """waves at someone."""
        await self.perform(ctx, "wave", user)


    @command(name="wink", description="winks at someone", example=",wink @user")
    async def rp_wink(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """winks at someone."""
        await self.perform(ctx, "wink", user)

    @roleplay.command(name="wink", description="winks at someone")
    async def rp_group_wink(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """winks at someone."""
        await self.perform(ctx, "wink", user)
