"""
Wolf UC Store: Telegram Mini App uchun bot + API server (bitta jarayon).

- Saytni "/" da beradi (static/index.html)
- /api/* : balans, chek yuklash, buyurtma (Telegram initData imzosi tekshiriladi)
- Bot: /start, adminga chek va buyurtma xabarlari, Tasdiqlash/Bekor tugmalari
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wolf")

# ---------------------------------------------------------------- sozlamalar
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().strip("\"'").strip().rstrip("/")
if WEBAPP_URL and not WEBAPP_URL.startswith(("http://", "https://")):
    WEBAPP_URL = "https://" + WEBAPP_URL   # https:// yozilmagan bo'lsa o'zi qo'shadi
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN or not ADMIN_ID_RAW.isdigit():
    sys.exit("XATO: BOT_TOKEN va ADMIN_ID (faqat raqam) o'zgaruvchilarini kiriting.")
ADMIN_ID = int(ADMIN_ID_RAW)

# Chek va buyurtmalar keladigan egalar: ADMIN_ID + (ixtiyoriy) OWNER_IDS="111,222"
OWNER_IDS = {
    int(x) for x in os.getenv("OWNER_IDS", os.getenv("OWNER_ID", "")).replace(" ", "").split(",") if x.isdigit()
}
RECIPIENTS = {ADMIN_ID} | OWNER_IDS

# To'lov kartasi (Railway Variables orqali o'zgartirish mumkin)
CARD_NUMBER = os.getenv("CARD_NUMBER", "5614 6822 0781 4403").strip()
CARD_OWNER = os.getenv("CARD_OWNER", "Wolf UC Store").strip()
MAX_PENDING_ORDERS = 5

MIN_TOPUP = 1000
MAX_TOPUP = 50_000_000
MAX_PENDING_TOPUPS = 5
MAX_UPLOAD = 8 * 1024 * 1024

# Narxlar SERVERDA saqlanadi: mijoz yuborgan narxga ishonilmaydi.
PACKAGES = {
    "60 UC": 11700,
    "325 UC": 59000,
    "660 UC": 113000,
    "1800 UC": 280000,
    "3850 UC": 550000,
    "8100 UC": 1100000,
    "16200 UC": 2230000,
    "24300 UC": 3200000,
    "Prime Plus 1 yillik": 1335245,
    "Prime Plus 6 oylik": 663788,
    "Prime Plus 3 oylik": 339461,
    "Prime Plus 1 oylik": 116487,
    "Prime 12 oylik": 136784,
    "Prime 6 oylik": 69892,
    "Prime 3 oylik": 36446,
    "Prime 1 oylik": 14148,
    "First Purchase Pack": 12148,
    "Upgradeable Firearm Pack": 34395,
    "Mythic Emblem Pack": 56604,
    "Qizil kristal 1 dona": 34661,
}

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ------------------------------------------------------------------- baza
DATA_DIR.mkdir(parents=True, exist_ok=True)
_db = sqlite3.connect(DATA_DIR / "wolf.db", check_same_thread=False, isolation_level=None)
_db.row_factory = sqlite3.Row
_lock = threading.Lock()

_db.executescript(
    """
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY,
        name TEXT, username TEXT,
        balance INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0),
        created TEXT
    );
    CREATE TABLE IF NOT EXISTS topups(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, amount INTEGER NOT NULL, method TEXT,
        status TEXT NOT NULL, created TEXT
    );
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, game_id TEXT, nickname TEXT,
        title TEXT, price INTEGER NOT NULL, status TEXT NOT NULL, created TEXT
    );
    """
)


# Eski bazani yangilash: buyurtma holatlari
#   review   - karta cheki egasi tekshirmoqda
#   approved - to'lov tasdiqlangan, UC hali yuborilmagan
#   done     - UC tushdi | rejected - chek rad etildi | cancelled - bekor (balans qaytgan)
_cols = {r["name"] for r in _db.execute("PRAGMA table_info(orders)").fetchall()}
if "method" not in _cols:
    _db.execute("ALTER TABLE orders ADD COLUMN method TEXT DEFAULT 'balance'")
_db.execute("UPDATE orders SET status='approved' WHERE status='pending'")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tx(fn):
    """fn(db) ni bitta atomik tranzaksiyada bajaradi."""
    with _lock:
        _db.execute("BEGIN IMMEDIATE")
        try:
            result = fn(_db)
            _db.execute("COMMIT")
            return result
        except Exception:
            _db.execute("ROLLBACK")
            raise


def q_all(sql, args=()):
    with _lock:
        return [dict(r) for r in _db.execute(sql, args).fetchall()]


def q_one(sql, args=()):
    with _lock:
        r = _db.execute(sql, args).fetchone()
        return dict(r) if r else None


def upsert_user(u: dict):
    name = " ".join(filter(None, [u.get("first_name"), u.get("last_name")])) or "Foydalanuvchi"
    with _lock:
        _db.execute(
            "INSERT INTO users(id,name,username,balance,created) VALUES(?,?,?,0,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username",
            (u["id"], name, u.get("username"), now_iso()),
        )


def fmt(n: int) -> str:
    return f"{int(n):,}".replace(",", " ")


def who(user: dict) -> str:
    uname = f" @{escape(user['username'])}" if user.get("username") else ""
    return f"{escape(user['name'] or '-')}{uname}"


async def send_to_owners(text: str, kb, photo: bytes | None = None) -> int:
    """Xabarni (ixtiyoriy rasm bilan) barcha egalarga yuboradi. Nechtasiga yetganini qaytaradi."""
    sent = 0
    for rid in RECIPIENTS:
        try:
            if photo is not None:
                await bot.send_photo(rid, BufferedInputFile(photo, "chek.jpg"), caption=text, reply_markup=kb)
            else:
                await bot.send_message(rid, text, reply_markup=kb)
            sent += 1
        except Exception:
            log.exception("Egaga (%s) yuborib bo'lmadi", rid)
    return sent


# --------------------------------------------------------------- xavfsizlik
def verify_init_data(init_data: str):
    """Telegram Mini App initData imzosini tekshiradi. To'g'ri bo'lsa user dict qaytaradi."""
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop("hash", None)
        if not received:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > 86400:
            return None
        user = json.loads(pairs["user"])
        return user if "id" in user else None
    except Exception:
        return None


