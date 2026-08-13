from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

@dataclass(slots=True)
class ParsedSms:
    card_mask: str
    occurred_at: datetime
    amount: Decimal
    currency: str
    direction: str
    operation_hint: str
    description: str
    merchant: str | None
    balance_after: Decimal | None
    raw_text: str

@dataclass(slots=True)
class Transaction:
    id: int
    occurred_at: str
    amount_minor: int
    currency: str
    direction: str
    operation_type: str
    physical_account: str
    scope: str | None
    category_code: str | None
    source: str | None
    merchant: str | None
    description: str | None
    balance_after_minor: int | None
    origin: str
    status: str
    telegram_chat_id: int | None
    telegram_message_id: int | None
    amount_byn_minor: int | None = None
    fx_rate: str | None = None
    fx_rate_date: str | None = None

    @property
    def amount(self):
        return Decimal(self.amount_minor) / Decimal(100)

    @property
    def amount_byn(self):
        if self.amount_byn_minor is None:
            return None
        return Decimal(self.amount_byn_minor) / Decimal(100)

    @property
    def report_amount_minor(self):
        # Reports are denominated in BYN.
        if self.amount_byn_minor is not None:
            return self.amount_byn_minor
        if self.currency == "BYN":
            return self.amount_minor
        return 0

    @property
    def balance_after(self):
        return None if self.balance_after_minor is None else Decimal(self.balance_after_minor) / Decimal(100)
