import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
import httpx

from app.categories import BY_CODE,children,roots,title
from app.config import settings
from app.db import (
    clear_user_state,create_manual_transaction,find_rule,get_transaction,get_user_state,
    list_pending,reset_transaction,save_rule,set_opening_balance,set_telegram_message,
    set_user_state,update_transaction
)
from app.exchange import convert_byn,get_byn_rates
from app.reporting import (
    current_balances,current_calendar_month,family_report,financial_period,last_days,
    marketing_report,parse_custom_period,previous_calendar_month,
    previous_financial_period,reserve_report
)

logger=logging.getLogger(__name__)
class TelegramError(RuntimeError): pass

def b(text,data): return {'text':text,'callback_data':data}
def markup(rows): return {'inline_keyboard':rows}
def force_reply(): return {'force_reply':True,'selective':True}
def main_keyboard():
    return {
        'keyboard':[
            [{'text':'➕ Добавить'},{'text':'📊 Статистика'}],
            [{'text':'💰 Балансы'},{'text':'⏳ Разобрать'}],
            [{'text':'ℹ️ Помощь'}]
        ],
        'resize_keyboard':True,
        'is_persistent':True
    }

async def api_call(method,payload):
    url=f'https://api.telegram.org/bot{settings.telegram_bot_token}/{method}'
    async with httpx.AsyncClient(timeout=15) as client:
        r=await client.post(url,json=payload)
    try: data=r.json()
    except ValueError as exc: raise TelegramError(f'Telegram invalid JSON HTTP {r.status_code}') from exc
    if not r.is_success or not data.get('ok'): raise TelegramError(f"{method}: {data.get('description',r.text)}")
    return data['result']

async def send_text(text,chat_id=None,reply_markup=None):
    p={'chat_id':chat_id or settings.telegram_chat_id,'text':text,'disable_web_page_preview':True}
    if reply_markup is not None: p['reply_markup']=reply_markup
    result=await api_call('sendMessage',p)
    return int(result['message_id'])

async def edit_text(chat_id,message_id,text,reply_markup=None):
    p={'chat_id':chat_id,'message_id':message_id,'text':text}
    if reply_markup is not None: p['reply_markup']=reply_markup
    await api_call('editMessageText',p)

async def answer_callback(callback_id,text=None):
    p={'callback_query_id':callback_id}
    if text: p['text']=text[:180]
    await api_call('answerCallbackQuery',p)

async def setup_webhook():
    if not settings.app_base_url:
        logger.warning('APP_BASE_URL is empty; Telegram webhook not configured')
        return
    await api_call('setWebhook',{
        'url':f'{settings.app_base_url}/telegram/webhook',
        'secret_token':settings.telegram_webhook_secret,
        'allowed_updates':['message','callback_query'],
        'drop_pending_updates':False
    })
    await api_call('setMyCommands',{'commands':[
        {'command':'start','description':'Главное меню'},
        {'command':'add','description':'Добавить операцию'},
        {'command':'stats','description':'Статистика'},
        {'command':'balances','description':'Балансы'},
        {'command':'pending','description':'Неразобранные операции'},
        {'command':'help','description':'Помощь'}
    ]})
    logger.info('Telegram webhook and commands configured')

def allowed(user_id):
    return bool(user_id) and (not settings.allowed_telegram_user_ids or user_id in settings.allowed_telegram_user_ids)

def amount_text(tx,signed=False):
    sign=('+' if tx.direction=='in' else '-') if signed else ''
    return f'{sign}{tx.amount:.2f} {tx.currency}'

def header(tx):
    emoji='💰' if tx.direction=='in' else '💸'
    details=tx.merchant or tx.description or 'Операция'
    lines=[f'{emoji} {amount_text(tx,True)}',details,'',f"📅 {tx.occurred_at.replace('T',' ')}",f'💳 {tx.physical_account}']
    if tx.balance_after is not None: lines.append(f'Баланс после: {tx.balance_after:.2f} {tx.currency}')
    return '\n'.join(lines)

