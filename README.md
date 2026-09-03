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


## v1.1

- Starting balances for Family / Marketing / Reserve (НЗ) are stored separately and do not create transactions.
- Marketing and Reserve balances show approximate USD and EUR equivalents using the official NBRB exchange-rate API.
- Statistics has period buttons: current 15→14, previous 15→14, current month, previous month, last 30 days, custom dates.
- Persistent Telegram keyboard: Add, Statistics, Balances, Pending, Help.
- Bot command menu is configured automatically with setMyCommands.
- Existing SQLite data is preserved; new tables are created automatically.

## v1.2 — Priorbank real-world SMS formats

The parser now covers the real message formats observed from Priorbank, including:

- `Oplata ... BYN`
- `Oplata ... USD` (e.g. APPLE.COM/BILL / IRL)
- `Perevod ...`
- `Zachislenie perevoda ... BYN`
- `Zachislenie perevoda ... USD`
- `Nalichnye v bankomate ...`
- `DD/MM HH:MM. Na vashu kartu zachisleno ...`
- OTP/code messages are accepted and ignored as non-financial messages instead of returning HTTP 422.

### Cash withdrawal logic

A cash withdrawal is **not automatically an expense or a transfer**. The bot asks what happened to the cash:

- Family expense
- Marketing expense
- Assistant salary
- Keep as cash (card -> cash transfer, no expense yet)
- Own-money/internal transfer
- Ignore

If cash is kept, record the real expense later through the normal manual cash-entry flow.

### Recover missed SMS

You can copy an old Priorbank SMS from iPhone Messages and paste the complete SMS text directly into the Telegram group. The bot parses it with the same Priorbank parser and preserves the original transaction date/time from the SMS.

For transactions that were stored in SQLite but failed to reach Telegram, duplicate `/api/sms` delivery now retries Telegram when the transaction is still pending and has no Telegram message id.

### Foreign-currency safety

USD/EUR card transactions are parsed and stored in their original currency. Until transaction-date conversion is implemented, non-BYN transactions are intentionally excluded from BYN report totals/current logical balances so they cannot silently distort BYN statistics. Marketing/NZ balance display conversion remains separate.


## v1.3 — historical NBRB conversion

Foreign-currency bank operations are now stored with both values:

- original amount/currency, e.g. `9.99 USD`
- BYN equivalent for reports
- official NBRB rate used
- rate date

The rate date is the bank operation date, not the date when the bot receives or imports the SMS.

Existing foreign-currency transactions with no BYN equivalent are backfilled automatically on application startup. Existing BYN transactions are migrated locally without network access.

Telegram keeps showing the original amount, plus the BYN equivalent, for example:

`9.99 USD`
`≈ 30.xx BYN по курсу НБРБ на 2026-08-05`

Reports and balances use the stored BYN equivalent, so historical totals remain stable when exchange rates change later.


## v1.4 — operation journal

A new persistent Telegram button `📋 Операции` shows categorized transactions by:

- Family
- Marketing
- Reserve (НЗ)

The user chooses the same period presets used by statistics. Results are shown newest first and contain:

`DD.MM HH:MM | signed amount | merchant/description`

Foreign-currency items show the original currency and stored BYN equivalent. Reserve entries are displayed from the reserve perspective as `Маркетинг → НЗ` and `НЗ → Семья`.

The list is paginated at 15 items per page and can switch between Family / Marketing / НЗ while preserving the selected period.


## v1.5 — reserve sources + reconciled statistics

Reserve can now be funded from either logical contour:

- `🏠 Семья → НЗ`
- `📈 Маркетинг → НЗ`

Both are allocations/transfers between internal contours. They do not inflate expenses or income:
the source balance decreases and the reserve balance increases by exactly the same amount.

Statistics now use the same balance engine as `💰 Балансы`.
Each report shows:

- balance at the beginning of the selected period
- period inflows/outflows
- reserve movements
- net change during the period
- balance at the end of the selected period

For a current period whose end is today or later, `Баланс на конец` matches the corresponding value in `💰 Балансы` because both are calculated by the same function.


## v1.6 — subcategory expense analytics

Family statistics now show expense percentages by the actual selected
subcategory, not by the broad parent group.

Example:

- Продукты — 60%
- Кафе / рестораны — 20%
- Такси — 20%

Each percentage is calculated against all family expenses in the selected
period.

For broad categories that have children (Еда, Дом, Транспорт, Дети,
Здоровье, Обязательные платежи, Покупки, Досуг), new transactions must
choose a concrete subcategory. The previous "Оставить <родительскую
категорию>" button is removed.

Existing historical transactions that were already saved only at parent
level remain valid and appear as "<Категория> — без уточнения" so they are
not silently omitted from reports.


## v1.7 — beauty, car, sport and apartment-loan tracker

New Family categories:

- Красота
  - Жена
  - Кирилл
- Транспорт → Авто
  - Мойка
  - Ремонт авто
  - Запчасти
- Спорт
  - Инвентарь
  - Карты / абонементы

Apartment loan defaults:

- opening principal: 35,536 BYN
- annual rate: 14.56%
- accounting start date: 2026-08-14

They can be overridden before the first deployment with:

- LOAN_INITIAL_BALANCE
- LOAN_ANNUAL_RATE
- LOAN_START_DATE

Loan payments can be recorded from either Family or Marketing. The full cash
payment reduces the selected source balance. The loan ledger then splits it
into monthly interest and principal.

Simplified allocation rule requested by the user:

- on the first loan payment in a calendar month, monthly interest is assessed
  as `remaining principal * annual rate / 12`
- payments first cover that month's remaining interest
- the rest reduces principal
- later payments in the same calendar month go directly to principal once
  monthly interest has been fully covered
- the next calendar month recalculates interest using the then-current principal

Transactions before LOAN_START_DATE do not reduce the configured opening
principal, because that opening principal is treated as the debt as of the
start date.

A new `🏦 Кредит` screen shows principal remaining, total payments, total
interest, total principal reduction, and direct payment buttons for Family
and Marketing.


## v1.8 changes

- Removed apartment mortgage logic.
- Added general debt tracking (example: 8000 EUR debt).
- Added transfers:
  - Marketing -> Family
  - Family -> Debt
  - Marketing -> Debt
  - Reserve -> Debt
- Debt balance is tracked separately and does not inflate income/expenses.
- Added foundation for custom categories/subcategories.
- Statistics should use selected category/subcategory, not only parent groups.

Debt parameters:
- currency: EUR
- initial amount: configurable
- payments can come from Family, Marketing or Reserve.


## v1.9 Financial overview

Added a consolidated financial picture:

- Family balance
- Marketing balance
- Reserve balance
- Total available money
- Debt overview

The goal is to see not only expenses, but the whole financial position.
