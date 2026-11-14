"""Stateless, allowlisted Telegram classification for imported bank operations.
The group message is shared by both users. Finalization is atomic and only
pending SMS transactions may be finalized; stale buttons cannot overwrite them.
"""
import re
from app import ledger as L

KINDS={'e':'expense','i':'income','r':'refund','t':'transfer','n':'internal'}
KIND_NAMES={'e':'Расход','i':'Доход','r':'Возврат расхода','t':'Перевод между контурами','n':'Между своими картами / наличные'}
SCOPES={'f':'family','m':'marketing','z':'reserve','v':'investment'}
PAGE_SIZE=6


def callback(tid,*parts):
    data='ff:'+str(tid)+':'+':'.join(map(str,parts))
    if len(data.encode())>64: raise ValueError('callback too long')
    return data


def button(tid,label,*parts):
    return {'text':str(label)[:64],'callback_data':callback(tid,*parts)}


def bank_text(t):
    status={'pending':'Ожидает классификации','posted':'Учтена','ignored':'Исключена'}.get(t['status'],t['status'])
    return f"Банковская операция: {t['original']/100:.2f} {t['currency']}\n{t['description']}\n{t['occurred_at'] or t['day']} · {status}"


def initial_keyboard(t,link=None):
    out=[]
    if t['status']=='pending': out.append([button(t['id'],'Разобрать прямо в Telegram','start')])
    if link: out.append([link])
    return out


def summary(c,t):
    cat=c.execute('SELECT name FROM v3_categories WHERE id=?',(t['category_id'],)).fetchone()
    result=L.SCOPES.get(t['scope'],'')
    if t['target']: result+=' → '+L.SCOPES.get(t['target'],'')
    if cat: result+=' / '+cat['name']
    return result


def decode(data):
    if not isinstance(data,str) or len(data.encode())>64: raise ValueError('Некорректная кнопка')
    parts=data.split(':')
    if len(parts)<3 or parts[0]!='ff' or not re.fullmatch(r'[1-9][0-9]{0,18}',parts[1]): raise ValueError('Некорректная кнопка')
    tid=int(parts[1]);
    if tid>2**63-1: raise ValueError('Некорректная операция')
    return tid,parts[2:]


def numeric(value):
    if not re.fullmatch(r'0|[1-9][0-9]{0,18}',value): raise ValueError('Некорректная кнопка')
    n=int(value)
    if n>2**63-1: raise ValueError('Некорректная кнопка')
    return n


