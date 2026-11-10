"""节奏控制模块的离线测试。"""
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("radar_pacing_test")
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg

pacing = __import__(pkg.__name__ + ".services.pacing", fromlist=["x"])


class PauseTests(unittest.TestCase):
    def test_pause_stays_within_bounds(self):
        plan = pacing.Pacing(planned=3, pause=(20, 90))
        for _ in range(50):
            value = plan.pause_seconds()
            self.assertGreaterEqual(value, 20)
            self.assertLessEqual(value, 90)

    def test_zero_pause_is_allowed(self):
        self.assertEqual(pacing.Pacing(planned=1, pause=(0, 0)).pause_seconds(), 0.0)

    def test_inverted_bounds_are_normalised(self):
        self.assertEqual(pacing.Pacing(planned=1, pause=(90, 20)).pause_seconds(), 90.0)


class StopTests(unittest.TestCase):
    def test_never_stops_before_planned_count(self):
        plan = pacing.Pacing(planned=3, stop_after=1, boredom=0.0)
        self.assertFalse(plan.should_stop(1, {"score": 5}))
        self.assertFalse(plan.should_stop(2, {"score": 5}))
        self.assertTrue(plan.should_stop(3, {"score": 5}))

    def test_low_score_ends_the_round(self):
        plan = pacing.Pacing(planned=5, stop_after=1, boredom=0.0)
        self.assertTrue(plan.should_stop(1, {"score": 2}))

    def test_good_score_continues(self):
        plan = pacing.Pacing(planned=5, stop_after=1, boredom=0.0)
        self.assertFalse(plan.should_stop(1, {"score": 8}))

    def test_boredom_is_probabilistic(self):
        plan = pacing.Pacing(planned=5, stop_after=1, boredom=1.0)
        self.assertTrue(plan.should_stop(1, {"score": 8}))
        plan = pacing.Pacing(planned=5, stop_after=1, boredom=0.0)
        self.assertFalse(plan.should_stop(1, {"score": 8}))

    def test_boredom_rate_is_respected(self):
        plan = pacing.Pacing(planned=5, stop_after=1, boredom=0.25)
        with patch("random.random", return_value=0.20):
            self.assertTrue(plan.should_stop(1, {"score": 8}))
        with patch("random.random", return_value=0.30):
            self.assertFalse(plan.should_stop(1, {"score": 8}))

    def test_missing_verdict_still_allows_boredom(self):
        plan = pacing.Pacing(planned=3, stop_after=1, boredom=1.0)
        self.assertTrue(plan.should_stop(1, None))

    def test_malformed_score_is_treated_as_unknown(self):
        plan = pacing.Pacing(planned=5, stop_after=1, boredom=0.0)
        self.assertFalse(plan.should_stop(1, {"score": "不是数字"}))

    def test_zero_planned_is_treated_as_one(self):
        # planned 配置成 0 时不能变成「一条都不看」：那会让整轮空转。
        plan = pacing.Pacing(planned=0, stop_after=1, boredom=0.0)
        self.assertFalse(plan.should_stop(0, None))
        self.assertTrue(plan.should_stop(1, None))


if __name__ == "__main__":
    unittest.main()
