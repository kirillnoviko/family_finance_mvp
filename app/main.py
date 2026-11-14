import os, hmac, hashlib, asyncio, logging, json, csv, io
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from app import ledger as L
from app.parser import parse_priorbank_sms, IgnoredSms
from app.exchange import convert_foreign_to_byn, get_byn_rate
load_dotenv()
log=logging.getLogger(__name__)
static=Path(__file__).parent/'static'
security=HTTPBasic(auto_error=False)
def secret(): return os.getenv('API_SECRET','')
def equal(a,b): return bool(b) and hmac.compare_digest(a.encode(),b.encode())
def auth(creds:HTTPBasicCredentials|None=Depends(security)):
    if not creds or not equal(creds.username,os.getenv('WEB_USERNAME','family')) or not equal(creds.password,os.getenv('WEB_PASSWORD') or secret()): raise HTTPException(401,'Требуется авторизация',headers={'WWW-Authenticate':'Basic realm="Family Finance"'})
def fail(msg): raise HTTPException(422,msg)
def day(s):
    try: return date.fromisoformat(s).isoformat()
    except (ValueError,TypeError): fail('Некорректная дата')
def scope(s):
    if s not in L.SCOPES: fail('Неизвестный контур')
    return s
def money(v,zero=False):
    try: a=L.minor(v)
    except (ValueError,ArithmeticError): fail('Некорректная сумма')
    if a<0 or (a==0 and not zero): fail('Сумма должна быть положительной')
    return a
async def tg_send(text):
    token=os.getenv('TELEGRAM_BOT_TOKEN'); chat=os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat: return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r=await client.post(f'https://api.telegram.org/bot{token}/sendMessage',json={'chat_id':chat,'text':text}); r.raise_for_status(); return bool(r.json().get('ok'))
    except Exception: log.warning('Telegram delivery failed'); return False
async def flush():
    with L.db() as c:
        alerts=L.rows(c,'SELECT * FROM v3_alerts WHERE sent=0 ORDER BY id'); pending=L.rows(c,'SELECT * FROM v3_transactions WHERE sms_hash IS NOT NULL AND notified=0 ORDER BY id')
    for a in alerts:
        if await tg_send(a['message']):
            with L.db() as c: c.execute('UPDATE v3_alerts SET sent=1 WHERE id=?',(a['id'],))
    for t in pending:
        url=os.getenv('APP_BASE_URL','').rstrip('/')
        text=f"Банковская операция: {t['original']/100:.2f} {t['currency']}\n{t['description']}\n{t['occurred_at'] or t['day']} · {'Ожидает классификации' if t['status']=='pending' else 'Учтена'}"+(f'\n{url}/?transaction={t["id"]}' if url else '')
        if await tg_send(text):
            with L.db() as c: c.execute('UPDATE v3_transactions SET notified=1 WHERE id=?',(t['id'],))
async def backfill_fx():
    with L.db() as c: missing=L.rows(c,"SELECT * FROM v3_transactions WHERE status='pending' AND amount IS NULL LIMIT 30")
    for t in missing:
        try: converted,rate=await convert_foreign_to_byn(Decimal(t['original'])/100,t['currency'],date.fromisoformat(t['day'])); amount=money(converted)
        except Exception: continue
        with L.db() as c: c.execute("UPDATE v3_transactions SET amount=?,fx_rate=? WHERE id=? AND status='pending' AND amount IS NULL",(amount,str(rate),t['id']))
async def worker():
    ticks=0
    while True:
        try:
            with L.db() as c: L.queue_alerts(c)
            await flush()
            if ticks%10==0: await backfill_fx()
            ticks+=1
        except Exception: log.exception('Notification worker failed')
        await asyncio.sleep(30)
@asynccontextmanager
async def lifespan(app):
    if not secret(): raise RuntimeError('Set API_SECRET in environment')
    L.init(); task=asyncio.create_task(worker())
    try: yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError): await task
app=FastAPI(title='Family Finance',version='3.0.0',lifespan=lifespan)
app.mount('/static',StaticFiles(directory=static),name='static')
@app.get('/')
def index(_:None=Depends(auth)): return FileResponse(static/'index.html')
@app.get('/health')
def health():
    with L.db() as c: c.execute('SELECT 1')
    return {'status':'ok','version':'3.0.0'}
