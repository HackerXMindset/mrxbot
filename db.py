import os
import asyncio
import asyncpg
import logging
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
logger = logging.getLogger(__name__)

_pool = None

async def init_db():
    global _pool
    try:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
        async with _pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS keywords (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    keyword TEXT NOT NULL,
                    UNIQUE (user_id, keyword)
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    address TEXT PRIMARY KEY,
                    message_id BIGINT NOT NULL,
                    initial_market_cap FLOAT NOT NULL,
                    chat_id BIGINT NOT NULL,
                    bot_name TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    bonded BOOLEAN DEFAULT FALSE,
                    token_name TEXT,
                    creation_time TEXT,
                    market_cap_increase_alerted BOOLEAN DEFAULT FALSE,
                    bonding_alerted BOOLEAN DEFAULT FALSE
                );
                CREATE TABLE IF NOT EXISTS user_calls (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    address TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    initial_market_cap FLOAT NOT NULL,
                    success BOOLEAN DEFAULT FALSE
                );
                CREATE TABLE IF NOT EXISTS uptime_config (
                    id SERIAL PRIMARY KEY,
                    url TEXT NOT NULL,
                    last_ping TEXT,
                    status TEXT
                );
            ''')
        logger.info("Database initialized")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise

async def get_db_connection():
    global _pool
    if _pool is None or _pool._closed:
        await init_db()
    return _pool

async def close_db():
    global _pool
    if _pool is not None and not _pool._closed:
        await _pool.close()
        _pool = None
        logger.info("Database connection pool closed")
