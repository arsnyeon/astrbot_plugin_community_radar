"""平台可用性记录：连续失败到一定程度才提醒主人。

平台会时不时抽风（验证码、403、风控），偶尔失败是常态，不该每次都打扰主人。
这里只记录「连续失败次数」与最近原因，达到阈值且冷却期已过时，由上层用模型
生成一句人话去说明，而不是发固定文案。
"""

from __future__ import annotations

import time

#: 连续失败达到这个次数才认为「被挡住了」。
BLOCK_THRESHOLD = 3
#: 同一个平台的提醒冷却时间（秒），避免反复打扰。
NOTIFY_COOLDOWN = 12 * 3600


class PlatformHealth:
    """记录各平台最近的成败，判断是否需要提醒主人。"""

    def __init__(self, store):
        self.store = store

    def _all(self) -> dict:
        data = self.store.get("platform_health", {})
        return data if isinstance(data, dict) else {}

    def note(self, platform: str, ok: bool, reason: str = "") -> None:
        """记录一次请求结果。

        成功会把连续失败次数清零；失败会累加并记下最近原因。

        Args:
            platform: 平台标识，例如 ``heybox``。
            ok: 这次是否成功取到内容。
            reason: 失败原因摘要，只用于生成提醒与排查。
        """
        platform = str(platform or "").strip()
        if not platform:
            return
        data = self._all()
        record = data.get(platform)
        if not isinstance(record, dict):
            record = {"fails": 0, "reason": "", "last_at": 0, "notified_at": 0}
        record["last_at"] = time.time()
        if ok:
            record["fails"] = 0
            record["reason"] = ""
        else:
            record["fails"] = int(record.get("fails") or 0) + 1
            record["reason"] = str(reason or "")[:120]
            # 登录类失败单独记一笔：这种只有主人能解决。
            if any(word in record["reason"] for word in ("-101", "未登录", "登录", "验证码", "captcha")):
                self.note_auth(platform, record["reason"])
        data[platform] = record
        self.store.set("platform_health", data)

    def note_auth(self, platform: str, detail: str = "") -> None:
        """记下「这个平台需要重新登录」。

        与「被挡住」分开记录：被挡住可能只是风控，登录过期则必须主人出手。

        Args:
            platform: 平台标识。
            detail: 触发原因摘要。
        """
        platform = str(platform or "").strip()
        if not platform:
            return
        data = self.store.get("auth_needed", {})
        if not isinstance(data, dict):
            data = {}
        data[platform] = {"at": time.time(), "detail": str(detail or "")[:120]}
        self.store.set("auth_needed", data)

    def pending_auth(self) -> list[dict]:
        """返回需要重新登录、且还没处理过的平台。"""
        data = self.store.get("auth_needed", {})
        if not isinstance(data, dict):
            return []
        return [{"platform": platform, "detail": str(record.get("detail") or "")[:120]}
                for platform, record in data.items() if isinstance(record, dict)]

    def clear_auth(self, platform: str) -> None:
        """登录成功后清除该平台的待登录状态。"""
        data = self.store.get("auth_needed", {})
        if isinstance(data, dict) and platform in data:
            data.pop(platform, None)
            self.store.set("auth_needed", data)

    def blocked(self) -> list[dict]:
        """返回已被判定为「挡住」且还该提醒的平台。

        Returns:
            每项含 ``platform``/``fails``/``reason``；达到阈值且在冷却期外才包含。
        """
        now = time.time()
        out = []
        for platform, record in self._all().items():
            if not isinstance(record, dict):
                continue
            fails = int(record.get("fails") or 0)
            if fails < BLOCK_THRESHOLD:
                continue
            notified = float(record.get("notified_at") or 0)
            if now - notified < NOTIFY_COOLDOWN:
                continue
            out.append({
                "platform": platform,
                "fails": fails,
                "reason": str(record.get("reason") or "")[:120],
            })
        return out

    def pending(self) -> list[dict]:
        """返回「已经被挡住、但还没跟主人说过」的平台。

        与 :meth:`blocked` 不同，这里不受冷却期影响，专门用于把问题交给模型转述。

        Returns:
            每项含 ``platform``/``fails``/``reason``。
        """
        out = []
        for platform, record in self._all().items():
            if not isinstance(record, dict):
                continue
            fails = int(record.get("fails") or 0)
            if fails < BLOCK_THRESHOLD:
                continue
            if float(record.get("notified_at") or 0) > 0:
                continue
            out.append({"platform": platform, "fails": fails,
                        "reason": str(record.get("reason") or "")[:120]})
        return out

    def mark_notified(self, platforms: list[str]) -> None:
        """记下已经提醒过，避免冷却期内重复说。"""
        data = self._all()
        now = time.time()
        for platform in platforms:
            record = data.get(platform)
            if isinstance(record, dict):
                record["notified_at"] = now
        self.store.set("platform_health", data)

    def summary(self) -> dict:
        """给状态命令用的简要视图。"""
        out = {}
        for platform, record in self._all().items():
            if not isinstance(record, dict):
                continue
            fails = int(record.get("fails") or 0)
            if fails:
                out[platform] = {"fails": fails, "reason": str(record.get("reason") or "")[:80]}
        return out
