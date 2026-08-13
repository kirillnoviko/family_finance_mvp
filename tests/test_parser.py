import os
os.environ.setdefault('TELEGRAM_BOT_TOKEN','x');os.environ.setdefault('TELEGRAM_CHAT_ID','-100');os.environ.setdefault('API_SECRET','x');os.environ.setdefault('DATABASE_PATH','/tmp/family-finance-pytest.db')
from app.parser import parse_priorbank_sms
SAMPLES=[
('Karta 4***3730 11-08-26 10:34:07. Zachislenie perevoda 442.99 BYN. BLR KIRYL NOVIKAU. Balance: 605.96 BYN Tel. 7299090','in','income','442.99','KIRYL NOVIKAU'),
('Karta 4***3730 11-08-26 10:42:36. Perevod 442.00 BYN. BLR P2P SDBO NO FEE. Balance: 163.96 BYN Tel. 7299090','out','transfer','442.00','P2P SDBO NO FEE'),
('Karta 4***3730 11-08-26 16:02:38. Oplata 2.20 BYN. BLR KIOSK N700029 BAPB. Balance: 161.76 BYN Tel. 7299090','out','payment','2.20','KIOSK N700029 BAPB'),
('Karta 4***3730 11-08-26 16:18:52. Oplata 60.00 BYN. BLR KAFE PRIZMA PARK. Balance: 101.76 BYN Tel. 7299090','out','payment','60.00','KAFE PRIZMA PARK'),
('Karta 4***3730 09-08-26 20:37:36. Oplata 96.22 BYN. BLR TO Gippo. Balance: 183.47 BYN Tel. 7299090','out','payment','96.22','Gippo'),
('Karta 4***3730 10-08-26 11:05:21. Oplata 5.58 BYN. BLR "PEREKRESTOK TSENTROPOL". Balance: 132.85 BYN Tel. 7299090','out','payment','5.58','PEREKRESTOK TSENTROPOL'),
('Karta 4***3730 10-08-26 14:07:55. Oplata 5.85 BYN. BLR APTEKA N134. Balance: 127.00 BYN Tel. 7299090','out','payment','5.85','APTEKA N134')]
def test_samples():
    for raw,d,h,a,m in SAMPLES:
        p=parse_priorbank_sms(raw); assert p.card_mask=='4***3730'; assert p.direction==d; assert p.operation_hint==h; assert str(p.amount)==a; assert p.currency=='BYN'; assert p.merchant==m