def auth(request: web.Request) -> dict:
    user = verify_init_data(request.headers.get("X-Init-Data", ""))
    if not user:
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "Ilovani Telegram ichidan qayta oching."}),
            content_type="application/json",
        )
    upsert_user(user)
    return user


def err(msg: str, status: int = 400):
    return web.json_response({"error": msg}, status=status)


# --------------------------------------------------------------------- API
async def api_me(request: web.Request):
    user = auth(request)
    uid = user["id"]
    row = await asyncio.to_thread(q_one, "SELECT balance FROM users WHERE id=?", (uid,))
    orders = await asyncio.to_thread(
        q_all,
        "SELECT id,game_id,title,price,status,created FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 30",
        (uid,),
    )
    topups = await asyncio.to_thread(
        q_all,
        "SELECT id,amount,method,status,created FROM topups WHERE user_id=? ORDER BY id DESC LIMIT 30",
        (uid,),
    )
    return web.json_response({"balance": row["balance"], "orders": orders, "topups": topups})


async def api_check_player(request: web.Request):
    gid = request.match_info["gid"].strip()
    if not gid.isdigit() or not (5 <= len(gid) <= 15):
        return web.json_response({"username": None})
    # Eslatma: bu faqat ID formatini tekshiradi (PUBG nikini aniqlash uchun alohida xizmat kerak).
    return web.json_response({"username": f"ID {gid}"})


