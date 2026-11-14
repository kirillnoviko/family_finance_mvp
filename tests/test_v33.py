import asyncio
from app import ledger as L
from app import main as M
from app import telegram_ui as T
from test_v3 import client, cat, tx, limit


def monthly(c,name,amount,ids):
    res=c.post('/api/limits',json=dict(name=name,amount=amount,scope='family',category_ids=ids,start_month=L.today().strftime('%Y-%m'),period='monthly'))
    assert res.status_code==200,res.text
    return res.json()['id']


def test_exclusive_chart_groups_and_transfer_shares(client):
    c=client; food=cat(c,'food'); cafe=cat(c,'cafe'); groceries=cat(c,'groceries'); clothes=cat(c,'clothes'); doctors=cat(c,'doctors')
    general=limit(c,'Повседневные',500,[food]); cafe_limit=limit(c,'Кафе',70,[cafe]); clothes_limit=monthly(c,'Одежда и кафе',300,[clothes,cafe])
    a=tx(c,'expense','family',40,cafe); b=tx(c,'expense','family',30,groceries); d=tx(c,'expense','family',20,clothes); e=tx(c,'expense','family',10,doctors); f=tx(c,'refund','family',5,cafe)
    z=tx(c,'transfer','family',100,target='reserve'); m=tx(c,'transfer','family',50,target='marketing'); v=tx(c,'transfer','family',30,target='investment'); tx(c,'transfer','marketing',25,target='family')
    q=dict(start=L.today().isoformat(),end=L.today().isoformat(),scope='family',limit_period='weekly')
    r=c.get('/api/report',params=q).json(); assert r['expense']==9500 and r['weeks'][0]['limited']==6500
    assert r['weeks'][0]['limited_ids']==[f,b,a]
    assert set(r['weeks'][0]['ids'])=={a,b,d,e,f}
    groups={g['id']:g for g in r['limit_groups']}; assert groups['weekly']['amount']==6500
    assert {g['id']:g['amount'] for g in groups['weekly']['subgroups']}=={general:3000,cafe_limit:3500}
    assert groups['l'+str(clothes_limit)]['amount']==2000
    assert sum(g['amount'] for g in groups.values())==r['expense']
    all_ids=[i for g in groups.values() for i in g['ids']]; assert len(all_ids)==len(set(all_ids))==5
    assert {g['id']:g['amount'] for g in r['transfer_groups']}=={'reserve':10000,'marketing':5000,'investment':3000}
    assert r['transfer_out']==18000 and r['transfer_in']==2500
    assert {i for g in r['transfer_groups'] for i in g['ids']}=={z,m,v}
    # Explicit primary changes only the graph, not actual limit spend or the weekly union.
    assert c.put(f'/api/limits/mapping/{cafe}',json={'limit_id':clothes_limit}).status_code==200
    r=c.get('/api/report',params=q).json(); groups={g['id']:g for g in r['limit_groups']}
    assert groups['weekly']['amount']==3000 and groups['l'+str(clothes_limit)]['amount']==5500
    assert r['weeks'][0]['limited']==6500
    assert {l['id']:l['spent'] for l in c.get('/api/limits').json()}=={general:6500,cafe_limit:3500,clothes_limit:5500}
    assert c.put(f'/api/limits/mapping/{doctors}',json={'limit_id':clothes_limit}).status_code==422
    q['limit_period']='monthly'; assert c.get('/api/report',params=q).json()['weeks'][0]['limited']==5500
    q['limit_period']='daily'; assert c.get('/api/report',params=q).status_code==422


def test_mapping_inheritance_and_migration(client):
    c=client; food=cat(c,'food'); cafe=cat(c,'cafe'); groceries=cat(c,'groceries'); doctors=cat(c,'doctors')
    w=limit(c,'Неделя',500,[food]); m=monthly(c,'Месяц',1000,[food])
    txid=tx(c,'expense','family',20,groceries)
    assert c.put(f'/api/limits/mapping/{food}',json={'limit_id':m}).status_code==200
    mapping={x['category_id']:x for x in c.get('/api/limits/mapping').json()}
    assert mapping[cafe]['effective_id']==m and mapping[groceries]['effective_id']==m and mapping[doctors]['effective_id'] is None
    assert c.put(f'/api/limits/mapping/{cafe}',json={'limit_id':w}).status_code==200
    assert next(x for x in c.get('/api/limits/mapping').json() if x['category_id']==cafe)['effective_id']==w
    assert c.put(f'/api/limits/mapping/{cafe}',json={'limit_id':None}).status_code==200
    assert next(x for x in c.get('/api/limits/mapping').json() if x['category_id']==cafe)['effective_id']==m
    # Simulate the previous schema, then rerun the additive migration.
    with L.db() as db: db.execute('DROP TABLE v33_primary_limits')
    L.init()
    assert next(x for x in c.get('/api/limits/mapping').json() if x['category_id']==cafe)['effective_id']==w
    assert any(t['id']==txid for t in c.get('/api/transactions').json())
    assert len(c.get('/api/limits/all').json())==2


