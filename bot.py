import os
import asyncio
import logging
import aiohttp
import re
from telethon import TelegramClient, events
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.sessions import StringSession
from typing import List, Set
from datetime import datetime, timedelta, timezone
from db import get_db_connection
from utils import calculate_hitrate, format_value, format_percentage, format_time_diff, bonding_progress_bar
from aiolimiter import AsyncLimiter

logger = logging.getLogger(__name__)

ALERT_CHANNEL = os.getenv("ALERT_CHANNEL", "@FcallD")
MORALIS_API_KEY = os.getenv("MORALIS_API_KEY")
MARKET_CAP_BONDING_THRESHOLD = 71000
ADDRESS_PATTERNS = {"Solana": (r"^[1-9A-HJ-NP-Za-km-z]{32,44}$", "solana")}
rate_limiter = AsyncLimiter(5, 1)  # 5 requests per second for external APIs

NEW_TOKEN_TEMPLATE = """💊 **${TICKER} | {NAME_OF_TOKEN}**
├ `{contractaddressoftokenpump}`\n\n
🤙Caller Stats - {first_name_last_name}**
├Hit rate: 5x: **{five_x_hitrate:.0f}%**  |  2x: **{two_x_hitrate:.0f}%**
└Migration rate:**{migration_rate:.0f}%** (**{migrated}** out of **{total_unbonded}**)\n\n
**📊 Token Stats**
├`MC:` **{market_cap}** | **{mc_change}** 𝝙
├`LP:` **{liquidity_usd}**
├`VOL:` **{volume}**
├ Buys: **{buys}** |Sells: **{sells}**
└`DEX:` {dex}\n\n
{bonding_status}\n\n
💬 **Check Comments For More Details** - {alert_channel}
"""

MARKET_CAP_INCREASE_TEMPLATE = "{emoji} {multiplier:.1f}x | 💹From {initial_mc} ↗️ {new_mc} within {time_diff}"
BONDING_TEMPLATE = "Token has been bonded, achieved within {time_diff}"
KEYWORD_ALERT_TEMPLATE = "⚠️ **Keyword Alert!** ⚠️\n**Keyword:** {what_keyword_was_detected}\n**From:** {first_name_of_the_user}\n**Chat:** {name_of_group_chat}"

PHASE_1_DURATION, PHASE_1_INTERVAL = 5 * 3600, 3 * 60
PHASE_2_DURATION, PHASE_2_INTERVAL = 12 * 3600, 10 * 60
PHASE_3_DURATION, PHASE_3_INTERVAL = 7 * 24 * 3600, 3600
PHASE_4_DURATION, PHASE_4_INTERVAL = 14 * 24 * 3600, 6 * 3600
PHASE_5_DURATION, PHASE_5_INTERVAL = 14 * 24 * 3600, 12 * 3600
TOTAL_DURATION = sum([PHASE_1_DURATION, PHASE_2_DURATION, PHASE_3_DURATION, PHASE_4_DURATION, PHASE_5_DURATION])

MARKET_CAP_THRESHOLDS = [(100.0, "🌙"), (10.0, "🚀"), (7.0, "🌕"), (3.0, "🔥"), (1.0, "🎉")]

async def format_market_cap(mc: float) -> str:
    if mc >= 1_000_000_000:
        return f"${mc / 1_000_000_000:.1f}B"
    elif mc >= 1_000_000:
        return f"${mc / 1_000_000:.1f}M"
    return f"${format_value(mc)}"

