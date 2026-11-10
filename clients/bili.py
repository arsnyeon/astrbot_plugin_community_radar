#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bilibili web API 客户端。

仅做「看」类操作：搜索 / 热门 / 视频信息 / 评论；登录后可选发评论、点赞。
请求统一带 UA + Referer，搜索等接口走 WBI 签名，缺 buvid3 时自动申请。
"""
from __future__ import annotations

import hashlib
import json
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


def _fix_pic(url: Any) -> str:
    """B 站封面地址可能是 // 开头或 http，统一成能发的 https 链接。"""
    s = str(url or "").strip()
    if not s:
        return ""
    if s.startswith("//"):
        return "https:" + s
    if s.startswith("http://"):
        return "https://" + s[len("http://"):]
    return s if s.startswith("https://") else ""


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


class WriteUnknown(BiliError):
    """请求可能已到达 B 站服务端，不允许自动重试。"""


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
        return {
            "isLogin": bool(d.get("isLogin")),
            "uname": d.get("uname") or "",
            "mid": int(d.get("mid") or 0),
        }

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
                "pic": _fix_pic(v.get("pic")),
            })
        return out

    async def feed(self, ps: int = 10) -> list[dict]:
        """首页推荐流（刷 B 站用）。"""
        await self.ensure_buvid()
        url = await self._signed(
            "https://api.bilibili.com/x/web-interface/wbi/index/top/feed/rcmd",
            {
                "web_location": "1430650", "y_num": 3, "fresh_type": 4,
                "feed_version": "V8", "fresh_idx": 1, "fresh_idx_1h": 1,
                "fetch_row": 1, "brush": 0, "homepage_ver": 1, "ps": max(1, min(ps, 30)),
            },
        )
        d = await self._get(url) or {}
        out = []
        for it in (d.get("item") or []):
            if it.get("goto") not in ("av", None) or not it.get("bvid"):
                continue
            owner = it.get("owner") or {}
            stat = it.get("stat") or {}
            out.append({
                "bvid": it.get("bvid"),
                "aid": it.get("id") or it.get("aid"),
                "title": it.get("title"),
                "author": owner.get("name"),
                "play": int(stat.get("view") or 0),
                "like": int(stat.get("like") or 0),
                "desc": str(it.get("desc") or "")[:200],
                "duration": it.get("duration"),
                "pic": _fix_pic(it.get("pic")),
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
                "pic": _fix_pic(v.get("pic")),
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
            "mid": (d.get("owner") or {}).get("mid"),
            "play": int(stat.get("view") or 0),
            "like": int(stat.get("like") or 0),
            "reply": int(stat.get("reply") or 0),
            "pubdate": int(d.get("pubdate") or 0),
            "duration": ((d.get("pages") or [{}])[0].get("duration") or d.get("duration")),
            "total_duration": d.get("duration"),
            "parts": len(d.get("pages") or []),
            "tname": d.get("tname"),
            "pic": _fix_pic(d.get("pic")),
        }

    async def _post_form(self, url: str, data: dict) -> Any:
        try:
            r = await self._client.post(url, data=data, headers=self._headers())
        except httpx.TransportError as exc:
            raise WriteUnknown("发送结果未知，请先在平台核对，不自动重试") from exc
        try:
            j = r.json()
        except Exception as exc:  # noqa: BLE001
            raise WriteUnknown(f"发送响应无法确认（HTTP {r.status_code}）") from exc
        if not isinstance(j, dict) or "code" not in j:
            raise WriteUnknown("发送响应缺少结果码")
        if j.get("code") != 0:
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

    async def send_msg(self, receiver_mid: int, text: str) -> dict:
        """给某个 UID 发 B 站私信（需要登录态 + 对方允许接收私信）。"""
        import secrets

        csrf = self._csrf()
        nav = await self.nav()
        sender_mid = int(nav.get("mid") or 0)
        if not sender_mid:
            raise BiliError("拿不到自己的 B 站 UID（登录态可能失效）")
        content = json.dumps({"content": str(text)[:500]}, ensure_ascii=False)
        data = {
            "msg[sender_uid]": str(sender_mid),
            "msg[receiver_id]": str(int(receiver_mid)),
            "msg[dev_id]": secrets.token_hex(8),
            "msg[content]": content,
            "msg[msg_type]": "1",
            "msg[timestamp]": str(int(time.time() * 1000)),
            "msg[msg_status]": "0",
            "msg[new_face]": "0",
            "msg[reply_event]": "0",
            "build": "0",
            "mobi_app": "web",
            "csrf": csrf,
            "csrf_token": csrf,
        }
        d = await self._post_form("https://api.vc.bilibili.com/web_im/v1/web_im/send_msg", data)
        return {"ok": True, "data": d}

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
        segments = await self.subtitle_segments(bvid, cid)
        return " ".join(s["text"] for s in segments)

    async def subtitle_segments(self, bvid: str, cid: str) -> list[dict]:
        if not bvid or not cid:
            return []
        url = await self._signed(
            "https://api.bilibili.com/x/player/wbi/v2", {"bvid": bvid, "cid": cid}
        )
        d = await self._get(url) or {}
        subs = ((d.get("subtitle") or {}).get("subtitles")) or []
        if not subs:
            return []
        pick = next(
            (s for s in subs if "zh" in str(s.get("lan") or s.get("lan_doc")).lower()),
            subs[0],
        )
        sub_url = str(pick.get("subtitle_url") or "")
        if not sub_url:
            return []
        if sub_url.startswith("//"):
            sub_url = "https:" + sub_url
        try:
            r = await self._client.get(sub_url, headers={"User-Agent": UA})
            body = (r.json() or {}).get("body") or []
        except Exception:  # noqa: BLE001
            return []
        segments = []
        for item in body:
            if not isinstance(item, dict):
                continue
            try:
                start, end = float(item.get("from", 0)), float(item.get("to", 0))
            except (TypeError, ValueError):
                continue
            text = str(item.get("content") or "").strip()
            if text and 0 <= start < end:
                segments.append({"start": start, "end": end, "text": text})
        return sorted(segments, key=lambda s: s["start"])

    async def resolve_short(self, url: str) -> str:
        """b23.tv 短链跟一次跳转，返回真实地址。"""
        try:
            r = await self._client.get(url, headers={"User-Agent": UA}, follow_redirects=True)
            return str(r.url)
        except Exception:  # noqa: BLE001
            return ""

    async def play_url(self, bvid: str, cid: str, aid: str = "", qn: int = 32) -> str:
        """取一个可直接下载的视频流地址（优先渐进式 mp4，其次 DASH）。"""
        return (await self.play_sources(bvid, cid, aid, qn)).get("video", "")

    async def play_sources(self, bvid: str, cid: str, aid: str = "", qn: int = 32) -> dict:
        if not cid:
            return {}
        params = {"bvid": bvid, "cid": cid, "qn": qn, "fnval": 16, "fnver": 0, "fourk": 0}
        if aid:
            params["avid"] = aid
        url = await self._signed("https://api.bilibili.com/x/player/wbi/playurl", params)
        d = await self._get(url) or {}
        for item in (d.get("durl") or []):
            u = str(item.get("url") or "")
            if u:
                return {"video": u, "audio": u}
        vids = ((d.get("dash") or {}).get("video")) or []
        audios = ((d.get("dash") or {}).get("audio")) or []
        pick = lambda values: str(values[0].get("baseUrl") or values[0].get("base_url") or "") if values else ""
        return {"video": pick(vids), "audio": pick(audios)}

    async def following_updates(self, limit: int = 10) -> list[dict]:
        if not self.cookie:
            return []
        data = await self._get("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all",
                               {"type": "video", "offset": "", "timezone_offset": -480})
        out = []
        for item in (data or {}).get("items") or []:
            modules = item.get("modules") or {}
            author = modules.get("module_author") or {}
            archive = ((modules.get("module_dynamic") or {}).get("major") or {}).get("archive") or {}
            if not archive.get("bvid"):
                continue
            out.append({"bvid": archive["bvid"], "title": archive.get("title") or "",
                        "author": author.get("name") or "", "mid": author.get("mid"),
                        "pic": _fix_pic(archive.get("cover")), "desc": archive.get("desc") or "",
                        "_via": "关注更新"})
        return out[:max(0, min(30, limit))]

    async def extra_action(self, action: str, target: str, text: str = "", folder_id: str = "") -> dict:
        csrf = self._csrf()
        if action == "follow":
            return await self._post_form("https://api.bilibili.com/x/relation/modify",
                                         {"fid": target, "act": 1, "re_src": 11, "csrf": csrf})
        if action == "dynamic":
            payload = {"dyn_req": {"content": {"contents": [{"raw_text": text, "type": 1, "biz_id": ""}]},
                                   "scene": 1}, "csrf": csrf}
            try:
                response = await self._client.post(
                    "https://api.bilibili.com/x/dynamic/feed/create/dyn", params={"csrf": csrf},
                    json=payload, headers=self._headers())
                data = response.json()
            except (httpx.TransportError, ValueError) as exc:
                raise WriteUnknown("动态发送结果未知，不自动重试") from exc
            if not isinstance(data, dict) or "code" not in data:
                raise WriteUnknown("动态结果无法确认")
            if data["code"] != 0:
                raise BiliError(f"动态发布失败：{data['code']}")
            return data.get("data") or {}
        info = await self.view(target)
        aid = str(info.get("aid") or "")
        if action == "coin":
            return await self._post_form("https://api.bilibili.com/x/web-interface/coin/add",
                                         {"aid": aid, "multiply": 1, "select_like": 0, "csrf": csrf})
        if action == "favorite":
            if not folder_id.isdigit():
                raise BiliError("请先配置收藏夹 ID")
            return await self._post_form("https://api.bilibili.com/x/v3/fav/resource/deal",
                                         {"rid": aid, "type": 2, "add_media_ids": folder_id, "csrf": csrf})
        raise BiliError("不支持的动作")

    async def season(self, season_id: str = "", ep_id: str = "") -> dict:
        response = await self._client.get("https://api.bilibili.com/pgc/view/web/season",
                                         params={"season_id": season_id} if season_id else {"ep_id": ep_id},
                                         headers=self._headers())
        data = response.json()
        if data.get("code") != 0:
            raise BiliError(f"番剧详情不可用：{data.get('code')}")
        return data.get("result") or {}

    async def search_seasons(self, keyword: str) -> list[dict]:
        url = await self._signed("https://api.bilibili.com/x/web-interface/wbi/search/type",
                                 {"search_type": "media_bangumi", "keyword": keyword})
        return (await self._get(url) or {}).get("result") or []

    async def episode_sources(self, ep_id: str) -> dict:
        response = await self._client.get("https://api.bilibili.com/pgc/player/web/playurl",
                                         params={"ep_id": ep_id, "qn": 32, "fnval": 16},
                                         headers=self._headers())
        data = response.json()
        if data.get("code") != 0:
            raise BiliError(f"本账号无法播放这一集：{data.get('code')}")
        result = data.get("result") or data.get("data") or {}
        direct = result.get("durl") or []
        if direct:
            return {"video": direct[0].get("url", ""), "audio": direct[0].get("url", "")}
        dash = result.get("dash") or {}
        pick = lambda rows: str(rows[0].get("baseUrl") or rows[0].get("base_url") or "") if rows else ""
        return {"video": pick(dash.get("video") or []), "audio": pick(dash.get("audio") or [])}