async def api_topup(request: web.Request):
    user = auth(request)
    uid = user["id"]
    form = await request.post()

    method = str(form.get("method", ""))
    if method not in ("Uzcard", "Humo"):
        return err("To'lov usulini tanlang.")
    try:
        amount = int(str(form.get("amount", "0")))
    except ValueError:
        return err("Summa noto'g'ri.")
    if not (MIN_TOPUP <= amount <= MAX_TOPUP):
        return err(f"Summa {fmt(MIN_TOPUP)} dan {fmt(MAX_TOPUP)} so'mgacha bo'lishi kerak.")

    receipt = form.get("receipt")
    if receipt is None or not hasattr(receipt, "file"):
        return err("Chek rasmini yuklang.")
    if not str(getattr(receipt, "content_type", "")).startswith("image/"):
        return err("Faqat rasm (screenshot) yuklang.")
    raw = receipt.file.read()
    if not raw or len(raw) > MAX_UPLOAD:
        return err("Rasm hajmi 8 MB dan oshmasligi kerak.")

    pending = await asyncio.to_thread(
        q_one, "SELECT COUNT(*) AS c FROM topups WHERE user_id=? AND status='pending'", (uid,)
    )
    if pending["c"] >= MAX_PENDING_TOPUPS:
        return err("Sizda ko'rib chiqilmagan so'rovlar ko'p. Admin tasdiqlashini kuting.", 429)

    def create(db):
        cur = db.execute(
            "INSERT INTO topups(user_id,amount,method,status,created) VALUES(?,?,?,'pending',?)",
            (uid, amount, method, now_iso()),
        )
        return cur.lastrowid

    tid = await asyncio.to_thread(tx, create)
    urow = await asyncio.to_thread(q_one, "SELECT * FROM users WHERE id=?", (uid,))

    caption = (
        f"💳 <b>Balans to'ldirish #{tid}</b>\n"
        f"👤 {who(urow)}\n"
        f"🆔 <code>{uid}</code>\n"
        f"💰 <b>{fmt(amount)} so'm</b>\n"
        f"🏦 {escape(method)}\n\n"
        f"Chekni tekshirib, qaror qiling:"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"tu:ok:{tid}"),
            InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"tu:no:{tid}"),
        ]]
    )
    if not await send_to_owners(caption, kb, photo=raw):
        log.error("Egalarga chek yuborilmadi")
        await asyncio.to_thread(
            tx, lambda db: db.execute("UPDATE topups SET status='error' WHERE id=?", (tid,))
        )
        return err("So'rovni adminga yuborib bo'lmadi. Keyinroq urinib ko'ring.", 502)

    return web.json_response({"ok": True, "id": tid})


async def api_order(request: web.Request):
    user = auth(request)
    uid = user["id"]
    try:
        body = await request.json()
    except Exception:
        return err("So'rov noto'g'ri.")

    title = str(body.get("title", ""))
    game_id = str(body.get("game_id", "")).strip()
    nickname = str(body.get("nickname", ""))[:64]
    price = PACKAGES.get(title)
    if price is None:
        return err("Bunday paket yo'q.")
    if not game_id.isdigit() or not (5 <= len(game_id) <= 15):
        return err("PUBG ID noto'g'ri.")

    def create(db):
        bal = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]
        if bal < price:
            return ("low", bal, None)
        db.execute("UPDATE users SET balance=balance-? WHERE id=?", (price, uid))
        cur = db.execute(
            "INSERT INTO orders(user_id,game_id,nickname,title,price,status,created) "
            "VALUES(?,?,?,?,?,'approved',?)",
            (uid, game_id, nickname, title, price, now_iso()),
        )
        db.execute("UPDATE orders SET method='balance' WHERE id=?", (cur.lastrowid,))
        return ("ok", bal - price, cur.lastrowid)

    status, bal, oid = await asyncio.to_thread(tx, create)
    if status == "low":
        return web.json_response(
            {"error": "Balans yetarli emas.", "code": "low_balance", "balance": bal, "price": price},
            status=402,
        )

    urow = await asyncio.to_thread(q_one, "SELECT * FROM users WHERE id=?", (uid,))
    text = (
        f"🛒 <b>Yangi buyurtma #{oid}</b>\n"
        f"👤 {who(urow)} (<code>{uid}</code>)\n"
        f"🎮 PUBG ID: <code>{escape(game_id)}</code>\n"
        f"📦 {escape(title)}\n"
        f"💰 {fmt(price)} so'm (balansdan yechildi)\n\n"
        f"UC ni o'yinchiga yuborib, \"Yetkazildi\" ni bosing."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Yetkazildi", callback_data=f"or:ok:{oid}"),
            InlineKeyboardButton(text="❌ Bekor (pul qaytadi)", callback_data=f"or:no:{oid}"),
        ]]
    )
    if not await send_to_owners(text, kb):
        log.error("Egalarga buyurtma yuborilmadi, pul qaytarilmoqda")
        await asyncio.to_thread(tx, lambda db: _cancel_order(db, oid))
        return err("Buyurtmani adminga yuborib bo'lmadi. Pul qaytarildi.", 502)

    return web.json_response({"ok": True, "id": oid, "balance": bal})


def _cancel_order(db, oid):
    row = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not row or row["status"] != "approved":
        return None
    db.execute("UPDATE orders SET status='cancelled' WHERE id=?", (oid,))
    db.execute("UPDATE users SET balance=balance+? WHERE id=?", (row["price"], row["user_id"]))
    return dict(row)


async def api_config(_request: web.Request):
    return web.json_response({"card": CARD_NUMBER, "card_owner": CARD_OWNER})


