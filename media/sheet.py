"""把若干画面拼成一张带时间戳的联系人卡片。

一张图里放 4~6 个画面，比连发 6 张图清爽得多，而且每格标了「序号 秒数」，
文字里说「第 3 张」时对方能直接对上。
"""
from __future__ import annotations

import io
import math
from typing import Sequence

from astrbot.api import logger

#: 画布宽度。高度按列数算，尽量接近 16:9。
CANVAS_WIDTH = 1280
#: 格子之间的空隙。
GUTTER = 6
#: 标签条高度。
LABEL_HEIGHT = 26
#: 输出质量（JPEG）。
JPEG_QUALITY = 88


def _font(size: int = 16):
    """尽量取一个可读字体，取不到就退回内置点阵字体。"""
    from PIL import ImageFont

    for name in ("DejaVuSans.ttf", "arial.ttf", "msyh.ttc", "simhei.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:  # noqa: BLE001
            continue
    try:
        return ImageFont.load_default(size)  # Pillow >= 10.1
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def _fmt(seconds: float) -> str:
    """把秒数写成 ``mm:ss``。"""
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def columns_for(count: int) -> int:
    """按数量挑列数：2 张两列，3~4 张两列，5 张以上三列。"""
    if count <= 1:
        return 1
    if count <= 4:
        return 2
    return 3


def compose(items: Sequence[tuple[float, bytes]], *, width: int = CANVAS_WIDTH) -> bytes:
    """把若干帧拼成一张 JPEG。

    Args:
        items: ``(秒数, 图片字节)`` 列表，按时间升序。
        width: 画布宽度。

    Returns:
        JPEG 字节；输入为空或全部解码失败时返回 ``b""``。
    """
    from PIL import Image, ImageDraw

    usable = [(t, data) for t, data in items if data]
    if not usable:
        return b""
    images = []
    for stamp, data in usable:
        try:
            with Image.open(io.BytesIO(data)) as source:
                images.append((stamp, source.convert("RGB").copy()))
        except Exception as exc:  # noqa: BLE001 - 单张坏图不影响整张
            logger.debug(f"[CommunityRadar] 拼图跳过一张（{type(exc).__name__}）")
    if not images:
        return b""

    count = len(images)
    columns = columns_for(count)
    rows = math.ceil(count / columns)
    cell_width = (width - GUTTER * (columns + 1)) // columns
    # 先用第一张的宽高比估格子高度，避免整体过于扁平。
    ratio = images[0][1].height / max(1, images[0][1].width)
    cell_height = max(120, int(cell_width * min(max(ratio, 0.5), 0.75)))
    canvas_height = GUTTER + rows * (cell_height + GUTTER)

    canvas = Image.new("RGB", (width, canvas_height), (18, 18, 18))
    draw = ImageDraw.Draw(canvas)
    font = _font(18)

    for index, (stamp, image) in enumerate(images):
        row, column = divmod(index, columns)
        x = GUTTER + column * (cell_width + GUTTER)
        y = GUTTER + row * (cell_height + GUTTER)
        thumb = image.copy()
        thumb.thumbnail((cell_width, cell_height))
        canvas.paste(thumb, (x + (cell_width - thumb.width) // 2,
                             y + (cell_height - thumb.height) // 2))
        label = f"{index + 1}  {_fmt(stamp)}"
        box = draw.textbbox((0, 0), label, font=font)
        draw.rectangle([x, y, x + (box[2] - box[0]) + 14, y + LABEL_HEIGHT], fill=(0, 0, 0))
        draw.text((x + 7, y + 4), label, fill=(255, 226, 88), font=font)

    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue()
