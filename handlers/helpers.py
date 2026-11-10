#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享辅助：临时上下文注入、主人判定、私信与二维码回复。"""
from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import ProviderRequest

from ..clients.bili import BiliError
from ..core.compat import TextPart
from ..core.owners import admin_ids as resolve_admin_ids
from ..core.owners import astrbot_admins as resolve_astrbot_admins
from ..core.owners import owner_umo as resolve_owner_umo
from ..core.radar_session import recent as recent_session_share


class HelpersMixin:
    def _recent_share(self, umo: str) -> dict | None:
        minutes = float((self.config.get("share") or {}).get("followup_minutes", 45) or 45)
        return recent_session_share(self.radar_store, umo, minutes)

    @staticmethod
    def _append_temp(req: ProviderRequest, text: str) -> None:
        """把本轮临时上下文挂到请求上，不写入会话历史。"""
        text = str(text or "").strip()
        if not text:
            return
        parts = getattr(req, "extra_user_content_parts", None)
        if parts is None:
            req.extra_user_content_parts = []
            parts = req.extra_user_content_parts
        for part in parts:
            if getattr(part, "text", None) == text:
                return
        parts.append(TextPart(text=text).mark_as_temp())

    def _astrbot_admins(self) -> list[str]:
        """读取 AstrBot 全局管理员列表，作为 ``admin_uin`` 留空时的兜底。"""
        return resolve_astrbot_admins(self)

    def _admin_ids(self) -> list[str]:
        """返回主人账号列表：优先插件配置，留空时用 AstrBot 全局管理员。"""
        return resolve_admin_ids(self)

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        """判定发送者是否为主人。

        未配置 ``admin_uin`` 且 AstrBot 全局也没有管理员时，一律拒绝写操作，
        并只告警一次，避免误以为写操作可用。
        """
        admins = self._admin_ids()
        if not admins:
            if not self._owner_warned:
                self._owner_warned = True
                logger.warning(
                    "[CommunityRadar] 未配置「主人账号」(admin_uin)，"
                    "且 AstrBot 全局管理员（admins_id）为空；"
                    "写操作（评论/点赞/私信/发帖）已全部禁用"
                )
            return False
        try:
            sender = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            sender = ""
        return bool(sender) and sender in admins

    def _platform_name(self) -> str:
        try:
            pm = self.context.platform_manager
            insts = pm.get_insts() if hasattr(pm, "get_insts") else getattr(pm, "platform_insts", [])
            for inst in insts or []:
                try:
                    pid = inst.meta().id
                    if pid and "webchat" not in str(pid).lower():
                        return str(pid)
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass
        return "default"

    def _owner_umo(self) -> str:
        """返回主人私聊的会话标识（UMO）。

        未配置 ``admin_uin`` 时自动使用 AstrBot 全局管理员，因此不填插件配置
        也能正常推送。
        """
        return resolve_owner_umo(self)

    def _bili_owner_mid(self) -> int:
        return int((self.config.get("bili") or {}).get("owner_mid") or 0)

    async def _send_bili_dm(self, text: str) -> str:
        """发一条 B 站私信给主人；返回空串表示成功，否则是错误信息。"""
        mid = self._bili_owner_mid()
        if not mid:
            return "没配置主人的 B 站号（配置页「B 站」→ 主人 UID）"
        try:
            await self.bili.send_msg(mid, text)
            return ""
        except BiliError as exc:
            return f"B 站私信失败：{exc}"

    async def _qr_result(self, event: AstrMessageEvent, path: str, note: str):
        img = getattr(event, "image_result", None)
        if callable(img):
            return [event.plain_result(note), img(str(path))]
        try:
            from astrbot.core.message.components import Image
            return [event.plain_result(note), event.chain_result([Image.fromFileSystem(str(path))])]
        except Exception:  # noqa: BLE001
            return [event.plain_result(f"{note}\n二维码图片：{path}")]
