import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str
    api_secret: str

    @classmethod
    def from_env(cls) -> "Settings":
        missing: list[str] = []

        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        api_secret = os.getenv("API_SECRET", "").strip()

        if not telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not telegram_chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        if not api_secret:
            missing.append("API_SECRET")

        if missing:
            raise RuntimeError(
                "Missing required environment variables: " + ", ".join(missing)
            )

        return cls(
            telegram_bot_token=telegram_bot_token,
            telegram_chat_id=telegram_chat_id,
            api_secret=api_secret,
        )


settings = Settings.from_env()
