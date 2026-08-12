import asyncio
import time
from decimal import Decimal
import httpx

NBRB_URL = "https://api.nbrb.by/exrates/rates/{code}?parammode=2"

_cache = {}
_cache_time = 0.0
_lock = asyncio.Lock()
CACHE_TTL_SECONDS = 6 * 60 * 60

async def _fetch_one(code):
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(NBRB_URL.format(code=code))
        r.raise_for_status()
        data = r.json()
    rate = Decimal(str(data["Cur_OfficialRate"]))
    scale = Decimal(str(data["Cur_Scale"]))
    return rate / scale

async def get_byn_rates():
    global _cache_time, _cache
    now = time.time()
    if _cache and now - _cache_time < CACHE_TTL_SECONDS:
        return dict(_cache)
    async with _lock:
        now = time.time()
        if _cache and now - _cache_time < CACHE_TTL_SECONDS:
            return dict(_cache)
        try:
            usd, eur = await asyncio.gather(_fetch_one("USD"), _fetch_one("EUR"))
            _cache = {"USD": usd, "EUR": eur}
            _cache_time = time.time()
        except Exception:
            if not _cache:
                return {}
        return dict(_cache)

def convert_byn(amount_byn, rates):
    out = {}
    for code in ("USD", "EUR"):
        rate = rates.get(code)
        if rate and rate > 0:
            out[code] = amount_byn / rate
    return out
