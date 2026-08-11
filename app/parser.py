import re
from datetime import datetime
from decimal import Decimal
from app.models import ParsedSms

HEADER_RE=re.compile(
    r'^\s*Karta\s+(?P<card>\d\*+\d+)\s+(?P<date>\d{2}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2})\.\s*(?P<body>.*?)\s*Balance:\s*(?P<balance>\d+(?:[.,]\d+)?)\s*(?P<balance_currency>[A-Z]{3})',
    re.I|re.S)

OPS=[
 ('income','in',re.compile(r'^Zachislenie\s+perevoda\s+(?P<amount>\d+(?:[.,]\d+)?)\s+(?P<currency>[A-Z]{3})\.\s*(?P<description>.+?)(?:\.\s*)?$',re.I|re.S)),
 ('payment','out',re.compile(r'^Oplata\s+(?P<amount>\d+(?:[.,]\d+)?)\s+(?P<currency>[A-Z]{3})\.\s*(?P<description>.+?)(?:\.\s*)?$',re.I|re.S)),
 ('transfer','out',re.compile(r'^Perevod\s+(?P<amount>\d+(?:[.,]\d+)?)\s+(?P<currency>[A-Z]{3})\.\s*(?P<description>.+?)(?:\.\s*)?$',re.I|re.S)),
 ('cash','out',re.compile(r'^(?:Snyatie|Nalichnye|Cash)\s+(?P<amount>\d+(?:[.,]\d+)?)\s+(?P<currency>[A-Z]{3})\.\s*(?P<description>.*?)(?:\.\s*)?$',re.I|re.S)),
]

def norm(s): return re.sub(r'\s+',' ',s).strip(' .')
def dec(s): return Decimal(s.replace(',','.'))

def merchant(desc):
    v=norm(desc)
    v=re.sub(r'^(?:BLR|BY|BELARUS)\s+','',v,flags=re.I)
    v=re.sub(r'^TO\s+','',v,flags=re.I)
    return v.strip(' ".')

def parse_priorbank_sms(text):
    m=HEADER_RE.search(norm(text))
    if not m: raise ValueError('Unsupported Priorbank SMS format')
    body=norm(m.group('body'))
    op=None; hint='unknown'; direction='out'
    for h,d,p in OPS:
        mm=p.match(body)
        if mm: hint,direction,op=h,d,mm; break
    if op is None:
        op=re.search(r'(?P<amount>\d+(?:[.,]\d+)?)\s+(?P<currency>[A-Z]{3})\.\s*(?P<description>.*)',body,re.I)
        if not op: raise ValueError('Could not find amount/currency in SMS')
    desc=norm(op.groupdict().get('description') or '')
    return ParsedSms(
        card_mask=m.group('card'),
        occurred_at=datetime.strptime(f"{m.group('date')} {m.group('time')}",'%d-%m-%y %H:%M:%S'),
        amount=dec(op.group('amount')),
        currency=op.group('currency').upper(),
        direction=direction,
        operation_hint=hint,
        description=desc,
        merchant=merchant(desc) if desc else None,
        balance_after=dec(m.group('balance')) if m.group('balance') else None,
        raw_text=text)

def operation_type_from_parsed(p):
    return {'income':'income','payment':'expense','cash':'transfer','transfer':'transfer'}.get(p.operation_hint,'expense' if p.direction=='out' else 'income')
