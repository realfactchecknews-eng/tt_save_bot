# TikTok Downloader Bot

Telegram-бот, который скачивает видео по ссылке (TikTok и т.д.) и отправляет файл обратно в чат.

## Установка

```bash
pip install -r requirements.txt
```

Также нужен `ffmpeg` в системе:

```bash
apt install ffmpeg   # Debian/Ubuntu (на сервере)
brew install ffmpeg  # Mac (для локального теста)
```

## Настройка

1. Создай бота через [@BotFather](https://t.me/BotFather) в Telegram, получи токен.
1. Скопируй `.env.example` в `.env`:
   
   ```bash
   cp .env.example .env
   ```
1. Впиши токен в `.env`:
   
   ```
   TIKTOK_BOT_TOKEN=твой_токен
   ```

## Запуск

```bash
python3 tiktok_downloader_bot.py
```

## Как пользоваться

Просто пришли боту ссылку на TikTok-видео — он скачает и отправит файл обратно в чат.

## Деплой

Деплоится так же, как основной бот (через bothost или аналогичный хостинг):

1. Закинуть файлы репозитория на хостинг
1. Указать `TIKTOK_BOT_TOKEN` в переменных окружения хостинга
1. Команда запуска: `python3 tiktok_downloader_bot.py`