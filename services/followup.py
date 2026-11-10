"""分享之后的接话：有人在群里回应她分享的内容时，她自己接一句。

为什么需要单独一层：群聊里别人接着聊她发的东西，通常不会 @ 她。这时 AstrBot
的默认 LLM 不会触发，她就会一直沉默，看起来像「发完就没人了」。

本模块只负责判断「这条消息是不是在说她分享的东西」，以及频率控制；
具体说什么交给调用方的模型生成，保持她自己的口吻。
"""
from __future__ import annotations

import re
import time

#: 单次分享后最多主动接几次话。
MAX_REPLIES = 2
#: 两次接话之间的最小间隔（秒）。
REPLY_GAP = 90.0
#: 关键词至少多少字才拿来做匹配，太短容易误判。
MIN_TOKEN = 2

#: 英文数字按词切；中文按连续汉字取双字切片（下文处理）。
_TOKEN = re.compile(r"[A-Za-z0-9]{3,}")
_CJK_RUN = re.compile(r"[\u4e00-\u9fa5]+")

#: 这两个字都在里面时，说明这个双字切片基本没有信息量，丢掉。
_STOP_CHARS = set("的了是在我也你他她它们这那就都很和与及之吗呢吧啊哦嗯呀么嘛着过把被")

#: 这些消息明显不是在跟她说话。
_IGNORE = re.compile(
    r"^(?:\[图片\]|\[表情\]|\[语音\]|\[视频\]|\[文件\]|\[CQ:.*\])$"
)


def tokens_of(text: str, limit: int = 60) -> set[str]:
    """取出用于相关性判断的词。

    中文没有空格，按整句切词不可靠，因此改用**双字切片**：既保留「银松」「价格」
    这种实义片段，也能命中「银松镇那个我也在玩」这类整句。由两个虚词组成的切片
    （如「的了吗」）会丢掉，减少误触发。

    Args:
        text: 标题、摘要或聊天内容。
        limit: 最多取多少个词。

    Returns:
        归一化后的词集合。
    """
    text = str(text or "")
    out: set[str] = set()
    for match in _TOKEN.findall(text):
        out.add(match.lower())
        if len(out) >= limit:
            return out
    for run in _CJK_RUN.findall(text):
        for index in range(len(run) - 1):
            pair = run[index:index + 2]
            if pair[0] in _STOP_CHARS and pair[1] in _STOP_CHARS:
                continue
            out.add(pair)
            if len(out) >= limit:
                return out
    return out


def relevant(message: str, items: list[dict], threshold: int = 1) -> bool:
    """判断一条消息是否在聊她刚分享的内容。

    命中规则：消息里出现了标题或摘要中的某个实义片段。只要求一个，
    因为群里接话常常很短（「这个我也看了」「哈哈确实」）；真不相关时
    模型会自己选择不接，这里宁可稍微宽松一点。

    Args:
        message: 收到的消息文本。
        items: :func:`core.radar_session.recent` 返回的 ``items``。
        threshold: 至少要命中几个片段。

    Returns:
        是否相关。
    """
    text = str(message or "").strip()
    if len(text) < 2 or _IGNORE.match(text):
        return False
    said = tokens_of(text)
    if not said:
        return False
    need = max(1, int(threshold))
    for item in items or []:
        source = tokens_of(f"{item.get('title') or ''} {item.get('digest') or ''}")
        if len(said & source) >= need:
            return True
    return False


class FollowUpState:
    """记录每个会话的接话次数，防止她一个人刷屏。"""

    KEY = "followup_state"

    def __init__(self, store):
        self.store = store

    def _all(self) -> dict:
        data = self.store.get(self.KEY, {})
        return data if isinstance(data, dict) else {}

    def can_reply(self, umo: str, *, max_replies: int = MAX_REPLIES,
                  gap: float = REPLY_GAP, now: float | None = None) -> bool:
        """判断这个会话现在能不能再接一句。

        Args:
            umo: 会话标识。
            max_replies: 本次分享后最多接几次。
            gap: 两次之间至少隔多少秒。
            now: 当前时间，测试用。

        Returns:
            是否可以接话。
        """
        now = time.time() if now is None else now
        record = self._all().get(umo)
        if not isinstance(record, dict):
            return True
        if int(record.get("replies") or 0) >= max(0, int(max_replies)):
            return False
        return now - float(record.get("last_reply") or 0) >= max(0.0, float(gap))

    def mark(self, umo: str, now: float | None = None) -> None:
        """记下这一次接话。"""
        now = time.time() if now is None else now
        data = self._all()
        record = data.get(umo)
        if not isinstance(record, dict):
            record = {"replies": 0, "last_reply": 0}
        record["replies"] = int(record.get("replies") or 0) + 1
        record["last_reply"] = now
        data[umo] = record
        # 只保留最近的一批会话，避免无限增长。
        if len(data) > 80:
            data = dict(sorted(data.items(),
                               key=lambda kv: float((kv[1] or {}).get("last_reply") or 0),
                               reverse=True)[:80])
        self.store.set(self.KEY, data)

    def reset(self, umo: str) -> None:
        """她再次分享时重置计数，让新的一轮重新算。"""
        data = self._all()
        if umo in data:
            data.pop(umo, None)
            self.store.set(self.KEY, data)
