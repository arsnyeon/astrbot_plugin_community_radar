#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抖音写层：按需拉起的「有头浏览器 + CDP」运行时。

关键做法（照上游 dsh-aurabot-qq 的 src/douyin 移植）：
  1. 原生 Chromium（不是 Playwright launch）+ Xvfb 有头，独立 --user-data-dir 保登录态；
     Playwright 只通过 connect_over_cdp 接上去拿高层 API，避免注入自动化指纹。
  2. 所有数据都在抖音页面上下文里 fetch —— 签名 a_bogus 由抖音自己的脚本补，不自己算。
  3. 点赞 / 评论用真鼠标点击 + 等接口回执确认，不点完就算成功。
  4. 跑完就关浏览器，只留 profile，避免常驻占用资源。

playwright 是可选依赖：没装时读层（douyin_feed.py）照常工作，这里会给出明确提示。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
from astrbot.api import logger

HOME = "https://www.douyin.com/"
BASE_PARAMS = {
    "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
    "pc_client_type": "1", "version_code": "290100", "version_name": "29.1.0",
    "cookie_enabled": "true", "platform": "PC", "downlink": "10",
}
NOT_LOGIN = {8, 2190008}

VERIFY_PROBE = """(() => {
  const big = (n) => { const r = n.getBoundingClientRect(); const s = getComputedStyle(n);
    return r.width > 200 && r.height > 150 && s.visibility !== 'hidden' && s.display !== 'none' && Number(s.opacity) > 0.1 }
  const nodes = [...document.querySelectorAll('#captcha_container, .captcha_verify_container, [id^=captcha], iframe[src*="verifycenter"], iframe[src*="captcha"]')]
  const hit = nodes.find(big)
  if (!hit) return ''
  return (hit.innerText || '').slice(0, 60) || '页面弹了验证码'
})()"""

EDITOR = '.comment-input-inner-container [contenteditable="true"]'

# 分享面板里按 sec_uid 认出主人那一行（照上游 site.js::share 移植）
FIND_SHARE_ROW_JS = """(want) => {
  const secOf = (n) => {
    const k = Object.keys(n).find((x) => x.startsWith('__reactFiber$'));
    for (let f = k && n[k], j = 0; f && j < 6; f = f.return, j++) {
      for (const v of Object.values(f.memoizedProps || {})) {
        if (v && typeof v === 'object' && (v.sec_uid || v.secUid)) return String(v.sec_uid || v.secUid);
      }
    }
    return '';
  };
  const rows = [...document.querySelectorAll('div')].filter((n) =>
    /(分享|捎句话)$/.test((n.innerText || '').trim()) &&
    n.getBoundingClientRect().height > 30 &&
    n.getBoundingClientRect().height < 90 && secOf(n));
  const row = rows.find((n) => secOf(n) === want);
  if (!row) return { found: false, listed: rows.length };
  const btn = [...row.querySelectorAll('*')].reverse().find(
    (n) => ['分享', '捎句话'].includes((n.innerText || '').trim()));
  if (!btn) return { found: false, listed: rows.length };
  document.querySelectorAll('[data-aurabot="share-to"]').forEach(
    (n) => n.removeAttribute('data-aurabot'));
  btn.setAttribute('data-aurabot', 'share-to');
  return { found: true, done: btn.innerText.trim() === '捎句话' };
}"""


def _dm_comparable(t: Any) -> str:
    """比较私信输入框文字时忽略空白 / 变体选择符（编辑器会把 emoji 显示成图片）。"""
    return re.sub(r"[\s\u00a0\u200b\u200d\ufe0f]", "", str(t or "")).strip()


class BrowserError(Exception):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def _num(v: Any) -> int:
    try:
        n = int(float(v or 0))
        return n
    except (TypeError, ValueError):
        return 0


def _clean(v: Any, maxlen: int = 300) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:maxlen]


def _wan(n: int) -> str:
    return f"{n / 10000:.1f}万" if n >= 10000 else str(n)


