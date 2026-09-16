"""工作陪伴记录 (v0.9)。

语义由用户定义 —— **宠物醒着 = 在陪你工作；点"睡觉" = 今天收工**。
所以这个模块回答两个问题: 今天陪了你多久、这段陪你坐了多久。

口径: 只有"宠物醒着 **且** 你确实在用电脑"的时间才计入。
离开电脑(倒水、开会)的时段不算, 否则"陪了你 8 小时"会虚高 —— 那就不真诚了。

存档: pets/<名字>/work.json (照 PetStatus 的 JSON 范式)
"""
import json
import os
import time
from datetime import date


def format_minutes(minutes: float) -> str:
    """把分钟数说成人话: 47 分钟 / 3 小时 7 分 / 2 小时。"""
    total = int(round(minutes))
    if total < 60:
        return f"{total} 分钟"
    hours, mins = divmod(total, 60)
    return f"{hours} 小时 {mins} 分" if mins else f"{hours} 小时"


class WorkLog:
    """陪伴时长记账。每只宠物各存一份。"""

    def __init__(self, pet_dir: str):
        self.save_path = os.path.join(pet_dir, "work.json")
        today = self._today()
        self.first_day = today          # 第一次打开, 用于"陪伴天数"
        self.today = today
        self.today_minutes = 0.0
        self.total_minutes = 0.0
        self.last_session_minutes = 0.0  # 上一段收工时有多长
        self.sessions_today = 0          # 今天收工过几次
        self.in_session = False
        self._session_seconds = 0.0      # 当前这段还没收工的时长
        self.load()

    # ---------- 持久化 ----------
    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    def load(self):
        if not os.path.exists(self.save_path):
            return
        try:
            with open(self.save_path, encoding="utf-8") as f:
                d = json.load(f)
            self.first_day = str(d.get("first_day", self.first_day))
            self.today = str(d.get("today", self.today))
            self.today_minutes = float(d.get("today_minutes", 0))
            self.total_minutes = float(d.get("total_minutes", 0))
            self.last_session_minutes = float(d.get("last_session_minutes", 0))
            self.sessions_today = int(d.get("sessions_today", 0))
        except (json.JSONDecodeError, ValueError, OSError):
            pass  # 存档损坏时用默认值, 不让桌宠起不来
        self._rollover()

    def save(self):
        try:
            with open(self.save_path, "w", encoding="utf-8") as f:
                json.dump({
                    "first_day": self.first_day,
                    "today": self.today,
                    "today_minutes": round(self.today_minutes, 1),
                    "total_minutes": round(self.total_minutes, 1),
                    "last_session_minutes": round(self.last_session_minutes, 1),
                    "sessions_today": self.sessions_today,
                }, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _rollover(self):
        """跨天: 当天计数归零, 总计保留。"""
        today = self._today()
        if today != self.today:
            self.today = today
            self.today_minutes = 0.0
            self.sessions_today = 0
            self._session_seconds = 0.0
            self.in_session = False

    # ---------- 会话 ----------
    def start_session(self):
        """宠物醒来 -> 开始一段陪伴。"""
        self._rollover()
        if not self.in_session:
            self.in_session = True
            self._session_seconds = 0.0

    def end_session(self) -> float:
        """收工 -> 结束这段, 返回这段的分钟数。"""
        if self.in_session:
            self.last_session_minutes = self._session_seconds / 60.0
            self.sessions_today += 1
        self.in_session = False
        self._session_seconds = 0.0
        self.save()
        return self.last_session_minutes

    def tick(self, seconds: float):
        """累计实际经过的秒数 (只在该计时的时候调用)。"""
        self._rollover()
        if not self.in_session or seconds <= 0:
            return
        self._session_seconds += seconds
        self.today_minutes += seconds / 60.0
        self.total_minutes += seconds / 60.0

    # ---------- 查询 ----------
    def current_session_minutes(self) -> float:
        return self._session_seconds / 60.0

    def today_text(self) -> str:
        return format_minutes(self.today_minutes)

    def total_text(self) -> str:
        return format_minutes(self.total_minutes)

    def days_together(self) -> int:
        try:
            return (date.today() - date.fromisoformat(self.first_day)).days + 1
        except ValueError:
            return 1
