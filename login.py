"""
Читает куки из Safari и находит API-эндпоинты seller.ozon.ru.
Запуск: .venv/bin/python login.py
"""
import asyncio, json, browser_cookie3, httpx

DOMAINS = [".ozon.ru", "seller.ozon.ru", ".seller.ozon.ru", "ozon.ru"]

def get_safari_cookies():
    jar = browser_cookie3.safari(domain_name=".ozon.ru")
    cookies = {c.name: c.value for c in jar}
    print(f"✓ Найдено кук из Safari: {len(cookies)}")
    for k in list(cookies.keys())[:10]:
        print(f"   {k}: {cookies[k][:40]}...")
    return cookies

async def probe_api(cookies: dict):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Referer": "https://seller.ozon.ru/",
        "Origin": "https://seller.ozon.ru",
    }

    # Пробуем разные эндпоинты
    endpoints = [
        ("POST", "https://seller.ozon.ru/api/site/v1/review/list",
         {"page": 1, "pageSize": 20, "sortBy": "date", "sortDir": "desc"}),
        ("POST", "https://seller.ozon.ru/api/v1/review/list",
         {"limit": 20, "sort_dir": "DESC"}),
        ("GET",  "https://seller.ozon.ru/api/site/v1/review",    {}),
        ("POST", "https://seller.ozon.ru/api/review/list",
         {"limit": 20}),
        ("POST", "https://seller.ozon.ru/api/site/v2/review/list",
         {"page": 1, "pageSize": 20}),
        ("POST", "https://api.ozon.ru/composer-api.bx/page/json/v2",
         {"url": "/seller/reviews/", "layout_container": "DEFAULT", "layout_page_index": 1}),
    ]

    async with httpx.AsyncClient(cookies=cookies, headers=headers, follow_redirects=True, timeout=15) as client:
        results = []
        for method, url, payload in endpoints:
            try:
                if method == "POST":
                    r = await client.post(url, json=payload)
                else:
                    r = await client.get(url)
                status = r.status_code
                preview = r.text[:300]
                results.append({"method": method, "url": url, "status": status, "preview": preview})
                icon = "✓" if status == 200 else "✗"
                print(f"\n{icon} {status} {method} {url[:80]}")
                if status == 200:
                    print(f"  → {preview[:200]}")
            except Exception as e:
                print(f"  ✗ ОШИБКА {url[:60]}: {e}")
        return results

def main():
    print("="*55)
    print("  Читаю куки из Safari...")
    print("="*55)

    cookies = get_safari_cookies()
    if not cookies:
        print("\n✗ Куки не найдены. Убедитесь что вы залогинены в Safari на seller.ozon.ru")
        return

    print("\n" + "="*55)
    print("  Пробую API-эндпоинты seller.ozon.ru...")
    print("="*55)
    results = asyncio.run(probe_api(cookies))

    # Сохраняем куки и результаты
    with open("ozon_session.json", "w") as f:
        json.dump({"cookies": cookies}, f, ensure_ascii=False)
    with open("ozon_api_urls.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    working = [r for r in results if r["status"] == 200]
    print(f"\n{'='*55}")
    print(f"  Рабочих эндпоинтов: {len(working)}/{len(results)}")
    if working:
        print("  ✓ УСПЕХ! Подключение к Ozon работает")
    else:
        print("  Нужна другая стратегия — см. ниже")
    print("="*55)

main()
