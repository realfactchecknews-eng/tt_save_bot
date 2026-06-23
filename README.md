# MellSave Bot (@MellSaveBot)

Telegram-бот для скачивания видео и фото с TikTok, YouTube Shorts, Instagram Reels.

---

## Стек

- Python 3.11+
- [aiogram](https://docs.aiogram.dev/) 3.17.0 — async Telegram Bot framework
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) 2024.11.04 — скачивание видео (версия зафиксирована)
- SQLite — статистика, подписки
- ffmpeg — мёрж видео+аудио для YouTube

---

## Возможности

- Скачивание видео с TikTok, YouTube Shorts, Instagram Reels и любых сайтов поддерживаемых yt-dlp
- Скачивание фото-слайдшоу из TikTok (отправляет как альбом)
- Выбор качества: HD / SD
- Инлайн-режим: `@MellSaveBot <ссылка>` в любом чате
- Telegram Stars подписка (99 ⭐/месяц) — безлимитные скачивания
- Бесплатный лимит: 5 скачиваний в день
- Статистика скачиваний в SQLite
- Персистентная клавиатура (⭐ Подписка, 📊 Статистика, 📋 Помощь, ℹ️ О боте)
- Очередь задач (asyncio.Queue, 3 воркера параллельно)
- Rate limiting (не чаще 1 раза в 3 сек на пользователя)
- PID-файл (защита от двойного запуска)
- Ротирующиеся логи (5MB × 3 файла)

---

## Установка

```bash
apt install ffmpeg python3 python3-pip
pip install -r requirements.txt
```

---

## Конфигурация

Создай файл `.env` в папке бота:

```env
BOT_TOKEN=токен_от_BotFather
```

### Опциональные переменные окружения

| Переменная | Описание |
|---|---|
| `TT_SESSION_ID` | `sessionid` из cookies TikTok (32 hex символа) |
| `TT_UID` | `uid_tt` из cookies TikTok (64 hex символа) |
| `PROXY` | Прокси: `socks5://user:pass@host:port` или `http://...` |

При наличии `TT_SESSION_ID` бот автоматически генерирует `cookies.txt` при старте.

Также поддерживается полный файл `cookies.txt` в папке бота или в `/app/shared/cookies.txt`.

### Как получить TT_SESSION_ID и TT_UID

1. Установи расширение **"Get cookies.txt LOCALLY"** в Chrome/Firefox
2. Войди на [tiktok.com](https://tiktok.com) в аккаунт
3. Нажми расширение → Export
4. Открой файл и найди:
   - `sessionid` → это `TT_SESSION_ID`
   - `uid_tt` → это `TT_UID`

---

## Запуск

```bash
python3 tiktok_downloader_bot.py
```

---

## Деплой на bothost.ru

**Репозиторий:** `https://github.com/realfactchecknews-eng/tt_save_bot`  
**Ветка:** `main`

**Переменные окружения (обязательно):**
- `BOT_TOKEN`

**Переменные для TikTok (опционально но рекомендуется):**
- `TT_SESSION_ID`
- `TT_UID`

**Для TikTok через shared storage:**
1. bothost.ru → "Общие файлы" → загрузи `cookies.txt`
2. Настройки бота → включи "Общее хранилище"
3. Редеплой — бот найдёт куки по пути `/app/shared/cookies.txt`

---

## Структура файлов

```
tiktok_downloader_bot.py   — основной файл бота
requirements.txt           — зависимости (yt-dlp зафиксирован на 2024.11.04)
.env                       — токены (в .gitignore, не коммитить)
cookies.txt                — TikTok куки (в .gitignore, не коммитить)
bot.db                     — SQLite база данных
bot.log                    — логи (ротируются)
bot.pid                    — PID-файл (удаляется при нормальном завершении)
tmp_media/                 — временные файлы при скачивании (очищаются)
```

---

## Ключевые параметры (в начале кода)

```python
DAILY_LIMIT        = 5     # бесплатных скачиваний в день
SUBSCRIPTION_PRICE = 99    # Telegram Stars за подписку
SUBSCRIPTION_DAYS  = 30    # дней подписки
MAX_FILE_MB        = 50    # макс. размер файла для отправки
DL_TIMEOUT         = 90    # таймаут скачивания (сек)
WORKERS            = 3     # параллельных воркеров очереди
RATE_LIMIT         = 3     # сек между запросами от одного пользователя
```

---

## Известные проблемы

### TikTok не скачивается (status code 0 / requires login)

TikTok блокирует запросы с IP датацентров. Решения по приоритету:

1. **Куки:** добавь `TT_SESSION_ID` + `TT_UID` в env vars
2. **Полные куки:** загрузи `cookies.txt` в shared storage bothost.ru
3. **Прокси:** `PROXY=socks5://...` с residential IP

> ⚠️ Версия yt-dlp зафиксирована на `2024.11.04` — не обновляй без тестирования,
> новые версии могут ломать TikTok.

### YouTube без звука

Решено через iOS player client в extractor-args.

### Инлайн-режим не работает

Включить в BotFather: `/mybots → @MellSaveBot → Bot Settings → Inline Mode → Turn on`

---

## BotFather

- **Username:** @MellSaveBot
- **Inline Mode:** включён
- **Payments:** Telegram Stars (для подписки ⭐)
