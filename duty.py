#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定时浏览 B 站 / 小黑盒 / 抖音：按兴趣主动搜索、带自己的想法点评、推给主人。

设计目标是「她自己在逛社区」，而不是「把榜单贴一遍」：
- 除热门 / 信息流 / 热榜外，会挑感兴趣的关键词主动搜索；
- 点评由模型按自己的偏好决定聊哪几条、为什么在意，允许整段跳过没兴趣的；
- 记住最近看过什么，下一次不重复聊同样的角度。
"""
from __future__ import annotations

import asyncio
import datetime
import json
import random
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

DEFAULT_PERSONA = (
    "你是一个爱逛社区、有自己偏好和判断的人。看到对胃口的会多聊两句，"
    "没感觉的就跳过；会吐槽、会联想，但不会硬凑热闹。"
)


class BrowseDuty:
    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._task: asyncio.Task | None = None
        self._done: dict[str, set[str]] = {}
        self._history_file = self._data_dir() / "browse_history.json"

    @staticmethod
    def _data_dir() -> Path:
        try:
            from astrbot.api.star import StarTools
            return Path(StarTools.get_data_dir("astrbot_plugin_community_radar"))
        except Exception:  # noqa: BLE001
            return Path("/AstrBot/data/plugin_data/astrbot_plugin_community_radar")

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[CommunityRadar] 定时浏览异常: {exc}")

    async def _tick(self) -> None:
        cfg = self.plugin.config.get("browse") or {}
        if not cfg.get("enable"):
            return
        now = datetime.datetime.now()
        hhmm = now.strftime("%H:%M")
        today = now.strftime("%Y-%m-%d")
        times = [str(t).strip() for t in (cfg.get("times") or []) if str(t).strip()]
        if hhmm not in times:
            return
        if self._in_quiet_hours(cfg.get("quiet_hours", "1-6"), now):
            return
        done = self._done.setdefault(today, set())
        if hhmm in done:
            return
        done.add(hhmm)
        await self._run(hhmm, cfg)

    @staticmethod
    def _in_quiet_hours(quiet: str, now: datetime.datetime) -> bool:
        try:
            start, end = (int(x) for x in str(quiet).split("-"))
        except Exception:  # noqa: BLE001
            return False
        h = now.hour
        if start <= end:
            return start <= h < end
        return h >= start or h < end  # 跨天，例如 23-6

    # ── 浏览历史 ────────────────────────────────────────────────

    def _read_history(self) -> list[dict]:
        try:
            data = json.loads(self._history_file.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            return []

    def _record_history(self, groups: dict[str, list[dict]]) -> None:
        entries = []
        for platform, items in groups.items():
            for x in items[:5]:
                title = str(x.get("title") or "").strip()
                if title:
                    entries.append({"at": int(time.time()), "platform": platform, "title": title[:80]})
        if not entries:
            return
        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            merged = entries + self._read_history()
            self._history_file.write_text(
                json.dumps(merged[:60], ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] 浏览历史写入失败: {exc}")

    def _recent_history(self, limit: int = 6) -> str:
        seen: set[str] = set()
        lines = []
        for h in self._read_history():
            title = str(h.get("title") or "")
            if not title or title in seen:
                continue
            seen.add(title)
            lines.append(f"- [{h.get('platform', '?')}] {title}")
            if len(lines) >= limit:
                break
        return "\n".join(lines)

    # ── 一次浏览 ────────────────────────────────────────────────

    async def _run(self, hhmm: str, cfg: dict) -> None:
        platforms = str(cfg.get("platforms") or "all").lower()
        count = max(1, int(cfg.get("count") or 5))
        mode = str(cfg.get("mode") or "summary").lower()
        interests = [str(x).strip() for x in (cfg.get("interests") or []) if str(x).strip()]
        per_run = max(0, int(cfg.get("search_per_run") or 2))
        picks = random.sample(interests, min(per_run, len(interests))) if interests and per_run else []

        bili_items: list[dict] = []
        heybox_items: list[dict] = []
        douyin_items: list[dict] = []

        if platforms in ("bili", "both", "all"):
            try:
                bili_items = await self.plugin.bili.popular(count)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[CommunityRadar] 拉 B 站热门失败: {exc}")
            bili_items = await self._search(self.plugin.bili, "search", picks, bili_items, count)

        if platforms in ("heybox", "both", "all"):
            try:
                heybox_items = (await self.plugin.heybox.feed(0))[:count]
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[CommunityRadar] 拉小黑盒信息流失败: {exc}")
            heybox_items = await self._search(self.plugin.heybox, "search", picks, heybox_items, count)

        if platforms in ("douyin", "all") and getattr(self.plugin, "douyin_feed", None):
            try:
                douyin_items = await self.plugin.douyin_feed.hot(count)
                if interests:
                    for x in douyin_items:
                        if any(k in x["title"] for k in interests):
                            x["_via"] = "合口味"
                    douyin_items.sort(key=lambda x: (x.get("_via") != "合口味", x.get("rank", 99)))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[CommunityRadar] 拉抖音热榜失败: {exc}")

        if not bili_items and not heybox_items and not douyin_items:
            return

        self._record_history({"B站": bili_items, "小黑盒": heybox_items, "抖音": douyin_items})
        if mode == "summary":
            text = await self._summarize(bili_items, heybox_items, cfg, douyin_items, interests)
        else:
            text = self._links(bili_items, heybox_items, douyin_items)

        if not text:
            return
        text = f"【{hhmm} 逛社区】\n{text}"
        await self._send(text, cfg)

    @staticmethod
    async def _search(client: Any, method: str, picks: list[str], base: list[dict], count: int) -> list[dict]:
        """按兴趣主动搜索，结果并入热门；同平台内按标识去重。"""
        if not picks or not hasattr(client, method):
            return base
        out = list(base)
        seen = {str(x.get("bvid") or x.get("link_id") or x.get("title") or "") for x in out}
        take = max(2, count // 2)
        for kw in picks:
            try:
                found = await getattr(client, method)(kw)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[CommunityRadar] 按兴趣搜索「{kw}」失败: {exc}")
                continue
            for item in (found or [])[:take]:
                key = str(item.get("bvid") or item.get("link_id") or item.get("title") or "")
                if key and key in seen:
                    continue
                seen.add(key)
                item = dict(item)
                item["_via"] = f"搜「{kw}」"
                out.append(item)
        return out

    def _links(self, bili: list[dict], heybox: list[dict], douyin: list[dict] | None = None) -> str:
        def via(x: dict) -> str:
            return f" [{x['_via']}]" if x.get("_via") else ""

        lines = []
        if bili:
            lines.append("B 站：")
            for v in bili:
                lines.append(f"· {v['title']}{via(v)} https://www.bilibili.com/video/{v['bvid']}")
        if heybox:
            lines.append("小黑盒：")
            for x in heybox:
                lines.append(f"· {x['title']}{via(x)} https://www.xiaoheihe.cn/app/bbs/link/{x['link_id']}")
        if douyin:
            lines.append("抖音热榜：")
            for x in douyin:
                hot = x.get("hot_text") or ""
                hot_part = f" 热度{hot}" if hot not in ("", "-", "?") else ""
                lines.append(f"· {x['rank']}. {x['title']}{via(x)}{hot_part} {x['url']}")
        return "\n".join(lines)

    async def _summarize(self, bili: list[dict], heybox: list[dict], cfg: dict,
                         douyin: list[dict] | None = None,
                         interests: list[str] | None = None) -> str:
        links = self._links(bili, heybox, douyin)
        if not links:
            return ""
        persona = str(cfg.get("persona") or "").strip() or DEFAULT_PERSONA
        interest_line = ("你最近比较在意的：" + "、".join(interests or []) + "。") if interests else ""
        history = self._recent_history(6)
        history_line = ("\n你之前看过这些，别用同样的角度再聊一遍：\n" + history + "\n") if history else ""
        prompt = (
            "【这不是任务清单，是你自己刚逛完社区的收获】\n"
            f"{persona}\n{interest_line}\n"
            "下面这些是刚刷到的内容。像跟朋友聊天那样，只挑真正让你有反应的几条说说：\n"
            "- 讲讲为什么在意、让你想到什么、或者想吐槽什么，不要逐条复述，不要列清单\n"
            "- 没感觉的可以完全跳过，不用凑数，宁可只说一两条\n"
            "- 提到哪条就把它后面的链接带上，方便对方点开看\n"
            "- 别写「今日精选」「以下是」这类开场白，也别总结陈词\n"
            f"{history_line}\n内容：\n{links}"
        )
        system_prompt = "你在跟朋友聊自己刚刷到的东西。口语化、自然、有个人好恶，不写小作文。"
        try:
            provider = await self._resolve_provider(cfg)
            if provider is None:
                return links
            resp = await provider.text_chat(
                prompt=prompt,
                session_id=self._default_target(cfg),
                system_prompt=system_prompt,
                contexts=[],
            )
            text = getattr(resp, "completion_text", "") or ""
            return text.strip() or links
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] 点评生成失败，退回纯链接: {exc}")
            return links

    async def _resolve_provider(self, cfg: dict) -> Any:
        provider_id = str(cfg.get("ai_provider_id") or "").strip()
        ctx = self.plugin.context
        if provider_id:
            return ctx.get_provider_by_id(provider_id)
        umo = self._default_target(cfg)
        try:
            return await ctx.get_using_provider_async(umo)
        except Exception:  # noqa: BLE001
            return ctx.get_using_provider(umo)

    def _default_target(self, cfg: dict) -> str:
        targets = [str(t).strip() for t in (cfg.get("targets") or []) if str(t).strip()]
        if targets:
            return targets[0]
        admin = str(self.plugin.config.get("admin_uin") or "").strip()
        if not admin:
            return ""
        platform_name = self._platform_name()
        return f"{platform_name}:FriendMessage:{admin}"

    def _platform_name(self) -> str:
        try:
            pm = self.plugin.context.platform_manager
            insts = pm.get_insts() if hasattr(pm, "get_insts") else getattr(pm, "platform_insts", [])
            for inst in insts or []:
                try:
                    pid = inst.meta().id
                    if pid and "webchat" not in str(pid).lower():
                        return str(pid)
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass
        return "default"

    async def _send(self, text: str, cfg: dict) -> None:
        targets = [str(t).strip() for t in (cfg.get("targets") or []) if str(t).strip()]
        if not targets:
            targets = [self._default_target(cfg)]
        for umo in targets:
            if not umo or umo.endswith(":"):
                logger.warning("[CommunityRadar] 未配置「主人账号」或「推送目标」，跳过本次推送")
                continue
            try:
                await self.plugin.context.send_message(umo, MessageChain([Plain(text)]))
                logger.info(f"[CommunityRadar] 已推送定时浏览到 {umo}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[CommunityRadar] 推送到 {umo} 失败: {exc}")
