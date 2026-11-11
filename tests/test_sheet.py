"""拼图功能的离线测试：布局、标签与退化情况。"""
import io
import logging
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("radar_sheet_test")
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg

astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("radar_sheet")
sys.modules["astrbot"] = astrbot
sys.modules["astrbot.api"] = api

sheet = __import__(pkg.__name__ + ".media.sheet", fromlist=["x"])


def jpeg(width: int = 320, height: int = 180, color=(90, 120, 160)) -> bytes:
    from PIL import Image

    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


class ColumnTests(unittest.TestCase):
    def test_single(self):
        self.assertEqual(sheet.columns_for(1), 1)

    def test_two_to_four_use_two_columns(self):
        for count in (2, 3, 4):
            self.assertEqual(sheet.columns_for(count), 2)

    def test_many_use_three(self):
        for count in (5, 6, 7):
            self.assertEqual(sheet.columns_for(count), 3)


class TimeFormatTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(sheet._fmt(0), "00:00")
        self.assertEqual(sheet._fmt(72), "01:12")
        self.assertEqual(sheet._fmt(-5), "00:00")


class ComposeTests(unittest.TestCase):
    def test_produces_jpeg(self):
        data = sheet.compose([(5, jpeg()), (45, jpeg(color=(10, 200, 10)))])
        self.assertTrue(data.startswith(b"\xff\xd8"), "应当返回 JPEG")
        self.assertGreater(len(data), 2000)

    def test_single_frame_works(self):
        data = sheet.compose([(12, jpeg())])
        self.assertTrue(data.startswith(b"\xff\xd8"))

    def test_aspect_is_reasonable(self):
        from PIL import Image

        data = sheet.compose([(i * 10, jpeg()) for i in range(6)])
        with Image.open(io.BytesIO(data)) as image:
            ratio = image.width / image.height
        self.assertGreater(ratio, 0.8)
        self.assertLess(ratio, 3.0)

    def test_frames_carry_labels(self):
        """有标签就说明文字画上去了：纯色图不可能自己产生高亮像素。"""
        from PIL import Image

        data = sheet.compose([(0, jpeg(color=(0, 0, 0)))])
        with Image.open(io.BytesIO(data)) as image:
            raw = image.convert("RGB").tobytes()
        bright = sum(1 for i in range(0, len(raw), 3)
                     if raw[i] > 190 and raw[i + 1] > 170 and raw[i + 2] < 160)
        self.assertGreater(bright, 40, "应出现标签的黄字像素")

    def test_empty_returns_empty(self):
        self.assertEqual(sheet.compose([]), b"")

    def test_all_broken_returns_empty(self):
        self.assertEqual(sheet.compose([(0, b"not an image")]), b"")

    def test_partial_broken_still_composes(self):
        data = sheet.compose([(0, jpeg()), (10, b"broken"), (20, jpeg())])
        self.assertTrue(data.startswith(b"\xff\xd8"))


if __name__ == "__main__":
    unittest.main()
