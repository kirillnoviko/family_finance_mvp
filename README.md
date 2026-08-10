# Family Finance Bot — MVP

Минимальный сервис:

`iPhone / curl -> FastAPI -> Telegram`

На этом этапе приложение:
- принимает `POST /api/sms`;
- проверяет `Authorization: Bearer <API_SECRET>`;
- пересылает текст операции в Telegram-группу;
- имеет `GET /health`.

## 1. Переменные окружения

Скопируйте `.env.example` в `.env`:

```bash
cp .env.example .env
```

Заполните:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=-5101047364
API_SECRET=...
```

Сгенерировать API_SECRET:

```bash
openssl rand -hex 32
```

Никогда не коммитьте `.env`.

## 2. Локальный запуск через Docker

```bash
docker build -t family-finance-bot .
docker run --rm \
  --env-file .env \
  -p 8000:8000 \
  family-finance-bot
```

Проверка:

```bash
curl http://localhost:8000/health
```

Ожидается:

```json
{"status":"ok"}
```

## 3. Проверка Telegram

```bash
curl -X POST http://localhost:8000/api/sms \
  -H "Authorization: Bearer YOUR_API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "message":"Purchase 24.50 EUR RIMI",
    "device":"wife"
  }'
```

Ожидаемый ответ:

```json
{
  "ok": true,
  "telegram_message_id": 123
}
```

В группе `Family finance` должно появиться сообщение:

```text
💳 Новая операция

Purchase 24.50 EUR RIMI

📱 Источник: wife
```

## 4. Railway

Создайте проект Railway и подключите этот GitHub-репозиторий.

Добавьте Variables:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=-5101047364
API_SECRET=...
```

Railway обнаружит `Dockerfile` в корне репозитория.

После успешного deploy включите Public Networking / Generate Domain.

Проверьте:

```bash
curl https://YOUR-SERVICE.up.railway.app/health
```

А затем:

```bash
curl -X POST https://YOUR-SERVICE.up.railway.app/api/sms \
  -H "Authorization: Bearer YOUR_API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "message":"Purchase 24.50 EUR RIMI",
    "device":"wife"
  }'
```

## Что будет дальше

После проверки цепочки Railway -> Telegram добавляем:
1. Shortcut на iPhone.
2. Парсер конкретных банковских SMS.
3. Inline-кнопки категорий.
4. SQLite.
5. Правила `merchant -> category`.
6. Отчёты.