@app.get('/api/dashboard',dependencies=[Depends(auth)])
async def dashboard():
    now=L.today().isoformat()
    with L.db() as c:
        b=L.balances(c,now); inv=L.investment(c,now); limits=L.limits_status(c,now[:7]); pending=c.execute("SELECT COUNT(*) FROM v3_transactions WHERE status='pending'").fetchone()[0]
    rates={}
    async def get(code):
        try: rates[code]=str(await get_byn_rate(code,L.today()))
        except Exception: pass
    await asyncio.gather(*(get(code) for code in ('EUR','USD','RUB')))
    values={s:{code:float((Decimal(b[s] if s!='investment' or inv['value'] is None else inv['value'])/100/Decimal(rate)).quantize(Decimal('.01'))) for code,rate in rates.items()} for s in ('marketing','reserve','investment')}
    return dict(balances=b,investment=inv,limits=limits,pending=pending,rates=rates,converted=values,rate_date=now)
@app.get('/api/categories',dependencies=[Depends(auth)])
def get_categories():
    with L.db() as c: return L.categories(c)
class Category(BaseModel):
    name:str=Field(min_length=1,max_length=100); scope:str; kind:str; parent_id:int|None=None; archived:bool=False
@app.post('/api/categories',dependencies=[Depends(auth)])
@app.put('/api/categories/{id}',dependencies=[Depends(auth)])
def save_category(v:Category,id:int|None=None):
    scope(v.scope)
    if not v.name.strip(): fail('Введите название')
    if v.kind not in ('income','expense'): fail('Некорректный тип')
    with L.db() as c:
        c.execute('BEGIN IMMEDIATE')
        if v.parent_id:
            p=c.execute('SELECT * FROM v3_categories WHERE id=?',(v.parent_id,)).fetchone()
            if not p or p['scope']!=v.scope or p['kind']!=v.kind or p['archived'] or (id and v.parent_id in L.descendants(c,[id])): fail('Некорректная родительская категория')
        if id:
            old=c.execute('SELECT * FROM v3_categories WHERE id=?',(id,)).fetchone()
            if not old: raise HTTPException(404)
            if old['scope']!=v.scope or old['kind']!=v.kind: fail('Нельзя изменить контур или тип существующей категории')
            c.execute('UPDATE v3_categories SET name=?,parent_id=?,archived=? WHERE id=?',(v.name.strip(),v.parent_id,v.archived,id))
        else: id=c.execute('INSERT INTO v3_categories(name,scope,kind,parent_id,archived) VALUES(?,?,?,?,?)',(v.name.strip(),v.scope,v.kind,v.parent_id,v.archived)).lastrowid
        L.queue_alerts(c)
    return {'id':id}
class Tx(BaseModel):
    day:str; kind:str; scope:str; target:str|None=None; category_id:int|None=None; amount:Decimal; currency:str='BYN'; description:str=Field(default='',max_length=1000); remember:bool=False
@app.get('/api/transactions',dependencies=[Depends(auth)])
def get_tx(start:str='2000-01-01',end:str='2100-12-31',scope:str|None=None):
    start=day(start); end=day(end)
    if start>end: fail('Неверный диапазон')
    if scope and scope not in L.SCOPES: fail('Неизвестный контур')
    with L.db() as c: return L.transactions(c,start,end,scope)