async def api_order_card(request: web.Request):
    """Karta orqali to'g'ridan-to'g'ri to'lov: paket + PUBG ID + chek rasmi -> egaga."""
    user = auth(request)
    uid = user["id"]
    form = await request.post()

    title = str(form.get("title", ""))
    game_id = str(form.get("game_id", "")).strip()
    price = PACKAGES.get(title)
    if price is None:
        return err("Bunday paket yo'q.")
    if not game_id.isdigit() or not (5 <= len(game_id) <= 15):
        return err("PUBG ID noto'g'ri.")

    receipt = form.get("receipt")
    if receipt is None or not hasattr(receipt, "file"):
        return err("Chek rasmini yuklang.")
    if not str(getattr(receipt, "content_type", "")).startswith("image/"):
        return err("Faqat rasm (screenshot) yuklang.")
    raw = receipt.file.read()
    if not raw or len(raw) > MAX_UPLOAD:
        return err("Rasm hajmi 8 MB dan oshmasligi kerak.")

    pending = await asyncio.to_thread(
        q_one, "SELECT COUNT(*) AS c FROM orders WHERE user_id=? AND status='review'", (uid,)
    )
    if pending["c"] >= MAX_PENDING_ORDERS:
        return err("Sizda ko'rib chiqilmagan buyurtmalar ko'p. Tasdiqlanishini kuting.", 429)

    def create(db):
        cur = db.execute(
            "INSERT INTO orders(user_id,game_id,nickname,title,price,status,created,method) "
            "VALUES(?,?,?,?,?,'review',?,'card')",
            (uid, game_id, "", title, price, now_iso()),
        )
        return cur.lastrowid

    oid = await asyncio.to_thread(tx, create)
    urow = await asyncio.to_thread(q_one, "SELECT * FROM users WHERE id=?", (uid,))
    uname = f"@{escape(urow['username'])}" if urow.get("username") else "yo'q"

    caption = (
        f"🧾 <b>Yangi buyurtma #{oid}</b> (karta orqali)\n\n"
        f"👤 {escape(urow['name'] or '-')}\n"
        f"🔗 Username: {uname}\n"
        f"🪪 Telegram ID: <code>{uid}</code>\n"
        f"🎮 PUBG ID: <code>{escape(game_id)}</code>\n"
        f"📦 {escape(title)}\n"
        f"💰 <b>{fmt(price)} so'm</b>\n\n"
        f"Chekni tekshirib, qaror qiling:"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"oc:ok:{oid}"),
            InlineKeyboardButton(text="❌ Rad etish", callback_data=f"oc:no:{oid}"),
        ]]
    )
    if not await send_to_owners(caption, kb, photo=raw):
        log.error("Egalarga buyurtma cheki yuborilmadi")
        await asyncio.to_thread(tx, lambda db: db.execute("UPDATE orders SET status='error' WHERE id=?", (oid,)))
        return err("Chekni egasiga yuborib bo'lmadi. Keyinroq urinib ko'ring.", 502)

    return web.json_response({"ok": True, "id": oid})


def _find_index() -> Path | None:
    """index.html ni static/ ichidan yoki bot yonidan (bosh papkadan) qidiradi."""
    base = Path(__file__).resolve().parent
    for p in (base / "static" / "index.html", base / "index.html", Path.cwd() / "static" / "index.html", Path.cwd() / "index.html"):
        if p.is_file():
            return p
    return None


async def index_page(_request: web.Request):
    p = _find_index()
    if p is None:
        log.error("index.html topilmadi! GitHub'da static/index.html bo'lishi kerak. Papka: %s", Path(__file__).resolve().parent)
        return web.Response(text="index.html topilmadi: GitHub'ga static/index.html yuklang.", status=404)
    return web.FileResponse(p, headers={"Cache-Control": "no-cache"})


async def health(_request: web.Request):
    return web.Response(text="ok")


def make_app() -> web.Application:
    app = web.Application(client_max_size=MAX_UPLOAD + 1024 * 1024)
    app.add_routes([
        web.get("/", index_page),
        web.get("/healthz", health),
        web.get("/api/me", api_me),
        web.get("/api/config", api_config),
        web.get("/api/check-player/{gid}", api_check_player),
        web.post("/api/order-card", api_order_card),
        web.post("/api/topup", api_topup),
        web.post("/api/order", api_order),
    ])
    return app


