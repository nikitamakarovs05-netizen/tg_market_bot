# bot.py
# Маркетплейс-бот на aiogram 3: каталог → корзина → заказ → оплата (Telegram Payments)
# + Базовая верификация по номеру телефона (request_contact) и опциональная email-OTP.
# БД: SQLite (aiosqlite). Переменные окружения читаются из .env

import asyncio
import os
import re
import random
import string
import datetime
import logging
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove  
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from email_validator import validate_email, EmailNotValidError
from dotenv import load_dotenv

# -------------------- Настройки/инициализация --------------------
logging.basicConfig(level=logging.INFO)
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = set()
if os.getenv("ADMIN_IDS"):
    try:
        ADMIN_IDS = set(map(int, os.getenv("ADMIN_IDS").split(",")))
    except Exception:
        logging.warning("ADMIN_IDS не распознан. Укажи числа через запятую, например: 123,456")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN пуст. Укажи токен бота в .env")

DB_PATH = os.getenv("DB_PATH", "shop.db")
INIT_SQL = """\
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  tg_id INTEGER UNIQUE,
  full_name TEXT,
  username TEXT,
  phone TEXT,
  is_verified INTEGER DEFAULT 0,
  email TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT,
  price INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'EUR',
  photo_url TEXT,
  is_active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS carts (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cart_items (
  id INTEGER PRIMARY KEY,
  cart_id INTEGER NOT NULL,
  product_id INTEGER NOT NULL,
  qty INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  amount INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'EUR',
  status TEXT NOT NULL DEFAULT 'pending',
  address TEXT,
  note TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS order_items (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL,
  product_id INTEGER NOT NULL,
  qty INTEGER NOT NULL,
  price INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL,
  provider TEXT,
  payload TEXT,
  telegram_charge_id TEXT,
  provider_charge_id TEXT,
  status TEXT DEFAULT 'pending',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS email_otps (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  email TEXT NOT NULL,
  code TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used INTEGER DEFAULT 0
);
"""

from aiogram.client.default import DefaultBotProperties

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))

dp = Dispatcher()


# -------------------- Утилиты --------------------
def db():
    return aiosqlite.connect(DB_PATH)

async def ensure_tables():
    async with db() as conn:
        await conn.executescript(INIT_SQL)
        await conn.commit()

async def ensure_content_tables():
    async with db() as conn:
        await conn.executescript("""
        CREATE TABLE IF NOT EXISTS content_sections (
          id INTEGER PRIMARY KEY,
          key TEXT UNIQUE NOT NULL,
          text TEXT,
          updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS content_photos (
          id INTEGER PRIMARY KEY,
          section_key TEXT NOT NULL,
          file_id TEXT NOT NULL,
          sort_order INTEGER DEFAULT 0
        );
        """)
        await conn.commit()


def money_fmt(cents: int, curr: str = "EUR") -> str:
    return f"{cents/100:.2f} {curr}"

def gen_otp(n: int = 6) -> str:
    return "".join(random.choice(string.digits) for _ in range(n))

async def set_section_text(key: str, text: str):
    async with db() as conn:
        await conn.execute(
            "INSERT INTO content_sections(key, text) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET text=excluded.text, updated_at=CURRENT_TIMESTAMP",
            (key, text)
        )
        await conn.commit()

async def get_section_text(key: str) -> str | None:
    async with db() as conn:
        async with conn.execute("SELECT text FROM content_sections WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None

async def add_section_photo(key: str, file_id: str, sort: int = 0):
    async with db() as conn:
        await conn.execute(
            "INSERT INTO content_photos(section_key, file_id, sort_order) VALUES(?,?,?)",
            (key, file_id, sort)
        )
        await conn.commit()

async def get_section_photos(key: str) -> list[str]:
    async with db() as conn:
        async with conn.execute(
            "SELECT file_id FROM content_photos WHERE section_key=? ORDER BY sort_order, id",
            (key,)
        ) as cur:
            rows = await cur.fetchall()
    return [r[0] for r in rows]


async def send_email_code(email: str, code: str):
    """
    Заглушка: замени на реальную отправку (SMTP/SendGrid/Mailgun).
    См. инструкцию — добавь aiosmtplib и переменные SMTP_* в .env.
    """
    logging.info(f"[EMAIL_OTP] send to {email}: code={code}")


# -------------------- FSM --------------------
class CheckoutFSM(StatesGroup):
    waiting_address = State()
    waiting_note = State()
    waiting_email = State()
    waiting_email_code = State()


# -------------------- Клавиатуры --------------------
def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🛍 Каталог", callback_data="catalog")
    kb.button(text="🧺 Корзина", callback_data="cart")
    kb.button(text="ℹ️ Помощь", callback_data="help")
    kb.adjust(2, 1)
    return kb.as_markup()

def contact_request_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Подтвердить номер ☎️", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def bottom_menu_kb():
    # Постоянная нижняя клавиатура с одной кнопкой
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📦 Каталог")]],
        resize_keyboard=True, one_time_keyboard=False, is_persistent=True
    )