def category_keyboard(tx,scope):
    if tx.direction=='in':
        cats=[BY_CODE['salary_kirill'],BY_CODE['salary_wife'],BY_CODE['family_other_income'],BY_CODE['reserve_to_family']] if scope=='family' else [BY_CODE['marketing_client_income'],BY_CODE['marketing_other_income']]
    else:
        cats=[BY_CODE['assistant_salary'],BY_CODE['tax'],BY_CODE['business_expense'],BY_CODE['reserve_contribution']] if scope=='marketing' else roots('family','expense')
    rows=[]
    for i in range(0,len(cats),2):
        rows.append([b(f'{c.emoji} {c.title}',f'cat:{tx.id}:{c.code}') for c in cats[i:i+2]])
    rows.append([b('⬅️ Назад',f'change:{tx.id}')])
    return markup(rows)

def subcategory_keyboard(tx,parent):
    cats=children(parent); rows=[]
    for i in range(0,len(cats),2):
        rows.append([b(f'{c.emoji} {c.title}',f'cat:{tx.id}:{c.code}') for c in cats[i:i+2]])
    rows.append([b(f"✅ Оставить «{BY_CODE[parent].title}»",f'final:{tx.id}:{parent}')])
    rows.append([b('⬅️ Назад',f'scope:{tx.id}:family')])
    return markup(rows)

def initial_keyboard(tx):
    if tx.direction=='in':
        return markup([[b('🏠 Семья',f'scope:{tx.id}:family'),b('📈 Маркетинг',f'scope:{tx.id}:marketing')],
                       [b('🔄 Свои деньги',f'special:{tx.id}:own_transfer'),b('🚫 Игнорировать',f'special:{tx.id}:ignore')]])
    return markup([[b('🏠 Семья',f'scope:{tx.id}:family'),b('📈 Маркетинг',f'scope:{tx.id}:marketing')],
                   [b('🔄 Перевод',f'special:{tx.id}:own_transfer'),b('↩️ Возврат',f'special:{tx.id}:refund')],
                   [b('🚫 Игнорировать',f'special:{tx.id}:ignore')]])

def finalized_text(tx):
    scope='🏠 Семья' if tx.scope=='family' else '📈 Маркетинг' if tx.scope=='marketing' else '🔄 Внутренний'
    lines=['✅ Операция учтена','',header(tx),'',f'Контур: {scope}',f'Категория: {title(tx.category_code)}']
    if tx.source and not tx.source.startswith('telegram:'): lines.append(f'Источник: {tx.source}')
    return '\n'.join(lines)

def finalized_keyboard(tx):
    rows=[[b('✏️ Изменить',f'change:{tx.id}')]]
    if tx.merchant and tx.scope in {'family','marketing'} and tx.operation_type=='expense' and tx.category_code:
        rows.insert(0,[b('🧠 Запомнить магазин',f'remember:{tx.id}')])
    return markup(rows)

def manual_flow_keyboard():
    return markup([
        [b('🏠 Расход семьи','manual:family_expense'),b('🏠 Доход семьи','manual:family_income')],
        [b('📈 Расход маркетинга','manual:marketing_expense'),b('📈 Доход маркетинга','manual:marketing_income')],
        [b('🛡 Перевести в НЗ','manual:reserve_in'),b('🏠 Из НЗ семье','manual:reserve_out')]
    ])

def balances_keyboard():
    return markup([
        [b('🏠 Старт семьи','openbal:family'),b('📈 Старт маркетинга','openbal:marketing')],
        [b('🛡 Старт НЗ','openbal:reserve')]
    ])

def periods_keyboard():
    return markup([
        [b('📅 Текущий 15→14','period:current'),b('◀️ Прошлый 15→14','period:previous')],
        [b('🗓 Этот месяц','period:month'),b('◀️ Прошлый месяц','period:prevmonth')],
        [b('30 дней','period:30'),b('✏️ Свой период','period:custom')]
    ])

