import os
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from app import ledger as L
from app import main as M

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'test.db'))
    monkeypatch.setenv('API_SECRET','test-secret')
    monkeypatch.setenv('WEB_PASSWORD','web-secret')
    monkeypatch.setenv('WEB_USERNAME','family')
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN',raising=False)
    async def rate(code,on_date): return {'EUR':Decimal('4'),'USD':Decimal('3'),'RUB':Decimal('.03')}[code]
    monkeypatch.setattr(M,'get_byn_rate',rate)
    async def conversion(amount,currency,on_date):
        r=Decimal('1') if currency=='BYN' else await rate(currency,on_date)
        return amount*r,r
    monkeypatch.setattr(M,'convert_foreign_to_byn',conversion)
    with TestClient(M.app) as c:
        c.auth=('family','web-secret'); yield c

def cat(c,code): return next(x['id'] for x in c.get('/api/categories').json() if x['code']==code)
def tx(c,kind,scope,amount,category=None,target=None,day=None,**extra):
    r=c.post('/api/transactions',json=dict(kind=kind,scope=scope,amount=str(amount),category_id=category,target=target,day=day or L.today().isoformat(),**extra)); assert r.status_code==200,r.text; return r.json()['id']
def limit(c,name,amount,ids):
    r=c.post('/api/limits',json=dict(name=name,amount=amount,scope='family',category_ids=ids,start_month=L.today().strftime('%Y-%m'))); assert r.status_code==200,r.text; return r.json()['id']
def dash(c):
    r=c.get('/api/dashboard'); assert r.status_code==200,r.text; return r.json()
def test_auth_and_empty(client):
    assert client.get('/').status_code==200
    assert client.get('/api/dashboard',auth=('family','wrong')).status_code==401
    assert all(v==0 for v in dash(client)['balances'].values())
def test_four_contours_transfers_and_investment(client):
    c=client
    tx(c,'income','family',1000,cat(c,'salary_kirill'))
    tx(c,'income','marketing',1500,cat(c,'marketing_client_income'))
    tx(c,'expense','marketing',150,cat(c,'tax'))
    tx(c,'transfer','marketing',600,target='family'); tx(c,'transfer','family',300,target='reserve'); tx(c,'transfer','reserve',100,target='marketing'); tx(c,'transfer','reserve',200,target='investment')
    d=dash(c); assert d['balances']=={'family':130000,'marketing':85000,'reserve':0,'investment':20000}
    assert d['converted']['marketing']=={'EUR':212.5,'USD':283.33,'RUB':28333.33}
    assert c.post('/api/investment/valuation',json={'day':L.today().isoformat(),'amount':250}).status_code==200
    i=dash(c)['investment']; assert i['profit']==5000 and i['percent']==25
    tx(c,'transfer','family',100,target='investment'); i=dash(c)['investment']; assert i['value']==35000 and i['net']==30000 and i['profit']==5000
    tx(c,'transfer','investment',50,target='family'); i=dash(c)['investment']; assert i['value']==30000 and i['net']==25000 and i['profit']==5000
    r=c.get('/api/report',params={'start':L.today().isoformat(),'end':L.today().isoformat()}).json(); assert r['income']==250000 and r['expense']==15000

def test_limits_and_weekly_overlap(client):
    c=client; food=cat(c,'food'); cafe=cat(c,'cafe'); toys=cat(c,'child_toys'); clothes=cat(c,'clothes')
    main=limit(c,'Общий',500,[food,toys]); separate=limit(c,'Игрушки',50,[toys]); limit(c,'Одежда',1000,[clothes])
    tx(c,'expense','family',350,cafe); tx(c,'expense','family',100,clothes); tx(c,'expense','family',50,toys)
    d=dash(c); l=next(x for x in d['limits'] if x['id']==main); assert l['spent']==40000 and l['remaining']==10000
    tx(c,'expense','family',50,cafe); tx(c,'expense','family',51,cafe)
    with L.db() as db:
        alerts=L.rows(db,'SELECT tx_id FROM v31_purchase_alerts WHERE limit_id=? ORDER BY tx_id',(main,)); assert len(alerts)==4
        L.queue_alerts(db); assert db.execute('SELECT COUNT(*) FROM v31_purchase_alerts WHERE limit_id=?',(main,)).fetchone()[0]==4
    q={'start':L.today().isoformat(),'end':L.today().isoformat(),'scope':'family','limit_id':main}; r=c.get('/api/report',params=q).json(); assert r['expense']==60100 and r['weeks'][0]['limited']==50100
    q.pop('limit_id'); r=c.get('/api/report',params=q).json(); assert r['weeks'][0]['limited']==60100
    tx(c,'refund','family',10,cafe); assert next(x for x in dash(c)['limits'] if x['id']==main)['spent']==49100

