"""插件页面的后端接口。

AstrBot 的插件视图（``views/<name>/index.html``）通过
``window.AstrBotPluginView`` 调用这里注册的 Web API。只做只读汇总，
不提供任何修改配置或触发写操作的入口。
"""
from __future__ import annotations

import time

from astrbot.api import logger

try:  # AstrBot 4.29+ 提供 astrbot.api.web
    from astrbot.api.web import error_response, json_response, request
except Exception:  # pragma: no cover - 老版本降级
    error_response = json_response = request = None


class PageMixin:
    """把状态汇总成 JSON，供插件页面展示。"""

    def _register_page_api(self) -> None:
        """注册只读接口；不可用时静默跳过，不影响插件加载。"""
        register = getattr(self.context, "register_web_api", None)
        if register is None or json_response is None:
            logger.debug("[CommunityRadar] 当前 AstrBot 不支持插件 Web API，跳过页面接口")
            return
        route = "/astrbot_plugin_community_radar/overview"
        try:
            register(route, self.page_overview, ["GET"], "社区雷达状态总览")
            logger.info("[CommunityRadar] 插件页面接口已注册：%s", route)
        except Exception as exc:  # noqa: BLE001 - 重复注册或版本差异都不该影响启动
            logger.debug(f"[CommunityRadar] 页面接口注册失败（{type(exc).__name__}）")

    async def page_overview(self):
        """返回状态总览 JSON。"""
        try:
            return json_response(self._overview_payload())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CommunityRadar] 页面数据生成失败（{type(exc).__name__}）")
            return error_response("状态读取失败，请稍后再试")

    def _overview_payload(self) -> dict:
        """把各服务里的状态拼成一份可直接渲染的数据。"""
        store = self.radar_store
        knowledge = getattr(self, "knowledge", None)
        watchlist = getattr(self, "watchlist", None)
        health = getattr(self, "health", None)
        browse = self.config.get("browse") or {}
        share = self.config.get("share") or {}
        video = self.config.get("video") or {}
        return {
            "generated_at": time.time(),
            "browse": {
                "enabled": bool(browse.get("enable")),
                "platforms": browse.get("platforms") or "all",
                "next_run": self.duty.next_run_text(),
                "last": store.get("last_browse") or {},
                "activity": store.get("activity") or {},
            },
            "share": {
                "enabled": bool(share.get("enable", True)),
                "dry_run": bool(share.get("dry_run", False)),
                "style": share.get("style") or "chat",
                "max_per_day": share.get("max_per_day"),
                "max_images": share.get("max_images"),
                "min_score": share.get("min_score_to_share"),
                "today": store.stats().get("share_today", 0),
                "preview": store.get("last_preview") or {},
            },
            "ledger": store.stats(),
            "knowledge": knowledge.stats() if knowledge else {},
            "interests": [
                {"value": item.get("value"), "polarity": item.get("polarity"),
                 "strength": item.get("strength")}
                for item in (store.preferences() or [])[:8]
            ],
            "curiosity": store.curiosity(6) if hasattr(store, "curiosity") else [],
            "watchlist": [
                {"title": item.get("title") or item.get("url"),
                 "url": item.get("url"), "from": item.get("from_user")}
                for item in (watchlist.peek(8) if watchlist else [])
            ],
            "health": health.summary() if health else {},
            "video": {
                "frame_source": video.get("frame_source") or "ffmpeg",
                "frame_width": video.get("frame_width"),
                "frame_compose": video.get("frame_compose") or "auto",
                "ocr": {
                    "enabled": bool(video.get("ocr_subtitles", False)),
                    "fps": video.get("ocr_fps"),
                    "max_frames": video.get("ocr_max_frames"),
                },
            },
        }
