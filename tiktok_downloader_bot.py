"""
Telegram бот для скачивания видео и фото с TikTok, Instagram Reels, YouTube Shorts.

Установка зависимостей:
    pip install aiogram yt-dlp python-dotenv

Нужен ffmpeg:
    apt install ffmpeg   (Debian/Ubuntu)
    brew install ffmpeg  (Mac)

Запуск:
    python3 tiktok_downloader_bot.py
"""

import os
import sys
import atexit
import asyncio
import subprocess
import logging
import logging.handlers
import re
import uuid
import shutil
import sqlite3
import time
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, FSInputFile, InputMediaPhoto,
    CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineQuery, InlineQueryResultVideo,
)
from aiogram.filters import Command
from dotenv import load_dotenv

load_dotenv()

# ── Конфигурация ──────────────────────────────────────────────────────────────

BOT_TOKEN          = os.getenv("BOT_TOKEN")
TMP_DIR            = "tmp_media"
DB_FILE            = "bot.db"
LOG_FILE           = "bot.log"
CAPTION            = "Скачано с помощью @MellSaveBot"
MAX_FILE_MB        = 50
DL_TIMEOUT         = 90
RATE_LIMIT         = 3
DAILY_LIMIT        = 5
WORKERS            = 3
SUBSCRIPTION_PRICE = 99   # Telegram Stars
SUBSCRIPTION_DAYS  = 30
PID_FILE           = "bot.pid"

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
VIDEO_EXTS = (".mp4", ".webm", ".mov")
URL_RE     = re.compile(r"https?://\S+")

# ── Логирование ───────────────────────────────────────────────────────────────

def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

logger = logging.getLogger(__name__)

# ── База данных (SQLite) ──────────────────────────────────────────────────────

