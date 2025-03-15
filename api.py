import logging
from flask import Flask, jsonify
from db import get_db_connection
from utils import calculate_hitrate, format_market_cap
from datetime import datetime, timezone
from aiolimiter import AsyncLimiter

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
rate_limiter = AsyncLimiter(10, 1)  # 10 requests per second

@app.route('/')
async def home():
    async with rate_limiter:
        logger.info("Request to / endpoint")
        return jsonify({
            "status": "success",
            "message": "Welcome to Solana Monitor Bot API!",
            "endpoints": {
                "/alerts": "Get recent token alerts",
                "/stats/<int:user_id>": "Get user statistics",
                "/uptime": "Get uptime status"
            }
        })

@app.route('/alerts')
async def get_alerts():
    async with rate_limiter:
        logger.info("Request to /alerts endpoint")
        try:
            pool = await get_db_connection()
            async with pool.acquire() as conn:
                alerts = await conn.fetch(
                    "SELECT address, initial_market_cap, timestamp, token_name, bot_name, bonded "
                    "FROM alerts ORDER BY timestamp DESC LIMIT 10"
                )
                formatted_alerts = [
                    {
                        "address": a["address"],
                        "initial_market_cap": await format_market_cap(a["initial_market_cap"]),
                        "timestamp": a["timestamp"],
                        "token_name": a["token_name"],
                        "bot_name": a["bot_name"],
                        "bonded": a["bonded"]
                    } for a in alerts
                ]
                return jsonify({
                    "status": "success",
                    "alerts": formatted_alerts
                })
        except Exception as e:
            logger.error(f"Error fetching alerts: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/stats/<int:user_id>')
async def get_user_stats(user_id):
    async with rate_limiter:
        logger.info(f"Request to /stats/{user_id} endpoint")
        try:
            stats = await calculate_hitrate(user_id)
            return jsonify({
                "status": "success",
                "stats": {
                    "user_id": user_id,
                    "hitrate_5x": stats[0],
                    "hitrate_2x": stats[1],
                    "migration_rate": stats[2],
                    "total_calls": stats[3],
                    "successful_5x": stats[4],
                    "total_unbonded": stats[5],
                    "migrated": stats[6]
                }
            })
        except Exception as e:
            logger.error(f"Error fetching stats for user {user_id}: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/uptime')
async def get_uptime():
    async with rate_limiter:
        logger.info("Request to /uptime endpoint")
        try:
            pool = await get_db_connection()
            async with pool.acquire() as conn:
                result = await conn.fetchrow("SELECT url, last_ping, status FROM uptime_config LIMIT 1")
                if result:
                    return jsonify({
                        "status": "success",
                        "uptime": {
                            "url": result["url"],
                            "last_ping": result["last_ping"],
                            "status": result["status"]
                        }
                    })
                return jsonify({"status": "error", "message": "No uptime URL configured"})
        except Exception as e:
            logger.error(f"Error fetching uptime: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500
