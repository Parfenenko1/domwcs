#!/usr/bin/env python3
"""
Раз в сутки (и по кнопке вручную) скрипт:
1. Через Telegram Bot API забирает новые посты канала (getUpdates) — ОДИН раз
   для обоих сценариев, чтобы посты не терялись между расписанием и новостями.
2. Расписание: если среди новых постов есть картинка с тегом "#расписание" —
   скачивает самую свежую как schedule.jpg (работает так же, как раньше).
3. Новости: если среди новых постов есть текст с тегом "#Новости" — переносит
   текст поста на сайт как карточку события (без ИИ, бесплатно):
   - первая строка текста -> название карточки
   - остальные строки -> описание
   - дата -> дата публикации поста
   - тег справа -> если в тексте нашлось время (18:00-19:00) или цена (500 ₽) —
     возьмёт их, иначе поставит "Подробности в Telegram"
   Добавляет карточку в начало events.json, оставляя не больше MAX_EVENTS штук.

Один offset-файл (scripts/telegram_offset.txt) на оба сценария.

Переменные окружения (задаются как секреты в GitHub Actions):
  TELEGRAM_BOT_TOKEN — токен бота от @BotFather
  TELEGRAM_CHANNEL   — юзернейм канала, например "@domwcs"
"""

import json
import os
import re
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone

API_BASE = "https://api.telegram.org/bot{token}"
SCHEDULE_TAG_RE = re.compile(r"#расписание", re.IGNORECASE)
NEWS_TAG_RE = re.compile(r"#новости", re.IGNORECASE)
TIME_RANGE_RE = re.compile(r"\d{1,2}[:.]\d{2}\s*[-–—]\s*\d{1,2}[:.]\d{2}")
PRICE_RE = re.compile(r"\d[\d\s]{0,6}\s*(₽|руб\.?|рублей)", re.IGNORECASE)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OFFSET_FILE = os.path.join(REPO_ROOT, "scripts", "telegram_offset.txt")
SCHEDULE_IMAGE = os.path.join(REPO_ROOT, "schedule.jpg")
SCHEDULE_META = os.path.join(REPO_ROOT, "schedule-meta.json")
EVENTS_FILE = os.path.join(REPO_ROOT, "events.json")
EVENTS_META = os.path.join(REPO_ROOT, "events-meta.json")
MAX_EVENTS = 6

MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


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
    return channel if channel.startswith("@") else "@" + channel


def download_file(token, file_id, dest_path):
    file_info = api_call(token, "getFile", {"file_id": file_id})
    file_path = file_info["file_path"]
    file_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    urllib.request.urlretrieve(file_url, dest_path)


def strip_hashtags(line):
    return re.sub(r"#\S+", "", line).strip()


def build_event_card(text, date_ts):
    # Убираем теги вида #новости из текста и разбиваем на строки
    lines = [strip_hashtags(l).strip() for l in text.splitlines()]
    lines = [l for l in lines if l]  # убираем пустые строки

    name = lines[0] if lines else "Новость"
    if len(name) > 80:
        name = name[:77].rstrip() + "…"

    desc = " ".join(lines[1:]) if len(lines) > 1 else ""
    if len(desc) > 160:
        desc = desc[:157].rstrip() + "…"

    time_match = TIME_RANGE_RE.search(text)
    price_match = PRICE_RE.search(text)
    if time_match:
        tag = time_match.group(0).replace(".", ":")
    elif price_match:
        tag = "Участие — " + price_match.group(0).strip()
    else:
        tag = "Подробности в Telegram"

    dt = datetime.fromtimestamp(date_ts, tz=timezone.utc)
    return {
        "day": str(dt.day),
        "mon": MONTHS_RU[dt.month - 1],
        "name": name,
        "desc": desc,
        "tag": tag,
    }


def load_events():
    if os.path.exists(EVENTS_FILE):
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []
    return []


def save_events(events):
    with open(EVENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(events[:MAX_EVENTS], f, ensure_ascii=False, indent=2)


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

    max_update_id = max(u["update_id"] for u in updates)
    write_offset(max_update_id)

    best_schedule = None  # (date_ts, file_id)
    news_posts = []       # [(date_ts, text), ...]

    for update in updates:
        post = update.get("channel_post")
        if not post:
            continue
        chat = post.get("chat", {})
        chat_username = chat.get("username")
        if chat_username and normalize_channel(chat_username) != channel:
            continue

        caption = post.get("caption", "") or ""
        text = post.get("text", "") or ""
        combined_text = caption or text
        photos = post.get("photo")
        date_ts = post.get("date", 0)

        if photos and SCHEDULE_TAG_RE.search(caption):
            largest = photos[-1]
            if best_schedule is None or date_ts >= best_schedule[0]:
                best_schedule = (date_ts, largest["file_id"])

        if NEWS_TAG_RE.search(combined_text):
            news_posts.append((date_ts, combined_text))

    if best_schedule:
        _, file_id = best_schedule
        download_file(token, file_id, SCHEDULE_IMAGE)
        meta = {"updated": datetime.now(timezone.utc).isoformat()}
        with open(SCHEDULE_META, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        print(f"Расписание обновлено: {SCHEDULE_IMAGE}")
    else:
        print("Среди новых постов нет картинки с тегом #расписание.")

    if news_posts:
        news_posts.sort(key=lambda p: p[0])  # старые сначала — порядок вставки логичный
        events = load_events()
        for date_ts, text in news_posts:
            card = build_event_card(text, date_ts)
            events.insert(0, card)
            print(f"Добавлено событие: {card.get('name')}")
        save_events(events)
        with open(EVENTS_META, "w", encoding="utf-8") as f:
            json.dump({"updated": datetime.now(timezone.utc).isoformat()}, f, ensure_ascii=False, indent=2)
        print(f"Новости обновлены: {EVENTS_FILE}")
    else:
        print("Среди новых постов нет текста с тегом #Новости.")


if __name__ == "__main__":
    main()
