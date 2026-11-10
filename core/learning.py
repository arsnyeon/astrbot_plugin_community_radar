"""长期知识沉淀：从内容里学到的知识点、说法与梗。

与「观看记忆」不同，这里存的不是某条内容的摘要，而是**可复用**的东西：
一个概念、一条经验、一个梗的用法。观看记忆会随时间压缩明细，而知识条目
只按容量淘汰，不会因为「看过多久了」而消失。

条目按 ``(kind, topic, body)`` 归一后去重；同一说法再次出现只增加计数与
最近一次见到的时间，便于优先回忆起反复出现的说法。
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterable

from astrbot.api import logger

#: 允许的知识类型。``meme`` 指梗/流行说法，``term`` 指专有名词，``knowledge`` 是其余知识。
KINDS = ("knowledge", "meme", "term")
#: 单个条目的正文上限，避免把整段话塞进知识库。
MAX_BODY = 240
#: 知识库保留的最大条目数，超出时按最近使用时间淘汰。
MAX_ENTRIES = 800


def _clean(value: Any, limit: int) -> str:
    """压平空白并截断，用于入库前归一。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _key(value: str) -> str:
    """去重用的归一键：忽略空白、标点与大小写差异。"""
    return re.sub(r"[\W_]+", "", value).lower()


class KnowledgeBook:
    """知识条目读写。

    与 ``RadarStore`` 共用同一个 SQLite 连接；表结构在 ``RadarStore`` 初始化时创建。
    """

    def __init__(self, store):
        self.store = store
        self.db = store.db

    def add(self, entries: Iterable[dict], *, content_id: str = "", platform: str = "",
            title: str = "") -> int:
        """写入若干知识条目，返回真正新增的条数。

        Args:
            entries: 每项含 ``kind``/``topic``/``body``；``kind`` 缺失时按知识处理。
                重复条目只累加计数，不新增行。
            content_id: 来源内容标识，便于回溯出处。
            platform: 来源平台标识。
            title: 来源内容标题。

        Returns:
            新增（而非合并）的条目数量。
        """
        added = 0
        now = time.time()
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("kind") or "knowledge").strip().lower()
            if kind not in KINDS:
                kind = "knowledge"
            body = _clean(entry.get("body") or entry.get("content"), MAX_BODY)
            if len(body) < 2:
                continue
            topic = _clean(entry.get("topic"), 40)
            wanted = _key(body)
            row = self.db.execute(
                "SELECT id, body FROM knowledge WHERE kind=? AND topic=? "
                "ORDER BY seen DESC LIMIT 50", (kind, topic),
            ).fetchall()
            merged = None
            for candidate in row:
                if _key(candidate["body"]) == wanted:
                    merged = candidate["id"]
                    break
            try:
                if merged is not None:
                    self.db.execute(
                        "UPDATE knowledge SET hits=hits+1, seen=?, title=? WHERE id=?",
                        (now, _clean(title, 120), merged),
                    )
                else:
                    self.db.execute(
                        "INSERT INTO knowledge(content_id, platform, title, kind, topic, body,"
                        " created, seen, hits) VALUES(?,?,?,?,?,?,?,?,1)",
                        (content_id[:60], platform[:20], _clean(title, 120), kind,
                         topic, body, now, now),
                    )
                    added += 1
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响整批
                logger.debug(f"[CommunityRadar] 知识写入失败（{type(exc).__name__}）")
        if added:
            self._prune()
        return added

    def search(self, query: str = "", limit: int = 8) -> list[dict]:
        """按关键词检索知识条目。

        关键词会按空白拆分，逐个做包含匹配；空查询返回最近使用的条目。

        Args:
            query: 检索词，可以为空。
            limit: 最多返回条数。

        Returns:
            条目列表，按命中次数与最近使用时间排序。
        """
        limit = max(1, min(int(limit or 8), 50))
        words = [word for word in re.split(r"[\s,，、]+", str(query or "").strip()) if word][:5]
        sql = "SELECT * FROM knowledge"
        params: list[Any] = []
        if words:
            clauses = []
            for word in words:
                clauses.append("(topic LIKE ? OR body LIKE ? OR title LIKE ?)")
                pattern = f"%{word}%"
                params += [pattern, pattern, pattern]
            sql += " WHERE " + " OR ".join(clauses)
        sql += " ORDER BY hits DESC, seen DESC LIMIT ?"
        params.append(limit)
        try:
            rows = self.db.execute(sql, params).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[CommunityRadar] 知识检索失败（{type(exc).__name__}）")
            return []
        return [dict(row) for row in rows]

    def recent(self, limit: int = 10) -> list[dict]:
        """按最近写入顺序取条目。"""
        return self.search("", limit)

    def text(self, query: str = "", limit: int = 6) -> str:
        """把命中的知识拼成一段可直接放进提示词的文本。

        Returns:
            每行一条的文本；没有命中时返回空串。
        """
        labels = {"knowledge": "知道", "meme": "梗", "term": "名词"}
        lines = []
        for item in self.search(query, limit):
            label = labels.get(item.get("kind") or "knowledge", "知道")
            line = f"- [{label}] {item.get('body')}"
            title = str(item.get("title") or "").strip()
            if title:
                line += f"（来自《{title[:30]}》）"
            lines.append(line)
        return "\n".join(lines)

    def stats(self) -> dict:
        """返回条目总数与各类型数量，供状态命令展示。"""
        result = {"total": 0, "knowledge": 0, "meme": 0, "term": 0}
        try:
            for row in self.db.execute("SELECT kind, COUNT(*) AS n FROM knowledge GROUP BY kind"):
                result[str(row["kind"])] = int(row["n"])
                result["total"] += int(row["n"])
        except Exception:  # noqa: BLE001
            pass
        return result

    def _prune(self) -> None:
        """超出容量时按「最久未用」淘汰，保证知识库不会无限增长。"""
        try:
            total = self.db.execute("SELECT COUNT(*) AS n FROM knowledge").fetchone()["n"]
            if int(total) <= MAX_ENTRIES:
                return
            self.db.execute(
                "DELETE FROM knowledge WHERE id IN ("
                " SELECT id FROM knowledge ORDER BY hits ASC, seen ASC LIMIT ?)",
                (int(total) - MAX_ENTRIES,),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[CommunityRadar] 知识库清理失败（{type(exc).__name__}）")
