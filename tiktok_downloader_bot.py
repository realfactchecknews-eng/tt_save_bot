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

# ── Скачивание через yt-dlp ───────────────────────────────────────────────────

def _run_yt_dlp(url: str, folder: str, quality: str) -> Optional[list[str]]:
    os.makedirs(folder, exist_ok=True)
    fmt = (
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        if quality == "hd" else
        "worstvideo[ext=mp4]+worstaudio[ext=m4a]/worst[ext=mp4]/worst"
    )
    cmd = [
        "yt-dlp", url,
        "-o", os.path.join(folder, "%(autonumber)04d.%(ext)s"),
        "--no-warnings", "--no-playlist",
        "-f", fmt,
        "--merge-output-format", "mp4",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=DL_TIMEOUT)
    except subprocess.TimeoutExpired:
        logger.warning("Таймаут скачивания: %s", url)
        return None
    if res.returncode != 0:
        logger.error("yt-dlp ошибка [%s]: %s", url, res.stderr[-400:])
        return None
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
🎬 <b>TikTok Save Bot</b>

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
    await message.answer(START_TEXT, parse_mode="HTML")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT, parse_mode="HTML")


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
        title="Подписка TikTok Save Bot",
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

            files = await asyncio.to_thread(_run_yt_dlp, url, folder, quality)

            if not files:
                await status_msg.edit_text(
                    "Не получилось скачать. Контент может быть удалён, приватным "
                    "или платформа временно недоступна — попробуй позже."
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
                footer = (
                    f"Осталось сегодня: {remaining}/{DAILY_LIMIT}"
                    if remaining > 0 else
                    f"Лимит на сегодня исчерпан — /subscribe для безлимита ⭐"
                )
                await status_msg.edit_text(f"Готово! ✅  {footer}")

        except Exception as exc:
            logger.exception("Ошибка при обработке %s: %s", url, exc)
            try:
                await status_msg.edit_text("Что-то пошло не так. Попробуй ещё раз.")
            except Exception:
                pass
        finally:
            await asyncio.to_thread(shutil.rmtree, folder, ignore_errors=True)
            task_queue.task_done()


# ── Fallback ──────────────────────────────────────────────────────────────────

@dp.message()
async def fallback(message: Message):
    await message.answer(
        "Пришли ссылку на TikTok, Instagram Reels или YouTube Shorts — скачаю без водяного знака.\n"
        "Команда /help — список всех возможностей."
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
    for _ in range(WORKERS):
        asyncio.create_task(worker())
    logger.info("Бот запущен (%d воркеров)", WORKERS)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
