"""B 站扫码登录：生成二维码、轮询结果、解析登录后的 cookie。

只读取公开的登录接口，不绕过验证；扫码由主人在手机客户端完成。
"""
from __future__ import annotations

import re
from typing import Any

import httpx

#: 登录状态：等待扫码。
WAITING = "waiting"
#: 登录状态：已扫码，等待确认。
SCANNED = "scanned"
#: 登录状态：成功。
SUCCESS = "success"
#: 登录状态：二维码过期。
EXPIRED = "expired"

PASSPORT = "https://passport.bilibili.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

#: 登录成功后需要保留的 cookie 名称。
WANTED_COOKIES = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid")


class BiliLoginError(RuntimeError):
    """扫码登录过程出错。"""


def cookie_header(raw: str) -> str:
    """把 ``SESSDATA=...; bili_jct=...`` 规整成可写入配置的形式。

    Args:
        raw: 原始 cookie 字符串，可能带换行或多余空格。

    Returns:
        ``k=v; k=v`` 形式的字符串；无可识别字段时返回空串。
    """
    pairs: dict[str, str] = {}
    for chunk in re.split(r"[;\n]+", str(raw or "")):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        key, value = key.strip(), value.strip()
        if key in WANTED_COOKIES and value:
            pairs[key] = value
    if not pairs:
        return ""
    return "; ".join(f"{key}={pairs[key]}" for key in WANTED_COOKIES if key in pairs)


def _cookies_from_response(response: httpx.Response) -> str:
    """从响应的 Set-Cookie 里拼出登录 cookie。"""
    found: dict[str, str] = {}
    for header in response.headers.get_list("set-cookie"):
        name, _, rest = header.partition("=")
        name = name.strip()
        if name not in WANTED_COOKIES:
            continue
        value = rest.split(";", 1)[0].strip()
        if value:
            found[name] = value
    return cookie_header("; ".join(f"{k}={v}" for k, v in found.items()))


class BiliLogin:
    """B 站扫码登录客户端。"""

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    async def generate(self) -> dict:
        """生成登录二维码。

        Returns:
            ``{"url": 二维码内容, "key": 轮询用 key}``。
        """
        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": UA}) as client:
            response = await client.get(f"{PASSPORT}/x/passport-login/web/qrcode/generate")
        try:
            payload = response.json()
        except ValueError as exc:
            raise BiliLoginError("登录接口返回的不是 JSON") from exc
        data = payload.get("data") or {}
        url, key = str(data.get("url") or ""), str(data.get("qrcode_key") or "")
        if payload.get("code") != 0 or not url or not key:
            raise BiliLoginError(f"生成二维码失败：{payload.get('message') or payload.get('code')}")
        return {"url": url, "key": key}

    async def poll(self, key: str) -> dict:
        """查询一次扫码状态。

        Args:
            key: :meth:`generate` 返回的 ``key``。

        Returns:
            ``{"state": 状态, "cookie": 登录成功时的 cookie}``；状态为
            :data:`WAITING`、:data:`SCANNED`、:data:`SUCCESS`、:data:`EXPIRED` 之一。
        """
        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": UA}) as client:
            response = await client.get(
                f"{PASSPORT}/x/passport-login/web/qrcode/poll", params={"qrcode_key": key})
        try:
            payload = response.json()
        except ValueError as exc:
            raise BiliLoginError("轮询接口返回的不是 JSON") from exc
        data = payload.get("data") or {}
        code = data.get("code")
        state = {86101: WAITING, 86090: SCANNED, 0: SUCCESS, 86038: EXPIRED}.get(code, WAITING)
        cookie = ""
        if state == SUCCESS:
            cookie = _cookies_from_response(response)
            if not cookie:
                # 新版接口把跳转地址放在 data.url 里，cookie 也可能只在其中出现。
                cookie = cookie_header(str(data.get("url") or ""))
            if not cookie:
                raise BiliLoginError("扫码成功但没拿到登录 cookie，请重试或手动填写")
        return {"state": state, "cookie": cookie, "message": str(data.get("message") or "")}


async def login_cookie(plugin: Any, timeout_seconds: float = 150.0) -> str:
    """完整走一次扫码登录，返回可用于配置的 cookie。

    该函数只负责登录本身，二维码怎么发给主人由调用方决定。

    Args:
        plugin: 插件实例，用于读取配置的轮询间隔。
        timeout_seconds: 最长等待时间。

    Returns:
        登录成功后的 cookie 字符串。

    Raises:
        BiliLoginError: 超时、过期或接口异常。
    """
    import asyncio
    import time

    client = BiliLogin()
    ticket = await client.generate()
    deadline = time.time() + max(30.0, float(timeout_seconds))
    while time.time() < deadline:
        await asyncio.sleep(3.0)
        result = await client.poll(ticket["key"])
        if result["state"] == SUCCESS:
            return result["cookie"]
        if result["state"] == EXPIRED:
            raise BiliLoginError("二维码过期了，再发一次就好")
    raise BiliLoginError("等太久没扫，这次先算了")
