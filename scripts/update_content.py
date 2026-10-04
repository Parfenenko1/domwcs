#!/usr/bin/env python3
"""
Каждые 5 минут (и по кнопке вручную) скрипт:
1. Через Telegram Bot API забирает новые посты канала (getUpdates) — ОДИН раз
   для обоих сценариев, чтобы посты не терялись между расписанием и новостями.
2. Расписание: если среди новых постов есть картинка с тегом "#расписание" —
   скачивает самую свежую как schedule.jpg и запоминает дату поста.
3. Новости: если среди новых постов есть текст с тегом "#Новости" — переносит
   его на сайт карточкой (без ИИ, бесплатно):
   - первая строка текста -> заголовок карточки
   - остальные строки -> описание
   - дата -> дата публикации поста
   - ссылка -> прямо на этот пост в канале
   - фото -> если у поста есть фото (или обложка видео), бот скачивает его,
     уменьшает до превью (~450 px, WebP ~30 КБ) и кладёт в папку news/.
   Добавляет карточку в начало events.json, оставляя не больше MAX_EVENTS штук.
   Превью, которые больше не нужны, удаляются.
4. Если у старых карточек нет фото — пробует найти их пост на публичной
   странице канала t.me/s/<канал> и взять фото оттуда (разово, по тексту).

Правки постов тоже учитываются (edited_channel_post): забыли хештег и дописали его
потом — пост подхватится, как будто тег стоял сразу. Поправили текст новости —
обновится её карточка; убрали #Новости — карточка уйдёт с сайта. Правка старого
поста с расписанием не заменит более свежее расписание.

Один offset-файл (scripts/telegram_offset.txt) на все сценарии.

Переменные окружения (задаются как секреты в GitHub Actions):
  TELEGRAM_BOT_TOKEN — токен бота от @BotFather
  TELEGRAM_CHANNEL   — юзернейм канала, например "@domwcs"
"""

import html
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
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
SUBSCRIBERS_FILE = os.path.join(REPO_ROOT, "subscribers.json")
NEWS_DIR = os.path.join(REPO_ROOT, "news")
LOOKUPS_FILE = os.path.join(REPO_ROOT, "scripts", "news_lookups.json")   # служебный, на сайт не влияет
MAX_EVENTS = 6
THUMB_SHORT_SIDE = 450      # превью: короткая сторона в пикселях (хватает для экранов ×2)
THUMB_LONG_MAX = 900
USER_AGENT = "Mozilla/5.0 (compatible; domwcs-site-bot/1.0; +https://domwcs.netlify.app)"

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


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def telegram_file(token, file_id):
    file_info = api_call(token, "getFile", {"file_id": file_id})
    return http_get(f"https://api.telegram.org/file/bot{token}/{file_info['file_path']}", timeout=60)


def download_file(token, file_id, dest_path):
    with open(dest_path, "wb") as f:
        f.write(telegram_file(token, file_id))


# ---------------------------------------------------------------------------
# Фото-превью для новостей

