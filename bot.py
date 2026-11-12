# -*- coding: utf-8 -*-
"""
Imperiya Travel — Telegram bot + sayt (bitta fayl).
Bot ishga tushsa, sayt ham ishlaydi. Render uchun: BOT_TOKEN, ADMIN_ID kerak.
"""
import asyncio
import datetime
import hashlib
import hmac
import html
import json
import logging
import os
import platform
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import ClientSession, ClientTimeout, web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, MenuButtonWebApp, Message, ReplyKeyboardMarkup, WebAppInfo,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("imperiya")

TOKEN = os.getenv("BOT_TOKEN", "9060877636:AAEsRnliiV9Opul1aA5zb9dy5JNNlPAG2rA").strip()
ADMINS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_ID", "9027755231"))}
PORT = int(os.getenv("PORT", "10000"))
DATA_DIR = Path(os.getenv("DATA_DIR", ".")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "imperiya.db"
CACHE = DATA_DIR / "cache"
CACHE.mkdir(parents=True, exist_ok=True)
MAX_FILE = 20 * 1024 * 1024  # Telegram bot API limiti

bot: Bot = None  # main() ichida yaratiladi
BOT_USER = ""
TUNNEL_URL = ""
TUNNEL_ERR = ""

# ------------------------------------------------------------------ DB
LOCK = threading.RLock()
DB = sqlite3.connect(str(DB_PATH), check_same_thread=False)
DB.row_factory = sqlite3.Row

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, username TEXT, lang TEXT DEFAULT 'uz', joined INTEGER);
CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY AUTOINCREMENT, section TEXT, kind TEXT DEFAULT 'none',
  file_id TEXT, thumb_id TEXT, big INTEGER DEFAULT 0, title TEXT, place TEXT, price REAL, days INTEGER,
  date TEXT, descr TEXT, created INTEGER);
CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER, tg_id INTEGER,
  name TEXT, phone TEXT, pd TEXT, people INTEGER DEFAULT 1, note TEXT, status TEXT DEFAULT 'new', created INTEGER);
CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, text TEXT, created INTEGER);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
"""


def init_db():
    with LOCK:
        DB.executescript(SCHEMA)
        DB.commit()


def run(sql, args=()):
    with LOCK:
        cur = DB.execute(sql, args)
        DB.commit()
        return cur


def rows(sql, args=()):
    with LOCK:
        return [dict(r) for r in DB.execute(sql, args).fetchall()]


def row(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None


DEFAULTS = {
    "phone1": "+998 88 999 00 14",
    "phone2": "+998 95 090 00 15",
    "map_url": "https://maps.app.goo.gl/1RAuarP3N2u1HtgA9?g_st=it",
    "tg": "Imperialtravel_Andijon",
}


def gs(k, d=""):
    r = row("SELECT v FROM settings WHERE k=?", (k,))
    return r["v"] if r and r["v"] else DEFAULTS.get(k, d)


def ss(k, v):
    if v:
        run("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
    else:
        run("DELETE FROM settings WHERE k=?", (k,))


def backup_to(path):
    with LOCK:
        dst = sqlite3.connect(str(path))
        try:
            DB.backup(dst)
        finally:
            dst.close()


def restore_from(path):
    src = sqlite3.connect(str(path))
    try:
        names = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"items", "settings"} <= names:
            raise ValueError("Bu Imperiya Travel zaxira fayli emas")
        with LOCK:
            src.backup(DB)
    finally:
        src.close()
    init_db()


DEMO = [
    ("hot", "Antalya — issiq tur", "Turkiya, Antalya", 7),
    ("abroad", "Dubay va cho'l safari", "BAA, Dubay", 5),
    ("abroad", "Sharm-el-Sheyx dam olish", "Misr, Sharm-el-Sheyx", 7),
    ("umra", "Umra safari", "Makka va Madina", 10),
    ("domestic", "Samarqand va Buxoro", "O'zbekiston", 3),
    ("promo", "Erta bron chegirmasi", "Turkiya, Misr, BAA", None),
]


def seed():
    if gs("seeded"):
        return
    if not rows("SELECT id FROM items LIMIT 1"):
        for sec, title, place, days in DEMO:
            run("INSERT INTO items(section,kind,title,place,days,descr,created) VALUES(?,?,?,?,?,?,?)",
                (sec, "none", title, place, days,
                 "Batafsil ma'lumot va narx uchun operator bilan bog'laning.", int(time.time())))
    ss("seeded", "1")
    if not gs("tg"):
        ss("tg", "Imperialtravel_Andijon")


# ------------------------------------------------------------------ matnlar
LANGS = ("uz", "ru", "en")
SEC = {
    "hot": ("🔥", "Issiq turlar", "Горящие туры", "Hot tours"),
    "abroad": ("🌍", "Tashqi turizm", "Зарубежные туры", "International"),
    "umra": ("🕋", "Umra", "Умра", "Umrah"),
    "domestic": ("🏔", "Ichki turizm", "По Узбекистану", "Domestic"),
    "promo": ("🏷", "Aksiya", "Акции", "Deals"),
}


def sec_name(sec, lang):
    s = SEC.get(sec)
    if not s:
        return sec
    return f"{s[0]} {s[1 + LANGS.index(lang)]}"


TX = {
    "uz": {
        "welcome": "✈️ <b>Imperiya Travel</b> botiga xush kelibsiz!\nQuyidagi menyudan tanlang yoki saytni oching 👇",
        "b_site": "🌐 Sayt", "b_my": "🛒 Bronlarim", "b_about": "ℹ️ Biz haqimizda",
        "b_contact": "📞 Operator", "b_lang": "🌐 Til / Язык",
        "empty": "Bu bo'limda hozircha e'lon yo'q. Tez orada qo'shiladi.",
        "book": "🧳 Bron qilish", "open_site": "🌐 Saytda ochish",
        "ask_phone": "Telefon raqamingizni yuboring (tugmani bosing yoki raqamni yozing):",
        "share_phone": "📱 Raqamni yuborish", "cancel": "❌ Bekor qilish",
        "booked": "✅ Bron #{id} qabul qilindi: <b>{t}</b>\nMenejer tez orada bog'lanadi.",
        "bad_phone": "Raqam noto'g'ri. Qayta yuboring.",
        "my_none": "Sizda hali bronlar yo'q.", "my_title": "🛒 <b>Sizning bronlaringiz</b>",
        "st_new": "⏳ ko'rib chiqilmoqda", "st_ok": "✅ tasdiqlangan", "st_no": "❌ bekor qilingan",
        "bk_ok": "✅ Broningiz #{id} tasdiqlandi: <b>{t}</b>", "bk_no": "❌ Broningiz #{id} bekor qilindi: <b>{t}</b>",
        "about_def": "Imperiya Travel — Andijondagi sayohat agentligi. Turkiya, Misr, BAA, Yevropa, Osiyo, Umra va O'zbekiston bo'ylab turlar.",
        "hours": "🕘 Ish vaqti", "hours_d": "Du–Sha 09:00–19:00",
        "contact_t": "📞 <b>Operator bilan bog'lanish</b>", "write_tg": "✉️ Telegramda yozish",
        "show_loc": "📍 Lokatsiya", "site_none": "Sayt tez orada ishga tushadi. Hozircha bo'limlarni shu yerning o'zida ko'ring 👇",
        "lang_pick": "Tilni tanlang:", "lang_ok": "✅ Til o'zgardi.", "days": "kun",
        "reviews": "⭐ Mijozlar sharhlari",
    },
    "ru": {
        "welcome": "✈️ Добро пожаловать в бот <b>Imperiya Travel</b>!\nВыберите раздел в меню или откройте сайт 👇",
        "b_site": "🌐 Сайт", "b_my": "🛒 Мои брони", "b_about": "ℹ️ О нас",
        "b_contact": "📞 Оператор", "b_lang": "🌐 Til / Язык",
        "empty": "В этом разделе пока нет предложений. Скоро появятся.",
        "book": "🧳 Забронировать", "open_site": "🌐 Открыть на сайте",
        "ask_phone": "Отправьте номер телефона (нажмите кнопку или напишите номер):",
        "share_phone": "📱 Отправить номер", "cancel": "❌ Отмена",
        "booked": "✅ Бронь #{id} принята: <b>{t}</b>\nМенеджер скоро свяжется с вами.",
        "bad_phone": "Неверный номер. Отправьте ещё раз.",
        "my_none": "У вас пока нет броней.", "my_title": "🛒 <b>Ваши брони</b>",
        "st_new": "⏳ на рассмотрении", "st_ok": "✅ подтверждена", "st_no": "❌ отменена",
        "bk_ok": "✅ Ваша бронь #{id} подтверждена: <b>{t}</b>", "bk_no": "❌ Ваша бронь #{id} отменена: <b>{t}</b>",
        "about_def": "Imperiya Travel — турагентство в Андижане. Туры в Турцию, Египет, ОАЭ, Европу, Азию, Умра и по Узбекистану.",
        "hours": "🕘 Время работы", "hours_d": "Пн–Сб 09:00–19:00",
        "contact_t": "📞 <b>Связь с оператором</b>", "write_tg": "✉️ Написать в Telegram",
        "show_loc": "📍 Локация", "site_none": "Сайт скоро заработает. Пока смотрите разделы прямо здесь 👇",
        "lang_pick": "Выберите язык:", "lang_ok": "✅ Язык изменён.", "days": "дн.",
        "reviews": "⭐ Отзывы клиентов",
    },
    "en": {
        "welcome": "✈️ Welcome to the <b>Imperiya Travel</b> bot!\nPick a section from the menu or open the website 👇",
        "b_site": "🌐 Website", "b_my": "🛒 My bookings", "b_about": "ℹ️ About us",
        "b_contact": "📞 Operator", "b_lang": "🌐 Til / Язык",
        "empty": "No offers in this section yet. Coming soon.",
        "book": "🧳 Book now", "open_site": "🌐 Open on website",
        "ask_phone": "Send your phone number (tap the button or type it):",
        "share_phone": "📱 Share number", "cancel": "❌ Cancel",
        "booked": "✅ Booking #{id} received: <b>{t}</b>\nA manager will contact you soon.",
        "bad_phone": "Invalid number. Please send it again.",
        "my_none": "You have no bookings yet.", "my_title": "🛒 <b>Your bookings</b>",
        "st_new": "⏳ under review", "st_ok": "✅ confirmed", "st_no": "❌ cancelled",
        "bk_ok": "✅ Your booking #{id} is confirmed: <b>{t}</b>", "bk_no": "❌ Your booking #{id} was cancelled: <b>{t}</b>",
        "about_def": "Imperiya Travel is a travel agency in Andijan: tours to Turkey, Egypt, UAE, Europe, Asia, Umrah and across Uzbekistan.",
        "hours": "🕘 Working hours", "hours_d": "Mon–Sat 09:00–19:00",
        "contact_t": "📞 <b>Contact an operator</b>", "write_tg": "✉️ Message on Telegram",
        "show_loc": "📍 Location", "site_none": "The website will be online soon. For now, browse the sections right here 👇",
        "lang_pick": "Choose a language:", "lang_ok": "✅ Language changed.", "days": "days",
        "reviews": "⭐ Customer reviews",
    },
}
for _l in LANGS:
    for _k, _v in SEC.items():
        TX[_l]["b_" + _k] = sec_name(_k, _l)

BTN = {}  # tugma matni -> kalit
for _l in LANGS:
    for _k, _v in TX[_l].items():
        if _k.startswith("b_"):
            BTN[_v] = _k

esc = html.escape


def fmt_num(n):
    return f"{int(round(n)):,}".replace(",", " ")


def fmt_date(s):
    p = str(s).split("-")
    return f"{p[2]}.{p[1]}.{p[0]}" if len(p) == 3 else str(s)


# ------------------------------------------------------------------ yordamchilar
def manual_url():
    return (gs("site_url") or os.getenv("SITE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")


def site_url():
    u = manual_url() or TUNNEL_URL
    return u if u.startswith("https://") else ""


def lang_of(uid):
    r = row("SELECT lang FROM users WHERE id=?", (uid,))
    return r["lang"] if r and r["lang"] in LANGS else "uz"


def reg_user(u):
    name = (u.full_name or "")[:80]
    if row("SELECT id FROM users WHERE id=?", (u.id,)):
        run("UPDATE users SET name=?, username=? WHERE id=?", (name, u.username or "", u.id))
    else:
        code = (u.language_code or "uz")[:2]
        lang = "ru" if code == "ru" else "en" if code == "en" else "uz"
        run("INSERT INTO users(id,name,username,lang,joined) VALUES(?,?,?,?,?)",
            (u.id, name, u.username or "", lang, int(time.time())))


def menu_kb(lang):
    t = TX[lang]
    url = site_url()
    site_btn = KeyboardButton(text=t["b_site"], web_app=WebAppInfo(url=url)) if url else KeyboardButton(text=t["b_site"])
    K = KeyboardButton
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [site_btn],
        [K(text=t["b_hot"]), K(text=t["b_abroad"])],
        [K(text=t["b_umra"]), K(text=t["b_domestic"])],
        [K(text=t["b_promo"]), K(text=t["b_my"])],
        [K(text=t["b_about"]), K(text=t["b_contact"])],
        [K(text=t["b_lang"])],
    ])


def caption(it, lang):
    parts = [f"<b>{esc(it['title'] or '')}</b>"]
    if it["place"]:
        parts.append("🧭 " + esc(it["place"]))
    meta = []
    if it["price"] is not None:
        meta.append("💵 $" + fmt_num(it["price"]))
    if it["days"]:
        meta.append(f"🗓 {it['days']} {TX[lang]['days']}")
    if it["date"]:
        meta.append("✈️ " + fmt_date(it["date"]))
    if meta:
        parts.append(" • ".join(meta))
    if it["descr"]:
        parts.append(esc(it["descr"]))
    return "\n".join(parts)


async def send_item(chat_id, it, lang):
    t = TX[lang]
    kb_rows = [[InlineKeyboardButton(text=t["book"], callback_data=f"book:{it['id']}")]]
    url = site_url()
    if url:
        kb_rows.append([InlineKeyboardButton(text=t["open_site"], web_app=WebAppInfo(url=url))])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    cap = caption(it, lang)
    try:
        if it["kind"] == "video" and it["file_id"]:
            await bot.send_video(chat_id, it["file_id"], caption=cap, reply_markup=kb)
        elif it["kind"] == "photo" and it["file_id"]:
            await bot.send_photo(chat_id, it["file_id"], caption=cap, reply_markup=kb)
        else:
            await bot.send_message(chat_id, cap, reply_markup=kb)
    except Exception as e:  # media yaroqsiz bo'lsa ham matn boradi
        log.warning("send_item fallback: %s", e)
        await bot.send_message(chat_id, cap, reply_markup=kb)


async def notify_admins(text, kb=None):
    for a in ADMINS:
        try:
            await bot.send_message(a, text, reply_markup=kb)
        except Exception as e:
            log.warning("admin %s ga yuborilmadi: %s", a, e)


def create_booking(item_id, tg_id, name, phone, people, note):
    pd = re.sub(r"\D", "", phone)[-9:]
    cur = run("INSERT INTO bookings(item_id,tg_id,name,phone,pd,people,note,created) VALUES(?,?,?,?,?,?,?,?)",
              (item_id, tg_id, name, phone, pd, people, note, int(time.time())))
    return cur.lastrowid


def bk_kb(bid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"bk:ok:{bid}"),
        InlineKeyboardButton(text="❌ Bekor", callback_data=f"bk:no:{bid}"),
    ]])


def bk_text(b):
    st = {"new": "🆕 Yangi", "ok": "✅ Tasdiqlangan", "no": "❌ Bekor"}.get(b["status"], b["status"])
    s = (f"{st} bron #{b['id']}\n🧳 <b>{esc(b.get('title') or '—')}</b>\n"
         f"👤 {esc(b['name'] or '')}\n📞 {esc(b['phone'] or '')}\n👥 {b['people']} kishi")
    if b["note"]:
        s += f"\n📝 {esc(b['note'])}"
    if b["tg_id"]:
        s += f"\n🆔 <a href=\"tg://user?id={b['tg_id']}\">Telegram profil</a>"
    return s


def get_booking(bid):
    return row("SELECT b.*, i.title FROM bookings b LEFT JOIN items i ON i.id=b.item_id WHERE b.id=?", (bid,))


# ------------------------------------------------------------------ routerlar
common = Router()
admin = Router()
user = Router()
admin.message.filter(F.from_user.id.in_(ADMINS))
admin.callback_query.filter(F.from_user.id.in_(ADMINS))


class Book(StatesGroup):
    phone = State()


class Add(StatesGroup):
    media = State()
    info = State()


class SetV(StatesGroup):
    val = State()


class Rev(StatesGroup):
    text = State()


class Bc(StatesGroup):
    msg = State()


# ---- umumiy
@common.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext, command: CommandObject):
    await state.clear()
    reg_user(m.from_user)
    lang = lang_of(m.from_user.id)
    text = TX[lang]["welcome"]
    if m.from_user.id in ADMINS:
        text += "\n\n🛠 /admin"
    await m.answer(text, reply_markup=menu_kb(lang))
    p = command.args or ""
    if p.startswith("item_") and p[5:].isdigit():
        it = row("SELECT * FROM items WHERE id=?", (int(p[5:]),))
        if it:
            await send_item(m.chat.id, it, lang)


@common.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    lang = lang_of(m.from_user.id)
    await m.answer("👌", reply_markup=menu_kb(lang))


@common.message(Command("myid"))
async def cmd_myid(m: Message):
    await m.answer(f"ID: <code>{m.from_user.id}</code>")


# ---- admin
def admin_kb():
    B = InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [B(text="➕ E'lon qo'shish", callback_data="adm:add"), B(text="🗑 O'chirish", callback_data="adm:del")],
        [B(text="📋 Bronlar", callback_data="adm:bk"), B(text="⚙️ Sozlamalar", callback_data="adm:set")],
        [B(text="⭐ Sharhlar", callback_data="adm:rev"), B(text="📣 Xabar yuborish", callback_data="adm:bc")],
        [B(text="📊 Statistika", callback_data="adm:st"), B(text="💾 Zaxira", callback_data="adm:bak")],
    ])


def sec_kb(prefix):
    B = InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[[B(text=sec_name(k, "uz"), callback_data=f"{prefix}:{k}")] for k in SEC])


@admin.message(Command("admin", "settings"))
async def adm_panel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🛠 <b>Admin panel</b>", reply_markup=admin_kb())


@admin.message(Command("setsite"))
async def adm_setsite(m: Message, command: CommandObject):
    u = (command.args or "").strip().rstrip("/")
    if not u.startswith("https://"):
        cur = site_url() or "ulanmagan"
        return await m.answer(f"Hozirgi sayt: {esc(cur)}\n\nFoydalanish: <code>/setsite https://SIZNING-NOM.onrender.com</code>")
    ss("site_url", u)
    await set_menu_button()
    await m.answer(f"✅ Sayt ulandi: {esc(u)}\nMijozlar /start bosganda yangi menyu chiqadi.", reply_markup=menu_kb(lang_of(m.from_user.id)))


@admin.message(Command("sayt"))
async def adm_sayt(m: Message):
    u = site_url()
    txt = f"🌐 Sayt: {esc(u)}" if u else "🌐 Sayt manzili hali yo'q."
    if not u and TUNNEL_ERR:
        txt += f"\n⚠️ {esc(TUNNEL_ERR[:600])}"
    elif not u:
        txt += "\nManzil yaratilmoqda, 1–2 daqiqa kuting va qayta yozing."
    await m.answer(txt)


@admin.message(Command("backup"))
async def adm_backup_cmd(m: Message):
    await send_backup(m.chat.id)


@admin.callback_query(F.data == "adm:bak")
async def adm_backup_cb(cb: CallbackQuery):
    await cb.answer()
    await send_backup(cb.from_user.id)


async def send_backup(chat_id):
    tmp = Path(tempfile.gettempdir()) / "imperiya_backup.db"
    backup_to(tmp)
    await bot.send_document(chat_id, FSInputFile(str(tmp), filename="imperiya_backup.db"),
                            caption="💾 Zaxira. Tiklash: shu faylni caption'da <code>/restore</code> bilan botga yuboring.")


@admin.message(F.document, F.caption.startswith("/restore"))
async def adm_restore(m: Message):
    tmp = Path(tempfile.gettempdir()) / "imperiya_restore.db"
    try:
        await bot.download(m.document, destination=str(tmp))
        restore_from(tmp)
        await m.answer("✅ Ma'lumotlar tiklandi.", reply_markup=admin_kb())
    except Exception as e:
        await m.answer(f"❌ Tiklanmadi: {esc(str(e))}")


@admin.callback_query(F.data == "adm:st")
async def adm_stats(cb: CallbackQuery):
    await cb.answer()
    u = row("SELECT COUNT(*) c FROM users")["c"]
    i = row("SELECT COUNT(*) c FROM items")["c"]
    b = {r["status"]: r["c"] for r in rows("SELECT status, COUNT(*) c FROM bookings GROUP BY status")}
    await cb.message.answer(
        f"📊 <b>Statistika</b>\n👥 Foydalanuvchilar: {u}\n🧳 E'lonlar: {i}\n"
        f"🆕 Yangi bronlar: {b.get('new', 0)}\n✅ Tasdiqlangan: {b.get('ok', 0)}\n❌ Bekor: {b.get('no', 0)}\n"
        f"🌐 Sayt: {esc(site_url() or 'ulanmagan — /setsite')}")


# qo'shish
@admin.callback_query(F.data == "adm:add")
async def add_start(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer("Qaysi bo'limga qo'shamiz?", reply_markup=sec_kb("adds"))


@admin.callback_query(F.data.startswith("adds:"))
async def add_sec(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Add.media)
    await state.update_data(sec=cb.data.split(":")[1])
    await cb.message.answer("🎬 Video yoki 🖼 rasm yuboring.\nMedia kerak bo'lmasa: /skip\nBekor qilish: /cancel")


async def _ask_info(m: Message, state: FSMContext, kind, file_id, thumb_id, big):
    await state.update_data(kind=kind, file_id=file_id, thumb_id=thumb_id, big=big)
    await state.set_state(Add.info)
    await m.answer(
        "Endi ma'lumotni <b>bitta xabarda</b> yuboring:\n"
        "<code>Sarlavha | Joy | Narx($) | Kun | Sana | Tavsif</code>\n\n"
        "Misol:\n<code>Antalya 5★ | Turkiya, Antalya | 650 | 7 | 25.12.2026 | Hammasi kiritilgan</code>\n\n"
        "Keraksiz joyga <code>-</code> yozing. Faqat sarlavha ham bo'ladi.")


@admin.message(Add.media, F.video)
async def add_video(m: Message, state: FSMContext):
    v = m.video
    await _ask_info(m, state, "video", v.file_id, v.thumbnail.file_id if v.thumbnail else None,
                    1 if (v.file_size or 0) > MAX_FILE else 0)


@admin.message(Add.media, F.photo)
async def add_photo(m: Message, state: FSMContext):
    await _ask_info(m, state, "photo", m.photo[-1].file_id, None, 0)


@admin.message(Add.media, F.text)
async def add_skip(m: Message, state: FSMContext):
    if m.text.strip().lower().startswith("/skip"):
        await _ask_info(m, state, "none", None, None, 0)
    else:
        await m.answer("Video yoki rasm yuboring, yoki /skip. Bekor: /cancel")


def parse_date(s):
    s = s.strip()
    m1 = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", s)
    m2 = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    try:
        if m1:
            d, mo, y = map(int, m1.groups())
        elif m2:
            y, mo, d = map(int, m2.groups())
        else:
            return None
        return datetime.date(y, mo, d).isoformat()
    except ValueError:
        return None


def parse_info(text):
    p = [x.strip() for x in text.split("|", 5)]
    p += [""] * (6 - len(p))
    clean = lambda x: "" if x == "-" else x
    title, place, price, days, date, descr = [clean(x) for x in p]
    pr = re.sub(r"[^\d.]", "", price.replace(",", "."))
    try:
        price_v = float(pr) if pr else None
    except ValueError:
        price_v = None
    dd = re.sub(r"\D", "", days)
    return title[:100], place[:80], price_v, int(dd) if dd else None, parse_date(date), descr[:600]


@admin.message(Add.info, F.text)
async def add_info(m: Message, state: FSMContext):
    title, place, price, days, date, descr = parse_info(m.text)
    if not title:
        return await m.answer("Sarlavha kerak. Qayta yuboring yoki /cancel")
    d = await state.get_data()
    run("INSERT INTO items(section,kind,file_id,thumb_id,big,title,place,price,days,date,descr,created) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (d.get("sec", "hot"), d.get("kind", "none"), d.get("file_id"), d.get("thumb_id"), d.get("big", 0),
         title, place, price, days, date, descr, int(time.time())))
    await state.clear()
    note = ""
    if d.get("big"):
        note = "\n⚠️ Video 20 MB dan katta: saytda o'ynamaydi, saytda «Botda ochish» tugmasi chiqadi."
    await m.answer("✅ Qo'shildi! Saytda ham ko'rinadi." + note, reply_markup=admin_kb())


# o'chirish
@admin.callback_query(F.data == "adm:del")
async def del_start(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer("Qaysi bo'limdan o'chiramiz?", reply_markup=sec_kb("dels"))


async def show_del_list(cb: CallbackQuery, sec):
    items = rows("SELECT id,title FROM items WHERE section=? ORDER BY id DESC LIMIT 20", (sec,))
    if not items:
        return await cb.message.answer("Bu bo'limda e'lon yo'q.", reply_markup=admin_kb())
    B = InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[[B(text=f"🗑 {i['title'][:40]}", callback_data=f"deli:{i['id']}")] for i in items])
    await cb.message.answer("O'chirish uchun bosing:", reply_markup=kb)


@admin.callback_query(F.data.startswith("dels:"))
async def del_sec(cb: CallbackQuery):
    await cb.answer()
    await show_del_list(cb, cb.data.split(":")[1])


@admin.callback_query(F.data.startswith("deli:"))
async def del_item(cb: CallbackQuery):
    iid = int(cb.data.split(":")[1])
    it = row("SELECT section FROM items WHERE id=?", (iid,))
    run("DELETE FROM items WHERE id=?", (iid,))
    await cb.answer("O'chirildi")
    if it:
        await show_del_list(cb, it["section"])


# bronlar
@admin.callback_query(F.data == "adm:bk")
async def adm_bookings(cb: CallbackQuery):
    await cb.answer()
    rs = rows("SELECT id FROM bookings ORDER BY id DESC LIMIT 10")
    if not rs:
        return await cb.message.answer("Hozircha bronlar yo'q.")
    for r in reversed(rs):
        b = get_booking(r["id"])
        await cb.message.answer(bk_text(b), reply_markup=bk_kb(b["id"]) if b["status"] == "new" else None)


@admin.callback_query(F.data.startswith("bk:"))
async def bk_set(cb: CallbackQuery):
    _, act, bid = cb.data.split(":")
    status = "ok" if act == "ok" else "no"
    run("UPDATE bookings SET status=? WHERE id=?", (status, int(bid)))
    b = get_booking(int(bid))
    await cb.answer("Bajarildi")
    try:
        await cb.message.edit_text(bk_text(b), reply_markup=None)
    except Exception:
        pass
    if b and b["tg_id"]:
        lang = lang_of(b["tg_id"])
        try:
            await bot.send_message(b["tg_id"], TX[lang]["bk_" + status].format(id=b["id"], t=esc(b["title"] or "—")))
        except Exception as e:
            log.info("mijozga yuborilmadi: %s", e)


# sozlamalar
SETK = {
    "phone1": "📞 Telefon 1", "phone2": "📞 Telefon 2", "tg": "✉️ Telegram username",
    "hours": "🕘 Ish vaqti", "about": "ℹ️ Biz haqimizda matni", "license": "📜 Litsenziyalar",
    "map_url": "📍 Lokatsiya havolasi (Google Maps)",
    "loc": "📍 Lokatsiyani nuqta sifatida yuborish",
}


@admin.callback_query(F.data == "adm:set")
async def set_start(cb: CallbackQuery):
    await cb.answer()
    B = InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[[B(text=v, callback_data=f"set:{k}")] for k, v in SETK.items()])
    await cb.message.answer("Nimani o'zgartiramiz?\n(Sayt manzili: /setsite)", reply_markup=kb)


@admin.callback_query(F.data.startswith("set:"))
async def set_key(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    k = cb.data.split(":")[1]
    await state.set_state(SetV.val)
    await state.update_data(key=k)
    if k == "loc":
        await cb.message.answer("📎 → Location orqali joylashuvni yuboring. O'chirish: <code>-</code>")
    else:
        cur = gs(k)
        await cb.message.answer(f"{SETK.get(k, k)}\nHozirgi: {esc(cur) if cur else '—'}\n\nYangi qiymatni yuboring. O'chirish: <code>-</code>")


@admin.message(SetV.val, F.location)
async def set_loc(m: Message, state: FSMContext):
    d = await state.get_data()
    if d.get("key") != "loc":
        return await m.answer("Matn yuboring.")
    ss("lat", str(m.location.latitude))
    ss("lon", str(m.location.longitude))
    await state.clear()
    await m.answer("✅ Lokatsiya saqlandi (faqat «Operator» bo'limida ko'rinadi).", reply_markup=admin_kb())


@admin.message(SetV.val, F.text)
async def set_val(m: Message, state: FSMContext):
    d = await state.get_data()
    k = d.get("key")
    v = m.text.strip()
    if v == "-":
        v = ""
    if k == "loc":
        if v:
            return await m.answer("Lokatsiya yuboring yoki <code>-</code>")
        ss("lat", "")
        ss("lon", "")
    elif k == "map_url":
        if v and not v.startswith("https://"):
            return await m.answer("Havola https:// bilan boshlanishi kerak.")
        ss("map_url", v)
    elif k == "tg":
        v = re.sub(r"^(https?://)?(t\.me/)?@?", "", v).strip("/")[:64]
        ss("tg", v)
    else:
        ss(k, v[:1500])
    await state.clear()
    await m.answer("✅ Saqlandi.", reply_markup=admin_kb())


# sharhlar
@admin.callback_query(F.data == "adm:rev")
async def rev_menu(cb: CallbackQuery):
    await cb.answer()
    B = InlineKeyboardButton
    rs = rows("SELECT id,name FROM reviews ORDER BY id DESC LIMIT 15")
    kb = [[B(text="➕ Sharh qo'shish", callback_data="rev:add")]]
    kb += [[B(text=f"🗑 {r['name'][:30]}", callback_data=f"rev:del:{r['id']}")] for r in rs]
    await cb.message.answer("⭐ Mijozlar sharhlari (saytdagi «Biz haqimizda» bo'limida)", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@admin.callback_query(F.data == "rev:add")
async def rev_add(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Rev.text)
    await cb.message.answer("Yuboring: <code>Ism | Sharh matni</code>")


@admin.callback_query(F.data.startswith("rev:del:"))
async def rev_del(cb: CallbackQuery):
    run("DELETE FROM reviews WHERE id=?", (int(cb.data.split(":")[2]),))
    await cb.answer("O'chirildi")


@admin.message(Rev.text, F.text)
async def rev_save(m: Message, state: FSMContext):
    if "|" not in m.text:
        return await m.answer("Format: <code>Ism | Sharh matni</code>")
    name, text = [x.strip() for x in m.text.split("|", 1)]
    if not name or not text:
        return await m.answer("Ism va matn kerak.")
    run("INSERT INTO reviews(name,text,created) VALUES(?,?,?)", (name[:60], text[:500], int(time.time())))
    await state.clear()
    await m.answer("✅ Sharh qo'shildi.", reply_markup=admin_kb())


# xabar yuborish
@admin.callback_query(F.data == "adm:bc")
async def bc_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Bc.msg)
    await cb.message.answer("📣 Hammaga yuboriladigan xabarni yuboring (matn, rasm yoki video).\nBekor qilish: /cancel")


@admin.message(Bc.msg)
async def bc_send(m: Message, state: FSMContext):
    await state.clear()
    ids = [r["id"] for r in rows("SELECT id FROM users")]
    ok = fail = 0
    await m.answer(f"⏳ Yuborilmoqda: {len(ids)} ta foydalanuvchi...")
    for uid in ids:
        try:
            await bot.copy_message(uid, m.chat.id, m.message_id)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await m.answer(f"✅ Yuborildi: {ok}\n❌ Yetib bormadi: {fail}", reply_markup=admin_kb())


# ---- foydalanuvchi
@user.message(StateFilter(None), F.text)
async def on_text(m: Message, state: FSMContext):
    reg_user(m.from_user)
    uid = m.from_user.id
    lang = lang_of(uid)
    t = TX[lang]
    key = BTN.get(m.text)
    if key is None:
        return await m.answer(t["welcome"], reply_markup=menu_kb(lang))
    if key == "b_site":
        url = site_url()
        if url:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t["b_site"], web_app=WebAppInfo(url=url))]])
            await m.answer("👇", reply_markup=menu_kb(lang))
            return await m.answer(t["b_site"], reply_markup=kb)
        if uid in ADMINS:
            await m.answer("⚠️ Sayt manzili topilmadi.\nRender sahifasi tepasidagi https://....onrender.com havolani nusxalab yozing:\n"
                           "<code>/setsite https://SIZNING-NOM.onrender.com</code>"
                           + (f"\n\n⚠️ {esc(TUNNEL_ERR[:500])}" if TUNNEL_ERR else "\n\nAvtomatik manzil yaratilmoqda, 1–2 daqiqa kuting."))
        return await m.answer(t["site_none"])
    if key in ("b_" + k for k in SEC):
        sec = key[2:]
        items = rows("SELECT * FROM items WHERE section=? ORDER BY id DESC LIMIT 8", (sec,))
        await m.answer(f"<b>{sec_name(sec, lang)}</b>")
        if not items:
            return await m.answer(t["empty"])
        for it in items:
            await send_item(m.chat.id, it, lang)
        return
    if key == "b_my":
        bs = rows("SELECT b.*, i.title FROM bookings b LEFT JOIN items i ON i.id=b.item_id "
                  "WHERE b.tg_id=? ORDER BY b.id DESC LIMIT 10", (uid,))
        if not bs:
            return await m.answer(t["my_none"])
        lines = [t["my_title"]]
        for b in bs:
            lines.append(f"#{b['id']} — <b>{esc(b['title'] or '—')}</b>\n{t['st_' + b['status']]}")
        return await m.answer("\n\n".join(lines))
    if key == "b_about":
        txt = f"ℹ️ <b>Imperiya Travel</b>\n\n{esc(gs('about') or t['about_def'])}\n\n{t['hours']}: {esc(gs('hours') or t['hours_d'])}"
        lic = gs("license")
        if lic:
            txt += f"\n\n📜 {esc(lic)}"
        revs = rows("SELECT name,text FROM reviews ORDER BY id DESC LIMIT 5")
        if revs:
            txt += f"\n\n{t['reviews']}\n" + "\n".join(f"«{esc(r['text'])}» — {esc(r['name'])}" for r in revs)
        return await m.answer(txt)
    if key == "b_contact":
        lines = [t["contact_t"]]
        for k in ("phone1", "phone2"):
            if gs(k):
                lines.append("📞 " + esc(gs(k)))
        kb_rows = []
        tgu = gs("tg")
        if tgu:
            kb_rows.append([InlineKeyboardButton(text=t["write_tg"], url=f"https://t.me/{tgu}")])
        mu = gs("map_url")
        if mu.startswith("https://"):
            kb_rows.append([InlineKeyboardButton(text=t["show_loc"], url=mu)])
        elif gs("lat") and gs("lon"):
            kb_rows.append([InlineKeyboardButton(text=t["show_loc"], callback_data="sendloc")])
        return await m.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None)
    if key == "b_lang":
        B = InlineKeyboardButton
        kb = InlineKeyboardMarkup(inline_keyboard=[[B(text="🇺🇿 O'zbekcha", callback_data="lang:uz"),
                                                    B(text="🇷🇺 Русский", callback_data="lang:ru"),
                                                    B(text="🇬🇧 English", callback_data="lang:en")]])
        return await m.answer(t["lang_pick"], reply_markup=kb)


@user.callback_query(F.data.startswith("lang:"))
async def on_lang(cb: CallbackQuery):
    lang = cb.data.split(":")[1]
    if lang not in LANGS:
        return await cb.answer()
    reg_user(cb.from_user)
    run("UPDATE users SET lang=? WHERE id=?", (lang, cb.from_user.id))
    await cb.answer()
    await cb.message.answer(TX[lang]["lang_ok"], reply_markup=menu_kb(lang))


@user.callback_query(F.data == "sendloc")
async def on_sendloc(cb: CallbackQuery):
    await cb.answer()
    try:
        await bot.send_location(cb.from_user.id, float(gs("lat")), float(gs("lon")))
    except Exception as e:
        log.warning("lokatsiya: %s", e)


@user.callback_query(F.data.startswith("book:"))
async def on_book(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    iid = int(cb.data.split(":")[1])
    if not row("SELECT id FROM items WHERE id=?", (iid,)):
        return
    reg_user(cb.from_user)
    lang = lang_of(cb.from_user.id)
    t = TX[lang]
    await state.set_state(Book.phone)
    await state.update_data(item=iid)
    kb = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [KeyboardButton(text=t["share_phone"], request_contact=True)], [KeyboardButton(text=t["cancel"])]])
    await cb.message.answer(t["ask_phone"], reply_markup=kb)


async def finish_booking(m: Message, state: FSMContext, phone):
    lang = lang_of(m.from_user.id)
    t = TX[lang]
    if len(re.sub(r"\D", "", phone)) < 7:
        return await m.answer(t["bad_phone"])
    d = await state.get_data()
    it = row("SELECT * FROM items WHERE id=?", (d.get("item"),))
    await state.clear()
    if not it:
        return await m.answer(t["empty"], reply_markup=menu_kb(lang))
    bid = create_booking(it["id"], m.from_user.id, m.from_user.full_name or "", phone, 1, "")
    await m.answer(t["booked"].format(id=bid, t=esc(it["title"])), reply_markup=menu_kb(lang))
    b = get_booking(bid)
    await notify_admins(bk_text(b) + "\n🔗 Bot orqali", bk_kb(bid))


@user.message(Book.phone, F.contact)
async def book_contact(m: Message, state: FSMContext):
    await finish_booking(m, state, m.contact.phone_number)


@user.message(Book.phone, F.text)
async def book_text(m: Message, state: FSMContext):
    lang = lang_of(m.from_user.id)
    if m.text.strip() == TX[lang]["cancel"]:
        await state.clear()
        return await m.answer("👌", reply_markup=menu_kb(lang))
    await finish_booking(m, state, m.text.strip())


# ------------------------------------------------------------------ veb-server
def tg_user(init_data):
    """Telegram WebApp initData ni tekshiradi (HMAC). Haqiqiy bo'lsa foydalanuvchi dict qaytaradi."""
    try:
        d = dict(parse_qsl(init_data, keep_blank_values=True))
        h = d.pop("hash", "")
        data = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(key, data.encode(), hashlib.sha256).hexdigest()
        if not h or not hmac.compare_digest(calc, h):
            return None
        if time.time() - int(d.get("auth_date", "0")) > 7 * 86400:
            return None
        return json.loads(d["user"])
    except Exception:
        return None


