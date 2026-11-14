"""Publication scheduler plus VK comment monitor.

Run with ``python -m publications.worker``. Platform tokens are intentionally not
accepted here: all VK/Telegram traffic is proxied through Backend internal APIs.
"""
import asyncio
import logging

from publications.db import ensure_worker_tables
from publications.monitor import monitor_loop
from publications.scheduler import publisher_headers, scheduler_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


async def main() -> None:
    publisher_headers()
    await ensure_worker_tables()
    await asyncio.gather(scheduler_loop(), monitor_loop())


if __name__ == "__main__":
    asyncio.run(main())
