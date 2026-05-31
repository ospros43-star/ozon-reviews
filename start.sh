#!/bin/bash
# Запускает Ozon Review Bot
# Используется LaunchAgent для автозапуска при входе в систему

cd /Users/aleksandrsolonicyn/Documents/progect

export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

exec .venv/bin/uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --log-level info