def test_categories_edit_and_validation(client):
    c=client; parent=cat(c,'child_toys'); r=c.post('/api/categories',json={'name':'Игрушки ребёнку','scope':'family','kind':'expense','parent_id':parent}); assert r.status_code==200; id=r.json()['id']
    l=limit(c,'Подкатегория',50,[parent]); t=tx(c,'expense','family',30,id)
    assert next(x for x in dash(c)['limits'] if x['id']==l)['spent']==3000
    assert c.put(f'/api/categories/{parent}',json={'name':'Игрушки','scope':'family','kind':'expense','parent_id':id}).status_code==422
    assert c.delete(f'/api/transactions/{t}').status_code==200; assert next(x for x in dash(c)['limits'] if x['id']==l)['spent']==0
    assert c.post('/api/transactions',json={'day':L.today().isoformat(),'kind':'transfer','scope':'family','target':'family','amount':1}).status_code==422

def test_sms_and_rules(client):
    c=client; raw='Karta 4***1234 14-11-26 10:34:07. Oplata 42.00 BYN. BLR TEST SHOP. Balance: 200.00 BYN'; auth={'Authorization':'Bearer test-secret'}
    r=c.post('/api/sms',json={'message':raw,'device':'phone1'},headers=auth,auth=None); assert r.status_code==200,r.text; id=r.json()['transaction_id']
    r=c.post('/api/sms',json={'message':raw,'device':'phone2'},headers=auth,auth=None); assert r.json()['duplicate']
    assert c.get('/api/transactions').json()[0]['status']=='pending'
    r=c.put(f'/api/transactions/{id}',json={'day':'2026-11-14','kind':'expense','scope':'family','category_id':cat(c,'groceries'),'amount':42,'remember':True}); assert r.status_code==200,r.text
    raw=raw.replace('10:34:07','10:35:07'); r=c.post('/api/sms',json={'message':raw},headers=auth,auth=None); assert r.status_code==200
    assert all(t['status']=='posted' for t in c.get('/api/transactions').json())
    assert c.post('/api/sms',json={'message':'Priorbank 05/08 10:10. Code: 123'},headers=auth,auth=None).json()['ignored']

def test_notification_queue_and_delivery(client,monkeypatch):
    import asyncio
    c=client; food=cat(c,'food'); cafe=cat(c,'cafe'); l=limit(c,'Тест',100,[food]); tx(c,'expense','family',70,cafe)
    delivered=[]
    async def send(text,button=None): delivered.append(text); return True
    monkeypatch.setattr(M,'tg_send',send)
    asyncio.run(M.flush()); asyncio.run(M.flush())
    assert len(delivered)==1 and '30%' in delivered[0]
    tx(c,'expense','family',20,cafe); asyncio.run(M.flush()); assert len(delivered)==2 and '10%' in delivered[1]
    tx(c,'expense','family',10,cafe); asyncio.run(M.flush()); assert len(delivered)==3 and 'исчерпан' in delivered[2]
    tx(c,'expense','family',1,cafe); asyncio.run(M.flush()); assert len(delivered)==4 and 'превышен' in delivered[3]

