"""Magic Key Assistant — entry point."""

__version__ = "0.8.0"

import asyncio
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Add script directory to sys.path to allow imports from the same directory (like ux_helpers, cogs)
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Configure logging once for entire bot
def setup_logging():
    """Configure logging once for entire bot with rotation"""
    # Best-effort: ensure console can display Unicode log messages on Windows.
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning("setup_logging: suppressed %s", e)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    
    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # File handler with rotation
    file_handler = RotatingFileHandler(
        'leisurellm.log',
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        mode='a'
    )
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    ))
    root_logger.addHandler(file_handler)
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(
        '%(levelname)s - %(name)s - %(message)s'
    ))
    root_logger.addHandler(console_handler)
    
    logging.info("Logging configured with rotation")

setup_logging()

logger = logging.getLogger(__name__)
logger.info("Starting bot script...")

from datetime import time as dt_time
from zoneinfo import ZoneInfo

import discord
from database import Database
from discord.ext import commands, tasks
from services import ServiceContainer

import config

logger = logging.getLogger(__name__)

# Timezone for scheduled tasks
EASTERN = ZoneInfo("America/New_York")

TARGET_EMOJI = "🛠️" 

# Use all intents for maximum compatibility
intents = discord.Intents.all()
logger.info("[BOOT] Using discord.Intents.all() for bot.")

# Initialize the bot
bot = commands.Bot(command_prefix='/', intents=intents)
logger.info("Bot instance created.")


# Admin GUI server
def start_admin_server(bot_instance=None):
    """Start the admin web GUI in a separate thread."""
    if not getattr(config, 'ADMIN_GUI_ENABLED', True):
        logger.info("Admin GUI disabled via config")
        return
    
    host = getattr(config, 'ADMIN_GUI_HOST', '127.0.0.1')
    port = getattr(config, 'ADMIN_GUI_PORT', 8000)
    
    try:
        import uvicorn
        from admin.server import app, set_bot_instance
        
        # Register bot instance for control endpoints
        if bot_instance:
            set_bot_instance(bot_instance)
        
        logger.info(f"Starting admin GUI server on http://{host}:{port}")

        # Auto-open browser for team mode too
        if os.environ.get("NO_BROWSER") != "1":
            def _delayed_open():
                import time as _t
                import webbrowser as _wb
                _t.sleep(3)
                _wb.open(f"http://{host}:{port}")
            import threading as _thr
            _thr.Thread(target=_delayed_open, daemon=True).start()

        # Configure uvicorn with a new event loop for this thread
        config_uvicorn = uvicorn.Config(
            app, 
            host=host, 
            port=port, 
            log_level="warning",
            loop="asyncio"
        )
        server = uvicorn.Server(config_uvicorn)
        
        # Run in this thread's event loop
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())
    except ImportError as e:
        logger.warning(f"Admin GUI dependencies not available: {e}")
    except Exception as e:
        logger.error(f"Failed to start admin GUI: {e}")
        import traceback
        logger.error(traceback.format_exc())
        logger.error(f"Failed to start admin GUI: {e}")

try:
    bot.service_container = ServiceContainer.build()
    logger.info("[BOOT] Service container initialized.")
except Exception as exc:
    logging.error(f"Failed to initialize service container: {exc}")
    bot.service_container = None

# Initialize database (will connect in main())
try:
    default_db_path = str(Path(__file__).resolve().parent / "assistant.db")
    database_path = os.getenv("DATABASE_PATH") or default_db_path
    bot.db = Database(database_path)
    logger.info(f"[BOOT] Database configured: {database_path}")
except Exception as exc:
    logging.error(f"Failed to configure database: {exc}")
    bot.db = None

@bot.event
async def on_raw_reaction_add(payload):
    if str(payload.emoji.name) == TARGET_EMOJI:
        channel = bot.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        llm_cog = bot.get_cog("LLM")
        if llm_cog:
            await llm_cog.Test_Answer(message)
        

# Function to load cogs (extensions) for the bot, skipping __init__.py and ingest_metadata.py
COGS_PACKAGE = "cogs"


async def load_cogs(bot):
    cogs_dir = Path(__file__).resolve().parent / "cogs"
    for path in cogs_dir.iterdir():
        if not path.suffix == ".py":
            continue
        name = path.stem
        if name in {"__init__", "ingest_metadata", "SetupWizard"}:
            continue
        try:
            await bot.load_extension(f"{COGS_PACKAGE}.{name}")
            logger.info(f"Loaded extension {COGS_PACKAGE}.{name}")
        except Exception as e:
            logger.error(f"Failed to load {COGS_PACKAGE}.{name}: {e}")
            import traceback
            logger.error(traceback.format_exc())
    logger.info("All cogs loaded.")
    # Load the /setup wizard (uses async setup() pattern)
    try:
        from cogs.SetupWizard import SetupWizard
        await bot.add_cog(SetupWizard(bot))
        logger.info("Loaded SetupWizard cog")
    except Exception as e:
        logger.warning(f"SetupWizard cog not loaded: {e}")
    # Print loaded cogs after all cogs are loaded
    logger.info(f"Loaded cogs: {bot.cogs}")


