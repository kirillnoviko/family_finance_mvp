import logging
from decimal import Decimal, InvalidOperation
import httpx
from app.categories import BY_CODE,children,roots,title
from app.config import settings
from app.db import create_manual_transaction,find_rule,get_transaction,list_pending,reset_transaction,save_rule,set_telegram_message,update_transaction
from app.reporting import family_report,financial_period,marketing_report,parse_custom_period,reserve_report

logger=logging.getLogger(__name__)
class TelegramError(RuntimeError): pass

def b(text,data): return {'text':text,'callback_data':data}
def markup(rows): return {'inline_keyboard':rows}

async def api_call(method,payload):
    url=f'https://api.telegram.org/bot{settings.telegram_bot_token}/{method}'
    async with httpx.AsyncClient(timeout=15) as client: r=await client.post(url,json=payload)
    try: data=r.json()
    except ValueError as exc: raise TelegramError(f'Telegram invalid JSON HTTP {r.status_code}') from exc
    if not r.is_success or not data.get('ok'): raise TelegramError(f"{method}: {data.get('description',r.text)}")
    return data['result']

async def send_text(text,chat_id=None,reply_markup=None):
    p={'chat_id':chat_id or settings.telegram_chat_id,'text':text,'disable_web_page_preview':True}
    if reply_markup is not None: p['reply_markup']=reply_markup
    result=await api_call('sendMessage',p); return int(result['message_id'])

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
    await api_call('setWebhook',{'url':f'{settings.app_base_url}/telegram/webhook','secret_token':settings.telegram_webhook_secret,'allowed_updates':['message','callback_query'],'drop_pending_updates':False})
    logger.info('Telegram webhook configured')

def allowed(user_id): return bool(user_id) and (not settings.allowed_telegram_user_ids or user_id in settings.allowed_telegram_user_ids)

def amount_text(tx,signed=False):
    sign=('+' if tx.direction=='in' else '-') if signed else ''
    return f'{sign}{tx.amount:.2f} {tx.currency}'

def header(tx):
    emoji='💰' if tx.direction=='in' else '💸'; details=tx.merchant or tx.description or 'Операция'
    lines=[f'{emoji} {amount_text(tx,True)}',details,'',f"📅 {tx.occurred_at.replace('T',' ')}",f'💳 {tx.physical_account}']
    if tx.balance_after is not None: lines.append(f'Баланс после: {tx.balance_after:.2f} {tx.currency}')
    return '\n'.join(lines)

def initial_keyboard(tx):
    if tx.origin=='manual':
        return markup([
            [b('🏠 Расход семьи',f'flow:{tx.id}:family_expense'),b('📈 Расход маркетинга',f'flow:{tx.id}:marketing_expense')],
            [b('🏠 Доход семьи',f'flow:{tx.id}:family_income'),b('📈 Доход маркетинга',f'flow:{tx.id}:marketing_income')],
            [b('🛡 В НЗ',f'flow:{tx.id}:reserve_in'),b('🏠 Из НЗ семье',f'flow:{tx.id}:reserve_out')],
            [b('🔄 Свои деньги',f'special:{tx.id}:own_transfer'),b('🚫 Игнорировать',f'special:{tx.id}:ignore')]
        ])
    if tx.direction=='in':
        return markup([[b('🏠 Семья',f'scope:{tx.id}:family'),b('📈 Маркетинг',f'scope:{tx.id}:marketing')],[b('🔄 Свои деньги',f'special:{tx.id}:own_transfer'),b('🚫 Игнорировать',f'special:{tx.id}:ignore')]])
    return markup([[b('🏠 Семья',f'scope:{tx.id}:family'),b('📈 Маркетинг',f'scope:{tx.id}:marketing')],[b('🔄 Перевод',f'special:{tx.id}:own_transfer'),b('↩️ Возврат',f'special:{tx.id}:refund')],[b('🚫 Игнорировать',f'special:{tx.id}:ignore')]])

def category_keyboard(tx,scope):
    if tx.direction=='in':
        cats=[BY_CODE['salary_kirill'],BY_CODE['salary_wife'],BY_CODE['family_other_income'],BY_CODE['reserve_to_family']] if scope=='family' else [BY_CODE['marketing_client_income'],BY_CODE['marketing_other_income']]
    else:
        cats=[BY_CODE['assistant_salary'],BY_CODE['tax'],BY_CODE['business_expense'],BY_CODE['reserve_contribution']] if scope=='marketing' else roots('family','expense')
    rows=[]
    for i in range(0,len(cats),2): rows.append([b(f'{c.emoji} {c.title}',f'cat:{tx.id}:{c.code}') for c in cats[i:i+2]])
    rows.append([b('⬅️ Назад',f'change:{tx.id}')]); return markup(rows)

def subcategory_keyboard(tx,parent):
    cats=children(parent); rows=[]
    for i in range(0,len(cats),2): rows.append([b(f'{c.emoji} {c.title}',f'cat:{tx.id}:{c.code}') for c in cats[i:i+2]])
    rows.append([b(f"✅ Оставить «{BY_CODE[parent].title}»",f'final:{tx.id}:{parent}')]); rows.append([b('⬅️ Назад',f'scope:{tx.id}:family')]); return markup(rows)

