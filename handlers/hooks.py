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
import random
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from .registration import entry_handler
from ..core.radar_core import parse_json
from ..services import followup as followup_state


class HooksMixin:
    @filter.event_message_type(filter.EventMessageType.ALL)
    @entry_handler
    async def _on_any_message(self, event: AstrMessageEvent):
        """群聊 / 私聊里聊到的东西，攒成「她也想去看看」的话题队列。

        这里只做记账，不产生回复；预筛在 TopicWatch 内部完成，
        大部分消息不会触达模型。
        """
        cfg = self.config.get("topic") or {}
        if cfg.get("enable", True):
            try:
                await self.topic_watch.observe(event)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[CommunityRadar] 话题监听跳过: {exc}")
        # 别人发来的具体内容（链接/作品）也记下来，之后她自己决定看不看。
        board = getattr(self, "watchlist", None)
        if board is not None and cfg.get("watchlist", True):
            try:
                text = str(getattr(event, "message_str", "") or "")
                if text:
                    user = str(event.get_sender_name() or "")[:40]
                    board.add_from_text(text, from_user=user)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[CommunityRadar] 想刷列表跳过: {exc}")
        # 群里有人接着聊她分享的东西、但没 @ 她时，默认 LLM 不会触发，由她自己接一句。
        # 接话要等模型，放到后台跑，免得拖住这条消息的其他处理。
        self._spawn_followup(event)

    def _spawn_followup(self, event: AstrMessageEvent) -> None:
        """把接话放到后台任务里，并回收已完成的任务。

        ``_maybe_followup`` 里所有对 ``event`` 的读取都发生在第一个 ``await`` 之前，
        因此任务启动后事件对象即便被回收也不影响判断结果。

        Args:
            event: 当前消息事件。
        """
        tasks = getattr(self, "_followup_tasks", None)
        if tasks is None:
            tasks = set()
            self._followup_tasks = tasks
        for done in [t for t in tasks if t.done()]:
            tasks.discard(done)
        if len(tasks) >= 4:  # 群里刷屏时不要堆任务
            return
        task = asyncio.create_task(self._maybe_followup(event))
        tasks.add(task)

    async def _maybe_followup(self, event: AstrMessageEvent) -> None:
        """后台任务入口：吞掉所有异常，避免「Task exception was never retrieved」。

        Args:
            event: 收到的消息事件。
        """
        try:
            await self._followup_once(event)
        except Exception as exc:  # noqa: BLE001 - 接话失败不能影响其他处理
            logger.debug(f"[CommunityRadar] 接话跳过（{type(exc).__name__}）")

    async def _followup_once(self, event: AstrMessageEvent) -> None:
        """群聊里有人回应她分享的内容时，主动接一句。

        只在「默认 LLM 不会回复」的情况下出手，避免和主流程重复说话。

        这里必须看 ``is_at_or_wake_command``（真正被点名/被回复）而不是
        ``is_wake``：后者只是「事件被放行」，而本插件的全量消息监听本身就会让它
        变成 True，用它判断会永远跳过。私聊与 @ 她的消息都由默认链路处理。

        Args:
            event: 收到的消息事件。
        """
        share_cfg = self.config.get("share") or {}
        if not share_cfg.get("followup_groups", True):
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if ":GroupMessage:" not in umo:
            return
        # 她自己的消息不触发，避免自问自答。
        if str(event.get_sender_id() or "") == str(event.get_self_id() or ""):
            return
        # 已经被点名（@ / 引用回复 / 唤醒词）的消息，默认 LLM 会带着上下文回复。
        if getattr(event, "is_at_or_wake_command", False) or getattr(event, "call_llm", False):
            return
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text or text.startswith(("/", "／", "!")):
            return
        entry = self._recent_share(umo)
        if not entry:
            return
        items = entry.get("items") or []
        if not followup_state.relevant(text, items):
            return
        state = followup_state.FollowUpState(self.radar_store)
        if not state.can_reply(umo,
                               max_replies=int(share_cfg.get("followup_replies") or 2)):
            return
        chance = float(share_cfg.get("followup_chance") or 0.6)
        if chance < 1 and random.random() > chance:
            return

        reply = await self._draft_followup(text, items, share_cfg)
        if not reply:
            return
        parts = [p for p in re.split(r"[\n]+", reply) if p.strip()][:2]
        if not parts:
            return
        try:
            for index, part in enumerate(parts):
                await self.context.send_message(umo, MessageChain([Plain(part)]))
                if index < len(parts) - 1:
                    await asyncio.sleep(0.8 + random.random() * 0.6)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[CommunityRadar] 接话没发出去（{type(exc).__name__}）")
            return
        state.mark(umo)

    async def _draft_followup(self, message: str, items: list[dict], cfg: dict) -> str:
        """生成一句接话，语气跟随当前人格。

        Args:
            message: 对方说的话。
            items: 她刚分享过的内容。
            cfg: 分享配置。

        Returns:
            要说的话；模型认为不该接时返回空串。
        """
        try:
            persona = await self.duty._bot_persona(self.config.get("browse") or {})
        except Exception:  # noqa: BLE001
            persona = ""
        lines = []
        for item in items[:2]:
            lines.append(f"- 《{item.get('title')}》 {item.get('url') or ''}".strip())
            if item.get("digest"):
                lines.append(f"  你记得的内容：{str(item['digest'])[:400]}")
        prompt = (
            (f"你的人格：{persona}\n" if persona else "")
            + "你刚才在这个群里分享过这些：\n"
            + "\n".join(lines)
            + f"\n\n现在有人接着说：{message[:300]}\n"
            "如果你有想接的话，就接一句；像平时在群里聊天那样，1~2 句，别客套，"
            "别用「感谢分享」「很高兴」这种腔调，也别重复你刚才说过的。"
            "只依据上面记得的内容，不确定的不要编。\n"
            '不想接就只输出 {"skip":true}。\n'
            '要接就只输出 {"say":"你要说的话"}。'
        )
        try:
            raw = await self.radar_ai.ask(prompt, cfg={"provider_id": str(cfg.get("ai_provider_id") or "")},
                                          purpose="接话")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[CommunityRadar] 接话生成失败（{type(exc).__name__}）")
            return ""
        data = parse_json(raw)
        if not isinstance(data, dict) or data.get("skip") is True:
            return ""
        say = str(data.get("say") or "").strip()
        return say[:300]

    @filter.on_llm_request()
    @entry_handler
    async def _on_platform_trouble(self, event: AstrMessageEvent, req: ProviderRequest):
        """把运行中遇到的平台问题交给模型，由它自己找机会跟主人说。

        只在主人私聊里注入，避免在群里说内部故障；说完就标记，不会反复念叨。
        """
        book = getattr(self, "health", None)
        if book is None:
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if ":FriendMessage:" not in umo or not self._is_owner(event):
            return
        try:
            issues = book.pending()
        except Exception:  # noqa: BLE001
            return
        if not issues:
            return
        labels = {"bili": "B 站", "heybox": "小黑盒", "tieba": "百度贴吧", "douyin": "抖音"}
        lines = []
        for item in issues:
            name = labels.get(item["platform"], item["platform"])
            line = f"- {name}：连着 {item['fails']} 次没进去"
            if item.get("reason"):
                line += f"，最近的提示是「{item['reason']}」"
            lines.append(line)
        try:
            auths = [a for a in book.pending_auth() if a["platform"] == "bili"]
        except Exception:  # noqa: BLE001
            auths = []
        hint = ""
        if auths:
            hint = ("\n其中 B 站看起来是登录过期了。你可以问他要不要发个二维码，"
                    "他扫一下就能续上（他自己发 /雷达登录 也行）。")
        self._append_temp(
            req,
            "【你刚才自己逛的时候遇到点情况，主人还不知道】\n"
            + "\n".join(lines)
            + hint
            + "\n如果你觉得合适，就顺口跟他提一句：哪个地方进不去、你猜为什么、你打算怎么办。"
              "用你平时的说话方式，别用「通知」「告警」这类词，也别照抄上面的句子。"
              "如果当前话题不适合插话，就先不说。",
        )
        try:
            book.mark_notified([item["platform"] for item in issues])
        except Exception:  # noqa: BLE001
            pass

    @filter.on_llm_request()
    @entry_handler
    async def _on_known_terms(self, event: AstrMessageEvent, req: ProviderRequest):
        """聊天里提到学过的东西时，把相关知识带进本轮上下文。

        知识条目来自她平时逛社区时的积累（含梗与术语）。只在该话题确实命中时注入，
        且限制条数，避免每轮都塞一堆无关内容。
        """
        cfg = self.config.get("learning") or {}
        if not cfg.get("recall_in_chat", True):
            return
        book = getattr(self, "knowledge", None)
        if book is None:
            return
        text = str(getattr(event, "message_str", "") or "").strip()
        if len(text) < 2:
            return
        try:
            body = book.text(text, 3)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[CommunityRadar] 知识召回跳过: {exc}")
            return
        if not body:
            return
        self._append_temp(
            req,
            "【你以前逛社区时记下的东西，和当前话题可能有关；不对就忽略，别硬套】\n" + body,
        )

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
