#!/usr/bin/env python3
"""Проверка update_content.py без Telegram и без сети: python3 scripts/test_update_content.py

Поддельный Telegram отдаёт посты и их правки (как getUpdates), скрипт работает во временной папке.
Главное — хештег, дописанный после публикации, подхватывается без перепоста.
"""
import json
import urllib.parse
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
import io

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_content as uc  # noqa: E402

CH = {"id": -100, "type": "channel", "username": "domwcs"}


def read(path, binary=False):
    with open(path, "rb" if binary else "r", **({} if binary else {"encoding": "utf-8"})) as f:
        return f.read() if binary else json.load(f)


def write(path, data):
    with open(path, "wb") as f:
        f.write(data)


class FakeTelegram:
    def __init__(self):
        self.queue, self.seq = [], 0

    def push(self, kind, **post):
        self.seq += 1
        post.setdefault("chat", CH)
        post.setdefault("date", 1_790_000_000 + post["message_id"])
        self.queue.append({"update_id": self.seq, kind: post})

    def api(self, token, method, params=None):
        if method == "getChatMemberCount":
            return 100
        if method == "getUpdates":
            allowed = json.loads(params.get("allowed_updates", "[]"))
            start = params.get("offset", 0)
            # как настоящий Telegram: отдаёт только те виды событий, которые попросили
            return [u for u in self.queue if u["update_id"] >= start and any(k in u for k in allowed)]
        if method == "getFile":
            return {"file_path": params["file_id"]}
        raise AssertionError(method)


class UpdateContentTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "scripts"))
        for name, path in [("OFFSET_FILE", "scripts/telegram_offset.txt"), ("SCHEDULE_IMAGE", "schedule.jpg"),
                           ("SCHEDULE_META", "schedule-meta.json"), ("EVENTS_FILE", "events.json"),
                           ("EVENTS_META", "events-meta.json"), ("SUBSCRIBERS_FILE", "subscribers.json"),
                           ("NEWS_DIR", "news"), ("LOOKUPS_FILE", "scripts/news_lookups.json"), ("WCS_FILE", "news/wcs.json")]:
            setattr(uc, name, os.path.join(self.dir, path))
        uc.REPO_ROOT = self.dir
        self.tg = FakeTelegram()
        uc.api_call = self.tg.api
        uc.telegram_file = lambda token, file_id: b"file:" + file_id.encode()
        uc.download_file = lambda token, file_id, dest: write(dest, b"file:" + file_id.encode())
        def no_net(*a, **k):
            raise OSError("нет сети в тесте")
        uc.http_get = no_net
        os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHANNEL"] = "x", "@domwcs"

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_script(self):
        with redirect_stdout(io.StringIO()):
            uc.main()

    def events(self):
        p = os.path.join(self.dir, "events.json")
        return read(p) if os.path.exists(p) else []

    def meta(self):
        p = os.path.join(self.dir, "schedule-meta.json")
        return read(p) if os.path.exists(p) else {}

    def test_posts_from_crm_bot(self):
        # CHANNEL_FEED_URL: посты берём у бота CRM (тот же ответ, что у getUpdates), getUpdates не вызываем
        self.tg.push("channel_post", message_id=50, caption="#расписание", photo=[{"file_id": "s50", "file_unique_id": "u50"}])
        asked = []
        def feed(url, timeout=30):
            asked.append(url)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            start = int(q.get("offset", ["0"])[0])
            return json.dumps({"ok": True, "result": [u for u in self.tg.queue if u["update_id"] >= start]}).encode()
        uc.http_get = feed
        real = uc.api_call
        uc.api_call = lambda token, method, params=None: (_ for _ in ()).throw(AssertionError("getUpdates при CHANNEL_FEED_URL")) if method == "getUpdates" else real(token, method, params)
        os.environ["CHANNEL_FEED_URL"] = "https://crm.example/"
        try:
            self.run_script()
            self.assertEqual(self.meta().get("file"), "u50")
            self.assertTrue(asked[0].startswith("https://crm.example/api/channel/updates?chat=domwcs"))
            self.tg.push("edited_channel_post", message_id=50, caption="#расписание", photo=[{"file_id": "s51", "file_unique_id": "u51"}])
            self.run_script()
            self.assertIn("offset=2", [u for u in asked if u.startswith("https://crm.example/")][-1])
            self.assertEqual(self.meta().get("file"), "u51")
        finally:
            os.environ.pop("CHANNEL_FEED_URL")

    def test_vk_gets_tagged_posts(self):
        import vk_crosspost as vk
        from test_vk_crosspost import FakeVK
        fake = FakeVK()
        vk.vk_call, vk.vk_upload, vk.STATE_FILE = fake.call, fake.upload, os.path.join(self.dir, "scripts", "vk_posts.json")
        os.environ.update(VK_TOKEN="group", VK_GROUP_ID="42")
        try:
            self.tg.push("channel_post", message_id=40, text="Вечеринка\n#новости")
            self.tg.push("channel_post", message_id=41, text="Просто пост")
            self.run_script()
        finally:
            os.environ.pop("VK_TOKEN"); os.environ.pop("VK_GROUP_ID")
        self.assertEqual([m for m, _ in fake.wall.values()], ["Вечеринка\n#новости"])
        self.assertEqual(len(self.events()), 1, "сайт обновился как обычно")

    def test_tags_any_case(self):
        self.tg.push("channel_post", message_id=80, text="Вечеринка в субботу\n#НОВОСТИ")
        self.tg.push("channel_post", message_id=81, text="Мастер-класс\n#Новости")
        self.tg.push("channel_post", message_id=82, caption="#РАСПИСАНИЕ", photo=[{"file_id": "s1", "file_unique_id": "u1", "width": 800, "height": 800}])
        self.run_script()
        self.assertEqual(sorted(e["name"] for e in self.events()), ["Вечеринка в субботу", "Мастер-класс"])
        self.assertEqual(self.meta().get("message_id"), 82)

    def test_news_tag_added_later(self):
        self.tg.push("channel_post", message_id=10, text="Вечеринка в субботу\nПриходите все")
        self.run_script()
        self.assertEqual(self.events(), [], "без тега на сайт не попадает")
        self.tg.push("edited_channel_post", message_id=10, text="Вечеринка в субботу\nПриходите все\n#новости")
        self.run_script()
        ev = self.events()
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["name"], "Вечеринка в субботу")
        self.assertEqual(ev[0]["url"], "https://t.me/domwcs/10")

    def test_schedule_tag_added_later(self):
        self.tg.push("channel_post", message_id=20, caption="Неделя", photo=[{"file_id": "s20", "file_unique_id": "u20", "width": 1000, "height": 1000}])
        self.run_script()
        self.assertFalse(os.path.exists(os.path.join(self.dir, "schedule.jpg")))
        self.tg.push("edited_channel_post", message_id=20, caption="Неделя #расписание", photo=[{"file_id": "s20", "file_unique_id": "u20", "width": 1000, "height": 1000}])
        self.run_script()
        self.assertEqual(read(os.path.join(self.dir, "schedule.jpg"), True), b"file:s20")
        self.assertEqual(self.meta().get("message_id"), 20)

    def test_edit_updates_card_without_duplicate(self):
        self.tg.push("channel_post", message_id=30, text="Мастер-класс #новости\nв пятницу")
        self.run_script()
        self.tg.push("edited_channel_post", message_id=30, text="Мастер-класс по WCS #новости\nв пятницу в 19:00–21:00")
        self.run_script()
        ev = self.events()
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["name"], "Мастер-класс по WCS")
        self.assertEqual(ev[0]["tag"], "19:00–21:00")

    def test_post_and_edit_in_one_batch(self):
        self.tg.push("channel_post", message_id=40, text="Опечатка #новости")
        self.tg.push("edited_channel_post", message_id=40, text="Без опечатки #новости")
        self.run_script()
        self.assertEqual([e["name"] for e in self.events()], ["Без опечатки"])

    def test_tag_removed_card_goes_away(self):
        self.tg.push("channel_post", message_id=50, text="Новость #новости")
        self.run_script()
        self.tg.push("edited_channel_post", message_id=50, text="Новость")
        self.run_script()
        self.assertEqual(self.events(), [])

    def test_old_schedule_edit_does_not_replace_newer(self):
        self.tg.push("channel_post", message_id=61, caption="#расписание", photo=[{"file_id": "new", "file_unique_id": "un", "width": 1, "height": 1}])
        self.run_script()
        self.tg.push("edited_channel_post", message_id=60, caption="Прошлая неделя #расписание", photo=[{"file_id": "old", "file_unique_id": "uo", "width": 1, "height": 1}])
        self.run_script()
        self.assertEqual(read(os.path.join(self.dir, "schedule.jpg"), True), b"file:new")

    def test_same_schedule_picture_no_rewrite(self):
        photo = [{"file_id": "s70", "file_unique_id": "u70", "width": 1, "height": 1}]
        self.tg.push("channel_post", message_id=70, caption="#расписание", photo=photo)
        self.run_script()
        before = self.meta()
        self.tg.push("edited_channel_post", message_id=70, caption="#расписание — уточнили подпись", photo=photo)
        self.run_script()
        self.assertEqual(self.meta(), before, "картинка та же — сайт не пересобирается зря")

    def test_old_news_edit_not_resurrected(self):
        for i in range(100, 100 + uc.MAX_EVENTS):
            self.tg.push("channel_post", message_id=i, text=f"Новость {i} #новости")
        self.run_script()
        self.tg.push("edited_channel_post", message_id=5, text="Очень старая новость #новости — поправили опечатку")
        self.run_script()
        self.assertNotIn("https://t.me/domwcs/5", [e.get("url") for e in self.events()])

    def test_wcs_news_feed(self):
        self.assertTrue(uc.NEWS_TAG_RE.search("Вечеринка #Новости.") and not uc.NEWS_TAG_RE.search("Фестиваль #новостиWCS"))
        def page(*posts):  # как t.me/s: data-post, текст, время
            return "".join(f'<div class="tgme_widget_message_wrap"><div data-post="domwcs/{i}"><div class="tgme_widget_message_text js-message_text">{t}</div>'
                           f'<time datetime="2026-10-{i:02d}T10:00:00+00:00"></time></div></div>' for i, t in posts).encode()
        pages = [page((12, "Swing &amp; Snow<br>Ранняя цена до 31-го<br>#новостиWCS"), (11, "Вечеринка #новости"), (10, "Судейство WSDC<br>Текст #новостиWCS"))]
        uc.http_get = lambda url, timeout=30: pages[0] if "before" not in url else b""
        os.makedirs(os.path.dirname(uc.WCS_FILE), exist_ok=True)
        write(uc.WCS_FILE, json.dumps([{"id": 5, "url": "https://t.me/domwcs/5", "date": "", "title": "Старая", "text": ""}]).encode())
        self.assertTrue(uc.update_wcs_news("@domwcs"))
        with open(uc.WCS_FILE, encoding="utf-8") as f:
            feed = json.load(f)
        self.assertEqual([i["id"] for i in feed], [12, 10, 5])   # старше окна — осталась, без тега — нет
        self.assertEqual((feed[0]["title"], feed[0]["text"], feed[0]["date"]), ("Swing & Snow", "Ранняя цена до 31-го", "2026-10-12"))
        self.assertFalse(uc.update_wcs_news("@domwcs"))           # без изменений файл не трогаем
        uc.cleanup_thumbs([])                                     # уборка превью ленту не стирает
        self.assertTrue(os.path.exists(uc.WCS_FILE))
        pages[0] = page((12, "Swing &amp; Snow<br>#новостиWCS"), (11, "x"), (10, "Судейство WSDC<br>тег убрали"))
        uc.update_wcs_news("@domwcs")
        with open(uc.WCS_FILE, encoding="utf-8") as f:
            self.assertEqual([i["id"] for i in json.load(f)], [12, 5])

if __name__ == "__main__":
    unittest.main(verbosity=2)
