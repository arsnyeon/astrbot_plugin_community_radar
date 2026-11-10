"""Regression tests for the v0.18.0 fixes.

Each test pins one bug that was fixed, so a future refactor cannot quietly
reintroduce it.  Network, browser and model calls are stubbed.
"""
import datetime as dt
import logging
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("radar_regression_plugin")
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg


def module(name, **attrs):
    obj = types.ModuleType(name)
    obj.__dict__.update(attrs)
    sys.modules[name] = obj
    return obj


def deco(*a, **kw):
    return lambda fn: fn


class Filter:
    EventMessageType = types.SimpleNamespace(ALL="all")

    def __getattr__(self, name):
        return deco


class Star:
    def __init__(self, context):
        self.context = context


class Message:
    def __init__(self, value):
        self.value = value


class Image:
    @staticmethod
    def fromURL(url):
        return Message(url)

    @staticmethod
    def fromBytes(data):
        return Message(data)

    @staticmethod
    def fromFileSystem(path):
        return Message(path)


module("astrbot")
module("astrbot.api", logger=logging.getLogger("radar_regression"), AstrBotConfig=dict)
module("astrbot.api.event", AstrMessageEvent=object, filter=Filter())
module("astrbot.api.provider", ProviderRequest=object)
module("astrbot.api.star", Context=object, Star=Star, register=deco, StarTools=None)
module("astrbot.core")
module("astrbot.core.message")
module("astrbot.core.message.components", Image=Image, Plain=Message)
module("astrbot.core.message.message_event_result", MessageChain=Message)
module("astrbot.core.star")
module("astrbot.core.star.filter")
module("astrbot.core.star.filter.command", GreedyStr=str)


class TextPart:
    def __init__(self, text):
        self.text = text
        self._no_save = False

    def mark_as_temp(self):
        self._no_save = True
        return self


module("astrbot.core.agent")
module("astrbot.core.agent.message", TextPart=TextPart)

statefile = __import__(pkg.__name__ + ".core.statefile", fromlist=["x"])
core = __import__(pkg.__name__ + ".core.radar_core", fromlist=["x"])
autopilot = __import__(pkg.__name__ + ".services.autopilot", fromlist=["x"])
douyin_duty_mod = __import__(pkg.__name__ + ".services.douyin_duty", fromlist=["x"])
douyin_browser_mod = __import__(pkg.__name__ + ".browser.douyin_browser", fromlist=["x"])
video = __import__(pkg.__name__ + ".media.video_watch", fromlist=["x"])
link_peek = __import__(pkg.__name__ + ".media.link_peek", fromlist=["x"])
services = __import__(pkg.__name__ + ".services.radar_services", fromlist=["x"])
duty_mod = __import__(pkg.__name__ + ".services.duty", fromlist=["x"])

NS = types.SimpleNamespace


class FakeClient:
    """Stands in for httpx.AsyncClient in the CDP probe."""

    def __init__(self, *args, **kwargs):
        self.is_closed = False
        self.requests = []
        self.status = 200

    async def get(self, url, **kwargs):
        if self.is_closed:
            raise RuntimeError("Cannot send a request, as the client has been closed.")
        self.requests.append(url)
        return NS(status_code=self.status)

    async def aclose(self):
        self.is_closed = True


class BrowserProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_client_is_recreated_after_close(self):
        browser = douyin_browser_mod.DouyinBrowser({"enabled": True})
        with patch.object(douyin_browser_mod.httpx, "AsyncClient", FakeClient):
            self.assertTrue(await browser._cdp_alive())
            client = browser._probe_client
            self.assertIsNotNone(client)

            await browser.close()
            self.assertIsNone(browser._probe_client)
            self.assertTrue(client.is_closed)

            # 修复前这里会一直返回 False：旧 client 已关闭，探测永远失败。
            self.assertTrue(await browser._cdp_alive())
            self.assertFalse(browser._probe_client.is_closed)


class StateFileTests(unittest.TestCase):
    def test_broken_state_is_kept_and_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bili_state.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertLogs("radar_regression", level="WARNING"):
                value, ok = statefile.read_json(path, {})
            self.assertFalse(ok)
            self.assertEqual(value, {})
            self.assertFalse(path.exists())
            self.assertTrue(path.with_name("bili_state.json.broken").exists())

    def test_atomic_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self.assertTrue(statefile.write_json(path, {"count": 3}, indent=1))
            value, ok = statefile.read_json(path, {})
            self.assertTrue(ok)
            self.assertEqual(value, {"count": 3})
            leftovers = list(Path(tmp).glob("*.tmp"))
            self.assertEqual(leftovers, [])


class CircuitBreakerTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_failures_open_the_breaker(self):
        provider = NS(text_chat=AsyncMock(side_effect=RuntimeError("down")))
        plugin = NS(context=NS(get_provider_by_id=lambda pid: provider), _owner_umo=lambda: "")
        ai = core.RadarAI(plugin)
        for _ in range(5):
            self.assertEqual(await ai.ask("hi"), "")
        self.assertGreaterEqual(ai.failures, 5)
        self.assertGreater(ai.retry_at, 0)
        calls = provider.text_chat.await_count
        self.assertEqual(await ai.ask("hi"), "")
        self.assertEqual(provider.text_chat.await_count, calls)


