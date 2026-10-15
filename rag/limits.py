"""公开演示模式的提问限额：每个会话、全站每天各有一个上限。

注意：这是“软限制”。新开浏览器会话能重置会话计数，应用重启会重置全站计数。
真正的损失上限是 DeepSeek 账户里预充值的余额，建议只充少量。
"""
import threading
from datetime import date


class Quota:
    def __init__(self, daily_limit: int, today=date.today):
        self.daily_limit = daily_limit
        self._today = today
        self._lock = threading.Lock()
        self._day = today()
        self._used = 0

    def _roll(self):
        if self._today() != self._day:
            self._day, self._used = self._today(), 0

    def used_today(self) -> int:
        with self._lock:
            self._roll()
            return self._used

    def try_consume(self) -> bool:
        """占用一次全站额度。额度用完返回 False，不计数。"""
        with self._lock:
            self._roll()
            if self._used >= self.daily_limit:
                return False
            self._used += 1
            return True

    def refund(self):
        """调用大模型失败时退还额度。"""
        with self._lock:
            self._roll()
            self._used = max(0, self._used - 1)


def check_and_consume(session_used: int, session_limit: int, quota: Quota) -> tuple[bool, str]:
    """返回 (是否放行, 被拒绝时的原因)。会话额度先检查，通过后才占全站额度。"""
    if session_used >= session_limit:
        return False, "session"
    if not quota.try_consume():
        return False, "daily"
    return True, ""
