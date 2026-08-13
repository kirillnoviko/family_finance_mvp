import os,sqlite3
from pathlib import Path
from datetime import datetime
from decimal import Decimal
DB=os.environ.get('DATABASE_PATH','/tmp/family-finance-pytest.db')
os.environ['TELEGRAM_BOT_TOKEN']='x';os.environ['TELEGRAM_CHAT_ID']='-100';os.environ['API_SECRET']='x';os.environ.setdefault('DATABASE_PATH',DB);os.environ['PERIOD_START_DAY']='15'
from app.db import create_manual_transaction,init_db,update_transaction
from app.reporting import family_report,marketing_report,reserve_report

def add(amount,desc,scope,cat,op,direction,source=''):
    tx=create_manual_transaction(Decimal(str(amount)),desc,1)
    con=sqlite3.connect(DB);con.execute("UPDATE transactions SET occurred_at='2026-08-01T12:00:00' WHERE id=?",(tx.id,));con.commit();con.close()
    update_transaction(tx.id,scope=scope,category_code=cat,operation_type=op,direction=direction,status='categorized',source=source)

def test_reports():
    init_db();add(3000,'salary','family','salary_kirill','income','in','Кирилл');add(2000,'salary','family','salary_wife','income','in','Жена');add(800,'GIPPO','family','groceries','expense','out');add(200,'APTEKA','family','pharmacy','expense','out');add(5000,'Panda','marketing','marketing_client_income','income','in','Panda');add(2000,'assistant','marketing','assistant_salary','expense','out');add(500,'tax','marketing','tax','expense','out');add(1500,'reserve','marketing','reserve_contribution','allocation','out');add(300,'reserve->family','family','reserve_to_family','transfer','in','НЗ')
    s=datetime.fromisoformat('2026-07-15T00:00:00');e=datetime.fromisoformat('2026-08-15T00:00:00')
    f=family_report(s,e);m=marketing_report(s,e);r=reserve_report(s,e)
    assert '5 000.00 BYN' in f and '300.00 BYN' in f and '1 000.00 BYN' in f
    assert '5 000.00 BYN' in m and '2 000.00 BYN' in m
    assert '1 500.00 BYN' in r and '300.00 BYN' in r
