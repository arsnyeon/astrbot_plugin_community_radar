"""Tests for the human-like browsing flow: watch first, read comments, then react."""
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("radar_human_test")
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
module("astrbot.api", logger=logging.getLogger("radar_human"), AstrBotConfig=dict)
module("astrbot.api.event", AstrMessageEvent=object, filter=deco)
module("astrbot.api.provider", ProviderRequest=object)
module("astrbot.api.star", Context=object, Star=lambda context: None, register=deco)
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
module("astrbot.core.message.message_event_result", MessageChain=lambda parts: parts)

comments = __import__(pkg.__name__ + ".media.comments", fromlist=["x"])
duty_mod = __import__(pkg.__name__ + ".services.duty", fromlist=["x"])
autopilot_mod = __import__(pkg.__name__ + ".services.autopilot", fromlist=["x"])
video = __import__(pkg.__name__ + ".media.video_watch", fromlist=["x"])
NS = types.SimpleNamespace


class LowValueCommentTests(unittest.TestCase):
    """评论区只留有人话的部分，纯表情和广告不占上下文额度。"""

    def test_pure_reactions_and_placeholders_are_dropped(self):
        for text in ("哈哈", "哈哈哈哈", "[表情]", "[图片]", "？？？", "6", "@张三 @李四", ""):
            self.assertTrue(comments.is_low_value(text), text)

    def test_advertisement_is_dropped(self):
        for text in ("加微信领取资料", "点击链接下载app", "代刷粉丝私信我"):
            self.assertTrue(comments.is_low_value(text), text)

    def test_real_comment_is_kept(self):
        for text in ("这游戏难度确实高，卡在第三关了", "91块还一堆红问号", "啊这"):
            self.assertFalse(comments.is_low_value(text), text)

    def test_normalize_filters_then_sorts(self):
        raw = [
            {"text": "哈哈", "likes": 100},
            {"text": "加微领取", "likes": 50},
            {"text": "这游戏真不错", "likes": 3},
            {"text": "价格劝退", "likes": 9},
        ]
        out = comments.normalize_comments(raw, limit=5)
        self.assertEqual([c["text"] for c in out], ["价格劝退", "这游戏真不错"])

    def test_all_filtered_returns_empty(self):
        self.assertEqual(comments.normalize_comments([{"text": "哈哈哈"}, {"text": "[表情]"}], 5), [])


class NormalizeTests(unittest.TestCase):
    def test_three_client_shapes_normalize_to_one(self):
        raw = [
            {"name": "a", "text": "第一条", "like": 3},
            {"user": "b", "content": "第二条", "likes": 9},
            {"name": "c", "text": "第三条", "like": "7"},
            {"name": "d", "text": ""},
        ]
        out = comments.normalize_comments(raw, limit=5)
        self.assertEqual([c["text"] for c in out], ["第二条", "第三条", "第一条"])
        self.assertEqual(out[0]["likes"], 9)

    def test_duplicates_and_limit(self):
        raw = [{"name": "a", "text": "同一条"}] * 3 + [{"name": "b", "text": "另一条"}]
        out = comments.normalize_comments(raw, limit=1)
        self.assertEqual(len(out), 1)

    def test_digest_is_empty_for_no_comments(self):
        self.assertEqual(comments.digest_of([]), "")
        self.assertIn("第一条", comments.digest_of([{"name": "a", "text": "第一条", "likes": 1}]))

    def test_render_is_display_only(self):
        raw = [{"name": "甲", "text": "前排", "likes": 5},
               {"name": "乙", "text": "笑死", "likes": 2}]
        rendered = comments.render(raw, 5)
        self.assertEqual(rendered, ["甲：前排", "乙：笑死"])
        # 渲染不得改动原结构
        self.assertIsInstance(raw[0], dict)

    def test_render_skips_malformed_and_respects_limit(self):
        raw = [{"name": "甲", "text": "一"}, {"name": "乙", "text": ""}, "裸字符串",
               {"name": "丙", "text": "三"}]
        self.assertEqual(comments.render(raw, 1), ["甲：一"])
        self.assertEqual(comments.render(raw, 5), ["甲：一", "丙：三"])


class CommentReadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_bili_comments_join_the_evidence(self):
        plugin = NS(
            bili=NS(view=AsyncMock(return_value={"aid": "1"}),
                    comments=AsyncMock(return_value=[{"name": "甲", "text": "前排", "like": 5}])),
            config={},
        )
        result = {"content_id": "bili:BV1xx411c7mD", "digest": "正文", "status": "partial",
                  "evidence": ["subtitle"]}
        found = await comments.collect(plugin, result, limit=8)
        self.assertEqual(found[0]["text"], "前排")

    async def test_failure_returns_nothing_instead_of_raising(self):
        plugin = NS(bili=NS(view=AsyncMock(side_effect=RuntimeError("offline"))), config={})
        found = await comments.collect(plugin, {"content_id": "bili:BV1xx411c7mD"})
        self.assertEqual(found, [])

    async def test_douyin_disabled_does_not_open_a_browser(self):
        plugin = NS(config={"douyin": {"enabled": False}}, browser_lock=None)
        found = await comments.collect(plugin, {"content_id": "douyin:123"})
        self.assertEqual(found, [])


class FlowOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_comments_are_read_after_watching_and_before_review(self):
        calls = []

        async def watch(link, cfg):
            calls.append("watch")
            return {"ok": True, "status": "partial", "evidence": ["subtitle"],
                    "digest": "看完了", "content_id": "bili:BV1xx411c7mD"}

        async def review(result, comments=None):
            calls.append(("review", len(comments or [])))
            return {"score": 5, "review": "还行"}

        async def collect(plugin, result, limit=8):
            calls.append("comments")
            return [{"name": "甲", "text": "第一条", "likes": 1}]

        values = {}
        store = NS(directory=Path(tempfile.mkdtemp(prefix="radar_human_")), set=lambda k, v: values.__setitem__(k, v),
                   get=lambda k, d=None: values.get(k, d),
                   reserve=lambda *a, **k: (1, ""), finish=lambda *a, **k: None)
        plugin = NS(
            config={"browse": {"attach_image": False}, "share": {"enable": False},
                    "video": {"read_comments": True}, "douyin": {"enabled": False}},
            radar_store=store,
            bili=NS(popular=AsyncMock(return_value=[{"bvid": "BV1xx411c7mD", "title": "t"}])),
            heybox=NS(feed=AsyncMock(return_value=[])),
            video_watch=NS(any=watch),
            cognition=NS(plan=AsyncMock(return_value={"platforms": ["bili"], "focus": "bili",
                                                     "keywords": [], "skip": False}),
                         choose=AsyncMock(return_value=[{"title": "t", "bvid": "BV1xx411c7mD",
                                                         "_link": "https://www.bilibili.com/video/BV1xx411c7mD"}]),
                         review=review),
            topic_watch=None,
        )
        duty = duty_mod.BrowseDuty(plugin, store.directory)
        duty._summarize = AsyncMock(return_value=([], [], "", []))
        original = duty_mod.collect_comments
        duty_mod.collect_comments = collect
        try:
            await duty._run({"count": 1})
        finally:
            duty_mod.collect_comments = original
        self.assertEqual(calls[0], "watch")
        self.assertEqual(calls[1], "comments")
        self.assertEqual(calls[2], ("review", 1))

    async def test_review_receives_structured_comments(self):
        """评论必须以结构化的字典传给观感环节。

        回归：``_comments`` 曾经一处存「甲：内容」字符串、一处按字典读取，
        导致真实浏览在回味阶段抛 ``'str' object has no attribute 'get'`` 而中断。
        """
        captured = {}

        async def watch(link, cfg):
            return {"ok": True, "status": "partial", "evidence": ["subtitle"],
                    "digest": "看完了", "content_id": "bili:BV1xx411c7mD"}

        async def review(result, comments=None):
            captured["comments"] = comments
            return {"score": 6, "review": "还行"}

        async def collect(plugin, result, limit=8):
            return [{"name": "甲", "text": "第一条", "likes": 3, "replies": 0}]

        values = {}
        store = NS(directory=Path(tempfile.mkdtemp(prefix="radar_human_")),
                   set=lambda k, v: values.__setitem__(k, v),
                   get=lambda k, d=None: values.get(k, d),
                   reserve=lambda *a, **k: (1, ""), finish=lambda *a, **k: None)
        plugin = NS(
            config={"browse": {"attach_image": False}, "share": {"enable": False},
                    "video": {"read_comments": True}, "douyin": {"enabled": False}},
            radar_store=store,
            bili=NS(popular=AsyncMock(return_value=[{"bvid": "BV1xx411c7mD", "title": "t"}])),
            heybox=NS(feed=AsyncMock(return_value=[])),
            video_watch=NS(any=watch),
            cognition=NS(plan=AsyncMock(return_value={"platforms": ["bili"], "focus": "bili",
                                                     "keywords": [], "skip": False}),
                         choose=AsyncMock(return_value=[{"title": "t", "bvid": "BV1xx411c7mD",
                                                         "_link": "https://www.bilibili.com/video/BV1xx411c7mD"}]),
                         review=review),
            topic_watch=None,
        )
        duty = duty_mod.BrowseDuty(plugin, store.directory)
        duty._summarize = AsyncMock(return_value=([], [], "", []))
        original = duty_mod.collect_comments
        duty_mod.collect_comments = collect
        try:
            await duty._run({"count": 1})
        finally:
            duty_mod.collect_comments = original

        self.assertIsInstance(captured["comments"], list)
        self.assertIsInstance(captured["comments"][0], dict)
        self.assertEqual(captured["comments"][0]["text"], "第一条")

    async def test_pacing_uses_light_verdict_not_full_review(self):
        """「要不要收手」只问轻量判断；完整感想必须等评论区翻完。

        回归：为了决定提前收手，曾在翻评论区之前就调用 ``review`` 并把结果记在
        条目上，导致这条内容的感想永远看不到评论。
        """
        calls: list = []

        async def watch(link, cfg):
            return {"ok": True, "status": "partial", "evidence": ["subtitle"],
                    "digest": "看完了", "content_id": "bili:" + link[-5:]}

        async def verdict(result):
            calls.append("verdict")
            return {"score": 9}

        async def review(result, comments=None):
            calls.append(("review", len(comments or [])))
            return {"score": 9, "review": "还行"}

        async def collect(plugin, result, limit=8):
            return [{"name": "甲", "text": "有意思", "likes": 1, "replies": 0}]

        values = {}
        store = NS(directory=Path(tempfile.mkdtemp(prefix="radar_pace_")),
                   set=lambda k, v: values.__setitem__(k, v),
                   get=lambda k, d=None: values.get(k, d),
                   reserve=lambda *a, **k: (1, ""), finish=lambda *a, **k: None)
        items = [{"title": "一", "bvid": "BV1aaaaaaaaa",
                  "_link": "https://www.bilibili.com/video/BV1aaaaaaaaa"},
                 {"title": "二", "bvid": "BV1bbbbbbbbb",
                  "_link": "https://www.bilibili.com/video/BV1bbbbbbbbb"}]
        plugin = NS(
            config={"browse": {"attach_image": False, "pause_min_seconds": 0,
                               "pause_max_seconds": 0},
                    "share": {"enable": False}, "video": {"read_comments": True},
                    "douyin": {"enabled": False}},
            radar_store=store,
            bili=NS(popular=AsyncMock(return_value=items)),
            heybox=NS(feed=AsyncMock(return_value=[])),
            video_watch=NS(any=watch),
            cognition=NS(plan=AsyncMock(return_value={"platforms": ["bili"], "focus": "bili",
                                                      "keywords": [], "skip": False}),
                         choose=AsyncMock(return_value=items),
                         verdict=verdict, review=review),
            topic_watch=None,
        )
        duty = duty_mod.BrowseDuty(plugin, store.directory)
        duty._summarize = AsyncMock(return_value=([], [], "", []))
        original = duty_mod.collect_comments
        duty_mod.collect_comments = collect
        try:
            # boredom_chance 置零：否则有约 12% 概率在第一条后随机收手，
            # 断言「两条都形成感想」会变成偶发失败。
            await duty._run({"count": 2, "pause_min_seconds": 0, "pause_max_seconds": 0,
                             "boredom_chance": 0})
        finally:
            duty_mod.collect_comments = original

        self.assertIn("verdict", calls, "提前收手应使用轻量判断")
        reviews = [c for c in calls if isinstance(c, tuple)]
        self.assertEqual(len(reviews), 2, "两条内容都应各自形成一次完整感想")
        self.assertTrue(all(count >= 1 for _, count in reviews), "感想必须带上评论")


class TiebaPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_tieba_item_is_read_without_video_watch(self):
        """贴吧候选直接用自带正文构造证据，不再调用视频理解。"""
        watched = []

        async def watch(link, cfg):
            watched.append(link)
            return {"ok": True}

        plugin = NS(
            config={"video": {}, "tieba": {"enable": True, "forums": []}},
            video_watch=NS(any=watch),
            tieba=NS(),
        )
        duty = duty_mod.BrowseDuty(plugin, Path(tempfile.mkdtemp(prefix="radar_tieba_")))
        item = {"tid": "9", "title": "标题", "text": "正文", "images": [],
                "_tieba": True, "_link": "https://tieba.baidu.com/p/9"}
        result = await duty._read_item(item)
        self.assertEqual(watched, [])
        self.assertEqual(result["content_id"], "tieba:9")
        self.assertTrue(result["ok"])

    async def test_non_tieba_item_still_uses_video_watch(self):
        watched = []

        async def watch(link, cfg):
            watched.append(link)
            return {"ok": True, "status": "partial"}

        plugin = NS(config={"video": {}}, video_watch=NS(any=watch), tieba=NS())
        duty = duty_mod.BrowseDuty(plugin, Path(tempfile.mkdtemp(prefix="radar_tieba_")))
        await duty._read_item({"title": "t", "_link": "https://www.bilibili.com/video/BV1"})
        self.assertEqual(len(watched), 1)

    async def test_tieba_forums_produce_candidates(self):
        async def forum_threads(forum, limit=30):
            return [{"tid": "1", "title": f"{forum}的帖子", "url": "https://tieba.baidu.com/p/1"}]

        async def search(word, limit=5):
            return [{"tid": "2", "title": f"{word}的搜索结果", "url": "https://tieba.baidu.com/p/2"}]

        plugin = NS(
            config={"tieba": {"enable": True, "forums": ["独立游戏"]}},
            tieba=NS(forum_threads=forum_threads, search=search),
        )
        duty = duty_mod.BrowseDuty(plugin, Path(tempfile.mkdtemp(prefix="radar_tieba_")))
        found = await duty._tieba_candidates(["关键词"], 5, {"count": 5})
        self.assertTrue(found)
        self.assertTrue(all(item.get("_tieba") for item in found))
        self.assertTrue(all(item.get("platform") == "tieba" for item in found))
        self.assertTrue(all(item.get("_link") for item in found))

    async def test_tieba_disabled_yields_no_candidates(self):
        plugin = NS(config={"tieba": {"enable": False, "forums": ["x"]}},
                    tieba=NS(forum_threads=AsyncMock(), search=AsyncMock()))
        duty = duty_mod.BrowseDuty(plugin, Path(tempfile.mkdtemp(prefix="radar_tieba_")))
        self.assertEqual(await duty._tieba_candidates(["kw"], 5, {}), [])


