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
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr

from .bili import BiliClient, BiliError, find_video_id
from .bili_duty import BiliDuty
from .duty import BrowseDuty
from .douyin_browser import DouyinBrowser
from .douyin_duty import DouyinDuty
from .douyin_feed import DouyinFeed
from .heybox import HeyboxClient, HeyboxError
from .link_peek import LinkPeek
from .video_watch import VideoWatch


def _fmt_time(ts: int) -> str:
    if not ts:
        return "?"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


@register(
    "astrbot_plugin_community_radar",
    "arsnyeon",
    "社区雷达：自己决定什么时候逛 B 站 / 抖音 / 小黑盒，看得懂视频与图文，会点赞评论回复，也接得住你分享的链接。",
    "v0.12.2",
    "https://github.com/arsnyeon/astrbot_plugin_community_radar",
)
class CommunityRadarPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.bili = BiliClient(cookie=str(self.config.get("bili_cookie", "")))
        self.heybox = HeyboxClient(cookie=str(self.config.get("heybox_cookie", "")))
        self.bili_duty = BiliDuty(self)
        self.bili_duty.start()
        self.duty = BrowseDuty(self)
        self.duty.start()
        self.douyin_cfg = dict(self.config.get("douyin") or {})
        self.douyin_cfg.setdefault("targets", (self.config.get("browse") or {}).get("targets") or [])
        self.douyin_feed = DouyinFeed()
        self.douyin_browser = DouyinBrowser(self.douyin_cfg)
        self.douyin_duty = DouyinDuty(self, self.douyin_browser, self.douyin_cfg)
        self.douyin_duty.start()
        self.link_peek = LinkPeek(self)
        self.video_watch = VideoWatch(self)
        self._peek_last: dict[str, float] = {}
        self._owner_warned = False

    async def terminate(self) -> None:
        for stop in (
            self.duty.stop,
            self.bili_duty.stop,
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
        if len(self._peek_last) > 200:  # 防止字典无界增长
            cutoff = now - max(cooldown, 3600)
            self._peek_last = {k: v for k, v in self._peek_last.items() if v > cutoff}
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

    # ── 主动分享给主人 ────────────────────────────────────────────

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

    @filter.command("刷b站", alias={"b站刷"}, desc="让它自己刷一会儿 B 站（试运行下只记录）")
    async def bili_browse_cmd(self, event: AstrMessageEvent):
        if not self._is_owner(event):
            yield event.plain_result("这个操作只有主人能用。")
            return
        yield event.plain_result("去 B 站刷一会儿…")
        r = await self.bili_duty.browse(owner_asked=True)
        yield event.plain_result(r.get("message") or "刷完了")

    @filter.command("b站状态", alias={"b站浏览状态"}, desc="看 B 站自主浏览的状态与今日额度")
    async def bili_status_cmd(self, event: AstrMessageEvent):
        s = self.bili_duty.state()
        t, c = s["today"], s["caps"]
        yield event.plain_result(
            f"B 站自主浏览：{'开' if s['enabled'] else '关'}  "
            f"试运行：{'开' if s['dry_run'] else '关'}  "
            f"自动刷：{'开' if s['auto_browse'] else '关'}\n"
            f"今日 看{t['watched']} 赞{t['like']}/{c['like']} 评{t['comment']}/{c['comment']}\n"
            f"Cookie：{'已填' if s['has_cookie'] else '未填（写操作不可用）'}"
        )

    @filter.command("b站回复", desc="回复 B 站某条评论")
    async def bili_reply_cmd(self, event: AstrMessageEvent, arg: GreedyStr):
        parts = (arg or "").strip().split(" ", 2)
        if len(parts) < 3:
            yield event.plain_result("用法：/b站回复 BV号 评论rpid 回复内容")
            return
        bvid, rpid, text = parts
        r = await self.bili_duty.reply(bvid.strip(), rpid.strip(), text.strip())
        yield event.plain_result(r.get("message") or "没发成")

    @filter.command("b站私信", desc="给主人发一条 B 站私信")
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

    @filter.command("社区雷达", alias={"radar"}, desc="看社区雷达状态，或手动让它现在去逛")
    async def radar_cmd(self, event: AstrMessageEvent, arg: GreedyStr = ""):
        sub = str(arg or "").strip().lower()
        cfg = self.config.get("browse") or {}
        if sub in ("", "状态", "status"):
            yield event.plain_result(
                f"自动浏览：{'开' if cfg.get('enable') else '关'}"
                f"  下次：{self.duty.next_run_text()}\n"
                f"间隔：{cfg.get('interval_min_hours', 1)}~{cfg.get('interval_max_hours', 5)} 小时"
                f"  静默：{cfg.get('quiet_hours', '1-6')}\n"
                f"关键词来源：{cfg.get('interests_mode') or 'auto'}"
                f"  平台：{cfg.get('platforms') or 'all'}"
                f"  每次搜 {cfg.get('search_per_run', 2)} 个\n"
                f"最近搜过：{'、'.join(self.duty._recent_keywords(6)) or '还没有'}"
            )
            return
        if sub in ("逛", "现在逛", "go", "now"):
            if not self._is_owner(event):
                yield event.plain_result("只有主人能让我现在去逛。")
                return
            self.duty.schedule_now()
            yield event.plain_result("好，我这就去逛一圈，最多一分钟内会给你发消息。")
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

    @filter.command("看视频", alias={"b站看视频", "看懂"}, desc="让它看懂一条 B 站 / 抖音 / 小黑盒的内容")
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
        if not r.get("ok"):
            yield event.plain_result(r.get("message") or "没看成")
            return
        yield event.plain_result(f"（走的{r.get('via')}）\n{r.get('digest')[:1500]}")

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

    # ── 主动分享 LLM 工具 ────────────────────────────────────────

    @filter.llm_tool(name="radar_share")
    async def _t_radar_share(
        self, event: AstrMessageEvent, title: str, url: str = "", comment: str = ""
    ) -> str:
        """把一条你觉得有趣的社区内容主动私聊分享给主人。

        Args:
            title(string): 内容标题或一句话概括
            url(string): 原链接
            comment(string): 你想顺带说的那句话（可选）
        """
        name = str(title or "").strip()
        if not name:
            return "没有可分享的内容"
        if not self._is_owner(event):
            return "只有主人才能让我主动分享给他。"
        ok, why = self._share_allowed()
        if not ok:
            return why
        umo = self._owner_umo()
        if not umo:
            return "没配置主人 QQ 号，没法私聊分享"
        text = f"看到个有意思的：《{name[:60]}》"
        note = str(comment or "").strip()
        if note:
            text += f"\n{note[:200]}"
        link = str(url or "").strip()
        if link:
            text += f"\n{link}"
        try:
            await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] 主动分享失败: {exc}")
            return f"分享失败：{exc}"
        bili_err = ""
        if self._bili_share_enabled():
            bili_err = await self._send_bili_dm(text)
        self._mark_shared()
        if bili_err:
            return f"已私聊分享给主人（B 站私信没发成：{bili_err}）"
        return "已经私聊分享给主人了"

    @filter.llm_tool(name="radar_watch_video")
    async def _t_watch_video(self, event: AstrMessageEvent, url: str) -> str:
        """看懂一条社区内容（B 站 / 抖音 / 小黑盒的视频或图文），看完再回答用户。

        Args:
            url(string): 视频或图文链接，B 站可用 BV 号，抖音可用分享链接
        """
        cfg = self.config.get("video") or {}
        if not cfg.get("enable", True):
            return "视频理解没有开启"
        r = await self.video_watch.any(url, cfg)
        if not r.get("ok"):
            return r.get("message") or "没看成这条内容"
        return f"（已通过{r.get('via')}看完）\n{r.get('digest')}"

    @filter.llm_tool(name="bili_browse")
    async def _t_bili_browse(self, event: AstrMessageEvent) -> str:
        """自己去刷一会儿 B 站推荐流，按喜好点赞或评论。只有主人能触发。"""
        if not self._is_owner(event):
            return "只有主人能让我去刷 B 站。"
        r = await self.bili_duty.browse(owner_asked=True)
        return r.get("message") or "刷完了"

    @filter.llm_tool(name="bili_reply")
    async def _t_bili_reply(self, event: AstrMessageEvent, bvid: str, rpid: str, text: str) -> str:
        """回复 B 站某条评论。bvid 视频号，rpid 评论号，text 回复内容。"""
        if not self._is_owner(event):
            return "只有主人能回复评论。"
        r = await self.bili_duty.reply(bvid, rpid, text)
        return r.get("message") or "没发成"

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

