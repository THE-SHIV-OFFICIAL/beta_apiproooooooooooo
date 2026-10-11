"""Single-process BetaAPI launcher for the HTTP API and Telegram bot."""

import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

import uvicorn
from tgbot import run_bot


async def _run_api():
    config = uvicorn.Config(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        workers=1,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
    await uvicorn.Server(config).serve()


async def main():
    api_task = asyncio.create_task(_run_api(), name="betaapi-api")
    bot_task = asyncio.create_task(_bot_forever(), name="betaapi-bot")
    done, pending = await asyncio.wait(
        {api_task, bot_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    for task in done:
        exception = task.exception()
        if exception:
            raise exception


async def _bot_forever():
    # Bot crash must never take the API down — restart bot with backoff.
    while True:
        try:
            await run_bot()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.exception("Telegram bot crashed, restarting in 10s: %s", e)
        await asyncio.sleep(10)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
