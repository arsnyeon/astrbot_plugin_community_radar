#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""社区雷达：B 站 / 小黑盒浏览与互动插件。

- B 站：搜索、热门、视频信息、评论（读免登录）；发评论 / 点赞（需 Cookie）。
- 小黑盒：信息流、搜索（读免登录）；评论 / 点赞 / 发帖（需 Cookie）。
- 定时浏览：按配置的时间点自动刷 B 站热门 / 小黑盒信息流，推给主人或指定会话。
- 抖音因依赖无头浏览器暂未实现。
"""
from __future__ import annotations

import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

from .bili import BiliClient, BiliError, find_video_id
from .duty import BrowseDuty
from .heybox import HeyboxClient, HeyboxError


def _fmt_time(ts: int) -> str:
    if not ts:
        return "?"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


@register(
    "astrbot_plugin_community_radar",
    "arsnyeon",
    "社区雷达：定时/按需浏览 B 站与小黑盒，支持搜索、热门、视频详情、评论、点赞、发帖。",
    "v0.1.0",
    "https://github.com/arsnyeon/astrbot_plugin_community_radar",
)
class BiliHeyboxPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.bili = BiliClient(cookie=str(self.config.get("bili_cookie", "")))
        self.heybox = HeyboxClient(cookie=str(self.config.get("heybox_cookie", "")))
        self.duty = BrowseDuty(self)
        self.duty.start()

    async def terminate(self) -> None:
        try:
            await self.duty.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.bili.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.heybox.close()
        except Exception:  # noqa: BLE001
            pass
        logger.info("CommunityRadar plugin stopped.")

    # ── B 站命令 ──────────────────────────────────────────────────

    @filter.command("b站搜索", alias={"bili搜索", "B站搜索"}, desc="搜索 B 站视频")
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
    async def bili_popular_cmd(self, event: AstrMessageEvent):
        try:
            items = await self.bili.popular(10)
        except BiliError as exc:
            yield event.plain_result(f"获取失败：{exc}")
            return
        lines = [f"{i + 1}. {v['title']}\n  BV:{v['bvid']}  UP:{v['author']}  播放:{v['play']}" for i, v in enumerate(items[:10])]
        yield event.plain_result("\n\n".join(lines) or "没拿到热门。")

    @filter.command("b站视频", alias={"B站视频"}, desc="查看 B 站视频信息")
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

    # ── 小黑盒命令 ────────────────────────────────────────────────

    @filter.command("小黑盒", alias={"heybox"}, desc="小黑盒信息流/搜索")
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

    # ── 写操作命令 ────────────────────────────────────────────────

    @filter.command("b站发评论", alias={"B站发评论"}, desc="在 B 站视频下发评论")
    async def bili_comment_cmd(self, event: AstrMessageEvent, arg: GreedyStr):
        arg = (arg or "").strip()
        if " " not in arg:
            yield event.plain_result("用法：/b站发评论 BV号 评论内容")
            return
        bvid, message = arg.split(" ", 1)
        vid = find_video_id(bvid)
        if not vid or "short" in vid:
            yield event.plain_result("先给个有效 BV 号。")
            return
        try:
            info = await self.bili.view(vid.get("bvid", ""), vid.get("aid", ""))
            r = await self.bili.post_comment(str(info["aid"]), message.strip())
            yield event.plain_result(f"评论已发（rpid={r.get('rpid')}）。")
        except BiliError as exc:
            yield event.plain_result(f"评论失败：{exc}")

    @filter.command("b站点赞", alias={"B站点赞"}, desc="给 B 站视频点赞")
    async def bili_like_cmd(self, event: AstrMessageEvent, bvid: GreedyStr):
        bvid = (bvid or "").strip()
        vid = find_video_id(bvid)
        if not vid or "short" in vid:
            yield event.plain_result("给个 BV 号。")
            return
        try:
            info = await self.bili.view(vid.get("bvid", ""), vid.get("aid", ""))
            await self.bili.like_video(str(info["aid"]))
            yield event.plain_result(f"已给《{info['title']}》点赞。")
        except BiliError as exc:
            yield event.plain_result(f"点赞失败：{exc}")

    @filter.command("小黑盒发帖", alias={"heybox发帖"}, desc="在小黑盒发帖")
    async def heybox_post_cmd(self, event: AstrMessageEvent, arg: GreedyStr):
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

    # ── 抖音 ──────────────────────────────────────────────────────

    @filter.command("抖音", desc="抖音（暂不支持）")
    async def douyin_cmd(self, event: AstrMessageEvent):
        yield event.plain_result("抖音模块需要无头浏览器（CDP），当前 AstrBot 环境没配，暂不开放。可以用 /b站热门 或 /小黑盒 看东西。")

    # ── LLM 工具 ──────────────────────────────────────────────────

    @filter.llm_tool(name="bili_search")
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
    async def _t_bili_popular(self, event: AstrMessageEvent) -> str:
        """获取 B 站当前热门视频列表。"""
        try:
            items = await self.bili.popular(8)
        except BiliError as exc:
            return f"获取失败：{exc}"
        return "\n".join(f"{v['title']}（BV:{v['bvid']}，UP:{v['author']}）" for v in items[:8])

    @filter.llm_tool(name="bili_video_info")
    async def _t_bili_view(self, event: AstrMessageEvent, bvid: str) -> str:
        """查看 B 站视频详情。bvid 为 BV 号。"""
        try:
            v = await self.bili.view(bvid)
        except BiliError as exc:
            return f"获取失败：{exc}"
        return f"《{v['title']}》 UP:{v['author']} 播放{v['play']} 点赞{v['like']} 评论{v['reply']} 简介:{v['desc'][:150]}"

    @filter.llm_tool(name="bili_comments")
    async def _t_bili_comments(self, event: AstrMessageEvent, bvid: str) -> str:
        """查看 B 站视频的热门评论。bvid 为 BV 号。"""
        try:
            info = await self.bili.view(bvid)
            items = await self.bili.comments(str(info["aid"]), limit=6)
        except BiliError as exc:
            return f"获取失败：{exc}"
        return "\n".join(f"{c['name']}：{c['text']}" for c in items) or "没有评论。"

    @filter.llm_tool(name="heybox_feed")
    async def _t_heybox_feed(self, event: AstrMessageEvent) -> str:
        """获取小黑盒信息流。"""
        try:
            items = await self.heybox.feed(0)
        except HeyboxError as exc:
            return f"小黑盒：{exc}"
        return "\n".join(f"{x['title']}（ID:{x['link_id']}，赞{x['likes']}）" for x in items[:8]) or "没拿到信息流。"

    @filter.llm_tool(name="heybox_search")
    async def _t_heybox_search(self, event: AstrMessageEvent, query: str) -> str:
        """在小黑盒搜索帖子。query 为关键词。"""
        try:
            items = await self.heybox.search(query)
        except HeyboxError as exc:
            return f"小黑盒：{exc}"
        return "\n".join(f"{x['title']}（ID:{x['link_id']}，赞{x['likes']}）" for x in items[:8]) or "没搜到。"

    # ── 写操作 LLM 工具 ───────────────────────────────────────────

    @filter.llm_tool(name="bili_comment")
    async def _t_bili_comment(self, event: AstrMessageEvent, bvid: str, message: str) -> str:
        """在 B 站视频下发评论。bvid 为 BV 号，message 为评论内容。需要已在插件配置里填 B 站 Cookie。"""
        try:
            info = await self.bili.view(bvid)
            r = await self.bili.post_comment(str(info["aid"]), message)
            return f"评论已发（rpid={r.get('rpid')}）。"
        except BiliError as exc:
            return f"评论失败：{exc}"

    @filter.llm_tool(name="bili_like")
    async def _t_bili_like(self, event: AstrMessageEvent, bvid: str) -> str:
        """给 B 站视频点赞。bvid 为 BV 号。需要已在插件配置里填 B 站 Cookie。"""
        try:
            info = await self.bili.view(bvid)
            await self.bili.like_video(str(info["aid"]))
            return f"已给《{info['title']}》点赞。"
        except BiliError as exc:
            return f"点赞失败：{exc}"

    @filter.llm_tool(name="heybox_comment")
    async def _t_heybox_comment(self, event: AstrMessageEvent, link_id: str, text: str) -> str:
        """在小黑盒帖子下评论。link_id 为帖子 ID，text 为评论内容。需要已填小黑盒 Cookie。"""
        try:
            await self.heybox.comment_link(link_id, text)
            return "评论已发。"
        except HeyboxError as exc:
            return f"评论失败：{exc}"

    @filter.llm_tool(name="heybox_like")
    async def _t_heybox_like(self, event: AstrMessageEvent, link_id: str) -> str:
        """给小黑盒帖子点赞。link_id 为帖子 ID。需要已填小黑盒 Cookie。"""
        try:
            await self.heybox.like_link(link_id)
            return "已点赞。"
        except HeyboxError as exc:
            return f"点赞失败：{exc}"

    @filter.llm_tool(name="heybox_post")
    async def _t_heybox_post(self, event: AstrMessageEvent, title: str, text: str, topic_id: str = "7214") -> str:
        """在小黑盒发帖。title 为标题，text 为正文，topic_id 为分区（7214=盒友杂谈、18745=数码硬件、806933=AI）。需要已填小黑盒 Cookie。"""
        try:
            r = await self.heybox.post_text(title, text, topic_id)
            return f"发帖成功：{r['url']}" if r.get("url") else "发帖成功，但没拿到帖子编号。"
        except HeyboxError as exc:
            return f"发帖失败：{exc}"