def db_init():
    with sqlite3.connect(DB_FILE) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                user_id INTEGER,
                day     TEXT,
                count   INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, day)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id    INTEGER PRIMARY KEY,
                expires_at TEXT NOT NULL
            )
        """)

def db_today_count(user_id: int) -> int:
    with sqlite3.connect(DB_FILE) as con:
        row = con.execute(
            "SELECT count FROM downloads WHERE user_id=? AND day=?",
            (user_id, str(date.today()))
        ).fetchone()
    return row[0] if row else 0

def db_increment(user_id: int):
    with sqlite3.connect(DB_FILE) as con:
        con.execute("""
            INSERT INTO downloads (user_id, day, count) VALUES (?,?,1)
            ON CONFLICT(user_id, day) DO UPDATE SET count = count + 1
        """, (user_id, str(date.today())))

def db_is_subscribed(user_id: int) -> bool:
    with sqlite3.connect(DB_FILE) as con:
        row = con.execute(
            "SELECT expires_at FROM subscriptions WHERE user_id=?", (user_id,)
        ).fetchone()
    return bool(row and row[0] >= str(date.today()))

def db_sub_expiry(user_id: int) -> Optional[str]:
    with sqlite3.connect(DB_FILE) as con:
        row = con.execute(
            "SELECT expires_at FROM subscriptions WHERE user_id=?", (user_id,)
        ).fetchone()
    return row[0] if row else None

def db_activate_subscription(user_id: int):
    expiry = str(date.today() + timedelta(days=SUBSCRIPTION_DAYS))
    with sqlite3.connect(DB_FILE) as con:
        con.execute("""
            INSERT INTO subscriptions (user_id, expires_at) VALUES (?,?)
            ON CONFLICT(user_id) DO UPDATE SET expires_at=
                CASE
                    WHEN expires_at >= date('now') THEN date(expires_at, '+30 days')
                    ELSE ?
                END
        """, (user_id, expiry, expiry))

# ── Rate limiter (в памяти) ───────────────────────────────────────────────────

_user_hits: dict[int, list[float]] = defaultdict(list)

def rate_limited(user_id: int) -> bool:
    now = time.monotonic()
    hits = [t for t in _user_hits[user_id] if now - t < 60]
    _user_hits[user_id] = hits
    if len(hits) >= RATE_LIMIT:
        return True
    _user_hits[user_id].append(now)
    return False

def _write_tiktok_cookies(session_id: str) -> None:
    """Генерирует cookies.txt из TT_SESSION_ID и опционально TT_UID."""
    exp = 1797692884  # ~2026-12
    uid = os.getenv("TT_UID", "").strip()
    lines = [
        "# Netscape HTTP Cookie File",
        f".tiktok.com\tTRUE\t/\tTRUE\t{exp}\tsessionid\t{session_id}",
        f".tiktok.com\tTRUE\t/\tTRUE\t{exp}\tsid_tt\t{session_id}",
        f".tiktok.com\tTRUE\t/\tTRUE\t{exp}\tsessionid_ss\t{session_id}",
    ]
    if uid:
        lines += [
            f".tiktok.com\tTRUE\t/\tTRUE\t{exp}\tuid_tt\t{uid}",
            f".tiktok.com\tTRUE\t/\tTRUE\t{exp}\tuid_tt_ss\t{uid}",
        ]
    with open("cookies.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    logger.info("cookies.txt сгенерирован (uid=%s)", "да" if uid else "нет")


# ── Скачивание через yt-dlp ───────────────────────────────────────────────────

# Ищем cookies.txt: сначала в общем хранилище bothost.ru, затем локально
_SHARED_COOKIES = os.path.join(os.getenv("SHARED_DIR", "/app/shared"), "cookies.txt")
COOKIES_FILE = _SHARED_COOKIES if os.path.exists(_SHARED_COOKIES) else "cookies.txt"
PROXY        = os.getenv("PROXY")  # например: socks5://user:pass@host:port или http://host:port


def _get_direct_url(url: str) -> Optional[tuple[str, str]]:
    """Быстро извлекает прямую ссылку на видео и превью без скачивания."""
    cmd = [
        "yt-dlp", url,
        "--print", "url",
        "--print", "thumbnail",
        "--no-warnings", "--no-playlist",
        "-f", "best[ext=mp4][height<=720]/best[ext=mp4]/best",
        "--socket-timeout", "10",
    ]
    if _is_youtube(url):
        cmd += ["--extractor-args", "youtube:player_client=ios,android,web"]
    if os.path.exists(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]
    if PROXY:
        cmd += ["--proxy", PROXY]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
    except subprocess.TimeoutExpired:
        return None
    if res.returncode != 0:
        return None
    lines = [l.strip() for l in res.stdout.strip().splitlines() if l.strip()]
    if not lines:
        return None
    video_url = lines[0]
    thumb_url = lines[1] if len(lines) >= 2 else video_url
    return video_url, thumb_url


def _is_youtube(url: str) -> bool:
    return any(d in url for d in ("youtube.com", "youtu.be"))

def _is_tiktok(url: str) -> bool:
    return any(d in url for d in ("tiktok.com", "vm.tiktok", "vt.tiktok"))


def _run_yt_dlp(url: str, folder: str, quality: str) -> Optional[list[str]]:
    os.makedirs(folder, exist_ok=True)

    if quality == "hd":
        fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"
    else:
        fmt = "best[ext=mp4][height<=480]/best[height<=480]/worst[ext=mp4]/worst"

    cmd = [
        "yt-dlp", url,
        "-o", os.path.join(folder, "%(autonumber)04d.%(ext)s"),
        "--no-warnings", "--no-playlist",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "--socket-timeout", "30",
        "--retries", "3",
        "--no-part",
    ]

    if _is_youtube(url):
        cmd += [
            "--extractor-args", "youtube:player_client=ios,android,web",
            "--add-header", "User-Agent:com.google.ios.youtube/19.29.1 CFNetwork/1408.0.4 Darwin/22.5.0",
        ]
    elif _is_tiktok(url):
        cmd += [
            "--add-header", "User-Agent:Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        ]
    else:
        cmd += [
            "--add-header", "User-Agent:Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        ]

    if os.path.exists(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]
    if PROXY:
        cmd += ["--proxy", PROXY]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=DL_TIMEOUT)
    except subprocess.TimeoutExpired:
        logger.warning("Таймаут скачивания: %s", url)
        return None

    if res.returncode != 0:
        logger.error("yt-dlp ошибка [%s]: %s", url, res.stderr[-400:])
        raise RuntimeError(res.stderr)

    files = sorted(
        os.path.join(folder, f) for f in os.listdir(folder)
        if f.lower().endswith(IMAGE_EXTS + VIDEO_EXTS)
    )
    return files or None

# ── Бот ──────────────────────────────────────────────────────────────────────

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

task_queue: asyncio.Queue = asyncio.Queue()
_pending: dict[str, tuple] = {}


def _main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⭐ Подписка"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="📋 Помощь"),   KeyboardButton(text="ℹ️ О боте")],
        ],
        resize_keyboard=True,
        persistent=True,
    )

def _quality_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📹 HD",            callback_data=f"dl:hd:{token}"),
        InlineKeyboardButton(text="📱 SD (быстрее)", callback_data=f"dl:sd:{token}"),
    ]])

def _subscribe_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"⭐ Подписка — {SUBSCRIPTION_PRICE} звёзд / месяц",
            callback_data="buy_sub"
        ),
    ]])

# ── Тексты ───────────────────────────────────────────────────────────────────

START_TEXT = f"""
🎬 <b>MellSave Bot</b>

