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
        if 'period' not in {r['name'] for r in c.execute('PRAGMA table_info(v3_limits)')}:
            c.execute("ALTER TABLE v3_limits ADD COLUMN period TEXT NOT NULL DEFAULT 'monthly'")
        c.execute('CREATE TABLE IF NOT EXISTS v33_primary_limits(category_id INTEGER PRIMARY KEY REFERENCES v3_categories(id),limit_id INTEGER NOT NULL REFERENCES v3_limits(id))')
        c.execute('CREATE TABLE IF NOT EXISTS v31_purchase_alerts(id INTEGER PRIMARY KEY, limit_id INTEGER NOT NULL, tx_id INTEGER NOT NULL, message TEXT NOT NULL, sent INTEGER DEFAULT 0, UNIQUE(limit_id,tx_id))')
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
def week_start(d): return d-timedelta(days=(d.weekday()-5)%7)
def period_bounds(d,period):
    if period=='weekly':
        start=week_start(d); return start.isoformat(),(start+timedelta(days=7)).isoformat()
    return month_bounds(d.strftime('%Y-%m'))
def limit_spent(c,l,start,end):
    ids=descendants(c,json.loads(l['category_ids']) if isinstance(l['category_ids'],str) else l['category_ids'])
    return sum(t['amount']*(1 if t['kind']=='expense' else -1) for t in rows(c,"SELECT * FROM v3_transactions WHERE status='posted' AND kind IN ('expense','refund') AND scope=? AND day>=? AND day<?",(l['scope'],start,end)) if t['category_id'] in ids)
def limits_status(c,month,as_of=None):
    d=date.fromisoformat(as_of) if as_of else (today() if month==today().strftime('%Y-%m') else date.fromisoformat(month+'-01'))
    out=[]
    for l in rows(c,'SELECT * FROM v3_limits WHERE active=1 AND start_month<=? ORDER BY id',(month,)):
        start,end=period_bounds(d,l['period']); spent=limit_spent(c,l,start,end)
        l.update(category_ids=json.loads(l['category_ids']),spent=spent,remaining=l['amount']-spent,percent=round(spent/l['amount']*100,2),period_start=start,period_end=(date.fromisoformat(end)-timedelta(days=1)).isoformat())
        if l['period']=='weekly':
            ms,me=month_bounds(d.strftime('%Y-%m')); days=(date.fromisoformat(me)-date.fromisoformat(ms)).days
            budget=int((Decimal(l['amount'])*days/7).quantize(Decimal('1'),rounding=ROUND_HALF_UP)); actual=limit_spent(c,l,ms,me)
            l.update(month_budget=budget,month_spent=actual,month_remaining=budget-actual)
        out.append(l)
    return out
def queue_alerts(c,tx_id=None):
    # A purchase is notified once per matching limit; no periodic threshold spam.
    if tx_id is None: return
    t=c.execute("SELECT * FROM v3_transactions WHERE id=? AND status='posted' AND kind='expense'",(tx_id,)).fetchone()
    if not t: return
    d=date.fromisoformat(t['day'])
    if d>today(): return
    for l in limits_status(c,d.strftime('%Y-%m'),d.isoformat()):
        if l['scope']!=t['scope'] or t['category_id'] not in descendants(c,l['category_ids']): continue
        # Do not send stale warnings when historical transactions are entered.
        if not (l['period_start']<=today().isoformat()<=l['period_end']): continue
        remain=l['remaining']; spent=l['spent']; amount=l['amount']
        status='🚨 Лимит превышен' if remain<0 else '⚠️ Лимит исчерпан' if remain==0 else '⚠️ Осталось не более 10%' if remain*100<=amount*10 else '⚠️ Осталось не более 30%' if remain*100<=amount*30 else 'Остаток лимита'
        text=f"{status} «{l['name']}»\nПокупка: {t['amount']/100:.2f} BYN\nПериод: {l['period_start']} — {l['period_end']}\nПотрачено: {spent/100:.2f} из {amount/100:.2f} BYN\n{'Перерасход' if remain<0 else 'Осталось'}: {abs(remain)/100:.2f} BYN"
        c.execute('INSERT OR IGNORE INTO v31_purchase_alerts(limit_id,tx_id,message) VALUES(?,?,?)',(l['id'],tx_id,text))
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
def ancestry(cats,cat_id):
    result=[]; node=cats.get(cat_id)
    while node and node['id'] not in result:
        result.append(node['id']); node=cats.get(node['parent_id'])
    return result

def active_limits(c,month):
    ls=rows(c,'SELECT * FROM v3_limits WHERE active=1 AND start_month<=? ORDER BY id',(month,))
    for l in ls: l['category_ids']=json.loads(l['category_ids'])
    return ls

def match_limits(cats,ls,category_id,scope):
    chain=ancestry(cats,category_id); ranked=[]
    for l in ls:
        if l['scope']!=scope: continue
        distances=[chain.index(i) for i in l['category_ids'] if i in chain]
        if distances: ranked.append((0 if l['period']=='weekly' else 1,min(distances),l['id'],l))
    return [x[3] for x in sorted(ranked,key=lambda x:x[:3])]

def primary_limit(cats,primary,category_id,matches):
    by_id={l['id']:l for l in matches}
    for i in ancestry(cats,category_id):
        if primary.get(i) in by_id: return by_id[primary[i]]
    return matches[0] if matches else None