def two_wide_main_kb():
    # Две широкие inline-кнопки одна под другой
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 Каталог", callback_data="main_catalog")],
        [InlineKeyboardButton(text="🆘 Помощь", callback_data="help")]
    ])


# -------------------- Пользователи / Верификация --------------------
async def ensure_user_registered(message: Message):
    async with db() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO users (tg_id, full_name, username) VALUES (?,?,?)",
            (message.from_user.id, message.from_user.full_name, message.from_user.username)
        )
        await conn.commit()

@dp.message(CommandStart())
async def on_start(message: Message):
    await ensure_user_registered(message)
    async with db() as conn:
        async with conn.execute(
            "SELECT is_verified, phone FROM users WHERE tg_id=?",
            (message.from_user.id,)
        ) as cur:
            row = await cur.fetchone()

    if not row or row[0] == 0 or not row[1]:
        await message.answer(
            "Привет! Чтобы пользоваться магазином, подтвердите номер телефона.",
            reply_markup=contact_request_kb()
        )
    else:
        # уже верифицирован — показываем нижнее меню и 2 широкие кнопки
        await message.answer("Добро пожаловать в маркетплейс 👋", reply_markup=bottom_menu_kb())
        await message.answer("Выберите действие:", reply_markup=two_wide_main_kb())

@dp.message(F.contact)
async def on_contact(message: Message):
    phone = message.contact.phone_number
    async with db() as conn:
        await conn.execute(
            "UPDATE users SET phone=?, is_verified=1 WHERE tg_id=?",
            (phone, message.from_user.id)
        )
        await conn.commit()
    await message.answer("Номер подтверждён ✅", reply_markup=bottom_menu_kb())
    await message.answer("Выберите действие:", reply_markup=two_wide_main_kb())

@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    await message.answer("Выберите действие:", reply_markup=two_wide_main_kb())

@dp.message(F.text.casefold() == "📦 каталог".casefold())
async def bottom_catalog_pressed(message: Message):
    await message.answer("Выберите действие:", reply_markup=two_wide_main_kb())

def interests_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1) Одноразовые устройства", callback_data="cat:disposables")],
        [InlineKeyboardButton(text="2) Жидкости и картриджи",   callback_data="cat:liquids")],
        [InlineKeyboardButton(text="3) Под-системы",            callback_data="cat:pods")],
        [InlineKeyboardButton(text="↩️ Назад",                  callback_data="home_main")]
    ])

def brands_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Waka",     callback_data="brand:waka")],
        [InlineKeyboardButton(text="Vozol",    callback_data="brand:vozol")],
        [InlineKeyboardButton(text="Aerovibe", callback_data="brand:aerovibe")],
        [InlineKeyboardButton(text="Elfbar",   callback_data="brand:elfbar")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="main_catalog")]
    ])

@dp.callback_query(F.data == "cat:disposables")
async def disposables_menu(call: CallbackQuery):
    await call.message.edit_text("Выберите производителя:", reply_markup=brands_kb())

async def brand_card_text(brand: str) -> str:
    key = f"brand:{brand.lower()}"
    custom = await get_section_text(key)
    if custom:
        return f"<b>{brand}</b>\n\n{custom}"
    return (
        f"<b>{brand}</b>\n\n"
        "📋 Модели и вкусы:\n"
        "— <i>сюда позже вставим список</i>\n\n"
        "Нажмите «Заказать», затем укажите вкус и количество."
    )


def brand_card_kb(brand: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Заказать", callback_data=f"order:brand:{brand}")],
        [InlineKeyboardButton(text="↩️ Назад",   callback_data="cat:disposables")]
    ])

