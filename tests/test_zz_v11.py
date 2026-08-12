import os
from datetime import date
from decimal import Decimal

os.environ.setdefault('TELEGRAM_BOT_TOKEN','x')
os.environ.setdefault('TELEGRAM_CHAT_ID','-100')
os.environ.setdefault('API_SECRET','x')
os.environ.setdefault('DATABASE_PATH','/tmp/ff-parser.db')
os.environ.setdefault('PERIOD_START_DAY','15')

from app.db import init_db,set_opening_balance,get_opening_balances
from app.reporting import financial_period,previous_financial_period,current_calendar_month

def test_opening_balances():
    init_db()
    set_opening_balance('marketing',Decimal('5000'))
    set_opening_balance('reserve',Decimal('2000'))
    b=get_opening_balances()
    assert b['marketing']==500000
    assert b['reserve']==200000

def test_periods():
    s,e=financial_period(date(2026,8,12))
    assert str(s.date())=='2026-07-15'
    assert str(e.date())=='2026-08-15'
    s,e=previous_financial_period(date(2026,8,12))
    assert str(s.date())=='2026-06-15'
    assert str(e.date())=='2026-07-15'
    s,e=current_calendar_month(date(2026,8,12))
    assert str(s.date())=='2026-08-01'
    assert str(e.date())=='2026-09-01'
