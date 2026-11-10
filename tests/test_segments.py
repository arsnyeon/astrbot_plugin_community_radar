"""Segmented delivery tests; no network or platform calls."""
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("radar_seg_test")
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg

def module(name, **attrs):
    obj = types.ModuleType(name)
    obj.__dict__.update(attrs)
    sys.modules[name] = obj
    return obj

def deco(*a, **kw):
    return lambda fn: fn

module("astrbot")
module("astrbot.api", logger=__import__("logging").getLogger("test"), AstrBotConfig=dict)
module("astrbot.core")
module("astrbot.core.message")
class Image:
    @staticmethod
    def fromURL(url):
        return ("image", url)

    @staticmethod
    def fromBytes(data):
        return ("image", data)
class Plain:
    def __init__(self, text):
        self.text = text
module("astrbot.core.message.components", Image=Image, Plain=Plain)
class Chain:
    def __init__(self, parts):
        self.parts = parts
module("astrbot.core.message.message_event_result", MessageChain=Chain)
module("astrbot.api.event", AstrMessageEvent=object, filter=deco)
module("astrbot.api.provider", ProviderRequest=object)

from radar_seg_test.services.duty import BrowseDuty


class Store:
    def __init__(self, directory=None):
        self.directory = directory

    def reserve(self, *a, **k):
        return "token", ""

    def finish(self, *a, **k):
        pass


class Plugin:
    def __init__(self, config, directory=None):
        self.config = config
        self.radar_store = Store(directory)
        self.sent = []

        async def send(target, chain):
            self.sent.append((target, chain.parts))
            return True

        self.context = types.SimpleNamespace(send_message=send)


class Segments(unittest.TestCase):
    def test_split_and_cap(self):
        self.assertEqual(len(BrowseDuty._split_chat("一。二！三？")), 3)
        self.assertEqual(len(BrowseDuty._clean_parts(["a", "b", "c", "d"], 3)), 3)

    def test_send_parts_and_image_last(self):
        plugin = Plugin({"share": {"enable": True, "segment": True, "segment_delay": 0}})
        duty = BrowseDuty(plugin)
        cfg = {"targets": ["x:FriendMessage:1"]}
        import asyncio
        ok = asyncio.run(duty._send(["第一句", "第二句"], cfg, image_url="https://x/y.jpg"))
        self.assertTrue(ok)
        self.assertEqual(len(plugin.sent), 2)
        self.assertEqual(plugin.sent[0][1][0].text, "第一句")
        self.assertEqual(plugin.sent[1][1][-1][0], "image")

    def test_segment_disabled_single(self):
        plugin = Plugin({"share": {"enable": True, "segment": False}})
        duty = BrowseDuty(plugin)
        cfg = {"targets": ["x:FriendMessage:1"]}
        import asyncio
        asyncio.run(duty._send(["a", "b"], cfg))
        self.assertEqual(len(plugin.sent), 1)


class SpreadMediaTests(unittest.TestCase):
    """画面应分散到不同消息里，而不是全部堆在最后一条。"""

    def test_no_media(self):
        self.assertEqual(BrowseDuty._spread_media(["a", "b"], []), [[], []])

    def test_single_image_goes_before_the_last_bubble(self):
        buckets = BrowseDuty._spread_media(["a", "b", "c"], [{"id": 1}])
        self.assertEqual([len(b) for b in buckets], [0, 1, 0])

    def test_images_spread_across_bubbles(self):
        media = [{"id": 1}, {"id": 2}]
        buckets = BrowseDuty._spread_media(["a", "b", "c"], media)
        self.assertEqual([len(b) for b in buckets], [1, 1, 0])

    def test_overflow_images_stay_in_last_bubble(self):
        media = [{"id": i} for i in range(4)]
        buckets = BrowseDuty._spread_media(["a", "b"], media)
        self.assertEqual(sum(len(b) for b in buckets), 4)
        self.assertGreaterEqual(len(buckets[-1]), 1)

    def test_single_bubble_takes_all(self):
        buckets = BrowseDuty._spread_media(["only"], [{"id": 1}, {"id": 2}])
        self.assertEqual(len(buckets), 1)
        self.assertEqual(len(buckets[0]), 2)

    def test_many_images_are_balanced(self):
        media = [{"id": i} for i in range(6)]
        buckets = BrowseDuty._spread_media(["a", "b", "c"], media)
        self.assertEqual([len(b) for b in buckets], [2, 2, 2])
        self.assertEqual(sum(len(b) for b in buckets), 6)

    def test_four_images_over_three_bubbles_never_exceed_two(self):
        buckets = BrowseDuty._spread_media(["a", "b", "c"], [{"id": i} for i in range(4)])
        self.assertEqual(sum(len(b) for b in buckets), 4)
        self.assertLessEqual(max(len(b) for b in buckets), 2)


class MediaDeliveryTests(unittest.TestCase):
    def test_images_are_sent_in_separate_bubbles(self):
        import asyncio
        import tempfile

        from radar_seg_test.media.media_cache import MediaCache

        directory = Path(tempfile.mkdtemp(prefix="radar_seg_"))
        cache = MediaCache(directory)
        first = cache.put(b"\xff\xd8fake-jpeg-1", kind="frame", timestamp=5)
        second = cache.put(b"\xff\xd8fake-jpeg-2", kind="frame", timestamp=9)

        plugin = Plugin({"share": {"enable": True, "segment": True, "segment_delay": 0}},
                        directory=str(directory))
        duty = BrowseDuty(plugin)
        cfg = {"targets": ["x:FriendMessage:1"]}
        asyncio.run(duty._send(["第一句", "第二句", "第三句"], cfg,
                               selected_media=[first, second]))
        self.assertEqual(len(plugin.sent), 3)
        image_counts = [len([p for p in chain if isinstance(p, tuple)]) for _, chain in plugin.sent]
        self.assertEqual(sum(image_counts), 2, "两张图应分别发出")
        self.assertEqual(max(image_counts), 1, "同一条消息不应堆多张图")


if __name__ == "__main__":
    unittest.main()
