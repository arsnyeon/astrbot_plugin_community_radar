"""读取内容下方的评论区。

对人来说，「看视频」与「翻评论区」是同一件事：看完顺便看看大家在说什么，
两者一起构成印象。本模块只负责收集评论，如何理解由观感环节决定。
"""
from __future__ import annotations

import re
from typing import Any

from astrbot.api import logger


def _clip(text: Any, limit: int = 120) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def normalize_comments(raw: list[dict] | None, limit: int = 8) -> list[dict]:
    """把三个平台各自返回的评论结构归一成同一种形态。"""
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        text = _clip(item.get("text") or item.get("content"))
        if not text or text in seen:
            continue
        seen.add(text)
        try:
            likes = int(item.get("like") or item.get("likes") or 0)
        except (TypeError, ValueError):
            likes = 0
        out.append({
            "name": _clip(item.get("name") or item.get("user"), 24) or "匿名",
            "text": text,
            "likes": likes,
            "replies": int(item.get("replies") or 0) if str(item.get("replies") or "").isdigit() else 0,
        })
    out.sort(key=lambda x: x["likes"], reverse=True)
    return out[:max(1, min(int(limit or 8), 20))]


async def collect(plugin: Any, result: dict, limit: int = 8) -> list[dict]:
    """取得已读完内容的评论；尽力而为，失败不影响这条内容的结论。"""
    content_id = str(result.get("content_id") or "")
    if not content_id:
        return []
    platform, _, native_id = content_id.partition(":")
    try:
        if platform == "bili":
            info = await plugin.bili.view(native_id)
            raw = await plugin.bili.comments(str(info.get("aid") or ""), limit=limit)
        elif platform == "douyin":
            if not (plugin.config.get("douyin") or {}).get("enabled"):
                return []
            async with plugin.browser_lock:
                try:
                    raw = await plugin.douyin_browser.comments(native_id, limit=limit)
                finally:
                    if (plugin.config.get("douyin") or {}).get("close_after_run", True):
                        await plugin.douyin_browser.close()
        elif platform == "heybox":
            detail = await plugin.heybox.detail(native_id, comment_limit=limit)
            raw = detail.get("comments") or []
        else:
            return []
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[CommunityRadar] 评论区没看成（{type(exc).__name__}）")
        return []
    return normalize_comments(raw, limit)


def digest_of(comments: list[dict]) -> str:
    """拼成一段可供提示词与记忆使用的简短文本。"""
    if not comments:
        return ""
    return "\n".join(
        f"{c['name']}（{c['likes']}赞）：{c['text']}" for c in comments[:8]
    )


def render(comments: list[dict] | None, limit: int = 5) -> list[str]:
    """把归一后的评论渲染成 ``作者：内容`` 形式的短句。

    提示词与日志里需要的是可读文本，而观感环节需要结构化的字段，两者不要在
    同一条数据上混用，否则很容易出现「字符串当字典读」的错误。

    Args:
        comments: :func:`normalize_comments` 返回的评论列表。
        limit: 最多渲染几条。

    Returns:
        形如 ``["甲：前排", "乙：太好笑了"]`` 的列表。
    """
    out: list[str] = []
    for item in comments or []:
        if not isinstance(item, dict):
            continue
        text = _clip(item.get("text"), 120)
        if not text:
            continue
        name = _clip(item.get("name"), 24) or "匿名"
        out.append(f"{name}：{text}")
        if len(out) >= max(1, int(limit or 5)):
            break
    return out
