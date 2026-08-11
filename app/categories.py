from dataclasses import dataclass

@dataclass(frozen=True)
class Category:
    code: str
    title: str
    emoji: str
    scope: str
    kind: str
    parent: str | None = None

CATEGORIES=[
    Category('food','Еда','🍏','family','expense'),
    Category('groceries','Продукты','🛒','family','expense','food'),
    Category('cafe','Кафе / рестораны','🍽','family','expense','food'),
    Category('delivery','Доставка еды','🥡','family','expense','food'),
    Category('home','Дом','🏠','family','expense'),
    Category('rent','Аренда / жильё','🏢','family','expense','home'),
    Category('utilities','Коммунальные','💡','family','expense','home'),
    Category('household','Быт / товары для дома','🧽','family','expense','home'),
    Category('repair','Ремонт','🛠','family','expense','home'),
    Category('transport','Транспорт','🚗','family','expense'),
    Category('taxi','Такси','🚕','family','expense','transport'),
    Category('fuel','Топливо','⛽','family','expense','transport'),
    Category('public_transport','Общественный транспорт','🚌','family','expense','transport'),
    Category('parking','Парковка','🅿️','family','expense','transport'),
    Category('car_service','Авто / обслуживание','🔧','family','expense','transport'),
    Category('children','Дети','👶','family','expense'),
    Category('kindergarten','Сад / школа','🎒','family','expense','children'),
    Category('child_education','Занятия / образование','📚','family','expense','children'),
    Category('child_clothes','Одежда ребёнку','🧥','family','expense','children'),
    Category('child_toys','Игрушки','🧸','family','expense','children'),
    Category('child_fun','Развлечения ребёнка','🎠','family','expense','children'),
    Category('health','Здоровье','💊','family','expense'),
    Category('pharmacy','Аптека','💊','family','expense','health'),
    Category('doctors','Врачи','🩺','family','expense','health'),
    Category('tests','Анализы','🧪','family','expense','health'),
    Category('obligatory','Обязательные платежи','📌','family','expense'),
    Category('credit','Кредиты','🏦','family','expense','obligatory'),
    Category('internet_phone','Интернет / связь','📱','family','expense','obligatory'),
    Category('insurance','Страхование','🛡','family','expense','obligatory'),
    Category('shopping','Покупки','🛍','family','expense'),
    Category('clothes','Одежда','👕','family','expense','shopping'),
    Category('electronics','Электроника','💻','family','expense','shopping'),
    Category('other_shopping','Другие покупки','📦','family','expense','shopping'),
    Category('leisure','Досуг','🎉','family','expense'),
    Category('entertainment','Развлечения','🎬','family','expense','leisure'),
    Category('travel','Путешествия','✈️','family','expense','leisure'),
    Category('hobby','Хобби','🎨','family','expense','leisure'),
    Category('subscriptions','Подписки','🔁','family','expense'),
    Category('family_other_expense','Прочее','•••','family','expense'),
    Category('salary_kirill','Зарплата Кирилла','👨','family','income'),
    Category('salary_wife','Зарплата жены','👩','family','income'),
    Category('family_other_income','Другой доход','💰','family','income'),
    Category('marketing_client_income','Доход от клиента','💼','marketing','income'),
    Category('marketing_other_income','Другой доход','💰','marketing','income'),
    Category('assistant_salary','Зарплаты помощницам','👩‍💻','marketing','expense'),
    Category('tax','Налог','🏛','marketing','expense'),
    Category('business_expense','Прочие расходы бизнеса','📦','marketing','expense'),
    Category('reserve_contribution','В НЗ','🛡','marketing','allocation'),
    Category('reserve_to_family','Из НЗ в семью','🏠','marketing','transfer'),
    Category('own_transfer','Перевод между своими счетами','🔄','internal','transfer'),
    Category('refund','Возврат','↩️','internal','refund'),
]
BY_CODE={c.code:c for c in CATEGORIES}

def roots(scope,kind):
    return [c for c in CATEGORIES if c.scope==scope and c.kind==kind and c.parent is None]

def children(parent_code):
    return [c for c in CATEGORIES if c.parent==parent_code]

def title(code):
    if not code: return '—'
    c=BY_CODE.get(code)
    return code if not c else f'{c.emoji} {c.title}'
