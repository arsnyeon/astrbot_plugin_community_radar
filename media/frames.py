"""从一堆候选画面里挑出「彼此不一样」的那几张。

均匀采样的问题是会连续抽到几乎相同的画面（同一镜头里的两个人脸特写），
既浪费上下文，也让「挑它觉得有意思的画面」变成随机。

做法很朴素：把每帧压成一个很小的灰度签名，先保头尾保证覆盖，再每次挑
「与已选帧差异最大」的那张，差异不够大就提前收手，最后按时间排序。
"""
from __future__ import annotations

import io
from typing import Iterable, Sequence

from astrbot.api import logger

#: 签名边长：8×8 足够区分镜头，又不至于被压缩噪声影响。
SIGNATURE_SIDE = 8
#: 与已选帧的最小差异低于它，就认为「和已经有的差不多」，不再补充。
MIN_DISTANCE = 0.06


def signature(data: bytes, side: int = SIGNATURE_SIDE) -> tuple[int, ...]:
    """把图片压成 ``side×side`` 的灰度签名。

    Args:
        data: 图片字节（JPEG/PNG/WebP 均可）。
        side: 采样边长。

    Returns:
        长度 ``side*side`` 的灰度元组；无法解码时返回空元组。
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            small = image.convert("L").resize((side, side), Image.BILINEAR)
            # 用 tobytes() 而非 getdata()：后者在 Pillow 14 会被移除。
            return tuple(small.tobytes())
    except Exception as exc:  # noqa: BLE001 - 单张坏图不影响整轮
        logger.debug(f"[CommunityRadar] 画面签名失败（{type(exc).__name__}）")
        return ()


def distance(left: Sequence[int], right: Sequence[int]) -> float:
    """两帧签名的归一化差异，0 表示完全一样。

    Args:
        left: 签名 A。
        right: 签名 B。

    Returns:
        0~1 之间的差异值；长度不一致时返回 1.0（当作完全不同）。
    """
    if not left or not right or len(left) != len(right):
        return 1.0
    total = sum(abs(a - b) for a, b in zip(left, right))
    return total / (255.0 * len(left))


def pick_distinct(samples: Iterable[tuple[float, str]], limit: int,
                  *, min_distance: float = MIN_DISTANCE) -> list[tuple[float, str]]:
    """从候选里挑出彼此差别最大的若干帧。

    第一个和最后一个一定会留下，保证覆盖开头与结尾；其余按「与已选帧差异最大」
    依次补，补到差异低于 ``min_distance`` 为止——宁可少给几张，也不给一堆重复图。

    Args:
        samples: ``(秒数, 图片路径)`` 候选，顺序任意。
        limit: 最多保留几张。
        min_distance: 低于这个差异就不再补。

    Returns:
        按时间升序排列的 ``(秒数, 图片路径)``。
    """
    items = sorted(((float(t), p) for t, p in samples if p), key=lambda x: x[0])
    limit = max(1, int(limit or 1))
    if len(items) <= limit:
        return items

    signature_of: dict[str, tuple[int, ...]] = {}
    read_failed: set[str] = set()
    for _, path in items:
        try:
            with open(path, "rb") as stream:
                signature_of[path] = signature(stream.read())
        except Exception:  # noqa: BLE001
            read_failed.add(path)

    usable = [(t, p) for t, p in items if p not in read_failed]
    if len(usable) <= limit:
        return items

    kept: list[tuple[float, str]] = [usable[0], usable[-1]]
    if limit == 2:
        return sorted(kept)

    remaining = [item for item in usable[1:-1]]
    while remaining and len(kept) < limit:
        best_item = None
        best_score = -1.0
        for item in remaining:
            sig = signature_of.get(item[1]) or ()
            score = min(distance(sig, signature_of.get(other[1]) or ()) for other in kept)
            if score > best_score:
                best_score, best_item = score, item
        if best_item is None or best_score < min_distance:
            break
        kept.append(best_item)
        remaining.remove(best_item)

    return sorted(kept)


def describe(samples: Sequence[tuple[float, str]]) -> str:
    """给日志用的一句话概览，例如 ``12s/45s/98s``。"""
    return "/".join(f"{int(t)}s" for t, _ in samples)
