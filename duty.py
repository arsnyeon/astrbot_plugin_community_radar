#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定时浏览 B 站 / 小黑盒 / 抖音：自己决定逛哪、搜什么、说给谁听。

设计目标是「她自己在逛社区」，而不是「把榜单贴一遍」：
- 逛哪个平台、偏重哪儿，由 autopilot 按话题倾向和平台权重决定；
- 群聊 / 私聊里刚聊过的东西会进话题队列，下次优先去搜；
- 点评由模型按自己的偏好决定聊哪几条、为什么在意，允许整段跳过；
- 看到有意思的内容会带上封面 / 配图，不只是丢链接；
- 除了私聊主人，也可以顺手在群里说一句；
- 记住最近看过什么，下一次不重复聊同样的角度。
"""
from __future__ import annotations

import asyncio
import datetime
import json
import random
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.core.message.components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain

from .autopilot import PLATFORM_LABEL, Autopilot

DEFAULT_PERSONA = (
    "你是一个爱逛社区、有自己偏好和判断的人。看到对胃口的会多聊两句，"
    "没感觉的就跳过；会吐槽、会联想，但不会硬凑热闹。"
)


def _parse_keyword_array(text: str) -> list[str]:
    """从模型输出里取出 JSON 数组形式的关键词，容忍多余文字。"""
    s = str(text or "")
    m = re.search(r"\[[^\]]*\]", s, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    if isinstance(data, list):
        for x in data:
            w = str(x).strip().strip('"').strip("'")
            if w and len(w) <= 20 and w not in out:
                out.append(w)
    return out


def _parse_pick_object(text: str) -> dict:
    """解析模型给出的 {say, pick, skip} JSON；解析失败返回空 dict。"""
    s = str(text or "")
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


class BrowseDuty:
    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._task: asyncio.Task | None = None
        self._done: dict[str, set[str]] = {}
        self._history_file = self._data_dir() / "browse_history.json"
        self._state_file = self._data_dir() / "browse_state.json"
        self.autopilot = Autopilot(plugin)

    @staticmethod
    def _data_dir() -> Path:
        try:
            from astrbot.api.star import StarTools
            return Path(StarTools.get_data_dir("astrbot_plugin_community_radar"))
        except Exception:  # noqa: BLE001
            return Path("/AstrBot/data/plugin_data/astrbot_plugin_community_radar")

    # ── 自主调度状态 ─────────────────────────────────────────────

    def _load_state(self) -> dict:
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] 浏览调度状态写入失败: {exc}")

    def _next_due(self, now: float, cfg: dict) -> float:
        """随机决定下一次什么时候逛：在 min~max 小时里取一点，避开静默时段。"""
        lo = max(0.25, float(cfg.get("interval_min_hours") or 1.0))
        hi = max(lo, float(cfg.get("interval_max_hours") or 5.0))
        nxt = now + random.uniform(lo, hi) * 3600
        quiet = str(cfg.get("quiet_hours") or "1-6")
        # 落在静默时段就顺延到静默结束
        for _ in range(12):
            if not self._in_quiet_hours_ts(quiet, nxt):
                break
            nxt = self._quiet_end(quiet, nxt)
        return nxt

    def schedule_now(self) -> None:
        """把下一次浏览提前到现在（/社区雷达 逛 用）。"""
        state = self._load_state()
        state["next_due_at"] = 0.0
        self._save_state(state)

    @staticmethod
    def _quiet_range(quiet: str) -> tuple[int, int] | None:
        try:
            start, end = (int(x) for x in str(quiet).split("-"))
            return start, end
        except Exception:  # noqa: BLE001
            return None

    def _in_quiet_hours_ts(self, quiet: str, ts: float) -> bool:
        rng = self._quiet_range(quiet)
        if not rng:
            return False
        start, end = rng
        h = datetime.datetime.fromtimestamp(ts).hour
        if start <= end:
            return start <= h < end
        return h >= start or h < end  # 跨天，例如 23-6

    def _quiet_end(self, quiet: str, ts: float) -> float:
        rng = self._quiet_range(quiet)
        if not rng:
            return ts
        _, end = rng
        dt = datetime.datetime.fromtimestamp(ts)
        target = dt.replace(hour=end, minute=0, second=0, microsecond=0)
        if target <= dt:
            target += datetime.timedelta(days=1)
        return target.timestamp()

    # ── 调度循环 ────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[CommunityRadar] 自动浏览异常: {exc}")

    async def _tick(self) -> None:
        cfg = self.plugin.config.get("browse") or {}
        if not cfg.get("enable"):
            return
        now = time.time()
        state = self._load_state()
        due = float(state.get("next_due_at") or 0)
        if due <= 0:
            due = self._next_due(now, cfg)
            state["next_due_at"] = due
            self._save_state(state)
            return
        if now < due:
            return
        await self._run(cfg)
        state["last_run"] = now
        state["next_due_at"] = self._next_due(now, cfg)
        self._save_state(state)

    def next_run_text(self) -> str:
        state = self._load_state()
        due = float(state.get("next_due_at") or 0)
        if due <= 0:
            return "待定（下次 tick 会随机排期）"
        return datetime.datetime.fromtimestamp(due).strftime("%m-%d %H:%M")

    # ── 浏览历史 ────────────────────────────────────────────────

    def _read_history(self) -> list[dict]:
        try:
            data = json.loads(self._history_file.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            return []

    def _record_history(self, groups: dict[str, list[dict]]) -> None:
        entries = []
        for platform, items in groups.items():
            for x in items[:5]:
                title = str(x.get("title") or "").strip()
                if title:
                    entries.append({"at": int(time.time()), "platform": platform, "title": title[:80]})
        if not entries:
            return
        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            merged = entries + self._read_history()
            self._history_file.write_text(
                json.dumps(merged[:60], ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] 浏览历史写入失败: {exc}")

    def _recent_history(self, limit: int = 6) -> str:
        seen: set[str] = set()
        lines = []
        for h in self._read_history():
            title = str(h.get("title") or "")
            if not title or title in seen:
                continue
            seen.add(title)
            lines.append(f"- [{h.get('platform', '?')}] {title}")
            if len(lines) >= limit:
                break
        return "\n".join(lines)

    # ── 一次浏览 ────────────────────────────────────────────────

    async def _run(self, cfg: dict) -> None:
        count = max(1, int(cfg.get("count") or 5))
        mode = str(cfg.get("mode") or "summary").lower()
        per_run = max(0, int(cfg.get("search_per_run") or 2))

        # 先去把还压着的话题扫出来，这样这一轮就能用上
        topic_watch = getattr(self.plugin, "topic_watch", None)
        if topic_watch is not None:
            try:
                await topic_watch.flush(self.plugin.config.get("topic") or {})
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[CommunityRadar] 话题队列补扫失败: {exc}")
            topics = topic_watch.queue(4)
        else:
            topics = []

        plan = self.autopilot.plan(cfg, topics=topics)
        platforms = plan["platforms"]
        if not platforms:
            return
        logger.info(
            f"[CommunityRadar] 这轮逛：{plan['reason']}｜平台={platforms}"
            f"｜搜={plan['keywords'] or '（模型自己定）'}"
        )

        bili_items: list[dict] = []
        heybox_items: list[dict] = []
        douyin_items: list[dict] = []

        # 先拿到平台「眼前有什么」
        if "bili" in platforms:
            try:
                bili_items = await self.plugin.bili.popular(count)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[CommunityRadar] 拉 B 站热门失败: {exc}")

        if "heybox" in platforms:
            try:
                heybox_items = (await self.plugin.heybox.feed(0))[:count]
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[CommunityRadar] 拉小黑盒信息流失败: {exc}")

        if "douyin" in platforms and getattr(self.plugin, "douyin_feed", None):
            try:
                douyin_items = await self.plugin.douyin_feed.hot(count)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[CommunityRadar] 拉抖音热榜失败: {exc}")

        # 再让模型按自己的人格 + 刚看到的内容 + 话题队列，决定「我现在想搜什么」
        picks = await self._pick_keywords(cfg, bili_items, heybox_items, douyin_items, per_run)
        if plan["keywords"]:
            merged = list(dict.fromkeys(plan["keywords"] + picks))[: max(per_run, len(plan["keywords"]))]
            picks = merged

        if "bili" in platforms:
            bili_items = await self._search(self.plugin.bili, "search", picks, bili_items, count)

        if "heybox" in platforms:
            heybox_items = await self._search(self.plugin.heybox, "search", picks, heybox_items, count)

        # 只看标题点评不出来，先把前几条的正文 / 热评抓回来
        total = len(bili_items) + len(heybox_items) + len(douyin_items)
        limit = self.autopilot.enrich_limit(cfg, plan, total)
        await self._enrich("bili", bili_items, limit=limit)
        await self._enrich("heybox", heybox_items, limit=limit)

        if douyin_items and picks:
            for x in douyin_items:
                if any(k in str(x.get("title") or "") for k in picks):
                    x["_via"] = "合口味"
            douyin_items.sort(key=lambda x: (x.get("_via") != "合口味", x.get("rank", 99)))

        if not bili_items and not heybox_items and not douyin_items:
            return

        self._record_history({"B站": bili_items, "小黑盒": heybox_items, "抖音": douyin_items})
        # 这轮用掉了的话题，从队列划掉，别下次重复去搜
        if topic_watch is not None and picks:
            try:
                topic_watch.consume(picks)
            except Exception:  # noqa: BLE001
                pass

        if mode == "summary":
            text, picked = await self._summarize(
                bili_items, heybox_items, cfg, douyin_items, picks,
                plan=plan, topics=topics,
            )
        else:
            text, picked = self._links(bili_items, heybox_items, douyin_items), []

        if not text:
            return

        image_url = self.autopilot.pick_image(cfg, picked)
        await self._send(text, cfg, image_url=image_url, context_items=picked)

    async def _enrich(self, platform: str, items: list[dict], limit: int = 3) -> None:
        """给前几条补正文与热评，让点评基于内容而不是标题。"""
        for it in items[:limit]:
            try:
                if platform == "bili" and it.get("bvid"):
                    v = await self.plugin.bili.view(it["bvid"])
                    it["_body"] = str(v.get("desc") or "")[:400]
                    it["_stats"] = f"播放{v.get('play')} 点赞{v.get('like')}"
                    it["_link"] = f"https://www.bilibili.com/video/{v.get('bvid') or it['bvid']}"
                    if not it.get("pic"):
                        it["pic"] = str(v.get("pic") or "")
                    # 有字幕的话顺带读一眼，比简介信息量大得多
                    try:
                        sub = await self.plugin.bili.subtitles(
                            str(v.get("bvid") or it["bvid"]), str(v.get("cid") or "")
                        )
                        if sub:
                            it["_body"] = sub[:600]
                            it["_via"] = (it.get("_via") or "") + "配字幕"
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        cs = await self.plugin.bili.comments(str(v.get("aid")), limit=4)
                        it["_comments"] = [f"{c['name']}：{c['text'][:60]}" for c in cs]
                    except Exception:  # noqa: BLE001
                        pass
                elif platform == "heybox" and it.get("link_id"):
                    # 详情接口对小号会要验证码，先用信息流自带的简介兜底
                    it["_body"] = str(it.get("desc") or "")[:400]
                    it["_stats"] = f"赞{it.get('likes') or 0}"
                    it["_link"] = f"https://www.xiaoheihe.cn/app/bbs/link/{it['link_id']}"
                    try:
                        d = await self.plugin.heybox.detail(it["link_id"], comment_limit=3)
                        it["_body"] = str(d.get("body") or it["_body"])[:400]
                        it["_stats"] = f"赞{d.get('likes') or 0}"
                        it["_comments"] = [
                            f"{c.get('user') or '匿名'}：{str(c.get('text') or '')[:60]}"
                            for c in (d.get("comments") or [])
                        ]
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"[CommunityRadar] 小黑盒详情不可用，用简介兜底: {exc}")
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[CommunityRadar] 补充 {platform} 内容失败: {exc}")

    # ── 兴趣：由模型自己决定搜什么 ──────────────────────────────

    async def _pick_keywords(
        self,
        cfg: dict,
        bili: list[dict],
        heybox: list[dict],
        douyin: list[dict],
        per_run: int,
    ) -> list[str]:
        if per_run <= 0:
            return []
        mode = str(cfg.get("interests_mode") or "hybrid").lower()
        manual = [str(x).strip() for x in (cfg.get("interests") or []) if str(x).strip()]

        auto: list[str] = []
        if mode in ("auto", "hybrid"):
            candidates = [
                str(x.get("title") or "").strip()
                for x in (bili + heybox + douyin)
                if str(x.get("title") or "").strip()
            ]
            auto = await self._auto_keywords(cfg, candidates)

        if mode == "manual":
            pool = manual
        elif mode == "auto":
            pool = auto
        else:
            pool = manual + [w for w in auto if w not in manual]

        uniq: list[str] = []
        for w in pool:
            if w and w not in uniq:
                uniq.append(w)
        random.shuffle(uniq)
        picks = uniq[:per_run]
        if picks:
            self._record_keywords(picks)
        return picks

    async def _auto_keywords(self, cfg: dict, candidates: list[str]) -> list[str]:
        """让模型按自己的人格、最近看到的东西和话题队列，生成这次想搜的关键词。"""
        provider = await self._resolve_provider(cfg)
        if provider is None:
            return []
        persona = await self._bot_persona(cfg)
        history = self._recent_history(8)
        used = self._recent_keywords(6)

        parts = ["你在决定自己接下来想搜什么。"]
        if persona:
            parts.append("你的人格设定：\n" + persona)
        topic_brief = ""
        try:
            tw = getattr(self.plugin, "topic_watch", None)
            if tw is not None:
                topic_brief = tw.brief(4)
        except Exception:  # noqa: BLE001
            topic_brief = ""
        if topic_brief:
            parts.append(
                "最近聊天里聊到、你当时就想去搜一下的：\n" + topic_brief
                + "\n如果这些还想跟，就直接用它们当关键词。"
            )
        if history:
            parts.append("你最近看过的：\n" + history)
        if used:
            parts.append("你上次搜过：" + "、".join(used) + "（这次换点新的，别重复）")
        if candidates:
            parts.append("刚刷到的标题：\n" + "\n".join(f"- {t}" for t in candidates[:12]))
        parts.append(
            "按你自己的口味，给出你现在真的想搜的 2~3 个关键词。"
            "要具体（例如「独立游戏」「显卡降价」「恐怖游戏实况」），不要泛词（如「游戏」「视频」）。"
            '只输出 JSON 数组，例如 ["独立游戏","显卡降价"]，不要任何解释。'
        )
        try:
            resp = await provider.text_chat(
                prompt="\n\n".join(parts),
                session_id=self._default_target(cfg),
                contexts=[],
                system_prompt="你只输出 JSON 数组，不输出其他任何文字。",
            )
            words = _parse_keyword_array(getattr(resp, "completion_text", "") or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] 自动兴趣生成失败，这轮退回手动关键词: {exc}")
            return []
        return words[:3]

    async def _bot_persona(self, cfg: dict) -> str:
        """取当前会话生效的人格（大肥鱼），拿不到再退回插件里配置的 persona。"""
        try:
            pm = getattr(self.plugin.context, "persona_manager", None)
            if pm is not None and hasattr(pm, "resolve_selected_persona"):
                _, persona, _, _ = await pm.resolve_selected_persona(
                    umo=self._default_target(cfg),
                    conversation_persona_id=None,
                    platform_name="",
                    provider_settings=None,
                )
                if isinstance(persona, dict):
                    prompt = str(persona.get("prompt") or "")
                else:
                    prompt = str(getattr(persona, "prompt", "") or "")
                if prompt:
                    return prompt[:1200]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[CommunityRadar] 读取人格失败: {exc}")
        return str(cfg.get("persona") or "").strip()[:1200]

    def _keywords_file(self) -> Path:
        return self._history_file.parent / "interests_used.json"

    def _recent_keywords(self, limit: int = 6) -> list[str]:
        try:
            data = json.loads(self._keywords_file().read_text(encoding="utf-8"))
            return [str(x) for x in data][:limit] if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            return []

    def _record_keywords(self, words: list[str]) -> None:
        if not words:
            return
        try:
            merged = list(dict.fromkeys(list(words) + self._recent_keywords(20)))[:20]
            self._keywords_file().parent.mkdir(parents=True, exist_ok=True)
            self._keywords_file().write_text(
                json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] 关键词记录失败: {exc}")

    @staticmethod
    async def _search(client: Any, method: str, picks: list[str], base: list[dict], count: int) -> list[dict]:
        """按兴趣主动搜索，结果并入热门；同平台内按标识去重。"""
        if not picks or not hasattr(client, method):
            return base
        out = list(base)
        seen = {str(x.get("bvid") or x.get("link_id") or x.get("title") or "") for x in out}
        take = max(2, count // 2)
        for kw in picks:
            try:
                found = await getattr(client, method)(kw)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[CommunityRadar] 按兴趣搜索「{kw}」失败: {exc}")
                continue
            for item in (found or [])[:take]:
                key = str(item.get("bvid") or item.get("link_id") or item.get("title") or "")
                if key and key in seen:
                    continue
                seen.add(key)
                item = dict(item)
                item["_via"] = f"搜「{kw}」"
                out.append(item)
        return out

    def _links(self, bili: list[dict], heybox: list[dict], douyin: list[dict] | None = None) -> str:
        def via(x: dict) -> str:
            return f"（{x['_via']}）" if x.get("_via") else ""

        def block(items: list[dict], name: str, key: str, url_of) -> list[str]:
            out: list[str] = []
            if not items:
                return out
            out.append(f"【{name}】")
            for x in items:
                out.append(f"- 《{x.get('title') or '无标题'}》 {x.get('_stats') or ''}{via(x)}")
                body = str(x.get("_body") or "").strip()
                if body:
                    out.append(f"  简介：{body[:220]}")
                comments = x.get("_comments") or []
                if comments:
                    out.append("  热评：" + " / ".join(comments[:3]))
                out.append(f"  链接：{x.get('_link') or url_of(x)}")
            return out

        lines: list[str] = []
        lines += block(bili, "B 站", "bvid",
                       lambda x: f"https://www.bilibili.com/video/{x.get('bvid')}")
        lines += block(heybox, "小黑盒", "link_id",
                       lambda x: f"https://www.xiaoheihe.cn/app/bbs/link/{x.get('link_id')}")
        if douyin:
            lines.append("【抖音热榜】")
            for x in douyin:
                hot = x.get("hot_text") or ""
                hot_part = f" 热度{hot}" if hot not in ("", "-", "?") else ""
                lines.append(f"- {x.get('rank')}. {x.get('title')}{via(x)}{hot_part} {x.get('url')}")
        return "\n".join(lines)

    async def _summarize(
        self,
        bili: list[dict],
        heybox: list[dict],
        cfg: dict,
        douyin: list[dict] | None = None,
        interests: list[str] | None = None,
        *,
        plan: dict | None = None,
        topics: list[dict] | None = None,
    ) -> tuple[str, list[dict]]:
        """让模型自己挑着聊。返回 (文案, 被挑中的条目)。"""
        plan = plan or {}
        topics = topics or []
        candidates = self._ordered(plan, bili, heybox, douyin or [])
        links = self._links(bili, heybox, douyin)
        if not links:
            return "", []

        persona = str(cfg.get("persona") or "").strip() or DEFAULT_PERSONA
        interest_line = ("你最近比较在意的：" + "、".join(interests or []) + "。") if interests else ""
        history = self._recent_history(6)
        history_line = ("\n你之前看过这些，别用同样的角度再聊一遍：\n" + history + "\n") if history else ""

        topic_line = ""
        if topics:
            topic_line = (
                "\n你这次本来是想去看这些的（聊天里聊到的）：\n"
                + "\n".join(f"- {t.get('keyword')}：{t.get('why') or '想看看'}" for t in topics[:3])
                + "\n如果你确实看到了相关的东西，优先说这个。\n"
            )

        numbered = self._numbered_candidates(candidates)
        intro = (
            "【这不是任务清单，是你自己刚逛完社区、想跟朋友随口说两句】\n"
            f"{persona}\n{interest_line}\n"
        )
        if plan.get("focus"):
            intro += f"你这轮主要在刷{PLATFORM_LABEL.get(plan['focus'], plan['focus'])}。\n"
        intro += "下面每条都带编号，带了简介和热评，你可以据此判断内容到底是什么：\n"

        prompt = (
            intro
            + "要求：\n"
            "- 只聊你真正有感觉的 1~2 条，其余完全不用提，不用凑数\n"
            "- 说清为什么在意、想到什么、或者想吐槽什么；可以顺带提一句评论区\n"
            "- 绝对不要编号罗列、不要「今日精选」「以下是」这类开场白，也别逐条复述标题\n"
            "- 提到的那条，把链接放在句子里自然带出来\n"
            "- 整体 100~200 字，像微信发一段话，不要小作文\n"
            + (
                "- 如果这几条都很普通、你没有任何想主动说的话，就把 say 留空、skip 设为 true。\n"
                if bool(cfg.get("only_when_worth", True)) else ""
            )
            + topic_line
            + history_line
            + "\n"
            '只输出一个 JSON 对象：{"say":"你要说的话","pick":编号或0,"skip":false}\n'
            "pick 填你最想配图的那一条的编号，没有合适配图就填 0。\n\n"
            f"内容：\n{numbered}"
        )
        system_prompt = "你在跟朋友聊自己刚刷到的东西。口语化、自然、有个人好恶，不写小作文。只输出 JSON。"

        picked: list[dict] = []
        try:
            provider = await self._resolve_provider(cfg)
            if provider is None:
                return links, []
            resp = await provider.text_chat(
                prompt=prompt,
                session_id=self._default_target(cfg),
                system_prompt=system_prompt,
                contexts=[],
            )
            raw = (getattr(resp, "completion_text", "") or "").strip()
            data = _parse_pick_object(raw)
            if data:
                if data.get("skip"):
                    return "", []
                text = str(data.get("say") or "").strip()
                try:
                    idx = int(data.get("pick") or 0)
                except Exception:  # noqa: BLE001
                    idx = 0
                if 1 <= idx <= len(candidates):
                    picked = [candidates[idx - 1]]
                if not text:
                    return "", picked
                return text, picked
            # 模型没按 JSON 来，就按纯文本兜底
            if "[跳过]" in raw or raw.strip("[]【】 ") == "跳过":
                return "", []
            return raw or links, []
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] 点评生成失败，退回纯链接: {exc}")
            return links, []

    @staticmethod
    def _ordered(plan: dict, bili: list[dict], heybox: list[dict], douyin: list[dict]) -> list[dict]:
        """按本轮偏重的平台排序，让编号里前面的更可能是她想聊的。"""
        buckets = {"bili": bili, "heybox": heybox, "douyin": douyin}
        focus = str(plan.get("focus") or "")
        order = list(buckets)
        if focus in buckets:
            order = [focus] + [p for p in order if p != focus]
        out: list[dict] = []
        for p in order:
            out += list(buckets.get(p) or [])
        return out[:12]

    @staticmethod
    def _numbered_candidates(items: list[dict]) -> str:
        lines: list[str] = []
        for i, it in enumerate(items, 1):
            lines.append(f"[{i}] 《{it.get('title') or '无标题'}》 {it.get('_stats') or ''}")
            body = str(it.get("_body") or it.get("desc") or "").strip()
            if body:
                lines.append(f"    简介：{body[:200]}")
            comments = it.get("_comments") or []
            if comments:
                lines.append("    热评：" + " / ".join(comments[:3]))
            link = str(it.get("_link") or "")
            if not link:
                if it.get("bvid"):
                    link = f"https://www.bilibili.com/video/{it['bvid']}"
                elif it.get("link_id"):
                    link = f"https://www.xiaoheihe.cn/app/bbs/link/{it['link_id']}"
                else:
                    link = str(it.get("url") or "")
            if link:
                lines.append(f"    链接：{link}")
        return "\n".join(lines)

    def _default_target(self, cfg: dict) -> str:
        targets = [str(t).strip() for t in (cfg.get("targets") or []) if str(t).strip()]
        if targets:
            return targets[0]
        admin = str(self.plugin.config.get("admin_uin") or "").strip()
        if not admin:
            return ""
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

    # ── 投递 ────────────────────────────────────────────────────

    async def _send(
        self,
        text: str,
        cfg: dict,
        *,
        image_url: str = "",
        context_items: list[dict] | None = None,
    ) -> None:
        group_cfg = self.plugin.config.get("group_share") or {}
        targets = self.autopilot.targets(cfg, group_cfg=group_cfg)
        if not targets:
            logger.warning("[CommunityRadar] 未配置「主人账号」或「推送目标」，跳过本次推送")

        # 群聊是「顺便说一句」，决定一次，所有群共用
        want_group = self.autopilot.wants_group(cfg, group_cfg)
        owner_plat = self._platform_name()
        owner_umo = ""
        admin = str(self.plugin.config.get("admin_uin") or "").strip()
        if admin:
            owner_umo = f"{owner_plat}:FriendMessage:{admin}"

        for umo in targets:
            if not umo or umo.endswith(":"):
                continue
            is_group = ":GroupMessage:" in umo
            if is_group and not want_group:
                logger.debug(f"[CommunityRadar] 跳过群分享 {umo}（本轮没抽中）")
                continue
            chain: list[Any] = [Plain(text)]
            if image_url:
                try:
                    chain.append(Image.fromURL(image_url))
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[CommunityRadar] 配图不可用，只发文字: {exc}")
            try:
                await self.plugin.context.send_message(umo, MessageChain(chain))
                logger.info(f"[CommunityRadar] 已推送定时浏览到 {umo}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[CommunityRadar] 推送到 {umo} 失败: {exc}")
                if image_url:
                    try:
                        await self.plugin.context.send_message(umo, MessageChain([Plain(text)]))
                        logger.info(f"[CommunityRadar] 去掉配图后重发成功 {umo}")
                    except Exception as exc2:  # noqa: BLE001
                        logger.warning(f"[CommunityRadar] 纯文字重发也失败 {umo}: {exc2}")

        if owner_umo and owner_umo not in targets and want_group:
            # 群里说过了，也留一份在这儿，别让她「背着你说话」
            logger.debug(f"[CommunityRadar] owner 已在 targets 中或本轮未分享，跳过重复投递 {owner_umo}")