@dp.callback_query(F.data.startswith("brand:"))
async def brand_card(call: CallbackQuery):
    brand = call.data.split(":")[1]
    text = await brand_card_text(brand.capitalize())
    await call.message.edit_text(text, reply_markup=brand_card_kb(brand))



class ManualOrderFSM(StatesGroup):
    waiting_details = State()
    waiting_confirm = State()

@dp.callback_query(F.data.startswith("order:brand:"))
async def start_brand_order(call: CallbackQuery, state: FSMContext):
    brand = call.data.split(":")[2]
    await state.update_data(kind="brand", brand=brand)

    # клавиатура с кнопкой "Назад"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Назад", callback_data="cancel_order_step")]
    ])

    await call.message.edit_text(
        f"Вы выбрали <b>{brand.capitalize()}</b>.\n\n"
        "Напишите одним сообщением ВКУС и КОЛИЧЕСТВО.\n"
        "Пример: «Cola Ice × 2»",
        reply_markup=kb
    )

    await state.set_state(ManualOrderFSM.waiting_details)

@dp.message(ManualOrderFSM.waiting_details)
async def catch_details(message: Message, state: FSMContext):
    await state.update_data(details=message.text.strip())
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Заказать", callback_data="confirm_order")],
        [InlineKeyboardButton(text="↩️ Назад",   callback_data="cancel_order_step")]
    ])
    await message.answer(f"Проверьте данные:\n\n<code>{message.text.strip()}</code>", reply_markup=kb)
    await state.set_state(ManualOrderFSM.waiting_confirm)

@dp.callback_query(F.data == "cancel_order_step")
async def cancel_order_any_state(call: CallbackQuery, state: FSMContext):
    await call.answer()  # закрыть "крутилку" спиннера
    data = await state.get_data()
    kind = data.get("kind")

    try:
        if kind == "brand":
            await call.message.edit_text("Выберите производителя:", reply_markup=brands_kb())
        elif kind == "liquids":
            await call.message.edit_text(liquids_text(), reply_markup=liquids_kb())
        elif kind == "pods":
            await call.message.edit_text(pods_text(), reply_markup=pods_kb())
        else:
            await call.message.edit_text("Выберите действие:", reply_markup=two_wide_main_kb())
    except Exception:
        # если редактирование не удалось (например, до этого было фото/альбом) — отправим новое сообщение
        if kind == "brand":
            await call.message.answer("Выберите производителя:", reply_markup=brands_kb())
        elif kind == "liquids":
            await call.message.answer(liquids_text(), reply_markup=liquids_kb())
        elif kind == "pods":
            await call.message.answer(pods_text(), reply_markup=pods_kb())
        else:
            await call.message.answer("Выберите действие:", reply_markup=two_wide_main_kb())

    await state.clear()


@dp.callback_query(ManualOrderFSM.waiting_confirm, F.data == "confirm_order")
async def confirm_order(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    details = data.get("details", "—")
    kind = data.get("kind", "—")
    brand = data.get("brand", None)

    await call.message.edit_text("✅ Ваш заказ принят! В течение 5 минут с вами свяжутся!")

    if ADMIN_IDS:
        user_tag = f"@{call.from_user.username}" if call.from_user.username else call.from_user.full_name
        title = f"{'Бренд: ' + brand.capitalize() if brand else kind}"
        admin_text = (
            f"🆕 <b>Новый заказ</b>\n"
            f"{title}\n"
            f"Покупатель: {user_tag} (tg_id={call.from_user.id})\n"
            f"Детали: {details}"
        )
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, admin_text)
            except Exception:
                pass

    await state.clear()
def liquids_text() -> str:
    return (
        "<b>Жидкости и картриджи</b>\n\n"
        "📋 Модели/вкусы:\n"
        "— <i>сюда позже вставим список</i>\n\n"
        "Нажмите «Заказать», затем укажите вкус и количество."
    )

def liquids_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Заказать", callback_data="order:liquids")],
        [InlineKeyboardButton(text="↩️ Назад",   callback_data="main_catalog")]
    ])

@dp.callback_query(F.data == "cat:liquids")
async def liquids_menu(call: CallbackQuery):
    await call.message.edit_text(liquids_text(), reply_markup=liquids_kb())

