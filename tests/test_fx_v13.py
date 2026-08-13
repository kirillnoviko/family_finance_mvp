import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

DB=os.environ.get('DATABASE_PATH','/tmp/family-finance-pytest.db')
for suffix in ('','-wal','-shm'):
    try: Path(DB+suffix).unlink()
    except FileNotFoundError: pass

os.environ['TELEGRAM_BOT_TOKEN']='test'
os.environ['TELEGRAM_CHAT_ID']='-100'
os.environ['API_SECRET']='test'
os.environ.setdefault('DATABASE_PATH',DB)
os.environ['PERIOD_START_DAY']='15'

from app.db import create_sms_transaction, init_db, set_fx_conversion
from app.exchange import convert_foreign_to_byn
from app.models import ParsedSms


def test_byn_transaction_has_report_amount():
    init_db()
    p=ParsedSms(
        card_mask='4***3730',
        occurred_at=datetime(2026,8,5,10,23,44),
        amount=Decimal('10.00'),
        currency='BYN',
        direction='out',
        operation_hint='payment',
        description='TEST',
        merchant='TEST',
        balance_after=Decimal('100'),
        raw_text='unique-byn-v13'
    )
    tx,created=create_sms_transaction('wife',p,amount_byn=Decimal('10.00'),fx_rate=Decimal('1'),fx_rate_date=date(2026,8,5))
    assert created
    assert tx.report_amount_minor==1000
    assert tx.amount_byn==Decimal('10')


def test_foreign_conversion_is_stored_separately():
    init_db()
    p=ParsedSms(
        card_mask='4***3730',
        occurred_at=datetime(2026,8,5,10,23,44),
        amount=Decimal('9.99'),
        currency='USD',
        direction='out',
        operation_hint='payment',
        description='IRL APPLE.COM/BILL',
        merchant='APPLE.COM/BILL',
        balance_after=Decimal('24.73'),
        raw_text='unique-usd-v13'
    )
    tx,created=create_sms_transaction(
        'wife',p,
        amount_byn=Decimal('30.47'),
        fx_rate=Decimal('3.05005005'),
        fx_rate_date=date(2026,8,5)
    )
    assert created
    assert tx.currency=='USD'
    assert tx.amount==Decimal('9.99')
    assert tx.amount_byn==Decimal('30.47')
    assert tx.report_amount_minor==3047
    assert tx.fx_rate_date=='2026-08-05'


def test_conversion_math_uses_rate_per_unit():
    import asyncio
    import app.exchange as exchange

    key=('USD','2026-08-05')
    exchange._historical_cache[key]=Decimal('3.05005')
    amount_byn,rate=asyncio.run(
        exchange.convert_foreign_to_byn(
            Decimal('9.99'),
            'USD',
            date(2026,8,5),
        )
    )
    assert rate==Decimal('3.05005')
    assert amount_byn==Decimal('30.47')
