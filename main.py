"""
GoAuto Mini App — бэкенд.

Что делает этот сервис:
  1. Отдаёт статический фронтенд Telegram Mini App.
  2. Отдаёт каталог машин (GET /api/cars) — читает публичный Google Sheet
     (владелец бизнеса сам ведёт эту таблицу, как в MOCK_CARS раньше).
  3. Принимает заявки клиентов из Mini App (POST /api/leads):
       - проверяет подпись Telegram WebApp initData (чтобы заявки нельзя
         было подделать в обход Telegram),
       - сверяет запрос клиента с текущим каталогом,
       - если есть совпадения — бот СРАЗУ шлёт клиенту эти варианты
         (автоматический сценарий),
       - если совпадений нет — заявка уходит владельцу в Telegram, и он
         вручную подбирает вариант на Copart/IAAI (полуавтоматический
         сценарий).
  4. Обрабатывает ответ владельца: он просто делает Reply в Telegram на
     сообщение с заявкой и пишет предложение клиенту — бот сам находит,
     какому клиенту это переслать, ориентируясь по id сообщения.
"""

import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import sqlite3
import time
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, Message, MenuButtonWebApp, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")  # публичный https-адрес этого сервиса
CARS_SHEET_CSV_URL = os.getenv("CARS_SHEET_CSV_URL", "")
COMPANY_NAME = os.getenv("COMPANY_NAME", "SHASHKI MD")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "leads.db"

def _find_webapp_dir():
    candidates = [
        BASE_DIR.parent / "webapp",
        BASE_DIR / "webapp",
        BASE_DIR,
    ]
    for c in candidates:
        if (c / "index.html").exists():
            return c
    return BASE_DIR

