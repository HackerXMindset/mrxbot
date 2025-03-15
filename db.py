import os
import asyncpg
from typing import Optional

_pool: Optional[asyncpg.Pool] = None

async def init_db():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=os.getenv("DATABASE_URL"),
            min_size=1,
            max_size=10
        )
        async with _pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS alerts (
                    address TEXT PRIMARY KEY,
                    message_id BIGINT,
                    initial_market_cap DOUBLE PRECISION,
                    chat_id BIGINT,
                    bot_name TEXT,
                    timestamp TEXT,
                    bonded BOOLEAN DEFAULT FALSE,
                    closed BOOLEAN DEFAULT FALSE
                )
            ''')
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS user_calls (
                    user_id BIGINT,
                    address TEXT,
                    initial_market_cap DOUBLE PRECISION,
                    timestamp TEXT,
                    bonded BOOLEAN DEFAULT FALSE,
                    peak_market_cap DOUBLE PRECISION DEFAULT 0,
                    migrated BOOLEAN DEFAULT FALSE,
                    PRIMARY KEY (user_id, address)
                )
            ''')
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS keywords (
                    user_id BIGINT,
                    keyword TEXT,
                    PRIMARY KEY (user_id, keyword)
                )
            ''')
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS uptime_config (
                    id SERIAL PRIMARY KEY,
                    url TEXT,
                    last_ping TEXT,
                    status TEXT
                )
            ''')
            logger.info("Database schema initialized")

async def get_db_connection():
    global _pool
    if _pool is None:
        await init_db()
    return _pool

async def close_db():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
