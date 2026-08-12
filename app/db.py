import hashlib, sqlite3, json
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from app.categories import CATEGORIES
from app.config import settings
from app.models import Transaction
from app.parser import operation_type_from_parsed

def to_minor(amount): return int((amount*100).quantize(Decimal('1')))
def now_iso(): return datetime.now(timezone.utc).isoformat()

def ensure_parent(): Path(settings.database_path).expanduser().resolve().parent.mkdir(parents=True,exist_ok=True)

@contextmanager
def connect():
    ensure_parent(); con=sqlite3.connect(settings.database_path,timeout=15); con.row_factory=sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON'); con.execute('PRAGMA journal_mode=WAL')
    try:
        yield con; con.commit()
    finally: con.close()

def init_db():
    with connect() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS categories(code TEXT PRIMARY KEY,title TEXT NOT NULL,emoji TEXT NOT NULL,scope TEXT NOT NULL,kind TEXT NOT NULL,parent_code TEXT NULL);
        CREATE TABLE IF NOT EXISTS transactions(
          id INTEGER PRIMARY KEY AUTOINCREMENT, external_hash TEXT UNIQUE,
          occurred_at TEXT NOT NULL, created_at TEXT NOT NULL, amount_minor INTEGER NOT NULL CHECK(amount_minor>=0), currency TEXT NOT NULL,
          direction TEXT NOT NULL, operation_type TEXT NOT NULL, physical_account TEXT NOT NULL,
          scope TEXT NULL, category_code TEXT NULL, source TEXT NULL, merchant TEXT NULL, description TEXT NULL,
          balance_after_minor INTEGER NULL, raw_sms TEXT NULL, origin TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          telegram_chat_id INTEGER NULL, telegram_message_id INTEGER NULL,
          FOREIGN KEY(category_code) REFERENCES categories(code));
        CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(occurred_at);
        CREATE INDEX IF NOT EXISTS idx_tx_scope ON transactions(scope);
        CREATE INDEX IF NOT EXISTS idx_tx_status ON transactions(status);
        CREATE TABLE IF NOT EXISTS merchant_rules(
          id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_pattern TEXT NOT NULL UNIQUE, scope TEXT NOT NULL,
          operation_type TEXT NOT NULL, category_code TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(category_code) REFERENCES categories(code));
        CREATE TABLE IF NOT EXISTS opening_balances(
          bucket TEXT PRIMARY KEY,
          amount_minor INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_states(
          user_id INTEGER PRIMARY KEY,
          state TEXT NOT NULL,
          payload_json TEXT NOT NULL DEFAULT '{}',
          updated_at TEXT NOT NULL
        );
        ''')
        for c in CATEGORIES:
            con.execute('''INSERT INTO categories(code,title,emoji,scope,kind,parent_code) VALUES(?,?,?,?,?,?)
            ON CONFLICT(code) DO UPDATE SET title=excluded.title,emoji=excluded.emoji,scope=excluded.scope,kind=excluded.kind,parent_code=excluded.parent_code''',
            (c.code,c.title,c.emoji,c.scope,c.kind,c.parent))

def tx_from_row(r):
    return Transaction(id=r['id'],occurred_at=r['occurred_at'],amount_minor=r['amount_minor'],currency=r['currency'],direction=r['direction'],operation_type=r['operation_type'],physical_account=r['physical_account'],scope=r['scope'],category_code=r['category_code'],source=r['source'],merchant=r['merchant'],description=r['description'],balance_after_minor=r['balance_after_minor'],origin=r['origin'],status=r['status'],telegram_chat_id=r['telegram_chat_id'],telegram_message_id=r['telegram_message_id'])

def sms_hash(device,raw): return hashlib.sha256(f"{device}|{' '.join(raw.split())}".encode()).hexdigest()

def create_sms_transaction(device,p):
    h=sms_hash(device,p.raw_text); raw=p.raw_text if settings.store_raw_sms else None
    with connect() as con:
        ex=con.execute('SELECT * FROM transactions WHERE external_hash=?',(h,)).fetchone()
        if ex: return tx_from_row(ex),False
        cur=con.execute('''INSERT INTO transactions(external_hash,occurred_at,created_at,amount_minor,currency,direction,operation_type,physical_account,merchant,description,balance_after_minor,raw_sms,origin,status)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,\'sms\',\'pending\')''',(h,p.occurred_at.isoformat(),now_iso(),to_minor(p.amount),p.currency,p.direction,operation_type_from_parsed(p),p.card_mask,p.merchant,p.description,to_minor(p.balance_after) if p.balance_after is not None else None,raw))
        return tx_from_row(con.execute('SELECT * FROM transactions WHERE id=?',(cur.lastrowid,)).fetchone()),True

def create_manual_transaction(amount,description,user_id,currency='BYN'):
    with connect() as con:
        cur=con.execute('''INSERT INTO transactions(occurred_at,created_at,amount_minor,currency,direction,operation_type,physical_account,description,merchant,origin,status,source)
        VALUES(?,?,?,?,\'out\',\'expense\',\'cash\',?,?,\'manual\',\'pending\',?)''',(datetime.now().isoformat(timespec='seconds'),now_iso(),to_minor(amount),currency.upper(),description or 'Ручная операция',description or None,f'telegram:{user_id}'))
        return tx_from_row(con.execute('SELECT * FROM transactions WHERE id=?',(cur.lastrowid,)).fetchone())

