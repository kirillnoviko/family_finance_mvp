import os
from dataclasses import dataclass
from pathlib import Path
from decimal import Decimal
from dotenv import load_dotenv

load_dotenv()

def _csv_ints(value: str) -> set[int]:
    out=set()
    for item in value.split(','):
        item=item.strip()
        if item:
            out.add(int(item))
    return out

@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: int
    api_secret: str
    telegram_webhook_secret: str
    allowed_telegram_user_ids: set[int]
    app_base_url: str
    database_path: str
    period_start_day: int
    store_raw_sms: bool
    debt_initial_balance: Decimal
    debt_currency: str
    debt_annual_rate: Decimal

    @classmethod
    def from_env(cls):
        token=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
        chat=os.getenv('TELEGRAM_CHAT_ID','').strip()
        api=os.getenv('API_SECRET','').strip()
        missing=[k for k,v in [('TELEGRAM_BOT_TOKEN',token),('TELEGRAM_CHAT_ID',chat),('API_SECRET',api)] if not v]
        if missing:
            raise RuntimeError('Missing required environment variables: '+', '.join(missing))
        volume=os.getenv('RAILWAY_VOLUME_MOUNT_PATH','').strip()
        default_db=str(Path(volume)/'finance.db') if volume else './data/finance.db'
        day=int(os.getenv('PERIOD_START_DAY','15'))
        if not 1 <= day <= 28:
            raise RuntimeError('PERIOD_START_DAY must be between 1 and 28')
        webhook=os.getenv('TELEGRAM_WEBHOOK_SECRET','').strip() or api
        return cls(
            telegram_bot_token=token,
            telegram_chat_id=int(chat),
            api_secret=api,
            telegram_webhook_secret=webhook,
            allowed_telegram_user_ids=_csv_ints(os.getenv('ALLOWED_TELEGRAM_USER_IDS','')),
            app_base_url=os.getenv('APP_BASE_URL','').strip().rstrip('/'),
            database_path=os.getenv('DATABASE_PATH',default_db).strip(),
            period_start_day=day,
            store_raw_sms=os.getenv('STORE_RAW_SMS','false').lower() in {'1','true','yes','on'},
            debt_initial_balance=Decimal(os.getenv('DEBT_INITIAL_BALANCE','8000').replace(',','.')),
            debt_currency=os.getenv('DEBT_CURRENCY','EUR').strip(),
        )

settings=Settings.from_env()
