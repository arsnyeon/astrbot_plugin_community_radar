#!/bin/sh
# 安装「抖音写层」的浏览器运行时（幂等）。
#
# 用法（在宿主机执行，脚本在容器内跑）：
#   docker exec -it astrbot sh /AstrBot/data/plugins/astrbot_plugin_community_radar/install_browser.sh
#
# 行为：
#   - 先检查 chromium / xvfb / 中文字体 / playwright 是否已存在
#   - 已存在 → 跳过安装，并在清单里标记为 pre_existing（卸载时保留）
#   - 不存在 → 安装，标记为 plugin（卸载时删除）
#
# 卸载插件后清单仍在数据目录，可随时查看归属：
#   docker exec -it astrbot python3 /AstrBot/data/plugin_data/astrbot_plugin_community_radar/deps.py status
set -eu
exec python3 "$(dirname "$0")/deps.py" install
