import os, hmac, hashlib, asyncio, logging, json, csv, io, time, secrets
from urllib.parse import urlencode, urlsplit
import re
from html import escape
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from app import ledger as L
from app.telegram_auth import allowed_ids, validate_init_data, TelegramAuthError
from app.parser import parse_priorbank_sms, IgnoredSms
from app.exchange import convert_foreign_to_byn, get_byn_rate
load_dotenv()
log=logging.getLogger(__name__)
static=Path(__file__).parent/'static'
security=HTTPBasic(auto_error=False)
def secret(): return os.getenv('API_SECRET','')
def equal(a,b): return bool(b) and hmac.compare_digest(a.encode(),b.encode())
def credentials_ok(username,password):
    return equal(username,os.getenv('WEB_USERNAME','family')) and equal(password,os.getenv('WEB_PASSWORD') or secret())
def session_key(): return (secret()+'|'+os.getenv('WEB_USERNAME','family')+'|'+(os.getenv('WEB_PASSWORD') or secret())).encode()
def session_token(telegram_id=None):
    payload=f'{int(time.time())+30*86400}.{secrets.token_urlsafe(24)}'
    key=session_key()
    if telegram_id is not None:
        payload+=f'.tg{telegram_id}'
        key=hmac.new(key,('telegram|'+os.getenv('TELEGRAM_BOT_TOKEN','')).encode(),hashlib.sha256).digest()
    return payload+'.'+hmac.new(key,payload.encode(),hashlib.sha256).hexdigest()
def session_ok(request):
    token=request.cookies.get('finance_session','')
    try:
        parts=token.split('.'); key=session_key()
        if len(parts)==4:
            expiry,nonce,identity,sig=parts
            if not identity.startswith('tg') or int(identity[2:]) not in allowed_ids() or not os.getenv('TELEGRAM_BOT_TOKEN'): return False
            key=hmac.new(key,('telegram|'+os.getenv('TELEGRAM_BOT_TOKEN','')).encode(),hashlib.sha256).digest()
        elif len(parts)==3: expiry,nonce,sig=parts
        else: return False
        payload='.'.join(parts[:-1])
        return int(expiry)>time.time() and hmac.compare_digest(sig,hmac.new(key,payload.encode(),hashlib.sha256).hexdigest())
    except (ValueError,TypeError): return False
def check_origin(request):
    origin=request.headers.get('origin'); host=request.headers.get('host',''); configured=urlsplit(os.getenv('APP_BASE_URL','')).netloc
    if request.headers.get('sec-fetch-site')=='cross-site' or (origin and urlsplit(origin).netloc not in (host,configured)): raise HTTPException(403,'Недопустимый источник запроса')
def set_session(response,request,telegram_id=None):
    secure=request.url.scheme=='https' or request.headers.get('x-forwarded-proto')=='https' or os.getenv('APP_BASE_URL','').startswith('https://') or bool(os.getenv('RAILWAY_ENVIRONMENT'))
    response.set_cookie('finance_session',session_token(telegram_id),max_age=30*86400,httponly=True,secure=secure,samesite='lax',path='/')
    return response
def auth(request:Request,creds:HTTPBasicCredentials|None=Depends(security)):
    if session_ok(request):
        if request.method not in ('GET','HEAD','OPTIONS'):
            check_origin(request)
        return
    if creds and credentials_ok(creds.username,creds.password): return
    raise HTTPException(401,'Требуется авторизация')
def safe_next(value):
    # The SPA uses only the root path, with optional query parameters.
    if not isinstance(value,str) or not value.startswith('/') or value.startswith('//') or '\\' in value or any(ord(c)<32 or ord(c)==127 for c in value): return '/'
    try:
        parsed=urlsplit(value)
        return value if parsed.path=='/' and not parsed.netloc and not parsed.scheme and not parsed.fragment else '/'
    except ValueError: return '/'
