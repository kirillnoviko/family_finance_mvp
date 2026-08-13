import asyncio
import time
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

import httpx

# NBRB official exchange-rate API.
NBRB_URL = "https://api.nbrb.by/exrates/rates/{code}"

_current_cache = {}
_current_cache_time = 0.0
_historical_cache = {}
_lock = asyncio.Lock()
CACHE_TTL_SECONDS = 6 * 60 * 60


def _normalize_rate(data):
    rate = Decimal(str(data["Cur_OfficialRate"]))
    scale = Decimal(str(data["Cur_Scale"]))
    return rate / scale


async def _fetch_rate(code, on_date=None):
    params = {"parammode": 2}
    if on_date is not None:
        params["ondate"] = on_date.isoformat()
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            NBRB_URL.format(code=code.upper()),
            params=params,
        )
        response.raise_for_status()
        return _normalize_rate(response.json())


async def get_byn_rate(code, on_date):
    code = code.upper()
    if code == "BYN":
        return Decimal("1")

    if not isinstance(on_date, date):
        raise TypeError("on_date must be datetime.date")

    key = (code, on_date.isoformat())
    if key in _historical_cache:
        return _historical_cache[key]

    async with _lock:
        if key in _historical_cache:
            return _historical_cache[key]
        rate = await _fetch_rate(code, on_date)
        _historical_cache[key] = rate
        return rate


async def convert_foreign_to_byn(amount, currency, on_date):
    currency = currency.upper()
    if currency == "BYN":
        return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), Decimal("1")

    rate = await get_byn_rate(currency, on_date)
    converted = (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return converted, rate


async def get_byn_rates():
    # Used only to display today's approximate USD/EUR equivalents
    # for current BYN balances.
    global _current_cache_time, _current_cache
    now = time.time()
    if _current_cache and now - _current_cache_time < CACHE_TTL_SECONDS:
        return dict(_current_cache)

    async with _lock:
        now = time.time()
        if _current_cache and now - _current_cache_time < CACHE_TTL_SECONDS:
            return dict(_current_cache)
        try:
            usd, eur = await asyncio.gather(
                _fetch_rate("USD"),
                _fetch_rate("EUR"),
            )
            _current_cache = {"USD": usd, "EUR": eur}
            _current_cache_time = time.time()
        except Exception:
            if not _current_cache:
                return {}
        return dict(_current_cache)


def convert_byn(amount_byn, rates):
    out = {}
    for code in ("USD", "EUR"):
        rate = rates.get(code)
        if rate and rate > 0:
            out[code] = amount_byn / rate
    return out
