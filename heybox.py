#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小黑盒 web 接口客户端。

hkey 签名、_rnd、通用参数、请求底座。
写操作需要 Cookie（pkey / x_xhh_tokenid）；读操作不需要登录。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

API_BASE = "https://api.xiaoheihe.cn"
WEB_BASE = "https://www.xiaoheihe.cn"

ALPHABET = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"
HMAC_SALT = "Z7mFG4tQp9Ws2LxB8H"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

BASE_PARAMS = {
    "os_type": "web",
    "app": "heybox",
    "client_type": "web",
    "version": "999.0.4",
    "web_version": "3.0",
    "x_client_type": "web",
    "x_app": "heybox_website",
    "x_os_type": "Windows",
    "device_info": "Chrome",
    "device_id": "2c2fef8385ccef915e3b3caf94e3aa06",
}

AUTH_STATUS = {"relogin", "not_login", "need_login", "login_required", "need_google_check"}
CAPTCHA_STATUS = {"show_captcha", "need_captcha"}


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _dbl(v: int) -> int:
    return ((v << 1) ^ (27 if v & 128 else 0)) & 255


def _thr(v: int) -> int:
    return _dbl(v) ^ v


def _nin(v: int) -> int:
    return _thr(_dbl(v))


def _elv(v: int) -> int:
    return _nin(_thr(_dbl(v)))


def _tht(v: int) -> int:
    return _elv(v) ^ _nin(v) ^ _thr(v)


def _mix4(a: int, b: int, c: int, d: int) -> list[int]:
    return [
        _tht(a) ^ _elv(b) ^ _nin(c) ^ _thr(d),
        _thr(a) ^ _tht(b) ^ _elv(c) ^ _nin(d),
        _nin(a) ^ _thr(b) ^ _tht(c) ^ _elv(d),
        _elv(a) ^ _nin(b) ^ _thr(c) ^ _tht(d),
    ]


def _map_chars(value: Any, alphabet: str) -> str:
    return "".join(alphabet[ord(ch) % len(alphabet)] for ch in str(value))


def hkey(path: str, timestamp: int, nonce: str) -> str:
    norm = "/" + "/".join([p for p in str(path).split("/") if p]) + "/"
    comps = [
        _map_chars(timestamp, ALPHABET[:-2]),
        _map_chars(norm, ALPHABET),
        _map_chars(nonce, ALPHABET),
    ]
    max_len = max(len(c) for c in comps)
    interleaved = "".join(
        comps[j][k] for k in range(max_len) for j in range(3) if k < len(comps[j])
    )
    digest = _md5(interleaved[:20])
    tail = [ord(ch) for ch in digest[-6:]]
    mixed = _mix4(tail[0], tail[1], tail[2], tail[3]) + [tail[4], tail[5]]
    checksum = str(sum(mixed) % 100).zfill(2)
    return _map_chars(digest[:5], ALPHABET[:-4]) + checksum


def _make_nonce(timestamp: int) -> str:
    return _md5(f"{timestamp}{time.time()}{uuid.uuid4().hex}").upper()