class WatchListFlowTests(unittest.IsolatedAsyncioTestCase):
    """自己记下的「想看」应该真的被看到，而不是被随机挑空吞掉。"""

    def _duty(self, queued, choose_result):
        board = NS(
            peek=lambda limit=1: queued[:limit],
            remove=lambda *a, **k: True,
        )
        plugin = NS(
            radar_store=NS(set=lambda *a, **k: None, get=lambda *a, **k: None,
                           reserve=lambda *a, **k: (1, "")),
            watchlist=board,
            bili=NS(search=AsyncMock(return_value=[])),
            tieba=NS(search=AsyncMock(return_value=[])),
            cognition=NS(choose=AsyncMock(return_value=choose_result)),
        )
        return duty_mod.BrowseDuty(plugin, Path(tempfile.mkdtemp(prefix="radar_wl_flow_")))

    async def test_watchlist_item_is_placed_first(self):
        duty = self._duty([{"url": "https://www.bilibili.com/video/BV1xx411c7mD",
                            "title": ""}], [])
        candidates = await duty._from_watchlist([{"title": "别的", "_link": "https://x"}], 3)
        self.assertTrue(candidates[0].get("_from_list"))
        self.assertEqual(candidates[0]["_link"], "https://www.bilibili.com/video/BV1xx411c7mD")

    async def test_title_only_entry_is_resolved_by_search(self):
        board = NS(peek=lambda limit=1: [{"url": "", "title": "银松镇"}],
                   remove=lambda *a, **k: True)
        plugin = NS(radar_store=NS(set=lambda *a, **k: None, get=lambda *a, **k: None),
                    watchlist=board,
                    bili=NS(search=AsyncMock(return_value=[{"bvid": "BV1abc"}])),
                    tieba=NS(search=AsyncMock(return_value=[])),
                    cognition=NS(choose=AsyncMock(return_value=[])))
        duty = duty_mod.BrowseDuty(plugin, Path(tempfile.mkdtemp(prefix="radar_wl_flow_")))
        candidates = await duty._from_watchlist([], 3)
        self.assertEqual(candidates[0]["_link"], "https://www.bilibili.com/video/BV1abc")

    async def test_unresolvable_title_is_skipped(self):
        duty = self._duty([{"url": "", "title": "一个搜不到的东西"}], [])
        self.assertEqual(await duty._from_watchlist([], 3), [])


