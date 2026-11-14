"""Точка входа модуля публикаций: планировщик + мониторинг в одном процессе.

Запуск: python -m publications.worker  (в Docker — CMD этого контейнера)
"""
import asyncio
import logging

from publications.monitor import monitor_loop
from publications.scheduler import scheduler_loop

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def main() -> None:
    await asyncio.gather(scheduler_loop(), monitor_loop())


if __name__ == "__main__":
    asyncio.run(main())
