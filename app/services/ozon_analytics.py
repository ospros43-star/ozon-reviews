"""
Ozon Seller Analytics — official API (api-seller.ozon.ru).
Uses Client-Id + Api-Key headers (permanent, no cookie needed).
"""
import asyncio
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

BASE = "https://api-seller.ozon.ru"

# ── Кэш остатков ────────────────────────────────────────────────────────────
_stocks_cache: dict[str, dict] = {}
_stocks_updated_at: Optional[datetime] = None
_STOCKS_TTL_HOURS = 2

# ── Короткий кэш ответов (5 мин) ────────────────────────────────────────────
_resp_cache: dict[str, tuple[datetime, Any]] = {}
_RESP_TTL = 300  # секунд


def _cache_get(key: str) -> Any:
    entry = _resp_cache.get(key)
    if entry and (datetime.now() - entry[0]).total_seconds() < _RESP_TTL:
        return entry[1]
    return None


def _cache_set(key: str, value: Any) -> None:
    _resp_cache[key] = (datetime.now(), value)


def _headers() -> dict:
    return {
        "Client-Id": settings.ozon_client_id,
        "Api-Key": settings.ozon_api_key,
        "Content-Type": "application/json",
    }


# ── Stocks cache ─────────────────────────────────────────────────────────────

def stocks_cache_info() -> dict:
    return {
        "size": len(_stocks_cache),
        "updated_at": _stocks_updated_at.isoformat() if _stocks_updated_at else None,
        "stale": _is_stocks_stale(),
    }


def _is_stocks_stale() -> bool:
    if _stocks_updated_at is None:
        return True
    return (datetime.now() - _stocks_updated_at).total_seconds() > _STOCKS_TTL_HOURS * 3600


async def refresh_stocks_cache() -> int:
    global _stocks_cache, _stocks_updated_at
    new_cache: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=60) as client:
        last_id = ""
        while True:
            resp = await client.post(
                f"{BASE}/v4/product/info/stocks",
                headers=_headers(),
                json={"filter": {"visibility": "ALL"}, "last_id": last_id, "limit": 1000},
            )
            if resp.status_code != 200:
                logger.warning("stocks cache: HTTP %s", resp.status_code)
                break
            data = resp.json()
            items = data.get("items", [])
            for item in items:
                stocks_list = item.get("stocks", [])
                if not stocks_list:
                    continue
                sku = str(stocks_list[0].get("sku") or "")
                if not sku:
                    continue
                present = reserved = in_way = 0
                for s in stocks_list:
                    t = s.get("type", "")
                    if t in ("fbo", "fbs", "crossborder"):
                        present += s.get("present", 0)
                        reserved += s.get("reserved", 0)
                    elif t in ("in_way_to_client", "in_way"):
                        in_way += s.get("present", 0)
                new_cache[sku] = {"present": present, "reserved": reserved, "in_way": in_way}
            last_id = data.get("last_id") or ""
            if not last_id or not items:
                break
    _stocks_cache = new_cache
    _stocks_updated_at = datetime.now()
    logger.info("stocks cache refreshed: %d SKU", len(new_cache))
    return len(new_cache)


async def _get_stocks(skus: list[str]) -> dict[str, dict]:
    if _is_stocks_stale():
        await refresh_stocks_cache()
    return {s: _stocks_cache[s] for s in skus if s in _stocks_cache}


# ── Supply transit ────────────────────────────────────────────────────────────

async def fetch_supply_transit() -> dict[str, int]:
    """Возвращает {sku: promised_amount} — товары обещанные к поставке (в пути + приёмка).

    Использует /v2/analytics/stock_on_warehouses → promised_amount, который охватывает
    и заявки в пути, и заявки на приёмке (в отличие от supply-order API).
    """
    cache_key = "supply_transit"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    sku_qty: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=60) as client:
        offset = 0
        while True:
            resp = await client.post(
                f"{BASE}/v2/analytics/stock_on_warehouses",
                headers=_headers(),
                json={"limit": 1000, "offset": offset, "warehouse_type": "ALL"},
            )
            if resp.status_code != 200:
                break
            rows = resp.json().get("result", {}).get("rows", [])
            for r in rows:
                promised = r.get("promised_amount", 0)
                if promised:
                    sku = str(r["sku"])
                    sku_qty[sku] = sku_qty.get(sku, 0) + promised
            offset += 1000
            if len(rows) < 1000:
                break

    _cache_set(cache_key, sku_qty)
    logger.info("supply transit (promised) fetched: %d SKU", len(sku_qty))
    return sku_qty


# ── Summary ───────────────────────────────────────────────────────────────────

