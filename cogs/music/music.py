from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional, List

import discord
from discord.ext import commands
from discord.ui import Select, View
import wavelink

from core.client.embed import Embed
from core.client.commands import has_permissions, hybrid_command, hybrid_group
from core.config import COLORS, EMOJIS
from core.context import Context
from core.logger import log

SPEAKER = EMOJIS.SPEAKER
IDLE_TIMEOUT = 60
POLL_INTERVAL = 15
SEARCH_RESULTS_LIMIT = 10


class Music(commands.Cog):
    """Production-grade music cog powered by Lavalink."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._idle_tasks: dict[int, asyncio.Task] = {}
        self._node_ready = asyncio.Event()

    async def cog_load(self) -> None:
        self._node_ready.clear()
        self.bot.loop.create_task(self._background_connect())

    async def _background_connect(self) -> None:
        try:
            await asyncio.wait_for(self._connect_node(), timeout=15)
        except asyncio.TimeoutError:
            log.warning("Lavalink node connection timed out.")
        except Exception as exc:
            log.error("Lavalink node connection failed: %s", exc)

    async def cog_unload(self) -> None:
        for task in self._idle_tasks.values():
            task.cancel()
        self._idle_tasks.clear()
        try:
            await wavelink.Pool.close()
        except Exception:
            pass

    async def _connect_node(self) -> None:
        await self.bot.wait_until_ready()
        node = wavelink.Node(
            uri="https://lavalinkv4.serenetia.com:443",
            password="https://dsc.gg/ajidevserver",
        )
        try:
            await wavelink.Pool.connect(client=self.bot, nodes=[node])
            self._node_ready.set()
            log.info("Connected to Lavalink node")
        except Exception as exc:
            log.error("Lavalink connection failed: %s", exc)

    async def _get_player(self, guild: discord.Guild) -> Optional[wavelink.Player]:
        return guild.voice_client

    async def _join(self, ctx: Context) -> wavelink.Player:
        channel = ctx.author.voice.channel
        if ctx.voice_client:
            if ctx.voice_client.channel.id == channel.id:
                return ctx.voice_client
            await ctx.voice_client.move_to(channel)
            return ctx.voice_client
        return await channel.connect(cls=wavelink.Player)

    def _start_idle_timer(self, guild_id: int, player: wavelink.Player) -> None:
        self._cancel_idle_timer(guild_id)
        task = self.bot.loop.create_task(self._idle_monitor(guild_id, player))
        self._idle_tasks[guild_id] = task

    def _cancel_idle_timer(self, guild_id: int) -> None:
        task = self._idle_tasks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()

    async def _idle_monitor(self, guild_id: int, player: wavelink.Player) -> None:
        try:
            empty_since: Optional[datetime] = None
            while True:
                await asyncio.sleep(POLL_INTERVAL)
                if not getattr(player, "connected", True):
                    break
                vc = getattr(player, "channel", None)
                if vc is None:
                    break
                humans = [m for m in vc.members if not m.bot]
                queue_empty = self._queue_is_empty(player)
                is_idle = not getattr(player, "playing", False) and queue_empty
                if len(humans) == 0 or is_idle:
                    if empty_since is None:
                        empty_since = datetime.now(timezone.utc)
                    elif (datetime.now(timezone.utc) - empty_since).total_seconds() >= IDLE_TIMEOUT:
                        try:
                            await player.disconnect()
                        except Exception:
                            pass
                        self._cancel_idle_timer(guild_id)
                        break
                else:
                    empty_since = None
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Idle monitor error guild %s", guild_id)

    def _queue_is_empty(self, player: wavelink.Player) -> bool:
        queue = getattr(player, "queue", None)
        if queue is None:
            return True
        try:
            return queue.is_empty
        except AttributeError:
            pass
        try:
            return queue.empty()
        except Exception:
            return True

    async def _update_vc_status(self, vc: discord.VoiceChannel, title: str) -> None:
        text = f"{SPEAKER} {title}"
        try:
            await vc.edit(status=text)
        except Exception:
            pass

    async def _apply_filters(self, player: wavelink.Player, filters: wavelink.Filters) -> None:
        try:
            await player.set_filters(filters)
        except Exception as exc:
            log.warning("Failed to apply filters: %s", exc)
    @hybrid_command(name="connect", description="Manually connect to the Lavalink node.")
    @has_permissions(manage_guild=True)
    async def connect(self, ctx: Context) -> discord.Message:
        if self._node_ready.is_set():
            return await ctx.warn("Already connected to the Lavalink node.")
        try:
            await asyncio.wait_for(self._connect_node(), timeout=15)
            return await ctx.approve("Connected to the Lavalink node.")
        except asyncio.TimeoutError:
            return await ctx.warn("Connection to Lavalink timed out.")
        except Exception as exc:
            return await ctx.warn(f"Failed to connect: {exc}")

    @hybrid_command(name="play", description="Play a song from YouTube, Spotify, Apple Music, Deezer, or Twitch.")
    @has_permissions(connect=True, speak=True)
    async def play(self, ctx: Context, *, query: str) -> discord.Message:
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.warn("You must be in a voice channel to use this command.")

        if not self._node_ready.is_set():
            return await ctx.warn("Music node is not connected. Report this error to our support server (get invite via `,support`)")

        try:
            player = await self._join(ctx)
        except Exception as exc:
            return await ctx.warn(f"Failed to join voice channel: {exc}")

        try:
            tracks = await wavelink.Playable.search(query)
        except Exception as exc:
            return await ctx.warn(f"Search failed: {exc}")

        if not tracks:
            return await ctx.warn(f"No results found for: `{query}`")

        if len(tracks) == 1:
            track = tracks[0]
            try:
                player.queue.put(track)
            except Exception as exc:
                return await ctx.warn(f"Failed to queue track: {exc}")
            if not player.playing:
                try:
                    await player.play(track)
                    self._start_idle_timer(ctx.guild.id, player)
                    await self._update_vc_status(ctx.author.voice.channel, track.title)
                    return await ctx.approve(f"Now playing: **{track.title}**")
                except Exception as exc:
                    return await ctx.warn(f"Failed to play track: {exc}")
            else:
                return await ctx.approve(f"Added to queue: **{track.title}**")

        embed = Embed(
            title="Search Results",
            description=f"Found `{len(tracks)}` results. Select a track below:",
            color=COLORS.neutral,
        )
        for idx, track in enumerate(tracks[:SEARCH_RESULTS_LIMIT]):
            embed.add_field(
                name=f"`{idx + 1}.` {track.title}",
                value=track.author or "Unknown artist",
                inline=False,
            )
        view = SearchView(ctx, tracks[:SEARCH_RESULTS_LIMIT])
        return await ctx.send(embed=embed, view=view)

    @hybrid_command(name="skip", description="Skip the currently playing track.")
    @has_permissions(connect=True, speak=True)
    async def skip(self, ctx: Context) -> discord.Message:
        player = await self._get_player(ctx.guild)
        if not player or not player.playing:
            return await ctx.warn("Nothing is playing right now.")
        try:
            await player.skip()
            self._start_idle_timer(ctx.guild.id, player)
            return await ctx.approve("Skipped the current track.")
        except Exception as exc:
            return await ctx.warn(f"Failed to skip: {exc}")

    @hybrid_command(name="stop", description="Stop everything and leave the voice channel.")
    @has_permissions(connect=True, speak=True)
    async def stop(self, ctx: Context) -> discord.Message:
        player = await self._get_player(ctx.guild)
        if not player:
            return await ctx.warn("I'm not in a voice channel.")
        self._cancel_idle_timer(ctx.guild.id)
        try:
            await player.stop()
        except Exception:
            pass
        try:
            await player.disconnect()
        except Exception:
            pass
        return await ctx.approve("Stopped the music and left the voice channel.")

    @hybrid_command(name="queue", description="View the current music queue.")
    @has_permissions(connect=True, speak=True)
    async def queue(self, ctx: Context) -> discord.Message:
        player = await self._get_player(ctx.guild)
        if not player:
            return await ctx.warn("I'm not in a voice channel.")

        current = getattr(player, "current", None)
        queue = getattr(player, "queue", None)
        if queue is None:
            queue_empty = True
        else:
            try:
                queue_empty = queue.is_empty
            except AttributeError:
                try:
                    queue_empty = queue.empty()
                except Exception:
                    queue_empty = True

        if not current and queue_empty:
            return await ctx.warn("The queue is empty.")

        lines = []
        if current and getattr(current, "title", None):
            lines.append(f"**Now Playing:** {current.title}")
        else:
            lines.append("**Now Playing:** _None_")

        if not queue_empty:
            items = []
            for idx, track in enumerate(queue, start=1):
                title = getattr(track, "title", "Unknown")
                items.append(f"`{idx}.` {title}")
            lines.append("\n**Up Next:**")
            lines.extend(items[:20])
            if len(items) > 20:
                lines.append(f"_...and {len(items) - 20} more_")
        else:
            lines.append("\n**Up Next:** _Empty_")

        embed = Embed(
            title="Music Queue",
            description="\n".join(lines),
            color=COLORS.neutral,
        )
        return await ctx.send(embed=embed)

    @hybrid_command(name="repeat", description="Toggle or set the repeat mode for the queue.")
    @has_permissions(connect=True, speak=True)
    async def repeat(self, ctx: Context, mode: str = "toggle") -> discord.Message:
        player = await self._get_player(ctx.guild)
        if not player or not getattr(player, "queue", None):
            return await ctx.warn("I'm not in a voice channel or the queue is unavailable.")

        queue = player.queue
        current_mode = getattr(queue, "mode", None)

        normalized = mode.strip().lower()
        if normalized in ("toggle", "cycle"):
            if current_mode is wavelink.QueueMode.normal:
                new_mode = wavelink.QueueMode.loop
                label = "Track"
            elif current_mode is wavelink.QueueMode.loop:
                new_mode = wavelink.QueueMode.loop_all
                label = "Queue"
            else:
                new_mode = wavelink.QueueMode.normal
                label = "Off"
        elif normalized in ("off", "0", "none"):
            new_mode = wavelink.QueueMode.normal
            label = "Off"
        elif normalized in ("track", "1", "song"):
            new_mode = wavelink.QueueMode.loop
            label = "Track"
        elif normalized in ("queue", "2", "all"):
            new_mode = wavelink.QueueMode.loop_all
            label = "Queue"
        else:
            return await ctx.warn("Invalid repeat mode. Use: `off`, `track`, or `queue`.")

        try:
            queue.mode = new_mode
            self._start_idle_timer(ctx.guild.id, player)
            return await ctx.approve(f"Repeat mode set to **{label}**.")
        except Exception as exc:
            return await ctx.warn(f"Failed to set repeat mode: {exc}")

    @hybrid_group(
        name="preset",
        description="Apply audio presets to the current track.",
        with_app_command=False,
    )
    @has_permissions(connect=True, speak=True)
    async def preset(self, ctx: Context) -> discord.Message:
        if ctx.invoked_subcommand is None:
            embed = Embed(
                title="Audio Presets",
                description=(
                    "Available presets:\n"
                    "`nightcore` — speeds up and raises pitch\n"
                    "`daycore` — slows down and lowers pitch\n"
                    "`earrape` — max volume + bass boost + distortion\n"
                    "`bassboost` — bass boosted\n"
                    "`8d` — 8D rotating audio\n"
                    "`chipmunk` — extremely high pitch\n"
                    "`clear` — removes all presets and resets filters"
                ),
                color=COLORS.neutral,
            )
            return await ctx.send(embed=embed)

    async def _require_playing(self, ctx: Context) -> Optional[wavelink.Player]:
        player = await self._get_player(ctx.guild)
        if not player or not player.playing:
            await ctx.warn("Nothing is playing right now.")
            return None
        return player

    @preset.command(name="nightcore", description="Nightcore preset: speeds up and raises pitch.")
    @has_permissions(connect=True, speak=True)
    async def preset_nightcore(self, ctx: Context) -> discord.Message:
        player = await self._require_playing(ctx)
        if not player:
            return await ctx.send(embed=Embed(description="Nothing is playing.", color=COLORS.warn))
        try:
            filters = wavelink.Filters()
            filters.timescale.set(speed=1.25, pitch=1.25, rate=1.0)
            await self._apply_filters(player, filters)
            self._start_idle_timer(ctx.guild.id, player)
            return await ctx.approve("Applied **Nightcore** preset.")
        except Exception as exc:
            return await ctx.warn(f"Failed to apply preset: {exc}")

    @preset.command(name="daycore", description="Daycore preset: slows down and lowers pitch.")
    @has_permissions(connect=True, speak=True)
    async def preset_daycore(self, ctx: Context) -> discord.Message:
        player = await self._require_playing(ctx)
        if not player:
            return await ctx.send(embed=Embed(description="Nothing is playing.", color=COLORS.warn))
        try:
            filters = wavelink.Filters()
            filters.timescale.set(speed=0.8, pitch=0.8, rate=1.0)
            await self._apply_filters(player, filters)
            self._start_idle_timer(ctx.guild.id, player)
            return await ctx.approve("Applied **Daycore** preset.")
        except Exception as exc:
            return await ctx.warn(f"Failed to apply preset: {exc}")

    @preset.command(name="earrape", description="Maximum volume + bass boost + distortion.")
    @has_permissions(connect=True, speak=True)
    async def preset_earrape(self, ctx: Context) -> discord.Message:
        player = await self._require_playing(ctx)
        if not player:
            return await ctx.send(embed=Embed(description="Nothing is playing.", color=COLORS.warn))
        try:
            filters = wavelink.Filters()
            filters.distortion.set(scale=50.0, offset=0.5)
            filters.equalizer.set(bands=[{"band": 0, "gain": 1.0}])
            await self._apply_filters(player, filters)
            await player.set_volume(100)
            self._start_idle_timer(ctx.guild.id, player)
            return await ctx.approve(f"Applied **Earrape** preset.")
        except Exception as exc:
            return await ctx.warn(f"Failed to apply preset: {exc}")

    @preset.command(name="bassboost", description="Bass boost preset.")
    @has_permissions(connect=True, speak=True)
    async def preset_bassboost(self, ctx: Context) -> discord.Message:
        player = await self._require_playing(ctx)
        if not player:
            return await ctx.send(embed=Embed(description="Nothing is playing.", color=COLORS.warn))
        try:
            filters = wavelink.Filters()
            filters.equalizer.set(bands=[{"band": 0, "gain": 0.25}])
            await self._apply_filters(player, filters)
            self._start_idle_timer(ctx.guild.id, player)
            return await ctx.approve("Applied **Bass Boost** preset.")
        except Exception as exc:
            return await ctx.warn(f"Failed to apply preset: {exc}")

    @preset.command(name="8d", description="8D audio preset.")
    @has_permissions(connect=True, speak=True)
    async def preset_8d(self, ctx: Context) -> discord.Message:
        player = await self._require_playing(ctx)
        if not player:
            return await ctx.send(embed=Embed(description="Nothing is playing.", color=COLORS.warn))
        try:
            filters = wavelink.Filters()
            filters.rotation.set(rotation_hz=0.2)
            await self._apply_filters(player, filters)
            self._start_idle_timer(ctx.guild.id, player)
            return await ctx.approve("Applied **8D** preset.")
        except Exception as exc:
            return await ctx.warn(f"Failed to apply preset: {exc}")

    @preset.command(name="chipmunk", description="Extremely speeds up the pitch only.")
    @has_permissions(connect=True, speak=True)
    async def preset_chipmunk(self, ctx: Context) -> discord.Message:
        player = await self._require_playing(ctx)
        if not player:
            return await ctx.send(embed=Embed(description="Nothing is playing.", color=COLORS.warn))
        try:
            filters = wavelink.Filters()
            filters.timescale.set(speed=1.0, pitch=2.0, rate=1.0)
            await self._apply_filters(player, filters)
            self._start_idle_timer(ctx.guild.id, player)
            return await ctx.approve("Applied **Chipmunk** preset.")
        except Exception as exc:
            return await ctx.warn(f"Failed to apply preset: {exc}")

    @preset.command(name="clear", description="Remove all presets and reset filters.")
    @has_permissions(connect=True, speak=True)
    async def preset_clear(self, ctx: Context) -> discord.Message:
        player = await self._require_playing(ctx)
        if not player:
            return await ctx.send(embed=Embed(description="Nothing is playing.", color=COLORS.warn))
        try:
            filters = wavelink.Filters()
            filters.reset()
            await self._apply_filters(player, filters)
            self._start_idle_timer(ctx.guild.id, player)
            return await ctx.approve("Cleared all presets and reset filters.")
        except Exception as exc:
            return await ctx.warn(f"Failed to clear presets: {exc}")

    @hybrid_command(name="volume", description="Set the playback volume (1-100).")
    @has_permissions(connect=True, speak=True)
    async def volume(self, ctx: Context, volume: int) -> discord.Message:
        if not 1 <= volume <= 100:
            return await ctx.warn("Volume must be between 1 and 100.")
        player = await self._get_player(ctx.guild)
        if not player or not player.playing:
            return await ctx.warn("Nothing is playing right now.")
        try:
            await player.set_volume(volume)
            self._start_idle_timer(ctx.guild.id, player)
            return await ctx.approve(f"Set volume to **{volume}%**")
        except Exception as exc:
            return await ctx.warn(f"Failed to set volume: {exc}")

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEvent) -> None:
        player = getattr(payload, "player", None)
        if not player:
            return
        guild_id = getattr(getattr(player, "guild", None), "id", 0)
        if not self._queue_is_empty(player):
            try:
                next_track = player.queue.get()
                await player.play(next_track)
                vc = getattr(player, "channel", None)
                if vc:
                    await self._update_vc_status(vc, next_track.title)
                self._start_idle_timer(guild_id, player)
            except asyncio.TimeoutError:
                self._start_idle_timer(guild_id, player)
            except Exception:
                log.exception("Failed to play next track")
                self._start_idle_timer(guild_id, player)
        else:
            self._start_idle_timer(guild_id, player)

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEvent) -> None:
        player = getattr(payload, "player", None)
        if not player:
            return
        track = getattr(payload, "track", None) or getattr(payload, "original", None)
        vc = getattr(player, "channel", None)
        if vc and track:
            await self._update_vc_status(vc, track.title)
        guild = getattr(player, "guild", None)
        if guild:
            self._start_idle_timer(guild.id, player)


class TrackSelect(Select):
    def __init__(self, ctx: Context, tracks: List[wavelink.Playable]) -> None:
        self.ctx = ctx
        self.tracks = tracks[:SEARCH_RESULTS_LIMIT]
        options = [
            discord.SelectOption(
                label=track.title[:100],
                description=track.author[:100] if track.author else "Unknown artist",
                value=str(index),
            )
            for index, track in enumerate(self.tracks)
        ]
        super().__init__(
            placeholder="Select a track...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            index = int(self.values[0])
            track = self.tracks[index]
            cog: "Music" = self.ctx.cog
            player = await cog._join(self.ctx)
            try:
                player.queue.put(track)
            except Exception as exc:
                return await interaction.edit_original_response(
                    embed=Embed(description=f"Failed to queue track: `{exc}`", color=COLORS.deny)
                )
            if not player.playing:
                try:
                    await player.play(track)
                    cog._start_idle_timer(self.ctx.guild.id, player)
                    await cog._update_vc_status(self.ctx.author.voice.channel, track.title)
                    await interaction.edit_original_response(
                        embed=Embed(description=f"Now playing: **{track.title}**", color=COLORS.approve)
                    )
                except Exception as exc:
                    await interaction.edit_original_response(
                        embed=Embed(description=f"Failed to play track: `{exc}`", color=COLORS.deny)
                    )
            else:
                await interaction.edit_original_response(
                    embed=Embed(description=f"Added to queue: **{track.title}**", color=COLORS.approve)
                )
        except (ValueError, IndexError):
            await interaction.edit_original_response(
                embed=Embed(description="Invalid selection.", color=COLORS.deny)
            )


class SearchView(View):
    def __init__(self, ctx: Context, tracks: List[wavelink.Playable]) -> None:
        super().__init__(timeout=120)
        self.ctx = ctx
        self.add_item(TrackSelect(ctx, tracks))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                embed=Embed(description="You're not the **author** of this search!", color=COLORS.warn),
                ephemeral=True,
            )
            return False
        return True


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
