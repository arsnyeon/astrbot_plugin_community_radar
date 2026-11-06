"""Offline regression tests. Real module imports; network/model/platform calls are stubbed."""
import asyncio
import importlib
import json
import logging
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("radar_test_plugin")
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

module("astrbot")
module("astrbot.api", logger=logging.getLogger("test"), AstrBotConfig=dict)
module("astrbot.api.event", AstrMessageEvent=object, filter=Filter())
module("astrbot.api.provider", ProviderRequest=object)
module("astrbot.api.star", Context=object, Star=Star, register=deco)
module("astrbot.core")
module("astrbot.core.message")
module("astrbot.core.message.components", Image=Image, Plain=Message)
module("astrbot.core.message.message_event_result", MessageChain=Message)
module("astrbot.core.star")
module("astrbot.core.star.filter")
module("astrbot.core.star.filter.command", GreedyStr=str)

core = importlib.import_module("radar_test_plugin.radar_core")
video = importlib.import_module("radar_test_plugin.video_watch")
duty = importlib.import_module("radar_test_plugin.duty")
bili = importlib.import_module("radar_test_plugin.bili")
bili_duty = importlib.import_module("radar_test_plugin.bili_duty")
douyin = importlib.import_module("radar_test_plugin.douyin_duty")
autopilot = importlib.import_module("radar_test_plugin.autopilot")
services = importlib.import_module("radar_test_plugin.radar_services")
main = importlib.import_module("radar_test_plugin.main")
peek = importlib.import_module("radar_test_plugin.link_peek")
NS = types.SimpleNamespace

def evidence(key="bili:BV1234567890"):
    return dict(content_id=key, title="样例", url="https://www.bilibili.com/video/BV1234567890",
                ok=True, status="partial", evidence=["frames"], digest="实际画面：机器人走到终点")

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = core.RadarStore(Path(self.tmp.name))
    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_metadata_is_not_watched(self):
        self.assertFalse(core.verified(dict(ok=True, status="metadata_only", digest="title")))

    def test_metadata_cannot_replace_evidence(self):
        self.store.remember(evidence())
        self.store.remember({**evidence(), "status": "metadata_only", "digest": "new title"})
        self.assertTrue(core.verified(self.store.memory(evidence()["content_id"])["result"]))

    def test_memory_and_literal_search(self):
        result = evidence()
        result["title"] = "游戏100%胜率_测试"
        self.store.remember(result)
        self.assertEqual(len(self.store.search("100%")), 1)
        self.assertEqual(len(self.store.search("不存在")), 0)
        self.assertEqual(len(self.store.search("_")), 1)

    def test_unknown_blocks_duplicate(self):
        token, reason = self.store.reserve("comment", "account", "one", 2, 0)
        self.store.finish(token, "unknown")
        self.assertEqual(self.store.reserve("comment", "account", "one", 2, 0)[0], 0)

    def test_zero_budget_disables(self):
        self.assertEqual(self.store.reserve("like", "account", "one", 0, 0)[0], 0)

    def test_failed_attempt_allows_retry(self):
        token, _ = self.store.reserve("like", "account", "one", 1, 0)
        self.store.finish(token, "failed")
        self.assertGreater(self.store.reserve("like", "account", "one", 1, 0)[0], 0)

    def test_evidence_does_not_duplicate_preferences(self):
        self.store.remember(evidence())
        evaluation = {"signals": [{"value": "解谜", "polarity": "like", "strength": .8, "evidence": "谜题设计"}]}
        self.store.evaluate(evidence()["content_id"], evaluation)
        self.store.evaluate(evidence()["content_id"], evaluation)
        self.assertEqual(self.store.preferences()[0]["count"], 1)

    def test_compaction_preserves_id_and_status(self):
        self.store.remember({**evidence(), "digest": "a" * 1000})
        self.store.db.execute("UPDATE memory SET updated=?", (time.time()-100*86400,))
        self.store.db.commit()
        self.store.compact()
        row = self.store.memory(evidence()["content_id"])
        self.assertTrue(row["result"]["compacted"])
        self.assertEqual(row["result"]["status"], "partial")
        self.assertLessEqual(len(row["result"]["digest"]), 600)

    def test_inspection_is_separate(self):
        self.store.inspection("question", evidence())
        self.assertEqual(self.store.inspection("question")["digest"], evidence()["digest"])
        self.assertIsNone(self.store.memory(evidence()["content_id"]))

