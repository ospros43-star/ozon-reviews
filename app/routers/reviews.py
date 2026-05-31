import asyncio
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Состояние фоновой массовой публикации
_bulk_state: dict = {"running": False, "done": 0, "total": 0, "errors": 0}
# Состояние фоновой перегенерации ошибочных
_regen_state: dict = {"running": False, "done": 0, "total": 0, "errors": 0}
# Состояние подтягивания картинок
_img_state: dict = {"running": False, "done": 0, "total": 0}
# Состояние исторического импорта
_history_state: dict = {"running": False, "done": 0, "page": 0, "cancelled": False}
# Состояние обработки бэклога new-отзывов
_backlog_state: dict = {"running": False, "done": 0, "total": 0, "errors": 0, "cancelled": False}

from app.database import get_db
from app.models.review import Review
from app.schemas.review import (
    ApproveRequest,
    EditResponseRequest,
    PollResult,
    ReviewList,
    ReviewOut,
    StatsOut,
)
from app.services import review_processor

router = APIRouter(prefix="/api", tags=["reviews"])

_ENV_FILE = __import__("pathlib").Path(__file__).parent.parent.parent / ".env"


def _persist_setting(key: str, value: bool) -> None:
    """Сохраняет булевую настройку в .env чтобы она пережила перезапуск."""
    import re
    val_str = "true" if value else "false"
    try:
        if _ENV_FILE.exists():
            text = _ENV_FILE.read_text()
            if re.search(rf"^{key}=", text, re.MULTILINE):
                text = re.sub(rf"^{key}=.*$", f"{key}={val_str}", text, flags=re.MULTILINE)
            else:
                text = text.rstrip("\n") + f"\n{key}={val_str}\n"
            _ENV_FILE.write_text(text)
    except Exception as exc:
        logger.warning("Не удалось сохранить %s в .env: %s", key, exc)


def _base_query(
    status: str | None,
    rating_max: int | None,
    archived: bool,
):
    q = select(Review).where(Review.is_archived == archived)
    if status:
        q = q.where(Review.status == status)
    if rating_max is not None:
        q = q.where(Review.rating <= rating_max)
    return q