def normalize_aweme(raw: dict) -> dict | None:
    a = raw.get("aweme_info") or raw.get("aweme_detail") or raw
    if not isinstance(a, dict):
        return None
    vid = str(a.get("aweme_id") or "")
    if not vid or a.get("is_ads") or a.get("cell_room") or a.get("is_live") or a.get("live_reason"):
        return None
    st = a.get("statistics") or {}
    au = a.get("author") or {}
    desc = _clean(a.get("desc"))
    return {
        "aweme_id": vid,
        "title": _clean(a.get("item_title"), 80) or re.sub(r"#\S+", "", desc).strip()[:80],
        "desc": desc,
        "author": _clean(au.get("nickname"), 40),
        "author_sec_uid": str(au.get("sec_uid") or ""),
        "likes": _num(st.get("digg_count")),
        "comments": _num(st.get("comment_count")),
        "shares": _num(st.get("share_count")),
        "duration": round(_num(a.get("video", {}).get("duration") or a.get("duration")) / 1000),
        "tags": [t.get("hashtag_name") for t in (a.get("text_extra") or []) if isinstance(t, dict) and t.get("hashtag_name")][:8],
        "category": [t.get("tag_name") for t in (a.get("video_tag") or []) if isinstance(t, dict) and t.get("tag_name")],
        "summary": _clean(a.get("chapter_abstract"), 600),
        "is_images": bool(a.get("images")),
        "url": f"{HOME}video/{vid}",
    }


def parse_comments(payload: dict, limit: int = 8) -> list[dict]:
    out = []
    for c in payload.get("comments") or []:
        if not isinstance(c, dict):
            continue
        text = _clean(c.get("text"), 120)
        if not text:
            continue
        out.append({
            "name": _clean((c.get("user") or {}).get("nickname"), 20),
            "text": text,
            "likes": _num(c.get("digg_count")),
        })
    out.sort(key=lambda x: x["likes"], reverse=True)
    return out[:limit]


def digest_of(v: dict, comments: list[dict]) -> str:
    lines = [
        f"《{v.get('title') or '（无标题）'}》 作者：{v.get('author') or '?'}  "
        f"赞 {_wan(v.get('likes', 0))}  评论 {_wan(v.get('comments', 0))}"
        + (f"  {v.get('duration')} 秒" if v.get("duration") else "")
        + ("  图文" if v.get("is_images") else ""),
    ]
    if v.get("category"):
        lines.append("分类：" + " / ".join(v["category"]))
    if v.get("summary"):
        lines.append("内容摘要：" + v["summary"])
    if v.get("desc") and v["desc"] != v.get("title"):
        lines.append("文案：" + v["desc"])
    if comments:
        lines.append("热评：\n" + "\n".join(f"  {c['name']}：{c['text']}（{_wan(c['likes'])} 赞）" for c in comments))
    return "\n".join(lines)


