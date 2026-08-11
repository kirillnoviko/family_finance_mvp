import csv,hmac,io,logging
from contextlib import asynccontextmanager
from fastapi import FastAPI,Header,HTTPException,Request,status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel,Field
from app.config import settings
from app.db import create_sms_transaction,export_rows,init_db
from app.parser import parse_priorbank_sms
from app.telegram import TelegramError,handle_update,send_transaction_for_classification,setup_webhook

logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
logger=logging.getLogger('family-finance')

@asynccontextmanager
async def lifespan(app):
    init_db()
    try: await setup_webhook()
    except Exception: logger.exception('Could not configure Telegram webhook')
    yield

app=FastAPI(title='Family Finance Bot',version='1.0.0-mvp',docs_url='/docs',redoc_url=None,lifespan=lifespan)

class SmsRequest(BaseModel):
    message:str=Field(min_length=1,max_length=5000)
    device:str=Field(default='wife',min_length=1,max_length=50)

def verify_bearer(auth):
    if not auth: raise HTTPException(status_code=401,detail='Missing Authorization header')
    if not auth.startswith('Bearer '): raise HTTPException(status_code=401,detail='Authorization must use Bearer scheme')
    if not hmac.compare_digest(auth[7:].strip(),settings.api_secret): raise HTTPException(status_code=403,detail='Invalid API secret')

@app.get('/health')
async def health(): return {'status':'ok','version':'1.0.0-mvp'}

@app.post('/api/sms')
async def receive_sms(request:SmsRequest,authorization:str|None=Header(default=None)):
    verify_bearer(authorization)
    try: parsed=parse_priorbank_sms(request.message)
    except ValueError as exc: raise HTTPException(status_code=422,detail=str(exc))
    tx,created=create_sms_transaction(request.device,parsed)
    if not created: return {'ok':True,'duplicate':True,'transaction_id':tx.id}
    try: await send_transaction_for_classification(tx)
    except TelegramError as exc: logger.exception('Telegram send failed'); raise HTTPException(status_code=502,detail=str(exc))
    return {'ok':True,'duplicate':False,'transaction_id':tx.id,'parsed':{'amount':str(parsed.amount),'currency':parsed.currency,'direction':parsed.direction,'operation_hint':parsed.operation_hint,'merchant':parsed.merchant,'card':parsed.card_mask,'balance_after':str(parsed.balance_after) if parsed.balance_after is not None else None}}

@app.post('/telegram/webhook')
async def telegram_webhook(request:Request,x_telegram_bot_api_secret_token:str|None=Header(default=None)):
    if settings.telegram_webhook_secret and not (x_telegram_bot_api_secret_token and hmac.compare_digest(x_telegram_bot_api_secret_token,settings.telegram_webhook_secret)):
        raise HTTPException(status_code=403,detail='Invalid Telegram webhook secret')
    await handle_update(await request.json()); return {'ok':True}

@app.get('/api/export.csv',response_class=PlainTextResponse)
async def export_csv(authorization:str|None=Header(default=None)):
    verify_bearer(authorization); rows=export_rows(); out=io.StringIO()
    if rows:
        fields=list(rows[0].keys())+['amount','balance_after']; w=csv.DictWriter(out,fieldnames=fields); w.writeheader()
        for r in rows:
            r=dict(r); r['amount']=f"{r['amount_minor']/100:.2f}"; r['balance_after']='' if r.get('balance_after_minor') is None else f"{r['balance_after_minor']/100:.2f}"; w.writerow(r)
    return PlainTextResponse(out.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="family-finance.csv"'})
