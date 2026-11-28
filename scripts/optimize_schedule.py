#!/usr/bin/env python3
"""
Делает лёгкую копию картинки расписания для сайта: schedule.jpg → schedule.webp.

schedule.jpg из Telegram весит ~450 КБ (2560×1800). Её оставляем как есть —
по ней update_week.py распознаёт расписание. На сайте показываем WebP того же
размера (~200 КБ): картинка грузится вдвое быстрее, а «Открыть крупно» остаётся чётким.

Результат детерминированный: если schedule.jpg не менялась, schedule.webp
получается байт-в-байт таким же и git не видит изменений.
"""
import io
import os

from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO_ROOT, "schedule.jpg")
DST = os.path.join(REPO_ROOT, "schedule.webp")
MAX_WIDTH = 2560
QUALITY = 80


def build(src=SRC):
    with Image.open(src) as im:
        im = im.convert("RGB")
        if im.width > MAX_WIDTH:
            im = im.resize((MAX_WIDTH, round(im.height * MAX_WIDTH / im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=QUALITY, method=6)
        return buf.getvalue()


def main():
    if not os.path.exists(SRC):
        print("schedule.jpg нет — пропускаю")
        return
    data = build()
    old = open(DST, "rb").read() if os.path.exists(DST) else None
    if data != old:
        with open(DST, "wb") as f:
            f.write(data)
        print("schedule.webp обновлён: %d КБ (было %d КБ в jpg)" % (len(data) // 1024, os.path.getsize(SRC) // 1024))
    else:
        print("schedule.webp не менялся")


if __name__ == "__main__":
    main()