def test_sms_without_fx_is_not_lost(client,monkeypatch):
    c=client
    async def unavailable(*args): raise RuntimeError('offline')
    monkeypatch.setattr(M,'convert_foreign_to_byn',unavailable)
    raw='Karta 4***1234 14-11-26 10:34:07. Oplata 9.99 USD. IRL TEST SHOP. Balance: 200.00 BYN'
    r=c.post('/api/sms',json={'message':raw},headers={'Authorization':'Bearer test-secret'},auth=None); assert r.status_code==200
    t=c.get('/api/transactions').json()[0]; assert t['amount'] is None and t['original']==999 and t['status']=='pending'
    assert dash(c)['balances']['family']==0
    import asyncio
    async def conversion(amount,currency,on_date): return amount*3,Decimal('3')
    monkeypatch.setattr(M,'convert_foreign_to_byn',conversion); asyncio.run(M.backfill_fx())
    t=c.get('/api/transactions').json()[0]; assert t['amount']==2997 and t['status']=='pending'

def test_saturday_reset_and_monthly_limit(client,monkeypatch):
    from datetime import date
    c=client; monkeypatch.setattr(L,'today',lambda:date(2026,11,13))
    food=cat(c,'food'); cafe=cat(c,'cafe'); weekly=limit(c,'Неделя',100,[food])
    monthly=c.post('/api/limits',json=dict(name='Месяц',amount=500,scope='family',category_ids=[food],start_month='2026-11',period='monthly')).json()['id']
    tx(c,'expense','family',80,cafe,day='2026-11-13'); ls={l['id']:l for l in dash(c)['limits']}
    assert ls[weekly]['period_start']=='2026-11-07' and ls[weekly]['period_end']=='2026-11-13' and ls[weekly]['remaining']==2000
    monkeypatch.setattr(L,'today',lambda:date(2026,11,14))
    ls={l['id']:l for l in dash(c)['limits']}; assert ls[weekly]['spent']==0 and ls[weekly]['month_spent']==8000 and ls[monthly]['spent']==8000
    tx(c,'expense','family',25,cafe,day='2026-11-14'); ls={l['id']:l for l in dash(c)['limits']}; assert ls[weekly]['spent']==2500 and ls[monthly]['spent']==10500 and ls[weekly]['month_budget']==42857
    r=c.get('/api/report',params={'start':'2026-11-13','end':'2026-11-14','limit_id':weekly}).json(); assert [w['week'] for w in r['weeks']]==['2026-11-07','2026-11-14']; assert [w['limited'] for w in r['weeks']]==[8000,2500]

def test_form_login_deep_link_logout(client):
    c=client; c.auth=None
    r=c.get('/?transaction=123',follow_redirects=False); assert r.status_code==303 and 'login' in r.headers['location']
    assert c.get('/login').status_code==200
    r=c.post('/login',data={'username':'family','password':'wrong','next':'/?transaction=123'},follow_redirects=False); assert 'error=1' in r.headers['location']
    r=c.post('/login',data={'username':'family','password':'web-secret','next':'/?transaction=123'},follow_redirects=False); assert r.headers['location']=='/?transaction=123' and 'HttpOnly' in r.headers['set-cookie']
    assert c.get('/api/dashboard').status_code==200
    assert c.post('/logout',headers={'Origin':'https://untrusted.example'}).status_code==403
    assert c.post('/logout').status_code==200 and c.get('/api/dashboard').status_code==401
    r=c.post('/login',data={'username':'family','password':'web-secret','next':'//evil.example'},follow_redirects=False); assert r.headers['location']=='/'