def configure_telegram(monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN','fake-token')
    monkeypatch.setenv('TELEGRAM_CHAT_ID','-10012345')
    monkeypatch.setenv('ALLOWED_TELEGRAM_USER_IDS','123,456')
    monkeypatch.setenv('APP_BASE_URL','https://finance.example')
    monkeypatch.setenv('TELEGRAM_WEBHOOK_SECRET','webhook-secret')
    monkeypatch.setenv('TELEGRAM_BOT_USERNAME','financefamily_bot')
    monkeypatch.setenv('TELEGRAM_MINI_APP_SHORT_NAME','finance')


def imported(c,seconds='01',amount='20.00',merchant='TEST SHOP',currency='BYN'):
    d=L.today().strftime('%d-%m-%y')
    raw=f'Karta 4***1234 {d} 10:34:{seconds}. Oplata {amount} {currency}. BLR {merchant}. Balance: 200.00 BYN'
    r=c.post('/api/sms',json={'message':raw},headers={'Authorization':'Bearer test-secret'},auth=None)
    assert r.status_code==200,r.text
    return r.json()['transaction_id']


def test_group_callback_full_classification_and_access(client,monkeypatch):
    configure_telegram(monkeypatch); c=client; calls=[]
    async def request(method,data): calls.append((method,data)); return True
    monkeypatch.setattr(M,'tg_request',request)
    food=cat(c,'food'); cafe=cat(c,'cafe'); lid=limit(c,'Готовая еда',70,[cafe]); tid=imported(c)
    hdr={'X-Telegram-Bot-Api-Secret-Token':'webhook-secret'}
    def click(action,uid=123,chat=-10012345,headers=hdr):
        payload={'callback_query':{'id':'q','from':{'id':uid},'message':{'chat':{'id':chat},'message_id':42},'data':f'ff:{tid}:{action}'}}
        return c.post('/telegram/webhook',json=payload,headers=headers,auth=None)
    assert click('start',headers={}).status_code==403
    assert click('start',uid=999).status_code==200 and calls[-1][0]=='answerCallbackQuery' and calls[-1][1]['show_alert']
    assert click('start',chat=-100999).status_code==200 and calls[-1][1]['show_alert']
    asyncio.run(M.flush()); message=next(data for method,data in calls if method=='sendMessage' and 'Банковская' in data['text']); keyboard=message['reply_markup']['inline_keyboard']
    assert keyboard[0][0]['callback_data']==f'ff:{tid}:start'
    assert keyboard[1][0]['url'].endswith(f'?startapp=tx_{tid}')
    for action in ['start','k:e','s:e:f',f'p:e:f:{food}:0',f'c:e:f:{cafe}']:
        assert click(action).status_code==200
        assert calls[-1][0]=='editMessageText'
        assert calls[-1][1]['reply_markup']['inline_keyboard']
    assert 'Подтвердите' in calls[-1][1]['text']
    # Invalid category in another contour is denied, and nothing is posted.
    assert click(f'save:e:f:{cat(c,"tax")}:0').status_code==200 and calls[-1][0]=='answerCallbackQuery' and calls[-1][1]['show_alert']
    assert next(t for t in c.get('/api/transactions').json() if t['id']==tid)['status']=='pending'
    assert click(f'save:e:f:{cafe}:1').status_code==200
    t=next(t for t in c.get('/api/transactions').json() if t['id']==tid)
    assert t['status']=='posted' and t['category_id']==cafe and t['amount']==2000 and t['currency']=='BYN'
    assert '✅' in calls[-1][1]['text']
    # Wife's stale button cannot overwrite the result.
    assert click(f'save:e:f:{cat(c,"groceries")}:0',uid=456).status_code==200
    assert next(t for t in c.get('/api/transactions').json() if t['id']==tid)['category_id']==cafe
    # Web form opened while pending cannot overwrite the completed classification.
    res=c.put(f'/api/transactions/{tid}',json={'day':t['day'],'kind':'expense','scope':'family','category_id':food,'amount':20,'expected_status':'pending'})
    assert res.status_code==409
    with L.db() as db:
        assert db.execute('SELECT COUNT(*) FROM v31_purchase_alerts WHERE limit_id=? AND tx_id=?',(lid,tid)).fetchone()[0]==1
        assert db.execute('SELECT category_id FROM v3_rules').fetchone()[0]==cafe
    second=imported(c,seconds='02'); assert next(t for t in c.get('/api/transactions').json() if t['id']==second)['status']=='posted'
    asyncio.run(M.flush()); assert any(method=='sendMessage' and 'Покупка:' in data['text'] and data['reply_markup']['inline_keyboard'][0][0]['url'].endswith('startapp=limits') for method,data in calls)


def test_group_pending_command_and_other_operation_types(client,monkeypatch):
    configure_telegram(monkeypatch); calls=[]; c=client
    async def request(method,data): calls.append((method,data)); return True
    monkeypatch.setattr(M,'tg_request',request)
    tid=imported(c,merchant='TRANSFER'); internal=imported(c,seconds='02',merchant='INTERNAL'); ignored=imported(c,seconds='03',merchant='IGNORE')
    with L.db() as db: db.execute('UPDATE v3_transactions SET notified=1')
    payload={'message':{'from':{'id':123},'chat':{'id':-10012345},'text':'/pending@financefamily_bot'}}
    assert c.post('/telegram/webhook',json=payload,headers={'X-Telegram-Bot-Api-Secret-Token':'webhook-secret'},auth=None).status_code==200
    sent=[d for m,d in calls if m=='sendMessage']; assert len(sent)==4 and sent[0]['reply_markup']['inline_keyboard'][0][0]['url'].endswith('startapp=pending')
    assert M.mini_target('pending')=='/?section=pending'
    text,buttons=T.screen(f'ff:{tid}:s:t:f',M.telegram_button); assert 'Куда перевести' in text and len(buttons)==5
    T.screen(f'ff:{tid}:save:t:f:m:0',M.telegram_button)
    T.screen(f'ff:{internal}:save:n:f:0:0',M.telegram_button)
    T.screen(f'ff:{ignored}:drop',M.telegram_button)
    with L.db() as db:
        b=L.balances(db,L.today().isoformat()); assert b['family']==-2000 and b['marketing']==2000
        assert db.execute('SELECT status FROM v3_transactions WHERE id=?',(ignored,)).fetchone()[0]=='ignored'
    assert all(t['status']!='pending' for t in c.get('/api/transactions').json())


def test_webhook_registration_and_railway_url_fallback(client,monkeypatch):
    configure_telegram(monkeypatch); calls=[]
    async def request(method,data): calls.append((method,data)); return True
    monkeypatch.setattr(M,'tg_request',request)
    monkeypatch.delenv('APP_BASE_URL'); monkeypatch.setenv('RAILWAY_PUBLIC_DOMAIN','finance.up.railway.app')
    assert M.public_base()=='https://finance.up.railway.app'
    assert asyncio.run(M.setup_webhook())
    assert calls[-1]==('setWebhook',{'url':'https://finance.up.railway.app/telegram/webhook','secret_token':'webhook-secret','allowed_updates':['message','callback_query']})
    monkeypatch.delenv('TELEGRAM_MINI_APP_SHORT_NAME'); assert M.telegram_button('/?transaction=42')['url']=='https://finance.up.railway.app/?transaction=42'
    monkeypatch.setenv('APP_BASE_URL','my-finance.example'); assert M.public_base()=='https://my-finance.example'
    monkeypatch.delenv('TELEGRAM_WEBHOOK_SECRET'); assert M.webhook_secret()!=M.secret() and len(M.webhook_secret())==64
    monkeypatch.delenv('APP_BASE_URL'); monkeypatch.delenv('RAILWAY_PUBLIC_DOMAIN'); assert M.telegram_button() is None
    tid=imported(client); with_db=None
    with L.db() as db: t=dict(db.execute('SELECT * FROM v3_transactions WHERE id=?',(tid,)).fetchone())
    assert M.tx_keyboard(t)==[[{'text':'Разобрать прямо в Telegram','callback_data':f'ff:{tid}:start'}]]


def test_combined_distribution(client):
    from datetime import timedelta
    c=client; d=L.today(); prev=(d-timedelta(days=1)).isoformat(); today=d.isoformat()
    tx(c,'opening','family',100,day=prev)
    tx(c,'opening','family',50)
    tx(c,'income','family',1000,cat(c,'salary_kirill'))
    tx(c,'income','marketing',500,cat(c,'marketing_client_income'))
    tx(c,'transfer','marketing',200,target='family')
    tx(c,'expense','family',100,cat(c,'cafe'))
    tx(c,'transfer','family',300,target='reserve')
    tx(c,'transfer','family',100,target='investment')
    limit(c,'Еда',500,[cat(c,'food')])
    q=dict(start=today,end=today,scope='family')
    r=c.get('/api/report',params=q).json()
    assert r['available']==135000 and r['opening_added']==5000 and r['closing']==85000
    for key in ('distribution_categories','distribution_limits'):
        g=r[key]; assert sum(x['amount'] for x in g)==r['available']
        assert {x['id']:x['amount'] for x in g if x['type']=='transfer'}=={'reserve':30000,'investment':10000}
        assert g[-1]['amount']==85000
    q.pop('scope'); r=c.get('/api/report',params=q).json()
    assert r['available']==165000 and r['closing']==155000
    assert all(x['type']!='transfer' for x in r['distribution_categories'])
    assert sum(x['amount'] for x in r['distribution_limits'])==r['available']
