#!/bin/sh
# 卸载本插件装的依赖（幂等、只删自己装的）。
#
# 用法（在宿主机执行，脚本在容器内跑）：
#   docker exec -it astrbot sh /AstrBot/data/plugins/astrbot_plugin_community_radar/uninstall_cleanup.sh
#
# 也可以在卸载插件之后再跑（deps.py 已复制到数据目录）：
#   docker exec -it astrbot python3 /AstrBot/data/plugin_data/astrbot_plugin_community_radar/deps.py uninstall
#
# 行为：
#   - 只卸载清单里「归属：plugin」的依赖
#   - 「归属：pre_existing」的保留（说明安装前就有，可能被别的程序使用）
#   - 没找到清单时不做任何卸载，避免误删
set -eu
exec python3 "$(dirname "$0")/../browser/deps.py" uninstall
