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
import json
import uuid
import shutil
import sqlite3
import time
import threading
import urllib.request
import urllib.parse
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, FSInputFile, InputMediaPhoto,
    CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineQuery, InlineQueryResultVideo, InlineQueryResultsButton,
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


# ── TikTok через tikwm.com ────────────────────────────────────────────────────
# TikTok блокирует IP дата-центров (status_code=0), поэтому видео резолвим через
# сторонний сервис tikwm: он сам ходит к TikTok и отдаёт прямой mp4 без вотермарка.

TIKWM_API = "https://www.tikwm.com/api/"
_BROWSER_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def _http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# Глобальный троттл: tikwm даёт ~1 запрос/сек на IP, а воркеров несколько.
# Гейт пускает к API не чаще TIKWM_MIN_INTERVAL независимо от числа потоков.
TIKWM_MIN_INTERVAL = 1.1   # секунд между запросами к tikwm
TIKWM_RETRIES      = 4     # попыток при лимите/сбое
_tikwm_lock = threading.Lock()
_tikwm_last = 0.0


def _tikwm_gate() -> None:
    """Блокирует поток, пока не пройдёт минимальный интервал с прошлого запроса."""
    global _tikwm_last
    with _tikwm_lock:
        wait = TIKWM_MIN_INTERVAL - (time.monotonic() - _tikwm_last)
        if wait > 0:
            time.sleep(wait)
        _tikwm_last = time.monotonic()


def _tikwm_fetch(url: str, timeout: int = 20) -> Optional[dict]:
    """Запрашивает данные о видео у tikwm. Возвращает блок data или None."""
    api_url = TIKWM_API + "?" + urllib.parse.urlencode({"url": url, "hd": 1})
    for attempt in range(TIKWM_RETRIES):
        _tikwm_gate()
        try:
            data = json.loads(_http_get(api_url, timeout=timeout))
        except Exception as e:
            logger.warning("tikwm запрос не удался [%s]: %s", url, e)
            data = None
        if data and data.get("code") == 0 and data.get("data"):
            return data["data"]
        # code != 0 обычно значит лимит бесплатного API — ждём с нарастанием и повторяем
        if data is not None:
            logger.warning("tikwm code=%s msg=%s", data.get("code"), data.get("msg"))
        if attempt < TIKWM_RETRIES - 1:
            time.sleep(1.5 * (attempt + 1))
    return None


def _tikwm_abs(link: str) -> str:
    return ("https://www.tikwm.com" + link) if link.startswith("/") else link


def _download_tiktok(url: str, folder: str, quality: str) -> Optional[list[str]]:
    """Скачивает TikTok видео или фото-слайдшоу через tikwm."""
    os.makedirs(folder, exist_ok=True)
    d = _tikwm_fetch(url)
    if not d:
        return None

    files: list[str] = []
    images = d.get("images")
    if images:  # фото-слайдшоу
        for i, img in enumerate(images):
            path = os.path.join(folder, f"{i:04d}.jpg")
            try:
                with open(path, "wb") as f:
                    f.write(_http_get(_tikwm_abs(img), timeout=30))
                files.append(path)
            except Exception as e:
                logger.warning("Не скачал фото %s: %s", img, e)
    else:  # видео
        play = d.get("hdplay") if quality == "hd" else d.get("play")
        play = play or d.get("play") or d.get("hdplay")
        if not play:
            return None
        path = os.path.join(folder, "0001.mp4")
        try:
            with open(path, "wb") as f:
                f.write(_http_get(_tikwm_abs(play), timeout=DL_TIMEOUT))
            files.append(path)
        except Exception as e:
            logger.error("Не скачал видео TikTok [%s]: %s", url, e)
            return None

    return files or None


def _tiktok_direct_url(url: str) -> Optional[tuple[str, str]]:
    """Прямая ссылка на mp4 и превью для inline-режима (через tikwm)."""
    d = _tikwm_fetch(url, timeout=12)
    if not d:
        return None
    play = d.get("play") or d.get("hdplay")
    if not play:
        return None
    cover = d.get("cover") or d.get("origin_cover") or play
    return _tikwm_abs(play), _tikwm_abs(cover)


# ── YouTube ───────────────────────────────────────────────────────────────────
# YouTube блокирует IP дата-центров. Пробуем три метода по очереди:
# 1) yt-dlp с tv_embedded/ios клиентами (не требуют авторизации)
# 2) cobalt.tools API
# 3) Invidious публичные инстансы

