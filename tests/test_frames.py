"""按画面差异选帧的测试。

核心诉求：多抽的候选里，挑出来的几张必须「彼此不一样」，同时保住开头与结尾。
"""
import io
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("radar_frames_test")
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg

astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("radar_frames")
sys.modules["astrbot"] = astrbot
sys.modules["astrbot.api"] = api

frames = __import__(pkg.__name__ + ".media.frames", fromlist=["x"])


def solid(color: int, side: int = 8) -> bytes:
    """生成一张纯色 PNG 字节，用来模拟「同一镜头」与「换镜头」。"""
    from PIL import Image

    image = Image.new("L", (side * 4, side * 4), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class SignatureTests(unittest.TestCase):
    def test_signature_length_and_uniformity(self):
        sig = frames.signature(solid(120))
        self.assertEqual(len(sig), frames.SIGNATURE_SIDE ** 2)
        self.assertTrue(all(abs(v - 120) <= 2 for v in sig))

    def test_bad_image_returns_empty(self):
        self.assertEqual(frames.signature(b"not an image"), ())

    def test_distance_zero_for_same(self):
        self.assertEqual(frames.distance(frames.signature(solid(50)),
                                         frames.signature(solid(50))), 0.0)

    def test_distance_large_for_different(self):
        self.assertGreater(frames.distance(frames.signature(solid(0)),
                                           frames.signature(solid(255))), 0.9)

    def test_distance_mismatched_lengths(self):
        self.assertEqual(frames.distance((1, 2), (1, 2, 3)), 1.0)


class PickTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="radar_frames_"))
        self.counter = 0

    def add(self, seconds, color):
        self.counter += 1
        path = self.folder / f"f{self.counter}.png"
        path.write_bytes(solid(color))
        return (seconds, str(path))

    def test_keeps_all_when_under_limit(self):
        samples = [self.add(1, 10), self.add(2, 20)]
        self.assertEqual(len(frames.pick_distinct(samples, 5)), 2)

    def test_keeps_first_and_last(self):
        samples = [self.add(i, i * 20 % 256) for i in range(1, 9)]
        picked = frames.pick_distinct(samples, 2)
        self.assertEqual([t for t, _ in picked], [1.0, 8.0])

    def test_drops_near_duplicates_in_favour_of_change(self):
        # 前七帧几乎一样，第八帧彻底不同
        samples = [self.add(i, 30) for i in range(1, 8)]
        samples.append(self.add(8, 240))
        picked = frames.pick_distinct(samples, 3, min_distance=0.05)
        times = [t for t, _ in picked]
        self.assertIn(1.0, times)
        self.assertIn(8.0, times)
        self.assertEqual(times, sorted(times))

    def test_output_is_time_sorted(self):
        samples = [self.add(5, 200), self.add(1, 10), self.add(9, 90)]
        picked = frames.pick_distinct(samples, 3)
        self.assertEqual([t for t, _ in picked], sorted(t for t, _ in picked))

    def test_blank_paths_are_ignored(self):
        samples = [(1.0, ""), self.add(2, 10), (3.0, "")]
        picked = frames.pick_distinct(samples, 3)
        self.assertTrue(all(p for _, p in picked))

    def test_describe(self):
        self.assertEqual(frames.describe([(12.4, "a"), (45.9, "b")]), "12s/45s")


if __name__ == "__main__":
    unittest.main()