def category_limit_mapping(c):
    cats={r['id']:r for r in categories(c)}; ls=active_limits(c,today().strftime('%Y-%m'))
    primary={r['category_id']:r['limit_id'] for r in rows(c,'SELECT * FROM v33_primary_limits')}
    result=[]
    for cat in cats.values():
        if cat['kind']!='expense' or cat['archived']: continue
        matches=match_limits(cats,ls,cat['id'],cat['scope']); chosen=primary_limit(cats,primary,cat['id'],matches)
        result.append(dict(category_id=cat['id'],limit_ids=[l['id'] for l in matches],primary_id=primary.get(cat['id']),effective_id=chosen['id'] if chosen else None))
    return result

def report(c,start,end,scope,limit_id=None,limit_period=None):
    ts=[t for t in transactions(c,start,end,scope) if t['status']=='posted']; cats={r['id']:r for r in categories(c)}
    primary={r['category_id']:r['limit_id'] for r in rows(c,'SELECT * FROM v33_primary_limits')}
    groups={}; income_groups={}; transfer_groups={}; limit_groups={}; weeks={}; income=expense=0; incoming=outgoing=0; cache={}; match_cache={}
    first=date.fromisoformat(start); last=date.fromisoformat(end); week=week_start(first)
    while week<=last:
        k=week.isoformat(); weeks[k]={'week':k,'total':0,'limited':0,'ids':[],'limited_ids':[]}; week+=timedelta(days=7)
    for t in ts:
        a=t['amount']; k=t['kind']; d=date.fromisoformat(t['day']); week=week_start(d).isoformat(); w=weeks[week]
        if k=='income':
            income+=a; key=t['category_id'] or 0; cat=cats.get(key); g=income_groups.setdefault(key,{'id':key,'name':cat['name'] if cat else 'Без категории','amount':0,'ids':[]}); g['amount']+=a; g['ids'].append(t['id'])
        if k in ('expense','refund'):
            sign=1 if k=='expense' else -1; expense+=sign*a; w['total']+=sign*a; w['ids'].append(t['id'])
            chain=ancestry(cats,t['category_id']); root=cats.get(chain[-1]) if chain else None
            key=root['id'] if root else 0; g=groups.setdefault(key,{'id':key,'name':root['name'] if root else 'Без категории','amount':0,'ids':[]}); g['amount']+=sign*a; g['ids'].append(t['id'])
            m=d.strftime('%Y-%m'); cache_key=(m,t['scope'],t['category_id'])
            if m not in cache: cache[m]=active_limits(c,m)
            if cache_key not in match_cache: match_cache[cache_key]=match_limits(cats,cache[m],t['category_id'],t['scope'])
            matches=match_cache[cache_key]
            if any((limit_id is None or l['id']==limit_id) and (limit_period is None or l['period']==limit_period) for l in matches):
                w['limited']+=sign*a; w['limited_ids'].append(t['id'])
            # Exclusive analytic buckets, not a change to independent budget calculations.
            chosen=primary_limit(cats,primary,t['category_id'],matches)
            bucket='weekly' if chosen and chosen['period']=='weekly' else 'l'+str(chosen['id']) if chosen else 'c'+str(root['id'] if root else 0)
            name='Все недельные лимиты' if bucket=='weekly' else chosen['name']+' · месяц' if chosen else (root['name'] if root else 'Без категории')
            lg=limit_groups.setdefault(bucket,dict(id=bucket,name=name,amount=0,ids=[],subgroups={}))
            lg['amount']+=sign*a; lg['ids'].append(t['id'])
            if bucket=='weekly':
                sg=lg['subgroups'].setdefault(chosen['id'],dict(id=chosen['id'],name=chosen['name'],amount=0,ids=[])); sg['amount']+=sign*a; sg['ids'].append(t['id'])
        if k=='transfer':
            if scope is None or t['target']==scope: incoming+=a
            if scope is None or t['scope']==scope:
                outgoing+=a; dest=t['target']; g=transfer_groups.setdefault(dest,dict(id=dest,name=SCOPES[dest],amount=0,ids=[])); g['amount']+=a; g['ids'].append(t['id'])
    before=balances(c,(date.fromisoformat(start)-timedelta(days=1)).isoformat()) if start!='0001-01-01' else dict.fromkeys(SCOPES,0); after=balances(c,end)
    for g in limit_groups.values(): g['subgroups']=sorted(g['subgroups'].values(),key=lambda x:-x['amount'])
    opening=before[scope] if scope else sum(before.values()); closing=after[scope] if scope else sum(after.values())
    opening_added=sum(t['amount'] for t in ts if t['kind']=='opening')
    available=opening+opening_added+income+(incoming if scope else 0)
    # All-contours view consolidates internal transfers instead of counting money twice.
    destinations=[dict(g, type='transfer', name='В '+g['name']) for g in transfer_groups.values()] if scope else []
    remaining=dict(id='remaining',name='Остаток на конец',amount=closing,ids=[],type='remaining')
    distribution_categories=[dict(g,type='category') for g in sorted(groups.values(),key=lambda g:-g['amount'])]+destinations+[remaining]
    distribution_limits=[dict(g,type='limit') for g in sorted(limit_groups.values(),key=lambda g:-g['amount'])]+destinations+[remaining]
    return dict(available=available,opening_added=opening_added,distribution_categories=distribution_categories,distribution_limits=distribution_limits,income=income,expense=expense,transfer_in=incoming,transfer_out=outgoing,opening=opening,closing=closing,groups=sorted(groups.values(),key=lambda g:-g['amount']),income_groups=sorted(income_groups.values(),key=lambda g:-g['amount']),weeks=sorted(weeks.values(),key=lambda w:w['week']),transactions=ts,limit_groups=sorted(limit_groups.values(),key=lambda g:-g['amount']),transfer_groups=sorted(transfer_groups.values(),key=lambda g:-g['amount']))
