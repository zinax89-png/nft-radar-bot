import asyncio
import logging
import os
import time

import aiohttp
import main as bot


POLL_INTERVAL = max(10, int(os.getenv("POLL_INTERVAL_SECONDS", "15")))


async def run_worker():
    timeout = aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await bot.register_bot_commands(session)
        await bot.load_state(session)
        logging.info("Persistent NFT Radar worker started. Poll interval: %s seconds.", POLL_INTERVAL)

        while True:
            started = time.monotonic()
            try:
                result = await bot.telegram_call(
                    session,
                    "getUpdates",
                    {
                        "offset": bot.offset,
                        "timeout": 0,
                        "allowed_updates": ["message"],
                    },
                )
                if not result.get("ok"):
                    raise RuntimeError("Telegram getUpdates failed")

                for update in result.get("result", []):
                    await bot.handle_update(session, update)

                await bot.scan_telegram_gifts(session)
                await bot.scan_portals_sales(session)
                await bot.save_state(session)
                logging.info("Worker cycle complete. Subscribers: %d", len(bot.users))

            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Worker cycle failed; will retry.")

            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1, POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logging.info("Worker stopped.")
