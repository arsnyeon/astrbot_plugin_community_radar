#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自主决策：这一轮逛哪儿、搜什么、看多深、要不要带图、说给谁听。

和 duty.py 的分工：
- duty.py 负责「怎么逛」（调接口、拿数据、组文案、发出去）；
- autopilot.py 负责「想不想逛、逛哪、给谁看」这类判断。

所有方法都设计成「失败就退回保守默认值」，任何一个模型调用挂掉，
浏览本身都不会被拖死。
"""
from __future__ import annotations

import json
import random
import re
from typing import Any

from ..core.owners import owner_umo as resolve_owner_umo

PLATFORMS = ("bili", "heybox", "tieba", "douyin")

PLATFORM_LABEL = {
    "bili": "B 站",
    "heybox": "小黑盒",
    "tieba": "百度贴吧",
    "douyin": "抖音",
}


def _parse_json_object(text: str) -> dict:
    s = str(text or "")
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _parse_json_array(text: str) -> list:
    s = str(text or "")
    m = re.search(r"\[[\s\S]*\]", s)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def _norm_image_url(value: Any) -> str:
    """把常见的封面地址（// 开头、http、http+转义）统一成 https。"""
    s = str(value or "").strip()
    if not s:
        return ""
    if s.startswith("//"):
        return "https:" + s
    if s.startswith("http://"):
        return "https://" + s[len("http://"):]
    return s if s.startswith("https://") else ""


class Autopilot:
    def __init__(self, plugin: Any):
        self.plugin = plugin

    # ── 平台选择 ────────────────────────────────────────────────

    def _enabled_platforms(self, cfg: dict) -> list[str]:
        """按 platforms 配置 + 平台自身开关，算出这次能碰的平台。"""
        raw = str(cfg.get("platforms") or "all").lower()
        if raw == "bili":
            want = ["bili"]
        elif raw == "heybox":
            want = ["heybox"]
        elif raw == "tieba":
            want = ["tieba"]
        elif raw == "douyin":
            want = ["douyin"]
        elif raw == "both":
            want = ["bili", "heybox"]
        else:
            want = list(PLATFORMS)
        # 抖音依赖浏览器写层；没启用时不要把它选成本轮重点，否则整轮白跑。
        douyin_cfg = (getattr(self.plugin, "config", {}) or {}).get("douyin") or {}
        if not douyin_cfg.get("enabled"):
            want = [p for p in want if p != "douyin"]
        # 贴吧没有配置任何吧、也没有可搜索词时不参与挑选。
        tieba_cfg = (getattr(self.plugin, "config", {}) or {}).get("tieba") or {}
        if not tieba_cfg.get("enable", False):
            want = [p for p in want if p != "tieba"]
        return want

    def plan(self, cfg: dict, *, topics: list[dict] | None = None) -> dict:
        """这一轮逛哪儿。返回值一定会是可执行的保守计划。

        返回：
            platforms: 实际要拉数据的平台列表
            focus:     这次偏重哪个平台（用于文案口吻），可能为 ""
            keywords:  这轮要搜的词（话题队列优先）
            deep:      是否值得深看（多补几条正文/评论）
            reason:    为什么这么逛（只写日志，不发出去）
        """
        topics = topics or []
        enabled = self._enabled_platforms(cfg)
        if not enabled:
            return {"platforms": [], "focus": "", "keywords": [], "deep": False, "reason": ""}

        # 话题自带上平台倾向；有倾向的优先，没有就用启用集合
        platforms = list(enabled)
        focus = ""
        for t in topics:
            p = str(t.get("platform") or "any").lower()
            if p in enabled:
                platforms = [p]
                focus = p
                break

        keywords: list[str] = []
        for t in topics:
            kw = str(t.get("keyword") or "").strip()
            if kw and kw not in keywords:
                keywords.append(kw)
            if len(keywords) >= 3:
                break

        reason = f"跟话题「{keywords[0]}」" if keywords else "随便逛逛"

        # 没有话题时，按权重随机偏重一个平台，避免每次都是同一套
        if not focus and len(platforms) > 1:
            weights = cfg.get("platform_weights") or {}
            pool: list[str] = []
            for p in platforms:
                try:
                    w = int(weights.get(p, 1))
                except Exception:  # noqa: BLE001
                    w = 1
                pool += [p] * max(0, min(w, 5))
            focus = random.choice(pool) if pool else ""
            if focus:
                platforms = [focus]
                reason = f"今天更想刷{PLATFORM_LABEL.get(focus, focus)}"
            else:
                platforms = []

        return {
            "platforms": platforms,
            "focus": focus,
            "keywords": keywords,
            "reason": reason,
        }

    # ── 配图 ────────────────────────────────────────────────────

    @staticmethod
    def image_of(item: dict) -> str:
        """从一条内容里挖出一个能当配图用的地址。"""
        for key in ("pic", "cover", "image", "thumbnail"):
            v = _norm_image_url(item.get(key))
            if v:
                return v
        imgs = item.get("images")
        if isinstance(imgs, list):
            for v in imgs:
                v = _norm_image_url(v)
                if v:
                    return v
        return ""

    def pick_image(self, cfg: dict, items: list[dict]) -> str:
        """挑一张图发出去；关掉配图或没有可用图时返回空串。"""
        if not cfg.get("attach_image", True):
            return ""
        for it in items:
            url = self.image_of(it)
            if url:
                return url
        return ""

    # ── 投递目标 ────────────────────────────────────────────────

    def targets(self, cfg: dict, *, group_cfg: dict | None = None) -> list[str]:
        """这一轮发给谁。

        - 固定 targets 优先，配置了就只发那些；
        - 否则私聊主人；
        - 群聊分享由 group_cfg 的开关控制，命中时追加。
        """
        fixed = [str(x).strip() for x in (cfg.get("targets") or []) if str(x).strip()]
        if fixed:
            return fixed

        out: list[str] = []
        try:
            plat = self.plugin._platform_name()
        except Exception:  # noqa: BLE001
            plat = "default"
        # 主人私聊目标统一解析：填了 admin_uin 用它，留空则用 AstrBot 全局管理员。
        owner_umo = resolve_owner_umo(self.plugin)
        if owner_umo:
            out.append(owner_umo)

        group_cfg = group_cfg or {}
        if group_cfg.get("enable", True):
            for g in group_cfg.get("sessions") or []:
                g = str(g).strip()
                if not g:
                    continue
                if ":" not in g:
                    g = f"{plat}:GroupMessage:{g}"
                if g not in out:
                    out.append(g)
        return out

    def wants_group(self, cfg: dict, group_cfg: dict | None = None) -> bool:
        """这轮要不要顺便在群里说一句。"""
        group_cfg = group_cfg or {}
        if not group_cfg.get("enable", True):
            return False
        if not (group_cfg.get("sessions") or []):
            return False
        try:
            p = float(group_cfg.get("probability", 0.35))
        except Exception:  # noqa: BLE001
            p = 0.35
        return random.random() < max(0.0, min(p, 1.0))
