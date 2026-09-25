#!/usr/bin/env python3
"""
Читает картинку расписания (schedule.jpg) и собирает короткую сводку недели
для блока «Неделя в Доме» на сайте — week.json.

Как работает:
  1. Распознаёт текст на картинке (Tesseract OCR, русский + английский).
  2. Находит заголовки дней (ПОНЕДЕЛЬНИК … ВОСКРЕСЕНЬЕ) и по ним — колонки.
  3. Внутри колонки собирает слова в карточки и превращает каждую карточку
     в короткую подпись: «Новая группа», «Старшая», «1 уровень», «Свитч»,
     «Вечеринка в Доме», «Мероприятие», «Сампо» и т.д.

Запускается из GitHub Actions после update_content.py. Если картинка не
менялась — ничего не делает. Если распознать неделю уверенно не вышло —
week.json не трогает, и сайт показывает обычную неделю из базового шаблона.
"""
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEDULE_IMAGE = os.path.join(REPO_ROOT, "schedule.jpg")
SCHEDULE_META = os.path.join(REPO_ROOT, "schedule-meta.json")
WEEK_FILE = os.path.join(REPO_ROOT, "week.json")

DAYS = [
    ("Пн", ("ПОНЕД", "ПОНЕ", "НЕДЕЛЬН")),
    ("Вт", ("ВТОРН", "ВТОР")),
    ("Ср", ("СРЕД",)),
    ("Чт", ("ЧЕТВ", "ЧЕТВЕР")),
    ("Пт", ("ПЯТН", "ЯТНИЦ")),
    ("Сб", ("СУББ", "УББОТ")),
    ("Вс", ("ВОСКР", "СКРЕС")),
]
MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5, "июн": 6,
          "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}

# Латинские буквы, похожие на кириллицу (OCR иногда путает «УРОВЕНЬ» с «YPOBEHb»)
HOMO = str.maketrans("ABCEHKMOPTXYaceopxyb3", "АВСЕНКМОРТХУасеорхуЬЗ")

TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[.:,]([0-5]\d)\b")


