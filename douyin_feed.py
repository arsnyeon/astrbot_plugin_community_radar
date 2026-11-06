#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抖音读层（零浏览器 / 零签名 / 零登录）。

只走抖音网页端公开的热榜接口。实测：
  /aweme/v1/web/hot/search/list/  -> 200 + 完整 JSON（免 a_bogus）
  /aweme/v1/web/tab/feed/        -> 200 + 空 body（必须有页面内签名）
  /aweme/v1/web/aweme/detail/    -> 403 ArgusSecurityPlugin
所以「看内容」放这里，「刷推荐流 / 点赞 / 评论」放 douyin_browser.py。
"""
from __future__ import annotations

import time
import urllib.parse
from typing import Any

import httpx
from astrbot.api import logger

HOT_API = "https://www.douyin.com/aweme/v1/web/hot/search/list/"
BASE_PARAMS = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "pc_client_type": "1",
    "version_code": "290100",
    "version_name": "29.1.0",
    "cookie_enabled": "true",
    "platform": "PC",
}
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def hot_url(sentence_id: str) -> str:
    return f"https://www.douyin.com/hot/{sentence_id}" if sentence_id else "https://www.douyin.com/hot"


def search_url(word: str) -> str:
    return "https://www.douyin.com/search/" + urllib.parse.quote(str(word or ""))


def _fmt_hot(value: Any) -> str:
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return "-"
    if n <= 0:
        return "-"  # 置顶词条没有热度值
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def _cover_url(item: dict) -> str:
    cover = item.get("word_cover") or {}
    urls = cover.get("url_list") or [] if isinstance(cover, dict) else []
    return str(urls[0]) if urls else ""


class DouyinFeed:
    """无状态热榜读取，带 5 分钟缓存，避免定时任务重复打接口。"""

    def __init__(self, timeout: float = 12.0):
        self._timeout = timeout
        self._cache: list[dict] = []
        self._cached_at = 0.0
        self._ttl = 300.0

    async def hot(self, limit: int = 10, force: bool = False) -> list[dict]:
        if not force and self._cache and time.time() - self._cached_at < self._ttl:
            return self._cache[:limit]
        params = dict(BASE_PARAMS)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"},
            ) as client:
                resp = await client.get(HOT_API, params=params)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            payload = resp.json()
            data = payload.get("data") or {}
            words = data.get("word_list") or []
            if not words:
                raise RuntimeError("热榜返回空 word_list（接口可能改版）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] 抖音热榜获取失败: {exc}")
            return self._cache[:limit] if self._cache else []

        items: list[dict] = []
        for w in words:
            if not isinstance(w, dict):
                continue
            word = str(w.get("word") or "").strip()
            if not word:
                continue
            sid = str(w.get("sentence_id") or "")
            label = str(w.get("label") or "").strip()
            if label.isdigit() or len(label) > 4:
                label = ""  # 数字是内部标签码，不是「热 / 新 / 爆」这类展示文字
            items.append({
                "rank": len(items) + 1,  # 按接口顺序编号；置顶项的 position 也是 1，直接用会重号
                "raw_position": int(w.get("position") or 0),
                "title": word,
                "hot": int(w.get("hot_value") or 0),
                "hot_text": _fmt_hot(w.get("hot_value")),
                "url": hot_url(sid),
                "search_url": search_url(word),
                "sentence_id": sid,
                "group_id": str(w.get("group_id") or ""),
                "label": label,
                "cover": _cover_url(w),
                "event_time": int(w.get("event_time") or 0),
                "source": "douyin_hot",
            })
        self._cache = items
        self._cached_at = time.time()
        return items[:limit]

    async def trending(self, limit: int = 5) -> list[dict]:
        """热榜接口附带的趋势词（可选补充）。失败静默返回空。"""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"},
            ) as client:
                resp = await client.get(HOT_API, params=BASE_PARAMS)
            if resp.status_code != 200:
                return []
            data = (resp.json().get("data") or {})
            out = []
            for t in (data.get("trending_list") or [])[:limit]:
                if isinstance(t, dict) and t.get("word"):
                    out.append({"title": str(t["word"]), "url": search_url(t["word"])})
            return out
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def lines(items: list[dict], prefix: str = "抖音热榜") -> list[str]:
        out = [f"{prefix}："]
        for x in items:
            label = f" [{x['label']}]" if x.get("label") else ""
            hot = x.get("hot_text") or ""
            hot_part = f" 热度{hot}" if hot not in ("", "-", "?") else ""
            out.append(f"· {x['rank']}. {x['title']}{label}{hot_part} {x['url']}")
        return out

    async def close(self) -> None:
        self._cache = []
