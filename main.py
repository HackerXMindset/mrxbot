import os
import asyncio
import logging
import signal
import sys
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon import types
from typing import List, Set
from bot import UserBot, monitor_market_cap, monitor_messages, MARKET_CAP_THRESHOLDS
from db import init_db, get_db_connection, close_db
from utils import calculate_hitrate, format_value, format_percentage, format_time_diff, format_market_cap
from api import app as flask_app
from datetime import datetime, timezone
from aiolimiter import AsyncLimiter

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", 8091))  # Set for Koyeb
ALERT_CHANNEL = os.getenv("ALERT_CHANNEL", "@FcallD")

management_bot = TelegramClient('management_bot', API_ID, API_HASH)
target_users: Set[int] = set()
target_chats: Set[int] = set()
monitored_channels: Set[int] = set()
admins: Set[int] = {123456789}  # Replace with your admin ID
userbots: List[UserBot] = []
assignments: dict[int, str] = {}
channel_callers: dict[int, str] = {}
uptime_url = None
rate_limiter = AsyncLimiter(10, 1)  # 10 requests per second

async def check_admin(event):
    sender = await event.get_sender()
    return sender and sender.id in admins

async def handle_add_chat(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    args = message.text.split(maxsplit=1)
    if len(args) < 2: return await message.reply("Usage: /add_chat {number}")
    try:
        chat_id = int(args[1])
        target_chats.add(chat_id)
        await message.reply(f"Added chat {chat_id}")
    except ValueError:
        await message.reply("Invalid chat ID.")

async def handle_remove_chat(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    args = message.text.split(maxsplit=1)
    if len(args) < 2: return await message.reply("Usage: /remove_chat {number}")
    try:
        chat_id = int(args[1])
        target_chats.discard(chat_id)
        await message.reply(f"Removed chat {chat_id}")
    except ValueError:
        await message.reply("Invalid chat ID.")

async def handle_add_user(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    args = message.text.split(maxsplit=1)
    if len(args) < 2: return await message.reply("Usage: /add_user {number}")
    try:
        user_id = int(args[1])
        target_users.add(user_id)
        await message.reply(f"Added user {user_id}")
    except ValueError:
        await message.reply("Invalid user ID.")

async def handle_remove_user(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    args = message.text.split(maxsplit=1)
    if len(args) < 2: return await message.reply("Usage: /remove_user {number}")
    try:
        user_id = int(args[1])
        target_users.discard(user_id)
        await message.reply(f"Removed user {user_id}")
    except ValueError:
        await message.reply("Invalid user ID.")

async def handle_register_channel(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    args = message.text.split(maxsplit=1)
    if len(args) < 2: return await message.reply("Usage: /register_channel {chat_id}")
    try:
        chat_id = int(args[1])
        target_chats.add(chat_id)
        await message.reply(f"Registered channel {chat_id}")
    except ValueError:
        await message.reply("Invalid chat ID.")

async def handle_monitor_channel(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    args = message.text.split(maxsplit=1)
    if len(args) < 2: return await message.reply("Usage: /monitor_channel {chat_id}")
    try:
        chat_id = int(args[1])
        monitored_channels.add(chat_id)
        await message.reply(f"Monitoring channel {chat_id}")
    except ValueError:
        await message.reply("Invalid chat ID.")

async def handle_set_channel_caller(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    args = message.text.split(maxsplit=2)
    if len(args) < 3: return await message.reply("Usage: /set_channel_caller {chat_id} {name}")
    try:
        chat_id = int(args[1])
        name = args[2]
        channel_callers[chat_id] = name
        await message.reply(f"Set caller for {chat_id} to {name}")
    except ValueError:
        await message.reply("Invalid chat ID.")

async def handle_add_bot(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    args = message.text.split(maxsplit=4)
    if len(args) < 5: return await message.reply("Usage: /add_bot {api_id} {api_hash} {session_string} {name}")
    try:
        api_id, api_hash, session_string, name = int(args[1]), args[2], args[3], args[4]
        bot = UserBot(name, api_id, api_hash, session_string)
        await bot.start(target_chats, monitored_channels)
        userbots.append(bot)
        await message.reply(f"Added bot {name}")
    except ValueError:
        await message.reply("Invalid API ID.")

async def handle_list_targets(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    response = f"Target Users: {target_users}\nTarget Chats: {target_chats}\nMonitored Channels: {monitored_channels}"
    await message.reply(response)

async def handle_reload_bots(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    for bot in userbots:
        await bot.stop()
    userbots.clear()
    for i in range(1, 4):
        session = os.getenv(f"SESSION_{i}")
        if session:
            bot = UserBot(f"bot_{i}", API_ID, API_HASH, session)
            await bot.start(target_chats, monitored_channels)
            userbots.append(bot)
    await message.reply(f"Reloaded {len(userbots)} bots")

async def handle_assign_bot(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    args = message.text.split(maxsplit=2)
    if len(args) < 3: return await message.reply("Usage: /assign_bot {chat_id} {bot_name}")
    try:
        chat_id = int(args[1])
        bot_name = args[2]
        if any(b.name == bot_name for b in userbots):
            assignments[chat_id] = bot_name
            await message.reply(f"Assigned {bot_name} to {chat_id}")
        else:
            await message.reply(f"Bot {bot_name} not found.")
    except ValueError:
        await message.reply("Invalid chat ID.")

async def handle_unassign_bot(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    args = message.text.split(maxsplit=1)
    if len(args) < 2: return await message.reply("Usage: /unassign_bot {chat_id}")
    try:
        chat_id = int(args[1])
        assignments.pop(chat_id, None)
        await message.reply(f"Unassigned bot from {chat_id}")
    except ValueError:
        await message.reply("Invalid chat ID.")

async def handle_list_assignments(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    response = "\n".join(f"Chat {k}: {v}" for k, v in assignments.items()) or "No assignments."
    await message.reply(f"Assignments:\n{response}")

async def handle_add_keyword(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    args = message.text.split(maxsplit=2)
    if len(args) < 3: return await message.reply("Usage: /add_keyword {user_id} {keyword}")
    try:
        user_id = int(args[1])
        keyword = args[2]
        pool = await get_db_connection()
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO keywords (user_id, keyword) VALUES ($1, $2) ON CONFLICT DO NOTHING", user_id, keyword)
        await message.reply(f"Added keyword '{keyword}' for user {user_id}")
    except ValueError:
        await message.reply("Invalid user_id.")

async def handle_remove_keyword(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    args = message.text.split(maxsplit=2)
    if len(args) < 3: return await message.reply("Usage: /remove_keyword {user_id} {keyword}")
    try:
        user_id = int(args[1])
        keyword = args[2]
        pool = await get_db_connection()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM keywords WHERE user_id = $1 AND keyword = $2", user_id, keyword)
        await message.reply(f"Removed keyword '{keyword}' for user {user_id}")
    except ValueError:
        await message.reply("Invalid user_id.")

async def handle_list_keywords(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    pool = await get_db_connection()
    async with pool.acquire() as conn:
        keywords = await conn.fetch("SELECT user_id, keyword FROM keywords")
    response = "\n".join(f"User {k['user_id']}: {k['keyword']}" for k in keywords) or "No keywords."
    await message.reply(f"Keywords:\n{response}")

async def handle_add_admin(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    args = message.text.split(maxsplit=1)
    if len(args) < 2: return await message.reply("Usage: /add_admin {user_id}")
    try:
        user_id = int(args[1])
        admins.add(user_id)
        await message.reply(f"Added admin {user_id}")
    except ValueError:
        await message.reply("Invalid user ID.")

async def handle_list_configuration(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    response = f"Target Users: {len(target_users)}\nTarget Chats: {len(target_chats)}\nMonitored Channels: {len(monitored_channels)}\nBots: {len(userbots)}\nUptime URL: {uptime_url}"
    await message.reply(response)

async def handle_test(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    args = message.text.split(maxsplit=3)
    if len(args) < 3: return await message.reply("Usage: /test {contract_address} {market_cap|bonded|hypothetical}")
    address, test_type = args[1], args[2].lower()
    from bot import fetch_bonding_status
    from utils import fetch_market_cap

    if test_type == "market_cap":
        mc_str, mc_value, _, _ = await fetch_market_cap(address, datetime.now(timezone.utc))
        await message.reply(f"Market Cap for {address}: {mc_str}")
    elif test_type == "bonded":
        is_bonded, progress = await fetch_bonding_status(address)
        await message.reply(f"Bonding Status for {address}: {'Bonded' if is_bonded else 'Not Bonded'} ({progress}%)")
    elif test_type == "hypothetical" and len(args) == 4:
        try:
            hypo_mc = float(args[3].replace("m", "e6").replace("b", "e9"))
            mc_str = await format_market_cap(hypo_mc)
            pool = await get_db_connection()
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO alerts (address, message_id, initial_market_cap, chat_id, bot_name, timestamp, bonded) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (address) DO UPDATE SET initial_market_cap = $3",
                    address, message.id, hypo_mc / 2, message.chat_id, "test_bot", datetime.now(timezone.utc).isoformat(), False
                )
            await message.reply(f"Set {address} with hypothetical MC {mc_str} (initial {await format_market_cap(hypo_mc / 2)}).")
        except ValueError:
            await message.reply("Invalid value. Use e.g., 1m, 1b.")
    else:
        await message.reply("Invalid test type.")

async def handle_stats(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    args = message.text.split(maxsplit=1)
    if len(args) < 2: return await message.reply("Usage: /stats {user_id}")
    try:
        user_id = int(args[1])
        hitrate_5x, hitrate_2x, migration_rate, total_calls, successful_5x, total_unbonded, migrated = await calculate_hitrate(user_id)
        response = f"Stats for {user_id}:\n5x Hit Rate: {hitrate_5x:.0f}%\n2x Hit Rate: {hitrate_2x:.0f}%\nMigration Rate: {migration_rate:.0f}%\nTotal Calls: {total_calls}"
        await message.reply(response)
    except ValueError:
        await message.reply("Invalid user ID.")

async def handle_stats_history(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    args = message.text.split(maxsplit=1)
    if len(args) < 2: return await message.reply("Usage: /stats_history {user_id}")
    try:
        user_id = int(args[1])
        pool = await get_db_connection()
        async with pool.acquire() as conn:
            calls = await conn.fetch("SELECT address, timestamp, initial_market_cap FROM user_calls WHERE user_id = $1 ORDER BY timestamp DESC LIMIT 5", user_id)
        response = "\n".join(f"{c['address']} at {c['timestamp']}: {await format_market_cap(c['initial_market_cap'])}" for c in calls) or "No history."
        await message.reply(f"Recent calls for {user_id}:\n{response}")
    except ValueError:
        await message.reply("Invalid user ID.")

async def handle_set_uptime_url(message: types.Message):
    if not await check_admin(message): return await message.reply("Admins only.")
    args = message.text.split(maxsplit=1)
    if len(args) < 2: return await message.reply("Usage: /set_uptime_url {url}")
    global uptime_url
    uptime_url = args[1]
    pool = await get_db_connection()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO uptime_config (url, last_ping, status) VALUES ($1, $2, $3) "
            "ON CONFLICT (id) DO UPDATE SET url = $1, last_ping = $2, status = $3",
            uptime_url, None, "unknown"
        )
    await message.reply(f"Set uptime URL to {uptime_url}")

async def check_uptime():
    async with aiohttp.ClientSession() as session:
        while True:
            if uptime_url:
                async with rate_limiter:
                    try:
                        async with session.get(uptime_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                            status = "up" if response.status == 200 else "down"
                            last_ping = datetime.now(timezone.utc).isoformat()
                            pool = await get_db_connection()
                            async with pool.acquire() as conn:
                                await conn.execute(
                                    "INSERT INTO uptime_config (url, last_ping, status) VALUES ($1, $2, $3) "
                                    "ON CONFLICT (id) DO UPDATE SET last_ping = $2, status = $3",
                                    uptime_url, last_ping, status
                                )
                            logger.info(f"Uptime check: {status} ({response.status})")
                    except Exception as e:
                        pool = await get_db_connection()
                        async with pool.acquire() as conn:
                            await conn.execute(
                                "INSERT INTO uptime_config (url, last_ping, status) VALUES ($1, $2, $3) "
                                "ON CONFLICT (id) DO UPDATE SET last_ping = $2, status = $3",
                                uptime_url, datetime.now(timezone.utc).isoformat(), "error"
                            )
                        logger.error(f"Uptime check failed: {e}")
            await asyncio.sleep(300)

async def start_bot():
    await init_db()
    logger.info("Database initialized")

    await management_bot.start(bot_token=BOT_TOKEN)
    logger.info("Management bot started")

    await handle_reload_bots(None)

    management_bot.add_event_handler(lambda event: monitor_messages(event, userbots, target_users, target_chats), events.NewMessage())
    asyncio.create_task(monitor_market_cap(userbots))
    asyncio.create_task(check_uptime())
    logger.info("Started monitoring tasks")

async def shutdown():
    logger.info("Shutting down...")
    # Stop all userbots
    for bot in userbots:
        await bot.stop()
    # Disconnect the management bot
    if management_bot.is_connected():
        await management_bot.disconnect()
    # Close the database connection
    await close_db()
    logger.info("Shutdown complete")

def handle_shutdown(signum, frame):
    logger.info(f"Received signal {signum}, initiating shutdown...")
    loop = asyncio.get_event_loop()
    loop.create_task(shutdown())
    loop.stop()
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.close()
    sys.exit(0)

if __name__ == "__main__":
    handlers = {
        r'^/add_chat(?:\s+(.+))?$': handle_add_chat,
        r'^/remove_chat(?:\s+(.+))?$': handle_remove_chat,
        r'^/add_user(?:\s+(.+))?$': handle_add_user,
        r'^/remove_user(?:\s+(.+))?$': handle_remove_user,
        r'^/register_channel(?:\s+(.+))?$': handle_register_channel,
        r'^/monitor_channel(?:\s+(.+))?$': handle_monitor_channel,
        r'^/set_channel_caller(?:\s+(.+))?$': handle_set_channel_caller,
        r'^/add_bot(?:\s+(.+))?$': handle_add_bot,
        r'^/list_targets$': handle_list_targets,
        r'^/reload_bots$': handle_reload_bots,
        r'^/assign_bot(?:\s+(.+))?$': handle_assign_bot,
        r'^/unassign_bot(?:\s+(.+))?$': handle_unassign_bot,
        r'^/list_assignments$': handle_list_assignments,
        r'^/add_keyword(?:\s+(.+))?$': handle_add_keyword,
        r'^/remove_keyword(?:\s+(.+))?$': handle_remove_keyword,
        r'^/list_keywords$': handle_list_keywords,
        r'^/add_admin(?:\s+(.+))?$': handle_add_admin,
        r'^/list_configuration$': handle_list_configuration,
        r'^/test(?:\s+(.+))?$': handle_test,
        r'^/stats(?:\s+(.+))?$': handle_stats,
        r'^/stats_history(?:\s+(.+))?$': handle_stats_history,
        r'^/set_uptime_url(?:\s+(.+))?$': handle_set_uptime_url,
    }
    for pattern, handler in handlers.items():
        management_bot.on(events.NewMessage(pattern=pattern))(handler)

    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Start the Telegram bot in a separate task
    loop = asyncio.get_event_loop()
    loop.create_task(start_bot())

    # Set the Flask app
    app = flask_app

    # Fallback for local development: run Flask directly if not using gunicorn
    if os.getenv("FLASK_ENV") == "development":
        logger.info(f"Running Flask app in development mode on port {PORT}")
        app.run(host="0.0.0.0", port=PORT, debug=True)
    else:
        logger.info("Running in production mode (expecting gunicorn to start the app)")