WEBAPP_DIR = _find_webapp_dir()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("goauto")

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT,
                client_chat_id INTEGER,
                client_username TEXT,
                name TEXT,
                phone TEXT,
                budget_lo INTEGER,
                budget_hi INTEGER,
                body TEXT,
                fuel TEXT,
                comment TEXT,
                status TEXT,
                admin_message_id INTEGER
            )
        """)


_cars_cache = {"ts": 0.0, "rows": []}
CARS_CACHE_TTL = 120


async def get_cars(force: bool = False):
    if not CARS_SHEET_CSV_URL:
        return []
    if not force and time.time() - _cars_cache["ts"] < CARS_CACHE_TTL:
        return _cars_cache["rows"]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(CARS_SHEET_CSV_URL)
            resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        rows = []
        for r in reader:
            try:
                rows.append({
                    "title": r.get("title", "").strip(),
                    "body": r.get("body", "").strip().lower(),
                    "price": int(float(r.get("price") or 0)),
                    "mileage": r.get("mileage", "").strip(),
                    "fuel": r.get("fuel", "").strip(),
                    "volume": int(float(r.get("volume") or 0)),
                    "note": r.get("note", "").strip(),
                    "photo_url": r.get("photo_url", "").strip(),
                    "vin": r.get("vin", "").strip(),
                    "engine": r.get("engine", "").strip(),
                    "drive": r.get("drive", "").strip(),
                    "location": r.get("location", "").strip(),
                    "auctionDate": r.get("auction_date", "").strip(),
                    "damage": r.get("damage", "").strip(),
                    "keys": r.get("keys", "").strip(),
                    "titleCert": r.get("title_cert", "").strip(),
                    "seller": r.get("seller", "").strip(),
                    "lot": r.get("lot", "").strip(),
                })
            except (ValueError, TypeError):
                log.warning("Пропускаю некорректную строку каталога: %s", r)
        _cars_cache["rows"] = rows
        _cars_cache["ts"] = time.time()
    except Exception:
        log.exception("Не удалось обновить каталог из Google Sheet, оставляю старый кеш")
    return _cars_cache["rows"]


def match_cars(cars, budget_lo, budget_hi, body):
    return [
        c for c in cars
        if budget_lo <= c["price"] <= budget_hi
        and (body in ("", "any") or c["body"] == body)
    ][:3]


def validate_init_data(init_data: str, bot_token: str):
    try:
        pairs = urllib.parse.parse_qsl(init_data, strict_parsing=True)
    except ValueError:
        return None
    parsed = dict(pairs)
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        return None
    user_raw = parsed.get("user")
    user = json.loads(user_raw) if user_raw else {}
    return {"raw": parsed, "user": user}


router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    kb = InlineKeyboardBuilder()
    if WEBAPP_URL:
        kb.button(text="🚀 Открыть приложение", web_app=WebAppInfo(url=WEBAPP_URL))
    await message.answer(
        f"👋 Добро пожаловать в <b>{COMPANY_NAME}</b>!\n\n"
        "Нажмите кнопку ниже, чтобы рассчитать растаможку и подобрать авто.",
        reply_markup=kb.as_markup(),
    )


@router.message(F.reply_to_message)
async def admin_reply_handler(message: Message, bot: Bot):
    if str(message.chat.id) != str(ADMIN_CHAT_ID):
        return

    with db() as conn:
        row = conn.execute(
            "SELECT * FROM leads WHERE admin_message_id = ?",
            (message.reply_to_message.message_id,),
        ).fetchone()

    if not row:
        return

    offer_text = (
        f"📩 <b>Менеджер подобрал для вас вариант</b>\n\n{message.text or message.caption or ''}"
    )
    try:
        if message.photo:
            await bot.send_photo(row["client_chat_id"], message.photo[-1].file_id, caption=offer_text)
        else:
            await bot.send_message(row["client_chat_id"], offer_text)
        with db() as conn:
            conn.execute("UPDATE leads SET status = 'answered' WHERE id = ?", (row["id"],))
        await message.reply("✅ Отправлено клиенту.")
    except Exception:
        log.exception("Не удалось переслать ответ клиенту")
        await message.reply("⚠️ Не удалось отправить клиенту (возможно, он заблокировал бота).")


bot: Bot | None = None
dp: Dispatcher | None = None
_polling_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot, dp, _polling_task
    init_db()
    if BOT_TOKEN:
        bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dp = Dispatcher(storage=MemoryStorage())
        dp.include_router(router)
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_my_commands([BotCommand(command="start", description="Открыть приложение")])
        if WEBAPP_URL:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                text="Приложение", web_app=WebAppInfo(url=WEBAPP_URL)))
        _polling_task = asyncio.create_task(dp.start_polling(bot))
        log.info("Бот запущен (long polling)")
    else:
        log.warning("BOT_TOKEN не задан — бот не запущен, работает только REST API")
    yield
    if _polling_task:
        _polling_task.cancel()
    if bot:
        await bot.session.close()


app = FastAPI(title="GoAuto Mini App backend", lifespan=lifespan)


@app.get("/api/cars")
async def api_cars():
    return await get_cars()


class LeadIn(BaseModel):
    init_data: str
    name: str
    phone: str
    budget_lo: int
    budget_hi: int
    body: str
    fuel: str = ""
    comment: str = ""


@app.post("/api/leads")
async def api_create_lead(lead: LeadIn):
    if not BOT_TOKEN:
        raise HTTPException(500, "BOT_TOKEN не настроен на сервере")

    auth = validate_init_data(lead.init_data, BOT_TOKEN)
    if not auth:
        raise HTTPException(401, "Не удалось проверить данные Telegram (invalid initData)")

    user = auth["user"]
    client_chat_id = user.get("id")
    client_username = user.get("username", "")
    if not client_chat_id:
        raise HTTPException(400, "Не удалось определить пользователя Telegram")

    cars = await get_cars()
    matches = match_cars(cars, lead.budget_lo, lead.budget_hi, lead.body)

    with db() as conn:
        cur = conn.execute(
            "INSERT INTO leads (created_at, client_chat_id, client_username, name, phone, "
            "budget_lo, budget_hi, body, fuel, comment, status) VALUES "
            "(datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (client_chat_id, client_username, lead.name, lead.phone, lead.budget_lo,
             lead.budget_hi, lead.body, lead.fuel, lead.comment,
             "auto_matched" if matches else "pending"),
        )
        lead_id = cur.lastrowid

    if matches:
        text = "🚗 <b>Нашли для вас варианты:</b>\n\n" + "\n\n".join(
            f"<b>{c['title']}</b>\n{c['mileage']}, {c['fuel']}\n{c['note']}\n💵 ${c['price']:,}"
            for c in matches
        )
        await bot.send_message(client_chat_id, text)
        return {"status": "matched", "cars": matches}

    if ADMIN_CHAT_ID:
        admin_text = (
            f"🆕 <b>Новая заявка №{lead_id}</b>\n\n"
            f"Имя: {lead.name}\nТелефон: {lead.phone}\n"
            f"Telegram: @{client_username or client_chat_id}\n"
            f"Бюджет: ${lead.budget_lo:,}–${lead.budget_hi:,}\n"
            f"Кузов: {lead.body or 'любой'}\nТопливо: {lead.fuel or 'без разницы'}\n"
            f"Комментарий: {lead.comment or '-'}\n\n"
            f"👉 Чтобы ответить клиенту, сделайте <b>Reply</b> на это сообщение "
            f"с текстом (или фото) предложения."
        )
        sent = await bot.send_message(ADMIN_CHAT_ID, admin_text)
        with db() as conn:
            conn.execute("UPDATE leads SET admin_message_id = ? WHERE id = ?",
                         (sent.message_id, lead_id))

    return {"status": "pending"}


app.mount("/", StaticFiles(directory=WEBAPP_DIR, html=True), name="webapp")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