HITS = {}


def limited(req, n=8, per=60):
    ip = (req.headers.get("X-Forwarded-For") or req.remote or "?").split(",")[0].strip()
    now = time.time()
    if len(HITS) > 5000:
        HITS.clear()
    lst = [x for x in HITS.get(ip, []) if now - x < per]
    if len(lst) >= n:
        HITS[ip] = lst
        return True
    lst.append(now)
    HITS[ip] = lst
    return False


async def jbody(req):
    try:
        d = await req.json()
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def cl(v, n):
    return str(v if v is not None else "").strip()[:n]


async def h_index(req):
    return web.Response(text=PAGE, content_type="text/html", charset="utf-8", headers={"Cache-Control": "no-cache"})


async def h_health(req):
    return web.Response(text="ok")


async def h_data(req):
    items = []
    for i in rows("SELECT * FROM items ORDER BY id DESC"):
        has = i["kind"] in ("video", "photo") and i["file_id"]
        thumb = ""
        if i["kind"] == "photo" and i["file_id"]:
            thumb = f"/media/{i['id']}"
        elif i["thumb_id"]:
            thumb = f"/media/{i['id']}/t"
        items.append({
            "id": i["id"], "sec": i["section"], "kind": i["kind"] if has else "none",
            "title": i["title"], "place": i["place"] or "", "price": i["price"], "days": i["days"],
            "date": i["date"] or "", "descr": i["descr"] or "", "big": bool(i["big"]),
            "media": f"/media/{i['id']}" if has and not i["big"] else "", "thumb": thumb,
        })
    cfg = {k: gs(k) for k in ("phone1", "phone2", "tg", "hours", "about", "license", "lat", "lon", "map_url")}
    cfg["bot"] = BOT_USER
    revs = [{"name": r["name"], "text": r["text"]} for r in rows("SELECT name,text FROM reviews ORDER BY id DESC LIMIT 20")]
    return web.json_response({"items": items, "reviews": revs, "cfg": cfg})


