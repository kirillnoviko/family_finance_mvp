import asyncio
from app import main as M, ledger as L, telegram_ui as T
from test_v3 import client, cat
from test_v33 import configure_telegram, imported


def test_detailed_bank_notifications_and_remember(client, monkeypatch):
    configure_telegram(monkeypatch)
    calls=[]
    async def request(method,data): calls.append((method,data)); return True
    monkeypatch.setattr(M,'tg_request',request)
    tid=imported(client, amount='39.00', merchant='TEST SHOP', currency='USD')
    asyncio.run(M.flush())
    msg=next(d for m,d in calls if m=='sendMessage' and 'Банковская' in d['text'])
    assert '−39.00 USD' in msg['text']
    assert 'Баланс после: 200.00 BYN' in msg['text']
    assert '4***1234' in msg['text'] and 'TEST SHOP' in msg['text']
    assert msg['reply_markup']['inline_keyboard'][0][0]['callback_data']==f'ff:{tid}:start'
    cafe=cat(client,'cafe')
    text,keyboard=T.screen(f'ff:{tid}:save:e:f:{cafe}:0',M.telegram_button)
    assert '✅ Операция учтена' in text and 'Контур: 🏠 Семья' in text
    assert 'Категория: ' in text and 'Кафе' in text and '−39.00 USD' in text
    assert keyboard[0][0]['callback_data']==f'ff:{tid}:remember'
    text,keyboard=T.screen(f'ff:{tid}:remember',M.telegram_button)
    assert 'Магазин запомнен' in text and all('callback_data' not in b for row in keyboard for b in row)
    with L.db() as db:
        assert db.execute('SELECT category_id FROM v3_rules').fetchone()[0]==cafe
    second=imported(client,seconds='02',merchant='TEST SHOP',currency='USD')
    with L.db() as db:
        t=dict(db.execute('SELECT * FROM v3_transactions WHERE id=?',(second,)).fetchone())
    assert t['status']=='posted' and 'Контур: 🏠 Семья' in T.bank_text(t)
    assert len(M.tx_keyboard(t))==1


def test_incoming_sign_survives_accounting_classification(client):
    raw='Karta 4***1234 14-11-26 11:18:54. Zachislenie perevoda 39.00 BYN. P2P SDBO NO FEE. Balance: 3759.82 BYN'
    r=client.post('/api/sms',json={'message':raw},headers={'Authorization':'Bearer test-secret'},auth=None)
    assert r.status_code==200,r.text
    tid=r.json()['transaction_id']
    with L.db() as db: t=dict(db.execute('SELECT * FROM v3_transactions WHERE id=?',(tid,)).fetchone())
    assert '+39.00 BYN' in T.bank_text(t) and '3759.82 BYN' in T.bank_text(t)
    text,_=T.screen(f'ff:{tid}:save:t:z:f:0',lambda *a:None)
    assert '+39.00 BYN' in text and 'НЗ → 🏠 Семья' in text


def test_legacy_metadata_migration(client):
    with L.db() as db:
        for name in ['bank_direction','bank_balance_minor','bank_balance_currency','bank_amount_minor','bank_currency','bank_description']:
            db.execute(f'ALTER TABLE v3_transactions DROP COLUMN {name}')
        tid=db.execute("INSERT INTO v3_transactions(day,kind,amount,currency,original,description,status,sms_hash,hint) VALUES('2026-11-14','expense',3900,'BYN',3900,'P2P','pending','old','transfer')").lastrowid
    L.init(); L.init()
    with L.db() as db: t=dict(db.execute('SELECT * FROM v3_transactions WHERE id=?',(tid,)).fetchone())
    assert '−39.00 BYN' in T.bank_text(t) and 'Баланс после' not in T.bank_text(t)