def screen(data,link_for):
    """Return (text, keyboard). All DB changes are committed before Telegram I/O."""
    tid,args=decode(data); action=args[0]; rest=args[1:]
    with L.db() as c:
        c.execute('BEGIN IMMEDIATE')
        t=c.execute('SELECT * FROM v3_transactions WHERE id=? AND sms_hash IS NOT NULL',(tid,)).fetchone()
        if not t: raise ValueError('Банковская операция не найдена')
        t=dict(t); base=bank_text(t)
        link=link_for(f'/?transaction={tid}','Открыть в приложении')
        app_rows=[[link]] if link else []
        back=lambda *parts: [button(tid,'← Назад',*parts)]
        if t['status']!='pending':
            details=summary(c,t) if t['status']=='posted' else ''
            return base+('\n'+details if details else ''),app_rows
        if action=='start' and not rest:
            return base+'\n\nВыберите тип операции:',[[button(tid,KIND_NAMES[k],'k',k)] for k in KINDS]+[[button(tid,'Исключить из учёта','ignore')]]+app_rows
        if action=='ignore' and not rest:
            return base+'\n\nИсключить эту операцию из финансового учёта?',[[button(tid,'Да, исключить','drop')],back('start')]+app_rows
        if action=='drop' and not rest:
            c.execute("UPDATE v3_transactions SET status='ignored' WHERE id=? AND status='pending'",(tid,))
            t['status']='ignored'
            return bank_text(t),app_rows
        if t['amount'] is None: raise ValueError('Курс валюты ещё не получен. Повторите позже или откройте приложение.')
        if not rest or rest[0] not in KINDS: raise ValueError('Неизвестный тип операции')
        k=rest[0]; kind=KINDS[k]
        if action=='k' and len(rest)==1:
            return base+f'\n\n{KIND_NAMES[k]}: выберите контур'+(' отправления' if k=='t' else ''),[[button(tid,name,'s',k,short)] for short,s in SCOPES.items() for name in [L.SCOPES[s]]]+[back('start')]+app_rows
        if len(rest)<2 or rest[1] not in SCOPES: raise ValueError('Неизвестный контур')
        short=rest[1]; scope=SCOPES[short]; prefix=base+f'\n\n{KIND_NAMES[k]} · {L.SCOPES[scope]}'
        if action=='s' and len(rest)==2:
            if k=='t':
                return prefix+'\nКуда перевести?',[[button(tid,L.SCOPES[s],'dest',k,short,x)] for x,s in SCOPES.items() if x!=short]+[back('k',k)]+app_rows
            if k=='n':
                return prefix+'\nСохранить внутреннюю операцию? Она не изменит баланс.',[[button(tid,'Сохранить','save',k,short,0,0)],back('k',k)]+app_rows
            action='p'; rest=[k,short,'0','0']
        if action=='dest' and len(rest)==3 and k=='t':
            dest=rest[2]
            if dest not in SCOPES or dest==short: raise ValueError('Выберите другой контур')
            return prefix+' → '+L.SCOPES[SCOPES[dest]]+'\nПодтвердите перевод.',[[button(tid,'Сохранить перевод','save',k,short,dest,0)],back('s',k,short)]+app_rows
        cat_kind='expense' if k=='r' else kind
        cats={x['id']:x for x in L.categories(c) if x['scope']==scope and x['kind']==cat_kind and not x['archived']}
        if action=='p' and len(rest)==4 and k in ('e','i','r'):
            parent=numeric(rest[2]); page=numeric(rest[3]); parent_cat=cats.get(parent)
            if parent and not parent_cat: raise ValueError('Категория недоступна')
            children=[x for x in cats.values() if (x['parent_id'] if x['parent_id'] in cats else None)==(parent or None)]
            if page>max(0,(len(children)-1)//PAGE_SIZE): raise ValueError('Страница недоступна')
            out=[]
            if parent_cat: out.append([button(tid,'✓ '+parent_cat['name'],'c',k,short,parent)])
            for cat in children[page*PAGE_SIZE:(page+1)*PAGE_SIZE]:
                has_children=any(x['parent_id']==cat['id'] for x in cats.values())
                out.append([button(tid,cat['name']+(' ›' if has_children else ''),*(['p',k,short,cat['id'],0] if has_children else ['c',k,short,cat['id']]))])
            nav=[]
            if page: nav.append(button(tid,'‹ Предыдущая','p',k,short,parent,page-1))
            if (page+1)*PAGE_SIZE<len(children): nav.append(button(tid,'Следующая ›','p',k,short,parent,page+1))
            if nav: out.append(nav)
            out.append(back('p',k,short,parent_cat['parent_id'] if parent_cat['parent_id'] in cats else 0,0) if parent_cat else back('k',k))
            return prefix+'\n'+('Подкатегории: '+parent_cat['name'] if parent_cat else 'Выберите категорию:'),out+app_rows
        if action=='c' and len(rest)==3 and k in ('e','i','r'):
            cat_id=numeric(rest[2]); cat=cats.get(cat_id)
            if not cat: raise ValueError('Категория недоступна')
            out=[[button(tid,'Сохранить','save',k,short,cat_id,0)]]
            if t['merchant'] and t['hint']=='payment' and k in ('e','i'): out.append([button(tid,'Сохранить и запомнить магазин','save',k,short,cat_id,1)])
            return prefix+' / '+cat['name']+'\nПодтвердите классификацию.',out+[back('p',k,short,cat['parent_id'] if cat['parent_id'] in cats else 0,0)]+app_rows
        if action=='save' and len(rest)==4 and rest[3] in ('0','1'):
            target=None; cat_id=None
            if k=='t':
                target=SCOPES.get(rest[2]);
                if not target or target==scope: raise ValueError('Выберите другой контур')
            elif k=='n':
                if rest[2]!='0': raise ValueError('Некорректная операция')
            else:
                cat_id=numeric(rest[2]);
                if cat_id not in cats: raise ValueError('Категория недоступна')
            changed=c.execute("UPDATE v3_transactions SET kind=?,scope=?,target=?,category_id=?,status='posted' WHERE id=? AND status='pending'",(kind,scope,target,cat_id,tid)).rowcount
            if not changed: raise ValueError('Операция уже обработана')
            if rest[3]=='1' and t['merchant'] and t['hint']=='payment' and k in ('e','i'):
                c.execute('INSERT OR REPLACE INTO v3_rules VALUES(?,?,?,?)',(t['merchant'].casefold(),kind,scope,cat_id))
            L.queue_alerts(c,tid)
            t.update(status='posted',kind=kind,scope=scope,target=target,category_id=cat_id)
            return bank_text(t)+'\n✅ '+summary(c,t),app_rows
        raise ValueError('Некорректная кнопка')
