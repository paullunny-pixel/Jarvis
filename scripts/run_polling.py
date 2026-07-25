"""Local development runner — long-polls Telegram instead of using a webhook.

Usage: python -m scripts.run_polling   (reads .env for keys)
"""
from __future__ import annotations

import asyncio
import logging

from app.main import build_components

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("jarvis.polling")


async def main() -> None:
    router, _heartbeat = build_components()
    await router.db.init()
    try:
        await router.telegram.delete_webhook()
    except Exception:  # webhook may simply not exist yet
        pass
    logger.info("Polling for messages — talk to your bot on Telegram. Ctrl-C to stop.")
    offset = 0
    while True:
        try:
            updates = await router.telegram.get_updates(offset=offset, timeout=30)
        except Exception:
            logger.exception("getUpdates failed; retrying in 5s")
            await asyncio.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            await router.handle_update(update)


if __name__ == "__main__":
    asyncio.run(main())
