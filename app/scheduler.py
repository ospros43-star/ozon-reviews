import asyncio
import logging
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.database import SessionLocal
from app.services import review_processor
from app.services.cookie_refresher import refresh_from_safari, try_restore_session

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="UTC")


async def _analytics_warm_job() -> None:
    from app.services.ozon_analytics import fetch_summary, fetch_product_sales, fetch_supply_transit, fetch_delivery_rate
    try:
        await asyncio.gather(
            fetch_summary(28),
            fetch_product_sales(28, 50),
            fetch_supply_transit(),
            fetch_delivery_rate(28),
        )
        logger.info("Analytics cache warmed (28d)")
    except Exception as exc:
        logger.warning("Analytics cache warm failed: %s", exc)


async def _stocks_refresh_job() -> None:
    from app.services.ozon_analytics import refresh_stocks_cache
    try:
        n = await refresh_stocks_cache()
        logger.info("Плановое обновление остатков: %d SKU", n)
    except Exception as exc:
        logger.warning("Ошибка обновления остатков: %s", exc)


async def _poll_job() -> None:
    loop = asyncio.get_event_loop()

    # Проактивно обновляем токены перед каждым опросом:
    # читаем из Safari И делаем GET к seller.ozon.ru → Ozon выдаёт свежие
    # токены в Set-Cookie. Это критично после выхода из сна (access-token живёт ~1ч).
    try:
        await loop.run_in_executor(None, try_restore_session)
    except Exception as exc:
        logger.warning("Проактивный refresh сессии не удался: %s", exc)

    db = SessionLocal()
    try:
        result = await review_processor.run_poll_cycle(db)
        logger.info("Опрос Ozon завершён: %s", result)

        if result.get("error") and "401" in str(result.get("error", "")):
            logger.warning("401 после проактивного refresh — повторяем restore и опрос")
            db.close()

            await asyncio.sleep(5)
            await loop.run_in_executor(None, try_restore_session)
            await asyncio.sleep(2)

            db = SessionLocal()
            result = await review_processor.run_poll_cycle(db)
            logger.info("Повторный опрос: %s", result)

            if result.get("error") and "401" in str(result.get("error", "")):
                logger.error(
                    "Сессия Ozon истекла — зайдите в seller.ozon.ru в Safari "
                    "или нажмите '🔑 Обновить сессию' в приложении"
                )
    except Exception as exc:
        logger.exception("Необработанная ошибка в цикле опроса: %s", exc)
    finally:
        db.close()


async def _session_keepalive_job() -> None:
    """Раз в 10 мин делает GET к seller.ozon.ru чтобы Ozon выдал свежий access-token."""
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, try_restore_session)
    except Exception as exc:
        logger.warning("Keepalive ошибка: %s", exc)


def start_scheduler() -> None:
    logger.info("Инициализация сессии Ozon...")
    refresh_from_safari()  # читаем из Safari-базы (быстро, без сети)
    # try_restore_session() перенесён в _poll_job (async, через executor) — не блокирует event loop

    # Регулярный опрос Ozon
    # coalesce=True: если пропущено несколько итераций (сон ноутбука) — запускаем только одну
    # misfire_grace_time=3600: не пропускать job если компьютер проснулся с опозданием до 1 часа
    scheduler.add_job(
        _poll_job,
        trigger=IntervalTrigger(seconds=settings.poll_interval_seconds),
        id="ozon_poll",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5),
        misfire_grace_time=3600,
        coalesce=True,
    )

    scheduler.add_job(
        _session_keepalive_job,
        trigger=IntervalTrigger(minutes=10),
        id="session_keepalive",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=15),
        misfire_grace_time=3600,
        coalesce=True,
    )

    scheduler.add_job(
        _analytics_warm_job,
        trigger=IntervalTrigger(minutes=4),
        id="analytics_warm",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=10),
        misfire_grace_time=3600,
        coalesce=True,
    )

    scheduler.add_job(
        _stocks_refresh_job,
        trigger=IntervalTrigger(hours=2),
        id="stocks_refresh",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
        misfire_grace_time=3600,
        coalesce=True,
    )

    scheduler.start()
    logger.info("Планировщик запущен. Интервал опроса: %d сек.", settings.poll_interval_seconds)


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