class PlatformSelectionTests(unittest.TestCase):
    def _autopilot(self, tieba_enable):
        plugin = NS(config={"douyin": {"enabled": False},
                            "tieba": {"enable": tieba_enable}})
        return autopilot_mod.Autopilot(plugin)

    def test_tieba_excluded_when_disabled(self):
        self.assertNotIn("tieba", self._autopilot(False)._enabled_platforms({"platforms": "all"}))

    def test_tieba_included_when_enabled(self):
        self.assertIn("tieba", self._autopilot(True)._enabled_platforms({"platforms": "all"}))

    def test_single_platform_choice_is_respected(self):
        self.assertEqual(self._autopilot(True)._enabled_platforms({"platforms": "tieba"}), ["tieba"])
        self.assertEqual(self._autopilot(False)._enabled_platforms({"platforms": "tieba"}), [])


class CrossCheckTests(unittest.IsolatedAsyncioTestCase):
    """刷到有感觉的内容时，会顺手去别处看一眼别人怎么说。"""

    async def test_no_cross_check_when_score_low(self):
        called = []

        async def search(word, limit=5):
            called.append(word)
            return [{"tid": "1", "text": "别人在聊", "forum": "独立游戏"}]

        plugin = NS(config={"tieba": {"enable": True}},
                    tieba=NS(search=search))
        duty = duty_mod.BrowseDuty(plugin, Path(tempfile.mkdtemp(prefix="radar_cross_")))
        items = [{"title": "标题", "_review": {"score": 2}, "_body": "正文"}]
        await duty._cross_check(items, {})
        self.assertEqual(called, [])
        self.assertNotIn("_cross", items[0])

    async def test_cross_check_appends_related_discussion(self):
        async def search(word, limit=5):
            return [{"tid": "1", "text": "贴吧里有人说手感一般", "forum": "独立游戏"}]

        plugin = NS(config={"tieba": {"enable": True}},
                    tieba=NS(search=search))
        duty = duty_mod.BrowseDuty(plugin, Path(tempfile.mkdtemp(prefix="radar_cross_")))
        item = {"title": "某游戏", "_review": {"score": 7, "keywords": ["某游戏"]}, "_body": "正文"}
        await duty._cross_check([item], {})
        self.assertIn("_cross", item)
        self.assertIn("贴吧里有人说手感一般", item["_body"])
        self.assertIn("不是同一条内容", item["_body"])

    async def test_cross_check_can_be_disabled(self):
        called = []

        async def search(word, limit=5):
            called.append(word)
            return [{"tid": "1", "text": "x", "forum": "f"}]

        plugin = NS(config={"tieba": {"enable": True}}, tieba=NS(search=search))
        duty = duty_mod.BrowseDuty(plugin, Path(tempfile.mkdtemp(prefix="radar_cross_")))
        await duty._cross_check([{"title": "t", "_review": {"score": 9}, "_body": ""}],
                                {"cross_check": False})
        self.assertEqual(called, [])

    async def test_cross_check_survives_search_failure(self):
        async def search(word, limit=5):
            raise RuntimeError("boom")

        plugin = NS(config={"tieba": {"enable": True}}, tieba=NS(search=search))
        duty = duty_mod.BrowseDuty(plugin, Path(tempfile.mkdtemp(prefix="radar_cross_")))
        item = {"title": "t", "_review": {"score": 9, "keywords": ["t"]}, "_body": "正文"}
        await duty._cross_check([item], {})
        self.assertNotIn("_cross", item)


