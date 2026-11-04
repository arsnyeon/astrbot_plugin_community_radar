#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定时浏览 B 站 / 小黑盒，并把看到的内容推给主人（或指定会话）。"""
from __future__ import annotations

import asyncio
import datetime
from typing import Any

from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain


class BrowseDuty:
    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._task: asyncio.Task | None = None
        self._done: dict[str, set[str]] = {}

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
                logger.warning(f"[BiliHeybox] 定时浏览异常: {exc}")

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

    async def _run(self, hhmm: str, cfg: dict) -> None:
        platforms = str(cfg.get("platforms") or "both").lower()
        count = max(1, int(cfg.get("count") or 5))
        mode = str(cfg.get("mode") or "summary").lower()

        bili_items: list[dict] = []
        heybox_items: list[dict] = []

        if platforms in ("bili", "both"):
            try:
                bili_items = await self.plugin.bili.popular(count)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[BiliHeybox] 拉 B 站热门失败: {exc}")
        if platforms in ("heybox", "both"):
            try:
                heybox_items = await self.plugin.heybox.feed(0)[:count]
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[BiliHeybox] 拉小黑盒信息流失败: {exc}")

        if not bili_items and not heybox_items:
            return

        if mode == "summary":
            text = await self._summarize(bili_items, heybox_items, cfg)
        else:
            text = self._links(bili_items, heybox_items)

        if not text:
            return
        text = f"【{hhmm} 定时浏览】\n{text}"
        await self._send(text, cfg)

    def _links(self, bili: list[dict], heybox: list[dict]) -> str:
        lines = []
        if bili:
            lines.append("B 站热门：")
            for v in bili:
                lines.append(f"· {v['title']} https://www.bilibili.com/video/{v['bvid']}")
        if heybox:
            lines.append("小黑盒信息流：")
            for x in heybox:
                lines.append(f"· {x['title']} https://www.xiaoheihe.cn/app/bbs/link/{x['link_id']}")
        return "\n".join(lines)

    async def _summarize(self, bili: list[dict], heybox: list[dict], cfg: dict) -> str:
        links = self._links(bili, heybox)
        if not links:
            return ""
        prompt = (
            "下面是定时抓取的 B 站热门和小黑盒信息流。请用大肥鱼的口吻，"
            "挑 3~5 条你觉得有意思的，每条用一句话点评（可吐槽、可玩梗），"
            "点评后面跟上原文链接。不要长篇大论，不要逐条复述。\n\n" + links
        )
        system_prompt = (
            "你是「大肥鱼」：蓝发小鲸鱼娘，18 岁，说话口语化、爱玩梗、偶尔自嘲，"
            "不列点、不写小作文。"
        )
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
            logger.warning(f"[BiliHeybox] 点评生成失败，退回纯链接: {exc}")
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
        chain = MessageChain([Plain(text)])
        for umo in targets:
            try:
                await self.plugin.context.send_message(umo, chain)
                logger.info(f"[BiliHeybox] 已推送定时浏览到 {umo}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[BiliHeybox] 推送到 {umo} 失败: {exc}")
