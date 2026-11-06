#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""社区雷达依赖管理：装之前先查，装过就记归属；卸载只删自己装的。

归属规则
--------
- 安装前已存在 → 记为 ``pre_existing``（别的程序在用），卸载时**保留**
- 由本插件安装   → 记为 ``plugin``，卸载时删除

用法（容器内）::

    python3 deps.py status      # 看当前状态与归属
    python3 deps.py install     # 装缺失的依赖（已存在则跳过并标记）
    python3 deps.py uninstall   # 只卸载归本插件的依赖

清单落在插件数据目录 ``deps_manifest.json``，卸载插件后仍然保留，
所以可以先卸载插件、再跑 ``deps.py uninstall``。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

DATA_DIR = Path(
    os.environ.get("COMMUNITY_RADAR_DATA")
    or "/AstrBot/data/plugin_data/astrbot_plugin_community_radar"
)
MANIFEST = DATA_DIR / "deps_manifest.json"

# Python 包依赖（用于 connect_over_cdp，不需要下载浏览器内核）
PIP_DEPS = [
    {"key": "py:playwright", "module": "playwright", "pip": "playwright"},
]

# 系统包依赖
APT_DEPS = [
    {"key": "apt:chromium", "pkg": "chromium", "bins": ["chromium", "chromium-browser", "google-chrome"]},
    {"key": "apt:xvfb", "pkg": "xvfb", "bins": ["Xvfb"]},
    {"key": "apt:fonts-wqy-zenhei", "pkg": "fonts-wqy-zenhei", "bins": []},
]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except Exception as exc:  # noqa: BLE001
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def _pip_has(module: str) -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec(module) is not None
    except Exception:  # noqa: BLE001
        return False


def _apt_has(pkg: str) -> bool:
    r = _run(["dpkg-query", "-W", "-f=${Status}", pkg])
    return r.returncode == 0 and "install ok installed" in (r.stdout or "")


def _bin_has(bins: list[str]) -> bool:
    return any(shutil.which(b) for b in (bins or []))


def _present(dep: dict) -> bool:
    if dep["key"].startswith("py:"):
        return _pip_has(dep["module"])
    if dep.get("bins"):
        return _bin_has(dep["bins"])
    return _apt_has(dep["pkg"])


