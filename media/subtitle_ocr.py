"""读视频画面里的硬字幕。

B 站很多视频没有官方字幕，只有压在画面上的硬字幕。这类视频以前只能靠
「画面描述」猜内容，等于半瞎；把底部字幕条裁出来做 OCR，就能拿到真实台词。

做法：一次 ffmpeg 把底部字幕条按固定帧率抽成小图 → 逐张 OCR →
相邻相同的帧视为同一句字幕、只延长时间 → 合并成带时间轴的字幕轨。

OCR 引擎是可选依赖（``rapidocr_onnxruntime``），装不上就整体跳过，
不影响其它能力。
"""
from __future__ import annotations

import asyncio
import glob
import os
import re
import shutil
import tempfile
import time
from typing import Iterable

from astrbot.api import logger

from . import frames as frame_tools

#: OCR 默认只处理底部这一条：字幕几乎都在画面下部。
REGION_CROP = {
    "bottom": "crop=iw:ih*0.32:0:ih*0.68",
    "top": "crop=iw:ih*0.35:0:0",
}
#: 单行字幕保留的最长字符数。
MAX_LINE_CHARS = 80
#: 汉字/字母占比低于它，基本是 OCR 噪声。
MIN_CJK_RATIO = 0.3
#: 连续两帧的差异小于它就当作「字幕没变」。
SAME_FRAME_DISTANCE = 0.02


def _engine_ready() -> bool:
    """OCR 引擎是否可用（只看是否装了包，不加载模型）。"""
    from importlib.util import find_spec

    try:
        return find_spec("rapidocr_onnxruntime") is not None
    except (ImportError, ValueError):
        return False


def _meaningful(text: str) -> str:
    """压平空白并去掉 OCR 常见的孤立符号。"""
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    return re.sub(r"^[\|丨丨\-—_~·。，,、\s]+|[\|丨\-—_~·\s]+$", "", body)


def _cjk_ratio(text: str) -> float:
    """实义字符（汉字/字母/数字）占比，用来丢掉纯噪声。"""
    if not text:
        return 0.0
    useful = len(re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]", text))
    return useful / max(1, len(text))


def is_noise(text: str) -> bool:
    """判断一行 OCR 结果是不是噪声。

    Args:
        text: 已清理的文本。

    Returns:
        是否丢弃。
    """
    body = str(text or "")
    if len(body) < 2:
        return True
    if _cjk_ratio(body) < MIN_CJK_RATIO:
        return True
    # 同一字符重复（水印、进度条读数）
    if len(set(body)) <= 2 and len(body) >= 4:
        return True
    return False


def merge_lines(samples: Iterable[tuple[float, str]],
                *, same_distance: float = SAME_FRAME_DISTANCE) -> list[tuple[float, str]]:
    """把逐帧 OCR 结果合并成字幕轨。

    同一句字幕会在多帧里重复出现，这里按「与上一条相同就延长」合并，
    只保留第一次出现的时间点。

    Args:
        samples: ``(秒数, 文本)``，按时间升序。
        same_distance: 保留参数以便调用方统一接口，当前按文本相等判断。

    Returns:
        ``(秒数, 文本)`` 列表，已去掉噪声与相邻重复。
    """
    out: list[tuple[float, str]] = []
    for stamp, text in samples:
        body = _meaningful(text)
        if not body or is_noise(body):
            continue
        body = body[:MAX_LINE_CHARS]
        if out and out[-1][1] == body:
            continue
        out.append((float(stamp), body))
    return out


