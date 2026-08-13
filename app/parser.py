import re
from datetime import datetime, date
from decimal import Decimal

from app.models import ParsedSms


class IgnoredSms(ValueError):
    """A Priorbank message that is valid, but is not a financial transaction."""


CARD_HEADER_RE = re.compile(
    r'^\s*Karta\s+(?P<card>\d\*+\d+)\s+'
    r'(?P<date>\d{2}-\d{2}-\d{2})\s+'
    r'(?P<time>\d{2}:\d{2}:\d{2})\.\s*'
    r'(?P<body>.*?)\s*'
    r'Balance:\s*(?P<balance>\d+(?:[.,]\d+)?)\s*'
    r'(?P<balance_currency>[A-Z]{3})',
    re.I | re.S,
)

DIRECT_INCOME_RE = re.compile(
    r'^\s*(?P<date>\d{2})/(?P<month>\d{2})\s+'
    r'(?P<time>\d{2}:\d{2})\.\s*'
    r'Na\s+vashu\s+kartu\s+zachisleno\s+'
    r'(?P<amount>\d+(?:[.,]\d+)?)\s+(?P<currency>[A-Z]{3})\.\s*'
    r'Dostupnaja\s+summa:\s*(?P<balance>\d+(?:[.,]\d+)?)\s*'
    r'(?P<balance_currency>[A-Z]{3})',
    re.I | re.S,
)

OTP_RE = re.compile(
    r'^\s*Priorbank\s+\d{2}/\d{2}\s+\d{2}:\d{2}\.\s*Code:',
    re.I | re.S,
)

OP_PATTERNS = [
    (
        'income',
        'in',
        re.compile(
            r'^Zachislenie\s+perevoda\s+'
            r'(?P<amount>\d+(?:[.,]\d+)?)\s+(?P<currency>[A-Z]{3})\.\s*'
            r'(?P<description>.+?)(?:\.\s*)?$',
            re.I | re.S,
        ),
    ),
    (
        'payment',
        'out',
        re.compile(
            r'^Oplata\s+'
            r'(?P<amount>\d+(?:[.,]\d+)?)\s+(?P<currency>[A-Z]{3})\.\s*'
            r'(?P<description>.+?)(?:\.\s*)?$',
            re.I | re.S,
        ),
    ),
    (
        'transfer',
        'out',
        re.compile(
            r'^Perevod\s+'
            r'(?P<amount>\d+(?:[.,]\d+)?)\s+(?P<currency>[A-Z]{3})\.\s*'
            r'(?P<description>.+?)(?:\.\s*)?$',
            re.I | re.S,
        ),
    ),
    (
        'cash',
        'out',
        re.compile(
            r'^(?:Nalichnye(?:\s+v\s+bankomate)?|Snyatie(?:\s+nalichnyh)?|Cash(?:\s+withdrawal)?)\s+'
            r'(?P<amount>\d+(?:[.,]\d+)?)\s+(?P<currency>[A-Z]{3})\.\s*'
            r'(?P<description>.*?)(?:\.\s*)?$',
            re.I | re.S,
        ),
    ),
]


def norm(value: str) -> str:
    return re.sub(r'\s+', ' ', value).strip(' .')


def dec(value: str) -> Decimal:
    return Decimal(value.replace(',', '.'))


def merchant(description: str) -> str:
    value = norm(description)
    # Priorbank prepends an ISO-like country code to card operation descriptions:
    # BLR, IRL, etc. Remove it from the merchant shown in Telegram.
    value = re.sub(r'^[A-Z]{3}\s+', '', value)
    value = re.sub(r'^TO\s+', '', value, flags=re.I)
    value = value.strip(' .')
    # Remove quotes only when they wrap the whole merchant name. Quotes inside
    # strings such as MAGAZIN "SANTA-353" must stay intact.
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1].strip()
    return value


def _infer_year(day: int, month: int, today: date | None = None) -> int:
    """Infer year for messages that contain only DD/MM.

    Pick the current year except around New Year, where a future-looking date more
    than ~45 days away is treated as belonging to the previous year.
    """
    today = today or date.today()
    candidate = date(today.year, month, day)
    if (candidate - today).days > 45:
        return today.year - 1
    return today.year


def _parse_card_message(text: str) -> ParsedSms | None:
    normalized = norm(text)
    match = CARD_HEADER_RE.search(normalized)
    if not match:
        return None

    body = norm(match.group('body'))
    op_match = None
    hint = 'unknown'
    direction = 'out'

    for candidate_hint, candidate_direction, pattern in OP_PATTERNS:
        candidate = pattern.match(body)
        if candidate:
            hint = candidate_hint
            direction = candidate_direction
            op_match = candidate
            break

    if op_match is None:
        # Keep a conservative fallback for new transaction wording from the bank.
        fallback = re.search(
            r'(?P<amount>\d+(?:[.,]\d+)?)\s+(?P<currency>[A-Z]{3})\.\s*'
            r'(?P<description>.*)',
            body,
            re.I | re.S,
        )
        if not fallback:
            raise ValueError('Could not find amount/currency in Priorbank SMS')
        op_match = fallback

    description = norm(op_match.groupdict().get('description') or '')
    return ParsedSms(
        card_mask=match.group('card'),
        occurred_at=datetime.strptime(
            f"{match.group('date')} {match.group('time')}",
            '%d-%m-%y %H:%M:%S',
        ),
        amount=dec(op_match.group('amount')),
        currency=op_match.group('currency').upper(),
        direction=direction,
        operation_hint=hint,
        description=description,
        merchant=merchant(description) if description else None,
        balance_after=dec(match.group('balance')),
        raw_text=text,
    )


def _parse_direct_income(text: str) -> ParsedSms | None:
    normalized = norm(text)
    match = DIRECT_INCOME_RE.search(normalized)
    if not match:
        return None

    day = int(match.group('date'))
    month = int(match.group('month'))
    year = _infer_year(day, month)
    occurred_at = datetime.strptime(
        f"{day:02d}-{month:02d}-{year} {match.group('time')}",
        '%d-%m-%Y %H:%M',
    )
    return ParsedSms(
        card_mask='priorbank-card',
        occurred_at=occurred_at,
        amount=dec(match.group('amount')),
        currency=match.group('currency').upper(),
        direction='in',
        operation_hint='income',
        description='Зачисление на карту',
        merchant='Зачисление на карту',
        balance_after=dec(match.group('balance')),
        raw_text=text,
    )


def parse_priorbank_sms(text: str) -> ParsedSms:
    normalized = norm(text)
    if OTP_RE.search(normalized):
        raise IgnoredSms('Priorbank OTP/code message')

    parsed = _parse_card_message(text)
    if parsed is not None:
        return parsed

    parsed = _parse_direct_income(text)
    if parsed is not None:
        return parsed

    raise ValueError('Unsupported Priorbank SMS format')


def operation_type_from_parsed(parsed: ParsedSms) -> str:
    return {
        'income': 'income',
        'payment': 'expense',
        'cash': 'cash_withdrawal',
        'transfer': 'transfer',
    }.get(
        parsed.operation_hint,
        'expense' if parsed.direction == 'out' else 'income',
    )
