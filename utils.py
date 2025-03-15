import logging
from datetime import datetime, timezone, timedelta
from db import get_db_connection
from bot import fetch_market_cap

logger = logging.getLogger(__name__)

async def calculate_hitrate(user_id: int) -> tuple:
    try:
        pool = await get_db_connection()
        async with pool.acquire() as conn:
            thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            calls = await conn.fetch(
                "SELECT initial_market_cap, address FROM user_calls WHERE user_id = $1 AND timestamp >= $2",
                user_id, thirty_days_ago
            )
            total_calls = len(calls)
            if total_calls < 5:
                return 0.0, 0.0, 0.0, total_calls, 0, 0, 0

            successful_5x = 0
            successful_2x = 0
            total_unbonded = 0
            migrated = 0

            for call in calls:
                initial_mc = call["initial_market_cap"]
                address = call["address"]
                alert = await conn.fetchrow(
                    "SELECT initial_market_cap, bonded FROM alerts WHERE address = $1",
                    address
                )
                if not alert:
                    continue
                current_mc = await get_current_market_cap(address)
                if current_mc >= initial_mc * 5:
                    successful_5x += 1
                    successful_2x += 1
                elif current_mc >= initial_mc * 2:
                    successful_2x += 1
                if not alert["bonded"]:
                    total_unbonded += 1
                    if current_mc >= MARKET_CAP_BONDING_THRESHOLD:
                        migrated += 1

            hitrate_5x = (successful_5x / total_calls) * 100
            hitrate_2x = (successful_2x / total_calls) * 100
            migration_rate = (migrated / total_unbonded * 100) if total_unbonded > 0 else 0.0

            return hitrate_5x, hitrate_2x, migration_rate, total_calls, successful_5x, total_unbonded, migrated
    except Exception as e:
        logger.error(f"Error calculating hitrate for user {user_id}: {e}")
        return 0.0, 0.0, 0.0, 0, 0, 0, 0

async def get_current_market_cap(address: str) -> float:
    _, current_mc, _, _, _, _, _, _, _, _, _, _ = await fetch_market_cap(address, datetime.now(timezone.utc))
    return current_mc

async def format_market_cap(mc: float) -> str:
    if mc >= 1_000_000_000:
        return f"${mc / 1_000_000_000:.1f}B"
    elif mc >= 1_000_000:
        return f"${mc / 1_000_000:.1f}M"
    return f"${format_value(mc)}"

def format_value(value: float) -> str:
    try:
        if value >= 1000:
            return "{:,.0f}".format(value)
        elif value >= 1:
            return "{:,.2f}".format(value)
        else:
            return "{:.7f}".format(value).rstrip('0').rstrip('.')
    except Exception as e:
        logger.error(f"Error formatting value {value}: {e}")
        return str(value)

def format_percentage(value: float) -> str:
    try:
        return f"{value:+.1f}%"
    except Exception as e:
        logger.error(f"Error formatting percentage {value}: {e}")
        return f"{value}%"

def format_time_diff(start: datetime, end: datetime) -> str:
    try:
        diff = (end - start).total_seconds()
        if diff >= 86400:
            return f"{int(diff // 86400)}d"
        elif diff >= 3600:
            return f"{int(diff // 3600)}h"
        elif diff >= 60:
            return f"{int(diff // 60)}m"
        else:
            return f"{int(diff)}s"
    except Exception as e:
        logger.error(f"Error formatting time diff {start} to {end}: {e}")
        return "unknown"

def bonding_progress_bar(progress: float) -> str:
    try:
        bar_length = 10
        filled = int(progress / 100 * bar_length)
        return f"[{'█' * filled}{'░' * (bar_length - filled)}] {progress:.1f}%"
    except Exception as e:
        logger.error(f"Error generating bonding progress bar for {progress}: {e}")
        return f"{progress:.1f}%"

MARKET_CAP_BONDING_THRESHOLD = 71000
