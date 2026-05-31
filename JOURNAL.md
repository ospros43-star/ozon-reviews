# Журнал работы — Ozon Review Manager

---

## Сессия 3 — 19 мая 2026 (продолжение)

**Что сделано:**
- Найден рабочий Ozon endpoint: `POST seller.ozon.ru/api/review/list` (105,848 отзывов, 1059 страниц)
- Найден endpoint отправки ответа: `POST seller.ozon.ru/api/review/comment/create` (нужен `review_uuid`, не числовой id)
- Автообновление куки из Safari при каждом запуске (через `browser_cookie3`)
- VS Code добавлен в Полный доступ к диску → куки читаются автоматически
- Модель Gemini: `gemini-3.1-flash-lite-preview` (самая дешёвая рабочая)
- Стратегия загрузки: первый запуск = 5 страниц (500 отзывов), регулярный опрос = 1 страница
- Отзывы с `comments_amount > 0` сразу получают статус `posted` (Gemini не вызывается)
- Добавлены фильтры: 1-3★ / 4-5★ / без текста
- Создан файл запуска на рабочем столе: `Запустить Ozon Reviews.command`
- Автообновление кук каждые 2 часа через APScheduler
- Кнопка 🔑 в интерфейсе для принудительного обновления кук

---

## Дорожная карта (план будущих работ)

### Ближайшие задачи
- [ ] Загрузка существующих ответов из Ozon (найти `api/review/comment/list` endpoint)
- [ ] Улучшить первичную загрузку — добавить настройку глубины (сколько страниц грузить)
- [ ] Показывать статус кук в интерфейсе (живые / истекли)
- [ ] Добавить статистику: сколько отвечено сегодня, процент охвата

### Отправка ответов — два варианта (оба рабочих)

**Вариант A: Через куки (текущий)**
Ответы отправляются через `POST seller.ozon.ru/api/review/comment/create` с cookie-сессией из Safari.
Работает пока пользователь залогинен в Safari.
✅ Уже реализовано

**Вариант B: Через браузерную автоматизацию (OpenClaw / Firefox)**
Идея: Playwright управляет Firefox, заходит на seller.ozon.ru под аккаунтом продавца,
нажимает «Ответить» на каждый отзыв через UI браузера.
Плюсы: не зависит от куки, работает даже если изменится API.
Минусы: медленнее, сложнее в поддержке.
📋 Реализовать в будущем если cookie-подход перестанет работать.

### SaaS-деплой (когда готовы продавать)
- [ ] Render.com — деплой (Европа/Азия, Gemini доступен без VPN)
- [ ] PostgreSQL вместо SQLite
- [ ] JWT-авторизация + мультитенант (каждый продавец = свои ключи)
- [ ] ЮКасса для оплаты подписок через ИП
- [ ] Лендинг с тарифами

---

---

## Сессия 1 — 14 мая 2026

**Что сделано:**
- Создан проект с нуля: FastAPI + SQLAlchemy (SQLite) + APScheduler
- Первый AI-провайдер: Anthropic → переключён на Google Gemini (`google-genai`)
- Review processor: логика poll → generate → auto-post (4-5★) / pending (1-3★)
- Базовый UI: дашборд, очередь на проверке, детальная страница
- Установлен Python 3.12 автономный (arm64 macOS, без Xcode), все зависимости в `.venv/`

**Проблемы сессии:**
- Gemini недоступен без VPN из России (FAILED_PRECONDITION: User location)
- Официальный Ozon API (`api-seller.ozon.ru`) заблокирован по подписке: `not available with existing subscription`

---

## Сессия 2 — 19 мая 2026

### Главное открытие: рабочий Ozon endpoint

**Официальный API (`api-seller.ozon.ru`) — НЕ РАБОТАЕТ** без подписки «Управление отзывами».

**Рабочий подход — cookie-сессия через seller.ozon.ru:**

#### Аутентификация
- Берём куки из Safari (или Chrome) пока залогинен на seller.ozon.ru
- Куки живут **несколько часов**, после нужно обновить
- Ключевые куки: `__Secure-access-token`, `__Secure-token`, `__Secure-sid`, `sc_company_id`, `__Secure-ETC`, `abt_data`
- Обязательные заголовки (без них WAF возвращает 403):
  ```
  Sec-Fetch-Dest: empty
  Sec-Fetch-Site: same-origin
  Sec-Fetch-Mode: cors
  Origin: https://seller.ozon.ru
  Referer: https://seller.ozon.ru/app/reviews
  User-Agent: Mozilla/5.0 ... Safari/605.1.15
  x-o3-company-id: 1799619
  x-o3-app-name: seller-ui
  x-o3-language: ru
  x-o3-page-type: review
  ```

#### Рабочие endpoints

**Получить список отзывов:**
```
POST https://seller.ozon.ru/api/review/list
Body: {"company_id": "1799619", "page": 1, "pageSize": 100}

Ответ:
{
  "result": [...],
  "total_items": 105848,   ← всего отзывов у продавца
  "page_count": 1059       ← страниц при pageSize=100
}
```

**Структура одного отзыва:**
```json
{
  "id": 361373571,                          ← числовой ID (НЕ использовать для ответа!)
  "uuid": "019e40f8-...",                   ← UUID (использовать для ответа)
  "sku": "1865716923",
  "rating": 5,
  "text": {
    "positive": "",
    "negative": "",
    "comment": "Вкусно, но попадались горькие немного"
  },
  "author_name": "Жукова Л.",
  "product": {
    "title": "KEDROVICH Орех макадамия...",
    "url": "https://www.ozon.ru/product/1865716923/",
    "offer_id": "makadamya v skorlupe.500"
  },
  "published_at": "2026-05-19T16:01:56.054950Z",
  "comments_amount": 0,    ← количество ответов продавца (0 = без ответа)
  "comments_count": 0,
  "has_unread_comment": false,
  "interaction_status": "NOT_VIEWED"
}
```