class FrameBudgetTests(unittest.TestCase):
    def test_longer_videos_get_more_frames_within_the_cap(self):
        self.assertLessEqual(video.frame_budget(30, 6), 4)
        self.assertEqual(video.frame_budget(300, 12), 8)
        self.assertEqual(video.frame_budget(3600, 12), 12)
        self.assertEqual(video.frame_budget(0, 6), 4)
        self.assertEqual(video.frame_budget(600, 0), 0)


class PageOverviewTests(unittest.TestCase):
    """插件页面的数据汇总必须容错：任一服务缺失也不能抛。"""

    def payload(self, **overrides):
        page = __import__(pkg.__name__ + ".handlers.page_api", fromlist=["x"])
        store = NS(get=lambda k, d=None: {"last_browse": {"at": 1, "analyzed": 2}}.get(k, d),
                   stats=lambda: {"memory": 5, "events": 9, "share_today": 1},
                   preferences=lambda: [{"value": "独立游戏", "polarity": "like", "strength": 0.8}],
                   curiosity=lambda n=6: ["银松镇"])
        base = dict(
            config={"browse": {"enable": True, "platforms": "all"},
                    "share": {"enable": True, "dry_run": False, "style": "chat",
                              "max_per_day": 8, "max_images": 5, "min_score_to_share": 6},
                    "video": {"frame_source": "ffmpeg", "frame_width": 854,
                              "frame_compose": "auto", "ocr_subtitles": True,
                              "ocr_fps": 1, "ocr_max_frames": 60}},
            radar_store=store,
            knowledge=NS(stats=lambda: {"total": 3, "knowledge": 1, "meme": 1, "term": 1}),
            watchlist=NS(peek=lambda n=8: [{"title": "某视频", "url": "u", "from_user": "群友"}]),
            health=NS(summary=lambda: {"bili": {"fails": 2, "reason": "-101"}}),
            duty=NS(next_run_text=lambda: "11-11 09:20"),
        )
        base.update(overrides)
        return page.PageMixin._overview_payload(NS(**base))

    def test_payload_has_all_sections(self):
        data = self.payload()
        for key in ("browse", "share", "ledger", "knowledge", "interests",
                    "watchlist", "health", "video"):
            self.assertIn(key, data)
        self.assertEqual(data["browse"]["next_run"], "11-11 09:20")
        self.assertEqual(data["share"]["today"], 1)
        self.assertEqual(data["video"]["ocr"]["enabled"], True)

    def test_missing_services_do_not_break(self):
        data = self.payload(knowledge=None, watchlist=None, health=None)
        self.assertEqual(data["knowledge"], {})
        self.assertEqual(data["watchlist"], [])
        self.assertEqual(data["health"], {})

    def test_interests_are_trimmed(self):
        data = self.payload()
        self.assertEqual(len(data["interests"]), 1)
        self.assertEqual(data["interests"][0]["value"], "独立游戏")


class SegmentTests(unittest.TestCase):
    def test_long_text_is_split_not_truncated_into_one_blob(self):
        long_text = "这是一句很长的话，需要被切开才能像聊天。" * 12
        parts = duty_mod.BrowseDuty._clean_parts([long_text], 5)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 160 for p in parts))

    def test_short_parts_pass_through_unchanged(self):
        parts = duty_mod.BrowseDuty._clean_parts(["哈哈", "这个真好看"], 5)
        self.assertEqual(parts, ["哈哈", "这个真好看"])

    def test_overflow_merges_tail_instead_of_dropping(self):
        parts = duty_mod.BrowseDuty._clean_parts(["一", "二", "三", "四", "五", "六", "七"], 5)
        self.assertEqual(len(parts), 5)
        self.assertEqual(parts[-1], "五六七")


if __name__ == "__main__":
    unittest.main()
