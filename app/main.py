import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware
from datetime import date, datetime, timedelta

from sqlalchemy import case, func, or_, select

_TRANSIT_FILE = Path(__file__).parent.parent / "transit_stock.json"

def _load_transit() -> dict:
    if _TRANSIT_FILE.exists():
        try:
            return json.loads(_TRANSIT_FILE.read_text())
        except Exception:
            pass
    return {}

def _save_transit(data: dict) -> None:
    _TRANSIT_FILE.write_text(json.dumps(data, ensure_ascii=False))

from app.database import get_db, init_db
from app.models.review import Review
from app.routers.reviews import router as reviews_router
from app.routers.supplies import router as supplies_router
from app.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

templates = Jinja2Templates(directory="templates")

PAGE_SIZE = 30


def _fetch_seller_info() -> dict:
    import httpx
    from app.config import settings as cfg
    try:
        resp = httpx.post(
            "https://api-seller.ozon.ru/v1/seller/info",
            headers={
                "Client-Id": cfg.ozon_client_id,
                "Api-Key": cfg.ozon_api_key,
                "Content-Type": "application/json",
            },
            json={},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            company = data.get("company", {})
            rating = None
            for r in data.get("ratings", []):
                if r.get("rating") == "rating_review_avg_score_total":
                    rating = r.get("current_value", {}).get("formatted")
                    break
            return {
                "name": company.get("name", ""),
                "form": company.get("ownership_form", ""),
                "legal_name": company.get("legal_name", ""),
                "rating": rating,
            }
    except Exception as exc:
        logging.warning("Не удалось получить данные продавца: %s", exc)
    return {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    from app.routers.reviews import _backfill_images_task, _fetch_history_task

    init_db()
    start_scheduler()

    seller = _fetch_seller_info()
    templates.env.globals["seller"] = seller

    asyncio.create_task(_backfill_images_task())
    asyncio.create_task(_warmup_products_cache())

    # Если база пустая — новый кабинет, загружаем историю за 28 дней
    db = next(get_db())
    total = db.scalar(select(func.count()).select_from(Review))
    db.close()
    if total == 0:
        logging.info("Новый кабинет — запускаем импорт истории за 28 дней")
        asyncio.create_task(_fetch_history_task(28))
    else:
        # Есть накопившиеся pending — запускаем bulk через 30с после старта
        from app.config import settings as _cfg
        from app.routers.reviews import _bulk_approve_task, _bulk_approve_negative_task, _bulk_state
        from sqlalchemy import select as _select
        db2 = next(get_db())
        pending_pos = db2.scalar(_select(func.count()).where(
            Review.status == "pending_approval", Review.rating >= 4, Review.is_archived == False))  # noqa
        pending_neg = db2.scalar(_select(func.count()).where(
            Review.status == "pending_approval", Review.rating <= 3, Review.is_archived == False))  # noqa
        db2.close()
        if _cfg.auto_post_enabled and pending_pos and pending_pos > 0:
            logging.info("Автозапуск bulk publish: %d отзывов 4-5★", pending_pos)
            asyncio.create_task(_delayed_bulk(_bulk_approve_task, 30))
        if _cfg.auto_post_negative_enabled and pending_neg and pending_neg > 0:
            logging.info("Автозапуск bulk publish: %d отзывов 1-3★", pending_neg)
            asyncio.create_task(_delayed_bulk(_bulk_approve_negative_task, 35))

    yield
    stop_scheduler()


async def _delayed_bulk(task_fn, delay_sec: int):
    await asyncio.sleep(delay_sec)
    await task_fn(1.5)


async def _warmup_products_cache():
    """Прогревает кеш товаров при старте в фоне."""
    import asyncio as _aio
    await _aio.sleep(8)
    try:
        from app.services.ozon_products import fetch_products_for_sale
        await fetch_products_for_sale()
        logging.info("Products cache warmed up")
    except Exception as exc:
        logging.warning("Products cache warmup failed: %s", exc)


from app.config import settings as _app_settings
from app.auth import check_credentials, is_authenticated, login_user, logout_user
from starlette.middleware.base import BaseHTTPMiddleware

class _AuthMiddleware(BaseHTTPMiddleware):
    """Проверяет авторизацию. Запускается после SessionMiddleware."""
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        public = path.startswith("/login") or path.startswith("/static") or path == "/favicon.ico" or path == "/api/sync-cookie"
        if not public and not request.session.get("user"):
            accept = request.headers.get("accept", "")
            if "text/html" in accept or not path.startswith("/api"):
                return RedirectResponse("/login", status_code=302)
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

app = FastAPI(title="Ozon Review Bot", lifespan=lifespan)
# Порядок add_middleware — LIFO: SessionMiddleware (добавлен последним) запускается первым,
# затем _AuthMiddleware уже имеет доступ к request.session
app.add_middleware(_AuthMiddleware)
app.add_middleware(SessionMiddleware, secret_key=_app_settings.secret_key, max_age=86400 * 30)

app.include_router(reviews_router)
app.include_router(supplies_router)

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if check_credentials(username, password):
        login_user(request, username)
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": "Неверный логин или пароль",
        "username": username,
    })