def report_keyboard(start,end):
    s=start.strftime('%Y%m%d'); e=end.strftime('%Y%m%d')
    return markup([
        [b('🏠 Семья',f'rep:family:{s}:{e}'),b('📈 Маркетинг',f'rep:marketing:{s}:{e}')],
        [b('🛡 НЗ',f'rep:reserve:{s}:{e}'),b('📅 Другой период','showperiod:x')]
    ])

async def balances_text():
    balances=current_balances()
    rates=await get_byn_rates()
    def line(label,minor,fx=False):
        byn=Decimal(minor)/100
        s=f'{label}: {byn:.2f} BYN'
        if fx and rates:
            c=convert_byn(byn,rates)
            p=[]
            if 'USD' in c: p.append(f"${c['USD']:.2f}")
            if 'EUR' in c: p.append(f"€{c['EUR']:.2f}")
            if p: s+=' ≈ '+' / '.join(p)
        return s
    return '\n'.join([
        '💰 БАЛАНСЫ','',
        line('🏠 Семья',balances['family']),
        line('📈 Маркетинг',balances['marketing'],True),
        line('🛡 НЗ',balances['reserve'],True),
        '',
        'Стартовые остатки не создают доход или расход.'
    ])

async def fx_suffix(bucket):
    bal=current_balances()[bucket]
    rates=await get_byn_rates()
    if not rates: return ''
    byn=Decimal(bal)/100
    c=convert_byn(byn,rates)
    parts=[]
    if 'USD' in c: parts.append(f"${c['USD']:.2f}")
    if 'EUR' in c: parts.append(f"€{c['EUR']:.2f}")
    return '' if not parts else '\n\n💱 Текущий баланс: '+f'{byn:.2f} BYN ≈ '+' / '.join(parts)

async def send_transaction_for_classification(tx):
    rule=find_rule(tx.merchant,tx.operation_type)
    if rule:
        tx=update_transaction(tx.id,scope=rule['scope'],category_code=rule['category_code'],status='categorized')
        mid=await send_text(finalized_text(tx)+'\n\n🧠 Категория применена по правилу.',reply_markup=finalized_keyboard(tx))
        set_telegram_message(tx.id,settings.telegram_chat_id,mid)
        return
    mid=await send_text(header(tx)+'\n\nК чему относится операция?',reply_markup=initial_keyboard(tx))
    set_telegram_message(tx.id,settings.telegram_chat_id,mid)

async def edit_tx(tx,text,keyb,msg=None):
    if msg:
        chat_id=msg['chat']['id']; mid=msg['message_id']
    elif tx.telegram_chat_id and tx.telegram_message_id:
        chat_id=tx.telegram_chat_id; mid=tx.telegram_message_id
    else: return
    await edit_text(chat_id,mid,text,keyb)

async def finalize(tx,code,msg=None):
    cat=BY_CODE[code]; scope=tx.scope or cat.scope; op=tx.operation_type; direction=tx.direction; source=tx.source
    if cat.kind=='income': op='income'; direction='in'
    elif cat.kind=='expense': op='expense'; direction='out'
    elif cat.kind=='allocation': op='allocation'; direction='out'
    elif cat.kind=='transfer': op='transfer'
    if code=='marketing_client_income': source=tx.merchant or tx.description or 'Клиент'
    elif code=='salary_kirill': source='Кирилл'
    elif code=='salary_wife': source='Жена'
    elif code=='reserve_to_family': scope='family'; direction='in'; source='НЗ'
    tx=update_transaction(tx.id,scope=scope,category_code=code,operation_type=op,direction=direction,status='categorized',source=source)
    await edit_tx(tx,finalized_text(tx),finalized_keyboard(tx),msg)

async def show_period(chat_id,start,end):
    await send_text(family_report(start,end),chat_id=chat_id,reply_markup=report_keyboard(start,end))

def decode_dates(s,e):
    return datetime.strptime(s,'%Y%m%d'),datetime.strptime(e,'%Y%m%d')