@dp.callback_query(F.data == "order:liquids")
async def liquids_order(call: CallbackQuery, state: FSMContext):
    await state.update_data(kind="liquids")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Назад", callback_data="cancel_order_step")]
    ])
    await call.message.edit_text("Напишите ВКУС и КОЛИЧЕСТВО.\nПример: «Mango 30мл × 2»", reply_markup=kb)
    await state.set_state(ManualOrderFSM.waiting_details)


def pods_text() -> str:
    return (
        "<b>Под-системы</b>\n\n"
        "📋 Модели:\n"
        "— <i>сюда позже вставим список</i>\n\n"
        "Нажмите «Заказать», затем укажите модель."
    )

def pods_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Заказать", callback_data="order:pods")],
        [InlineKeyboardButton(text="↩️ Назад",   callback_data="main_catalog")]
    ])

@dp.callback_query(F.data == "cat:pods")
async def pods_menu(call: CallbackQuery):
    await call.message.edit_text(pods_text(), reply_markup=pods_kb())

@dp.callback_query(F.data == "order:pods")
async def pods_order(call: CallbackQuery, state: FSMContext):
    await state.update_data(kind="pods")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Назад", callback_data="cancel_order_step")]
    ])
    await call.message.edit_text("Напишите МОДЕЛЬ (и при необходимости цвет/комплектацию).", reply_markup=kb)
    await state.set_state(ManualOrderFSM.waiting_details)



@dp.callback_query(F.data == "main_catalog")
async def show_interests(call: CallbackQuery):
    await call.message.edit_text("Что вас интересует?", reply_markup=interests_kb())

@dp.callback_query(F.data == "home_main")
async def home_main(call: CallbackQuery):
    await call.message.edit_text("Выберите действие:", reply_markup=two_wide_main_kb())


@dp.message(F.text.lower() == "верификация email")
async def email_verify_entry(message: Message, state: FSMContext):
    await message.answer("Введите ваш email:")
    await state.set_state(CheckoutFSM.waiting_email)

@dp.message(CheckoutFSM.waiting_email)
async def email_input(message: Message, state: FSMContext):
    try:
        info = validate_email(message.text, check_deliverability=False)
        email = info.normalized
    except EmailNotValidError as e:
        await message.answer(f"Некорректный email: {e}. Попробуйте снова.")
        return
    code = gen_otp()
    expires = (datetime.datetime.utcnow() + datetime.timedelta(minutes=10)).isoformat()
    async with db() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO users (tg_id, full_name, username) VALUES (?,?,?)",
            (message.from_user.id, message.from_user.full_name, message.from_user.username)
        )
        await conn.execute(
            "INSERT INTO email_otps (user_id, email, code, expires_at) "
            "SELECT id, ?, ?, ? FROM users WHERE tg_id=?",
            (email, code, expires, message.from_user.id)
        )
        await conn.commit()
    await send_email_code(email, code)
    await state.update_data(email=email)
    await message.answer(f"Код отправлен на {email}. Введите 6-значный код:")
    await state.set_state(CheckoutFSM.waiting_email_code)

