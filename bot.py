"""
Демо-бот "Авто зі США" — Telegram-бот для компаній, що займаються
перегоном авто зі США в Україну.

Функціонал (демо-версія, для показу клієнту на питчі):
  1. 🧮 Калькулятор розмитнення — розрахунок мита + акцизу + ПДВ
  2. 🚗 Підбір авто — коротка анкета + приклади лотів (mock-дані)
  3. 📝 Заявка — збір контакту клієнта (лід падає в leads.csv)
  4. 📞 Контакти — хто з команди за що відповідає
  5. ℹ️ Як це працює — короткий шлях "від аукціону до видачі"

Формули розмитнення взяті з відкритих джерел на 2026 рік (мито 10%,
акциз за об'ємом двигуна і роком авто, ПДВ 20%). Це орієнтовний
розрахунок для демонстрації механіки бота — перед використанням із
реальними клієнтами ставки варто звірити з чинним митним
законодавством або консультантом з розмитнення.

Запуск:
    1) pip install -r requirements.txt
    2) скопіювати .env.example у .env і вписати токен від @BotFather
    3) python bot.py
"""

import asyncio
import csv
import logging
import os
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # опційно: куди слати нові заявки
LEADS_FILE = os.path.join(os.path.dirname(__file__), "leads.csv")

logging.basicConfig(level=logging.INFO)
router = Router()

# ---------------------------------------------------------------------------
# Дані, які легко перетюнити під конкретного клієнта (бренд, контакти, лоти)
# ---------------------------------------------------------------------------

COMPANY_NAME = "AutoFromUSA Demo"

CONTACTS = [
    ("🇺🇸 Викуп на аукціоні (Copart/IAAI)", "@demo_us_buyer"),
    ("🚢 Логістика та морське перевезення", "@demo_logistics"),
    ("📋 Розмитнення", "@demo_customs_broker"),
    ("🛠 Загальна підтримка", "@demo_support"),
]

# mock-лоти для демонстрації "підбору" — у бойовій версії замінюється
# на вибірку з реальної бази/парсера Copart, IAAI, auction API тощо
MOCK_CARS = [
    {"title": "Toyota Camry 2019", "body": "sedan", "price": 9800,
     "mileage": "62 000 миль", "fuel": "Бензин", "volume": 2500, "note": "Clean title, легкі пошкодження бампера"},
    {"title": "Honda CR-V 2020", "body": "suv", "price": 14200,
     "mileage": "48 000 миль", "fuel": "Бензин", "volume": 1500, "note": "Salvage title, ДТП зліва спереду"},
    {"title": "Ford F-150 2018", "body": "pickup", "price": 17500,
     "mileage": "71 000 миль", "fuel": "Бензин", "volume": 3500, "note": "Clean title, без пошкоджень"},
    {"title": "Volkswagen Golf 2017", "body": "hatchback", "price": 6900,
     "mileage": "89 000 миль", "fuel": "Дизель", "volume": 1600, "note": "Clean title"},
    {"title": "Tesla Model 3 2021", "body": "sedan", "price": 19500,
     "mileage": "35 000 миль", "fuel": "Електро", "volume": 0, "note": "Battery 75 kWh, Clean title"},
    {"title": "Chrysler Pacifica 2019", "body": "minivan", "price": 12300,
     "mileage": "58 000 миль", "fuel": "Бензин", "volume": 3600, "note": "Clean title, 7 місць"},
]

BODY_LABELS = {
    "sedan": "Седан", "suv": "Позашляховик/SUV", "hatchback": "Хетчбек",
    "minivan": "Мінівен", "pickup": "Пікап",
}

# ---------------------------------------------------------------------------
# FSM-стани
# ---------------------------------------------------------------------------

class CalcStates(StatesGroup):
    fuel = State()
    volume = State()
    battery = State()
    age = State()
    price = State()


class SelectStates(StatesGroup):
    budget = State()
    body = State()


class LeadStates(StatesGroup):
    name = State()
    phone = State()


# ---------------------------------------------------------------------------
# Головне меню
# ---------------------------------------------------------------------------

def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🧮 Розрахувати розмитнення", callback_data="menu:calc")
    kb.button(text="🚗 Підібрати авто", callback_data="menu:select")
    kb.button(text="📝 Залишити заявку", callback_data="menu:lead")
    kb.button(text="📞 Контакти", callback_data="menu:contacts")
    kb.button(text="ℹ️ Як це працює", callback_data="menu:howto")
    kb.adjust(1)
    return kb.as_markup()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        f"👋 Вітаю у <b>{COMPANY_NAME}</b>!\n\n"
        "Допоможу порахувати вартість розмитнення авто зі США та підібрати "
        "варіант під ваш бюджет. Оберіть, що потрібно:",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "menu:main")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        f"👋 <b>{COMPANY_NAME}</b>. Оберіть, що потрібно:",
        reply_markup=main_menu_kb(),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# 1. Калькулятор розмитнення
