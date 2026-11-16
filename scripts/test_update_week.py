#!/usr/bin/env python3
"""Проверка разбора данных генератора картинки (weeks/*.json) без Tesseract: python3 scripts/test_update_week.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_week as u


class SourceWeek(unittest.TestCase):
    def test_open_lesson_keeps_new_group(self):
        src = {"days": [{"slots": [{"kind": "class", "time": "20.30", "tag": "Открытое занятие",
                "title": "Аня и Оля", "sub": "(1,5 часа, группа с нуля)"}]}]}
        week = u.to_week_json(u.days_from_source(src), u.dt.date(2026, 11, 16), 'test')
        group = u.update_groups(week, None)['groups'][0]
        self.assertEqual((group['time'], group['who'], group['label'], group['new']),
                         ('20:30', 'Аня и Оля', 'Группа с нуля', True))

    def test_event_time_range_uses_start(self):
        for time_range in ("18:00–22:00", "18.00-22.00", "18:00 — 22:00"):
            with self.subTest(time_range=time_range):
                src = {"days": [{"slots": [{"kind": "free", "lines": ["Квартирник", time_range]}]}]}
                item = u.days_from_source(src)[0][0]
                self.assertEqual(item["time"], "18:00")
                self.assertEqual(item["label"], "Квартирник")

    def test_slots(self):
        src = {"days": [{"slots": [
            {"kind": "sampo"},
            {"kind": "class", "time": "19.00", "title": "Саша и Лена", "sub": "(1,5 часа, старшая группа)"},
            {"kind": "class", "time": "12.00", "title": "Даша и Вова", "sub": "(3&nbsp;часа, группа с&nbsp;нуля)"},
            {"kind": "class", "time": "19.00", "title": "WCS Switch", "sub": "Дарья Комкина · (1,5 часа)"},
            {"kind": "class", "time": "16.00", "tag": "Коллективка", "title": "Новис-коллективка под&nbsp;присмотром",
             "sub": "Лена Логашина · (2&nbsp;часа)"},
            {"kind": "note", "time": "19.30", "title": "Закрыт<br>на мероприятие", "foot": "bunting"},
            {"kind": "note", "time": "19.00", "title": "Вечеринка<br>в Доме", "foot": "disco"},
        ]}]}
        sampo, older, zero, switch, coll, closed, party = u.days_from_source(src)[0]
        self.assertEqual(sampo["kind"], "sampo")
        self.assertEqual((older["label"], older["who"], older["time"]), ("Старшая группа", "Саша и Лена", "19:00"))
        self.assertEqual((zero["label"], zero["who"], zero.get("new")), ("Группа с нуля", "Даша и Вова", True))
        self.assertEqual((switch["label"], switch["who"]), ("WCS Switch", "Даша"))
        self.assertEqual((coll["tag"], coll["who"], coll["title"]),
                         ("Коллективка", "Лена Логашина", "Новис-коллективка под присмотром"))
        self.assertEqual(closed["label"], "Закрыт на мероприятие")
        self.assertTrue(party.get("party"))


if __name__ == "__main__":
    unittest.main()
