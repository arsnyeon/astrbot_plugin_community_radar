"""硬字幕 OCR 的离线测试：只测纯逻辑，不加载模型、不跑 ffmpeg。

OCR 引擎是可选依赖，CI 里不装；因此这里覆盖「去噪、合并、配置收敛」这些
不依赖模型的部分，真正跑 OCR 的行为在实机验证。
"""
import logging
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("radar_ocr_test")
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg

astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("radar_ocr")
sys.modules["astrbot"] = astrbot
sys.modules["astrbot.api"] = api

ocr = __import__(pkg.__name__ + ".media.subtitle_ocr", fromlist=["x"])


class MeaningfulTests(unittest.TestCase):
    def test_strips_ocr_edges(self):
        self.assertEqual(ocr._meaningful("  | 你好世界 ~  "), "你好世界")

    def test_collapses_whitespace(self):
        self.assertEqual(ocr._meaningful("你  好\n世界"), "你 好 世界")

    def test_empty(self):
        self.assertEqual(ocr._meaningful(""), "")


class NoiseTests(unittest.TestCase):
    def test_too_short_is_noise(self):
        self.assertTrue(ocr.is_noise("嗯"))

    def test_punctuation_only_is_noise(self):
        self.assertTrue(ocr.is_noise("。。。。。"))

    def test_repeated_char_is_noise(self):
        self.assertTrue(ocr.is_noise("======="))

    def test_real_subtitle_is_kept(self):
        self.assertFalse(ocr.is_noise("这是我自己做的游戏"))

    def test_mixed_latin_kept(self):
        self.assertFalse(ocr.is_noise("rhodopsin 的作用"))

    def test_garbage_symbols_dropped(self):
        self.assertTrue(ocr.is_noise("¯\\_(ツ)_/¯"))


class MergeTests(unittest.TestCase):
    def test_consecutive_duplicates_collapse(self):
        raw = [(0.0, "第一句"), (1.0, "第一句"), (2.0, "第二句")]
        self.assertEqual(ocr.merge_lines(raw), [(0.0, "第一句"), (2.0, "第二句")])

    def test_noise_lines_are_dropped(self):
        raw = [(0.0, "嗯"), (1.0, "真实的台词"), (2.0, "。。")]
        self.assertEqual(ocr.merge_lines(raw), [(1.0, "真实的台词")])

    def test_long_line_is_truncated(self):
        raw = [(0.0, "这是很长的一句台词" * 20)]
        picked = ocr.merge_lines(raw)
        self.assertEqual(len(picked[0][1]), ocr.MAX_LINE_CHARS)

    def test_single_repeated_char_is_treated_as_noise(self):
        # 进度条读数、水印常见形态
        self.assertEqual(ocr.merge_lines([(0.0, "啊" * 50)]), [])

    def test_empty_input(self):
        self.assertEqual(ocr.merge_lines([]), [])

    def test_time_is_float(self):
        picked = ocr.merge_lines([(3, "一句话")])
        self.assertIsInstance(picked[0][0], float)


class ConfigTests(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertFalse(ocr.configured({})["enabled"])

    def test_configured_reports_values(self):
        info = ocr.configured({"ocr_subtitles": True, "ocr_fps": 2})
        self.assertTrue(info["enabled"])
        self.assertEqual(info["fps"], 2)

    def test_available_is_bool(self):
        self.assertIsInstance(ocr.available(), bool)


if __name__ == "__main__":
    unittest.main()