Скачиваю видео и фото <b>без водяных знаков</b> с:
• TikTok (видео + фото-слайдшоу)
• Instagram Reels
• YouTube Shorts

<b>Бесплатно:</b> {DAILY_LIMIT} скачиваний в день
<b>Подписка ⭐:</b> безлимит за {SUBSCRIPTION_PRICE} звёзд / месяц

<b>Команды:</b>
/help — все возможности
/subscribe — оформить подписку
/stats — моя статистика
"""

HELP_TEXT = f"""
📋 <b>Возможности бота</b>

<b>Поддерживаемые платформы:</b>
• TikTok — видео и фото-слайдшоу
• Instagram Reels
• YouTube Shorts

<b>Качество видео:</b>
• 📹 HD — максимальное качество
• 📱 SD — меньше размер, скачивается быстрее

<b>Бесплатный план:</b>
• {DAILY_LIMIT} скачиваний в день
• Файлы до {MAX_FILE_MB} МБ
• Не более {RATE_LIMIT} запросов в минуту

<b>Подписка ⭐ — {SUBSCRIPTION_PRICE} звёзд / месяц:</b>
• Безлимитные скачивания
• Приоритет в очереди
• Подписка суммируется при продлении

<b>Как пользоваться:</b>
1. Скопируй ссылку из TikTok / Instagram / YouTube
2. Отправь её сюда
3. Выбери HD или SD
4. Получи файл без водяного знака
"""

LIMIT_TEXT = (
    f"😔 Бесплатный лимит исчерпан — сегодня уже {DAILY_LIMIT} скачиваний.\n\n"
    f"Оформи подписку за <b>{SUBSCRIPTION_PRICE} ⭐ звёзд в месяц</b> "
    f"и скачивай <b>без ограничений</b>!\n\n"
    f"Или возвращайся завтра — лимит обнуляется каждый день."
)

# ── Команды ───────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(START_TEXT, parse_mode="HTML", reply_markup=_main_keyboard())


@dp.message(Command("help"))
async def cmd_help(message: Message):
    kb = None if db_is_subscribed(message.from_user.id) else _subscribe_keyboard()
    await message.answer(HELP_TEXT, parse_mode="HTML", reply_markup=kb)


@dp.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    user_id = message.from_user.id
    if db_is_subscribed(user_id):
        expiry = db_sub_expiry(user_id)
        await message.answer(
            f"⭐ У тебя уже активна подписка до <b>{expiry}</b>.\n"
            f"Можешь продлить — дни суммируются!",
            reply_markup=_subscribe_keyboard(),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            f"⭐ <b>Подписка TikTok Save Bot</b>\n\n"
            f"• Безлимитные скачивания\n"
            f"• Срок: {SUBSCRIPTION_DAYS} дней\n"
            f"• Стоимость: {SUBSCRIPTION_PRICE} звёзд Telegram\n\n"
            f"Звёзды можно купить прямо в Telegram — никаких карт не нужно.",
            reply_markup=_subscribe_keyboard(),
            parse_mode="HTML"
        )


@dp.message(F.text == "⭐ Подписка")
async def btn_subscribe(message: Message):
    await cmd_subscribe(message)

@dp.message(F.text == "📊 Статистика")
async def btn_stats(message: Message):
    await cmd_stats(message)

@dp.message(F.text == "📋 Помощь")
async def btn_help(message: Message):
    await cmd_help(message)

@dp.message(F.text == "ℹ️ О боте")
async def btn_about(message: Message):
    await message.answer(START_TEXT, parse_mode="HTML", reply_markup=_main_keyboard())

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    user_id = message.from_user.id
    count = db_today_count(user_id)
    subscribed = db_is_subscribed(user_id)

    if subscribed:
        expiry = db_sub_expiry(user_id)
        text = (
            f"📊 <b>Твоя статистика</b>\n\n"
            f"⭐ Подписка активна до <b>{expiry}</b>\n"
            f"Скачиваний сегодня: {count} (без ограничений)"
        )
    else:
        remaining = max(0, DAILY_LIMIT - count)
        text = (
            f"📊 <b>Твоя статистика на сегодня</b>\n\n"
            f"Скачано: {count} из {DAILY_LIMIT}\n"
            f"Осталось: {remaining}\n\n"
            f"⭐ Хочешь безлимит? /subscribe"
        )
    await message.answer(text, parse_mode="HTML")

# ── Обработка ссылок ──────────────────────────────────────────────────────────

@dp.message(F.text.regexp(URL_RE.pattern))
async def handle_link(message: Message):
    user_id = message.from_user.id

    if rate_limited(user_id):
        await message.answer(
            f"Слишком много запросов — максимум {RATE_LIMIT} в минуту. Подожди немного."
        )
        return

    if not db_is_subscribed(user_id) and db_today_count(user_id) >= DAILY_LIMIT:
        await message.answer(LIMIT_TEXT, reply_markup=_subscribe_keyboard(), parse_mode="HTML")
        return

    url = URL_RE.search(message.text).group(0)
    token = uuid.uuid4().hex[:8]
    _pending[token] = (url, user_id, message, time.monotonic())

    await message.answer("Выбери качество:", reply_markup=_quality_keyboard(token))


@dp.callback_query(F.data.startswith("dl:"))
async def handle_quality_cb(callback: CallbackQuery):
    _, quality, token = callback.data.split(":")

    entry = _pending.pop(token, None)
    if entry is None:
        await callback.answer("Запрос устарел — пришли ссылку заново.", show_alert=True)
        return

    url, user_id, orig_msg, _ = entry
    await callback.message.edit_text("Добавлено в очередь... ⏳")
    await task_queue.put((url, quality, user_id, orig_msg, callback.message))
    await callback.answer()


# ── Telegram Stars: покупка подписки ─────────────────────────────────────────

@dp.callback_query(F.data == "buy_sub")
async def handle_buy_sub(callback: CallbackQuery):
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Подписка MellSave Bot",
        description=f"Безлимитные скачивания на {SUBSCRIPTION_DAYS} дней",
        payload="monthly_sub",
        currency="XTR",
        prices=[LabeledPrice(label=f"Подписка на {SUBSCRIPTION_DAYS} дней", amount=SUBSCRIPTION_PRICE)],
    )
    await callback.answer()


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def on_payment(message: Message):
    user_id = message.from_user.id
    db_activate_subscription(user_id)
    expiry = db_sub_expiry(user_id)
    logger.info("Подписка оплачена: user_id=%s, expires=%s", user_id, expiry)
    await message.answer(
        f"🎉 <b>Спасибо! Подписка активирована.</b>\n\n"
        f"⭐ Действует до: <b>{expiry}</b>\n"
        f"Теперь скачивай без ограничений — просто отправляй ссылки!",
        parse_mode="HTML"
    )


# ── Воркеры очереди ───────────────────────────────────────────────────────────

async def worker():
    while True:
        url, quality, user_id, orig_msg, status_msg = await task_queue.get()
        folder = os.path.join(TMP_DIR, uuid.uuid4().hex)
        try:
            await status_msg.edit_text("Скачиваю... ⏳")

            try:
                files = await asyncio.to_thread(_run_yt_dlp, url, folder, quality)
            except RuntimeError as e:
                err = str(e).lower()
                if "status code 0" in err or "ip address is blocked" in err:
                    await status_msg.edit_text(
                        "❌ TikTok не отдаёт это видео с нашего сервера.\n\n"
                        "Попробуй другую ссылку — некоторые видео доступны только авторизованным пользователям."
                    )
                elif "private" in err:
                    await status_msg.edit_text("❌ Это видео приватное — скачать нельзя.")
                elif "removed" in err or "deleted" in err or "not available" in err:
                    await status_msg.edit_text("❌ Видео удалено или недоступно.")
                elif "sign in" in err or "login" in err or "age" in err:
                    await status_msg.edit_text("❌ Видео требует входа в аккаунт — скачать нельзя.")
                else:
                    await status_msg.edit_text(
                        "❌ Не удалось скачать. Попробуй другую ссылку или зайди позже."
                    )
                continue

            if not files:
                await status_msg.edit_text(
                    "❌ Не удалось скачать. Попробуй позже или другую ссылку."
                )
                continue

            too_big = [f for f in files if os.path.getsize(f) > MAX_FILE_MB * 1024 * 1024]
            if too_big:
                await status_msg.edit_text(
                    f"Файл превышает {MAX_FILE_MB} МБ — Telegram не позволяет его отправить. "
                    "Попробуй SD качество."
                )
                continue

            await status_msg.edit_text("Отправляю...")

            photos = [f for f in files if f.lower().endswith(IMAGE_EXTS)]
            videos = [f for f in files if f.lower().endswith(VIDEO_EXTS)]

            if photos and not videos:
                if len(photos) == 1:
                    await orig_msg.answer_photo(FSInputFile(photos[0]), caption=CAPTION)
                else:
                    media = [InputMediaPhoto(media=FSInputFile(p)) for p in photos]
                    media[0] = InputMediaPhoto(media=FSInputFile(photos[0]), caption=CAPTION)
                    await orig_msg.answer_media_group(media)
            elif videos:
                await orig_msg.answer_video(FSInputFile(videos[0]), caption=CAPTION)

            db_increment(user_id)

            if db_is_subscribed(user_id):
                await status_msg.edit_text("Готово! ✅  (⭐ подписка — безлимит)")
            else:
                remaining = max(0, DAILY_LIMIT - db_today_count(user_id))
                if remaining == 0:
                    await status_msg.edit_text(
                        f"Готово! ✅\n\n"
                        f"😔 Бесплатный лимит исчерпан.\n"
                        f"Оформи подписку за <b>{SUBSCRIPTION_PRICE} ⭐</b> и скачивай без ограничений!",
                        parse_mode="HTML",
                        reply_markup=_subscribe_keyboard()
                    )
                elif remaining == 1:
                    await status_msg.edit_text(
                        f"Готово! ✅  Остался <b>1 бесплатный</b> запрос на сегодня.\n\n"
                        f"⭐ Подписка за {SUBSCRIPTION_PRICE} звёзд — безлимит навсегда!",
                        parse_mode="HTML",
                        reply_markup=_subscribe_keyboard()
                    )
                elif remaining == 2:
                    await status_msg.edit_text(
                        f"Готово! ✅  Осталось <b>{remaining}</b> бесплатных запроса.\n\n"
                        f"⭐ Не хочешь считать? Подписка за {SUBSCRIPTION_PRICE} звёзд / месяц!",
                        parse_mode="HTML",
                        reply_markup=_subscribe_keyboard()
                    )
                else:
                    await status_msg.edit_text(
                        f"Готово! ✅  Осталось сегодня: {remaining}/{DAILY_LIMIT}\n"
                        f"⭐ <a href='https://t.me/MellSaveBot?start=sub'>Безлимит за {SUBSCRIPTION_PRICE} звёзд / мес</a>",
                        parse_mode="HTML"
                    )

        except Exception as exc:
            logger.exception("Ошибка при обработке %s: %s", url, exc)
            try:
                await status_msg.edit_text("Что-то пошло не так. Попробуй ещё раз.")
            except Exception:
                pass
        finally:
            await asyncio.to_thread(shutil.rmtree, folder, ignore_errors=True)
            task_queue.task_done()


# ── Inline режим ─────────────────────────────────────────────────────────────

@dp.inline_query()
async def inline_handler(query: InlineQuery):
    url_match = URL_RE.search(query.query.strip())
    if not url_match:
        await query.answer(
            [],
            cache_time=1,
            switch_pm_text="Вставь ссылку на TikTok / YouTube / Instagram",
            switch_pm_parameter="start",
        )
        return

    url = url_match.group(0)
    result = await asyncio.to_thread(_get_direct_url, url)

    if not result:
        await query.answer([], cache_time=5)
        return

    video_url, thumb_url = result
    await query.answer(
        [
            InlineQueryResultVideo(
                id=uuid.uuid4().hex[:8],
                video_url=video_url,
                mime_type="video/mp4",
                thumbnail_url=thumb_url,
                title="🎬 Отправить видео в чат",
                description="Нажми — видео появится в чате",
                caption=CAPTION,
            )
        ],
        cache_time=300,
    )


# ── Fallback ──────────────────────────────────────────────────────────────────

@dp.message()
async def fallback(message: Message):
    await message.answer(
        "Пришли ссылку на TikTok, Instagram Reels или YouTube Shorts — скачаю без водяного знака.",
        reply_markup=_main_keyboard()
    )


# ── Точка входа ───────────────────────────────────────────────────────────────

def acquire_pid_lock():
    if os.path.exists(PID_FILE):
        try:
            old_pid = int(open(PID_FILE).read().strip())
            os.kill(old_pid, 0)  # проверяем, жив ли процесс
            print(f"[ОШИБКА] Бот уже запущен (PID {old_pid}).")
            print(f"         Останови его командой:  kill {old_pid}")
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            os.remove(PID_FILE)  # устаревший файл — удаляем

    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(PID_FILE) and os.remove(PID_FILE))


async def main():
    acquire_pid_lock()
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан. Создай .env файл с BOT_TOKEN=твой_токен")
    setup_logging()
    db_init()
    os.makedirs(TMP_DIR, exist_ok=True)

    # Записываем cookies из env var при каждом старте
    # Генерируем cookies.txt из TT_SESSION_ID (32-символьный токен сессии TikTok)
    tt_session = os.getenv("TT_SESSION_ID", "").strip()
    if tt_session:
        _write_tiktok_cookies(tt_session)
    elif os.path.exists(COOKIES_FILE):
        logger.info("cookies.txt найден: %s", COOKIES_FILE)

    ver = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True)
    logger.info("yt-dlp версия: %s", ver.stdout.strip())

    if PROXY:
        logger.info("Прокси: %s", PROXY)

    for _ in range(WORKERS):
        asyncio.create_task(worker())
    logger.info("Бот запущен (%d воркеров)", WORKERS)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