@dp.message(CheckoutFSM.waiting_email_code)
async def email_code_check(message: Message, state: FSMContext):
    code = message.text.strip()
    if not re.fullmatch(r"\d{6}", code):
        await message.answer("Нужно 6 цифр. Попробуйте снова.")
        return
    async with db() as conn:
        async with conn.execute(
            "SELECT e.id FROM email_otps e JOIN users u ON u.id=e.user_id "
            "WHERE u.tg_id=? AND e.code=? AND e.used=0 AND datetime(e.expires_at) > datetime('now') "
            "ORDER BY e.id DESC LIMIT 1",
            (message.from_user.id, code)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            await message.answer("Код неверный или просрочен.")
            return
        otp_id = row[0]
        await conn.execute("UPDATE email_otps SET used=1 WHERE id=?", (otp_id,))
        await conn.execute("UPDATE users SET is_verified=1 WHERE tg_id=?", (message.from_user.id,))
        await conn.commit()
    await state.clear()
    await message.answer("Email верифицирован ✅")
    await message.answer("Главное меню:", reply_markup=main_menu_kb())


# -------------------- Каталог --------------------
async def list_products():
    async with db() as conn:
        async with conn.execute(
            "SELECT id, title, price, currency FROM products WHERE is_active=1 ORDER BY id DESC"
        ) as cur:
            return await cur.fetchall()

async def get_product(pid: int):
    async with db() as conn:
        async with conn.execute(
            "SELECT id, title, description, price, currency, photo_url FROM products WHERE id=?",
            (pid,)
        ) as cur:
            return await cur.fetchone()

@dp.callback_query(F.data == "catalog")
async def cb_catalog(call: CallbackQuery):
    items = await list_products()
    if not items:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Назад", callback_data="home")]
        ])
        await call.message.edit_text("Каталог пуст. Добавьте товары (админ).", reply_markup=kb)
        return
    kb = InlineKeyboardBuilder()
    for pid, title, price, currency in items:
        kb.button(text=f"{title} — {money_fmt(price, currency)}", callback_data=f"p:{pid}")
    kb.button(text="↩️ Назад", callback_data="home")
    kb.adjust(1, 1)
    await call.message.edit_text("Каталог:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("p:"))
async def cb_product(call: CallbackQuery):
    pid = int(call.data.split(":")[1])
    p = await get_product(pid)
    if not p:
        await call.answer("Товар не найден", show_alert=True)
        return
    pid, title, desc, price, curr, photo = p
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ В корзину", callback_data=f"add:{pid}")],
        [InlineKeyboardButton(text="🧺 Корзина", callback_data="cart")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="catalog")]
    ])
    text = f"<b>{title}</b>\n\n{desc or 'Без описания'}\n\nЦена: {money_fmt(price, curr)}"
    if photo:
        # удаляем старое сообщение (чтобы показать фото как новое)
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.message.answer_photo(photo=photo, caption=text, reply_markup=kb)
    else:
        await call.message.edit_text(text, reply_markup=kb)


# -------------------- Корзина --------------------
async def get_or_create_cart(user_tg_id: int) -> int:
    async with db() as conn:
        async with conn.execute("SELECT id FROM users WHERE tg_id=?", (user_tg_id,)) as cur:
            u = await cur.fetchone()
        if not u:
            await conn.execute("INSERT INTO users (tg_id) VALUES (?)", (user_tg_id,))
            await conn.commit()
            async with conn.execute("SELECT id FROM users WHERE tg_id=?", (user_tg_id,)) as cur:
                u = await cur.fetchone()
        user_id = u[0]
        async with conn.execute("SELECT id FROM carts WHERE user_id=?", (user_id,)) as cur:
            c = await cur.fetchone()
        if not c:
            await conn.execute("INSERT INTO carts (user_id) VALUES (?)", (user_id,))
            await conn.commit()
            async with conn.execute("SELECT id FROM carts WHERE user_id=?", (user_id,)) as cur:
                c = await cur.fetchone()
        return c[0]

@dp.callback_query(F.data == "cart")
async def cb_cart(call: CallbackQuery):
    cart_id = await get_or_create_cart(call.from_user.id)
    async with db() as conn:
        async with conn.execute(
            "SELECT ci.id, p.title, ci.qty, p.price, p.currency, p.id "
            "FROM cart_items ci JOIN products p ON p.id=ci.product_id "
            "WHERE ci.cart_id=?", (cart_id,)
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛍 В каталог", callback_data="catalog")],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="home")]
        ])
        await call.message.edit_text("Ваша корзина пуста.", reply_markup=kb)
        return
    total = sum(q * price for _, _, q, price, _, _ in rows)
    currency = rows[0][4]
    text = "🧺 <b>Корзина</b>\n\n"
    kb = InlineKeyboardBuilder()
    for item_id, title, qty, price, curr, pid in rows:
        text += f"• {title} × {qty} = {money_fmt(qty*price, curr)}\n"
        kb.button(text=f"➖ {title}", callback_data=f"dec:{pid}")
        kb.button(text=f"➕ {title}", callback_data=f"inc:{pid}")
        kb.button(text=f"✖️ Удалить", callback_data=f"del:{pid}")
        kb.adjust(3)
    text += f"\nИтого: <b>{money_fmt(total, currency)}</b>"
    kb.row(InlineKeyboardButton(text="✅ Оформить заказ", callback_data="checkout"))
    kb.row(InlineKeyboardButton(text="↩️ Назад", callback_data="catalog"))
    await call.message.edit_text(text, reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith(("add:", "inc:", "dec:", "del:")))