async def main():
    # ── Solo mode: admin GUI only, no Discord ──────────────────────────────
    if config.OPERATION_MODE == "solo" or not config.bot_token:
        logger.info("Running in SOLO mode — admin GUI only, no Discord bot")
        # Connect database
        if bot.db:
            try:
                await bot.db.connect()
                logger.info("Database connected successfully")
            except Exception as e:
                logger.error(f"Database connection failed: {e}")
                bot.db = None

        # Start admin GUI in the main thread's event loop
        import uvicorn
        from admin.server import app, set_bot_instance

        set_bot_instance(bot)  # provide DB access to admin routes

        host = getattr(config, "ADMIN_GUI_HOST", "127.0.0.1")
        port = getattr(config, "ADMIN_GUI_PORT", 8000)
        logger.info(f"Solo mode admin GUI on http://{host}:{port}")

        # Auto-open browser so the user doesn't have to remember the URL
        def _open_browser_delayed():
            import time
            time.sleep(2)  # give uvicorn a moment to bind
            import webbrowser
            webbrowser.open(f"http://{host}:{port}")

        if os.environ.get("NO_BROWSER") != "1":
            import threading as _threading
            _threading.Thread(target=_open_browser_delayed, daemon=True).start()

        server_config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(server_config)
        try:
            await server.serve()
        finally:
            if bot.db:
                try:
                    await bot.db.close()
                except Exception as e:
                    logger.warning("_open_browser_delayed: suppressed %s", e)
        return

    # ── Team mode: full Discord bot + admin GUI ────────────────────────────
    # Start admin GUI server in a background thread (pass bot instance for control)
    admin_thread = threading.Thread(target=start_admin_server, args=(bot,), daemon=True)
    admin_thread.start()
    logger.info("Admin GUI thread started")
    
    async with bot:
        try:
            # Connect to database if configured
            if bot.db:
                try:
                    await bot.db.connect()
                    logger.info("Database connected successfully")
                except Exception as e:
                    logger.error(f"Database connection failed: {e}")
                    logger.warning("Bot will run without database (degraded functionality)")
                    bot.db = None
            
            await load_cogs(bot)
            await bot.start(config.bot_token)
        except Exception as e:
            logger.error(f"Bot failed: {e}")
        finally:
            # Cleanup database connection
            if bot.db:
                try:
                    await bot.db.close()
                    logger.info("Database connection closed")
                except Exception as e:
                    logger.error(f"Error closing database: {e}")


async def _reload_cogs_impl():
    """Implementation of cog reload without interaction dependency"""
    cogs_dir = Path(__file__).resolve().parent / "cogs"
    for path in cogs_dir.iterdir():
        if not path.suffix == ".py":
            continue
        name = path.stem
        if name in {"__init__", "ingest_metadata", "SetupWizard"}:
            continue
        try:
            logger.info(f'Unloading {COGS_PACKAGE}.{name}')
            await bot.unload_extension(f'{COGS_PACKAGE}.{name}')
        except Exception as e:
            logger.error(f"Failed to unload {name}: {e}")
    
    for path in cogs_dir.iterdir():
        if not path.suffix == ".py":
            continue
        name = path.stem
        if name in {"__init__", "ingest_metadata", "SetupWizard"}:
            continue
        try:
            logger.info(f'Loading {COGS_PACKAGE}.{name}')
            await bot.load_extension(f'{COGS_PACKAGE}.{name}')
        except Exception as e:
            logger.error(f"Failed to reload {name}: {e}")
    
    logger.info("Cog reload complete")


@bot.tree.command(name='reload', description='Reload Files')
@commands.is_owner()
async def reload_cogs(interaction: discord.Interaction):
    """Slash command handler for manual reload"""
    await interaction.response.defer(ephemeral=True)
    try:
        await _reload_cogs_impl()
        await interaction.followup.send('✅ All cogs reloaded', ephemeral=True)
    except Exception as e:
        logger.error(f"Reload failed: {e}")
        await interaction.followup.send(f'❌ Reload failed: {e}', ephemeral=True)

# Syncs commands to the server
@bot.tree.command(name='sync', description='Sync commands')
@commands.is_owner()
async def sync(interaction: discord.Interaction):
    '''Sync commands'''
    try:
        await interaction.response.defer(ephemeral=True)
        # Prefer the guild where the command was invoked.
        # Falling back to config.server_id avoids breaking DM usage.
        target_guild_id = interaction.guild_id or getattr(config, "server_id", None)
        if not target_guild_id:
            synced = await bot.tree.sync()
            await interaction.followup.send(f'Synced {len(synced)} commands globally.', ephemeral=False)
            return

        target = discord.Object(id=int(target_guild_id))
        bot.tree.copy_global_to(guild=target)
        synced = await bot.tree.sync(guild=target)
        await interaction.followup.send(f'Synced {len(synced)} commands to guild {int(target_guild_id)}.', ephemeral=False)
    except Exception as e:
        logger.error(e)
        await interaction.followup.send(f'Following error occured {e}', ephemeral=False)

