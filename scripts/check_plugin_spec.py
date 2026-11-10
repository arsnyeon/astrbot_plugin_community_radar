"""按 AstrBot 官方规范校验插件的元数据与配置，可作为发布前检查或测试用例执行。

用法::

    python scripts/check_plugin_spec.py

全部通过时退出码为 0，否则列出不符合项并返回 1。
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
failures = []


def fail(message):
    failures.append(message)


# ── metadata.yaml ──────────────────────────────────────────────
try:
    import yaml

    with (ROOT / "metadata.yaml").open(encoding="utf-8") as stream:
        meta = yaml.safe_load(stream)
except Exception as exc:  # noqa: BLE001
    meta = {}
    fail(f"metadata.yaml 无法解析：{exc}")

ALLOWED_META = {
    "name", "display_name", "short_desc", "desc", "version", "author", "repo",
    "astrbot_version", "support_platforms", "social_link", "tags",
}
unknown = set(meta) - ALLOWED_META
if unknown:
    fail(f"metadata.yaml 含非官方字段：{sorted(unknown)}")

for required in ("name", "desc", "version", "author"):
    if not str(meta.get(required) or "").strip():
        fail(f"metadata.yaml 缺少必填字段 {required}")

name = str(meta.get("name") or "")
if not name.startswith("astrbot_plugin_"):
    fail(f"插件名建议以 astrbot_plugin_ 开头：{name}")
if " " in name or name != name.lower():
    fail(f"插件名需小写且不含空格：{name}")

version = str(meta.get("version") or "")
if version.startswith("v"):
    fail(f"version 不得带 v 前缀：{version}")
if not re.fullmatch(r"\d+\.\d+\.\d+([.\-+][0-9A-Za-z.\-+]*)?", version):
    fail(f"version 不是语义化版本：{version}")

repo = str(meta.get("repo") or "")
if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+(\.git)?", repo):
    fail(f"repo 不是合法的 GitHub 仓库地址：{repo}")

astrbot_version = str(meta.get("astrbot_version") or "")
if astrbot_version.startswith("v"):
    fail(f"astrbot_version 不得带 v 前缀：{astrbot_version}")

ADAPTERS = {
    "aiocqhttp", "qq_official", "qq_official_webhook", "telegram", "wecom",
    "wecom_ai_bot", "lark", "dingtalk", "discord", "slack", "kook", "vocechat",
    "weixin_official_account", "weixin_oc", "satori", "misskey", "line",
    "matrix", "mattermost",
}
for adapter in meta.get("support_platforms") or []:
    if adapter not in ADAPTERS:
        fail(f"support_platforms 含未知适配器：{adapter}")

# ── _conf_schema.json ──────────────────────────────────────────
with (ROOT / "_conf_schema.json").open(encoding="utf-8") as stream:
    schema = json.load(stream)
ALLOWED_TYPES = {"string", "text", "int", "float", "bool", "object", "list",
                 "dict", "template_list", "file"}
SENSITIVE = ("cookie", "token", "secret", "password", "sessdata", "jct", "key")
# secret 仅在 string / list 上生效（见 ConfigItemRenderer.vue）
SECRET_CAPABLE = {"string", "list"}


def walk(node, path=""):
    for key, value in node.items():
        here = f"{path}.{key}" if path else key
        if not isinstance(value, dict):
            fail(f"{here}: 配置项必须是对象")
            continue
        kind = value.get("type")
        if kind not in ALLOWED_TYPES:
            fail(f"{here}: 非法 type={kind!r}")
        if kind == "object":
            items = value.get("items")
            if not isinstance(items, dict):
                fail(f"{here}: object 类型必须提供 items")
            else:
                walk(items, here)


def check_secrets(node, path=""):
    for key, value in node.items():
        here = f"{path}.{key}" if path else key
        if not isinstance(value, dict):
            continue
        kind = value.get("type")
        if kind == "object" and isinstance(value.get("items"), dict):
            check_secrets(value["items"], here)
            continue
        if any(word in key.lower() for word in SENSITIVE):
            if not value.get("secret"):
                fail(f"{here}: 敏感字段未标记 secret")
            elif kind not in SECRET_CAPABLE:
                fail(f"{here}: secret 在 {kind!r} 类型上不生效，应改用 string")


walk(schema)
check_secrets(schema)

# ── 目录与文件 ─────────────────────────────────────────────────
for required_file in ("main.py", "metadata.yaml", "_conf_schema.json", "logo.png"):
    if not (ROOT / required_file).is_file():
        fail(f"缺少必需文件：{required_file}")

try:
    from PIL import Image

    with Image.open(ROOT / "logo.png") as logo:
        width, height = logo.size
    if width != height:
        fail(f"logo.png 建议为 1:1，当前 {width}x{height}")
except Exception as exc:  # noqa: BLE001
    fail(f"logo.png 无法读取：{exc}")

# ── 发布包体积 ─────────────────────────────────────────────────
files = [p for p in ROOT.rglob("*") if p.is_file()
         and ".git" not in p.parts and "__pycache__" not in p.parts
         and p.suffix != ".pyc"]
total = sum(p.stat().st_size for p in files)
if total > 16 * 1024 * 1024:
    fail(f"发布包超过 16MB：{total / 1024 / 1024:.2f}MB")

print(f"文件数 {len(files)}，总体积 {total / 1024 / 1024:.2f} MB")
if failures:
    print("\n不符合项：")
    for item in failures:
        print(" -", item)
    sys.exit(1)
print("全部符合官方规范")
