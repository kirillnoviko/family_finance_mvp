# Family Finance Bot — Full MVP

Схема: `Priorbank SMS -> iPhone Shortcuts -> Railway/FastAPI -> SQLite -> Telegram`.

## Что уже умеет MVP

- Парсит Priorbank: `Oplata`, `Perevod`, `Zachislenie perevoda`.
- Извлекает сумму, валюту, дату/время, карту, магазин/контрагента и баланс после операции.
- Не создаёт дубль, если одно SMS пришло повторно.
- Разделяет учёт на `Семья` и `Маркетинг`; НЗ учитывается как резерв маркетинга.
- Одна физическая карта может содержать операции обоих контуров.
- Семейные категории и подкатегории.
- Маркетинг: доход клиентов, зарплаты помощницам, налог, прочие расходы, пополнение НЗ.
- Перевод из НЗ в семью не считается новым глобальным доходом.
- Inline-кнопки Telegram, исправление категории, запоминание магазина.
- Ручные/наличные операции: `/add 35 кофе` или `/add 1200 Panda`.
- Очередь неразобранного: `/pending`.
- Статистика текущего периода: `/stats`.
- Произвольный период: `/stats 2026-07-15 2026-08-14`.
- CSV export через защищённый endpoint.
- Telegram allow-list и webhook secret.

## Railway Variables

Оставьте существующие переменные и добавьте новые:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=-5101047364
API_SECRET=...
APP_BASE_URL=https://YOUR-SERVICE.up.railway.app
TELEGRAM_WEBHOOK_SECRET=ANOTHER_RANDOM_HEX_SECRET
ALLOWED_TELEGRAM_USER_IDS=825091438
PERIOD_START_DAY=15
STORE_RAW_SMS=false
```

Секрет можно создать:

```bash
openssl rand -hex 32
```

После того как жена напишет боту `/start`, добавьте её Telegram user ID через запятую:

```env
ALLOWED_TELEGRAM_USER_IDS=825091438,WIFE_USER_ID
```

## ОБЯЗАТЕЛЬНО: Railway Volume для SQLite

До реального учёта добавьте persistent volume к тому же Railway service.

`Service -> Settings/Volumes -> Add Volume`

Mount path:

```text
/data
```

Приложение увидит `RAILWAY_VOLUME_MOUNT_PATH` и само будет использовать:

```text
/data/finance.db
```

Можно дополнительно явно задать:

```env
DATABASE_PATH=/data/finance.db
```

Без volume SQLite находится в ephemeral filesystem и может потеряться при redeploy.

## Telegram webhook

Укажите публичный Railway URL без `/` в конце:

```env
APP_BASE_URL=https://YOUR-SERVICE.up.railway.app
```

При запуске приложение автоматически вызывает Telegram `setWebhook` на:

```text
https://YOUR-SERVICE.up.railway.app/telegram/webhook
```

Проверка сервиса:

```bash
curl https://YOUR-SERVICE.up.railway.app/health
```

Ожидается:

```json
{"status":"ok","version":"1.0.0-mvp"}
```

## Тест реального SMS

```bash
curl -X POST https://YOUR-SERVICE.up.railway.app/api/sms \
  -H "Authorization: Bearer YOUR_API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "device":"wife",
    "message":"Karta 4***3730 11-08-26 16:18:52. Oplata 60.00 BYN. BLR KAFE PRIZMA PARK. Balance: 101.76 BYN Tel. 7299090"
  }'
```

Telegram должен показать уже распарсенную операцию и кнопки `Семья / Маркетинг / ...`.

## Команды бота

```text
/help
/stats
/stats 2026-07-15 2026-08-14
/add 35 кофе
/add 1200 Panda
/pending
```

Для `/add` бот сначала спросит кнопками, что это: семейный/маркетинговый доход или расход, пополнение НЗ, деньги из НЗ в семью и т.д.

## CSV

```bash
curl \
  -H "Authorization: Bearer YOUR_API_SECRET" \
  https://YOUR-SERVICE.up.railway.app/api/export.csv \
  -o family-finance.csv
```

## iPhone

Ваш существующий Shortcut менять не нужно, если Railway domain и `API_SECRET` остаются прежними:

```text
POST https://YOUR-SERVICE.up.railway.app/api/sms
Authorization: Bearer YOUR_API_SECRET
JSON: { message: <SMS>, device: wife }
```

## Локальные тесты

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```
