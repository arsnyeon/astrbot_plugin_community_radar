#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""话题监听：从群聊 / 私聊里捡出「她自己也想去看看」的东西。

设计意图不是「记录所有消息」，而是回答一个问题：
    刚才谁聊到了什么，值得我待会儿自己去搜一下？

做法：
1. 每条消息先做一次极轻的本地预筛（长度、命令、纯表情、链接已在别处处理），
   把明显不值得跟的挡掉，避免每条都去问模型；
2. 剩下的按会话聚成小批，攒够几条或到时间再问一次模型「这里有没有你想去看的话题」；
3. 模型只负责判断「值不值得搜」，输出 JSON；关键词、平台倾向、急不急都由它给；
4. 结果进一个带过期时间的队列，浏览时优先消费最新的、没过期的。

副作用要可控：同一个话题反复聊不会重复入队（去重靠标题近似 + 关键词集合）。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

# 这些开头基本都是指令，没必要当话题
_CMD_PREFIX = ("/", "／", "!", "！", "#", "＃")

# 纯寒暄 / 纯表情 / 单字回应，问模型也没意义
_NOISE = re.compile(
    r"^(?:[\s\W_]*|哈+|呵+|嘿+|嗯+|哦+|噢+|额+|呃+|啊+|在吗|在不在|你好|您好|早上好|晚上好|晚安|早安|"
    r"收到|好的|好|行|可以|没事|谢谢|感谢|拜拜|再见|笑死|草|6|666|？|\?)+$"
)

# 出现这些词说明在聊「某个具体的东西」，更可能是想搜的话题
_TOPIC_HINT = re.compile(
    r"(游戏|番|动画|漫画|电影|剧|视频|up主|主播|直播|显卡|cpu|笔记本|手机|相机|耳机|键盘|"
    r"显卡|装机|驱动|系统|软件|工具|插件|模型|ai|跑分|涨价|降价|发布|新作|更新|版本|"
    r"比赛|赛事|战队|选手|冠军|版本|攻略|抽卡|卡池|皮肤|联动|活动|演唱会|专辑|新歌|"
    r"手办|周边|谷子|展会|漫展|综艺|纪录片|解说|测评|评测|教程|踩坑|翻车|瓜|塌房|"
    r"股价|基金|币圈|裁员|面试|offer|考研|论文|毕业|宿舍|外卖|好吃|探店)",
    re.I,
)


def _looks_like_topic(text: str, min_len: int, max_len: int) -> bool:
    s = str(text or "").strip()
    if not s or len(s) < min_len or len(s) > max_len:
        return False
    if s.startswith(_CMD_PREFIX):
        return False
    if _NOISE.match(s):
        return False
    # 至少要有一个非表情、非纯标点的实义字符
    if not re.search(r"[\u4e00-\u9fa5A-Za-z0-9]", s):
        return False
    # 只有话题提示词命中，或句子够长（有描述性内容）才留下
    return bool(_TOPIC_HINT.search(s)) or len(s) >= max(12, min_len + 6)


def _norm(text: str) -> str:
    """归一化用于去重：去空白、标点、大小写。"""
    return re.sub(r"[\s\W_]+", "", str(text or "").lower())


