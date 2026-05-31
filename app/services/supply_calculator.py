"""Расчёт плана поставки по кластерам."""
import logging
from datetime import datetime, timedelta

import httpx

from app.config import settings

logger = logging.getLogger(__name__)
BASE = "https://api-seller.ozon.ru"

CLUSTERS = [
    # Москва: убраны ПУШКИН (путал с ПУШКИНО→МО), добавлены все реальные МО-склады
    {"id": "msk",  "name": "Москва и МО",       "kw": ["ГРИВНО","ЖУКОВСКИЙ","ДОМОДЕДОВО","МОЛЖАНИНОВО","ЭЛЕКТРОСТАЛЬ","МОСКВА","ТВЕРЬ","ПОДОЛЬСК","НОГИНСК","ПЕТРОВСКОЕ","ХОРУГВИНО","ЯРОСЛАВЛЬ","СОФЬИНО","ПУШКИНО"]},
    # СПБ: убран ПУШКИН (его нет среди СПБ-складов), добавлен ПЕТЕРБУРГ для "Санкт_Петербург_РФЦ"
    {"id": "spb",  "name": "Санкт-Петербург",   "kw": ["ПЕТЕРБУРГ","СПБ","ШУШАРЫ","КОЛПИНО","БУГРЫ"]},
    # Екатеринбург: убраны ПЕРМЬ/ТЮМЕНЬ/УФА — теперь свои кластеры
    {"id": "ekb",  "name": "Екатеринбург",       "kw": ["ЕКАТЕРИНБУРГ","ЧЕЛЯБИНСК"]},
    {"id": "nsk",  "name": "Новосибирск",        "kw": ["НОВОСИБИРСК","ОМСК","БАРНАУЛ","ТОМСК"]},
    {"id": "krd",  "name": "Краснодар",          "kw": ["КРАСНОДАР","АДЫГЕЙСК","НЕВИННОМЫССК","НОВОРОССИЙСК"]},
    # Казань: убраны САМАРА/САРАТОВ — теперь свои кластеры
    {"id": "kzn",  "name": "Казань",             "kw": ["КАЗАНЬ","НИЖНИЙ","ЧЕБОКСАРЫ"]},
    {"id": "rnd",  "name": "Ростов-на-Дону",     "kw": ["РОСТОВ"]},
    {"id": "krs",  "name": "Красноярск",         "kw": ["КРАСНОЯРСК","ИРКУТСК","СТАРЦЕВО"]},
    {"id": "vrn",  "name": "Воронеж",            "kw": ["ВОРОНЕЖ"]},
    {"id": "vgd",  "name": "Волгоград",          "kw": ["ВОЛГОГРАД","АСТРАХАНЬ"]},
    {"id": "mhk",  "name": "Махачкала",          "kw": ["МАХАЧКАЛА","СТАВРОПОЛЬ"]},
    {"id": "klg",  "name": "Калининград",        "kw": ["КАЛИНИНГРАД"]},
    {"id": "far",  "name": "Дальний Восток",     "kw": ["ХАБАРОВСК","ВЛАДИВОСТОК","ЯКУТСК"]},
    {"id": "sng",  "name": "СНГ",               "kw": ["АЛМАТЫ","АСТАНА","МИНСК","ЕРЕВАН","БАКУ","БИШКЕК"]},
    # Новые кластеры (отдельные РФЦ)
    {"id": "prm",  "name": "Пермь",             "kw": ["ПЕРМЬ"]},
    {"id": "sam",  "name": "Самара",             "kw": ["САМАРА"]},
    {"id": "tym",  "name": "Тюмень",            "kw": ["ТЮМЕНЬ"]},
    {"id": "ufa",  "name": "Уфа",               "kw": ["УФА","ОРЕНБУРГ"]},
    {"id": "sar",  "name": "Саратов",           "kw": ["САРАТОВ"]},
]

CLUSTER_BY_ID = {c["id"]: c for c in CLUSTERS}

# TTL-кеш для сырых данных API (5 минут)
_data_cache: dict = {}
_data_cache_ts: dict = {}
_CACHE_TTL = 300


def warehouse_to_cluster_id(name: str) -> str | None:
    upper = name.upper()
    for c in CLUSTERS:
        if any(kw in upper for kw in c["kw"]):
            return c["id"]
    return None


def _headers(key: str | None = None) -> dict:
    return {
        "Client-Id": settings.ozon_client_id,
        "Api-Key": key or settings.ozon_api_key,
        "Content-Type": "application/json",
    }


