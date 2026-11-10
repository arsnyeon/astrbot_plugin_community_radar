"""把一段文本渲染成二维码图片。

B 站扫码登录需要把二维码直接发给主人，因此这里只做一件事：文本 → PNG 字节。
没有安装 ``qrcode`` 依赖时返回空字节，由调用方退回发送链接文本。
"""
from __future__ import annotations

import io

from astrbot.api import logger


def png_bytes(text: str, box_size: int = 8) -> bytes:
    """生成二维码 PNG。

    Args:
        text: 要编码的内容，通常是登录链接。
        box_size: 每个模块的像素大小。

    Returns:
        PNG 字节；缺少依赖或内容为空时返回 ``b""``。
    """
    if not str(text or "").strip():
        return b""
    try:
        import qrcode
    except Exception as exc:  # noqa: BLE001 - 依赖缺失时由上层降级
        logger.debug(f"[CommunityRadar] 二维码依赖不可用（{type(exc).__name__}）")
        return b""
    try:
        image = qrcode.make(str(text), box_size=box_size, border=2)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[CommunityRadar] 二维码生成失败（{type(exc).__name__}）")
        return b""