def get_transaction(tx_id):
    with connect() as con:
        r=con.execute('SELECT * FROM transactions WHERE id=?',(tx_id,)).fetchone(); return tx_from_row(r) if r else None

def set_telegram_message(tx_id,chat_id,message_id):
    with connect() as con: con.execute('UPDATE transactions SET telegram_chat_id=?,telegram_message_id=? WHERE id=?',(chat_id,message_id,tx_id))

def update_transaction(tx_id,**kwargs):
    allowed={'scope','category_code','operation_type','direction','status','source'}; vals={k:v for k,v in kwargs.items() if k in allowed and v is not None}
    if vals:
        with connect() as con:
            sql=', '.join(f'{k}=?' for k in vals); con.execute(f'UPDATE transactions SET {sql} WHERE id=?',[*vals.values(),tx_id])
    tx=get_transaction(tx_id)
    if not tx: raise KeyError(tx_id)
    return tx

def reset_transaction(tx_id):
    with connect() as con: con.execute("UPDATE transactions SET scope=NULL,category_code=NULL,status='pending' WHERE id=?",(tx_id,))
    return get_transaction(tx_id)

def find_rule(merchant,operation_type):
    if not merchant: return None
    n=merchant.upper().strip()
    with connect() as con:
        rows=con.execute('SELECT * FROM merchant_rules ORDER BY LENGTH(merchant_pattern) DESC').fetchall()
        return next((r for r in rows if r['merchant_pattern'].upper() in n),None)

def save_rule(merchant,scope,operation_type,category_code):
    p=merchant.upper().strip(); now=now_iso()
    with connect() as con:
        con.execute('''INSERT INTO merchant_rules(merchant_pattern,scope,operation_type,category_code,created_at,updated_at) VALUES(?,?,?,?,?,?)
        ON CONFLICT(merchant_pattern) DO UPDATE SET scope=excluded.scope,operation_type=excluded.operation_type,category_code=excluded.category_code,updated_at=excluded.updated_at''',(p,scope,operation_type,category_code,now,now))

def list_pending(limit=20):
    with connect() as con: return [tx_from_row(r) for r in con.execute("SELECT * FROM transactions WHERE status='pending' ORDER BY occurred_at DESC LIMIT ?",(limit,)).fetchall()]

def query_transactions(start_iso,end_iso):
    with connect() as con: return [tx_from_row(r) for r in con.execute("SELECT * FROM transactions WHERE occurred_at>=? AND occurred_at<? AND status='categorized' ORDER BY occurred_at",(start_iso,end_iso)).fetchall()]

def all_reserve_transactions(end_iso=None):
    sql="SELECT * FROM transactions WHERE status='categorized' AND category_code IN ('reserve_contribution','reserve_to_family')"; params=[]
    if end_iso: sql+=' AND occurred_at<?'; params.append(end_iso)
    with connect() as con: return [tx_from_row(r) for r in con.execute(sql+' ORDER BY occurred_at',params).fetchall()]

def export_rows():
    with connect() as con: return [dict(r) for r in con.execute('''SELECT id,occurred_at,amount_minor,currency,direction,operation_type,physical_account,scope,category_code,source,merchant,description,balance_after_minor,origin,status FROM transactions ORDER BY occurred_at DESC''').fetchall()]


def set_opening_balance(bucket, amount):
    if bucket not in {'family','marketing','reserve'}:
        raise ValueError('Unknown bucket')
    with connect() as con:
        con.execute(
            """INSERT INTO opening_balances(bucket,amount_minor,updated_at)
               VALUES(?,?,?)
               ON CONFLICT(bucket) DO UPDATE SET
                 amount_minor=excluded.amount_minor,
                 updated_at=excluded.updated_at""",
            (bucket,to_minor(amount),now_iso())
        )

def get_opening_balance(bucket):
    with connect() as con:
        row=con.execute(
            'SELECT amount_minor FROM opening_balances WHERE bucket=?',
            (bucket,)
        ).fetchone()
        return int(row['amount_minor']) if row else 0

def get_opening_balances():
    return {
        'family':get_opening_balance('family'),
        'marketing':get_opening_balance('marketing'),
        'reserve':get_opening_balance('reserve'),
    }

def set_user_state(user_id,state,payload=None):
    with connect() as con:
        con.execute(
            """INSERT INTO user_states(user_id,state,payload_json,updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 state=excluded.state,
                 payload_json=excluded.payload_json,
                 updated_at=excluded.updated_at""",
            (user_id,state,json.dumps(payload or {},ensure_ascii=False),now_iso())
        )

def get_user_state(user_id):
    with connect() as con:
        row=con.execute(
            'SELECT state,payload_json FROM user_states WHERE user_id=?',
            (user_id,)
        ).fetchone()
        if not row:
            return None
        try:
            payload=json.loads(row['payload_json'])
        except Exception:
            payload={}
        return {'state':row['state'],'payload':payload}

def clear_user_state(user_id):
    with connect() as con:
        con.execute('DELETE FROM user_states WHERE user_id=?',(user_id,))

def query_all_transactions(end_iso=None):
    sql="SELECT * FROM transactions WHERE status='categorized'"
    params=[]
    if end_iso:
        sql+=' AND occurred_at<?'
        params.append(end_iso)
    sql+=' ORDER BY occurred_at'
    with connect() as con:
        return [tx_from_row(r) for r in con.execute(sql,params).fetchall()]
