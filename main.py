"""main.py — CrabVPN entry point"""
import asyncio
import logging
from models import init_db
from bot import build_application
from api import run_web_server

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)

async def post_init(app):
    asyncio.create_task(run_web_server(app))

if __name__ == "__main__":
    init_db()
    app = build_application()
    app.post_init = post_init
    app.run_polling()
