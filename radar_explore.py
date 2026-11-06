"""Read-only evidence lookup for other plugins.

Contract: no writes, no model calls, no messages, no browser automation.
Callers get titles, descriptions and links; they decide what to do next.
"""
from __future__ import annotations

import asyncio
from typing import Any


def _bili_item(v: dict) -> dict:
    bvid = str(v.get("bvid") or "")
    return {
        "platform": "bili",
        "title": str(v.get("title") or "")[:120],
        "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
        "author": str(v.get("author") or "")[:40],
        "desc": str(v.get("desc") or "")[:240],
        "image": str(v.get("pic") or ""),
    }


def _heybox_item(v: dict) -> dict:
    link_id = str(v.get("link_id") or "")
    images = v.get("images") or []
    return {
        "platform": "heybox",
        "title": str(v.get("title") or "")[:120],
        "url": f"https://www.xiaoheihe.cn/app/bbs/link/{link_id}" if link_id else "",
        "author": "",
        "desc": str(v.get("desc") or "")[:240],
        "image": str(images[0]) if images else "",
    }


class RadarExploreAPI:
    """Small, bounded, read-only facade used by other plugins."""

    def __init__(self, plugin: Any):
        self.plugin = plugin

    async def explore(self, topic: str, limit: int = 3, timeout: float = 25.0) -> dict:
        topic = str(topic or "").strip()[:80]
        if not topic:
            return {"ok": False, "error": "empty_topic", "topic": "", "items": []}
        limit = max(1, min(int(limit or 3), 5))
        errors: list[str] = []

        async def bili() -> list[dict]:
            try:
                found = await self.plugin.bili.search(topic)
                return [_bili_item(v) for v in (found or [])[:limit] if v.get("bvid")]
            except Exception as exc:  # noqa: BLE001
                errors.append(f"bili:{type(exc).__name__}")
                return []

        async def heybox() -> list[dict]:
            try:
                found = await self.plugin.heybox.search(topic, limit=limit)
                return [_heybox_item(v) for v in (found or []) if v.get("link_id")]
            except Exception as exc:  # noqa: BLE001
                errors.append(f"heybox:{type(exc).__name__}")
                return []

        try:
            groups = await asyncio.wait_for(
                asyncio.gather(bili(), heybox(), return_exceptions=False),
                timeout=max(5.0, float(timeout)),
            )
        except asyncio.TimeoutError:
            return {"ok": False, "error": "timeout", "topic": topic, "items": []}

        items = [x for group in groups for x in group][: limit * 2]
        return {
            "ok": bool(items),
            "error": "" if items else "no_result",
            "topic": topic,
            "items": items,
            "errors": errors,
        }
