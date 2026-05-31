#!/usr/bin/env python3
"""
Читает свежие куки Ozon из Safari и пушит их на Railway сервер.
Запускается каждые 20 минут через LaunchAgent.
"""
import json
import sys
import os
from pathlib import Path

# Загружаем .env чтобы знать SYNC_SECRET и RAILWAY_URL
env_file = Path(__file__).parent.parent / ".env"
env = {}
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")

RAILWAY_URL = env.get("RAILWAY_URL", "https://ozon-reviews-production.up.railway.app")
SYNC_SECRET = env.get("SYNC_SECRET", "")

if not SYNC_SECRET:
    print("ERROR: SYNC_SECRET не задан в .env", file=sys.stderr)
    sys.exit(1)


def get_cookies_from_safari() -> str:
    try:
        import browser_cookie3
        jar = browser_cookie3.safari(domain_name=".ozon.ru")
        cookies = {c.name: c.value for c in jar}
        if not cookies:
            return ""
        return "; ".join(f"{k}={v}" for k, v in cookies.items())
    except Exception as e:
        print(f"Safari недоступен: {e}", file=sys.stderr)
        return ""


def push_to_railway(cookie: str) -> bool:
    import urllib.request
    import urllib.error

    data = json.dumps({"cookie": cookie}).encode()
    req = urllib.request.Request(
        f"{RAILWAY_URL}/api/sync-cookie",
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-sync-secret": SYNC_SECRET,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            print(f"Railway обновлён: {result}")
            return True
    except urllib.error.HTTPError as e:
        print(f"HTTP ошибка {e.code}: {e.read().decode()}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Ошибка соединения с Railway: {e}", file=sys.stderr)
        return False


def main():
    print("Читаем куки из Safari...")
    cookie = get_cookies_from_safari()
    if not cookie:
        print("Куки не получены, выходим")
        sys.exit(1)

    print(f"Куки получены ({len(cookie)} символов), пушим на Railway...")
    ok = push_to_railway(cookie)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