class UserBot:
    def __init__(self, name: str, api_id: int, api_hash: str, session_string: str):
        self.name = name
        self.client = TelegramClient(StringSession(session_string), api_id, api_hash)
        self.running = False
        self.assigned_chats = set()
        self.monitored_channels = set()

    async def start(self, target_chats: Set[int], monitored_channels: Set[int]):
        try:
            await self.client.start()
            self.running = True
            for chat_id in target_chats | monitored_channels | {ALERT_CHANNEL}:
                try:
                    await self.client(JoinChannelRequest(chat_id))
                    logger.info(f"Userbot {self.name} joined {chat_id}")
                except Exception as e:
                    logger.error(f"Userbot {self.name} failed to join {chat_id}: {e}")
            self.assigned_chats = target_chats
            self.monitored_channels = monitored_channels
        except Exception as e:
            logger.error(f"Failed to start UserBot {self.name}: {e}")
            self.running = False

    async def stop(self):
        try:
            if self.client.is_connected():
                await self.client.disconnect()
            self.running = False
            logger.info(f"UserBot {self.name} stopped")
        except Exception as e:
            logger.error(f"Error stopping UserBot {self.name}: {e}")

    async def send_message(self, message, target=None):
        target = target or ALERT_CHANNEL
        retries = 3
        for attempt in range(retries):
            try:
                msg = await self.client.send_message(target, message, parse_mode="Markdown")
                logger.info(f"Userbot {self.name} sent message to {target}")
                return msg
            except Exception as e:
                logger.error(f"Userbot {self.name} failed to send message to {target} (Attempt {attempt + 1}): {e}")
                if attempt == retries - 1:
                    return None
                await asyncio.sleep(2)

    async def send_reply(self, message, reply_to_msg_id):
        retries = 3
        for attempt in range(retries):
            try:
                await self.client.send_message(ALERT_CHANNEL, message, reply_to=reply_to_msg_id, parse_mode="Markdown")
                logger.info(f"Userbot {self.name} sent reply to {reply_to_msg_id}")
                return
            except Exception as e:
                logger.error(f"Userbot {self.name} failed to send reply to {reply_to_msg_id} (Attempt {attempt + 1}): {e}")
                if attempt == retries - 1:
                    return
                await asyncio.sleep(2)

async def fetch_market_cap(address: str, event_time: datetime, network: str = "solana") -> tuple:
    if address.startswith("TEST_"):
        mc_value = 100_000 if address == "TEST_1" else 42_300
        usd_price = 0.0001 if address == "TEST_1" else 0.0000423
        return (
            await format_market_cap(mc_value), mc_value, f"${format_value(usd_price)}",
            f"TestToken_{address}", f"TEST{address.split('_')[1]}",
            event_time - timedelta(minutes=5), 0.0, 0, 0, 0, "unknown", 0
        )
    pattern = ADDRESS_PATTERNS["Solana"][0]
    if not re.match(pattern, address):
        logger.warning(f"Invalid Solana address: {address}")
        return "$0.00", 0.0, "$0.0000000", "Unknown Token", "TOKEN", event_time - timedelta(hours=1), 0.0, 0, 0, 0, "unknown", 0
    url = f"https://api.dexscreener.com/token-pairs/v1/{network}/{address}"
    async with aiohttp.ClientSession() as session:
        retries = 3
        for attempt in range(retries):
            async with rate_limiter:
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                        data = (await response.json())[0]
                        usd_price = float(data.get("priceUsd", 0.0))
                        market_cap = float(data.get("marketCap", data.get("fdv", 0.0)))
                        token_name = data["baseToken"]["name"]
                        token_symbol = data["baseToken"]["symbol"]
                        creation_time = datetime.fromtimestamp(data["pairCreatedAt"] / 1000, tz=timezone.utc) if "pairCreatedAt" in data else event_time - timedelta(hours=1)
                        price_change_h6 = data["priceChange"].get("h6", 0.0) * 100 if "priceChange" in data else 0.0
                        volume_h6 = data["volume"].get("h6", 0) if "volume" in data else 0
                        buys_h6 = data["txns"].get("h6", {}).get("buys", 0) if "txns" in data else 0
                        sells_h6 = data["txns"].get("h6", {}).get("sells", 0) if "txns" in data else 0
                        dex_name = data["dexId"].capitalize()
                        liquidity_usd = data["liquidity"].get("usd", 0) if "liquidity" in data else 0
                        return (
                            await format_market_cap(market_cap), market_cap, f"${format_value(usd_price)}",
                            token_name, token_symbol, creation_time, price_change_h6, volume_h6,
                            buys_h6, sells_h6, dex_name, liquidity_usd
                        )
                except Exception as e:
                    logger.error(f"Failed to fetch market cap for {address} (Attempt {attempt + 1}): {e}")
                    if attempt == retries - 1:
                        return "$0.00", 0.0, "$0.0000000", "Unknown Token", "TOKEN", event_time - timedelta(hours=1), 0.0, 0, 0, 0, "unknown", 0
                    await asyncio.sleep(2)