**Отправить ответ на отзыв:**
```
POST https://seller.ozon.ru/api/review/comment/create
Body: {
  "company_id": "1799619",
  "review_uuid": "019e40f8-...",   ← именно UUID, не числовой id!
  "text": "текст ответа"
}
```

#### Важные цифры
- **Всего отзывов:** 105,848 (у продавца KEDROVICH)
- **Страниц (по 100):** 1,059
- **Отзывы сортированы:** от новых к старым (page 1 = самые свежие)
- Нельзя загрузить все 105К за один раз — нужна умная стратегия

---

## Стратегия загрузки отзывов (план)

### Проблема
Загружать все 105,848 отзывов при каждом запуске нереально (~50 часов).

### Правильная стратегия

**При первом запуске (fresh DB):**
- Загружать только последние N страниц, например за 90 дней
- Пример: `page 1` → `page 5` (500 новейших отзывов)
- Отзывы с `comments_amount > 0` → сразу ставить статус `posted` (уже отвечено)
- Настраивается параметром `INITIAL_PAGES=5` в `.env`

**При регулярном опросе (каждые 5 мин):**
- Только страница 1 (100 новейших)
- Сравнивать `uuid` с тем что уже есть в БД
- Новые → обрабатывать, старые → пропускать

**Отображение уже отвеченных:**
- Поле `comments_amount > 0` в ответе API → статус `posted` в БД
- Для получения текста существующего ответа нужно найти endpoint `api/review/comment/list` (требует актуальные куки для тестирования)

---

## Статус на конец сессии 2

### Что работает ✅
- Cookie-auth к seller.ozon.ru
- Получение отзывов через `/api/review/list`
- Отправка ответов через `/api/review/comment/create` (с `review_uuid`)
- Gemini `gemini-2.5-flash` генерирует живые ответы (нужен VPN)
- UI: двухпанельный интерфейс как otveto.ru
- Фильтры: без ответа / все / 1-3★ / 4-5★ / без текста
- Пагинация загрузки отзывов из Ozon (страница за страницей)

### Что нужно доделать ❌
1. **Обновление куки** — сессия истекает через несколько часов, нужен UI для обновления
2. **Умная первичная загрузка** — брать только последние N страниц, не все 1059
3. **Определение уже отвеченных** — `comments_amount > 0` → статус `posted`
4. **Fetch текста существующих ответов** — найти endpoint `/api/review/comment/list`
5. **Деплой** — Render.com + PostgreSQL + YooKassa

---

## Структура проекта

```
progect/
├── app/
│   ├── main.py               # FastAPI app + HTML роуты + htmx партиалы
│   ├── config.py             # Настройки из .env (OZON_COOKIE, GEMINI_API_KEY, etc.)
│   ├── database.py           # SQLAlchemy + WAL mode для SQLite
│   ├── models/review.py      # ORM модель (49 полей включая ozon_review_id=UUID)
│   ├── schemas/review.py     # Pydantic схемы
│   ├── routers/reviews.py    # REST API /api/* (approve, reject, archive, improve, etc.)
│   ├── scheduler.py          # APScheduler (опрос каждые 5 мин)
│   └── services/
│       ├── ozon_client.py    # seller.ozon.ru: fetch_new_reviews, post_review_response
│       ├── ai_service.py     # Gemini: generate_response, improve_response
│       └── review_processor.py  # Оркестрация + asyncio Lock
├── templates/
│   ├── base.html             # Общий layout
│   ├── app.html              # Главная (двухпанельный UI + вкладки + фильтры)
│   └── partials/
│       ├── review_list.html  # Список отзывов
│       └── review_detail.html # Детали + кнопки действий
├── python/                   # Python 3.12 автономный (arm64)
├── .venv/                    # Виртуальное окружение
├── .env                      # Куки Ozon + ключи API (НЕ коммитить!)
├── login.py                  # Скрипт захвата куки из Safari
└── JOURNAL.md                # Этот файл
```

---

## Как обновить куки (когда перестанет работать)

1. Открыть Safari → seller.ozon.ru → убедиться что залогинен
2. Открыть Web Inspector → вкладка Сеть → перейти на `/app/reviews`
3. Найти любой xhr-запрос к `seller.ozon.ru` → правая кнопка → **Скопировать как cURL**
4. Скопировать строку после `-H 'Cookie:` из cURL
5. Вставить в `.env` в параметр `OZON_COOKIE=...`
6. Перезапустить сервер

**Или:** запустить `python login.py` (даёт Терминалу доступ к диску в Настройках)

---

## Переменные окружения (.env)

```bash
OZON_CLIENT_ID=1799619
OZON_API_KEY=da6c5f54-...        # официальный ключ (не используется сейчас)
OZON_COOKIE=bacntid=...          # ← главное, обновлять когда истекает

GEMINI_API_KEY=AIzaSy...         # Google AI Studio
GEMINI_MODEL=gemini-2.5-flash    # актуальная рабочая модель

DATABASE_URL=sqlite:///./reviews.db
POLL_INTERVAL_SECONDS=300        # опрос каждые 5 мин
AUTO_POST_ENABLED=false          # false = только предлагать, не публиковать
```
