import os
from datetime import datetime
from decimal import Decimal

os.environ.setdefault('TELEGRAM_BOT_TOKEN','test')
os.environ.setdefault('TELEGRAM_CHAT_ID','-100')
os.environ.setdefault('API_SECRET','test')
os.environ.setdefault('PERIOD_START_DAY','15')

from app.db import create_manual_transaction,init_db,update_transaction
from app.telegram import operations_page_text


def add(amount, desc, scope, category, operation_type, direction):
    tx=create_manual_transaction(Decimal(str(amount)),desc,1)
    from app.db import connect
    with connect() as con:
        con.execute("UPDATE transactions SET occurred_at='2026-08-13T10:00:00' WHERE id=?",(tx.id,))
    return update_transaction(
        tx.id,
        scope=scope,
        category_code=category,
        operation_type=operation_type,
        direction=direction,
        status='categorized',
    )


def test_family_operations_list():
    init_db()
    add(30.52,'MAGAZIN "SANTA-353"','family','groceries','expense','out')
    text,count,page,pages=operations_page_text(
        'family',
        datetime(2026,8,1),
        datetime(2026,9,1),
        0,
    )
    assert count==1
    assert '13.08 10:00' in text
    assert '-30.52 BYN' in text
    assert 'SANTA-353' in text


def test_reserve_operations_have_direction_names():
    init_db()
    add(500,'reserve','marketing','reserve_contribution','allocation','out')
    add(200,'reserve->family','family','reserve_to_family','transfer','in')
    text,count,page,pages=operations_page_text(
        'reserve',
        datetime(2026,8,1),
        datetime(2026,9,1),
        0,
    )
    assert count==2
    assert '+500.00 BYN | Маркетинг → НЗ' in text
    assert '-200.00 BYN | НЗ → Семья' in text


def test_reserve_operations_show_family_contribution():
    init_db()
    add(75,'family reserve','family','reserve_from_family','allocation','out')
    text,count,page,pages=operations_page_text(
        'reserve',
        datetime(2026,8,1),
        datetime(2026,9,1),
        0,
    )
    assert 'Семья → НЗ' in text
    assert '+75.00 BYN' in text
