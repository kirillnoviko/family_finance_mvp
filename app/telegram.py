import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
import httpx

from app.categories import BY_CODE,children,roots,title
from app.config import settings
from app.db import (
    clear_user_state,create_manual_transaction,create_sms_transaction,find_rule,get_transaction,get_user_state,
    list_pending,loan_payment_details,loan_summary,query_transactions,reset_transaction,save_rule,set_opening_balance,set_telegram_message,
    set_user_state,update_transaction
)
from app.exchange import convert_byn,convert_foreign_to_byn,get_byn_rates
from app.parser import IgnoredSms,parse_priorbank_sms
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
            [{'text':'📋 Операции'},{'text':'💰 Балансы'}],
            [{'text':'🏦 Кредит'},{'text':'⏳ Разобрать'}],
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
        {'command':'operations','description':'Список операций'},
        {'command':'balances','description':'Балансы'},
        {'command':'loan','description':'Кредит на квартиру'},
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
    lines=[f'{emoji} {amount_text(tx,True)}']
    if tx.currency!='BYN' and tx.amount_byn is not None:
        lines.append(f'≈ {tx.amount_byn:.2f} BYN по курсу НБРБ на {tx.fx_rate_date or tx.occurred_at[:10]}')
    elif tx.currency!='BYN':
        lines.append('⚠️ BYN-эквивалент пока не рассчитан')
    lines += [details,'',f"📅 {tx.occurred_at.replace('T',' ')}",f'💳 {tx.physical_account}']
    if tx.balance_after is not None:
        lines.append(f"Баланс после: {tx.balance_after:.2f} {'BYN' if tx.origin=='sms' else tx.currency}")
    return '\n'.join(lines)

async def create_bank_transaction(device,parsed):
    amount_byn=None
    fx_rate=None
    fx_date=parsed.occurred_at.date()
    if parsed.currency=='BYN':
        amount_byn=parsed.amount
        fx_rate=Decimal('1')
    else:
        try:
            amount_byn,fx_rate=await convert_foreign_to_byn(
                parsed.amount,
                parsed.currency,
                fx_date,
            )
        except Exception:
            logger.exception(
                'Could not get NBRB rate for %s on %s',
                parsed.currency,
                fx_date,
            )
    return create_sms_transaction(
        device,
        parsed,
        amount_byn=amount_byn,
        fx_rate=fx_rate,
        fx_rate_date=fx_date if amount_byn is not None else None,
    )
def category_keyboard(tx,scope):
    if tx.direction=='in':
        cats=[BY_CODE['salary_kirill'],BY_CODE['salary_wife'],BY_CODE['family_other_income'],BY_CODE['reserve_to_family']] if scope=='family' else [BY_CODE['marketing_client_income'],BY_CODE['marketing_other_income']]
    else:
        cats=[BY_CODE['assistant_salary'],BY_CODE['tax'],BY_CODE['business_expense'],BY_CODE['mortgage_payment'],BY_CODE['reserve_contribution']] if scope=='marketing' else roots('family','expense')+[BY_CODE['reserve_from_family']]
    rows=[]
    for i in range(0,len(cats),2):
        rows.append([b(f'{c.emoji} {c.title}',f'cat:{tx.id}:{c.code}') for c in cats[i:i+2]])
    rows.append([b('⬅️ Назад',f'change:{tx.id}')])
    return markup(rows)

def subcategory_keyboard(tx,parent):
    cats=children(parent); rows=[]
    for i in range(0,len(cats),2):
        rows.append([b(f'{c.emoji} {c.title}',f'cat:{tx.id}:{c.code}') for c in cats[i:i+2]])
    rows.append([b('⬅️ Назад',f'scope:{tx.id}:family')])
    return markup(rows)