async def handle_callback(cb):
    cid=cb['id']; uid=cb.get('from',{}).get('id'); data=cb.get('data',''); msg=cb.get('message')
    chat_id=msg['chat']['id'] if msg else settings.telegram_chat_id
    if not allowed(uid):
        await answer_callback(cid,'Нет доступа'); return
    try:
        p=data.split(':'); action=p[0]
        if action=='showperiod':
            await send_text('Выберите период:',chat_id=chat_id,reply_markup=periods_keyboard())
            await answer_callback(cid); return
        if action=='period':
            choice=p[1]
            if choice=='current': start,end=financial_period()
            elif choice=='previous': start,end=previous_financial_period()
            elif choice=='month': start,end=current_calendar_month()
            elif choice=='prevmonth': start,end=previous_calendar_month()
            elif choice=='30': start,end=last_days(30)
            else:
                set_user_state(uid,'custom_period',{})
                await send_text('Ответьте двумя датами:\n2026-07-15 2026-08-14',chat_id=chat_id,reply_markup=force_reply())
                await answer_callback(cid); return
            await show_period(chat_id,start,end); await answer_callback(cid); return
        if action=='rep':
            name=p[1]; start,end=decode_dates(p[2],p[3])
            if name=='family': text=family_report(start,end)
            elif name=='marketing': text=marketing_report(start,end)+await fx_suffix('marketing')
            else: text=reserve_report(start,end)+await fx_suffix('reserve')
            await send_text(text,chat_id=chat_id,reply_markup=report_keyboard(start,end))
            await answer_callback(cid); return
        if action=='manual':
            set_user_state(uid,'manual_add',{'flow':p[1]})
            await send_text('Ответьте в формате:\n1200 Panda\n\nСумма + описание/источник.',chat_id=chat_id,reply_markup=force_reply())
            await answer_callback(cid); return
        if action=='openbal':
            bucket=p[1]
            set_user_state(uid,'opening_balance',{'bucket':bucket})
            labels={'family':'семьи','marketing':'маркетинга','reserve':'НЗ'}
            await send_text(f"Введите стартовую сумму для {labels[bucket]} в BYN.\nНапример: 3500",chat_id=chat_id,reply_markup=force_reply())
            await answer_callback(cid); return

        tx_id=int(p[1]) if len(p)>1 and p[1].isdigit() else None
        if action=='scope':
            scope=p[2]; tx=update_transaction(tx_id,scope=scope)
            await edit_tx(tx,header(tx)+f"\n\nКонтур: {'🏠 Семья' if scope=='family' else '📈 Маркетинг'}\nВыберите категорию:",category_keyboard(tx,scope),msg)
            await answer_callback(cid); return
        if action=='cat':
            code=p[2]; tx=get_transaction(tx_id)
            if not tx: await answer_callback(cid,'Операция не найдена'); return
            if children(code):
                await edit_tx(tx,header(tx)+f'\n\n{title(code)}\nВыберите подкатегорию:',subcategory_keyboard(tx,code),msg)
                await answer_callback(cid)
            else:
                await finalize(tx,code,msg); await answer_callback(cid,'Сохранено')
            return
        if action=='final':
            tx=get_transaction(tx_id)
            if tx: await finalize(tx,p[2],msg)
            await answer_callback(cid,'Сохранено'); return
        if action=='special':
            tx=get_transaction(tx_id)
            if not tx: await answer_callback(cid,'Операция не найдена'); return
            special=p[2]
            if special=='ignore':
                tx=update_transaction(tx.id,status='ignored')
                await edit_tx(tx,'🚫 Операция не учитывается\n\n'+header(tx),markup([[b('↩️ Вернуть',f'change:{tx.id}')]]),msg)
            elif special=='own_transfer':
                tx=update_transaction(tx.id,scope='internal',category_code='own_transfer',operation_type='transfer',status='categorized')
                await edit_tx(tx,finalized_text(tx),finalized_keyboard(tx),msg)
            elif special=='refund':
                tx=update_transaction(tx.id,scope='internal',category_code='refund',operation_type='refund',status='categorized')
                await edit_tx(tx,finalized_text(tx),finalized_keyboard(tx),msg)
            await answer_callback(cid,'Готово'); return
        if action=='remember':
            tx=get_transaction(tx_id)
            if tx and tx.merchant and tx.scope and tx.category_code:
                save_rule(tx.merchant,tx.scope,tx.operation_type,tx.category_code)
                await answer_callback(cid,'Правило сохранено')
            else:
                await answer_callback(cid,'Нечего запоминать')
            return
        if action=='change':
            tx=reset_transaction(tx_id)
            await edit_tx(tx,header(tx)+'\n\nК чему относится операция?',initial_keyboard(tx),msg)
            await answer_callback(cid); return
        await answer_callback(cid,'Неизвестное действие')
    except Exception:
        logger.exception('Callback error %s',data)
        try: await answer_callback(cid,'Ошибка обработки')
        except Exception: pass

