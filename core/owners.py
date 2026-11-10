"""解析「主人」账号与会话标识。

插件配置里的 ``admin_uin`` 是首选来源；留空时自动回退到 AstrBot 全局配置的
``admins_id``（即 WebUI「管理员」里填写的账号），避免用户在两处重复填写。

本模块只做解析，不持有状态，便于在服务层与命令层共用，也便于单独测试。
"""

from __future__ import annotations

import re
from typing import Any

_SEPARATORS = re.compile(r"[,，;；\s]+")


def _config_of(plugin: Any) -> dict:
    config = getattr(plugin, "config", None)
    return config if isinstance(config, dict) else {}


def _split(raw: Any) -> list[str]:
    """把配置中的账号串拆成列表，支持逗号、分号与空格分隔。"""
    return [item for item in _SEPARATORS.split(str(raw or "").strip()) if item]


def astrbot_admins(plugin: Any) -> list[str]:
    """读取 AstrBot 全局管理员列表（``cmd_config.json`` 的 ``admins_id``）。

    Args:
        plugin: 插件实例，需提供 ``context.get_config()``。

    Returns:
        管理员账号列表；配置不可用或未设置时返回空列表。
    """
    try:
        context = getattr(plugin, "context", None)
        config = context.get_config()
        values = config.get("admins_id") or []
    except Exception:  # noqa: BLE001 - 全局配置不可用不应影响主流程
        return []
    if not isinstance(values, (list, tuple)):
        return []
    return [str(item).strip() for item in values if str(item).strip()]


def admin_ids(plugin: Any) -> list[str]:
    """返回主人账号列表。

    优先使用插件配置的 ``admin_uin``；若为空则回退到 AstrBot 全局管理员。
    结果会去重并保持配置顺序。

    Args:
        plugin: 插件实例，需提供 ``config`` 属性。

    Returns:
        账号列表；两处都没有配置时返回空列表。
    """
    ids = _split(_config_of(plugin).get("admin_uin"))
    if ids:
        return list(dict.fromkeys(ids))
    return list(dict.fromkeys(astrbot_admins(plugin)))


def platform_name(plugin: Any) -> str:
    """返回用于拼接会话标识的平台名。

    复用插件自身的 ``_platform_name()``；不可用时回退为 ``default``。
    """
    try:
        name = plugin._platform_name()
    except Exception:  # noqa: BLE001
        return "default"
    return str(name or "default")


def owner_umo(plugin: Any) -> str:
    """返回主人私聊的会话标识（unified_message_origin）。

    Args:
        plugin: 插件实例。

    Returns:
        形如 ``平台名:FriendMessage:账号`` 的字符串；无法确定主人时返回空串。
    """
    ids = admin_ids(plugin)
    if not ids:
        return ""
    return f"{platform_name(plugin)}:FriendMessage:{ids[0]}"
