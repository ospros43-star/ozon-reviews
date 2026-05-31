"""
Автоматически читает актуальные куки Ozon из Safari и обновляет settings.
После 401 пробует восстановить сессию через HTTP-запрос к seller.ozon.ru.

Требует: Полный доступ к диску для Terminal
(Системные настройки → Конфиденциальность → Полный доступ к диску → Terminal)
"""
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_COOKIE_CACHE = Path.home() / ".ozon_session_cache.json"


def _save_cookie_cache(cookies: dict) -> None:
    try:
        _COOKIE_CACHE.write_text(json.dumps(cookies, ensure_ascii=False))
    except Exception:
        pass


def _load_cookie_cache() -> dict:
    try:
        if _COOKIE_CACHE.exists():
            data = json.loads(_COOKIE_CACHE.read_text())
            if isinstance(data, dict) and len(data) >= 3:
                return data
    except Exception:
        pass
    return {}

OZON_DOMAINS = [".ozon.ru", "seller.ozon.ru", ".seller.ozon.ru", "ozon.ru"]

REQUIRED_COOKIES = [
    "__Secure-access-token",
    "__Secure-token",
    "__Secure-sid",
    "sc_company_id",
    "__Secure-ETC",
]

# Куки которые Ozon обновляет при каждом запросе (короткоживущие)
SESSION_COOKIES = {
    "__Secure-access-token",
    "__Secure-ETC",
    "__Secure-ab-group",
}


def _build_cookie_string(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def _jar_to_dict(jar) -> dict:
    result = {}
    for c in jar:
        result[c.name] = c.value
    return result


def _has_required_cookies(cookies: dict) -> bool:
    found = [k for k in REQUIRED_COOKIES if k in cookies]
    return len(found) >= 3


def refresh_from_safari() -> bool:
    try:
        import browser_cookie3
        from app.config import settings

        jar = browser_cookie3.safari(domain_name=".ozon.ru")
        cookies = _jar_to_dict(jar)

        if not cookies:
            logger.warning("Safari: куки для ozon.ru не найдены")
            return False

        if not _has_required_cookies(cookies):
            logger.warning(
                "Safari: куки найдены (%d штук), но нет ключевых авторизационных кук",
                len(cookies),
            )
            return False

        settings.ozon_cookie = _build_cookie_string(cookies)
        _save_cookie_cache(cookies)  # кешируем для LaunchAgent
        logger.info("✓ Куки Ozon обновлены из Safari (%d кук)", len(cookies))
        return True

    except PermissionError:
        logger.error(
            "Нет доступа к куки Safari. Дайте разрешение: "
            "Системные настройки → Конфиденциальность → Полный доступ к диску → Terminal"
        )
        return False
    except Exception as exc:
        logger.warning("Не удалось прочитать куки Safari: %s", exc)
        return False


def _merge_set_cookie_headers(current: dict, set_cookie_headers: list[str]) -> dict:
    """Обновляет словарь кук из Set-Cookie заголовков ответа."""
    updated = dict(current)
    for header in set_cookie_headers:
        m = re.match(r'([^=]+)=([^;]*)', header.strip())
        if m:
            name, value = m.group(1).strip(), m.group(2).strip()
            if name and value:
                updated[name] = value
                logger.debug("Кука обновлена из Set-Cookie: %s", name)
    return updated


def try_restore_session() -> bool:
    """
    Читает куки из Safari (или кеш-файла) и проверяет что сессия живая.
    При PermissionError использует кешированные куки из прошлого успешного сеанса.
    """
    import httpx
    from app.config import settings as _settings

    # Пробуем читать свежие куки из Safari
    safari_ok = refresh_from_safari()

    # Если Safari недоступен (LaunchAgent без FDA) — используем кеш
    if not safari_ok and not _settings.ozon_cookie:
        cached = _load_cookie_cache()
        if cached:
            _settings.ozon_cookie = _build_cookie_string(cached)
            logger.info("Safari недоступен — используем кешированные куки (%d штук)", len(cached))

    if not _settings.ozon_cookie:
        logger.warning("Куки Ozon не настроены")
        return False

    # Проверяем сессию реальным запросом к API
    try:
        resp = httpx.post(
            "https://seller.ozon.ru/api/review/list",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "Cookie": _settings.ozon_cookie,
                "x-o3-company-id": _settings.ozon_client_id,
                "x-o3-app-name": "seller-ui",
                "x-o3-language": "ru",
                "x-o3-page-type": "review",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/17.5 Safari/605.1.15"
                ),
                "Origin": "https://seller.ozon.ru",
                "Referer": "https://seller.ozon.ru/app/reviews",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
            },
            json={"company_id": _settings.ozon_client_id, "page": 1, "pageSize": 20},
            follow_redirects=False,
            timeout=15,
        )
        if resp.status_code == 200:
            logger.info("✓ Сессия Ozon активна, куки из Safari применены")
            return True
        logger.warning(
            "Сессия Ozon недоступна (HTTP %d) — войдите в seller.ozon.ru в Safari",
            resp.status_code,
        )
        return False
    except Exception as exc:
        logger.warning("Ошибка проверки сессии: %s", exc)
        return False


def refresh_cookies() -> bool:
    """
    Основная функция обновления кук.
    Пробует Safari, при неудаче оставляет текущие куки из .env.
    """
    success = refresh_from_safari()
    if not success:
        from app.config import settings
        if settings.ozon_cookie:
            logger.info("Используются куки из .env (Safari недоступен)")
            return True
        else:
            logger.error("Куки Ozon не настроены! Заполните OZON_COOKIE в .env")
            return False
    return success