@router.get("/reviews", response_model=ReviewList)
def list_reviews(
    status: str | None = None,
    rating_max: int | None = None,
    archived: bool = False,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    q = _base_query(status, rating_max, archived)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    items = db.scalars(q.order_by(Review.review_created_at.desc()).offset(skip).limit(limit)).all()
    return ReviewList(items=list(items), total=total or 0)


@router.get("/reviews/search", response_model=ReviewList)
def search_reviews(
    q: str = "",
    archived: bool = False,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    like = f"%{q}%"
    query = (
        select(Review)
        .where(Review.is_archived == archived)
        .where(
            or_(
                Review.review_text.ilike(like),
                Review.author_name.ilike(like),
                Review.product_name.ilike(like),
            )
        )
    )
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(Review.fetched_at.desc()).offset(skip).limit(limit)).all()
    return ReviewList(items=list(items), total=total or 0)


@router.get("/reviews/{review_id}", response_model=ReviewOut)
def get_review(review_id: int, db: Session = Depends(get_db)):
    review = db.get(Review, review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Отзыв не найден")
    return review


@router.post("/reviews/{review_id}/approve", response_model=ReviewOut)
async def approve_review(
    review_id: int,
    body: ApproveRequest,
    db: Session = Depends(get_db),
):
    try:
        review = await review_processor.approve_and_post(review_id, body.response_text, db)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return review


@router.post("/reviews/{review_id}/reject", response_model=ReviewOut)
async def reject_review(review_id: int, db: Session = Depends(get_db)):
    try:
        review = await review_processor.reject_review(review_id, db)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return review


@router.post("/reviews/{review_id}/archive", response_model=ReviewOut)
def archive_review(review_id: int, db: Session = Depends(get_db)):
    review = db.get(Review, review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Отзыв не найден")
    review.is_archived = True
    db.commit()
    db.refresh(review)
    return review


@router.put("/reviews/{review_id}/response", response_model=ReviewOut)
def save_draft(
    review_id: int,
    body: EditResponseRequest,
    db: Session = Depends(get_db),
):
    review = db.get(Review, review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Отзыв не найден")
    review.edited_response = body.edited_response
    db.commit()
    db.refresh(review)
    return review


@router.post("/reviews/{review_id}/regenerate", response_model=ReviewOut)
async def regenerate_response(review_id: int, db: Session = Depends(get_db)):
    try:
        review = await review_processor.regenerate_response(review_id, db)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return review


@router.post("/reviews/{review_id}/improve", response_model=ReviewOut)
async def improve_response(review_id: int, db: Session = Depends(get_db)):
    try:
        review = await review_processor.improve_response(review_id, db)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return review


@router.get("/stats", response_model=StatsOut)
def get_stats(db: Session = Depends(get_db)):
    rows = db.execute(select(Review.status, func.count()).group_by(Review.status)).all()
    counts = {row[0]: row[1] for row in rows}
    total = sum(counts.values())
    pending_unarchived = db.scalar(
        select(func.count()).where(
            Review.status == "pending_approval", Review.is_archived == False  # noqa: E712
        )
    ) or 0
    pending_negative = db.scalar(
        select(func.count()).where(
            Review.status == "pending_approval", Review.rating <= 3, Review.is_archived == False  # noqa: E712
        )
    ) or 0
    pending_positive = db.scalar(
        select(func.count()).where(
            Review.status == "pending_approval", Review.rating >= 4, Review.is_archived == False  # noqa: E712
        )
    ) or 0

    today_start = datetime.combine(date.today(), datetime.min.time())
    posted_today = db.scalar(
        select(func.count()).where(
            Review.status == "posted",
            Review.posted_at >= today_start,
        )
    ) or 0
    posted_negative = db.scalar(
        select(func.count()).where(Review.status == "posted", Review.rating <= 3)
    ) or 0
    posted_positive = db.scalar(
        select(func.count()).where(Review.status == "posted", Review.rating >= 4)
    ) or 0
    fetched_today = db.scalar(
        select(func.count()).where(Review.fetched_at >= today_start)
    ) or 0
    posted_total = counts.get("posted", 0)
    coverage_pct = round(posted_total / total * 100, 1) if total > 0 else 0.0

    return StatsOut(
        new=counts.get("new", 0),
        pending_approval=counts.get("pending_approval", 0),
        auto_posting=counts.get("auto_posting", 0),
        posted=posted_total,
        rejected=counts.get("rejected", 0),
        error=counts.get("error", 0),
        total=total,
        pending_unarchived=pending_unarchived,
        pending_negative=pending_negative,
        pending_positive=pending_positive,
        posted_today=posted_today,
        posted_negative=posted_negative,
        posted_positive=posted_positive,
        fetched_today=fetched_today,
        coverage_pct=coverage_pct,
    )


async def _bulk_approve_task(delay: float):
    from app.database import SessionLocal
    from app.services import ozon_client
    from app.services.cookie_refresher import refresh_cookies

    _bulk_state["running"] = True
    _bulk_state["cancelled"] = False
    _bulk_state["done"] = 0
    _bulk_state["errors"] = 0

    db = SessionLocal()
    try:
        reviews = db.scalars(
            select(Review).where(
                Review.status == "pending_approval",
                Review.rating >= 4,
                Review.is_archived == False,  # noqa: E712
            ).order_by(Review.fetched_at.asc())
        ).all()
        _bulk_state["total"] = len(reviews)

        for review in reviews:
            if _bulk_state.get("cancelled"):
                break

            # Перечитываем из БД — вдруг уже опубликован параллельным процессом
            db.refresh(review)
            if review.status == "posted":
                _bulk_state["done"] += 1
                continue

            text = review.edited_response or review.generated_response
            if not text:
                _bulk_state["done"] += 1
                continue

            try:
                await ozon_client.post_review_response(review.ozon_review_id, text)
                review.status = "posted"
                review.final_response = text
                review.posted_at = datetime.now()
                db.commit()
                _bulk_state["done"] += 1
            except ozon_client.OzonAPIError as exc:
                msg = str(exc).lower()
                # Уже опубликован другим процессом — не ошибка
                if "already" in msg or "exist" in msg or "duplicate" in msg or exc.status_code == 409:
                    review.status = "posted"
                    review.final_response = text
                    if not review.posted_at:
                        review.posted_at = datetime.now()
                    db.commit()
                    _bulk_state["done"] += 1
                    continue
                if exc.status_code == 401:
                    # Протухли куки — обновляем из Safari и пробуем ещё раз
                    logger.warning("Bulk approve: 401, обновляем куки и повторяем...")
                    refresh_cookies()
                    await asyncio.sleep(2)
                    try:
                        await ozon_client.post_review_response(review.ozon_review_id, text)
                        review.status = "posted"
                        review.final_response = text
                        review.posted_at = datetime.now()
                        db.commit()
                        _bulk_state["done"] += 1
                        continue
                    except Exception as retry_exc:
                        logger.error("Bulk approve retry error for %s: %s", review.ozon_review_id, retry_exc)
                else:
                    logger.error("Bulk approve error for %s: %s", review.ozon_review_id, exc)
                review.status = "error"
                review.error_message = str(exc)
                db.commit()
                _bulk_state["errors"] += 1
                _bulk_state["done"] += 1
            except Exception as exc:
                logger.error("Bulk approve error for %s: %s", review.ozon_review_id, exc)
                review.status = "error"
                review.error_message = str(exc)
                db.commit()
                _bulk_state["errors"] += 1
                _bulk_state["done"] += 1
            await asyncio.sleep(delay)
    finally:
        db.close()
        _bulk_state["running"] = False


async def _bulk_approve_negative_task(delay: float):
    """Публикует pending 1-3★ отзывы с уже готовыми ответами."""
    from app.database import SessionLocal
    from app.services import ozon_client
    from app.services.cookie_refresher import refresh_cookies

    _bulk_state["running"] = True
    _bulk_state["cancelled"] = False
    _bulk_state["done"] = 0
    _bulk_state["errors"] = 0

    db = SessionLocal()
    try:
        reviews = db.scalars(
            select(Review).where(
                Review.status == "pending_approval",
                Review.rating <= 3,
                Review.is_archived == False,  # noqa: E712
            ).order_by(Review.fetched_at.asc())
        ).all()
        _bulk_state["total"] = len(reviews)

        for review in reviews:
            if _bulk_state.get("cancelled"):
                break
            db.refresh(review)
            if review.status == "posted":
                _bulk_state["done"] += 1
                continue
            text = review.edited_response or review.generated_response
            if not text:
                _bulk_state["done"] += 1
                continue
            try:
                await ozon_client.post_review_response(review.ozon_review_id, text)
                review.status = "posted"
                review.final_response = text
                review.posted_at = datetime.now()
                db.commit()
                _bulk_state["done"] += 1
            except ozon_client.OzonAPIError as exc:
                msg = str(exc).lower()
                if "already" in msg or "exist" in msg or "duplicate" in msg or exc.status_code == 409:
                    review.status = "posted"
                    review.final_response = text
                    if not review.posted_at:
                        review.posted_at = datetime.now()
                    db.commit()
                    _bulk_state["done"] += 1
                    continue
                if exc.status_code == 401:
                    refresh_cookies()
                    await asyncio.sleep(2)
                    try:
                        await ozon_client.post_review_response(review.ozon_review_id, text)
                        review.status = "posted"
                        review.final_response = text
                        review.posted_at = datetime.now()
                        db.commit()
                        _bulk_state["done"] += 1
                        continue
                    except Exception:
                        pass
                review.status = "error"
                review.error_message = str(exc)
                db.commit()
                _bulk_state["errors"] += 1
                _bulk_state["done"] += 1
            except Exception as exc:
                logger.error("Bulk neg error for %s: %s", review.ozon_review_id, exc)
                review.status = "error"
                review.error_message = str(exc)
                db.commit()
                _bulk_state["errors"] += 1
                _bulk_state["done"] += 1
            await asyncio.sleep(delay)
    finally:
        db.close()
        _bulk_state["running"] = False


@router.post("/reviews/bulk-approve")
async def bulk_approve(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    if _bulk_state["running"]:
        return {"started": False, "message": "Уже выполняется", **_bulk_state}

    total = db.scalar(
        select(func.count()).where(
            Review.status == "pending_approval",
            Review.rating >= 4,
            Review.is_archived == False,  # noqa: E712
        )
    ) or 0

    if total == 0:
        return {"started": False, "total": 0, "message": "Нет отзывов 4–5★ для публикации"}

    _bulk_state["total"] = total
    _bulk_state["done"] = 0
    _bulk_state["errors"] = 0
    _bulk_state["cancelled"] = False
    background_tasks.add_task(_bulk_approve_task, 10.0)
    return {"started": True, "total": total}


@router.get("/reviews/bulk-approve/status")
def bulk_approve_status():
    return _bulk_state


@router.post("/reviews/bulk-approve/stop")
def bulk_approve_stop():
    _bulk_state["cancelled"] = True
    return {"stopped": True}


@router.post("/reviews/reset-errors")
def reset_errors(db: Session = Depends(get_db)):
    """Сбрасывает все error-отзывы с рейтингом 4-5 обратно в pending_approval."""
    reviews = db.scalars(
        select(Review).where(
            Review.status == "error",
            Review.rating >= 4,
            Review.is_archived == False,  # noqa: E712
        )
    ).all()
    count = 0
    for rv in reviews:
        rv.status = "pending_approval"
        rv.error_message = None
        count += 1
    db.commit()
    return {"reset": count}


async def _regen_errors_task():
    from app.database import SessionLocal
    from app.services import ai_service

    _regen_state["running"] = True
    _regen_state["done"] = 0
    _regen_state["errors"] = 0

    db = SessionLocal()
    try:
        # error отзывы + pending без ответа
        reviews = db.scalars(
            select(Review).where(
                (Review.status == "error") |
                ((Review.status == "pending_approval") &
                 (Review.generated_response.is_(None) | (Review.generated_response == "")))
            ).order_by(Review.fetched_at.asc())
        ).all()
        _regen_state["total"] = len(reviews)

        from app.config import settings as _cfg
        from app.services import review_processor as _rp

        for review in reviews:
            try:
                text = await ai_service.generate_response(
                    rating=review.rating,
                    review_text=review.review_text,
                    product_name=review.product_name or "",
                    author_name=review.author_name or "",
                )
                review.generated_response = text
                review.edited_response = None
                review.error_message = None
                review.processed_at = datetime.now()

                # Если авто-публикация включена — сразу публикуем
                is_pos = review.rating >= 4
                auto_ok = (is_pos and _cfg.auto_post_enabled) or \
                          (not is_pos and _cfg.auto_post_negative_enabled)
                if auto_ok:
                    await _rp._post_response(db, review, text)
                else:
                    review.status = "pending_approval"
                db.commit()
                _regen_state["done"] += 1
            except Exception as exc:
                logger.error("Regen error for %s: %s", review.ozon_review_id, exc)
                _regen_state["errors"] += 1
                _regen_state["done"] += 1
            await asyncio.sleep(0.5)
    finally:
        db.close()
        _regen_state["running"] = False


@router.post("/reviews/regen-errors")
async def regen_errors(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    if _regen_state["running"]:
        return {"started": False, "message": "Уже выполняется", **_regen_state}

    total = db.scalar(select(func.count()).where(Review.status == "error")) or 0
    if total == 0:
        return {"started": False, "total": 0, "message": "Нет отзывов с ошибками"}

    _regen_state["total"] = total
    _regen_state["done"] = 0
    _regen_state["errors"] = 0
    background_tasks.add_task(_regen_errors_task)
    return {"started": True, "total": total}


@router.get("/reviews/regen-errors/status")
def regen_errors_status():
    return _regen_state


_last_manual_poll: float = 0.0
_MANUAL_POLL_COOLDOWN = 60.0  # секунд между ручными опросами

@router.post("/poll/trigger", response_model=PollResult)
async def trigger_poll(db: Session = Depends(get_db)):
    import time
    global _last_manual_poll
    now = time.time()
    if now - _last_manual_poll < _MANUAL_POLL_COOLDOWN:
        # Слишком частые вызовы — отдаём пустой ответ без запуска опроса
        return PollResult()
    _last_manual_poll = now
    # Обновляем куки из Safari перед ручным опросом
    import asyncio as _aio
    from app.services.cookie_refresher import try_restore_session as _trs
    await _aio.get_event_loop().run_in_executor(None, _trs)
    result = await review_processor.run_poll_cycle(db)
    return PollResult(**result) if isinstance(result, dict) else PollResult()


@router.get("/settings")
def get_settings():
    from app.config import settings
    return {
        "auto_post_enabled": settings.auto_post_enabled,
        "auto_post_negative_enabled": settings.auto_post_negative_enabled,
    }


@router.post("/settings/auto-post-negative")
async def toggle_auto_post_negative(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    priority_from: str = "",
    priority_to: str = "",
):
    from app.config import settings
    settings.auto_post_negative_enabled = not settings.auto_post_negative_enabled
    _persist_setting("AUTO_POST_NEGATIVE_ENABLED", settings.auto_post_negative_enabled)

    bulk_started = False
    bulk_total = 0
    if settings.auto_post_negative_enabled and not _bulk_state["running"]:
        total = db.scalar(
            select(func.count()).where(
                Review.status == "pending_approval",
                Review.rating <= 3,
                Review.is_archived == False,  # noqa: E712
            )
        ) or 0
        if total > 0:
            _bulk_state["total"] = total
            _bulk_state["done"] = 0
            _bulk_state["errors"] = 0
            background_tasks.add_task(_bulk_approve_priority_task, 2.0, priority_from, priority_to, "negative")
            bulk_started = True
            bulk_total = total

    return {
        "auto_post_negative_enabled": settings.auto_post_negative_enabled,
        "bulk_started": bulk_started,
        "bulk_total": bulk_total,
    }


@router.post("/settings/auto-post")
async def toggle_auto_post(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    priority_from: str = "",
    priority_to: str = "",
):
    from app.config import settings
    settings.auto_post_enabled = not settings.auto_post_enabled
    _persist_setting("AUTO_POST_ENABLED", settings.auto_post_enabled)

    bulk_started = False
    bulk_total = 0
    if settings.auto_post_enabled and not _bulk_state["running"]:
        total = db.scalar(
            select(func.count()).where(
                Review.status == "pending_approval",
                Review.rating >= 4,
                Review.is_archived == False,  # noqa: E712
            )
        ) or 0
        if total > 0:
            _bulk_state["total"] = total
            _bulk_state["done"] = 0
            _bulk_state["errors"] = 0
            background_tasks.add_task(_bulk_approve_priority_task, 1.5, priority_from, priority_to)
            bulk_started = True
            bulk_total = total

    return {
        "auto_post_enabled": settings.auto_post_enabled,
        "bulk_started": bulk_started,
        "bulk_total": bulk_total,
    }


async def _bulk_approve_priority_task(delay: float, priority_from: str = "", priority_to: str = "", mode: str = "positive"):
    """Bulk publish с приоритетом: сначала выбранный период, затем сегодня, потом остальные.
    mode: 'positive' = 4-5★, 'negative' = 1-3★"""
    from app.database import SessionLocal
    from app.services import ozon_client
    from app.services.cookie_refresher import refresh_cookies
    from datetime import date as _date, datetime as _dt, timedelta as _td

    _bulk_state["running"] = True
    _bulk_state["cancelled"] = False
    _bulk_state["done"] = 0
    _bulk_state["errors"] = 0

    db = SessionLocal()
    try:
        rating_filter = (Review.rating >= 4) if mode == "positive" else (Review.rating <= 3)
        base_q = select(Review).where(
            Review.status == "pending_approval",
            rating_filter,
            Review.is_archived == False,  # noqa: E712
        )
        all_reviews = db.scalars(base_q).all()
        _bulk_state["total"] = len(all_reviews)

        today_start = _dt.combine(_date.today(), _dt.min.time())

        def priority(r: Review) -> int:
            if priority_from and priority_to:
                try:
                    pf = _dt.strptime(priority_from, "%Y-%m-%d")
                    pt = _dt.strptime(priority_to, "%Y-%m-%d") + _td(days=1)
                    if r.review_created_at and pf <= r.review_created_at < pt:
                        return 0  # приоритет 1 — выбранный период
                except ValueError:
                    pass
            if r.review_created_at and r.review_created_at >= today_start:
                return 1  # приоритет 2 — сегодня
            return 2  # приоритет 3 — остальные

        sorted_reviews = sorted(all_reviews, key=priority)

        for review in sorted_reviews:
            if _bulk_state.get("cancelled"):
                break
            db.refresh(review)
            if review.status == "posted":
                _bulk_state["done"] += 1
                continue
            text = review.edited_response or review.generated_response
            if not text:
                _bulk_state["done"] += 1
                continue
            try:
                await ozon_client.post_review_response(review.ozon_review_id, text)
                review.status = "posted"
                review.final_response = text
                review.posted_at = _dt.now()
                db.commit()
                _bulk_state["done"] += 1
            except ozon_client.OzonAPIError as exc:
                msg = str(exc).lower()
                if "already" in msg or "exist" in msg or "duplicate" in msg or exc.status_code == 409:
                    review.status = "posted"
                    review.final_response = text
                    if not review.posted_at:
                        review.posted_at = _dt.now()
                    db.commit()
                    _bulk_state["done"] += 1
                    continue
                if exc.status_code == 401:
                    refresh_cookies()
                    await asyncio.sleep(2)
                    try:
                        await ozon_client.post_review_response(review.ozon_review_id, text)
                        review.status = "posted"
                        review.final_response = text
                        review.posted_at = _dt.now()
                        db.commit()
                        _bulk_state["done"] += 1
                        continue
                    except Exception:
                        pass
                review.status = "error"
                review.error_message = str(exc)
                db.commit()
                _bulk_state["errors"] += 1
                _bulk_state["done"] += 1
            except Exception as exc:
                review.status = "error"
                review.error_message = str(exc)
                db.commit()
                _bulk_state["errors"] += 1
                _bulk_state["done"] += 1
            await asyncio.sleep(delay)
    finally:
        db.close()
        _bulk_state["running"] = False


async def _backfill_images_task():
    from app.database import SessionLocal
    from app.services.ozon_images import fetch_images_by_skus

    _img_state["running"] = True
    _img_state["done"] = 0

    db = SessionLocal()
    try:
        reviews = db.scalars(
            select(Review).where(Review.ozon_sku.isnot(None), Review.image_url.is_(None))
        ).all()
        _img_state["total"] = len(reviews)

        skus = list({r.ozon_sku for r in reviews if r.ozon_sku})
        images = await fetch_images_by_skus(skus)

        done = 0
        for review in reviews:
            if review.ozon_sku and review.ozon_sku in images:
                review.image_url = images[review.ozon_sku]
                done += 1
        db.commit()
        _img_state["done"] = done
        logger.info("Backfill images: обновлено %d из %d", done, len(reviews))
    finally:
        db.close()
        _img_state["running"] = False


@router.post("/reviews/backfill-images")
async def backfill_images(background_tasks: BackgroundTasks):
    if _img_state["running"]:
        return {"started": False, "message": "Уже выполняется", **_img_state}
    background_tasks.add_task(_backfill_images_task)
    return {"started": True}


@router.get("/reviews/backfill-images/status")
def backfill_images_status():
    return _img_state


@router.post("/reviews/backfill-responses")
async def backfill_responses(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Подтягивает тексты ответов для posted-отзывов у которых final_response пустой."""
    async def _task():
        from app.services import ozon_client
        from app.database import get_db as _get_db
        _db = next(_get_db())
        reviews = _db.scalars(
            select(Review).where(
                Review.status == "posted",
                (Review.final_response == None) | (Review.final_response == ""),  # noqa: E711
            )
        ).all()
        updated = 0
        for rv in reviews:
            try:
                text = await ozon_client.fetch_seller_comment(rv.ozon_review_id)
                if text:
                    rv.final_response = text
                    updated += 1
            except Exception:
                pass
        _db.commit()
        _db.close()
        logger.info("Backfill responses: обновлено %d", updated)
    background_tasks.add_task(_task)
    return {"started": True}


_IMPORT_PROGRESS_FILE = Path(__file__).parent.parent.parent / "import_progress.json"


def _save_import_progress(page: int, total_fetched: int) -> None:
    try:
        _IMPORT_PROGRESS_FILE.write_text(
            __import__("json").dumps({"page": page, "total_fetched": total_fetched}),
            encoding="utf-8",
        )
    except Exception:
        pass


def _load_import_progress() -> tuple[int, int]:
    try:
        if _IMPORT_PROGRESS_FILE.exists():
            d = __import__("json").loads(_IMPORT_PROGRESS_FILE.read_text(encoding="utf-8"))
            return d.get("page", 1), d.get("total_fetched", 0)
    except Exception:
        pass
    return 1, 0


async def _fetch_history_task(days: int, resume: bool = True):
    import httpx as _httpx
    from app.services import ozon_client, review_processor
    from app.services.cookie_refresher import try_restore_session
    from app.database import get_db as _get_db

    start_page, prev_fetched = _load_import_progress() if resume else (1, 0)
    _history_state.update({"running": True, "done": prev_fetched, "page": start_page, "cancelled": False})
    cutoff = datetime.now() - timedelta(days=days)
    page = start_page
    total_fetched = prev_fetched
    empty_streak = 0  # подряд пустых страниц
    if start_page > 1:
        logger.info("Импорт возобновлён с стр. %d (уже загружено: %d)", start_page, prev_fetched)

    # Один постоянный клиент — Ozon обновляет access-token через Set-Cookie,
    # клиент подхватывает его автоматически и следующие запросы проходят.
    async with _httpx.AsyncClient(timeout=30) as http_client:
        try:
            while not _history_state["cancelled"]:
                _history_state["page"] = page

                # Обновляем куки каждые 50 страниц чтобы токен не протухал
                if page % 50 == 0:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, try_restore_session)

                try:
                    reviews, _, total_pages = await ozon_client.fetch_page(
                        page, page_size=100, client=http_client
                    )
                except Exception as exc:
                    logger.error("Ошибка при историческом импорте (стр. %d): %s", page, exc)
                    await asyncio.sleep(5)
                    # Пробуем восстановить сессию и повторить
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, try_restore_session)
                    try:
                        reviews, _, total_pages = await ozon_client.fetch_page(
                            page, page_size=100, client=http_client
                        )
                    except Exception:
                        break

                if not reviews:
                    # Rate limit Ozon — ждём и повторяем (лимит сбрасывается за ~90 сек)
                    empty_streak += 1
                    wait = min(30 * empty_streak, 120)
                    logger.info(
                        "Стр. %d пустая (попытка %d) — ждём %d сек и обновляем сессию",
                        page, empty_streak, wait,
                    )
                    if empty_streak >= 5:
                        logger.warning("Стр. %d: rate limit не сброшен после 5 попыток, завершаем", page)
                        break
                    await asyncio.sleep(wait)
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, try_restore_session)
                    continue
                empty_streak = 0

                # Пауза каждые 10 страниц — rate limit Ozon сбрасывается за ~90 сек
                if page % 10 == 0:
                    _save_import_progress(page + 1, total_fetched)
                    logger.info("Импорт: стр. %d, загружено %d — пауза 90с", page, total_fetched)
                    await asyncio.sleep(90)
                    # Обновляем токен ПОСЛЕ паузы (не до — иначе протухнет за 90с)
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, try_restore_session)

                db = next(_get_db())
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
                    total_fetched += 1
                    _history_state["done"] = total_fetched
                    new_reviews.append(review)

                # Сохраняем в DB без AI — только статус pending_approval для новых
                for review in new_reviews:
                    if review.status == "new":
                        review.status = "pending_approval"
                        review.error_message = None
                    db.commit()
                db.close()

                if page % 25 == 0:
                    logger.info(
                        "Импорт: стр. %d/%d, загружено %d новых", page, total_pages, total_fetched
                    )

                if stop or page >= total_pages:
                    break
                page += 1
                await asyncio.sleep(0.2)
        finally:
            _history_state["running"] = False
            logger.info("Исторический импорт завершён: %d отзывов за %d дней", total_fetched, days)


async def _fetch_history_by_date_task(date_from: str, date_to: str):
    """Загружает отзывы за конкретный диапазон дат используя фильтр API."""
    from app.services import ozon_client, review_processor
    from app.database import get_db as _get_db
    _history_state.update({"running": True, "done": 0, "page": 0, "cancelled": False})
    page = 1
    total_fetched = 0

    try:
        while not _history_state["cancelled"]:
            _history_state["page"] = page
            try:
                reviews, total_items, total_pages = await ozon_client.fetch_page(
                    page, page_size=100, date_from=date_from, date_to=date_to
                )
            except Exception as exc:
                logger.error("Ошибка при импорте по дате (стр. %d): %s", page, exc)
                break

            if not reviews:
                break

            db = next(_get_db())
            new_reviews = []
            for raw in reviews:
                review = review_processor._insert_review(db, raw)
                if review is None:
                    continue
                total_fetched += 1
                _history_state["done"] = total_fetched
                new_reviews.append(review)

            if new_reviews:
                await review_processor._fill_images(db, new_reviews)

            _counters = {"fetched": 0, "auto_posted": 0, "pending": 0, "errors": 0}
            for review in new_reviews:
                if review.status != "posted":
                    await review_processor._process_review(db, review, _counters)
                db.commit()
            db.close()

            if page >= total_pages:
                break
            page += 1
            await asyncio.sleep(0.3)
    finally:
        _history_state["running"] = False
        logger.info("Импорт по дате завершён: %d отзывов за %s–%s", total_fetched, date_from, date_to)


@router.post("/reviews/fetch-history")
async def fetch_history(
    background_tasks: BackgroundTasks,
    days: int = 56,
    date_from: str = "",
    date_to: str = "",
):
    if _history_state["running"]:
        return {"error": "already running", "state": _history_state}
    if date_from and date_to:
        background_tasks.add_task(_fetch_history_by_date_task, date_from, date_to)
        return {"started": True, "date_from": date_from, "date_to": date_to}
    background_tasks.add_task(_fetch_history_task, days)
    return {"started": True, "days": days}


@router.get("/reviews/fetch-history/status")
def fetch_history_status():
    return _history_state


@router.post("/reviews/fetch-history/stop")
def fetch_history_stop():
    _history_state["cancelled"] = True
    return {"cancelled": True}


async def _process_backlog_task():
    from app.database import SessionLocal
    from app.services import review_processor
    _backlog_state.update({"running": True, "done": 0, "errors": 0, "cancelled": False})
    db = SessionLocal()
    try:
        reviews = db.scalars(
            select(Review).where(Review.status == "new")
            .order_by(Review.review_created_at.desc())
        ).all()
        _backlog_state["total"] = len(reviews)
        await review_processor._fill_images(db, reviews)
        _counters: dict = {"fetched": 0, "auto_posted": 0, "pending": 0, "errors": 0}
        for review in reviews:
            if _backlog_state.get("cancelled"):
                break
            await review_processor._process_review(db, review, _counters)
            db.commit()
            _backlog_state["done"] += 1
        _backlog_state["errors"] = _counters["errors"]
    except Exception as exc:
        logger.error("process_backlog error: %s", exc)
    finally:
        db.close()
        _backlog_state["running"] = False
        logger.info("Обработка бэклога завершена: %s", _backlog_state)


@router.post("/reviews/process-backlog")
async def process_backlog(background_tasks: BackgroundTasks):
    if _backlog_state["running"]:
        return {"started": False, **_backlog_state}
    db = next(get_db())
    total = db.scalar(select(func.count()).where(Review.status == "new")) or 0
    db.close()
    if total == 0:
        return {"started": False, "message": "Нет отзывов со статусом new", "total": 0}
    _backlog_state["total"] = total
    background_tasks.add_task(_process_backlog_task)
    return {"started": True, "total": total}


@router.get("/reviews/process-backlog/status")
def process_backlog_status():
    return _backlog_state


@router.post("/cookies/refresh")
def refresh_cookies_endpoint():
    from app.services.cookie_refresher import refresh_cookies
    ok = refresh_cookies()
    return {"success": ok, "message": "Куки обновлены из Safari" if ok else "Не удалось обновить куки — проверьте доступ к диску"}