def mini_target(start_param):
    if re.fullmatch(r'tx_[1-9][0-9]{0,17}',start_param): return '/?transaction='+start_param[3:]
    if start_param=='limits': return '/?section=limits'
    return '/'
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
def telegram_button(target='/',label='Открыть приложение'):
    base=os.getenv('APP_BASE_URL','').rstrip('/')
    if not base.startswith('https://') or not urlsplit(base).netloc: return None
    target=safe_next(target); chat=os.getenv('TELEGRAM_CHAT_ID','').strip()
    if chat.isdigit() and int(chat)>0:
        return {'text':label,'web_app':{'url':base+'/mini?'+urlencode({'next':target})}}
    # Inline web_app buttons are only supported in private user-bot chats.
    # For groups use a named Mini App direct link, if configured.
    bot=os.getenv('TELEGRAM_BOT_USERNAME','').lstrip('@'); name=os.getenv('TELEGRAM_MINI_APP_SHORT_NAME','')
    if re.fullmatch(r'[A-Za-z0-9_]{5,32}',bot) and re.fullmatch(r'[A-Za-z0-9_]{3,64}',name):
        start='home'
        if re.fullmatch(r'/\?transaction=[1-9][0-9]{0,17}',target): start='tx_'+target.split('=')[1]
        elif target=='/?section=limits': start='limits'
        return {'text':label,'url':f'https://t.me/{bot}/{name}?startapp={start}'}
    return {'text':label,'url':base+target}
async def tg_send(text,button=None):
    token=os.getenv('TELEGRAM_BOT_TOKEN'); chat=os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat: return False
    data={'chat_id':chat,'text':text}
    if button: data['reply_markup']={'inline_keyboard':[[button]]}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r=await client.post(f'https://api.telegram.org/bot{token}/sendMessage',json=data); r.raise_for_status(); return bool(r.json().get('ok'))
    except Exception: log.warning('Telegram delivery failed'); return False
async def flush():
    with L.db() as c:
        alerts=L.rows(c,'SELECT * FROM v31_purchase_alerts WHERE sent=0 ORDER BY id'); pending=L.rows(c,'SELECT * FROM v3_transactions WHERE sms_hash IS NOT NULL AND notified=0 ORDER BY id')
    for a in alerts:
        if await tg_send(a['message'],telegram_button('/?section=limits','Посмотреть лимиты')):
            with L.db() as c: c.execute('UPDATE v31_purchase_alerts SET sent=1 WHERE id=?',(a['id'],))
    for t in pending:
        text=f"Банковская операция: {t['original']/100:.2f} {t['currency']}\n{t['description']}\n{t['occurred_at'] or t['day']} · {'Ожидает классификации' if t['status']=='pending' else 'Учтена'}"
        if await tg_send(text,telegram_button(f'/?transaction={t["id"]}','Разобрать операцию' if t['status']=='pending' else 'Открыть операцию')):
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
app=FastAPI(title='Family Finance',version='3.2.0',lifespan=lifespan)
app.mount('/static',StaticFiles(directory=static),name='static')
@app.get('/')
def index(request:Request,creds:HTTPBasicCredentials|None=Depends(security)):
    if session_ok(request) or (creds and credentials_ok(creds.username,creds.password)): return FileResponse(static/'index.html',headers={'Cache-Control':'no-store'})
    return RedirectResponse('/login?'+urlencode({'next':str(request.url.path)+('?' + request.url.query if request.url.query else '')}),status_code=303)
