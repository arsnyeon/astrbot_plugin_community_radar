#!/bin/sh
# 可选：清理「抖音写层」的残留。AstrBot 卸载插件不会动这些。
# 用法： docker exec -it astrbot sh /AstrBot/data/plugins/astrbot_plugin_community_radar/uninstall_cleanup.sh
#
# 这个脚本只清理确定属于本插件、且重装代价低的东西。
# 系统包 chromium / xvfb 可能被别的插件用到，所以只打印命令，不自动删除。
set -eu

echo "== 1. 卸载 playwright Python 包 =="
python -m pip uninstall -y playwright || true

echo ""
echo "== 2. 插件数据目录 =="
echo "浏览器登录态、状态、二维码在："
echo "  /AstrBot/data/plugin_data/astrbot_plugin_community_radar"
echo "在 AstrBot WebUI 卸载插件时勾选「删除数据」即可自动清掉；"
echo "若已卸载，手动执行： rm -r /AstrBot/data/plugin_data/astrbot_plugin_community_radar"

echo ""
echo "== 3. 系统包（需你确认没有其他插件在用）=="
echo "  apt-get remove --purge -y chromium xvfb && apt-get autoremove -y"
echo "  或者直接重建 astrbot 容器，容器可写层会整个重置。"

echo ""
echo "== 4. 插件配置 =="
echo "在 WebUI 卸载时勾选「删除配置」，或手动删 data/config/ 下对应 _config.json"
