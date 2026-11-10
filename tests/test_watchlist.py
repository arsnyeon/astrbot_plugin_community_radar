"""「想刷列表」的测试：链接、口头提到的作品名、去重与按名字找回来。"""
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("radar_watchlist_test")
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg

import logging  # noqa: E402

astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("radar_watchlist")
sys.modules["astrbot"] = astrbot
sys.modules["astrbot.api"] = api

watch_mod = __import__(pkg.__name__ + ".services.watchlist", fromlist=["x"])
statefile = __import__(pkg.__name__ + ".core.statefile", fromlist=["x"])
watch_mod.read_json = statefile.read_json
watch_mod.write_json = statefile.write_json


class ExtractTests(unittest.TestCase):
    def test_bilibili_url(self):
        found = watch_mod.extract_links("看这个 https://www.bilibili.com/video/BV1xx411c7mD 挺逗")
        self.assertEqual(found[0]["platform"], "bili")
        self.assertEqual(found[0]["native_id"], "BV1xx411c7mD")

    def test_bare_bv_number(self):
        found = watch_mod.extract_links("BV1xx411c7mD 这个不错")
        self.assertEqual(found[0]["platform"], "bili")
        self.assertTrue(found[0]["url"].startswith("https://"))

    def test_short_link_and_tieba_and_heybox(self):
        text = "https://b23.tv/abc123 还有 https://tieba.baidu.com/p/123456 和 https://www.xiaoheihe.cn/app/bbs/link/999"
        platforms = [item["platform"] for item in watch_mod.extract_links(text)]
        self.assertEqual(platforms.count("bili"), 1)
        self.assertIn("tieba", platforms)
        self.assertIn("heybox", platforms)

    def test_no_duplicate_same_video(self):
        text = "https://www.bilibili.com/video/BV1xx411c7mD 就是 BV1xx411c7mD"
        self.assertEqual(len(watch_mod.extract_links(text)), 1)

    def test_plain_text_has_no_links(self):
        self.assertEqual(watch_mod.extract_links("今天中午吃什么好呢"), [])


class WatchListTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="radar_wl_"))
        self.board = watch_mod.WatchList(self.dir)

    def test_add_and_size(self):
        self.assertTrue(self.board.add("https://www.bilibili.com/video/BV1xx411c7mD"))
        self.assertEqual(self.board.size(), 1)

    def test_duplicate_add_returns_false(self):
        url = "https://www.bilibili.com/video/BV1xx411c7mD"
        self.board.add(url)
        self.assertFalse(self.board.add(url))
        self.assertEqual(self.board.size(), 1)

    def test_title_only_entry_is_allowed(self):
        self.assertTrue(self.board.add("", title="银松镇"))
        self.assertEqual(self.board.size(), 1)

    def test_title_dedupes_by_name(self):
        self.board.add("", title="银松镇")
        self.assertFalse(self.board.add("", title="银松镇"))

    def test_empty_both_is_rejected(self):
        self.assertFalse(self.board.add("", title=""))

    def test_add_from_text_counts_new(self):
        added = self.board.add_from_text("https://b23.tv/a1 https://tieba.baidu.com/p/2")
        self.assertEqual(added, 2)
        self.assertEqual(self.board.size(), 2)

    def test_take_removes_items(self):
        self.board.add("https://www.bilibili.com/video/BV1xx411c7mD")
        taken = self.board.take(1)
        self.assertEqual(len(taken), 1)
        self.assertEqual(self.board.size(), 0)

    def test_take_empty_returns_empty(self):
        self.assertEqual(self.board.take(1), [])

    def test_brief_shows_title_and_source(self):
        self.board.add("", title="某部新番", from_user="群友")
        self.assertIn("某部新番", self.board.brief())

    def test_brief_when_empty(self):
        self.assertIn("空", self.board.brief())

    def test_clear(self):
        self.board.add("", title="x")
        self.board.clear()
        self.assertEqual(self.board.size(), 0)

    def test_expired_entries_are_dropped(self):
        import json
        import time

        self.board.path.write_text(json.dumps([
            {"url": "https://www.bilibili.com/video/BV1xx411c7mD",
             "added_at": time.time() - 8 * 86400},
        ]), encoding="utf-8")
        self.assertEqual(self.board.size(), 0)


if __name__ == "__main__":
    unittest.main()
