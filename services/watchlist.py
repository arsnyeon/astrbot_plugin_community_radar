"""「想刷列表」：聊天里别人发的东西，她感兴趣就记下来待会儿自己去看。

与话题队列的区别：话题队列记的是**关键词**（"去搜搜看"），这里记的是**具体某一条**
内容（"这个视频我想看"）。两者互补：话题影响她逛的方向，列表决定她下次具体打开什么。

只做记账与去重，不产生回复；是否真的去看由浏览流程决定。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..core.statefile import read_json, write_json

#: 队列上限，超出时丢掉最旧的。
MAX_ITEMS = 20
#: 单条在列表里最多保留多久（秒）。放太久就不想看了。
MAX_AGE = 7 * 86400

#: 支持的内容链接；命中即认为「这是一条具体内容」。
_PATTERNS = (
    ("bili", re.compile(r"https?://(?:www\.|m\.)?bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)")),
    ("bili", re.compile(r"https?://b23\.tv/([0-9A-Za-z]+)")),
    ("bili", re.compile(r"(?<![0-9A-Za-z])(BV[0-9A-Za-z]{10})(?![0-9A-Za-z])")),
    ("douyin", re.compile(r"https?://(?:v\.|www\.)?douyin\.com/(?:video/)?(\d{6,})")),
    ("tieba", re.compile(r"https?://(?:tieba\.baidu\.com|tieba\.baidu\.com/mo/q/m)/p/(\d+)")),
    ("heybox", re.compile(r"https?://(?:www\.)?xiaoheihe\.cn/app/bbs/link/(\d+)")),
)


def extract_links(text: str) -> list[dict]:
    """从一句话里找出支持的内容链接。

    Args:
        text: 原始消息文本。

    Returns:
        每项含 ``platform``/``native_id``/``url``；同一链接不会重复出现。
    """
    text = str(text or "")
    found: list[dict] = []
    seen: set[str] = set()
    for platform, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(1)
            url = match.group(0)
            if url.startswith("http") is False:
                url = f"https://www.bilibili.com/video/{value}" if platform == "bili" else url
            key = f"{platform}:{value}"
            if key in seen:
                continue
            seen.add(key)
            found.append({"platform": platform, "native_id": value, "url": url})
    return found


class WatchList:
    """落盘的「想刷列表」，按加入时间先入先出。"""

    def __init__(self, directory: Path):
        self.path = Path(directory) / "watchlist.json"

    def _load(self) -> list[dict]:
        data, _ = read_json(self.path, [])
        if not isinstance(data, list):
            return []
        now = time.time()
        out = []
        for item in data:
            if not isinstance(item, dict) or not (item.get("url") or item.get("title")):
                continue
            try:
                added = float(item.get("added_at") or 0)
            except (TypeError, ValueError):
                added = 0.0
            if now - added > MAX_AGE:
                continue
            out.append(item)
        return out[-MAX_ITEMS:]

    def _save(self, items: list[dict]) -> None:
        write_json(self.path, items[-MAX_ITEMS:])

    def add(self, url: str, *, platform: str = "", native_id: str = "",
            title: str = "", source: str = "chat", from_user: str = "") -> bool:
        """加入列表；已存在时只更新标题等元信息。

        Args:
            url: 内容链接。
            platform: 平台标识。
            native_id: 平台内编号。
            title: 已知标题，可空。
            source: 来源，例如 ``chat``。
            from_user: 谁提到的，便于日志排查。

        Returns:
            是否新增（重复加入返回 ``False``）。
        """
        url = str(url or "").strip()
        title = str(title or "").strip()
        if not url and not title:
            return False
        items = self._load()
        identity = url or f"title:{title}"
        for item in items:
            current = str(item.get("url") or "") or f"title:{item.get('title') or ''}"
            if current == identity:
                if title and not item.get("title"):
                    item["title"] = title[:120]
                self._save(items)
                return False
        items.append({
            "url": url,
            "platform": platform,
            "native_id": native_id,
            "title": title[:120],
            "source": source,
            "from_user": str(from_user or "")[:40],
            "added_at": time.time(),
        })
        self._save(items)
        return True

    def add_from_text(self, text: str, *, from_user: str = "") -> int:
        """把一句话里所有可识别的内容加入列表，返回新增条数。"""
        added = 0
        for link in extract_links(text):
            if self.add(link["url"], platform=link["platform"], native_id=link["native_id"],
                        source="chat", from_user=from_user):
                added += 1
        return added

    def peek(self, limit: int = 5) -> list[dict]:
        """查看列表，不消费。"""
        return self._load()[:max(1, int(limit or 5))]

    def take(self, limit: int = 1) -> list[dict]:
        """取出最旧的若干条，并从列表里移除。

        取出来就当作「已经安排去看」，避免每次浏览都重复打开同一条。

        Args:
            limit: 最多取几条。

        Returns:
            取出的条目。
        """
        items = self._load()
        if not items:
            return []
        count = max(1, min(int(limit or 1), len(items)))
        taken, rest = items[:count], items[count:]
        self._save(rest)
        return taken

    def remove(self, url: str = "", title: str = "") -> bool:
        """把一条从列表里删掉（真的看过了才调用）。

        Args:
            url: 条目的链接；为空时按 ``title`` 匹配。
            title: 只有名字的条目用这个匹配。

        Returns:
            是否删掉了某条。
        """
        identity = str(url or "").strip() or (f"title:{title}" if title else "")
        if not identity:
            return False
        items = self._load()
        kept = []
        removed = False
        for item in items:
            current = str(item.get("url") or "") or f"title:{item.get('title') or ''}"
            if current == identity:
                removed = True
                continue
            kept.append(item)
        if removed:
            self._save(kept)
        return removed

    def size(self) -> int:
        """当前条数。"""
        return len(self._load())

    def clear(self) -> None:
        """清空列表。"""
        self._save([])

    def brief(self, limit: int = 5) -> str:
        """给状态命令用的一行摘要。"""
        items = self.peek(limit)
        if not items:
            return "想刷列表还是空的"
        return "\n".join(
            f"· {item.get('title') or item.get('url')}（{item.get('from_user') or '聊天里看到的'}）"
            for item in items
        )


def remember(plugin: Any) -> WatchList | None:
    """取插件上的列表实例，没初始化时返回 ``None``。"""
    return getattr(plugin, "watchlist", None)


def note_error(exc: Exception) -> None:
    """统一记一条调试日志，避免调用处重复写 try/except。"""
    logger.debug(f"[CommunityRadar] 想刷列表跳过（{type(exc).__name__}）")
