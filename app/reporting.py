from collections import defaultdict
from datetime import date,datetime,time,timedelta
from decimal import Decimal
from app.categories import BY_CODE,title
from app.config import settings
from app.db import all_reserve_transactions,get_opening_balances,query_all_transactions,query_transactions

def money(minor,currency='BYN'):
    return f"{Decimal(minor)/100:,.2f} {currency}".replace(',',' ')

def pct(part,total):
    return '0.0%' if total<=0 else f'{part/total*100:.1f}%'

def financial_period(today=None):
    today=today or date.today()
    day=settings.period_start_day
    if today.day>=day:
        start=date(today.year,today.month,day)
    else:
        prev=today.replace(day=1)-timedelta(days=1)
        start=date(prev.year,prev.month,day)
    nxt=(start.replace(day=28)+timedelta(days=4)).replace(day=1)
    end=date(nxt.year,nxt.month,day)
    return datetime.combine(start,time.min),datetime.combine(end,time.min)

def previous_financial_period(today=None):
    cur_start,_=financial_period(today)
    return financial_period(cur_start.date()-timedelta(days=1))

def current_calendar_month(today=None):
    today=today or date.today()
    start=date(today.year,today.month,1)
    nxt=(start.replace(day=28)+timedelta(days=4)).replace(day=1)
    return datetime.combine(start,time.min),datetime.combine(nxt,time.min)

def previous_calendar_month(today=None):
    today=today or date.today()
    cur=date(today.year,today.month,1)
    prev=cur-timedelta(days=1)
    start=date(prev.year,prev.month,1)
    return datetime.combine(start,time.min),datetime.combine(cur,time.min)

def last_days(days=30,today=None):
    today=today or date.today()
    end=datetime.combine(today+timedelta(days=1),time.min)
    return end-timedelta(days=days),end

def parse_custom_period(args):
    if len(args)!=2:
        return None
    try:
        s=date.fromisoformat(args[0]); e=date.fromisoformat(args[1])
    except ValueError:
        return None
    if e<s:
        return None
    return datetime.combine(s,time.min),datetime.combine(e+timedelta(days=1),time.min)

def label(s,e):
    return f'{s:%d.%m.%Y} — {(e-timedelta(days=1)):%d.%m.%Y}'

def balances_at(end_iso=None):
    opening=get_opening_balances()
    family=opening['family']
    marketing=opening['marketing']
    reserve=opening['reserve']

    for t in query_all_transactions(end_iso):
        amount=t.report_amount_minor
        if amount==0 and t.currency!='BYN' and t.amount_byn_minor is None:
            continue

        # Transfers/allocation involving the reserve are balance movements,
        # not income/expense.
        if t.category_code=='reserve_contribution':
            marketing-=amount
            reserve+=amount
            continue
        if t.category_code=='reserve_from_family':
            family-=amount
            reserve+=amount
            continue
        if t.category_code=='reserve_to_family':
            reserve-=amount
            family+=amount
            continue

        if t.scope=='family':
            if t.operation_type=='income':
                family+=amount
            elif t.operation_type=='expense':
                family-=amount
        elif t.scope=='marketing':
            if t.operation_type=='income':
                marketing+=amount
            elif t.operation_type=='expense':
                marketing-=amount

    return {'family':family,'marketing':marketing,'reserve':reserve}


def family_report(s,e):
    tx=query_transactions(s.isoformat(),e.isoformat())
    missing_fx=[t for t in tx if t.scope=='family' and t.currency!='BYN' and t.amount_byn_minor is None]

    opening=balances_at(s.isoformat())['family']
    closing=balances_at(e.isoformat())['family']

    incomes=[t for t in tx if t.scope=='family' and t.operation_type=='income']
    expenses=[t for t in tx if t.scope=='family' and t.operation_type=='expense']
    reserve_in=[t for t in tx if t.category_code=='reserve_to_family']
    reserve_out=[t for t in tx if t.category_code=='reserve_from_family']

    earned=sum(t.report_amount_minor for t in incomes)
    rz_in=sum(t.report_amount_minor for t in reserve_in)
    rz_out=sum(t.report_amount_minor for t in reserve_out)
    spent=sum(t.report_amount_minor for t in expenses)
    change=closing-opening

    byinc=defaultdict(int)
    for t in incomes:
        byinc[t.category_code or 'family_other_income']+=t.report_amount_minor
    if rz_in:
        byinc['reserve_to_family']+=rz_in

    byexp=defaultdict(int)
    for t in expenses:
        c=BY_CODE.get(t.category_code or 'family_other_expense')
        root=c.parent if c and c.parent else (t.category_code or 'family_other_expense')
        byexp[root]+=t.report_amount_minor

    inflow=earned+rz_in
    usable=opening+inflow

    lines=[
        '🏠 СЕМЬЯ',label(s,e),'',
        f'💵 Баланс на начало: {money(opening)}',
        f'➕ Доходы семьи: {money(earned)}',
        f'🛡 Из НЗ в семью: {money(rz_in)}',
        f'💸 Расходы: {money(spent)}',
        f'🛡 В НЗ из семьи: {money(rz_out)}',
        f'📈 Изменение за период: {money(change)}',
        f'💰 Баланс на конец: {money(closing)}',
    ]

    if usable>0:
        lines.append(f'📉 Расходы / доступные деньги: {pct(spent,usable)}')

    if byinc:
        denom=inflow if inflow else 1
        lines+=['','💰 Откуда пришли деньги:']+[
            f'• {title(c)} — {money(a)} ({pct(a,denom)})'
            for c,a in sorted(byinc.items(),key=lambda x:x[1],reverse=True)
        ]

    if byexp:
        lines+=['','💸 Расходы:']+[
            f'• {title(c)} — {money(a)} ({pct(a,spent)})'
            for c,a in sorted(byexp.items(),key=lambda x:x[1],reverse=True)
        ]

    if missing_fx:
        lines+=['',f'⚠️ Для {len(missing_fx)} валютных операций пока не удалось получить исторический курс НБРБ; они временно не входят в BYN-итоги.']
    return '\n'.join(lines)