async def _fetch_period_totals(date_from: date, date_to: date) -> dict:
    """Суммарные + дневные метрики за период одним запросом."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{BASE}/v1/analytics/data",
            headers=_headers(),
            json={
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "dimension": ["day"],
                "metrics": ["revenue", "ordered_units", "returns", "cancellations", "delivered_units"],
                "sort": [{"key": "day", "order": "ASC"}],
                "limit": 1000,
                "offset": 0,
            },
        )
        if resp.status_code != 200:
            return {}
        rows = resp.json().get("result", {}).get("data", [])

    totals: dict = {"revenue": 0.0, "ordered_units": 0, "returns": 0,
                    "cancellations": 0, "delivered_units": 0, "daily": []}
    for r in rows:
        m = r["metrics"]
        totals["revenue"] += m[0]
        totals["ordered_units"] += int(m[1])
        totals["returns"] += int(m[2])
        totals["cancellations"] += int(m[3])
        totals["delivered_units"] += int(m[4])
        totals["daily"].append({
            "day": r["dimensions"][0]["id"][:10],
            "revenue": m[0],
            "units": int(m[1]),
        })
    return totals


async def fetch_summary(days: int = 28, date_from_str: str = "", date_to_str: str = "") -> dict:
    """Сводка за текущий и предыдущий период + дневные данные для графика."""
    if date_from_str and date_to_str:
        date_from = date.fromisoformat(date_from_str)
        date_to = date.fromisoformat(date_to_str)
        days = (date_to - date_from).days + 1
        cache_key = f"summary:{date_from_str}:{date_to_str}"
    else:
        cache_key = f"summary:{days}"
        date_to = date.today() - timedelta(days=1)
        date_from = date_to - timedelta(days=days - 1)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    prev_to = date_from - timedelta(days=1)
    prev_from = prev_to - timedelta(days=days - 1)

    # Оба периода параллельно
    cur, prev = await asyncio.gather(
        _fetch_period_totals(date_from, date_to),
        _fetch_period_totals(prev_from, prev_to),
    )

    def delta(a, b):
        diff = a - b
        pct = round(diff / b * 100, 1) if b else None
        return {"diff": round(diff, 2), "pct": pct}

    n = days
    result = {
        "period_days": days,
        "revenue": cur.get("revenue", 0),
        "ordered_units": cur.get("ordered_units", 0),
        "cancellations": cur.get("cancellations", 0),
        "returns": cur.get("returns", 0),
        "delivered_units": cur.get("delivered_units", 0),
        "avg_units_day": round(cur.get("ordered_units", 0) / n, 1),
        "delta_revenue": delta(cur.get("revenue", 0), prev.get("revenue", 0)),
        "delta_units": delta(cur.get("ordered_units", 0), prev.get("ordered_units", 0)),
        "delta_cancellations": delta(cur.get("cancellations", 0), prev.get("cancellations", 0)),
        "delta_returns": delta(cur.get("returns", 0), prev.get("returns", 0)),
        "delta_avg_units_day": delta(
            cur.get("ordered_units", 0) / n,
            prev.get("ordered_units", 0) / n,
        ),
        "daily": cur.get("daily", []),  # revenue + units по дням для графика
    }
    _cache_set(cache_key, result)
    return result


# ── Product sales ─────────────────────────────────────────────────────────────

async def fetch_product_sales(days: int = 28, limit: int = 50,
                             date_from_str: str = "", date_to_str: str = "") -> list[dict]:
    """Продажи по SKU с остатками и спарклайнами (только завершённые дни)."""
    if date_from_str and date_to_str:
        date_from = date.fromisoformat(date_from_str)
        date_to = date.fromisoformat(date_to_str)
        days = (date_to - date_from).days + 1
        cache_key = f"product_sales:{date_from_str}:{date_to_str}:{limit}"
    else:
        cache_key = f"product_sales:{days}:{limit}"
        date_to = date.today() - timedelta(days=1)
        date_from = date_to - timedelta(days=days - 1)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    all_days = [(date_from + timedelta(i)).isoformat() for i in range(days)]

    async with httpx.AsyncClient(timeout=60) as client:
        sku_resp, daily_resp = await asyncio.gather(
            client.post(f"{BASE}/v1/analytics/data", headers=_headers(), json={
                "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
                "dimension": ["sku"],
                "metrics": ["revenue", "ordered_units", "returns"],
                "sort": [{"key": "revenue", "order": "DESC"}],
                "limit": limit, "offset": 0,
            }),
            client.post(f"{BASE}/v1/analytics/data", headers=_headers(), json={
                "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
                "dimension": ["sku", "day"],
                "metrics": ["ordered_units", "revenue"],
                "sort": [{"key": "ordered_units", "order": "DESC"}],
                "limit": 1000, "offset": 0,
            }),
        )

    sku_resp.raise_for_status()
    rows = sku_resp.json().get("result", {}).get("data", [])
    if not rows:
        return []

    skus = [r["dimensions"][0]["id"] for r in rows]
    skus_set = set(skus)

    # Разбираем дневные данные
    daily_by_sku: dict[str, list[dict]] = {s: [] for s in skus}
    if daily_resp.status_code == 200:
        tmp: dict[str, dict[str, dict]] = defaultdict(dict)
        for dr in daily_resp.json().get("result", {}).get("data", []):
            s = dr["dimensions"][0]["id"]
            if s not in skus_set:
                continue
            tmp[s][dr["dimensions"][1]["id"][:10]] = {
                "u": int(dr["metrics"][0]),
                "r": round(dr["metrics"][1]),
            }
        for s in skus:
            daily_by_sku[s] = [tmp[s].get(d, {"u": 0, "r": 0}) for d in all_days]

    # Названия/картинки, остатки и транзит — параллельно
    (names, images), stocks, transit = await asyncio.gather(
        _fetch_product_info(skus),
        _get_stocks(skus),
        fetch_supply_transit(),
    )

    result = []
    for r in rows:
        sku = r["dimensions"][0]["id"]
        revenue = r["metrics"][0]
        units = int(r["metrics"][1])
        stock_info = stocks.get(sku, {})
        result.append({
            "sku": sku,
            "name": names.get(sku, r["dimensions"][0].get("name", "")),
            "image": images.get(sku),
            "revenue": revenue,
            "units": units,
            "returns": int(r["metrics"][2]),
            "avg_day": round(revenue / days, 2) if days > 0 else 0,
            "stock": stock_info.get("present") if stock_info else None,
            "in_way": stock_info.get("in_way") if stock_info else None,
            "reserved": stock_info.get("reserved") if stock_info else None,
            "transit": transit.get(sku) or None,
            "daily": daily_by_sku.get(sku, []),
        })

    _cache_set(cache_key, result)
    return result


# ── Product info ──────────────────────────────────────────────────────────────

async def _fetch_product_info(skus: list[str]) -> tuple[dict, dict]:
    names: dict[str, str] = {}
    images: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(skus), 100):
            batch = skus[i:i + 100]
            resp = await client.post(
                f"{BASE}/v3/product/info/list",
                headers=_headers(),
                json={"sku": [int(s) for s in batch if s.isdigit()]},
            )
            if resp.status_code != 200:
                continue
            for item in resp.json().get("items", []):
                sku = str(item.get("sku") or "")
                if not sku:
                    continue
                names[sku] = item.get("name") or ""
                primary = item.get("primary_image") or []
                imgs = item.get("images", [])
                images[sku] = primary[0] if primary else (imgs[0] if imgs else None)
    return names, images


# ── Local sales share ─────────────────────────────────────────────────────────

async def fetch_delivery_rate(days: int = 28, date_from_str: str = "", date_to_str: str = "") -> list[dict]:
    """Доля доставленных заказов по дням (%).

    delivered_units / ordered_units * 100 — показывает какой % заказов
    дошёл до покупателя (без отмен и возвратов в пути).
    """
    if date_from_str and date_to_str:
        date_from = date.fromisoformat(date_from_str)
        date_to = date.fromisoformat(date_to_str)
        cache_key = f"delivery_rate:{date_from_str}:{date_to_str}"
    else:
        cache_key = f"delivery_rate:{days}"
        date_to = date.today() - timedelta(days=1)
        date_from = date_to - timedelta(days=days - 1)

    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{BASE}/v1/analytics/data",
            headers=_headers(),
            json={
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "dimension": ["day"],
                "metrics": ["ordered_units", "delivered_units", "cancellations", "returns"],
                "sort": [{"key": "day", "order": "ASC"}],
                "limit": 1000,
                "offset": 0,
            },
        )

    if resp.status_code != 200:
        logger.warning("delivery_rate: HTTP %s", resp.status_code)
        return []

    rows = resp.json().get("result", {}).get("data", [])
    result = []
    for r in rows:
        ordered = int(r["metrics"][0]) or 0
        delivered = int(r["metrics"][1]) or 0
        cancelled = int(r["metrics"][2]) or 0
        returned = int(r["metrics"][3]) or 0
        rate = round(delivered / ordered * 100, 1) if ordered > 0 else 0.0
        result.append({
            "day": r["dimensions"][0]["id"][:10],
            "rate": rate,
            "delivered": delivered,
            "ordered": ordered,
            "cancelled": cancelled,
            "returned": returned,
        })

    _cache_set(cache_key, result)
    logger.info("delivery_rate fetched: %d days", len(result))
    return result