DL_LOCKS = {}


async def serve_media(req, thumb):
    try:
        iid = int(req.match_info["id"])
    except ValueError:
        raise web.HTTPNotFound()
    it = row("SELECT * FROM items WHERE id=?", (iid,))
    if not it:
        raise web.HTTPNotFound()
    fid = it["thumb_id"] if thumb else it["file_id"]
    if not fid or (not thumb and it["big"]):
        raise web.HTTPNotFound()
    ext = "jpg" if (thumb or it["kind"] == "photo") else "mp4"
    path = CACHE / f"{hashlib.md5(fid.encode()).hexdigest()}.{ext}"
    if not path.exists():
        lock = DL_LOCKS.setdefault(str(path), asyncio.Lock())
        async with lock:
            if not path.exists():
                tmp = path.with_suffix(".part")
                try:
                    await bot.download(fid, destination=str(tmp))
                    tmp.replace(path)
                except Exception as e:
                    log.warning("media yuklanmadi %s: %s", iid, e)
                    if tmp.exists():
                        tmp.unlink()
                    raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


async def h_media(req):
    return await serve_media(req, False)


async def h_thumb(req):
    return await serve_media(req, True)


async def h_book(req):
    if limited(req):
        return web.json_response({"ok": False}, status=429)
    d = await jbody(req)
    name, phone, note = cl(d.get("name"), 60), cl(d.get("phone"), 30), cl(d.get("note"), 300)
    try:
        iid = int(d.get("item_id"))
        people = max(1, min(50, int(d.get("people") or 1)))
    except (TypeError, ValueError):
        return web.json_response({"ok": False}, status=400)
    if len(name) < 2 or len(re.sub(r"\D", "", phone)) < 7 or not row("SELECT id FROM items WHERE id=?", (iid,)):
        return web.json_response({"ok": False}, status=400)
    u = tg_user(cl(d.get("initData"), 4096))
    bid = create_booking(iid, u["id"] if u else None, name, phone, people, note)
    b = get_booking(bid)
    await notify_admins(bk_text(b) + "\n🔗 Sayt orqali", bk_kb(bid))
    if u:
        lang = lang_of(u["id"])
        try:
            await bot.send_message(u["id"], TX[lang]["booked"].format(id=bid, t=esc(b["title"] or "—")))
        except Exception:
            pass
    return web.json_response({"ok": True, "id": bid})


async def h_my(req):
    if limited(req, n=20):
        return web.json_response({"items": []}, status=429)
    d = await jbody(req)
    u = tg_user(cl(d.get("initData"), 4096))
    base = "SELECT b.id,b.status,b.people,b.created,i.title FROM bookings b LEFT JOIN items i ON i.id=b.item_id WHERE "
    if u:
        rs = rows(base + "b.tg_id=? ORDER BY b.id DESC LIMIT 20", (u["id"],))
    else:
        pd = re.sub(r"\D", "", cl(d.get("phone"), 30))[-9:]
        rs = rows(base + "b.pd=? ORDER BY b.id DESC LIMIT 20", (pd,)) if len(pd) >= 7 else []
    return web.json_response({"items": [
        {"id": r["id"], "title": r["title"], "status": r["status"], "people": r["people"],
         "date": datetime.datetime.fromtimestamp(r["created"] or 0).strftime("%Y-%m-%d")} for r in rs]})


async def h_ask(req):
    if limited(req, n=4):
        return web.json_response({"ok": False}, status=429)
    d = await jbody(req)
    name, phone, text = cl(d.get("name"), 60), cl(d.get("phone"), 30), cl(d.get("text"), 600)
    if len(name) < 2 or len(re.sub(r"\D", "", phone)) < 7 or len(text) < 2:
        return web.json_response({"ok": False}, status=400)
    u = tg_user(cl(d.get("initData"), 4096))
    extra = f"\n🆔 <a href=\"tg://user?id={u['id']}\">Telegram profil</a>" if u else ""
    await notify_admins(f"❓ <b>Sayt: yangi savol</b>\n👤 {esc(name)}\n📞 {esc(phone)}\n\n{esc(text)}{extra}")
    return web.json_response({"ok": True})


def make_app():
    app = web.Application(client_max_size=1_000_000)
    app.add_routes([
        web.get("/", h_index), web.get("/health", h_health), web.get("/api/data", h_data),
        web.get("/media/{id}", h_media), web.get("/media/{id}/t", h_thumb),
        web.post("/api/book", h_book), web.post("/api/my", h_my), web.post("/api/ask", h_ask),
    ])
    return app


# ------------------------------------------------------------------ fon vazifalari
async def set_menu_button():
    url = site_url()
    if not url or bot is None:
        return
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🌐 Sayt", web_app=WebAppInfo(url=url)))
    except Exception as e:
        log.warning("menu tugmasi o'rnatilmadi: %s", e)


# ------------------------------------------------------------------ avtomatik https manzil (cloudflared)
def cf_asset():
    sysname = platform.system().lower()
    mach = platform.machine().lower()
    arch = "arm64" if ("arm" in mach or "aarch" in mach) else "amd64"
    if sysname == "windows":
        return f"cloudflared-windows-{arch}.exe", "cloudflared.exe"
    if sysname == "darwin":
        return None, None
    return f"cloudflared-linux-{arch}", "cloudflared"


async def get_cloudflared():
    found = shutil.which("cloudflared")
    if found:
        return found
    asset, local = cf_asset()
    if not asset:
        return None
    path = DATA_DIR / local
    if path.exists() and path.stat().st_size > 1_000_000:
        return str(path)
    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}"
    log.info("cloudflared yuklanmoqda (bir marta)...")
    tmp = path.with_suffix(".part")
    async with ClientSession(timeout=ClientTimeout(total=300)) as sess:
        async with sess.get(url) as r:
            if r.status != 200:
                raise RuntimeError(f"cloudflared yuklanmadi: HTTP {r.status}")
            with open(tmp, "wb") as f:
                async for chunk in r.content.iter_chunked(1 << 16):
                    f.write(chunk)
    tmp.replace(path)
    try:
        path.chmod(0o755)
    except Exception:
        pass
    return str(path)


async def tunnel_once(cmd, pat):
    """Bitta tunnel usulini ishga tushiradi. (manzil_topildimi, oxirgi_qatorlar) qaytaradi."""
    global TUNNEL_URL
    tail = []
    found = False
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    start = time.time()
    try:
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
            except asyncio.TimeoutError:
                if not found and time.time() - start > 90:
                    tail.append("90 soniyada manzil chiqmadi")
                    break
                continue
            if not line:
                break
            txt = line.decode("utf-8", "ignore").strip()
            if txt:
                tail = (tail + [txt])[-4:]
            m = re.search(pat, txt)
            if m:
                found = True
                if m.group(0) != TUNNEL_URL:
                    TUNNEL_URL = m.group(0)
                    log.info("sayt: %s", TUNNEL_URL)
                    await set_menu_button()
                    await refresh_menus()
    finally:
        if proc.returncode is None:
            try:
                proc.terminate()
            except Exception:
                pass
        if found:
            TUNNEL_URL = ""
    return found, " | ".join(tail)


