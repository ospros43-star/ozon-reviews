"""Ozon Seller Products — official API."""
import asyncio
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)
BASE = "https://api-seller.ozon.ru"

_products_cache: list[dict] = []
_products_cached = False
_archived_cache: list[dict] = []
_archived_cached = False


def _headers() -> dict:
    return {
        "Client-Id": settings.ozon_client_id,
        "Api-Key": settings.ozon_api_key,
        "Content-Type": "application/json",
    }


async def fetch_products_for_sale() -> list[dict]:
    global _products_cache, _products_cached
    if _products_cached:
        return _products_cache

    async with httpx.AsyncClient(timeout=30) as client:
        # 1. Получаем список товаров в продаже
        product_ids = []
        last_id = ""
        while True:
            resp = await client.post(
                f"{BASE}/v3/product/list",
                headers=_headers(),
                json={"filter": {"visibility": "FOR_SALE"}, "last_id": last_id, "limit": 100},
            )
            if resp.status_code != 200:
                break
            data = resp.json().get("result", {})
            items = data.get("items", [])
            product_ids.extend(i["product_id"] for i in items)
            last_id = data.get("last_id", "")
            if not last_id or len(items) < 100:
                break

        if not product_ids:
            return []

        # 2. Детальная информация о товарах (батчами по 100)
        info_map: dict[int, dict] = {}
        for i in range(0, len(product_ids), 100):
            batch = product_ids[i:i+100]
            r = await client.post(
                f"{BASE}/v3/product/info/list",
                headers=_headers(),
                json={"product_id": batch},
            )
            if r.status_code == 200:
                for item in r.json().get("items", []):
                    info_map[item["id"]] = item

        # 3. Цены
        price_map: dict[int, float] = {}
        for i in range(0, len(product_ids), 100):
            batch = product_ids[i:i+100]
            r = await client.post(
                f"{BASE}/v5/product/info/prices",
                headers=_headers(),
                json={"filter": {"product_id": batch}, "last_id": "", "limit": 100},
            )
            if r.status_code == 200:
                for item in r.json().get("items", []):
                    price_raw = item.get("price", {})
                    price_val = price_raw.get("marketing_seller_price") or price_raw.get("price") or 0
                    price_map[item["product_id"]] = float(price_val)

        # 4. Остатки из существующего кэша
        from app.services.ozon_analytics import _stocks_cache
        # SKU для товара — берём из stocks cache
        # Строим маппинг offer_id → stock через product info
        sku_map: dict[int, str] = {}
        for pid, info in info_map.items():
            for bc in info.get("barcodes", []):
                if bc.startswith("OZN"):
                    sku = bc[3:]
                    sku_map[pid] = sku
                    break

    result = []
    for pid in product_ids:
        info = info_map.get(pid, {})
        sku = sku_map.get(pid, "")
        stock_info = _stocks_cache.get(sku, {}) if sku else {}
        result.append({
            "id": pid,
            "name": info.get("name", ""),
            "offer_id": info.get("offer_id", ""),
            "image": (info.get("primary_image") or info.get("images") or [None])[0],
            "price": price_map.get(pid, 0),
            "stock": stock_info.get("present", 0),
            "reserved": stock_info.get("reserved", 0),
            "sku": sku,
        })

    # 5. Средний рейтинг из нашей базы отзывов (всё время + за 7 дней)
    from datetime import datetime, timedelta
    from app.database import SessionLocal
    from app.models.review import Review as ReviewModel
    from sqlalchemy import select as _select, func as _func
    db = SessionLocal()
    cutoff_7d = datetime.now() - timedelta(days=7)
    try:
        rating_rows = db.execute(
            _select(
                ReviewModel.product_id,
                _func.avg(ReviewModel.rating).label("avg_rating"),
                _func.count().label("review_count"),
            ).group_by(ReviewModel.product_id)
        ).all()
        rating_map = {r.product_id: (round(r.avg_rating, 1), r.review_count) for r in rating_rows}

        rating_7d_rows = db.execute(
            _select(
                ReviewModel.product_id,
                _func.avg(ReviewModel.rating).label("avg_rating"),
                _func.count().label("review_count"),
            )
            .where(ReviewModel.review_created_at >= cutoff_7d)
            .group_by(ReviewModel.product_id)
        ).all()
        rating_7d_map = {r.product_id: (round(r.avg_rating, 1), r.review_count) for r in rating_7d_rows}
    finally:
        db.close()

    for p in result:
        sku = p["sku"]
        avg, cnt = rating_map.get(sku, (None, 0))
        p["avg_rating"] = avg
        p["review_count"] = cnt
        avg7, cnt7 = rating_7d_map.get(sku, (None, 0))
        p["avg_rating_7d"] = avg7
        p["review_count_7d"] = cnt7

    result.sort(key=lambda x: x["stock"], reverse=True)
    _products_cache = result
    _products_cached = True
    logger.info("Products fetched: %d for sale", len(result))
    return result


async def fetch_archived_products() -> list[dict]:
    global _archived_cache, _archived_cached
    if _archived_cached:
        return _archived_cache

    async with httpx.AsyncClient(timeout=30) as client:
        product_ids = []
        last_id = ""
        while True:
            resp = await client.post(
                f"{BASE}/v3/product/list",
                headers=_headers(),
                json={"filter": {"visibility": "ARCHIVED"}, "last_id": last_id, "limit": 100},
            )
            if resp.status_code != 200:
                break
            data = resp.json().get("result", {})
            items = data.get("items", [])
            product_ids.extend(i["product_id"] for i in items)
            last_id = data.get("last_id", "")
            if not last_id or len(items) < 100:
                break

        if not product_ids:
            _archived_cache = []
            _archived_cached = True
            return []

        info_map: dict[int, dict] = {}
        for i in range(0, len(product_ids), 100):
            batch = product_ids[i:i+100]
            r = await client.post(
                f"{BASE}/v3/product/info/list",
                headers=_headers(),
                json={"product_id": batch},
            )
            if r.status_code == 200:
                for item in r.json().get("items", []):
                    info_map[item["id"]] = item

    result = []
    for pid in product_ids:
        info = info_map.get(pid, {})
        sku = ""
        for bc in info.get("barcodes", []):
            if bc.startswith("OZN"):
                sku = bc[3:]
                break
        result.append({
            "id": pid,
            "name": info.get("name", ""),
            "offer_id": info.get("offer_id", ""),
            "image": (info.get("primary_image") or info.get("images") or [None])[0],
            "sku": sku,
            "price": 0,
            "stock": 0,
            "reserved": 0,
            "avg_rating": None,
            "review_count": 0,
        })

    from app.database import SessionLocal
    from app.models.review import Review as ReviewModel
    from sqlalchemy import select as _select, func as _func
    db = SessionLocal()
    try:
        rating_rows = db.execute(
            _select(
                ReviewModel.product_id,
                _func.avg(ReviewModel.rating).label("avg_rating"),
                _func.count().label("review_count"),
            ).group_by(ReviewModel.product_id)
        ).all()
        rating_map = {r.product_id: (round(r.avg_rating, 1), r.review_count) for r in rating_rows}
    finally:
        db.close()

    for p in result:
        avg, cnt = rating_map.get(p["sku"], (None, 0))
        p["avg_rating"] = avg
        p["review_count"] = cnt

    _archived_cache = result
    _archived_cached = True
    logger.info("Archived products fetched: %d", len(result))
    return result


def invalidate_cache():
    global _products_cached
    _products_cached = False


def invalidate_archived_cache():
    global _archived_cached
    _archived_cached = False
