#!/usr/bin/env python3
"""
Дублирование постов канала в группу ВКонтакте. Вызывается из update_content.py с постами,
которые тот уже забрал из Telegram (getUpdates), — отдельного опроса Telegram нет.

Что уходит в ВК: посты с тегом #расписание или #Новости (текст и все фото, альбом — целиком).
- Новый пост → запись на стене группы от имени группы.
- Пост исправили в Telegram → запись в ВК правится (текст, а если сменились фото — и фото).
- Тег дописали позже → запись появляется в ВК (если посту не больше VK_MAX_AGE_DAYS дней).
- Тег убрали → запись из ВК удаляется.
- ВК не ответил → пост ждёт в очереди и уходит при следующем запуске (не дольше суток).
- Без дублей: посты, вышедшие до включения дублирования, в ВК не трогаем (даже если их правят);
  перед публикацией сверяемся с последними записями стены — если такой текст уже выложили
  вручную, второй раз не публикуем, а ручную запись бот потом не правит и не удаляет.

Связь «пост в Telegram → запись в ВК» хранится в scripts/vk_posts.json (только номера, без ключей).

Переменные окружения (секреты в GitHub Actions; без них дублирование просто выключено):
  VK_TOKEN       — ключ доступа сообщества (Управление → Работа с API → Ключи доступа)
  VK_GROUP_ID    — необязательно: номер или короткое имя группы (wcs_spb); без него группа
                   берётся из ключа сообщества
  VK_USER_TOKEN  — необязательно: ключ администратора группы для загрузки фото, если ключ
                   сообщества фото загружать не может (тогда без него в записи будет ссылка на пост
                   в Telegram с превью вместо фото)
"""

import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid

VK_API = "https://api.vk.com/method/"
VK_VERSION = "5.199"
VK_MAX_AGE_DAYS = 7          # тег дописали к старому посту — в ВК не тащим
QUEUE_TTL = 24 * 3600        # сколько повторять отправку, если ВК не отвечает
MAX_PHOTOS = 10              # больше ВК к записи не прикрепляет

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vk_posts.json")


class VkError(RuntimeError):
    def __init__(self, method, err):
        self.code = (err or {}).get("error_code")
        super().__init__(f"VK {method}: {self.code} {(err or {}).get('error_msg')}")


def vk_call(token, method, params):
    data = urllib.parse.urlencode(dict(params, access_token=token, v=VK_VERSION)).encode()
    with urllib.request.urlopen(urllib.request.Request(VK_API + method, data=data), timeout=30) as resp:
        j = json.load(resp)
    if "error" in j:
        raise VkError(method, j["error"])
    return j["response"]


def vk_upload(url, filename, raw):
    """Загрузка файла на сервер ВК (multipart, поле photo)."""
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"{filename}\"\r\n"
            f"Content-Type: image/jpeg\r\n\r\n").encode() + raw + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    s.setdefault("posts", {})
    s.setdefault("queue", [])
    return s


def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2, sort_keys=True)


def enabled():
    return bool(os.environ.get("VK_TOKEN"))


_gid = {}


def group_id():
    """Номер группы: из VK_GROUP_ID (цифры или короткое имя) или из самого ключа сообщества."""
    raw = (os.environ.get("VK_GROUP_ID") or "").strip().lstrip("-")
    if raw.isdigit():
        return raw
    if raw not in _gid:
        r = vk_call(os.environ["VK_TOKEN"], "groups.getById", {"group_id": raw} if raw else {})
        groups = r.get("groups", []) if isinstance(r, dict) else r
        _gid[raw] = str(groups[0]["id"])
    return _gid[raw]