async def tunnel_loop():
    """Bot o'zi internetdagi https manzil yaratadi (cloudflared, bo'lmasa ssh/localhost.run)."""
    global TUNNEL_ERR
    notified = False
    while True:
        errs = []
        attempts = []
        try:
            exe = await get_cloudflared()
            if exe:
                attempts.append(("cloudflared", [exe, "tunnel", "--url", f"http://localhost:{PORT}", "--no-autoupdate"],
                                 r"https://[a-z0-9-]+\.trycloudflare\.com"))
        except Exception as e:
            errs.append(f"cloudflared yuklanmadi: {e}")
        ssh = shutil.which("ssh")
        if ssh:
            attempts.append(("localhost.run", [ssh, "-o", "StrictHostKeyChecking=no", "-o", f"UserKnownHostsFile={os.devnull}",
                                               "-o", "ServerAliveInterval=30", "-o", "ExitOnForwardFailure=yes",
                                               "-R", f"80:localhost:{PORT}", "nokey@localhost.run"],
                             r"https://[a-z0-9-]+\.(?:lhr\.life|localhost\.run)"))
        ran = False
        for name, cmd, pat in attempts:
            try:
                ok, tail = await tunnel_once(cmd, pat)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                ok, tail = False, str(e)
            if ok:
                ran = True
                TUNNEL_ERR = ""
                notified = False
                log.warning("%s tunnel to'xtadi, qayta ulanadi...", name)
                break
            errs.append(name + ": " + (tail or "javob yoq"))
        if not ran:
            TUNNEL_ERR = "; ".join(errs) or "cloudflared va ssh topilmadi"
            log.warning("Avtomatik manzil yaratilmadi: %s", TUNNEL_ERR)
            if not notified:
                notified = True
                await notify_admins("⚠️ Sayt manzili yaratilmadi.\n" + esc(TUNNEL_ERR[:600]) +
                                    "\n\nVPN/antivirus/tarmoqni tekshiring yoki Render'da joylang (SITE_URL).")
        await asyncio.sleep(15 if ran else 60)


async def refresh_menus():
    """Manzil o'zgarsa, hammaning menyusi yangilanadi (eski tugma ishlamay qolmasin)."""
    url = site_url()
    if not url or gs("last_url") == url:
        return
    ss("last_url", url)
    await notify_admins(f"🌐 Sayt manzili: {esc(url)}")
    ids = [r["id"] for r in rows("SELECT id FROM users")]
    if len(ids) > 1000:
        return
    for uid in ids:
        try:
            await bot.send_message(uid, "🔄", reply_markup=menu_kb(lang_of(uid)))
        except Exception:
            pass
        await asyncio.sleep(0.05)


async def keepalive():
    await asyncio.sleep(60)
    while True:
        u = site_url()
        if u:
            try:
                async with ClientSession(timeout=ClientTimeout(total=20)) as s:
                    await s.get(u + "/health")
            except Exception:
                pass
        await asyncio.sleep(600)


async def daily_backup():
    await asyncio.sleep(900)
    while True:
        if ADMINS:
            try:
                await send_backup(sorted(ADMINS)[0])
            except Exception as e:
                log.warning("zaxira yuborilmadi: %s", e)
        await asyncio.sleep(86400)


async def main():
    global bot, BOT_USER
    if not TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi. Render → Environment bo'limiga BOT_TOKEN qo'shing.")
    if not ADMINS:
        log.warning("ADMIN_ID berilmagan — admin panel ishlamaydi!")
    init_db()
    seed()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_routers(common, admin, user)

    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    BOT_USER = (await bot.get_me()).username or ""
    await bot.delete_webhook(drop_pending_updates=True)
    await set_menu_button()
    tasks = [asyncio.create_task(keepalive()), asyncio.create_task(daily_backup())]
    if not manual_url():
        tasks.append(asyncio.create_task(tunnel_loop()))
    else:
        await refresh_menus()
    log.info("Bot ishga tushdi: @%s", BOT_USER)
    log.info("sayt: %s", site_url() or "avtomatik manzil yaratilmoqda, kuting...")
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
        await runner.cleanup()
        await bot.session.close()