def _youtube_video_id(url: str) -> Optional[str]:
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def _clean_youtube_url(url: str) -> str:
    """Убирает tracking/sharing параметры, оставляет только video ID."""
    vid = _youtube_video_id(url)
    if not vid:
        return url
    if "/shorts/" in url:
        return f"https://www.youtube.com/shorts/{vid}"
    return f"https://www.youtube.com/watch?v={vid}"


# ── Метод 1: yt-dlp с embedded/app клиентами ──────────────────────────────────

def _ytdlp_youtube(url: str, folder: str, quality: str) -> Optional[list[str]]:
    """yt-dlp с клиентами tv_embedded/ios — они не требуют Sign In на публичных видео."""
    if quality == "hd":
        fmt = "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/22/18/best[ext=mp4]/best"
    else:
        fmt = "18/bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/worst[ext=mp4]/worst"
    cmd = [
        "yt-dlp", url,
        "-o", os.path.join(folder, "%(autonumber)04d.%(ext)s"),
        "--no-warnings", "--no-playlist",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "--extractor-args", "youtube:player_client=tv_embedded,ios,web_embedded",
        "--socket-timeout", "30",
        "--retries", "2",
        "--no-part",
    ]
    if PROXY:
        cmd += ["--proxy", PROXY]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=DL_TIMEOUT)
    except subprocess.TimeoutExpired:
        logger.warning("yt-dlp YouTube таймаут: %s", url)
        return None
    if res.returncode != 0:
        logger.warning("yt-dlp YouTube failed: %s", res.stderr[-300:])
        return None
    files = sorted(
        os.path.join(folder, f) for f in os.listdir(folder)
        if f.lower().endswith(IMAGE_EXTS + VIDEO_EXTS)
    )
    return files or None


# ── Метод 2: cobalt.tools ─────────────────────────────────────────────────────

COBALT_API          = "https://api.cobalt.tools/"
_YT_QUALITY         = {"hd": "1080", "sd": "480"}
COBALT_MIN_INTERVAL = 2.0
_cobalt_lock        = threading.Lock()
_cobalt_last        = 0.0


def _cobalt_gate() -> None:
    global _cobalt_last
    with _cobalt_lock:
        wait = COBALT_MIN_INTERVAL - (time.monotonic() - _cobalt_last)
        if wait > 0:
            time.sleep(wait)
        _cobalt_last = time.monotonic()


