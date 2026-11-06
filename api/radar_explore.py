"""Read-only evidence lookup for other plugins.

Contract: no writes, no model calls, no messages, no browser automation.
Callers get titles, descriptions and links; they decide what to do next.
"""
from __future__ import annotations

import asyncio
from typing import Any
from ..clients.adapters import read_registry


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

        registry = read_registry(self.plugin)

        async def search_platform(name):
            try:
                adapter = registry.get(name, 'search')
                found = await adapter.search(topic, limit=limit)
                return [{'platform': item.platform, 'title': item.title[:120],
                         'url': item.url, 'author': item.author[:40], 'desc': item.text[:240],
                         'image': item.media[0].source if item.media else '',
                         'limitations': item.limitations} for item in found]
            except Exception as exc:
                errors.append(f'{name}:{type(exc).__name__}')
                return []

        try:
            groups = await asyncio.wait_for(
                asyncio.gather(search_platform('bili'), search_platform('heybox'), return_exceptions=False),
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
