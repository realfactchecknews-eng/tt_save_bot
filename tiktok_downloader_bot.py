"""
Telegram-бот: принимает ссылку на видео (TikTok и т.д.), скачивает его
и отправляет файл видео обратно в чат.

Установка зависимостей:
    pip install aiogram yt-dlp python-dotenv

Нужен ffmpeg в системе (на сервере обычно через apt):
    apt install ffmpeg   (Debian/Ubuntu)
    brew install ffmpeg  (Mac)

Запуск:
    python3 tiktok_downloader_bot.py
"""

import os
import asyncio
import subprocess
import logging
import re

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile
from aiogram.filters import Command
from dotenv import load_dotenv

load_dotenv()

# ---------- НАСТРОЙКИ ----------

BOT_TOKEN = os.getenv("BOT_TOKEN")  # положи токен в .env файл

ВРЕМЕННАЯ_ПАПКА = "tmp_videos"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

URL_REGEX = re.compile(r"https?://\S+")


def скачать_видео(ссылка: str, папка: str):
    """Скачивает видео по ссылке, возвращает путь к файлу или None."""
    os.makedirs(папка, exist_ok=True)

    команда = [
        "yt-dlp",
        ссылка,
        "-o", os.path.join(папка, "%(id)s.%(ext)s"),
        "--no-warnings",
    ]

    результат = subprocess.run(команда, capture_output=True, text=True)
    if результат.returncode != 0:
        logger.error(f"yt-dlp ошибка: {результат.stderr[-500:]}")
        return None

    видео_файлы = [
        os.path.join(папка, f) for f in os.listdir(папка)
        if f.endswith((".mp4", ".webm"))
    ]
    if not видео_файлы:
        return None

    return max(видео_файлы, key=os.path.getmtime)


def удалить_видео(путь: str):
    if os.path.exists(путь):
        os.remove(путь)


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Пришли мне ссылку на TikTok-видео — скачаю и отправлю файл обратно."
    )


@dp.message(F.text.regexp(URL_REGEX.pattern))
async def handle_link(message: Message):
    ссылка_match = URL_REGEX.search(message.text)
    if not ссылка_match:
        return
    ссылка = ссылка_match.group(0)

    статус = await message.answer("Скачиваю видео...")

    путь = await asyncio.to_thread(скачать_видео, ссылка, ВРЕМЕННАЯ_ПАПКА)

    if путь is None:
        await статус.edit_text(
            "Не получилось скачать это видео. Возможно, оно удалено, приватное, "
            "или TikTok временно блокирует запрос — попробуй другую ссылку или чуть позже."
        )
        return

    try:
        await message.answer_video(FSInputFile(путь))
        await статус.delete()
    except Exception as e:
        logger.error(f"Ошибка отправки видео: {e}")
        await статус.edit_text("Видео скачалось, но не получилось отправить (возможно, слишком большое).")
    finally:
        await asyncio.to_thread(удалить_видео, путь)


@dp.message()
async def fallback(message: Message):
    await message.answer("Пришли мне ссылку на TikTok-видео, и я скачаю его для тебя.")


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN. Создай файл .env с BOT_TOKEN=твой_токен")
    os.makedirs(ВРЕМЕННАЯ_ПАПКА, exist_ok=True)
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
