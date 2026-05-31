import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.review import Review
from app.services import ai_service, ozon_client

logger = logging.getLogger(__name__)

LOW_RATING_THRESHOLD = 3  # 1–3 → ручная проверка, 4–5 → авто
_poll_lock = asyncio.Lock()  # предотвращает одновременный запуск двух циклов


class _SessionError(Exception):
    """Сессия Ozon истекла — нужно прервать батч публикации."""


async def _refresh_session_once() -> bool:
    """Обновляет сессию и возвращает True если сессия рабочая."""
    try:
        from app.services.cookie_refresher import try_restore_session
        import asyncio
        result = await asyncio.get_event_loop().run_in_executor(None, try_restore_session)
        return bool(result)
    except Exception as exc:
        logger.warning("Не удалось обновить сессию: %s", exc)
        return False


_MSK = timezone(timedelta(hours=3))

def _parse_ozon_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            dt = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            return dt.astimezone(_MSK).replace(tzinfo=None)
        except ValueError:
            continue
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
        return dt.astimezone(_MSK).replace(tzinfo=None)
    except ValueError:
        pass
    return None


async def run_poll_cycle(db: Session) -> dict[str, Any]:
    if _poll_lock.locked():
        return {"error": "poll already running"}
    async with _poll_lock:
        return await _run_poll_cycle_inner(db)


async def _run_poll_cycle_inner(db: Session) -> dict[str, Any]:
    # Обновляем сессию в начале каждого цикла (читаем свежие куки из Safari)
    await _refresh_session_once()

    # Повтор зависших auto_posting
    await _retry_stuck(db)
    # Обработка застрявших "new" отзывов (сохранены но не обработаны)
    await _process_stale_new(db)
    # Повтор error-отзывов у которых уже есть ответ — изолируем от основного цикла
    try:
        await _retry_errors_with_response(db)
    except Exception as exc:
        logger.error("Ошибка в _retry_errors_with_response (цикл продолжается): %s", exc)
    # Авто-публикация pending_approval — тоже изолируем
    try:
        await _auto_post_ready_pending(db)
    except Exception as exc:
        logger.error("Ошибка в _auto_post_ready_pending (цикл продолжается): %s", exc)

    from app.config import settings as cfg

    # Курсор — максимальная дата предыдущего опроса
    last_fetched = db.scalar(select(func.max(Review.fetched_at)))

    # Первый запуск — грузим больше страниц; регулярный опрос — только свежие
    max_pages = cfg.initial_pages if last_fetched is None else cfg.poll_pages

    try:
        raw_reviews = await ozon_client.fetch_new_reviews(since=last_fetched, max_pages=max_pages)
    except ozon_client.OzonAPIError as exc:
        logger.error("Ошибка при получении отзывов из Ozon: %s", exc)
        return {"error": str(exc)}

    counters: dict[str, int] = {"fetched": 0, "auto_posted": 0, "pending": 0, "errors": 0}

    # Вставляем все отзывы без коммита
    new_reviews: list[Review] = []
    for raw in raw_reviews:
        review = _insert_review(db, raw)
        if review is None:
            continue
        new_reviews.append(review)

    # Подтягиваем картинки до первого коммита — чтобы отзыв появился сразу с фото
    if new_reviews:
        await _fill_images(db, new_reviews)

    # Теперь обрабатываем и коммитим каждый отзыв
    for review in new_reviews:
        counters["fetched"] += 1
        if review.status == "posted":
            reply_text = await ozon_client.fetch_seller_comment(review.ozon_review_id)
            if reply_text:
                review.final_response = reply_text
            db.commit()
            continue
        await _process_review(db, review, counters)
        db.commit()

    logger.info("Цикл опроса завершён: %s", counters)
    return counters


async def _fill_images(db: Session, reviews: list[Review]) -> None:
    from app.services.ozon_images import fetch_images_by_skus

    skus = list({r.ozon_sku for r in reviews if r.ozon_sku and not r.image_url})
    if not skus:
        return
    try:
        images = await fetch_images_by_skus(skus)
    except Exception as exc:
        logger.warning("_fill_images failed: %s", exc)
        return
    updated = 0
    for review in reviews:
        if review.ozon_sku and review.ozon_sku in images and not review.image_url:
            review.image_url = images[review.ozon_sku]
            updated += 1
    if updated:
        db.commit()
        logger.info("Обновлено картинок: %d", updated)


