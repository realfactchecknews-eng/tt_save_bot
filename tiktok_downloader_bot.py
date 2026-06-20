"""
Telegram-бот: принимает ссылку на TikTok, скачивает видео или фото-слайдшоу
и отправляет файлы обратно в чат.

Установка зависимостей:
    pip install aiogram yt-dlp python-dotenv

Нужен ffmpeg в системе:
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
import uuid
import shutil

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile, InputMediaPhoto, InputMediaVideo
from aiogram.filters import Command
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

TMP_DIR = "tmp_media"
CAPTION = "Скачано с помощью @TikTok_SaveVideo_ForFree_bot"

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
VIDEO_EXTS = (".mp4", ".webm", ".mov")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

URL_REGEX = re.compile(r"https?://\S+")


def скачать_медиа(ссылка: str, папка: str):
    """Скачивает медиа по ссылке в отдельную папку, возвращает список файлов или None."""
    os.makedirs(папка, exist_ok=True)

    команда = [
        "yt-dlp",
        ссылка,
        "-o", os.path.join(папка, "%(autonumber)04d.%(ext)s"),
        "--no-warnings",
        "--no-playlist",
    ]

    результат = subprocess.run(команда, capture_output=True, text=True)
    if результат.returncode != 0:
        logger.error(f"yt-dlp ошибка: {результат.stderr[-500:]}")
        return None

    файлы = sorted([
        os.path.join(папка, f)
        for f in os.listdir(папка)
        if f.lower().endswith(IMAGE_EXTS + VIDEO_EXTS)
    ])

    return файлы if файлы else None


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Пришли мне ссылку на TikTok — скачаю видео или фото и отправлю обратно."
    )


@dp.message(F.text.regexp(URL_REGEX.pattern))
async def handle_link(message: Message):
    match = URL_REGEX.search(message.text)
    if not match:
        return
    ссылка = match.group(0)

    папка = os.path.join(TMP_DIR, str(uuid.uuid4()))
    статус = await message.answer("Скачиваю...")

    файлы = await asyncio.to_thread(скачать_медиа, ссылка, папка)

    if not файлы:
        await статус.edit_text(
            "Не получилось скачать. Возможно, контент удалён, приватный "
            "или TikTok временно блокирует запрос — попробуй позже."
        )
        return

    try:
        фото = [f for f in файлы if f.lower().endswith(IMAGE_EXTS)]
        видео = [f for f in файлы if f.lower().endswith(VIDEO_EXTS)]

        if фото and not видео:
            if len(фото) == 1:
                await message.answer_photo(FSInputFile(фото[0]), caption=CAPTION)
            else:
                медиа = [InputMediaPhoto(media=FSInputFile(p)) for p in фото]
                медиа[0] = InputMediaPhoto(media=FSInputFile(фото[0]), caption=CAPTION)
                await message.answer_media_group(медиа)
        elif видео:
            await message.answer_video(FSInputFile(видео[0]), caption=CAPTION)

        await статус.delete()
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")
        await статус.edit_text(
            "Скачалось, но не удалось отправить (возможно, слишком большой файл)."
        )
    finally:
        await asyncio.to_thread(shutil.rmtree, папка, ignore_errors=True)


@dp.message()
async def fallback(message: Message):
    await message.answer("Пришли мне ссылку на TikTok, и я скачаю видео или фото.")


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN. Создай файл .env с BOT_TOKEN=твой_токен")
    os.makedirs(TMP_DIR, exist_ok=True)
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
