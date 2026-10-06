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

Связь «пост в Telegram → запись в ВК» хранится в scripts/vk_posts.json (только номера, без ключей).

Переменные окружения (секреты в GitHub Actions; без них дублирование просто выключено):
  VK_TOKEN       — ключ доступа сообщества (Управление → Работа с API → Ключи доступа)
  VK_GROUP_ID    — номер группы (цифры, без минуса), например 12345678
  VK_USER_TOKEN  — необязательно: ключ администратора группы для загрузки фото, если ключ
                   сообщества фото загружать не может (тогда без него в записи будет ссылка на пост
                   в Telegram с превью вместо фото)
"""

import hashlib
import json
import os
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
    return bool(os.environ.get("VK_TOKEN") and os.environ.get("VK_GROUP_ID"))


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
        if post.get("photo"):
            best = post["photo"][-1]
            it["photos"].append((mid, best["file_id"], best.get("file_unique_id") or best["file_id"]))
    for it in items.values():
        it["photos"] = [p[1:] for p in sorted(it["photos"])][:MAX_PHOTOS]
        it["tagged"] = bool(tag_re.search(it["text"]))
    return items


def fingerprint(text, pkey):
    return hashlib.sha1((text + "|" + pkey).encode()).hexdigest()[:16]


def photo_key(photos):
    return ",".join(u for _, u in photos)


def upload_photos(photos, get_file):
    """Фото в альбом стены группы. Ключ сообщества может не уметь (ошибка 27) — тогда ключ администратора."""
    gid = os.environ["VK_GROUP_ID"].lstrip("-")
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
    token, gid = os.environ["VK_TOKEN"], os.environ["VK_GROUP_ID"].lstrip("-")
    pkey = photo_key(it["photos"])
    if rec and rec.get("photos") == pkey and rec.get("att") is not None:
        att = rec["att"]              # фото не менялись — не загружаем заново
    elif it["photos"]:
        att = upload_photos(it["photos"], get_file)
        if att is None:
            att = [url] if url else []
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


def crosspost(posts, tag_re, get_file, channel_name, now=None):
    """posts — [(post, edited)] из getUpdates. Возвращает True, если что-то поменялось в ВК."""
    if not enabled():
        return False
    now = now or time.time()
    state = load_state()
    # в очереди — то, что не ушло в прошлый раз (ВК не отвечал); свежие версии из этого запуска главнее
    fresh = {(p.get("message_id"), p.get("media_group_id")) for p, _ in posts}
    queued = [(q["post"], q["edited"]) for q in state["queue"]
              if now - q["at"] < QUEUE_TTL and (q["post"].get("message_id"), q["post"].get("media_group_id")) not in fresh]
    all_posts = queued + list(posts)
    at = {q["post"].get("message_id"): q["at"] for q in state["queue"]}
    items = collect(all_posts, tag_re)
    by_msg = {str(m): k for k, r in state["posts"].items() for m in r.get("ids", [])}
    retry, changed = [], False
    for key, it in sorted(items.items(), key=lambda kv: kv[1]["msg"]):
        known = key if key in state["posts"] else by_msg.get(str(it["msg"]))
        rec = state["posts"].get(known) if known else None
        url = f"https://t.me/{channel_name}/{it['msg']}"
        partial = bool(rec) and key.startswith("g") and not set(rec.get("ids") or []) <= set(it["ids"])
        if partial and not it["text"]:
            continue   # исправили одно фото альбома без подписи — запись в ВК не трогаем
        try:
            if not it["tagged"]:
                if rec and it["edited"]:
                    vk_call(os.environ["VK_TOKEN"], "wall.delete", {"owner_id": f"-{os.environ['VK_GROUP_ID'].lstrip('-')}", "post_id": rec["vk"]})
                    del state["posts"][known]
                    changed = True
                    print(f"ВК: из поста убрали тег — запись удалена ({url})")
                continue
            if rec:
                if partial:   # в альбоме исправили подпись, остальные фото не пришли — фото оставляем прежние
                    it["photos"] = [(None, u) for u in rec["photos"].split(",") if u]
                if rec.get("fp") == fingerprint(it["text"], photo_key(it["photos"])):
                    continue
                state["posts"][known] = publish(it, rec, url, get_file)
                changed = True
                print(f"ВК: запись исправлена ({url})")
                continue
            if it["edited"] and now - it["date"] > VK_MAX_AGE_DAYS * 86400:
                print(f"ВК: тег дописали к старому посту — в ВК не публикую ({url})")
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
