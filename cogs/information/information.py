# STATUS: WORKING BUT PROBABLY NOT COMPLETE
import os
import platform
import random
import time
import humanize
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.client.embed import Embed
from core.client.commands import hybrid_command
from core.context import Paginator
from core.config import COLORS, EMOJIS


class Information(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @hybrid_command(aliases=["si"],
            description="View server information",
            example="")
    @app_commands.describe(user="The member to view info for (owner only)")
    async def serverinfo(self, ctx: commands.Context, *, user: Optional[discord.Member] = None):
        guild = ctx.guild
        if not guild:
            return await ctx.deny("This command can only be used in a server.")

        target = user or ctx.author

        features = ", ".join(guild.features) if guild.features else "None"

        embed = Embed(
            title=guild.name,
            color=COLORS.neutral,
            timestamp=datetime.utcnow(),
        )
        embed.set_thumbnail(url=guild.icon.url if guild.icon else None)

        if guild.description:
            embed.description = guild.description

        embed.add_field(
            name="General",
            value=(
                f"**Name:** {guild.name}\n"
                f"**ID:** {guild.id}\n"
                f"**Owner:** {guild.owner}\n"
                f"**Created:** {discord.utils.format_dt(guild.created_at)}\n"
                f"**Features:** {features}"
            ),
            inline=False,
        )

        embed.add_field(
            name="Members",
            value=(
                f"**Total:** {guild.member_count}\n"
                f"**Humans:** {len([m for m in guild.members if not m.bot])}\n"
                f"**Bots:** {len([m for m in guild.members if m.bot])}"
            ),
            inline=True,
        )

        embed.add_field(
            name="Channels",
            value=(
                f"**Text:** {len(guild.text_channels)}\n"
                f"**Voice:** {len(guild.voice_channels)}\n"
                f"**Categories:** {len(guild.categories)}\n"
                f"**Threads:** {len(guild.threads)}"
            ),
            inline=True,
        )

        embed.add_field(
            name="Counts",
            value=(
                f"**Roles:** {len(guild.roles)}\n"
                f"**Emojis:** {len(guild.emojis)}\n"
                f"**Stickers:** {len(guild.stickers)}"
            ),
            inline=True,
        )

        embed.add_field(
            name="Settings",
            value=(
                f"**Verification:** {guild.verification_level}\n"
                f"**Content Filter:** {guild.explicit_content_filter}\n"
                f"**MFA Level:** {guild.mfa_level}\n"
                f"**Locale:** {guild.preferred_locale}"
            ),
            inline=False,
        )

        if guild.premium_tier > 0:
            embed.add_field(
                name="Nitro Boosts",
                value=(
                    f"**Tier:** {guild.premium_tier}\n"
                    f"**Boosts:** {guild.premium_subscription_count}"
                ),
                inline=True,
            )

        if guild.banner:
            embed.set_image(url=guild.banner.url)

        if guild.splash:
            embed.set_footer(text=f"Invite Splash: {guild.splash.url}")

        await ctx.send(embed=embed)

    @hybrid_command(aliases=["ui"],
            description="View user information",
            example="@user")
    @app_commands.describe(user="The member to view info for")
    async def userinfo(self, ctx: commands.Context, *, user: Optional[discord.Member] = None):
        if user is None:
            user = ctx.author

        roles = [role.mention for role in reversed(user.roles[1:])]
        role_count = len(user.roles) - 1

        activity = "No Status"
        if user.activities:
            for activity_obj in user.activities:
                if isinstance(activity_obj, discord.CustomActivity):
                    if activity_obj.name:
                        activity = f"Custom Status: {activity_obj.name}"
                elif isinstance(activity_obj, discord.Activity):
                    activity = f"Playing {activity_obj.name}"

        status_emojis = {
            discord.Status.online: EMOJIS.ONLINE,
            discord.Status.idle: EMOJIS.IDLE,
            discord.Status.dnd: EMOJIS.DND,
            discord.Status.offline: EMOJIS.OFFLINE,
        }
        status_emoji = status_emojis.get(user.status, EMOJIS.OFFLINE)

        embed = Embed(
            title=f"{user.name}",
            color=user.color if user.color != discord.Color.default() else COLORS.neutral,
            timestamp=datetime.utcnow(),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_author(name=f"{user.name} ({user.id})", icon_url=user.display_avatar.url)

        embed.add_field(
            name="User Information",
            value=(
                f"**Username:** {user.name}\n"
                f"**Nickname:** {user.nick or 'None'}\n"
                f"**Status:** {status_emoji} {user.status}\n"
                f"**Activity:** {activity}"
            ),
            inline=False,
        )

        embed.add_field(
            name="Server Information",
            value=(
                f"**Joined:** {discord.utils.format_dt(user.joined_at)}\n"
                f"**Roles:** {role_count} ({', '.join(roles[:5]) if roles else 'None'})"
            ),
            inline=True,
        )

        embed.add_field(
            name="Account Information",
            value=(
                f"**Created:** {discord.utils.format_dt(user.created_at)}\n"
                f"**Boosted:** {'Yes' if user.premium_since else 'No'}"
            ),
            inline=True,
        )

        if user.color != discord.Color.default():
            embed.add_field(
                name="Color",
                value=f"**Hex:** {user.color}\n**Role:** {user.color.to_rgb()}",
                inline=False,
            )

        if user.voice:
            embed.add_field(
                name="Voice",
                value=f"**Channel:** {user.voice.channel.mention}\n**Deafened:** {user.voice.deaf}\n**Muted:** {user.voice.mute}",
                inline=False,
            )

        await ctx.send(embed=embed)

    @hybrid_command(aliases=["memberbanner", "userbanner"],
            description="Show a user's banner",
            example="@user")
    @app_commands.describe(user="The user whose banner to show")
    async def banner(self, ctx: commands.Context, *, user: Optional[discord.User] = None):
        if user is None:
            user = ctx.author

        user = await ctx.bot.fetch_user(user.id)

        embed = Embed(
            title=f"{user.name}'s Banner",
            color=COLORS.neutral,
        )

        if user.banner:
            embed.set_image(url=user.banner.url)
        else:
            embed.set_image(url="https://placehold.co/256x256?text=NoBanner")
            embed.description = "This user has no banner set."

        await ctx.send(embed=embed)

    @hybrid_command(aliases=["pfp", "profilepicture", "av"],
            description="Show a user's avatar",
            example="@user")
    @app_commands.describe(user="The user whose avatar to show")
    async def avatar(self, ctx: commands.Context, *, user: Optional[discord.User] = None):
        if user is None:
            user = ctx.author

        user = await ctx.bot.fetch_user(user.id)

        embed = Embed(
            title=f"{user.name}'s Avatar",
            color=COLORS.neutral,
        )
        embed.set_image(url=user.display_avatar.url)

        await ctx.send(embed=embed)

    @hybrid_command(aliases=["guildbanner", "gb"], description="Show the server banner")
    async def serverbanner(self, ctx: commands.Context):
        if not ctx.guild:
            return await ctx.deny("This command can only be used in a server.")

        embed = Embed(
            title=f"{ctx.guild.name} Banner",
            color=COLORS.neutral,
        )

        if ctx.guild.banner:
            embed.set_image(url=ctx.guild.banner.url)
        else:
            embed.set_image(url="https://placehold.co/256x256?text=NoBanner")
            embed.description = "This server has no banner set."

        await ctx.send(embed=embed)

    @hybrid_command(aliases=["servicon", "sicon", "guildicon", "gicon"], description="Show the server icon")
    async def servericon(self, ctx: commands.Context):
        if not ctx.guild:
            return await ctx.deny("This command can only be used in a server.")

        embed = Embed(
            title=f"{ctx.guild.name} Icon",
            color=COLORS.neutral,
        )

        if ctx.guild.icon:
            embed.set_image(url=ctx.guild.icon.url)
        else:
            embed.set_image(url="https://placehold.co/256x256?text=NoIcon")
            embed.description = "This server has no icon set."

        await ctx.send(embed=embed)

    @hybrid_command(aliases=["chi", "chinfo"],
            description="View channel information",
            example="#general")
    @app_commands.describe(channel="The channel to view info about")
    async def channelinfo(self, ctx: commands.Context, channel: Optional[discord.abc.GuildChannel] = None):
        if not ctx.guild:
            return await ctx.deny("This command can only be used in a server.")

        channel = channel or ctx.channel

        embed = Embed(
            title=f"{channel.name}",
            color=COLORS.neutral,
            timestamp=datetime.utcnow(),
        )

        channel_type_names = {
            discord.ChannelType.text: "Text Channel",
            discord.ChannelType.voice: "Voice Channel",
            discord.ChannelType.category: "Category",
            discord.ChannelType.news: "News/Announcement Channel",
            discord.ChannelType.news_thread: "News Thread",
            discord.ChannelType.public_thread: "Public Thread",
            discord.ChannelType.private_thread: "Private Thread",
            discord.ChannelType.stage_voice: "Stage Channel",
            discord.ChannelType.forum: "Forum Channel",
            discord.ChannelType.media: "Media Channel",
        }

        type_name = channel_type_names.get(channel.type, str(channel.type))

        info_value = f"**Name:** {channel.name}\n**ID:** {channel.id}\n**Type:** {type_name}\n**Created:** {discord.utils.format_dt(channel.created_at)}"

        if isinstance(channel, discord.CategoryChannel):
            info_value += f"\n**Channels:** {len(channel.channels)}"
        elif isinstance(channel, discord.TextChannel):
            info_value += f"\n**Topic:** {channel.topic or 'None'}"
            info_value += f"\n**NSFW:** {channel.is_nsfw()}"
            info_value += f"\n**Slowmode:** {channel.slowmode_delay}s"
            if channel.category:
                info_value += f"\n**Category:** {channel.category.name}"
        elif isinstance(channel, discord.VoiceChannel):
            info_value += f"\n**Bitrate:** {channel.bitrate // 1000}kbps"
            info_value += f"\n**User Limit:** {channel.user_limit or 'Unlimited'}"
            if channel.category:
                info_value += f"\n**Category:** {channel.category.name}"
        elif isinstance(channel, discord.StageChannel):
            info_value += f"\n**Bitrate:** {channel.bitrate // 1000}kbps"
            info_value += f"\n**Topic:** {channel.topic or 'None'}"
            if channel.category:
                info_value += f"\n**Category:** {channel.category.name}"
        elif isinstance(channel, discord.ForumChannel):
            info_value += f"\n**Topic:** {channel.topic or 'None'}"
            info_value += f"\n**NSFW:** {channel.is_nsfw()}"
        elif isinstance(channel, discord.Thread):
            info_value += f"\n**Parent:** {channel.parent.name if channel.parent else 'Unknown'}"
            info_value += f"\n**Archived:** {channel.archived}"
            info_value += f"\n**Auto-Archive:** {channel.auto_archive_duration}min"

        embed.add_field(name="Channel Info", value=info_value, inline=False)

        if hasattr(channel, 'overwrites') and channel.overwrites:
            view_perms = []
            connect_perms = []
            speak_perms = []

            for target, overwrite in channel.overwrites:
                perms = []
                if isinstance(channel, discord.abc.GuildChannel):
                    for name, value in overwrite:
                        if value is not None:
                            perms.append(f"{name}: {value}")

                if isinstance(target, discord.Role):
                    target_name = f"@{target.name}"
                else:
                    target_name = target.name

                if perms:
                    perms_str = ", ".join(perms[:3])
                    if len(perms) > 3:
                        perms_str += f" (+{len(perms) - 3})"

                    for p in perms:
                        if 'view_channel' in p:
                            view_perms.append(f"{target_name}: {p}")
                        elif 'connect' in p:
                            connect_perms.append(f"{target_name}: {p}")
                        elif 'speak' in p:
                            speak_perms.append(f"{target_name}: {p}")

            if view_perms:
                embed.add_field(name="View Overwrites", value="\n".join(view_perms[:5]), inline=True)
            if connect_perms:
                embed.add_field(name="Connect Overwrites", value="\n".join(connect_perms[:5]), inline=True)
            if speak_perms:
                embed.add_field(name="Speak Overwrites", value="\n".join(speak_perms[:5]), inline=True)

        await ctx.send(embed=embed)
    @hybrid_command(aliases=["stickers"], description="View the server's stickers")
    async def sticker(self, ctx: commands.Context):
        if not ctx.guild:
            return await ctx.deny("This command can only be used in a server.")

        stickers = list(ctx.guild.stickers)
        if not stickers:
            return await ctx.deny("This server has no stickers.")

        embeds = []
        for sticker in stickers:
            embed = Embed(
                title=sticker.name,
                description=sticker.description or "No description",
                color=COLORS.neutral,
            )
            embed.add_field(name="ID", value=sticker.id, inline=True)
            embed.add_field(name="Format", value=str(sticker.format), inline=True)
            embed.add_field(
                name="Available",
                value=sticker.available,
                inline=True,
            )
            if sticker.user:
                embed.add_field(name="Uploaded By", value=sticker.user.mention, inline=True)
            embed.set_thumbnail(url=sticker.url)
            embed.set_footer(
                text=f"{sticker.name} ({sticker.id}) • Page {len(embeds) + 1}/{len(stickers)}"
            )
            embeds.append(embed)

        if hasattr(ctx, "paginate"):
            await ctx.paginate(embeds)
        else:
            await ctx.send(embed=embeds[0], view=Paginator(ctx, embeds))

    @hybrid_command(description="Get the support server invite")
    async def support(self, ctx: commands.Context):
        invite = os.getenv("SUPPORT_INVITE")
        if not invite:
            return await ctx.deny("No support server invite has been configured.")

        await ctx.send(
            embed=Embed(
                description=f"[Click here to join the support server]({invite})",
                color=COLORS.neutral,
            )
        )

    @hybrid_command(description="View someone's Spotify activity")
    async def spotify(self, ctx: commands.Context, *, user: Optional[discord.Member] = None):
        user = user or ctx.author

        spotify = discord.utils.get(user.activities, type=discord.Spotify)
        if not spotify:
            return await ctx.deny(f"**{user.display_name}** is not listening to Spotify.")

        embed = Embed(
            title=spotify.title,
            description=(
                f"**Artists:** {', '.join(spotify.artists)}\n"
                f"**Album:** {spotify.album}\n"
                f"**Duration:** {humanize.precisedelta(spotify.duration, minimum_unit='seconds')}"
            ),
            color=spotify.color,
            timestamp=datetime.utcnow(),
        )
        embed.set_thumbnail(url=spotify.album_cover_url)
        embed.set_author(
            name=f"{user.display_name}'s Spotify",
            icon_url="https://cdn.discordapp.com/emojis/1134139302089494599.png",
        )

        start = spotify.start
        end = spotify.end
        if start and end:
            total = int((end - start).total_seconds())
            elapsed = int((discord.utils.utcnow() - start).total_seconds())
            elapsed = max(0, min(elapsed, total))
            bar_count = 15
            filled = int((elapsed / total) * bar_count) if total else 0
            bar = "▬" * filled + "🔘" + "▬" * (bar_count - filled)
            embed.add_field(
                name="\u200b",
                value=f"`{humanize.naturaldelta(elapsed)}` {bar} `-{humanize.naturaldelta(total - elapsed)}`",
                inline=False,
            )

        await ctx.send(embed=embed)

    @hybrid_command(aliases=["invinfo"], description="View info about an invite")
    async def inviteinfo(self, ctx: commands.Context, invite: Optional[str] = None):
        if not invite:
            return await ctx.send_help(ctx.command)

        try:
            invite_obj = await ctx.bot.fetch_invite(invite, with_counts=True, with_expiration=True)
        except discord.NotFound:
            return await ctx.deny("That invite is **invalid** or expired.")
        except discord.HTTPException:
            return await ctx.deny("I couldn't fetch that invite.")

        embed = Embed(
            title=f"Invite: {invite_obj.code}",
            color=COLORS.neutral,
            timestamp=datetime.utcnow(),
        )

        if invite_obj.guild:
            embed.set_thumbnail(url=invite_obj.guild.icon.url if invite_obj.guild.icon else None)
            embed.add_field(
                name="Guild",
                value=(
                    f"**Name:** {invite_obj.guild.name}\n"
                    f"**ID:** {invite_obj.guild.id}\n"
                    f"**Members:** {invite_obj.approximate_member_count or 'Unknown'}\n"
                    f"**Online:** {invite_obj.approximate_presence_count or 'Unknown'}"
                ),
                inline=False,
            )

        if invite_obj.channel:
            embed.add_field(
                name="Channel",
                value=f"**Name:** {invite_obj.channel.mention}\n**Type:** {invite_obj.channel.type}",
                inline=True,
            )

        if invite_obj.inviter:
            embed.add_field(
                name="Inviter",
                value=f"**User:** {invite_obj.inviter.mention}\n**ID:** {invite_obj.inviter.id}",
                inline=True,
            )

        if invite_obj.uses is not None:
            embed.add_field(name="Uses", value=f"{invite_obj.uses}/{invite_obj.max_uses or '∞'}", inline=True)

        if invite_obj.max_age:
            embed.add_field(name="Expires", value=humanize.naturaldelta(invite_obj.max_age), inline=True)

        await ctx.send(embed=embed)

    @hybrid_command(aliases=["inv"], description="Invite hollow to your server")
    async def invite(self, ctx: commands.Context):
        await ctx.send(
            embed=Embed(
                description=f"[Click here to invite **{ctx.bot.user.name}** to your server](https://discord.com/oauth2/authorize?client_id={ctx.bot.user.id}&permissions=8&scope=bot%20applications.commands)",
                color=COLORS.neutral,
            )
        )

    @hybrid_command(description="Check the bot's latency")
    async def ping(self, ctx: commands.Context):
        targets = [
            "your ip",
            "netflix database",
            "pentagon servers",
            "area 51 mainframe",
            "fortnite item shop",
            "your dad's browser history",
            "the backrooms wi-fi",
            "santa's naughty list",
            "nasa secret files",
            "roblox headquarters",
            "minecraft chunk generator",
            "elons mars rover",
            "the matrix source code",
            "skynet decision engine",
            "discord's actual servers",
            "the illuminati member portal",
            "your wifi router",
            "the iceberg below discord",
            "notion's 17th outage this week",
            "the one piece",
            "rickroll database",
            "the backrooms level 0",
            "your screen time report",
            "the dark web homepage",
            "steam's summer sale server",
            "the digits of pi",
            "spongebob's krabby patty formula",
            "gordon ramsay's raw steak",
            "shrek's swamp thermostat",
            "the grand line",
            "your unread email count",
            "the infinite improbability drive",
            "a creative writing ai",
            "your mom's facebook profile",
            "the fbi watchlist",
            "your cringe tiktok drafts",
            "the krabby patty secret formula",
            "north korea's only computer",
            "the queen's private spotify playlist",
            "your spotify wrapped",
            "the google docs you forgot to close",
            "your ex's new relationship status",
            "the simulation's save file",
            "your sleep schedule",
            "the DMV queue server",
            "the cat video recommendation algorithm",
            "your browser's 47 open tabs",
        ]

        random_target = random.choice(targets).replace("'", "\u2019")
        start = time.perf_counter()
        msg = await ctx.send(f"it took `{ctx.bot.ping()}ms` to ping **{random_target}** (edit: `0ms`)")
        end = time.perf_counter()
        edit_ms = round((end - start) * 1000, 2)

        await msg.edit(
            content=(
                f"It took `{ctx.bot.ping()}ms` to ping **{random_target}** (edit: `{edit_ms}ms`)"
            )
        )


    @hybrid_command(aliases=["bi"], description="Show information about the bot")
    async def botinfo(self, ctx: commands.Context):
        bot = ctx.bot
        total_members = sum(guild.member_count or 0 for guild in bot.guilds)
        total_commands = sum(len(list(cog.walk_commands())) for cog in bot.cogs.values())
        uptime = bot.booted() if isinstance(bot.booted(), str) else "unknown"
        # AutoShardedBot.owner_id is None until the application owner is
        # fetched. The explicitly configured IDs are reliable immediately and
        # contain the owner ID on normal deployments.
        owner_id = bot.owner_id or (bot.owner_ids[0] if bot.owner_ids else None)
        owner_display = f"<@{owner_id}>" if owner_id else "Unknown"

        embed = Embed(
            title=bot.user.name,
            color=COLORS.neutral,
            timestamp=datetime.utcnow(),
        )
        embed.set_thumbnail(url=bot.user.display_avatar.url)

        embed.add_field(
            name="Bot",
            value=(
                f"**ID:** {bot.user.id}\n"
                f"**Created:** {discord.utils.format_dt(bot.user.created_at)}\n"
                f"**Owner:** {owner_display}\n"
                f"**Uptime:** {uptime}"
            ),
            inline=False,
        )

        embed.add_field(
            name="Stats",
            value=(
                f"**Servers:** {len(bot.guilds)}\n"
                f"**Members:** {total_members}\n"
                f"**Commands:** {total_commands}\n"
                f"**Latency:** {bot.ping()}ms"
            ),
            inline=True,
        )

        embed.add_field(
            name="Library",
            value=(
                f"**discord.py:** {discord.__version__}\n"
                f"**Python:** {platform.python_version()}"
            ),
            inline=True,
        )

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Information(bot))
