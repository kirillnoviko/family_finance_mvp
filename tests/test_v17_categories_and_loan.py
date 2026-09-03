import os
from datetime import datetime
from decimal import Decimal

os.environ.setdefault('TELEGRAM_BOT_TOKEN','test')
os.environ.setdefault('TELEGRAM_CHAT_ID','-100')
os.environ.setdefault('API_SECRET','test')
os.environ.setdefault('PERIOD_START_DAY','15')
os.environ.setdefault('DEBT_INITIAL_BALANCE','35536')
os.environ.setdefault('LOAN_ANNUAL_RATE','14.56')
os.environ.setdefault('LOAN_START_DATE','2026-08-14')

from app.categories import children
from app.db import (
    create_manual_transaction,init_db,debt_payment_details,debt_summary,
    set_opening_balance,update_transaction
)
from app.reporting import current_balances,marketing_report
from app.telegram import loan_text


def add_loan(amount,when,scope):
    tx=create_manual_transaction(Decimal(str(amount)),'кредит',1)
    from app.db import connect
    with connect() as con:
        con.execute("UPDATE transactions SET occurred_at=? WHERE id=?",(when,tx.id))
    return update_transaction(
        tx.id,
        scope=scope,
        category_code='mortgage_payment',
        operation_type='expense',
        direction='out',
        status='categorized',
        source='Семья' if scope=='family' else 'Маркетинг'
    )


def test_requested_categories_exist():
    assert {c.code for c in children('beauty')}=={'beauty_wife','beauty_kirill'}
    assert {c.code for c in children('car_service')}=={'car_wash','car_repair','car_parts'}
    assert {c.code for c in children('sport')}=={'sport_equipment','sport_membership'}


def test_first_payment_month_pays_interest_then_principal():
    init_db()
    tx=add_loan(1000,'2026-08-14T10:00:00','family')
    d=debt_payment_details(tx.id)
    assert d['interest_paid_minor']==43117
    assert d['principal_paid_minor']==56883
    assert d['balance_after_minor']==3496717


def test_second_payment_same_month_all_goes_to_principal():
    init_db()
    add_loan(1000,'2026-08-14T10:00:00','family')
    tx2=add_loan(500,'2026-08-20T10:00:00','marketing')
    d=debt_payment_details(tx2.id)
    assert d['interest_paid_minor']==0
    assert d['principal_paid_minor']==50000
    assert d['balance_after_minor']==3446717


def test_new_month_recalculates_interest_on_remaining_principal():
    init_db()
    add_loan(1000,'2026-08-14T10:00:00','family')
    add_loan(500,'2026-08-20T10:00:00','marketing')
    tx3=add_loan(1000,'2026-09-05T10:00:00','family')
    d=debt_payment_details(tx3.id)
    assert d['interest_paid_minor']==41820
    assert d['principal_paid_minor']==58180
    assert d['balance_after_minor']==3388537


def test_payment_reduces_source_balance_full_amount():
    init_db()
    set_opening_balance('family',Decimal('2000'))
    set_opening_balance('marketing',Decimal('3000'))
    add_loan(1000,'2026-08-14T10:00:00','family')
    add_loan(500,'2026-08-20T10:00:00','marketing')
    b=current_balances()
    assert b['family']==100000
    assert b['marketing']==250000


def test_marketing_report_includes_apartment_loan():
    init_db()
    set_opening_balance('marketing',Decimal('3000'))
    add_loan(500,'2026-08-20T10:00:00','marketing')
    text=marketing_report(datetime(2026,8,1),datetime(2026,9,1))
    assert 'Кредит на квартиру — 500.00 BYN' in text


def test_loan_screen_contains_current_balance():
    init_db()
    add_loan(1000,'2026-08-14T10:00:00','family')
    text=loan_text()
    assert 'Остаток тела: 34967.17 BYN' in text
    assert 'Ставка: 14.56% годовых' in text
