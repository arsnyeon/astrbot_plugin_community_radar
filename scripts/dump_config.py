"""打印配置项清单，便于核对文档中的键名与默认值。"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))


def walk(node, prefix=""):
    for key, value in node.items():
        if not isinstance(value, dict):
            continue
        path = f"{prefix}.{key}" if prefix else key
        items = value.get("items")
        if isinstance(items, dict):
            print(f"[{path}] {value.get('description', '')}")
            walk(items, path)
        else:
            print(f"  {path} = {value.get('default')!r}  — {value.get('description', '')}")


walk(schema)