@app.get("/logout")
async def logout(request: Request):
    logout_user(request)
    return RedirectResponse("/login", status_code=302)


@app.post("/api/sync-cookie")
async def sync_cookie(request: Request):
    """Mac пушит сюда свежие куки Ozon каждые 20 минут."""
    from app.config import settings as _cfg
    secret = request.headers.get("x-sync-secret", "")
    if not _cfg.sync_secret or secret != _cfg.sync_secret:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json()
    cookie = body.get("cookie", "").strip()
    if not cookie:
        return JSONResponse({"error": "cookie required"}, status_code=400)
    _cfg.ozon_cookie = cookie
    logging.info("Куки Ozon обновлены удалённо (%d символов)", len(cookie))
    return {"ok": True, "length": len(cookie)}


@app.post("/api/reviews/import-excel")
async def import_reviews_excel(request: Request):
    """Импортирует отзывы из Excel-выгрузки Ozon (тело запроса = bytes файла)."""
    from app.services import review_processor
    from app.database import SessionLocal
    import openpyxl, io, asyncio

    body = await request.body()
    if not body:
        return JSONResponse({"error": "файл не получен"}, status_code=400)

    try:
        wb = openpyxl.load_workbook(io.BytesIO(body), read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
    except Exception as exc:
        return JSONResponse({"error": f"не удалось прочитать Excel: {exc}"}, status_code=400)

    if not rows:
        return JSONResponse({"error": "файл пустой"}, status_code=400)

    # Автоопределение колонок по заголовку
    header = [str(c).strip().lower() if c else "" for c in rows[0]]

    def col(names):
        for n in names:
            for i, h in enumerate(header):
                if n in h:
                    return i
        return None

    idx_uuid   = col(["uuid", "id отзыва", "review_id", "идентификатор"])
    idx_date   = col(["дата", "date", "создан", "created"])
    idx_rating = col(["оценк", "rating", "балл", "звезд"])
    idx_text   = col(["текст", "text", "отзыв", "комментар"])
    idx_author = col(["автор", "покупател", "author", "имя"])
    idx_sku    = col(["артикул", "sku", "товар id", "product_id"])
    idx_pname  = col(["товар", "product", "наименован"])
    idx_reply  = col(["ответ", "reply", "comment"])

    imported = skipped = already = 0
    db = SessionLocal()
    try:
        for row in rows[1:]:
            def v(i):
                return str(row[i]).strip() if i is not None and i < len(row) and row[i] is not None else ""

            uuid = v(idx_uuid)
            if not uuid or uuid == "None":
                skipped += 1
                continue

            rating_raw = v(idx_rating)
            try:
                rating = int(float(rating_raw)) if rating_raw else 0
            except ValueError:
                rating = 0

            date_raw = v(idx_date)
            review_dt = None
            if date_raw:
                for fmt in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        review_dt = datetime.strptime(date_raw, fmt)
                        break
                    except ValueError:
                        pass
                if not review_dt and hasattr(row[idx_date] if idx_date is not None else None, 'strftime'):
                    review_dt = row[idx_date]

            has_reply = bool(v(idx_reply)) if idx_reply is not None else False

            raw = {
                "review_uuid": uuid,
                "review_id": uuid,
                "review_created_at": review_dt.isoformat() if review_dt else None,
                "rating": rating,
                "review_text": v(idx_text),
                "author_name": v(idx_author),
                "product_id": v(idx_sku),
                "product_name": v(idx_pname),
                "ozon_sku": v(idx_sku),
                "has_seller_reply": has_reply,
            }
            review = review_processor._insert_review(db, raw)
            if review is None:
                already += 1
                continue

            counters = {"fetched": 0, "auto_posted": 0, "pending": 0, "errors": 0}
            await review_processor._process_review(db, review, counters)
            db.commit()
            imported += 1
    finally:
        db.close()

    return JSONResponse({
        "ok": True,
        "imported": imported,
        "already_in_db": already,
        "skipped_no_uuid": skipped,
        "message": f"Импортировано {imported} новых отзывов, {already} уже были в базе",
    })


@app.get("/api/settings/response-options")
async def get_response_options():
    from app.services.response_settings import load
    return JSONResponse(load())


@app.post("/api/settings/response-options")
async def save_response_options(request: Request):
    from app.services.response_settings import save
    body = await request.json()
    save(body)
    return JSONResponse({"ok": True})


@app.post("/api/refresh-session")
async def refresh_session():
    """Обновляет куки из Safari и запускает опрос Ozon."""
    import asyncio
    from app.services.cookie_refresher import refresh_cookies, try_restore_session
    from app.database import SessionLocal
    from app.services import review_processor

    refresh_cookies()

    db = SessionLocal()
    try:
        result = await review_processor.run_poll_cycle(db)
        if result.get("error") and "401" in str(result.get("error", "")):
            # Куки протухли — пробуем восстановить через HTTP и ретраим
            await asyncio.get_event_loop().run_in_executor(None, try_restore_session)
            await asyncio.sleep(1)
            db.close()
            db = SessionLocal()
            result = await review_processor.run_poll_cycle(db)
            if result.get("error") and "401" in str(result.get("error", "")):
                return JSONResponse(
                    {"ok": False, "message": "Сессия истекла — откройте seller.ozon.ru в Safari и нажмите 🔑 снова"},
                    status_code=401,
                )
        fetched = result.get("fetched", 0)
        return JSONResponse({"ok": True, "message": f"Готово — загружено {fetched} новых отзывов", "fetched": fetched})
    except Exception as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=502)
    finally:
        db.close()


