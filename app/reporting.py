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

def family_report(s,e):
    tx=query_transactions(s.isoformat(),e.isoformat())
    incomes=[t for t in tx if t.scope=='family' and t.operation_type=='income']
    reserve_in=[t for t in tx if t.category_code=='reserve_to_family']
    expenses=[t for t in tx if t.scope=='family' and t.operation_type=='expense']
    earned=sum(t.amount_minor for t in incomes)
    rz=sum(t.amount_minor for t in reserve_in)
    avail=earned+rz
    spent=sum(t.amount_minor for t in expenses)
    byinc=defaultdict(int)
    for t in incomes:
        byinc[t.category_code or 'family_other_income']+=t.amount_minor
    if rz:
        byinc['reserve_to_family']+=rz
    byexp=defaultdict(int)
    for t in expenses:
        c=BY_CODE.get(t.category_code or 'family_other_expense')
        root=c.parent if c and c.parent else (t.category_code or 'family_other_expense')
        byexp[root]+=t.amount_minor
    lines=['🏠 СЕМЬЯ',label(s,e),'',
           f'💰 Заработано семьёй: {money(earned)}',
           f'🛡 Добавлено из НЗ: {money(rz)}',
           f'💵 Всего доступно: {money(avail)}',
           f'💸 Потрачено: {money(spent)}',
           f'📌 Остаток периода: {money(avail-spent)}']
    if avail>0:
        lines.append(f'📉 Расходы / доступные деньги: {pct(spent,avail)}')
    if byinc:
        lines+=['','💰 Откуда пришли деньги:']+[f'• {title(c)} — {money(a)} ({pct(a,avail)})' for c,a in sorted(byinc.items(),key=lambda x:x[1],reverse=True)]
    if byexp:
        lines+=['','💸 Расходы:']+[f'• {title(c)} — {money(a)} ({pct(a,spent)})' for c,a in sorted(byexp.items(),key=lambda x:x[1],reverse=True)]
    return '\n'.join(lines)

def marketing_report(s,e):
    tx=query_transactions(s.isoformat(),e.isoformat())
    incomes=[t for t in tx if t.scope=='marketing' and t.operation_type=='income']
    income=sum(t.amount_minor for t in incomes)
    salary=sum(t.amount_minor for t in tx if t.category_code=='assistant_salary')
    tax=sum(t.amount_minor for t in tx if t.category_code=='tax')
    business=sum(t.amount_minor for t in tx if t.category_code=='business_expense')
    reserve=sum(t.amount_minor for t in tx if t.category_code=='reserve_contribution')
    bysrc=defaultdict(int)
    for t in incomes:
        bysrc[(t.source or t.merchant or t.description or 'Прочее').strip()]+=t.amount_minor
    lines=['📈 МАРКЕТИНГ',label(s,e),'',f'💰 Получено: {money(income)}']
    if bysrc:
        lines+=['','Откуда пришли деньги:']+[f'• {src} — {money(a)} ({pct(a,income)})' for src,a in sorted(bysrc.items(),key=lambda x:x[1],reverse=True)]
    lines+=['','Распределение:',
            f'• 👩‍💻 Зарплаты — {money(salary)} ({pct(salary,income)})',
            f'• 🏛 Налог — {money(tax)} ({pct(tax,income)})',
            f'• 📦 Расходы бизнеса — {money(business)} ({pct(business,income)})',
            f'• 🛡 В НЗ — {money(reserve)} ({pct(reserve,income)})',
            f'• 💵 Нераспределено за период — {money(income-salary-tax-business-reserve)}']
    return '\n'.join(lines)

def reserve_report(s,e):
    period=query_transactions(s.isoformat(),e.isoformat())
    pin=sum(t.amount_minor for t in period if t.category_code=='reserve_contribution')
    pout=sum(t.amount_minor for t in period if t.category_code=='reserve_to_family')
    initial=get_opening_balances()['reserve']
    def movements(items):
        return sum(t.amount_minor if t.category_code=='reserve_contribution' else -t.amount_minor for t in items)
    opening=initial+movements(all_reserve_transactions(s.isoformat()))
    closing=initial+movements(all_reserve_transactions(e.isoformat()))
    return '\n'.join(['🛡 НЕПРИКОСНОВЕННЫЙ ЗАПАС',label(s,e),'',
                      f'На начало: {money(opening)}',
                      f'+ Пополнено: {money(pin)}',
                      f'- Передано семье: {money(pout)}',
                      f'На конец: {money(closing)}'])

def current_balances():
    opening=get_opening_balances()
    family=opening['family']
    marketing=opening['marketing']
    reserve=opening['reserve']
    for t in query_all_transactions():
        if t.category_code=='reserve_contribution':
            marketing-=t.amount_minor
            reserve+=t.amount_minor
            continue
        if t.category_code=='reserve_to_family':
            reserve-=t.amount_minor
            family+=t.amount_minor
            continue
        if t.scope=='family':
            if t.operation_type=='income': family+=t.amount_minor
            elif t.operation_type=='expense': family-=t.amount_minor
        elif t.scope=='marketing':
            if t.operation_type=='income': marketing+=t.amount_minor
            elif t.operation_type=='expense': marketing-=t.amount_minor
    return {'family':family,'marketing':marketing,'reserve':reserve}
