"""崩溃安全的 JSON 状态文件读写。

原地写入在进程中途退出时会留下被截断的文件；加载失败后若静默回退，
当日计数与 ``seen`` 去重集合会被清零，等同于绕过写操作配额。
因此统一先写临时文件再原子替换。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from astrbot.api import logger


def write_json(path: Path, value: Any, *, indent: int | None = None) -> bool:
    """把 ``value`` 以 UTF-8 JSON 原子替换写入 ``path``，返回是否成功。"""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=indent)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[CommunityRadar] 状态写入失败 {path.name}: {exc}")
        return False


def read_json(path: Path, default: Any) -> tuple[Any, bool]:
    """读取 JSON，返回 ``(值, 是否正常)``。

    文件不存在视为正常（用默认值）；文件存在但无法解析时返回 ``False``，
    并把原文件保留为 ``.broken`` 供排查，而不是直接覆盖。
    """
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8")), True
    except FileNotFoundError:
        return default, True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[CommunityRadar] 状态文件 {path.name} 无法解析（{type(exc).__name__}），"
            "本轮按空状态处理，请检查该文件是否损坏"
        )
        # Keep the broken file for inspection instead of overwriting it silently.
        try:
            broken = path.with_name(path.name + ".broken")
            os.replace(path, broken)
            logger.warning(f"[CommunityRadar] 已保留损坏文件：{broken}")
        except Exception:  # noqa: BLE001
            pass
        return default, False
