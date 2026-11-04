#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内容理解：把社区内容「看懂」之后再交给模型自然接话。

覆盖三类内容
------------
- **视频**：B 站读字幕，抖音走分享页；没字幕/没文字时用 ffmpeg 抽帧交给视觉模型，
  必要时还可以把音轨转写成文字。
- **图文**：抖音图文、小黑盒配图，下载原图后交给视觉模型描述。
- **兜底**：以上都拿不到时退回文案与热评，不让这一轮变成空上下文。

抽帧不是乱抽：按视频时长在 8%~88% 区间等距取点（避开片头片尾），
再用 8x8 均值哈希去掉几乎重复的画面。
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import subprocess
import tempfile
from typing import Any

import httpx
from astrbot.api import logger

from .bili import BiliError, UA as BILI_UA, find_video_id


class VideoWatch:
    def __init__(self, plugin: Any):
        self.plugin = plugin

    # ── 各平台入口 ──────────────────────────────────────────────

    async def any(self, link: str, cfg: dict | None = None) -> dict:
        """按链接自动判断平台。"""
        cfg = cfg or {}
        s = str(link or "")
        if re.search(r"douyin\.com|iesdouyin\.com", s):
            return await self.douyin(s, cfg)
        if re.search(r"xiaoheihe\.cn|/link/\d+", s):
            return await self.heybox(s, cfg)
        return await self.bili(s, cfg)

    async def bili(self, link: str, cfg: dict | None = None) -> dict:
        cfg = cfg or {}
        bvid = await self._bvid(link)
        if not bvid:
            return {"ok": False, "message": "没认出 B 站视频号（支持 BV 号 / 视频链接 / b23.tv 短链）"}
        try:
            info = await self.plugin.bili.view(bvid)
        except BiliError as exc:
            return {"ok": False, "message": f"打不开这个视频：{exc}"}

        max_chars = int(cfg.get("max_transcript_chars") or 3000)
        cid = str(info.get("cid") or "")
        aid = str(info.get("aid") or "")
        head = self._head(info.get("title"), info.get("author"), info.get("duration"),
                          info.get("play"), info.get("like"))

        # 1) 字幕
        try:
            transcript = await self.plugin.bili.subtitles(bvid, cid)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[CommunityRadar] 取字幕失败: {exc}")
            transcript = ""
        if transcript:
            return {"ok": True, "via": "字幕", "title": info.get("title") or "",
                    "digest": head + "字幕内容：\n" + transcript[:max_chars]}

        # 2) 音轨转写（可选）
        if cfg.get("audio_transcription", False):
            try:
                play = await self.plugin.bili.play_url(bvid, cid, aid)
                spoken = await self._audio_text(play, cfg, referer="https://www.bilibili.com/")
                if spoken:
                    return {"ok": True, "via": "音轨", "title": info.get("title") or "",
                            "digest": head + "口述内容：\n" + spoken[:max_chars]}
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[CommunityRadar] B 站音轨转写不可用: {exc}")

        # 3) 抽帧
        if cfg.get("use_frames", True):
            try:
                play = await self.plugin.bili.play_url(bvid, cid, aid)
                frames = await self._frames_from(
                    play, int(info.get("duration") or 0),
                    int(cfg.get("frames") or 3), referer="https://www.bilibili.com/",
                )
                caption = await self._caption(frames, cfg)
                if caption:
                    return {"ok": True, "via": "画面", "title": info.get("title") or "",
                            "digest": head + "画面描述：\n" + caption[:max_chars]}
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[CommunityRadar] B 站抽帧失败: {exc}")

        # 4) 兜底：简介 + 热评
        body = str(info.get("desc") or "").strip()
        comments: list[str] = []
        try:
            cs = await self.plugin.bili.comments(aid, limit=5)
            comments = [f"{c['name']}：{c['text'][:80]}" for c in cs]
        except Exception:  # noqa: BLE001
            pass
        if not body and not comments:
            return {"ok": False, "message": "这个视频没有字幕、也拿不到简介，看不了"}
        digest = head + "简介：" + (body or "（无简介）")
        if comments:
            digest += "\n热评：\n" + "\n".join("  " + c for c in comments)
        return {"ok": True, "via": "简介", "title": info.get("title") or "", "digest": digest}

    async def douyin(self, link: str, cfg: dict | None = None) -> dict:
        cfg = cfg or {}
        from .douyin_browser import find_aweme_id

        vid = await find_aweme_id(str(link))
        if not vid:
            return {"ok": False, "message": "没认出抖音视频或图文（支持分享链接 / v.douyin.com 短链）"}
        from .douyin_ssr import fetch, images_of, stats_of, video_url_of

        item = await fetch(vid)
        if not item:
            return {"ok": False, "message": "这条抖音打不开（可能需要登录或已删除）"}
        st = stats_of(item)
        head = self._head(st["desc"][:60] or "抖音作品", st["author"],
                          st["duration_ms"] // 1000, None, st["likes"])
        max_chars = int(cfg.get("max_transcript_chars") or 3000)

        # 图文：下载原图交给视觉模型
        images = images_of(item)
        if images:
            if cfg.get("use_frames", True):
                try:
                    caption = await self._caption_images(images, cfg)
                    if caption:
                        return {"ok": True, "via": "图文", "title": st["desc"][:40],
                                "digest": head + "图片内容：\n" + caption[:max_chars]}
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[CommunityRadar] 抖音图文理解失败: {exc}")
            return {"ok": True, "via": "文案", "title": st["desc"][:40],
                    "digest": head + "文案：" + (st["desc"] or "（无文案）")}

        # 视频：抽帧 + 可选音轨
        play = video_url_of(item)
        if play:
            if cfg.get("audio_transcription", False):
                try:
                    spoken = await self._audio_text(play, cfg)
                    if spoken:
                        return {"ok": True, "via": "音轨", "title": st["desc"][:40],
                                "digest": head + "口述内容：\n" + spoken[:max_chars]}
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[CommunityRadar] 抖音音轨转写不可用: {exc}")
            if cfg.get("use_frames", True):
                try:
                    frames = await self._frames_from(
                        play, st["duration_ms"] // 1000, int(cfg.get("frames") or 3)
                    )
                    caption = await self._caption(frames, cfg)
                    if caption:
                        return {"ok": True, "via": "画面", "title": st["desc"][:40],
                                "digest": head + "画面描述：\n" + caption[:max_chars]}
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[CommunityRadar] 抖音抽帧失败: {exc}")

        return {"ok": True, "via": "文案", "title": st["desc"][:40],
                "digest": head + "文案：" + (st["desc"] or "（无文案）")}

    async def heybox(self, link: str, cfg: dict | None = None) -> dict:
        cfg = cfg or {}
        m = re.search(r"/(?:bbs/)?link/(\d+)", str(link))
        link_id = m.group(1) if m else (str(link).strip() if str(link).strip().isdigit() else "")
        if not link_id:
            return {"ok": False, "message": "没认出小黑盒帖子编号"}
        title, body, likes, images = "", "", 0, []
        # 信息流里能拿到简介和配图，先从这里找
        try:
            for it in await self.plugin.heybox.feed(0):
                if it.get("link_id") == link_id:
                    title = str(it.get("title") or "")
                    body = str(it.get("desc") or "")
                    likes = int(it.get("likes") or 0)
                    images = list(it.get("images") or [])
                    break
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[CommunityRadar] 小黑盒信息流读取失败: {exc}")
        # 找不到再试详情接口（可能要验证码）
        if not body:
            try:
                d = await self.plugin.heybox.detail(link_id)
                title = title or str(d.get("title") or "")
                body = str(d.get("body") or "")
                likes = likes or int(d.get("likes") or 0)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[CommunityRadar] 小黑盒详情不可用: {exc}")
        head = self._head(title or "小黑盒帖子", "", None, None, likes)
        max_chars = int(cfg.get("max_transcript_chars") or 3000)
        if images and cfg.get("use_frames", True):
            try:
                caption = await self._caption_images(images, cfg)
                if caption:
                    return {"ok": True, "via": "图文", "title": title,
                            "digest": head + (f"正文：{body[:600]}\n" if body else "")
                            + "图片内容：\n" + caption[:max_chars]}
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[CommunityRadar] 小黑盒配图理解失败: {exc}")
        if not body and not images:
            return {"ok": False, "message": "这个帖子拿不到正文，也读不出图片"}
        return {"ok": True, "via": "正文", "title": title,
                "digest": head + "正文：" + (body or "（无正文）")}

    # ── 通用工具 ────────────────────────────────────────────────

    async def _bvid(self, link: str) -> str:
        vid = find_video_id(link) or {}
        if vid.get("bvid"):
            return str(vid["bvid"])
        if vid.get("short"):
            real = await self.plugin.bili.resolve_short(str(vid["short"]))
            again = find_video_id(real) or {}
            return str(again.get("bvid") or "")
        return ""

    @staticmethod
    def _head(title, author, duration, play, like) -> str:
        parts = [f"《{title or '无标题'}》"]
        meta = []
        if author:
            meta.append(f"作者：{author}")
        if duration:
            meta.append(f"时长 {duration}s")
        if play is not None:
            meta.append(f"播放 {play}")
        if like is not None:
            meta.append(f"赞 {like}")
        return parts[0] + ("\n" + "  ".join(meta) if meta else "") + "\n"

    # ── 抽帧 ────────────────────────────────────────────────────

    @staticmethod
    def _ahash(path: str) -> str:
        try:
            from PIL import Image

            with Image.open(path) as im:
                small = im.convert("L").resize((8, 8))
                px = list(small.getdata())
            avg = sum(px) / len(px)
            return "".join("1" if p > avg else "0" for p in px)
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _dist(a: str, b: str) -> int:
        return sum(1 for x, y in zip(a, b) if x != y)

    async def _frames_from(
        self, url: str, duration: int, count: int, referer: str = "", ua: str = ""
    ) -> list[str]:
        """按视频时长均匀取帧，避开片头片尾，并去掉几乎重复的画面。"""
        if not url:
            return []
        count = max(1, min(count, 6))
        dur = int(duration or 0)
        if dur > 20:
            lo, hi = 0.08, 0.88
            step = (hi - lo) / max(1, count)
            seeks = [max(2, int(dur * (lo + step * (i + 0.5)))) for i in range(count)]
            seeks += [max(2, int(dur * f)) for f in (0.15, 0.30, 0.45, 0.60, 0.75, 0.85)]
        else:
            seeks = [2 + i * 5 for i in range(count)]

        headers = ""
        if referer:
            headers += f"Referer: {referer}\r\n"
        headers += f"User-Agent: {ua or BILI_UA}\r\n"

        out: list[str] = []
        hashes: list[str] = []
        seen: set[int] = set()
        for seek in seeks:
            if len(out) >= count:
                break
            if seek in seen:
                continue
            seen.add(seek)
            fd, path = tempfile.mkstemp(suffix=".jpg", prefix="radar_frame_")
            os.close(fd)
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error", "-headers", headers,
                "-ss", str(seek), "-i", url,
                "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", path,
            ]
            try:
                await asyncio.to_thread(
                    subprocess.run, cmd, capture_output=True, timeout=90, check=False
                )
            except Exception:  # noqa: BLE001
                continue
            if not (os.path.exists(path) and os.path.getsize(path) > 3000):
                self._unlink(path)
                continue
            h = self._ahash(path)
            if h and any(self._dist(h, other) < 6 for other in hashes):
                self._unlink(path)
                continue
            hashes.append(h)
            out.append(path)
        return out

    # ── 音轨 ────────────────────────────────────────────────────

    async def _audio_text(self, url: str, cfg: dict, referer: str = "") -> str:
        """抽音轨交给支持音频输入的模型转写。"""
        if not url:
            return ""
        fd, path = tempfile.mkstemp(suffix=".mp3", prefix="radar_audio_")
        os.close(fd)
        seconds = max(30, min(int(cfg.get("audio_seconds") or 180), 600))
        headers = (f"Referer: {referer}\r\n" if referer else "") + f"User-Agent: {BILI_UA}\r\n"
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-headers", headers,
            "-i", url, "-t", str(seconds),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "48k", path,
        ]
        try:
            await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=120, check=False)
            if not os.path.exists(path) or os.path.getsize(path) < 2000:
                return ""
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
        except Exception:  # noqa: BLE001
            return ""
        finally:
            self._unlink(path)

        provider = await self._provider(cfg, "audio_provider_id")
        if provider is None:
            return ""
        try:
            resp = await provider.text_chat(
                prompt=(
                    "把这段音频里说的话尽量完整地转写成文字，保留语气与关键信息；"
                    "听不清的地方标注「听不清」，不要编造，不要总结成提纲。"
                ),
                session_id="",
                contexts=[],
                audio_urls=[f"data:audio/mpeg;base64,{b64}"],
            )
            return str(getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] 音轨转写失败（模型可能不支持音频）: {exc}")
            return ""

    # ── 画面描述 ────────────────────────────────────────────────

    async def _caption(self, frames: list[str], cfg: dict) -> str:
        if not frames:
            return ""
        urls: list[str] = []
        for path in frames:
            try:
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                urls.append(f"data:image/jpeg;base64,{b64}")
            except Exception:  # noqa: BLE001
                continue
        try:
            return await self._describe(urls, "视频", cfg)
        finally:
            for path in frames:
                self._unlink(path)

    async def _caption_images(self, images: list[str], cfg: dict) -> str:
        """下载图文原图（最多 3 张）后交给视觉模型描述。"""
        urls: list[str] = []
        paths: list[str] = []
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            for u in images[:3]:
                try:
                    r = await client.get(u, headers={"User-Agent": BILI_UA})
                    if r.status_code != 200 or len(r.content) < 3000:
                        continue
                    fd, path = tempfile.mkstemp(suffix=".jpg", prefix="radar_img_")
                    with os.fdopen(fd, "wb") as f:
                        f.write(r.content)
                    paths.append(path)
                    urls.append("data:image/jpeg;base64," + base64.b64encode(r.content).decode())
                except Exception:  # noqa: BLE001
                    continue
        try:
            if not urls:
                return ""
            return await self._describe(urls, "图文", cfg)
        finally:
            for p in paths:
                self._unlink(p)

    async def _describe(self, urls: list[str], kind: str, cfg: dict) -> str:
        provider = await self._provider(cfg, "provider_id")
        if provider is None:
            return ""
        if kind == "视频":
            ask = (
                f"这是同一个视频里按时间顺序抽出的 {len(urls)} 帧画面。"
                "用中文客观描述画面里发生了什么、出现了什么人和物、场景与氛围，"
                "最后用一句话概括这个视频大概在讲什么。不要评价，不要编造。"
            )
        else:
            ask = (
                f"这是一个图文的 {len(urls)} 张图片。用中文客观描述每张图拍了什么、"
                "文字信息（如果有）、整体在表达什么。不要评价，不要编造。"
            )
        try:
            resp = await provider.text_chat(
                prompt=ask, session_id="", contexts=[], image_urls=urls
            )
            return str(getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] 画面描述失败: {exc}")
            return ""

    async def _provider(self, cfg: dict, key: str) -> Any:
        pid = str(cfg.get(key) or cfg.get("provider_id") or "").strip()
        ctx = self.plugin.context
        if pid:
            try:
                return ctx.get_provider_by_id(pid)
            except Exception:  # noqa: BLE001
                return None
        try:
            return await ctx.get_using_provider_async("")
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _unlink(path: str) -> None:
        try:
            os.unlink(path)
        except Exception:  # noqa: BLE001
            pass
