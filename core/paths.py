"""解析插件数据目录。

按 AstrBot 官方《插件存储》规范，插件的大文件应存放在
``data/plugin_data/{plugin_name}/``，推荐通过 ``get_astrbot_data_path()`` 拼接。
``StarTools.get_data_dir()`` 内部做了同样的计算，因此优先使用官方推荐的写法；
它不可用时再回退到 ``StarTools``，最后回退到相对路径，保证非 AstrBot 环境
（单元测试、独立脚本）也能运行。
"""

from __future__ import annotations

from pathlib import Path

#: 插件标识符，必须与 ``metadata.yaml`` 的 ``name`` 保持一致。
PLUGIN_NAME = "astrbot_plugin_community_radar"


def data_dir() -> Path:
    """返回插件数据目录的绝对路径。

    Returns:
        形如 ``<AstrBot 根目录>/data/plugin_data/astrbot_plugin_community_radar``
        的路径。调用方可以在此基础上继续拼接自己的文件名。
    """
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
    except Exception:  # noqa: BLE001 - 非 AstrBot 环境或内部 API 变更时降级
        pass
    try:
        from astrbot.api.star import StarTools

        return Path(StarTools.get_data_dir(PLUGIN_NAME))
    except Exception:  # noqa: BLE001
        return Path("data") / "plugin_data" / PLUGIN_NAME
