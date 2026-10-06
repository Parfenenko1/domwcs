#!/usr/bin/env python3
"""Проверка дублирования в ВК без сети: python3 scripts/test_vk_crosspost.py

Поддельный ВК запоминает записи на стене; посты — как их отдаёт getUpdates.
"""
import io
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vk_crosspost as vk  # noqa: E402

TAG = re.compile(r"#(расписание|новости)", re.IGNORECASE)
NOW = 1_790_000_000


class FakeVK:
    def __init__(self, group_photos=True):
        self.wall, self.seq, self.photo_seq, self.down, self.group_photos, self.calls = {}, 100, 0, False, group_photos, []
        self.group_admin = True
        self.uploaded_videos, self.video_seq, self.bad_admin = [], 500, False

    def call(self, token, method, params):
        self.calls.append((token, method))
        if self.down:
            raise OSError("ВК не отвечает")
        if token == "admin" and self.bad_admin:
            raise vk.VkError(method, {"error_code": 5, "error_msg": "User authorization failed: invalid access_token (4)."})
        if method == "video.save":
            assert token == "admin", "видео грузится только ключом администратора"
            self.video_seq += 1
            return {"upload_url": f"https://vup/{self.video_seq}", "owner_id": -42, "video_id": self.video_seq}
        if method == "groups.getById":
            assert params.get("group_id") in (None, "wcs_spb"), params
            return {"groups": [{"id": 42, "screen_name": "wcs_spb"}]}
        if method == "photos.getWallUploadServer":
            if token == "group" and not self.group_photos:
                raise vk.VkError(method, {"error_code": 27, "error_msg": "Group authorization failed"})
            return {"upload_url": "https://up/" + token}
        if method == "photos.saveWallPhoto":
            self.photo_seq += 1
            return [{"owner_id": -42, "id": self.photo_seq}]
        if method in ("wall.get", "wall.edit", "wall.delete") and token == "group" and self.group_admin is False:
            raise vk.VkError(method, {"error_code": 27, "error_msg": "method is unavailable with group auth."})
        if method == "wall.get":
            return {"items": [{"id": k, "text": m} for k, (m, _) in sorted(self.wall.items(), reverse=True)]}
        if method == "wall.post":
            self.seq += 1
            self.wall[self.seq] = (params["message"], params["attachments"])
            return {"post_id": self.seq}
        if method == "wall.edit":
            assert int(params["post_id"]) in self.wall
            self.wall[int(params["post_id"])] = (params["message"], params["attachments"])
            return 1
        if method == "wall.delete":
            del self.wall[int(params["post_id"])]
            return 1
        raise AssertionError(method)

    def upload(self, url, filename, raw, field="photo", ctype="image/jpeg"):
        if field == "video_file":
            self.uploaded_videos.append(raw)
            return {"owner_id": -42, "video_id": int(url.rsplit("/", 1)[-1]), "size": len(raw)}
        return {"photo": raw.decode(), "server": 1, "hash": "h"}


def post(mid, text=None, photo=None, group=None, date=NOW - 60):
    p = {"message_id": mid, "date": date, "chat": {"username": "domwcs"}}
    if photo:
        p["photo"] = [{"file_id": photo + "_s", "file_unique_id": photo + "_s"}, {"file_id": photo, "file_unique_id": photo}]
        if text is not None:
            p["caption"] = text
    elif text is not None:
        p["text"] = text
    if group:
        p["media_group_id"] = group
    return p


class VkCrosspostTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        vk.STATE_FILE = os.path.join(self.dir, "vk_posts.json")
        self.vk = FakeVK()
        vk.vk_call, vk.vk_upload = self.vk.call, self.vk.upload
        os.environ.update(VK_TOKEN="group", VK_GROUP_ID="42")
        os.environ.pop("VK_USER_TOKEN", None)

    def run_vk(self, *posts, now=NOW):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return vk.crosspost(list(posts), TAG, lambda fid: fid.encode(), "domwcs", now=now)

    def test_off_without_secrets(self):
        os.environ.pop("VK_TOKEN")
        self.assertFalse(self.run_vk((post(1, "Вечеринка\n#Новости"), False)))
        self.assertEqual(self.vk.calls, [])

    def test_group_from_key_or_short_name(self):
        for gid in (None, "wcs_spb"):
            vk._gid.clear()
            if gid:
                os.environ["VK_GROUP_ID"] = gid
            else:
                os.environ.pop("VK_GROUP_ID")
            self.run_vk((post(40 + len(self.vk.wall), "Вечеринка\n#новости", photo="x"), False))
        self.assertEqual(len(self.vk.wall), 2)
        self.assertIn(("group", "groups.getById"), self.vk.calls)

    def test_manual_vk_post_not_duplicated(self):
        # в ВК уже вручную выложили этот пост (без тега, чуть другие знаки)
        self.vk.wall[7] = ("Вечеринка в субботу! Начало в 20:00, вход 500 ₽", "photo-42_99")
        self.run_vk((post(50, "Вечеринка в субботу. Начало в 20:00, вход 500 ₽\n#Новости", photo="z"), False))
        self.assertEqual(len(self.vk.wall), 1, "второй раз не выложили")
        # правка такого поста в Telegram ручную запись не трогает
        self.run_vk((post(50, "Вечеринка в воскресенье\n#Новости", photo="z"), True))
        self.assertEqual(self.vk.wall[7][0], "Вечеринка в субботу! Начало в 20:00, вход 500 ₽")
        self.run_vk((post(50, "Без тега"), True))
        self.assertIn(7, self.vk.wall, "и не удаляет")

    def test_posts_before_launch_untouched(self):
        self.run_vk(now=NOW)                       # первый запуск — запоминается время включения
        self.assertIn("since", vk.load_state())
        self.run_vk((post(60, "Старый пост, исправили\n#Новости", date=NOW - 3600), True), now=NOW + 300)
        self.assertEqual(self.vk.wall, {}, "правка поста, вышедшего до включения, в ВК не уходит")
        self.run_vk((post(61, "Новый пост после включения\n#Новости", date=NOW + 600), False), now=NOW + 900)
        self.assertEqual(len(self.vk.wall), 1)

    def test_tag_any_case(self):
        for i, tag in enumerate(["#Расписание", "#РАСПИСАНИЕ", "#расписание", "#Новости", "#НОВОСТИ", "#новости", "#НоВоСтИ"]):
            self.run_vk((post(70 + i, f"Пост номер {i} про танцы\n{tag}"), False))
        self.assertEqual(len(self.vk.wall), 7)

    def test_backfill_today_from_public_page(self):
        page = (
            '<div class="tgme_widget_message_wrap"><div data-post="domwcs/454" class="x">'
            '<a class="tgme_widget_message_photo_wrap" style="width:1px;background-image:url(\'https://cdn/a.jpg\')"></a>'
            '<a class="tgme_widget_message_photo_wrap" style="background-image:url(\'https://cdn/b.jpg\')"></a>'
            '<div class="tgme_widget_message_text js-message_text" dir="auto">Расписание на неделю<br/><a href="?q=%23расписание">#Расписание</a></div>'
            '<time datetime="2026-09-21T06:00:23+00:00" class="time">09:00</time></div></div>'
            '<div class="tgme_widget_message_wrap"><div data-post="domwcs/460" class="x">'
            '<div class="tgme_widget_message_text js-message_text" dir="auto">Сегодня вечеринка &amp; танцы до утра<br/>#НОВОСТИ</div>'
            '<time datetime="' + "2026-09-21T15:00:00+00:00" + '" class="time">18:00</time></div></div>'
            '<div class="tgme_widget_message_wrap"><div data-post="domwcs/461" class="x">'
            '<div class="tgme_widget_message_text js-message_text" dir="auto">Просто пост без тега, сегодня</div>'
            '<time datetime="2026-09-21T16:00:00+00:00" class="time">19:00</time></div></div>')
        fetched = []
        def fetch(url):
            fetched.append(url)
            return page.encode() if "t.me/s/" in url else url.encode()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            vk.backfill("сегодня", TAG, "domwcs", fetch, now=NOW)   # NOW — 21.09.2026 по Москве
        msgs = sorted(m for m, _ in self.vk.wall.values())
        self.assertEqual(msgs, ["Расписание на неделю\n#Расписание", "Сегодня вечеринка & танцы до утра\n#НОВОСТИ"])
        self.assertIn("photo-42_1,photo-42_2", [a for _, a in self.vk.wall.values()], "альбом — оба фото")
        # тот же пост по номеру второй раз — не дублируется; правка настоящего поста потом правит запись
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            vk.backfill("https://t.me/domwcs/460", TAG, "domwcs", fetch, now=NOW + 60)
        self.assertEqual(len(self.vk.wall), 2)
        self.run_vk((post(460, "Сегодня вечеринка до утра!\n#НОВОСТИ", date=NOW - 3600), True), now=NOW + 120)
        self.assertIn("Сегодня вечеринка до утра!\n#НОВОСТИ", [m for m, _ in self.vk.wall.values()])

    def test_video_post_gets_link(self):
        p = post(90, "Соло с Аней! В эту субботу!\n#Новости")
        p["caption"] = p.pop("text"); p["video"] = {"file_id": "v", "thumbnail": {"file_id": "t"}}
        self.run_vk((p, False))
        self.assertEqual(list(self.vk.wall.values()), [("Соло с Аней! В эту субботу!\n#Новости", "https://t.me/domwcs/90")])

    def test_backfill_adds_video_link_to_text_only_record(self):
        # запись ушла без видео (как было до правки) — повторная отправка поста добавляет ссылку
        self.run_vk((post(91, "Соло с Аней!\n#Новости"), False))
        page = ('<div data-post="domwcs/91"><div class="tgme_widget_message_video_wrap"><video class="tgme_widget_message_video"></video></div>'
                '<div class="tgme_widget_message_text js-message_text">Соло с Аней!<br/>#Новости</div><time datetime="2026-09-21T06:00:00+00:00"></time></div>')
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            vk.backfill("91", TAG, "domwcs", lambda url: page.encode(), now=NOW)
        self.assertEqual(list(self.vk.wall.values()), [("Соло с Аней!\n#Новости", "https://t.me/domwcs/91")])

    def test_edit_needs_admin_key(self):
        self.vk.group_admin = False
        self.run_vk((post(95, "Вечеринка в субботу\n#Новости"), False))
        self.run_vk((post(95, "Вечеринка в воскресенье\n#Новости"), True))
        self.assertEqual(list(self.vk.wall.values())[0][0], "Вечеринка в субботу\n#Новости", "ключом сообщества не исправить")
        self.assertEqual(vk.load_state()["queue"], [], "и не повторяем каждые 5 минут")
        os.environ["VK_USER_TOKEN"] = "admin"
        self.run_vk((post(95, "Вечеринка в воскресенье\n#Новости"), True))
        self.assertEqual(list(self.vk.wall.values())[0][0], "Вечеринка в воскресенье\n#Новости", "с ключом администратора — исправлено")
        self.assertIn(("admin", "wall.edit"), self.vk.calls)

    def test_admin_key_pasted_with_url_and_bad_key_not_retried(self):
        self.assertEqual(vk.clean_token(" https://oauth.vk.com/blank.html#access_token=vk1.a.XYZ&expires_in=0&user_id=1 "), "vk1.a.XYZ")
        self.assertEqual(vk.clean_token(" vk1.a.XYZ\n"), "vk1.a.XYZ")
        self.run_vk((post(96, "Вечеринка в субботу\n#Новости"), False))
        os.environ["VK_USER_TOKEN"] = "bad"
        real = self.vk.call
        def call(token, method, params):
            if token == "bad":
                raise vk.VkError(method, {"error_code": 5, "error_msg": "User authorization failed: invalid access_token (4)."})
            return real(token, method, params)
        vk.vk_call = call
        self.run_vk((post(96, "Вечеринка в воскресенье\n#Новости"), True))
        self.assertEqual(vk.load_state()["queue"], [], "плохой ключ — не повторяем каждые 5 минут")

    def test_video_uploaded_with_admin_key(self):
        os.environ["VK_USER_TOKEN"] = "admin"
        p = post(97, "Соло с Аней!\n#Новости")
        p["caption"] = p.pop("text"); p["video"] = {"file_id": "vid1", "file_unique_id": "u1", "file_size": 5_000_000}
        self.run_vk((p, False))
        self.assertEqual(list(self.vk.wall.values()), [("Соло с Аней!\n#Новости", "video-42_501")])
        self.assertEqual(self.vk.uploaded_videos, [b"vid1"])
        self.assertEqual(self.vk.calls[-1], ("admin", "wall.post"), "публикует ключ администратора")
        # правка текста — видео заново не грузится
        p2 = dict(p, caption="Соло с Аней! Старт 10 октября\n#Новости")
        self.run_vk((p2, True))
        self.assertEqual(list(self.vk.wall.values()), [("Соло с Аней! Старт 10 октября\n#Новости", "video-42_501")])
        self.assertEqual(len(self.vk.uploaded_videos), 1)

    def test_big_video_and_photo_album(self):
        os.environ["VK_USER_TOKEN"] = "admin"
        a = post(98, "Вечеринка\n#Новости", photo="ph", group="A")
        b = post(99, group="A"); b["video"] = {"file_id": "big", "file_unique_id": "ub", "file_size": 50_000_000}
        self.run_vk((a, False), (b, False))
        self.assertEqual(list(self.vk.wall.values()), [("Вечеринка\n#Новости", "photo-42_1,https://t.me/domwcs/98")],
                         "видео больше 20 МБ — ссылкой на пост, фото — загружено")

    def test_bad_admin_key_falls_back_to_group_for_new_posts(self):
        os.environ["VK_USER_TOKEN"] = "admin"
        self.vk.bad_admin = True
        self.run_vk((post(100, "Новая запись\n#Новости"), False))
        self.assertEqual(list(self.vk.wall.values()), [("Новая запись\n#Новости", "")])

    def test_only_tagged_posts(self):
        self.run_vk((post(1, "Просто пост"), False), (post(2, "Вечеринка\n#Новости"), False), (post(3, "#расписание", photo="sch"), False))
        msgs = sorted(m for m, _ in self.vk.wall.values())
        self.assertEqual(msgs, ["#расписание", "Вечеринка\n#Новости"])
        self.assertIn("photo-42_1", [a for _, a in self.vk.wall.values()])

    def test_edit_updates_vk_post(self):
        self.run_vk((post(5, "Вечеринка в субботу\n#Новости"), False))
        self.run_vk((post(5, "Вечеринка в воскресенье\n#Новости"), True))
        self.assertEqual(list(self.vk.wall.values()), [("Вечеринка в воскресенье\n#Новости", "")])
        n = len(self.vk.calls)
        self.run_vk((post(5, "Вечеринка в воскресенье\n#Новости"), True))
        self.assertEqual(len(self.vk.calls), n, "та же правка второй раз — ВК не трогаем")

    def test_edit_text_keeps_photo_without_reupload(self):
        self.run_vk((post(6, "Расписание\n#расписание", photo="a"), False))
        self.run_vk((post(6, "Расписание на неделю\n#расписание", photo="a"), True))
        self.assertEqual(list(self.vk.wall.values()), [("Расписание на неделю\n#расписание", "photo-42_1")])
        self.assertEqual(self.vk.photo_seq, 1)

    def test_tag_added_later_and_removed(self):
        self.run_vk((post(7, "Мастер-класс"), False))
        self.assertEqual(self.vk.wall, {})
        self.run_vk((post(7, "Мастер-класс\n#новости"), True))
        self.assertEqual(len(self.vk.wall), 1)
        self.run_vk((post(7, "Мастер-класс"), True))
        self.assertEqual(self.vk.wall, {}, "тег убрали — запись удалена")

    def test_old_post_tag_added_not_published(self):
        self.run_vk((post(8, "Старое\n#новости", date=NOW - 30 * 86400), True))
        self.assertEqual(self.vk.wall, {})

    def test_album_one_post_all_photos(self):
        self.run_vk((post(10, "Фото с вечеринки\n#новости", photo="p1", group="G"), False),
                    (post(11, photo="p2", group="G"), False), (post(12, photo="p3", group="G"), False))
        self.assertEqual(list(self.vk.wall.values()), [("Фото с вечеринки\n#новости", "photo-42_1,photo-42_2,photo-42_3")])
        # исправили подпись — пришло только первое сообщение альбома: фото остаются, текст меняется
        self.run_vk((post(10, "Фото с субботней вечеринки\n#новости", photo="p1", group="G"), True))
        self.assertEqual(list(self.vk.wall.values()), [("Фото с субботней вечеринки\n#новости", "photo-42_1,photo-42_2,photo-42_3")])
        # правка фото без подписи в альбоме — запись не удаляется
        self.run_vk((post(11, photo="p2", group="G"), True))
        self.assertEqual(len(self.vk.wall), 1)

    def test_photo_falls_back_to_admin_key_then_link(self):
        self.vk.group_photos = False
        os.environ["VK_USER_TOKEN"] = "admin"
        self.run_vk((post(20, "#расписание", photo="a"), False))
        self.assertEqual(list(self.vk.wall.values())[0][1], "photo-42_1", "ключ сообщества не смог — загрузил ключ администратора")
        os.environ.pop("VK_USER_TOKEN")
        self.run_vk((post(21, "#расписание", photo="b"), False))
        self.assertEqual(self.vk.wall[102][1], "https://t.me/domwcs/21", "без ключа администратора — ссылка на пост")

    def test_vk_down_retried_next_run(self):
        self.vk.down = True
        self.run_vk((post(30, "Вечеринка\n#новости"), False))
        self.assertEqual(self.vk.wall, {})
        self.vk.down = False
        self.run_vk(now=NOW + 300)
        self.assertEqual(list(self.vk.wall.values()), [("Вечеринка\n#новости", "")])
        self.assertEqual(vk.load_state()["queue"], [])

    def test_queue_expires(self):
        self.vk.down = True
        self.run_vk((post(31, "Вечеринка\n#новости"), False))
        self.vk.down = False
        self.run_vk(now=NOW + 2 * 86400)
        self.assertEqual(self.vk.wall, {})


if __name__ == "__main__":
    unittest.main()