@tasks.loop(time=dt_time(hour=7, minute=0, tzinfo=EASTERN))
async def daily_reload():
    """Scheduled reload at 7am Eastern"""
    await bot.wait_until_ready()
    logger.info("Running scheduled cog reload")
    try:
        await _reload_cogs_impl()
        logger.info("Scheduled reload completed successfully")
    except Exception as e:
        logger.error(f"Scheduled reload failed: {e}")

@bot.event
async def on_ready():
    try:
        if not daily_reload.is_running():
            daily_reload.start()
        logger.info(f"We have logged in as {bot.user}")

        # ── Evergreen: catalog update + auto-pull missing models ─────
        import asyncio

        from services.model_discovery import startup_model_refresh

        async def _run_model_refresh():
            try:
                result = await startup_model_refresh()
                cat = result.get("catalog_update", {})
                pull = result.get("auto_pull", {})
                if cat.get("added"):
                    logger.info(
                        "Bot startup catalog: %d new model(s) discovered",
                        len(cat["added"]),
                    )
                if pull.get("pulled"):
                    logger.info(
                        "Bot startup auto-pull: %s",
                        ", ".join(pull["pulled"]),
                    )
            except Exception as exc:
                logger.warning("Bot startup model refresh failed: %s", exc)

        asyncio.create_task(_run_model_refresh())

        # Development-friendly behavior: sync to every guild the bot is currently in.
        # This makes new/changed commands (like /ask) show up immediately without waiting for global propagation.
        synced_total = 0
        for g in bot.guilds:
            target = discord.Object(id=g.id)
            bot.tree.copy_global_to(guild=target)
            synced = await bot.tree.sync(guild=target)
            synced_total += len(synced)
            logger.info(f'synced {len(synced)} commands to guild {g.id}.')

        if not bot.guilds:
            synced = await bot.tree.sync()
            logger.info(f'synced {len(synced)} commands globally (no guilds detected).')
        else:
            logger.info(f'synced commands across {len(bot.guilds)} guild(s); total returned: {synced_total}.')
    except Exception as e:
        logging.error(f"error loading bot: {e}")

@bot.event
async def on_connect():
    logger.info("Bot connected to Discord.")

@bot.event
async def on_disconnect():
    logger.info("Bot disconnected from Discord.")

@bot.event
async def on_resumed():
    logger.info("Bot has successfully resumed after a disconnection.")

# Track command usage for Steward self-monitoring
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
    """Handle slash command errors gracefully and log to Steward tracking."""
    # Log to Steward
    auto_ops = bot.get_cog("AutonomousOps")
    if auto_ops and hasattr(auto_ops, '_steward_log_command_usage'):
        await auto_ops._steward_log_command_usage(
            command_name=interaction.command.name if interaction.command else "unknown",
            cog_name=interaction.command.binding.__class__.__name__ if interaction.command and interaction.command.binding else "unknown",
            user_id=interaction.user.id,
            username=str(interaction.user),
            channel_id=interaction.channel_id,
            guild_id=interaction.guild_id if interaction.guild else 0,
            success=False,
            error_message=str(error)[:200]
        )
    
    # Send user-friendly error message
    error_msg = "❌ Something went wrong with that command."
    
    if isinstance(error, discord.app_commands.errors.CheckFailure):
        error_msg = "❌ You don't have permission to use this command."
    elif isinstance(error, discord.app_commands.errors.CommandOnCooldown):
        error_msg = f"⏳ Command on cooldown. Try again in {error.retry_after:.1f}s."
    elif isinstance(error, discord.app_commands.errors.MissingPermissions):
        error_msg = "❌ I don't have the permissions needed for that."
    
    try:
        if interaction.response.is_done():
            await interaction.followup.send(error_msg, ephemeral=True)
        else:
            await interaction.response.send_message(error_msg, ephemeral=True)
    except discord.errors.InteractionResponded:
        # Already responded, try followup
        try:
            await interaction.followup.send(error_msg, ephemeral=True)
        except Exception as e:
            logger.warning("operation: suppressed %s", e)
    except Exception as e:
        logger.error(f"Failed to send error message to user: {e}")
    
    # Log the actual error for debugging
    logger.error(f"App command error: {error}")

@bot.event
async def on_app_command_completion(interaction: discord.Interaction, command: discord.app_commands.Command):
    """Log successful commands to Steward tracking."""
    auto_ops = bot.get_cog("AutonomousOps")
    if auto_ops and hasattr(auto_ops, '_steward_log_command_usage'):
        await auto_ops._steward_log_command_usage(
            command_name=command.name,
            cog_name=command.binding.__class__.__name__ if command.binding else "unknown",
            user_id=interaction.user.id,
            username=str(interaction.user),
            channel_id=interaction.channel_id,
            guild_id=interaction.guild_id if interaction.guild else 0,
            success=True
        )

# Disabled global on_message to prevent double responses and duplicate logs.
# All message handling is now done in the LLM cog.
@bot.event
async def on_message(message):
    pass

logger.info("About to run bot.")
asyncio.run(main())