def parse_amount_desc(text):
    parts=text.strip().split(maxsplit=1)
    if not parts: raise ValueError
    amount=Decimal(parts[0].replace(',','.'))
    if amount<0: amount=-amount
    if amount<=0: raise ValueError
    return amount,(parts[1].strip() if len(parts)>1 else 'Ручная операция')

async def handle_state_reply(msg,uid,state):
    chat_id=msg['chat']['id']; text=(msg.get('text') or '').strip()
    if state['state']=='custom_period':
        custom=parse_custom_period(text.split())
        if not custom:
            await send_text('Не понял. Ответьте так:\n2026-07-15 2026-08-14',chat_id=chat_id,reply_markup=force_reply()); return True
        clear_user_state(uid); await show_period(chat_id,*custom); return True
    if state['state']=='opening_balance':
        try:
            amount=Decimal(text.replace(',','.'))
            if amount<0: raise ValueError
        except Exception:
            await send_text('Введите только сумму, например 3500',chat_id=chat_id,reply_markup=force_reply()); return True
        set_opening_balance(state['payload']['bucket'],amount)
        clear_user_state(uid)
        await send_text('✅ Стартовый остаток сохранён.\n\n'+await balances_text(),chat_id=chat_id,reply_markup=main_keyboard())
        return True
    if state['state']=='manual_add':
        try: amount,desc=parse_amount_desc(text)
        except Exception:
            await send_text('Не понял. Ответьте, например:\n1200 Panda',chat_id=chat_id,reply_markup=force_reply()); return True
        flow=state['payload']['flow']; clear_user_state(uid)
        tx=create_manual_transaction(amount,desc,int(uid))
        if flow=='family_expense':
            tx=update_transaction(tx.id,scope='family',operation_type='expense',direction='out')
            mid=await send_text(header(tx)+'\n\n🏠 Расход семьи\nВыберите категорию:',chat_id=chat_id,reply_markup=category_keyboard(tx,'family'))
        elif flow=='family_income':
            tx=update_transaction(tx.id,scope='family',operation_type='income',direction='in')
            mid=await send_text(header(tx)+'\n\n🏠 Доход семьи\nВыберите источник:',chat_id=chat_id,reply_markup=category_keyboard(tx,'family'))
        elif flow=='marketing_expense':
            tx=update_transaction(tx.id,scope='marketing',operation_type='expense',direction='out')
            mid=await send_text(header(tx)+'\n\n📈 Расход маркетинга\nВыберите категорию:',chat_id=chat_id,reply_markup=category_keyboard(tx,'marketing'))
        elif flow=='marketing_income':
            tx=update_transaction(tx.id,scope='marketing',operation_type='income',direction='in',source=desc)
            mid=await send_text(header(tx)+'\n\n📈 Доход маркетинга\nВыберите тип:',chat_id=chat_id,reply_markup=category_keyboard(tx,'marketing'))
        elif flow=='reserve_in':
            tx=update_transaction(tx.id,scope='marketing',category_code='reserve_contribution',operation_type='allocation',direction='out',status='categorized')
            mid=await send_text(finalized_text(tx),chat_id=chat_id,reply_markup=finalized_keyboard(tx))
        else:
            tx=update_transaction(tx.id,scope='family',category_code='reserve_to_family',operation_type='transfer',direction='in',status='categorized',source='НЗ')
            mid=await send_text(finalized_text(tx),chat_id=chat_id,reply_markup=finalized_keyboard(tx))
        set_telegram_message(tx.id,chat_id,mid)
        return True
    return False