def finalized_text(tx):
    scope='🏠 Семья' if tx.scope=='family' else '📈 Маркетинг' if tx.scope=='marketing' else '🔄 Внутренний'
    lines=['✅ Операция учтена','',header(tx),'',f'Контур: {scope}',f'Категория: {title(tx.category_code)}']
    if tx.source and not tx.source.startswith('telegram:'): lines.append(f'Источник: {tx.source}')
    return '\n'.join(lines)

def finalized_keyboard(tx):
    rows=[[b('✏️ Изменить',f'change:{tx.id}')]]
    if tx.merchant and tx.scope in {'family','marketing'} and tx.operation_type=='expense' and tx.category_code: rows.insert(0,[b('🧠 Запомнить магазин',f'remember:{tx.id}')])
    return markup(rows)

async def send_transaction_for_classification(tx):
    rule=find_rule(tx.merchant,tx.operation_type)
    if rule:
        code=rule['category_code']; cat=BY_CODE.get(code); op=tx.operation_type; direction=tx.direction
        if cat:
            if cat.kind=='expense': op='expense'; direction='out'
            elif cat.kind=='income': op='income'; direction='in'
            elif cat.kind=='allocation': op='allocation'; direction='out'
            elif cat.kind=='transfer': op='transfer'
        tx=update_transaction(tx.id,scope=rule['scope'],category_code=code,operation_type=op,direction=direction,status='categorized')
        mid=await send_text(finalized_text(tx)+'\n\n🧠 Категория применена по сохранённому правилу.',reply_markup=finalized_keyboard(tx)); set_telegram_message(tx.id,settings.telegram_chat_id,mid); return
    mid=await send_text(header(tx)+'\n\nК чему относится операция?',reply_markup=initial_keyboard(tx)); set_telegram_message(tx.id,settings.telegram_chat_id,mid)

async def edit_tx(tx,text,keyb,callback_message=None):
    if callback_message: chat_id=callback_message['chat']['id']; mid=callback_message['message_id']
    elif tx.telegram_chat_id and tx.telegram_message_id: chat_id=tx.telegram_chat_id; mid=tx.telegram_message_id
    else: return
    await edit_text(chat_id,mid,text,keyb)

async def finalize(tx,code,message=None):
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
    await edit_tx(tx,finalized_text(tx),finalized_keyboard(tx),message)

async def handle_callback(cb):
    cid=cb['id']; uid=cb.get('from',{}).get('id'); data=cb.get('data',''); msg=cb.get('message')
    if not allowed(uid): await answer_callback(cid,'Нет доступа'); return
    try:
        p=data.split(':'); action=p[0]
        if action=='report':
            start,end=financial_period(); name=p[1]; text=family_report(start,end) if name=='family' else marketing_report(start,end) if name=='marketing' else reserve_report(start,end)
            await send_text(text,chat_id=msg['chat']['id'] if msg else None); await answer_callback(cid); return
        tx_id=int(p[1]) if len(p)>1 else None
        if action=='flow':
            flow=p[2]; tx=get_transaction(tx_id)
            if not tx: await answer_callback(cid,'Операция не найдена'); return
            if flow=='family_expense':
                tx=update_transaction(tx.id,scope='family',operation_type='expense',direction='out'); await edit_tx(tx,header(tx)+'\n\n🏠 Расход семьи\nВыберите категорию:',category_keyboard(tx,'family'),msg)
            elif flow=='marketing_expense':
                tx=update_transaction(tx.id,scope='marketing',operation_type='expense',direction='out'); await edit_tx(tx,header(tx)+'\n\n📈 Расход маркетинга\nВыберите категорию:',category_keyboard(tx,'marketing'),msg)
            elif flow=='family_income':
                tx=update_transaction(tx.id,scope='family',operation_type='income',direction='in'); await edit_tx(tx,header(tx)+'\n\n🏠 Доход семьи\nВыберите источник:',category_keyboard(tx,'family'),msg)
            elif flow=='marketing_income':
                tx=update_transaction(tx.id,scope='marketing',operation_type='income',direction='in'); await edit_tx(tx,header(tx)+'\n\n📈 Доход маркетинга\nВыберите тип:',category_keyboard(tx,'marketing'),msg)
            elif flow=='reserve_in':
                tx=update_transaction(tx.id,scope='marketing',operation_type='allocation',direction='out'); await finalize(tx,'reserve_contribution',msg)
            elif flow=='reserve_out':
                tx=update_transaction(tx.id,scope='family',operation_type='transfer',direction='in'); await finalize(tx,'reserve_to_family',msg)
            await answer_callback(cid,'Готово'); return
        if action=='scope':
            scope=p[2]; tx=update_transaction(tx_id,scope=scope); await edit_tx(tx,header(tx)+f"\n\nКонтур: {'🏠 Семья' if scope=='family' else '📈 Маркетинг'}\nВыберите категорию:",category_keyboard(tx,scope),msg); await answer_callback(cid); return
        if action=='cat':
            code=p[2]; tx=get_transaction(tx_id)
            if not tx: await answer_callback(cid,'Операция не найдена'); return
            if children(code): await edit_tx(tx,header(tx)+f'\n\n{title(code)}\nВыберите подкатегорию:',subcategory_keyboard(tx,code),msg)
            else: await finalize(tx,code,msg)
            await answer_callback(cid,'Сохранено' if not children(code) else None); return
        if action=='final':
            tx=get_transaction(tx_id)
            if tx: await finalize(tx,p[2],msg)
            await answer_callback(cid,'Сохранено'); return
        if action=='special':
            tx=get_transaction(tx_id)
            if not tx: await answer_callback(cid,'Операция не найдена'); return
            special=p[2]
            if special=='ignore':
                tx=update_transaction(tx.id,status='ignored'); await edit_tx(tx,'🚫 Операция не учитывается\n\n'+header(tx),markup([[b('↩️ Вернуть',f'change:{tx.id}')]]),msg)
            elif special=='own_transfer':
                tx=update_transaction(tx.id,scope='internal',category_code='own_transfer',operation_type='transfer',status='categorized'); await edit_tx(tx,finalized_text(tx),finalized_keyboard(tx),msg)
            elif special=='refund':
                tx=update_transaction(tx.id,scope='internal',category_code='refund',operation_type='refund',status='categorized'); await edit_tx(tx,finalized_text(tx),finalized_keyboard(tx),msg)
            await answer_callback(cid,'Готово'); return
        if action=='remember':
            tx=get_transaction(tx_id)
            if tx and tx.merchant and tx.scope and tx.category_code: save_rule(tx.merchant,tx.scope,tx.operation_type,tx.category_code); await answer_callback(cid,'Правило сохранено')
            else: await answer_callback(cid,'Нечего запоминать')
            return
        if action=='change':
            tx=reset_transaction(tx_id); await edit_tx(tx,header(tx)+'\n\nК чему относится операция?',initial_keyboard(tx),msg); await answer_callback(cid); return
        await answer_callback(cid,'Неизвестное действие')
    except Exception:
        logger.exception('Callback error %s',data)
        try: await answer_callback(cid,'Ошибка обработки')
        except Exception: pass

