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

class CommandsMixin:
    @filter.command("b站搜索", alias={"bili搜索", "B站搜索"}, desc="搜索 B 站视频")
    @entry_handler
    async def bili_search_cmd(self, event: AstrMessageEvent, keyword: GreedyStr):
        keyword = (keyword or "").strip()
        if not keyword:
            yield event.plain_result("用法：/b站搜索 关键词")
            return
        try:
            items = await self.bili.search(keyword)
        except BiliError as exc:
            yield event.plain_result(f"搜索失败：{exc}")
            return
        if not items:
            yield event.plain_result("没搜到。")
            return
        lines = []
        for v in items[:8]:
            lines.append(
                f"{v['title']}\n  BV:{v['bvid']}  UP:{v['author']}  播放:{v['play']}  {_fmt_time(v['pubdate'])}"
            )
        yield event.plain_result("\n\n".join(lines))

    @filter.command("b站热门", alias={"B站热门"}, desc="B 站热门视频")
    @entry_handler
    async def bili_popular_cmd(self, event: AstrMessageEvent):
        try:
            items = await self.bili.popular(10)
        except BiliError as exc:
            yield event.plain_result(f"获取失败：{exc}")
            return
        lines = [f"{i + 1}. {v['title']}\n  BV:{v['bvid']}  UP:{v['author']}  播放:{v['play']}" for i, v in enumerate(items[:10])]
        yield event.plain_result("\n\n".join(lines) or "没拿到热门。")

    @filter.command("b站视频", alias={"B站视频"}, desc="查看 B 站视频信息")
    @entry_handler
    async def bili_view_cmd(self, event: AstrMessageEvent, ref: GreedyStr):
        ref = (ref or "").strip()
        vid = find_video_id(ref)
        if not vid:
            yield event.plain_result("给个 BV 号或视频链接，例如 /b站视频 BV1xx411c7mD")
            return
        if "short" in vid:
            yield event.plain_result("b23.tv 短链暂时不支持，直接给 BV 号吧。")
            return
        try:
            v = await self.bili.view(vid.get("bvid", ""), vid.get("aid", ""))
        except BiliError as exc:
            yield event.plain_result(f"获取失败：{exc}")
            return
        text = (
            f"{v['title']}\n"
            f"UP：{v['author']}  分区：{v['tname']}\n"
            f"播放 {v['play']}  点赞 {v['like']}  评论 {v['reply']}  时长 {v['duration']}\n"
            f"简介：{v['desc'][:200]}"
        )
        yield event.plain_result(text)

    @filter.command("b站评论", alias={"B站评论"}, desc="查看 B 站视频热评")
    @entry_handler
    async def bili_comments_cmd(self, event: AstrMessageEvent, ref: GreedyStr):
        ref = (ref or "").strip()
        vid = find_video_id(ref)
        if not vid or "short" in vid:
            yield event.plain_result("给个 BV 号，例如 /b站评论 BV1xx411c7mD")
            return
        info = await self.bili.view(vid.get("bvid", ""), vid.get("aid", ""))
        try:
            items = await self.bili.comments(str(info["aid"]), limit=8)
        except BiliError as exc:
            yield event.plain_result(f"评论获取失败：{exc}")
            return
        if not items:
            yield event.plain_result("没评论或评论关了。")
            return
        lines = [f"{c['name']}（赞{c['like']}）：{c['text']}" for c in items]
        yield event.plain_result(f"《{info['title']}》热评：\n" + "\n".join(lines))

    @filter.command("刷b站", alias={"b站刷"}, desc="让它自己刷一会儿 B 站（试运行下只记录）")
    @entry_handler
    async def bili_browse_cmd(self, event: AstrMessageEvent):
        if not self._is_owner(event):
            yield event.plain_result("这个操作只有主人能用。")
            return
        yield event.plain_result("去 B 站刷一会儿…")
        r = await self.bili_duty.browse(owner_asked=True)
        yield event.plain_result(r.get("message") or "刷完了")

    @filter.command("b站状态", alias={"b站浏览状态"}, desc="看 B 站自主浏览的状态与今日额度")
    @entry_handler
    async def bili_status_cmd(self, event: AstrMessageEvent):
        s = self.bili_duty.state()
        t, c = s["today"], s["caps"]
        skip = s.get("last_skip_reason") or ""
        yield event.plain_result(
            f"B 站自主浏览：{'开' if s['enabled'] else '关'}  "
            f"试运行：{'开' if s['dry_run'] else '关'}  "
            f"自动刷：{'开' if s['auto_browse'] else '关'}\n"
            f"今日 看{t['watched']} 赞{t['like']}/{c['like']} 评{t['comment']}/{c['comment']}\n"
            f"Cookie：{'已填' if s['has_cookie'] else '未填（写操作不可用）'}"
            + (f"\n上次没动手：{skip}" if skip else "")
        )

    @filter.command("b站回复", desc="回复 B 站某条评论")
    @entry_handler
    async def bili_reply_cmd(self, event: AstrMessageEvent, arg: GreedyStr):
        if not self._is_owner(event):
            yield event.plain_result("这个操作只有主人能用。")
            return
        parts = (arg or "").strip().split(" ", 2)
        if len(parts) < 3:
            yield event.plain_result("用法：/b站回复 BV号 评论rpid 回复内容")
            return
        bvid, rpid, text = parts
        r = await self.bili_duty.reply(bvid.strip(), rpid.strip(), text.strip())
        yield event.plain_result(r.get("message") or "没发成")

    @filter.command("b站私信", desc="给主人发一条 B 站私信")
    @entry_handler
    async def bili_dm_cmd(self, event: AstrMessageEvent, arg: GreedyStr):
        if not self._is_owner(event):
            yield event.plain_result("这个操作只有主人能用。")
            return
        text = (arg or "").strip()
        if not text:
            yield event.plain_result("用法：/b站私信 内容（需要已配置主人的 B 站号）")
            return
        err = await self._send_bili_dm(text)
        yield event.plain_result(err or "已通过 B 站私信发给他了")

    @filter.command("小黑盒", alias={"heybox"}, desc="小黑盒信息流/搜索")
    @entry_handler
    async def heybox_cmd(self, event: AstrMessageEvent, arg: GreedyStr = ""):
        arg = (arg or "").strip()
        try:
            if not arg or arg.lower() in {"feed", "信息流", "热榜"}:
                items = await self.heybox.feed(0)
                lines = [f"{x['title']}\n  ID:{x['link_id']}  赞{x['likes']} 评{x['comments']}" for x in items[:10]]
                yield event.plain_result("\n\n".join(lines) or "没拿到信息流。")
                return
            if arg.lower().startswith("search ") or arg.startswith("搜索 "):
                q = arg.split(" ", 1)[1]
                items = await self.heybox.search(q)
                lines = [f"{x['title']}\n  ID:{x['link_id']}  赞{x['likes']} 评{x['comments']}" for x in items[:8]]
                yield event.plain_result("\n\n".join(lines) or "没搜到。")
                return
            yield event.plain_result("用法：/小黑盒（信息流）或 /小黑盒 search 关键词")
        except HeyboxError as exc:
            yield event.plain_result(f"小黑盒：{exc}")

    @filter.command("b站发评论", alias={"B站发评论"}, desc="在B站视频下留言")
    @entry_handler
    async def bili_comment_cmd(self, event: AstrMessageEvent, arg: GreedyStr):
        if not self._is_owner(event):
            yield event.plain_result("只有主人能发评论。")
            return
        parts = str(arg).strip().split(maxsplit=1)
        if len(parts) != 2:
            yield event.plain_result("用法：/b站发评论 BV号 评论内容")
            return
        yield event.plain_result((await self.bili_duty.comment(parts[0], parts[1]))["message"])

    @filter.command("b站点赞", alias={"B站点赞"}, desc="给B站视频点赞")
    @entry_handler
    async def bili_like_cmd(self, event: AstrMessageEvent, bvid: GreedyStr):
        if not self._is_owner(event):
            yield event.plain_result("只有主人能点赞。")
            return
        yield event.plain_result((await self.bili_duty.like(str(bvid).strip()))["message"])

    @filter.command("小黑盒发帖", alias={"heybox发帖"}, desc="在小黑盒发帖")
    @entry_handler
    async def heybox_post_cmd(self, event: AstrMessageEvent, arg: GreedyStr):
        if not self._is_owner(event):
            yield event.plain_result("这个操作只有主人能用。")
            return
        arg = (arg or "").strip()
        if " " not in arg:
            yield event.plain_result("用法：/小黑盒发帖 标题 正文（默认分区盒友杂谈 7214）")
            return
        title, text = arg.split(" ", 1)
        try:
            r = await self.heybox.post_text(title.strip(), text.strip())
            yield event.plain_result(f"发帖成功：{r['url']}" if r.get("url") else "发帖成功，但没拿到帖子编号。")
        except HeyboxError as exc:
            yield event.plain_result(f"发帖失败：{exc}")

    @filter.command("抖音", alias={"douyin"}, desc="抖音热榜 / 刷 / 登录 / 状态 / 看 / 点赞 / 评论 / 分享 / 关")
    @entry_handler
    async def douyin_cmd(self, event: AstrMessageEvent, arg: GreedyStr = ""):
        parts = str(arg or "").strip().split(" ", 1)
        sub = (parts[0] if parts else "").lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub in ("", "热榜", "hot", "榜"):
            items = await self.douyin_feed.hot(10)
            yield event.plain_result("\n".join(DouyinFeed.lines(items)) if items else "抖音热榜没拿到，稍后再试。")
            return

        if sub in ("帮助", "help", "?"):
            yield event.plain_result(
                "社区雷达·抖音用法：\n"
                "/抖音 热榜            看抖音热榜（零浏览器）\n"
                "/抖音 刷              按需开浏览器刷推荐流（会长达几分钟）\n"
                "/抖音 登录            发登录二维码，用抖音 App 扫码（建议小号）\n"
                "/抖音 状态            看今日额度、风控暂停、浏览器状态\n"
                "/抖音 看 <链接>        点开视频读完详情与热评\n"
                "/抖音 点赞 <链接>      给视频点赞\n"
                "/抖音 评论 <链接> <话> 发评论\n"
                "/抖音 分享 <链接> [话] 按配置分享（QQ / 抖音私信）\n"
                "/抖音 核对主人 <抖音号或主页链接>  核对后才能站内私信分享\n"
                "/抖音 私信 <话>        在抖音私信里发一句话给主人\n"
                "/抖音 关              立刻关掉浏览器释放内存"
            )
            return

        if sub in ("状态", "status"):
            s = self.douyin_duty.state()
            t, caps, b = s["today"], s["caps"], s["browser"]
            yield event.plain_result(
                f"抖音写层：{'开' if s['enabled'] else '关'}  试运行：{'开' if s['dry_run'] else '关'}  "
                f"自动刷：{'开' if s['auto_browse'] else '关'}\n"
                f"今日 看{t['watched']}/{caps['watch']} 赞{t['like']}/{caps['like']} "
                f"评{t['comment']}/{caps['comment']} 分享{t['share']}/{caps['share']} 写操作{t['writes']}\n"
                f"下次自动刷：{s['next_run']}  {('已暂停：' + s['pause_reason']) if s['paused'] else ''}\n"
                f"分享渠道：{s['share_to']}  主人："
                f"{('已核对 ' + s['owner']['nickname']) if s['owner']['confirmed'] else ('未核对' + (('，配置=' + s['owner_input']) if s['owner_input'] else '，配置为空'))}"
                f"  站内已分享 {s['shared_dy']} 条\n"
                f"浏览器：{'已连接' if b['connected'] else '未连接'}  {b['browser'] or '未安装'}"
                + (f"\n上次没动手：{s['last_skip_reason']}" if s.get("last_skip_reason") else "")
            )
            return

        if sub in ("热搜词", "trending"):
            items = await self.douyin_feed.trending(8)
            yield event.plain_result("\n".join(f"· {x['title']} {x['url']}" for x in items) or "没拿到趋势词。")
            return

        if not self._is_owner(event):
            yield event.plain_result("这个操作只有主人能用。")
            return

        if sub in ("核对主人", "认主人", "confirm", "owner") and rest:
            yield event.plain_result("去抖音查一下这个人…")
            r = await self.douyin_duty.confirm_owner(rest)
            if not r.get("ok"):
                yield event.plain_result(r.get("message") or "没核对上。")
                return
            # 把核对时用的输入写回插件配置：改了输入就必须重新核对（照上游 setOwner）
            saved = False
            try:
                d = self.config.get("douyin")
                if isinstance(d, dict):
                    d["owner_input"] = rest
                self.config.save_config()
                self.douyin_cfg["owner_input"] = rest
                saved = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[CommunityRadar] 主人配置写回失败: {exc}")
            yield event.plain_result(
                f"{r['message']}\n"
                + ("已把「主人的抖音号或主页链接」写进配置。\n" if saved else
                   "自动写配置没成功，请到配置页把「主人的抖音号或主页链接」填成完全相同的内容。\n")
                + "发 /抖音 状态 应显示「已核对」。想走抖音站内分享，把 share_to 设为 douyin 或 both。"
            )
            return

        if sub in ("私信", "dm", "发私信") and rest:
            r = await self.douyin_duty.send_dm(rest)
            yield event.plain_result(r.get("message") or "没发成。")
            return

        if sub in ("刷", "browse"):
            yield event.plain_result("去刷一会儿抖音，刷完告诉你看了啥…")
            r = await self.douyin_duty.browse(owner_asked=True)
            yield event.plain_result(r.get("message") or "刷完了，但没拿到结果。")
            return

        if sub in ("登录", "login", "扫码"):
            try:
                info = await self.douyin_browser.login_qr()
            except Exception as exc:  # noqa: BLE001
                yield event.plain_result(f"打开登录页失败：{exc}")
                return
            if not info.get("ok"):
                yield event.plain_result(info.get("reason") or "没能打开扫码登录框")
                return
            for item in await self._qr_result(
                event, info["path"], "用抖音 App 扫码登录（建议小号）。扫完发 /抖音 状态 确认。"
            ):
                yield item
            return

        if sub in ("截图", "screenshot", "看看"):
            try:
                info = await self.douyin_browser.screenshot()
            except Exception as exc:  # noqa: BLE001
                yield event.plain_result(f"截图失败：{exc}")
                return
            if info.get("url"):
                yield event.plain_result(f"它现在在这个页面：{info['url']}")
            for item in await self._qr_result(event, info["path"], "这是它当前看到的画面"):
                yield item
            return

        if sub in ("关", "close", "stop"):
            await self.douyin_browser.close()
            yield event.plain_result("浏览器已关，登录态还在。")
            return

        if sub in ("看", "watch", "读") and rest:
            r = await self.douyin_duty.watch(rest)
            yield event.plain_result(r.get("message") or "没看成。")
            return

        if sub in ("点赞", "like") and rest:
            r = await self.douyin_duty.like(rest)
            yield event.plain_result(r.get("message") or "没点成。")
            return

        if sub in ("评论", "comment") and rest:
            link, _, text = rest.partition(" ")
            if not text.strip():
                yield event.plain_result("用法：/抖音 评论 <链接> <内容>")
                return
            r = await self.douyin_duty.comment(link, text.strip())
            yield event.plain_result(r.get("message") or "没发成。")
            return

        if sub in ("分享", "share") and rest:
            link, _, note = rest.partition(" ")
            r = await self.douyin_duty.share_video(link, note.strip())
            yield event.plain_result(r.get("message") or "没分享成。")
            return

        yield event.plain_result("没看懂，发 /抖音 帮助 看用法。")

    @filter.command("社区雷达", alias={"radar"}, desc="看社区雷达状态，或手动让它现在去逛")
    @entry_handler
    async def radar_cmd(self, event: AstrMessageEvent, arg: GreedyStr = ""):
        sub = str(arg or "").strip().lower()
        cfg = self.config.get("browse") or {}
        if sub in ("", "状态", "status"):
            plan = self.duty.autopilot.plan(cfg, topics=self.topic_watch.queue(4))
            topic_line = self.topic_watch.brief(4) or "还没有"
            yield event.plain_result(
                f"自动浏览：{'开' if cfg.get('enable') else '关'}"
                f"  下次：{self.duty.next_run_text()}\n"
                f"间隔：{cfg.get('interval_min_hours', 1)}~{cfg.get('interval_max_hours', 5)} 小时"
                f"  静默：{cfg.get('quiet_hours', '1-6')}\n"
                f"关键词来源：{cfg.get('interests_mode') or 'auto'}"
                f"  平台：{cfg.get('platforms') or 'all'}"
                f"  每次搜 {cfg.get('search_per_run', 2)} 个\n"
                f"这轮打算：{plan.get('reason') or '随便逛逛'}"
                f"（{'、'.join(plan.get('platforms') or []) or '无'}）\n"
                f"最近搜过：{'、'.join(self.duty._recent_keywords(6)) or '还没有'}\n"
                f"想去看看的话题：\n{topic_line}"
            )
            return
        if sub in ("逛", "现在逛", "go", "now"):
            if not self._is_owner(event):
                yield event.plain_result("只有主人能让我现在去逛。")
                return
            self.duty.schedule_now()
            yield event.plain_result("已排队，下一次检查会开始浏览。看懂内容后再决定是否分享，可以用 /雷达活动 查看进度。")
            return
        if sub in ("兴趣", "想搜什么", "interests"):
            persona = await self.duty._bot_persona(cfg)
            words = await self.duty._pick_keywords(cfg, [], [], [], 3)
            yield event.plain_result(
                (f"人格节选：{persona[:100]}…\n" if persona else "")
                + f"这会儿想搜：{'、'.join(words) if words else '（没生成出来，看日志）'}"
            )
            return
        if sub in ("话题", "topic", "queue"):
            brief = self.topic_watch.brief(8)
            yield event.plain_result(
                ("聊天里攒下的、她想去看看的：\n" + brief) if brief
                else "现在没有攒下的话题。多聊点具体的东西，她自己会记。"
            )
            return
        if sub in ("清话题", "清空话题", "topic clear"):
            if not self._is_owner(event):
                yield event.plain_result("只有主人能清话题队列。")
                return
            self.topic_watch._save([])
            yield event.plain_result("话题队列清空了。")
            return
        yield event.plain_result(
            "用法：\n"
            "/社区雷达（看状态与想去的话题）\n"
            "/社区雷达 逛（现在就去逛）\n"
            "/社区雷达 兴趣（看它这会儿想搜什么）\n"
            "/社区雷达 话题（看聊天里攒下的话题）\n"
            "/社区雷达 清话题（清空话题队列）"
        )

    @filter.command("看视频", alias={"b站看视频", "看懂"}, desc="让它看懂一条 B 站 / 抖音 / 小黑盒的内容")
    @entry_handler
    async def watch_video_cmd(self, event: AstrMessageEvent, ref: GreedyStr):
        ref = (ref or "").strip()
        if not ref:
            yield event.plain_result("用法：/看视频 BV号 或 视频/图文链接")
            return
        cfg = self.config.get("video") or {}
        if not cfg.get("enable", True):
            yield event.plain_result("视频理解没开启")
            return
        yield event.plain_result("我去看看这个…")
        r = await self.video_watch.any(ref, cfg)
        if verified(r):
            await self.cognition.review(r)
        if not r.get("ok"):
            yield event.plain_result(r.get("message") or "没看成")
            return
        yield event.plain_result(f"（走的{r.get('via')}）\n{r.get('digest')[:1500]}")

    @filter.command("雷达记忆", desc="查询看过内容与当时感想")
    @entry_handler
    async def radar_memory_cmd(self, event: AstrMessageEvent, query: GreedyStr = ""):
        if not self._is_owner(event):
            yield event.plain_result("只有主人能查询完整浏览记忆。")
            return
        yield event.plain_result(self.cognition.recall(str(query).strip()))

    @filter.command("雷达兴趣", desc="查看从真实观看积累的偏好")
    @entry_handler
    async def radar_interests_cmd(self, event: AstrMessageEvent):
        yield event.plain_result(self.radar_store.preference_text())

    @filter.command("雷达周记", desc="根据真实观看记录写浏览周记")
    @entry_handler
    async def radar_weekly_cmd(self, event: AstrMessageEvent):
        if not self._is_owner(event):
            yield event.plain_result("只有主人能查询浏览周记。")
            return
        text = await self.cognition.weekly()
        card = self.cognition.weekly_card(text)
        if card:
            yield event.image_result(card)
        else:
            yield event.plain_result(text)

    @filter.command("雷达活动", desc="查看当前浏览进度与计划")
    @entry_handler
    async def radar_activity_cmd(self, event: AstrMessageEvent):
        if not self._is_owner(event):
            return
        activity = self.radar_memory_api.activity()
        plan = self.radar_store.get("last_plan", {})
        yield event.plain_result(
            f"当前：{activity.get('stage', 'idle')} {activity.get('title', '')}\n"
            f"最近计划：{plan.get('reason', '尚未形成')}\n下次浏览：{self.duty.next_run_text()}")

    @filter.command("雷达补看", desc="按问题补看指定片段")
    @entry_handler
    async def radar_inspect_cmd(self, event: AstrMessageEvent, arg: GreedyStr):
        if not self._is_owner(event):
            yield event.plain_result("只有主人能触发补看。")
            return
        parts = str(arg).split(maxsplit=3)
        if len(parts) != 4:
            yield event.plain_result("用法：/雷达补看 链接 起始秒 结束秒 要确认的问题（最多180秒）")
            return
        try:
            start, end = float(parts[1]), float(parts[2])
        except ValueError:
            yield event.plain_result("起止时间请用秒数。")
            return
        result = await self.video_watch.inspect(parts[0], parts[3], start, end)
        yield event.plain_result(result.get("digest") or result.get("message", "未取得资料"))

    @filter.command("雷达重看", desc="明确重新分析一个已看过的链接")
    @entry_handler
    async def radar_rewatch_cmd(self, event: AstrMessageEvent, url: GreedyStr):
        if not self._is_owner(event):
            yield event.plain_result("只有主人能要求重新分析。")
            return
        cfg = dict(self.config.get("video") or {}, force_rewatch=True)
        result = await self.video_watch.any(str(url).strip(), cfg)
        if verified(result):
            await self.cognition.review(result)
        yield event.plain_result(result.get("digest") or result.get("message", "未取得资料"))

    @filter.command("雷达追番", desc="搜索番剧、查询更新、观看并记忆单集")
    @entry_handler
    async def radar_bangumi_cmd(self, event: AstrMessageEvent, arg: GreedyStr = ""):
        if not self._is_owner(event):
            yield event.plain_result("只有主人能使用追番管理。")
            return
        parts = str(arg).strip().split(maxsplit=1)
        try:
            result = await self.services.bangumi(parts[0] if parts else "", parts[1] if len(parts) > 1 else "")
        except Exception as exc:
            result = f"番剧请求未完成（{type(exc).__name__}），没有记为看过。"
        yield event.plain_result(result)

    @filter.command("雷达B站操作", desc="投币、收藏、关注或发布动态；受独立开关与试运行约束")
    @entry_handler
    async def radar_action_cmd(self, event: AstrMessageEvent, arg: GreedyStr = ""):
        if not self._is_owner(event):
            yield event.plain_result("只有主人能执行平台操作。")
            return
        parts = str(arg).strip().split(maxsplit=1)
        mapping = {"投币": "coin", "收藏": "favorite", "关注": "follow", "动态": "dynamic"}
        if len(parts) != 2 or parts[0] not in mapping:
            yield event.plain_result("用法：/雷达B站操作 投币 BV号｜收藏 BV号｜关注 UID｜动态 正文")
            return
        action = mapping[parts[0]]
        result = await self.services.extra_action(action, parts[1] if action != "dynamic" else "",
                                                  parts[1] if action == "dynamic" else "")
        yield event.plain_result(result["message"])
