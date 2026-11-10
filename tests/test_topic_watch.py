"""话题监听的健壮性测试：模型卡住时必须能自己放弃。

``_scan`` 跑在自动浏览的开头，如果模型调用没有超时，整轮会被拖到外层
十分钟预算耗尽，表现为「什么都没发生」。
"""
import asyncio
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("radar_topic_test")
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg

astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("radar_topic")
sys.modules["astrbot"] = astrbot
sys.modules["astrbot.api"] = api

topic = __import__(pkg.__name__ + ".services.topic_watch", fromlist=["x"])
NS = types.SimpleNamespace


class TopicScanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="radar_topic_"))
        self.patch_dir = patch.object(topic.TopicWatch, "_data_dir",
                                      return_value=self.folder)
        self.patch_dir.start()
        self.addCleanup(self.patch_dir.stop)

    def watcher(self):
        plugin = NS(config={"browse": {}, "topic": {}},
                    duty=NS(_bot_persona=AsyncMock(return_value="")))
        watch = topic.TopicWatch(plugin)
        return watch

    async def test_scan_gives_up_when_model_hangs(self):
        watch = self.watcher()
        provider = NS(text_chat=AsyncMock(side_effect=lambda **kwargs: asyncio.sleep(3600)))
        watch._provider = AsyncMock(return_value=provider)

        async def fake_wait_for(coro, timeout=None):
            """模拟超时：先把未等待的协程收掉，避免 RuntimeWarning。"""
            coro.close()
            raise asyncio.TimeoutError()

        with patch.object(topic.asyncio, "wait_for", fake_wait_for):
            await watch._scan("g:GroupMessage:1", [{"text": "在聊银松镇"}], {})

        self.assertEqual(watch.queue(5), [], "超时不应产生半成品话题")

    async def test_scan_survives_other_errors(self):
        watch = self.watcher()
        provider = NS(text_chat=AsyncMock(side_effect=RuntimeError("boom")))
        watch._provider = AsyncMock(return_value=provider)

        await watch._scan("g:GroupMessage:1", [{"text": "随便聊聊"}], {})
        self.assertEqual(watch.queue(5), [])

    async def test_scan_records_topic_on_success(self):
        watch = self.watcher()
        provider = NS(text_chat=AsyncMock(return_value=NS(
            completion_text='[{"keyword":"银松镇","why":"想看看","platform":"bili","title":"银松镇"}]')))
        watch._provider = AsyncMock(return_value=provider)

        await watch._scan("g:GroupMessage:1", [{"text": "银松镇有人玩吗"}], {})
        queued = watch.queue(5)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["keyword"], "银松镇")

    async def test_missing_provider_is_skipped(self):
        watch = self.watcher()
        watch._provider = AsyncMock(return_value=None)
        await watch._scan("g:GroupMessage:1", [{"text": "x"}], {})
        self.assertEqual(watch.queue(5), [])


if __name__ == "__main__":
    unittest.main()