class ChooseEmptySetTests(unittest.IsolatedAsyncioTestCase):
    def _cognition(self, raw):
        store = NS(memory=lambda key: None, preference_text=lambda: "无")
        plugin = NS(
            config={"browse": {}},
            radar_store=store,
            radar_ai=NS(ask=AsyncMock(return_value=raw)),
            duty=NS(_bot_persona=AsyncMock(return_value="人格")),
        )
        return core.Cognition(plugin)

    async def test_explicit_empty_array_means_watch_nothing(self):
        cog = self._cognition("```json\n[]\n```")
        items = [{"title": "a", "_link": "https://www.bilibili.com/video/BV1xx411c7mD"}]
        chosen = await cog.choose(items, {"deep_watch_per_run": 2})
        self.assertEqual(chosen, [])

    async def test_unparsable_output_falls_back_to_watching(self):
        cog = self._cognition("我觉得都不错")
        items = [{"title": "a", "_link": "https://www.bilibili.com/video/BV1xx411c7mD"},
                 {"title": "b", "_link": "https://www.bilibili.com/video/BV1xx411c7mE"}]
        chosen = await cog.choose(items, {"deep_watch_per_run": 2})
        self.assertEqual([x["title"] for x in chosen], ["a", "b"])


class ParseJsonListTests(unittest.TestCase):
    def test_reports_decode_success(self):
        self.assertEqual(core.parse_json_list(""), ([], False))
        self.assertEqual(core.parse_json_list("[]"), ([], True))
        self.assertEqual(core.parse_json_list("[1, 2]"), ([1, 2], True))
        self.assertEqual(core.parse_json_list('{"a": 1}'), ([], False))


class PlatformSwitchTests(unittest.TestCase):
    def test_disabled_douyin_is_not_selected(self):
        enabled = NS(config={"douyin": {"enabled": True}})
        disabled = NS(config={"douyin": {"enabled": False}})
        self.assertIn("douyin", autopilot.Autopilot(enabled)._enabled_platforms({"platforms": "all"}))
        self.assertNotIn("douyin", autopilot.Autopilot(disabled)._enabled_platforms({"platforms": "all"}))
        self.assertEqual(autopilot.Autopilot(disabled)._enabled_platforms({"platforms": "bili"}), ["bili"])


class DouyinPushGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_push_respects_share_switch_and_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            browser = NS(data_dir=tmp)
            plugin = NS(
                config={"share": {"enable": True}, "admin_uin": "1"},
                context=NS(send_message=AsyncMock()),
                radar_store=NS(reserve=lambda *a, **k: (0, "额度用完了"),
                               finish=lambda *a, **k: None),
            )
            duty = douyin_duty_mod.DouyinDuty(plugin, browser, {"targets": ["qq:FriendMessage:1"]})
            self.assertFalse(await duty._send("看过三条"))

            plugin.config["share"]["enable"] = False
            self.assertFalse(await duty._send("看过三条"))
            plugin.context.send_message.assert_not_called()


class WeeklyBackoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_weekly_push_backs_off(self):
        class Sunday(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return dt.datetime(2026, 11, 8, 19, 0)

        values = {"compact_day": dt.date.today().isoformat()}
        store = NS(get=lambda k, d=None: values.get(k, d),
                   set=lambda k, v: values.__setitem__(k, v),
                   compact=lambda: None)
        plugin = NS(config={"activity": {"weekly_push": True}, "browse": {}},
                    cognition=NS(weekly=AsyncMock(return_value="这周的周记")),
                    duty=NS(_send=AsyncMock(return_value=False)),
                    radar_store=store)
        svc = services.RadarServices(plugin)
        with patch.object(services.dt, "datetime", Sunday):
            await svc.tick()
            self.assertEqual(plugin.cognition.weekly.await_count, 1)
            self.assertIn("weekly_retry_at", values)
            await svc.tick()
        self.assertEqual(plugin.cognition.weekly.await_count, 1)


class LinkPeekBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_over_budget_link_is_not_resolved(self):
        watcher = NS(any=AsyncMock())
        plugin = NS(config={"video": {}}, video_watch=watcher, bili=NS())
        peek = link_peek.LinkPeek(plugin)
        text = "看这个 https://www.bilibili.com/video/BV1xx411c7mD"
        with patch.object(link_peek, "TOTAL_PEEK_SECONDS", 0.0):
            out = await peek.peek(text, {"platforms": "all"})
        self.assertIn("B 站视频", out)
        watcher.any.assert_not_called()


class MissingFfmpegTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_ffmpeg_is_reported(self):
        watcher = video.VideoWatch(NS())
        with patch.object(video, "run_media", AsyncMock(side_effect=video.MediaToolMissing("ffmpeg"))):
            result = await watcher._understand(
                title="t", author="a", description="", duration=30, transcript=[],
                sources={"video": "https://example.org/v.mp4"}, cfg={"use_frames": True},
            )
        self.assertTrue(any("ffmpeg" in note for note in result["uncertainties"]))


class ShareDryRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_records_preview_without_sending(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = NS(directory=Path(tmp), data={},
                       set=lambda k, v: store.data.__setitem__(k, v),
                       get=lambda k, d=None: store.data.get(k, d),
                       reserve=lambda *a, **k: (1, ""),
                       finish=lambda *a, **k: None)
            plugin = NS(
                config={"share": {"enable": True, "dry_run": True, "segment": False},
                        "group_share": {"enable": False}, "admin_uin": "1"},
                radar_store=store,
                context=NS(send_message=AsyncMock()),
            )
            browse = duty_mod.BrowseDuty(plugin, tmp)
            sent = await browse._send("准备发的文案", {}, context_items=[{"title": "某条内容"}])
            self.assertFalse(sent)
            plugin.context.send_message.assert_not_called()
            preview = store.data["last_preview"]
            self.assertEqual(preview["parts"], ["准备发的文案"])
            self.assertEqual(preview["items"], ["某条内容"])

    async def test_normal_send_unaffected_when_dry_run_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = NS(directory=Path(tmp), data={},
                       set=lambda k, v: store.data.__setitem__(k, v),
                       get=lambda k, d=None: store.data.get(k, d),
                       reserve=lambda *a, **k: (1, ""),
                       finish=lambda *a, **k: None)
            plugin = NS(
                config={"share": {"enable": True, "segment": False},
                        "group_share": {"enable": False}, "admin_uin": "1"},
                radar_store=store,
                context=NS(send_message=AsyncMock(return_value=True)),
            )
            browse = duty_mod.BrowseDuty(plugin, tmp)
            self.assertTrue(await browse._send("正常发", {}, context_items=[]))
            plugin.context.send_message.assert_awaited()
            self.assertNotIn("last_preview", store.data)


class LastBrowseVisibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_records_why_nothing_was_said(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = NS(directory=Path(tmp), data={},
                       set=lambda k, v: store.data.__setitem__(k, v),
                       get=lambda k, d=None: store.data.get(k, d),
                       reserve=lambda *a, **k: (1, ""),
                       finish=lambda *a, **k: None)
            plugin = NS(
                config={"browse": {}, "share": {"enable": True}, "admin_uin": "1",
                        "douyin": {"enabled": False}},
                radar_store=store,
                context=NS(send_message=AsyncMock(return_value=True)),
                cognition=NS(plan=AsyncMock(return_value={"platforms": [], "focus": "",
                                                          "keywords": [], "skip": False}),
                             choose=AsyncMock(return_value=[])),
                topic_watch=None,
                bili=NS(popular=AsyncMock(return_value=[]), following_updates=AsyncMock(return_value=[])),
                heybox=NS(feed=AsyncMock(return_value=[])),
                video_watch=NS(any=AsyncMock()),
            )
            browse = duty_mod.BrowseDuty(plugin, tmp)
            await browse._run({"count": 1})
            recorded = store.data["last_browse"]
            self.assertIn("reason", recorded)
            self.assertTrue(recorded["reason"].strip(), "没发出去时必须留下原因")


class KeywordRecordTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_records_the_keywords_it_actually_uses(self):
        recorded = []

        class Duty:
            autopilot = autopilot.Autopilot(NS(config={"douyin": {"enabled": False}}))

            @staticmethod
            async def _bot_persona(cfg):
                return "人格"

            @staticmethod
            def _record_keywords(words):
                recorded.append(list(words))

        values = {}
        plugin = NS(
            config={"browse": {}},
            duty=Duty(),            radar_store=NS(get=lambda k, d=None: values.get(k, d),
                           set=lambda k, v: values.__setitem__(k, v),
                           preference_text=lambda: "无",
                           curiosity=lambda limit=6: []),
            radar_ai=NS(ask=AsyncMock(return_value='{"keywords":["独立游戏","显卡降价"]}')),
        )
        cog = core.Cognition(plugin)
        plan = await cog.plan({"platforms": "bili", "search_per_run": 2}, [])
        self.assertEqual(recorded, [plan["keywords"]])
        self.assertIn("独立游戏", plan["keywords"])


class TickFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_reschedule_happens_even_when_run_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            values = {"next_due_at": 0}
            store = NS(directory=Path(tmp), data=values,
                       get=lambda k, d=None: values.get(k, d),
                       set=lambda k, v: values.__setitem__(k, v))
            plugin = NS(
                config={"browse": {"enable": True, "interval_min_hours": 1,
                                   "interval_max_hours": 2, "quiet_hours": "1-6"}},
                radar_store=store,
                services=NS(next_planned=AsyncMock(return_value=None)),
            )
            browse = duty_mod.BrowseDuty(plugin, tmp)
            browse._run = AsyncMock(side_effect=RuntimeError("boom"))
            browse.schedule_now()
            with self.assertRaises(RuntimeError):
                await browse._tick()
            state = browse._load_state()
            self.assertGreater(float(state.get("next_due_at") or 0), 0)
            self.assertFalse(state.get("manual_due"))
            self.assertEqual(store.data["activity"]["stage"], "idle")


if __name__ == "__main__":
    unittest.main()
