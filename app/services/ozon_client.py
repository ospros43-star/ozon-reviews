"""
Ozon seller cabinet web client — cookie-based auth.

Endpoints:
  GET reviews:  POST https://seller.ozon.ru/api/review/list
  POST reply:   POST https://seller.ozon.ru/api/review/comment/create

Auth: Cookie header from active Safari/Chrome session on seller.ozon.ru
Required headers: Sec-Fetch-*, x-o3-company-id, x-o3-app-name, x-o3-language, x-o3-page-type

Total reviews: ~105,848 (1059 pages × 100).
Strategy: on first run load INITIAL_PAGES (newest). On subsequent polls: page 1 only.
"""
from datetime import datetime, timedelta, timezone

_MSK = timezone(timedelta(hours=3))

import httpx

from app.config import settings

REVIEW_MAX_LENGTH = 1000
BASE = "https://seller.ozon.ru"


class OzonAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Ozon error {status_code}: {message}")


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Origin": BASE,
        "Referer": f"{BASE}/app/reviews",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.5 Safari/605.1.15"
        ),
        "x-o3-company-id": settings.ozon_client_id,
        "x-o3-app-name": "seller-ui",
        "x-o3-language": "ru",
        "x-o3-page-type": "review",
        "Cookie": settings.ozon_cookie,
    }


def _parse_review(raw: dict) -> dict:
    text_block = raw.get("text") or {}
    parts = []
    if text_block.get("positive"):
        parts.append(f"Плюсы: {text_block['positive']}")
    if text_block.get("negative"):
        parts.append(f"Минусы: {text_block['negative']}")
    if text_block.get("comment"):
        parts.append(text_block["comment"])
    text = " / ".join(parts).strip()

    product = raw.get("product") or {}
    sku = str(raw.get("sku") or "")

    # comments_amount > 0 означает что продавец уже отвечал на этот отзыв
    has_seller_reply = int(raw.get("comments_amount") or raw.get("comments_count") or 0) > 0

    return {
        "review_id": raw.get("uuid", ""),       # UUID — для отправки ответа
        "review_uuid": raw.get("uuid", ""),
        "review_numeric_id": str(raw.get("id", "")),
        "product_id": sku,
        "product_name": product.get("title") or "",
        "author_name": raw.get("author_name") or "",
        "rating": int(raw.get("rating") or 0),
        "review_text": text,
        "review_created_at": raw.get("published_at") or raw.get("created_at"),
        "image_url": None,
        "ozon_sku": sku,
        "ozon_product_url": product.get("url") or (f"https://www.ozon.ru/product/{sku}/" if sku else None),
        "purchase_verified": None,
        "has_seller_reply": has_seller_reply,   # True = уже есть ответ на Ozon
    }


async def get_total_info() -> dict:
    """Возвращает total_items и page_count для информации."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{BASE}/api/review/list",
            headers=_headers(),
            json={"company_id": settings.ozon_client_id, "page": 1, "pageSize": 1},
        )
        if resp.status_code != 200:
            raise OzonAPIError(resp.status_code, resp.text[:200])
        data = resp.json()
    return {
        "total_items": data.get("total_items", 0),
        "page_count": data.get("page_count", 0),
    }


async def fetch_page(
    page: int, page_size: int = 100,
    date_from: str = "", date_to: str = "",
    client: httpx.AsyncClient | None = None,
) -> tuple[list[dict], int, int]:
    """
    Загружает одну страницу отзывов.
    Возвращает (reviews, total_items, page_count).
    client — можно передать постоянный клиент (для длинного импорта)
             чтобы Set-Cookie автоматически подхватывались между запросами.
    """
    body: dict = {
        "company_id": settings.ozon_client_id,
        "page": page,
        "pageSize": page_size,
    }
    if date_from:
        body["date_from"] = date_from
    if date_to:
        body["date_to"] = date_to

    async def _do(c: httpx.AsyncClient):
        resp = await c.post(f"{BASE}/api/review/list", headers=_headers(), json=body)
        if resp.status_code != 200:
            raise OzonAPIError(resp.status_code, resp.text[:200])
        data = resp.json()
        raw = data.get("result", [])
        total = data.get("total_items", 0)
        pages = data.get("page_count", 0)
        return [_parse_review(r) for r in raw], total, pages

    if client is not None:
        return await _do(client)

    async with httpx.AsyncClient(timeout=30) as c:
        return await _do(c)


async def fetch_new_reviews(since: datetime | None = None, max_pages: int = 3) -> list[dict]:
    """
    Загружает новые отзывы.
    - since=None (первый запуск): загружает max_pages страниц с начала (новые)
    - since=datetime: загружает страницы пока не встретит отзыв старше since
    """
    all_reviews: list[dict] = []

    async with httpx.AsyncClient(timeout=30) as client:
        for page in range(1, max_pages + 1):
            resp = await client.post(
                f"{BASE}/api/review/list",
                headers=_headers(),
                json={
                    "company_id": settings.ozon_client_id,
                    "page": page,
                    "pageSize": 100,
                },
            )
            if resp.status_code != 200:
                raise OzonAPIError(resp.status_code, resp.text[:200])

            data = resp.json()
            raw = data.get("result", [])
            if not raw:
                break

            stop = False
            for r in raw:
                parsed = _parse_review(r)
                if since:
                    published = r.get("published_at") or r.get("created_at")
                    if published:
                        try:
                            dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                            dt_msk = dt.astimezone(_MSK).replace(tzinfo=None)
                            if dt_msk <= since.replace(tzinfo=None):
                                stop = True
                                break
                        except ValueError:
                            pass
                all_reviews.append(parsed)

            if stop:
                break

    return all_reviews


async def fetch_seller_comment(review_uuid: str) -> str | None:
    """Возвращает текст ответа продавца на отзыв (первый is_owner=True комментарий)."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{BASE}/api/review/comment/list",
            headers=_headers(),
            json={"review_uuid": review_uuid, "company_id": settings.ozon_client_id, "page": 1, "pageSize": 10},
        )
        if resp.status_code != 200:
            return None
        for comment in resp.json().get("result", []):
            if comment.get("is_owner") or comment.get("is_official"):
                return comment.get("text")
    return None


async def post_review_response(review_id: str, text: str) -> bool:
    """
    POST /api/review/comment/create — публикует ответ продавца.
    review_id должен быть UUID (не числовой id).
    """
    if len(text) > REVIEW_MAX_LENGTH:
        text = text[:REVIEW_MAX_LENGTH]

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{BASE}/api/review/comment/create",
            headers=_headers(),
            json={
                "company_id": settings.ozon_client_id,
                "review_uuid": review_id,
                "text": text,
            },
        )
        if resp.status_code != 200:
            raise OzonAPIError(resp.status_code, resp.text[:200])

    return True
