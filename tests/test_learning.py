"""长期知识与知识库同步的测试。

知识条目是「不会随时间遗忘」的那一层：观看记忆会压缩，这里只按容量淘汰。
"""
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("radar_learn_test")
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg

import logging  # noqa: E402

astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("radar_learn")
sys.modules["astrbot"] = astrbot
sys.modules["astrbot.api"] = api

learning = __import__(pkg.__name__ + ".core.learning", fromlist=["x"])
kb_sync = __import__(pkg.__name__ + ".core.kb_sync", fromlist=["x"])
NS = types.SimpleNamespace


class Store:
    """最小可用的 RadarStore 替身：只用 kv 与一个 sqlite 连接。"""

    def __init__(self):
        import sqlite3

        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE knowledge(
                id INTEGER PRIMARY KEY, content_id TEXT NOT NULL DEFAULT '',
                platform TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'knowledge', topic TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL, created REAL NOT NULL, seen REAL NOT NULL,
                hits INTEGER NOT NULL DEFAULT 1);
        """)
        self.kv = {}

    def get(self, key, default=None):
        return self.kv.get(key, default)

    def set(self, key, value):
        self.kv[key] = value


class KnowledgeBookTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.book = learning.KnowledgeBook(self.store)

    def test_add_and_count(self):
        added = self.book.add([{"kind": "knowledge", "topic": "游戏", "body": "独立游戏通常指小团队作品"}])
        self.assertEqual(added, 1)
        self.assertEqual(self.book.stats()["total"], 1)

    def test_duplicate_only_increments_hits(self):
        entry = {"kind": "meme", "topic": "梗", "body": "「破防了」表示情绪被击穿"}
        self.book.add([entry])
        self.book.add([entry])
        self.assertEqual(self.book.stats()["total"], 1)
        item = self.book.recent(1)[0]
        self.assertEqual(item["hits"], 2)

    def test_dedupe_ignores_punctuation(self):
        self.book.add([{"kind": "term", "topic": "名词", "body": "3A 大作"}])
        self.book.add([{"kind": "term", "topic": "名词", "body": "3A大作"}])
        self.assertEqual(self.book.stats()["total"], 1)

    def test_unknown_kind_falls_back_to_knowledge(self):
        self.book.add([{"kind": "奇怪类型", "topic": "t", "body": "内容"}])
        self.assertEqual(self.book.stats()["knowledge"], 1)

    def test_too_short_body_is_ignored(self):
        self.assertEqual(self.book.add([{"body": "a"}]), 0)
        self.assertEqual(self.book.add([{"body": ""}]), 0)

    def test_search_by_keyword(self):
        self.book.add([{"kind": "meme", "topic": "游戏圈", "body": "「电子核桃」指难啃的老游戏"}])
        self.book.add([{"kind": "knowledge", "topic": "硬件", "body": "显卡显存影响大分辨率表现"}])
        found = self.book.search("核桃")
        self.assertEqual(len(found), 1)
        self.assertIn("电子核桃", found[0]["body"])

    def test_search_empty_returns_recent(self):
        self.book.add([{"body": "第一条"}, {"body": "第二条"}])
        self.assertTrue(self.book.recent(5))

    def test_text_renders_labels(self):
        self.book.add([{"kind": "meme", "topic": "梗", "body": "「云玩家」指只看不玩"}])
        text = self.book.text("云玩家")
        self.assertIn("梗", text)
        self.assertIn("云玩家", text)

    def test_text_empty_without_match(self):
        self.assertEqual(self.book.text("完全不存在的词"), "")


class KbSyncTests(unittest.TestCase):
    def _plugin(self, kb_name="", docs=None):
        store = Store()
        book = learning.KnowledgeBook(store)
        book.add([{"kind": "knowledge", "topic": "游戏", "body": "独立游戏通常体量较小"}])

        class Helper:
            def __init__(self):
                self.uploaded = []
                self.deleted = []
                self.docs = docs or []

            async def list_documents(self, offset=0, limit=100, search=None):
                return list(self.docs)

            async def delete_document(self, doc_id):
                self.deleted.append(doc_id)

            async def upload_document(self, name, content, file_type, pre_chunked_text=None):
                self.uploaded.append({"name": name, "text": content.decode("utf-8"),
                                      "chunks": pre_chunked_text})

        helper = Helper()

        class Manager:
            async def get_kb_by_name(self, name):
                return helper

        plugin = NS(config={"learning": {"kb_name": kb_name}},
                    knowledge=book,
                    radar_store=store,
                    context=NS(kb_manager=Manager()))
        return plugin, helper, store

    def test_render_contains_sections(self):
        plugin, _, _ = self._plugin()
        text = kb_sync._render(plugin.knowledge)
        self.assertIn("社区见闻", text)
        self.assertIn("独立游戏通常体量较小", text)

    def test_no_kb_configured_is_noop(self):
        plugin, helper, _ = self._plugin(kb_name="")
        self.assertFalse(self._run(plugin))
        self.assertEqual(helper.uploaded, [])

    def test_replaces_previous_document(self):
        doc = NS(doc_name=kb_sync.DOC_NAME, doc_id="doc-1")
        plugin, helper, store = self._plugin(kb_name="社区", docs=[doc])
        self.assertTrue(self._run(plugin))
        self.assertEqual(helper.deleted, ["doc-1"])
        self.assertEqual(len(helper.uploaded), 1)
        self.assertEqual(helper.uploaded[0]["name"], kb_sync.DOC_NAME)
        self.assertIn("digest", store.kv.get("kb_sync", {}))

    def test_unchanged_content_skips_upload(self):
        plugin, helper, _ = self._plugin(kb_name="社区")
        self.assertTrue(self._run(plugin))
        self.assertFalse(self._run(plugin))
        self.assertEqual(len(helper.uploaded), 1)

    def test_missing_kb_is_tolerated(self):
        plugin, _, _ = self._plugin(kb_name="不存在")

        class Empty:
            async def get_kb_by_name(self, name):
                return None

        plugin.context = NS(kb_manager=Empty())
        self.assertFalse(self._run(plugin))

    def _run(self, plugin):
        import asyncio

        return asyncio.run(kb_sync.sync(plugin))


if __name__ == "__main__":
    unittest.main()
