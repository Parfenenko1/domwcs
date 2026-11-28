#!/usr/bin/env python3
"""
Вписывает расписание групп прямо в HTML главной страницы — для поисковиков.

Яндекс и Google не читают картинку расписания и не всегда выполняют JavaScript,
поэтому без этого скрипта они видят только устаревшую заглушку «Обычная неделя».

Что делает:
  1. Блок «Обычная неделя в Доме» (между метками static-week) — те же карточки,
     что рисует JavaScript, но уже в исходном HTML и со временем занятий.
  2. Разметку schema.org (между метками static-ld): каждая постоянная группа —
     курс (Course) с еженедельным расписанием, воскресная вечеринка — событие (Event).

Источник — groups.json (его пишет update_week.py). Если groups.json пустой или
сломан, используется week.json; если и его нет — файл не трогается.
Скрипт детерминированный: при тех же данных index.html не меняется, лишних коммитов нет.

Запуск: python scripts/render_static.py [--check]
  --check — ничего не пишет, код выхода 1, если index.html устарел.
"""
import html
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(REPO_ROOT, "index.html")
GROUPS_FILE = os.path.join(REPO_ROOT, "groups.json")
WEEK_FILE = os.path.join(REPO_ROOT, "week.json")
SITE = "https://wcshome.ru/"

DAY_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
DAY_SCHEMA = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# то же, что BASE_EXTRA в index.html: пятница закрыта с 19:30, в воскресенье вечеринка
EXTRA = [
    {"day": 4, "time": "19:30", "kind": "note", "label": "Закрыт"},
    {"day": 6, "time": "19:00", "kind": "note", "label": "Вечеринка в Доме", "hot": True},
]
PARTY_PRICE = "500"


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def mins(t):
    try:
        h, m = str(t).split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        return 99 * 60


def regular_groups():
    g = load_json(GROUPS_FILE)
    if g and isinstance(g.get("groups"), list) and g["groups"]:
        return [x for x in g["groups"] if isinstance(x.get("day"), int) and 0 <= x["day"] <= 6 and x.get("time")]
    w = load_json(WEEK_FILE)
    out = []
    if w and isinstance(w.get("days"), list) and len(w["days"]) == 7:
        for day, d in enumerate(w["days"]):
            for it in d.get("items") or []:
                if it.get("kind") == "class" and it.get("time") and not it.get("tag"):
                    out.append({"day": day, "time": it["time"], "label": it.get("label", ""),
                                "who": it.get("who", ""), "new": bool(it.get("new"))})
    return out


def regular_week(groups):
    """Повторяет regularWeek() + stripItems() из index.html."""
    days = [[] for _ in range(7)]
    for g in groups:
        days[g["day"]].append({"kind": "class", "time": g["time"], "label": g.get("label", ""),
                               "who": g.get("who", ""), "hot": bool(g.get("new"))})
    for x in EXTRA:
        days[x["day"]].append(dict(x))
    out = []
    for items in days:
        closed = next((it for it in items if it["kind"] == "note" and it["label"].lower().startswith("закрыт")), None)
        rest = sorted((it for it in items if it is not closed), key=lambda it: mins(it.get("time")))
        if closed:
            rest.append({"label": "Сампо до " + closed["time"]})   # сампо в Доме каждый день
        if not rest:
            rest.append({"label": "Сампо"})
        out.append(rest)
    return out


def esc(s):
    return html.escape(str(s or ""), quote=True)


def week_html(days):
    lines = []
    for i, items in enumerate(days):
        cells = []
        for it in items:
            tm = '<span class="tm">' + esc(it["time"]) + "</span> " if it.get("time") else ""
            who = '<small>' + esc(it["who"]) + "</small>" if it.get("who") else ""
            cls = "e hot" if it.get("hot") else "e"
            cells.append('<span class="' + cls + '">' + tm + esc(it["label"]) + who + "</span>")
        lines.append('          <li><span class="d">' + DAY_SHORT[i] + "</span>" + "".join(cells) + "</li>")
    return '        <ul class="week" id="week" lang="ru">\n' + "\n".join(lines) + "\n        </ul>\n"


def teacher_names(index_html):
    """«Саша и Лена» → «Александр Парфененко и Елена Логашина» из карточек преподавателей."""
    names = {}
    for short, full in re.findall(r'<h3>([^<]+)</h3>\s*<p class="names">([^<]+)</p>', index_html):
        names[html.unescape(short).strip()] = html.unescape(full).strip()
    return names