class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.store = core.RadarStore(self.path)
        self.plugin = NS(
            config={"video": {"enable": True, "use_frames": True}, "browse": {}, "bili": {"caps": {"like": 3, "comment": 2}},
                    "share": {"enable": True}, "admin_uin": "123"},
            radar_store=self.store, radar_ai=NS(ask=AsyncMock(return_value="")),
            browser_lock=asyncio.Lock(), _owner_umo=lambda: "qq:FriendMessage:123",
            _platform_name=lambda: "qq",
            services=NS(next_planned=AsyncMock(return_value=None)),
            context=NS(send_message=AsyncMock(return_value=True)),
        )
        self.dir_patch = patch.object(duty.BrowseDuty, "_data_dir", return_value=self.path)
        self.dir_patch.start()
        self.plugin.duty = duty.BrowseDuty(self.plugin)
        self.plugin.duty._bot_persona = AsyncMock(return_value="测试人格")
        self.plugin.cognition = core.Cognition(self.plugin)
        self.plugin.video_watch = video.VideoWatch(self.plugin)
    async def asyncTearDown(self):
        self.dir_patch.stop()
        self.store.close()
        self.tmp.cleanup()

    async def test_manual_browse_when_automatic_disabled(self):
        self.plugin.duty._run = AsyncMock()
        self.plugin.config["browse"] = {"enable": False}
        self.plugin.duty.schedule_now()
        await self.plugin.duty._tick()
        self.plugin.duty._run.assert_awaited_once()
        self.assertTrue(self.plugin.duty._run.call_args.args[0]["_manual"])

    async def test_manual_request_during_run_is_preserved(self):
        async def run(cfg):
            self.plugin.duty.schedule_now()
        self.plugin.duty._run = run
        self.plugin.duty.schedule_now()
        await self.plugin.duty._tick()
        self.assertTrue(self.plugin.duty._load_state()["manual_due"])

    async def test_zero_group_probability(self):
        with patch("random.random", return_value=.01):
            self.assertFalse(autopilot.Autopilot(self.plugin).wants_group({}, {"enable": True, "sessions": ["123"], "probability": 0}))

    async def test_platform_focus_actually_limits_fetch(self):
        plan = autopilot.Autopilot(self.plugin).plan({"platforms": "all"})
        self.assertEqual(len(plan["platforms"]), 1)

    async def test_no_subscription_to_disabled_platform(self):
        plan = autopilot.Autopilot(self.plugin).plan({"platforms": "heybox"}, topics=[{"platform": "bili", "keyword": "游戏"}])
        self.assertEqual(plan["platforms"], ["heybox"])
        self.assertNotEqual(plan["focus"], "bili")

    async def test_search_candidates_precede_popular(self):
        client = NS(search=AsyncMock(return_value=[{"bvid": "new", "title": "result"}]))
        out = await duty.BrowseDuty._search(client, "search", ["topic"], [{"bvid": "popular"}], 5)
        self.assertEqual(out[0]["bvid"], "new")
        self.assertEqual(out[0]["_query"], "topic")

    async def test_failed_topic_not_consumed(self):
        tw = NS(flush=AsyncMock(), queue=lambda n: [{"keyword": "topic"}], consume=lambda words: self.assertEqual(words, []))
        self.plugin.topic_watch = tw
        self.plugin.bili = NS(popular=AsyncMock(return_value=[{"bvid": "BV1234567890", "title": "popular"}]),
                              following_updates=AsyncMock(return_value=[]), search=AsyncMock(side_effect=RuntimeError()))
        self.plugin.cognition = NS(plan=AsyncMock(return_value={"platforms": ["bili"], "focus": "bili", "keywords": ["topic"]}),
                                   choose=AsyncMock(return_value=[]))
        await self.plugin.duty._run({})

    async def test_subtitles_and_frames_both_used(self):
        watcher = self.plugin.video_watch
        watcher._extract = AsyncMock(return_value=[(1.0, "fake.jpg")])
        watcher._caption = AsyncMock(return_value="画面里出现结局")
        result = await watcher._understand(title="样例", author="", description="", duration=20,
                    transcript=[{"start": 0., "end": 20., "text": "完整字幕"}],
                    sources={"video": "stub"}, cfg={"use_frames": True})
        self.assertEqual(result["evidence"], ["subtitle", "frames"])
        self.assertEqual(result["sampled_timestamps"], [1.0])
        self.assertEqual(result["status"], "partial")

    async def test_caption_failure_retains_subtitle(self):
        watcher = self.plugin.video_watch
        watcher._extract = AsyncMock(return_value=[])
        result = await watcher._understand(title="样例", author="", description="", duration=20,
                    transcript=[{"start": 0., "end": 20., "text": "字幕"}],
                    sources={"video": "stub"}, cfg={"use_frames": True})
        self.assertIn("subtitle", result["evidence"])

    async def test_cached_video_not_downloaded_twice(self):
        watcher = self.plugin.video_watch
        loader = AsyncMock(return_value=evidence())
        result = await watcher._read(evidence()["content_id"], evidence()["url"], {}, loader)
        cached = await watcher._read(evidence()["content_id"], evidence()["url"], {}, loader)
        self.assertTrue(cached["from_cache"])
        self.assertEqual(loader.await_count, 1)

    async def test_simultaneous_reads_share_one_result(self):
        watcher = self.plugin.video_watch
        async def load():
            await asyncio.sleep(.01)
            return evidence()
        loader = AsyncMock(side_effect=load)
        await asyncio.gather(*(watcher._read(evidence()["content_id"], evidence()["url"], {}, loader) for _ in range(3)))
        self.assertEqual(loader.await_count, 1)
        self.assertEqual(watcher._locks._locks, {})

    async def test_long_transcript_keeps_end(self):
        output = await self.plugin.video_watch._summarize_text("开头" + "a"*11000 + "最后结论", {"max_transcript_chars": 3000})
        self.assertIn("最后结论", output)

    async def test_invalid_inspection_range_rejected(self):
        result = await self.plugin.video_watch.inspect("BV1234567890", "问题", 0, 181)
        self.assertFalse(result["ok"])

    async def test_metadata_preview_never_claims_watched(self):
        self.plugin.video_watch.any = AsyncMock(return_value={"ok": True, "status": "metadata_only", "digest": "只有文案"})
        preview = peek.LinkPeek(self.plugin)
        result = await preview.peek("https://www.douyin.com/video/1234567890123456789", {})
        self.assertNotIn("你已经看过了", result)
        self.assertIn("metadata_only", result)

    async def test_bili_zero_caps_and_rollover(self):
        with patch.object(bili_duty.BiliDuty, "_data_dir", return_value=self.path):
            obj = bili_duty.BiliDuty(self.plugin)
        obj.cfg["caps"] = {"like": 0, "comment": 0}
        obj.s.update(day="2000-01-01", comment=2)
        self.assertEqual(obj._caps(), {"like": 0, "comment": 0})
        self.assertEqual(obj.s["comment"], 0)

    async def test_douyin_rollover_preserves_seen(self):
        browser = NS(data_dir=self.path)
        obj = douyin.DouyinDuty(self.plugin, browser, {"max_watch_per_day": 0})
        obj.s.update(day="2000-01-01", watched=20, seen=["one"])
        self.assertEqual(obj._caps()["watch"], 0)
        self.assertEqual(obj.s["watched"], 0)
        self.assertEqual(obj.s["seen"], ["one"])

    async def test_dry_run_never_calls_api(self):
        with patch.object(bili_duty.BiliDuty, "_data_dir", return_value=self.path):
            obj = bili_duty.BiliDuty(self.plugin)
        self.plugin.bili = NS(post_comment=AsyncMock(), view=AsyncMock())
        result = await obj.comment("BV1234567890", "评论")
        self.assertTrue(result["dry_run"])
        self.plugin.bili.post_comment.assert_not_called()
        self.assertEqual(obj.s["comment"], 0)

    async def test_failed_watch_never_generates_comment(self):
        with patch.object(bili_duty.BiliDuty, "_data_dir", return_value=self.path):
            obj = bili_duty.BiliDuty(self.plugin)
        item = {"bvid": "BV1234567890", "title": "sample"}
        self.plugin.bili = NS(feed=AsyncMock(return_value=[item]))
        self.plugin.cognition.choose = AsyncMock(return_value=[item])
        self.plugin.video_watch.bili = AsyncMock(return_value={"ok": False})
        obj._decide = AsyncMock()
        await obj.browse(owner_asked=True)
        obj._decide.assert_not_called()
        self.assertEqual(obj.s["watched"], 0)

    async def test_comment_is_decided_after_watching(self):
        with patch.object(bili_duty.BiliDuty, "_data_dir", return_value=self.path):
            obj = bili_duty.BiliDuty(self.plugin)
        item = {"bvid": "BV1234567890", "title": "sample"}
        self.plugin.bili = NS(feed=AsyncMock(return_value=[item]), view=AsyncMock(return_value={"aid": "1"}),
                              comments=AsyncMock(return_value=[]))
        self.plugin.cognition.choose = AsyncMock(return_value=[item])
        self.plugin.cognition.review = AsyncMock(return_value={})
        self.plugin.video_watch.bili = AsyncMock(return_value=evidence())
        async def decide(items):
            self.assertIn("实际画面", items[0]["_body"])
            self.plugin.video_watch.bili.assert_awaited_once()
            return []
        obj._decide = decide
        await obj.browse(owner_asked=True)

    async def test_no_duplicate_share_after_unknown_result(self):
        self.plugin.context.send_message.side_effect = TimeoutError()
        cfg = {"targets": ["qq:FriendMessage:123"]}
        await self.plugin.duty._send("message", cfg, context_items=[{"url": evidence()["url"]}])
        await self.plugin.duty._send("message", cfg, context_items=[{"url": evidence()["url"]}])
        self.assertEqual(self.plugin.context.send_message.await_count, 1)

    async def test_extra_actions_disabled_by_default(self):
        svc = services.RadarServices(self.plugin)
        result = await svc.extra_action("coin", "BV1234567890")
        self.assertFalse(result["ok"])

    async def test_empty_daily_plan_creates_no_fake_events(self):
        self.plugin.radar_ai.ask.return_value = '{"times":[],"reason":"今天不逛"}'
        svc = services.RadarServices(self.plugin)
        result = await svc.daily_plan()
        self.assertEqual(result["times"], [])

    async def test_full_plugin_construct_and_shutdown(self):
        with patch.object(bili_duty.BiliDuty, "_data_dir", return_value=self.path), \
             patch.object(main.DouyinBrowser, "_default_data_dir", return_value=str(self.path)), \
             patch.object(main.TopicWatch, "_data_dir", return_value=self.path):
            plugin = main.CommunityRadarPlugin(NS(), {})
        self.assertIsNone(plugin.duty._task)
        await plugin.initialize()
        self.assertIsNotNone(plugin.duty._task)
        await plugin.terminate()

    async def test_frame_error_retains_other_successful_samples(self):
        watcher = self.plugin.video_watch
        calls = 0
        async def fake_process(cmd, timeout=35):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise TimeoutError()
            Path(cmd[-1]).write_bytes(b"x" * 1200)
            return True
        with patch.object(video, "run_media", side_effect=fake_process):
            pairs = await watcher._extract("unused", 30, 3)
        self.assertEqual(len(pairs), 2)
        self.assertGreater(pairs[-1][0], 29)
        for _, path in pairs:
            Path(path).unlink()

    async def test_cancellation_kills_media_process(self):
        proc = NS(returncode=None, wait=AsyncMock(), kill=lambda: setattr(proc, "returncode", -9))
        async def wait():
            if proc.returncode is None:
                await asyncio.sleep(30)
        proc.wait = AsyncMock(side_effect=wait)
        with patch.object(video.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)):
            task = asyncio.create_task(video.run_media(["fake"]))
            await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(proc.returncode, -9)

    async def test_first_inbox_poll_does_not_reply_to_old_notifications(self):
        self.plugin.bili = NS(cookie="present", _get=AsyncMock(return_value={
            "items": [{"user": {"mid": "123"}, "item": {"source_id": "9", "subject_id": "1", "business_id": 1}}]}))
        self.plugin.bili_duty = NS(reply=AsyncMock())
        svc = services.RadarServices(self.plugin)
        await svc.poll_inbox({"allowed_uids": ["123"], "comments": True})
        self.plugin.bili_duty.reply.assert_not_called()
        self.assertEqual(self.store.get("inbox:reply"), ["9"])

    async def test_private_preview_does_not_send_when_dry(self):
        self.plugin.bili = NS(send_msg=AsyncMock())
        svc = services.RadarServices(self.plugin)
        await svc._send_private("123", 1, "hello", {})
        self.plugin.bili.send_msg.assert_not_called()

    async def test_bangumi_records_only_reliable_episode(self):
        self.plugin.config["bangumi"] = {"enable": True}
        self.plugin.bili = NS(season=AsyncMock(return_value={
            "title": "Test", "episodes": [{"id": 99, "title": "1", "duration": 60000, "cid": "1", "bvid": "BV1234567890"}]}),
            episode_sources=AsyncMock(return_value={}), subtitle_segments=AsyncMock(return_value=[
                {"start": 0., "end": 60., "text": "结尾事实"}]))
        svc = services.RadarServices(self.plugin)
        output = await svc.bangumi("看", "ep99")
        self.assertIn("结尾事实", output)
        self.assertTrue(core.verified(self.store.memory("episode:99")["result"]))

class PureTests(unittest.TestCase):
    def test_frame_points_include_end_without_overrun(self):
        for duration in (.2, 1, 6, 20, 310, 610):
            points = video.sample_times(duration, 6)
            self.assertTrue(all(0 <= t < duration for t in points))
            self.assertLess(duration-points[-1], .3)
            self.assertEqual(points, sorted(set(points)))

    def test_inspection_points_stay_in_range(self):
        points = video.sample_times(600, 6, 400, 500)
        self.assertTrue(all(400 <= p < 500 for p in points))

    def test_structured_json_and_zero_values(self):
        self.assertEqual(core.parse_json('```json\n{"a":1}\n```'), {"a":1})
        self.assertEqual(core.parse_json('{"a":1} garbage'), {})
        self.assertEqual(core.number({"limit":0},"limit",3,0,10),0)
        self.assertEqual(core.number({"limit":float("nan")},"limit",3,0,10),3)

if __name__ == "__main__":
    unittest.main(verbosity=2)
