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

class HelpersMixin:
    def _recent_share(self, umo: str) -> dict | None:
        minutes = float((self.config.get("share") or {}).get("followup_minutes", 45) or 45)
        return recent_session_share(self.radar_store, umo, minutes)

    @staticmethod
    def _append_temp(req: ProviderRequest, text: str) -> None:
        """Attach per-turn context without persisting it into history."""
        text = str(text or "").strip()
        if not text:
            return
        parts = getattr(req, "extra_user_content_parts", None)
        if parts is None:
            req.extra_user_content_parts = []
            parts = req.extra_user_content_parts
        for part in parts:
            if getattr(part, "text", None) == text:
                return
        parts.append(TextPart(text=text).mark_as_temp())

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        """主人判定。未配置 admin_uin 时一律拒绝写操作（安全默认），只告警一次。"""
        admin = str(self.config.get("admin_uin") or "").strip()
        if not admin:
            if not self._owner_warned:
                self._owner_warned = True
                logger.warning(
                    "[CommunityRadar] 未配置「主人 QQ 号」(admin_uin)，"
                    "写操作（评论/点赞/私信/发帖）已全部禁用"
                )
            return False
        try:
            sender = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            sender = ""
        return bool(sender) and sender == admin

    def _platform_name(self) -> str:
        try:
            pm = self.context.platform_manager
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

    def _owner_umo(self) -> str:
        admin = str(self.config.get("admin_uin") or "").strip()
        if not admin:
            return ""
        return f"{self._platform_name()}:FriendMessage:{admin}"

    def _bili_owner_mid(self) -> int:
        return int((self.config.get("bili") or {}).get("owner_mid") or 0)

    def _bili_share_enabled(self) -> bool:
        return "bili" in str((self.config.get("bili") or {}).get("share_to") or "qq").lower()

    async def _send_bili_dm(self, text: str) -> str:
        """发一条 B 站私信给主人；返回空串表示成功，否则是错误信息。"""
        mid = self._bili_owner_mid()
        if not mid:
            return "没配置主人的 B 站号（bili_owner_mid）"
        try:
            await self.bili.send_msg(mid, text)
            return ""
        except BiliError as exc:
            return f"B 站私信失败：{exc}"

    def _share_state_file(self):
        from pathlib import Path

        base = Path("/AstrBot/data/plugin_data/astrbot_plugin_community_radar")
        try:
            from astrbot.api.star import StarTools

            base = Path(StarTools.get_data_dir("astrbot_plugin_community_radar"))
        except Exception:  # noqa: BLE001
            pass
        return base / "share_state.json"

    def _share_allowed(self) -> tuple[bool, str]:
        cfg = self.config.get("share") or {}
        if not cfg.get("enable", True):
            return False, "主动分享已经关掉了"
        import json

        path = self._share_state_file()
        state: dict = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                state = data
        except Exception:  # noqa: BLE001
            state = {}
        today = time.strftime("%Y-%m-%d")
        if state.get("day") != today:
            state = {"day": today, "count": 0, "last": 0}
        if int(state.get("count") or 0) >= int(cfg.get("max_per_day") or 8):
            return False, "今天的主动分享额度用完了，明天再说"
        cooldown = int(cfg.get("cooldown_sec") or 600)
        if time.time() - float(state.get("last") or 0) < cooldown:
            return False, "刚才分享过一条了，等会儿再发"
        return True, ""

    def _mark_shared(self) -> None:
        import json

        path = self._share_state_file()
        state: dict = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                state = data
        except Exception:  # noqa: BLE001
            state = {}
        today = time.strftime("%Y-%m-%d")
        if state.get("day") != today:
            state = {"day": today, "count": 0, "last": 0}
        state["count"] = int(state.get("count") or 0) + 1
        state["last"] = time.time()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] 分享状态写入失败: {exc}")

    async def _qr_result(self, event: AstrMessageEvent, path: str, note: str):
        img = getattr(event, "image_result", None)
        if callable(img):
            return [event.plain_result(note), img(str(path))]
        try:
            from astrbot.core.message.components import Image
            return [event.plain_result(note), event.chain_result([Image.fromFileSystem(str(path))])]
        except Exception:  # noqa: BLE001
            return [event.plain_result(f"{note}\n二维码图片：{path}")]
