"""Автономный импорт истории отзывов — запускается отдельно от сервера."""
import asyncio
import logging
import sys
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DAYS = 3650  # 10 лет


async def run():
    from app.services import ozon_client, review_processor
    from app.services.cookie_refresher import refresh_cookies
    from app.database import SessionLocal, init_db
    from app.models.review import Review
    from sqlalchemy import select, func

    init_db()
    refresh_cookies()

    # Проверяем что сессия работает
    try:
        reviews, total_items, total_pages = await ozon_client.fetch_page(1, page_size=1)
        logger.info("Сессия OK. Всего отзывов на Ozon: %d, страниц: %d", total_items, total_pages)
    except Exception as e:
        logger.error("Ошибка сессии: %s — войди в seller.ozon.ru в Safari", e)
        return

    db = SessionLocal()
    existing = db.scalar(select(func.count()).select_from(Review))
    db.close()
    logger.info("В базе уже: %d отзывов", existing)

    cutoff = datetime.now() - timedelta(days=DAYS)
    page = 1
    total_fetched = 0
    start_time = datetime.now()

    while True:
        try:
            reviews, _, total_pages = await ozon_client.fetch_page(page, page_size=100)
        except Exception as exc:
            logger.error("Ошибка на стр. %d: %s", page, exc)
            await asyncio.sleep(10)
            continue

        if not reviews:
            logger.info("Стр. %d — пустая, конец", page)
            break

        db = SessionLocal()
        stop = False
        new_reviews = []
        for raw in reviews:
            dt = review_processor._parse_ozon_datetime(raw.get("review_created_at"))
            if dt and dt < cutoff:
                stop = True
                break
            review = review_processor._insert_review(db, raw)
            if review is None:
                continue
            new_reviews.append(review)

        # AI только для отзывов без ответа (без автопубликации при импорте)
        for review in new_reviews:
            if review.status == "new":
                review.status = "pending_approval"
                db.commit()

        db.close()
        total_fetched += len(new_reviews)

        elapsed = (datetime.now() - start_time).seconds
        eta_pages = total_pages - page
        speed = page / max(elapsed, 1) * 60  # страниц в минуту
        eta_min = int(eta_pages / max(speed, 1))
        logger.info(
            "Стр. %d/%d | Новых: +%d (итого %d) | ~%d мин осталось",
            page, total_pages, len(new_reviews), total_fetched, eta_min,
        )

        if stop or page >= total_pages:
            break
        page += 1
        await asyncio.sleep(0.15)

    logger.info("Импорт завершён: загружено %d новых отзывов", total_fetched)


if __name__ == "__main__":
    asyncio.run(run())