@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    # Возвращаем HTML-страницу вместо JSON {"detail":"Not Found"}
    if request.url.path.startswith("/api/") or request.url.path.startswith("/partials/"):
        return await http_exception_handler(request, exc)
    return templates.TemplateResponse(
        "404.html", {"request": request}, status_code=404
    )


@app.get("/api/stats-by-date")
async def stats_by_date(date_from: str = "", date_to: str = ""):
    db = next(get_db())
    q = select(
        func.count().label("total"),
        func.sum(case((Review.status == "posted", 1), else_=0)).label("posted"),
        func.sum(case((Review.status.in_(["pending_approval", "error"]), 1), else_=0)).label("pending"),
        func.sum(case((Review.status.in_(["pending_approval", "error"]) & (Review.rating <= 3), 1), else_=0)).label("pending_negative"),
        func.sum(case((Review.status.in_(["pending_approval", "error"]) & (Review.rating >= 4), 1), else_=0)).label("pending_positive"),
        func.sum(case(((Review.status == "posted") & (Review.rating <= 3), 1), else_=0)).label("posted_negative"),
        func.sum(case(((Review.status == "posted") & (Review.rating >= 4), 1), else_=0)).label("posted_positive"),
    ).where(Review.is_archived == False)  # noqa: E712
    if date_from:
        try:
            q = q.where(Review.review_created_at >= datetime.strptime(date_from, "%Y-%m-%d"))
        except ValueError:
            pass
    if date_to:
        try:
            q = q.where(Review.review_created_at < datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1))
        except ValueError:
            pass
    row = db.execute(q).one()
    db.close()
    return JSONResponse({
        "total": row.total, "posted": row.posted, "pending": row.pending,
        "pending_negative": row.pending_negative, "pending_positive": row.pending_positive,
        "posted_negative": row.posted_negative, "posted_positive": row.posted_positive,
    })