async def fetch_bonding_status(address: str) -> tuple:
    if address.startswith("TEST_BOND_"):
        progress = {"TEST_BOND_1": 100.0}.get(address, 0.0)
        return progress >= 100.0, progress
    if not MORALIS_API_KEY:
        logger.error("MORALIS_API_KEY not set")
        return False, 0.0
    headers = {"Accept": "application/json", "X-API-Key": MORALIS_API_KEY}
    url = f"https://solana-gateway.moralis.io/token/mainnet/{address}/bonding-status"
    async with aiohttp.ClientSession() as session:
        retries = 3
        for attempt in range(retries):
            async with rate_limiter:
                try:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                        data = await response.json()
                        bonding_progress = float(data.get("bondingProgress", 0.0))
                        return bonding_progress >= 100.0, bonding_progress
                except Exception as e:
                    logger.error(f"Failed to fetch bonding status for {address} (Attempt {attempt + 1}): {e}")
                    if attempt == retries - 1:
                        return False, 0.0
                    await asyncio.sleep(2)

async def monitor_market_cap(userbots: List['UserBot']):
    while True:
        try:
            pool = await get_db_connection()
            async with pool.acquire() as conn:
                alerts = await conn.fetch(
                    "SELECT address, message_id, initial_market_cap, bot_name, bonded, timestamp, "
                    "market_cap_increase_alerted, bonding_alerted "
                    "FROM alerts"
                )
                for address, message_id, initial_mc, bot_name, bonded, timestamp, mc_alerted, bond_alerted in alerts:
                    if initial_mc <= 0:
                        continue
                    alert_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    time_since_alert = (datetime.now(timezone.utc) - alert_time).total_seconds()
                    sleep_interval = PHASE_1_INTERVAL
                    if time_since_alert > TOTAL_DURATION:
                        await conn.execute("DELETE FROM alerts WHERE address = $1", address)
                        logger.info(f"Deleted expired alert for {address}")
                        continue
                    elif time_since_alert <= PHASE_1_DURATION:
                        sleep_interval = PHASE_1_INTERVAL
                    elif time_since_alert <= PHASE_1_DURATION + PHASE_2_DURATION:
                        sleep_interval = PHASE_2_INTERVAL
                    elif time_since_alert <= PHASE_1_DURATION + PHASE_2_DURATION + PHASE_3_DURATION:
                        sleep_interval = PHASE_3_INTERVAL
                    elif time_since_alert <= PHASE_1_DURATION + PHASE_2_DURATION + PHASE_3_DURATION + PHASE_4_DURATION:
                        sleep_interval = PHASE_4_INTERVAL
                    else:
                        sleep_interval = PHASE_5_INTERVAL

                    mc_str, current_mc, _, _, _, _, _, _, _, _, _, _ = await fetch_market_cap(address, datetime.now(timezone.utc))
                    bot = next((b for b in userbots if b.name == bot_name), userbots[0] if userbots else None)
                    if not bot or not bot.running:
                        logger.warning(f"No active bot found for {address}")
                        continue

                    if current_mc >= initial_mc * 2 and not mc_alerted:
                        multiplier = current_mc / initial_mc
                        emoji = next((e for t, e in MARKET_CAP_THRESHOLDS if multiplier >= t), "🎉")
                        time_diff = format_time_diff(alert_time, datetime.now(timezone.utc))
                        template = MARKET_CAP_INCREASE_TEMPLATE.format(
                            emoji=emoji, multiplier=multiplier, initial_mc=await format_market_cap(initial_mc),
                            new_mc=await format_market_cap(current_mc), time_diff=time_diff
                        )
                        await bot.send_reply(template, message_id)
                        await conn.execute(
                            "UPDATE alerts SET market_cap_increase_alerted = TRUE, timestamp = $1 WHERE address = $2",
                            datetime.now(timezone.utc).isoformat(), address
                        )
                        logger.info(f"Sent market cap increase alert for {address}: {template}")

                    if not bonded and not bond_alerted:
                        is_bonded, _ = await fetch_bonding_status(address)
                        if is_bonded:
                            time_diff = format_time_diff(alert_time, datetime.now(timezone.utc))
                            template = BONDING_TEMPLATE.format(time_diff=time_diff)
                            await bot.send_reply(template, message_id)
                            await conn.execute(
                                "UPDATE alerts SET bonded = TRUE, bonding_alerted = TRUE WHERE address = $1",
                                address
                            )
                            logger.info(f"Sent bonding alert for {address}: {template}")

            await asyncio.sleep(sleep_interval)
        except Exception as e:
            logger.error(f"Error in monitor_market_cap: {e}")
            await asyncio.sleep(60)