def _insert_review(db: Session, raw: dict) -> Review | None:
    review = Review(
        # Сохраняем UUID для отправки ответа через /api/review/comment/create
        ozon_review_id=raw.get("review_uuid") or raw.get("review_id") or "",
        product_id=raw.get("product_id") or "",
        product_name=raw.get("product_name") or "",
        author_name=raw.get("author_name") or "",
        rating=int(raw.get("rating") or 0),
        review_text=raw.get("review_text") or "",
        review_created_at=_parse_ozon_datetime(raw.get("review_created_at")),
        image_url=raw.get("image_url"),
        ozon_sku=raw.get("ozon_sku"),
        ozon_product_url=raw.get("ozon_product_url"),
        purchase_verified=raw.get("purchase_verified"),
        fetched_at=datetime.now(),
        # Если на Ozon уже есть ответ — сразу помечаем как posted
        status="posted" if raw.get("has_seller_reply") else "new",
    )
    try:
        db.add(review)
        db.flush()
        db.commit()  # commit immediately — prevents IntegrityError rollback from wiping other inserts
        db.refresh(review)
        return review
    except IntegrityError:
        db.rollback()
        return None


async def _process_review(db: Session, review: Review, counters: dict) -> None:
    from app.config import settings

    try:
        text = await ai_service.generate_response(
            rating=review.rating,
            review_text=review.review_text,
            product_name=review.product_name or "",
            author_name=review.author_name or "",
        )
    except ai_service.AIServiceError as exc:
        logger.error("Ошибка генерации ответа для %s: %s", review.ozon_review_id, exc)
        review.status = "error"
        review.error_message = str(exc)
        counters["errors"] += 1
        return

    review.generated_response = text
    review.processed_at = datetime.now()

    from app.routers.reviews import _bulk_state
    bulk_running = _bulk_state.get("running", False)

    is_positive = review.rating >= 4
    auto_ok = (is_positive and settings.auto_post_enabled) or \
              (not is_positive and settings.auto_post_negative_enabled)
    if auto_ok and not bulk_running:
        review.status = "auto_posting"
        db.flush()
        try:
            await _post_response(db, review, text)
        except _SessionError:
            # Сессия истекла — сохраняем как pending, опубликуем в следующем цикле
            review.status = "pending_approval"
            review.error_message = None
            counters["pending"] += 1
            return
        if review.status == "posted":
            counters["auto_posted"] += 1
        else:
            counters["errors"] += 1
    else:
        review.status = "pending_approval"
        review.error_message = None
        counters["pending"] += 1


async def _post_response(db: Session, review: Review, text: str) -> None:
    try:
        await ozon_client.post_review_response(review.ozon_review_id, text)
        review.status = "posted"
        review.final_response = text
        review.posted_at = datetime.now()
    except ozon_client.OzonAPIError as exc:
        status = getattr(exc, "status_code", 0)
        exc_str = str(exc).lower()
        # 401/403 / unauthenticated — сессия истекла, прерываем батч
        if status in (401, 403) or "unauthenticated" in exc_str or "unauthorized" in exc_str:
            raise _SessionError(str(exc))
        msg = exc_str
        if "already" in msg or "exist" in msg or "duplicate" in msg or status == 409:
            logger.info("Ответ уже опубликован для %s — помечаем как posted", review.ozon_review_id)
            review.status = "posted"
            review.final_response = text
            if not review.posted_at:
                review.posted_at = datetime.now()
        else:
            logger.error("Ошибка публикации ответа на %s: %s", review.ozon_review_id, exc)
            review.status = "error"
            review.error_message = str(exc)
    except Exception as exc:
        # ConnectTimeout и другие сетевые ошибки — прерываем батч, не помечаем как error
        logger.warning("Сетевая ошибка при публикации %s: %s", review.ozon_review_id, type(exc).__name__)
        raise _SessionError(f"network: {exc}")


async def _auto_post_ready_pending(db: Session) -> None:
    """Публикует pending_approval отзывы с готовым ответом если авто-публикация включена."""
    from app.config import settings
    from app.routers.reviews import _bulk_state
    if _bulk_state.get("running"):
        return  # bulk сам справится
    ready = db.scalars(
        select(Review).where(
            Review.status == "pending_approval",
            Review.is_archived == False,  # noqa: E712
            (Review.edited_response.isnot(None) | Review.generated_response.isnot(None)),
        )
    ).all()
    has_auto = any(
        (r.rating >= 4 and settings.auto_post_enabled) or
        (r.rating < 4 and settings.auto_post_negative_enabled)
        for r in ready
    )
    if not has_auto:
        return
    if not await _refresh_session_once():
        logger.warning("Сессия недоступна — пропускаем авто-публикацию, войдите в seller.ozon.ru в Safari")
        return
    for review in ready:
        is_positive = review.rating >= 4
        auto_ok = (is_positive and settings.auto_post_enabled) or \
                  (not is_positive and settings.auto_post_negative_enabled)
        if not auto_ok:
            continue
        text = review.edited_response or review.generated_response or ""
        if not text:
            continue
        try:
            await _post_response(db, review, text)
        except _SessionError as exc:
            logger.warning("Сессия истекла в _auto_post_ready_pending — прерываем батч: %s", exc)
            await _refresh_session_once()
            break
        db.commit()
        if review.status == "posted":
            logger.info("Авто-опубликован pending отзыв %s", review.ozon_review_id)