async def handle_message(msg):
    uid=msg.get('from',{}).get('id')
    if not allowed(uid): return
    text=(msg.get('text') or '').strip()
    if not text.startswith('/'): return
    chat_id=msg['chat']['id']; parts=text.split(maxsplit=1); command=parts[0].split('@',1)[0].lower(); argline=parts[1] if len(parts)>1 else ''
    if command in {'/start','/help'}:
        await send_text(f'💰 Family Finance\n\n/stats — статистика текущего периода\n/stats YYYY-MM-DD YYYY-MM-DD — свой период\n/add 35 описание — ручная операция / наличные\n/pending — неразобранные операции\n/help — помощь\n\nФинансовый период начинается {settings.period_start_day}-го числа.',chat_id=chat_id); return
    if command=='/stats':
        args=argline.split(); custom=parse_custom_period(args) if args else None
        if args and custom is None: await send_text('Формат: /stats 2026-07-15 2026-08-14',chat_id=chat_id); return
        start,end=custom or financial_period()
        if custom:
            await send_text(family_report(start,end),chat_id=chat_id); await send_text(marketing_report(start,end),chat_id=chat_id); await send_text(reserve_report(start,end),chat_id=chat_id)
        else:
            kb=markup([[b('🏠 Семья','report:family'),b('📈 Маркетинг','report:marketing')],[b('🛡 НЗ','report:reserve')]])
            await send_text(family_report(start,end)+'\n\nОткройте другие отчёты:',chat_id=chat_id,reply_markup=kb)
        return
    if command=='/pending':
        items=list_pending()
        if not items: await send_text('✅ Неразобранных операций нет.',chat_id=chat_id); return
        await send_text(f'⏳ Неразобранных операций: {len(items)}',chat_id=chat_id)
        for tx in items[:10]:
            mid=await send_text(header(tx)+'\n\nК чему относится операция?',chat_id=chat_id,reply_markup=initial_keyboard(tx)); set_telegram_message(tx.id,chat_id,mid)
        return
    if command=='/add':
        if not argline: await send_text('Использование:\n/add 35 продукты\n/add 1200 Panda\n\nПосле команды выберите назначение кнопками.',chat_id=chat_id); return
        ap=argline.split(maxsplit=1); token=ap[0]; desc=ap[1] if len(ap)>1 else 'Ручная операция'
        try:
            amount=Decimal(token.replace(',','.'))
            if amount<=0: raise InvalidOperation
        except (InvalidOperation,ValueError): await send_text('Не понял сумму. Например: /add 35 кофе',chat_id=chat_id); return
        tx=create_manual_transaction(amount,desc,int(uid)); mid=await send_text(header(tx)+'\n\nК чему относится операция?',chat_id=chat_id,reply_markup=initial_keyboard(tx)); set_telegram_message(tx.id,chat_id,mid); return

async def handle_update(update):
    if 'callback_query' in update: await handle_callback(update['callback_query'])
    elif 'message' in update: await handle_message(update['message'])
