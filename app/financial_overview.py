from decimal import Decimal


def money(minor: int) -> str:
    return f"{Decimal(minor)/100:.2f}"


def build_financial_overview(balances, debt):
    family = balances.get("family", 0)
    marketing = balances.get("marketing", 0)
    reserve = balances.get("reserve", 0)
    debt_minor = debt.get("balance_minor", 0)

    total_assets = family + marketing + reserve

    return "\n".join([
        "📊 ФИНАНСОВАЯ КАРТИНА",
        "",
        f"🏠 Семья: {money(family)} BYN",
        f"📈 Маркетинг: {money(marketing)} BYN",
        f"🛡 НЗ: {money(reserve)} BYN",
        "",
        f"💰 Всего денег: {money(total_assets)} BYN",
        "",
        f"💳 Долг: {money(debt_minor)} EUR",
        "",
        "Чистый капитал = деньги - обязательства"
    ])