# ---------------------------------------------------------------------------

def fuel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="⛽ Бензин", callback_data="fuel:petrol")
    kb.button(text="🛢 Дизель", callback_data="fuel:diesel")
    kb.button(text="🔋 Електро", callback_data="fuel:electric")
    kb.button(text="🔄 Гібрид", callback_data="fuel:hybrid")
    kb.button(text="⬅️ Назад", callback_data="menu:main")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


@router.callback_query(F.data == "menu:calc")
async def calc_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CalcStates.fuel)
    await callback.message.edit_text(
        "🧮 <b>Калькулятор розмитнення</b>\n\nОберіть тип палива авто:",
        reply_markup=fuel_kb(),
    )
    await callback.answer()


@router.callback_query(CalcStates.fuel, F.data.startswith("fuel:"))
async def calc_fuel_chosen(callback: CallbackQuery, state: FSMContext):
    fuel = callback.data.split(":")[1]
    await state.update_data(fuel=fuel)
    if fuel == "electric":
        await state.set_state(CalcStates.battery)
        await callback.message.edit_text(
            "🔋 Вкажіть ємність батареї в кВт·год (наприклад: 75)"
        )
    else:
        await state.set_state(CalcStates.volume)
        await callback.message.edit_text(
            "🔧 Вкажіть об'єм двигуна в см³ (наприклад: 2000)"
        )
    await callback.answer()


@router.message(CalcStates.battery)
async def calc_battery_entered(message: Message, state: FSMContext):
    try:
        battery = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Введіть число, наприклад: 75")
        return
    await state.update_data(battery=battery, volume=0)
    await state.set_state(CalcStates.age)
    await message.answer("📅 Скільки повних років авто? (наприклад: 5)")


@router.message(CalcStates.volume)
async def calc_volume_entered(message: Message, state: FSMContext):
    try:
        volume = float(message.text.replace(",", "."))
        if volume < 100:  # ввели в літрах, а не см3
            volume *= 1000
    except ValueError:
        await message.answer("Введіть число у см³, наприклад: 2000 (або 2.0 для літрів)")
        return
    await state.update_data(volume=volume)
    await state.set_state(CalcStates.age)
    await message.answer("📅 Скільки повних років авто? (наприклад: 5)")


@router.message(CalcStates.age)
async def calc_age_entered(message: Message, state: FSMContext):
    try:
        age = int(message.text)
    except ValueError:
        await message.answer("Введіть ціле число років, наприклад: 5")
        return
    await state.update_data(age=age)
    await state.set_state(CalcStates.price)
    await message.answer("💵 Вкажіть митну вартість авто в EUR (наприклад: 8000)")


@router.message(CalcStates.price)
async def calc_price_entered(message: Message, state: FSMContext):
    try:
        value = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Введіть число в EUR, наприклад: 8000")
        return

    data = await state.get_data()
    fuel = data["fuel"]
    age = data["age"]
    volume = data.get("volume", 0)
    battery = data.get("battery", 0)

    duty = value * 0.10

    if fuel == "electric":
        excise = battery * 1.0  # €1 за кВт·год ємності батареї
    else:
        if fuel == "diesel":
            base_rate = 150 if volume > 3500 else 75
        else:  # petrol / hybrid (спрощено як бензин)
            base_rate = 100 if volume > 3000 else 50
        excise = base_rate * (volume / 1000) * age

    vat = 0.20 * (value + duty + excise)
    total = duty + excise + vat

    fuel_label = {"petrol": "Бензин", "diesel": "Дизель",
                  "electric": "Електро", "hybrid": "Гібрид"}[fuel]

    text = (
        "🧮 <b>Розрахунок розмитнення (орієнтовний)</b>\n\n"
        f"Паливо: {fuel_label}\n"
        f"{'Батарея: ' + str(battery) + ' кВт·год' if fuel == 'electric' else 'Об’єм двигуна: ' + str(int(volume)) + ' см³'}\n"
        f"Вік авто: {age} р.\n"
        f"Митна вартість: {value:,.0f} €\n\n"
        f"Мито (10%): {duty:,.0f} €\n"
        f"Акциз: {excise:,.0f} €\n"
        f"ПДВ (20%): {vat:,.0f} €\n"
        f"<b>Разом до сплати: {total:,.0f} €</b>\n\n"
        "⚠️ Розрахунок орієнтовний. Точну суму й документи уточнить наш "
        "митний брокер після перегляду авто."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Залишити заявку", callback_data="menu:lead")
    kb.button(text="⬅️ У меню", callback_data="menu:main")
    kb.adjust(1)
    await state.clear()
    await message.answer(text, reply_markup=kb.as_markup())


# ---------------------------------------------------------------------------
# 2. Підбір авто
# ---------------------------------------------------------------------------

def budget_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="до $10 000", callback_data="budget:0-10000")
    kb.button(text="$10 000–20 000", callback_data="budget:10000-20000")
    kb.button(text="$20 000+", callback_data="budget:20000-999999")
    kb.button(text="⬅️ Назад", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def body_kb():
    kb = InlineKeyboardBuilder()
    for key, label in BODY_LABELS.items():
        kb.button(text=label, callback_data=f"body:{key}")
    kb.button(text="Будь-який", callback_data="body:any")
    kb.adjust(2)
    return kb.as_markup()


@router.callback_query(F.data == "menu:select")
async def select_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SelectStates.budget)
    await callback.message.edit_text(
        "🚗 <b>Підбір авто</b>\n\nЯкий орієнтовний бюджет (митна вартість авто)?",
        reply_markup=budget_kb(),
    )
    await callback.answer()