def marketing_report(s,e):
    tx=query_transactions(s.isoformat(),e.isoformat())
    missing_fx=[t for t in tx if t.scope=='marketing' and t.currency!='BYN' and t.amount_byn_minor is None]

    opening=balances_at(s.isoformat())['marketing']
    closing=balances_at(e.isoformat())['marketing']

    incomes=[t for t in tx if t.scope=='marketing' and t.operation_type=='income']
    income=sum(t.report_amount_minor for t in incomes)
    salary=sum(t.report_amount_minor for t in tx if t.category_code=='assistant_salary')
    tax=sum(t.report_amount_minor for t in tx if t.category_code=='tax')
    business=sum(t.report_amount_minor for t in tx if t.category_code=='business_expense')
    reserve=sum(t.report_amount_minor for t in tx if t.category_code=='reserve_contribution')
    expenses=salary+tax+business
    change=closing-opening

    bysrc=defaultdict(int)
    for t in incomes:
        bysrc[(t.source or t.merchant or t.description or 'Прочее').strip()]+=t.report_amount_minor

    lines=[
        '📈 МАРКЕТИНГ',label(s,e),'',
        f'💵 Баланс на начало: {money(opening)}',
        f'➕ Получено: {money(income)}',
        f'💸 Расходы бизнеса: {money(expenses)}',
        f'🛡 В НЗ из маркетинга: {money(reserve)}',
        f'📈 Изменение за период: {money(change)}',
        f'💰 Баланс на конец: {money(closing)}',
    ]

    if bysrc:
        lines+=['','Откуда пришли деньги:']+[
            f'• {src} — {money(a)} ({pct(a,income)})'
            for src,a in sorted(bysrc.items(),key=lambda x:x[1],reverse=True)
        ]

    lines+=['','Расходы / распределение:',
            f'• 👩‍💻 Зарплаты — {money(salary)} ({pct(salary,income)})',
            f'• 🏛 Налог — {money(tax)} ({pct(tax,income)})',
            f'• 📦 Прочие расходы бизнеса — {money(business)} ({pct(business,income)})',
            f'• 🛡 В НЗ — {money(reserve)} ({pct(reserve,income)})']

    if missing_fx:
        lines+=['',f'⚠️ Для {len(missing_fx)} валютных операций пока не удалось получить исторический курс НБРБ; они временно не входят в BYN-итоги.']
    return '\n'.join(lines)


def reserve_report(s,e):
    period=query_transactions(s.isoformat(),e.isoformat())

    from_marketing=sum(
        t.report_amount_minor for t in period
        if t.category_code=='reserve_contribution'
    )
    from_family=sum(
        t.report_amount_minor for t in period
        if t.category_code=='reserve_from_family'
    )
    to_family=sum(
        t.report_amount_minor for t in period
        if t.category_code=='reserve_to_family'
    )

    opening=balances_at(s.isoformat())['reserve']
    closing=balances_at(e.isoformat())['reserve']
    change=closing-opening

    return '\n'.join([
        '🛡 НЕПРИКОСНОВЕННЫЙ ЗАПАС',label(s,e),'',
        f'💵 Баланс на начало: {money(opening)}',
        f'📈 Из маркетинга в НЗ: {money(from_marketing)}',
        f'🏠 Из семьи в НЗ: {money(from_family)}',
        f'➖ Из НЗ в семью: {money(to_family)}',
        f'📈 Изменение за период: {money(change)}',
        f'💰 Баланс на конец: {money(closing)}',
    ])


def current_balances():
    return balances_at()
