"""一轮浏览的节奏控制。

人刷社区不是匀速的：有时看两条就收手，有时连着看一会儿；两次内容之间
会停几十秒。本模块只负责回答「这一轮看几条」「两条之间停多久」，不关心
内容本身，便于单独测试与调整。

参考设计：chenluQwQ/astrbot_plugin_bilibili_ai_bot 的主动看片节奏。
"""

from __future__ import annotations

import random
from dataclasses import dataclass


#: 两条内容之间停顿的秒数下限。
MIN_PAUSE_SECONDS = 20.0
#: 两条内容之间停顿的秒数上限。
MAX_PAUSE_SECONDS = 90.0
#: 「看完这条就不看了」的最低判断阈值；低于它时仍至少看一条。
MIN_ITEMS_BEFORE_STOPPING = 1


@dataclass(frozen=True)
class Pacing:
    """一轮浏览的规模与节奏。

    Attributes:
        planned: 计划看几条（候选上限）。
        pause: ``(最短, 最长)`` 两条内容之间的停顿秒数。
        stop_after: 看到第几条后允许「没劲了，收手」。
        boredom: 每条看完后「今天不想再看了」的概率，模拟状态起伏。
    """

    planned: int
    pause: tuple[float, float] = (MIN_PAUSE_SECONDS, MAX_PAUSE_SECONDS)
    stop_after: int = MIN_ITEMS_BEFORE_STOPPING
    boredom: float = 0.12

    def pause_seconds(self) -> float:
        """取一个本次使用的停顿时间。"""
        low, high = self.pause
        low = max(0.0, float(low))
        high = max(low, float(high))
        if high <= 0:
            return 0.0
        return random.uniform(low, high)

    def should_stop(self, watched: int, verdict: dict | None = None) -> bool:
        """看完一条之后，判断这一轮是否该收手。

        只有在看过 ``stop_after`` 条之后才可能提前结束，避免刚开一局就退出。
        收手有两个来源：这条内容实在没意思（评分很低），或单纯「今天看够了」。
        后者是随机的，模拟人的状态起伏，而不是每次都把候选清单刷完。

        Args:
            watched: 本轮已看完的条数。
            verdict: 刚看完那一条的观感，含 ``score``（0~10）；未知时传 ``None``。

        Returns:
            是否停止本轮继续观看。
        """
        target = max(1, int(self.planned))
        if watched >= target:
            return True
        if watched < max(1, self.stop_after):
            return False
        score = None
        if isinstance(verdict, dict):
            try:
                score = float(verdict.get("score"))
            except (TypeError, ValueError):
                score = None
        if score is not None and score < 3.0:
            return True
        return random.random() < max(0.0, min(1.0, float(self.boredom)))
