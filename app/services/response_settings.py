"""Настройки генерации ответов — подпись и компенсация."""
import json
from pathlib import Path

_FILE = Path(__file__).parent.parent.parent / "response_settings.json"

_DEFAULTS: dict = {
    "signature": "",           # текст подписи
    "signature_enabled": False, # включена ли подпись
    "compensation": False,      # предлагать компенсацию в негативных отзывах
}


def load() -> dict:
    if _FILE.exists():
        try:
            return {**_DEFAULTS, **json.loads(_FILE.read_text(encoding="utf-8"))}
        except Exception:
            pass
    return dict(_DEFAULTS)


def save(data: dict) -> None:
    current = load()
    for k in _DEFAULTS:
        if k in data:
            current[k] = data[k]
    _FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
