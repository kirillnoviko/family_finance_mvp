import os
from pathlib import Path

DB=os.environ.get('DATABASE_PATH','/tmp/family-finance-pytest.db')
for suffix in ('','-wal','-shm'):
    try: Path(DB+suffix).unlink()
    except FileNotFoundError: pass

# The full suite normally imports config earlier; these defaults are sufficient
# when this test is run alone as well.
os.environ.setdefault('TELEGRAM_BOT_TOKEN','x')
os.environ.setdefault('TELEGRAM_CHAT_ID','-100')
os.environ.setdefault('API_SECRET','x')
os.environ.setdefault('DATABASE_PATH','/tmp/family-finance-pytest.db')

from app.db import create_sms_transaction, init_db
from app.parser import parse_priorbank_sms
from app.telegram import initial_keyboard


def test_cash_is_pending_choice_not_expense():
    init_db()
    raw='Karta 4***3730 13-08-26 09:54:23. Nalichnye v bankomate 310.00 BYN. BLR BANKOMAT N2 MAGAZIN SA. Balance: 1420.78 BYN Tel. 7299090'
    tx,created=create_sms_transaction('cash-test-unique',parse_priorbank_sms(raw))
    assert created is True
    assert tx.operation_type=='cash_withdrawal'
    assert tx.status=='pending'
    labels=[button['text'] for row in initial_keyboard(tx)['inline_keyboard'] for button in row]
    assert '🏠 Расход семьи' in labels
    assert '📈 Расход маркетинга' in labels
    assert '👩‍💻 Зарплата помощнице' in labels
    assert '💵 Оставил наличными' in labels