async def show_main(chat_id):
    await send_text('💰 Family Finance\n\nВыберите действие кнопкой ниже.',chat_id=chat_id,reply_markup=main_keyboard())

async def handle_message(msg):
    uid=msg.get('from',{}).get('id')
    if not allowed(uid): return
    text=(msg.get('text') or '').strip(); chat_id=msg['chat']['id']
    state=get_user_state(uid)
    if state and not text.startswith('/'):
        if await handle_state_reply(msg,uid,state): return

    if text=='➕ Добавить':
        await send_text('Что добавляем?',chat_id=chat_id,reply_markup=manual_flow_keyboard()); return
    if text=='📊 Статистика':
        await send_text('Выберите период:',chat_id=chat_id,reply_markup=periods_keyboard()); return
    if text=='💰 Балансы':
        await send_text(await balances_text(),chat_id=chat_id,reply_markup=balances_keyboard()); return
    if text=='⏳ Разобрать': text='/pending'
    elif text=='ℹ️ Помощь': text='/help'

    if not text.startswith('/'): return
    parts=text.split(maxsplit=1); command=parts[0].split('@',1)[0].lower(); argline=parts[1] if len(parts)>1 else ''

    if command in {'/start','/menu'}:
        await show_main(chat_id); return
    if command=='/help':
        await send_text('Основные действия теперь доступны кнопками.\n\nСтартовый НЗ задавайте через «💰 Балансы».\n«🛡 Перевести в НЗ» используйте только для нового реального перевода маркетинговых денег в резерв.',chat_id=chat_id,reply_markup=main_keyboard()); return
    if command=='/balances':
        await send_text(await balances_text(),chat_id=chat_id,reply_markup=balances_keyboard()); return
    if command=='/stats':
        if argline:
            custom=parse_custom_period(argline.split())
            if not custom:
                await send_text('Формат: /stats 2026-07-15 2026-08-14',chat_id=chat_id); return
            await show_period(chat_id,*custom)
        else:
            await send_text('Выберите период:',chat_id=chat_id,reply_markup=periods_keyboard())
        return
    if command=='/pending':
        items=list_pending()
        if not items:
            await send_text('✅ Неразобранных операций нет.',chat_id=chat_id,reply_markup=main_keyboard()); return
        await send_text(f'⏳ Неразобранных операций: {len(items)}',chat_id=chat_id)
        for tx in items[:10]:
            mid=await send_text(header(tx)+'\n\nК чему относится операция?',chat_id=chat_id,reply_markup=initial_keyboard(tx))
            set_telegram_message(tx.id,chat_id,mid)
        return
    if command=='/add':
        if not argline:
            await send_text('Что добавляем?',chat_id=chat_id,reply_markup=manual_flow_keyboard()); return
        try: amount,desc=parse_amount_desc(argline)
        except Exception:
            await send_text('Например: /add 35 кофе',chat_id=chat_id); return
        tx=create_manual_transaction(amount,desc,int(uid))
        mid=await send_text(header(tx)+'\n\nК чему относится операция?',chat_id=chat_id,reply_markup=initial_keyboard(tx))
        set_telegram_message(tx.id,chat_id,mid); return

async def handle_update(update):
    if 'callback_query' in update: await handle_callback(update['callback_query'])
    elif 'message' in update: await handle_message(update['message'])
