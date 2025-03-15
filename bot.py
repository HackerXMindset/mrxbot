import os
import re
import aiohttp
import logging
import html
from typing import Set, List, Tuple, Optional
from datetime import datetime, timezone
from aiolimiter import AsyncLimiter
from telethon import TelegramClient
from telethon.tl.types import Message
from telethon.sessions import StringSession
from db import get_db_connection, close_db

from utils import format_value, format_market_cap, format_percentage, format_time_diff, fetch_market_cap, bonding_progress_bar, calculate_hitrate, format_liquidity, format_volume, format_percentage_change

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

rate_limiter = AsyncLimiter(5, 1)  # 5 requests per second
MARKET_CAP_THRESHOLDS = [1_000_000, 2_000_000, 5_000_000, 10_000_000, 25_000_000, 50_000_000, 100_000_000, 250_000_000, 500_000_000, 1_000_000_000]

class UserBot:
    def __init__(self, name: str, api_id: int, api_hash: str, session_string: str):
        self.name = name
        self.client = TelegramClient(StringSession(session_string), api_id, api_hash)
        self.target_chats: Set[int] = set()
        self.monitored_channels: Set[int] = set()

    async def start(self, target_chats: Set[int], monitored_channels: Set[int]):
        self.target_chats = target_chats
        self.monitored_channels = monitored_channels
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise ValueError(f"Bot {self.name} is not authorized. Please check the session string.")
        await self.client.start()
        logger.info(f"Bot {self.name} started")

    async def stop(self):
        await self.client.disconnect()
        logger.info(f"Bot {self.name} stopped")

def escape_markdown(text: str) -> str:
    """Escape Markdown special characters."""
    return str(html.escape(text)).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")

async def fetch_bonding_status(address: str) -> Tuple[bool, float]:
    moralis_api_key = os.getenv("MORALIS_API_KEY")
    if not moralis_api_key:
        return False, 0.0
    headers = {"X-API-Key": moralis_api_key}
    async with aiohttp.ClientSession() as session:
        url = f"https://deep-index.moralis.io/api/v2/solana/token/{address}/status"
        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                return False, 0.0
            data = await response.json()
            bonded = data.get("bonded", False)
            progress = data.get("bondingProgress", 0.0)
            return bonded, progress

async def monitor_market_cap(bots: List[UserBot]):
    while True:
        pool = await get_db_connection()
        async with pool.acquire() as conn:
            alerts = await conn.fetch("SELECT * FROM alerts WHERE NOT closed")
            for alert in alerts:
                address = escape_markdown(alert["address"])
                initial_mc = alert["initial_market_cap"]
                chat_id = alert["chat_id"]
                message_id = alert["message_id"]
                bot_name = escape_markdown(alert["bot_name"])
                timestamp = datetime.fromisoformat(alert["timestamp"])
                bonded = alert["bonded"]
                mc_str, market_cap, _, token_stats = await fetch_market_cap(address, timestamp)
                if market_cap == 0.0:
                    continue
                is_bonded, progress = await fetch_bonding_status(address)
                if is_bonded and not bonded:
                    bonded = True
                    await conn.execute(
                        "UPDATE alerts SET bonded = $1 WHERE address = $2", True, address
                    )
                    for bot in bots:
                        if bot.name == bot_name:
                            # Fetch caller stats (using chat_id as a proxy for sender if needed)
                            hitrate_5x, hitrate_2x, migration_rate, total_calls, _, total_unbonded, migrated = await calculate_hitrate(chat_id)
                            # Construct the alert message
                            alert_message = (
                                f"💊*${token_stats.get('ticker', 'UNKNOWN')} | {token_stats.get('name', 'Unknown Token')}*\n"
                                f"├ `{address}`\n\n"
                                f"🤙*Caller Stats: {bot_name}*\n"
                                f"├ Hit rate: 5x: {hitrate_5x:.0f}%  | 2x: {hitrate_2x:.0f}%\n"
                                f"└ Migration rate: {migration_rate:.0f}% ({migrated} out of {total_unbonded})\n\n"
                                f"📊 *Token Stats*\n"
                                f"├ MC: ${await format_market_cap(market_cap)} | {format_percentage_change(market_cap, token_stats.get('market_cap_6h_ago', market_cap))} 𝝙\n"
                                f"├ LP: ${format_liquidity(token_stats.get('liquidity', 0.0))}\n"
                                f"├ VOL: ${format_volume(token_stats.get('volume_6h', 0.0))}\n"
                                f"├ Buys: {token_stats.get('buys_5h', 0)} | Sells: {token_stats.get('sells_5h', 0)}\n"
                                f"└ DEX: {token_stats.get('dex', 'Unknown DEX')}\n\n"
                            )
                            if not is_bonded:
                                alert_message += (
                                    f"🏦 *Bond Stats:*\n"
                                    f"└ {await bonding_progress_bar(progress)}\n\n"
                                )
                            alert_message += f"💬 *Check Comments For More Details - @FcallD*"
                            await bot.client.send_message(os.getenv("ALERT_CHANNEL", "@FcallD"), alert_message, parse_mode="Markdown")
                for threshold in MARKET_CAP_THRESHOLDS:
                    if initial_mc < threshold <= market_cap:
                        for bot in bots:
                            if bot.name == bot_name:
                                # Fetch caller stats
                                hitrate_5x, hitrate_2x, migration_rate, total_calls, _, total_unbonded, migrated = await calculate_hitrate(chat_id)
                                # Construct the alert message
                                alert_message = (
                                    f"💊*${token_stats.get('ticker', 'UNKNOWN')} | {token_stats.get('name', 'Unknown Token')}*\n"
                                    f"├ `{address}`\n\n"
                                    f"🤙*Caller Stats - {bot_name}*\n"
                                    f"├ Hit rate: 5x: {hitrate_5x:.0f}%  | 2x: {hitrate_2x:.0f}%\n"
                                    f"└ Migration rate: {migration_rate:.0f}% ({migrated} out of {total_unbonded})\n\n"
                                    f"📊 *Token Stats*\n"
                                    f"├ MC: ${await format_market_cap(market_cap)} | {format_percentage_change(market_cap, token_stats.get('market_cap_6h_ago', market_cap))} 𝝙\n"
                                    f"├ LP: ${format_liquidity(token_stats.get('liquidity', 0.0))}\n"
                                    f"├ VOL: ${format_volume(token_stats.get('volume_6h', 0.0))}\n"
                                    f"├ Buys: {token_stats.get('buys_5h', 0)} | Sells: {token_stats.get('sells_5h', 0)}\n"
                                    f"└ DEX: {token_stats.get('dex', 'Unknown DEX')}\n\n"
                                )
                                if not is_bonded:
                                    alert_message += (
                                        f"🏦 *Bond Stats:*\n"
                                        f"└ {await bonding_progress_bar(progress)}\n\n"
                                    )
                                alert_message += f"💬 *Check Comments For More Details - @FcallD*"
                                await bot.client.send_message(os.getenv("ALERT_CHANNEL", "@FcallD"), alert_message, parse_mode="Markdown")
        await asyncio.sleep(300)

