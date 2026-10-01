# STATUS: COMPLETE
import re
import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional
from core.client.embed import Embed
from core.config import COLORS, EMOJIS
from core.context import hollowHelp
# Pristine defaults captured at import, before cog_load mutates EMOJIS from the DB.
DEFAULT_EMOJIS = {
    "approve": EMOJIS.APPROVE,
    "deny": EMOJIS.DENY,
    "warn": EMOJIS.WARN,
    "cooldown": EMOJIS.COOLDOWN,
}

class Config(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # load bot_config into memory so EMOJIS/COLORS reflect DB
        try:
            cur = await self.bot.db.execute("SELECT emoji_approve, emoji_deny, emoji_warn, emoji_cooldown, neutral_color FROM bot_config WHERE id = 1")
            row = await cur.fetchone()
            if row:
                if row["emoji_approve"]:
                    EMOJIS.APPROVE = row["emoji_approve"]
                if row["emoji_deny"]:
                    EMOJIS.DENY = row["emoji_deny"]
                if row["emoji_warn"]:
                    EMOJIS.WARN = row["emoji_warn"]
                if row["emoji_cooldown"]:
                    EMOJIS.COOLDOWN = row["emoji_cooldown"]
                if row["neutral_color"] is not None:
                    COLORS.neutral = int(row["neutral_color"])
        except Exception:
            pass

    @commands.hybrid_group(name="prefix", description="Manage server prefixes")
    async def prefix(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @prefix.group(name="self",
            description="Manage your personal prefix")
    async def prefix_self(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @prefix_self.command(name="add",
            description="Add a personal prefix",
            example="prefix self add ?")
    @app_commands.describe(prefix="The prefix to add (you can only have one)")
    async def prefix_self_add(self, ctx: commands.Context, prefix: str):
        try:
            cursor = await self.bot.db.execute(
                "SELECT id FROM user_config WHERE user_id = ?",
                (ctx.author.id,),
            )
            existing = await cursor.fetchone()

            if existing:
                await ctx.deny("You can only have one personal prefix. Use `prefix self remove` first.")
                return

            await self.bot.db.execute(
                "INSERT INTO user_config (user_id, prefix) VALUES (?, ?)",
                (ctx.author.id, prefix),
            )
            await self.bot.db.commit()
            await ctx.approve(f"Personal prefix set to `{prefix}`.")
        except Exception as e:
            await ctx.deny(f"Failed to set personal prefix: {e}")

    @prefix_self.command(name="remove",
            description="Remove your personal prefix",
            example="prefix self remove")
    async def prefix_self_remove(self, ctx: commands.Context):
        try:
            await self.bot.db.execute(
                "DELETE FROM user_config WHERE user_id = ?",
                (ctx.author.id,),
            )
            await self.bot.db.commit()
            await ctx.approve("Personal prefix removed.")
        except Exception as e:
            await ctx.deny(f"Failed to remove personal prefix: {e}")

    @prefix.command(name="add",
            description="Set the server prefix",
            example="prefix add !")
    @app_commands.describe(prefix="The prefix to set")
    async def prefix_add(self, ctx: commands.Context, prefix: str):
        if not ctx.guild:
            return await ctx.deny("This command can only be used in a server.")

        try:
            await self.bot.db.execute(
                "INSERT INTO guild_config (guild_id, prefix) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET prefix = excluded.prefix",
                (ctx.guild.id, prefix),
            )
            await self.bot.db.commit()
            await ctx.approve(f"Prefix set to `{prefix}` for this server.")
        except Exception as e:
            await ctx.deny(f"Failed to set prefix: {e}")

    @prefix.command(name="remove",
            description="Remove the server prefix",
            example="prefix remove")
    async def prefix_remove(self, ctx: commands.Context):
        if not ctx.guild:
            return await ctx.deny("This command can only be used in a server.")

        try:
            await self.bot.db.execute(
                "DELETE FROM guild_config WHERE guild_id = ?",
                (ctx.guild.id,),
            )
            await self.bot.db.commit()
            await ctx.approve("Prefix removed for this server.")
        except Exception as e:
            await ctx.deny(f"Failed to remove prefix: {e}")

    @prefix.command(name="list",
            description="List the server prefix",
            example="prefix list")
    async def prefix_list(self, ctx: commands.Context):
        if not ctx.guild:
            return await ctx.deny("This command can only be used in a server.")

        try:
            cursor = await self.bot.db.execute(
                "SELECT prefix FROM guild_config WHERE guild_id = ?",
                (ctx.guild.id,),
            )
            result = await cursor.fetchone()
            if result:
                await ctx.send(embed=Embed(
                    title="Server Prefix",
                    description=f"Current prefix: `{result['prefix']}`",
                    color=COLORS.neutral
                ))
            else:
                await ctx.deny("No custom prefix set for this server.")
        except Exception as e:
            await ctx.deny(f"Failed to list prefixes: {e}")

    @commands.hybrid_group(name="bot", description="Manage bot settings")
    async def bot(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @bot.command(name="emoji_approve",
            description="Set the approve emoji for embeds",
            example="bot emoji_approve :white_check_mark:")
    @app_commands.describe(emoji="The emoji to use for approve")
    async def embed_emoji_approve(self, ctx: commands.Context, emoji: str):
        try:
            await self.bot.db.execute(
                "UPDATE bot_config SET emoji_approve = ? WHERE id = 1",
                (emoji,),
            )
            await self.bot.db.commit()
            EMOJIS.APPROVE = emoji
            await ctx.approve(f"Approve emoji set to {emoji}")
        except Exception as e:
            await ctx.deny(f"Failed to set emoji: {e}")

    @bot.command(name="emoji_deny",
            description="Set the deny emoji for embeds",
            example="bot emoji_deny :x:")
    @app_commands.describe(emoji="The emoji to use for deny")
    async def embed_emoji_deny(self, ctx: commands.Context, emoji: str):
        try:
            await self.bot.db.execute(
                "UPDATE bot_config SET emoji_deny = ? WHERE id = 1",
                (emoji,),
            )
            await self.bot.db.commit()
            EMOJIS.DENY = emoji
            await ctx.approve(f"Deny emoji set to {emoji}")
        except Exception as e:
            await ctx.deny(f"Failed to set emoji: {e}")

    @bot.command(name="color",
            description="Change the bot's neutral colour",
            example="bot color 0xFF5733")
    @app_commands.describe(color="The color in hex (e.g., 0xFF5733) or decimal")
    async def embed_color(self, ctx: commands.Context, color: str):
        try:
            color_int = int(color, 16) if color.startswith("0x") else int(color)
            if not (0 <= color_int <= 0xFFFFFF):
                raise ValueError

            await self.bot.db.execute(
                "UPDATE bot_config SET neutral_color = ? WHERE id = 1",
                (color_int,),
            )
            await self.bot.db.commit()
            COLORS.neutral = color_int
            await ctx.approve(f"Neutral color set to `0x{color_int:06X}`.")
        except ValueError:
            await ctx.deny("Invalid color. Use hex format (e.g., 0xFF5733) or a decimal number.")

    @bot.command(name="reset_emojis",
            description="Reset the emoji configuration to the defaults",
            example="bot reset_emojis")
    async def reset_emojis(self, ctx: commands.Context):
        try:
            await self.bot.db.execute(
                "UPDATE bot_config SET emoji_approve = ?, emoji_deny = ?, emoji_warn = ?, emoji_cooldown = ? WHERE id = 1",
                (
                    DEFAULT_EMOJIS["approve"],
                    DEFAULT_EMOJIS["deny"],
                    DEFAULT_EMOJIS["warn"],
                    DEFAULT_EMOJIS["cooldown"],
                ),
            )
            await self.bot.db.commit()

            EMOJIS.APPROVE = DEFAULT_EMOJIS["approve"]
            EMOJIS.DENY = DEFAULT_EMOJIS["deny"]
            EMOJIS.WARN = DEFAULT_EMOJIS["warn"]
            EMOJIS.COOLDOWN = DEFAULT_EMOJIS["cooldown"]

            await ctx.approve("Emoji configuration reset to defaults.")
        except Exception as e:
            await ctx.deny(f"Failed to reset emojis: {e}")

    @commands.hybrid_group(name="alias", description="Manage command aliases")
    async def alias(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @alias.command(name="removeall",
            description="Remove all aliases for a command",
            example=",alias removeall ban")
    @app_commands.describe(command_name="The command to remove all aliases from")
    async def alias_removeall(self, ctx: commands.Context, command_name: str):
        if not ctx.guild:
            return await ctx.deny("This command can only be used in a server.")

        try:
            cursor = await self.bot.db.execute(
                "DELETE FROM aliases WHERE guild_id = ? AND command_name = ?",
                (ctx.guild.id, command_name),
            )
            await self.bot.db.commit()
            if cursor.rowcount:
                await ctx.approve(f"Removed all aliases for `{command_name}`.")
            else:
                await ctx.deny(f"No aliases found for command `{command_name}`.")
        except Exception as e:
            await ctx.deny(f"Failed to remove aliases: {e}")

    @alias.command(name="remove",
            description="Remove an alias for a command",
            example=",alias remove b")
    @app_commands.describe(shortcut="The alias shortcut to remove")
    async def alias_remove(self, ctx: commands.Context, shortcut: str):
        if not ctx.guild:
            return await ctx.deny("This command can only be used in a server.")

        try:
            cursor = await self.bot.db.execute(
                "DELETE FROM aliases WHERE guild_id = ? AND shortcut = ?",
                (ctx.guild.id, shortcut),
            )
            await self.bot.db.commit()
            if cursor.rowcount:
                await ctx.approve(f"Alias `{shortcut}` removed.")
            else:
                await ctx.deny(f"No alias found for shortcut `{shortcut}`.")
        except Exception as e:
            await ctx.deny(f"Failed to remove alias: {e}")

    @alias.command(name="list",
            description="List every alias for all commands",
            example=",alias list")
    async def alias_list(self, ctx: commands.Context):
        if not ctx.guild:
            return await ctx.deny("This command can only be used in a server.")

        try:
            cursor = await self.bot.db.execute(
                "SELECT shortcut, command_name FROM aliases WHERE guild_id = ?",
                (ctx.guild.id,),
            )
            results = await cursor.fetchall()

            if results:
                description = "\n".join(
                    [f"`{row['shortcut']}` → `{row['command_name']}`" for row in results]
                )
                await ctx.send(embed=Embed(
                    title="Aliases",
                    description=description,
                    color=COLORS.neutral,
                ))
            else:
                await ctx.deny("No aliases set for this server.")
        except Exception as e:
            await ctx.deny(f"Failed to list aliases: {e}")

    @alias.command(name="add",
            description="Create an alias for a command",
            example=",alias add b ban")
    @app_commands.describe(shortcut="The shortcut to create", command_name="The command to alias")
    async def alias_add(self, ctx: commands.Context, shortcut: str, command_name: str):
        if not ctx.guild:
            return await ctx.deny("This command can only be used in a server.")

        command = self.bot.get_command(command_name)
        if command is None:
            return await ctx.deny(f"Command `{command_name}` not found.")

        try:
            await self.bot.db.execute(
                "INSERT INTO aliases (guild_id, command_name, shortcut) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, shortcut) DO UPDATE SET command_name = excluded.command_name",
                (ctx.guild.id, command_name, shortcut),
            )
            await self.bot.db.commit()
            await ctx.approve(f"Alias `{shortcut}` created for command `{command_name}`.")
        except Exception as e:
            await ctx.deny(f"Failed to create alias: {e}")

    @alias.command(name="view",
            description="View command execution for an alias",
            example=",alias view b")
    @app_commands.describe(shortcut="The alias shortcut to view")
    async def alias_view(self, ctx: commands.Context, shortcut: str):
        if not ctx.guild:
            return await ctx.deny("This command can only be used in a server.")

        try:
            cursor = await self.bot.db.execute(
                "SELECT command_name FROM aliases WHERE guild_id = ? AND shortcut = ?",
                (ctx.guild.id, shortcut),
            )
            result = await cursor.fetchone()
            if result:
                await ctx.send(embed=Embed(
                    title="Alias Info",
                    description=f"Shortcut: `{shortcut}` → Command: `{result['command_name']}`",
                    color=COLORS.neutral,
                ))
            else:
                await ctx.deny(f"No alias found for shortcut `{shortcut}`.")
        except Exception as e:
            await ctx.deny(f"Failed to view alias: {e}")

    @commands.hybrid_command(name="help", aliases=["h", "cmds"], description="Get help for commands")
    async def _help(self, ctx: commands.Context, *, query: Optional[str] = None):

        if not query:
            return await hollowHelp.send_bot_help(ctx)

        command = self.bot.get_command(query)
        if command is None:
            return await ctx.deny("No command or group found for **" + query + "**.")

        if isinstance(command, commands.Group):
            return await hollowHelp.send_group_help(ctx, command)
        return await hollowHelp.send_command_help(ctx, command)

    @commands.hybrid_group(name="buttonrole", description="Manage button roles")
    async def buttonrole(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @buttonrole.command(
        name="add",
        description="Create a buttonrole",
        example="buttonrole add [message_link] [role] primary [label]",
    )
    @app_commands.describe(
        message_link="The message link or ID",
        role="The role to assign",
        button_style="The button style",
        label="The button label",
        emoji="The button emoji (optional)",
    )
    async def buttonrole_add(
        self,
        ctx: commands.Context,
        message_link: str,
        role: discord.Role,
        button_style: str,
        label: str,
        emoji: Optional[str] = None,
    ):
        if not ctx.guild:
            return await ctx.deny("This command can only be used in a server.")

        guild_id, channel_id, message_id = self._parse_message_link(message_link)
        if message_id is None or channel_id is None:
            return await ctx.deny("Invalid message link or ID.")

        style = button_style.lower()
        valid_styles = {"primary", "secondary", "success", "danger", "warning", "blurple", "grey"}
        if style not in valid_styles:
            return await ctx.deny(f"Invalid button style. Valid styles: {', '.join(valid_styles)}")

        try:
            await self.bot.db.execute(
                "INSERT INTO buttonroles (guild_id, message_id, channel_id, role_id, button_style, label, emoji) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, message_id, role_id) DO UPDATE SET button_style = excluded.button_style, label = excluded.label, emoji = excluded.emoji",
                (ctx.guild.id, message_id, channel_id, role.id, style, label, emoji),
            )
            await self.bot.db.commit()
            await ctx.approve(f"Buttonrole created for {role.mention} on message `{message_id}`.")
        except Exception as e:
            await ctx.deny(f"Failed to create buttonrole: {e}")

    @buttonrole.command(name="list", description="List every buttonrole in the server", example="buttonrole list")
    async def buttonrole_list(self, ctx: commands.Context):
        if not ctx.guild:
            return await ctx.deny("This command can only be used in a server.")

        try:
            cursor = await self.bot.db.execute(
                "SELECT message_id, channel_id, role_id, button_style, label, emoji, user_limit FROM buttonroles WHERE guild_id = ?",
                (ctx.guild.id,),
            )
            results = await cursor.fetchall()

            if not results:
                return await ctx.deny("No buttonroles set for this server.")

            lines = []
            for row in results:
                link = f"https://discord.com/channels/{ctx.guild.id}/{row['channel_id']}/{row['message_id']}"
                emoji_str = f"{row['emoji']} " if row["emoji"] else ""
                limit_str = f" (limit: {row['user_limit']})" if row["user_limit"] else ""
                lines.append(f"`{row['message_id']}` | {emoji_str}{row['label']} | <@&{row['role_id']}> | {row['button_style']}{limit_str}\n{link}")

            await ctx.send(embed=Embed(
                title="Buttonroles",
                description="\n\n".join(lines)[:4096],
                color=COLORS.neutral,
            ))
        except Exception as e:
            await ctx.deny(f"Failed to list buttonroles: {e}")

    @buttonrole.command(name="remove", description="Remove a buttonrole from a message", example="buttonrole remove [message_link]")
    @app_commands.describe(message_link="The message link or ID")
    async def buttonrole_remove(self, ctx: commands.Context, message_link: str):
        if not ctx.guild:
            return await ctx.deny("This command can only be used in a server.")

        guild_id, channel_id, message_id = self._parse_message_link(message_link)
        if message_id is None or channel_id is None:
            return await ctx.deny("Invalid message link or ID.")

        try:
            cursor = await self.bot.db.execute(
                "DELETE FROM buttonroles WHERE guild_id = ? AND message_id = ?",
                (ctx.guild.id, message_id),
            )
            await self.bot.db.commit()
            if cursor.rowcount:
                await ctx.approve(f"Removed all buttonroles from message `{message_id}`.")
            else:
                await ctx.deny("No buttonroles found for that message.")
        except Exception as e:
            await ctx.deny(f"Failed to remove buttonrole: {e}")

    @buttonrole.command(
        name="edit",
        description="Edit a buttonrole",
        example="buttonrole edit [message_link]",
    )
    @app_commands.describe(
        message_link="The message link or ID",
        role="The new role (optional)",
        button_style="The new button style (optional)",
        label="The new label (optional)",
        emoji="The new emoji (optional)",
    )
    async def buttonrole_edit(
        self,
        ctx: commands.Context,
        message_link: str,
        role: Optional[discord.Role] = None,
        button_style: Optional[str] = None,
        label: Optional[str] = None,
        emoji: Optional[str] = None,
    ):
        if not ctx.guild:
            return await ctx.deny("This command can only be used in a server.")

        guild_id, channel_id, message_id = self._parse_message_link(message_link)
        if message_id is None or channel_id is None:
            return await ctx.deny("Invalid message link or ID.")

        if not any([role, button_style, label, emoji]):
            return await ctx.deny("You must provide at least one field to edit.")

        updates = {}
        if role:
            updates["role_id"] = role.id
        if button_style:
            style = button_style.lower()
            valid_styles = {"primary", "secondary", "success", "danger", "warning", "blurple", "grey"}
            if style not in valid_styles:
                return await ctx.deny(f"Invalid button style. Valid styles: {', '.join(valid_styles)}")
            updates["button_style"] = style
        if label:
            updates["label"] = label
        if emoji is not None:
            updates["emoji"] = emoji

        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values()) + [ctx.guild.id, message_id]

        try:
            cursor = await self.bot.db.execute(
                f"UPDATE buttonroles SET {set_clause} WHERE guild_id = ? AND message_id = ?",
                tuple(values),
            )
            await self.bot.db.commit()
            if cursor.rowcount:
                await ctx.approve("Buttonrole updated.")
            else:
                await ctx.deny("No buttonroles found for that message.")
        except Exception as e:
            await ctx.deny(f"Failed to edit buttonrole: {e}")

    @buttonrole.command(
        name="limit",
        description="Limit how many roles a user can take from a message",
        example="buttonrole limit [message_link] [limit]",
    )
    @app_commands.describe(message_link="The message link or ID", limit="The maximum roles per user (leave empty to remove)")
    async def buttonrole_limit(self, ctx: commands.Context, message_link: str, limit: Optional[int] = None):
        if not ctx.guild:
            return await ctx.deny("This command can only be used in a server.")

        guild_id, channel_id, message_id = self._parse_message_link(message_link)
        if message_id is None or channel_id is None:
            return await ctx.deny("Invalid message link or ID.")

        try:
            if limit is None:
                await self.bot.db.execute(
                    "UPDATE buttonroles SET user_limit = NULL WHERE guild_id = ? AND message_id = ?",
                    (ctx.guild.id, message_id),
                )
                await ctx.approve("Removed the user limit for this message.")
            else:
                await self.bot.db.execute(
                    "UPDATE buttonroles SET user_limit = ? WHERE guild_id = ? AND message_id = ?",
                    (limit, ctx.guild.id, message_id),
                )
                await ctx.approve(f"User limit set to {limit} for this message.")
            await self.bot.db.commit()
        except Exception as e:
            await ctx.deny(f"Failed to set limit: {e}")

    @buttonrole.command(name="styles", description="Show all button styles", example="buttonrole styles")
    async def buttonrole_styles(self, ctx: commands.Context):
        styles = [
            ("primary", "Blurple"),
            ("secondary", "Grey"),
            ("success", "Green"),
            ("danger", "Red"),
            ("warning", "Yellow"),
            ("blurple", "Blurple"),
            ("grey", "Grey"),
        ]
        desc = "\n".join(f"`{s}` - {d}" for s, d in styles)
        await ctx.send(embed=Embed(
            title="Button Styles",
            description=desc,
            color=COLORS.neutral,
        ))

    @buttonrole.command(name="reset", description="Mass-remove all buttonroles from messages", example="buttonrole reset")
    async def buttonrole_reset(self, ctx: commands.Context):
        if not ctx.guild:
            return await ctx.deny("This command can only be used in a server.")

        try:
            await self.bot.db.execute(
                "DELETE FROM buttonroles WHERE guild_id = ?",
                (ctx.guild.id,),
            )
            await self.bot.db.commit()
            await ctx.approve("All buttonroles have been removed.")
        except Exception as e:
            await ctx.deny(f"Failed to reset buttonroles: {e}")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if not interaction.data or interaction.data.get("component_type") != 2:
            return

        custom_id = interaction.data.get("custom_id", "")
        if not custom_id.startswith("buttonrole:"):
            return

        parts = custom_id.split(":")
        if len(parts) != 3:
            return

        _, message_id_str, role_id_str = parts
        try:
            message_id = int(message_id_str)
            role_id = int(role_id_str)
        except ValueError:
            return

        cursor = await self.bot.db.execute(
            "SELECT * FROM buttonroles WHERE message_id = ? AND role_id = ?",
            (message_id, role_id),
        )
        row = await cursor.fetchone()
        if not row:
            return

        guild = interaction.guild
        if not guild:
            return await interaction.response.send_message("This can only be used in a server.", ephemeral=True)

        member = guild.get_member(interaction.user.id)
        if not member:
            return await interaction.response.send_message("Could not find you as a member.", ephemeral=True)

        role = guild.get_role(role_id)
        if not role:
            return await interaction.response.send_message("Role not found.", ephemeral=True)

        has_role = role in member.roles

        if has_role:
            try:
                await member.remove_roles(role, reason="Button role removal")
                await interaction.response.send_message(f"Removed {role.mention} from you.", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"Failed to remove role: {e}", ephemeral=True)
        else:
            if row["user_limit"]:
                cursor = await self.bot.db.execute(
                    "SELECT role_id FROM buttonroles WHERE message_id = ? AND guild_id = ?",
                    (message_id, guild.id),
                )
                all_roles = await cursor.fetchall()
                user_role_count = 0
                for r in all_roles:
                    br_role = guild.get_role(r["role_id"])
                    if br_role and br_role in member.roles:
                        user_role_count += 1

                if user_role_count >= row["user_limit"]:
                    await interaction.response.send_message(f"You have reached the limit of {row['user_limit']} roles from this message.", ephemeral=True)
                    return

            try:
                await member.add_roles(role, reason="Button role assignment")
                await interaction.response.send_message(f"Added {role.mention} to you.", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"Failed to add role: {e}", ephemeral=True)

    @commands.hybrid_group(name="embed", description="Create and manage embeds")
    async def embed(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await hollowHelp.send_group_help(ctx, ctx.command)

    @embed.command(
        name="create",
        aliases=["ec", "embedcreate"],
        description="Create an embed using the embed script feature",
        example="embed create {embed}$vtitle: Hello$vdescription: World",
    )
    @app_commands.describe(script="The embed script to use")
    async def embed_create(self, ctx: commands.Context, *, script: str):
        try:
            from core.client.EmbedBuilder import to_object
            await ctx.send(embed=to_object(script))
        except Exception as e:
            await ctx.deny(f"Failed to create embed: {e}")

    def _parse_message_link(self, link: str):
        pattern = r"https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)"
        match = re.match(pattern, link)
        if match:
            return int(match.group(1)), int(match.group(2)), int(match.group(3))
        try:
            return None, None, int(link)
        except ValueError:
            return None, None, None

    def _embed_to_script(self, content: Optional[str], embed: discord.Embed, view: Optional[discord.ui.View]) -> str:
        parts = []
        if content:
            parts.append(f"content: {content}")
        if embed.title:
            parts.append(f"title: {embed.title}")
        if embed.description:
            parts.append(f"description: {embed.description}")
        if embed.color:
            parts.append(f"color: {embed.color.value:06X}")
        if embed.image:
            parts.append(f"image: {embed.image.url}")
        if embed.thumbnail:
            parts.append(f"thumbnail: {embed.thumbnail.url}")
        if embed.author:
            author_parts = [embed.author.name or "\u200b"]
            if embed.author.icon_url:
                author_parts.append(embed.author.icon_url)
            if embed.author.url:
                author_parts.append(embed.author.url)
            parts.append("author: " + " && ".join(author_parts))
        for field in embed.fields:
            parts.append(f"field: {field.name} && {field.value} && {str(field.inline).lower()}")
        if embed.footer:
            footer_parts = [embed.footer.text or "\u200b"]
            if embed.footer.icon_url:
                footer_parts.append(embed.footer.icon_url)
            parts.append("footer: " + " && ".join(footer_parts))
        if view:
            for child in view.children:
                if isinstance(child, discord.ui.Button):
                    button_parts = []
                    if child.label:
                        button_parts.append(f"label: {child.label}")
                    if child.url:
                        button_parts.append(f"url: {child.url}")
                    if child.emoji:
                        button_parts.append(f"emoji: {child.emoji}")
                    if child.disabled:
                        button_parts.append("disabled")
                    style_map = {
                        discord.ButtonStyle.red: "red",
                        discord.ButtonStyle.green: "green",
                        discord.ButtonStyle.gray: "gray",
                        discord.ButtonStyle.blurple: "blurple",
                        discord.ButtonStyle.link: "link",
                    }
                    if child.style and child.style != discord.ButtonStyle.secondary:
                        button_parts.append(f"style: {style_map.get(child.style, 'gray')}")
                    if button_parts:
                        parts.append("button: " + " && ".join(button_parts))
        return "{embed}\n" + "\n$v\n".join(parts) if parts else "{embed}"

    @embed.command(
        name="code",
        description="Recreate a message's embed as an embed script",
        example="embed code [message_link]",
    )
    @app_commands.describe(message_link="The message link or ID to convert")
    async def embed_code(self, ctx: commands.Context, message_link: Optional[str] = None):
        target_message = None

        if ctx.message.reference and ctx.message.reference.message_id:
            try:
                target_message = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            except Exception:
                pass

        if not target_message and message_link:
            guild_id, channel_id, msg_id = self._parse_message_link(message_link)
            if msg_id is None:
                return await ctx.deny("Invalid message link or ID.")
            if channel_id is None:
                return await ctx.deny("Please provide a full message link (not just an ID).")
            try:
                channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                target_message = await channel.fetch_message(msg_id)
            except Exception:
                return await ctx.deny("Failed to fetch the message.")

        if not target_message:
            return await ctx.deny("Reply to a message with embeds or provide a message link.")

        if not target_message.embeds:
            return await ctx.deny("That message has no embeds.")

        scripts = []
        for embed in target_message.embeds:
            script = self._embed_to_script(target_message.content, embed, None)
            scripts.append(script)

        full_script = "\n$v\n".join(scripts)

        if len(full_script) > 2000:
            return await ctx.deny("The generated script is too long to display.")

        await ctx.send(f"```{full_script}```")

    @embed.command(
        name="save",
        description="Save an embed for future use",
        example="embed save {embed}$vtitle: Hello welcome",
    )
    @app_commands.describe(script="The embed script to save", name="The name to save it under")
    async def embed_save(self, ctx: commands.Context, script: str, name: str):
        try:
            await self.bot.db.execute(
                "INSERT INTO saved_embeds (user_id, name, script) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, name) DO UPDATE SET script = excluded.script",
                (ctx.author.id, name, script)
            )
            await self.bot.db.commit()
            await ctx.approve(f"Embed saved as `{name}`.")
        except Exception as e:
            await ctx.deny(f"Failed to save embed: {e}")

    @embed.command(
        name="view",
        description="View a saved embed",
        example="embed view welcome",
    )
    @app_commands.describe(name="The name of the saved embed")
    async def embed_view(self, ctx: commands.Context, name: str):
        try:
            cursor = await self.bot.db.execute(
                "SELECT script FROM saved_embeds WHERE user_id = ? AND name = ?",
                (ctx.author.id, name)
            )
            row = await cursor.fetchone()
            if not row:
                return await ctx.deny(f"No saved embed found with name `{name}`.")

            from core.client.EmbedBuilder import to_object
            await ctx.send(embed=to_object(row["script"]))
        except Exception as e:
            await ctx.deny(f"Failed to view embed: {e}")