def end_time(t, minutes):
    m = mins(t) + minutes
    return "%02d:%02d" % (m // 60 % 24, m % 60)


def ld_json(groups, names):
    school = {"@id": SITE + "#school"}
    place = {"@type": "Place", "name": "Дом танца WCS",
             "address": {"@type": "PostalAddress", "streetAddress": "ул. Разъезжая, 36",
                         "addressLocality": "Санкт-Петербург", "addressCountry": "RU"}}
    graph = []
    for g in sorted(groups, key=lambda x: (x["day"], mins(x["time"]))):
        who = (g.get("who") or "").strip()
        instructors = []
        for part in re.split(r"\s+и\s+", names.get(who, who)):
            if part.strip():
                instructors.append({"@type": "Person", "name": part.strip()})
        label = g.get("label") or "Группа"
        name = "West Coast Swing: " + label.lower() + (" (" + who + ")" if who else "")
        course = {
            "@type": "Course",
            "name": name,
            "description": "Групповые занятия по вест-кост свингу (West Coast Swing) в Санкт-Петербурге, "
                           + DAY_SHORT[g["day"]].lower() + " " + g["time"] + ", 1,5 часа"
                           + (". Идёт набор с нуля." if g.get("new") else "."),
            "inLanguage": "ru",
            "provider": school,
            "hasCourseInstance": {
                "@type": "CourseInstance",
                "courseMode": "onsite",
                "location": place,
                "courseSchedule": {
                    "@type": "Schedule",
                    "repeatFrequency": "P1W",
                    "byDay": "https://schema.org/" + DAY_SCHEMA[g["day"]],
                    "startTime": g["time"],
                    "endTime": end_time(g["time"], 90),
                    "duration": "PT1H30M",
                    "scheduleTimezone": "Europe/Moscow",
                },
            },
        }
        if instructors:
            course["hasCourseInstance"]["instructor"] = instructors
        graph.append(course)
    party = next(x for x in EXTRA if x.get("hot"))
    graph.append({
        "@type": "Event",
        "name": "Вечеринка West Coast Swing в Доме танца WCS",
        "description": "Танцевальный вечер вест-кост свинга по воскресеньям, 3 часа. Можно прийти без пары.",
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "eventStatus": "https://schema.org/EventScheduled",
        "location": place,
        "organizer": school,
        "eventSchedule": {
            "@type": "Schedule",
            "repeatFrequency": "P1W",
            "byDay": "https://schema.org/" + DAY_SCHEMA[party["day"]],
            "startTime": party["time"],
            "endTime": end_time(party["time"], 180),
            "duration": "PT3H",
            "scheduleTimezone": "Europe/Moscow",
        },
        "offers": {"@type": "Offer", "price": PARTY_PRICE, "priceCurrency": "RUB", "url": SITE + "#pricing"},
    })
    data = {"@context": "https://schema.org", "@graph": graph}
    body = json.dumps(data, ensure_ascii=False, indent=1).replace("</", "<\\/")
    return '<script type="application/ld+json" id="ld-schedule">\n' + body + "\n</script>\n"


def replace_between(text, name, content):
    pat = re.compile(r"(<!-- " + name + r":start[^>]*-->\n)(.*?)([ \t]*<!-- " + name + r":end -->)", re.S)
    if not pat.search(text):
        raise SystemExit("В index.html нет меток " + name + " — пропускаю, ничего не меняю.")
    return pat.sub(lambda m: m.group(1) + content + m.group(3), text, count=1)


def render(index_html):
    groups = regular_groups()
    if not groups:
        return index_html
    out = replace_between(index_html, "static-week", week_html(regular_week(groups)))
    out = replace_between(out, "static-ld", ld_json(groups, teacher_names(index_html)))
    return out


def main():
    with open(INDEX, encoding="utf-8") as f:
        old = f.read()
    new = render(old)
    if "--check" in sys.argv:
        sys.exit(0 if new == old else 1)
    if new != old:
        with open(INDEX, "w", encoding="utf-8") as f:
            f.write(new)
        print("index.html: расписание в HTML обновлено")
    else:
        print("index.html: расписание в HTML не менялось")


if __name__ == "__main__":
    main()
