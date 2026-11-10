"""AstrBot 4.30 导入路径迁移的兼容层。

AstrBot 4.30 会把若干公共类型（``TextPart``、工具结果、Agent 上下文）从
``astrbot.core.*`` 迁到 ``astrbot.api.*``。直接引用私有路径会在该版本上失效，
因此这类导入统一经过本模块。
"""
from __future__ import annotations

try:  # AstrBot >= 4.30
    from astrbot.api.agents import TextPart  # type: ignore[attr-defined]
except ImportError:  # AstrBot 4.29.x
    from astrbot.core.agent.message import TextPart  # type: ignore[no-redef]

__all__ = ["TextPart"]