def save_thumb(raw, name):
    """Уменьшает картинку до превью и сохраняет как news/<name>.webp. Возвращает путь для сайта."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        print("Pillow не установлен — превью не делаю", file=sys.stderr)
        return None
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as e:
        print(f"Не удалось открыть фото для превью: {e}", file=sys.stderr)
        return None
    w, h = img.size
    scale = min(1.0, THUMB_SHORT_SIDE / min(w, h), THUMB_LONG_MAX / max(w, h))
    if scale < 1:
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    os.makedirs(NEWS_DIR, exist_ok=True)
    rel = f"news/{name}.webp"
    img.save(os.path.join(REPO_ROOT, rel), "WEBP", quality=74, method=6)
    return rel


def pick_photo_size(sizes):
    """Из размеров фото, которые отдаёт Telegram, берём самый маленький, но не меньше превью."""
    good = [s for s in sizes if min(s.get("width", 0), s.get("height", 0)) >= THUMB_SHORT_SIDE]
    return min(good, key=lambda s: s["width"] * s["height"]) if good else sizes[-1]


def post_media_file_id(post):
    if post.get("photo"):
        return pick_photo_size(post["photo"])["file_id"]
    for key in ("video", "animation", "video_note", "document"):
        media = post.get(key) or {}
        thumb = media.get("thumbnail") or media.get("thumb")
        if thumb:
            return thumb["file_id"]
    return None


def cleanup_thumbs(events):
    if not os.path.isdir(NEWS_DIR):
        return
    keep = {os.path.basename(e["img"]) for e in events if e.get("img")}
    for fn in os.listdir(NEWS_DIR):
        if fn not in keep:
            os.remove(os.path.join(NEWS_DIR, fn))
            print(f"Удалено старое превью: news/{fn}")


# --- публичная страница канала t.me/s/<канал> (для старых карточек без фото) ---

def text_key(text, limit=60):
    key = re.sub(r"[^0-9a-zа-яё]", "", text.lower())
    return key[:limit] if limit else key


def parse_channel_page(page):
    posts = []
    for chunk in page.split('data-post="')[1:]:
        post_id = chunk.split('"', 1)[0]
        body = chunk.split('class="tgme_widget_message_wrap', 1)[0]
        m = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', body, re.S)
        text = ""
        if m:
            text = re.sub(r"<br\s*/?>", "\n", m.group(1))
            text = html.unescape(re.sub(r"<[^>]+>", "", text))
        img = None
        for cls in ("tgme_widget_message_photo_wrap", "tgme_widget_message_video_thumb", "link_preview_image"):
            mm = re.search(cls + r"[^>]*background-image:url\('([^']+)'\)", body)
            if mm:
                img = mm.group(1)
                break
        posts.append({"post": post_id, "text": text, "img": img})
    return posts


def backfill_from_public_page(channel, events):
    # ещё не найденные на странице канала: без ссылки на пост или без фото (если фото у поста есть)
    try:
        with open(LOOKUPS_FILE, encoding="utf-8") as f:
            lookups = json.load(f)
    except (OSError, ValueError):
        lookups = {}
    missing = [e for e in events if (not e.get("url") or (not e.get("img") and not e.get("noimg")))
               and lookups.get(text_key(e.get("name", "")), 0) < 3]
    if not missing:
        return False
    name = channel.lstrip("@")
    posts, url = [], f"https://t.me/s/{name}"
    try:
        for _ in range(3):
            page = http_get(url).decode("utf-8", "replace")
            chunk = parse_channel_page(page)
            if not chunk:
                break
            posts += chunk
            first = min(int(p["post"].split("/")[-1]) for p in chunk if p["post"].split("/")[-1].isdigit())
            url = f"https://t.me/s/{name}?before={first}"
    except Exception as e:
        print(f"Публичная страница канала недоступна ({e}) — старые карточки останутся без фото.")
        return False
    changed = False
    for ev in missing:
        key = text_key(ev.get("name", "").rstrip("…"))
        if len(key) < 12:
            continue
        for p in posts:
            if key[:40] in text_key(p["text"], limit=None):
                msg_id = p["post"].split("/")[-1]
                if not ev.get("url"):
                    ev["url"] = f"https://t.me/{p['post']}"
                    changed = True
                if not ev.get("img") and not p["img"]:
                    ev["noimg"] = True           # у поста нет фото — больше не искать
                    changed = True
                if not ev.get("img") and p["img"]:
                    try:
                        rel = save_thumb(http_get(p["img"]), msg_id)
                    except Exception as e:
                        print(f"Не скачалось фото поста {p['post']}: {e}")
                        rel = None
                    if rel:
                        ev["img"] = rel
                        changed = True
                        print(f"Нашёл фото для «{ev['name'][:40]}…»: {rel}")
                break
        else:
            k = text_key(ev.get("name", ""))           # не нашёлся — после 3 попыток перестанем искать
            lookups[k] = lookups.get(k, 0) + 1
    live = {text_key(e.get("name", "")) for e in events}
    lookups = {k: v for k, v in lookups.items() if k in live}
    with open(LOOKUPS_FILE, "w", encoding="utf-8") as f:
        json.dump(lookups, f, ensure_ascii=False, indent=1)
    return changed


# ---------------------------------------------------------------------------

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


def update_subscriber_count(token, channel):
    """Запрашивает у Telegram текущее число участников канала и сохраняет его.
    Файл переписывается, только если число изменилось: каждый лишний коммит —
    это лишняя публикация сайта на Netlify (15 кредитов)."""
    try:
        count = api_call(token, "getChatMemberCount", {"chat_id": channel})
    except Exception as e:
        print(f"Не удалось получить число подписчиков: {e}", file=sys.stderr)
        return
    try:
        with open(SUBSCRIBERS_FILE, encoding="utf-8") as f:
            if json.load(f).get("count") == count:
                print(f"Подписчиков в канале: {count} (не изменилось)")
                return
    except (OSError, ValueError):
        pass
    with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"count": count, "updated": datetime.now(timezone.utc).isoformat()},
            f, ensure_ascii=False, indent=2,
        )
    print(f"Подписчиков в канале: {count}")


def load_events():
    if os.path.exists(EVENTS_FILE):
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []
    return []


def save_events(events):
    events = events[:MAX_EVENTS]
    with open(EVENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)
    with open(EVENTS_META, "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.now(timezone.utc).isoformat()}, f, ensure_ascii=False, indent=2)
    cleanup_thumbs(events)


def post_url(post, channel_name):
    username = (post.get("chat") or {}).get("username") or channel_name
    return f"https://t.me/{username}/{post['message_id']}" if post.get("message_id") else None


def load_schedule_meta():
    try:
        with open(SCHEDULE_META, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    channel = os.environ.get("TELEGRAM_CHANNEL")
    if not token or not channel:
        print("Не заданы TELEGRAM_BOT_TOKEN и/или TELEGRAM_CHANNEL", file=sys.stderr)
        sys.exit(1)
    channel = normalize_channel(channel)
    channel_name = channel.lstrip("@")

    update_subscriber_count(token, channel)

    offset = read_offset()
    # правки постов тоже: хештег могли дописать уже после публикации
    params = {"timeout": 0, "allowed_updates": json.dumps(["channel_post", "edited_channel_post"])}
    if offset is not None:
        params["offset"] = offset + 1

    updates = api_call(token, "getUpdates", params)
    if updates:
        write_offset(max(u["update_id"] for u in updates))
    else:
        print("Новых постов нет.")

    # один пост мог прийти несколько раз (опубликован, потом исправлен) — берём последнюю версию
    latest = {}           # message_id -> (post, это правка?)
    for update in updates or []:
        edited = "edited_channel_post" in update
        post = update.get("edited_channel_post") if edited else update.get("channel_post")
        if not post:
            continue
        chat = post.get("chat", {})
        chat_username = chat.get("username")
        if chat_username and normalize_channel(chat_username) != channel:
            continue
        key = post.get("message_id") or id(post)
        latest[key] = (post, edited or latest.get(key, (None, False))[1])

    best_schedule = None  # (date_ts, file_id, file_unique_id, message_id)
    news_posts = []       # [(date_ts, text, post), ...]
    untagged_edits = []   # исправленные посты без #Новости — если их карточка на сайте, она уходит

    for post, edited in latest.values():
        caption = post.get("caption", "") or ""
        text = post.get("text", "") or ""
        combined_text = caption or text
        photos = post.get("photo")
        date_ts = post.get("date", 0)

        if photos and SCHEDULE_TAG_RE.search(caption):
            largest = photos[-1]
            if best_schedule is None or date_ts >= best_schedule[0]:
                best_schedule = (date_ts, largest["file_id"], largest.get("file_unique_id"), post.get("message_id"))

        if NEWS_TAG_RE.search(combined_text):
            news_posts.append((date_ts, combined_text, post, edited))
        elif edited:
            untagged_edits.append(post)

    if best_schedule:
        date_ts, file_id, file_uid, msg_id = best_schedule
        meta_old = load_schedule_meta()
        posted = datetime.fromtimestamp(date_ts, tz=timezone.utc)
        try:
            cur = datetime.fromisoformat(meta_old["posted"]) if meta_old.get("posted") else None
        except ValueError:
            cur = None
        if cur and posted < cur:
            print("Исправлен старый пост с расписанием — на сайте уже более свежее, не трогаю.")
        elif file_uid and meta_old.get("file") == file_uid:
            print("Расписание то же самое (пост исправили, картинка прежняя) — обновлять нечего.")
        else:
            download_file(token, file_id, SCHEDULE_IMAGE)
            meta = {
                "updated": datetime.now(timezone.utc).isoformat(),
                "posted": posted.isoformat(),
            }
            if msg_id:
                meta["message_id"] = msg_id
            if file_uid:
                meta["file"] = file_uid
            with open(SCHEDULE_META, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            print(f"Расписание обновлено: {SCHEDULE_IMAGE}")
    else:
        print("Среди новых постов нет картинки с тегом #расписание.")

    events = load_events()
    changed = False
    for post in untagged_edits:
        url = post_url(post, channel_name)
        if url and any(e.get("url") == url for e in events):
            events = [e for e in events if e.get("url") != url]
            changed = True
            print(f"Из поста убрали #Новости — карточка снята с сайта: {url}")
    if news_posts:
        news_posts.sort(key=lambda p: p[0])  # старые сначала — порядок вставки логичный
        shown = [int(m.group(1)) for e in events[:MAX_EVENTS] for m in [re.search(r"/(\d+)$", e.get("url") or "")] if m]
        for date_ts, text, post, edited in news_posts:
            card = build_event_card(text, date_ts)
            url = post_url(post, channel_name)
            if url:
                card["url"] = url
            old = next((e for e in events if url and e.get("url") == url), None)
            if old:   # пост уже на сайте — его исправили: обновляем текст, фото оставляем
                fresh = {k: card[k] for k in ("day", "mon", "name", "desc", "tag")}
                if any(old.get(k) != v for k, v in fresh.items()):
                    old.update(fresh)
                    changed = True
                    print(f"Обновлена карточка: {card.get('name')}")
                continue
            if edited and len(events) >= MAX_EVENTS and shown and (post.get("message_id") or 0) < min(shown):
                print(f"Исправили старый пост, которого уже нет на сайте, — назад не возвращаю: {url}")
                continue
            file_id = post_media_file_id(post)
            if not file_id:
                card["noimg"] = True
            if file_id:
                try:
                    rel = save_thumb(telegram_file(token, file_id), str(post.get("message_id") or date_ts))
                    if rel:
                        card["img"] = rel
                except Exception as e:
                    print(f"Фото к новости не скачалось: {e}", file=sys.stderr)
            events.insert(0, card)
            changed = True
            print(f"Добавлено событие: {card.get('name')}" + (" (с фото)" if card.get("img") else ""))
    else:
        print("Среди новых постов нет текста с тегом #Новости.")

    if backfill_from_public_page(channel, events[:MAX_EVENTS]):
        changed = True

    if changed:
        save_events(events)
        print(f"Новости обновлены: {EVENTS_FILE}")


if __name__ == "__main__":
    main()