async def _grab_band(stream_url: str, duration: float, cfg: dict, referer: str,
                     workdir: str) -> list[str]:
    """用一次 ffmpeg 把字幕条抽成小图序列。

    Args:
        stream_url: 视频直链。
        duration: 总时长。
        cfg: 视频理解配置。
        referer: 请求头 Referer。
        workdir: 输出目录。

    Returns:
        按时间顺序的图片路径列表。
    """
    from .video_watch import MediaToolMissing, run_media

    fps = max(1, min(4, int(cfg.get("ocr_fps") or 1)))
    limit = max(1, min(int(cfg.get("ocr_max_frames") or 60), 200))
    region = str(cfg.get("ocr_region") or "bottom").lower()
    crop = REGION_CROP.get(region, REGION_CROP["bottom"])
    seconds = max(1.0, min(float(duration or 0) or 3600.0, limit / fps))
    headers = f"User-Agent: Mozilla/5.0\r\nReferer: {referer}\r\n"
    pattern = os.path.join(workdir, "band_%05d.jpg")
    try:
        ok = await run_media([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-rw_timeout", "15000000", "-headers", headers,
            "-i", stream_url, "-t", f"{seconds:.1f}",
            "-vf", f"{crop},fps={fps},scale=640:-2",
            "-q:v", "4", pattern,
        ], timeout=max(20, min(120, int(seconds))))
    except MediaToolMissing:
        return []
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[CommunityRadar] 字幕条抽取失败（{type(exc).__name__}）")
        return []
    if not ok:
        return []
    return sorted(glob.glob(os.path.join(workdir, "band_*.jpg")))[:limit]


def _ocr_files(paths: list[str], fps: int) -> list[tuple[float, str]]:
    """对图片序列做 OCR，跳过与上一张几乎相同的帧。

    Args:
        paths: 图片路径，按时间顺序。
        fps: 抽帧帧率，用于把序号换算成秒。

    Returns:
        ``(秒数, 文本)`` 列表。
    """
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    out: list[tuple[float, str]] = []
    previous = ()
    for index, path in enumerate(paths):
        try:
            with open(path, "rb") as stream:
                data = stream.read()
        except OSError:
            continue
        current = frame_tools.signature(data)
        if previous and current and frame_tools.distance(previous, current) < SAME_FRAME_DISTANCE:
            # 画面没变，字幕也不会变，省一次 OCR。
            continue
        previous = current or previous
        try:
            result, _ = engine(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[CommunityRadar] OCR 单帧失败（{type(exc).__name__}）")
            continue
        text = " ".join(str(item[1]) for item in (result or []) if len(item) > 1)
        out.append((index / float(fps), text))
    return out


async def extract(stream_url: str, duration: float, cfg: dict,
                  referer: str = "https://www.bilibili.com/") -> list[tuple[float, str]]:
    """读取视频里的硬字幕。

    这是尽力而为的能力：引擎缺失、抽帧失败、超时都只返回空列表，由调用方
    继续用其它证据（简介、评论、画面描述）。

    Args:
        stream_url: 视频直链。
        duration: 总时长（秒）。
        cfg: 视频理解配置，读取 ``ocr_*`` 系列。
        referer: 请求头 Referer。

    Returns:
        ``(秒数, 字幕文本)`` 列表；没有可用结果时为空。
    """
    if not cfg.get("ocr_subtitles", False):
        return []
    if not stream_url:
        return []
    if not _engine_ready():
        logger.info("[CommunityRadar] 未安装 rapidocr_onnxruntime，跳过硬字幕 OCR")
        return []

    budget = max(20.0, float(cfg.get("ocr_time_budget") or 120.0))
    started = time.monotonic()
    workdir = tempfile.mkdtemp(prefix="radar_ocr_")
    try:
        paths = await _grab_band(stream_url, duration, cfg, referer, workdir)
        if not paths:
            return []
        fps = max(1, min(4, int(cfg.get("ocr_fps") or 1)))
        left = budget - (time.monotonic() - started)
        if left <= 5:
            return []
        try:
            raw = await asyncio.wait_for(asyncio.to_thread(_ocr_files, paths, fps), timeout=left)
        except (asyncio.TimeoutError, RuntimeError):
            logger.info("[CommunityRadar] 硬字幕 OCR 超时，改用其它证据")
            return []
        lines = merge_lines(raw)
        if lines:
            logger.info("[CommunityRadar] 硬字幕 OCR 得到 %s 行（覆盖到 %.0f 秒）",
                        len(lines), lines[-1][0])
        return lines
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def available() -> bool:
    """对外暴露引擎可用性，供状态命令与文档说明。"""
    return _engine_ready()


def configured(cfg: dict) -> dict:
    """把相关配置整理成便于展示的字典（状态命令用）。"""
    return {
        "enabled": bool(cfg.get("ocr_subtitles", False)),
        "engine": available(),
        "fps": int(cfg.get("ocr_fps") or 1),
        "max_frames": int(cfg.get("ocr_max_frames") or 60),
        "region": str(cfg.get("ocr_region") or "bottom"),
    }