async def _fetch_raw_data(analysis_days: int) -> tuple[list, dict, dict]:
    """Забирает остатки, продажи и инфо о товарах; кеширует на 5 минут."""
    now = datetime.now()
    if (
        analysis_days in _data_cache_ts
        and (now - _data_cache_ts[analysis_days]).total_seconds() < _CACHE_TTL
    ):
        logger.info("supply_calculator: cache hit (analysis_days=%d)", analysis_days)
        return _data_cache[analysis_days]

    async with httpx.AsyncClient(timeout=60) as client:
        # 1. Остатки по складам
        stock_rows: list[dict] = []
        offset = 0
        while True:
            r = await client.post(
                f"{BASE}/v2/analytics/stock_on_warehouses",
                headers=_headers(),
                json={"limit": 1000, "offset": offset, "warehouse_type": "ALL"},
            )
            if r.status_code != 200:
                logger.warning("stock_on_warehouses: %d", r.status_code)
                break
            batch = r.json().get("result", {}).get("rows", [])
            stock_rows.extend(batch)
            if len(batch) < 1000:
                break
            offset += 1000

        # 2. Продажи за period
        date_to = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        date_from = (now - timedelta(days=analysis_days)).strftime("%Y-%m-%d")
        sr = await client.post(
            f"{BASE}/v1/analytics/data",
            headers=_headers(),
            json={
                "date_from": date_from,
                "date_to": date_to,
                "metrics": ["ordered_units"],
                "dimension": ["sku"],
                "limit": 1000,
                "offset": 0,
            },
        )
        sales_total: dict[str, int] = {}
        if sr.status_code == 200:
            for item in sr.json().get("result", {}).get("data", []):
                sku = str(item.get("dimensions", [{}])[0].get("id", ""))
                units = int(item.get("metrics", [0])[0] or 0)
                if sku:
                    sales_total[sku] = units

        # 3. Инфо о товарах
        all_skus = list({str(row["sku"]) for row in stock_rows} | set(sales_total.keys()))
        info_by_sku: dict[str, dict] = {}
        for i in range(0, len(all_skus), 100):
            batch_skus = [s for s in all_skus[i:i+100] if s.isdigit()]
            if not batch_skus:
                continue
            ir = await client.post(
                f"{BASE}/v3/product/info/list",
                headers=_headers(),
                json={"product_id": [int(s) for s in batch_skus]},
            )
            if ir.status_code == 200:
                for item in ir.json().get("items", []):
                    sku_str = str(item.get("id", ""))
                    info_by_sku[sku_str] = {
                        "name": item.get("name", ""),
                        "offer_id": item.get("offer_id", ""),
                        "image": (item.get("primary_image") or item.get("images") or [None])[0],
                    }

    logger.info(
        "supply_calculator: fetched %d stock rows, %d sales, %d products",
        len(stock_rows), len(sales_total), len(info_by_sku),
    )
    result = (stock_rows, sales_total, info_by_sku)
    _data_cache[analysis_days] = result
    _data_cache_ts[analysis_days] = now
    return result


async def fetch_product_analytics(analysis_days: int) -> list[dict]:
    """Возвращает список товаров с аналитикой для страницы выбора."""
    stock_rows, sales_total, info_by_sku = await _fetch_raw_data(analysis_days)

    sku_agg: dict[str, dict] = {}
    for row in stock_rows:
        sku = str(row["sku"])
        if sku not in sku_agg:
            sku_agg[sku] = {"total": 0, "in_way": 0}
        sku_agg[sku]["total"] += row.get("free_to_sell_amount", 0) or 0
        sku_agg[sku]["in_way"] += row.get("in_way_amount", 0) or 0

    all_skus = set(sku_agg.keys()) | set(sales_total.keys())
    results: list[dict] = []

    for sku in all_skus:
        info = info_by_sku.get(sku, {})
        if not info.get("name"):
            continue
        agg = sku_agg.get(sku, {"total": 0, "in_way": 0})
        orders = sales_total.get(sku, 0)
        daily = orders / analysis_days if orders > 0 else 0
        turnover = round(agg["total"] / daily) if daily > 0 and agg["total"] > 0 else None
        results.append({
            "sku": sku,
            "offer_id": info.get("offer_id", sku),
            "name": info.get("name", f"SKU {sku}"),
            "image": info.get("image"),
            "total_stock": agg["total"],
            "in_way": agg["in_way"],
            "orders_period": orders,
            "daily_sales": round(daily, 2),
            "turnover_days": turnover,
        })

    results.sort(key=lambda x: -(x["orders_period"] or 0))
    return results