@app.get("/api/period-stats")
async def period_stats(date_from: str = "", date_to: str = ""):
    db = next(get_db())
    q = select(
        func.count().label("total"),
        func.sum(case((Review.status == "posted", 1), else_=0)).label("posted"),
        func.sum(case((Review.status == "pending_approval", 1), else_=0)).label("pending"),
    ).where(Review.is_archived == False)  # noqa: E712
    if date_from:
        try:
            q = q.where(Review.review_created_at >= datetime.strptime(date_from, "%Y-%m-%d"))
        except ValueError:
            pass
    if date_to:
        try:
            q = q.where(Review.review_created_at < datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1))
        except ValueError:
            pass
    row = db.execute(q).one()
    db.close()
    return JSONResponse({"total": row.total, "posted": row.posted, "pending": row.pending})


# Редиректы для старых URL
@app.get("/pending")
async def redirect_pending():
    return RedirectResponse(url="/", status_code=301)


@app.get("/review/{review_id}")
async def redirect_review_detail(review_id: int):
    return RedirectResponse(url="/", status_code=301)


# ── Главная страница ──────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def main_page(request: Request):
    db = next(get_db())
    rows = db.execute(select(Review.status, func.count()).group_by(Review.status)).all()
    db.close()
    counts = {row[0]: row[1] for row in rows}
    pending_unarchived = sum(
        v for k, v in counts.items() if k == "pending_approval"
    )
    total = sum(counts.values())
    stats = type("Stats", (), {
        "pending_unarchived": pending_unarchived,
        "posted": counts.get("posted", 0),
        "total": total,
    })()
    return templates.TemplateResponse("app.html", {
        "request": request,
        "stats": stats,
        "active_nav": "reviews",
        "pending_count": pending_unarchived,
    })


# ── htmx-партиалы ─────────────────────────────────────────────

@app.get("/partials/reviews", response_class=HTMLResponse)
async def partial_review_list(
    request: Request,
    tab: str = "pending",       # pending | all
    filter: str = "",           # "" | negative | positive | notext
    status: str = "",           # прямой фильтр по статусу: posted | error | pending_approval | ...
    today: bool = False,        # только опубликованные сегодня
    q: str = "",
    skip: int = 0,
    date_from: str = "",        # фильтр по дате отзыва YYYY-MM-DD
    date_to: str = "",
):
    db = next(get_db())
    query = select(Review).where(Review.is_archived == False)  # noqa: E712

    if status:
        query = query.where(Review.status == status)
    elif tab == "pending":
        query = query.where(Review.status == "pending_approval")

    if today:
        today_start = datetime.combine(date.today(), datetime.min.time())
        query = query.where(Review.posted_at >= today_start)

    if date_from:
        try:
            df = datetime.strptime(date_from, "%Y-%m-%d")
            query = query.where(Review.review_created_at >= df)
        except ValueError:
            pass
    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            query = query.where(Review.review_created_at < dt)
        except ValueError:
            pass

    # Фильтры по рейтингу / тексту
    if filter == "negative":
        query = query.where(Review.rating <= 3)
    elif filter == "positive":
        query = query.where(Review.rating >= 4)
    elif filter == "notext":
        query = query.where(
            (Review.review_text == None) | (Review.review_text == "")  # noqa: E711
        )

    if q:
        like = f"%{q}%"
        query = query.where(
            or_(
                Review.review_text.ilike(like),
                Review.author_name.ilike(like),
                Review.product_name.ilike(like),
            )
        )

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    reviews = db.scalars(
        query.order_by(Review.review_created_at.desc()).offset(skip).limit(PAGE_SIZE)
    ).all()
    db.close()

    has_more = (skip + PAGE_SIZE) < total
    next_skip = skip + PAGE_SIZE

    return templates.TemplateResponse("partials/review_list.html", {
        "request": request,
        "reviews": reviews,
        "tab": tab,
        "filter": filter,
        "status": status,
        "today": today,
        "has_more": has_more,
        "next_skip": next_skip,
        "q": q,
    })