@router.callback_query(SelectStates.budget, F.data.startswith("budget:"))
async def select_budget_chosen(callback: CallbackQuery, state: FSMContext):
    lo, hi = map(int, callback.data.split(":")[1].split("-"))
    await state.update_data(budget_lo=lo, budget_hi=hi)
    await state.set_state(SelectStates.body)
    await callback.message.edit_text(
        "Який тип кузова цікавить?", reply_markup=body_kb()
    )
    await callback.answer()


@router.callback_query(SelectStates.body, F.data.startswith("body:"))
async def select_body_chosen(callback: CallbackQuery, state: FSMContext):
    body = callback.data.split(":")[1]
    data = await state.get_data()
    lo, hi = data["budget_lo"], data["budget_hi"]

    results = [
        c for c in MOCK_CARS
        if lo <= c["price"] <= hi and (body == "any" or c["body"] == body)
    ]
    await state.clear()

    if not results:
        await callback.message.edit_text(
            "😔 Під цей запит зараз немає прикладів у демо-базі. "
            "У бойовій версії тут буде живий підбір з бази аукціонів.",
            reply_markup=main_menu_kb(),
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        f"🚗 Знайшов {len(results)} варіант(и) під ваш запит:"
    )
    for c in results[:3]:
        text = (
            f"🚘 <b>{c['title']}</b>\n"
            f"Кузов: {BODY_LABELS[c['body']]}\n"
            f"Пробіг: {c['mileage']}\n"
            f"Паливо: {c['fuel']}\n"
            f"Стан: {c['note']}\n"
            f"💵 Митна вартість: ${c['price']:,}"
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="📝 Хочу цей варіант", callback_data="menu:lead")
        kb.adjust(1)
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.message.answer("Оберіть дію:", reply_markup=main_menu_kb())
    await callback.answer()


# ---------------------------------------------------------------------------
# 3. Заявка (лід)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "menu:lead")
async def lead_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(LeadStates.name)
    await callback.message.answer("📝 Як до вас звертатись? Напишіть ім'я:")
    await callback.answer()


@router.message(LeadStates.name)
async def lead_name_entered(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(LeadStates.phone)
    await message.answer("📱 І номер телефону для зв'язку:")


@router.message(LeadStates.phone)
async def lead_phone_entered(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    name = data.get("name", "-")
    phone = message.text
    user = message.from_user

    is_new = not os.path.exists(LEADS_FILE)
    with open(LEADS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["datetime", "name", "phone", "telegram_username", "telegram_id"])
        writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            name, phone, user.username or "-", user.id,
        ])

    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(
                ADMIN_CHAT_ID,
                f"🆕 Нова заявка!\nІм'я: {name}\nТелефон: {phone}\n"
                f"Telegram: @{user.username or user.id}",
            )
        except Exception:
            logging.exception("Не вдалось надіслати заявку адміну")

    await state.clear()
    await message.answer(
        "✅ Дякуємо! Заявку прийнято, менеджер зв'яжеться з вами найближчим часом.",
        reply_markup=main_menu_kb(),
    )


# ---------------------------------------------------------------------------
# 4. Контакти та 5. Як це працює
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "menu:contacts")
async def show_contacts(callback: CallbackQuery):
    lines = "\n".join(f"{label}: {handle}" for label, handle in CONTACTS)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ У меню", callback_data="menu:main")
    await callback.message.edit_text(f"📞 <b>Контакти команди</b>\n\n{lines}", reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "menu:howto")
async def show_howto(callback: CallbackQuery):
    text = (
        "ℹ️ <b>Як це працює</b>\n\n"
        "1️⃣ Обираєте авто (самі або з нашим підбором)\n"
        "2️⃣ Ми викуповуємо лот на аукціоні (Copart/IAAI)\n"
        "3️⃣ Доставка до порту й морське перевезення\n"
        "4️⃣ Розмитнення в українському порту\n"
        "5️⃣ Доставка авто вам\n\n"
        "На кожному етапі ви бачите статус і контакт відповідального менеджера."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ У меню", callback_data="menu:main")
    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()


# ---------------------------------------------------------------------------
# Точка входу
# ---------------------------------------------------------------------------

async def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN не задано. Скопіюйте .env.example у .env і вставте "
            "токен, отриманий від @BotFather."
        )
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