# --------------------------------------------------------------------- bot
@dp.message(CommandStart())
async def cmd_start(m: Message):
    if m.from_user:
        upsert_user({
            "id": m.from_user.id,
            "first_name": m.from_user.first_name,
            "last_name": m.from_user.last_name,
            "username": m.from_user.username,
        })
    await m.answer(
        "🐺 <b>Wolf UC Store</b> ga xush kelibsiz!\n\n"
        "UC xarid qilish uchun pastdagi chap burchakdagi <b>ko'k tugmani</b> bosing 👇"
    )


async def _notify(uid: int, text: str):
    try:
        await bot.send_message(uid, text)
    except Exception:
        log.warning("Foydalanuvchiga xabar yuborilmadi: %s", uid)


@dp.callback_query(F.data.startswith("tu:"))
async def cb_topup(cb: CallbackQuery):
    if cb.from_user.id not in RECIPIENTS:
        await cb.answer("Ruxsat yo'q", show_alert=True)
        return
    _, act, tid_s = cb.data.split(":")
    tid = int(tid_s)

    def work(db):
        row = db.execute("SELECT * FROM topups WHERE id=?", (tid,)).fetchone()
        if not row or row["status"] != "pending":
            return None
        if act == "ok":
            db.execute("UPDATE topups SET status='approved' WHERE id=?", (tid,))
            db.execute("UPDATE users SET balance=balance+? WHERE id=?", (row["amount"], row["user_id"]))
        else:
            db.execute("UPDATE topups SET status='rejected' WHERE id=?", (tid,))
        bal = db.execute("SELECT balance FROM users WHERE id=?", (row["user_id"],)).fetchone()["balance"]
        return dict(row), bal

    res = await asyncio.to_thread(tx, work)
    if res is None:
        await cb.answer("Bu so'rov allaqachon ko'rib chiqilgan.", show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    row, bal = res
    if act == "ok":
        mark = "✅ <b>TASDIQLANDI</b>"
        await _notify(
            row["user_id"],
            f"✅ Balansingiz <b>{fmt(row['amount'])} so'm</b> ga to'ldirildi.\nHozirgi balans: <b>{fmt(bal)} so'm</b>",
        )
    else:
        mark = "❌ <b>BEKOR QILINDI</b>"
        await _notify(
            row["user_id"],
            f"❌ {fmt(row['amount'])} so'mlik to'ldirish so'rovi rad etildi. "
            f"Muammo bo'lsa, admin bilan bog'laning.",
        )
    try:
        await cb.message.edit_caption(caption=f"{cb.message.html_text}\n\n{mark}", reply_markup=None)
    except Exception:
        log.exception("Admin xabarini yangilab bo'lmadi")
    await cb.answer("Bajarildi")


@dp.callback_query(F.data.startswith("or:"))
async def cb_order(cb: CallbackQuery):
    if cb.from_user.id not in RECIPIENTS:
        await cb.answer("Ruxsat yo'q", show_alert=True)
        return
    _, act, oid_s = cb.data.split(":")
    oid = int(oid_s)

    def work(db):
        if act == "no":
            row = _cancel_order(db, oid)
            if row is None:
                return None
        else:
            row = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if not row or row["status"] != "approved":
                return None
            db.execute("UPDATE orders SET status='done' WHERE id=?", (oid,))
            row = dict(row)
        return row

    row = await asyncio.to_thread(tx, work)
    if row is None:
        await cb.answer("Bu buyurtma allaqachon ko'rib chiqilgan.", show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    if act == "ok":
        mark = "✅ <b>YETKAZILDI</b>"
        await _notify(row["user_id"], "UC tushdi ✅")
    else:
        mark = "❌ <b>BEKOR QILINDI (pul qaytarildi)</b>"
        await _notify(
            row["user_id"],
            f"❌ Buyurtma #{oid} bekor qilindi. <b>{fmt(row['price'])} so'm</b> balansingizga qaytarildi.",
        )
    try:
        await cb.message.edit_text(f"{cb.message.html_text}\n\n{mark}", reply_markup=None)
    except Exception:
        log.exception("Admin xabarini yangilab bo'lmadi")
    await cb.answer("Bajarildi")


@dp.callback_query(F.data.startswith("oc:"))
async def cb_order_card(cb: CallbackQuery):
    """Karta orqali buyurtma: ok = tasdiqlash, no = rad etish, done = UC yuborildi."""
    if cb.from_user.id not in RECIPIENTS:
        await cb.answer("Ruxsat yo'q", show_alert=True)
        return
    _, act, oid_s = cb.data.split(":")
    steps = {"ok": ("review", "approved"), "no": ("review", "rejected"), "done": ("approved", "done")}
    if act not in steps or not oid_s.isdigit():
        await cb.answer()
        return
    oid = int(oid_s)
    need, new = steps[act]

    def work(db):
        row = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not row or row["status"] != need:
            return None
        db.execute("UPDATE orders SET status=? WHERE id=?", (new, oid))
        return dict(row)

    row = await asyncio.to_thread(tx, work)
    if row is None:
        await cb.answer("Bu buyurtma allaqachon ko'rib chiqilgan.", show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    uid = row["user_id"]
    kb = None
    if act == "ok":
        await _notify(uid, "UC yaqinda tushadi ✅")
        mark = (
            "✅ <b>TASDIQLANDI</b>\n"
            f"UC ni PUBG ID ga tashlang, keyin yozing:\n/yuborildi <code>{uid}</code>\n"
            "yoki pastdagi tugmani bosing."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="📦 UC yuborildi", callback_data=f"oc:done:{oid}")]]
        )
    elif act == "no":
        await _notify(uid, "❌ To'lov tasdiqlanmadi. Muammo bo'lsa, admin bilan bog'laning.")
        mark = "❌ <b>RAD ETILDI</b>"
    else:
        await _notify(uid, "UC tushdi ✅")
        mark = "📦 <b>UC YUBORILDI</b>"
    try:
        await cb.message.edit_caption(caption=f"{cb.message.html_text}\n\n{mark}", reply_markup=kb)
    except Exception:
        log.exception("Eganing xabarini yangilab bo'lmadi")
    await cb.answer("Bajarildi")


