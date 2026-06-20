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
from datetime import date
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, FSInputFile, InputMediaPhoto,
    CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import Command
from dotenv import load_dotenv

load_dotenv()

# ── Конфигурация ──────────────────────────────────────────────────────────────

BOT_TOKEN   = os.getenv("BOT_TOKEN")
TMP_DIR     = "tmp_media"
DB_FILE     = "bot.db"
LOG_FILE    = "bot.log"
CAPTION     = "Скачано с помощью @TikTok_SaveVideo_ForFree_bot"
MAX_FILE_MB = 50          # Telegram ограничение для Bot API
DL_TIMEOUT  = 90          # секунд на скачивание одного файла
RATE_LIMIT  = 3           # максимум запросов в минуту с одного пользователя
DAILY_LIMIT = 10          # скачиваний в день на пользователя
WORKERS     = 3           # параллельных скачиваний

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
# token -> (url, user_id, original_message, created_at)
_pending: dict[str, tuple] = {}


def _quality_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📹 HD",            callback_data=f"dl:hd:{token}"),
        InlineKeyboardButton(text="📱 SD (быстрее)", callback_data=f"dl:sd:{token}"),
    ]])


# ── Команды ───────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Пришли ссылку на TikTok, Instagram Reels или YouTube Shorts.\n"
        "Скачаю видео или фото и отправлю обратно.\n\n"
        f"Лимит: {DAILY_LIMIT} скачиваний в день.\n"
        "Команда /stats — посмотреть свою статистику."
    )

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    count = db_today_count(message.from_user.id)
    remaining = DAILY_LIMIT - count
    await message.answer(
        f"Сегодня скачано: {count} из {DAILY_LIMIT}.\n"
        f"Осталось: {remaining}."
    )


# ── Обработка ссылок ──────────────────────────────────────────────────────────

@dp.message(F.text.regexp(URL_RE.pattern))
async def handle_link(message: Message):
    user_id = message.from_user.id

    if rate_limited(user_id):
        await message.answer(
            f"Слишком много запросов — максимум {RATE_LIMIT} в минуту. Подожди немного."
        )
        return

    if db_today_count(user_id) >= DAILY_LIMIT:
        await message.answer(
            f"Достигнут лимит {DAILY_LIMIT} скачиваний на сегодня. Возвращайся завтра!"
        )
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


@dp.message()
async def fallback(message: Message):
    await message.answer(
        "Пришли ссылку на TikTok, Instagram Reels или YouTube Shorts, и я скачаю его для тебя."
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

            # Проверка размера файла
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
            remaining = DAILY_LIMIT - db_today_count(user_id)
            await status_msg.edit_text(
                f"Готово! ✅  Осталось скачиваний сегодня: {remaining}/{DAILY_LIMIT}"
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


# ── Точка входа ───────────────────────────────────────────────────────────────

async def main():
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
