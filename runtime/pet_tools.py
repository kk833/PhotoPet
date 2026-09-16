"""第三梯队实用功能 (v0.4)。

- PomodoroTimer: 番茄钟状态机 (设计参考开源项目 TomatoClock 的 QTimer 模式)
  默认 25 分钟专注 / 5 分钟休息, 结束回调通知宠物做庆祝动作。
- 全屏检测: Windows 用 GetForegroundWindow 与屏幕矩形比对 (社区标准做法),
  非 Windows 平台自动禁用, 不影响其他功能。
- 久坐检测 (v0.8.1): GetLastInputInfo 读键鼠空闲时长, 累计"连续在用电脑"的时间,
  到点提醒休息; 离开电脑自动重新计时。非 Windows 平台自动禁用。
"""
import ctypes
import ctypes.wintypes
import sys
import time


class PomodoroTimer:
    """番茄钟状态机。work_sec/break_sec 可自定义。

    用法:
        p = PomodoroTimer(on_phase_end=回调)
        p.start_work()
        p.tick()  # 每秒调用, 返回剩余秒数
    """

    def __init__(self, work_sec: int = 25 * 60, break_sec: int = 5 * 60):
        self.work_sec = work_sec
        self.break_sec = break_sec
        self.phase: str | None = None  # None / "work" / "break"
        self.remain = 0
        self.on_phase_end = None  # callback(phase)

    @property
    def running(self) -> bool:
        return self.phase is not None

    def start_work(self):
        self.phase = "work"
        self.remain = self.work_sec

    def start_break(self):
        self.phase = "break"
        self.remain = self.break_sec

    def stop(self):
        self.phase = None
        self.remain = 0

    def tick(self) -> int:
        """返回剩余秒数; 阶段结束时触发 on_phase_end 回调。"""
        if not self.running:
            return 0
        self.remain -= 1
        if self.remain <= 0:
            ended = self.phase
            if ended == "work":
                self.start_break()
            else:
                self.stop()
            if self.on_phase_end:
                self.on_phase_end(ended)
        return self.remain

    def mmss(self) -> str:
        m, s = divmod(max(self.remain, 0), 60)
        return f"{m:02d}:{s:02d}"


def is_fullscreen() -> bool:
    """检测前台窗口是否全屏 (覆盖整个显示器则视为全屏)。

    仅 Windows 有效; 其他平台返回 False (桌宠保持显示)。
    参考: 用 GetForegroundWindow 的窗口矩形与显示器工作区比较,
    排除桌面 (Progman/WorkerW) 与任务栏误判。
    """
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        # 排除桌面窗口
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        if buf.value in ("Progman", "WorkerW"):
            return False
        rect = ctypes.wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        # 与虚拟屏幕比较 (取主屏)
        screen_w = user32.GetSystemMetrics(0)   # SM_CXSCREEN
        screen_h = user32.GetSystemMetrics(1)   # SM_CYSCREEN
        return (rect.left <= 0 and rect.top <= 0
                and rect.right >= screen_w and rect.bottom >= screen_h)
    except Exception:  # noqa: BLE001
        return False


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.wintypes.UINT),
                ("dwTime", ctypes.wintypes.DWORD)]


def idle_seconds() -> float | None:
    """距最近一次键鼠输入过了多少秒; 不支持的平台返回 None。

    用 GetLastInputInfo 拿"最后一次输入的系统 tick", 与当前 tick 相减。
    两个 tick 都是 32 位, 所以按 32 位取模相减, 跨 49.7 天回绕也不会算错。
    """
    if sys.platform != "win32":
        return None
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        tick = ctypes.windll.kernel32.GetTickCount64() & 0xFFFFFFFF
        return ((tick - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:  # noqa: BLE001
        return None


class RestWatcher:
    """久坐提醒: 累计"连续在用电脑"的时长, 到点让宠物喊你起来动动。

    只在**真的一直有键鼠输入**时计时; 离开超过 idle_sec 秒(去倒水、开会)
    就把这一轮计时清零, 回来重新算 —— 不会"人不在还催你休息"。

    用法:
        w = RestWatcher(active_minutes=45)
        if w.poll():   # 每分钟调用一次
            pet.say("该休息啦")
    """

    def __init__(self, active_minutes: float = 45, idle_sec: float = 60):
        self.active_seconds = max(1, int(active_minutes * 60))
        self.idle_sec = max(5, idle_sec)
        self._streak_start: float | None = None
        self._snooze_until: float = 0.0
        self.last_remind: float | None = None
        # 上一次提醒时, 这一轮实际连续用了多久 (提醒后计时会重置, 所以必须当时记下来)
        self.last_active_minutes: float = 0.0
        self.try_count = 0               # 已经劝过几次, 用于话术升级

    @property
    def supported(self) -> bool:
        return idle_seconds() is not None

    def snooze(self, minutes: float, now: float | None = None):
        """用户选了"再干一会儿": 这段时间内不再打扰。"""
        now = time.time() if now is None else now
        self._snooze_until = now + max(1, minutes) * 60

    def poll(self, now: float | None = None) -> bool:
        """返回 True 表示"该提醒休息了"(并已重置计时, 不会连环催)。"""
        now = time.time() if now is None else now
        idle = idle_seconds()
        if idle is None:                 # 平台不支持 -> 永不提醒
            return False
        if now < self._snooze_until:     # 用户说了"再干一会儿"
            return False
        if idle > self.idle_sec:         # 人不在, 重新计时
            self._streak_start = None
            return False
        if self._streak_start is None:   # 刚开始用 / 刚回来
            self._streak_start = now
            return False
        elapsed = now - self._streak_start
        if elapsed >= self.active_seconds:
            # 先记下"这次连续用了多久"再重置, 否则文案里会永远是 0 分钟
            self.last_active_minutes = elapsed / 60.0
            self.last_remind = now
            self.try_count += 1
            self._streak_start = now     # 提醒后重新计一轮
            return True
        return False

    def reset_tries(self):
        """收工/重新开始工作 -> 话术从头来过。"""
        self.try_count = 0
        self._snooze_until = 0.0

    def active_minutes(self) -> float:
        """这一轮到目前为止连续用了多久(分钟)。"""
        if self._streak_start is None:
            return 0.0
        return (time.time() - self._streak_start) / 60.0
