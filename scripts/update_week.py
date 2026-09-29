#!/usr/bin/env python3
"""
Читает картинку расписания (schedule.jpg) и превращает её в данные для сайта.

  week.json   — что происходит на этой неделе: блоки «Неделя в Доме»,
                «Сегодня в Доме» и «Ближайшая новая группа».
  groups.json — постоянные группы: кто ведёт, как называется, идёт ли набор.
                Отсюда карточки преподавателей и окно записи. Бот помнит
                последние 3 недели, поэтому разовая замена преподавателя или
                неделя без занятий не ломают карточки.

Картинку рисует генератор расписания (шаблон 1280×900, 7 колонок). Скрипт
знает сетку шаблона и читает каждую колонку Tesseract'ом (русский + английский).
Белый текст на цветных плашках (вечеринка, «Лаборатория», даты на ленточках)
читается отдельно.

Названия групп берутся прямо с карточек:
  «(1,5 часа, старшая группа)»  →  «Старшая группа»
  «(1,5 часа)»                  →  «Группа»
  «(1,5 часа, группа с нуля)»   →  «Группа с нуля» + отметка «идёт набор»
Слова «с нуля», «новая», «набор», «для начинающих» в подписи = группа открыта
для новичков: сайт покажет её в плашке «Ближайшая новая группа» и в записи.

Если распознать уверенно не вышло — файлы не трогаются, сайт показывает
прошлые данные или обычную неделю.

Запуск:
  python scripts/update_week.py            — распознать, если картинка поменялась
  python scripts/update_week.py --need     — напечатать yes/no: нужно ли распознавать
  python scripts/update_week.py --force    — распознать в любом случае
  python scripts/update_week.py --image X.jpg --out-dir DIR --posted 2026-09-28  — для проверки
"""
import argparse
import csv
import datetime as dt
import hashlib
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
GROUPS_FILE = os.path.join(REPO_ROOT, "groups.json")
FORMAT_VERSION = 2

# ---------------------------------------------------------------------------
# Сетка шаблона картинки (template.html генератора расписания, 1280×900).
# Если в шаблоне поменяют отступы/ширину колонок — поправить здесь.
REF_W, REF_H = 1280, 900
GRID_X0, COL_W, COL_STEP = 30, 164, 176          # .grid left:30px; 7 колонок; gap:12px
RIBBON_Y = (190, 236)                             # ленточка с датой под названием дня
CARDS_Y = (234, 806)                              # карточки занятий
MEMORY_DAYS = 21                                  # сколько дней groups.json помнит группы

DAY_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]

# Постоянные преподаватели: как их зовут на картинке. Пара находится, только если
# на карточке есть оба имени. Порядок не важен.
TEACHERS = [
    ("Саша и Лена", [["саша", "александр", "парфененко"], ["лена", "логашина"]]),
    ("Аня и Оля", [["аня", "анна", "данилова"], ["оля", "ольга", "михеева"]]),
    ("Олег и Женя", [["олег", "фабрицкий"], ["женя", "евгения", "земскова"]]),
    ("Даша", [["даша", "дарья", "комкина"]]),
    ("Елена Котельникова", [["котельникова"]]),
]

# Слова, которые встречаются в подписях групп: по ним исправляются ошибки распознавания.
VOCAB = """группа группы старшая младшая средняя новая общая открытая продолжающие
начинающие начинающих набор нуля с для уровень уровня часа час часов минут сампо
закрыт закрыто на мероприятие вечеринка вечер в доме дома лаборатория мастер-класс
мастер класс интенсив другие роли роль практика открытый урок свитч и
""".split()
EN_VOCAB = set("wcs switch international rally open party lab pro am proam jack jill "
               "strictly workshop west coast swing".split())
NAMES = sorted({w for _, groups in TEACHERS for alts in groups for w in alts})
TAGS = ["Лаборатория", "Мастер-класс", "Интенсив", "Практика", "Воркшоп", "Открытый урок",
        "Опен", "Конкурс", "Лекция", "Разбор", "Новинка", "Спецкурс"]
NEW_RE = re.compile(r"с\s*нуля|нов|набор|начинающ", re.I)
CLASS_WORDS = re.compile(r"switch|свитч|роли|лаборатор|мастер|интенсив|практик|урок|rally|группа|"
                         r"вечеринк|open|опен|стайлинг|техник|музыкальн", re.I)

