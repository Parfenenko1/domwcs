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

    def call(self, token, method, params):
        self.calls.append((token, method))
        if self.down:
            raise OSError("ВК не отвечает")
        if method == "photos.getWallUploadServer":
            if token == "group" and not self.group_photos:
                raise vk.VkError(method, {"error_code": 27, "error_msg": "Group authorization failed"})
            return {"upload_url": "https://up/" + token}
        if method == "photos.saveWallPhoto":
            self.photo_seq += 1
            return [{"owner_id": -42, "id": self.photo_seq}]
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

    def upload(self, url, filename, raw):
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
