import hmac
import logging

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.config import settings
from app.telegram import TelegramError, send_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("family-finance")

app = FastAPI(
    title="Family Finance Bot",
    version="0.1.0",
    docs_url="/docs",
    redoc_url=None,
)


class SmsRequest(BaseModel):
    message: str = Field(min_length=1, max_length=3500)
    device: str = Field(default="wife", min_length=1, max_length=50)


class SmsResponse(BaseModel):
    ok: bool
    telegram_message_id: int


def verify_authorization(authorization: str | None) -> None:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )

    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization must use Bearer scheme",
        )

    supplied_secret = authorization[len(prefix):].strip()

    if not hmac.compare_digest(supplied_secret, settings.api_secret):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API secret",
        )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/sms", response_model=SmsResponse)
async def receive_sms(
    request: SmsRequest,
    authorization: str | None = Header(default=None),
) -> SmsResponse:
    verify_authorization(authorization)

    logger.info(
        "Received SMS event from device=%s, message_length=%d",
        request.device,
        len(request.message),
    )

    telegram_text = (
        "💳 Новая операция\n\n"
        f"{request.message}\n\n"
        f"📱 Источник: {request.device}"
    )

    try:
        telegram_message_id = await send_message(telegram_text)
    except TelegramError:
        logger.exception("Failed to send message to Telegram")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to send message to Telegram",
        )

    logger.info(
        "SMS forwarded to Telegram, telegram_message_id=%d",
        telegram_message_id,
    )

    return SmsResponse(
        ok=True,
        telegram_message_id=telegram_message_id,
    )