async def monitor_messages(event: Message, bots: List[UserBot], target_users: Set[int], target_chats: Set[int]):
    message = event.message
    if not message.text:
        return
    sender = await event.get_sender()
    if not sender:
        return
    chat_id = event.chat_id
    sender_id = sender.id
    if sender_id not in target_users and chat_id not in target_chats:
        return
    solana_address_pattern = r"[1-9A-HJ-NP-Za-km-z]{32,44}"
    addresses = re.findall(solana_address_pattern, message.text)
    if not addresses:
        return
    pool = await get_db_connection()
    for address in addresses:
        async with pool.acquire() as conn:
            existing = await conn.fetchrow("SELECT * FROM alerts WHERE address = $1 AND NOT closed", address)
            if existing:
                continue
            mc_str, market_cap, _, token_stats = await fetch_market_cap(address, datetime.now(timezone.utc))
            if market_cap == 0.0:
                continue
            is_bonded, progress = await fetch_bonding_status(address)
            # Use sender's name or channel caller
            sender_name = f"{sender.first_name or ''} {sender.last_name or ''}".strip()
            if not sender_name and chat_id in channel_callers:
                sender_name = channel_callers[chat_id]
            if not sender_name:
                sender_name = "Unknown Caller"
            sender_name = escape_markdown(sender_name)
            bot_name = escape_markdown(next((bot.name for bot in bots if bot.name in message.text.lower()), bots[0].name if bots else "unknown"))
            await conn.execute(
                "INSERT INTO alerts (address, message_id, initial_market_cap, chat_id, bot_name, timestamp, bonded) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (address) DO NOTHING",
                address, message.id, market_cap, chat_id, bot_name, datetime.now(timezone.utc).isoformat(), is_bonded
            )
            await conn.execute(
                "INSERT INTO user_calls (user_id, address, initial_market_cap, timestamp, bonded) "
                "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (user_id, address) DO NOTHING",
                sender_id, address, market_cap, datetime.now(timezone.utc).isoformat(), is_bonded
            )
            for bot in bots:
                if bot.name == bot_name:
                    # Fetch caller stats
                    hitrate_5x, hitrate_2x, migration_rate, total_calls, _, total_unbonded, migrated = await calculate_hitrate(sender_id)
                    # Construct the alert message
                    alert_message = (
                        f"💊*${token_stats.get('ticker', 'UNKNOWN')} | {token_stats.get('name', 'Unknown Token')}*\n"
                        f"├ `{address}`\n\n"
                        f"🤙Caller Stats: *{sender_name}*\n"
                        f"├ Hit rate: *5x: {hitrate_5x:.0f}%  | 2x: {hitrate_2x:.0f}%*\n"
                        f"└ Migration rate: *{migration_rate:.0f}%* ({migrated} out of {total_unbonded})\n\n"
                        f"📊 *Token Stats*\n"
                        f"├ `MC:` *${await format_market_cap(market_cap)}* | *{format_percentage_change(market_cap, token_stats.get('market_cap_6h_ago', market_cap))}* 𝝙\n"
                        f"├ `LP:` *${format_liquidity(token_stats.get('liquidity', 0.0))}*\n"
                        f"├ `VOL:` *${format_volume(token_stats.get('volume_6h', 0.0))}*\n"
                        f"├ `Buys:` *{token_stats.get('buys_5h', 0)}* | *Sells: {token_stats.get('sells_5h', 0)}*\n"
                        f"└ `DEX:` *{token_stats.get('dex', 'Unknown DEX')}*\n\n"
                    )
                    if not is_bonded:
                        alert_message += (
                            f"🏦 *Bond Stats:*\n"
                            f"└ {await bonding_progress_bar(progress)}\n\n"
                        )
                    alert_message += f"💬 *Check Comments For More Details - @FcallD*"
                    await bot.client.send_message(os.getenv("ALERT_CHANNEL", "@FcallD"), alert_message, parse_mode="Markdown")
