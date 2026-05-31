import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

BATCH = 100


async def fetch_images_by_skus(skus: list[str]) -> dict[str, str]:
    """Returns {sku_str: first_image_url} for each SKU that has images."""
    if not skus:
        return {}

    int_skus: list[int] = []
    for s in skus:
        try:
            int_skus.append(int(s))
        except (ValueError, TypeError):
            pass

    if not int_skus:
        return {}

    result: dict[str, str] = {}
    headers = {
        "Client-Id": settings.ozon_client_id,
        "Api-Key": settings.ozon_api_key,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(int_skus), BATCH):
            batch = int_skus[i : i + BATCH]
            try:
                resp = await client.post(
                    "https://api-seller.ozon.ru/v3/product/info/list",
                    headers=headers,
                    json={"sku": batch},
                )
                if resp.status_code != 200:
                    logger.warning("ozon_images: API returned %s", resp.status_code)
                    continue
                data = resp.json()
            except Exception as exc:
                logger.error("ozon_images: request failed: %s", exc)
                continue

            for item in data.get("items", []):
                primary = item.get("primary_image") or []
                images = item.get("images", [])
                url = primary[0] if primary else (images[0] if images else None)
                if not url:
                    continue
                for bc in item.get("barcodes", []):
                    if bc.startswith("OZN"):
                        result[bc[3:]] = url

    return result