@app.get('/mini')
@app.get('/login')
def login_page(next:str='/',error:int=0,manual:int=0):
    target=escape(safe_next(next),quote=True)
    return HTMLResponse(f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>Вход — Family Finance</title><link rel='stylesheet' href='/static/style.css'><script src='https://telegram.org/js/telegram-web-app.js' defer></script><script src='/static/login.js' defer></script></head><body><main style='max-width:420px;padding-top:12vh'><div class='card'><small>ЛИЧНЫЕ ФИНАНСЫ</small><h1>Family Finance</h1><p>Войдите в семейный бюджет</p><p id='telegram-status' role='status' hidden></p><form id='login-form' method='post' action='/login' class='fields' data-manual='{int(bool(manual or error))}'><input type='hidden' name='next' value='{target}'><label>Логин<input name='username' autocomplete='username' required></label><label>Пароль<input name='password' type='password' autocomplete='current-password' required></label>{'<p class="error">Неверный логин или пароль</p>' if error else ''}<button class='primary' type='submit'>Войти</button></form></div></main></body></html>""",headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
@app.post('/login')
async def login_submit(request:Request):
    from urllib.parse import parse_qs
    check_origin(request)
    data=parse_qs((await request.body()).decode('utf-8',errors='replace')); get=lambda k:data.get(k,[''])[0]
    target=safe_next(get('next'))
    if not credentials_ok(get('username'),get('password')):
        await asyncio.sleep(0.5)
        return RedirectResponse('/login?'+urlencode({'next':target,'error':1}),status_code=303)
    return set_session(RedirectResponse(target,status_code=303),request)
class TelegramLogin(BaseModel):
    init_data:str=Field(min_length=1,max_length=16384)
    next:str='/'
@app.post('/api/auth/telegram')
def telegram_login(v:TelegramLogin,request:Request):
    check_origin(request)
    try: uid,start=validate_init_data(v.init_data,os.getenv('TELEGRAM_BOT_TOKEN',''))
    except TelegramAuthError as e: raise HTTPException(401,str(e)) from None
    if uid not in allowed_ids(): raise HTTPException(403,'Этот аккаунт Telegram не имеет доступа к семейному бюджету.')
    target=safe_next(v.next)
    if target=='/': target=mini_target(start)
    return set_session(JSONResponse({'ok':True,'next':target},headers={'Cache-Control':'no-store'}),request,uid)
@app.post('/logout',dependencies=[Depends(auth)])
def logout():
    response=JSONResponse({'ok':True}); response.delete_cookie('finance_session',path='/'); return response
@app.get('/health')
def health():
    with L.db() as c: c.execute('SELECT 1')
    return {'status':'ok','version':'3.2.0'}
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
        L.queue_alerts(c,id)
    return {'id':id}
@app.delete('/api/transactions/{id}',dependencies=[Depends(auth)])
def delete_tx(id:int):
    with L.db() as c:
        if not c.execute("UPDATE v3_transactions SET status='ignored' WHERE id=?",(id,)).rowcount: raise HTTPException(404)
    return {'ok':True}
class Limit(BaseModel):
    name:str=Field(min_length=1,max_length=100); amount:Decimal; scope:str='family'; category_ids:list[int]; start_month:str; active:bool=True; period:str='weekly'
@app.get('/api/limits',dependencies=[Depends(auth)])
def get_limits(month:str|None=None):
    month=month or L.today().strftime('%Y-%m'); day(month+'-01')
    with L.db() as c: return L.limits_status(c,month)
@app.post('/api/limits',dependencies=[Depends(auth)])
@app.put('/api/limits/{id}',dependencies=[Depends(auth)])
def save_limit(v:Limit,id:int|None=None):
    scope(v.scope); a=money(v.amount); day(v.start_month+'-01')
    if not v.name.strip(): fail('Введите название')
    if v.period not in ('weekly','monthly'): fail('Выберите недельный или месячный лимит')
    with L.db() as c:
        c.execute('BEGIN IMMEDIATE'); cats={r['id']:r for r in L.categories(c)}
        if not v.category_ids or any(i not in cats or cats[i]['scope']!=v.scope or cats[i]['kind']!='expense' or cats[i]['archived'] for i in v.category_ids): fail('Выберите категории расходов этого контура')
        fields=(v.name.strip(),a,v.scope,json.dumps(sorted(set(v.category_ids))),v.start_month,int(v.active),v.period)
        if id:
            if not c.execute('UPDATE v3_limits SET name=?,amount=?,scope=?,category_ids=?,start_month=?,active=?,period=? WHERE id=?',fields+(id,)).rowcount: raise HTTPException(404)
        else: id=c.execute('INSERT INTO v3_limits(name,amount,scope,category_ids,start_month,active,period) VALUES(?,?,?,?,?,?,?)',fields).lastrowid
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
        created=bool(cur.rowcount); id=cur.lastrowid if created else c.execute('SELECT id FROM v3_transactions WHERE sms_hash=?',(key,)).fetchone()[0]; L.queue_alerts(c,id) if created else None
    return {'ok':True,'duplicate':not created,'transaction_id':id}
@app.post('/telegram/webhook')
async def webhook(request:Request):
    if not equal(request.headers.get('x-telegram-bot-api-secret-token',''),os.getenv('TELEGRAM_WEBHOOK_SECRET') or secret()): raise HTTPException(403)
    return {'ok':True}
@app.get('/api/export.csv')
def export(request:Request,creds:HTTPBasicCredentials|None=Depends(security)):
    if not equal(request.headers.get('authorization',''),'Bearer '+secret()): auth(request,creds)
    with L.db() as c: data=L.transactions(c,'2000-01-01','2100-12-31')
    out=io.StringIO(); fields=['id','day','kind','scope','target','category','amount','currency','original','description','status']; w=csv.DictWriter(out,fieldnames=fields,extrasaction='ignore'); w.writeheader()
    for t in data:
        t['amount']='' if t['amount'] is None else f"{t['amount']/100:.2f}"; t['original']=f"{t['original']/100:.2f}"; w.writerow(t)
    return StreamingResponse(iter([out.getvalue()]),media_type='text/csv',headers={'Content-Disposition':'attachment; filename=finance.csv'})