def collect(posts, tag_re):
    """Посты Telegram → записи для ВК. Альбом (media_group_id) — одна запись: текст из подписи, все фото.
    Возвращает {ключ: {"key", "ids", "text", "photos", "date", "edited", "tagged", "msg"}}."""
    items = {}
    for post, edited in posts:
        mid = post.get("message_id")
        if not mid:
            continue
        key = f"g{post['media_group_id']}" if post.get("media_group_id") else str(mid)
        it = items.setdefault(key, {"key": key, "ids": [], "text": "", "photos": [], "date": post.get("date", 0),
                                    "edited": False, "msg": mid})
        it["ids"].append(mid)
        it["msg"] = min(it["msg"], mid)
        it["edited"] = it["edited"] or edited
        text = post.get("caption") or post.get("text") or ""
        if text:
            it["text"] = text
        if any(post.get(k) for k in ("video", "animation", "video_note", "document")):
            it["video"] = True   # видео ВК с ключом сообщества не загрузить — будет ссылка на пост с превью
        if post.get("photo"):
            best = post["photo"][-1]
            it["photos"].append((mid, best["file_id"], best.get("file_unique_id") or best["file_id"]))
    for it in items.values():
        it["photos"] = [p[1:] for p in sorted(it["photos"])][:MAX_PHOTOS]
        it["tagged"] = bool(tag_re.search(it["text"]))
    return items


def fingerprint(text, pkey):
    return hashlib.sha1((text + "|" + pkey).encode()).hexdigest()[:16]


def photo_key(photos, video=False):
    return ",".join(u for _, u in photos) or ("video" if video else "")


def upload_photos(photos, get_file):
    """Фото в альбом стены группы. Ключ сообщества может не уметь (ошибка 27) — тогда ключ администратора."""
    gid = group_id()
    tokens = [t for t in (os.environ.get("VK_TOKEN"), os.environ.get("VK_USER_TOKEN")) if t]
    last = None
    for token in tokens:
        try:
            server = vk_call(token, "photos.getWallUploadServer", {"group_id": gid})
            out = []
            for i, (file_id, _) in enumerate(photos):
                up = vk_upload(server["upload_url"], f"photo{i}.jpg", get_file(file_id))
                saved = vk_call(token, "photos.saveWallPhoto", {"group_id": gid, "photo": up["photo"],
                                                                 "server": up["server"], "hash": up["hash"]})
                out += [f"photo{p['owner_id']}_{p['id']}" for p in saved]
            return out
        except Exception as e:   # пробуем следующий ключ
            last = e
    print(f"ВК: фото не загрузились ({last}) — вместо фото будет ссылка на пост в Telegram", file=sys.stderr)
    return None


def publish(it, rec, url, get_file):
    """Создать или исправить запись. rec — что уже есть в ВК по этому посту (или None)."""
    token, gid = os.environ["VK_TOKEN"], group_id()
    pkey = photo_key(it["photos"], it.get("video"))
    if rec and rec.get("photos") == pkey and rec.get("att") is not None:
        att = rec["att"]              # фото не менялись — не загружаем заново
    elif it["photos"]:
        att = upload_photos(it["photos"], get_file)
        if att is None:
            att = [url] if url else []
        elif it.get("video") and url:
            att = att[:MAX_PHOTOS - 1] + [url]   # в альбоме есть и видео — ссылка на пост, чтобы его увидели
    elif it.get("video"):
        att = [url] if url else []   # видео: ссылка на пост в Telegram, ВК покажет превью
    else:
        att = []
    params = {"owner_id": f"-{gid}", "message": it["text"], "attachments": ",".join(att)}
    if rec:
        vk_call(token, "wall.edit", dict(params, post_id=rec["vk"]))
        vk_id = rec["vk"]
    else:
        vk_id = vk_call(token, "wall.post", dict(params, from_group=1))["post_id"]
    return {"vk": vk_id, "fp": fingerprint(it["text"], pkey), "photos": pkey, "att": att,
            "ids": sorted(set(it["ids"]) | set((rec or {}).get("ids") or []))}


def norm(text):
    """Текст для сравнения с записями на стене: без тегов, ссылок, знаков и регистра."""
    t = re.sub(r"#\w+|https?://\S+|t\.me/\S+", " ", (text or "").lower().replace("ё", "е"))
    return re.sub(r"[^\w]+", "", t)