async def cart_actions(call: CallbackQuery):
    action, pid = call.data.split(":")
    pid = int(pid)
    cart_id = await get_or_create_cart(call.from_user.id)
    async with db() as conn:
        async with conn.execute(
            "SELECT id, qty FROM cart_items WHERE cart_id=? AND product_id=?",
            (cart_id, pid)
        ) as cur:
            row = await cur.fetchone()
        if action == "add":
            if row:
                await conn.execute("UPDATE cart_items SET qty=qty+1 WHERE id=?", (row[0],))
            else:
                await conn.execute(
                    "INSERT INTO cart_items (cart_id, product_id, qty) VALUES (?,?,1)",
                    (cart_id, pid)
                )
        elif action == "inc":
            if row:
                await conn.execute("UPDATE cart_items SET qty=qty+1 WHERE id=?", (row[0],))
        elif action == "dec":
            if row and row[1] > 1:
                await conn.execute("UPDATE cart_items SET qty=qty-1 WHERE id=?", (row[0],))
            elif row:
                await conn.execute("DELETE FROM cart_items WHERE id=?", (row[0],))
        elif action == "del":
            if row:
                await conn.execute("DELETE FROM cart_items WHERE id=?", (row[0],))
        await conn.execute("UPDATE carts SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (cart_id,))
        await conn.commit()
    await cb_cart(call)



# -------------------- Оформление (без онлайн-оплаты) --------------------
@dp.callback_query(F.data == "checkout")
async def checkout_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("Введите адрес доставки (улица, дом/квартира, город, ZIP):")
    await state.set_state(CheckoutFSM.waiting_address)

@dp.message(CheckoutFSM.waiting_address)
async def checkout_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text.strip())
    await message.answer("Добавьте комментарий к заказу (или напишите «-»):")
    await state.set_state(CheckoutFSM.waiting_note)

@dp.message(CheckoutFSM.waiting_note)
async def checkout_note(message: Message, state: FSMContext):
    note = None if message.text.strip() == "-" else message.text.strip()
    await state.update_data(note=note)

    # соберём корзину
    cart_id = await get_or_create_cart(message.from_user.id)
    async with db() as conn:
        async with conn.execute(
            "SELECT p.id, p.title, p.price, p.currency, ci.qty "
            "FROM cart_items ci JOIN products p ON p.id=ci.product_id "
            "WHERE ci.cart_id=?", (cart_id,)
        ) as cur:
            items = await cur.fetchall()

    if not items:
        await state.clear()
        await message.answer("Корзина пуста.")
        return

    total = sum(price * qty for _, _, price, _, qty in items)
    currency = items[0][3]

    # создаём заказ pending без инвойса
    data = await state.get_data()
    async with db() as conn:
        async with conn.execute("SELECT id FROM users WHERE tg_id=?", (message.from_user.id,)) as cur:
            u = await cur.fetchone()
        user_id = u[0]

        await conn.execute(
            "INSERT INTO orders (user_id, amount, currency, status, address, note) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, total, currency, 'pending', data['address'], data['note'])
        )
        await conn.commit()
        async with conn.execute("SELECT last_insert_rowid()") as cur:
            order_id = (await cur.fetchone())[0]

        # позиции заказа
        for pid, title, price, curr, qty in items:
            await conn.execute(
                "INSERT INTO order_items (order_id, product_id, qty, price) VALUES (?,?,?,?)",
                (order_id, pid, qty, price)
            )
        # очищаем корзину
        await conn.execute("DELETE FROM cart_items WHERE cart_id=?", (cart_id,))
        await conn.commit()

    await state.clear()

    # текст-итоги для пользователя
    lines = [f"• {t} × {q} — {money_fmt(p*q, currency)}" for _, t, p, _, q in items]
    summary = "\n".join(lines)
    text = (
        f"✅ <b>Заказ №{order_id} оформлен</b>\n\n"
        f"{summary}\n\n"
        f"Итого: <b>{money_fmt(total, currency)}</b>\n\n"
        "Оплата: <b>офлайн</b> (при получении/по договорённости).\n"
        "Мы свяжемся с вами для подтверждения и деталей доставки."
    )
    await message.answer(text)

    # уведомление админам
    if ADMIN_IDS:
        user_tag = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
        admin_text = (
            f"🆕 Новый заказ №{order_id}\n"
            f"Покупатель: {user_tag} (tg_id={message.from_user.id})\n"
            f"Сумма: {money_fmt(total, currency)}\n"
            f"Адрес: {data['address']}\n"
            f"Комментарий: {data['note'] or '—'}\n\n"
            f"{summary}"
        )
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, admin_text)
            except Exception:
                pass

