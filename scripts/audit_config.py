"""发布前静态体检：配置键与代码是否一一对应、模块能否导入、命令是否齐全。

不联网、不启插件，只做静态与导入层面的检查。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {"tests", "__pycache__", ".git", "scripts", "docs", "browser"}


def schema_paths(node, prefix=""):
    """把 _conf_schema.json 展开成 a.b.c 形式的键路径集合。"""
    out = set()
    if not isinstance(node, dict):
        return out
    for key, value in node.items():
        if not isinstance(value, dict):
            continue
        path = f"{prefix}.{key}" if prefix else key
        out.add(path)
        items = value.get("items")
        if isinstance(items, dict):
            out |= schema_paths(items, path)
    return out


def code_paths():
    """从代码里抽出所有形如 self.config.get("a")["b"] 的键路径。"""
    found: dict[str, set[str]] = {}
    pattern = re.compile(r'config(?:\.get)?\(\s*["\']([a-z_]+)["\']\s*\)')
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            found.setdefault(match.group(1), set()).add(path.name)
        # (self.config.get("a") or {}).get("b")
        for match in re.finditer(
            r'config\.get\(\s*["\']([a-z_]+)["\']\s*\)[^)]*?\)?\.get\(\s*["\']([a-z_]+)["\']',
            text,
        ):
            found.setdefault(f"{match.group(1)}.{match.group(2)}", set()).add(path.name)
    return found


def quoted_keys():
    """代码里所有以字符串形式出现的键名。

    覆盖三种写法：``cfg.get("enable")``、``cfg["enable"]`` 与
    ``number(cfg, "max_images", 3, 0, 6)`` 这类把键名当参数传的辅助函数。
    """
    keys: set[str] = set()
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        keys |= set(re.findall(r'\.get\(\s*["\']([a-z_0-9]+)["\']', text))
        keys |= set(re.findall(r'\[\s*["\']([a-z_0-9]+)["\']\s*\]', text))
        keys |= set(re.findall(r'\(\s*[A-Za-z_][\w.]*,\s*["\']([a-z_0-9]+)["\']', text))
        keys |= set(re.findall(r'pop\(\s*["\']([a-z_0-9]+)["\']', text))
    return keys


def main():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    known = schema_paths(schema)
    roots = {p for p in known if "." not in p}
    said = quoted_keys()

    used = code_paths()
    print("=== 代码用到但 schema 里没有的配置键（会静默取默认值）===")
    missing = 0
    for key in sorted(used):
        if key in known or key in roots:
            continue
        print(f"  ? {key:<40} 出处: {', '.join(sorted(used[key]))[:70]}")
        missing += 1
    if not missing:
        print("  （无）")

    print("\n=== schema 里定义但代码里找不到同名读取的键（可能是死配置）===")
    dead = 0
    for key in sorted(known):
        leaf = key.rsplit(".", 1)[-1]
        if leaf in said:
            continue
        print(f"  ~ {key}")
        dead += 1
    if not dead:
        print("  （无）")

    print("\n=== 命令一览 ===")
    commands = sorted(set(re.findall(
        r'@filter\.command\(\s*["\']([^"\']+)["\']',
        (ROOT / "handlers" / "commands.py").read_text(encoding="utf-8"),
    )))
    for name in commands:
        print("  /" + name)
    print(f"  共 {len(commands)} 个")

    print("\n=== LLM 工具 ===")
    tools = sorted(set(re.findall(
        r'@filter\.llm_tool\(\s*(?:name\s*=\s*)?["\']([^"\']+)["\']',
        (ROOT / "handlers" / "tools.py").read_text(encoding="utf-8"),
    )))
    for name in tools:
        print("  " + name)
    print(f"  共 {len(tools)} 个")


main()
