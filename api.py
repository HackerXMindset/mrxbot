import os
from datetime import datetime, timezone
from flask import Flask, jsonify
from aiolimiter import AsyncLimiter
from db import get_db_connection
from utils import calculate_hitrate, format_value, format_percentage, format_time_diff, format_market_cap, format_liquidity, format_volume

app = Flask(__name__)
rate_limiter = AsyncLimiter(5, 1)  # 5 requests per second

@app.route('/')
async def index():
    return jsonify({
        "message": "Welcome to Spymrx API",
        "endpoints": [
            {"path": "/alerts", "method": "GET", "description": "Get recent token alerts"},
            {"path": "/stats/<user_id>", "method": "GET", "description": "Get user statistics"},
            {"path": "/uptime", "method": "GET", "description": "Get uptime status"},
            {"path": "/health", "method": "GET", "description": "Health check endpoint"}
        ]
    })

@app.route('/health')
async def health():
    return jsonify({"status": "healthy"}), 200

@app.route('/alerts')
async def get_alerts():
    async with rate_limiter:
        pool = await get_db_connection()
        async with pool.acquire() as conn:
            alerts = await conn.fetch("SELECT * FROM alerts WHERE NOT closed ORDER BY timestamp DESC LIMIT 10")
            formatted_alerts = []
            for alert in alerts:
                formatted_alerts.append({
                    "address": alert["address"],
                    "message_id": alert["message_id"],
                    "initial_market_cap": await format_market_cap(alert["initial_market_cap"]),
                    "chat_id": alert["chat_id"],
                    "bot_name": alert["bot_name"],
                    "timestamp": alert["timestamp"],
                    "bonded": alert["bonded"]
                })
            return jsonify(formatted_alerts)

@app.route('/stats/<int:user_id>')
async def get_stats(user_id):
    async with rate_limiter:
        hitrate_5x, hitrate_2x, migration_rate, total_calls, successful_5x, total_unbonded, migrated = await calculate_hitrate(user_id)
        return jsonify({
            "user_id": user_id,
            "hitrate_5x": format_percentage(hitrate_5x),
            "hitrate_2x": format_percentage(hitrate_2x),
            "migration_rate": format_percentage(migration_rate),
            "total_calls": total_calls,
            "successful_5x": successful_5x,
            "total_unbonded": total_unbonded,
            "migrated": migrated
        })

@app.route('/uptime')
async def get_uptime():
    async with rate_limiter:
        pool = await get_db_connection()
        async with pool.acquire() as conn:
            uptime = await conn.fetchrow("SELECT * FROM uptime_config LIMIT 1")
            if not uptime:
                return jsonify({"error": "Uptime URL not set"}), 404
            return jsonify({
                "url": uptime["url"],
                "last_ping": uptime["last_ping"],
                "status": uptime["status"]
            })
