#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""社区雷达：B 站 / 小黑盒 / 抖音浏览与互动插件。

- B 站：搜索、热门、视频信息、评论（读免登录）；发评论 / 点赞（需 Cookie）。
- 小黑盒：信息流、搜索（读免登录）；评论 / 点赞 / 发帖（需 Cookie）。
- 抖音热榜：官方公开接口，零浏览器 / 零签名 / 零登录。
- 抖音推荐流与互动：按需拉起的有头浏览器（Xvfb + Chromium + CDP），
  页面内 fetch 免算 a_bogus，支持点赞 / 评论 / 分享，带限额与风控闸门。
- 定时浏览：按兴趣关键词主动搜索，按设定时间浏览三个社区并聊给主人听。
- 分享链接接话：有人发社区链接时先看过内容，再让模型自然接话。
"""
from __future__ import annotations

import asyncio
from .registration import entry_handler
import json
import re
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr

from ..clients.bili import BiliClient, BiliError, find_video_id
from ..services.bili_duty import BiliDuty
from ..services.duty import BrowseDuty
from ..browser.douyin_browser import DouyinBrowser
from ..services.douyin_duty import DouyinDuty
from ..clients.douyin_feed import DouyinFeed
from ..clients.heybox import HeyboxClient, HeyboxError
from ..media.link_peek import LinkPeek
from ..services.topic_watch import TopicWatch
from ..media.video_watch import VideoWatch
from ..core.radar_core import RadarStore, RadarAI, Cognition, RadarMemoryAPI, verified
from ..api.radar_explore import RadarExploreAPI
from ..core.radar_session import recent as recent_session_share, ids as session_share_ids
from ..services.radar_services import RadarServices



def _fmt_time(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "?"

class HooksMixin:
    @filter.event_message_type(filter.EventMessageType.ALL)
    @entry_handler
    async def _on_any_message(self, event: AstrMessageEvent):
        """群聊 / 私聊里聊到的东西，攒成「她也想去看看」的话题队列。

        这里只做记账，不产生回复；预筛在 TopicWatch 内部完成，
        大部分消息不会触达模型。
        """
        cfg = self.config.get("topic") or {}
        if not cfg.get("enable", True):
            return
        try:
            await self.topic_watch.observe(event)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[CommunityRadar] 话题监听跳过: {exc}")

    @filter.on_llm_request()
    @entry_handler
    async def _on_recent_share(self, event: AstrMessageEvent, req: ProviderRequest):
        """刚分享过内容时，把摘要带进当前会话，方便用户直接追问细节。"""
        if not (self.config.get("share") or {}).get("auto_followup", True):
            return
        try:
            umo = str(event.unified_msg_origin or "")
            entry = self._recent_share(umo)
            if not entry:
                return
            lines = ["【你刚在这个会话分享过的内容，用户可能追问细节；据此回答，不确定就说明局限】"]
            for item in entry.get("items", [])[:2]:
                lines.append(f"- 《{item.get('title')}》 {item.get('url')}")
                if item.get("digest"):
                    lines.append(f"  摘要：{str(item['digest'])[:500]}")
            lines.append("不要编造摘要之外的情节；需要核实原片时可让主人用 /雷达补看。")
            self._append_temp(req, "\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[CommunityRadar] 最近分享注入跳过: {exc}")

    @filter.on_llm_request()
    @entry_handler
    async def _on_community_link(self, event: AstrMessageEvent, req: ProviderRequest):
        """有人分享社区链接时先看过内容，再让模型自然接话，不额外发消息。"""
        cfg = self.config.get("link_peek") or {}
        if not cfg.get("enable", True):
            return
        text = str(getattr(event, "message_str", "") or "")
        if not text or text.lstrip().startswith("/"):
            return
        if not any(k in text for k in (
            "bilibili.com", "b23.tv", "douyin.com", "iesdouyin.com", "xiaoheihe.cn",
        )):
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        now = time.time()
        cooldown = max(0, int(cfg.get("cooldown_sec") or 30))
        if now - self._peek_last.get(umo, 0) < cooldown:
            return
        self._peek_last[umo] = now
        if len(self._peek_last) > 200:  # 防止字典无界增长
            cutoff = now - max(cooldown, 3600)
            self._peek_last = {k: v for k, v in self._peek_last.items() if v > cutoff}
        try:
            injected = await self.link_peek.peek(text, cfg)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[CommunityRadar] 链接接话跳过: {exc}")
            return
        if injected:
            self._append_temp(req, injected)