@app.post('/api/transactions',dependencies=[Depends(auth)])
@app.put('/api/transactions/{id}',dependencies=[Depends(auth)])
async def save_tx(v:Tx,id:int|None=None):
    d=day(v.day); scope(v.scope); a=money(v.amount); currency=v.currency.upper()
    if v.kind not in ('income','expense','transfer','opening','refund','internal'): fail('Неизвестный тип операции')
    if v.kind=='transfer':
        scope(v.target)
        if v.target==v.scope: fail('Выберите другой контур')
    else: v.target=None
    if currency not in ('BYN','EUR','USD','RUB'): fail('Валюта не поддерживается')
    try: converted,rate=await convert_foreign_to_byn(Decimal(a)/100,currency,date.fromisoformat(d)); byn=money(converted)
    except Exception: raise HTTPException(503,'Курс недоступен. Повторите позже.')
    with L.db() as c:
        c.execute('BEGIN IMMEDIATE')
        if v.kind in ('income','expense','refund'):
            cat=c.execute('SELECT * FROM v3_categories WHERE id=?',(v.category_id,)).fetchone()
            if not cat or cat['scope']!=v.scope or cat['kind']!=('expense' if v.kind=='refund' else v.kind) or cat['archived']: fail('Выберите подходящую категорию')
        else: v.category_id=None
        fields=(d,v.kind,v.scope,v.target,v.category_id,byn,currency,a,v.description,str(rate))
        if id:
            old=c.execute('SELECT * FROM v3_transactions WHERE id=?',(id,)).fetchone()
            if not old: raise HTTPException(404)
            c.execute("UPDATE v3_transactions SET day=?,kind=?,scope=?,target=?,category_id=?,amount=?,currency=?,original=?,description=?,fx_rate=?,status='posted' WHERE id=?",fields+(id,))
            if v.remember and old['merchant'] and v.kind in ('income','expense'):
                c.execute('INSERT OR REPLACE INTO v3_rules VALUES(?,?,?,?)',(old['merchant'].casefold(),v.kind,v.scope,v.category_id))
        else: id=c.execute('INSERT INTO v3_transactions(day,kind,scope,target,category_id,amount,currency,original,description,fx_rate) VALUES(?,?,?,?,?,?,?,?,?,?)',fields).lastrowid
        L.queue_alerts(c)
    return {'id':id}
@app.delete('/api/transactions/{id}',dependencies=[Depends(auth)])
def delete_tx(id:int):
    with L.db() as c:
        if not c.execute("UPDATE v3_transactions SET status='ignored' WHERE id=?",(id,)).rowcount: raise HTTPException(404)
    return {'ok':True}
class Limit(BaseModel):
    name:str=Field(min_length=1,max_length=100); amount:Decimal; scope:str='family'; category_ids:list[int]; start_month:str; active:bool=True
@app.get('/api/limits',dependencies=[Depends(auth)])
def get_limits(month:str|None=None):
    month=month or L.today().strftime('%Y-%m'); day(month+'-01')
    with L.db() as c: return L.limits_status(c,month)
@app.post('/api/limits',dependencies=[Depends(auth)])
@app.put('/api/limits/{id}',dependencies=[Depends(auth)])
def save_limit(v:Limit,id:int|None=None):
    scope(v.scope); a=money(v.amount); day(v.start_month+'-01')
    if not v.name.strip(): fail('Введите название')
    with L.db() as c:
        c.execute('BEGIN IMMEDIATE'); cats={r['id']:r for r in L.categories(c)}
        if not v.category_ids or any(i not in cats or cats[i]['scope']!=v.scope or cats[i]['kind']!='expense' or cats[i]['archived'] for i in v.category_ids): fail('Выберите категории расходов этого контура')
        fields=(v.name.strip(),a,v.scope,json.dumps(sorted(set(v.category_ids))),v.start_month,int(v.active))
        if id:
            if not c.execute('UPDATE v3_limits SET name=?,amount=?,scope=?,category_ids=?,start_month=?,active=? WHERE id=?',fields+(id,)).rowcount: raise HTTPException(404)
        else: id=c.execute('INSERT INTO v3_limits(name,amount,scope,category_ids,start_month,active) VALUES(?,?,?,?,?,?)',fields).lastrowid
        L.queue_alerts(c)
    return {'id':id}
@app.get('/api/limits/all',dependencies=[Depends(auth)])
def all_limits():
    with L.db() as c:
        result=L.rows(c,'SELECT * FROM v3_limits ORDER BY id')
        for r in result: r['category_ids']=json.loads(r['category_ids'])
        return result
class Valuation(BaseModel):
    day:str; amount:Decimal; currency:str="BYN"
@app.post('/api/investment/valuation',dependencies=[Depends(auth)])
async def valuation(v:Valuation):
    d=day(v.day); a=money(v.amount,True)
    currency=v.currency.upper()
    if currency not in ("BYN","EUR","USD","RUB"): fail("Валюта не поддерживается")
    try: converted,_=await convert_foreign_to_byn(Decimal(a)/100,currency,date.fromisoformat(d)); a=money(converted,True)
    except Exception: raise HTTPException(503,"Курс недоступен")
    if d>L.today().isoformat(): fail('Дата оценки не может быть в будущем')
    with L.db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('INSERT INTO v3_valuations(day,amount,ledger_snapshot) VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET amount=excluded.amount,ledger_snapshot=excluded.ledger_snapshot',(d,a,L.balances(c,d)['investment']))
    return {'ok':True}
