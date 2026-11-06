"""Per-session record of what was actually shared, for follow-up questions."""
from __future__ import annotations

import time

KEY = "session_shares"
MAX_TARGETS = 40
MAX_ITEMS = 6
TTL = 7 * 86400


def remember(store, target: str, items: list[dict], now: float | None = None) -> None:
    target = str(target or "").strip()
    if not target or not items:
        return
    now = time.time() if now is None else now
    data = store.get(KEY, {})
    if not isinstance(data, dict):
        data = {}
    kept = []
    for item in items:
        cid = str(item.get("content_id") or "")
        if not cid:
            continue
        kept.append({
            "id": cid,
            "title": str(item.get("title") or "")[:120],
            "url": str(item.get("url") or "")[:400],
            "digest": str(item.get("digest") or item.get("_body") or "")[:600],
        })
    if not kept:
        return
    data[target] = {"at": now, "items": kept[:MAX_ITEMS]}
    data = {k: v for k, v in data.items() if now - float(v.get("at") or 0) < TTL}
    if len(data) > MAX_TARGETS:
        data = dict(sorted(data.items(), key=lambda kv: float(kv[1].get("at") or 0), reverse=True)[:MAX_TARGETS])
    store.set(KEY, data)


def recent(store, target: str, minutes: float = 45, now: float | None = None) -> dict | None:
    target = str(target or "").strip()
    if not target:
        return None
    now = time.time() if now is None else now
    data = store.get(KEY, {})
    entry = data.get(target) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return None
    if now - float(entry.get("at") or 0) > max(60.0, float(minutes) * 60):
        return None
    items = entry.get("items") or []
    return entry if items else None


def ids(store, target: str, minutes: float = 45, now: float | None = None) -> list[str]:
    entry = recent(store, target, minutes, now)
    return [str(x.get("id")) for x in (entry or {}).get("items", []) if x.get("id")]
