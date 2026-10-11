"""API-only launcher for high traffic (no Telegram bot).
Runs WEB_CONCURRENCY uvicorn workers (default = CPU cores x 2).
Run the bot separately with:  python -c "import asyncio,tgbot;asyncio.run(tgbot.run_bot())"
"""
import multiprocessing, os
from dotenv import load_dotenv
load_dotenv()
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")),
        workers=int(os.getenv("WEB_CONCURRENCY", multiprocessing.cpu_count() * 2)),
        loop="uvloop", http="httptools", backlog=4096,
        timeout_keep_alive=30, log_level=os.getenv("LOG_LEVEL", "warning").lower(),
    )