@app.get("/partials/stats", response_class=HTMLResponse)
async def partial_stats(request: Request):
    db = next(get_db())
    rows = db.execute(select(Review.status, func.count()).group_by(Review.status)).all()
    counts = {row[0]: row[1] for row in rows}
    total = sum(counts.values())

    today_start = datetime.combine(date.today(), datetime.min.time())
    posted_today = db.scalar(
        select(func.count()).where(
            Review.status == "posted",
            Review.posted_at >= today_start,
        )
    ) or 0
    fetched_today = db.scalar(
        select(func.count()).where(Review.fetched_at >= today_start)
    ) or 0
    posted_total = counts.get("posted", 0)
    coverage_pct = round(posted_total / total * 100, 1) if total > 0 else 0.0
    pending_unarchived = sum(v for k, v in counts.items() if k == "pending_approval")
    db.close()

    return templates.TemplateResponse("partials/stats.html", {
        "request": request,
        "posted_today": posted_today,
        "fetched_today": fetched_today,
        "coverage_pct": coverage_pct,
        "posted_total": posted_total,
        "total": total,
        "pending_unarchived": pending_unarchived,
        "error_count": counts.get("error", 0),
    })


@app.get("/partials/review/{review_id}", response_class=HTMLResponse)
async def partial_review_detail(request: Request, review_id: int):
    db = next(get_db())
    review = db.get(Review, review_id)
    db.close()
    if not review:
        return HTMLResponse("<p style='padding:24px;color:red'>Отзыв не найден</p>", status_code=404)
    return templates.TemplateResponse("partials/review_detail.html", {
        "request": request,
        "review": review,
    })


# ── Аналитика ────────────────────────────────────────────────

@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    db = next(get_db())
    rows = db.execute(select(Review.status, func.count()).group_by(Review.status)).all()
    db.close()
    pending = sum(v for k, v in rows if k == "pending_approval")
    return templates.TemplateResponse("analytics.html", {
        "request": request,
        "active_nav": "analytics",
        "pending_count": pending,
    })


@app.get("/products", response_class=HTMLResponse)
async def products_page(request: Request):
    db = next(get_db())
    rows = db.execute(select(Review.status, func.count()).group_by(Review.status)).all()
    db.close()
    pending = sum(v for k, v in rows if k == "pending_approval")
    return templates.TemplateResponse("products.html", {
        "request": request,
        "active_nav": "products",
        "pending_count": pending,
    })


@app.get("/api/products")
async def get_products_list():
    from app.services.ozon_products import fetch_products_for_sale
    products = await fetch_products_for_sale()
    return JSONResponse(products)


@app.post("/api/products/refresh")
async def refresh_products():
    from app.services.ozon_products import fetch_products_for_sale, invalidate_cache
    invalidate_cache()
    products = await fetch_products_for_sale()
    return JSONResponse({"ok": True, "count": len(products)})


@app.get("/api/products/archived")
async def get_archived_products():
    from app.services.ozon_products import fetch_archived_products
    products = await fetch_archived_products()
    return JSONResponse(products)


@app.post("/api/products/archived/refresh")
async def refresh_archived_products():
    from app.services.ozon_products import fetch_archived_products, invalidate_archived_cache
    invalidate_archived_cache()
    products = await fetch_archived_products()
    return JSONResponse({"ok": True, "count": len(products)})


# /supplies роуты — в app/routers/supplies.py


@app.get("/api/analytics/summary")
async def analytics_summary(days: int = 28, date_from: str = "", date_to: str = ""):
    from app.services.ozon_analytics import fetch_summary
    try:
        return JSONResponse(await fetch_summary(days, date_from, date_to))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/analytics/sales-by-day")
async def sales_by_day(days: int = 28):
    from app.services.ozon_analytics import fetch_sales_by_day
    try:
        return JSONResponse(await fetch_sales_by_day(days))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/analytics/product-sales")