def _rnd_signature(nonce: str, timestamp: int) -> str:
    message = f"{HMAC_SALT}{nonce}{timestamp}:{nonce}"
    digest = hmac.new(HMAC_SALT.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return "15:" + digest


def _sign_params(path: str, timestamp: int, nonce: str) -> dict[str, str]:
    return {"hkey": hkey(path, timestamp, nonce), "_time": str(timestamp), "nonce": nonce}


class HeyboxError(RuntimeError):
    def __init__(self, message: str, code: str = "api"):
        super().__init__(message)
        self.code = code


class HeyboxClient:
    def __init__(self, cookie: str = "", interval: float = 0.9, timeout: float = 25.0):
        self.cookie = str(cookie or "").strip()
        self.interval = interval
        self.timeout = timeout
        self._last_at = 0.0
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def logged_in(self) -> bool:
        return "pkey=" in self.cookie or "user_pkey=" in self.cookie

    @property
    def heybox_id(self) -> str:
        m = __import__("re").search(r"heybox_id=(\d+)", self.cookie)
        return m.group(1) if m else ""

    async def request(
        self,
        path: str,
        params: Optional[dict] = None,
        method: str = "GET",
        body: Optional[dict] = None,
        signed: bool = True,
        workshop: bool = False,
    ) -> Any:
        import re as _re

        if not path.startswith("/"):
            raise HeyboxError("path 必须以 / 开头", "input")
        if method != "GET" and not self.logged_in:
            raise HeyboxError("写操作需要登录：先在插件配置里填小黑盒 Cookie", "auth")

        wait = self.interval - (time.time() - self._last_at)
        if wait > 0:
            import asyncio
            await asyncio.sleep(wait)
        self._last_at = time.time()

        query: dict[str, Any] = dict(BASE_PARAMS)
        if self.heybox_id:
            query["heybox_id"] = self.heybox_id
        if params:
            query.update(params)
        if signed:
            ts = int(time.time())
            nonce = _make_nonce(ts)
            query.update(_sign_params(path, ts, nonce))
            if workshop:
                query["_rnd"] = _rnd_signature(nonce, ts)

        url = API_BASE + path
        headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": WEB_BASE + "/",
            "Origin": WEB_BASE,
        }
        if self.cookie:
            headers["Cookie"] = self.cookie

        if method == "POST" and body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            resp = await self._client.post(url, params=query, headers=headers, data=body)
        else:
            resp = await self._client.get(url, params=query, headers=headers)

        if resp.status_code in (401, 403):
            raise HeyboxError("小黑盒返回 401/403：登录态可能失效", "auth")
        if resp.status_code == 429:
            raise HeyboxError("小黑盒限流（429）", "rate_limit")

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise HeyboxError(f"响应不是 JSON（HTTP {resp.status_code}）", "protocol") from exc

        status = data.get("status")
        if status == "ok":
            return data
        if status in AUTH_STATUS:
            raise HeyboxError(f"登录态失效（{status}）：请重新登录", "auth")
        if status in CAPTCHA_STATUS:
            raise HeyboxError(f"小黑盒要求验证码（{status}）：请到网页完成后重试", "captcha")
        raise HeyboxError(f"小黑盒拒绝（{status or 'no-status'}）：{data.get('msg') or ''}", "api")

    async def get(self, path: str, params: Optional[dict] = None) -> Any:
        return await self.request(path, params=params, method="GET")

    async def post(self, path: str, body: dict, params: Optional[dict] = None) -> Any:
        return await self.request(path, params=params, method="POST", body=body, workshop=True)

    async def like_link(self, link_id: str) -> bool:
        await self.post("/bbs/app/profile/award/link", {"link_id": link_id, "award_type": "1"})
        return True

    async def comment_link(self, link_id: str, text: str, root_id: str = "-1", reply_id: str = "-1") -> bool:
        t = str(text or "").strip()[:100]
        if not t:
            raise HeyboxError("评论内容为空", "input")
        await self.post("/bbs/app/comment/create", {
            "link_id": link_id, "text": t,
            "root_id": str(root_id), "reply_id": str(reply_id), "is_cy": "0",
        })
        return True

    async def post_text(self, title: str, text: str, topic_id: str = "7214") -> dict:
        import html as _html

        if not self.logged_in:
            raise HeyboxError("写操作需要登录：先在插件配置里填小黑盒 Cookie", "auth")
        t = str(text or "").strip()
        if not t:
            raise HeyboxError("正文为空", "input")
        esc = _html.escape(t)
        paras = [p.strip() for p in esc.split("\n") if p.strip()]
        html_body = "".join(f"<p>{p}</p>" for p in paras)
        blocks = [{"type": "html", "text": html_body}]
        body = {
            "link_tag": "11",
            "text": json.dumps(blocks, ensure_ascii=False),
            "title": str(title or "").strip()[:40] or t[:20],
            "desc": " ".join(t.split())[:80],
            "words_count": str(len(t)),
            "topic_ids": topic_id,
            "hashtags": "[]",
            "schedule_ts": "0",
            "view_limit": "1",
            "post_type": "1",
            "original": "1",
            "declaration": "0",
        }
        d = await self.post("/bbs/app/api/link/post", body)
        r = d.get("result") or {}
        link_id = str(r.get("link_id") or r.get("linkid") or d.get("link_id") or d.get("linkid") or "")
        return {"link_id": link_id, "url": f"https://www.xiaoheihe.cn/app/bbs/link/{link_id}" if link_id else ""}

    async def feed(self, offset: int = 0) -> list[dict]:
        d = await self.get("/bbs/app/feeds", {"pull": 0, "offset": offset, "dw": 604})
        r = d.get("result") or {}
        items = r.get("items") or r.get("links") or []
        out = []
        for it in items:
            x = it.get("link") or it.get("info") or it
            out.append({
                "link_id": str(x.get("linkid") or x.get("link_id") or ""),
                "title": re.sub(r"<[^>]+>", "", str(x.get("title") or x.get("link_title") or x.get("text") or "")),
                "likes": int(x.get("link_award_num") or x.get("link_like_count") or x.get("like_num") or x.get("award_num") or x.get("likes") or 0),
                "comments": int(x.get("comment_num") or x.get("comment_count") or 0),
                "desc": re.sub(r"<[^>]+>", "", str(x.get("description") or x.get("desc") or ""))[:300],
            })
        return out

    async def detail(self, link_id: str, comment_limit: int = 6) -> dict:
        """帖子详情：标题、作者、点赞、正文、热评。"""
        d = await self.get("/bbs/app/link/tree", {
            "link_id": str(link_id), "page": 1, "is_first": 1, "index": 1,
            "owner_only": 0, "offset": 0, "limit": 20, "sort_filter": "hot",
        })
        r = d.get("result") or {}
        link = r.get("link") or r
        user = link.get("user") or {}
        comments = _find_comments(r) or []
        out = []
        for c in comments[:comment_limit]:
            u = c.get("user") or {}
            text = _text_of(c.get("text") or c.get("content"))
            if text:
                out.append({
                    "user": str(u.get("username") or u.get("nickname") or "匿名"),
                    "text": text[:120],
                    "likes": int(c.get("up") or c.get("like_num") or 0),
                })
        return {
            "link_id": str(link_id),
            "url": f"{WEB_BASE}/app/bbs/link/{link_id}",
            "title": re.sub(r"<[^>]+>", "", str(link.get("title") or "")),
            "author": str(user.get("username") or user.get("nickname") or ""),
            "likes": int(link.get("link_like_count") or link.get("link_award_num") or 0),
            "body": _text_of(link.get("text")) or str(link.get("description") or ""),
            "topics": [
                "#" + str(t.get("name") or "") + "#"
                for t in (link.get("topics") or [])
                if isinstance(t, dict) and t.get("name")
            ],
            "comments": out,
        }

    async def search(self, query: str, limit: int = 8) -> list[dict]:
        d = await self.get("/bbs/app/api/general/search/v1", {
            "q": query, "search_type": "general", "is_pull_down": "0",
            "offset": 0, "limit": max(1, min(int(limit) or 8, 30)), "dw": 744,
            "sort_filter": "award_num", "time_range": "7d",
        })
        r = d.get("result") or {}
        items = r.get("items") or r.get("links") or []
        out = []
        for it in items:
            x = it.get("link") or it.get("info") or it
            out.append({
                "link_id": str(x.get("linkid") or x.get("link_id") or ""),
                "title": re.sub(r"<[^>]+>", "", str(x.get("title") or x.get("link_title") or x.get("text") or "")),
                "likes": int(x.get("link_award_num") or x.get("link_like_count") or x.get("like_num") or x.get("award_num") or x.get("likes") or 0),
                "comments": int(x.get("comment_num") or x.get("comment_count") or 0),
                "desc": re.sub(r"<[^>]+>", "", str(x.get("description") or x.get("desc") or ""))[:300],
            })
        return out


def _text_of(value: Any) -> str:
    """小黑盒正文可能是字符串，也可能是富文本块列表。"""
    if isinstance(value, str):
        return re.sub(r"<[^>]+>", " ", value).strip()
    parts: list[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("text", "content", "title", "url"):
                    v = item.get(key)
                    if isinstance(v, str) and v.strip() and not v.startswith("http"):
                        parts.append(v)
    elif isinstance(value, dict):
        for key in ("text", "content", "description"):
            v = value.get(key)
            if isinstance(v, str):
                parts.append(v)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _find_comments(obj: Any, depth: int = 0) -> list[dict]:
    if depth > 4:
        return []
    if isinstance(obj, dict):
        for key in ("comments", "comment_list", "reply_list"):
            v = obj.get(key)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return [x for x in v if isinstance(x, dict)]
        for v in obj.values():
            hit = _find_comments(v, depth + 1)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _find_comments(v, depth + 1)
            if hit:
                return hit
    return []

