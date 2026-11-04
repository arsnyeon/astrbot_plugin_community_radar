#!/bin/sh
# 在 AstrBot 容器内执行一次，安装「抖音写层」的浏览器运行时。
# 用法： docker exec -it astrbot sh /AstrBot/data/plugins/astrbot_plugin_community_radar/install_browser.sh
#
# 注意：Playwright 只用于 connect_over_cdp 连接我们自行启动的原生 Chromium，
# 因此 **不需要** `playwright install chromium`（省掉 ~150MB 浏览器内核下载）。
# 这些依赖装在容器可写层：卸载插件不会自动删除，重建容器会自然清掉。
set -eu

echo "[1/3] 安装系统依赖 chromium + xvfb + 中文字体"
# 如果默认软件源下载较慢，可取消下面两处注释，换成你所在地区的镜像。
# sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    chromium xvfb fonts-wqy-zenhei ca-certificates

echo "[2/3] 安装 Playwright Python 包"
# 网络较慢时可加 -i <你的 PyPI 镜像地址>
python -m pip install -q --disable-pip-version-check playwright

echo "[3/3] 校验"
chromium --version || true
command -v Xvfb
python -c "import playwright; print('playwright python ok')"

echo ""
echo "完成。接下来："
echo "  1. 在 AstrBot WebUI 重载本插件，配置页「抖音」里打开「启用推荐流与互动」"
echo "  2. 保持「试运行」开启，用 /抖音 登录 扫码（建议使用小号）"
echo "  3. /抖音 状态 确认，再 /抖音 刷 试跑"
echo "  4. 确认行为正常后再考虑关掉「试运行」/ 打开「定时刷推荐流」"
echo ""
echo "卸载插件不会移除 chromium / xvfb / playwright；如需清理见 uninstall_cleanup.sh。"