@dp.message(Command("yuborildi"))
async def cmd_yuborildi(m: Message, command: CommandObject):
    """/yuborildi <mijoz telegram id> -> mijozga "UC tushdi ✅". Idsiz yozilsa, kutayotganlar ro'yxati."""
    if not m.from_user or m.from_user.id not in RECIPIENTS:
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        rows = await asyncio.to_thread(
            q_all, "SELECT id,user_id,game_id,title FROM orders WHERE status='approved' ORDER BY id LIMIT 30"
        )
        if not rows:
            await m.answer("UC kutayotgan buyurtma yo'q.\n\nIshlatilishi: /yuborildi <code>MIJOZ_TELEGRAM_ID</code>")
            return
        lines = [
            f"#{r['id']} — {escape(r['title'])}\nPUBG: <code>{escape(r['game_id'])}</code> | /yuborildi <code>{r['user_id']}</code>"
            for r in rows
        ]
        await m.answer("📦 UC kutayotganlar:\n\n" + "\n\n".join(lines))
        return

    uid = int(arg)

    def work(db):
        row = db.execute(
            "SELECT * FROM orders WHERE user_id=? AND status='approved' ORDER BY id LIMIT 1", (uid,)
        ).fetchone()
        if not row:
            return None
        db.execute("UPDATE orders SET status='done' WHERE id=?", (row["id"],))
        left = db.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE user_id=? AND status='approved'", (uid,)
        ).fetchone()["c"]
        return dict(row), left

    res = await asyncio.to_thread(tx, work)
    if res is None:
        await m.answer("Bu mijozda UC kutayotgan (tasdiqlangan) buyurtma topilmadi.")
        return
    row, left = res
    await _notify(uid, "UC tushdi ✅")
    more = f"\nYana {left} ta buyurtma kutmoqda." if left else ""
    await m.answer(
        f"📦 #{row['id']} — {escape(row['title'])} (PUBG ID: <code>{escape(row['game_id'])}</code>) "
        f"yuborildi deb belgilandi.\nMijozga «UC tushdi ✅» xabari ketdi.{more}"
    )


# -------------------------------------------------------------------- main
async def main():
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Server %s portda ishga tushdi", PORT)

    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception:
        log.exception("delete_webhook xatosi (davom etamiz)")
    if WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="🛒 Do'kon", web_app=WebAppInfo(url=WEBAPP_URL))
            )
            log.info("Menyu tugmasi o'rnatildi: %s", WEBAPP_URL)
        except Exception as e:
            # Bot va sayt baribir ishlayveradi; sababi shu qatorda ko'rinadi
            log.error("MENYU TUGMASI O'RNATILMADI (WEBAPP_URL=%r): %s", WEBAPP_URL, e)
    else:
        log.warning("WEBAPP_URL berilmagan: menyu tugmasi o'rnatilmadi")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
