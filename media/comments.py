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


#: 明显是广告或引流的词。命中就不进上下文，免得把推广当成人话。
_AD_WORDS = (
    "加微", "加v", "加V", "私信我", "代练", "代刷", "刷单", "优惠券",
    "扫码", "点击链接", "下载app", "下载APP", "领红包", "日赚", "兼职",
)

#: 纯表情 / 纯标记 / 纯符号的评论没有信息量。
_EMPTY_LIKE = re.compile(
    r"^(?:[\s\W_]*|\[[^\]]{1,6}\]|哈{1,}|呵{1,}|草{1,}|6{2,}|[?？!！。.~～]{1,})$"
)


def is_low_value(text: str) -> bool:
    """判断一条评论是不是没有信息量。

    群里接话、评论区「别人怎么说」都靠这些文本，纯表情、纯 @、广告会把
    有限的上下文额度占满，因此先筛掉。

    Args:
        text: 已去空白的评论正文。

    Returns:
        是否应当丢弃。
    """
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(body) < 2:
        return True
    if _EMPTY_LIKE.match(body):
        return True
    # 纯 @ 某人
    if re.fullmatch(r"(?:@\S+\s*)+", body):
        return True
    if any(word in body.lower() for word in _AD_WORDS):
        return True
    # 只剩标点与表情，没有任何实义字符
    if not re.search(r"[\u4e00-\u9fa5A-Za-z0-9]", body):
        return True
    return False


def normalize_comments(raw: list[dict] | None, limit: int = 8) -> list[dict]:
    """把三个平台各自返回的评论结构归一成同一种形态。

    低质评论（纯表情、纯 @、广告、超短）在这里直接丢掉，并统计数量便于排查：
    有时「评论区没人说话」其实是被过滤干净了。

    Args:
        raw: 各平台原始评论。
        limit: 最多保留几条。

    Returns:
        按点赞降序的评论列表。
    """
    out: list[dict] = []
    seen: set[str] = set()
    dropped = 0
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        text = _clip(item.get("text") or item.get("content"))
        if not text or text in seen:
            continue
        if is_low_value(text):
            dropped += 1
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
    if dropped:
        logger.debug(f"[CommunityRadar] 过滤掉 {dropped} 条无信息量评论")
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
