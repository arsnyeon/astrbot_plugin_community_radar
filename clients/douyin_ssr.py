#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抖音分享页（SSR）解析：免登录、免签名，读出视频地址 / 图文图片 / 文案。

分享页 ``https://www.iesdouyin.com/share/video/<aweme_id>/`` 会把作品数据
以 ``window._ROUTER_DATA`` 的形式直接渲染在 HTML 里，这正是浏览器之外
拿到「视频真实地址」和「图文原图列表」的最省事方式。
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
_URLS = (
    "https://www.iesdouyin.com/share/video/{vid}/",
    "https://www.douyin.com/share/video/{vid}/",
)


def find_item(obj: Any) -> dict | None:
    """在 SSR 数据里找出作品条目（结构会随版本变化，按特征找）。"""
    if isinstance(obj, dict):
        if obj.get("aweme_id") and (
            obj.get("video") is not None
            or obj.get("images") is not None
            or obj.get("desc") is not None
        ):
            return obj
        items = obj.get("item_list")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
        for v in obj.values():
            hit = find_item(v)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = find_item(v)
            if hit:
                return hit
    return None


def images_of(item: dict) -> list[str]:
    """图文的原图地址列表。"""
    out: list[str] = []
    for img in item.get("images") or []:
        if not isinstance(img, dict):
            continue
        urls = img.get("url_list") or []
        for u in urls:
            if isinstance(u, str) and u.startswith("http"):
                out.append(u)
                break
    return out


def video_url_of(item: dict) -> str:
    """视频的真实播放地址（优先无水印 play_addr）。"""
    video = item.get("video") or {}
    for key in ("play_addr", "play_addr_h264", "play_addr_265", "download_addr"):
        addr = video.get(key)
        if isinstance(addr, dict):
            for u in (addr.get("url_list") or []):
                if isinstance(u, str) and u.startswith("http"):
                    return u.replace("playwm", "play")
    return ""


def stats_of(item: dict) -> dict:
    st = item.get("statistics") or {}
    author = item.get("author") or {}
    return {
        "desc": re.sub(r"\s+", " ", str(item.get("desc") or "")).strip(),
        "author": str(author.get("nickname") or ""),
        "likes": int(st.get("digg_count") or 0),
        "comments": int(st.get("comment_count") or 0),
        "is_images": bool(item.get("images")),
        "duration_ms": int((item.get("video") or {}).get("duration") or 0),
    }


async def fetch(aweme_id: str, timeout: float = 12.0) -> dict | None:
    """抓分享页并解析出作品条目；失败返回 None。"""
    vid = str(aweme_id or "").strip()
    if not re.fullmatch(r"\d{8,}", vid):
        return None
    headers = {"User-Agent": UA, "Referer": "https://www.douyin.com/"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        for tpl in _URLS:
            try:
                r = await client.get(tpl.format(vid=vid))
            except Exception:  # noqa: BLE001
                continue
            if r.status_code != 200 or not r.text:
                continue
            m = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", r.text, re.S)
            if not m:
                continue
            try:
                data = json.loads(m.group(1))
            except Exception:  # noqa: BLE001
                continue
            item = find_item(data)
            if item:
                return item
    return None