# Латиница, похожая на кириллицу (Tesseract путает «группа» и «rpynna»)
HOMO = str.maketrans("ABCEHKMOPTXYaceopxyrnub", "АВСЕНКМОРТХУасеорхугпиь")
CYR = re.compile(r"[А-Яа-яЁё]")
LAT = re.compile(r"[A-Za-z]")


# ---------------------------------------------------------------------------
# Вспомогательное

def lev(a, b):
    """Расстояние Левенштейна (сколько букв надо поменять, чтобы из a получить b)."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def keep_case(src, word):
    if src[:1].isupper():
        return word[:1].upper() + word[1:]
    return word


def fix_word(tok, vocab=VOCAB + NAMES):
    """Исправляет одно слово по словарю: «rpynna» → «группа», «Олеги» остаётся."""
    low = tok.lower().replace("ё", "е")
    if low in vocab or low in EN_VOCAB or not low.isalpha() and "-" not in low:
        return tok
    cand = low.translate(HOMO) if LAT.search(low) else low
    if LAT.search(low) and low in EN_VOCAB:
        return tok
    if cand in vocab:
        return keep_case(tok, cand)
    best, dist = None, 99
    for w in vocab:
        if abs(len(w) - len(cand)) > 2:
            continue
        d = lev(cand, w)
        if d < dist:
            best, dist = w, d
    if best and ((len(best) >= 6 and dist <= 2) or (len(best) >= 4 and dist <= 1)):
        return keep_case(tok, best)
    if CYR.search(cand) and LAT.search(low):     # смесь алфавитов — точно кириллица
        return keep_case(tok, cand)
    return tok


def fix_words(text, vocab=VOCAB + NAMES):
    return re.sub(r"[A-Za-zА-Яа-яЁё-]+", lambda m: fix_word(m.group(0), vocab), text)


def tidy(text):
    text = text.replace("”", "»").replace("“", "«").replace("„", "«").replace('"', "»")
    text = re.sub(r"\s+", " ", text).strip(" .,;:-–—·•|")
    text = re.sub(r"»+", "»", re.sub(r"«+", "«", text))
    text = re.sub(r"«\s+", "«", text)
    text = re.sub(r"\s+»", "»", text)
    if text.count("»") > text.count("«") and not text.startswith("«"):
        text = "«" + text
    if text.count("«") > text.count("»"):
        text += "»"
    return text


def cap(text):
    return text[:1].upper() + text[1:] if text else text


def compact(text):
    low = text.lower().replace("ё", "е")
    if CYR.search(low):                      # «Oпа» с латинской O → «опа»
        low = low.translate(HOMO)
    return re.sub(r"[^а-яa-z]", "", low)


def contains_name(comp, name):
    if name in comp:
        return True
    if len(name) < 5:
        return False
    n = len(name)
    for size in (n - 1, n, n + 1):
        for i in range(0, max(0, len(comp) - size) + 1):
            if lev(comp[i:i + size], name) <= 1:
                return True
    return False


def canon_who(raw):
    """«СашаиЛена» → «Саша и Лена»; «Дарья Комкина» → «Даша»; чужие имена — как есть."""
    comp = compact(raw)
    if not comp:
        return ""
    for short, groups in TEACHERS:
        if all(any(contains_name(comp, alt) for alt in alts) for alts in groups):
            return short
    for short, groups in TEACHERS:                     # «Аня и Опа»: одно имя точно, второе почти
        if len(groups) == 2:
            hits = [any(contains_name(comp, a) for a in alts) for alts in groups]
            if sum(hits) == 1:
                other = groups[hits.index(False)]
                parts = re.split(r"\s+и\s+|\s*и(?=[А-ЯЁ])", raw)
                close = lambda p, a: (lev(p, a) <= 1 or (lev(p, a) == 2 and p[:1] == a[:1]
                                                        and abs(len(p) - len(a)) <= 1))
                if len(parts) == 2 and any(close(compact(p).translate(HOMO), a)
                                           for p in parts for a in other if len(a) <= 4):
                    return short
    words = [fix_word(w) for w in re.findall(r"[A-Za-zА-Яа-яЁё]+", raw)]
    words = [w for w in words if len(w) > 1 or w.lower() == "и"]
    return " ".join(w if w.lower() == "и" else cap(w) for w in words)


def looks_like_people(text):
    comp = compact(text)
    if any(all(any(contains_name(comp, a) for a in alts) for alts in g) for _, g in TEACHERS):
        return True
    if CLASS_WORDS.search(text):
        return False
    words = re.findall(r"[А-ЯЁ][а-яё]+", text)
    return bool(re.search(r"[А-ЯЁ][а-яё]+\s*и\s*[А-ЯЁ][а-яё]+", text)) or len(words) == 2 and len(text.split()) == 2


# ---------------------------------------------------------------------------
# Время: шрифт Nauryz рисует «1» с флажком, Tesseract читает её как «7»

def parse_time(text):
    digits = re.sub(r"\D", "", text)
    if len(digits) < 3:
        return None
    digits = digits[-4:]
    lost_one = len(digits) == 3
    if lost_one:
        digits = "0" + digits
    h, m = int(digits[:2]), int(digits[2:])
    if h > 23:                      # Nauryz: «1» читается как «7» или «4»
        h = 10 + int(digits[1])
    if lost_one and h < 10:         # «1» не прочиталась совсем: «9.00» → 19:00 (утром занятий нет)
        h += 10
    if not (8 <= h <= 23) or m > 59 or m % 5:
        return None
    return f"{h:02d}:{m:02d}"


def is_time_line(text):
    t = text.replace("О", "0").replace("о", "0").replace("O", "0")
    if not re.search(r"\d{1,2}\s*[.,:]?\s*\d{2}", t):
        return False
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", text)
    return len(letters) <= 2 and parse_time(t) is not None


# ---------------------------------------------------------------------------
# Распознавание

def tesseract_lines(img, psm=6, langs="rus+eng", scale=3):
    """Tesseract по картинке → строки текста с координатами (в пикселях исходника)."""
    from PIL import Image
    big = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.png")
        big.save(src)
        base = os.path.join(tmp, "out")
        subprocess.run(["tesseract", src, base, "-l", langs, "--psm", str(psm), "tsv"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(base + ".tsv", encoding="utf-8") as f:
            rows = list(csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE))
    lines = {}
    for r in rows:
        text = (r.get("text") or "").strip()
        try:
            conf = float(r["conf"])
        except (TypeError, ValueError):
            continue
        if not text or conf < 0:
            continue
        key = (r["block_num"], r["par_num"], r["line_num"])
        x, y, w, h = (int(r[k]) / scale for k in ("left", "top", "width", "height"))
        ln = lines.setdefault(key, {"words": [], "x0": x, "y0": y, "x1": x + w, "y1": y + h})
        ln["words"].append((text, conf, x, x + w))
        ln["x0"], ln["y0"] = min(ln["x0"], x), min(ln["y0"], y)
        ln["x1"], ln["y1"] = max(ln["x1"], x + w), max(ln["y1"], y + h)
    out = []
    for ln in lines.values():
        ln["text"] = " ".join(w[0] for w in ln["words"])
        ln["conf"] = sum(w[1] for w in ln["words"]) / len(ln["words"])
        out.append(ln)
    return sorted(out, key=lambda l: l["y0"])


def color_masks(arr):
    """Насыщенные заливки (плашки, ленточки, вечеринка) — любого цвета, не только оранжевые
    и зелёные: сменится сезонное оформление — распознавание не сломается."""
    import numpy as np
    a = arr[..., :3].astype(np.int16)
    hi, lo = a.max(axis=2), a.min(axis=2)
    solid = (hi > 60) & ((hi - lo) * 100 > hi * 40)
    light = lo > 190
    return solid, light


def runs(bool_row):
    best, cur, start, best_span = 0, 0, 0, (0, 0)
    for i, v in enumerate(bool_row):
        if v:
            if cur == 0:
                start = i
            cur += 1
            if cur > best:
                best, best_span = cur, (start, i + 1)
        else:
            cur = 0
    return best, best_span


def solid_regions(col):
    """Ищет в колонке залитые цветом плашки: карточку вечеринки и бейджи вроде «ЛАБОРАТОРИЯ»."""
    import numpy as np
    arr = np.asarray(col)
    solid, _ = color_masks(arr)
    h, w = solid.shape
    frac = solid.mean(axis=1)
    regions = []
    # Большая залитая карточка (вечеринка)
    y = 0
    while y < h:
        if frac[y] > 0.55:
            y2 = y
            while y2 < h and frac[y2] > 0.35:
                y2 += 1
            if y2 - y >= 60:
                cols = np.where(solid[y:y2].mean(axis=0) > 0.45)[0]
                if len(cols):
                    regions.append(("party", int(cols[0]), y, int(cols[-1]) + 1, y2))
            y = y2 + 1
        else:
            y += 1
    in_party = np.zeros(h, bool)
    for _, _, a, _, b in regions:
        in_party[max(0, a - 4):b + 4] = True
    # Бейджи: строки с длинной сплошной полосой цвета (верх и низ плашки)
    long_rows = []
    for yy in range(h):
        if in_party[yy]:
            continue
        n, span = runs(solid[yy])
        if n >= 45:
            long_rows.append((yy, span))
    groups = []
    for yy, span in long_rows:
        if groups and yy - groups[-1][-1][0] <= 18:
            groups[-1].append((yy, span))
        else:
            groups.append([(yy, span)])
    for g in groups:
        top, bottom = g[0][0], g[-1][0] + 1
        if 10 <= bottom - top <= 34:
            x0 = min(s[0] for _, s in g)
            x1 = max(s[1] for _, s in g)
            regions.append(("tag", x0, top, x1, bottom))
    return regions


def read_light_text(col, box, psm, langs="rus"):
    """Белый текст на цветной плашке → чёрный на белом → Tesseract.
    Цвет плашки тёмный хотя бы в одном канале (оранжевый — в синем, зелёный — в синем
    и красном), а кремовый текст светлый во всех трёх: делим по самому тёмному каналу."""
    import numpy as np
    from PIL import Image
    x0, y0, x1, y1 = box
    pad = 3
    crop = col.crop((max(0, x0 - pad), max(0, y0 - pad), x1 + pad, y1 + pad))
    scale = 4
    big = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
    arr = np.asarray(big).astype(np.int16)
    darkest = arr.min(axis=2)
    bw = np.where(darkest > 135, 0, 255).astype("uint8")
    # всё, что вокруг плашки (светлый фон бумаги), тоже стало бы «текстом» — убираем
    solid, _ = color_masks(np.asarray(big))
    rows = solid.mean(axis=1) > 0.05
    cols = solid.mean(axis=0) > 0.05
    bw[~rows, :] = 255
    bw[:, ~cols] = 255
    img = Image.fromarray(bw, "L").convert("RGB")
    lines = tesseract_lines(img, psm=psm, langs=langs, scale=1)
    for ln in lines:
        for k in ("y0", "y1"):
            ln[k] = ln[k] / scale + max(0, y0 - pad)
        for k in ("x0", "x1"):
            ln[k] = ln[k] / scale + max(0, x0 - pad)
    return lines


def ocr_column(col):
    """Все строки текста колонки: обычный тёмный текст + белый на плашках."""
    import numpy as np
    from PIL import Image
    regions = solid_regions(col)
    clean = np.asarray(col).copy()
    extra = []
    for kind, x0, y0, x1, y1 in regions:
        lines = read_light_text(col, (x0, y0, x1, y1), psm=6 if kind == "party" else 7,
                                langs="rus+eng" if kind == "party" else "rus")
        for ln in lines:
            ln["src"] = kind
            ln["region"] = (y0, y1)
        extra += lines
        clean[max(0, y0 - 3):y1 + 3, max(0, x0 - 3):x1 + 3] = (250, 246, 236)
    base = tesseract_lines(Image.fromarray(clean), psm=6)
    gray = np.asarray(Image.fromarray(clean).convert("L")).astype(np.int16)
    for ln in base:
        ln["src"] = "text"
        if not meaningful(ln) and ln["y1"] - ln["y0"] > 28:
            # иконка и подпись под ней («сампо») склеились в одну высокую строку — читаем низ отдельно
            top = ln["y0"] + (ln["y1"] - ln["y0"]) * 0.45
            fixed = False
            for box in ((0, int(ln["y1"]) - 4, col.width, int(ln["y1"]) + 34),
                        (0, int(top) - 2, col.width, int(ln["y1"]) + 6)):
                again = tesseract_lines(Image.fromarray(clean).crop(box), psm=7)
                if again and meaningful(again[0]) and (again[0]["conf"] >= 60 or any(
                        lev(compact(w), "сампо") <= 1 for w in again[0]["text"].split())):
                    ln.update(text=again[0]["text"], conf=again[0]["conf"], y0=again[0]["y0"] + box[1],
                              y1=again[0]["y1"] + box[1], words=[])
                    fixed = True
                    break
            if fixed:
                continue
        if not is_time_line(ln["text"]):
            respace(ln, gray)
    return sorted(base + extra, key=lambda l: l["y0"])


PREPS = {"с", "в", "к", "и", "о", "у", "а", "на", "по", "за", "из", "от", "до", "для", "про", "без", "под", "над"}


def respace(ln, gray):
    """Tesseract иногда склеивает предлог со словом («спартнёром»). Смотрим на пиксели:
    пробел в шрифте шире 3 px, а промежуток между буквами — 1–2 px."""
    y0, y1 = int(ln["y0"]), int(ln["y1"]) + 1
    words = []
    for text, conf, x0, x1 in ln["words"]:
        a, b = int(x0), int(x1) + 1
        ink = (gray[y0:y1, a:b] < 150).sum(axis=0)
        gaps, run = [], 0
        for i, v in enumerate(ink):
            if v == 0:
                run += 1
            else:
                if run >= 3 and i - run > 0:
                    gaps.append((i - run / 2) / max(1, len(ink)))
                run = 0
        letters = text
        done = False
        whole = fix_word(letters.strip("«»\"“”(),.")).lower()
        if whole in VOCAB or whole in NAMES or whole in EN_VOCAB:
            gaps = []
        for frac in gaps[:1]:
            guess = round(frac * len(letters))
            for cut in (guess, guess - 1, guess + 1):
                left = letters[:cut]
                if 0 < cut < len(letters) - 2 and left.lower().strip("«\"„“") in PREPS:
                    words.append(letters[:cut])
                    words.append(letters[cut:])
                    done = True
                    break
        if not done:
            words.append(text)
    ln["text"] = " ".join(words)


def meaningful(ln):
    text = ln["text"]
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", text)
    if is_time_line(text):
        return True
    if len(letters) < 3:
        return False
    if ln["conf"] < 35 and not CYR.search(text):
        return False
    # мусор от иконок: заглавные буквы вперемешку с чёрточками
    words = re.findall(r"[A-Za-zА-Яа-яЁё]{2,}", text)
    return bool(words) and (ln["conf"] >= 45 or any(len(w) >= 4 for w in words))


def split_cards(lines):
    cards, cur, last_y1, cur_region = [], [], None, None
    prev_time = False
    for ln in lines:
        if ln["src"] != "tag" and not meaningful(ln):
            continue
        region = ln.get("region") if ln["src"] == "party" else None
        new_card = False
        if cur:
            gap = ln["y0"] - last_y1
            if is_time_line(ln["text"]) and ln["src"] != "tag":
                has_time = any(is_time_line(c["text"]) for c in cur)
                if has_time or gap > 14:
                    new_card = region is None or region != cur_region
            same_party = region is not None and region == cur_region
            if gap > (70 if prev_time else 22) and ln["src"] != "tag" and not same_party:
                new_card = True
            if (region or cur_region) and region != cur_region:
                new_card = True
        if new_card:
            cards.append(cur)
            cur = []
        cur.append(ln)
        cur_region = region
        last_y1 = ln["y1"]
        prev_time = (is_time_line(ln["text"]) and ln["src"] != "tag") or (prev_time and ln["src"] == "tag")
    if cur:
        cards.append(cur)
    return cards


def parse_sub(text):
    """«Дарья Комкина · (1,5 часа)» → ("Дарья Комкина", ""); «(1,5 часа, старшая группа)» → ("", "старшая группа")."""
    text = text.replace("{", "(").replace("[", "(").replace("}", ")").replace("]", ")")
    pre, inside = text, ""
    if "(" in text:
        pre, inside = text.split("(", 1)
        inside = inside.rstrip(") ")
    elif re.search(r"\d", text):
        m = re.search(r"\d", text)
        pre, inside = text[:m.start()], text[m.start():]
    pre = re.sub(r"[·•\-–—|]+\s*$", "", pre.strip()).strip(" ·•-–—|")
    chunks = [c.strip() for c in inside.split(",")]
    kept = []
    for c in chunks:
        if not c:
            continue
        if re.search(r"\d", c) or re.search(r"ч[аa]с|м[иu]н", c.lower()):
            # «1,5 часа» / «54aca» — длительность; отделяем прилипшее название, если есть
            rest = re.sub(r"^[\d\s.,]*\S*?(час[а-я]*|aca|аса)\b", "", c, flags=re.I).strip()
            if rest and re.search(r"[а-яА-Я]{3,}", rest) and not re.search(r"\d", rest):
                kept.append(rest)
            continue
        kept.append(c)
    group = fix_words(", ".join(kept)).strip()
    group = re.sub(r"\s+", " ", group)
    return pre, group


def match_tag(text, conf):
    """Бейдж над названием («ЛАБОРАТОРИЯ») мелкий — сверяем со списком известных."""
    raw = compact(text.lower().translate(HOMO))
    if not raw:
        return ""
    best = min(TAGS, key=lambda t: lev(raw, compact(t)))
    if lev(raw, compact(best)) <= max(2, len(compact(best)) // 2):
        return best
    clean = tidy(re.sub(r"[^А-Яа-яЁё -]", "", text)).lower()
    return cap(clean) if conf >= 70 and len(clean) >= 4 else ""


def parse_card(card):
    texts = [ln["text"] for ln in card]
    srcs = [ln["src"] for ln in card]
    if "party" in srcs:
        time, words = None, []
        for ln in card:                   # название вечеринки — под временем, уверенно прочитанное
            t = ln["text"]
            if time is None and is_time_line(t):
                time = parse_time(t)
            elif time is not None and ln["conf"] >= 60 and re.search(r"[А-Яа-яЁё]{2,}", t):
                words.append(t)
        label = tidy(fix_words(" ".join(words)))
        label = re.sub(r"[^А-Яа-яЁёA-Za-z«» -]", "", label).strip()
        if not label:
            label = "Вечеринка"
        return {"kind": "note", "time": time, "label": cap(label), "party": True}

    joined = " ".join(texts)
    if not any(is_time_line(t) for t in texts):
        if any(lev(compact(w), "сампо") <= 1 for w in re.findall(r"[A-Za-zА-Яа-яЁё]+", joined)):
            return {"kind": "sampo", "label": "Сампо"}
        words = [t for t in texts if re.search(r"[A-Za-zА-Яа-яЁё]{3,}", t)]
        if not words:
            return None
        time = None
        for t in texts:
            m = re.search(r"\b\d{1,2}[:.]\d{2}\b", t)
            if m:
                time = parse_time(m.group(0))
        label = tidy(fix_words(" ".join(re.sub(r"\b\d{1,2}[:.]\d{2}\b", "", w) for w in words)))
        return {"kind": "free", "time": time, "label": cap(label)} if label else None

    time, tag, rest = None, "", []
    for ln in card:
        t = ln["text"]
        if time is None and is_time_line(t):
            time = parse_time(t)
            continue
        if ln["src"] == "tag":
            tag = match_tag(t, ln["conf"])
            continue
        rest.append(t)
    if not rest and not tag:
        return {"kind": "class", "time": time, "label": "Занятие"}
    body = fix_words(" ".join(rest))
    if re.search(r"\bзакрыт", body.lower()):
        label = "Закрыт на мероприятие" if "мероприят" in body.lower() else cap(tidy(re.sub(r"[^А-Яа-яЁё ]", " ", body)))
        return {"kind": "note", "time": time, "label": label}

    # подпись: всё, начиная со строки со скобкой / «час»
    sub_i = None
    for i, t in enumerate(rest):
        if re.search(r"[({\[]|ч[аa]с|\d[.,]\d|\d\s*ч", t.lower()):
            sub_i = i
            break
    title_lines = rest if sub_i is None else rest[:sub_i]
    sub_text = "" if sub_i is None else " ".join(rest[sub_i:])
    title = tidy(" ".join(title_lines))
    pre, group = parse_sub(sub_text) if sub_text else ("", "")

    if pre:
        who, name = canon_who(pre), tidy(fix_words(title, VOCAB + NAMES) if not tag else title)
    elif title and looks_like_people(title):
        who, name = canon_who(title), ""
    else:
        who, name = "", tidy(fix_words(title))
    if not tag and not sub_text and not who:            # «International RALLY 18:00» — разовое событие
        return {"kind": "event", "time": time, "label": cap(name or title or "Событие")}
    if tag:
        label = tag
    elif group:
        label = cap(group)
    elif name:
        label = cap(name)
        name = ""
    else:
        label = "Группа"
    item = {"kind": "class", "time": time, "label": label}
    if name:
        item["title"] = name
    if who:
        item["who"] = who
    if tag:
        item["tag"] = tag
    if NEW_RE.search(group) or NEW_RE.search(tag):
        item["new"] = True
    return item


def read_dates(img):
    """С ленточек под названиями дней: [(числа, месяц или None)] × 7."""
    out = []
    for i in range(7):
        x = GRID_X0 + i * COL_STEP
        band = img.crop((x + 16, RIBBON_Y[0], x + COL_W - 16, RIBBON_Y[1]))
        try:
            import numpy as np
            solid, _ = color_masks(np.asarray(band))
            rows = np.where(solid.mean(axis=1) > 0.5)[0]
            if len(rows) < 8:
                out.append(((), None))
                continue
            box = (0, int(rows[0]), band.width, int(rows[-1]) + 1)
            lines = read_light_text(band, box, psm=7)
        except Exception:
            out.append(((), None))
            continue
        text = " ".join(l["text"] for l in lines)
        # Nauryz рисует «1» так, что её читают как «7», «{», «/» — надёжна последняя цифра
        m = re.search(r"[А-Яа-яЁёA-Za-z]{3,}", text)
        head = text[:m.start()] if m else text
        nums = set(re.findall(r"\d{1,2}", head))
        month = None
        for w in re.findall(r"[А-Яа-яЁёA-Za-z]{4,}", text):
            w = w.lower().translate(HOMO)
            scored = sorted((lev(w, m), k) for k, m in enumerate(MONTHS_GEN))
            if scored[0][0] <= 4 and (len(scored) < 2 or scored[0][0] < scored[1][0]):
                month = scored[0][1] + 1
        out.append((tuple(sorted(nums)), month))
    return out


def pick_week_start(dates, posted):
    """Понедельник недели: по датам с ленточек, сверяя с датой публикации поста."""
    monday = posted - dt.timedelta(days=posted.weekday())
    candidates = [monday + dt.timedelta(days=7 * k) for k in (0, 1, -1)]
    best, best_score = None, 0
    for c in candidates:
        score = 0
        for i, (nums, month) in enumerate(dates):
            d = c + dt.timedelta(days=i)
            if month is not None and month != d.month:
                continue
            if any(n == str(d.day) or n[-1] == str(d.day)[-1] for n in nums):
                score += 1
        if score > best_score:
            best, best_score = c, score
    if best and best_score >= 2:
        return best, best_score
    # по датам не вышло: пост в пятницу–воскресенье — расписание на следующую неделю
    return (monday + dt.timedelta(days=7) if posted.weekday() >= 4 else monday), 0


def parse_image(path):
    from PIL import Image
    img = Image.open(path).convert("RGB")
    ratio = img.height / img.width
    if abs(ratio - REF_H / REF_W) > 0.04:
        raise ValueError(f"Картинка {img.width}×{img.height} не похожа на шаблон расписания")
    if img.width != REF_W:
        img = img.resize((REF_W, round(img.height * REF_W / img.width)), Image.LANCZOS)
    days = []
    for i in range(7):
        x = GRID_X0 + i * COL_STEP
        col = img.crop((x, CARDS_Y[0], x + COL_W, CARDS_Y[1]))
        items = []
        for card in split_cards(ocr_column(col)):
            item = parse_card(card)
            if item:
                items.append({k: v for k, v in item.items() if v not in (None, "")})
        days.append(items)
    return img, days


def validate(days):
    classes = [it for d in days for it in d if it["kind"] in ("class", "note") and it.get("time")]
    filled = sum(1 for d in days if d)
    problems = []
    if filled < 5:
        problems.append(f"распознано только {filled} дней")
    if len(classes) < 4:
        problems.append(f"найдено только {len(classes)} занятий со временем")
    return problems


# ---------------------------------------------------------------------------
# Сборка файлов для сайта

def to_week_json(days, week_start, sha1):
    out_days = []
    for i, items in enumerate(days):
        cleaned = []
        for it in items:
            e = {"kind": it["kind"], "label": it["label"]}
            for k in ("time", "who", "title", "tag"):
                if it.get(k):
                    e[k] = it[k]
            if it.get("new"):
                e["new"] = True
            if it.get("new") or it.get("party"):
                e["hot"] = True
            cleaned.append(e)
        cleaned.sort(key=lambda e: (e.get("time") is None and e["kind"] != "sampo", e.get("time") or "99"))
        out_days.append({"day": DAY_SHORT[i], "items": cleaned})
    return {
        "version": FORMAT_VERSION,
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(),
        "week_start": week_start.isoformat(),
        "source_sha1": sha1,
        "days": out_days,
    }


def update_groups(week, old):
    """Постоянные группы за последние недели: по каждому дню+времени — кто ведёт и как называется."""
    start = dt.date.fromisoformat(week["week_start"])
    seen = [o for o in (old or {}).get("seen", [])
            if o.get("week") != week["week_start"]
            and (start - dt.date.fromisoformat(o["week"])).days <= MEMORY_DAYS]
    for di, d in enumerate(week["days"]):
        for it in d["items"]:
            if it["kind"] != "class" or not it.get("time") or it.get("tag"):
                continue
            seen.append({"week": week["week_start"], "day": di, "time": it["time"],
                         "who": it.get("who", ""), "label": it["label"],
                         "title": it.get("title", ""), "new": bool(it.get("new"))})
    groups = []
    for key in sorted({(o["day"], o["time"]) for o in seen}):
        obs = [o for o in seen if (o["day"], o["time"]) == key]
        counts = {}
        for o in obs:
            counts[o["who"]] = counts.get(o["who"], 0) + 1
        latest = max(o["week"] for o in obs)
        # кто ведёт — чаще всего за 3 недели (разовая замена не считается),
        # название — самое свежее у этого преподавателя (группы растут и переименовываются)
        who = max(counts, key=lambda w: (counts[w], max(o["week"] for o in obs if o["who"] == w)))
        mine = max((o for o in obs if o["who"] == who), key=lambda o: o["week"])
        if (start - dt.date.fromisoformat(latest)).days > MEMORY_DAYS:
            continue
        g = {"day": key[0], "time": key[1], "label": mine["label"]}
        if who:
            g["who"] = who
        if mine.get("title"):
            g["title"] = mine["title"]
        if mine["new"] and mine["week"] == latest:
            g["new"] = True
        g["seen"] = mine["week"]
        groups.append(g)
    return {"version": FORMAT_VERSION, "updated": week["updated"], "week_start": week["week_start"],
            "groups": groups, "seen": seen}


# ---------------------------------------------------------------------------

def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def posted_date(meta_path, override=None):
    if override:
        return dt.date.fromisoformat(override)
    meta = load_json(meta_path) or {}
    stamp = meta.get("posted") or meta.get("updated")
    msk = dt.timezone(dt.timedelta(hours=3))
    if stamp:
        try:
            return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(msk).date()
        except ValueError:
            pass
    return dt.datetime.now(msk).date()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--need", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--image", default=SCHEDULE_IMAGE)
    ap.add_argument("--out-dir", default=REPO_ROOT)
    ap.add_argument("--posted")
    args = ap.parse_args()

    week_path = os.path.join(args.out_dir, "week.json")
    groups_path = os.path.join(args.out_dir, "groups.json")
    if not os.path.exists(args.image):
        print("no" if args.need else "Картинки расписания нет.")
        return
    sha1 = sha1_of(args.image)
    old = load_json(week_path) or {}
    fresh = old.get("source_sha1") == sha1 and old.get("version") == FORMAT_VERSION
    if args.need:
        print("no" if fresh else "yes")
        return
    if fresh and not args.force:
        print("Картинка расписания не менялась.")
        return

    img, days = parse_image(args.image)
    problems = validate(days)
    if problems:
        print("Не уверен в распознавании (" + "; ".join(problems) + ") — week.json не трогаю.")
        for i, d in enumerate(days):
            print(" ", DAY_SHORT[i], d)
        sys.exit(1)
    week_start, score = pick_week_start(read_dates(img), posted_date(SCHEDULE_META, args.posted))
    week = to_week_json(days, week_start, sha1)
    with open(week_path, "w", encoding="utf-8") as f:
        json.dump(week, f, ensure_ascii=False, indent=2)
    groups = update_groups(week, load_json(groups_path))
    with open(groups_path, "w", encoding="utf-8") as f:
        json.dump(groups, f, ensure_ascii=False, indent=2)

    print(f"Неделя с {week_start:%d.%m.%Y} (совпало дат на ленточках: {score} из 7):")
    for d in week["days"]:
        print(" ", d["day"], " | ".join(
            (it.get("time", "") + " " + it["label"] + (" — " + it["who"] if it.get("who") else "")
             + (" " + it["title"] if it.get("title") else "") + (" [набор]" if it.get("new") else "")).strip()
            for it in d["items"]))


if __name__ == "__main__":
    main()
