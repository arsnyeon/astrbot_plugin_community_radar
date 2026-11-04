#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""识别聊天里的社区链接，先替她看一眼，再把内容注入本轮对话上下文。

不发额外消息、不打断聊天；只是让模型在回复前「已经看过」用户分享的东西。
解析失败也会保留链接本身，不会让这一轮变成空上下文。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import httpx
from astrbot.api import logger

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

BILI = re.compile(
    r"https?://(?:www\.)?(?:bilibili\.com/video/[A-Za-z0-9]+|b23\.tv/[A-Za-z0-9]+)[^\s<>\"']*",
    re.I,
)
DOUYIN = re.compile(
    r"https?://(?:v\.douyin\.com/[A-Za-z0-9_-]+|(?:www\.)?douyin\.com/(?:video|note)/\d+"
    r"|(?:www\.)?iesdouyin\.com/share/(?:video|note)/\d+)[^\s<>\"']*",
    re.I,
)
HEYBOX = re.compile(
    r"https?://(?:www\.)?xiaoheihe\.cn/app/(?:bbs/link|link)/\d+[^\s<>\"']*",
    re.I,
)


def _find_item(obj: Any) -> dict | None:
    """从抖音分享页的 SSR 数据里找出视频条目（结构可能随版本变化）。"""
    if isinstance(obj, dict):
        items = obj.get("item_list")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
        for v in obj.values():
            hit = _find_item(v)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _find_item(v)
            if hit:
                return hit
    return None


class LinkPeek:
    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._cache: dict[str, tuple[float, str]] = {}
        self._ttl = 600.0

    def _cache_get(self, url: str) -> str | None:
        hit = self._cache.get(url)
        if hit and time.time() - hit[0] < self._ttl:
            return hit[1]
        return None

    def _cache_set(self, url: str, text: str) -> None:
        self._cache[url] = (time.time(), text)
        if len(self._cache) > 200:
            now = time.time()
            self._cache = {k: v for k, v in self._cache.items() if now - v[0] < self._ttl}

    async def peek(self, text: str, cfg: dict) -> str:
        platforms = str(cfg.get("platforms") or "all").lower()
        max_links = max(1, min(5, int(cfg.get("max_links") or 2)))
        found: list[tuple[str, str]] = []
        seen: set[str] = set()

        def collect(kind: str, pattern: re.Pattern, allowed: bool) -> None:
            if not allowed:
                return
            for m in pattern.finditer(text):
                url = m.group(0).rstrip("，。,.、)】]")
                if url not in seen:
                    seen.add(url)
                    found.append((kind, url))

        collect("bili", BILI, platforms in ("all", "bili"))
        collect("douyin", DOUYIN, platforms in ("all", "douyin"))
        collect("heybox", HEYBOX, platforms in ("all", "heybox"))
        if not found:
            return ""

        lines: list[str] = []
        for kind, url in found[:max_links]:
            cached = self._cache_get(url)
            if cached is None:
                try:
                    cached = await asyncio.wait_for(self._resolve(kind, url), timeout=7)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[CommunityRadar] 链接解析失败 {url}: {exc}")
                    cached = f"{self._label(kind)} {url}"
                self._cache_set(url, cached)
            lines.append("- " + cached)

        return (
            "【社区雷达 · 刚看过的内容】\n"
            "有人在聊天里分享了下面的链接，你已经看过了。像朋友那样自然接话："
            "可以聊内容、说看法、吐槽或追问，不要说「我查了一下」，也不必每条都提。\n"
            + "\n".join(lines)
        )

    @staticmethod
    def _label(kind: str) -> str:
        return {"bili": "B 站视频", "douyin": "抖音视频", "heybox": "小黑盒帖子"}.get(kind, "链接")

    async def _resolve(self, kind: str, url: str) -> str:
        if kind == "bili":
            return await self._bili(url)
        if kind == "douyin":
            return await self._douyin(url)
        return await self._heybox(url)

    async def _bili(self, url: str) -> str:
        from .bili import find_video_id

        vid = find_video_id(url) or {}
        if "short" in vid:
            # b23.tv 短链：跟一次跳转拿真实地址
            try:
                async with httpx.AsyncClient(timeout=5, follow_redirects=True, headers={"User-Agent": UA}) as c:
                    r = await c.get(url)
                vid = find_video_id(str(r.url)) or {}
            except Exception:  # noqa: BLE001
                pass
        bvid = str(vid.get("bvid") or "")
        aid = str(vid.get("aid") or "")
        if not bvid and not aid:
            return f"B 站视频 {url}"
        try:
            v = await self.plugin.bili.view(bvid, aid)
        except Exception:  # noqa: BLE001
            return f"B 站视频 {url}"
        desc = str(v.get("desc") or "").strip().replace("\n", " ")[:120]
        return (
            f"B 站《{v.get('title', '无标题')}》 UP:{v.get('author', '?')} "
            f"播放{v.get('play', '?')} 点赞{v.get('like', '?')}"
            + (f" 简介:{desc}" if desc else "")
            + f" https://www.bilibili.com/video/{v.get('bvid') or bvid}"
        )

    async def _douyin(self, url: str) -> str:
        from .douyin_browser import find_aweme_id

        vid = await find_aweme_id(url)
        link = f"https://www.douyin.com/video/{vid}" if vid else url
        if not vid:
            return f"抖音视频 {url}"
        item = await self._douyin_ssr(vid)
        if not item:
            return f"抖音视频 {link}"
        author = (item.get("author") or {}).get("nickname") or "?"
        desc = str(item.get("desc") or "").strip().replace("\n", " ")[:120]
        st = item.get("statistics") or {}
        return (
            f"抖音《{desc or '视频'}》 作者:{author} 赞{st.get('digg_count', '?')} "
            f"评论{st.get('comment_count', '?')} {link}"
        )

    @staticmethod
    async def _douyin_ssr(aweme_id: str) -> dict | None:
        """分享页的服务端渲染数据，无需登录与签名；拿不到就静默降级。"""
        try:
            async with httpx.AsyncClient(timeout=5, follow_redirects=True, headers={"User-Agent": UA}) as c:
                r = await c.get(f"https://www.iesdouyin.com/share/video/{aweme_id}/")
            m = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", r.text, re.S)
            if not m:
                return None
            return _find_item(json.loads(m.group(1)))
        except Exception:  # noqa: BLE001
            return None

    async def _heybox(self, url: str) -> str:
        m = re.search(r"/(?:bbs/)?link/(\d+)", url)
        link_id = m.group(1) if m else ""
        # 小黑盒帖子正文需要站内接口，这里不猜测端点，保留链接并交给对话中的工具处理
        return f"小黑盒帖子 {url}" + (f"（编号 {link_id}）" if link_id else "")