def initial_keyboard(tx):
    if tx.operation_type=='cash_withdrawal':
        return markup([
            [b('🏠 Расход семьи',f'cash:{tx.id}:family_expense'),b('📈 Расход маркетинга',f'cash:{tx.id}:marketing_expense')],
            [b('👩‍💻 Зарплата помощнице',f'cash:{tx.id}:assistant_salary'),b('💵 Оставил наличными',f'cash:{tx.id}:keep_cash')],
            [b('🔄 Свои деньги',f'special:{tx.id}:own_transfer'),b('🚫 Игнорировать',f'special:{tx.id}:ignore')]
        ])
    if tx.direction=='in':
        return markup([[b('🏠 Семья',f'scope:{tx.id}:family'),b('📈 Маркетинг',f'scope:{tx.id}:marketing')],
                       [b('🔄 Свои деньги',f'special:{tx.id}:own_transfer'),b('🚫 Игнорировать',f'special:{tx.id}:ignore')]])
    return markup([[b('🏠 Семья',f'scope:{tx.id}:family'),b('📈 Маркетинг',f'scope:{tx.id}:marketing')],
                   [b('🔄 Перевод',f'special:{tx.id}:own_transfer'),b('↩️ Возврат',f'special:{tx.id}:refund')],
                   [b('🚫 Игнорировать',f'special:{tx.id}:ignore')]])

def finalized_text(tx):
    scope='🏠 Семья' if tx.scope=='family' else '📈 Маркетинг' if tx.scope=='marketing' else '🔄 Внутренний'
    lines=['✅ Операция учтена','',header(tx),'',f'Контур: {scope}',f'Категория: {title(tx.category_code)}']
    if tx.category_code=='reserve_contribution':
        lines.append('Перемещение: 📈 Маркетинг → 🛡 НЗ')
    elif tx.category_code=='reserve_from_family':
        lines.append('Перемещение: 🏠 Семья → 🛡 НЗ')
    elif tx.category_code=='reserve_to_family':
        lines.append('Перемещение: 🛡 НЗ → 🏠 Семья')
    elif tx.category_code=='mortgage_payment':
        loan=loan_payment_details(tx.id)
        if loan:
            lines += [
                '',
                '🏦 Кредит на квартиру:',
                f"• Платёж: {Decimal(loan['payment_minor'])/100:.2f} BYN",
                f"• Проценты: {Decimal(loan['interest_paid_minor'])/100:.2f} BYN",
                f"• В тело: {Decimal(loan['principal_paid_minor'])/100:.2f} BYN",
                f"• Остаток тела: {Decimal(loan['balance_after_minor'])/100:.2f} BYN",
            ]
            if loan['excess_minor']:
                lines.append(f"• Переплата сверх тела: {Decimal(loan['excess_minor'])/100:.2f} BYN")
        else:
            summary=loan_summary()
            lines += [
                '',
                f"⚠️ Платёж не уменьшает стартовый долг, потому что дата операции раньше {summary['start_date']}.",
            ]
    if tx.source and not tx.source.startswith(('telegram:','system:')): lines.append(f'Источник: {tx.source}')
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
        [b('🏠 Семья → кредит','manual:loan_family'),b('📈 Маркетинг → кредит','manual:loan_marketing')],
        [b('🏠 Семья → НЗ','manual:reserve_from_family'),b('📈 Маркетинг → НЗ','manual:reserve_from_marketing')],
        [b('🛡 НЗ → Семья','manual:reserve_out')]
    ])


def operations_scope_keyboard():
    return markup([
        [b('🏠 Семья','opscope:family'),b('📈 Маркетинг','opscope:marketing')],
        [b('🛡 НЗ','opscope:reserve')]
    ])

def operations_periods_keyboard(scope):
    return markup([
        [b('📅 Текущий 15→14',f'opperiod:{scope}:current'),b('◀️ Прошлый 15→14',f'opperiod:{scope}:previous')],
        [b('🗓 Этот месяц',f'opperiod:{scope}:month'),b('◀️ Прошлый месяц',f'opperiod:{scope}:prevmonth')],
        [b('30 дней',f'opperiod:{scope}:30'),b('✏️ Свой период',f'opperiod:{scope}:custom')]
    ])