class DouyinBrowser:
    def __init__(self, cfg: dict):
        c = cfg or {}
        self.enabled = bool(c.get("enabled"))
        # 运行参数属于实现细节，不占用插件配置面板；默认即可用。
        # 高级用户可用环境变量覆盖：端口冲突、自定义显示或浏览器路径。
        self.port = int(os.environ.get("COMMUNITY_RADAR_CDP_PORT") or 9222)
        self.display = os.environ.get("COMMUNITY_RADAR_DISPLAY") or ":99"
        self.browser_path = (
            os.environ.get("COMMUNITY_RADAR_BROWSER_PATH")
            or os.environ.get("CHROME_PATH")
            or os.environ.get("EDGE_PATH")
            or ""
        ).strip()
        self.data_dir = self._default_data_dir()
        self.profile_dir = (
            os.environ.get("COMMUNITY_RADAR_PROFILE_DIR")
            or os.path.join(self.data_dir, "douyin-profile")
        )
        self._pw = None
        self._browser = None
        self._page = None
        self._chrome_proc: subprocess.Popen | None = None
        self._xvfb_proc: subprocess.Popen | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _default_data_dir() -> str:
        """AstrBot 标准插件数据目录：天然持久化，卸载时勾「删除数据」也能一起清掉。"""
        try:
            from astrbot.api.star import StarTools
            return str(StarTools.get_data_dir("astrbot_plugin_community_radar"))
        except Exception:  # noqa: BLE001
            return "/AstrBot/data/plugin_data/astrbot_plugin_community_radar"

    # ── 生命周期 ────────────────────────────────────────────────

    def state(self) -> dict:
        return {
            "enabled": self.enabled,
            "connected": self._page is not None,
            "cdp_port": self.port,
            "display": self.display,
            "browser": self._find_browser(),
            "profile_dir": self.profile_dir,
            "running_chrome": bool(self._chrome_proc and self._chrome_proc.poll() is None),
        }

    def _find_browser(self) -> str:
        if self.browser_path:
            return self.browser_path if Path(self.browser_path).exists() else ""
        for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "microsoft-edge"):
            p = shutil.which(name)
            if p:
                return p
        return ""

    async def _cdp_alive(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                r = await c.get(f"http://127.0.0.1:{self.port}/json/version")
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def ensure(self) -> dict:
        """按需启动 Xvfb + 原生 Chromium，并接上 CDP。已开着就复用。"""
        async with self._lock:
            if self._page is not None and await self._cdp_alive():
                return {"ok": True, "reused": True}
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                raise BrowserError(
                    "没装 playwright。在 astrbot 容器里执行：pip install playwright && "
                    "PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/ playwright install chromium",
                    "deps",
                )
            if not await self._cdp_alive():
                await self._launch()
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{self.port}", timeout=15000
            )
            ctx = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
            self._page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await self._page.set_default_timeout(15000)
            href = str(await self._page.evaluate("location.href") or "")
            if not href.startswith(HOME):
                await self._page.goto(HOME, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(1.5)
            return {"ok": True, "reused": False}

    async def _launch(self) -> None:
        binary = self._find_browser()
        if not binary:
            raise BrowserError(
                "找不到 Chromium。执行插件目录下的 install_browser.sh 安装"
                "（/AstrBot/data/plugins/astrbot_plugin_community_radar/install_browser.sh），"
                "或用环境变量 COMMUNITY_RADAR_BROWSER_PATH 指定浏览器路径", "deps",
            )
        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        disp_num = self.display.lstrip(":").split(".")[0]
        sock = Path(f"/tmp/.X11-unix/X{disp_num}")
        if not sock.exists():
            xvfb = shutil.which("Xvfb")
            if not xvfb:
                raise BrowserError("找不到 Xvfb（无头会被抖音拦），请先安装 xvfb", "deps")
            env["DISPLAY"] = self.display
            self._xvfb_proc = subprocess.Popen(
                [xvfb, self.display, "-screen", "0", "1280x860x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
            )
            for _ in range(40):
                if sock.exists():
                    break
                await asyncio.sleep(0.25)
            else:
                raise BrowserError("Xvfb 没起来", "deps")
        env["DISPLAY"] = self.display

        args = [
            binary,
            f"--user-data-dir={self.profile_dir}",
            f"--remote-debugging-port={self.port}",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run", "--no-default-browser-check", "--mute-audio",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-dev-shm-usage",
            "--disable-features=Translate,MediaRouter,CalculateNativeWinOcclusion",
            # 容器内以 root 运行，Chromium 默认拒绝无 sandbox 启动
            "--no-sandbox", "--disable-setuid-sandbox",
            "--window-size=1280,860", "--window-position=-2400,-2400",
            "about:blank",
        ]
        self._chrome_proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
            start_new_session=True,
        )
        for _ in range(60):
            if await self._cdp_alive():
                return
            if self._chrome_proc.poll() is not None:
                raise BrowserError("Chromium 启动后立刻退出了（profile 被别的实例占用？）", "deps")
            await asyncio.sleep(0.25)
        raise BrowserError("Chromium 没起来（等不到 CDP 端口）", "deps")

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._browser = self._pw = self._page = None
        for proc in (self._chrome_proc, self._xvfb_proc):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
        self._chrome_proc = self._xvfb_proc = None

    # ── 页面内接口 ──────────────────────────────────────────────

    async def _api(self, path: str, params: dict | None = None) -> dict:
        await self.ensure()
        q = {**BASE_PARAMS, **(params or {})}
        url = urllib.parse.urljoin(HOME, path) + "?" + urllib.parse.urlencode(q)
        expr = """async (u) => {
            const r = await fetch(u, { credentials: 'include' });
            return { status: r.status, text: await r.text() };
        }"""
        r = await self._page.evaluate(expr, url)
        if not r or r.get("status") != 200:
            raise BrowserError(f"抖音接口返回 {r.get('status') if r else '空'}", "http")
        text = r.get("text") or ""
        if not text:
            raise BrowserError("抖音接口返回空内容（可能被风控了）", "empty")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise BrowserError("抖音接口返回的不是 JSON", "bad")
        code = int(payload.get("status_code") or 0)
        if code in NOT_LOGIN:
            raise BrowserError("抖音还没登录（用 /抖音 登录 扫码）", "login")
        if code:
            raise BrowserError(str(payload.get("status_msg") or f"抖音接口出错({code})"), str(code))
        return payload

    async def self_info(self) -> dict | None:
        try:
            p = await self._api("/aweme/v1/web/user/profile/self/", {
                "publish_video_strategy_type": 2, "source": "channel_pc_web", "personal_center_strategy": 1,
            })
        except BrowserError as e:
            if e.code == "login":
                return None
            raise
        u = p.get("user") or {}
        return {"sec_uid": str(u.get("sec_uid") or ""), "nickname": _clean(u.get("nickname"), 40)} if u.get("sec_uid") else None

    async def feed(self, refresh_index: int = 1) -> list[dict]:
        p = await self._api("/aweme/v1/web/tab/feed/", {
            "count": 10, "refresh_index": refresh_index, "video_type_select": 1,
            "aweme_pc_rec_raw_data": "{}", "globalwid": "",
            "pull_type": 2 if refresh_index > 1 else 0, "min_window": 0, "free_right": 0,
        })
        return [x for x in (normalize_aweme(a) for a in (p.get("aweme_list") or [])) if x]

    async def detail(self, aweme_id: str) -> dict:
        p = await self._api("/aweme/v1/web/aweme/detail/", {"aweme_id": aweme_id})
        v = normalize_aweme(p.get("aweme_detail") or {})
        if not v:
            raise BrowserError("这个视频打不开（删了、私密或者是直播）")
        return v

    async def comments(self, aweme_id: str) -> list[dict]:
        p = await self._api("/aweme/v1/web/comment/list/", {
            "aweme_id": aweme_id, "cursor": 0, "count": 20, "item_type": 0,
        })
        return parse_comments(p)

    async def verify(self) -> str:
        """每次现查验证码，不留标记。"""
        try:
            return str(await self._page.evaluate(VERIFY_PROBE) or "")
        except Exception:  # noqa: BLE001
            return ""

    # ── 真看 / 真点 ────────────────────────────────────────────

    @staticmethod
    def _scope(vid: str) -> str:
        return f'[data-e2e="feed-active-video"][data-e2e-vid="{vid}"]'

    async def open_video(self, vid: str, dwell_ms: int = 3000) -> None:
        if not re.fullmatch(r"\d{15,}", str(vid)):
            raise BrowserError("视频 id 不对")
        await self.ensure()
        why = await self.verify()
        if why:
            raise BrowserError("要验证：" + why, "verify")
        scope = self._scope(vid)
        has = await self._page.evaluate("(s) => Boolean(document.querySelector(s))", scope)
        if not has:
            await self._page.goto(f"{HOME}jingxuan?modal_id={vid}", wait_until="domcontentloaded", timeout=20000)
            why = await self.verify()
            if why:
                raise BrowserError("要验证：" + why, "verify")
            try:
                await self._page.wait_for_selector(scope, timeout=12000)
            except Exception:  # noqa: BLE001
                raise BrowserError("视频没打开（可能删了或者是私密的）")
        if dwell_ms > 0:
            await self._page.evaluate("""(s) => {
                document.querySelectorAll(s + ' video').forEach((v) => {
                    v.muted = true; if (v.play) v.play().catch(() => {});
                });
            }""", scope)
            await asyncio.sleep(dwell_ms / 1000.0)

    async def _check_write(self, resp, what: str) -> dict:
        if resp is None:
            raise BrowserError(f"{what}没发出去（没等到抖音的回应）")
        headers = {k.lower(): v for k, v in resp.headers.items()}
        if headers.get("x-tt-verify-passport-decision"):
            raise BrowserError(
                f"抖音拦下了{what}并返回账号验证标记。请在手机抖音检查账号安全；未确认前不要反复重试", "verify",
            )
        try:
            body = await resp.json()
        except Exception:  # noqa: BLE001
            raise BrowserError(f"{what}没发出去（抖音没返回结果）")
        if not isinstance(body, dict) or body.get("status_code") != 0:
            msg = body.get("status_msg") if isinstance(body, dict) else ""
            code = body.get("status_code") if isinstance(body, dict) else ""
            raise BrowserError(str(msg or f"{what}失败({code})"), "write")
        return body

    async def like(self, vid: str) -> dict:
        await self.open_video(vid, 0)
        sel = f"{self._scope(vid)} [data-e2e='video-player-digg']"
        loc = self._page.locator(sel).first
        await loc.wait_for(timeout=10000)
        state = str(await loc.get_attribute("data-e2e-state") or "")
        if "digged" in state and "no-digged" not in state:
            return {"ok": True, "already": True}
        async with self._page.expect_response(
            lambda r: "/aweme/v1/web/commit/item/digg/" in r.url, timeout=10000
        ) as info:
            await loc.click()
            resp = await info.value
        await self._check_write(resp, "点赞")
        return {"ok": True}

    async def comment(self, vid: str, text: str) -> dict:
        await self.open_video(vid, 0)
        scope = self._scope(vid)
        for _ in range(2):
            if await self._page.evaluate("() => Boolean(document.querySelector('.comment-input-inner-container'))"):
                break
            btn = self._page.locator(f"{scope} [data-e2e='feed-comment-icon']").first
            await btn.click()
            try:
                await self._page.wait_for_selector(".comment-input-inner-container", timeout=7000)
            except Exception:  # noqa: BLE001
                continue
        edit = self._page.locator(EDITOR).first
        await edit.wait_for(timeout=7000)
        await edit.click()
        try:
            await self._page.keyboard.press("Control+A")
            await self._page.keyboard.press("Backspace")
        except Exception:  # noqa: BLE001
            pass
        await self._page.keyboard.insert_text(text[:100])
        await asyncio.sleep(0.5 + (time.time() % 0.8))
        marked = await self._page.evaluate("""() => {
            let bar = document.querySelector('.comment-input-inner-container');
            for (let i = 0; i < 3 && bar; i++) bar = bar.parentElement;
            const icons = bar ? [...bar.querySelectorAll('span')].filter(
                (n) => n.querySelector('svg') && n.getBoundingClientRect().width > 10) : [];
            const send = icons.sort((x, y) => x.getBoundingClientRect().x - y.getBoundingClientRect().x).at(-1);
            if (send) { send.setAttribute('data-aurabot', 'send'); return true; }
            return false;
        }""")
        async with self._page.expect_response(
            lambda r: "/aweme/v1/web/comment/publish" in r.url, timeout=12000
        ) as info:
            if marked:
                await self._page.locator('[data-aurabot="send"]').first.click()
            else:
                await self._page.keyboard.press("Enter")
            resp = await info.value
        body = await self._check_write(resp, "评论")
        return {"ok": True, "cid": str((body.get("comment") or {}).get("cid") or "")}

    async def watch_link(self, link: str, dwell_ms: int = 3000) -> tuple[dict, list[dict]]:
        """别人发来的链接：解析 id -> 真点开看 -> 读详情 + 热评。"""
        vid = await find_aweme_id(link)
        if not vid:
            raise BrowserError("没认出抖音视频链接（支持 v.douyin.com 短链 / video / note / modal_id）")
        await self.open_video(vid, dwell_ms)
        v = await self.detail(vid)
        comments = await self.comments(vid)
        return v, comments

    # ── 找人（核对主人）──────────────────────────────────────────

    async def user(self, sec_uid: str) -> dict:
        """按 sec_uid 查一个人，返回 sec_uid / uid / nickname / douyin_id。"""
        p = await self._api("/aweme/v1/web/user/profile/other/", {
            "sec_user_id": sec_uid, "source": "channel_pc_web",
        })
        u = p.get("user") or {}
        if not u.get("sec_uid"):
            raise BrowserError("找不到这个抖音号")
        return {
            "sec_uid": str(u["sec_uid"]),
            "uid": str(u.get("uid") or ""),
            "nickname": _clean(u.get("nickname"), 40),
            "douyin_id": str(u.get("unique_id") or u.get("short_id") or ""),
        }

    async def find_by_douyin_id(self, douyin_id: str) -> dict:
        """按抖音号搜人，只认抖音号完全一样的（照上游 findByDouyinId）。"""
        p = await self._api("/aweme/v1/web/discover/search/", {
            "keyword": douyin_id, "search_channel": "aweme_user_web",
            "search_source": "normal_search", "query_correct_type": 1,
            "is_filter_search": 0, "offset": 0, "count": 10,
        })
        for x in (p.get("user_list") or []):
            u = (x or {}).get("user_info") or {}
            ids = {str(u.get("unique_id") or ""), str(u.get("short_id") or "")}
            if str(douyin_id) in ids and u.get("sec_uid"):
                return await self.user(str(u["sec_uid"]))
        raise BrowserError("按抖音号没搜到这个人，换成主页链接试试")

    async def profile_of(self, link: str) -> dict:
        """主页链接 / sec_uid 直接查；短链在浏览器里跳一下取主页；其余当抖音号搜。"""
        s = str(link or "").strip()
        if not s:
            raise BrowserError("没给抖音主页链接或抖音号")
        m = re.search(r"douyin\.com/user/(MS4w[\w-]+)", s)
        sec = m.group(1) if m else (s if re.fullmatch(r"MS4w[\w-]+", s) else "")
        if sec:
            return await self.user(sec)
        if re.match(r"^https?://", s, re.I):
            await self.ensure()
            await self._page.goto(s, wait_until="domcontentloaded", timeout=20000)
            for _ in range(10):
                href = str(await self._page.evaluate("location.href") or "")
                m2 = re.search(r"douyin\.com/user/(MS4w[\w-]+)", href)
                if m2:
                    return await self.user(m2.group(1))
                await asyncio.sleep(1)
            raise BrowserError("这个链接不是抖音主页（或者没跳过去）")
        return await self.find_by_douyin_id(s.lstrip("@"))

    # ── 站内私信分享 ─────────────────────────────────────────────

    async def _press(self, selector: str) -> bool:
        """真鼠标点一下；页面不可见（窗口在屏幕外 / 锁屏）时改派发 DOM 鼠标事件，照上游 press()。"""
        r = await self._page.evaluate("""(s) => {
            const n = document.querySelector(s);
            if (!n) return null;
            n.scrollIntoView({ block: 'center', inline: 'center' });
            if (document.visibilityState !== 'visible') {
                for (const t of ['mouseover', 'mousedown', 'mouseup', 'click'])
                    n.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window, button: 0 }));
                return { dom: true };
            }
            const b = n.getBoundingClientRect();
            return { x: b.x + b.width / 2, y: b.y + b.height / 2 };
        }""", selector)
        if not r:
            return False
        if not r.get("dom"):
            await self._page.mouse.click(r["x"], r["y"])
        return True

    async def share(self, vid: str, who: dict) -> dict:
        """在抖音里把视频私信分享给一个人（视频卡片）。

        打开弹层播放器的「分享给朋友」面板 → 按 sec_uid 认出主人那一行 → 点「分享」
        → 等 /v1/message/send 回执。认不出就不发；按钮已是「捎句话」说明分享过，直接跳过。
        「捎句话」（私信里再补一句）上游也没做，想让主人看到话就同时发 QQ。
        """
        await self.open_video(vid, 0)
        why = await self.verify()
        if why:
            raise BrowserError("要验证：" + why, "verify")
        page = self._page
        want = str(who.get("sec_uid") or "")
        if not want:
            raise BrowserError("没核对主人的抖音号", "owner")
        hit = await page.evaluate(FIND_SHARE_ROW_JS, want) or {"found": False}

        # 分享按钮是开 / 关切换：面板已经开着就不点；点了等不到就再点一下
        for _ in range(2):
            if hit.get("found"):
                break
            if not await self._press(f"{self._scope(vid)} [data-e2e='video-player-share']"):
                raise BrowserError("页面上找不到分享按钮")
            for _ in range(16):
                if hit.get("found"):
                    break
                await asyncio.sleep(0.5)
                hit = await page.evaluate(FIND_SHARE_ROW_JS, want) or {"found": False}

        # 最近联系人里没有：用分享面板的搜索框搜昵称
        if not hit.get("found") and who.get("nickname"):
            searched = await page.evaluate("""() => {
                const box = [...document.querySelectorAll('input')].find(
                    (n) => n.getAttribute('placeholder') === '搜索' &&
                           n.getBoundingClientRect().width > 0);
                if (!box) return false;
                box.focus(); box.select(); return true;
            }""")
            if searched:
                await page.keyboard.insert_text(str(who["nickname"]))
                for _ in range(10):
                    if hit.get("found"):
                        break
                    await asyncio.sleep(0.6)
                    hit = await page.evaluate(FIND_SHARE_ROW_JS, want) or {"found": False}

        if not hit.get("found"):
            raise BrowserError("分享面板里没找到主人（可能互相没关注、私信不通）", "share")
        if hit.get("done"):
            return {"ok": True, "already": True}

        if not await page.evaluate(
            """() => Boolean(document.querySelector('[data-aurabot="share-to"]'))"""
        ):
            raise BrowserError("分享按钮点不到")
        # 必须先注册回执监听，再点；点完以 /v1/message/send 的 HTTP 200 为准
        try:
            async with page.expect_response(
                lambda r: "/v1/message/send" in r.url, timeout=12000
            ) as info:
                await self._press('[data-aurabot="share-to"]')
                resp = await info.value
        except Exception as exc:  # noqa: BLE001
            raise BrowserError("点了分享，但没等到抖音私信的回应") from exc
        if resp.status != 200:
            raise BrowserError(f"抖音私信没发出去（HTTP {resp.status}）")
        return {"ok": True, "already": False}

    # ── 主动私信 ─────────────────────────────────────────────────

    async def open_from_profile(self, sec_uid: str) -> bool:
        """打开某人主页，点「私信」发起会话（陌生人也能发）。只打开会话，不发消息。"""
        if not re.fullmatch(r"MS4w[\w-]+", str(sec_uid or "")):
            return False
        await self.ensure()
        await self._page.goto(f"{HOME}user/{sec_uid}", wait_until="domcontentloaded", timeout=20000)
        marked = False
        for _ in range(25):
            marked = bool(await self._page.evaluate("""() => {
                const nodes = [...document.querySelectorAll('button,[role="button"],div,span,a,[data-e2e]')]
                    .filter((n) => (n.innerText || '').trim() === '私信')
                    .filter((n) => { const r = n.getBoundingClientRect(); return r.width > 10 && r.height > 10; });
                if (!nodes.length) return false;
                nodes.sort((a, c) =>
                    (a.getBoundingClientRect().width * a.getBoundingClientRect().height) -
                    (c.getBoundingClientRect().width * c.getBoundingClientRect().height));
                document.querySelectorAll('[data-aurabot="dm-open"]').forEach(
                    (x) => x.removeAttribute('data-aurabot'));
                nodes[0].setAttribute('data-aurabot', 'dm-open');
                return true;
            }"""))
            if marked:
                break
            await asyncio.sleep(1)
        if not marked or not await self._press('[data-aurabot="dm-open"]'):
            return False
        for _ in range(20):
            await asyncio.sleep(1)
            if await self._page.evaluate(
                """() => Boolean(document.querySelector('.editor-kit-container[contenteditable="true"]'))"""
            ):
                return True
        return False

    async def send_dm(self, text: str, who: dict | None = None) -> dict:
        """在当前私信会话里发一条消息；没打开会话就先从主人主页开一个。

        没等到发送回执时**绝不重试**（IM 可能自动重发），直接报「状态不确定」。
        """
        text = _clean(text, 300)
        if not text:
            raise BrowserError("私信内容不能为空")
        await self.ensure()
        has_editor = bool(await self._page.evaluate(
            """() => Boolean(document.querySelector('.editor-kit-container[contenteditable="true"]'))"""
        ))
        if not has_editor:
            if not who or not who.get("sec_uid"):
                raise BrowserError("还没核对主人的抖音号，打不开私信会话", "owner")
            if not await self.open_from_profile(who["sec_uid"]):
                raise BrowserError("主页没有可用的「私信」入口（可能对方关闭了私信）", "dm")
        marked = bool(await self._page.evaluate("""() => {
            const e = document.querySelector('.editor-kit-container[contenteditable="true"]') ||
                [...document.querySelectorAll('[contenteditable="true"]')].find(
                    (x) => /发送消息/.test(x.getAttribute('data-placeholder') ||
                                           x.getAttribute('placeholder') || ''));
            if (!e) return false;
            e.setAttribute('data-aurabot', 'dm-edit');
            e.focus();
            return document.activeElement === e;
        }"""))
        if not marked:
            raise BrowserError("私信输入框点不进去")
        # 打完字核对一遍，避免只发出半句
        for tries in range(2):
            await self._page.keyboard.press("Control+A")
            await self._page.keyboard.press("Backspace")
            await self._page.keyboard.insert_text(text)
            await asyncio.sleep(0.4 + (time.time() % 0.6))
            typed = await self._page.evaluate(
                """() => { const e = document.querySelector('[data-aurabot="dm-edit"]'); return e ? e.innerText : ''; }"""
            )
            if _dm_comparable(typed) == _dm_comparable(text):
                break
            if tries >= 1:
                raise BrowserError("私信输入框里的字对不上，没发")
        try:
            async with self._page.expect_response(
                lambda r: "/v1/message/send" in r.url, timeout=8000
            ) as info:
                await self._page.keyboard.press("Enter")
                resp = await info.value
        except Exception as exc:  # noqa: BLE001
            raise BrowserError("私信发送状态不确定，已暂停重试，避免重复发送", "dm_uncertain") from exc
        if resp.status != 200:
            try:
                await self._page.keyboard.press("Control+A")
                await self._page.keyboard.press("Backspace")
            except Exception:  # noqa: BLE001
                pass
            raise BrowserError(f"私信没发出去（HTTP {resp.status}）")
        return {"ok": True}

    # ── 登录 / 窗口 ─────────────────────────────────────────────

    async def login_qr(self) -> str:
        """打开登录弹窗并截图，返回图片路径（由 main.py 发到 QQ）。"""
        await self.ensure()
        await self._page.goto(HOME, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)
        for sel in (
            '[data-e2e="top-login-button"]',
            '//*[contains(text(),"登录")]',
            ".login-button",
        ):
            try:
                loc = self._page.locator(sel).first
                if await loc.count():
                    await loc.click(timeout=3000)
                    break
            except Exception:  # noqa: BLE001
                continue
        await asyncio.sleep(2)
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        path = str(Path(self.data_dir) / "douyin_login_qr.png")
        await self._page.screenshot(path=path, full_page=False)
        return path

    async def show(self, visible: bool) -> None:
        await self.ensure()
        try:
            cdp = await self._browser.new_browser_cdp_session()
            w = await cdp.send("Browser.getWindowForTarget")
            bounds = {"left": 80, "top": 60, "width": 1280, "height": 860} if visible else {
                "left": -2400, "top": -2400, "width": 1280, "height": 860}
            await cdp.send("Browser.setWindowBounds", {"windowId": w["windowId"], "bounds": bounds})
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] 调整浏览器窗口失败: {exc}")


async def find_aweme_id(text: str) -> str:
    """从完整链接 / 短链 / 纯 id 里认出 aweme_id。短链做一次异步跳转（不阻塞事件循环）。"""
    s = str(text or "")
    m = (re.search(r"douyin\.com/(?:video|note)/(\d{15,})", s)
         or re.search(r"[?&]modal_id=(\d{15,})", s)
         or re.search(r"iesdouyin\.com/share/(?:video|note)/(\d{15,})", s))
    if m:
        return m.group(1)
    if re.fullmatch(r"\d{15,}", s.strip()):
        return s.strip()
    short = re.search(r"https?://v\.douyin\.com/[A-Za-z0-9_-]+/?", s)
    if short:
        try:
            async with httpx.AsyncClient(
                timeout=10, follow_redirects=False, headers={"User-Agent": "Mozilla/5.0"}
            ) as c:
                r = await c.get(short.group(0))
            loc = r.headers.get("location") or ""
            m2 = (re.search(r"(?:video|note|modal_id=)(\d{15,})", loc)
                  or re.search(r"/(\d{15,})", loc))
            if m2:
                return m2.group(1)
        except Exception:  # noqa: BLE001
            pass
    return ""
