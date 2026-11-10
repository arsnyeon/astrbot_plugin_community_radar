"""快照拼版模块的离线测试：只验证解析、选帧与裁切，不联网。"""
import io
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("radar_story_test")
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg

import logging  # noqa: E402

astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("radar_story")
sys.modules["astrbot"] = astrbot
sys.modules["astrbot.api"] = api

storyboard = __import__(pkg.__name__ + ".media.storyboard", fromlist=["x"])


def make_sheet(width=1600, height=900, grid=(10, 10), color=(120, 120, 120)):
    from PIL import Image, ImageDraw

    sheet = Image.new("RGB", (width, height), color)
    draw = ImageDraw.Draw(sheet)
    cell_w, cell_h = width // grid[0], height // grid[1]
    for index in range(grid[0] * grid[1]):
        row, column = divmod(index, grid[0])
        # 每格画一个不同的方块，便于验证裁切位置
        draw.rectangle(
            [column * cell_w + 4, row * cell_h + 4, column * cell_w + cell_w - 4, row * cell_h + cell_h - 4],
            fill=((index * 7) % 256, (index * 13) % 256, (index * 29) % 256),
        )
    buffer = io.BytesIO()
    sheet.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, content):
        self.content = content
        self.status_code = 200


class FakeClient:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.requests = []

        class Inner:
            def __init__(self, outer):
                self.outer = outer

            async def get(self, url, headers=None, **kwargs):
                self.outer.requests.append(url)
                return FakeResponse(self.outer.payload)

        self._client = Inner(self)

    def _headers(self):
        return {}

    async def _get(self, url, params=None):
        self.requests.append((url, params))
        return {
            "image": ["//example.org/sheet0.jpg"],
            "index": [0, 0, 5, 11, 17],
            "img_x_len": 10,
            "img_y_len": 10,
            "img_x_size": 160,
            "img_y_size": 90,
            "pvdata": "",
        }


class NormalizeIndexTests(unittest.TestCase):
    def test_seconds_are_kept(self):
        self.assertEqual(storyboard.normalize_index([0, 5, 11], duration=60), [0, 5, 11])

    def test_leading_zero_offsets_are_preserved(self):
        # 接口首项恒为 0，其后才是逐帧时间；这里只做规范化，不做裁剪。
        self.assertEqual(storyboard.normalize_index([0, 0, 5, 11], duration=60), [0, 0, 5, 11])

    def test_milliseconds_are_converted(self):
        self.assertEqual(storyboard.normalize_index([0, 5000, 11000], duration=60), [0, 5, 11])

    def test_object_items_are_supported(self):
        raw = [0, {"timestamp": 3000}, {"timestamp": 6000}]
        self.assertEqual(storyboard.normalize_index(raw, duration=20), [0, 3, 6])

    def test_degenerate_input(self):
        self.assertEqual(storyboard.normalize_index(None), [])
        self.assertEqual(storyboard.normalize_index([0]), [])
        self.assertEqual(storyboard.normalize_index(["a", "b"]), [])


class PvdataTests(unittest.TestCase):
    def test_parses_big_endian_uint16(self):
        payload = b"\x00\x05\x00\x0b\x00\x11"
        self.assertEqual(storyboard.parse_pvdata(payload), [5, 11, 17])

    def test_rejects_odd_length_and_empty(self):
        self.assertEqual(storyboard.parse_pvdata(b"\x00"), [])
        self.assertEqual(storyboard.parse_pvdata(b""), [])


class StoryboardHelperTests(unittest.TestCase):
    def board(self, timestamps=(5, 15, 25, 35)):
        return storyboard.Storyboard(
            image_urls=("https://example.org/a.jpg",),
            timestamps=timestamps,
            grid=(10, 10),
            frame_size=(160, 90),
            duration=40,
        )

    def test_covered_until_and_usable(self):
        board = self.board()
        self.assertEqual(board.covered_until, 35)
        self.assertTrue(board.usable)

    def test_empty_board_is_not_usable(self):
        board = storyboard.Storyboard(image_urls=(), timestamps=())
        self.assertFalse(board.usable)
        self.assertEqual(board.covered_until, 0)