def ocr_words(img, langs):
    """Прогоняет Tesseract и возвращает слова с координатами (в пикселях исходной картинки)."""
    from PIL import Image
    scale = 2
    big = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.png")
        big.save(src)
        base = os.path.join(tmp, "out")
        subprocess.run(["tesseract", src, base, "-l", langs, "--psm", "11", "tsv"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(base + ".tsv", encoding="utf-8") as f:
            rows = list(csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE))
    words = []
    for r in rows:
        text = (r.get("text") or "").strip()
        try:
            conf = float(r["conf"])
        except (TypeError, ValueError):
            continue
        if not text or conf < 55:
            continue
        x, y, w, h = (int(r[k]) / scale for k in ("left", "top", "width", "height"))
        words.append({"text": text, "conf": conf, "x": x, "y": y, "w": w, "h": h,
                      "cx": x + w / 2, "cy": y + h / 2})
    return words


def merge(a, b):
    """Объединяет два прохода OCR, убирая дубли (перекрывающиеся слова)."""
    out = list(a)
    for w in b:
        dup = False
        for i, v in enumerate(out):
            ox = max(0, min(w["x"] + w["w"], v["x"] + v["w"]) - max(w["x"], v["x"]))
            oy = max(0, min(w["y"] + w["h"], v["y"] + v["h"]) - max(w["y"], v["y"]))
            if ox * oy > 0.4 * min(w["w"] * w["h"], v["w"] * v["h"]):
                dup = True
                if w["conf"] > v["conf"] + 3:
                    out[i] = w
                break
        if not dup:
            out.append(w)
    return out


def norm(t):
    return t.upper().translate(HOMO).replace("Ё", "Е")


def is_meaningful(w):
    t = w["text"]
    if TIME_RE.search(t):
        return True
    letters = sum(ch.isalpha() for ch in t)
    if letters >= 3:
        return True
    if re.fullmatch(r"[1-9][-–]?[ЙйЯя]?", t):   # номер уровня: «1 уровень», «2-й»
        return True
    return norm(t) in ("НА", "В", "ВО", "С", "И")


def find_headers(words):
    found = {}
    for w in words:
        t = norm(w["text"])
        for i, (_, keys) in enumerate(DAYS):
            if any(k in t for k in keys) and len(t) >= 4:
                if i not in found or w["conf"] > found[i]["conf"]:
                    found[i] = w
    return found


def column_centers(headers, img_w):
    """Центры 7 колонок: по найденным заголовкам, недостающие — по прямой."""
    pts = sorted((i, h["cx"]) for i, h in headers.items())
    if len(pts) >= 2:
        n = len(pts)
        mx = sum(p[0] for p in pts) / n
        my = sum(p[1] for p in pts) / n
        den = sum((p[0] - mx) ** 2 for p in pts)
        b = sum((p[0] - mx) * (p[1] - my) for p in pts) / den
        a = my - b * mx
    else:
        b = img_w / 7
        a = b / 2
    return [a + b * i for i in range(7)], abs(b)


def label_for(card_text):
    """Короткая подпись карточки. Возвращает (подпись, выделять_ли) или None."""
    raw = TIME_RE.sub(" ", card_text).upper()
    t = norm(TIME_RE.sub(" ", card_text))
    if "ВЕЧЕРИНК" in t:
        if "ДОМ" in t:
            return "Вечеринка в Доме", True
        m = re.search(r"ВЕЧЕРИНК\w*\s+(?:В|ВО)\s+([А-ЯЁ-]{3,})", t)
        if m:
            place = m.group(1).capitalize()
            return f"Вечеринка в {place}", True
        return "Вечеринка", True
    if re.search(r"НОВ\w*\s+ГРУПП|НАЧИНАЮЩ|С\s+НУЛЯ", t):
        return "Новая группа", True
    if "СТАРШ" in t:
        return "Старшая", False
    m = re.search(r"(?<![\d,.])([1-9])\s*[-–]?\s*(?:Й\s*)?УРОВ", t)
    if m:
        return f"{m.group(1)} уровень", False
    if "УРОВ" in t:
        return "Уровень", False
    if "SWITCH" in raw or "СВИТЧ" in t:
        return "Свитч", False
    if "ОБЩ" in t:
        return "Общая группа", False
    if "ДРУГИЕ" in t or "ДРУГ" in t and "РОЛ" in t:
        return "«Другие роли»", False
    if re.search(r"R?ALLY|INTERNATIONAL|РАЛЛИ|РЭЛЛИ", raw):
        return "Rally", False
    if "МАСТЕР" in t or re.search(r"\bМК\b", t):
        return "Мастер-класс", False
    if "ЛАБОРАТОР" in t:
        return "Лаборатория", False
    if "ИНТЕНСИВ" in t:
        return "Интенсив", False
    if "ОПЕН" in t or "OPEN" in raw:
        return "Опен", False
    if re.search(r"ЗАКРЫТ|МЕРОПРИЯТ|АРЕНД", t):
        return "Мероприятие", False
    if "САМПО" in t:
        return "Сампо", False
    return None


def fallback_label(card):
    """Незнакомая карточка со временем: берём первое уверенно распознанное слово."""
    if not any(TIME_RE.search(w["text"]) for w in card):
        return None
    for w in sorted(card, key=lambda w: (round(w["y"] / 12), w["x"])):
        word = re.sub(r"[^А-Яа-яЁёA-Za-z-]", "", w["text"])
        if len(word) >= 4 and w["conf"] >= 85 and norm(word) not in ("ЧАСА", "ЧАСОВ", "ЗАПИСЬ"):
            return word.capitalize()[:18], False
    return None


def build_days(words, centers, col_w, header_bottom, img_h):
    cols = [[] for _ in range(7)]
    for w in words:
        if w["cy"] <= header_bottom or not is_meaningful(w):
            continue
        dists = [abs(w["cx"] - c) for c in centers]
        i = min(range(7), key=lambda k: dists[k])
        if dists[i] <= col_w * 0.6:
            cols[i].append(w)

    days = []
    for i, col in enumerate(cols):
        col.sort(key=lambda w: (w["y"], w["x"]))
        cards, cur, last_y = [], [], None
        gap = img_h * 0.05  # расстояние по вертикали между карточками
        for w in col:
            if last_y is not None and w["y"] - last_y > gap:
                cards.append(cur)
                cur = []
            cur.append(w)
            last_y = w["y"] + w["h"]
        if cur:
            cards.append(cur)

        items, has_sampo = [], False
        for card in cards:
            text = " ".join(w["text"] for w in sorted(card, key=lambda w: (round(w["y"] / 12), w["x"])))
            lab = label_for(text) or fallback_label(card)
            if not lab:
                continue
            name, hot = lab
            if name == "Сампо":
                has_sampo = True
                continue
            tm = TIME_RE.search(text)
            item = {"label": name}
            if tm:
                item["time"] = f"{int(tm.group(1))}:{tm.group(2)}"
            if hot:
                item["hot"] = True
            if not any(x["label"] == name and x.get("time") == item.get("time") for x in items):
                items.append(item)
        items = items[:3]
        if has_sampo and len(items) < 2:
            items.append({"label": "Сампо"})
        days.append({"day": DAYS[i][0], "items": items})
    return days


def week_start_from(words, headers, meta_date, scale=1.0):
    """Понедельник недели: по датам под заголовками, иначе — по дате публикации."""
    year = meta_date.year
    for i, h in sorted(headers.items()):
        near = [w for w in words if abs(w["cx"] - h["cx"]) < 90 * scale and 0 < w["y"] - h["y"] < 70 * scale]
        text = " ".join(w["text"] for w in sorted(near, key=lambda w: w["x"])).lower()
        m = re.search(r"(\d{1,2})\s*([а-я]+)", text)
        if not m:
            continue
        month = next((v for k, v in MONTHS.items() if m.group(2).startswith(k)), None)
        if not month:
            continue
        try:
            d = dt.date(year, month, int(m.group(1)))
        except ValueError:
            continue
        monday = d - dt.timedelta(days=i)
        if monday.weekday() == 0 and abs((monday - meta_date).days) < 14:
            return monday
    # расписание обычно публикуют в выходные или в понедельник
    probe = meta_date + dt.timedelta(days=2)
    return probe - dt.timedelta(days=probe.weekday())


def up_to_date(sha):
    if not os.path.exists(WEEK_FILE):
        return False
    try:
        with open(WEEK_FILE, encoding="utf-8") as f:
            return json.load(f).get("source_sha1") == sha
    except (ValueError, OSError):
        return False


def main():
    if not os.path.exists(SCHEDULE_IMAGE):
        print("no" if "--need" in sys.argv else "Нет schedule.jpg — пропускаю.")
        return
    with open(SCHEDULE_IMAGE, "rb") as f:
        raw = f.read()
    sha = hashlib.sha1(raw).hexdigest()
    if "--need" in sys.argv:          # для GitHub Actions: нужно ли вообще распознавать
        print("no" if up_to_date(sha) else "yes")
        return
    if up_to_date(sha) and "--force" not in sys.argv:
        print("Картинка расписания не менялась — week.json актуален.")
        return

    from PIL import Image, ImageOps
    img = ImageOps.grayscale(Image.open(io.BytesIO(raw)))
    scale = img.height / 853
    words = merge(ocr_words(img, "rus"), ocr_words(img, "rus+eng"))
    headers = find_headers(words)
    if len(headers) < 4:
        print(f"Нашёл только {len(headers)} заголовка дней — не уверен, week.json не трогаю.")
        return
    centers, col_w = column_centers(headers, img.width)
    header_bottom = max(h["y"] + h["h"] for h in headers.values()) + 30 * scale
    days = build_days(words, centers, col_w, header_bottom, img.height)
    filled = sum(1 for d in days if d["items"])
    if filled < 5:
        print(f"Распознал только {filled} дней из 7 — week.json не трогаю.")
        return

    meta_date = dt.date.today()
    if os.path.exists(SCHEDULE_META):
        try:
            with open(SCHEDULE_META, encoding="utf-8") as f:
                meta_date = dt.datetime.fromisoformat(json.load(f)["updated"]).date()
        except (ValueError, KeyError, OSError):
            pass
    monday = week_start_from(words, headers, meta_date, scale)

    data = {
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(),
        "week_start": monday.isoformat(),
        "source_sha1": sha,
        "days": days,
    }
    with open(WEEK_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("week.json обновлён:")
    for d in days:
        print(" ", d["day"], "·", ", ".join(i["label"] + (" " + i["time"] if "time" in i else "") for i in d["items"]))


if __name__ == "__main__":
    main()
