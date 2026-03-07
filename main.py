"""
NatsuBot — NSFW Rule34 Discord Bot
Main entry point. Configures logging, loads cogs, runs the bot.
"""

import os
import sys
import time
import asyncio
import traceback
import logging
from datetime import datetime
from typing import Optional

import aiohttp
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

from database import Database
from keep_alive import start_health_server

# ── Load .env ─────────────────────────────────────────────────────
load_dotenv()


# ══════════════════════════════════════════════════════════════════
#  Logging — single configuration point (BUG 4 fix)
# ══════════════════════════════════════════════════════════════════

logger = logging.getLogger("discord_bot")
logger.setLevel(logging.DEBUG)

_console = logging.StreamHandler(stream=sys.stdout)
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
logger.addHandler(_console)


# ══════════════════════════════════════════════════════════════════
#  Configuration
# ══════════════════════════════════════════════════════════════════

class Config:
    TOKEN: str = os.getenv("DISCORD_TOKEN", "")
    OWNER_IDS: list[int] = [
        int(i) for i in os.getenv("OWNER_IDS", "").split(",") if i.strip()
    ]
    PREFIX = commands.when_mentioned_or("n ", "n!", "natsu ", "N ")
    ERROR_WEBHOOK_URL: str = os.getenv("ERROR_WEBHOOK_URL", "")
    DEBUG_MODE: bool = os.getenv("DEBUG_MODE", "false").lower() == "true"

    @classmethod
    def validate(cls):
        if not cls.TOKEN:
            logger.critical("DISCORD_TOKEN not set — aborting.")
            sys.exit(1)
        logger.info("Configuration validated")


# ══════════════════════════════════════════════════════════════════
#  Allowed cogs — only these are loaded
# ══════════════════════════════════════════════════════════════════

ALLOWED_COGS = {"r34", "verification", "itch_cog"}


# ══════════════════════════════════════════════════════════════════
#  Bot class
# ══════════════════════════════════════════════════════════════════

class NatsuBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True

        super().__init__(
            command_prefix=Config.PREFIX,
            intents=intents,
            help_command=None,
            owner_ids=set(Config.OWNER_IDS),
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="🔞 /r34 | Adult Content Bot",
            ),
            status=discord.Status.online,
        )

        self.start_time = time.time()
        self.db = Database()
        self.session: Optional[aiohttp.ClientSession] = None
        self.ready = False

        logger.info("NatsuBot instance created")

    # ── Setup hook ────────────────────────────────────────────────

    async def setup_hook(self):
        logger.info("Running setup_hook …")

        # Initialize MongoDB
        await self.db.initialize()

        # HTTP session
        self.session = aiohttp.ClientSession()

        # Load cogs
        await self._load_extensions()

        # Background tasks
        self.status_update.start()

        logger.info("setup_hook complete")

    async def _load_extensions(self):
        os.makedirs("cogs", exist_ok=True)
        loaded, skipped = [], []

        for filename in sorted(os.listdir("cogs")):
            if not filename.endswith(".py") or filename.startswith("__"):
                continue
            name = filename[:-3]
            if name not in ALLOWED_COGS:
                skipped.append(name)
                logger.info(f"⏭  Skipped cog: {name}")
                continue
            try:
                await self.load_extension(f"cogs.{name}")
                loaded.append(name)
                logger.info(f"✅ Loaded cog: {name}")
            except Exception as exc:
                logger.error(f"❌ Failed to load {name}: {exc}")
                if Config.DEBUG_MODE:
                    traceback.print_exc()

        logger.info(
            f"Extensions: {len(loaded)} loaded, {len(skipped)} skipped"
        )

    # ── on_ready ──────────────────────────────────────────────────

    async def on_ready(self):
        if self.ready:
            logger.info("Reconnected")
            return
        self.ready = True

        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")

        # Sync slash commands
        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} global slash commands")
        except Exception as exc:
            logger.error(f"Command sync failed: {exc}")

        logger.info(f"Serving {len(self.guilds)} guild(s)")

    # ── Message handling ──────────────────────────────────────────

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        await self.process_commands(message)

    # ── Error handling ────────────────────────────────────────────

    async def on_command_error(self, ctx: commands.Context, error: Exception):
        ignored = (commands.CommandNotFound, commands.CheckFailure)
        if isinstance(error, ignored):
            return

        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"⚠️ Missing argument: `{error.param.name}`", delete_after=10)
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("⚠️ You lack permissions for this command.", delete_after=10)
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏱️ Cooldown — retry in {error.retry_after:.1f}s", delete_after=10)
        else:
            logger.error(f"Unhandled command error in {ctx.command}: {error}", exc_info=True)
            await ctx.send("⚠️ Something went wrong.", delete_after=10)

            if Config.ERROR_WEBHOOK_URL and self.session:
                await self._send_error_webhook(ctx, error)

    async def _send_error_webhook(self, ctx: commands.Context, error: Exception):
        try:
            payload = {
                "embeds": [{
                    "title": "Bot Error",
                    "description": f"```{str(error)[:1900]}```",
                    "fields": [
                        {"name": "Command", "value": ctx.command.name if ctx.command else "N/A", "inline": True},
                        {"name": "User", "value": f"{ctx.author} ({ctx.author.id})", "inline": True},
                        {"name": "Guild", "value": ctx.guild.name if ctx.guild else "DM", "inline": True},
                    ],
                    "color": 0xFF0000,
                    "timestamp": datetime.utcnow().isoformat(),
                }]
            }
            async with self.session.post(Config.ERROR_WEBHOOK_URL, json=payload) as resp:
                if resp.status not in (200, 204):
                    logger.warning(f"Webhook returned {resp.status}")
        except Exception as exc:
            logger.error(f"Error webhook failed: {exc}")

    # ── Background tasks ──────────────────────────────────────────

    @tasks.loop(minutes=10)
    async def status_update(self):
        if not self.is_ready():
            return
        try:
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name=f"🔞 /r34 | {len(self.guilds)} servers",
                )
            )
        except Exception:
            pass

    # ── Uptime helper ─────────────────────────────────────────────

    def format_uptime(self) -> str:
        seconds = int(time.time() - self.start_time)
        d, rem = divmod(seconds, 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        parts = []
        if d:
            parts.append(f"{d}d")
        if h:
            parts.append(f"{h}h")
        if m:
            parts.append(f"{m}m")
        parts.append(f"{s}s")
        return " ".join(parts)

    # ── Cleanup ───────────────────────────────────────────────────

    async def close(self):
        logger.info("Shutting down …")
        self.status_update.cancel()
        if self.session:
            await self.session.close()
        await self.db.close()
        await super().close()
        logger.info("Shutdown complete")


# ══════════════════════════════════════════════════════════════════
#  Global slash commands (not in a cog)
# ══════════════════════════════════════════════════════════════════

Config.validate()
bot = NatsuBot()


@bot.tree.command(name="ping", description="Check bot latency")
async def ping_slash(interaction: discord.Interaction):
    ws = round(bot.latency * 1000)
    color = discord.Color.green() if ws < 100 else discord.Color.yellow()
    embed = discord.Embed(title="🏓 Pong!", color=color)
    embed.add_field(name="WebSocket", value=f"{ws}ms", inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="uptime", description="Bot uptime")
async def uptime_slash(interaction: discord.Interaction):
    embed = discord.Embed(
        title="⏰ Uptime",
        description=f"```{bot.format_uptime()}```",
        color=discord.Color.blue(),
    )
    embed.add_field(name="Started", value=f"<t:{int(bot.start_time)}:R>", inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="stats", description="Bot statistics")
async def stats_slash(interaction: discord.Interaction):
    embed = discord.Embed(title="📊 Bot Statistics", color=discord.Color.blue())
    embed.add_field(name="Servers", value=f"{len(bot.guilds):,}", inline=True)
    embed.add_field(name="Users", value=f"{len(bot.users):,}", inline=True)
    embed.add_field(name="Uptime", value=bot.format_uptime(), inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="Cogs", value=str(len(bot.cogs)), inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="View all commands")
async def help_slash(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📚 NatsuBot Commands",
        description="An adult content discovery bot for age-restricted servers.",
        color=0xE91E63,
    )

    # R34 commands
    embed.add_field(
        name="🔞 R34 — Content Search",
        value=(
            "`/r34` — Search by tags\n"
            "`/r34random` — Random explicit post\n"
            "`/r34tags` — Search with autocomplete\n"
            "`/r34trending` — Top trending tags\n"
            "`/r34favorites` — Manage saved favorites\n"
            "`/r34stats` — Your personal stats\n"
            "`/r34source` — Reverse image lookup"
        ),
        inline=False,
    )

    # Verification commands
    embed.add_field(
        name="🔒 Verification",
        value=(
            "`/setup_verification` — Set up age verification (Admin)\n"
            "`/set_verification_banner` — Custom banner (Admin)\n"
            "`/verification_status` — View stats (Admin)"
        ),
        inline=False,
    )

    # Itch commands
    embed.add_field(
        name="🎮 Itch.io — Adult Games",
        value=(
            "`/itch search` — Search adult games\n"
            "`/itch trending` — Trending games\n"
            "`/itch random` — Random game\n"
            "`/itch new` — Newest releases"
        ),
        inline=False,
    )

    # Utility
    embed.add_field(
        name="⚙️ Utility",
        value="`/ping` `/uptime` `/stats` `/help`",
        inline=False,
    )

    embed.set_footer(text="Prefix commands also available: n!r34, n!r34r, n!r34g, n!r34a, n!r34h | or: n r34, n r34r ...")
    await interaction.response.send_message(embed=embed)


# ── Owner-only prefix commands ────────────────────────────────────

@bot.command(name="reload", hidden=True)
@commands.is_owner()
async def reload_cmd(ctx: commands.Context, cog_name: str):
    """Reload a cog by name."""
    ext = f"cogs.{cog_name}"
    try:
        await bot.reload_extension(ext)
        await ctx.send(f"✅ Reloaded `{cog_name}`")
    except commands.ExtensionNotLoaded:
        await bot.load_extension(ext)
        await ctx.send(f"✅ Loaded `{cog_name}`")
    except Exception as exc:
        await ctx.send(f"❌ {exc}")


@bot.command(name="sync", hidden=True)
@commands.is_owner()
async def sync_cmd(ctx: commands.Context):
    """Force-sync slash commands."""
    synced = await bot.tree.sync()
    await ctx.send(f"✅ Synced {len(synced)} commands globally")


@bot.command(name="shutdown", hidden=True)
@commands.is_owner()
async def shutdown_cmd(ctx: commands.Context):
    await ctx.send("🔌 Shutting down …")
    await bot.close()


# ══════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════

async def main():
    async with bot:
        await asyncio.gather(
            start_health_server(),
            bot.start(Config.TOKEN),
        )

if __name__ == "__main__":
    try:
        logger.info("🚀 Starting NatsuBot …")
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by keyboard")
    except Exception as exc:
        logger.critical(f"Fatal: {exc}", exc_info=True)
        sys.exit(1)
