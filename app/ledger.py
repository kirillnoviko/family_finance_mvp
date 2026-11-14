import os, sqlite3, hashlib, json
from contextlib import contextmanager
from pathlib import Path
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_HALF_UP
from app.categories import CATEGORIES

SCOPES = {'family':'Семья','marketing':'Маркетинг','reserve':'НЗ','investment':'Инвестиции'}
TZ = ZoneInfo('Europe/Minsk')
def today(): return datetime.now(TZ).date()
def minor(value):
    d = Decimal(str(value))
    if not d.is_finite() or abs(d)>Decimal('1000000000'): raise ValueError('Некорректная сумма')
    return int((d*100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
def db_path():
    return os.getenv('DATABASE_PATH') or str(Path(os.getenv('RAILWAY_VOLUME_MOUNT_PATH') or './data')/'finance-v3.db')
@contextmanager
def db():
    p=Path(db_path()); p.parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(p,timeout=15); c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON'); c.execute('PRAGMA busy_timeout=15000')
    try:
        yield c; c.commit()
    except BaseException:
        c.rollback(); raise
    finally: c.close()
def init():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS v3_categories(id INTEGER PRIMARY KEY, code TEXT UNIQUE, name TEXT NOT NULL, scope TEXT NOT NULL, kind TEXT NOT NULL, parent_id INTEGER REFERENCES v3_categories(id), archived INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS v3_transactions(id INTEGER PRIMARY KEY, day TEXT NOT NULL, kind TEXT NOT NULL, scope TEXT, target TEXT, category_id INTEGER REFERENCES v3_categories(id), amount INTEGER CHECK(amount IS NULL OR amount>0), currency TEXT NOT NULL DEFAULT 'BYN', original INTEGER NOT NULL, description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'posted', sms_hash TEXT UNIQUE, hint TEXT, merchant TEXT, notified INTEGER DEFAULT 0, fx_rate TEXT, device TEXT, occurred_at TEXT, card_mask TEXT);
        CREATE INDEX IF NOT EXISTS v3_tx_day ON v3_transactions(day,status);
        CREATE TABLE IF NOT EXISTS v3_limits(id INTEGER PRIMARY KEY, name TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>0), scope TEXT NOT NULL, category_ids TEXT NOT NULL, start_month TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS v3_alerts(id INTEGER PRIMARY KEY, limit_id INTEGER NOT NULL, month TEXT NOT NULL, threshold INTEGER NOT NULL, message TEXT NOT NULL, sent INTEGER DEFAULT 0, UNIQUE(limit_id,month,threshold));
        CREATE TABLE IF NOT EXISTS v3_valuations(id INTEGER PRIMARY KEY, day TEXT NOT NULL UNIQUE, amount INTEGER NOT NULL CHECK(amount>=0), ledger_snapshot INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS v3_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS v3_rules(merchant TEXT NOT NULL,kind TEXT NOT NULL,scope TEXT NOT NULL,category_id INTEGER NOT NULL REFERENCES v3_categories(id),PRIMARY KEY(merchant,kind));
        ''')
        if not c.execute("SELECT 1 FROM v3_settings WHERE key='initialized'").fetchone():
            ids={}
            for x in CATEGORIES:
                if x.scope not in ('family','marketing') or x.kind not in ('income','expense'): continue
                cur=c.execute('INSERT INTO v3_categories(code,name,scope,kind,parent_id) VALUES(?,?,?,?,?)',(x.code,x.title,x.scope,x.kind,ids.get(x.parent))); ids[x.code]=cur.lastrowid
            c.execute("INSERT INTO v3_categories(code,name,scope,kind) VALUES('child_benefit','Детские','family','income')")
            for scope in ('reserve','investment'):
                for kind,name in [('income','Прочие поступления'),('expense','Прочие расходы')]: c.execute('INSERT INTO v3_categories(code,name,scope,kind) VALUES(?,?,?,?)',(scope+'_'+kind,name,scope,kind))
            c.execute("INSERT INTO v3_settings VALUES('initialized','1')")
def rows(c,sql,args=()): return [dict(r) for r in c.execute(sql,args)]
def categories(c): return rows(c,'SELECT * FROM v3_categories ORDER BY id')
def descendants(c, ids):
    result=set(ids); cats=categories(c)
    while True:
        new=result|{r['id'] for r in cats if r['parent_id'] in result}
        if new==result: return result
        result=new
def month_bounds(month):
    start=date.fromisoformat(month+'-01'); end=(start.replace(day=28)+timedelta(days=4)).replace(day=1); return start.isoformat(),end.isoformat()
def limits_status(c,month):
    start,end=month_bounds(month); out=[]
    for l in rows(c,'SELECT * FROM v3_limits WHERE active=1 AND start_month<=? ORDER BY id',(month,)):
        ids=descendants(c,json.loads(l['category_ids'])); tx=rows(c,"SELECT * FROM v3_transactions WHERE status='posted' AND kind IN ('expense','refund') AND scope=? AND day>=? AND day<?",(l['scope'],start,end))
        spent=sum(t['amount']*(1 if t['kind']=='expense' else -1) for t in tx if t['category_id'] in ids)
        l.update(category_ids=json.loads(l['category_ids']),spent=spent,remaining=l['amount']-spent,percent=round(spent/l['amount']*100,2)); out.append(l)
    return out
def queue_alerts(c):
    month=today().strftime('%Y-%m')
    for l in limits_status(c,month):
        for threshold in (70,90,100):
            if (l['spent'] > l['amount'] if threshold==100 else l['spent']*100 >= l['amount']*threshold):
                text=f"Лимит «{l['name']}»: {'превышен' if threshold==100 else 'осталось не более '+str(100-threshold)+'%'}\nПотрачено {l['spent']/100:.2f} из {l['amount']/100:.2f} BYN. Остаток {l['remaining']/100:.2f} BYN."
                c.execute('INSERT OR IGNORE INTO v3_alerts(limit_id,month,threshold,message) VALUES(?,?,?,?)',(l['id'],month,threshold,text))
def balances(c,end):
    result={s:0 for s in SCOPES}
    for t in rows(c,"SELECT * FROM v3_transactions WHERE status='posted' AND day<=?",(end,)):
        if t['kind'] in ('income','refund','opening'): result[t['scope']]+=t['amount']
        elif t['kind']=='expense': result[t['scope']]-=t['amount']
        elif t['kind']=='transfer': result[t['scope']]-=t['amount']; result[t['target']]+=t['amount']
    return result
def investment(c,end):
    ts=rows(c,"SELECT * FROM v3_transactions WHERE status='posted' AND day<=? AND (scope='investment' OR target='investment')",(end,))
    contributions=sum(t['amount'] for t in ts if t['target']=='investment' or t['kind'] in ('income','opening'))
    withdrawals=sum(t['amount'] for t in ts if t['scope']=='investment' and t['kind']=='transfer')
    net=contributions-withdrawals
    v=c.execute('SELECT * FROM v3_valuations WHERE day<=? ORDER BY day DESC LIMIT 1',(end,)).fetchone()
    # After valuation, only cash movements adjust the last known value, not market prices.
    value=None
    if v:
        value=v['amount']+balances(c,end)['investment']-v['ledger_snapshot']
    profit=None if value is None else value-net
    return dict(contributions=contributions,withdrawals=withdrawals,net=net,value=value,profit=profit,percent=round(profit/net*100,2) if profit is not None and net>0 else None,value_percent=round(value/net*100,2) if value is not None and net>0 else None,valuation_date=v['day'] if v else None)
def transactions(c,start,end,scope=None):
    return rows(c,"SELECT t.*,cat.name category FROM v3_transactions t LEFT JOIN v3_categories cat ON cat.id=t.category_id WHERE day BETWEEN ? AND ? AND (? IS NULL OR t.scope=? OR t.target=?) ORDER BY day DESC,t.id DESC",(start,end,scope,scope,scope))
def report(c,start,end,scope,limit_id=None):
    ts=[t for t in transactions(c,start,end,scope) if t['status']=='posted']; cats={r['id']:r for r in categories(c)}
    groups={}; income_groups={}; weeks={}; income=expense=0; incoming=outgoing=0; selected_cache={}
    first=date.fromisoformat(start); last=date.fromisoformat(end); week=first-timedelta(days=first.weekday())
    while week<=last:
        k=week.isoformat(); weeks[k]={'week':k,'total':0,'limited':0}; week+=timedelta(days=7)
    for t in ts:
        a=t['amount']; k=t['kind']; d=date.fromisoformat(t['day']); week=(d-timedelta(days=d.weekday())).isoformat(); w=weeks.setdefault(week,{'week':week,'total':0,'limited':0})
        if k=='income':
            income+=a; key=t['category_id'] or 0; cat=cats.get(key); g=income_groups.setdefault(key,{'id':key,'name':cat['name'] if cat else 'Без категории','amount':0,'ids':[]}); g['amount']+=a; g['ids'].append(t['id'])
        if k in ('expense','refund'):
            sign=1 if k=='expense' else -1; expense+=sign*a; w['total']+=sign*a
            cat=cats.get(t['category_id']); root=cat
            while root and root['parent_id']: root=cats.get(root['parent_id'])
            key=root['id'] if root else 0; g=groups.setdefault(key,{'id':key,'name':root['name'] if root else 'Без категории','amount':0,'ids':[]}); g['amount']+=sign*a; g['ids'].append(t['id'])
            key=(d.strftime('%Y-%m'),t['scope'])
            if key not in selected_cache:
                selected=set()
                for l in limits_status(c,key[0]):
                    if l['scope']==t['scope'] and (limit_id is None or l['id']==limit_id): selected |= descendants(c,l['category_ids'])
                selected_cache[key]=selected
            selected=selected_cache[key]
            if t['category_id'] in selected: w['limited']+=sign*a
        if k=='transfer':
            if scope is None or t['target']==scope: incoming+=a
            if scope is None or t['scope']==scope: outgoing+=a
    before=balances(c,(date.fromisoformat(start)-timedelta(days=1)).isoformat()) if start!='0001-01-01' else dict.fromkeys(SCOPES,0); after=balances(c,end)
    return dict(income=income,expense=expense,transfer_in=incoming,transfer_out=outgoing,opening=before[scope] if scope else sum(before.values()),closing=after[scope] if scope else sum(after.values()),groups=sorted(groups.values(),key=lambda g:-g['amount']),income_groups=sorted(income_groups.values(),key=lambda g:-g['amount']),weeks=sorted(weeks.values(),key=lambda w:w['week']),transactions=ts)
