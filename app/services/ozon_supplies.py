"""Ozon FBO Supply Orders — список заявок на поставку из официального API."""
import logging
from datetime import datetime, timezone, timedelta

import httpx

from app.config import settings

logger = logging.getLogger(__name__)
BASE = "https://api-seller.ozon.ru"

_CACHE: dict | None = None
_CACHED_AT: datetime | None = None
_CACHE_TTL = timedelta(minutes=15)

# Маппинг числовых состояний → читаемые названия
_STATES = {
    1: "Заполнение данных",
    2: "Принята",
    3: "Ожидает поставки",
    4: "Принята на складе",
    5: "На сортировке",
    6: "Сортировка завершена",
    7: "Доставляется",
    8: "Доставлена",
    9: "Отменена",
}

_STATE_LABELS = {
    "DATA_FILLING":      ("Заполнение данных", "blue"),
    "ACCEPTED":          ("Принята Ozon", "green"),
    "AWAITING_SUPPLY":   ("Ожидает поставки", "orange"),
    "IN_WAREHOUSE":      ("На складе", "green"),
    "IN_SORT":           ("На сортировке", "blue"),
    "SORTED":            ("Отсортирована", "green"),
    "DELIVERING":        ("В пути", "blue"),
    "DELIVERED":         ("Доставлена", "green"),
    "CANCELLED":         ("Отменена", "grey"),
}

# cluster_id → название
_CLUSTERS: dict[str, str] = {}


def _headers() -> dict:
    return {
        "Client-Id": settings.ozon_client_id,
        "Api-Key": settings.ozon_api_key,
        "Content-Type": "application/json",
    }


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        msk = dt.astimezone(timezone(timedelta(hours=3)))
        return msk.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return iso[:16]


async def fetch_supply_orders() -> dict:
    global _CACHE, _CACHED_AT
    if _CACHE and _CACHED_AT and (datetime.now() - _CACHED_AT) < _CACHE_TTL:
        return _CACHE

    async with httpx.AsyncClient(timeout=30) as client:
        # 1. Получаем все ID заявок
        all_ids: list[int] = []
        last_id = "0"
        while True:
            r = await client.post(
                f"{BASE}/v3/supply-order/list",
                headers=_headers(),
                json={
                    "limit": 50,
                    "sort_by": 1,
                    "sort_direction": 2,  # DESC — сначала новые
                    "filter": {"states": [1, 2, 3, 4, 5, 6, 7, 8, 9]},
                    **({"last_id": last_id} if last_id != "0" else {}),
                },
            )
            if r.status_code != 200:
                logger.error("supply-order/list error %d: %s", r.status_code, r.text[:200])
                break
            data = r.json()
            ids = data.get("order_ids", [])
            all_ids.extend(ids)
            last_id = data.get("last_id", "")
            if not ids or len(ids) < 50:
                break

        if not all_ids:
            result = {"orders": [], "updated_at": datetime.now().isoformat(), "total": 0}
            _CACHE = result
            _CACHED_AT = datetime.now()
            return result

        # 2. Получаем детали заказов батчами по 50
        orders_raw: list[dict] = []
        for i in range(0, len(all_ids), 50):
            batch = all_ids[i:i+50]
            r = await client.post(
                f"{BASE}/v3/supply-order/get",
                headers=_headers(),
                json={"order_ids": batch},
            )
            if r.status_code == 200:
                orders_raw.extend(r.json().get("orders", []))

    # 3. Форматируем
    orders = []
    for o in orders_raw:
        state_raw = o.get("state", "")
        label, color = _STATE_LABELS.get(state_raw, (state_raw, "grey"))

        supply = o.get("supplies", [{}])[0] if o.get("supplies") else {}
        cluster_id = supply.get("macrolocal_cluster_id", "")

        timeslot = o.get("timeslot", {}).get("timeslot", {})
        slot_from = _fmt_dt(timeslot.get("from"))
        slot_to_raw = timeslot.get("to", "")
        slot_time = slot_to_raw[11:16] if len(slot_to_raw) >= 16 else ""

        deadline = _fmt_dt(o.get("data_filling_deadline"))
        warehouse = o.get("drop_off_warehouse", {})

        orders.append({
            "order_id": o.get("order_id"),
            "order_number": o.get("order_number", ""),
            "state": state_raw,
            "state_label": label,
            "state_color": color,
            "created": _fmt_dt(o.get("created_date")),
            "deadline": deadline,
            "slot_from": slot_from,
            "slot_time": slot_time,
            "warehouse_name": warehouse.get("name", "").replace("_", " "),
            "warehouse_address": warehouse.get("address", ""),
            "cluster_id": cluster_id,
            "supply_id": supply.get("supply_id"),
            "is_super_fbo": o.get("order_tags", {}).get("is_super_fbo", False),
        })

    # Сортировка: активные первыми
    order_priority = {"DATA_FILLING": 0, "ACCEPTED": 1, "AWAITING_SUPPLY": 2,
                      "IN_WAREHOUSE": 3, "DELIVERING": 4, "DELIVERED": 5, "CANCELLED": 9}
    orders.sort(key=lambda x: order_priority.get(x["state"], 5))

    result = {
        "orders": orders,
        "updated_at": datetime.now().isoformat(),
        "total": len(orders),
        "active": sum(1 for o in orders if o["state"] not in ("CANCELLED", "DELIVERED")),
    }
    _CACHE = result
    _CACHED_AT = datetime.now()
    return result


def invalidate_cache() -> None:
    global _CACHE, _CACHED_AT
    _CACHE = None
    _CACHED_AT = None
