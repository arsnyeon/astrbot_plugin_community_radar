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

import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

from .bili import BiliClient, BiliError, find_video_id
from .duty import BrowseDuty
from .douyin_browser import DouyinBrowser
from .douyin_duty import DouyinDuty
from .douyin_feed import DouyinFeed
from .heybox import HeyboxClient, HeyboxError
from .link_peek import LinkPeek


def _fmt_time(ts: int) -> str:
    if not ts:
        return "?"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


@register(
    "astrbot_plugin_community_radar",
    "arsnyeon",
    "社区雷达：有自己兴趣的社区雷达，主动逛 B 站 / 抖音 / 小黑盒，按偏好搜索与点评，接得住你分享的链接。",
    "v0.6.0",
    "https://github.com/arsnyeon/astrbot_plugin_community_radar",
)
class CommunityRadarPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.bili = BiliClient(cookie=str(self.config.get("bili_cookie", "")))
        self.heybox = HeyboxClient(cookie=str(self.config.get("heybox_cookie", "")))
        self.duty = BrowseDuty(self)
        self.duty.start()
        self.douyin_cfg = dict(self.config.get("douyin") or {})
        self.douyin_cfg.setdefault("targets", (self.config.get("browse") or {}).get("targets") or [])
        self.douyin_feed = DouyinFeed()
        self.douyin_browser = DouyinBrowser(self.douyin_cfg)
        self.douyin_duty = DouyinDuty(self, self.douyin_browser, self.douyin_cfg)
        self.douyin_duty.start()
        self.link_peek = LinkPeek(self)
        self._peek_last: dict[str, float] = {}
        self._owner_warned = False

    async def terminate(self) -> None:
        for stop in (
            self.duty.stop,
            self.douyin_duty.stop,
            self.bili.close,
            self.heybox.close,
            self.douyin_feed.close,
        ):
            try:
                await stop()
            except Exception:  # noqa: BLE001
                pass
        logger.info("CommunityRadar plugin stopped.")

    @filter.on_llm_request()
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
        try:
            injected = await self.link_peek.peek(text, cfg)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[CommunityRadar] 链接接话跳过: {exc}")
            return
        if injected:
            req.system_prompt = ((req.system_prompt or "") + "\n\n" + injected).strip()

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

    async def _qr_result(self, event: AstrMessageEvent, path: str, note: str):
        img = getattr(event, "image_result", None)
        if callable(img):
            return [event.plain_result(note), img(str(path))]
        try:
            from astrbot.core.message.components import Image
            return [event.plain_result(note), event.chain_result([Image.fromFileSystem(str(path))])]
        except Exception:  # noqa: BLE001
            return [event.plain_result(f"{note}\n二维码图片：{path}")]

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

    @filter.command("抖音", alias={"douyin"}, desc="抖音热榜 / 刷 / 登录 / 状态 / 看 / 点赞 / 评论 / 分享 / 关")
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
                path = await self.douyin_browser.login_qr()
            except Exception as exc:  # noqa: BLE001
                yield event.plain_result(f"打开登录页失败：{exc}")
                return
            for r in await self._qr_result(event, path, "用抖音 App 扫码登录（建议小号）。扫完发 /抖音 状态 确认。"):
                yield r
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

    # ── 社区雷达自身 ──────────────────────────────────────────────

    @filter.command("社区雷达", alias={"radar"}, desc="看社区雷达状态，或看它这会儿自己想搜什么")
    async def radar_cmd(self, event: AstrMessageEvent, arg: GreedyStr = ""):
        sub = str(arg or "").strip().lower()
        cfg = self.config.get("browse") or {}
        if sub in ("", "状态", "status"):
            times = cfg.get("times") or []
            yield event.plain_result(
                f"定时浏览：{'开' if cfg.get('enable') else '关'}"
                f"  时间：{'、'.join(str(t) for t in times) or '未设'}\n"
                f"关键词来源：{cfg.get('interests_mode') or 'auto'}"
                f"  平台：{cfg.get('platforms') or 'all'}"
                f"  每次搜 {cfg.get('search_per_run', 2)} 个\n"
                f"最近搜过：{'、'.join(self.duty._recent_keywords(6)) or '还没有'}"
            )
            return
        if sub in ("兴趣", "想搜什么", "interests"):
            persona = await self.duty._bot_persona(cfg)
            words = await self.duty._pick_keywords(cfg, [], [], [], 3)
            yield event.plain_result(
                (f"人格节选：{persona[:100]}…\n" if persona else "")
                + f"这会儿想搜：{'、'.join(words) if words else '（没生成出来，看日志）'}"
            )
            return
        yield event.plain_result("用法：/社区雷达（状态）\n/社区雷达 兴趣（看它现在想搜什么）")

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

    # ── 抖音 LLM 工具 ────────────────────────────────────────────

    @filter.llm_tool(name="douyin_hot")
    async def _t_douyin_hot(self, event: AstrMessageEvent, limit: int = 8) -> str:
        """看抖音实时热榜（零浏览器、无需登录）。有人问抖音上什么火、今天有啥热点时用。limit 为条数。"""
        items = await self.douyin_feed.hot(max(1, min(20, int(limit or 8))))
        if not items:
            return "抖音热榜没拿到。"
        return "\n".join(
            f"{x['rank']}. {x['title']} 热度{x.get('hot_text', '?')} {x['url']}" for x in items
        )

    @filter.llm_tool(name="douyin_browse")
    async def _t_douyin_browse(self, event: AstrMessageEvent) -> str:
        """现在去刷一会儿抖音推荐流，看点视频、按心情点赞评论、挑好玩的分享给主人。要几分钟，需要已登录且安装了浏览器。"""
        if not self._is_owner(event):
            return "只有主人能让我去刷抖音。"
        r = await self.douyin_duty.browse(owner_asked=True)
        if not r.get("ok"):
            return f"没刷成：{r.get('message')}"
        return f"{r.get('message')}。看过：{'、'.join((r.get('titles') or [])[:5])}"

    @filter.llm_tool(name="douyin_watch")
    async def _t_douyin_watch(self, event: AstrMessageEvent, link: str) -> str:
        """点开一条抖音视频读完详情和热评再聊。link 为抖音视频链接或 v.douyin.com 短链。"""
        r = await self.douyin_duty.watch(link)
        return r.get("message") or "没看成。"

    @filter.llm_tool(name="douyin_like")
    async def _t_douyin_like(self, event: AstrMessageEvent, link: str) -> str:
        """给一条抖音视频点赞。link 为视频链接。默认试运行，需主人在配置里关掉 dry_run 才会真点。"""
        if not self._is_owner(event):
            return "只有主人能点赞。"
        r = await self.douyin_duty.like(link)
        return r.get("message") or "没点成。"

    @filter.llm_tool(name="douyin_comment")
    async def _t_douyin_comment(self, event: AstrMessageEvent, link: str, text: str) -> str:
        """在一条抖音视频下发评论。link 为视频链接，text 为评论内容（100 字内）。默认试运行。"""
        if not self._is_owner(event):
            return "只有主人能发评论。"
        r = await self.douyin_duty.comment(link, text)
        return r.get("message") or "没发成。"

    @filter.llm_tool(name="douyin_share")
    async def _t_douyin_share(self, event: AstrMessageEvent, link: str, note: str = "") -> str:
        """把一条抖音视频分享给主人（按配置走 QQ / 抖音站内私信）。link 为视频链接，note 为可选附言。"""
        r = await self.douyin_duty.share_video(link, note)
        return r.get("message") or "没分享成。"

    @filter.llm_tool(name="douyin_send_dm")
    async def _t_douyin_send_dm(self, event: AstrMessageEvent, text: str) -> str:
        """主人在 QQ 里明确要求时，在抖音私信里给核对过的主人发一句话。text 为内容（300 字内）。默认试运行，且只能发给核对过的主人。"""
        if not self._is_owner(event):
            return "只有主人能发抖音私信。"
        r = await self.douyin_duty.send_dm(text)
        return r.get("message") or "没发成。"

    @filter.llm_tool(name="douyin_status")
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

