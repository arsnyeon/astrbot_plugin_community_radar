"""接话判断的测试：相关性、次数上限与间隔。

这些测试不接触网络，也不调用模型——只验证「该不该开口」这一层。
"""
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("radar_followup_test")
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg

import logging  # noqa: E402

astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("radar_followup")
sys.modules["astrbot"] = astrbot
sys.modules["astrbot.api"] = api

followup = __import__(pkg.__name__ + ".services.followup", fromlist=["x"])


ITEMS = [{
    "id": "bili:BV1xx411c7mD",
    "title": "《银松镇》正式版全收集攻略",
    "digest": "生化危机风格的恐怖游戏，评论区在聊难度和价格",
}]


class TokenTests(unittest.TestCase):
    def test_splits_chinese_into_bigrams(self):
        words = followup.tokens_of("银松镇")
        self.assertIn("银松", words)
        self.assertIn("松镇", words)

    def test_keeps_latin_words(self):
        self.assertIn("bv1xx411", followup.tokens_of("看 BV1xx411 这个"))

    def test_drops_pure_function_pairs(self):
        words = followup.tokens_of("的了吗呢")
        self.assertEqual(words, set())

    def test_empty_input(self):
        self.assertEqual(followup.tokens_of(""), set())


class RelevanceTests(unittest.TestCase):
    def test_matches_title_word(self):
        self.assertTrue(followup.relevant("银松镇那个我也在玩", ITEMS))

    def test_matches_digest_word(self):
        self.assertTrue(followup.relevant("这价格确实劝退", ITEMS))

    def test_unrelated_message_is_ignored(self):
        self.assertFalse(followup.relevant("今天中午吃什么", ITEMS))

    def test_too_short_is_ignored(self):
        self.assertFalse(followup.relevant("哈", ITEMS))

    def test_media_placeholder_is_ignored(self):
        self.assertFalse(followup.relevant("[图片]", ITEMS))

    def test_empty_items(self):
        self.assertFalse(followup.relevant("银松镇", []))

    def test_case_insensitive_latin(self):
        items = [{"title": "Hades II 试玩", "digest": ""}]
        self.assertTrue(followup.relevant("hades ii 好玩", items))


class Store:
    def __init__(self):
        self.kv = {}

    def get(self, key, default=None):
        return self.kv.get(key, default)

    def set(self, key, value):
        self.kv[key] = value


class StateTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.state = followup.FollowUpState(self.store)

    def test_first_reply_allowed(self):
        self.assertTrue(self.state.can_reply("g:GroupMessage:1", now=1000))

    def test_respects_max_replies(self):
        umo = "g:GroupMessage:1"
        self.state.mark(umo, now=1000)
        self.state.mark(umo, now=1200)
        self.assertFalse(self.state.can_reply(umo, max_replies=2, now=1400))

    def test_gap_blocks_immediate_second_reply(self):
        umo = "g:GroupMessage:1"
        self.state.mark(umo, now=1000)
        self.assertFalse(self.state.can_reply(umo, gap=90, now=1030))
        self.assertTrue(self.state.can_reply(umo, gap=90, now=1100))

    def test_sessions_are_independent(self):
        self.state.mark("g:GroupMessage:1", now=1000)
        self.assertTrue(self.state.can_reply("g:GroupMessage:2", now=1001))

    def test_reset_clears_counter(self):
        umo = "g:GroupMessage:1"
        self.state.mark(umo, now=1000)
        self.state.mark(umo, now=1100)
        self.state.reset(umo)
        self.assertTrue(self.state.can_reply(umo, max_replies=2, now=1200))

    def test_reset_unknown_session_is_noop(self):
        self.state.reset("never:GroupMessage:9")
        self.assertEqual(self.store.get(followup.FollowUpState.KEY) or {}, {})


if __name__ == "__main__":
    unittest.main()
