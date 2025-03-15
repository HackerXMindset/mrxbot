import re
from datetime import datetime
import aiohttp
from typing import Tuple, Optional

async def calculate_hitrate(user_id: int) -> tuple[float, float, float, int, int, int, int]:
    from db import get_db_connection
    pool = await get_db_connection()
    async with pool.acquire() as conn:
        calls = await conn.fetch("SELECT * FROM user_calls WHERE user_id = $1", user_id)
        total_calls = len(calls)
        if total_calls == 0:
            return 0.0, 0.0, 0.0, 0, 0, 0, 0
        successful_5x = sum(1 for call in calls if call["peak_market_cap"] >= call["initial_market_cap"] * 5)
        successful_2x = sum(1 for call in calls if call["peak_market_cap"] >= call["initial_market_cap"] * 2)
        total_unbonded = sum(1 for call in calls if not call["bonded"])
        migrated = sum(1 for call in calls if call["migrated"])
        hitrate_5x = (successful_5x / total_calls) * 100 if total_calls > 0 else 0
        hitrate_2x = (successful_2x / total_calls) * 100 if total_calls > 0 else 0
        migration_rate = (migrated / total_unbonded) * 100 if total_unbonded > 0 else 0
        return hitrate_5x, hitrate_2x, migration_rate, total_calls, successful_5x, total_unbonded, migrated

def format_value(value: float) -> str:
    return f"{value:,.0f}"

def format_percentage(value: float) -> str:
    return f"{value:.0f}%"

def format_time_diff(dt: datetime) -> str:
    now = datetime.now(dt.tzinfo)
    diff = now - dt
    if diff.days > 0:
        return f"{diff.days}d ago"
    if diff.seconds >= 3600:
        return f"{diff.seconds // 3600}h ago"
    if diff.seconds >= 60:
        return f"{diff.seconds // 60}m ago"
    return f"{diff.seconds}s ago"

def bonding_progress_bar(progress: float) -> str:
    filled = int(progress / 10)
    empty = 10 - filled
    return f"[{'█' * filled}{' ' * empty}] {progress}%"

async def format_market_cap(mc: float) -> str:
    if mc >= 1_000_000_000:
        return f"${mc / 1_000_000_000:.1f}B"
    elif mc >= 1_000_000:
        return f"${mc / 1_000_000:.1f}M"
    return f"${format_value(mc)}"

async def fetch_market_cap(address: str, timestamp: datetime) -> Tuple[str, float, Optional[datetime]]:
    async with aiohttp.ClientSession() as session:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
        async with session.get(url) as response:
            if response.status != 200:
                return "N/A", 0.0, None
            data = await response.json()
            if not data["pairs"]:
                return "N/A", 0.0, None
            pair = data["pairs"][0]
            market_cap = pair.get("fdv", 0.0)
            created_at = pair.get("pairCreatedAt")
            created_dt = datetime.fromtimestamp(created_at / 1000, tz=timestamp.tzinfo) if created_at else None
            return await format_market_cap(market_cap), market_cap, created_dt