def _operations_for_scope(scope,start,end):
    items=query_transactions(start.isoformat(),end.isoformat())
    if scope=='reserve':
        items=[t for t in items if t.category_code in {'reserve_contribution','reserve_from_family','reserve_to_family'}]
    else:
        items=[t for t in items if t.scope==scope]
    return list(reversed(items))

def _operation_name(tx,scope):
    if scope=='reserve':
        if tx.category_code=='reserve_contribution':
            return 'Маркетинг → НЗ'
        if tx.category_code=='reserve_from_family':
            return 'Семья → НЗ'
        if tx.category_code=='reserve_to_family':
            return 'НЗ → Семья'
    name=(tx.merchant or tx.description or tx.source or '').strip()
    if name and not name.startswith(('telegram:','system:')):
        return name
    return title(tx.category_code) if tx.category_code else 'Операция'

def _operation_amount(tx,scope):
    if scope=='reserve':
        sign='+' if tx.category_code in {'reserve_contribution','reserve_from_family'} else '-'
    else:
        sign='+' if tx.direction=='in' else '-'

    if tx.currency!='BYN':
        original=f'{sign}{tx.amount:.2f} {tx.currency}'
        if tx.amount_byn is not None:
            return f'{original} (≈ {tx.amount_byn:.2f} BYN)'
        return original

    amount=tx.amount_byn if tx.amount_byn is not None else tx.amount
    return f'{sign}{amount:.2f} BYN'