async def _process_stale_new(db: Session) -> None:
    """Обрабатывает отзывы в статусе 'new' — они были сохранены, но AI-ответ не был сгенерирован."""
    stale = db.scalars(select(Review).where(Review.status == "new")).all()
    if not stale:
        return
    logger.info("Обрабатываем %d застрявших 'new' отзывов", len(stale))
    counters: dict[str, int] = {"fetched": 0, "auto_posted": 0, "pending": 0, "errors": 0}
    for review in stale:
        await _process_review(db, review, counters)
        db.commit()


async def _retry_errors_with_response(db: Session) -> None:
    """Повторяет публикацию error-отзывов у которых уже есть сгенерированный ответ.
    При 401/ConnectTimeout прерывает батч и обновляет сессию."""
    from app.config import settings
    from app.routers.reviews import _bulk_state
    if _bulk_state.get("running"):
        return
    errors = db.scalars(
        select(Review).where(
            Review.status == "error",
            Review.is_archived == False,  # noqa: E712
            (Review.edited_response.isnot(None) | Review.generated_response.isnot(None)),
        )
    ).all()
    if not errors:
        return
    logger.info("Повтор %d error-отзывов с готовым ответом", len(errors))
    if not await _refresh_session_once():
        logger.warning("Сессия недоступна — пропускаем повтор ошибок, войдите в seller.ozon.ru в Safari")
        return
    for review in errors:
        is_positive = review.rating >= 4
        auto_ok = (is_positive and settings.auto_post_enabled) or \
                  (not is_positive and settings.auto_post_negative_enabled)
        if not auto_ok:
            review.status = "pending_approval"
            review.error_message = None
            db.commit()
            continue
        text = review.edited_response or review.generated_response or ""
        if not text:
            continue
        try:
            await _post_response(db, review, text)
        except _SessionError as exc:
            logger.warning("Сессия истекла в _retry_errors_with_response — прерываем батч: %s", exc)
            await _refresh_session_once()
            break  # оставшиеся отзывы будут повторены в следующем цикле
        db.commit()
        if review.status == "posted":
            logger.info("Повторно опубликован error-отзыв %s", review.ozon_review_id)


async def _retry_stuck(db: Session) -> None:
    stuck = db.scalars(select(Review).where(Review.status == "auto_posting")).all()
    for review in stuck:
        logger.warning("Повтор застрявшего авто-ответа для %s", review.ozon_review_id)
        text = review.generated_response or ""
        if text:
            try:
                await _post_response(db, review, text)
            except _SessionError:
                review.status = "pending_approval"
                review.error_message = None
    if stuck:
        db.commit()


async def approve_and_post(review_id: int, response_text: str, db: Session) -> Review:
    review = db.get(Review, review_id)
    if not review:
        raise ValueError(f"Отзыв {review_id} не найден")
    if review.status not in ("pending_approval", "error"):
        raise ValueError(f"Отзыв {review_id} имеет статус '{review.status}', ожидается 'pending_approval'")

    await _post_response(db, review, response_text)
    db.commit()
    db.refresh(review)
    return review


async def reject_review(review_id: int, db: Session) -> Review:
    review = db.get(Review, review_id)
    if not review:
        raise ValueError(f"Отзыв {review_id} не найден")
    review.status = "rejected"
    db.commit()
    db.refresh(review)
    return review


async def regenerate_response(review_id: int, db: Session) -> Review:
    review = db.get(Review, review_id)
    if not review:
        raise ValueError(f"Отзыв {review_id} не найден")

    text = await ai_service.generate_response(
        rating=review.rating,
        review_text=review.review_text,
        product_name=review.product_name or "",
        author_name=review.author_name or "",
    )
    review.generated_response = text
    review.edited_response = None
    review.regenerate_count = (review.regenerate_count or 0) + 1
    review.processed_at = datetime.now()
    db.commit()
    db.refresh(review)
    return review


async def improve_response(review_id: int, db: Session) -> Review:
    review = db.get(Review, review_id)
    if not review:
        raise ValueError(f"Отзыв {review_id} не найден")

    current = review.edited_response or review.generated_response or ""
    text = await ai_service.improve_response(
        existing_response=current,
        review_text=review.review_text,
        rating=review.rating,
    )
    review.edited_response = text
    review.processed_at = datetime.now()
    db.commit()
    db.refresh(review)
    return review
