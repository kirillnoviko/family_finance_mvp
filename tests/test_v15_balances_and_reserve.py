import os
from datetime import datetime
from decimal import Decimal

os.environ.setdefault('TELEGRAM_BOT_TOKEN','test')
os.environ.setdefault('TELEGRAM_CHAT_ID','-100')
os.environ.setdefault('API_SECRET','test')
os.environ.setdefault('PERIOD_START_DAY','15')

from app.db import (
    create_manual_transaction,init_db,set_opening_balance,update_transaction
)
from app.reporting import balances_at,current_balances,family_report,marketing_report,reserve_report


def tx(amount, when, scope, category, op, direction, desc='test'):
    row=create_manual_transaction(Decimal(str(amount)),desc,1)
    from app.db import connect
    with connect() as con:
        con.execute("UPDATE transactions SET occurred_at=? WHERE id=?",(when,row.id))
    return update_transaction(
        row.id,
        scope=scope,
        category_code=category,
        operation_type=op,
        direction=direction,
        status='categorized',
    )


def test_family_to_reserve_moves_money_without_expense():
    init_db()
    set_opening_balance('family',Decimal('1000'))
    set_opening_balance('marketing',Decimal('500'))
    set_opening_balance('reserve',Decimal('100'))

    tx(200,'2026-08-10T10:00:00','family','reserve_from_family','allocation','out')
    balances=current_balances()

    assert balances['family']==80000
    assert balances['marketing']==50000
    assert balances['reserve']==30000


def test_marketing_to_reserve_moves_money_without_expense():
    init_db()
    set_opening_balance('family',Decimal('1000'))
    set_opening_balance('marketing',Decimal('500'))
    set_opening_balance('reserve',Decimal('100'))

    tx(120,'2026-08-10T10:00:00','marketing','reserve_contribution','allocation','out')
    balances=current_balances()

    assert balances['family']==100000
    assert balances['marketing']==38000
    assert balances['reserve']==22000


def test_period_closing_balance_equals_balance_engine():
    init_db()
    set_opening_balance('family',Decimal('65'))

    tx(500,'2026-08-01T10:00:00','family','salary_kirill','income','in')
    tx(50,'2026-08-02T10:00:00','family','groceries','expense','out')

    start=datetime(2026,8,1)
    end=datetime(2026,9,1)
    expected=balances_at(end.isoformat())['family']

    text=family_report(start,end)
    assert expected==51500
    assert 'Баланс на начало: 65.00 BYN' in text
    assert 'Баланс на конец: 515.00 BYN' in text


def test_reserve_report_splits_family_and_marketing_sources():
    init_db()
    set_opening_balance('reserve',Decimal('100'))
    tx(200,'2026-08-03T10:00:00','family','reserve_from_family','allocation','out')
    tx(300,'2026-08-04T10:00:00','marketing','reserve_contribution','allocation','out')
    tx(50,'2026-08-05T10:00:00','family','reserve_to_family','transfer','in')

    text=reserve_report(datetime(2026,8,1),datetime(2026,9,1))
    assert 'Из маркетинга в НЗ: 300.00 BYN' in text
    assert 'Из семьи в НЗ: 200.00 BYN' in text
    assert 'Из НЗ в семью: 50.00 BYN' in text
    assert 'Баланс на конец: 550.00 BYN' in text