def find_on_wall(text):
    """Такая запись уже есть на стене (выложили вручную)? Сравниваем с последними 50 записями.
    Стена недоступна ключу — не проверяем (лучше опубликовать, чем потерять пост)."""
    want = norm(text)
    if len(want) < 20:
        return None
    try:
        wall = vk_call(os.environ["VK_TOKEN"], "wall.get", {"owner_id": f"-{group_id()}", "count": 50})
    except Exception as e:
        print(f"ВК: стену проверить не получилось ({e})", file=sys.stderr)
        return None
    for w in wall.get("items", []):
        have = norm(w.get("text") or "".join(c.get("text") or "" for c in w.get("copy_history") or []))
        if len(have) >= 20 and (want[:150] in have or have[:150] in want):
            return w["id"]
    return None


def crosspost(posts, tag_re, get_file, channel_name, now=None):
    """posts — [(post, edited)] из getUpdates. Возвращает True, если что-то поменялось в ВК."""
    if not enabled():
        return False
    now = now or time.time()
    state = load_state()
    # посты, вышедшие до включения дублирования, в ВК не трогаем: их могли выложить туда вручную
    first = "since" not in state
    state.setdefault("since", int(now) - 15 * 60)   # запас: пост мог выйти за несколько минут до первого запуска
    # в очереди — то, что не ушло в прошлый раз (ВК не отвечал); свежие версии из этого запуска главнее
    fresh = {(p.get("message_id"), p.get("media_group_id")) for p, _ in posts}
    queued = [(q["post"], q["edited"]) for q in state["queue"]
              if now - q["at"] < QUEUE_TTL and (q["post"].get("message_id"), q["post"].get("media_group_id")) not in fresh]
    all_posts = queued + list(posts)
    at = {q["post"].get("message_id"): q["at"] for q in state["queue"]}
    items = collect(all_posts, tag_re)
    by_msg = {str(m): k for k, r in state["posts"].items() for m in r.get("ids", [])}
    retry, changed = [], first
    for key, it in sorted(items.items(), key=lambda kv: kv[1]["msg"]):
        known = key if key in state["posts"] else by_msg.get(str(it["msg"]))
        rec = state["posts"].get(known) if known else None
        url = f"https://t.me/{channel_name}/{it['msg']}"
        if not rec and it["edited"] and it["date"] < state["since"]:
            continue   # пост старше включения дублирования (его правка) — в ВК мог уйти вручную, не трогаем
        if rec and rec.get("manual"):
            continue   # запись на стене выложена вручную — бот её не правит и не удаляет
        partial = bool(rec) and key.startswith("g") and not set(rec.get("ids") or []) <= set(it["ids"])
        if partial and not it["text"]:
            continue   # исправили одно фото альбома без подписи — запись в ВК не трогаем
        try:
            if not it["tagged"]:
                if rec and it["edited"]:
                    vk_call(os.environ["VK_TOKEN"], "wall.delete", {"owner_id": f"-{group_id()}", "post_id": rec["vk"]})
                    del state["posts"][known]
                    changed = True
                    print(f"ВК: из поста убрали тег — запись удалена ({url})")
                continue
            if rec:
                if partial:   # в альбоме исправили подпись, остальные фото не пришли — фото оставляем прежние
                    it["photos"] = [(None, u) for u in rec["photos"].split(",") if u and u != "video"]
                    it["video"] = it.get("video") or rec["photos"] == "video"
                if rec.get("fp") == fingerprint(it["text"], photo_key(it["photos"], it.get("video"))):
                    continue
                state["posts"][known] = publish(it, rec, url, get_file)
                changed = True
                print(f"ВК: запись исправлена ({url})")
                continue
            if it["edited"] and now - it["date"] > VK_MAX_AGE_DAYS * 86400:
                print(f"ВК: тег дописали к старому посту — в ВК не публикую ({url})")
                continue
            same = find_on_wall(it["text"])
            if same:
                state["posts"][key] = {"vk": same, "manual": True, "ids": it["ids"]}
                changed = True
                print(f"ВК: такая запись уже есть на стене (выложили вручную) — не дублирую ({url})")
                continue
            state["posts"][key] = publish(it, None, url, get_file)
            changed = True
            print(f"ВК: опубликовано ({url})")
        except Exception as e:
            print(f"ВК: не получилось ({url}): {e} — попробую в следующий раз", file=sys.stderr)
            for p, ed in all_posts:
                pk = f"g{p['media_group_id']}" if p.get("media_group_id") else str(p.get("message_id"))
                if pk == key:
                    retry.append({"post": p, "edited": ed, "at": at.get(p.get("message_id"), now)})
    if retry or state["queue"]:
        changed = True
    state["queue"] = retry
    # старые связи не копим: хватит последних 300 записей
    if len(state["posts"]) > 300:
        for k in sorted(state["posts"], key=lambda k: min(state["posts"][k].get("ids") or [0]))[:-300]:
            del state["posts"][k]
    if changed:
        save_state(state)
    return changed


