import os
from datetime import datetime

os.environ.setdefault('TELEGRAM_BOT_TOKEN','x')
os.environ.setdefault('TELEGRAM_CHAT_ID','-100')
os.environ.setdefault('API_SECRET','x')
os.environ.setdefault('DATABASE_PATH','/tmp/family-finance-pytest.db')

from app.parser import IgnoredSms, parse_priorbank_sms, operation_type_from_parsed


def test_cash_withdrawal_wording():
    raw='Karta 4***3730 13-08-26 09:53:20. Nalichnye v bankomate 600.00 BYN. BLR BANKOMAT N2 MAGAZIN SA. Balance: 1730.78 BYN Tel. 7299090'
    p=parse_priorbank_sms(raw)
    assert p.operation_hint=='cash'
    assert operation_type_from_parsed(p)=='cash_withdrawal'
    assert p.amount == 600
    assert p.currency == 'BYN'
    assert p.merchant == 'BANKOMAT N2 MAGAZIN SA'


def test_byn_payment_santa():
    raw='Karta 4***3730 13-08-26 10:00:38. Oplata 30.52 BYN. BLR MAGAZIN "SANTA-353". Balance: 1390.26 BYN Tel. 7299090'
    p=parse_priorbank_sms(raw)
    assert p.operation_hint=='payment'
    assert str(p.amount)=='30.52'
    assert p.merchant=='MAGAZIN "SANTA-353"'


def test_usd_purchase():
    raw='Karta 4***3730 05-08-26 10:23:44. Oplata 9.99 USD. IRL APPLE.COM/BILL. Balance: 24.73 BYN Tel. 7299090'
    p=parse_priorbank_sms(raw)
    assert p.operation_hint=='payment'
    assert p.currency=='USD'
    assert str(p.amount)=='9.99'
    assert p.merchant=='APPLE.COM/BILL'
    assert str(p.balance_after)=='24.73'


def test_usd_incoming_transfer():
    raw='Karta 4***3730 11-08-26 21:45:53. Zachislenie perevoda 400.00 USD. BLR HANNA NOVIKAVA. Balance: 1463.52 BYN Tel. 7299090'
    p=parse_priorbank_sms(raw)
    assert p.operation_hint=='income'
    assert p.currency=='USD'
    assert str(p.amount)=='400.00'
    assert p.merchant=='HANNA NOVIKAVA'


def test_direct_card_credit_without_card_mask(monkeypatch):
    raw='12/08 17:34. Na vashu kartu zachisleno 1755.00 BYN. Dostupnaja summa: 2413.97 BYN. Tel. 7299090'
    p=parse_priorbank_sms(raw)
    assert p.operation_hint=='income'
    assert p.direction=='in'
    assert str(p.amount)=='1755.00'
    assert p.card_mask=='priorbank-card'
    assert str(p.balance_after)=='2413.97'


def test_otp_is_ignored():
    raw='Priorbank 05/08 10:10. Code: 537 po karte DK8683. Spravka: 80172899292'
    try:
        parse_priorbank_sms(raw)
    except IgnoredSms:
        pass
    else:
        raise AssertionError('OTP message must be ignored')
