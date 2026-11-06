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
    def reserve(self, *a, **k):
        return "token", ""
    def finish(self, *a, **k):
        pass


class Plugin:
    def __init__(self, config):
        self.config = config
        self.radar_store = Store()
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


if __name__ == "__main__":
    unittest.main()
