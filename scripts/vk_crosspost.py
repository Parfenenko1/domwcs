#!/usr/bin/env python3
"""
Дублирование постов канала в группу ВКонтакте. Вызывается из update_content.py с постами,
которые тот уже забрал из Telegram (getUpdates), — отдельного опроса Telegram нет.

Что уходит в ВК: посты с тегом #расписание или #Новости — текст, все фото и видео (альбом — целиком).
Всё делается ключом администратора (от имени группы); ключ сообщества — запасной, только для новых записей.
Видео загружается в видеозаписи группы; больше 20 МБ бот Telegram не отдаёт — тогда ссылка на пост с превью.
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
  VK_USER_TOKEN  — ключ администратора группы. Ключ сообщества умеет только публиковать новые
                   записи; править и удалять их, сверять стену с ручными постами и (скорее всего)
                   загружать фото может только ключ администратора. Без него новые посты уходят,
                   а вместо фото — ссылка на пост в Telegram с превью.
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


class NeedAdmin(RuntimeError):
    """Ключ сообщества этого не умеет (ошибка 27), а ключа администратора нет — повторять бессмысленно."""


def clean_token(raw):
    """Ключ могли вставить вместе с адресом страницы или пробелами — берём только сам ключ."""
    raw = (raw or "").strip().strip('"\'')
    m = re.search(r"access_token=([^&\s]+)", raw)
    return m.group(1) if m else raw


def admin_call(method, params):
    """Правка, удаление и чтение стены: ключ сообщества их не умеет — нужен ключ администратора (VK_USER_TOKEN)."""
    user = clean_token(os.environ.get("VK_USER_TOKEN"))
    if user:
        try:
            return vk_call(user, method, params)
        except VkError as e:
            if e.code == 5:   # ключ не принят — повторять бессмысленно, пока его не заменят
                # сам ключ в журнал не пишем — только тип (начало) и длину, чтобы понять, что вставлено
                hint = f"ключ начинается с «{user[:6]}», длина {len(user)}; нужен «vk1.a.», около 200 символов"
                raise NeedAdmin(f"{method} — ВК не принял ключ администратора ({e}); {hint}; проверь секрет VK_USER_TOKEN")
            raise
    try:
        return vk_call(os.environ["VK_TOKEN"], method, params)
    except VkError as e:
        if e.code == 27:
            raise NeedAdmin(f"{method} — нужен ключ администратора (секрет VK_USER_TOKEN)")
        raise


def vk_upload(url, filename, raw, field="photo", ctype="image/jpeg"):
    """Загрузка файла на сервер ВК (multipart): фото — поле photo, видео — поле video_file."""
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {ctype}\r\n\r\n").encode() + raw + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp)


def tokens():
    """Ключи по порядку: сначала администратора (умеет всё), потом сообщества (запасной — только новые записи)."""
    return [t for t in (clean_token(os.environ.get("VK_USER_TOKEN")), os.environ.get("VK_TOKEN")) if t]


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
        it = items.setdefault(key, {"key": key, "ids": [], "text": "", "photos": [], "videos": [], "date": post.get("date", 0),
                                    "edited": False, "msg": mid})
        it["ids"].append(mid)
        it["msg"] = min(it["msg"], mid)
        it["edited"] = it["edited"] or edited
        text = post.get("caption") or post.get("text") or ""
        if text:
            it["text"] = text
        for k in ("video", "animation", "video_note"):
            v = post.get(k)
            if v:
                it["videos"].append((mid, v.get("file_id") or "", v.get("file_unique_id") or v.get("file_id") or "", v.get("file_size") or 0))
        if post.get("document"):
            it["link"] = True   # файл в ВК не переносим — будет ссылка на пост
        if post.get("photo"):
            best = post["photo"][-1]
            it["photos"].append((mid, best["file_id"], best.get("file_unique_id") or best["file_id"]))
    for it in items.values():
        it["photos"] = [p[1:] for p in sorted(it["photos"])][:MAX_PHOTOS]
        it["videos"] = [v[1:] for v in sorted(it["videos"])][:MAX_PHOTOS]
        it["tagged"] = bool(tag_re.search(it["text"]))
    return items


def fingerprint(text, pkey):
    return hashlib.sha1((text + "|" + pkey).encode()).hexdigest()[:16]


def media_key(it):
    """Отпечаток вложений: фото и видео поста — по нему видно, менялись ли они при правке."""
    if it.get("mkey") is not None:
        return it["mkey"]
    keys = [u for _, u in it["photos"]] + ["v:" + v[1] for v in it.get("videos", [])] + (["link"] if it.get("link") else [])
    return ",".join(keys)


TG_FILE_LIMIT = 20 * 1024 * 1024   # больше этого бот Telegram файл не отдаёт


def upload_video(file_id, size, get_file, title):
    """Видео в видеозаписи группы (ключом администратора). Не вышло — None (будет ссылка на пост)."""
    user = clean_token(os.environ.get("VK_USER_TOKEN"))
    if not user or not file_id:
        return None
    if size and size > TG_FILE_LIMIT and not file_id.startswith("http"):
        print(f"ВК: видео {size // 1048576} МБ — больше 20 МБ бот Telegram не отдаёт, будет ссылка на пост", file=sys.stderr)
        return None
    try:
        raw = get_file(file_id)
        saved = vk_call(user, "video.save", {"group_id": group_id(), "name": title[:120] or "Видео", "wallpost": 0})
        up = vk_upload(saved["upload_url"], "video.mp4", raw, field="video_file", ctype="video/mp4")
        return f"video{up.get('owner_id') or saved['owner_id']}_{up.get('video_id') or saved['video_id']}"
    except Exception as e:
        print(f"ВК: видео не загрузилось ({e}) — будет ссылка на пост в Telegram", file=sys.stderr)
        return None


def upload_photos(photos, get_file):
    """Фото в альбом стены группы: ключом администратора, не вышло — ключом сообщества."""
    gid = group_id()
    last = None
    for token in tokens():
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
    gid = group_id()
    pkey = media_key(it)
    if rec and rec.get("photos") == pkey and rec.get("att") is not None:
        att = rec["att"]              # фото и видео не менялись — не загружаем заново
    else:
        att, need_link = [], bool(it.get("link"))
        if it["photos"]:
            ph = upload_photos(it["photos"], get_file)
            att += ph or []
            need_link = need_link or ph is None
        title = (it["text"].strip().splitlines() or [""])[0]
        for file_id, _, size in it.get("videos", []):
            v = upload_video(file_id, size, get_file, title)
            if v:
                att.append(v)
            else:
                need_link = True
        if need_link and url:          # что не загрузилось — ссылкой на пост в Telegram (ВК покажет превью)
            att = att[:MAX_PHOTOS - 1] + [url]
        att = att[:MAX_PHOTOS]
    params = {"owner_id": f"-{gid}", "message": it["text"], "attachments": ",".join(att)}
    if rec:
        admin_call("wall.edit", dict(params, post_id=rec["vk"]))
        vk_id = rec["vk"]
    else:
        vk_id, last = None, None
        for token in tokens():         # от имени группы: ключом администратора, не вышло — ключом сообщества
            try:
                vk_id = vk_call(token, "wall.post", dict(params, from_group=1))["post_id"]
                break
            except VkError as e:
                last = e
        if vk_id is None:
            raise last
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
        wall = admin_call("wall.get", {"owner_id": f"-{group_id()}", "count": 50})
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
                    admin_call("wall.delete", {"owner_id": f"-{group_id()}", "post_id": rec["vk"]})
                    del state["posts"][known]
                    changed = True
                    print(f"ВК: из поста убрали тег — запись удалена ({url})")
                continue
            if rec:
                if partial:   # в альбоме исправили подпись, остальные фото не пришли — вложения оставляем прежние
                    it["mkey"] = rec.get("photos") or ""
                if rec.get("fp") == fingerprint(it["text"], media_key(it)):
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
        except NeedAdmin as e:
            print(f"ВК: не получилось ({url}): {e}", file=sys.stderr)   # не повторяем: без ключа не выйдет
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
        vsrc = re.search(r'<video[^>]+src="([^"]+)"', body)
        d = re.search(r'<time datetime="([^"]+)"', body)
        out.append({"id": int(post.split("/")[-1]), "text": text.strip(), "photos": photos, "time": d.group(1) if d else "", "video": video,
                    "video_src": html.unescape(vsrc.group(1)) if vsrc else ""})
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
                src = p.get("video_src") or ""   # видео со страницы канала (большие там без ссылки — будет ссылка на пост)
                m = {"message_id": p["id"], "date": int(now), "caption": p["text"], "video": {"file_id": src, "file_unique_id": src or "-"}}
            posts.append((m, False))
        for i, ph in enumerate(p["photos"]):   # альбом: подпись у первого фото, номера идут подряд
            m = {"message_id": p["id"] + i, "date": int(now), "photo": [{"file_id": ph, "file_unique_id": ph}]}
            if len(p["photos"]) > 1:
                m["media_group_id"] = f"b{p['id']}"
            if i == 0:
                m["caption"] = p["text"]
            posts.append((m, False))
    return crosspost(posts, tag_re, fetch, channel_name, now=now)
