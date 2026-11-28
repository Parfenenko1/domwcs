import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render_static as rs  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RenderStaticTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as f:
            self.index = f.read()

    def test_idempotent(self):
        once = rs.render(self.index)
        self.assertEqual(once, rs.render(once))

    def test_week_has_times_and_teachers(self):
        out = rs.render(self.index)
        block = out.split("static-week:start")[1].split("static-week:end")[0]
        self.assertEqual(block.count("<li>"), 7)
        self.assertIn('<span class="tm">19:00</span>', block)
        self.assertIn("Сампо до 19:30", block)
        self.assertIn("Вечеринка в Доме", block)

    def test_ld_json_valid(self):
        out = rs.render(self.index)
        raw = out.split('id="ld-schedule">')[1].split("</script>")[0].replace("<\\/", "</")
        data = json.loads(raw)
        types = [x["@type"] for x in data["@graph"]]
        self.assertIn("Course", types)
        self.assertEqual(types.count("Event"), 1)
        names = [p["name"] for c in data["@graph"] if c["@type"] == "Course"
                 for p in c["hasCourseInstance"].get("instructor", [])]
        self.assertIn("Анна Данилова", names)

    def test_escapes_html(self):
        days = rs.regular_week([{"day": 0, "time": "19:00", "label": "<b>x</b>", "who": "a&b"}])
        html = rs.week_html(days)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)
        self.assertIn("a&amp;b", html)

    def test_missing_markers(self):
        with self.assertRaises(SystemExit):
            rs.replace_between("<html></html>", "static-week", "x")


if __name__ == "__main__":
    unittest.main()
