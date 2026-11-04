#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bilibili web API 客户端。

仅做「看」类操作：搜索 / 热门 / 视频信息 / 评论；登录后可选发评论、点赞。
请求统一带 UA + Referer，搜索等接口走 WBI 签名，缺 buvid3 时自动申请。
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Optional
from urllib.parse import quote

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
REFERER = "https://www.bilibili.com/"

# B 站 WBI 签名 mixin 表
_MIXIN = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5,
    49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55,
    40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57,
    62, 11, 36, 20, 34, 44, 52,
]

_BV_RE = re.compile(r"\b(BV[0-9A-Za-z]{10})\b")
_AV_RE = re.compile(r"\bav(\d{3,})\b", re.I)
_SHORT_RE = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+")

_COOKIE_KEYS = (
    "SESSDATA", "bili_jct", "DedeUserID", "buvid3", "buvid4",
    "DedeUserID__ckMd5", "sid",
)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _enc(value: Any) -> str:
    """等价于 JS 的 ``encodeURIComponent(v.replace(/[!'()*]/g, ''))``。"""
    v = str(value)
    for ch in "!'()*":
        v = v.replace(ch, "")
    return quote(v, safe="")


def parse_cookie(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in str(raw or "").split(";"):
        i = part.find("=")
        if i <= 0:
            continue
        k = part[:i].strip()
        if k in _COOKIE_KEYS:
            out[k] = part[i + 1 :].strip()
    return out


def find_video_id(text: str) -> Optional[dict[str, str]]:
    s = str(text or "")
    m = _BV_RE.search(s)
    if m:
        return {"bvid": m.group(1)}
    m = _AV_RE.search(s)
    if m:
        return {"aid": m.group(1)}
    m = _SHORT_RE.search(s)
    if m:
        return {"short": m.group(0)}
    return None


class BiliError(RuntimeError):
    pass


class BiliClient:
    def __init__(self, cookie: str = "", timeout: float = 15.0):
        self.cookie = str(cookie or "").strip()
        self.timeout = timeout
        self._buvid = ""
        self._wbi: Optional[tuple[str, str, float]] = None  # (img, sub, at)
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        h = {"User-Agent": UA, "Referer": REFERER, "Accept": "application/json, text/plain, */*"}
        if self.cookie:
            h["Cookie"] = self.cookie
        return h

    async def _get(self, url: str, params: dict | None = None) -> Any:
        r = await self._client.get(url, params=params, headers=self._headers())
        try:
            j = r.json()
        except Exception as exc:  # noqa: BLE001
            raise BiliError(f"响应不是 JSON（HTTP {r.status_code}）") from exc
        if j.get("code") not in (0, None):
            raise BiliError(f"B 站接口错误 {j.get('code')}: {j.get('message')}")
        return j.get("data")

    async def ensure_buvid(self) -> None:
        if self._buvid or parse_cookie(self.cookie).get("buvid3"):
            return
        try:
            d = await self._get("https://api.bilibili.com/x/frontend/finger/spi")
            self._buvid = (d or {}).get("b_3") or ""
        except Exception:  # noqa: BLE001
            pass

    async def nav(self) -> dict[str, Any]:
        r = await self._client.get(
            "https://api.bilibili.com/x/web-interface/nav", headers=self._headers()
        )
        j = r.json()
        d = j.get("data") or {}
        wbi_img = d.get("wbi_img") or {}
        img = str(wbi_img.get("img_url") or "").split("/")[-1].split(".")[0]
        sub = str(wbi_img.get("sub_url") or "").split("/")[-1].split(".")[0]
        if img and sub:
            self._wbi = (img, sub, time.time())
        return {"isLogin": bool(d.get("isLogin")), "uname": d.get("uname") or ""}

    async def _signed(self, base: str, params: dict) -> str:
        if self._wbi is None or time.time() - self._wbi[2] > 6 * 3600:
            await self.nav()
        if not self._wbi:
            raise BiliError("拿不到 B 站的 WBI 签名 key")
        img, sub, _ = self._wbi
        raw = img + sub
        mixin = "".join(raw[i] for i in _MIXIN)[:32]
        p = dict(params)
        p["wts"] = int(time.time())
        query = "&".join(
            f"{_enc(k)}={_enc(p[k])}" for k in sorted(p)
        )
        return f"{base}?{query}&w_rid={_md5(query + mixin)}"

    async def search(self, keyword: str, page: int = 1) -> list[dict]:
        await self.ensure_buvid()
        url = await self._signed(
            "https://api.bilibili.com/x/web-interface/wbi/search/type",
            {"search_type": "video", "keyword": keyword, "page": page},
        )
        d = await self._get(url)
        out = []
        for v in (d or {}).get("result") or []:
            out.append({
                "bvid": v.get("bvid"),
                "aid": v.get("aid"),
                "title": re.sub(r"<[^>]+>", "", v.get("title") or ""),
                "author": v.get("author"),
                "play": int(v.get("play") or 0),
                "pubdate": int(v.get("pubdate") or 0),
                "duration": v.get("duration"),
                "desc": re.sub(r"<[^>]+>", "", v.get("description") or "")[:200],
            })
        return out

    async def popular(self, ps: int = 20) -> list[dict]:
        await self.ensure_buvid()
        d = await self._get(
            "https://api.bilibili.com/x/web-interface/popular", {"ps": ps, "pn": 1}
        )
        out = []
        for v in (d or {}).get("list") or []:
            stat = v.get("stat") or {}
            out.append({
                "bvid": v.get("bvid"),
                "aid": v.get("aid"),
                "title": v.get("title"),
                "author": (v.get("owner") or {}).get("name"),
                "play": int(stat.get("view") or 0),
                "pubdate": int(v.get("pubdate") or 0),
                "desc": str(v.get("desc") or "")[:200],
            })
        return out

    async def view(self, bvid: str = "", aid: str = "") -> dict:
        await self.ensure_buvid()
        params = {"bvid": bvid} if bvid else {"aid": aid}
        url = await self._signed("https://api.bilibili.com/x/web-interface/wbi/view", params)
        d = await self._get(url) or {}
        stat = d.get("stat") or {}
        return {
            "bvid": d.get("bvid"),
            "aid": d.get("aid"),
            "cid": d.get("cid"),
            "title": d.get("title"),
            "desc": str(d.get("desc") or "")[:600],
            "author": (d.get("owner") or {}).get("name"),
            "play": int(stat.get("view") or 0),
            "like": int(stat.get("like") or 0),
            "reply": int(stat.get("reply") or 0),
            "pubdate": int(d.get("pubdate") or 0),
            "duration": d.get("duration"),
            "tname": d.get("tname"),
        }

    async def _post_form(self, url: str, data: dict) -> Any:
        r = await self._client.post(url, data=data, headers=self._headers())
        try:
            j = r.json()
        except Exception as exc:  # noqa: BLE001
            raise BiliError(f"响应不是 JSON（HTTP {r.status_code}）") from exc
        if j.get("code") not in (0, None):
            raise BiliError(f"B 站接口错误 {j.get('code')}: {j.get('message')}")
        return j.get("data")

    def _csrf(self) -> str:
        csrf = parse_cookie(self.cookie).get("bili_jct")
        if not csrf:
            raise BiliError("未登录 B 站（Cookie 需含 bili_jct）")
        return csrf

    async def post_comment(self, aid: str, message: str, root: str = "", parent: str = "") -> dict:
        data = {"type": 1, "oid": aid, "message": message, "plat": 1, "csrf": self._csrf()}
        if root:
            data["root"] = root
            data["parent"] = parent or root
        d = await self._post_form("https://api.bilibili.com/x/v2/reply/add", data)
        return {"rpid": (d or {}).get("rpid")}

    async def like_video(self, aid: str) -> bool:
        await self._post_form(
            "https://api.bilibili.com/x/web-interface/archive/like",
            {"aid": aid, "like": 1, "csrf": self._csrf()},
        )
        return True

    async def comments(self, aid: str, limit: int = 12) -> list[dict]:
        url = await self._signed(
            "https://api.bilibili.com/x/v2/reply/wbi/main",
            {"type": 1, "oid": aid, "mode": 3, "plat": 1, "web_location": 1315875},
        )
        d = await self._get(url) or {}
        out = []
        for r in (d.get("replies") or [])[:limit]:
            out.append({
                "rpid": r.get("rpid"),
                "name": (r.get("member") or {}).get("uname"),
                "text": str((r.get("content") or {}).get("message") or "")[:200],
                "like": r.get("like"),
                "replies": r.get("rcount") or 0,
            })
        return out

    # ── 视频理解：字幕 / 播放地址 ────────────────────────────────

    async def subtitles(self, bvid: str, cid: str) -> str:
        """拿视频字幕（CC）全文；没有字幕或需要登录时返回空串。"""
        if not bvid or not cid:
            return ""
        url = await self._signed(
            "https://api.bilibili.com/x/player/wbi/v2", {"bvid": bvid, "cid": cid}
        )
        d = await self._get(url) or {}
        subs = ((d.get("subtitle") or {}).get("subtitles")) or []
        if not subs:
            return ""
        pick = next(
            (s for s in subs if "zh" in str(s.get("lan") or s.get("lan_doc")).lower()),
            subs[0],
        )
        sub_url = str(pick.get("subtitle_url") or "")
        if not sub_url:
            return ""
        if sub_url.startswith("//"):
            sub_url = "https:" + sub_url
        try:
            r = await self._client.get(sub_url, headers={"User-Agent": UA})
            body = (r.json() or {}).get("body") or []
        except Exception:  # noqa: BLE001
            return ""
        text = " ".join(str(x.get("content") or "") for x in body if isinstance(x, dict))
        return re.sub(r"\s+", " ", text).strip()

    async def resolve_short(self, url: str) -> str:
        """b23.tv 短链跟一次跳转，返回真实地址。"""
        try:
            r = await self._client.get(url, headers={"User-Agent": UA}, follow_redirects=True)
            return str(r.url)
        except Exception:  # noqa: BLE001
            return ""

    async def play_url(self, bvid: str, cid: str, aid: str = "", qn: int = 32) -> str:
        """取一个可直接下载的视频流地址（优先渐进式 mp4，其次 DASH）。"""
        if not cid:
            return ""
        params = {"bvid": bvid, "cid": cid, "qn": qn, "fnval": 1, "fnver": 0, "fourk": 1}
        if aid:
            params["avid"] = aid
        url = await self._signed("https://api.bilibili.com/x/player/wbi/playurl", params)
        d = await self._get(url) or {}
        for item in (d.get("durl") or []):
            u = str(item.get("url") or "")
            if u:
                return u
        vids = ((d.get("dash") or {}).get("video")) or []
        if vids:
            return str(vids[0].get("baseUrl") or vids[0].get("base_url") or "")
        return ""