def _cobalt_fetch(url: str, quality: str = "hd", timeout: int = 25) -> Optional[str]:
    _cobalt_gate()
    payload = json.dumps({
        "url": url,
        "videoQuality": _YT_QUALITY.get(quality, "720"),
        "youtubeVideoCodec": "h264",
        "downloadMode": "auto",
        "filenameStyle": "basic",
    }).encode()
    req = urllib.request.Request(
        COBALT_API, data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": _BROWSER_UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        status = data.get("status")
        if status in ("tunnel", "redirect", "stream"):
            return data.get("url")
        logger.warning("cobalt status=%s error=%s", status, data.get("error"))
    except urllib.error.HTTPError as e:
        logger.warning("cobalt HTTP %s: %s", e.code, e.read().decode(errors="replace")[:200])
    except Exception as e:
        logger.warning("cobalt ошибка: %s", e)
    return None


# ── Метод 3: Invidious ────────────────────────────────────────────────────────

_INVIDIOUS_INSTANCES = [
    "https://inv.nadeko.net",
    "https://invidious.privacyredirect.com",
    "https://yt.cdaut.de",
]
_INVIDIOUS_ITAGS = {"hd": "22", "sd": "18"}  # 22=720p mp4+audio, 18=360p mp4+audio


def _invidious_url(vid: str, quality: str) -> Optional[str]:
    itag = _INVIDIOUS_ITAGS.get(quality, "18")
    for instance in _INVIDIOUS_INSTANCES:
        url = f"{instance}/latest_version?id={vid}&itag={itag}&local=true"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
            req.get_method = lambda: "HEAD"
            with urllib.request.urlopen(req, timeout=6) as resp:
                if resp.status == 200:
                    logger.info("Invidious OK: %s", instance)
                    return url
        except Exception:
            pass
    return None


# ── Публичный интерфейс ───────────────────────────────────────────────────────

def _yt_resolve_url(vid: str, url: str, quality: str) -> Optional[str]:
    """cobalt → Invidious: возвращает прямую ссылку для скачивания/инлайна."""
    clean = _clean_youtube_url(url)
    result = _cobalt_fetch(clean, quality)
    if result:
        return result
    logger.info("cobalt не сработал, пробуем Invidious для %s", vid)
    return _invidious_url(vid, quality)


def _download_youtube(url: str, folder: str, quality: str) -> Optional[list[str]]:
    os.makedirs(folder, exist_ok=True)
    vid = _youtube_video_id(url)
    clean = _clean_youtube_url(url)

    # 1. yt-dlp с embedded клиентами
    files = _ytdlp_youtube(clean, folder, quality)
    if files:
        return files

    logger.info("yt-dlp YouTube не сработал, пробуем внешние сервисы")

    # 2/3. cobalt или Invidious → скачиваем файл сами
    video_url = _yt_resolve_url(vid, url, quality) if vid else None
    if not video_url:
        return None
    path = os.path.join(folder, "0001.mp4")
    try:
        with open(path, "wb") as f:
            f.write(_http_get(video_url, timeout=DL_TIMEOUT))
        return [path]
    except Exception as e:
        logger.error("Не скачал YouTube [%s]: %s", url, e)
        return None


def _youtube_direct_url(url: str) -> Optional[tuple[str, str]]:
    vid = _youtube_video_id(url)
    if not vid:
        return None
    # Для инлайна нужен прямой URL — yt-dlp не подходит
    video_url = _yt_resolve_url(vid, url, "hd")
    if not video_url:
        return None
    thumb = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
    return video_url, thumb


def _get_direct_url(url: str) -> Optional[tuple[str, str]]:
    """Быстро извлекает прямую ссылку на видео и превью без скачивания."""
    if _is_tiktok(url):
        return _tiktok_direct_url(url)
    if _is_youtube(url):
        return _youtube_direct_url(url)
    cmd = [
        "yt-dlp", url,
        "--print", "url",
        "--print", "thumbnail",
        "--no-warnings", "--no-playlist",
        "--no-check-formats",
        "-f", "best[ext=mp4][height<=720]/best[ext=mp4]/best",
        "--socket-timeout", "15",
    ]
    if os.path.exists(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]
    if PROXY:
        cmd += ["--proxy", PROXY]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
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
    if _is_tiktok(url):
        return _download_tiktok(url, folder, quality)
    if _is_youtube(url):
        return _download_youtube(url, folder, quality)

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
        "--add-header", f"User-Agent:{_BROWSER_UA}",
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
_PENDING_TTL = 300  # секунд — сколько ждём нажатия HD/SD


async def _cleanup_pending():
    """Раз в минуту удаляет токены, которые юзер проигнорировал > 5 минут."""
    while True:
        await asyncio.sleep(60)
        cutoff = time.monotonic() - _PENDING_TTL
        stale = [k for k, v in _pending.items() if v[3] < cutoff]
        for k in stale:
            _pending.pop(k, None)
        if stale:
            logger.debug("Удалено %d устаревших pending-токенов", len(stale))


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
    open_bot_btn = InlineQueryResultsButton(
        text="Вставь ссылку на TikTok / YouTube / Instagram",
        start_parameter="start",
    )

    url_match = URL_RE.search(query.query.strip())
    if not url_match:
        await query.answer([], cache_time=1, button=open_bot_btn)
        return

    url = url_match.group(0)
    result = await asyncio.to_thread(_get_direct_url, url)

    if not result:
        await query.answer(
            [], cache_time=5,
            button=InlineQueryResultsButton(
                text="Не удалось получить видео — открой бота",
                start_parameter="start",
            ),
        )
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

    # TikTok: cookies для tikwm не нужны, но пишем файл на случай если yt-dlp понадобится для других платформ
    tt_session = os.getenv("TT_SESSION_ID", "").strip()
    if tt_session:
        _write_tiktok_cookies(tt_session)

    ver = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True)
    logger.info("yt-dlp версия: %s | TikTok→tikwm | YouTube→cobalt | прокси: %s",
                ver.stdout.strip(), PROXY or "нет")

    for _ in range(WORKERS):
        asyncio.create_task(worker())
    asyncio.create_task(_cleanup_pending())
    logger.info("Бот запущен (%d воркеров)", WORKERS)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