class SelectFramesTests(unittest.TestCase):
    def board(self, count=100, step=5):
        return storyboard.Storyboard(
            image_urls=("https://example.org/a.jpg",),
            timestamps=tuple(range(step, step * count + 1, step)),
            duration=step * count,
        )

    def test_respects_count(self):
        picks = storyboard.select_frames(self.board(), 6)
        self.assertEqual(len(picks), 6)

    def test_covers_beginning_and_end(self):
        board = self.board()
        picks = storyboard.select_frames(board, 5)
        self.assertLessEqual(picks[0][1], 20)
        self.assertGreaterEqual(picks[-1][1], board.covered_until - 20)

    def test_range_is_honoured(self):
        picks = storyboard.select_frames(self.board(), 4, start=100, end=200)
        self.assertTrue(picks)
        self.assertTrue(all(100 <= seconds <= 200 for _, seconds in picks))

    def test_empty_range_returns_nothing(self):
        self.assertEqual(storyboard.select_frames(self.board(), 4, start=10_000), [])

    def test_subtitles_do_not_starve_the_tail(self):
        board = self.board(count=60, step=1)
        cues = [{"start": 0, "end": 8}, {"start": 10, "end": 18}]
        picks = storyboard.select_frames(board, 6, cues=cues)
        self.assertEqual(len(picks), 6)
        # 字幕全挤在开头时，后面仍须有画面覆盖
        self.assertGreater(max(seconds for _, seconds in picks), 30)

    def test_more_frames_than_available(self):
        board = storyboard.Storyboard(image_urls=("u",), timestamps=(5, 10))
        self.assertEqual(len(storyboard.select_frames(board, 10)), 2)


class CropTests(unittest.IsolatedAsyncioTestCase):
    async def test_crops_requested_cells(self):
        payload = make_sheet()
        client = FakeClient(payload)
        board = storyboard.Storyboard(
            image_urls=("https://example.org/sheet.jpg",),
            timestamps=tuple(range(1, 101)),
            grid=(10, 10),
            frame_size=(160, 90),
            duration=100,
        )
        frames = await storyboard.crop(client, board, [(0, 1), (5, 6), (99, 100)],
                                       avoid_blank=False, avoid_similar=False)
        self.assertEqual(len(frames), 3)
        from PIL import Image

        for seconds, data in frames:
            image = Image.open(io.BytesIO(data))
            self.assertEqual(image.size, (160, 90))
        # 只下载用到的拼版，而不是每帧一次
        self.assertEqual(len(client.requests), 1)

    async def test_blank_frames_are_skipped_when_neighbours_exist(self):
        from PIL import Image

        sheet = Image.new("RGB", (1600, 900), (0, 0, 0))
        draw = __import__("PIL.ImageDraw", fromlist=["x"]).Draw(sheet)
        # 第 1 格留黑，第 2 格画满
        draw.rectangle([164, 4, 316, 86], fill=(200, 100, 50))
        buffer = io.BytesIO()
        sheet.save(buffer, format="JPEG", quality=95)

        client = FakeClient(buffer.getvalue())
        board = storyboard.Storyboard(
            image_urls=("https://example.org/sheet.jpg",),
            timestamps=tuple(range(1, 101)),
            grid=(10, 10),
            frame_size=(160, 90),
            duration=100,
        )
        frames = await storyboard.crop(client, board, [(0, 1)], avoid_blank=True, avoid_similar=False)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0][0], 2)

    async def test_missing_sheet_yields_nothing(self):
        class NoSheet(FakeClient):
            """接口有响应但没给拼版图。"""

            async def _get(self, url, params=None):
                return {"image": [], "index": [0, 5, 10]}

        self.assertIsNone(await storyboard.fetch(NoSheet(make_sheet()), "BV1xx411c7mD"))

    async def test_sheet_download_failure_yields_no_frames(self):
        class Unreachable(FakeClient):
            class Inner:
                def __init__(self, outer):
                    self.outer = outer

                async def get(self, url, headers=None, **kwargs):
                    raise OSError("network down")

            def __init__(self, payload):
                super().__init__(payload)
                self._client = Unreachable.Inner(self)

        board = storyboard.Storyboard(image_urls=("u",), timestamps=(1,))
        self.assertEqual(await storyboard.crop(Unreachable(make_sheet()), board, [(0, 1)]), [])

    async def test_fetch_builds_board_from_api(self):
        board = await storyboard.fetch(FakeClient(make_sheet()), "BV1xx411c7mD", duration=100)
        self.assertIsNotNone(board)
        self.assertEqual(board.grid, (10, 10))
        self.assertEqual(board.frame_size, (160, 90))
        self.assertEqual(board.covered_until, 17)
        self.assertTrue(board.image_urls[0].startswith("https://"))


if __name__ == "__main__":
    unittest.main()