async def calculate_supply(
    delivery_date: datetime,
    source_cluster_id: str,
    target_cluster_ids: list[str],
    analysis_days: int,
    target_days: int,
    selected_skus: list[str] | None = None,
) -> list[dict]:
    """
    Рассчитывает сколько единиц каждого товара нужно отправить на выбранные кластеры.
    Если selected_skus указаны — считает только по ним.
    """
    stock_rows, sales_total, info_by_sku = await _fetch_raw_data(analysis_days)

    # Группируем остатки по кластерам
    stock_by_cluster: dict[str, dict[str, int]] = {cid: {} for cid in target_cluster_ids}
    for row in stock_rows:
        sku = str(row["sku"])
        cid = warehouse_to_cluster_id(row["warehouse_name"])
        if cid not in target_cluster_ids:
            continue
        free = row.get("free_to_sell_amount", 0) or 0
        stock_by_cluster[cid][sku] = stock_by_cluster[cid].get(sku, 0) + free

    transit_map = _transit_days()

    relevant_skus: set[str] = set(sales_total.keys())
    for cid in target_cluster_ids:
        relevant_skus.update(stock_by_cluster[cid].keys())

    if selected_skus:
        relevant_skus &= set(selected_skus)

    results: list[dict] = []
    for sku in relevant_skus:
        total_sales = sales_total.get(sku, 0)
        daily_sales = total_sales / analysis_days if total_sales > 0 else 0

        if daily_sales == 0:
            continue

        by_cluster: dict[str, dict] = {}
        total_need = 0

        for cid in target_cluster_ids:
            transit = transit_map.get(source_cluster_id, {}).get(cid, 3)
            current = stock_by_cluster[cid].get(sku, 0)
            days_until = max(0, (delivery_date - datetime.now()).days)
            stock_at_delivery = max(0, current - daily_sales * (days_until + transit))
            needed = max(0, round(target_days * daily_sales - stock_at_delivery))

            by_cluster[cid] = {
                "cluster_name": CLUSTER_BY_ID[cid]["name"],
                "current_stock": current,
                "daily_sales": round(daily_sales, 2),
                "days_left": round(current / daily_sales) if daily_sales > 0 else None,
                "needed": needed,
            }
            total_need += needed

        if total_need == 0 and not selected_skus:
            continue

        info = info_by_sku.get(sku, {})
        results.append({
            "sku": sku,
            "offer_id": info.get("offer_id", sku),
            "name": info.get("name", f"SKU {sku}"),
            "image": info.get("image"),
            "daily_sales": round(daily_sales, 2),
            "total_needed": total_need,
            "by_cluster": by_cluster,
        })

    results.sort(key=lambda x: -x["total_needed"])
    return results


def _transit_days() -> dict[str, dict[str, int]]:
    # Примерное время в пути (дни) source → target
    base = {
        "msk": {"msk":1,"spb":2,"ekb":3,"nsk":4,"krd":2,"kzn":2,"rnd":2,"krs":5,"vrn":1,"vgd":2,"mhk":3,"klg":3,"far":7,"sng":5,"prm":3,"sam":2,"tym":4,"ufa":3,"sar":2},
        "spb": {"msk":2,"spb":1,"ekb":3,"nsk":5,"krd":3,"kzn":3,"rnd":3,"krs":5,"vrn":2,"vgd":3,"mhk":4,"klg":2,"far":8,"sng":6,"prm":3,"sam":3,"tym":4,"ufa":4,"sar":3},
        "ekb": {"msk":3,"spb":3,"ekb":1,"nsk":2,"krd":4,"kzn":2,"rnd":4,"krs":3,"vrn":3,"vgd":3,"mhk":4,"klg":5,"far":5,"sng":4,"prm":1,"sam":2,"tym":1,"ufa":1,"sar":3},
        "nsk": {"msk":4,"spb":5,"ekb":2,"nsk":1,"krd":5,"kzn":3,"rnd":5,"krs":2,"vrn":4,"vgd":4,"mhk":5,"klg":6,"far":3,"sng":3,"prm":3,"sam":3,"tym":2,"ufa":3,"sar":4},
        "krd": {"msk":2,"spb":3,"ekb":4,"nsk":5,"krd":1,"kzn":3,"rnd":1,"krs":6,"vrn":2,"vgd":2,"mhk":2,"klg":4,"far":8,"sng":4,"prm":4,"sam":3,"tym":5,"ufa":3,"sar":2},
        "prm": {"msk":3,"spb":3,"ekb":1,"nsk":3,"krd":4,"kzn":2,"rnd":4,"krs":4,"vrn":3,"vgd":3,"mhk":4,"klg":5,"far":6,"sng":5,"prm":1,"sam":2,"tym":2,"ufa":2,"sar":3},
        "sam": {"msk":2,"spb":3,"ekb":2,"nsk":3,"krd":3,"kzn":1,"rnd":2,"krs":4,"vrn":2,"vgd":2,"mhk":3,"klg":4,"far":7,"sng":4,"prm":2,"sam":1,"tym":3,"ufa":2,"sar":1},
        "ufa": {"msk":3,"spb":4,"ekb":1,"nsk":3,"krd":3,"kzn":2,"rnd":3,"krs":4,"vrn":3,"vgd":3,"mhk":3,"klg":5,"far":6,"sng":4,"prm":2,"sam":2,"tym":2,"ufa":1,"sar":3},
    }
    # Для кластеров без явного источника используем среднее значение 3 дня
    all_ids = [c["id"] for c in CLUSTERS]
    default = {tid: 3 for tid in all_ids}
    for src, targets in base.items():
        for tid in all_ids:
            if tid not in targets:
                base[src][tid] = 3
        base[src][src] = 1
    for src in all_ids:
        if src not in base:
            base[src] = {**default, src: 1}
    return base