def signed_telegram_data(uid=123456789, age=0, start='', token='123456:fake-bot-token'):
    import hashlib, hmac, json, time
    from urllib.parse import urlencode
    fields={'auth_date':str(int(time.time())-age),'user':json.dumps({'id':uid,'first_name':'Test'},separators=(',',':')),'query_id':'test-query','signature':'example-third-party-signature'}
    if start: fields['start_param']=start
    key=hmac.new(b'WebAppData',token.encode(),hashlib.sha256).digest()
    check='\n'.join(f'{k}={v}' for k,v in sorted(fields.items()))
    fields['hash']=hmac.new(key,check.encode(),hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_telegram_login_and_revocation(client,monkeypatch):
    c=client; c.auth=None
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN','123456:fake-bot-token')
    monkeypatch.setenv('ALLOWED_TELEGRAM_USER_IDS','123456789, 987654321')
    assert c.get('/mini?next=%2F%3Ftransaction%3D42').status_code==200
    assert 'telegram-web-app.js' in c.get('/mini').text
    assert c.get('/api/dashboard').status_code==401
    r=c.post('/api/auth/telegram',json={'init_data':signed_telegram_data(),'next':'/?transaction=42'})
    assert r.status_code==200 and r.json()['next']=='/?transaction=42'
    assert 'HttpOnly' in r.headers['set-cookie'] and 'SameSite=lax' in r.headers['set-cookie']
    assert c.get('/api/dashboard').status_code==200
    assert c.post('/logout',headers={'Origin':'https://evil.example'}).status_code==403
    monkeypatch.setenv('ALLOWED_TELEGRAM_USER_IDS','987654321')
    assert c.get('/api/dashboard').status_code==401
    r=c.post('/api/auth/telegram',json={'init_data':signed_telegram_data(uid=987654321,start='tx_42')})
    assert r.status_code==200 and r.json()['next']=='/?transaction=42'
    assert c.post('/logout').status_code==200
    assert c.get('/api/dashboard').status_code==401


def test_telegram_rejects_untrusted_data(client,monkeypatch):
    c=client; c.auth=None
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN','123456:fake-bot-token')
    monkeypatch.setenv('ALLOWED_TELEGRAM_USER_IDS','123456789')
    raw=signed_telegram_data()
    for value in [raw.replace('123456789','987654321'),signed_telegram_data(age=301),signed_telegram_data(age=-120),raw+'&user=%7B%7D',signed_telegram_data(token='different'), 'user=%7B%22id%22%3A123456789%7D']:
        r=c.post('/api/auth/telegram',json={'init_data':value}); assert r.status_code==401,r.text
        assert 'finance_session' not in r.cookies
    assert c.post('/api/auth/telegram',json={'init_data':signed_telegram_data(uid=987654321)}).status_code==403
    assert c.post('/api/auth/telegram',json={'init_data':raw},headers={'Origin':'https://evil.example'}).status_code==403
    monkeypatch.delenv('ALLOWED_TELEGRAM_USER_IDS')
    assert c.post('/api/auth/telegram',json={'init_data':raw}).status_code==403
    monkeypatch.setenv('ALLOWED_TELEGRAM_USER_IDS','123456789')
    r=c.post('/api/auth/telegram',json={'init_data':raw,'next':'//evil.example'}); assert r.json()['next']=='/'
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN','rotated-token')
    assert c.get('/api/dashboard').status_code==401


def test_mini_app_buttons(monkeypatch):
    monkeypatch.setenv('APP_BASE_URL','https://finance.example')
    monkeypatch.setenv('TELEGRAM_CHAT_ID','123456789')
    b=M.telegram_button('/?transaction=42','Разобрать'); assert b['web_app']['url']=='https://finance.example/mini?next=%2F%3Ftransaction%3D42'
    monkeypatch.setenv('TELEGRAM_CHAT_ID','-100123456789')
    monkeypatch.delenv('TELEGRAM_BOT_USERNAME',raising=False); monkeypatch.delenv('TELEGRAM_MINI_APP_SHORT_NAME',raising=False)
    assert M.telegram_button('/?transaction=42')['url']=='https://finance.example/?transaction=42'
    monkeypatch.setenv('TELEGRAM_BOT_USERNAME','family_finance_bot'); monkeypatch.setenv('TELEGRAM_MINI_APP_SHORT_NAME','finance')
    assert M.telegram_button('/?transaction=42')['url']=='https://t.me/family_finance_bot/finance?startapp=tx_42'
    assert M.telegram_button('/?section=limits')['url'].endswith('startapp=limits')
    assert M.telegram_button()['url'].endswith('startapp=home')
    monkeypatch.setenv('APP_BASE_URL','http://localhost:8000'); assert M.telegram_button() is None
