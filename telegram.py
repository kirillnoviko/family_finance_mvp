import httpx

from family_finance_mvp.app.config import settings


class TelegramError(RuntimeError):
    pass


async def send_message(text: str) -> int:
    url = (
        f"https://api.telegram.org/"
        f"bot{settings.telegram_bot_token}/sendMessage"
    )

    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, json=payload)

    try:
        data = response.json()
    except ValueError as exc:
        raise TelegramError(
            f"Telegram returned invalid JSON. HTTP {response.status_code}"
        ) from exc

    if not response.is_success or not data.get("ok"):
        description = data.get("description", "Unknown Telegram API error")
        raise TelegramError(
            f"Telegram API error: HTTP {response.status_code}: {description}"
        )

    return int(data["result"]["message_id"])
