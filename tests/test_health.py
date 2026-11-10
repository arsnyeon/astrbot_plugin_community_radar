"""平台健康记录与「被挡住时提醒」的测试。

提醒文案必须由模型按人格生成，这里只验证触发条件与冷却，不校验具体措辞。
"""
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("radar_health_test")
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg

import logging  # noqa: E402

astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("radar_health")
sys.modules["astrbot"] = astrbot
sys.modules["astrbot.api"] = api

health_mod = __import__(pkg.__name__ + ".core.health", fromlist=["x"])


class Store:
    def __init__(self):
        self.kv = {}

    def get(self, key, default=None):
        return self.kv.get(key, default)

    def set(self, key, value):
        self.kv[key] = value


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.health = health_mod.PlatformHealth(self.store)

    def test_single_failure_is_not_an_alert(self):
        self.health.note("heybox", False, "验证码")
        self.assertEqual(self.health.blocked(), [])

    def test_repeated_failures_trigger_alert(self):
        for _ in range(health_mod.BLOCK_THRESHOLD):
            self.health.note("heybox", False, "验证码")
        blocked = self.health.blocked()
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["platform"], "heybox")
        self.assertIn("验证码", blocked[0]["reason"])

    def test_success_resets_counter(self):
        for _ in range(health_mod.BLOCK_THRESHOLD):
            self.health.note("heybox", False, "验证码")
        self.health.note("heybox", True)
        self.assertEqual(self.health.blocked(), [])
        self.assertEqual(self.health.summary(), {})

    def test_cooldown_suppresses_repeat_alert(self):
        for _ in range(health_mod.BLOCK_THRESHOLD):
            self.health.note("heybox", False, "验证码")
        self.health.mark_notified(["heybox"])
        self.assertEqual(self.health.blocked(), [])

    def test_platforms_are_tracked_independently(self):
        for _ in range(health_mod.BLOCK_THRESHOLD):
            self.health.note("bili", False, "超时")
        self.health.note("heybox", False, "验证码")
        blocked = self.health.blocked()
        self.assertEqual([item["platform"] for item in blocked], ["bili"])

    def test_empty_platform_is_ignored(self):
        self.health.note("", False, "x")
        self.assertEqual(self.store.get("platform_health", {}), {})

    def test_summary_only_lists_failing_platforms(self):
        self.health.note("bili", True)
        self.health.note("heybox", False, "验证码")
        summary = self.health.summary()
        self.assertIn("heybox", summary)
        self.assertNotIn("bili", summary)


if __name__ == "__main__":
    unittest.main()