# -------------------- Help & Home --------------------
@dp.callback_query(F.data == "help")
async def cb_help(call: CallbackQuery):
    text = (
        "Помощь:\n"
        "• Просматривайте каталог и добавляйте товары в корзину.\n"
        "• Оформляйте заказ, менеджер свяжется с вами для оплаты/доставки.\n"
        "• Базовая верификация — по номеру телефона. Дополнительно можно подтвердить email (команда: «верификация email»).\n"
        "• Админам: /addproduct Товар;Цена_в_центах"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Назад", callback_data="home")]
    ])
    await call.message.edit_text(text, reply_markup=kb)

@dp.callback_query(F.data == "home")
async def cb_home(call: CallbackQuery):
    await call.message.edit_text("Главное меню:", reply_markup=main_menu_kb())


# -------------------- Admin (пример) --------------------
@dp.message(Command("addproduct"))
async def add_product(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        payload = message.text.split(" ", 1)[1]
        title, cents = payload.split(";", 1)
        title = title.strip()
        price = int(cents.strip())
    except Exception:
        await message.reply("Формат: /addproduct Название;Цена_в_центах")
        return
    async with db() as conn:
        await conn.execute(
            "INSERT INTO products (title, price, currency) VALUES (?,?, 'EUR')",
            (title, price)
        )
        await conn.commit()
    await message.reply(f"Добавлен товар: {title} — {money_fmt(price)}")


# -------------------- Admin: просмотр пользователей --------------------
@dp.message(Command("users"))
async def list_users(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return  # только админ может смотреть
    async with db() as conn:
        async with conn.execute(
            "SELECT full_name, username, phone, is_verified FROM users ORDER BY id DESC LIMIT 20"
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        await message.answer("Пользователей пока нет.")
        return
    text = "📋 <b>Последние пользователи:</b>\n\n"
    for full_name, username, phone, verified in rows:
        user_tag = f"@{username}" if username else "—"
        text += f"👤 {full_name or 'Без имени'} ({user_tag})\n☎️ {phone or '—'}\n✅ Верифицирован: {'да' if verified else 'нет'}\n\n"
    await message.answer(text)


# === Admin: контент разделов/брендов ===

@dp.message(Command("settext"))
async def cmd_settext(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply("Формат: /settext <key> <текст>\nПримеры ключей: brand:waka, brand:vozol, brand:aerovibe, brand:elfbar, liquids, pods")
        return
    key, text = parts[1], parts[2]
    await set_section_text(key, text)
    await message.reply(f"Текст для [{key}] сохранён.")

@dp.message(Command("addphoto"))
async def cmd_addphoto(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Формат: ответь на фото командой:\n/addphoto <key>\nНапр.: /addphoto brand:waka")
        return
    key = parts[1]
    if not message.reply_to_message or not message.reply_to_message.photo:
        await message.reply("Нужно отправить команду как ответ на сообщение с фото.")
        return
    file_id = message.reply_to_message.photo[-1].file_id
    await add_section_photo(key, file_id)
    await message.reply(f"Фото добавлено в [{key}].")

@dp.message(Command("listphotos"))
async def cmd_listphotos(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Формат: /listphotos <key>")
        return
    key = parts[1]
    photos = await get_section_photos(key)
    await message.reply(f"Фото в [{key}]: {len(photos)} шт.")

@dp.message(Command("clearphotos"))
async def cmd_clearphotos(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Формат: /clearphotos <key>")
        return
    key = parts[1]
    async with db() as conn:
        await conn.execute("DELETE FROM content_photos WHERE section_key=?", (key,))
        await conn.commit()
    await message.reply(f"Фото очищены для [{key}].")

# -------------------- Точка входа --------------------
async def main():
    # Создаём БД/таблицы, если их нет
    if not Path(DB_PATH).exists():
        logging.info("Создаю БД %s ...", DB_PATH)
    await ensure_tables()
    await ensure_content_tables()


    # Подсказка в логи
    logging.info("Бот запущен. Админы: %s", ADMIN_IDS if ADMIN_IDS else "—")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
