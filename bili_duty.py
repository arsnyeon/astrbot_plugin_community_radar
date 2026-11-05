#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 站自主浏览：拉推荐流 → 自己判断要不要点赞/评论 → 执行并记账。

安全默认
--------
- ``dry_run`` 默认开启：只记录「本来想做什么」，不真的点赞评论。
- 每日上限 + 最小间隔，避免高频操作触发风控。
- 没有配置 Cookie 时写操作直接拒绝（只读不受影响）。
"""
from __future__ import annotations

import datetime
import json
import random
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .bili import BiliError


def _day() -> str:
    return datetime.date.today().isoformat()


class BiliDuty:
    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.cfg = plugin.config.get("bili") or {}
        self._state_file = self._data_dir() / "bili_state.json"
        self.s = self._load()

    # ── 状态 ────────────────────────────────────────────────────

    @staticmethod
    def _data_dir() -> Path:
        try:
            from astrbot.api.star import StarTools

            return Path(StarTools.get_data_dir("astrbot_plugin_community_radar"))
        except Exception:  # noqa: BLE001
            return Path("/AstrBot/data/plugin_data/astrbot_plugin_community_radar")

    def _empty(self) -> dict:
        return {"day": _day(), "watched": 0, "like": 0, "comment": 0,
                "writes": 0, "last_write_at": 0.0, "records": []}

    def _load(self) -> dict:
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return self._empty()
            if data.get("day") != _day():
                return self._empty()
            base = self._empty()
            base.update(data)
            return base
        except Exception:  # noqa: BLE001
            return self._empty()

    def _save(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(
                json.dumps(self.s, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] B 站状态写入失败: {exc}")

    def _caps(self) -> dict:
        caps = dict(self.cfg.get("caps") or {})
        return {"like": int(caps.get("like") or 3), "comment": int(caps.get("comment") or 2)}

    def _left(self, kind: str) -> int:
        return max(0, self._caps().get(kind, 0) - int(self.s.get(kind) or 0))

    def _blocked(self, kind: str) -> str:
        if self._left(kind) <= 0:
            return f"今天的{kind}额度用完了"
        gap = int(self.cfg.get("min_write_gap_sec") or 120)
        if time.time() - float(self.s.get("last_write_at") or 0) < gap:
            return "刚操作过，缓一缓"
        return ""

    def _record(self, kind: str, **kw) -> None:
        self.s.setdefault("records", [])
        self.s["records"] = (self.s["records"] + [{"at": int(time.time()), "kind": kind, **kw}])[-50:]

    def state(self) -> dict:
        return {
            "enabled": bool(self.cfg.get("enabled")),
            "dry_run": bool(self.cfg.get("dry_run", True)),
            "auto_browse": bool(self.cfg.get("auto_browse")),
            "today": {k: int(self.s.get(k) or 0) for k in ("watched", "like", "comment", "writes")},
            "caps": self._caps(),
            "has_cookie": bool(str(self.plugin.config.get("bili_cookie") or "").strip()),
        }

    # ── 主体的「刷」 ────────────────────────────────────────────

    async def browse(self, owner_asked: bool = False) -> dict:
        if not self.cfg.get("enabled") and not owner_asked:
            return {"ok": False, "message": "B 站自主浏览没打开"}
        try:
            items = await self.plugin.bili.feed(10)
        except BiliError as exc:
            return {"ok": False, "message": f"拉推荐流失败：{exc}"}
        if not items:
            return {"ok": False, "message": "推荐流是空的"}

        # 先看评论区：每条补 3 条热评，判断才不是只看标题
        for it in items[:8]:
            try:
                aid = str(it.get("aid") or "")
                if not aid and it.get("bvid"):
                    info = await self.plugin.bili.view(str(it["bvid"]))
                    aid = str(info.get("aid") or "")
                if aid:
                    cs = await self.plugin.bili.comments(aid, limit=8)
                    it["_comments"] = [str(c.get("text") or "")[:40] for c in cs if c.get("text")]
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[CommunityRadar] B 站评论读取失败: {exc}")

        self.s["watched"] = int(self.s.get("watched") or 0) + len(items)

        plan = await self._decide(items)
        done: list[str] = []
        for action in plan:
            bvid = str(action.get("bvid") or "")
            item = next((x for x in items if x.get("bvid") == bvid), None)
            if not item:
                continue
            wants_act = bool(action.get("like") or action.get("comment"))
            digest = await self._deep_watch(item) if wants_act else ""
            if action.get("like"):
                r = await self.like(bvid)
                if r.get("ok"):
                    done.append(f"赞了《{item['title'][:18]}》")
            text = str(action.get("comment") or "").strip()
            if text and self._left("comment") > 0:
                r = await self.comment(bvid, text)
                if r.get("ok"):
                    done.append(f"评论了《{item['title'][:18]}》：{text}")
                    if digest:
                        done.append(f"（{digest[:60]}）")
        self._save()

        if not done:
            # 定时刷且什么都没做：不打扰
            return {"ok": True, "message": "" if not owner_asked else f"刷了 {len(items)} 条，没特别想互动的"}
        msg = "；".join(done)
        if self.cfg.get("dry_run", True) and owner_asked:
            msg += "（试运行，没真操作）"
        return {"ok": True, "message": msg, "titles": [x["title"] for x in items[:5]]}

    async def _deep_watch(self, item: dict) -> str:
        """真看一遍：字幕优先，没字幕就抽帧描述画面。"""
        vw = getattr(self.plugin, "video_watch", None)
        cfg = self.plugin.config.get("video") or {}
        if vw is None or not cfg.get("enable", True):
            return ""
        bvid = str(item.get("bvid") or "")
        if not bvid:
            return ""
        try:
            r = await vw.bili(f"https://www.bilibili.com/video/{bvid}", cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] B 站深看失败: {exc}")
            return ""
        if not r.get("ok"):
            return ""
        return f"{r.get('via')}：{str(r.get('digest') or '')[:120]}"

    async def _decide(self, items: list[dict]) -> list[dict]:
        """让模型挑 1~2 条它真想互动的，并给评论草稿。"""
        provider = await self._provider()
        if provider is None:
            return []
        persona = ""
        try:
            persona = await self.plugin.duty._bot_persona(self.cfg)
        except Exception:  # noqa: BLE001
            persona = ""
        lines = []
        for x in items[:10]:
            line = f"- {x.get('bvid')} 《{x.get('title')}》 UP:{x.get('author')} 播放{x.get('play')}"
            cs = x.get("_comments") or []
            if cs:
                line += "\n  评论：" + " / ".join(cs[:5])
            lines.append(line)
        prompt = (
            (f"你的人格：\n{persona}\n\n" if persona else "")
            + "你刚刷 B 站推荐流，看到这些（含热评）：\n" + "\n".join(lines)
            + "\n\n挑 0~3 条你真有话想说的。没感觉就返回空数组。"
            "判断要基于标题和热评里实际写了什么，不要只看标题。"
            "只输出 JSON 数组，元素形如 "
            '{"bvid":"BV...","like":true,"comment":"一句话评论"}；'
            "评论要口语、短（20 字内），像随口留的，不要客套，不要 emoji。"
        )
        try:
            resp = await provider.text_chat(
                prompt=prompt, session_id="", contexts=[],
                system_prompt="你只输出 JSON 数组，不输出其他内容。",
            )
            raw = str(getattr(resp, "completion_text", "") or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] B 站互动决策失败: {exc}")
            return []
        m = re.search(r"\[[^\]]*\]", raw, re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return []
        out = []
        for x in data[:3] if isinstance(data, list) else []:
            if isinstance(x, dict) and x.get("bvid"):
                out.append(x)
        return out

    async def _provider(self):
        pid = str(self.cfg.get("ai_provider_id") or "").strip()
        ctx = self.plugin.context
        if pid:
            try:
                return ctx.get_provider_by_id(pid)
            except Exception:  # noqa: BLE001
                return None
        try:
            return await ctx.get_using_provider_async(None)
        except Exception:  # noqa: BLE001
            return None

    # ── 写操作 ──────────────────────────────────────────────────

    async def like(self, bvid: str) -> dict:
        why = self._blocked("like")
        if why:
            return {"ok": False, "message": why}
        try:
            info = await self.plugin.bili.view(bvid)
        except BiliError as exc:
            return {"ok": False, "message": str(exc)}
        if self.cfg.get("dry_run", True):
            self._record("dry:like", bvid=bvid)
            self.s["like"] = int(self.s.get("like") or 0) + 1
            self._save()
            return {"ok": True, "message": "试运行：本来要点赞"}
        try:
            await self.plugin.bili.like_video(str(info.get("aid")))
        except BiliError as exc:
            return {"ok": False, "message": f"点赞失败：{exc}"}
        self.s["like"] = int(self.s.get("like") or 0) + 1
        self.s["writes"] = int(self.s.get("writes") or 0) + 1
        self.s["last_write_at"] = time.time()
        self._record("like", bvid=bvid)
        self._save()
        return {"ok": True, "message": "已点赞"}

    async def comment(self, bvid: str, text: str) -> dict:
        why = self._blocked("comment")
        if why:
            return {"ok": False, "message": why}
        body = str(text or "").strip()[:100]
        if not body:
            return {"ok": False, "message": "评论内容是空的"}
        try:
            info = await self.plugin.bili.view(bvid)
        except BiliError as exc:
            return {"ok": False, "message": str(exc)}
        if self.cfg.get("dry_run", True):
            self._record("dry:comment", bvid=bvid, text=body)
            self.s["comment"] = int(self.s.get("comment") or 0) + 1
            self._save()
            return {"ok": True, "message": f"试运行：本来要评论「{body}」"}
        try:
            await self.plugin.bili.post_comment(str(info.get("aid")), body)
        except BiliError as exc:
            return {"ok": False, "message": f"评论失败：{exc}"}
        self.s["comment"] = int(self.s.get("comment") or 0) + 1
        self.s["writes"] = int(self.s.get("writes") or 0) + 1
        self.s["last_write_at"] = time.time()
        self._record("comment", bvid=bvid, text=body)
        self._save()
        return {"ok": True, "message": f"评论已发：{body}"}

    async def reply(self, bvid: str, rpid: str, text: str) -> dict:
        """回复某个评论（root/parent 都是被回复的那条）。"""
        why = self._blocked("comment")
        if why:
            return {"ok": False, "message": why}
        body = str(text or "").strip()[:100]
        if not body or not rpid:
            return {"ok": False, "message": "要给出评论号(rpid)和内容"}
        try:
            info = await self.plugin.bili.view(bvid)
        except BiliError as exc:
            return {"ok": False, "message": str(exc)}
        if self.cfg.get("dry_run", True):
            self._record("dry:reply", bvid=bvid, rpid=rpid, text=body)
            return {"ok": True, "message": f"试运行：本来要回复 {rpid}「{body}」"}
        try:
            await self.plugin.bili.post_comment(
                str(info.get("aid")), body, root=str(rpid), parent=str(rpid)
            )
        except BiliError as exc:
            return {"ok": False, "message": f"回复失败：{exc}"}
        self.s["comment"] = int(self.s.get("comment") or 0) + 1
        self.s["writes"] = int(self.s.get("writes") or 0) + 1
        self.s["last_write_at"] = time.time()
        self._record("reply", bvid=bvid, rpid=rpid, text=body)
        self._save()
        return {"ok": True, "message": f"已回复：{body}"}

    # ── 定时 ────────────────────────────────────────────────────

    def start(self) -> None:
        import asyncio

        if getattr(self, "_task", None) is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task = getattr(self, "_task", None)
        if task is not None:
            task.cancel()
            self._task = None

    async def _loop(self) -> None:
        import asyncio

        while True:
            await asyncio.sleep(300)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[CommunityRadar] B 站定时循环异常: {exc}")

    async def tick(self) -> None:
        if not self.cfg.get("enabled") or not self.cfg.get("auto_browse"):
            return
        hours = float(self.cfg.get("interval_hours") or 4)
        last = float(self.s.get("last_auto_run") or 0)
        if time.time() - last < hours * 3600:
            return
        try:
            r = await self.browse()
            if r.get("ok") and r.get("message"):
                await self.plugin.duty._send(f"刚刷了会儿 B 站。{r['message']}", self.plugin.config.get("browse") or {})
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] B 站定时浏览异常: {exc}")
        self.s["last_auto_run"] = time.time()
        self._save()
