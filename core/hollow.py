import os
import time
from datetime import timedelta
from typing import Optional
import aiohttp
import discord_vr
import discord

from discord.ext import commands, tasks
from discord.utils import MISSING
from dotenv import load_dotenv
import humanize

from core.client.embed import Embed
from core.client.reduceslash import NESTED_GROUP_NAMES
from core.context import Context
from core.logger import log
import core.client.interactions  # noqa: F401 - patches Interaction/Webhook

load_dotenv()

OWNER_IDS = [int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip().isdigit()]
DEFAULT_PREFIX = os.getenv("PREFIX", ",")
DATABASE_PATH = os.getenv("DATABASE_PATH", "core/schema/hollow.db")
COMMANDS_API_URL = os.getenv("COMMANDS_API_URL", "https://hollow-phi.vercel.app/api/commands/commands")
BOT_API_KEY = os.getenv("BOT_API_KEY", "")


class hollow(commands.AutoShardedBot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix=self.get_prefix,
            owner_ids=OWNER_IDS,
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=False,
                everyone=False,
                replied_user=False,
            ),
            intents=discord.Intents.all(),
            help_command=None,
            context_class=Context,
        )
        self.start_time: Optional[float] = None

    async def get_prefix(self, message: discord.Message):
        """
        Returns prefixes for the bot:
        - Mention (@Bot)
        - Custom per-server prefix from DB
        """
        # Determine custom prefix
        if not message.guild:
            prefixes = [DEFAULT_PREFIX]
        else:
            try:
                cursor = await self.db.execute(
                    "SELECT prefix FROM guild_config WHERE guild_id = ?",
                    (message.guild.id,),
                )
                row = await cursor.fetchone()
                if row and row["prefix"]:
                    prefixes = [row["prefix"]]
                else:
                    prefixes = [DEFAULT_PREFIX]
            except Exception:
                prefixes = [DEFAULT_PREFIX]

        # Wrap with mention support
        prefixes = list(prefixes)

        # Auto-generated slash groups (see core/client/todo.py NESTED_GROUPS) also
        # answer to their prefix name/aliases: `,moderation ban`, `,mod ban`, ...
        group_name = NESTED_GROUP_NAMES.get(self._strip_prefix(message.content, prefixes))
        if group_name:
            prefixes.append(group_name)

        return commands.when_mentioned_or(*prefixes)(self, message)

    @staticmethod
    def _strip_prefix(content: str, prefixes: list) -> str:
        """First whitespace separated token of `content` with a known prefix removed."""
        stripped = content.strip()
        for prefix in sorted((p for p in prefixes if p), key=len, reverse=True):
            if stripped.startswith(prefix):
                return stripped[len(prefix):].strip().split(" ")[0].casefold()
        return ""

    async def get_context(self, origin, *, cls=MISSING):
        if cls is MISSING:
            cls = Context
        return await super().get_context(origin, cls=cls)

    async def setup_hook(self) -> None:
        self.start_time = time.time()
        log.banner("hollow", "discord bot")
        await self.initialize_database()
        await self.load_cogs()
        self.register_auto_nested_groups()
        self.tree.on_error = self.on_app_command_error

        from core.client.reduceslash import GLOBAL_COMMAND_LIMIT
        top_level = self.tree.get_commands(guild=None)
        if len(top_level) > GLOBAL_COMMAND_LIMIT:
            log.warning(
                f"{len(top_level)} application commands exceed Discord's "
                f"{GLOBAL_COMMAND_LIMIT} global limit - demote more commands in "
                f"core/client/todo.py TEXT_ONLY_COMMANDS"
            )
        else:
            log.info(
                f"{len(top_level)}/{GLOBAL_COMMAND_LIMIT} global application command slots used"
            )

        synced = await self.tree.sync()
        log.success(f"Synced {len(synced)} application commands")

        self.sync_commands_json.start()

        # Patch send/edit to auto-convert Embed shim -> LayoutView
        from core.client.embed import patch_send
        patch_send()
        log.success("Patched send/edit for Components V2 Embed shim")

    async def initialize_database(self) -> None:
        """Initialize the database connection pool and run schema migrations."""
        import aiosqlite
        import os

        # Create database directory if it doesn't exist
        db_dir = os.path.dirname(DATABASE_PATH)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)

        # Connect to database and run schema
        self.db = await aiosqlite.connect(DATABASE_PATH)
        self.db.row_factory = aiosqlite.Row

        # Read and execute schema
        schema_path = os.path.join(os.path.dirname(__file__), "schema", "schema.sql")
        if os.path.exists(schema_path):
            with open(schema_path, "r", encoding="utf-8") as f:
                schema = f.read()
            await self.db.executescript(schema)
        else:
            # Fallback to basic tables if schema file missing
            await self.db.execute("""
                CREATE TABLE IF NOT EXISTS guild_config (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER UNIQUE NOT NULL,
                    prefix TEXT NOT NULL DEFAULT ','
                )
            """)
            await self.db.execute("""
                CREATE TABLE IF NOT EXISTS user_config (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE NOT NULL,
                    prefix TEXT NOT NULL
                )
            """)
            await self.db.execute("""
                CREATE TABLE IF NOT EXISTS bot_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    emoji_approve TEXT DEFAULT '<:approve:1547570974457733141>',
                    emoji_deny TEXT DEFAULT '<:deny:1547570956191535144>',
                    emoji_warn TEXT DEFAULT '<:warning:1547570970791772270>',
                    emoji_cooldown TEXT DEFAULT '<:cooldown:1547572522004914218>',
                    neutral_color INTEGER DEFAULT 0x2B2D31
                )
            """)
            await self.db.execute("""
                INSERT OR IGNORE INTO bot_config (id, emoji_approve, emoji_deny, emoji_warn, emoji_cooldown, neutral_color)
                VALUES (1, '<:approve:1547570974457733141>', '<:deny:1547570956191535144>', '<:warning:1547570970791772270>', '<:cooldown:1547572522004914218>', 0x2B2D31)
            """)
        await self.db.commit()
    async def on_shard(self) -> None:
        log.info(f"Shard {self.shard_id} ready")

    async def on_ready(self) -> None:
        log.success(f"Connected as {self.user}")

    async def on_command(self, ctx) -> None:
        log.info(f"{ctx.user} used command: {ctx.command}", name="Commands")
        try:
            cmd_name = ctx.command.qualified_name or ctx.command.name
            await self.db.execute(
                "INSERT INTO command_usage (command_name, usage_count) VALUES (?, 1) "
                "ON CONFLICT(command_name) DO UPDATE SET usage_count = usage_count + 1",
                (cmd_name,),
            )
            await self.db.commit()
        except Exception:
            pass

    async def on_command_error(self, ctx, error) -> None:
        from core.context import hollowHelp

        if isinstance(error, commands.MissingRequiredArgument):
            return await hollowHelp.send_command_help(ctx, ctx.command)

        if isinstance(error, (commands.MissingRole, commands.MissingPermissions, commands.CheckFailure)):
            return await ctx.deny("You don't have permission to use this command.")

        if isinstance(error, commands.CommandNotFound):
            return

        if isinstance(error, commands.CommandInvokeError):
            error = error.original

        # Unhandled error — log the full traceback and notify the user
        log.traceback(error, f"Ignoring exception in command {ctx.command}")

        try:
            await ctx.deny(f"Error while invoking the command: {error}\nplease report the error in our support server https://discord.gg/VhfaX9V6KD")
        except Exception:
            pass

    async def on_app_command_error(self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError) -> None:
        # Unwrap original error if wrapped
        original = getattr(error, "original", error)
        log.traceback(original, f"Ignoring exception in app command {interaction.command}")

        # Try to notify user if possible (interaction may already be responded to)
        try:
            msg = f"Error while invoking the command: {original}"
            if interaction.response.is_done():
                await interaction.followup.send(embed=Embed(description=msg, color=0xED4245), ephemeral=True)
            else:
                await interaction.response.send_message(embed=Embed(description=msg, color=0xED4245), ephemeral=True)
        except Exception:
            pass

    @tasks.loop(minutes=240)
    async def sync_commands_json(self) -> None:
        try:
            import inspect
            cursor = await self.db.execute("SELECT command_name, usage_count FROM command_usage")
            rows = await cursor.fetchall()
            usage = [{"name": row["command_name"], "count": row["usage_count"]} for row in rows]
            usage_map = {row["command_name"]: row["usage_count"] for row in rows}
        except Exception:
            return

        try:
            from core.context import hollowHelp
            synced_commands = []
            for name, cog in self.cogs.items():
                if name == "HelpCog":
                    continue
                for cmd in hollowHelp.collect_commands(cog):
                    params = getattr(cmd, "params", {})
                    args = []
                    for p in params.values():
                        if p.name in ("self", "ctx"):
                            continue
                        args.append({
                            "name": p.name,
                            "description": getattr(p, "description", "") or "",
                            "required": p.default is inspect.Parameter.empty,
                        })
                    synced_commands.append({
                        "name": cmd.qualified_name,
                        "usage_count": usage_map.get(cmd.qualified_name, 0),
                        "category": name.lower(),
                        "description": hollowHelp.command_description(cmd),
                        "arguments": args,
                        "permissions": hollowHelp.command_permission(cmd).split(", ") if hollowHelp.command_permission(cmd) != "N/A" else [],
                    })
        except Exception:
            return

        guilds_payload = []
        for g in self.guilds:
            guilds_payload.append({
                "name": g.name,
                "members": g.member_count or len(g.members) if hasattr(g, 'members') else 0,
                "icon": g.icon.url if g and g.icon else "",
                "verified": True,
            })

        total_users = 0
        for g in self.guilds:
            try:
                total_users += g.member_count or len(g.members) if hasattr(g, 'members') else 0
            except Exception:
                pass

        info_payload = {
            "guilds": len(self.guilds),
            "users": total_users,
        }

        base_url = COMMANDS_API_URL.replace("/api/commands/commands", "").rstrip("/")
        headers = {"Content-Type": "application/json"}
        if BOT_API_KEY:
            headers["Authorization"] = f"Bearer {BOT_API_KEY}"

        try:
            async with aiohttp.ClientSession() as session:
                if synced_commands:
                    try:
                        async with session.post(
                            f"{base_url}/api/commands/commands",
                            json={"commands": synced_commands},
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            if resp.status >= 400:
                                text = await resp.text()
                                log.error(f"Failed to sync commands: {resp.status} {text}")
                    except Exception:
                        pass

                if guilds_payload:
                    try:
                        async with session.post(
                            f"{base_url}/api/guilds/guilds",
                            json={"guilds": guilds_payload},
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            if resp.status >= 400:
                                text = await resp.text()
                                log.error(f"Failed to sync guilds: {resp.status} {text}")
                    except Exception:
                        pass

                if info_payload:
                    try:
                        async with session.post(
                            f"{base_url}/api/info/info",
                            json=info_payload,
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            if resp.status >= 400:
                                text = await resp.text()
                                log.error(f"Failed to sync info: {resp.status} {text}")
                    except Exception:
                        pass
        except Exception:
            pass

    def register_auto_nested_groups(self) -> None:
        """Add the auto-generated slash groups (e.g. /moderation) and their children.

        Children were attached while the cogs were being decorated; adding them now
        gives every one of them a cog, so `,ban` still routes to the right callback.
        """
        from core.client.commands import _AUTO_GROUPS

        for group in _AUTO_GROUPS.values():
            for child in group.commands:
                if child.cog is None:
                    owner = getattr(child.callback, "__self__", None)
                    if owner is not None:
                        child.cog = owner

    async def load_cogs(self) -> None:
        for root, dirs, files in os.walk("./cogs"):
            if "__init__.py" in files:
                # Package with __init__.py: load the package itself, skip its modules
                relpath = os.path.relpath(root, "./cogs")
                module_path = "" if relpath == "." else relpath.replace(os.sep, ".")
                if module_path:
                    try:
                        await self.load_extension(f"cogs.{module_path}")
                        log.success(f"Loaded cog: cogs.{module_path}")
                    except Exception as e:
                        if type(e).__name__ == "CommandLimitReached":
                            # keep loading the rest; the cog is skipped, not fatal
                            log.warning(f"Skipped cog cogs.{module_path}: {e}")
                        else:
                            log.error(f"Failed to load cog cogs.{module_path}: {e}")
                dirs.clear()  # don't descend, the package handles its own contents
                continue
            for filename in files:
                if not filename.endswith(".py"):
                    continue
                relpath = os.path.relpath(os.path.join(root, filename), "./cogs")
                module_path = relpath.replace(os.sep, ".")[:-3]
                try:
                    await self.load_extension(f"cogs.{module_path}")
                    log.success(f"Loaded cog: cogs.{module_path}")
                except Exception as e:
                    if type(e).__name__ == "CommandLimitReached":
                        log.warning(f"Skipped cog cogs.{module_path}: {e}")
                    else:
                        log.error(f"Failed to load cog cogs.{module_path}: {e}")

    def booted(self, unix: bool = False) -> str | float:
        if self.start_time is None:
            return "not booted yet"
        if unix:
            return time.time() - self.start_time
        elapsed = time.time() - self.start_time
        return humanize.naturaldelta(timedelta(seconds=elapsed))

    def ping(self) -> int:
        return round(self.latency * 1000)

    def run(self) -> None:
        token = os.getenv("DISCORD_TOKEN")
        if not token:
            raise RuntimeError("DISCORD_TOKEN is not set in the environment")
        super().run(token, log_handler=None)


bot = hollow()