@app.get('/api/report',dependencies=[Depends(auth)])
def report(start:str,end:str,scope:str|None=None,limit_id:int|None=None):
    start=day(start); end=day(end)
    if start>end: fail('Неверный диапазон')
    if (date.fromisoformat(end)-date.fromisoformat(start)).days>3660: fail('Выберите период не больше 10 лет')
    if scope and scope not in L.SCOPES: fail('Неизвестный контур')
    with L.db() as c: return L.report(c,start,end,scope,limit_id)
class SMS(BaseModel):
    message:str=Field(min_length=1,max_length=5000); device:str=Field(default='wife',max_length=50)
@app.post('/api/sms')
async def sms(v:SMS,request:Request):
    if not equal(request.headers.get('authorization',''),'Bearer '+secret()) or not secret(): raise HTTPException(401)
    try: p=parse_priorbank_sms(v.message)
    except IgnoredSms: return {'ok':True,'ignored':True}
    except ValueError as e: fail(str(e))
    key=hashlib.sha256(' '.join(v.message.split()).encode()).hexdigest()
    with L.db() as c:
        existing=c.execute('SELECT id FROM v3_transactions WHERE sms_hash=?',(key,)).fetchone()
        if existing: return {'ok':True,'duplicate':True,'transaction_id':existing['id']}
    try: byn,rate=await convert_foreign_to_byn(p.amount,p.currency,p.occurred_at.date()); amount=money(byn)
    except Exception: amount=None; rate=None
    kind='income' if p.direction=='in' else 'expense'; status='pending'; s=cat=None
    with L.db() as c:
        c.execute('BEGIN IMMEDIATE')
        if amount is not None and p.operation_hint=='payment' and p.merchant:
            rule=c.execute('SELECT r.* FROM v3_rules r JOIN v3_categories c ON c.id=r.category_id WHERE r.merchant=? AND r.kind=? AND c.archived=0',(p.merchant.casefold(),kind)).fetchone()
            if rule: s=rule['scope']; cat=rule['category_id']; status='posted'
        cur=c.execute('INSERT OR IGNORE INTO v3_transactions(day,kind,scope,category_id,amount,currency,original,description,status,sms_hash,hint,merchant,fx_rate,device,occurred_at,card_mask) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(p.occurred_at.date().isoformat(),kind,s,cat,amount,p.currency,money(p.amount),p.description,status,key,p.operation_hint,p.merchant,str(rate) if rate is not None else None,v.device,p.occurred_at.isoformat(sep=' '),p.card_mask))
        created=bool(cur.rowcount); id=cur.lastrowid if created else c.execute('SELECT id FROM v3_transactions WHERE sms_hash=?',(key,)).fetchone()[0]; L.queue_alerts(c)
    return {'ok':True,'duplicate':not created,'transaction_id':id}
@app.post('/telegram/webhook')
async def webhook(request:Request):
    if not equal(request.headers.get('x-telegram-bot-api-secret-token',''),os.getenv('TELEGRAM_WEBHOOK_SECRET') or secret()): raise HTTPException(403)
    return {'ok':True}
@app.get('/api/export.csv')
def export(request:Request,creds:HTTPBasicCredentials|None=Depends(security)):
    if not equal(request.headers.get('authorization',''),'Bearer '+secret()): auth(creds)
    with L.db() as c: data=L.transactions(c,'2000-01-01','2100-12-31')
    out=io.StringIO(); fields=['id','day','kind','scope','target','category','amount','currency','original','description','status']; w=csv.DictWriter(out,fieldnames=fields,extrasaction='ignore'); w.writeheader()
    for t in data:
        t['amount']='' if t['amount'] is None else f"{t['amount']/100:.2f}"; t['original']=f"{t['original']/100:.2f}"; w.writerow(t)
    return StreamingResponse(iter([out.getvalue()]),media_type='text/csv',headers={'Content-Disposition':'attachment; filename=finance.csv'})