async def product_sales(days: int = 28, limit: int = 50, date_from: str = "", date_to: str = ""):
    from app.services.ozon_analytics import fetch_product_sales
    try:
        return JSONResponse(await fetch_product_sales(days, limit, date_from, date_to))
    except Exception as exc:
        logging.exception("product_sales error")
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.post("/api/analytics/refresh-stocks")
async def refresh_stocks():
    from app.services.ozon_analytics import refresh_stocks_cache, stocks_cache_info
    try:
        n = await refresh_stocks_cache()
        return JSONResponse({"ok": True, "sku_count": n, **stocks_cache_info()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


@app.get("/api/analytics/stocks-info")
async def stocks_info():
    from app.services.ozon_analytics import stocks_cache_info
    return JSONResponse(stocks_cache_info())


@app.get("/api/analytics/transit-stock")
async def get_transit_stock():
    return JSONResponse(_load_transit())


@app.post("/api/analytics/transit-stock")
async def set_transit_stock(request: Request):
    body = await request.json()
    sku = str(body.get("sku", ""))
    qty = body.get("quantity")
    if not sku:
        return JSONResponse({"error": "sku required"}, status_code=400)
    data = _load_transit()
    if qty is None or qty == "":
        data.pop(sku, None)
    else:
        data[sku] = int(qty)
    _save_transit(data)
    return JSONResponse({"ok": True, "sku": sku, "quantity": data.get(sku)})


@app.get("/api/analytics/delivery-rate-by-day")
async def delivery_rate_by_day(days: int = 28, date_from: str = "", date_to: str = ""):
    from app.services.ozon_analytics import fetch_delivery_rate
    try:
        return JSONResponse(await fetch_delivery_rate(days, date_from, date_to))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/reviews-charts")
async def reviews_charts(days: int = 14, date_from: str = "", date_to: str = ""):
    db = next(get_db())
    if date_from and date_to:
        try:
            start = datetime.strptime(date_from, "%Y-%m-%d")
            end_d = datetime.strptime(date_to, "%Y-%m-%d")
        except ValueError:
            start = datetime.now() - timedelta(days=days - 1)
            end_d = datetime.now()
    else:
        end_d = datetime.now()
        start = end_d - timedelta(days=days - 1)

    end = end_d + timedelta(days=1)

    # Полный список дат периода — чтобы пустые дни показывались как 0
    all_days = []
    cur = start.date()
    while cur <= end_d.date():
        all_days.append(str(cur))
        cur += timedelta(days=1)

    reviews_rows = db.execute(
        select(
            func.date(Review.review_created_at).label("day"),
            func.sum(case((Review.rating >= 4, 1), else_=0)).label("positive"),
            func.sum(case((Review.rating <= 3, 1), else_=0)).label("negative"),
        )
        .where(Review.review_created_at >= start, Review.review_created_at < end)
        .group_by(func.date(Review.review_created_at))
    ).all()

    answers_rows = db.execute(
        select(
            func.date(Review.review_created_at).label("day"),
            func.sum(case((Review.status == "posted", 1), else_=0)).label("answered"),
            func.sum(case((Review.status != "posted", 1), else_=0)).label("unanswered"),
        )
        .where(Review.review_created_at >= start, Review.review_created_at < end)
        .group_by(func.date(Review.review_created_at))
    ).all()

    db.close()

    rev_map = {r.day: r for r in reviews_rows}
    ans_map = {r.day: r for r in answers_rows}

    reviews_out = [
        {"day": d,
         "positive": rev_map[d].positive if d in rev_map else 0,
         "negative": rev_map[d].negative if d in rev_map else 0,
         "no_data": d not in rev_map}
        for d in all_days
    ]
    answers_out = [
        {"day": d,
         "answered": ans_map[d].answered if d in ans_map else 0,
         "unanswered": ans_map[d].unanswered if d in ans_map else 0,
         "no_data": d not in ans_map}
        for d in all_days
    ]

    return JSONResponse({"reviews": reviews_out, "answers": answers_out})


@app.get("/api/analytics/reviews-by-day")
async def reviews_by_day(days: int = 30):
    db = next(get_db())
    start = datetime.now() - timedelta(days=days)
    rows = db.execute(
        select(
            func.date(Review.review_created_at).label("day"),
            func.count().label("total"),
            func.sum(case((Review.rating >= 4, 1), else_=0)).label("positive"),
            func.sum(case((Review.rating <= 3, 1), else_=0)).label("negative"),
        )
        .where(Review.review_created_at >= start)
        .group_by(func.date(Review.review_created_at))
        .order_by(func.date(Review.review_created_at))
    ).all()
    db.close()
    return JSONResponse([
        {"day": r.day, "total": r.total, "positive": r.positive, "negative": r.negative}
        for r in rows
    ])