async def monitor_messages(event, userbots: List['UserBot'], target_users: Set[int], target_chats: Set[int]):
    if not any(bot.running and event.chat_id in bot.assigned_chats for bot in userbots):
        return
    sender = await event.get_sender()
    if not sender or not event.message.text:
        return
    sender_id = sender.id
    chat = await event.get_chat()
    chat_title = getattr(chat, 'title', 'Unknown Chat')
    if sender_id not in target_users and event.chat_id not in target_chats:
        return

    pool = await get_db_connection()
    async with pool.acquire() as conn:
        tokens = event.message.text.split()
        found_addresses = {token for token in tokens if re.match(ADDRESS_PATTERNS["Solana"][0], token)}
        if found_addresses:
            logger.info(f"Detected addresses: {found_addresses}")

        keywords = await conn.fetch("SELECT keyword FROM keywords WHERE user_id = $1", sender_id)
        keyword_matches = [kw["keyword"] for kw in keywords if kw["keyword"].lower() in event.message.text.lower()]
        for keyword in keyword_matches:
            alert = KEYWORD_ALERT_TEMPLATE.format(
                what_keyword_was_detected=keyword,
                first_name_of_the_user=f"{sender.first_name or 'Unknown'} {sender.last_name or ''}".strip(),
                name_of_group_chat=chat_title
            )
            bot = userbots[0] if userbots else None
            if bot and bot.running:
                await bot.send_message(alert)
                logger.info(f"Sent keyword alert for '{keyword}' from {sender_id} in {chat_title}")

        for address in found_addresses:
            mc_str, mc_value, _, token_name, token_symbol, creation_time, price_change_h6, volume_h6, buys_h6, sells_h6, dex_name, liquidity_usd = await fetch_market_cap(address, event.date)
            is_bonded, progress = await fetch_bonding_status(address)
            sender_name = channel_callers.get(event.chat_id, f"{sender.first_name or 'Unknown'} {sender.last_name or ''}".strip())
            hitrate_5x, hitrate_2x, migration_rate, total_calls, successful_5x, total_unbonded, migrated = await calculate_hitrate(sender_id)
            bonding_status = "" if is_bonded else f"🏦 **Bond Stats:**\n└ {bonding_progress_bar(progress)}" if mc_value <= MARKET_CAP_BONDING_THRESHOLD else ""
            alert_message = NEW_TOKEN_TEMPLATE.format(
                TICKER=token_symbol, NAME_OF_TOKEN=token_name, contractaddressoftokenpump=address,
                first_name_last_name=sender_name, five_x_hitrate=hitrate_5x, two_x_hitrate=hitrate_2x,
                migration_rate=migration_rate, migrated=migrated, total_unbonded=total_unbonded,
                market_cap=mc_str, mc_change=format_percentage(price_change_h6), liquidity_usd=f"${format_value(liquidity_usd)}",
                volume=f"${format_value(volume_h6)}", buys=buys_h6, sells=sells_h6, dex=dex_name,
                bonding_status=bonding_status, alert_channel=ALERT_CHANNEL
            )
            bot = next((b for b in userbots if b.running and b.name == assignments.get(event.chat_id)), userbots[0] if userbots else None)
            if not bot or not bot.running:
                logger.warning(f"No active bot available for {address}")
                continue
            message = await bot.send_message(alert_message)
            if message:
                await conn.execute(
                    "INSERT INTO alerts (address, message_id, initial_market_cap, chat_id, bot_name, timestamp, bonded, token_name, creation_time) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) ON CONFLICT (address) DO UPDATE SET message_id = $2, timestamp = $6, bonded = $7",
                    address, message.id, mc_value, event.chat_id, bot.name, datetime.now(timezone.utc).isoformat(), is_bonded, token_name, creation_time.isoformat()
                )
                await conn.execute(
                    "INSERT INTO user_calls (user_id, address, timestamp, initial_market_cap) "
                    "VALUES ($1, $2, $3, $4)",
                    sender_id, address, datetime.now(timezone.utc).isoformat(), mc_value
                )
                logger.info(f"Sent new token alert for {address} by {sender_name}")
