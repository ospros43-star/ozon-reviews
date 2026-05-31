#!/bin/bash
# Читает свежие куки из Safari и обновляет .env
# Запускать из Терминала (требует Full Disk Access для Terminal)

cd "$(dirname "$0")"

echo "🔄 Читаю куки из Safari..."

COOKIE=$(.venv/bin/python3 -c "
import browser_cookie3, sys
try:
    jar = browser_cookie3.safari(domain_name='.ozon.ru')
    cookies = {c.name: c.value for c in jar}
    if not cookies:
        print('ERROR: no cookies', file=sys.stderr)
        sys.exit(1)
    key_cookies = ['__Secure-access-token', '__Secure-token', '__Secure-sid']
    missing = [k for k in key_cookies if k not in cookies]
    if missing:
        print(f'ERROR: missing {missing}', file=sys.stderr)
        sys.exit(1)
    print('; '.join(f'{k}={v}' for k,v in cookies.items()))
except PermissionError:
    print('ERROR: no permission', file=sys.stderr)
    sys.exit(1)
" 2>/tmp/cookie_err)

if [ $? -ne 0 ]; then
    echo "❌ Ошибка: $(cat /tmp/cookie_err)"
    echo ""
    echo "Если 'no permission' — добавь Terminal в Системные настройки → Конфиденциальность → Полный доступ к диску"
    echo "Если 'missing cookies' — войди в seller.ozon.ru в Safari и попробуй снова"
    exit 1
fi

# Обновляем OZON_COOKIE в .env
if grep -q "^OZON_COOKIE=" .env; then
    # Заменяем существующую строку
    python3 -c "
import re
with open('.env','r') as f: content = f.read()
new = re.sub(r'^OZON_COOKIE=.*$', 'OZON_COOKIE=${COOKIE}', content, flags=re.MULTILINE)
with open('.env','w') as f: f.write(new)
print('✓ OZON_COOKIE обновлён в .env')
" 2>/dev/null || {
    # Fallback через sed
    ESCAPED=$(echo "$COOKIE" | sed 's/[&/\]/\\&/g')
    sed -i '' "s|^OZON_COOKIE=.*|OZON_COOKIE=$ESCAPED|" .env
    echo "✓ OZON_COOKIE обновлён в .env"
}
else
    echo "OZON_COOKIE=$COOKIE" >> .env
    echo "✓ OZON_COOKIE добавлен в .env"
fi

echo ""
echo "✅ Готово! Перезапусти приложение чтобы применить новые куки."
echo "   (.venv/bin/uvicorn app.main:app --port 8000)"
