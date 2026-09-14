#!/usr/bin/env python3
"""
Раз в неделю в Telegram-канал школы выкладывают пост-картинку с расписанием
и подписью, содержащей тег "#расписание".

Этот скрипт:
1. Через Telegram Bot API забирает новые посты канала (getUpdates).
2. Находит среди них самый свежий пост, у которого есть фото и подпись
   содержит "#расписание" (без учёта регистра).
3. Скачивает это фото в максимальном качестве и сохраняет в корень
   репозитория как schedule.jpg (сайт ссылается именно на этот файл).
4. Пишет schedule-meta.json с временем обновления — сайт показывает
   надпись "Обновлено: ...".
5. Запоминает, до какого update_id дошли (telegram_offset.txt), чтобы
   при следующем запуске не обрабатывать те же посты заново.

Если новых подходящих постов не было — скрипт просто ничего не меняет
(картинка на сайте остаётся прежней).

Переменные окружения (задаются как секреты в GitHub Actions):
  TELEGRAM_BOT_TOKEN — токен бота от @BotFather
  TELEGRAM_CHANNEL   — юзернейм канала, например "@domwcs"
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

API_BASE = "https://api.telegram.org/bot{token}"
TAG_RE = re.compile(r"#расписание", re.IGNORECASE)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OFFSET_FILE = os.path.join(REPO_ROOT, "scripts", "telegram_offset.txt")
SCHEDULE_IMAGE = os.path.join(REPO_ROOT, "schedule.jpg")
SCHEDULE_META = os.path.join(REPO_ROOT, "schedule-meta.json")


def api_call(token, method, params=None):
    url = API_BASE.format(token=token) + "/" + method
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.load(resp)
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error on {method}: {data}")
    return data["result"]


def read_offset():
    if os.path.exists(OFFSET_FILE):
        with open(OFFSET_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content.isdigit():
                return int(content)
    return None


def write_offset(offset):
    with open(OFFSET_FILE, "w", encoding="utf-8") as f:
        f.write(str(offset))


def normalize_channel(channel):
    # Позволяем задать канал и как "@domwcs", и как "domwcs"
    return channel if channel.startswith("@") else "@" + channel


def find_latest_schedule_post(updates, channel):
    best = None  # (date, largest_photo_file_id)
    for update in updates:
        post = update.get("channel_post")
        if not post:
            continue
        chat = post.get("chat", {})
        chat_username = chat.get("username")
        if chat_username and normalize_channel(chat_username) != channel:
            continue

        caption = post.get("caption", "") or ""
        photos = post.get("photo")
        if not photos or not TAG_RE.search(caption):
            continue

        # photo — список размеров одного и того же снимка, последний — самый крупный
        largest = photos[-1]
        date_ts = post.get("date", 0)
        if best is None or date_ts >= best[0]:
            best = (date_ts, largest["file_id"])
    return best


def download_file(token, file_id, dest_path):
    file_info = api_call(token, "getFile", {"file_id": file_id})
    file_path = file_info["file_path"]
    file_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    urllib.request.urlretrieve(file_url, dest_path)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    channel = os.environ.get("TELEGRAM_CHANNEL")
    if not token or not channel:
        print("Не заданы TELEGRAM_BOT_TOKEN и/или TELEGRAM_CHANNEL", file=sys.stderr)
        sys.exit(1)
    channel = normalize_channel(channel)

    offset = read_offset()
    params = {"timeout": 0, "allowed_updates": json.dumps(["channel_post"])}
    if offset is not None:
        params["offset"] = offset + 1

    updates = api_call(token, "getUpdates", params)

    if not updates:
        print("Новых постов нет.")
        return

    # Сдвигаем offset на последний увиденный update_id, чтобы не читать их снова,
    # даже если ни один не подошёл под формат расписания.
    max_update_id = max(u["update_id"] for u in updates)
    write_offset(max_update_id)

    result = find_latest_schedule_post(updates, channel)
    if result is None:
        print("Среди новых постов нет картинки с тегом #расписание.")
        return

    _, file_id = result
    download_file(token, file_id, SCHEDULE_IMAGE)

    meta = {"updated": datetime.now(timezone.utc).isoformat()}
    with open(SCHEDULE_META, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"Расписание обновлено: {SCHEDULE_IMAGE}")


if __name__ == "__main__":
    import urllib.parse
    main()
