"""把长期积累的知识同步进 AstrBot 知识库。

本插件自己的知识条目是**关键词触发**的（类似世界书）：聊天里提到才注入。
AstrBot 知识库则是**向量检索**：切块后由 RAG 召回。两者互补，因此这里把知识
条目汇总成一份文档投进指定的知识库，让普通聊天也能检索到。

同步策略是「整份替换」：知识库里的这份文档始终是当前全量快照，避免逐条上传
产生大量碎片文档。内容未变化时跳过，不重复生成向量。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from astrbot.api import logger

#: 知识库文档的固定名称；同一个知识库里只会存在一份。
DOC_NAME = "社区雷达·社区见闻.txt"
#: 单次同步进文档的最大条目数，避免文档过长导致切块与嵌入耗时失控。
MAX_ITEMS = 200
#: 文档正文上限字符数。
MAX_CHARS = 12000


def _render(book) -> str:
    """把知识条目渲染成一份可分块的中文文档。

    Args:
        book: :class:`core.learning.KnowledgeBook` 实例。

    Returns:
        文档正文；没有条目时返回空串。
    """
    labels = {"knowledge": "知识", "meme": "梗与说法", "term": "名词解释"}
    items = book.recent(MAX_ITEMS)
    if not items:
        return ""
    grouped: dict[str, list[dict]] = {}
    for item in items:
        grouped.setdefault(str(item.get("kind") or "knowledge"), []).append(item)

    lines = [
        "# 社区雷达 · 社区见闻",
        "",
        "以下是机器人在 B 站、小黑盒、贴吧等社区浏览时自己记下的东西，"
        "包含知识点、专有名词与流行梗。回答时可以自然引用，但不确定就不要硬套。",
        "",
    ]
    for kind in ("knowledge", "term", "meme"):
        group = grouped.get(kind) or []
        if not group:
            continue
        lines.append(f"## {labels.get(kind, kind)}")
        for item in group:
            body = str(item.get("body") or "").strip()
            if not body:
                continue
            topic = str(item.get("topic") or "").strip()
            source = str(item.get("title") or "").strip()
            suffix = ""
            if topic:
                suffix += f"（关于{topic}"
                suffix += f"，来自《{source[:30]}》）" if source else "）"
            elif source:
                suffix += f"（来自《{source[:30]}》）"
            lines.append(f"- {body}{suffix}")
        lines.append("")
    return "\n".join(lines)[:MAX_CHARS]


async def sync(plugin: Any) -> bool:
    """把当前知识条目同步到配置的知识库。

    该函数尽力而为：知识库未配置、未就绪或嵌入失败都只记日志，不影响浏览流程。

    Args:
        plugin: 插件实例，需提供 ``config``、``knowledge``、``radar_store`` 与 ``context``。

    Returns:
        是否真的写入（内容无变化或被跳过时返回 ``False``）。
    """
    cfg = plugin.config.get("learning") or {}
    kb_name = str(cfg.get("kb_name") or "").strip()
    if not kb_name:
        return False
    book = getattr(plugin, "knowledge", None)
    if book is None:
        return False
    text = _render(book)
    if not text:
        return False

    store = getattr(plugin, "radar_store", None)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    previous = store.get("kb_sync", {}) if store is not None else {}
    if isinstance(previous, dict) and previous.get("digest") == digest:
        return False

    manager = getattr(getattr(plugin, "context", None), "kb_manager", None)
    if manager is None:
        logger.debug("[CommunityRadar] 当前 AstrBot 未提供知识库管理器，跳过同步")
        return False
    try:
        helper = await manager.get_kb_by_name(kb_name)
    except Exception as exc:  # noqa: BLE001 - 知识库不可用时静默降级
        logger.debug(f"[CommunityRadar] 读取知识库「{kb_name}」失败（{type(exc).__name__}）")
        return False
    if helper is None:
        logger.warning(f"[CommunityRadar] 找不到知识库「{kb_name}」，本轮不同步")
        return False

    # 先删掉上一次的文档，保证知识库里的这份始终是全量快照。
    try:
        for doc in await helper.list_documents(0, 200, search=DOC_NAME):
            name = str(getattr(doc, "doc_name", "") or getattr(doc, "name", "") or "")
            if name == DOC_NAME:
                await helper.delete_document(getattr(doc, "doc_id", ""))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[CommunityRadar] 清理旧知识库文档失败（{type(exc).__name__}）")

    chunks = [section for section in text.split("\n## ") if section.strip()]
    try:
        await helper.upload_document(
            DOC_NAME, text.encode("utf-8"), "txt",
            pre_chunked_text=chunks or None,
        )
    except Exception as exc:  # noqa: BLE001 - 嵌入服务缺失时不应影响浏览
        logger.warning(f"[CommunityRadar] 同步知识库失败（{type(exc).__name__}）")
        return False

    if store is not None:
        store.set("kb_sync", {"digest": digest, "at": time.time(), "chars": len(text)})
    logger.info("[CommunityRadar] 已把社区见闻同步到知识库「%s」", kb_name)
    return True