def _similar(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 6 and (a in b or b in a):
        return True
    # 字面重合度
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return False
    return len(sa & sb) / max(1, len(sa | sb)) >= 0.7


class TopicWatch:
    """把群聊/私聊里值得跟的话题攒成待搜队列。"""

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._buffer: dict[str, list[dict]] = {}
        self._buf_at: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._file = self._data_dir() / "topic_queue.json"
        self._last_scan: dict[str, float] = {}

    @staticmethod
    def _data_dir() -> Path:
        try:
            from astrbot.api.star import StarTools

            return Path(StarTools.get_data_dir("astrbot_plugin_community_radar"))
        except Exception:  # noqa: BLE001
            return Path("/AstrBot/data/plugin_data/astrbot_plugin_community_radar")

    # ── 队列持久化 ──────────────────────────────────────────────

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            return []

    def _save(self, items: list[dict]) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(
                json.dumps(items[:80], ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] 话题队列写入失败: {exc}")

    def queue(self, limit: int = 8) -> list[dict]:
        """取还没过期的待搜话题，最新的在前。"""
        cfg = self.plugin.config.get("topic") or {}
        ttl = max(600, int(cfg.get("ttl_hours") or 12) * 3600)
        now = time.time()
        items = [x for x in self._load() if now - float(x.get("at") or 0) < ttl]
        items.sort(key=lambda x: float(x.get("at") or 0), reverse=True)
        return items[:limit]

    def add(self, entry: dict) -> bool:
        """入队；和已有话题太像就不重复加。"""
        kw = str(entry.get("keyword") or "").strip()
        if not kw:
            return False
        items = self._load()
        for old in items:
            if _similar(str(old.get("keyword") or ""), kw):
                # 老话题再被提起，只刷新时间与热度，不重复堆
                old["at"] = float(entry.get("at") or time.time())
                old["hits"] = int(old.get("hits") or 1) + 1
                self._save(items)
                return False
        items.insert(0, entry)
        self._save(items)
        return True

    def consume(self, keywords: list[str]) -> None:
        """被这次浏览用掉了，从队列里划掉。"""
        if not keywords:
            return
        items = self._load()
        keep = [
            x for x in items
            if not any(_similar(str(x.get("keyword") or ""), k) for k in keywords)
        ]
        if len(keep) != len(items):
            self._save(keep)

    # ── 消息入口 ────────────────────────────────────────────────

    async def observe(self, event: Any) -> None:
        """每条消息都会走这里；绝大部分会在本地被挡掉，不产生模型调用。"""
        cfg = self.plugin.config.get("topic") or {}
        if not cfg.get("enable", True):
            return
        try:
            text = str(getattr(event, "message_str", "") or "").strip()
        except Exception:  # noqa: BLE001
            return
        if not _looks_like_topic(
            text,
            int(cfg.get("min_chars") or 6),
            int(cfg.get("max_chars") or 120),
        ):
            return

        umo = ""
        try:
            umo = str(event.unified_msg_origin or "")
        except Exception:  # noqa: BLE001
            umo = ""
        if not umo:
            return
        if umo in self._ignored(cfg):
            return

        # 同会话按批攒，够条数或够时间才问模型
        batch_size = max(2, int(cfg.get("batch_size") or 4))
        async with self._lock:
            buf = self._buffer.setdefault(umo, [])
            buf.append({"text": text[:200], "at": time.time()})
            if len(buf) > batch_size * 2:
                del buf[:-batch_size * 2]
            if len(buf) < batch_size:
                return
            batch, self._buffer[umo] = buf, []
        try:
            await self._scan(umo, batch, cfg)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[CommunityRadar] 话题扫描跳过: {exc}")

    async def flush(self, cfg: dict | None = None) -> None:
        """把还压着的半批也送出去扫一次（浏览前调用）。"""
        cfg = cfg or (self.plugin.config.get("topic") or {})
        async with self._lock:
            pending = {k: v for k, v in self._buffer.items() if v}
            self._buffer = {}
        for umo, batch in pending.items():
            try:
                await self._scan(umo, batch, cfg)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[CommunityRadar] 话题补扫跳过: {exc}")

    @staticmethod
    def _ignored(cfg: dict) -> set[str]:
        raw = cfg.get("ignore_sessions") or []
        return {str(x).strip() for x in raw if str(x).strip()}

    # ── 让模型判断值不值得跟 ────────────────────────────────────

    async def _scan(self, umo: str, batch: list[dict], cfg: dict) -> None:
        cooldown = max(0, int(cfg.get("scan_cooldown_sec") or 180))
        now = time.time()
        if now - self._last_scan.get(umo, 0) < cooldown:
            # 冷却中就把这批放回去，下一轮一起扫
            async with self._lock:
                merged = batch + self._buffer.get(umo, [])
                self._buffer[umo] = merged[-20:]
            return
        self._last_scan[umo] = now

        provider = await self._provider(cfg)
        if provider is None:
            return
        persona = ""
        try:
            persona = await self.plugin.duty._bot_persona(self.plugin.config.get("browse") or {})
        except Exception:  # noqa: BLE001
            persona = ""

        lines = "\n".join(f"- {x['text']}" for x in batch if x.get("text"))
        if not lines.strip():
            return
        prompt = (
            "下面是群里/私聊里刚出现的几句话。\n"
            + (f"你的人格：\n{persona[:700]}\n" if persona else "")
            + f"对话片段：\n{lines}\n\n"
            "问你自己：这里面有没有哪件事，是你真的会想自己去搜一下、看看最新进展的？\n"
            "要求：\n"
            "- 只挑你真的感兴趣、且有明确可搜对象的（作品名、游戏、硬件、赛事、事件、梗都行）\n"
            "- 拿不准、只是寒暄、或者搜了也没内容的，就不要\n"
            "- 最多 2 条，宁缺毋滥\n"
            '只输出 JSON 数组，元素形如 {"keyword":"要搜的词","why":"一句话说明你为什么想看","platform":"bili|heybox|douyin|any"}\n'
            "没有就输出 []，不要任何解释。"
        )
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                session_id=umo,
                contexts=[],
                system_prompt="你只输出 JSON 数组，不输出其他任何文字。",
            )
            raw = getattr(resp, "completion_text", "") or ""
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[CommunityRadar] 话题判断失败: {exc}")
            return

        for entry in self._parse(raw):
            entry["at"] = time.time()
            entry["session"] = umo
            entry["hits"] = 1
            if self.add(entry):
                logger.info(
                    f"[CommunityRadar] 记下一个想去看的话题：{entry['keyword']}"
                    f"（{entry.get('platform') or 'any'}）"
                )

    @staticmethod
    def _parse(raw: str) -> list[dict]:
        s = str(raw or "")
        m = re.search(r"\[[\s\S]*\]", s)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return []
        out: list[dict] = []
        if not isinstance(data, list):
            return out
        for x in data:
            if not isinstance(x, dict):
                continue
            kw = str(x.get("keyword") or "").strip()[:24]
            if not kw:
                continue
            plat = str(x.get("platform") or "any").strip().lower()
            if plat not in ("bili", "heybox", "douyin", "any"):
                plat = "any"
            out.append({
                "keyword": kw,
                "why": str(x.get("why") or "").strip()[:120],
                "platform": plat,
            })
        return out[:2]

    async def _provider(self, cfg: dict) -> Any:
        pid = str(cfg.get("ai_provider_id") or "").strip()
        ctx = self.plugin.context
        try:
            if pid:
                return ctx.get_provider_by_id(pid)
            return await ctx.get_using_provider_async("")
        except Exception:  # noqa: BLE001
            try:
                return ctx.get_using_provider("")
            except Exception:  # noqa: BLE001
                return None

    # ── 给浏览用的一句话摘要 ────────────────────────────────────

    def brief(self, limit: int = 4) -> str:
        items = self.queue(limit)
        if not items:
            return ""
        lines = []
        for x in items:
            why = str(x.get("why") or "").strip()
            plat = str(x.get("platform") or "any")
            lines.append(
                f"- {x.get('keyword')}"
                + (f"（{plat}）" if plat != "any" else "")
                + (f"：{why}" if why else "")
            )
        return "\n".join(lines)