def operations_page_text(scope,start,end,page=0,page_size=15):
    labels={'family':'🏠 СЕМЬЯ','marketing':'📈 МАРКЕТИНГ','reserve':'🛡 НЗ'}
    items=_operations_for_scope(scope,start,end)
    pages=max(1,(len(items)+page_size-1)//page_size)
    page=max(0,min(page,pages-1))
    chunk=items[page*page_size:(page+1)*page_size]
    period_end=(end.date() - __import__('datetime').timedelta(days=1))
    lines=[
        f'📋 ОПЕРАЦИИ — {labels[scope]}',
        f'{start:%d.%m.%Y} — {period_end:%d.%m.%Y}',
        ''
    ]
    if not chunk:
        lines.append('За этот период операций нет.')
    else:
        for tx in chunk:
            dt=datetime.fromisoformat(tx.occurred_at)
            lines.append(f'{dt:%d.%m %H:%M} | {_operation_amount(tx,scope)} | {_operation_name(tx,scope)}')
        lines += ['',f'Операций: {len(items)} · страница {page+1}/{pages}']
    return '\n'.join(lines),len(items),page,pages

def operations_page_keyboard(scope,start,end,page,pages):
    s=start.strftime('%Y%m%d'); e=end.strftime('%Y%m%d')
    nav=[]
    if page>0:
        nav.append(b('⬅️',f'oplist:{scope}:{s}:{e}:{page-1}'))
    if page+1<pages:
        nav.append(b('➡️',f'oplist:{scope}:{s}:{e}:{page+1}'))
    rows=[]
    if nav:
        rows.append(nav)
    rows += [
        [
            b('🏠 Семья',f'oplist:family:{s}:{e}:0'),
            b('📈 Маркетинг',f'oplist:marketing:{s}:{e}:0'),
            b('🛡 НЗ',f'oplist:reserve:{s}:{e}:0')
        ],
        [b('📅 Другой период',f'opchooseperiod:{scope}'),b('🔙 Направление','opshome:x')]
    ]
    return markup(rows)

async def show_operations_page(chat_id,scope,start,end,page=0):
    text,_,page,pages=operations_page_text(scope,start,end,page)
    await send_text(
        text,
        chat_id=chat_id,
        reply_markup=operations_page_keyboard(scope,start,end,page,pages)
    )


def loan_keyboard():
    return markup([
        [b('🏠 Семья → кредит','loanpay:family'),b('📈 Маркетинг → кредит','loanpay:marketing')]
    ])

def loan_text():
    info=loan_summary()
    initial=Decimal(info['initial_principal_minor'])/100
    balance=Decimal(info['balance_minor'])/100
    paid=Decimal(info['total_payment_minor'])/100
    interest=Decimal(info['total_interest_minor'])/100
    principal=Decimal(info['total_principal_minor'])/100
    estimated=Decimal(info['estimated_month_interest_minor'])/100
    lines=[
        '🏦 КРЕДИТ НА КВАРТИРУ','',
        f"Старт учёта: {info['start_date']}",
        f"Начальный долг: {initial:.2f} BYN",
        f"Ставка: {info['annual_rate']}% годовых",
        '',
        f"💳 Внесено после старта: {paid:.2f} BYN",
        f"💸 Из них проценты: {interest:.2f} BYN",
        f"📉 В тело кредита: {principal:.2f} BYN",
        f"🏦 Остаток тела: {balance:.2f} BYN",
        '',
        f"Ориентир процентов на первый платёж нового месяца: {estimated:.2f} BYN",
        f"Расчёт: остаток тела × {info['annual_rate']}% / 12.",
    ]
    if info['entries']:
        last=info['entries'][-1]
        if last['interest_remaining_minor']>0:
            lines.append(
                f"⚠️ В месяце {last['month']} осталось закрыть процентов: "
                f"{Decimal(last['interest_remaining_minor'])/100:.2f} BYN"
            )
        else:
            lines.append(f"✅ Проценты месяца {last['month']} уже закрыты; следующий платёж этого месяца идёт в тело.")
    return '\n'.join(lines)

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
    loan=loan_summary()
    return '\n'.join([
        '💰 БАЛАНСЫ','',
        line('🏠 Семья',balances['family']),
        line('📈 Маркетинг',balances['marketing'],True),
        line('🛡 НЗ',balances['reserve'],True),
        f"🏦 Долг по квартире: {Decimal(loan['balance_minor'])/100:.2f} BYN",
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
    prompt='Что сделали со снятыми наличными?' if tx.operation_type=='cash_withdrawal' else 'К чему относится операция?'
    mid=await send_text(header(tx)+'\n\n'+prompt,reply_markup=initial_keyboard(tx))
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
        if action=='loanpay':
            scope=p[1]
            flow='loan_family' if scope=='family' else 'loan_marketing'
            set_user_state(uid,'manual_add',{'flow':flow})
            await send_text('Введите сумму платежа по кредиту.\nНапример:\n1200 кредит',chat_id=chat_id,reply_markup=force_reply())
            await answer_callback(cid); return
        if action=='opshome':
            await send_text('Какое направление показать?',chat_id=chat_id,reply_markup=operations_scope_keyboard())
            await answer_callback(cid); return
        if action=='opscope':
            scope=p[1]
            await send_text('Выберите период операций:',chat_id=chat_id,reply_markup=operations_periods_keyboard(scope))
            await answer_callback(cid); return
        if action=='opchooseperiod':
            scope=p[1]
            await send_text('Выберите период операций:',chat_id=chat_id,reply_markup=operations_periods_keyboard(scope))
            await answer_callback(cid); return
        if action=='opperiod':
            scope=p[1]; choice=p[2]
            if choice=='current': start,end=financial_period()
            elif choice=='previous': start,end=previous_financial_period()
            elif choice=='month': start,end=current_calendar_month()
            elif choice=='prevmonth': start,end=previous_calendar_month()
            elif choice=='30': start,end=last_days(30)
            else:
                set_user_state(uid,'operations_custom_period',{'scope':scope})
                await send_text('Ответьте двумя датами:\n2026-07-15 2026-08-14',chat_id=chat_id,reply_markup=force_reply())
                await answer_callback(cid); return
            await show_operations_page(chat_id,scope,start,end,0)
            await answer_callback(cid); return
        if action=='oplist':
            scope=p[1]; start,end=decode_dates(p[2],p[3]); page=int(p[4])
            await show_operations_page(chat_id,scope,start,end,page)
            await answer_callback(cid); return
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
        if action=='cash':
            tx=get_transaction(tx_id)
            if not tx:
                await answer_callback(cid,'Операция не найдена'); return
            choice=p[2]
            if choice=='family_expense':
                tx=update_transaction(tx.id,scope='family',operation_type='expense',direction='out')
                await edit_tx(tx,header(tx)+'\n\n🏠 Расход семьи из снятых наличных\nВыберите категорию:',category_keyboard(tx,'family'),msg)
            elif choice=='marketing_expense':
                tx=update_transaction(tx.id,scope='marketing',operation_type='expense',direction='out')
                await edit_tx(tx,header(tx)+'\n\n📈 Расход маркетинга из снятых наличных\nВыберите категорию:',category_keyboard(tx,'marketing'),msg)
            elif choice=='assistant_salary':
                tx=update_transaction(tx.id,scope='marketing',operation_type='expense',direction='out')
                await finalize(tx,'assistant_salary',msg)
            elif choice=='keep_cash':
                tx=update_transaction(tx.id,scope='internal',category_code='own_transfer',operation_type='transfer',status='categorized')
                await edit_tx(tx,finalized_text(tx)+'\n\n💵 Снятие отмечено как перемещение денег в наличные. Сам расход добавьте позже, когда эти наличные будут потрачены.',finalized_keyboard(tx),msg)
            await answer_callback(cid,'Готово'); return
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
    if state['state']=='operations_custom_period':
        custom=parse_custom_period(text.split())
        if not custom:
            await send_text('Не понял. Ответьте так:\n2026-07-15 2026-08-14',chat_id=chat_id,reply_markup=force_reply()); return True
        scope=state['payload']['scope']
        clear_user_state(uid)
        await show_operations_page(chat_id,scope,*custom,0)
        return True
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
        elif flow=='loan_family':
            tx=update_transaction(
                tx.id,scope='family',category_code='mortgage_payment',
                operation_type='expense',direction='out',status='categorized',
                source='Семья'
            )
            mid=await send_text(finalized_text(tx),chat_id=chat_id,reply_markup=finalized_keyboard(tx))
        elif flow=='loan_marketing':
            tx=update_transaction(
                tx.id,scope='marketing',category_code='mortgage_payment',
                operation_type='expense',direction='out',status='categorized',
                source='Маркетинг'
            )
            mid=await send_text(finalized_text(tx),chat_id=chat_id,reply_markup=finalized_keyboard(tx))
        elif flow=='reserve_from_marketing':
            tx=update_transaction(
                tx.id,scope='marketing',category_code='reserve_contribution',
                operation_type='allocation',direction='out',status='categorized',
                source='Маркетинг'
            )
            mid=await send_text(finalized_text(tx),chat_id=chat_id,reply_markup=finalized_keyboard(tx))
        elif flow=='reserve_from_family':
            tx=update_transaction(
                tx.id,scope='family',category_code='reserve_from_family',
                operation_type='allocation',direction='out',status='categorized',
                source='Семья'
            )
            mid=await send_text(finalized_text(tx),chat_id=chat_id,reply_markup=finalized_keyboard(tx))
        elif flow=='reserve_out':
            tx=update_transaction(
                tx.id,scope='family',category_code='reserve_to_family',
                operation_type='transfer',direction='in',status='categorized',source='НЗ'
            )
            mid=await send_text(finalized_text(tx),chat_id=chat_id,reply_markup=finalized_keyboard(tx))
        else:
            await send_text('Неизвестный тип операции.',chat_id=chat_id)
            return True
        set_telegram_message(tx.id,chat_id,mid)
        return True
    return False

async def show_main(chat_id):
    await send_text('💰 Family Finance\n\nВыберите действие кнопкой ниже.',chat_id=chat_id,reply_markup=main_keyboard())

async def handle_message(msg):
    uid=msg.get('from',{}).get('id')
    if not allowed(uid): return
    text=(msg.get('text') or '').strip(); chat_id=msg['chat']['id']

    # Recovery/import path: paste an old Priorbank SMS directly into the group.
    # Use the same device key as the iPhone automation so deduplication remains
    # consistent if the same SMS later arrives from Shortcuts.
    looks_like_bank = text.lower().startswith('karta ') or (
        len(text) >= 16 and text[:2].isdigit() and '/' in text[:6] and 'na vashu kartu' in text.lower()
    ) or text.lower().startswith('priorbank ')
    if looks_like_bank:
        try:
            parsed=parse_priorbank_sms(text)
        except IgnoredSms:
            await send_text('ℹ️ Это служебное сообщение Priorbank, финансовой операции в нём нет.',chat_id=chat_id)
            return
        except ValueError:
            await send_text('⚠️ Не смог разобрать это сообщение Priorbank. Пришлите его целиком, без редактирования.',chat_id=chat_id)
            return
        tx,created=await create_bank_transaction('wife',parsed)
        if not created and tx.status!='pending':
            await send_text('ℹ️ Эта операция уже есть в базе и уже обработана.',chat_id=chat_id)
            return
        if not created and tx.telegram_message_id:
            await send_text('ℹ️ Эта операция уже есть в очереди «Разобрать».',chat_id=chat_id)
            return
        await send_transaction_for_classification(tx)
        return

    state=get_user_state(uid)
    if state and not text.startswith('/'):
        if await handle_state_reply(msg,uid,state): return

    if text=='➕ Добавить':
        await send_text('Что добавляем?',chat_id=chat_id,reply_markup=manual_flow_keyboard()); return
    if text=='📊 Статистика':
        await send_text('Выберите период:',chat_id=chat_id,reply_markup=periods_keyboard()); return
    if text=='📋 Операции':
        await send_text('Какое направление показать?',chat_id=chat_id,reply_markup=operations_scope_keyboard()); return
    if text=='💰 Балансы':
        await send_text(await balances_text(),chat_id=chat_id,reply_markup=balances_keyboard()); return
    if text=='🏦 Кредит':
        await send_text(loan_text(),chat_id=chat_id,reply_markup=loan_keyboard()); return
    if text=='⏳ Разобрать': text='/pending'
    elif text=='ℹ️ Помощь': text='/help'

    if not text.startswith('/'): return
    parts=text.split(maxsplit=1); command=parts[0].split('@',1)[0].lower(); argline=parts[1] if len(parts)>1 else ''

    if command in {'/start','/menu'}:
        await show_main(chat_id); return
    if command=='/help':
        await send_text('Основные действия теперь доступны кнопками.\n\n«🏦 Кредит» показывает остаток тела, проценты и позволяет внести платёж из семьи или маркетинга.\n\n«📋 Операции» показывает журнал по семье, маркетингу или НЗ за выбранный период.\n\nСтартовый НЗ задавайте через «💰 Балансы».\nДля пополнения НЗ выбирайте отдельно «🏠 Семья → НЗ» или «📈 Маркетинг → НЗ». Это перемещение между контурами, а не расход.',chat_id=chat_id,reply_markup=main_keyboard()); return
    if command=='/balances':
        await send_text(await balances_text(),chat_id=chat_id,reply_markup=balances_keyboard()); return
    if command=='/loan':
        await send_text(loan_text(),chat_id=chat_id,reply_markup=loan_keyboard()); return
    if command in {'/operations','/ops'}:
        await send_text('Какое направление показать?',chat_id=chat_id,reply_markup=operations_scope_keyboard()); return
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
            prompt='Что сделали со снятыми наличными?' if tx.operation_type=='cash_withdrawal' else 'К чему относится операция?'
            mid=await send_text(header(tx)+'\n\n'+prompt,chat_id=chat_id,reply_markup=initial_keyboard(tx))
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