# ---------------------------------------------------------------------------
# Разовая отправка постов, вышедших до включения (кнопка «Run workflow», поле vk_post):
# номера или ссылки на посты через запятую, или «сегодня» — все посты с тегом за сегодня (по Москве).
# Посты берутся с публичной страницы канала t.me/s/<канал>: Bot API старые посты не отдаёт.

def parse_public(page, channel_name):
    """Посты со страницы t.me/s: номер, дата, текст, все фото (у альбома — несколько)."""
    out = []
    for chunk in page.split('data-post="')[1:]:
        post = chunk.split('"', 1)[0]
        if not post.lower().startswith(channel_name.lower() + "/"):
            continue
        body = chunk.split('class="tgme_widget_message_wrap', 1)[0]
        m = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', body, re.S)
        text = html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", "\n", m.group(1)))) if m else ""
        photos = re.findall(r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)", body)
        video = "tgme_widget_message_video" in body or "tgme_widget_message_roundvideo" in body
        d = re.search(r'<time datetime="([^"]+)"', body)
        out.append({"id": int(post.split("/")[-1]), "text": text.strip(), "photos": photos, "time": d.group(1) if d else "", "video": video})
    return out


def backfill(spec, tag_re, channel_name, fetch, now=None):
    """spec — «сегодня» или номера/ссылки через запятую. Публикует, как обычный новый пост (с проверкой стены)."""
    from datetime import datetime, timedelta, timezone
    now = now or time.time()
    spec = (spec or "").strip().lower()
    want = {int(x) for x in re.findall(r"(\d+)", spec)} if spec not in ("сегодня", "today") else None
    pages, url = [], f"https://t.me/s/{channel_name}"
    for _ in range(3):
        if want:
            url = f"https://t.me/s/{channel_name}?before={max(want) + 1}"
        page = parse_public(fetch(url).decode("utf-8", "replace"), channel_name)
        pages += page
        if want is not None or not page:
            break
        url = f"https://t.me/s/{channel_name}?before={min(p['id'] for p in page)}"
    msk = timezone(timedelta(hours=3))
    today = datetime.fromtimestamp(now, msk).date()
    picked = []
    for p in {p["id"]: p for p in pages}.values():
        if want is not None and p["id"] not in want:
            continue
        if want is None:
            try:
                if datetime.fromisoformat(p["time"]).astimezone(msk).date() != today:
                    continue
            except ValueError:
                continue
        if not tag_re.search(p["text"]):
            print(f"ВК: у поста {p['id']} нет тега #расписание или #Новости — пропускаю")
            continue
        picked.append(p)
    if not picked:
        print(f"ВК: на странице канала не нашёл постов для отправки ({spec})")
        return False
    posts = []
    for p in sorted(picked, key=lambda p: p["id"]):
        if not p["photos"]:
            m = {"message_id": p["id"], "date": int(now), "text": p["text"]}
            if p.get("video"):
                m = {"message_id": p["id"], "date": int(now), "caption": p["text"], "video": {"file_id": "-"}}
            posts.append((m, False))
        for i, ph in enumerate(p["photos"]):   # альбом: подпись у первого фото, номера идут подряд
            m = {"message_id": p["id"] + i, "date": int(now), "photo": [{"file_id": ph, "file_unique_id": ph}]}
            if len(p["photos"]) > 1:
                m["media_group_id"] = f"b{p['id']}"
            if i == 0:
                m["caption"] = p["text"]
            posts.append((m, False))
    return crosspost(posts, tag_re, fetch, channel_name, now=now)
