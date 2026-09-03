import os
from datetime import datetime
from decimal import Decimal

os.environ.setdefault('TELEGRAM_BOT_TOKEN','test')
os.environ.setdefault('TELEGRAM_CHAT_ID','-100')
os.environ.setdefault('API_SECRET','test')
os.environ.setdefault('PERIOD_START_DAY','15')

from app.db import create_manual_transaction,init_db,set_opening_balance,update_transaction
from app.reporting import family_report
from app.telegram import subcategory_keyboard


def add(amount, when, category):
    tx=create_manual_transaction(Decimal(str(amount)),category,1)
    from app.db import connect
    with connect() as con:
        con.execute("UPDATE transactions SET occurred_at=? WHERE id=?",(when,tx.id))
    return update_transaction(
        tx.id,
        scope='family',
        category_code=category,
        operation_type='expense',
        direction='out',
        status='categorized',
    )


def test_family_stats_show_subcategories_and_percent_of_total():
    init_db()
    set_opening_balance('family',Decimal('1000'))
    add(300,'2026-08-02T10:00:00','groceries')
    add(100,'2026-08-03T10:00:00','cafe')
    add(100,'2026-08-04T10:00:00','taxi')

    text=family_report(datetime(2026,8,1),datetime(2026,9,1))

    assert 'Расходы по подкатегориям' in text
    assert 'Продукты — 300.00 BYN (60.0%)' in text
    assert 'Кафе / рестораны — 100.00 BYN (20.0%)' in text
    assert 'Такси — 100.00 BYN (20.0%)' in text
    assert '🍏 Еда — 400.00 BYN' not in text


def test_old_root_category_is_visible_as_unspecified():
    init_db()
    set_opening_balance('family',Decimal('1000'))
    add(50,'2026-08-05T10:00:00','food')

    text=family_report(datetime(2026,8,1),datetime(2026,9,1))
    assert 'Еда — без уточнения' in text


def test_subcategory_keyboard_no_longer_allows_root_finalization():
    kb=subcategory_keyboard(type('T',(),{'id':123})(),'food')
    serialized=str(kb)
    assert 'Оставить' not in serialized
    assert 'groceries' in serialized
    assert 'cafe' in serialized
    assert 'delivery' in serialized