PAGE = r'''<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Imperiya Travel</title>
<meta name="theme-color" content="#F6FAFF">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;800&family=Unbounded:wght@500;700;800&display=swap&subset=cyrillic,latin-ext" rel="stylesheet">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--paper:#F6FAFF;--ink:#0B2545;--sky:#8FD3FF;--sky2:#DDF1FF;--sun:#FF7A2F;--sun2:#FFB36B;--muted:#5B7089;--line:#D6E6F5;--r:22px}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{font-family:'Manrope',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;background:var(--paper);color:var(--ink);line-height:1.5;overflow-x:hidden;-webkit-tap-highlight-color:transparent}
body.lock{overflow:hidden}
h1,h2,h3,.brand{font-family:'Unbounded','Manrope',system-ui,sans-serif;line-height:1.18}
a{color:inherit}
button,input,select,textarea{font:inherit;color:inherit}
:focus-visible{outline:3px solid var(--sun);outline-offset:2px}
[hidden]{display:none!important}
.muted{color:var(--muted)}
.wrap{max-width:1080px;margin:auto;padding:0 18px}
section{padding:44px 0}
h2{font-size:clamp(21px,3.3vw,30px);font-weight:700;margin-bottom:6px}
.sub{color:var(--muted);margin-bottom:20px}

header{position:sticky;top:0;z-index:50;display:flex;align-items:center;gap:10px;padding:10px 16px;background:rgba(246,250,255,.85);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}
.brand{font-weight:800;font-size:15px;display:flex;align-items:center;gap:8px;margin-right:auto;text-decoration:none}
.brand svg{width:22px;height:22px;fill:var(--sun)}
.lang{display:flex;background:#fff;border:1px solid var(--line);border-radius:99px;padding:3px}
.lang button{border:0;background:none;font-weight:800;font-size:12px;padding:6px 10px;border-radius:99px;color:var(--muted);cursor:pointer}
.lang button.on{background:var(--ink);color:#fff}

.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;border:0;cursor:pointer;text-decoration:none;font-weight:800;font-size:14px;padding:12px 20px;border-radius:99px;background:var(--sun);color:#fff;box-shadow:0 10px 22px -10px rgba(255,122,47,.9);transition:transform .15s}
.btn:active{transform:scale(.97)}
.btn.ghost{background:#fff;color:var(--ink);border:1px solid var(--line);box-shadow:none}
.btn.dark{background:var(--ink);box-shadow:none}
.btn.sm{padding:8px 14px;font-size:13px}

.hero{position:relative;overflow:hidden;padding:34px 0 80px;background:linear-gradient(180deg,#CBE8FF 0%,#E7F5FF 58%,var(--paper) 100%)}
.hero-in{position:relative;z-index:3;display:grid;grid-template-columns:1.1fr .9fr;gap:30px;align-items:center}
.hero h1{font-size:clamp(28px,5.4vw,52px);font-weight:800;letter-spacing:-.02em}
.hero p{margin:14px 0 22px;color:#3F5873;max-width:46ch;font-size:16px}
.cta{display:flex;gap:10px;flex-wrap:wrap}
.cloud{position:absolute;z-index:1;font-size:var(--s,14px);width:10em;height:3.1em;background:#fff;border-radius:3em;opacity:.92;animation:drift linear infinite;left:0}
.cloud:before,.cloud:after{content:'';position:absolute;background:#fff;border-radius:50%}
.cloud:before{width:4.3em;height:4.3em;top:-2em;left:1.6em}
.cloud:after{width:3.1em;height:3.1em;top:-1.3em;left:5.2em}
@keyframes drift{from{transform:translateX(-240px)}to{transform:translateX(calc(100vw + 240px))}}
.plane{position:absolute;left:0;top:10%;width:46px;z-index:2;animation:fly 15s linear infinite;filter:drop-shadow(0 10px 6px rgba(11,37,69,.22))}
.plane svg{display:block;width:100%;fill:var(--ink);transform:rotate(90deg)}
@keyframes fly{0%{transform:translate(-90px,50px) rotate(-7deg)}50%{transform:translate(48vw,0) rotate(0)}100%{transform:translate(calc(100vw + 90px),38px) rotate(6deg)}}

.scene{perspective:900px}
.float{animation:bob 6s ease-in-out infinite}
@keyframes bob{50%{transform:translateY(-10px)}}
.ticket{position:relative;max-width:360px;margin:auto;background:#fff;border-radius:20px;box-shadow:0 34px 50px -26px rgba(11,37,69,.45);transform-style:preserve-3d;transform:rotateY(var(--ry,-12deg)) rotateX(var(--rx,7deg));transition:transform .18s ease-out}
.t-top{background:var(--ink);color:#fff;border-radius:20px 20px 0 0;padding:12px 18px;display:flex;justify-content:space-between;font-size:12px;font-weight:800}
.t-top svg{width:16px;height:16px;fill:var(--sun2);vertical-align:-3px}
.t-mid{padding:20px 18px 14px;display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:6px;transform:translateZ(34px)}
.code{font-family:'Unbounded',sans-serif;font-weight:800;font-size:34px;letter-spacing:-.02em}
.city{font-size:12px;color:var(--muted);font-weight:600}
.t-mid .to{text-align:right}
.t-mid svg{width:26px;height:26px;fill:var(--sun);transform:rotate(90deg)}
.swap{transition:opacity .3s,transform .3s}
.swap.out{opacity:0;transform:translateY(8px)}
.perf{position:relative;border-top:2px dashed var(--line);margin:4px 14px 0}
.perf:before,.perf:after{content:'';position:absolute;top:-11px;width:20px;height:20px;border-radius:50%;background:#E7F5FF}
.perf:before{left:-24px}.perf:after{right:-24px}
.t-bot{padding:14px 18px 18px;display:flex;justify-content:space-between;align-items:flex-end;gap:10px;transform:translateZ(18px)}
.t-bot .ph a{display:block;font-weight:800;font-size:14px;text-decoration:none}
.t-bot small{display:block;color:var(--muted);font-size:11px;font-weight:600}
.bar{width:92px;height:36px;background:repeating-linear-gradient(90deg,var(--ink) 0 2px,transparent 2px 4px,var(--ink) 4px 5px,transparent 5px 8px,var(--ink) 8px 11px,transparent 11px 13px);border-radius:3px}

.panel{background:#fff;border:1px solid var(--line);border-radius:var(--r);padding:18px;box-shadow:0 26px 40px -30px rgba(11,37,69,.5)}
.filters{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.filters label{display:block;font-size:12px;font-weight:800;color:var(--muted);margin-bottom:4px}
.filters .full{grid-column:1/-1}
input,select,textarea{width:100%;background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:11px 13px;font-size:15px}
textarea{resize:vertical;min-height:70px}
input[type=range]{padding:0;height:34px;accent-color:var(--sun);background:none;border:0}
.out{font-weight:800;color:var(--sun)}
.chips{display:flex;gap:8px;overflow-x:auto;padding:16px 2px 6px;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{flex:none;border:1px solid var(--line);background:#fff;padding:9px 15px;border-radius:99px;font-weight:800;font-size:13px;cursor:pointer;white-space:nowrap}
.chip.on{background:var(--ink);color:#fff;border-color:var(--ink)}
.count{font-size:13px;color:var(--muted);font-weight:600;margin:6px 2px 14px}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:18px}
.card{background:#fff;border:1px solid var(--line);border-radius:var(--r);overflow:hidden;cursor:pointer;display:flex;flex-direction:column;transition:transform .2s ease-out,box-shadow .2s;box-shadow:0 22px 34px -28px rgba(11,37,69,.6)}
.card:hover{box-shadow:0 34px 44px -28px rgba(11,37,69,.55)}
.m{position:relative;display:flex;align-items:center;justify-content:center;overflow:hidden}
.m img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.m .ico{font-size:54px;filter:drop-shadow(0 8px 8px rgba(0,0,0,.2))}
.m .play{position:absolute;right:10px;bottom:10px;width:34px;height:34px;border-radius:50%;background:rgba(255,255,255,.92);color:var(--ink);display:flex;align-items:center;justify-content:center;font-size:13px;padding-left:2px}
.cm{height:170px}
.cb{padding:14px 16px 16px;display:flex;flex-direction:column;gap:8px;flex:1}
.tag{font-size:12px;font-weight:800;color:var(--muted)}
.card h3{font-size:16px;font-weight:700}
.pl{font-size:13px;color:var(--muted)}
.meta{display:flex;gap:6px;flex-wrap:wrap}
.meta span{background:var(--sky2);border-radius:99px;padding:3px 10px;font-size:12px;font-weight:800}
.row{display:flex;justify-content:space-between;align-items:center;margin-top:auto;padding-top:6px;gap:8px}
.pr{font-family:'Unbounded',sans-serif;font-size:16px}
.empty{grid-column:1/-1;text-align:center;padding:40px 10px;color:var(--muted)}
.empty .btn{margin-top:12px}

#ringSec{background:linear-gradient(180deg,var(--paper),#EAF5FF 50%,var(--paper));overflow:hidden}
.stage{perspective:1100px;height:330px;margin-top:6px;touch-action:pan-y;cursor:grab;user-select:none;-webkit-user-select:none}
.ring{position:relative;width:100%;height:260px;margin-top:30px;transform-style:preserve-3d}
.rc{position:absolute;left:50%;top:0;width:190px;height:260px;margin-left:-95px;border-radius:20px;background:#fff;overflow:hidden;backface-visibility:hidden;-webkit-backface-visibility:hidden;box-shadow:0 20px 30px -16px rgba(11,37,69,.5);display:flex;flex-direction:column}
.rm{height:180px;flex:none}
.rt{padding:10px 12px;display:flex;flex-direction:column;gap:2px;font-size:13px}
.rt b{font-weight:800;line-height:1.25;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.rt span{color:var(--sun);font-weight:800}

.about{display:grid;grid-template-columns:1.2fr .8fr;gap:18px}
.about p{white-space:pre-wrap}
.revs{display:flex;gap:14px;overflow-x:auto;padding:6px 2px 14px;scroll-snap-type:x mandatory}
.rev{flex:none;width:min(300px,80vw);background:#fff;border:1px solid var(--line);border-radius:var(--r);padding:16px;scroll-snap-align:start}
.rev p{margin-bottom:8px}
.rev footer{font-weight:800;font-size:13px;color:var(--muted)}
.contact{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.btns{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}
form .f{margin-bottom:10px}
form label{display:block;font-size:12px;font-weight:800;color:var(--muted);margin-bottom:4px}
footer.end{text-align:center;color:var(--muted);font-size:13px;padding:30px 16px 40px}

.ov{position:fixed;inset:0;z-index:100;background:rgba(11,37,69,.55);display:flex;align-items:flex-end;justify-content:center;animation:fade .2s}
@keyframes fade{from{opacity:0}}
.md{background:var(--paper);width:100%;max-width:560px;max-height:92vh;overflow:auto;border-radius:26px 26px 0 0;animation:up .28s cubic-bezier(.2,.8,.2,1);position:relative;padding-bottom:max(18px,env(safe-area-inset-bottom))}
@keyframes up{from{transform:translateY(60px);opacity:0}}
@media(min-width:620px){.ov{align-items:center}.md{border-radius:26px}}
.x{position:absolute;right:12px;top:12px;z-index:3;width:36px;height:36px;border-radius:50%;border:0;background:rgba(255,255,255,.95);font-size:18px;cursor:pointer;box-shadow:0 4px 10px rgba(0,0,0,.18)}
.md .big{height:240px;border-radius:26px 26px 0 0}
.md video,.md .big img{width:100%;height:100%;object-fit:cover;display:block;background:#000}
.md .big video{position:absolute;inset:0}
.mb{padding:16px 18px 6px}
.mb h2{font-size:22px;margin-bottom:8px}
.mb .descr{white-space:pre-wrap;margin:10px 0}
.mb .price{font-family:'Unbounded',sans-serif;font-size:22px;color:var(--sun);margin:8px 0 14px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.ok{text-align:center;padding:30px 18px}
.ok .big-ico{font-size:54px}
.bk{background:#fff;border:1px solid var(--line);border-radius:16px;padding:12px 14px;margin-bottom:10px}
.bk b{display:block}
.st{display:inline-block;margin-top:6px;padding:3px 10px;border-radius:99px;font-size:12px;font-weight:800}
.st.new{background:#FFF1E3;color:#B25200}.st.ok{background:#DDF7EE;color:#0B7A5E}.st.no{background:#FFE5E8;color:#B3243C}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--ink);color:#fff;padding:12px 18px;border-radius:99px;font-weight:700;font-size:14px;z-index:200;animation:fade .2s;max-width:90vw}

@media(max-width:820px){
 .hero-in,.about,.contact{grid-template-columns:1fr}
 .filters{grid-template-columns:1fr 1fr}
 .scene{margin-top:6px}
}
@media(max-width:420px){.filters{grid-template-columns:1fr}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<header>
 <a class="brand" href="#top"><svg viewBox="0 0 24 24"><path d="M21 16v-2l-8-5V3.5c0-.83-.67-1.5-1.5-1.5S10 2.67 10 3.5V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5l8 2.5z"/></svg>Imperiya Travel</a>
 <button class="btn sm dark" id="myBtn" data-i18n="nav_my"></button>
 <div class="lang" id="langs"></div>
</header>

<div class="hero" id="top">
 <div class="cloud" style="--s:16px;top:8%;animation-duration:70s;animation-delay:-20s"></div>
 <div class="cloud" style="--s:10px;top:46%;animation-duration:50s;animation-delay:-8s;opacity:.7"></div>
 <div class="cloud" style="--s:20px;top:72%;animation-duration:90s;animation-delay:-55s;opacity:.8"></div>
 <div class="plane" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M21 16v-2l-8-5V3.5c0-.83-.67-1.5-1.5-1.5S10 2.67 10 3.5V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5l8 2.5z"/></svg></div>
 <div class="wrap hero-in">
  <div>
   <h1 data-i18n="h1"></h1>
   <p data-i18n="sub"></p>
   <div class="cta">
    <a class="btn" href="#tours" data-i18n="cta1"></a>
    <a class="btn ghost" href="#contact" data-i18n="cta2"></a>
   </div>
  </div>
  <div class="scene" id="scene">
   <div class="float">
    <div class="ticket" id="ticket">
     <div class="t-top"><span><svg viewBox="0 0 24 24"><path d="M21 16v-2l-8-5V3.5c0-.83-.67-1.5-1.5-1.5S10 2.67 10 3.5V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5l8 2.5z"/></svg> Imperiya Travel</span><span>Boarding pass</span></div>
     <div class="t-mid">
      <div><div class="code">AZN</div><div class="city" data-i18n="city"></div></div>
      <svg viewBox="0 0 24 24"><path d="M21 16v-2l-8-5V3.5c0-.83-.67-1.5-1.5-1.5S10 2.67 10 3.5V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5l8 2.5z"/></svg>
      <div class="to swap" id="dest"><div class="code" id="dCode">IST</div><div class="city" id="dCity">Istanbul</div></div>
     </div>
     <div class="perf"></div>
     <div class="t-bot"><div class="ph" id="tPhones"></div><div class="bar"></div></div>
    </div>
   </div>
  </div>
 </div>
</div>

<section id="ringSec" hidden>
 <div class="wrap"><h2 data-i18n="ring_h"></h2><p class="sub" data-i18n="ring_p"></p></div>
 <div class="stage" id="stage"><div class="ring" id="ring"></div></div>
</section>

<section id="tours">
 <div class="wrap">
  <h2 data-i18n="find_h"></h2>
  <div class="panel">
   <div class="filters">
    <div class="full"><label for="fq" data-i18n="f_q"></label><input id="fq" type="search" autocomplete="off" data-ph="f_qph"></div>
    <div><label for="fdate" data-i18n="f_date"></label><input id="fdate" type="date"></div>
    <div><label for="fdur" data-i18n="f_dur"></label><select id="fdur"></select></div>
    <div class="full" id="budBox"><label for="fmax"><span data-i18n="f_bud"></span>: <span class="out" id="fmaxOut"></span></label><input id="fmax" type="range" min="0" max="1" step="50" value="1"></div>
   </div>
  </div>
  <div class="chips" id="chips"></div>
  <div class="count" id="count"></div>
  <div class="grid" id="grid"></div>
 </div>
</section>

<section id="about">
 <div class="wrap">
  <h2 data-i18n="about_h"></h2>
  <div class="about">
   <div class="panel"><p id="aboutP"></p></div>
   <div class="panel">
    <h3 style="font-size:15px;margin-bottom:6px" data-i18n="hours_h"></h3>
    <p id="hours" style="margin-bottom:14px"></p>
    <div id="licBox" hidden><h3 style="font-size:15px;margin-bottom:6px" data-i18n="lic_h"></h3><p id="lic" style="white-space:pre-wrap"></p></div>
   </div>
  </div>
  <h3 style="font-size:17px;margin:26px 0 8px" data-i18n="rev_h"></h3>
  <div class="revs" id="revs"></div>
 </div>
</section>

<section id="contact">
 <div class="wrap">
  <h2 data-i18n="con_h"></h2>
  <p class="sub" data-i18n="con_p"></p>
  <div class="contact">
   <div class="panel"><div class="btns" id="cBtns" style="margin-top:0"></div></div>
   <div class="panel">
    <h3 style="font-size:16px;margin-bottom:10px" data-i18n="ask_h"></h3>
    <form id="askForm" novalidate>
     <div class="f"><input id="aName" maxlength="60" data-ph="name" autocomplete="name"></div>
     <div class="f"><input id="aPhone" type="tel" maxlength="30" data-ph="phone" autocomplete="tel"></div>
     <div class="f"><textarea id="aText" maxlength="600" data-ph="ask_ph"></textarea></div>
     <button class="btn" type="submit" data-i18n="ask_send"></button>
    </form>
   </div>
  </div>
 </div>
</section>
<footer class="end" data-i18n="foot"></footer>

<script>
(function(){
'use strict';
var $=function(s){return document.querySelector(s)};
var tg=(window.Telegram&&window.Telegram.WebApp)||null;
try{if(tg){tg.ready();tg.expand();}}catch(e){}
var initData=(tg&&tg.initData)||'';
var store={get:function(k){try{return localStorage.getItem(k)}catch(e){return null}},set:function(k,v){try{localStorage.setItem(k,v)}catch(e){}}};
var tgUser=(tg&&tg.initDataUnsafe&&tg.initDataUnsafe.user)||null;

var I18N={
uz:{nav_my:'Bronlarim',h1:'Dunyoni biz bilan kashf eting',sub:"Issiq turlar, Umra, Turkiya, Misr, BAA va O'zbekiston bo'ylab sayohatlar. Tanlang, bron qiling — qolganini menejer hal qiladi.",cta1:"Turlarni ko'rish",cta2:"Operator bilan bog'lanish",city:'Andijon',ring_h:'Issiq takliflar',ring_p:'Aylantiring va yoqqanini tanlang',find_h:'Tur qidirish',f_q:'Davlat yoki shahar',f_qph:'Masalan: Turkiya, Dubay',f_date:'Sanadan boshlab',f_dur:'Muddat',f_bud:'Byudjet (gacha)',any:'Hammasi',d_a:'1–5 kun',d_b:'6–10 kun',d_c:'10 kundan ko\'p',s_all:'Hammasi',s_hot:'Issiq turlar',s_abroad:'Tashqi turizm',s_umra:'Umra',s_domestic:'Ichki turizm',s_promo:'Aksiya',found:'Topildi:',none:'Hech narsa topilmadi. Filtrlarni o\'zgartiring.',reset:'Filtrlarni tozalash',days:'kun',book:'Bron qilish',ask_price:'Narxni so\'rang',name:'Ismingiz',phone:'Telefon raqam',people:'Necha kishi',note:'Izoh (ixtiyoriy)',send_book:'Bronni yuborish',booked_h:'Bron qabul qilindi!',booked_p:'Menejer tez orada siz bilan bog\'lanadi.',err:'Xatolik. Qayta urinib ko\'ring.',fill:'Ism va telefon raqamni to\'ldiring.',big:'Video hajmi katta — Telegram botda ko\'ring',open_bot:'Botda ochish',my_h:'Bronlarim',my_none:'Hozircha bronlar yo\'q.',my_phone:'Bron qilgan telefon raqamingiz',my_show:'Ko\'rsatish',st_new:'Ko\'rib chiqilmoqda',st_ok:'Tasdiqlangan',st_no:'Bekor qilingan',about_h:'Biz haqimizda',about_p:'Imperiya Travel — Andijondagi sayohat agentligi. Turkiya, Misr, BAA, Yevropa, Osiyo yo\'nalishlari, Umra va O\'zbekiston bo\'ylab turlarni tashkil qilamiz. Hujjatlar, aviachiptalar va mehmonxonalar bilan yordam beramiz.',hours_h:'Ish vaqti',hours_d:'Du–Sha 09:00–19:00',lic_h:'Litsenziyalar',rev_h:'Mijozlar sharhlari',rev_none:'Sharhlar tez orada qo\'shiladi.',con_h:'Operator bilan bog\'lanish',con_p:'Savol bering yoki menejerga yozing.',ask_h:'Savol bering',ask_ph:'Savolingiz',ask_send:'Yuborish',ask_ok:'Savol yuborildi. Tez orada javob beramiz.',map:'Xaritada ochish',tg:'Telegramda yozish',close:'Yopish',foot:'© Imperiya Travel, Andijon'},
ru:{nav_my:'Мои брони',h1:'Откройте мир вместе с нами',sub:'Горящие туры, Умра, Турция, Египет, ОАЭ и поездки по Узбекистану. Выберите тур, забронируйте — остальное сделает менеджер.',cta1:'Смотреть туры',cta2:'Связаться с оператором',city:'Андижан',ring_h:'Горящие предложения',ring_p:'Прокрутите и выберите',find_h:'Поиск тура',f_q:'Страна или город',f_qph:'Например: Турция, Дубай',f_date:'Начиная с даты',f_dur:'Длительность',f_bud:'Бюджет (до)',any:'Любая',d_a:'1–5 дней',d_b:'6–10 дней',d_c:'Более 10 дней',s_all:'Все',s_hot:'Горящие туры',s_abroad:'Зарубежные туры',s_umra:'Умра',s_domestic:'По Узбекистану',s_promo:'Акции',found:'Найдено:',none:'Ничего не найдено. Измените фильтры.',reset:'Сбросить фильтры',days:'дн.',book:'Забронировать',ask_price:'Цена по запросу',name:'Ваше имя',phone:'Номер телефона',people:'Сколько человек',note:'Комментарий (необязательно)',send_book:'Отправить бронь',booked_h:'Бронь принята!',booked_p:'Менеджер скоро свяжется с вами.',err:'Ошибка. Попробуйте ещё раз.',fill:'Заполните имя и телефон.',big:'Видео большое — смотрите в Telegram-боте',open_bot:'Открыть в боте',my_h:'Мои брони',my_none:'Броней пока нет.',my_phone:'Телефон, указанный при брони',my_show:'Показать',st_new:'На рассмотрении',st_ok:'Подтверждена',st_no:'Отменена',about_h:'О нас',about_p:'Imperiya Travel — туристическое агентство в Андижане. Организуем туры в Турцию, Египет, ОАЭ, Европу и Азию, Умру и поездки по Узбекистану. Помогаем с документами, авиабилетами и отелями.',hours_h:'Время работы',hours_d:'Пн–Сб 09:00–19:00',lic_h:'Лицензии',rev_h:'Отзывы клиентов',rev_none:'Отзывы скоро появятся.',con_h:'Связаться с оператором',con_p:'Задайте вопрос или напишите менеджеру.',ask_h:'Задайте вопрос',ask_ph:'Ваш вопрос',ask_send:'Отправить',ask_ok:'Вопрос отправлен. Скоро ответим.',map:'Открыть на карте',tg:'Написать в Telegram',close:'Закрыть',foot:'© Imperiya Travel, Андижан'},
en:{nav_my:'My bookings',h1:'Discover the world with us',sub:'Hot tours, Umrah, Turkey, Egypt, UAE and trips across Uzbekistan. Pick a tour, book it — the manager handles the rest.',cta1:'Browse tours',cta2:'Contact an operator',city:'Andijan',ring_h:'Hot offers',ring_p:'Spin it and pick your favourite',find_h:'Find a tour',f_q:'Country or city',f_qph:'e.g. Turkey, Dubai',f_date:'Starting from',f_dur:'Duration',f_bud:'Budget (up to)',any:'Any',d_a:'1–5 days',d_b:'6–10 days',d_c:'Over 10 days',s_all:'All',s_hot:'Hot tours',s_abroad:'International',s_umra:'Umrah',s_domestic:'Domestic',s_promo:'Deals',found:'Found:',none:'Nothing found. Try changing the filters.',reset:'Clear filters',days:'days',book:'Book now',ask_price:'Price on request',name:'Your name',phone:'Phone number',people:'Number of people',note:'Note (optional)',send_book:'Send booking',booked_h:'Booking received!',booked_p:'A manager will contact you soon.',err:'Something went wrong. Please try again.',fill:'Please enter your name and phone.',big:'Large video — watch it in the Telegram bot',open_bot:'Open in bot',my_h:'My bookings',my_none:'No bookings yet.',my_phone:'Phone number used for booking',my_show:'Show',st_new:'Under review',st_ok:'Confirmed',st_no:'Cancelled',about_h:'About us',about_p:'Imperiya Travel is a travel agency in Andijan. We arrange tours to Turkey, Egypt, UAE, Europe and Asia, Umrah and trips across Uzbekistan, and help with documents, flights and hotels.',hours_h:'Working hours',hours_d:'Mon–Sat 09:00–19:00',lic_h:'Licenses',rev_h:'Customer reviews',rev_none:'Reviews are coming soon.',con_h:'Contact an operator',con_p:'Ask a question or message a manager.',ask_h:'Ask a question',ask_ph:'Your question',ask_send:'Send',ask_ok:'Question sent. We will reply soon.',map:'Open on map',tg:'Message on Telegram',close:'Close',foot:'© Imperiya Travel, Andijan'}
};
var lang=store.get('lang');
if(['uz','ru','en'].indexOf(lang)<0){var lc=String((tgUser&&tgUser.language_code)||navigator.language||'uz').slice(0,2);lang=lc==='ru'?'ru':(lc==='en'?'en':'uz');}
function t(k){return (I18N[lang]&&I18N[lang][k])||I18N.uz[k]||k}

var SEC=['hot','abroad','umra','domestic','promo'];
var ICO={hot:'🔥',abroad:'🌍',umra:'🕋',domestic:'🏔',promo:'🏷'};
var GR={hot:'linear-gradient(135deg,#FF7A2F,#FFC46B)',abroad:'linear-gradient(135deg,#2E7BFF,#8FD3FF)',umra:'linear-gradient(135deg,#0F9D8A,#9FE8D5)',domestic:'linear-gradient(135deg,#7B5CFF,#C9BBFF)',promo:'linear-gradient(135deg,#FF4D79,#FFB3C6)'};
var DATA={items:[],reviews:[],cfg:{}};
var S={sec:'all',q:'',date:'',dur:'any',max:0};
var MAXP=0;

function el(tag,attrs){
 var e=document.createElement(tag),a=attrs||{};
 for(var k in a){var v=a[k];
  if(k==='class')e.className=v;
  else if(k==='text')e.textContent=v;
  else if(k.indexOf('on')===0)e.addEventListener(k.slice(2),v);
  else if(v!==false&&v!=null)e.setAttribute(k,v);}
 function add(c){
  if(c==null||c===false)return;
  if(Array.isArray(c)){c.forEach(add);return;}
  e.appendChild(c.nodeType?c:document.createTextNode(String(c)));}
 for(var i=2;i<arguments.length;i++)add(arguments[i]);
 return e;
}
function fmt(n){return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g,' ')}
function priceText(i){return i.price!=null?'$'+fmt(i.price):t('ask_price')}
function fdate(s){var p=String(s).split('-');return p.length===3?p[2]+'.'+p[1]+'.'+p[0]:s}
function toast(msg){var n=el('div',{class:'toast',text:msg});document.body.appendChild(n);setTimeout(function(){n.remove()},3200)}
function post(url,body){
 return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(function(r){return r.json()});
}
function openLink(a){
 a.addEventListener('click',function(e){
  var h=a.getAttribute('href')||'';
  if(tg&&h.indexOf('https://t.me/')===0&&tg.openTelegramLink){e.preventDefault();tg.openTelegramLink(h);}
 });
 return a;
}

/* ---------- media ---------- */
function media(i,cls){
 var box=el('div',{class:'m '+cls});
 box.style.background=GR[i.sec]||GR.hot;
 if(i.thumb)box.appendChild(el('img',{src:i.thumb,alt:'',loading:'lazy'}));
 else box.appendChild(el('span',{class:'ico',text:ICO[i.sec]||'✈️'}));
 if(i.kind==='video')box.appendChild(el('span',{class:'play',text:'▶'}));
 return box;
}

/* ---------- cards ---------- */
function chip(x){return el('span',{text:x})}
function tilt(c){
 if(!window.matchMedia||!matchMedia('(hover:hover)').matches)return;
 c.addEventListener('pointermove',function(e){
  var r=c.getBoundingClientRect(),x=(e.clientX-r.left)/r.width-.5,y=(e.clientY-r.top)/r.height-.5;
  c.style.transform='perspective(800px) rotateY('+(x*10)+'deg) rotateX('+(-y*10)+'deg) translateY(-4px)';
 });
 c.addEventListener('pointerleave',function(){c.style.transform=''});
}
function card(i){
 var c=el('article',{class:'card'},
  media(i,'cm'),
  el('div',{class:'cb'},
   el('span',{class:'tag',text:(ICO[i.sec]||'')+' '+t('s_'+i.sec)}),
   el('h3',{text:i.title}),
   i.place?el('p',{class:'pl',text:'🧭 '+i.place}):null,
   el('div',{class:'meta'},i.days?chip(i.days+' '+t('days')):null,i.date?chip(fdate(i.date)):null),
   el('div',{class:'row'},el('b',{class:'pr',text:priceText(i)}),el('button',{class:'btn sm',type:'button',text:t('book'),onclick:function(e){e.stopPropagation();openItem(i)}}))
  ));
 c.addEventListener('click',function(){openItem(i)});
 tilt(c);
 return c;
}

/* ---------- filters ---------- */
function match(i){
 if(S.sec!=='all'&&i.sec!==S.sec)return false;
 if(S.q){var s=(i.title+' '+(i.place||'')+' '+(i.descr||'')).toLowerCase();if(s.indexOf(S.q.toLowerCase())<0)return false;}
 if(S.date&&i.date&&i.date<S.date)return false;
 if(S.dur!=='any'&&i.days){
  if(S.dur==='a'&&i.days>5)return false;
  if(S.dur==='b'&&(i.days<6||i.days>10))return false;
  if(S.dur==='c'&&i.days<=10)return false;
 }
 if(i.price!=null&&S.max&&i.price>S.max)return false;
 return true;
}
function renderGrid(){
 var list=DATA.items.filter(match),g=$('#grid');
 g.innerHTML='';
 $('#count').textContent=t('found')+' '+list.length;
 if(!list.length){g.appendChild(el('div',{class:'empty'},el('p',{text:t('none')}),el('button',{class:'btn ghost',type:'button',text:t('reset'),onclick:resetF})));return;}
 list.forEach(function(i){g.appendChild(card(i))});
}
function renderChips(){
 var box=$('#chips');box.innerHTML='';
 ['all'].concat(SEC).forEach(function(k){
  box.appendChild(el('button',{class:'chip'+(S.sec===k?' on':''),type:'button',text:(k==='all'?'':ICO[k]+' ')+t('s_'+k),onclick:function(){S.sec=k;renderChips();renderGrid();}}));
 });
}
function renderDur(){
 var s=$('#fdur');s.innerHTML='';
 [['any',t('any')],['a',t('d_a')],['b',t('d_b')],['c',t('d_c')]].forEach(function(o){var op=el('option',{value:o[0],text:o[1]});if(S.dur===o[0])op.selected=true;s.appendChild(op)});
}
function resetF(){
 S.sec='all';S.q='';S.date='';S.dur='any';S.max=MAXP;
 $('#fq').value='';$('#fdate').value='';$('#fmax').value=MAXP;$('#fmaxOut').textContent=MAXP?'$'+fmt(MAXP):'';
 renderChips();renderDur();renderGrid();
}
function initFilters(){
 var mx=0;DATA.items.forEach(function(i){if(i.price!=null&&i.price>mx)mx=i.price});
 MAXP=mx?Math.ceil(mx/100)*100:0;S.max=MAXP;
 $('#budBox').hidden=!MAXP;
 var r=$('#fmax');r.max=MAXP||1;r.value=MAXP;$('#fmaxOut').textContent=MAXP?'$'+fmt(MAXP):'';
 r.addEventListener('input',function(){S.max=+r.value;$('#fmaxOut').textContent='$'+fmt(S.max);renderGrid();});
 $('#fq').addEventListener('input',function(e){S.q=e.target.value.trim();renderGrid();});
 $('#fdate').addEventListener('change',function(e){S.date=e.target.value;renderGrid();});
 $('#fdur').addEventListener('change',function(e){S.dur=e.target.value;renderGrid();});
}

/* ---------- 3D ring ---------- */
var ang=0,dragging=false,lastX=0,moved=0,ringR=300,reduced=window.matchMedia&&matchMedia('(prefers-reduced-motion:reduce)').matches;
function buildRing(){
 var ring=$('#ring');ring.innerHTML='';
 var list=DATA.items.filter(function(i){return i.sec==='hot'});
 if(list.length<3)list=DATA.items.slice();
 list=list.slice(0,10);
 if(!list.length){$('#ringSec').hidden=true;return;}
 $('#ringSec').hidden=false;
 var n=list.length,slots=Math.max(n,6),w=190,step=360/slots;
 ringR=Math.round((w/2)/Math.tan(Math.PI/slots))+26;
 for(var k=0;k<slots;k++){
  (function(it,k){
   var c=el('div',{class:'rc'},media(it,'rm'),el('div',{class:'rt'},el('b',{text:it.title}),el('span',{text:priceText(it)})));
   c.style.transform='rotateY('+(k*step)+'deg) translateZ('+ringR+'px)';
   c.addEventListener('click',function(){if(moved<6)openItem(it)});
   ring.appendChild(c);
  })(list[k%n],k);
 }
}
function ringTick(){
 if(!dragging&&!reduced)ang-=0.18;
 $('#ring').style.transform='translateZ('+(-ringR)+'px) rotateY('+ang+'deg)';
 requestAnimationFrame(ringTick);
}
function ringInit(){
 var st=$('#stage');
 st.addEventListener('pointerdown',function(e){dragging=true;lastX=e.clientX;moved=0;});
 window.addEventListener('pointermove',function(e){if(!dragging)return;var dx=e.clientX-lastX;lastX=e.clientX;ang+=dx*0.4;moved+=Math.abs(dx);});
 window.addEventListener('pointerup',function(){if(dragging){dragging=false;setTimeout(function(){moved=0},60);}});
 window.addEventListener('pointercancel',function(){dragging=false});
 ringTick();
}

/* ---------- hero ticket ---------- */
var DEST=[['IST','Istanbul'],['DXB','Dubai'],['SSH','Sharm el-Sheikh'],['AYT','Antalya'],['JED','Jeddah'],['SKD','Samarkand'],['BHK','Bukhara']];
function heroInit(){
 var tk=$('#ticket'),sc=$('#scene'),d=$('#dest'),di=0;
 if(window.matchMedia&&matchMedia('(hover:hover)').matches){
  sc.addEventListener('pointermove',function(e){var r=sc.getBoundingClientRect(),x=(e.clientX-r.left)/r.width-.5,y=(e.clientY-r.top)/r.height-.5;tk.style.setProperty('--ry',(x*26)+'deg');tk.style.setProperty('--rx',(8-y*20)+'deg');});
  sc.addEventListener('pointerleave',function(){tk.style.removeProperty('--ry');tk.style.removeProperty('--rx');});
 }
 setInterval(function(){
  d.classList.add('out');
  setTimeout(function(){di=(di+1)%DEST.length;$('#dCode').textContent=DEST[di][0];$('#dCity').textContent=DEST[di][1];d.classList.remove('out');},320);
 },2800);
}

/* ---------- modal ---------- */
function closeModal(){var o=$('.ov');if(o)o.remove();document.body.classList.remove('lock')}
function modal(content){
 closeModal();
 var md=el('div',{class:'md',role:'dialog','aria-modal':'true'},el('button',{class:'x',type:'button','aria-label':t('close'),text:'✕',onclick:closeModal}),content);
 var ov=el('div',{class:'ov',onclick:function(e){if(e.target===ov)closeModal()}},md);
 document.body.appendChild(ov);document.body.classList.add('lock');
}
function openItem(i){
 var big=el('div',{class:'m big'});
 big.style.background=GR[i.sec]||GR.hot;
 if(i.kind==='video'&&i.media){
  var v=el('video',{controls:'',playsinline:'',preload:'metadata',src:i.media});
  if(i.thumb)v.setAttribute('poster',i.thumb);
  big.appendChild(v);
 }else if(i.kind==='photo'&&i.thumb){big.appendChild(el('img',{src:i.thumb,alt:i.title}))}
 else{
  if(i.thumb)big.appendChild(el('img',{src:i.thumb,alt:''}));else big.appendChild(el('span',{class:'ico',text:ICO[i.sec]||'✈️'}));
 }
 var bodyKids=[];
 if(i.kind==='video'&&i.big){
  var bl=DATA.cfg.bot?'https://t.me/'+DATA.cfg.bot+'?start=item_'+i.id:null;
  bodyKids.push(el('p',{class:'muted',text:t('big')}));
  if(bl)bodyKids.push(openLink(el('a',{class:'btn dark sm',href:bl,target:'_blank',rel:'noopener',text:'✈️ '+t('open_bot')})));
 }
 var form=el('form',{novalidate:''});
 var fn=el('input',{maxlength:'60',autocomplete:'name'}),fp=el('input',{type:'tel',maxlength:'30',autocomplete:'tel'}),fc=el('input',{type:'number',min:'1',max:'50',value:'1',inputmode:'numeric'}),fo=el('textarea',{maxlength:'300'});
 fn.placeholder=t('name');fp.placeholder='+998 90 123 45 67';fo.placeholder=t('note');
 if(tgUser&&tgUser.first_name)fn.value=tgUser.first_name;
 var sp=store.get('phone');if(sp)fp.value=sp;
 var sub=el('button',{class:'btn',type:'submit',text:t('send_book'),style:'width:100%'});
 form.append(el('div',{class:'two f'},fn,fp),el('div',{class:'f'},el('label',{text:t('people')}),fc),el('div',{class:'f'},fo),sub);
 form.addEventListener('submit',function(e){
  e.preventDefault();
  var name=fn.value.trim(),ph=fp.value.trim();
  if(name.length<2||ph.replace(/\D/g,'').length<7){toast(t('fill'));return;}
  sub.disabled=true;
  post('/api/book',{item_id:i.id,name:name,phone:ph,people:+fc.value||1,note:fo.value.trim(),initData:initData}).then(function(r){
   if(r&&r.ok){store.set('phone',ph);var md=$('.md');md.innerHTML='';md.append(el('button',{class:'x',type:'button',text:'✕',onclick:closeModal}),el('div',{class:'ok'},el('div',{class:'big-ico',text:'✅'}),el('h2',{text:t('booked_h')}),el('p',{class:'muted',text:t('booked_p')}),el('p',{style:'margin-top:8px',text:'#'+r.id}),el('button',{class:'btn',type:'button',style:'margin-top:16px',text:t('close'),onclick:closeModal})));}
   else{sub.disabled=false;toast(t('err'));}
  }).catch(function(){sub.disabled=false;toast(t('err'))});
 });
 modal(el('div',null,big,el('div',{class:'mb'},
  el('span',{class:'tag',text:(ICO[i.sec]||'')+' '+t('s_'+i.sec)}),
  el('h2',{text:i.title}),
  i.place?el('p',{class:'pl',text:'🧭 '+i.place}):null,
  el('div',{class:'meta',style:'margin-top:8px'},i.days?chip(i.days+' '+t('days')):null,i.date?chip(fdate(i.date)):null),
  i.descr?el('p',{class:'descr',text:i.descr}):null,
  el('div',{class:'price',text:priceText(i)}),
  bodyKids,form)));
}

/* ---------- my bookings ---------- */
function openMy(){
 var list=el('div',{style:'margin-top:12px'});
 var kids=[el('h2',{text:t('my_h')})];
 function load(ph){
  list.innerHTML='';
  post('/api/my',{initData:initData,phone:ph||''}).then(function(r){
   var arr=(r&&r.items)||[];
   if(!arr.length){list.appendChild(el('p',{class:'muted',text:t('my_none')}));return;}
   arr.forEach(function(b){
    list.appendChild(el('div',{class:'bk'},el('b',{text:'#'+b.id+' · '+(b.title||'—')}),el('span',{class:'muted',text:fdate(b.date)+' · '+b.people+' 👥'}),el('br'),el('span',{class:'st '+b.status,text:t('st_'+b.status)})));
   });
  }).catch(function(){list.appendChild(el('p',{class:'muted',text:t('err')}))});
 }
 if(initData){kids.push(list);}
 else{
  var ph=el('input',{type:'tel',maxlength:'30'});ph.placeholder=t('my_phone');ph.value=store.get('phone')||'';
  var go=el('button',{class:'btn sm',type:'button',text:t('my_show'),onclick:function(){if(ph.value.replace(/\D/g,'').length<7){toast(t('fill'));return;}store.set('phone',ph.value);load(ph.value);}});
  kids.push(el('div',{class:'two',style:'grid-template-columns:1fr auto;align-items:center'},ph,go),list);
 }
 modal(el('div',{class:'mb',style:'padding-top:22px;padding-bottom:18px'},kids));
 if(initData)load('');
 else if(store.get('phone'))load(store.get('phone'));
}

/* ---------- info / contact ---------- */
function renderInfo(){
 var c=DATA.cfg||{};
 $('#aboutP').textContent=c.about||t('about_p');
 $('#hours').textContent=c.hours||t('hours_d');
 $('#licBox').hidden=!c.license;$('#lic').textContent=c.license||'';
 var rv=$('#revs');rv.innerHTML='';
 if(!DATA.reviews.length)rv.appendChild(el('p',{class:'muted',text:t('rev_none')}));
 DATA.reviews.forEach(function(r){rv.appendChild(el('blockquote',{class:'rev'},el('p',{text:r.text}),el('footer',{text:'— '+r.name})))});
 var cb=$('#cBtns');cb.innerHTML='';var tp=$('#tPhones');tp.innerHTML='';
 [c.phone1,c.phone2].forEach(function(p){
  if(!p)return;
  var h='tel:'+p.replace(/[^\d+]/g,'');
  cb.appendChild(el('a',{class:'btn',href:h,text:'📞 '+p}));
  tp.appendChild(el('a',{href:h,text:p}));
 });
 if(!tp.children.length)tp.appendChild(el('small',{text:'Imperiya Travel'}));
 if(c.tg)cb.appendChild(openLink(el('a',{class:'btn ghost',href:'https://t.me/'+String(c.tg).replace('@',''),target:'_blank',rel:'noopener',text:'✈️ '+t('tg')})));
 var mu=(c.map_url&&c.map_url.indexOf('https://')===0)?c.map_url:((c.lat&&c.lon)?'https://www.google.com/maps?q='+encodeURIComponent(c.lat+','+c.lon):'');
 if(mu)cb.appendChild(el('a',{class:'btn ghost',href:mu,target:'_blank',rel:'noopener',text:'📍 '+t('map')}));
}

/* ---------- i18n ---------- */
function applyI18n(){
 document.documentElement.lang=lang;
 document.querySelectorAll('[data-i18n]').forEach(function(n){n.textContent=t(n.getAttribute('data-i18n'))});
 document.querySelectorAll('[data-ph]').forEach(function(n){n.placeholder=t(n.getAttribute('data-ph'))});
 var lb=$('#langs');lb.innerHTML='';
 ['uz','ru','en'].forEach(function(l){lb.appendChild(el('button',{type:'button',class:l===lang?'on':'',text:l.toUpperCase(),onclick:function(){lang=l;store.set('lang',l);applyI18n();}}))});
 renderChips();renderDur();renderGrid();renderInfo();
}

/* ---------- ask form ---------- */
$('#askForm').addEventListener('submit',function(e){
 e.preventDefault();
 var n=$('#aName').value.trim(),p=$('#aPhone').value.trim(),x=$('#aText').value.trim();
 if(n.length<2||p.replace(/\D/g,'').length<7||x.length<2){toast(t('fill'));return;}
 post('/api/ask',{name:n,phone:p,text:x,initData:initData}).then(function(r){
  if(r&&r.ok){toast(t('ask_ok'));$('#aText').value='';}else toast(t('err'));
 }).catch(function(){toast(t('err'))});
});
$('#myBtn').addEventListener('click',openMy);
document.addEventListener('keydown',function(e){if(e.key==='Escape')closeModal()});

/* ---------- start ---------- */
heroInit();ringInit();
fetch('/api/data').then(function(r){return r.json()}).then(function(d){
 DATA=d;initFilters();buildRing();applyI18n();
}).catch(function(){applyI18n();toast(t('err'));});
applyI18n();
})();
</script>
</body>
</html>
'''

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass