#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抖音写层调度：定时刷推荐、模型点评、限额闸门、推送主人。

闸门照上游 duty.js 移植：每日上限、总写操作上限、两次动作最小间隔、
新号前 N 天减半、验证码 / 风控当天停手、dryRun 默认开、写操作默认仅主人。
状态落在 /AstrBot/data/community_radar/douyin_state.json，容器重建不丢。
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
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from .douyin_browser import BrowserError, DouyinBrowser, digest_of

STOP_RE = re.compile(r"验证|频繁|稍后再试|异常|限制|风控")


def _day() -> str:
    return datetime.date.today().isoformat()


def _clip(text: Any, n: int = 200) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:n]


def _obj(v: Any) -> dict:
    """兼容 AstrBot object 型配置的两种形态：{k: v} 或 {items: {k: default}}。"""
    if isinstance(v, dict) and isinstance(v.get("items"), dict):
        return v["items"]
    return v if isinstance(v, dict) else {}


def _pick_json(out: str) -> list:
    m = re.search(r"\[[\s\S]*\]", str(out or ""))
    if not m:
        return []
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []


class DouyinDuty:
    def __init__(self, plugin: Any, browser: DouyinBrowser, cfg: dict):
        self.plugin = plugin
        self.b = browser
        self.cfg = cfg or {}
        browse_cfg = (getattr(plugin, "config", {}) or {}).get("browse") or {}
        self.persona = str(self.cfg.get("persona") or browse_cfg.get("persona") or "").strip()
        self.state_file = Path(self.b.data_dir) / "douyin_state.json"
        self._task: asyncio.Task | None = None
        self._busy = False
        self.s = self._load()

    # ── 状态 ────────────────────────────────────────────────────

    def _empty(self) -> dict:
        return {
            "day": _day(), "runs": 0, "watched": 0, "like": 0, "comment": 0,
            "share": 0, "writes": 0, "last_write_at": 0, "next_due_at": 0,
            "last_run": 0, "pause_day": "", "pause_reason": "", "login_at": 0,
            "seen": [], "history": [], "refresh_index": 1,
            "owner": {}, "shared_dy": [],
        }

    def _load(self) -> dict:
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            s = {**self._empty(), **(raw if isinstance(raw, dict) else {})}
        except Exception:  # noqa: BLE001
            s = self._empty()
        if s.get("day") != _day():
            for k in ("runs", "watched", "like", "comment", "share", "writes"):
                s[k] = 0
            s["day"] = _day()
            s["pause_day"] = ""
            s["pause_reason"] = ""
        return s

    def _save(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(self.s, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] 抖音状态保存失败: {exc}")

    def _record(self, kind: str, **kw) -> None:
        self.s.setdefault("history", []).insert(0, {"at": int(time.time()), "kind": kind, **kw})
        self.s["history"] = self.s["history"][:40]

    # ── 闸门 ────────────────────────────────────────────────────

    def _caps(self) -> dict:
        c = _obj(self.cfg.get("caps"))
        factor = 0.5 if self._is_new() else 1.0
        return {
            "watch": max(0, int(self.cfg.get("max_watch_per_day") or 20)),
            "like": int(float(c.get("like", 10)) * factor),
            "comment": int(float(c.get("comment", 5)) * factor),
            "share": int(float(c.get("share", 3)) * factor),
        }

    def _is_new(self) -> bool:
        days = int(self.cfg.get("new_account_days") or 7)
        return bool(days and self.s.get("login_at") and time.time() - self.s["login_at"] < days * 86400)

    def _left(self, kind: str) -> int:
        return max(0, self._caps().get(kind, 0) - int(self.s.get(kind, 0)))

    def _paused(self) -> bool:
        return self.s.get("pause_day") == _day()

    def _blocked(self, kind: str) -> str:
        if not self.cfg.get("enabled"):
            return "抖音写层没打开"
        if self._paused():
            return "今天暂停了：" + str(self.s.get("pause_reason") or "被风控了")
        if self._left(kind) <= 0:
            return f"今天{kind}次数用完了"
        maxw = int(float(self.cfg.get("max_writes_per_day") or 15) * (0.5 if self._is_new() else 1.0))
        if self.s.get("writes", 0) >= maxw:
            return "今天在抖音上动手的次数用完了"
        gap = int(self.cfg.get("min_write_gap_sec") or 90) * 1000
        wait = gap - (time.time() * 1000 - self.s.get("last_write_at", 0) * 1000)
        if wait > 0:
            return f"动作太密了，再等 {int(wait / 1000) + 1} 秒"
        return ""

    def _pause(self, why: str) -> None:
        self.s["pause_day"] = _day()
        self.s["pause_reason"] = _clip(why, 60) or "被风控了"
        self._save()
        logger.warning(f"[CommunityRadar] 抖音今天停手：{self.s['pause_reason']}")
        asyncio.create_task(self._notify(f"抖音那边要验证 / 被限制了，今天先不点赞评论了：{self.s['pause_reason']}。用 /抖音 登录 或 /抖音 状态 看看。"))

    def _on_error(self, exc: Exception) -> None:
        code = getattr(exc, "code", "")
        if code == "verify" or code == "login" or STOP_RE.search(str(exc)):
            self._pause(str(exc))

    # ── 推送 ────────────────────────────────────────────────────

    def _targets(self) -> list[str]:
        cfg = self.cfg
        t = [str(x).strip() for x in (cfg.get("targets") or []) if str(x).strip()]
        if t:
            return t
        try:
            return [self.plugin.duty._default_target(cfg)]
        except Exception:  # noqa: BLE001
            admin = str(self.plugin.config.get("admin_uin") or "").strip()
            return [f"default:FriendMessage:{admin}"] if admin else []

    async def _send(self, text: str) -> None:
        for umo in self._targets():
            try:
                await self.plugin.context.send_message(umo, MessageChain([Plain(text)]))
                logger.info(f"[CommunityRadar] 抖音推送到 {umo}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[CommunityRadar] 抖音推送到 {umo} 失败: {exc}")

    async def _notify(self, text: str) -> None:
        await self._send(f"【社区雷达·抖音】{text}")

    async def _provider(self):
        cfg = self.cfg
        ctx = self.plugin.context
        pid = str(cfg.get("ai_provider_id") or "").strip()
        if pid:
            return ctx.get_provider_by_id(pid)
        umo = self._targets()[0] if self._targets() else ""
        try:
            return await ctx.get_using_provider_async(umo)
        except Exception:  # noqa: BLE001
            try:
                return ctx.get_using_provider(umo)
            except Exception:  # noqa: BLE001
                return None

    # ── 定时 ────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        try:
            await self.b.close()
        except Exception:  # noqa: BLE001
            pass

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[CommunityRadar] 抖音定时异常: {exc}")

    async def tick(self) -> None:
        if not self.cfg.get("enabled") or not self.cfg.get("auto_browse") or self._busy:
            return
        if self._paused():
            return
        due = float(self.s.get("next_due_at") or 0)
        if due and time.time() < due:
            return
        if not due and self.s.get("last_run") and time.time() - self.s["last_run"] < float(self.cfg.get("interval_hours") or 4) * 3600:
            return
        await self.browse()

    # ── 主流程 ──────────────────────────────────────────────────

    async def browse(self, owner_asked: bool = False) -> dict:
        if self._busy:
            return {"ok": False, "message": "正在刷抖音，稍等一下"}
        if not self.cfg.get("enabled"):
            return {"ok": False, "message": "抖音写层没打开（配置页「抖音」→ 开启）"}
        self._busy = True
        try:
            return await self._browse(owner_asked)
        except BrowserError as exc:
            self._on_error(exc)
            logger.warning(f"[CommunityRadar] 刷抖音失败: {exc}")
            return {"ok": False, "message": f"刷抖音失败：{exc}", "code": getattr(exc, "code", "")}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] 刷抖音异常: {exc}")
            return {"ok": False, "message": f"刷抖音异常：{exc}"}
        finally:
            self._busy = False
            if self.cfg.get("close_after_run", True):
                try:
                    await self.b.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _browse(self, owner_asked: bool) -> dict:
        await self.b.ensure()
        why = await self.b.verify()
        if why:
            raise BrowserError("要验证：" + why, "verify")

        want = self._left("watch") if not owner_asked else max(1, min(5, self._left("watch") or 3))
        if want <= 0:
            return {"ok": False, "message": "今天看的视频数到上限了"}
        items = await self.b.feed(int(self.s.get("refresh_index") or 1))
        if not items:
            return {"ok": False, "message": "推荐流是空的（可能没登录或被风控）"}
        self.s["refresh_index"] = int(self.s.get("refresh_index") or 1) + 1
        seen = set(self.s.get("seen") or [])
        items = [v for v in items if v["aweme_id"] not in seen][:want]

        looked: list[dict] = []
        digests: list[str] = []
        for v in items:
            try:
                await self.b.open_video(v["aweme_id"], random.randint(2500, 6000))
                d = await self.b.detail(v["aweme_id"])
                cs = await self.b.comments(v["aweme_id"])
            except BrowserError as exc:
                if getattr(exc, "code", "") == "verify":
                    raise
                logger.warning(f"[CommunityRadar] 看视频 {v['aweme_id']} 失败: {exc}")
                continue
            looked.append(d)
            digests.append(f"#{len(looked)}\n{digest_of(d, cs)}")
            seen.add(v["aweme_id"])
            self.s["watched"] = int(self.s.get("watched", 0)) + 1

        if not looked:
            return {"ok": False, "message": "一个视频都没看成"}
        self.s["seen"] = list(seen)[-300:]
        self.s["runs"] = int(self.s.get("runs", 0)) + 1
        self.s["last_run"] = time.time()
        jitter = 0.8 + random.random() * 0.5
        self.s["next_due_at"] = time.time() + float(self.cfg.get("interval_hours") or 4) * 3600 * jitter
        self._save()

        decisions = await self._decide(digests, owner_asked)
        liked = commented = shared = 0
        shared_list: list[str] = []
        for idx, d in enumerate(looked):
            dec = decisions[idx] if idx < len(decisions) and isinstance(decisions[idx], dict) else {}
            dry = bool(self.cfg.get("dry_run", True))
            if dec.get("like") and not self._blocked("like"):
                if dry:
                    self._record("dry:like", aweme_id=d["aweme_id"], title=d["title"])
                    liked += 1
                else:
                    try:
                        await self.b.like(d["aweme_id"])
                        self.s["like"] += 1
                        self.s["writes"] += 1
                        self.s["last_write_at"] = time.time()
                        self._record("like", aweme_id=d["aweme_id"], title=d["title"])
                        liked += 1
                    except BrowserError as exc:
                        self._on_error(exc)
                        if getattr(exc, "code", "") == "verify":
                            break
            text = _clip(dec.get("comment"), 100)
            if text and not self._blocked("comment"):
                if dry:
                    self._record("dry:comment", aweme_id=d["aweme_id"], title=d["title"], text=text)
                    commented += 1
                else:
                    try:
                        await self.b.comment(d["aweme_id"], text)
                        self.s["comment"] += 1
                        self.s["writes"] += 1
                        self.s["last_write_at"] = time.time()
                        self._record("comment", aweme_id=d["aweme_id"], title=d["title"], text=text)
                        commented += 1
                    except BrowserError as exc:
                        self._on_error(exc)
                        if getattr(exc, "code", "") == "verify":
                            break
            share_text = _clip(dec.get("share"), 120)
            if share_text and self._left("share") > 0 and not self._paused():
                r = await self._share(d, share_text)
                if r.get("ok"):
                    self.s["share"] = int(self.s.get("share", 0)) + 1
                    self._record("share", aweme_id=d["aweme_id"], title=d["title"],
                                 text=share_text, via=r.get("via", []))
                    shared += 1
                    shared_list.append(f"{d['title']}（{'/'.join(r.get('via', []))}）")
                else:
                    logger.warning("[CommunityRadar] 这条视频没分享出去")

        self._save()
        head = f"【社区雷达·抖音】看了 {len(looked)} 个视频，点赞 {liked}，评论 {commented}，分享 {shared}"
        if self.cfg.get("dry_run", True):
            head += "（试运行：未真的点赞评论，站内私信分享只记录）"
        if shared_list:
            head += "\n分享给你：" + "、".join(shared_list[:3])
        await self._send(head)
        return {"ok": True, "message": head, "watched": len(looked), "liked": liked,
                "commented": commented, "shared": shared, "titles": [v["title"] for v in looked]}

    async def _decide(self, digests: list[str], owner_asked: bool) -> list[dict]:
        """让模型按人格决定点赞/评论/分享。失败就退化为「只看不动手」。"""
        prompt = (
            "你刚刷了一会儿抖音，下面是看到的视频。对每个视频给一个对象："
            '{"i":序号, "feeling":"一句真实感受", "like":true/false, '
            '"comment":"想发的评论（像普通观众，一两句，不想评就空字符串）", '
            '"share":"特别想让主人看时写一句推荐语，不想分享就空字符串"}。\n'
            + (f"这次最多点赞 {self._left('like')} 个、评论 {self._left('comment')} 个、分享 {self._left('share')} 个。" if not self.cfg.get("dry_run", True) else "")
            + ("主人专门让你找视频，至少挑一个写 share。" if owner_asked else "")
            + "只输出 JSON 数组，不要解释。\n\n" + "\n\n".join(digests)
        )
        system_prompt = (
            (self.persona or "一个爱逛社区、有自己偏好和判断的人，说话口语化。")
            + " 你是在自己刷视频，按喜好决定要不要动手；不想互动就都留空。"
              "评论要像普通观众，别打广告，别刷屏。"
        )
        try:
            provider = await self._provider()
            if provider is None:
                return []
            resp = await provider.text_chat(
                prompt=prompt,
                session_id=self._targets()[0] if self._targets() else "",
                system_prompt=system_prompt, contexts=[],
            )
            return _pick_json(getattr(resp, "completion_text", "") or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] 抖音点评生成失败，本轮不动手: {exc}")
            return []

    # ── 主人核对 ────────────────────────────────────────────────

    def owner(self) -> dict | None:
        """只有「核对过」且配置里的 owner_input 没被改动，才认这个主人。

        照上游 ownerDy()：改了输入就必须重新核对，避免把视频分享给陌生人。
        """
        o = self.s.get("owner") or {}
        configured = str(self.cfg.get("owner_input") or "").strip()
        if not o.get("sec_uid") or not configured:
            return None
        if str(o.get("confirmed_input") or "") != configured:
            return None
        if str(o.get("input") or "") != configured:
            return None
        return o

    async def confirm_owner(self, raw: str) -> dict:
        """填抖音号 / 主页链接 / sec_uid，查到这个人后把身份记进状态。"""
        raw = str(raw or "").strip()
        direct = re.search(r"douyin\.com/user/(MS4w[\w-]+)", raw)
        is_sec = bool(direct or re.fullmatch(r"MS4w[\w-]+", raw))
        douyin_id = "" if is_sec else re.sub(r"^@|^抖音号[:：]?\s*", "", raw)
        if not is_sec and not re.fullmatch(r"[\w.]{2,30}", douyin_id):
            return {"ok": False, "message": "填你的抖音号，或者主页链接（douyin.com/user/…）"}
        try:
            u = await self.b.profile_of(raw)
        except BrowserError as exc:
            return {"ok": False, "message": "没核对上：" + str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"核对时出错：{exc}"}
        finally:
            if self.cfg.get("close_after_run", True):
                try:
                    await self.b.close()
                except Exception:  # noqa: BLE001
                    pass
        self.s["owner"] = {**u, "input": raw, "confirmed_input": raw}
        self._save()
        extra = f"（抖音号 {u['douyin_id']}）" if u.get("douyin_id") else ""
        return {"ok": True, "message": f"核对好了：{u['nickname']}{extra}", "owner": u}

    # ── 分享（抖音站内私信 / QQ / 两边）──────────────────────────

    async def _share(self, v: dict, text: str) -> dict:
        """把视频分享给主人。按 share_to 走抖音站内私信、QQ，或两边都发。

        抖音那边没核对主人 / 发不出去时回退 QQ，免得白看。返回 {ok, via}。
        """
        mode = str(self.cfg.get("share_to") or "qq").lower()
        via: list[str] = []
        owner = self.owner()
        vid = v["aweme_id"]
        shared_dy: list[str] = self.s.setdefault("shared_dy", [])

        if mode in ("douyin", "both") and owner:
            if vid in shared_dy:
                via.append("douyin")  # 这个视频之前就在抖音里分享过了
            elif self.cfg.get("dry_run", True):
                self._record("dry:share", aweme_id=vid, title=v["title"], via="douyin")
                via.append("douyin")
            else:
                why = self._blocked("share")
                if why and "太密" in why:
                    wait = max(0.0, float(self.cfg.get("min_write_gap_sec") or 90)
                               - (time.time() - float(self.s.get("last_write_at") or 0)))
                    if wait < 180:
                        await asyncio.sleep(wait + 2)
                        why = self._blocked("share")
                if why:
                    logger.info(f"[CommunityRadar] 抖音站内分享跳过：{why}")
                else:
                    try:
                        r = await self.b.share(vid, owner)
                        self.s["writes"] = int(self.s.get("writes", 0)) + 1
                        self.s["last_write_at"] = time.time()
                        if not r.get("already"):
                            shared_dy.append(vid)
                            self.s["shared_dy"] = shared_dy[-300:]
                        self._record("share_douyin", aweme_id=vid, title=v["title"],
                                     already=bool(r.get("already")), text=_clip(text, 80))
                        via.append("douyin")
                    except BrowserError as exc:
                        self._on_error(exc)
                        logger.warning(f"[CommunityRadar] 抖音里分享给主人失败: {exc}")
                        self._record("error", aweme_id=vid, title=v["title"],
                                     text=f"抖音里分享没成功：{exc}")
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"[CommunityRadar] 抖音里分享异常: {exc}")

        if mode in ("qq", "both") or not via:
            body = f"刷到个有意思的：《{v['title']}》\n{text}\n{v['url']}"
            await self._send(body)
            via.append("qq")
        return {"ok": bool(via), "via": via}

    async def share_video(self, link: str, text: str = "") -> dict:
        """主人明确要求分享某条视频：按 share_to 分发，计入当日份额额。"""
        vid = await self._vid(link)
        if not vid:
            return {"ok": False, "message": "没认出抖音视频链接或视频 id"}
        why = self._blocked("share")
        if why:
            return {"ok": False, "message": why}
        try:
            v = await self.b.detail(vid)
            r = await self._share(v, _clip(text, 120) or "这个给你看看")
        except BrowserError as exc:
            self._on_error(exc)
            return {"ok": False, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"分享时出错：{exc}"}
        finally:
            if self.cfg.get("close_after_run", True):
                try:
                    await self.b.close()
                except Exception:  # noqa: BLE001
                    pass
        if r.get("ok"):
            self.s["share"] = int(self.s.get("share", 0)) + 1
            self._record("share", aweme_id=vid, title=v.get("title", ""), via=r["via"])
            self._save()
        return {"ok": bool(r.get("ok")), "message": "已分享（" + " / ".join(r.get("via", [])) + "）" if r.get("ok") else "没分享出去", "via": r.get("via", [])}

    async def send_dm(self, text: str) -> dict:
        """主人在 QQ 里明确要求时，在抖音私信里给核对过的主人发一句话。"""
        owner = self.owner()
        if not owner:
            return {"ok": False, "message": "先用 /抖音 核对主人 <抖音号或主页链接> 核对身份"}
        why = self._blocked("share")
        if why:
            return {"ok": False, "message": why}
        text = _clip(text, 300)
        if not text:
            return {"ok": False, "message": "私信内容不能为空"}
        if self.cfg.get("dry_run", True):
            self._record("dry:dm", text=text)
            return {"ok": True, "message": "试运行：本来要在抖音私信里发（未真发）"}
        try:
            await self.b.send_dm(text, owner)
        except BrowserError as exc:
            self._on_error(exc)
            return {"ok": False, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"私信发送出错：{exc}"}
        finally:
            if self.cfg.get("close_after_run", True):
                try:
                    await self.b.close()
                except Exception:  # noqa: BLE001
                    pass
        self.s["writes"] = int(self.s.get("writes", 0)) + 1
        self.s["last_write_at"] = time.time()
        self._record("dm", text=text)
        self._save()
        return {"ok": True, "message": "抖音私信已发"}

    # ── 单条动作 ────────────────────────────────────────────────

    async def watch(self, link: str) -> dict:
        try:
            v, cs = await self.b.watch_link(link)
        except BrowserError as exc:
            self._on_error(exc)
            return {"ok": False, "message": str(exc)}
        finally:
            if self.cfg.get("close_after_run", True):
                await self.b.close()
        return {"ok": True, "message": digest_of(v, cs), "video": v}

    async def like(self, link: str) -> dict:
        vid = await self._vid(link)
        if not vid:
            return {"ok": False, "message": "给个抖音视频链接或视频 id"}
        why = self._blocked("like")
        if why:
            return {"ok": False, "message": why}
        try:
            if self.cfg.get("dry_run", True):
                self._record("dry:like", aweme_id=vid)
                return {"ok": True, "message": "试运行：本来要点赞（未真点）"}
            r = await self.b.like(vid)
            self.s["like"] += 1
            self.s["writes"] += 1
            self.s["last_write_at"] = time.time()
            self._record("like", aweme_id=vid)
            self._save()
            return {"ok": True, "message": "已点赞" if not r.get("already") else "之前已经赞过了"}
        except BrowserError as exc:
            self._on_error(exc)
            return {"ok": False, "message": str(exc)}
        finally:
            if self.cfg.get("close_after_run", True):
                await self.b.close()

    async def comment(self, link: str, text: str) -> dict:
        vid = await self._vid(link)
        if not vid:
            return {"ok": False, "message": "给个抖音视频链接或视频 id"}
        why = self._blocked("comment")
        if why:
            return {"ok": False, "message": why}
        try:
            if self.cfg.get("dry_run", True):
                self._record("dry:comment", aweme_id=vid, text=_clip(text, 100))
                return {"ok": True, "message": "试运行：本来要发评论（未真发）"}
            r = await self.b.comment(vid, _clip(text, 100))
            self.s["comment"] += 1
            self.s["writes"] += 1
            self.s["last_write_at"] = time.time()
            self._record("comment", aweme_id=vid, text=_clip(text, 100))
            self._save()
            return {"ok": True, "message": f"评论已发（cid={r.get('cid', '')}）"}
        except BrowserError as exc:
            self._on_error(exc)
            return {"ok": False, "message": str(exc)}
        finally:
            if self.cfg.get("close_after_run", True):
                await self.b.close()

    @staticmethod
    async def _vid(link: str) -> str:
        from .douyin_browser import find_aweme_id
        return await find_aweme_id(link)

    def state(self) -> dict:
        caps = self._caps()
        return {
            "enabled": bool(self.cfg.get("enabled")),
            "dry_run": bool(self.cfg.get("dry_run", True)),
            "auto_browse": bool(self.cfg.get("auto_browse")),
            "busy": self._busy,
            "paused": self._paused(),
            "pause_reason": self.s.get("pause_reason", ""),
            "today": {k: self.s.get(k, 0) for k in ("runs", "watched", "like", "comment", "share", "writes")},
            "caps": caps,
            "share_to": str(self.cfg.get("share_to") or "qq"),
            "owner_input": str(self.cfg.get("owner_input") or ""),
            "owner": {
                "confirmed": bool(self.owner()),
                "nickname": (self.s.get("owner") or {}).get("nickname", ""),
                "sec_uid": (self.s.get("owner") or {}).get("sec_uid", ""),
            },
            "shared_dy": len(self.s.get("shared_dy") or []),
            "browser": self.b.state(),
            "next_run": datetime.datetime.fromtimestamp(self.s["next_due_at"]).strftime("%Y-%m-%d %H:%M")
            if self.s.get("next_due_at") else "未排期",
        }