def _load() -> dict:
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _copy_self() -> None:
    """把 deps.py 复制到数据目录，卸载插件后仍可执行清理。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        here = Path(__file__).resolve()
        target = DATA_DIR / "deps.py"
        if here != target and here.exists():
            shutil.copy2(here, target)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! 复制 deps.py 到数据目录失败：{exc}")


def cmd_status() -> int:
    data = _load()
    deps = data.get("deps") or {}
    print(f"数据目录：{DATA_DIR}")
    print(f"清单文件：{MANIFEST}{'' if MANIFEST.exists() else '（不存在，说明还没跑过 install）'}")
    print()
    print(f"{'依赖':<26}{'当前存在':<10}{'归属':<14}")
    print("-" * 52)
    for dep in PIP_DEPS + APT_DEPS:
        key = dep["key"]
        owner = (deps.get(key) or {}).get("owner", "-")
        print(f"{key:<26}{('是' if _present(dep) else '否'):<10}{owner:<14}")
    return 0


def cmd_install() -> int:
    data = _load()
    data.setdefault("deps", {})
    data["updated_at"] = int(time.time())

    print("== 检查并安装依赖（已存在的跳过，只记录归属）==")
    missing_apt: list[dict] = []
    for dep in PIP_DEPS:
        key = dep["key"]
        if _present(dep):
            data["deps"][key] = {
                "owner": (data["deps"].get(key) or {}).get("owner") or "pre_existing",
                "at": int(time.time()),
            }
            print(f"  = {key} 已存在，跳过（归属：{data['deps'][key]['owner']}）")
            continue
        print(f"  + {key} 缺失，安装中…")
        r = _run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", dep["pip"]])
        if r.returncode != 0:
            print(f"  ! {key} 安装失败：{(r.stderr or '').strip()[:200]}")
            continue
        data["deps"][key] = {"owner": "plugin", "at": int(time.time())}
        print(f"  + {key} 已安装（归属：plugin）")

    for dep in APT_DEPS:
        key = dep["key"]
        if _present(dep):
            data["deps"][key] = {
                "owner": (data["deps"].get(key) or {}).get("owner") or "pre_existing",
                "at": int(time.time()),
            }
            print(f"  = {key} 已存在，跳过（归属：{data['deps'][key]['owner']}）")
            continue
        missing_apt.append(dep)

    if missing_apt:
        names = " ".join(d["pkg"] for d in missing_apt)
        print(f"  + apt 安装中：{names}")
        _run(["apt-get", "update", "-qq"])
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        try:
            r = subprocess.run(
                ["apt-get", "install", "-y", "-qq", "--no-install-recommends", *[d["pkg"] for d in missing_apt]],
                capture_output=True, text=True, timeout=1800, env=env,
            )
        except Exception as exc:  # noqa: BLE001
            r = subprocess.CompletedProcess([], 1, "", str(exc))
        for dep in missing_apt:
            if _apt_has(dep["pkg"]) or _bin_has(dep.get("bins") or []):
                data["deps"][dep["key"]] = {"owner": "plugin", "at": int(time.time())}
                print(f"  + {dep['key']} 已安装（归属：plugin）")
            else:
                print(f"  ! {dep['key']} 安装失败：{(r.stderr or '').strip()[:200]}")

    _save(data)
    _copy_self()
    print()
    print("清单已写入：", MANIFEST)
    print("提示：卸载插件时只会删除「归属：plugin」的依赖；pre_existing 的会保留。")
    return 0


def cmd_uninstall() -> int:
    data = _load()
    deps = data.get("deps") or {}
    if not deps:
        print("没找到依赖清单（可能没跑过 install，或是旧版本安装的）。")
        print("为避免误删其他程序的依赖，这里不做任何卸载。")
        print(f"如需手动清理，先跑：python3 {__file__} status")
        return 0

    keep, removed, failed = [], [], []
    for dep in PIP_DEPS + APT_DEPS:
        key = dep["key"]
        info = deps.get(key) or {}
        owner = info.get("owner", "pre_existing")
        if owner != "plugin":
            if _present(dep):
                keep.append(key)
            continue
        if not _present(dep):
            continue
        print(f"  - 卸载 {key}（本插件安装）…")
        if key.startswith("py:"):
            r = _run([sys.executable, "-m", "pip", "uninstall", "-y", dep["pip"]])
        else:
            r = _run(["apt-get", "remove", "-y", dep["pkg"]])
        (removed if r.returncode == 0 else failed).append(key)
        if r.returncode != 0:
            print(f"    ! 失败：{(r.stderr or '').strip()[:200]}")

    print()
    print(f"已卸载：{'、'.join(removed) or '无'}")
    print(f"保留（安装前就存在，可能被别的程序使用）：{'、'.join(keep) or '无'}")
    if failed:
        print(f"卸载失败：{'、'.join(failed)}")
    data["deps"] = {k: v for k, v in deps.items() if k in keep}
    data["updated_at"] = int(time.time())
    if data["deps"]:
        _save(data)
    else:
        try:
            MANIFEST.unlink()
        except Exception:  # noqa: BLE001
            pass
    print()
    print("浏览器登录态等数据在：", DATA_DIR)
    print("确认不需要后可手动删除该目录。")
    return 0


def main() -> int:
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").strip().lower()
    if cmd in ("install", "i", "up"):
        return cmd_install()
    if cmd in ("uninstall", "remove", "clean", "u"):
        return cmd_uninstall()
    if cmd in ("status", "s", "list"):
        return cmd_status()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
