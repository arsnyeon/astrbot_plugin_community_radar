#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""社区雷达：让机器人自己逛社区。

- B 站：搜索、热门、视频信息、评论、字幕；发评论 / 点赞（需 Cookie）。
- 小黑盒：信息流、搜索；评论 / 点赞 / 发帖（需 Cookie）。
- 百度贴吧：吧内主题与搜索（匿名可读）；楼层需登录，自动降级为摘要级。
- 抖音热榜：官方公开接口，零浏览器 / 零签名 / 零登录。
- 抖音推荐流与互动：按需拉起的有头浏览器（Xvfb + Chromium + CDP），
  页面内 fetch 免算 a_bogus，支持点赞 / 评论 / 分享，带限额与风控闸门。
- 定时浏览：按人格与兴趣自己决定逛哪、看什么，看完评论区再回味。
- 长期积累：观看记忆、兴趣证据与知识条目（含梗与术语），可同步进 AstrBot 知识库。
- 分享与接话：带图分条分享；群里有人接话时会自己回一句。
"""
from __future__ import annotations

import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star

from .clients.bili import BiliClient
from .services.bili_duty import BiliDuty
from .services.duty import BrowseDuty
from .browser.douyin_browser import DouyinBrowser
from .services.douyin_duty import DouyinDuty
from .clients.douyin_feed import DouyinFeed
from .clients.heybox import HeyboxClient
from .clients.tieba import TiebaClient
from .media.link_peek import LinkPeek
from .services.topic_watch import TopicWatch
from .services.watchlist import WatchList
from .media.video_watch import VideoWatch
from .core.paths import data_dir
from .core.learning import KnowledgeBook
from .core.health import PlatformHealth
from .core.radar_core import RadarStore, RadarAI, Cognition, RadarMemoryAPI
from .api.radar_explore import RadarExploreAPI
from .services.radar_services import RadarServices

from .handlers.hooks import HooksMixin
from .handlers.commands import CommandsMixin
from .handlers.tools import ToolsMixin
from .handlers.helpers import HelpersMixin


class CommunityRadarPlugin(HooksMixin, CommandsMixin, ToolsMixin, HelpersMixin, Star):
    """自己逛 B 站 / 小黑盒 / 贴吧 / 抖音，看得懂内容，会带图分享。

    常用命令：``/社区雷达``（状态）、``/社区雷达 逛``（立刻逛一轮）、
    ``/雷达记忆``、``/雷达兴趣``、``/雷达活动``、``/雷达预演``、``/雷达知识``、
    ``/雷达待看``、``/雷达登录``、``/看视频``。逐条说明见 README。
    """

    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.browser_lock = asyncio.Lock()
        self.radar_store = RadarStore(data_dir())
        self.knowledge = KnowledgeBook(self.radar_store)
        self.health = PlatformHealth(self.radar_store)
        self.watchlist = WatchList(data_dir())
        self.radar_ai = RadarAI(self)
        self.cognition = Cognition(self)
        self.radar_memory_api = RadarMemoryAPI(self)
        self.radar_explore_api = RadarExploreAPI(self)
        self.services = RadarServices(self)
        self.bili = BiliClient(cookie=str(self.config.get("bili_cookie", "")))
        self.heybox = HeyboxClient(cookie=str(self.config.get("heybox_cookie", "")))
        self.tieba = TiebaClient()
        self.bili_duty = BiliDuty(self)
        self.topic_watch = TopicWatch(self)
        self.duty = BrowseDuty(self)
        self.douyin_cfg = dict(self.config.get("douyin") or {})
        self.douyin_cfg.setdefault("targets", (self.config.get("browse") or {}).get("targets") or [])
        self.douyin_feed = DouyinFeed()
        self.douyin_browser = DouyinBrowser(self.douyin_cfg)
        self.douyin_duty = DouyinDuty(self, self.douyin_browser, self.douyin_cfg)
        self.link_peek = LinkPeek(self)
        self.video_watch = VideoWatch(self)
        self._peek_last: dict[str, float] = {}
        self._owner_warned = False

    async def initialize(self):
        self.bili_duty.start()
        self.duty.start()
        self.douyin_duty.start()
        self.services.start()
        logger.info("[CommunityRadar] 观看证据、兴趣与活动服务已启动")

    async def terminate(self) -> None:
        for stop in (
            self.services.stop,
            self.duty.stop,
            self.bili_duty.stop,
            self.douyin_duty.stop,
            self.bili.close,
            self.heybox.close,
            self.tieba.close,
            self.douyin_feed.close,
        ):
            try:
                await stop()
            except Exception:  # noqa: BLE001
                pass
        await self.douyin_browser.close()
        self.radar_store.close()
        logger.info("CommunityRadar plugin stopped.")
