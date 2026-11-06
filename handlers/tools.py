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

class ToolsMixin:
    @filter.llm_tool(name="bili_search")
    @entry_handler
    async def _t_bili_search(self, event: AstrMessageEvent, keyword: str) -> str:
        """在 B 站搜索视频。keyword 为搜索关键词。"""
        try:
            items = await self.bili.search(keyword)
        except BiliError as exc:
            return f"B 站搜索失败：{exc}"
        if not items:
            return "没搜到。"
        return "\n".join(f"BV:{v['bvid']} 《{v['title']}》 UP:{v['author']} 播放:{v['play']}" for v in items[:5])

    @filter.llm_tool(name="bili_popular")
    @entry_handler
    async def _t_bili_popular(self, event: AstrMessageEvent) -> str:
        """获取 B 站当前热门视频列表。"""
        try:
            items = await self.bili.popular(8)
        except BiliError as exc:
            return f"获取失败：{exc}"
        return "\n".join(f"{v['title']}（BV:{v['bvid']}，UP:{v['author']}）" for v in items[:8])

    @filter.llm_tool(name="bili_video_info")
    @entry_handler
    async def _t_bili_view(self, event: AstrMessageEvent, bvid: str) -> str:
        """查看 B 站视频详情。bvid 为 BV 号。"""
        try:
            v = await self.bili.view(bvid)
        except BiliError as exc:
            return f"获取失败：{exc}"
        return f"《{v['title']}》 UP:{v['author']} 播放{v['play']} 点赞{v['like']} 评论{v['reply']} 简介:{v['desc'][:150]}"

    @filter.llm_tool(name="bili_comments")
    @entry_handler
    async def _t_bili_comments(self, event: AstrMessageEvent, bvid: str) -> str:
        """查看 B 站视频的热门评论。bvid 为 BV 号。"""
        try:
            info = await self.bili.view(bvid)
            items = await self.bili.comments(str(info["aid"]), limit=6)
        except BiliError as exc:
            return f"获取失败：{exc}"
        return "\n".join(f"{c['name']}：{c['text']}" for c in items) or "没有评论。"

    @filter.llm_tool(name="heybox_feed")
    @entry_handler
    async def _t_heybox_feed(self, event: AstrMessageEvent) -> str:
        """获取小黑盒信息流。"""
        try:
            items = await self.heybox.feed(0)
        except HeyboxError as exc:
            return f"小黑盒：{exc}"
        return "\n".join(f"{x['title']}（ID:{x['link_id']}，赞{x['likes']}）" for x in items[:8]) or "没拿到信息流。"

    @filter.llm_tool(name="heybox_search")
    @entry_handler
    async def _t_heybox_search(self, event: AstrMessageEvent, query: str) -> str:
        """在小黑盒搜索帖子。query 为关键词。"""
        try:
            items = await self.heybox.search(query)
        except HeyboxError as exc:
            return f"小黑盒：{exc}"
        return "\n".join(f"{x['title']}（ID:{x['link_id']}，赞{x['likes']}）" for x in items[:8]) or "没搜到。"

    @filter.llm_tool(name="bili_comment")
    @entry_handler
    async def _t_bili_comment(self, event: AstrMessageEvent, bvid: str, message: str) -> str:
        """在B站视频下留言，只有主人可用，遵守试运行和额度。

        Args:
            bvid(string): 视频BV号
            message(string): 评论内容
        """
        if not self._is_owner(event):
            return "只有主人能发评论。"
        return (await self.bili_duty.comment(bvid, message))["message"]

    @filter.llm_tool(name="bili_like")
    @entry_handler
    async def _t_bili_like(self, event: AstrMessageEvent, bvid: str) -> str:
        """给B站视频点赞，只有主人可用，遵守试运行和额度。

        Args:
            bvid(string): 视频BV号
        """
        if not self._is_owner(event):
            return "只有主人能点赞。"
        return (await self.bili_duty.like(bvid))["message"]

    @filter.llm_tool(name="heybox_comment")
    @entry_handler
    async def _t_heybox_comment(self, event: AstrMessageEvent, link_id: str, text: str) -> str:
        """在小黑盒帖子下评论。link_id 为帖子 ID，text 为评论内容。需要已填小黑盒 Cookie。"""
        if not self._is_owner(event):
            return "只有主人才能让我去评论。"
        try:
            await self.heybox.comment_link(link_id, text)
            return "评论已发。"
        except HeyboxError as exc:
            return f"评论失败：{exc}"

    @filter.llm_tool(name="heybox_like")
    @entry_handler
    async def _t_heybox_like(self, event: AstrMessageEvent, link_id: str) -> str:
        """给小黑盒帖子点赞。link_id 为帖子 ID。需要已填小黑盒 Cookie。"""
        if not self._is_owner(event):
            return "只有主人才能让我去点赞。"
        try:
            await self.heybox.like_link(link_id)
            return "已点赞。"
        except HeyboxError as exc:
            return f"点赞失败：{exc}"

    @filter.llm_tool(name="heybox_post")
    @entry_handler
    async def _t_heybox_post(self, event: AstrMessageEvent, title: str, text: str, topic_id: str = "7214") -> str:
        """在小黑盒发帖。title 为标题，text 为正文，topic_id 为分区（7214=盒友杂谈、18745=数码硬件、806933=AI）。需要已填小黑盒 Cookie。"""
        if not self._is_owner(event):
            return "只有主人才能让我去发帖。"
        try:
            r = await self.heybox.post_text(title, text, topic_id)
            return f"发帖成功：{r['url']}" if r.get("url") else "发帖成功，但没拿到帖子编号。"
        except HeyboxError as exc:
            return f"发帖失败：{exc}"

    @filter.llm_tool(name="radar_share")
    @entry_handler
    async def _t_radar_share(self, event: AstrMessageEvent, title: str, url: str = "",
                             comment: str = "", image: str = "") -> str:
        """把实际发现的社区内容分享给主人，按内容去重并遵守每日上限。

        Args:
            title(string): 内容标题
            url(string): 原链接
            comment(string): 基于实际资料的感想，可为空
            image(string): 可选封面图片地址
        """
        if not self._is_owner(event):
            return "只有主人能触发主动分享。"
        if not str(title).strip():
            return "没有可分享的内容"
        target = self._owner_umo()
        if not target:
            return "未配置主人账号"
        text = f"《{str(title)[:80]}》\n{str(comment)[:300]}\n{str(url)[:500]}".strip()
        thought = str(comment or "").strip()
        source = f"《{str(title)[:80]}》 {str(url)[:500]}".strip()
        text = (thought + "\n" + source) if thought else source
        cfg = dict(self.config.get("browse") or {}, targets=[target])
        sent = await self.duty._send(text, cfg, image_url=image if image.startswith("https://") else "",
                                     context_items=[{"url": url}])
        return "已经分享给主人" if sent else "未确认发送成功，可能已分享、达到额度或发送结果未知。"

    @filter.llm_tool(name="radar_watch_video")
    @entry_handler
    async def _t_watch_video(self, event: AstrMessageEvent, url: str) -> str:
        """看懂一条社区内容（B 站 / 抖音 / 小黑盒的视频或图文），看完再回答用户。

        Args:
            url(string): 视频或图文链接，B 站可用 BV 号，抖音可用分享链接
        """
        cfg = self.config.get("video") or {}
        if not cfg.get("enable", True):
            return "视频理解没有开启"
        r = await self.video_watch.any(url, cfg)
        if verified(r):
            await self.cognition.review(r)
        if not r.get("ok"):
            return r.get("message") or "没看成这条内容"
        return f"证据状态：{r.get('status')}；来源：{r.get('via')}。抽样不等于完整观看。\n{r.get('digest')}"

    @filter.llm_tool(name="bili_browse")
    @entry_handler
    async def _t_bili_browse(self, event: AstrMessageEvent) -> str:
        """自己去刷一会儿 B 站推荐流，按喜好点赞或评论。只有主人能触发。"""
        if not self._is_owner(event):
            return "只有主人能让我去刷 B 站。"
        r = await self.bili_duty.browse(owner_asked=True)
        return r.get("message") or "刷完了"

    @filter.llm_tool(name="bili_reply")
    @entry_handler
    async def _t_bili_reply(self, event: AstrMessageEvent, bvid: str, rpid: str, text: str) -> str:
        """回复 B 站某条评论。bvid 视频号，rpid 评论号，text 回复内容。"""
        if not self._is_owner(event):
            return "只有主人能回复评论。"
        r = await self.bili_duty.reply(bvid, rpid, text)
        return r.get("message") or "没发成"

    @filter.llm_tool(name="douyin_hot")
    @entry_handler
    async def _t_douyin_hot(self, event: AstrMessageEvent, limit: int = 8) -> str:
        """看抖音实时热榜（零浏览器、无需登录）。有人问抖音上什么火、今天有啥热点时用。limit 为条数。"""
        items = await self.douyin_feed.hot(max(1, min(20, int(limit or 8))))
        if not items:
            return "抖音热榜没拿到。"
        return "\n".join(
            f"{x['rank']}. {x['title']} 热度{x.get('hot_text', '?')} {x['url']}" for x in items
        )

    @filter.llm_tool(name="douyin_browse")
    @entry_handler
    async def _t_douyin_browse(self, event: AstrMessageEvent) -> str:
        """现在去刷一会儿抖音推荐流，看点视频、按心情点赞评论、挑好玩的分享给主人。要几分钟，需要已登录且安装了浏览器。"""
        if not self._is_owner(event):
            return "只有主人能让我去刷抖音。"
        r = await self.douyin_duty.browse(owner_asked=True)
        if not r.get("ok"):
            return f"没刷成：{r.get('message')}"
        return f"{r.get('message')}。看过：{'、'.join((r.get('titles') or [])[:5])}"

    @filter.llm_tool(name="douyin_watch")
    @entry_handler
    async def _t_douyin_watch(self, event: AstrMessageEvent, link: str) -> str:
        """点开一条抖音视频读完详情和热评再聊。link 为抖音视频链接或 v.douyin.com 短链。"""
        r = await self.douyin_duty.watch(link)
        return r.get("message") or "没看成。"

    @filter.llm_tool(name="douyin_like")
    @entry_handler
    async def _t_douyin_like(self, event: AstrMessageEvent, link: str) -> str:
        """给一条抖音视频点赞。link 为视频链接。默认试运行，需主人在配置里关掉 dry_run 才会真点。"""
        if not self._is_owner(event):
            return "只有主人能点赞。"
        r = await self.douyin_duty.like(link)
        return r.get("message") or "没点成。"

    @filter.llm_tool(name="douyin_comment")
    @entry_handler
    async def _t_douyin_comment(self, event: AstrMessageEvent, link: str, text: str) -> str:
        """在一条抖音视频下发评论。link 为视频链接，text 为评论内容（100 字内）。默认试运行。"""
        if not self._is_owner(event):
            return "只有主人能发评论。"
        r = await self.douyin_duty.comment(link, text)
        return r.get("message") or "没发成。"

    @filter.llm_tool(name="douyin_share")
    @entry_handler
    async def _t_douyin_share(self, event: AstrMessageEvent, link: str, note: str = "") -> str:
        """把一条抖音视频分享给主人（按配置走 QQ / 抖音站内私信）。link 为视频链接，note 为可选附言。"""
        r = await self.douyin_duty.share_video(link, note)
        return r.get("message") or "没分享成。"

    @filter.llm_tool(name="douyin_send_dm")
    @entry_handler
    async def _t_douyin_send_dm(self, event: AstrMessageEvent, text: str) -> str:
        """主人在 QQ 里明确要求时，在抖音私信里给核对过的主人发一句话。text 为内容（300 字内）。默认试运行，且只能发给核对过的主人。"""
        if not self._is_owner(event):
            return "只有主人能发抖音私信。"
        r = await self.douyin_duty.send_dm(text)
        return r.get("message") or "没发成。"

    @filter.llm_tool(name="douyin_status")
    @entry_handler
    async def _t_douyin_status(self, event: AstrMessageEvent) -> str:
        """查看抖音模块状态：今日额度、是否被风控暂停、浏览器是否可用。"""
        s = self.douyin_duty.state()
        t, caps = s["today"], s["caps"]
        return (
            f"抖音写层{'开启' if s['enabled'] else '关闭'}，试运行{'开' if s['dry_run'] else '关'}，"
            f"今日看{t['watched']}/{caps['watch']} 赞{t['like']}/{caps['like']} "
            f"评{t['comment']}/{caps['comment']} 分享{t['share']}/{caps['share']}。"
            f"{'已暂停：' + s['pause_reason'] if s['paused'] else ''}"
            f"分享渠道={s['share_to']}，主人"
            f"{('已核对 ' + s['owner']['nickname']) if s['owner']['confirmed'] else '未核对'}。"
            f"浏览器：{s['browser']['browser'] or '未安装'}，已连接={s['browser']['connected']}。"
        )

    @filter.llm_tool(name="radar_recall")
    @entry_handler
    async def radar_recall_tool(self, event: AstrMessageEvent, query: str = "") -> str:
        """查询已经取得内容依据的浏览记忆和当时感想，不重新下载。

        Args:
            query(string): 要回忆的作品、题材或作者；空字符串查询最近记录
        """
        if self._is_owner(event):
            return self.cognition.recall(query)
        umo = str(event.unified_msg_origin or "")
        allowed = session_share_ids(
            self.radar_store, umo,
            float((self.config.get("share") or {}).get("group_recall_minutes", 180) or 180),
        )
        if not allowed:
            return "当前会话没有分享过可回忆的内容。"
        return self.cognition.recall_ids(allowed, query)

    @filter.llm_tool(name="radar_inspect")
    @entry_handler
    async def radar_inspect_tool(self, event: AstrMessageEvent, url: str,
                                 question: str, start: float, end: float) -> str:
        """有明确疑问时检查原视频的一段，最多180秒；不更新偏好和观看记忆。仅主人可用。

        Args:
            url(string): 要核查的原视频链接
            question(string): 需要核实的具体内容
            start(number): 原视频起始秒数
            end(number): 原视频结束秒数
        """
        if not self._is_owner(event):
            return "当前会话不能触发补看。"
        result = await self.video_watch.inspect(url, question, start, end)
        return json.dumps(result, ensure_ascii=False)
