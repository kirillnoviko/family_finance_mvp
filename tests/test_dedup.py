import os
from pathlib import Path
os.environ.setdefault('TELEGRAM_BOT_TOKEN','x');os.environ.setdefault('TELEGRAM_CHAT_ID','-100');os.environ.setdefault('API_SECRET','x');os.environ.setdefault('DATABASE_PATH','/tmp/ff-parser.db')
from app.db import create_sms_transaction,init_db
from app.parser import parse_priorbank_sms

def test_sms_dedup():
    init_db()
    raw='Karta 4***3730 11-08-26 16:18:52. Oplata 60.00 BYN. BLR KAFE PRIZMA PARK. Balance: 101.76 BYN Tel. 7299090'
    p=parse_priorbank_sms(raw)
    first,created1=create_sms_transaction('wife-dedup',p)
    second,created2=create_sms_transaction('wife-dedup',p)
    assert created1 is True
    assert created2 is False
    assert first.id==second.id
