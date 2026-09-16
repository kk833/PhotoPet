"""PhotoPet 桌宠运行时 — PyQt6 透明悬浮窗 (v0.2)。

v0.2 新增（参考开源项目实现）:
- GIF/序列动画: QMovie 按状态映射 (参考 Desktop_Pet_LLM 的 GIF 方案)
- 大小调整: 托盘「大小」Word 风格百分比档位 (60%~180%), 选中即固定; 滚轮缩放已移除
- 系统托盘: QSystemTrayIcon, 关闭窗口不退出 (参考 Qt 官方 systray 示例)
- 开机自启动: HKCU Run 注册表, 无需管理员权限 (参考社区标准做法)
- 多桌宠: main.py --pet 支持多个目录, 一次启动多只

用法:
    python main.py --pet pets/mypet
    python main.py --pet pets/cat pets/dog        # 多只
    python main.py                                # 不带参数: 加载 pets/ 下全部
"""
import argparse
import json
import os
import sys
import random
import subprocess
import threading
import time

from pet_status import PetStatus
from pet_work import WorkLog, format_minutes
from pet_tools import PomodoroTimer, RestWatcher, is_fullscreen, idle_seconds
from pet_ai import AIChat, PetTTS, load_ai_config, save_ai_config
from chat_window import ChatWindow
import pet_pack
from pet_profile import PetProfile
import pet_schedule
import pet_task
import pet_import
import pet_jobfair
import pet_weather
import pet_memory
from datetime import datetime
from pet_extras import set_click_through as win32_click_through, match_action

from PyQt6.QtCore import QSettings

from PyQt6.QtCore import (Qt, QTimer, QPoint, QPointF, QRectF, QSize, QUrl,
                          pyqtSignal)
from PyQt6.QtGui import (QPixmap, QAction, QActionGroup, QMovie, QIcon, QImageReader,
                         QDesktopServices,
                         QPainter, QColor, QBrush, QPen, QPainterPath, QImage,
                         QLinearGradient)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput, QMediaDevices
from PyQt6.QtWidgets import (QApplication, QLabel, QMenu, QSystemTrayIcon,
                             QMessageBox, QFrame, QPushButton, QVBoxLayout,
                             QHBoxLayout, QFileDialog, QInputDialog, QWidget,
                             QDialog, QFormLayout, QLineEdit, QComboBox,
                             QSpinBox, QDialogButtonBox, QScrollArea, QCheckBox)

import app_paths

ROOT = app_paths.BUNDLE_DIR          # 打进 exe 的只读资源 / 源码仓库根

ZOOM_MIN, ZOOM_MAX = 0.6, 1.8  # 参考 OpenDesktopPet 的缩放范围
# 托盘「大小」Word 风格百分比档位 (选中即固定)
ZOOM_STEPS = [0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8]

# 开机自启动注册表键 (HKCU 无需管理员权限)
APP_NAME = "PhotoPet"
# .pet 文件关联用的 ProgID
PET_PROG_ID = "PhotoPet.pet"


_TASKS = None


def tasks():
    """全进程共用的待办对象 (用户级, 和日程/记忆一样不按宠物分家)。"""
    global _TASKS
    if _TASKS is None:
        _TASKS = pet_task.TaskStore(pet_task.default_path())
    return _TASKS


_SCHEDULE = None


def schedule():
    """全进程共用的日程对象 (存在用户数据目录, **不按宠物分家**)。

    日程是"人的事"不是"某只宠物的事": 同时养两只宠物时不该各提醒一遍。
    共用一个对象还有个好处 —— 提醒去重 (fired) 天然生效, 谁先说都只算一次。
    """
    global _SCHEDULE
    if _SCHEDULE is None:
        _SCHEDULE = pet_schedule.Schedule(pet_schedule.default_path())
    return _SCHEDULE


def _tray_icon() -> QIcon:
    """托盘图标: 优先 runtime/icon.png, 缺失时用 QPainter 画一个兜底图标。

    仓库 .gitignore 排除了 *.png, 不能假定 icon.png 一定存在;
    QIcon() 空图标在托盘里是看不见的, 所以必须兜底绘制。
    """
    path = os.path.join(ROOT, "runtime", "icon.png")
    if os.path.exists(path):
        icon = QIcon(path)
        if not icon.isNull():
            return icon
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#ffb35c")))          # 掌垫
    p.drawEllipse(20, 32, 26, 24)
    p.setBrush(QBrush(QColor("#5a3b1a")))          # 四个脚趾
    for cx, cy in ((22, 24), (42, 24), (14, 40), (50, 40)):
        p.drawEllipse(cx - 7, cy - 7, 14, 14)
    p.end()
    return QIcon(pm)


def _autostart_command(pet_dirs: list[str]) -> str:
    """开机自启动命令行。

    - 打包成 exe 后指向 exe, 源码运行时指向 pythonw 避免黑窗
      (没有 pythonw.exe 时回退 python.exe, 不能写出一个不存在的路径)
    - 必须带上 --pet: 否则开机只会去加载默认资源包, 用户自己的桌宠不见了
    """
    if getattr(sys, "frozen", False):
        launcher = f'"{sys.executable}"'
    else:
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = pythonw if os.path.exists(pythonw) else sys.executable
        launcher = f'"{exe}" "{os.path.join(ROOT, "runtime", "main.py")}"'
    pets = " ".join(f'"{os.path.abspath(d)}"' for d in pet_dirs)
    return f"{launcher} --pet {pets}" if pets else launcher


def is_auto_start() -> bool:
    """当前是否已设置开机自启动。"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run")
    except (ImportError, OSError):
        return False
    try:
        winreg.QueryValueEx(key, APP_NAME)
        return True
    except FileNotFoundError:
        return False
    finally:
        winreg.CloseKey(key)


def set_auto_start(enabled: bool, pet_dirs: list[str]) -> bool:
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ,
                              _autostart_command(pet_dirs))
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except (ImportError, OSError):
        return False  # 非 Windows 或权限受限时静默降级


class PetWindow(QLabel):
    # AI 回复在后台线程产生, 用它把结果切回主线程再碰 GUI
    # (跨线程直接操作 QLabel/QTimer 是未定义行为, 会随机崩溃)
    ai_reply_ready = pyqtSignal(str)
    # 语音合成在工作线程完成, 同样用信号把"可以播放了"切回主线程
    # 参数: (音频路径或空串, 这句大约说多久)
    speech_ready = pyqtSignal(str, float)
    # 大模型解析导入文件的结果 (在工作线程产生) -> 切回主线程弹预览
    # 参数: (事件列表, 错误信息)
    import_parsed = pyqtSignal(list, str)
    # 就业网抓取结果 (工作线程 -> 主线程)。手动抓取和定时同步**分成两条信号**,
    # 因为两者行为完全不同: 手动要弹预览让人勾选, 定时是安静地直接加
    jobfair_ready = pyqtSignal(list, str)
    jobfair_auto_ready = pyqtSignal(list)

    def __init__(self, pet_dir: str, all_pet_dirs: list[str] | None = None):
        super().__init__()
        self.pet_dir = pet_dir
        # 开机自启动要把所有宠物一起注册, 所以需要知道全量目录
        self.all_pet_dirs = list(all_pet_dirs) if all_pet_dirs else [pet_dir]
        with open(os.path.join(pet_dir, "pet.json"), encoding="utf-8") as f:
            self.config = json.load(f)
        # 资源包自定义台词: pet.json 的 dialogues, 为空则用内置情绪台词
        self.dialogues = [str(s) for s in self.config.get("dialogues", [])
                          if str(s).strip()]

        self.drag_pos: QPoint | None = None
        self.state = "idle"
        # 应用级配置 (说话时机 / 语音 / AI): 定时器创建之前就要用到, 所以先加载
        self.cfg = load_ai_config()
        self.base_size = QSize(*self.config.get("size", [160, 160]))
        # 统一基准高度 (DyberPet 方案): 所有状态动画按同一人物高度渲染
        self.base_height = self.config.get("base_height", 160)
        self.zoom = 1.0  # 实际档位在读取 QSettings 后确定, 见 _restore_geometry

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAcceptDrops(True)      # 把 Excel/Word 直接拖到宠物身上

        self.resize(self.base_size)
        # v0.6: 恢复上次的位置和大小 (Qt 官方 QSettings 方案)
        self.settings = QSettings("PhotoPet", os.path.basename(pet_dir))
        self._restore_geometry()

        # --- 动画: 每个状态可对应 GIF (animation/) 或静态 PNG (avatar/) ---
        self.movies: dict[str, QMovie] = {}
        # 记下每个动画的原始帧尺寸: 说话动画要靠它判断"切过去会不会让窗口跳一下"
        self.movie_sizes: dict[str, QSize] = {}
        anim_dir = os.path.join(pet_dir, "animation")
        if os.path.isdir(anim_dir):
            for fn in os.listdir(anim_dir):
                if fn.endswith(".gif"):
                    state = os.path.splitext(fn)[0]
                    path = os.path.join(anim_dir, fn)
                    mv = QMovie(path, parent=self)
                    mv.setCacheMode(QMovie.CacheMode.CacheAll)
                    self.movies[state] = mv
                    # 用 QImageReader 直接读首帧尺寸, 不用把 movie 跑起来
                    reader = QImageReader(path)
                    if reader.canRead():
                        self.movie_sizes[state] = reader.size()

        self.float_phase = 0
        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self.tick)
        self.anim_timer.start(50)

        self.bubble = Bubble(self)

        # v0.8.1: 说话时机全部可配, 且默认静默陪伴 —— 只在互动和久坐提醒时开口。
        # 定时闲聊默认关闭 (chatter_seconds=0); 想要老行为就把它设成 15。
        chatter_sec = int(self.cfg.get("chatter_seconds", 0) or 0)
        if chatter_sec > 0:
            self.chat_timer = QTimer(self)
            self.chat_timer.timeout.connect(self.random_chat)
            self.chat_timer.start(chatter_sec * 1000)

        # --- 养成系统 (v0.3, 参考 DyberPet 的饱食/心情/好感模型) ---
        self.status = PetStatus(pet_dir)
        # --- 工作陪伴 (v0.9): 宠物醒着 = 在陪你工作; 点睡觉 = 今天收工 ---
        self.work = WorkLog(pet_dir)
        self.work.start_session()      # 启动时醒着, 直接开一段
        self._work_tick_at = time.time()
        self._last_hungry_nag = 0.0
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._status_tick)
        self.status_timer.start(60 * 1000)  # 每分钟衰减一次
        # 注意: 首次 _status_tick() 要等 tts/bubble 都建好之后再调用 (见 __init__ 末尾),
        # 否则"启动时恰好饿着"会在 say() 里撞上还不存在的 self.tts 而崩掉

        # --- 第三梯队 (v0.4): 番茄钟 / 随机动作 / 全屏隐藏 ---
        self.pomodoro = PomodoroTimer()
        self.pomodoro.on_phase_end = self._on_pomodoro_phase
        self.pomo_timer = QTimer(self)
        self.pomo_timer.timeout.connect(self._pomo_tick)
        self.pomo_timer.start(1000)

        # 随机小动作: 只做表情动作, 不再说话 (v0.8.1)
        random_min = int(self.cfg.get("random_action_minutes", 5) or 0)
        if random_min > 0:
            self.random_timer = QTimer(self)
            self.random_timer.timeout.connect(self._random_action)
            self.random_timer.start(random_min * 60 * 1000)

        self.fullscreen_timer = QTimer(self)
        self.fullscreen_timer.timeout.connect(self._check_fullscreen)
        self.fullscreen_timer.start(2000)
        # 全屏隐藏前是否可见 —— 用它保证"用户自己隐藏的"不会被全屏退出后强行显示
        self._was_visible_before_fullscreen = False
        self.ai_reply_ready.connect(self._on_ai_reply)
        self.import_parsed.connect(self._on_import_parsed)
        self.jobfair_ready.connect(self._on_jobfair)
        self.jobfair_auto_ready.connect(self._on_jobfair_auto)

        # --- v0.8.1: 久坐提醒 (只在"你确实一直在用电脑"时才催你休息) ---
        self.rest = RestWatcher(
            active_minutes=float(self.cfg.get("rest_reminder_minutes", 45) or 45),
            idle_sec=float(self.cfg.get("rest_reminder_idle_seconds", 60) or 60))
        if self.cfg.get("rest_reminder_enabled", True) and self.rest.supported:
            self.rest_timer = QTimer(self)
            self.rest_timer.timeout.connect(self._check_rest)
            self.rest_timer.start(60 * 1000)   # 每分钟看一眼


        # --- 第四梯队 (v0.5): AI 对话 + 离线语音 ---
        self.ai = AIChat(persona=self.config.get("persona", ""))
        # v0.8: 语音引擎由 ai_config.json 决定 (edge 神经语音 / sapi 系统语音 / auto)
        self.tts = PetTTS(self.cfg)
        # 语音默认关闭; ai_config.json 的 tts_enabled 可覆盖, 菜单「语音开关」随时切
        self.tts.on = bool(self.ai.cfg.get("tts_enabled", False))
        # 语音用独立播放器, 不和状态音效抢同一个声道;
        # on_speak 在工作线程被调用 -> 发信号切回主线程再碰 GUI
        self.tts_player = QMediaPlayer(self)
        # 语音排队 (v0.12): 流式朗读是一句一句丢进来的, 不排队就会互相掐断
        self._tts_queue: list[str] = []
        self.tts_output = QAudioOutput(self)
        self.tts_output.setVolume(0.9)
        self.tts_player.setAudioOutput(self.tts_output)
        self.speech_ready.connect(self._on_speech)
        # on_speak 在工作线程被调用 -> 发信号切回主线程再碰 GUI
        self.tts.on_speak = self.speech_ready.emit
        self.click_through = False  # v0.6: 鼠标穿透, 默认关

        # --- v0.7: 语音包 (sounds/) + 状态动作动画 (animation/) ---
        # sounds/ 下按状态命名: idle.mp3 click.mp3 eat.mp3 sleep.mp3 pet.mp3
        # 支持本地语音包(豆包/TTS生成)或音效(wav/mp3均可)
        self.snd_player = QMediaPlayer(self)
        self.snd_output = QAudioOutput(self)
        self.snd_output.setVolume(0.8)
        self.snd_player.setAudioOutput(self.snd_output)
        self.sounds: dict[str, str] = {}
        snd_dir = os.path.join(pet_dir, "sounds")
        if os.path.isdir(snd_dir):
            for fn in os.listdir(snd_dir):
                stem, ext = os.path.splitext(fn)
                if ext.lower() in (".mp3", ".wav", ".ogg"):
                    self.sounds[stem] = os.path.join(snd_dir, fn)

        # 资源包自检: 一张形象图都没有时明确报出来, 而不是显示一个空白窗口
        if not self.movies and not os.path.exists(
                os.path.join(pet_dir, "avatar", "idle.png")):
            QTimer.singleShot(800, lambda: self.say(
                "⚠ 资源包缺少形象图：请把 idle.png 放到 "
                f"{os.path.basename(os.path.normpath(pet_dir))}/avatar/ 目录"))

        # 启动时就要贴上形象图: 这一步曾误放在鼠标穿透的开关函数里,
        # 结果是启动后只有气泡、没有小人
        self.apply_state("idle")
        # 到这里 tts/bubble 都已就绪, 才做第一次养成结算 (启动即饿着也不会崩)
        self._status_tick()

        # --- 日程 (v0.12) ---
        # 每分钟看一眼有没有安排进入"提前量"窗口。定时器在 tts/bubble 之后才建,
        # 因为一进窗口就要 say(), 而那需要语音和气泡都就绪。
        self.sched_timer = QTimer(self)
        self.sched_timer.timeout.connect(self._check_schedule)
        self.sched_timer.start(60 * 1000)
        self._report_missed()      # 开机时把错过的安排汇总说一次
        # 开机顺手清一次过期日程（秋招季一天十几场，不清会一直涨）。
        # 设成 0 就不自动清，只留菜单里的手动清理
        keep_past = int(self.cfg.get("schedule_keep_past_days", 7) or 0)
        if keep_past > 0:
            schedule().cleanup(keep_past)

        # 天气: 定时看会不会下雨 (没配城市就什么都不做)
        self.weather_timer = QTimer(self)
        self.weather_timer.timeout.connect(self._check_weather)
        self.weather_timer.start(int(float(
            self.cfg.get("weather_check_minutes", 30) or 30) * 60 * 1000))
        QTimer.singleShot(30000, self._check_weather)     # 开机也看一眼

        # 秋招模式: 定时同步就业网的宣讲会 (默认关闭, jobfair_auto_hours=0)
        auto_hours = float(self.cfg.get("jobfair_auto_hours", 0) or 0)
        if auto_hours > 0:
            self.jobfair_timer = QTimer(self)
            self.jobfair_timer.timeout.connect(self.auto_sync_jobfair)
            self.jobfair_timer.start(int(auto_hours * 3600 * 1000))
            QTimer.singleShot(20000, self.auto_sync_jobfair)   # 开机也同步一次

        # --- 让大模型主动搭话 (v0.15) ---
        # **默认关闭** (ai_chatter_seconds=0): 它是持续烧 token 的功能, 而且
        # "时不时自己说一句"正是 pet-quiet-by-default 要避免的。想要就自己去配置里开。
        self._ai_chatter_count = 0
        self._ai_chatter_day = datetime.now().date()
        chat_sec = int(self.cfg.get("ai_chatter_seconds", 0) or 0)
        if chat_sec > 0 and self.ai.available:
            self.ai_chatter_timer = QTimer(self)
            self.ai_chatter_timer.timeout.connect(self._ai_small_talk)
            self.ai_chatter_timer.start(chat_sec * 1000)

    def _sync_audio_device(self):
        """把两路播放器挪到**当前**的系统默认输出设备上。

        QAudioOutput 是在**构造那一刻**绑定默认设备的, 之后不会自己跟着变。
        所以拔掉耳机(或插上耳机)之后, 播放器还指着那个已经不存在的设备 ——
        表现是"一声不响地不出声", 非得重启桌宠(重新构造)才恢复。
        每次出声前比一下、不一致就切过去, 就不用重启了。

        实测: 播放中 setDevice 会触发 deviceChanged 并打断当前播放, 但不会卡死,
        所以放在 setSource/play 之前做最省事。
        """
        try:
            default = QMediaDevices.defaultAudioOutput()
            if default.isNull():
                return                   # 一个输出设备都没有: 保持原样
            for out in (self.tts_output, self.snd_output):
                if bytes(out.device().id()) != bytes(default.id()):
                    out.setDevice(default)
        except Exception:
            pass                         # 查设备/切设备出问题不该阻断桌宠出声

    def play_sound(self, name: str):
        """播放语音包/音效: sounds/<name>.mp3 或 .wav, 不存在则静默跳过。

        ⚠ 千万别写成 stop() -> setSource() -> play() 的老套路:
        Qt6 的 FFmpeg 后端在"短时间内反复 stop/setSource"时会**死锁**
        （实测连点宠物 40 次左右就能复现, 主线程永久卡在 stop() 里, 界面直接冻结）。

        所以这里的写法是:
        - **同一个文件重播**: 不碰 setSource(也就不需要 stop), 直接回到开头重播
        - **换文件**: 只 setSource —— 它本身就会停掉正在播的, 显式 stop 是多余的
        """
        path = self.sounds.get(name)
        if not path:
            return
        self._sync_audio_device()
        try:
            cur = self.snd_player.source().toLocalFile()
            if os.path.normcase(os.path.abspath(cur)) != \
                    os.path.normcase(os.path.abspath(path)):
                self.snd_player.setSource(QUrl.fromLocalFile(path))
            self.snd_player.setPosition(0)
            self.snd_player.play()
        except Exception:
            pass  # 无声卡/解码失败不阻断桌宠

    def _on_speech(self, path: str, est_seconds: float):
        """在主线程处理一次说话 (由 speech_ready 信号送来)。

        path 为空字符串表示 sapi 直接发声(没有音频文件), 此时不播文件、只做口型。
        """
        if path:
            self._play_tts_audio(path)
        self._start_talk_animation(est_seconds)

    def _play_tts_audio(self, path: str):
        """播放合成好的语音文件。**正在说上一句就先排队** (v0.12)。

        原来直接 setSource 会立刻掐掉上一句 —— 单句对话看不出来, 但"想出一句
        说一句"的流式朗读会把每句都截断。排队后按顺序念完, 也是一般聊天该有的行为。

        同样避开 stop() 的churn (见 play_sound 里那条死锁说明):
        只在状态音效真的在播时才压掉它, 也不显式 stop 语音播放器。
        """
        if self.tts_player.playbackState() == \
                QMediaPlayer.PlaybackState.PlayingState or self._tts_queue:
            self._tts_queue.append(path)      # 上一句还在说, 排到后面
            return
        self._start_tts_audio(path)

    def _start_tts_audio(self, path: str):
        try:
            self._sync_audio_device()
            if self.snd_player.playbackState() == \
                    QMediaPlayer.PlaybackState.PlayingState:
                self.snd_player.stop()  # 说话时压掉状态音效, 免得两路打架
            self.tts_player.setSource(QUrl.fromLocalFile(path))
            self.tts_player.play()
        except Exception:
            pass  # 解码/声卡异常不阻断桌宠

    def _drain_tts_queue(self):
        """一句话说完了 -> 看看排队的下一句。"""
        if self._tts_queue:
            self._start_tts_audio(self._tts_queue.pop(0))

    # ---------- 说话动画 (v0.9): 说话时切 animation/talk.gif ----------
    def _talk_ok(self) -> bool:
        """talk 动画能不能用: 关掉了 / 没素材 / 尺寸与其它动画差太多都不行。

        尺寸那条很重要: talk.gif 若与 idle.gif 宽高比不同, 切过去会把窗口按
        新比例重算 —— 说话瞬间整个窗口跳一下、说完再跳回来, 正是"抽搐"那种观感。
        所以这里宁可不用口型, 也不让窗口跳。
        """
        if not self.cfg.get("talk_animation", True):
            return False
        if "talk" not in self.movies:
            return False
        talk_sz = self.movie_sizes.get("talk")
        base_sz = self.movie_sizes.get("idle") or self.movie_sizes.get("click")
        if talk_sz and base_sz and talk_sz.height() > 0 and base_sz.height() > 0:
            a_talk = talk_sz.width() / talk_sz.height()
            a_base = base_sz.width() / base_sz.height()
            if abs(a_talk - a_base) / a_base > 0.05:
                if not getattr(self, "_talk_size_warned", False):
                    self._talk_size_warned = True
                    QTimer.singleShot(1200, lambda: self.say(
                        f"小提示：talk.gif 尺寸是 {talk_sz.width()}×{talk_sz.height()}，"
                        f"和其它动画({base_sz.width()}×{base_sz.height()})不一致，"
                        "说话动画已跳过（保持一致才不会跳窗）"))
                return False
        return True

    def _start_talk_animation(self, est_seconds: float):
        """说话期间切到 talk 动画, 说完切回。

        结束判定用「序号令牌 + 计时器」主控, EndOfMedia 只用来提前收尾 ——
        因为语音是排队播放的, 新句子会 stop() 掉上一句, 上一句的 EndOfMedia
        永远不会触发, 只靠它会把口型永久卡在 talk 状态。
        """
        if not self._talk_ok():
            return
        if self.state != "talk":
            self._talk_prev_state = self.state
            self.apply_state("talk", sound=False, keep_size=True)
        self._talk_token = getattr(self, "_talk_token", 0) + 1
        token = self._talk_token

        if getattr(self, "_talk_timer", None) is None:
            self._talk_timer = QTimer(self)
            self._talk_timer.setSingleShot(True)
        try:
            self._talk_timer.timeout.disconnect()
        except TypeError:
            pass
        self._talk_timer.timeout.connect(lambda: self._end_talk(token))
        # 估算时长 + 余量; 太短会一闪而过, 所以给个下限
        self._talk_timer.start(int(max(0.9, est_seconds + 0.7) * 1000))

        try:
            self.tts_player.mediaStatusChanged.disconnect(self._on_tts_media_status)
        except TypeError:
            pass
        self.tts_player.mediaStatusChanged.connect(self._on_tts_media_status)

    def _on_tts_media_status(self, status):
        """音频播完就提前收口型 (只认 EndOfMedia, 中途状态变化忽略)。"""
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self._tts_queue:          # 排队的话接着念下一句, 口型别收
                self._drain_tts_queue()
                return
            self._end_talk(getattr(self, "_talk_token", 0))

    def _end_talk(self, token: int):
        if token != getattr(self, "_talk_token", 0):
            return                       # 已经开了新的一句, 别把新的关掉
        if self.state != "talk":
            return                       # 期间用户点了别处, 不抢回来
        prev = getattr(self, "_talk_prev_state", "idle")
        self.apply_state("idle" if prev == "talk" else prev,
                         sound=False, keep_size=True)

    def _toggle_tts(self, checked: bool):
        self.tts.on = checked
        if checked and not self.tts.engine_ready:
            self.say("语音引擎还没装好，先 pip install edge-tts 哦")
            return
        self.say("好呀，我可以说话啦~ 🔊" if checked else "好的，安静模式 🤫")

    def set_click_through(self, enabled: bool):
        """v0.6: 鼠标穿透 (Win32 WS_EX_TRANSPARENT, 参考社区可靠方案)。

        注意: 穿透开启后宠物自己的右键菜单也点不到 (点击会落到下层窗口),
        所以关闭入口必须在托盘菜单里 —— 提示文案要指对地方。
        """
        if enabled:
            if not win32_click_through(int(self.winId()), True):
                self.click_through = False
                self.say("当前系统暂不支持穿透模式哦")
                return
            self.click_through = True
            self.say("穿透模式开启，我现在摸不到啦~ "
                     "想关掉请右键托盘图标 → 👻 鼠标穿透")
        else:
            win32_click_through(int(self.winId()), False)
            self.click_through = False
            self.say("穿透已关闭，可以摸我了~")

        self.apply_state("idle", sound=False)

    def toggle_click_through(self):
        self.set_click_through(not self.click_through)

    # ---------- 第三梯队 (v0.4) ----------
    def _pomo_tick(self):
        if self.pomodoro.running:
            self._update_tooltip()

    def _update_tooltip(self):
        """tooltip 上同时显示番茄钟与陪伴时长 (两者共用一个 tooltip)。"""
        parts = []
        if self.pomodoro.running:
            parts.append(f"🍅 {self.pomodoro.phase} 剩余 {self.pomodoro.mmss()}")
        work = getattr(self, "work", None)
        if work is not None and self.cfg.get("work_log_enabled", True):
            parts.append(f"🪑 今天陪了你 {work.today_text()}")
            if work.in_session and work.current_session_minutes() >= 1:
                parts.append(f"本段 {format_minutes(work.current_session_minutes())}")
        self.setToolTip("　".join(parts))

    def _on_pomodoro_phase(self, phase: str):
        """番茄钟阶段结束: 专注完成→庆祝+好感奖励; 休息结束→开工提醒。"""
        if phase == "work":
            self.say("🍅 专注完成！休息一下，你真棒！")
            if self.status.add_favor(10):
                self.say(f"陪你完成番茄钟，好感度升到 Lv.{self.status.level}！🎉")
            self.status.mood = min(100, self.status.mood + 10)
            self.status.save()
        else:
            self.say("休息结束，开工啦！我会陪着你的~ 💪")

    def toggle_pomodoro(self):
        if self.pomodoro.running:
            self.pomodoro.stop()
            self.setToolTip("")
            self.say("番茄钟已暂停，随时回来继续哦")
        else:
            self.pomodoro.start_work()
            self.say("番茄钟开始！25 分钟专注，我会在这里陪你 🍅")

    def _random_action(self):
        """随机小动作 (v0.8.1: 只动不出声, 且不打扰睡觉/隐藏状态)。

        原来这里会随机说一句台词, 属于"定时说话"; 现在只做表情动作,
        让它像个活着的小家伙, 但不打断你。
        """
        if self.isHidden() or self.state != "idle" or self.bubble.isVisible():
            return
        self.apply_state("click", sound=False)   # 只动不出声, 免得定时吵人
        QTimer.singleShot(2000, lambda: self.apply_state("idle"))

    def _check_fullscreen(self):
        """全屏程序 (游戏/演示) 时自动隐藏，退出后恢复 (竞品"游戏模式")。

        只记「隐藏前是否可见」这一个状态: 用户自己用托盘隐藏的宠物,
        在全屏退出后不会被强行 show() 出来 (此前会覆盖用户意图)。
        """
        fs = is_fullscreen()
        if fs:
            if self.isVisible():
                self._was_visible_before_fullscreen = True
                self.hide()
        elif self._was_visible_before_fullscreen:
            self._was_visible_before_fullscreen = False
            self.show()
            self.raise_()

    # ---------- 养成 ----------
    def _status_tick(self):
        self.status.tick(1.0)
        self.status.save()
        self._work_tick()
        # 饿了会主动乞食, 但加了冷却 (默认 10 分钟一次), 不会每分钟都念
        nag_min = float(self.cfg.get("hungry_nag_minutes", 10) or 0)
        if (self.status.is_hungry and nag_min > 0
                and not self.bubble.isVisible()
                and time.time() - self._last_hungry_nag >= nag_min * 60):
            self._last_hungry_nag = time.time()
            self.say(random.choice([
                "咕噜咕噜…肚子饿了…",
                "有好吃的吗？求投喂！🥺",
                "再不喂我就要饿扁啦~",
            ]))

    # ---------- 工作陪伴 (v0.9) ----------
    def _work_tick(self):
        """累计陪伴时长。只算"宠物醒着 且 你确实在用电脑"的时间。

        离开电脑(倒水/开会)就不算 —— 否则"陪了你 8 小时"会虚高, 那就不真诚了。
        """
        now = time.time()
        elapsed = now - self._work_tick_at
        self._work_tick_at = now
        if elapsed <= 0 or not self.cfg.get("work_log_enabled", True):
            return
        elapsed = min(elapsed, 120)      # 定时器延迟/机器睡眠时不虚记
        if self.state == "sleep":        # 收工了就不算
            return
        idle = idle_seconds()
        away = float(self.cfg.get("rest_reminder_idle_seconds", 60) or 60)
        if idle is not None and idle > away:
            return                       # 人不在
        self.work.tick(elapsed)
        self.work.save()                 # 每分钟落一次盘, 被强杀也不丢当天时长

    # ---------- 久坐提醒 / 劝休息 (v0.8.1, v0.9 升级为可选择的仪式) ----------
    def _check_rest(self):
        """连续用电脑到点 -> 先出声劝, 再弹出带按钮的小窗由用户决定。

        这是"非互动也会说话"的两个例外之一(另一个是睡觉收工报告)。
        """
        if not self.rest.poll():
            return
        if self.state == "sleep":        # 已经收工了, 不用再劝
            return
        minutes = int(round(self.rest.last_active_minutes))
        self.say(self._rest_line(minutes))
        self._show_rest_prompt(minutes)

    def _rest_line(self, minutes: int) -> str:
        """话术随劝的次数升级: 先客气, 再认真, 然后摆出陪伴时长来劝。"""
        today = self.work.today_text()
        n = self.rest.try_count
        if n <= 1:
            return random.choice([
                f"你已经连续坐了 {minutes} 分钟啦，起来走两步吧~ 🚶",
                "坐太久对腰不好哦，站起来伸个懒腰！",
                "眼睛也要休息呀，看看窗外 20 秒~ 🌿",
            ])
        if n == 2:
            return random.choice([
                f"都陪你工作 {minutes} 分钟了，歇一会儿好不好~",
                "陪我一起休息一下吧，站起来动动肩膀",
            ])
        # 第 3 次起, 拿出"我陪了你多久"来劝 (用户原话的意思)
        return random.choice([
            f"我已经陪你工作 {today}了，你也需要休息呀，快放下手中的工作吧",
            f"今天都陪了你 {today}，够了够了，放下手里的活，我们一起休息 🌙",
        ])

    def _rest_prompt_text(self, minutes: int) -> str:
        if self.rest.try_count >= 3:
            return (f"我都陪你工作 {self.work.today_text()}了，\n"
                    "你也需要休息呀，放下手中的工作吧~")
        return f"已经连续坐了 {minutes} 分钟啦，\n要不要一起休息一下？"

    def _show_rest_prompt(self, minutes: int):
        """弹不抢焦点的小窗, 让用户自己决定要不要睡。"""
        old = getattr(self, "_rest_prompt", None)
        if old is not None:
            old.close_if_open()
        self._rest_prompt = RestPrompt(self, self._rest_prompt_text(minutes),
                                       self._on_rest_choice)
        self._rest_prompt.show_near_pet()

    def _on_rest_choice(self, sleep: bool, timed_out: bool = False):
        """用户在劝休息小窗里做了选择。"""
        if sleep:
            self.rest.reset_tries()
            self.apply_state("sleep")     # 统一入口 -> 自动结工并报告
            return
        snooze_min = float(self.cfg.get("rest_snooze_minutes", 15) or 15)
        self.rest.snooze(snooze_min)
        if not timed_out:
            # 主动选了"再干一会儿"才回一句; 超时是"没理我", 不该反过来吵你
            self.say(f"好~ 那{snooze_min:.0f}分钟后再提醒你，别太累哦")

    def _work_state_changed(self, prev_state: str, state: str):
        """睡觉 = 今天收工; 醒来 = 开始新的陪伴。"""
        work = getattr(self, "work", None)
        if work is None:
            return
        if state == "sleep" and prev_state != "sleep":
            work.end_session()
            if self.cfg.get("work_log_enabled", True):
                # 用 say_after_sound: 让打哈欠的音效先放完, 别被语音掐断
                self.say_after_sound(
                    f"今天陪了你 {work.today_text()}，收工啦～ 好好休息 🌙")
            self.rest.reset_tries()
        elif prev_state == "sleep" and state != "sleep":
            work.start_session()

    def say_after_sound(self, text: str, fallback_ms: int = 7000):
        """先冒泡, 等当前音效放完再朗读。

        睡觉时打哈欠的音效有 4 秒多, 直接 say() 会被 TTS 的 snd_player.stop()
        从中间掐断 —— 那样刚修好的哈欠声又白做了。
        """
        self.bubble.show_text(text)
        tts = getattr(self, "tts", None)
        if tts is None:
            return
        state = {"fired": False}

        def fire(status=None):
            # 只在音效真正播完时提前触发; 中途的状态变化一律忽略
            if status is not None and status != QMediaPlayer.MediaStatus.EndOfMedia:
                return
            if state["fired"]:
                return
            state["fired"] = True
            try:
                self.snd_player.mediaStatusChanged.disconnect(fire)
            except TypeError:
                pass
            tts.speak(text)

        self.snd_player.mediaStatusChanged.connect(fire)
        QTimer.singleShot(fallback_ms, fire)   # 兜底: 音效加载失败也不会永远不念

    def _mood_dialogue(self) -> str:
        """闲聊台词。

        饿/低落是功能性提示 (乞食、求安慰), 用内置文案;
        其余情况优先用资源包 pet.json 里的 dialogues —— 这样资源包作者
        写的台词才真的生效 (此前该字段从没被读取过)。
        """
        tier = self.status.mood_tier()
        if tier in ("happy", "calm") and self.dialogues:
            return random.choice(self.dialogues)
        pools = {
            "happy": ["今天心情超好！", "嘿嘿，最喜欢你啦~ ❤", "一起玩吗一起玩吗！"],
            "calm": ["今天也要加油鸭!", "陪着你呢~", "记得多喝水哦"],
            "sad": ["有点无聊…摸摸我好不好", "陪我玩一会儿嘛…"],
            "hungry": ["饿…饿…先给我吃的…", "肚子在抗议了！"],
        }
        return random.choice(pools.get(tier, pools["calm"]))

    # ---------- 显示 / 动画 ----------
    def _native_size(self, mv, state: str) -> QSize:
        """动画的**原始**帧尺寸。

        绝对不能用 mv.frameRect() 来算宽高比 —— 它有两个坑, 会把形象拉变形:
        1. 动画还没解码出第一帧时它返回 0x0 (启动瞬间就命中这个情况)
        2. setScaledSize 生效之后它返回的是**缩放后**的尺寸, 于是错误的宽高比
           会被反复算出来、自我固化, 再也纠正不回来
        所以这里用加载时通过 QImageReader 读到的原生尺寸。
        """
        sz = self.movie_sizes.get(state)
        if sz and sz.width() > 0 and sz.height() > 0:
            return sz
        # 兜底: 把缩放重置掉, frameRect 就恢复成原始尺寸
        mv.setScaledSize(QSize())
        fr = mv.frameRect()
        if fr.width() > 0 and fr.height() > 0:
            return fr
        return QSize(320, 426)

    def _apply_movie(self, mv, keep_size: bool = False, state: str = ""):
        """统一基准高度缩放 (参考 DyberPet):
        所有状态动画按同一「人物显示高度」渲染, 缩放只改变这个高度,
        保证不同状态之间人物大小一致, 放大缩小永远等比。

        keep_size=True 时保持当前窗口尺寸不变 —— 说话动画(talk)用它,
        免得说话瞬间窗口按 talk.gif 的宽高比重算而"跳一下"。
        """
        if keep_size:
            target = self.size()
        else:
            # DyberPet 方案: 以人物高度为锚点等比缩放,
            # 窗口尺寸 = 动画缩放后的实际尺寸 (窗口与画面永远一样大, 不可能裁剪)
            fr = self._native_size(mv, state)
            h = int(self.base_height * self.zoom)
            w = int(h * fr.width() / fr.height())
            target = QSize(w, h)
        mv.setScaledSize(target)
        self.resize(target)
        self.setMovie(mv)
        if mv.state() != QMovie.MovieState.Running:
            mv.start()

    def _placeholder_pixmap(self) -> QPixmap:
        """资源包缺形象图时的占位图。

        以前 QPixmap 为空会算出宽度 0 的窗口 —— 桌宠直接消失且没有任何提示,
        资源包作者完全不知道哪里错了。现在画个明确的"缺图"占位。
        """
        side = max(64, int(self.base_height * self.zoom))
        pm = QPixmap(side, side)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QColor("#c8a06a"))
        p.setBrush(QBrush(QColor(255, 240, 215, 200)))
        p.drawRoundedRect(2, 2, side - 4, side - 4, 12, 12)
        p.setPen(QColor("#8a6a3a"))
        font = p.font()
        font.setPointSize(max(8, side // 10))
        p.setFont(font)
        p.drawText(pm.rect(), int(Qt.AlignmentFlag.AlignCenter),
                   f"缺图\n{self.config.get('name', '?')}")
        p.end()
        return pm

    def _apply_pixmap(self, state: str, keep_size: bool = False):
        if state in self.movies:
            mv = self.movies[state]
            self._apply_movie(mv, keep_size=keep_size, state=state)
            return
        # GIF 不存在则回退静态 PNG，PNG 不存在则回退 idle
        path = os.path.join(self.pet_dir, "avatar", f"{state}.png")
        if not os.path.exists(path):
            path = os.path.join(self.pet_dir, "avatar", "idle.png")
        pm = QPixmap(path)
        self.setMovie(None)
        if pm.isNull():
            pm = self._placeholder_pixmap()
        h = max(1, int(self.base_height * self.zoom))
        w = max(1, int(h * pm.width() / max(1, pm.height())))
        target = QSize(w, h)
        self.resize(target)
        self.setPixmap(pm.scaled(
            target, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def apply_state(self, state: str, sound: bool = True, keep_size: bool = False):
        """切换状态。sound=False 用于"只是重画/非用户触发"的场合。

        自动音效只在**用户真的做了什么**时播; 缩放、穿透开关、随机小动作
        都会重新调用本函数, 那些情况必须静默, 否则会莫名其妙地出声。
        keep_size=True 保持窗口尺寸 (说话动画用, 见 _apply_movie)。
        """
        prev_state = self.state
        self.state = state
        # v0.7: 状态切换自动播放语音包 sounds/<state>.mp3/wav (idle 除外)
        if sound and state != "idle":
            self.play_sound(state)
        self._work_state_changed(prev_state, state)
        # 暂停其他状态的 GIF，省资源
        for s, mv in self.movies.items():
            if s != state and mv.state() != QMovie.MovieState.NotRunning:
                mv.setPaused(True)
        mv = self.movies.get(state)
        if mv is not None:
            mv.setPaused(False)
            # 两段式睡觉: 资源包里**同时有** sleep 和 sleep_loop 时, sleep 只播
            # 一遍(打哈欠/入睡), 然后交接给 sleep_loop 循环。
            #
            # 没有 sleep_loop 就说明 sleep.gif 本身就是循环段(单段式),
            # 这时千万别接管 —— 否则会播一遍就冻在最后一帧不动。
            if state == "sleep" and "sleep_loop" in self.movies:
                self._sleep_prev_frame = 0
                try:
                    mv.frameChanged.disconnect(self._hold_last_frame)
                except TypeError:
                    pass
                mv.frameChanged.connect(self._hold_last_frame)
        else:
            self._sleep_prev_frame = 0
        self._apply_pixmap(state, keep_size=keep_size)

    def _hold_last_frame(self, frame_no: int):
        """sleep 状态机: 过渡段(sleep.gif)播完一遍 -> 无缝切入睡着循环段。"""
        if self.state != "sleep":
            return
        mv = self.movies.get("sleep")
        if mv is None:
            return
        prev = getattr(self, "_sleep_prev_frame", 0)
        self._sleep_prev_frame = frame_no
        total = mv.frameCount()
        loop = self.movies.get("sleep_loop")
        wrapped = frame_no == 0 and prev > 1
        reached_end = total > 0 and frame_no >= total - 1
        if (wrapped or reached_end):
            if loop is not None:
                # 切到睡着循环段: 先彻底停掉过渡段(避免画面重叠), 从第0帧重启循环
                try:
                    mv.frameChanged.disconnect(self._hold_last_frame)
                except TypeError:
                    pass
                mv.stop()
                loop.jumpToFrame(0)
                loop.setPaused(False)
                # 入睡过渡段(打哈欠)已放完, 接上睡着后的呼吸声 (sounds/sleep_loop.*)
                self.play_sound("sleep_loop")
                # 关键: 切换到循环段时窗口尺寸完全不变,
                # 循环段直接按当前窗口尺寸渲染, 杜绝切换瞬间大小跳变
                target = self.size()
                loop.setScaledSize(target)
                self.setMovie(loop)
                if loop.state() != QMovie.MovieState.Running:
                    loop.start()
            else:
                mv.setPaused(True)      # 无循环段则停在最后一帧

    def _restore_geometry(self):
        """恢复上次的位置与缩放档位; 没有存档就落到右下角。

        位置和缩放分开存: 窗口尺寸是 base_height * zoom 算出来的,
        恢复 zoom 就能恢复大小, 不必再存一份宽度 (存了也没人读)。
        """
        geo = self.settings.value("geometry")
        moved = False
        if isinstance(geo, str):
            parts = geo.split(",")
            if len(parts) >= 2:
                try:
                    self.move(int(parts[0]), int(parts[1]))
                    moved = True
                except ValueError:
                    moved = False
        if not moved:
            self.move_to_bottom_right()

        z = self.settings.value("zoom")
        try:
            self.zoom = (max(ZOOM_MIN, min(ZOOM_MAX, float(z)))
                         if z is not None else 1.0)
        except (TypeError, ValueError):
            self.zoom = 1.0

    def save_geometry(self):
        """退出前调用: 位置 + 缩放档位写回 QSettings 并落盘。"""
        self.settings.setValue("geometry", f"{self.x()},{self.y()}")
        self.settings.setValue("zoom", self.zoom)
        self.settings.sync()

    def set_zoom(self, zoom: float):
        """托盘「大小」档位: 设定缩放并立即按当前状态重绘 (纯重绘, 不出声)。"""
        self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, float(zoom)))
        self.apply_state(self.state, sound=False)

    def move_to_bottom_right(self):
        screen = self.screen().availableGeometry()
        self.move(screen.right() - self.width() - 40,
                  screen.bottom() - self.height() - 40)

    def tick(self):
        self.float_phase = (self.float_phase + 1) % 40
        # idle 已有专属 GIF 时，GIF 自带浮动效果，关闭代码浮动避免叠加抖动
        if self.state == "idle" and "idle" not in self.movies:
            off = (self.float_phase // 20) * 4 - 2
            self.move(self.x(), self.y() + (off - getattr(self, "_last_off", 0)))
            self._last_off = off

    # ---------- 缩放 (滚轮 + 托盘档位, 参考 OpenDesktopPet 0.6x~1.8x) ----------



    def wheelEvent(self, e):
        # 滚轮缩放已移除 (会引发 resize 合成滚轮事件的反馈循环导致来回缩放)
        # 大小调整请使用托盘菜单「📏 大小」选择固定百分比
        e.ignore()

    # ---------- 互动 ----------
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            pos = e.globalPosition().toPoint()
            self.drag_pos = pos - self.frameGeometry().topLeft()
            self._press_pos = pos
            self._dragged = False
            self.apply_state("click")
            if self.status.petted():  # 抚摸攒好感, 升级时冒泡
                self.say(f"好感度升到 Lv.{self.status.level} 啦！🎉")
        elif e.button() == Qt.MouseButton.RightButton:
            self.show_menu(e.globalPosition().toPoint())

    def mouseMoveEvent(self, e):
        if self.drag_pos is None:
            return
        pos = e.globalPosition().toPoint()
        if not self._dragged:
            # 挪动超过几个像素才算"拖" —— 否则手稍微一抖就被当成拖动, 点按就失灵了
            if (pos - self._press_pos).manhattanLength() > 4:
                self._dragged = True
        self.move(pos - self.drag_pos)

    def mouseReleaseEvent(self, e):
        self.drag_pos = None
        if self._dragged:
            self._dragged = False
            self.apply_state("idle")     # 拖完松手 -> 回待机
            return
        # 只是**点了一下**: 让 click 动画自己播一会儿再回待机。
        # 以前这里直接 apply_state("idle") —— 一松手就切回待机, 点按等于什么都看不见,
        # 必须一直按着才看得到反应。现在点一下就行。
        hold = int(self.cfg.get("click_hold_ms", 2500) or 2500)
        QTimer.singleShot(hold, self._end_click)

    def _end_click(self):
        """点按后的收尾: 还在 click 状态才切回待机 (期间用户切了别的就不抢)。"""
        if self.state == "click" and self.drag_pos is None:
            self.apply_state("idle")

    def mouseDoubleClickEvent(self, e):
        self.say(self._mood_dialogue())

    def feed(self):
        """喂食: 数值回复 + 升级检测 + 反馈气泡 + 吃东西动画/音效 (v0.7)。"""
        leveled = self.status.feed()
        self.status.save()
        self.apply_state("eat")          # 吃东西动画+音效 (animation/eat.* + sounds/eat.*)
        QTimer.singleShot(2500, lambda: self.apply_state("idle"))
        if leveled:
            self.say(f"谢谢你投喂！好感度升到 Lv.{self.status.level} 啦！🎉")
        elif self.status.hunger >= 95:
            self.say("吃不下啦，好撑~")
        else:
            self.say("哇，好好吃！谢谢投喂~ 😋")

    def show_menu(self, pos):
        menu = QMenu(self)
        h = self.status
        info = QAction(f"好感 Lv.{h.level} | 饱食 {int(h.hunger)} | 心情 {int(h.mood)}", menu)
        info.setEnabled(False)
        menu.addAction(info)
        if self.cfg.get("work_log_enabled", True):
            w = self.work
            companion = QAction(
                f"🪑 今天陪了你 {w.today_text()}（第 {w.days_together()} 天）", menu)
            companion.setEnabled(False)
            menu.addAction(companion)
        menu.addSeparator()
        profile_act = QAction("📇 档案", menu)
        profile_act.triggered.connect(self.open_profile)
        menu.addAction(profile_act)

        # 日程相关全收进子菜单 —— 主菜单已经 20 多项了, 新功能会被淹掉,
        # 而且这些本来就是一件事的不同侧面
        talk_menu = menu.addMenu("📅 日程 / 待办")
        talk_menu.addAction("看日程", self.open_schedule)
        talk_menu.addAction("看待办", self.open_tasks)
        talk_menu.addAction("➕ 添加日程…", self.add_event)
        talk_menu.addAction("📥 导入日程文件…", self.pick_import_file)
        talk_menu.addSeparator()
        talk_menu.addAction("🎓 抓宣讲会（秋招）", self.fetch_jobfair_async)
        auto_act = QAction("⏱ 宣讲会自动同步", talk_menu)
        auto_act.setCheckable(True)
        auto_act.setChecked(float(self.cfg.get("jobfair_auto_hours", 0) or 0) > 0)
        auto_act.triggered.connect(self._toggle_jobfair_auto)
        talk_menu.addAction(auto_act)
        talk_menu.addAction("⚙ 秋招设置（换学校 / 改网址）",
                            self.open_jobfair_settings)
        talk_menu.addSeparator()
        # 清理: 秋招季一天十几场, 不清的话 schedule.json 会一直涨。
        # 条数直接写在菜单上 —— 用户一眼知道"要不要清"
        keep_days = int(self.cfg.get("schedule_keep_past_days", 7) or 7)
        stale = schedule().stale_count(keep_days)
        clean_act = QAction(f"🧹 清理过期日程（{stale} 条）", talk_menu)
        clean_act.setEnabled(stale > 0)
        clean_act.triggered.connect(self.clean_schedule)
        talk_menu.addAction(clean_act)
        mem_act = QAction("🧠 它记住了什么（打开记忆文件）", talk_menu)
        mem_act.triggered.connect(self.open_memory_file)
        talk_menu.addAction(mem_act)
        talk_menu.addSeparator()
        wx_act = QAction("☔ 下雨提醒（收衣服）", talk_menu)
        wx_act.setCheckable(True)
        wx_act.setChecked(bool(str(self.cfg.get("weather_city") or "").strip()))
        wx_act.triggered.connect(self._toggle_weather)
        talk_menu.addAction(wx_act)
        talk_menu.addAction("⚙ 天气设置（在哪个城市）", self.open_weather_settings)

        feed_act = QAction("🍖 投喂", menu)
        feed_act.triggered.connect(self.feed)
        menu.addAction(feed_act)
        pomo_act = QAction("🍅 番茄钟 (开始/停止)", menu)
        pomo_act.triggered.connect(self.toggle_pomodoro)
        menu.addAction(pomo_act)
        # 不管配没配 key 都显示: 没配的时候这个窗口就是填 key 的入口
        chat_act = QAction("💬 和它聊天 (AI)", menu)
        chat_act.triggered.connect(self.open_chat)
        menu.addAction(chat_act)
        tts_act = QAction("🔊 语音开关", menu)
        tts_act.setCheckable(True)
        tts_act.setChecked(self.tts.on)
        tts_act.triggered.connect(self._toggle_tts)
        menu.addAction(tts_act)
        # 让用户看得见现在用的是什么声音 (换声音改 ai_config.json 的 tts_voice)
        voice_info = QAction("     " + self.tts.describe(), menu)
        voice_info.setEnabled(False)
        menu.addAction(voice_info)
        ct_act = QAction("👻 鼠标穿透", menu)
        ct_act.setCheckable(True)
        ct_act.setChecked(self.click_through)
        ct_act.triggered.connect(lambda: self.toggle_click_through())
        menu.addAction(ct_act)
        for act_text, state in (("睡觉", "sleep"), ("醒来", "idle")):
            a = QAction(act_text, menu)
            a.triggered.connect(lambda _, s=state: self.apply_state(s))
            menu.addAction(a)
        menu.addSeparator()
        if sys.platform == "win32":
            auto = QAction("开机自启动", menu)
            auto.setCheckable(True)
            auto.setChecked(is_auto_start())
            # 自启动要把所有宠物一起拉起来, 所以传全量目录而不是只有自己
            auto.triggered.connect(
                lambda checked: set_auto_start(checked, self.all_pet_dirs))
            menu.addAction(auto)
        quit_act = QAction("退出", menu)
        quit_act.triggered.connect(QApplication.quit)
        menu.addAction(quit_act)
        menu.exec(pos)

    def random_chat(self):
        if self.state == "idle" and not self.bubble.isVisible():
            self.say(self._mood_dialogue())

    def say(self, text: str):
        self.bubble.show_text(text)
        # 初始化早期(self.tts 还没建好)也允许冒泡, 只是不朗读 —— 不让它崩
        tts = getattr(self, "tts", None)
        if tts is not None:
            tts.speak(text)

    # ---------- AI 对话 (v0.5) ----------
    def open_chat(self):
        """右键「和它聊天」: 打开对话窗口 (v0.12)。

        以前是弹个输入框一问一答, 问完就没了 —— 看不到上下文, 也没法连着聊。
        现在即使没配 key 也能打开 (窗口里有「⚙ 设置」可以填), 否则用户永远
        找不到填 key 的地方, 只能去手改 json。
        """
        win = getattr(self, "_chat_window", None)
        if win is not None:
            win.show()
            win.raise_()
            win.activateWindow()
            return
        self._chat_window = ChatWindow(self)
        self._chat_window.show()

    def _on_ai_reply(self, reply: str):
        """在主线程处理 AI 回复 (由 ai_reply_ready 信号送来)。"""
        self.say(reply)
        # v0.6: AI 驱动动作 (参考 DesktopFriends) — 回复命中关键词触发动作
        action = match_action(reply, self.config.get("ai_action_map"))
        if action and action != self.state:
            self.apply_state(action)

    def open_profile(self):
        """右键「档案」: 弹出人物档案卡 (v0.11)。

        只读展示 —— 不碰 status/work 的任何数值, 也不触发说话 (见 pet-quiet-by-default)。
        再点一次菜单就收起, 和弹出互为开关; 卡片自己点一下也能关。
        """
        old = getattr(self, "_profile_card", None)
        if old is not None:
            old.close_card()
            return
        work = getattr(self, "work", None)      # 没填相识日时拿它兜底算陪伴天数
        profile = PetProfile(self.pet_dir, self.config,
                             first_day=work.first_day if work else "")
        self._profile_card = ProfileCard(self, profile)
        self._profile_card.show_near_pet()

    def schedule_ref(self):
        """给 AI 工具箱用的日程对象 (全进程共用那一份)。"""
        return schedule()

    def open_schedule(self):
        """右键「日程」: 弹出日程卡。和档案卡一样是只读展示, 点一下收起。"""
        old = getattr(self, "_schedule_card", None)
        if old is not None:
            old.close_card()
            return
        self._schedule_card = ScheduleCard(self, schedule())
        self._schedule_card.show_near_pet()

    def import_schedule_file(self, path: str):
        """把一个 Excel/Word/文本文件读成日程 (v0.17)。

        两条路: 规整表格**规则直接解析**(离线、零成本、毫秒级); 规则啃不动的
        (自然语言、Word 正文)交给大模型。**都先弹预览让你过目**, 确认了才写。
        """
        try:
            result = pet_import.read_file(path)
        except ValueError as e:
            self.say(f"这个文件我读不了：{e}")
            return
        if result.empty:
            self.say("这个文件里没读到内容，是不是空的？")
            return
        events = pet_import.parse_by_rules(result.text)
        if events:
            if result.note:
                self.say(f"读到了 {len(events)} 条安排（{result.note}）")
            self._preview_import(events, "按表格解析")
            return
        # 规则啃不动 -> 交给大模型
        if not self.ai.available:
            self.say("这份内容不是规整的表格，得靠大模型来读 —— "
                     "先右键「💬 和它聊天」→「⚙ 设置」填个 key 吧")
            return
        self.say("这份写得不规整，我慢慢看…")
        # 回调是 (事件, 错误, 回复全文) 三个参数, 信号只带前两个
        pet_import.collect_by_llm(
            result.text, self.ai, self,
            on_done=lambda events, err, reply: self.import_parsed.emit(events, err))

    def open_jobfair_settings(self):
        """右键「⚙ 秋招设置」: 换学校、改网址、调提前天数和自动同步。"""
        dlg = JobFairSettingsDialog(None, self.cfg)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        values = dlg.values()
        if not values["jobfair_url"]:
            values["jobfair_url"] = pet_jobfair.DEFAULT_URL
        save_ai_config(values)
        self.cfg = load_ai_config()
        # 自动同步的间隔可能变了, 定时器要重建
        timer = getattr(self, "jobfair_timer", None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
            self.jobfair_timer = None
        hours = float(self.cfg.get("jobfair_auto_hours", 0) or 0)
        if hours > 0:
            self.jobfair_timer = QTimer(self)
            self.jobfair_timer.timeout.connect(self.auto_sync_jobfair)
            self.jobfair_timer.start(int(hours * 3600 * 1000))
        self.say("秋招设置存好了")

    def clean_schedule(self):
        """右键「🧹 清理过期日程」: 清掉早就过去的一次性日程。

        重复日程一条不动（删了等于把整条规则也删了），最近过去的也留着 ——
        用户可能想回看"上周去了哪些宣讲会"。
        """
        keep_days = int(self.cfg.get("schedule_keep_past_days", 7) or 7)
        removed = schedule().cleanup(keep_days)
        if removed:
            self.say(f"清掉了 {removed} 条过期日程（{keep_days} 天以内的还留着）")
        else:
            self.say("没有需要清理的日程")

    def open_memory_file(self):
        """打开记忆文件让用户自己看/改/删 —— 比"提供一个删除按钮"更实在。"""
        path = pet_memory.default_path()
        if not os.path.exists(path):
            self.say("我还没记住什么呢")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _weather_watch(self):
        """天气监视器（每只宠物一份，状态存在用户数据目录）。

        注意用 `is None` 判断而不是 `hasattr` —— 属性存在但被置空时，
        `hasattr` 会返回 True 从而把 None 交出去（测试里设成 None 清缓存时踩到）。
        """
        watch = getattr(self, "_wx_watch", None)
        if watch is None:
            watch = pet_weather.WeatherWatch(pet_weather.default_state_path())
            self._wx_watch = watch
        return watch

    def _check_weather(self):
        """看会不会下雨。**只在"快下雨了"时说一句** —— 不做每日天气播报。

        这就是 pet-quiet-by-default 里"有事要办"那条边界的又一个实例:
        下雨要收衣服是"事", "今天 25 度晴"不是。
        """
        city = str(self.cfg.get("weather_city") or "").strip()
        if not city or not self.isVisible() or self.state == "sleep":
            return
        line = self._weather_watch().check(
            city,
            within_hours=int(self.cfg.get("weather_within_hours", 2) or 2),
            min_probability=int(self.cfg.get("weather_min_probability", 60) or 60))
        if line:
            self.say(line)

    def _toggle_weather(self, checked: bool):
        """勾选「☔ 下雨提醒」—— 勾上得先知道你在哪个城市。"""
        if not checked:
            save_ai_config({"weather_city": ""})
            self.cfg = load_ai_config()
            self.say("好，不提醒天气了")
            return
        if not str(self.cfg.get("weather_city") or "").strip():
            # 没配城市就先弹设置窗（不然勾了也没用，还得回头找）
            dlg = WeatherSettingsDialog(None, self.cfg)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                self.cfg = load_ai_config()
                return
            self._apply_weather_settings(dlg.values())
        self.say(f"好，{self.cfg.get('weather_city')}要下雨的时候我提醒你收衣服")

    def open_weather_settings(self):
        dlg = WeatherSettingsDialog(None, self.cfg)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._apply_weather_settings(dlg.values())

    def _apply_weather_settings(self, values: dict):
        save_ai_config(values)
        self.cfg = load_ai_config()
        watch = self._weather_watch()
        watch.lat = watch.lon = None        # 城市可能换了, 坐标缓存作废
        watch.save()
        city = self.cfg.get("weather_city")
        if city:
            self.say(f"好，以后看 {city} 的天气")
        else:
            self.say("天气提醒关掉了")

    def _toggle_jobfair_auto(self, checked: bool):
        """勾选「⏱ 宣讲会自动同步」—— 勾上就每 6 小时自动抓一次。

        以前这只是配置里一个数字（`jobfair_auto_hours`），没界面 —— 用户想开自动
        抓取却找不到地方，只能去手改 json。
        """
        hours = 6 if checked else 0
        save_ai_config({"jobfair_auto_hours": hours})
        self.cfg = load_ai_config()          # 让菜单勾选状态立刻跟上
        timer = getattr(self, "jobfair_timer", None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
            self.jobfair_timer = None
        if hours > 0:
            self.jobfair_timer = QTimer(self)
            self.jobfair_timer.timeout.connect(self.auto_sync_jobfair)
            self.jobfair_timer.start(int(hours * 3600 * 1000))
            self.say(f"好，我每 {hours} 小时去就业网看一次，有新的就记进日程")
        else:
            self.say("好，不自动同步了 —— 想看就点「🎓 抓宣讲会（秋招）」")

    def jobfair_url(self) -> str:
        return str(self.cfg.get("jobfair_url") or pet_jobfair.DEFAULT_URL)

    def fetch_jobfair_async(self):
        """右键「🎓 抓宣讲会」: 去就业网捞一遍（v0.19）。

        两条路：先试**内置规则**（对这套 CMS 又快又免费）；认不出来就把页面正文
        交给**大模型**抽 —— 这样换一所学校、换一套系统也能用。抓取要联网还可能重试，
        所以放后台线程，卡住 UI 的话宠物会僵在那儿。
        """
        url = self.jobfair_url()
        if not url:
            self.say("还没填就业网地址 —— 右键「📅 日程 / 待办」→「⚙ 秋招设置」"
                     "把你们学校就业网的宣讲会列表页填进去")
            return
        self.say("我去就业网看看最近有什么宣讲会…")
        days = int(self.cfg.get("jobfair_days_ahead", 7) or 7)
        want_llm = self.ai.available

        def worker():
            try:
                items, err = pet_jobfair.fetch_teachins(url, days_ahead=days)
            except Exception as e:                       # noqa: BLE001
                items, err = [], f"{e.__class__.__name__}: {e}"
            if items:
                self.jobfair_ready.emit(items, "")
                return
            # 内置规则没认出来 -> 看看这页到底是日程页还是别的什么
            html = pet_jobfair.fetch_page(url)
            if not html:
                self.jobfair_ready.emit([], err or "这个地址打不开")
                return
            text = pet_jobfair.html_to_text(html)
            if not pet_jobfair.looks_like_jobfair(text):
                self.jobfair_ready.emit(
                    [], "这个页面看起来不是宣讲会/招聘会页面，检查一下网址？")
                return
            if not want_llm:
                self.jobfair_ready.emit([], "__need_llm__")
                return
            # 交给大模型抽 —— 和导入文件共用同一条链路（同一个 add_event schema）
            def done(evs, e, _reply):
                today = datetime.now().date().isoformat()
                # **日期过滤由代码做, 不交给模型判断** —— 提示词里已经让它"别自己筛",
                # 但模型有时还是会自作主张漏掉几条 (实测同一页面两次调用: 15 条 / 0 条)。
                # 代码筛是确定的, 模型筛是概率的, 而这里漏一条就是少一个机会
                evs = [ev for ev in evs
                       if str(ev.get("date") or "") >= today]
                for ev in evs:
                    ev.setdefault("remind_before_minutes", 30)   # 宣讲会要提前占座
                self.jobfair_ready.emit(evs, e)

            pet_import.collect_by_llm(text, self.ai, self, on_done=done)

        threading.Thread(target=worker, daemon=True).start()

    def _on_jobfair(self, items: list, err: str):
        """抓取回来了（在主线程）。

        **一个都不筛** —— 只按"日期在将来"过滤（在 fetch_teachins 里做了）。
        秋招漏一场是少一个机会，多看几场不相关的只是浪费几秒，代价完全不对称。
        """
        if err == "__need_llm__":
            self.say("这个就业网的格式内置规则认不出来，得靠大模型读 —— "
                     "先去「💬 和它聊天」→「⚙ 设置」填个 key，或者把页面另存成文件拖给我")
            return
        if err and not items:
            self.say(f"没抓到：{err}")
            return
        if not items:
            self.say("就业网上没查到最近的宣讲会")
            return
        self.say(pet_jobfair.summarize(items))
        self._preview_import(pet_jobfair.to_events(items), "就业网宣讲会")

    def auto_sync_jobfair(self):
        """定时同步（默认关闭）：抓到新的**直接加进日程**，不弹窗打扰。

        这是唯一一处"未经确认就写日程"的地方，所以三条限制卡死：
        只加未来 N 天内的、只加就业网上的公开信息、开关默认关（用户主动开才有）。
        """
        url = str(self.cfg.get("jobfair_url") or pet_jobfair.DEFAULT_URL)
        days = int(self.cfg.get("jobfair_days_ahead", 7) or 7)

        def worker():
            try:
                items, _ = pet_jobfair.fetch_teachins(url, days_ahead=days)
            except Exception:                            # noqa: BLE001
                return
            self.jobfair_auto_ready.emit(items)

        threading.Thread(target=worker, daemon=True).start()

    def _on_jobfair_auto(self, items: list):
        """定时同步的结果：安静地加，只在真加了东西时说一句。"""
        sched = schedule()
        added = 0
        for ev in pet_jobfair.to_events(items):
            if sched.has_similar(ev["title"], ev["date"], ev["time"]):
                continue
            sched.add(ev["title"], ev["date"], ev["time"],
                      repeat="none", note=ev["note"],
                      url=ev.get("url") or "",
                      remind_before=ev.get("remind_before_minutes")
                      or pet_schedule.DEFAULT_REMIND_BEFORE)
            added += 1
        if added:
            self.say(f"就业网上又更新了 {added} 场宣讲会，都加进日程了")

    def _preview_import(self, events: list, source: str):
        """先给清单过目。已有的条目默认不勾 —— 同一个文件导两次不该变成两份。"""
        sched = schedule()
        existing = {(e.title, e.date, e.time) for e in sched.events}
        dlg = ImportPreviewDialog(None, events, source, existing)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self.say("好，那就不加")
            return
        picked = dlg.selected()
        added = updated = 0
        for ev in picked:
            when = pet_schedule.parse_when(ev["date"], ev.get("time") or "")
            old = next((e for e in sched.events
                        if e.title == ev["title"] and e.start == when), None)
            if old is not None:
                # 已经有的不重复加, 但**要把新拿到的信息补上去** ——
                # 尤其是详情页链接: 早先版本抓的日程没存链接, 只靠"跳过已存在"
                # 的话它们永远补不上, 用户得先删再导
                filled = False
                if ev.get("url") and not old.url:
                    old.url = ev["url"]
                    filled = True
                if ev.get("note") and not old.note:
                    old.note = ev["note"]
                    filled = True
                if filled:
                    sched.save()
                    updated += 1
                continue
            sched.add(ev["title"], ev["date"], ev.get("time") or "",
                      repeat=ev.get("repeat") or "none",
                      note=ev.get("note") or "",
                      url=ev.get("url") or "",
                      # 宣讲会要提前到场占座, 自带更早的提醒量; 文件导入没有这项就用默认
                      remind_before=ev.get("remind_before_minutes")
                      or pet_schedule.DEFAULT_REMIND_BEFORE)
            added += 1
        if updated:
            bits = []
            if added:
                bits.append(f"加好了 {added} 条日程")
            bits.append(f"另外给 {updated} 条补上了详情页链接")
            self.say("，".join(bits))
        elif added:
            self.say(f"加好了 {added} 条日程")
        else:
            self.say("这些之前都已经在日程里了")

    def _on_import_parsed(self, events: list, err: str):
        """大模型解析回来了 (在主线程)。"""
        if err:
            self.say(f"读这份文件时出错了：{err}")
            return
        if not events:
            self.say("我看了一遍，没找出明确的安排 —— 可能这份文件里没有日程？")
            return
        self._preview_import(events, "大模型解析")

    def dragEnterEvent(self, e):
        """把文件拖到宠物身上 —— 用户原话就是"直接丢给他"。"""
        if self._dropped_docs(e.mimeData()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        files = self._dropped_docs(e.mimeData())
        if files:
            e.acceptProposedAction()
            self.import_schedule_file(files[0])

    @staticmethod
    def _dropped_docs(mime) -> list[str]:
        if not mime.hasUrls():
            return []
        exts = (".xlsx", ".xlsm", ".xls", ".docx", ".doc", ".csv", ".txt", ".md")
        out = []
        for url in mime.urls():
            path = url.toLocalFile()
            if path and path.lower().endswith(exts):
                out.append(path)
        return out

    def pick_import_file(self):
        """右键「导入日程文件…」: 选个 Excel/Word 丢进来。"""
        path, _ = QFileDialog.getOpenFileName(
            None, "选一份日程表或计划书",
            os.path.expanduser("~"),
            "表格/文档 (*.xlsx *.xls *.docx *.doc *.csv *.txt *.md)")
        if path:
            self.import_schedule_file(path)

    def open_tasks(self):
        """右键「待办」: 弹出待办卡 (和别的卡片一样, 点一下收起)。"""
        old = getattr(self, "_task_card", None)
        if old is not None:
            old.close_card()
            return
        self._task_card = TaskCard(self, tasks())
        self._task_card.show_near_pet()

    def add_event(self):
        """右键「添加日程」: 填个小表单, 加完顺手把日程卡显示出来。"""
        dlg = AddEventDialog(None)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        ev = schedule().add(**dlg.values())
        if ev is None:
            self.say("这个日期我看不懂，没记上 😥")
            return
        when = ev.next_occurrence(datetime.now())
        when_text = pet_schedule.format_when(when) if when else ev.date
        self.say(f"记下了：{when_text} {ev.title}")
        old = getattr(self, "_schedule_card", None)
        if old is not None:                 # 日程卡开着就刷新一下内容
            old.close_card()
            self.open_schedule()

    def _check_schedule(self):
        """每分钟看一眼: 有安排进入"提前量"窗口就开口。

        这是**唯一**允许日程主动说话的时机 (见 pet-quiet-by-default): 不播报
        "今天有几件事", 只在该动身之前提醒那一次。
        """
        now = datetime.now()
        sched = schedule()
        # 按时刻分组: 同一时刻的几场并成一句话说, 不然会连说好几句
        for when, events in pet_schedule.group_by_time(sched.due(now)):
            for ev in events:
                sched.mark_fired(ev, when)
            self.say(schedule_reminder_text(events, when, now))
        # 待办: 逾期或今天到期的说一次 —— 这就是"有事要办"那条开口事由,
        # 不是定时闲聊 (见 pet-quiet-by-default)。同一条一天只说一次。
        store = tasks()
        for task in store.due_checks(now.date()):
            store.mark_fired(task, now.date())
            self.say(task_due_line(task))

    def _ai_small_talk(self):
        """主动说一句 (默认关闭)。开着也要守两条: 只在你空闲时 + 一天有上限。"""
        if not self.isVisible() or self.state == "sleep":
            return                     # 全屏隐藏中/已睡着 -> 不打扰
        today = datetime.now().date()
        if self._ai_chatter_day != today:
            self._ai_chatter_day = today
            self._ai_chatter_count = 0
        limit = int(self.cfg.get("ai_chatter_daily_limit", 20) or 20)
        if self._ai_chatter_count >= limit:
            return
        if self.bubble.isVisible():
            return                     # 正在说话, 别叠着说
        self._ai_chatter_count += 1
        self.ai.small_talk(self._small_talk_context(),
                           on_reply=self.ai_reply_ready.emit)

    def _small_talk_context(self) -> str:
        """给模型的"现在什么情况"—— 没有它, 主动搭话只能是干巴巴的废话。"""
        now = datetime.now()
        bits = [f"现在是 {now:%H:%M}（{'凌晨' if now.hour < 6 else '上午' if now.hour < 12 else '下午' if now.hour < 18 else '晚上'}）"]
        work = getattr(self, "work", None)
        if work is not None and work.today_minutes >= 1:
            bits.append(f"用户今天已经连续忙了 {work.today_text()}")
        nxt = schedule().next_event(now)
        if nxt is not None:
            ev, when, minutes = nxt
            if minutes <= 180:
                bits.append(f"下一个安排是「{ev.title}」，{minutes} 分钟后开始")
        status = getattr(self, "status", None)
        if status is not None:
            bits.append(f"你自己的饱食度 {int(status.hunger)}、心情 {int(status.mood)}")
        return "\n".join(bits)

    def _report_missed(self):
        """开机时把关机期间错过的安排**汇总成一条**说一次 (不逐个重放, 防轰炸)。"""
        sched = schedule()
        missed = sched.missed_since_last_seen()
        if not missed:
            return
        for ev, when in missed:             # 记成已提醒过, 免得反复播报
            sched.mark_fired(ev, when)
        # 等宠物把开场那几句话说完再说, 免得撞在一起
        QTimer.singleShot(6000, lambda: self.say(missed_summary_text(missed)))


class Bubble(QLabel):
    def __init__(self, pet: PetWindow):
        super().__init__()
        self.pet = pet
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(
            "background: rgba(255,255,255,235); color:#333; border-radius:10px;"
            "padding:8px 12px; font-size:13px;"
        )
        self.setWordWrap(True)
        self.setFixedWidth(180)
        self.full_text = ""
        self.pos_i = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.type_step)
        self.hide_timer = QTimer(self)
        self.hide_timer.timeout.connect(self.hide)
        self.hide_timer.setSingleShot(True)

    def show_text(self, text: str):
        self.full_text = text
        self.pos_i = 0
        self.setText("")
        self.adjustSize()
        self._follow()
        self.show()
        self.raise_()
        self.timer.start(40)

    def type_step(self):
        self.pos_i += 1
        self.setText(self.full_text[: self.pos_i])
        self.adjustSize()
        self._follow()
        if self.pos_i >= len(self.full_text):
            self.timer.stop()
            self.hide_timer.start(3000)

    def _follow(self):
        g = self.pet.frameGeometry()
        self.move(g.center().x() - self.width() // 2, g.top() - self.height() - 8)


class RestPrompt(QFrame):
    """劝休息的小窗: 一句话 + 两个按钮, 由用户决定要不要睡 (v0.9)。

    两条不可妥协的设计:
    - WA_ShowWithoutActivating: 弹出时**不夺键盘焦点**, 你正在打的字不会被打断
      (这是它和 QMessageBox 的本质区别 —— 劝告不该变成打断)
    - 超时自动消失 = 当作"再干一会儿", 不选也是一种回答, 不会吊在那里
    """

    def __init__(self, pet: "PetWindow", text: str, on_choice):
        super().__init__(None)
        self.pet = pet
        self.on_choice = on_choice
        self._answered = False
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool          # 不进任务栏
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # 关键: 出现时不激活窗口 -> 不抢键盘焦点
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet(
            "QFrame{background: rgba(255,252,245,245); border-radius:12px;}"
            "QLabel{color:#333; font-size:13px;}"
            "QPushButton{border:none; border-radius:8px; padding:6px 10px;"
            "font-size:12px; background:#eee; color:#333;}"
            "QPushButton:hover{background:#e0e0e0;}"
            "QPushButton#sleep{background:#ffd9a0; font-weight:bold;}"
            "QPushButton#sleep:hover{background:#ffc978;}"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 10)
        lay.setSpacing(10)
        self.label = QLabel(text, self)
        self.label.setWordWrap(True)
        self.label.setFixedWidth(196)
        lay.addWidget(self.label)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.sleep_btn = QPushButton("好，一起睡 😴", self)
        self.sleep_btn.setObjectName("sleep")
        self.sleep_btn.clicked.connect(lambda: self._answer(True))
        later = QPushButton("再干一会儿 💪", self)
        later.clicked.connect(lambda: self._answer(False))
        row.addWidget(self.sleep_btn)
        row.addWidget(later)
        lay.addLayout(row)

        # 没人理它就自己消失 (算"再干一会儿"), 默认 40 秒
        secs = int(pet.cfg.get("rest_prompt_seconds", 40) or 40)
        self.timeout = QTimer(self)
        self.timeout.setSingleShot(True)
        self.timeout.timeout.connect(lambda: self._answer(False, timed_out=True))
        self.timeout.start(secs * 1000)
        self.adjustSize()

    def show_near_pet(self):
        self.adjustSize()
        g = self.pet.frameGeometry()
        self.move(g.center().x() - self.width() // 2, g.top() - self.height() - 10)
        self.show()
        self.raise_()      # raise_ 只是置顶, 不激活 -> 仍不抢焦点

    def _answer(self, sleep: bool, timed_out: bool = False):
        if self._answered:      # 按钮和超时可能抢跑, 只认第一个
            return
        self._answered = True
        self.timeout.stop()
        self.hide()
        self.on_choice(sleep, timed_out)

    def close_if_open(self):
        """宠物隐藏/退出时一并收掉。"""
        self._answered = True
        self.timeout.stop()
        self.hide()
        self.deleteLater()


class IconMark(QWidget):
    """档案行首的单色小图标 —— 手绘, 不用图片文件, 也不依赖 emoji 字体。

    为什么不用彩色 emoji: 那是有立体光影的 3D 图形, 压在纸面上会跳戏。
    为什么形状都这么简: **实测过 13~15px 下的辨识度** —— 蛋糕、礼物、蜡烛这些
    细节多的画出来全糊成一团, 只有实心剪影(星/心)和"一圈加两根针"(时钟)认得出来。
    """

    def __init__(self, kind: str, color: str, size: int = 14):
        super().__init__(None)
        self.kind, self.color = kind, color
        # 叫 icon_size 而不是 size: QWidget 已经有 size() 方法了, 同名会把方法盖掉
        self.icon_size = size
        # 比图形本身高 6px, 好在绘制时往下挪 —— 让图标压在**第一行文字**的高度上,
        # 而不是整行的垂直中线。注意别用样式表 margin 实现: QWidget 的样式表 margin
        # 会缩小内容区, 把画出来的图形直接剪掉 (踩过)
        self.setFixedSize(size + 2, size + 6)

    def paintEvent(self, e):
        col = QColor(self.color)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = h = self.icon_size
        p.translate(1, 4)          # 下移, 与第一行文字的视觉中线对齐
        pen = QPen(col, max(1.1, w * 0.11))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        k = self.kind
        if k == "dot":                           # 列表续行: 一个小实心点
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            r = w * 0.16
            p.drawEllipse(QRectF(w * .5 - r, h * .5 - r, r * 2, r * 2))
        elif k == "star":                        # 生日 (蛋糕在这个尺寸画不出来)
            import math
            path = QPainterPath()
            for i in range(10):
                ang = -math.pi / 2 + i * math.pi / 5
                r = (w * .48) if i % 2 == 0 else (w * .20)
                pt = QPointF(w * .5 + r * math.cos(ang), h * .5 + r * math.sin(ang))
                path.moveTo(pt) if i == 0 else path.lineTo(pt)
            path.closeSubpath()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawPath(path)
        elif k == "calendar":                    # 相识
            p.drawRoundedRect(QRectF(w * .10, h * .24, w * .80, h * .66), 1.2, 1.2)
            p.drawLine(QPointF(w * .10, h * .46), QPointF(w * .90, h * .46))
            p.drawLine(QPointF(w * .32, h * .12), QPointF(w * .32, h * .32))
            p.drawLine(QPointF(w * .68, h * .12), QPointF(w * .68, h * .32))
        elif k == "clock":                       # 陪伴
            p.drawEllipse(QRectF(w * .10, h * .10, w * .80, h * .80))
            p.drawLine(QPointF(w * .5, h * .5), QPointF(w * .5, h * .26))
            p.drawLine(QPointF(w * .5, h * .5), QPointF(w * .68, h * .62))
        elif k == "heart":                       # 相恋
            path = QPainterPath()
            path.moveTo(w * .5, h * .88)
            path.cubicTo(w * -.12, h * .46, w * .16, h * -.02, w * .5, h * .30)
            path.cubicTo(w * .84, h * -.02, w * 1.12, h * .46, w * .5, h * .88)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawPath(path)
        p.end()


class PaperCard(QFrame):
    """「一页纸」卡片基类 (v0.11) —— 档案卡和日程卡共用同一张纸。

    纸的质感靠三样叠出来: 纸底斜向微渐变、噪点平铺(纤维感)、软阴影(没有阴影,
    纸就"贴"在屏幕上而不是"浮"着)。横线要**对齐到字行** —— 纸上的线是给字当坐垫的,
    按固定间距平铺、让字骑在两条线中间, 一眼就是假的。

    和 RestPrompt 一样是无边框 Tool 窗 (不进任务栏、不抢焦点) —— 但那个是**提醒**,
    会自己弹出来, 所以必须自己消失; 卡片是用户主动点开的**查询**, 所以点一下就收起。
    对"安静陪伴"的底线: 卡片永远不会自己弹 (见 pet-quiet-by-default)。

    子类只负责往 self.lay 里塞内容, 最后调一次 self.finish()。
    """

    # 290 而不是更窄: 档案的生日行是三行结构("农历十月十九 · 今年 11-27" 正好一行),
    # 窄了会把第一行折成两行, 三行变四行, 反而更挤
    WIDTH = 290
    MARGIN_L, MARGIN_R, MARGIN_T, MARGIN_B = 26, 16, 14, 13
    TAG_W = 44                 # 行首标签那一列
    PAD = 6                    # 纸外面留给阴影的边距

    PAPER = "#F7F1E3"
    EDGE = "#DDD2BC"
    RULE = "#CBD8E2"
    MARGIN_LINE = "#D98B8B"
    INK = "#3B352F"
    LABEL = "#8B8371"
    HINT = "#B3AA96"
    # 楷体优先, 后面几个是不同系统上的等价字体 (Qt 找不到就用下一个)
    FONT = "'KaiTi','STKaiti','Kaiti SC','SimSun',serif"

    def __init__(self, pet: "PetWindow", title_text: str, slot_name: str):
        super().__init__(None)
        self.pet = pet
        self._slot = slot_name        # 宠物身上挂自己的属性名, 收起时清掉
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool          # 不进任务栏
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedWidth(self.WIDTH)
        self._grain = self._grain_tile()

        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(self.MARGIN_L, self.MARGIN_T,
                                    self.MARGIN_R, self.MARGIN_B)
        self.lay.setSpacing(0)        # 行距在 finish() 里按"一整行"设

        # 注意 QLabel#body 这条不能漏: 正文标签就是靠它拿到 13px 楷体的。
        # 漏掉的话它们会退回默认字体, 字体度量比标题小一圈 —— 而横线是按正文行高
        # 铺的, 结果是线全部错位 (踩过)
        self.setStyleSheet(
            f"QLabel#title{{color:{self.INK}; font-family:{self.FONT};"
            f" font-size:17px; font-weight:bold;}}"
            f"QLabel#body{{color:{self.INK}; font-family:{self.FONT}; font-size:13px;}}"
            f"QLabel#intro{{color:{self.INK}; font-family:{self.FONT}; font-size:13px;}}"
            f"QLabel#hint{{color:{self.HINT}; font-family:{self.FONT}; font-size:11px;}}"
            f"QLabel#foot{{color:{self.HINT}; font-family:{self.FONT}; font-size:11px;}}"
        )
        title = QLabel(title_text, self)
        title.setObjectName("title")
        self.lay.addWidget(title)

    def finish(self):
        """子类塞完内容后调一次: 定行距 + 算尺寸。

        行间空一整行: 既像手写在横线纸上, 又保证每行起点都落在格线上。
        """
        self.adjustSize()
        bodies = [w for w in self._text_labels() if w.objectName() == "body"]
        if bodies:
            # 用 lineSpacing() 而不是 height(): 前者才是 QLabel 实际换行时用的行距
            self.lay.setSpacing(bodies[0].fontMetrics().lineSpacing())

    # ---------- 内容小工具 ----------
    def _text_width(self) -> int:
        return self.WIDTH - self.MARGIN_L - self.MARGIN_R

    def _text_labels(self) -> list:
        """参与"横线对齐"的文字: 正文 + 简介。标题/指引/脚注不算。"""
        return [w for w in self.findChildren(QLabel)
                if w.objectName() in ("body", "intro", "hint")]

    def _wrapped(self, text: str, object_name: str, width: int) -> QLabel:
        """会自动换行的标签。

        必须显式钉死宽度: QLabel 的 wordWrap 靠 heightForWidth 算高度, 而在
        固定宽的 QFrame 里布局层常常算不准 —— 表现是**最后一行被裁掉**
        (空档案的填写指引就踩过这个坑, 代码片段少了半行)。
        """
        lbl = QLabel(text, self)
        lbl.setObjectName(object_name)
        lbl.setWordWrap(True)
        lbl.setFixedWidth(width)
        sp = lbl.sizePolicy()
        sp.setHeightForWidth(True)
        lbl.setSizePolicy(sp)
        return lbl

    def _row(self, icon: str, label: str, value: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(0)
        top = Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        mark = IconMark(icon, self.LABEL, 14)
        tag = QLabel(label, self)
        tag.setObjectName("body")
        tag.setStyleSheet(f"color:{self.LABEL}; font-family:{self.FONT}; font-size:13px;")
        tag.setFixedWidth(self.TAG_W)
        tag.setAlignment(top)
        val = self._wrapped(value, "body", self._text_width() - self.TAG_W - 19)
        val.setAlignment(top)
        row.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
        row.addSpacing(3)
        row.addWidget(tag)
        row.addWidget(val, 1)
        return row

    def _foot(self) -> QLabel:
        foot = QLabel("点一下收起", self)
        foot.setObjectName("foot")
        foot.setAlignment(Qt.AlignmentFlag.AlignRight)
        return foot

    # ---------- 纸 ----------
    def _grain_tile(self) -> QPixmap:
        """噪点贴图: 纸的纤维感来源。固定随机种子, 每次运行长得一样。"""
        import random
        rnd = random.Random(7)
        img = QImage(72, 72, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        for _ in range(220):
            x, y = rnd.randrange(72), rnd.randrange(72)
            a = rnd.randrange(4, 11)
            p.setPen(QColor(120, 105, 80, a) if rnd.random() < 0.55
                     else QColor(255, 255, 255, a))
            p.drawPoint(x, y)
        p.end()
        return QPixmap.fromImage(img)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pad = self.PAD
        r = QRectF(self.rect()).adjusted(pad, pad - 1, -pad, -pad - 2)
        path = QPainterPath()
        path.addRoundedRect(r, 3, 3)

        # 1) 软阴影: 没有它纸就"贴"在屏幕上而不是"浮"着
        for i in range(7, 0, -1):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, int(28 / (i + 1.2))))
            p.drawRoundedRect(r.adjusted(-i / 2, i / 2 + 1, i / 2, i / 2 + 3),
                              5, 5)

        # 2) 纸底: 极轻的斜向渐变, 免得死板
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        paper = QColor(self.PAPER)
        g.setColorAt(0, paper.lighter(102))
        g.setColorAt(1, paper.darker(103))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawPath(path)

        # 3) 噪点(纤维感) —— 平铺贴图, 只画在纸的范围内
        p.save()
        p.setClipPath(path)
        p.fillPath(path, QBrush(self._grain))
        p.restore()

        # 4) 横线 —— 对齐到字行。只覆盖正文和简介(同为 13px): 填写指引是 11px 小字,
        # 拿正文行高去套它, 字和线会交错着压在一起, 反而更难读
        labels = [w for w in self._text_labels()
                  if w.isVisible() and w.objectName() in ("body", "intro")]
        if labels:
            fm = labels[0].fontMetrics()
            top = labels[0].y()
            bottom = labels[-1].y() + labels[-1].height()
            p.save()
            p.setClipPath(path)
            p.setPen(QPen(QColor(self.RULE), 1))
            pitch = fm.lineSpacing()          # 不是 height(): 换行用的是行距
            y = top + fm.ascent() + (pitch - fm.ascent() - fm.descent()) // 2
            while y < bottom + fm.descent():
                p.drawLine(QPointF(r.left() + 1, y), QPointF(r.right() - 1, y))
                y += pitch
            p.restore()
            # 5) 左侧红边线 (信纸的招牌), 画在文字左边的留白里, 不压着图标
            p.setPen(QPen(QColor(self.MARGIN_LINE), 1))
            x = r.left() + self.MARGIN_L - 13
            p.drawLine(QPointF(x, r.top() + 2), QPointF(x, r.bottom() - 2))

        # 6) 纸边: 一圈淡描边, 让纸有厚度
        p.setPen(QPen(QColor(self.EDGE), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.end()

    # ---------- 位置与交互 ----------
    def show_near_pet(self):
        """贴在宠物上方; 上方放不下就挪到下面, 并夹进屏幕可用区域。"""
        self.adjustSize()
        g = self.pet.frameGeometry()
        x = g.center().x() - self.width() // 2
        y = g.top() - self.height() - 10
        screen = self.pet.screen()
        if screen is not None:
            area = screen.availableGeometry()
            if y < area.top():
                y = g.bottom() + 10
            x = max(area.left() + 4, min(x, area.right() - self.width() - 4))
            y = max(area.top() + 4, min(y, area.bottom() - self.height() - 4))
        self.move(x, y)
        self.show()
        self.raise_()      # raise_ 只置顶, 不激活 -> 不抢当前窗口的键盘焦点

    def close_card(self):
        """收起: 点卡片 / 再点一次菜单。"""
        if getattr(self.pet, self._slot, None) is self:
            setattr(self.pet, self._slot, None)   # 下次点菜单是"重新打开"而不是关空气
        self.hide()
        self.deleteLater()

    def mousePressEvent(self, e):
        self.close_card()


class ProfileCard(PaperCard):
    """人物档案卡 (v0.11): 它是谁、陪了多久、下一个纪念日还有几天。

    文案全部由 PetProfile 算好, 这里只负责摆位置。
    """

    # 实测: 14px 下蛋糕/蜡烛/礼物这类细节多的都糊成一团, 只有实心剪影认得出
    ICON = {"生日": "star", "相识": "calendar", "陪伴": "clock", "相恋": "heart"}

    def __init__(self, pet: "PetWindow", profile: PetProfile):
        super().__init__(pet, profile.header() or "（还没有名字）", "_profile_card")
        rows = profile.rows()
        for icon, label, value in rows:
            self.lay.addLayout(self._row(self.ICON.get(label, "star"), label, value))
        if not profile.filled:
            # 两件事不互斥: 示例宠物没填档案也会有一行"陪伴第 N 天"(来自 work.json),
            # 但那不代表档案填好了 —— 指引还得给
            self.lay.addWidget(self._wrapped(self._empty_tip(profile), "hint",
                                             self._text_width()))
        if profile.intro:
            self.lay.addWidget(self._wrapped(profile.intro, "intro",
                                             self._text_width()))
        self.lay.addWidget(self._foot())
        self.finish()

    @staticmethod
    def _empty_tip(profile: PetProfile) -> str:
        # 路径太长的只留 "pets/<名字>/pet.json" 这一段, 面板上写全路径没法看
        rel = profile.pet_dir.replace("\\", "/").rstrip("/")
        rel = "pets/" + rel.split("/")[-1] if "pets/" in rel else rel
        return (f"还没有填档案。在 {rel}/pet.json 里加一段：\n"
                '  "profile": {"title": "你的专属桌宠",\n'
                '   "birthday": "03-14", "meet_date": "2026-09-14"}\n'
                "生日只写月日也能算倒计时。")


def _day_tag(when: datetime, now: datetime) -> str:
    """行首那个短标签: 今天/明天/后天, 再远就写 月/日 (44px 里放得下)。"""
    delta = (when.date() - now.date()).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "明天"
    if delta == 2:
        return "后天"
    return f"{when.month}/{when.day}"


def schedule_reminder_text(events: list, when: datetime, now: datetime) -> str:
    """到点该说给用户听的那句话。events 是**同一时刻**的所有安排 (可能多场)。

    同时段的几场要**一句话说完**, 不能一件事说一句 —— 那听起来像有好几件事
    要办, 其实是撞在一起了, 用户得马上做取舍。
    """
    minutes = int((when - now).total_seconds() // 60)
    names = pet_schedule.titles_text(events, sep="、")
    if len(events) > 1:
        head = f"有 {len(events)} 场撞在一起了"
        tail = "现在就开始" if minutes <= 0 else f"{pet_schedule.humanize_gap(minutes)}后开始"
        return f"{tail}，{head}：{names}"
    if minutes <= 0:
        return f"到点了：{names}"
    return f"{pet_schedule.humanize_gap(minutes)}后有安排：{names}"


def missed_summary_text(missed: list) -> str:
    """错过补报: 汇总成一条, 不逐个重放 (防轰炸)。"""
    titles = []
    for ev, _ in missed:
        if ev.title not in titles:
            titles.append(ev.title)
    shown = "、".join(titles[:3])
    if len(titles) > 3:
        shown += "…"
    return f"你不在的时候错过了 {len(missed)} 个安排：{shown}"


class ScheduleCard(PaperCard):
    """日程卡 (v0.12): 下一个安排是什么、还有多久, 后面还排着几件。

    比档案卡多一层约束: 它是**会被反复打开**的, 所以信息要一眼看完 ——
    第一行给"还有多久", 其余只列时间 + 标题。

    **同一时刻的几场并成一行** (v0.17): 秋招季常见"同一时间两场宣讲会",
    排成两行会让人以为先开一场再开一场 —— 它们其实是冲突的, 得摆在一起看。
    """

    def __init__(self, pet: "PetWindow", sched):
        super().__init__(pet, "日程安排", "_schedule_card")
        now = datetime.now()
        groups = sched.grouped(now, limit=6)
        if not groups:
            self.lay.addWidget(self._wrapped(
                "还没有日程。\n右键宠物 →「➕ 添加日程…」、或把 Excel/Word 拖到我身上。",
                "hint", self._text_width()))
        else:
            for i, (when, events) in enumerate(groups[:4]):
                value = f"{when:%H:%M}  {pet_schedule.titles_text(events)}"
                if len(events) > 1:
                    value += f"\n同时 {len(events)} 场，得挑一个去"
                elif events[0].describe_repeat():
                    value += f"（{events[0].describe_repeat()}）"
                if i == 0:                      # 第一行才是"下一个", 多给一行倒计时
                    minutes = int((when - now).total_seconds() // 60)
                    left = "就是现在" if minutes <= 0 else f"还有 {pet_schedule.humanize_gap(minutes)}"
                    lead = min(e.remind_before for e in events)
                    tip = (f"{left} · 提前 {lead} 分钟提醒" if lead
                           else f"{left} · 到点提醒")
                    value += f"\n{tip}"
                self.lay.addLayout(
                    self._row("clock" if i == 0 else "dot",
                              _day_tag(when, now), value))
            if len(groups) > 4:
                self.lay.addWidget(self._wrapped(
                    f"后面还有 {len(groups) - 4} 个时段。", "hint",
                    self._text_width()))
        self.lay.addWidget(self._foot())
        self.finish()


class TaskCard(PaperCard):
    """待办卡 (v0.16): 什么事还没做完、到哪一步了。

    和日程卡的区别: 日程按时间排, 待办按"还剩几天"排, 逾期的最前面 ——
    打开这张卡应该一眼看到"哪件事要炸了"。
    """

    def __init__(self, pet: "PetWindow", store):
        super().__init__(pet, "待办", "_task_card")
        opened = store.sorted_open()
        if not opened:
            self.lay.addWidget(self._wrapped(
                "手头没有待办。\n聊天里跟我说「我要做…」就会记在这儿。",
                "hint", self._text_width()))
        else:
            for task in opened[:4]:
                left = task.days_left()
                if left is None:
                    tag, icon = "——", "dot"
                elif left < 0:
                    tag, icon = "逾期", "clock"
                elif left == 0:
                    tag, icon = "今天", "clock"
                elif left == 1:
                    tag, icon = "明天", "dot"
                else:
                    tag, icon = f"{left} 天", "dot"
                value = task.title
                prog = task.progress()
                if prog:
                    value += f"\n进度 {prog}"
                if task.note:
                    value += f"（{task.note}）"
                self.lay.addLayout(self._row(icon, tag, value))
            if len(opened) > 4:
                self.lay.addWidget(self._wrapped(
                    f"还有 {len(opened) - 4} 条没列出来。", "hint",
                    self._text_width()))
        self.lay.addWidget(self._foot())
        self.finish()


def task_due_line(task) -> str:
    """待办到点时该说的那句话。"""
    left = task.days_left()
    if left is not None and left < 0:
        return f"「{task.title}」已经逾期 {abs(left)} 天了，还做吗？"
    return f"「{task.title}」今天到期，别忘了"


class ImportPreviewDialog(QDialog):
    """导入预览: **先让你过目, 确认了才写进日程** (v0.17)。

    为什么不"丢进去就自动生成": 一个 Excel 可能几十行, 里面几条解析错了, 用户
    根本发现不了 —— 等哪天提醒没响才回头查, 代价太大。多这一步, 是让"导入"这件
    事可撤销、可核对。
    """

    def __init__(self, parent, events: list[dict], source: str, existing: set):
        super().__init__(parent)
        self.setWindowTitle("导入日程")
        self.setStyleSheet(
            f"QDialog{{background:{PaperCard.PAPER};}}"
            f"QLabel{{font-family:{PaperCard.FONT}; font-size:13px;"
            f" color:{PaperCard.INK};}}"
            f"QCheckBox{{font-family:{PaperCard.FONT}; color:{PaperCard.INK};}}"
            f"QPushButton{{font-family:{PaperCard.FONT}; font-size:13px;"
            f" padding:5px 12px; border:1px solid {PaperCard.EDGE};"
            f" border-radius:6px; background:#FFFDF7; color:{PaperCard.INK};}}"
            f"QPushButton:hover{{background:#F1E9D6;}}")
        self.resize(430, 460)
        self._boxes: list[tuple[QCheckBox, dict]] = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 10)
        lay.setSpacing(8)
        head = QLabel(f"从文件里读到 {len(events)} 条安排（{source}），确认要加进日程吗？",
                      self)
        head.setWordWrap(True)
        lay.addWidget(head)

        area = QScrollArea(self)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setStyleSheet("QScrollArea{background:transparent;}")
        holder = QFrame(area)
        holder.setStyleSheet("background:transparent;")
        box_lay = QVBoxLayout(holder)
        box_lay.setContentsMargins(2, 2, 2, 2)
        box_lay.setSpacing(6)

        dup = 0
        for ev in events:
            label = f"{ev['date']} {ev['time'] or '全天'}  {ev['title']}"
            reps = pet_schedule.REPEAT_NAMES.get(ev.get("repeat") or "none", "")
            if reps and ev["repeat"] != "none":
                label += f"（{reps}）"
            key = (ev["title"], ev["date"], ev.get("time") or "")
            already = key in existing
            if already:
                dup += 1
                label += "  —— 日程里已经有了"
            cb = QCheckBox(label, holder)
            cb.setChecked(not already)          # 已有的默认不勾, 免得重复导入
            cb.setStyleSheet("QCheckBox{font-size:12px;}")
            box_lay.addWidget(cb)
            self._boxes.append((cb, ev))
        box_lay.addStretch(1)
        area.setWidget(holder)
        lay.addWidget(area, 1)

        tip = QLabel(("其中 %d 条日程里已经有了，默认没勾。" % dup) if dup
                     else "取消勾选就不要那条。", self)
        tip.setStyleSheet(f"QLabel{{color:{PaperCard.HINT}; font-size:11px;}}")
        lay.addWidget(tip)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("算了", self)
        cancel.clicked.connect(self.reject)
        ok = QPushButton("加进日程", self)
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)

    def selected(self) -> list[dict]:
        return [ev for cb, ev in self._boxes if cb.isChecked()]


class JobFairSettingsDialog(QDialog):
    """秋招设置 (v0.20): 填就业网地址、调提前天数、开关自动同步。

    为什么要给界面: 不同学校的就业网地址不同, 以前只能去手改 ai_config.json ——
    用户根本不知道有这么个东西。

    还有一个「测试这个地址」: 填完直接告诉你能不能读（内置规则 / 要大模型 /
    根本不是宣讲会页面），别等抓完才发现填错了。
    """

    test_done = pyqtSignal(str)

    def __init__(self, parent, cfg: dict):
        super().__init__(parent)
        self.setWindowTitle("秋招设置")
        self.setStyleSheet(
            f"QDialog{{background:{PaperCard.PAPER};}}"
            f"QLabel{{font-family:{PaperCard.FONT}; font-size:13px;"
            f" color:{PaperCard.INK};}}"
            f"QLineEdit,QSpinBox{{font-family:{PaperCard.FONT}; font-size:13px;"
            f" padding:3px 6px;}}"
            f"QPushButton{{font-family:{PaperCard.FONT}; font-size:13px;"
            f" padding:5px 12px; border:1px solid {PaperCard.EDGE};"
            f" border-radius:6px; background:#FFFDF7; color:{PaperCard.INK};}}"
            f"QPushButton:hover{{background:#F1E9D6;}}")
        self.resize(460, 260)

        form = QFormLayout(self)
        form.setContentsMargins(14, 14, 14, 12)
        form.setSpacing(9)

        self.url_edit = QLineEdit(str(cfg.get("jobfair_url")
                                      or pet_jobfair.DEFAULT_URL), self)
        self.url_edit.setPlaceholderText("学校就业网的宣讲会列表页地址")
        form.addRow("就业网地址", self.url_edit)

        self.days_box = QSpinBox(self)
        self.days_box.setRange(1, 60)
        self.days_box.setValue(int(cfg.get("jobfair_days_ahead", 7) or 7))
        self.days_box.setSuffix(" 天内")
        form.addRow("只看未来", self.days_box)

        self.auto_box = QSpinBox(self)
        self.auto_box.setRange(0, 24)
        self.auto_box.setValue(int(float(cfg.get("jobfair_auto_hours", 0) or 0)))
        self.auto_box.setSuffix(" 小时一次")
        self.auto_box.setSpecialValueText("不自动同步")
        form.addRow("自动同步", self.auto_box)

        self.status = QLabel("填完点「测试这个地址」看看认不认得出。", self)
        self.status.setWordWrap(True)
        self.status.setStyleSheet(
            f"QLabel{{color:{PaperCard.HINT}; font-family:{PaperCard.FONT};"
            f" font-size:11px;}}")
        form.addRow(self.status)

        row = QHBoxLayout()
        self.test_btn = QPushButton("测试这个地址", self)
        self.test_btn.clicked.connect(self._test)
        row.addWidget(self.test_btn)
        row.addStretch(1)
        form.addRow(row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.test_done.connect(self._show_test)

    def values(self) -> dict:
        return {
            "jobfair_url": self.url_edit.text().strip(),
            "jobfair_days_ahead": self.days_box.value(),
            "jobfair_auto_hours": self.auto_box.value(),
        }

    # ---------- 测试 ----------
    def _test(self):
        self.test_btn.setEnabled(False)
        self.status.setText("正在看这个地址…")
        url = self.url_edit.text().strip()

        def worker():
            try:
                items, _ = pet_jobfair.fetch_teachins(url, days_ahead=30)
                if items:
                    days = len({i["date"] for i in items})
                    self.test_done.emit(
                        f"✓ 能读！内置规则解析出 {len(items)} 场宣讲会，"
                        f"分布在 {days} 天里（最近的：{items[0]['date']} "
                        f"{items[0]['time']} {items[0]['title'][:16]}）")
                    return
                html = pet_jobfair.fetch_page(url)
                if not html:
                    self.test_done.emit("✗ 这个地址打不开，检查一下网址有没有写错")
                    return
                text = pet_jobfair.html_to_text(html)
                if pet_jobfair.looks_like_jobfair(text):
                    self.test_done.emit(
                        "△ 这页看着是宣讲会页面，但内置规则认不出它的格式 —— "
                        "没关系，抓取时会自动交给大模型来读（需要配好 API key）")
                else:
                    self.test_done.emit(
                        "✗ 这页看起来不是宣讲会/招聘会列表页，"
                        "换个更具体的地址试试（要那种一列都是宣讲会的页面）")
            except Exception as e:                        # noqa: BLE001
                self.test_done.emit(f"✗ 出错：{e.__class__.__name__}: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _show_test(self, msg: str):
        self.test_btn.setEnabled(True)
        self.status.setText(msg)
        self.status.setStyleSheet(
            f"QLabel{{color:{'#3F7A46' if msg.startswith('✓') else '#A4462F'};"
            f" font-family:{PaperCard.FONT}; font-size:11px;}}")


class WeatherSettingsDialog(QDialog):
    """天气提醒设置 (v0.22): 你在哪个城市、提前多久提醒、概率多少才算。

    数据源是 Open-Meteo（免费、不用注册、不用 key），所以这里没有"填 api key"这一项。
    """

    def __init__(self, parent, cfg: dict):
        super().__init__(parent)
        self.setWindowTitle("天气设置")
        self.setStyleSheet(
            f"QDialog{{background:{PaperCard.PAPER};}}"
            f"QLabel{{font-family:{PaperCard.FONT}; font-size:13px;"
            f" color:{PaperCard.INK};}}"
            f"QLineEdit,QSpinBox{{font-family:{PaperCard.FONT}; font-size:13px;"
            f" padding:3px 6px;}}"
            f"QPushButton{{font-family:{PaperCard.FONT}; font-size:13px;"
            f" padding:5px 12px; border:1px solid {PaperCard.EDGE};"
            f" border-radius:6px; background:#FFFDF7; color:{PaperCard.INK};}}"
            f"QPushButton:hover{{background:#F1E9D6;}}")
        self.resize(420, 220)

        form = QFormLayout(self)
        form.setContentsMargins(14, 14, 14, 12)
        form.setSpacing(9)
        self.city_edit = QLineEdit(str(cfg.get("weather_city") or ""), self)
        self.city_edit.setPlaceholderText("比如：南昌（留空 = 关掉天气提醒）")
        form.addRow("你在哪个城市", self.city_edit)

        self.hours_box = QSpinBox(self)
        self.hours_box.setRange(1, 12)
        self.hours_box.setValue(int(cfg.get("weather_within_hours", 2) or 2))
        self.hours_box.setSuffix(" 小时内")
        form.addRow("提前多久提醒", self.hours_box)

        self.prob_box = QSpinBox(self)
        self.prob_box.setRange(20, 100)
        self.prob_box.setValue(int(cfg.get("weather_min_probability", 60) or 60))
        self.prob_box.setSuffix(" % 概率才算")
        form.addRow("多大的雨才说", self.prob_box)

        tip = QLabel("只在下雨前提醒收衣服 —— 不做每日天气播报。\n"
                     "数据来自 Open-Meteo，免费且不需要填 key。", self)
        tip.setWordWrap(True)
        tip.setStyleSheet(f"QLabel{{color:{PaperCard.HINT}; font-size:11px;}}")
        form.addRow(tip)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> dict:
        return {
            "weather_city": self.city_edit.text().strip(),
            "weather_within_hours": self.hours_box.value(),
            "weather_min_probability": self.prob_box.value(),
        }


class AddEventDialog(QDialog):
    """添加日程的小表单。

    日期时间都让用户手写字符串 (和 schedule.json 里存的一样), 不塞日历控件:
    一是少写一堆代码, 二是"09-16"这种省略年份的写法手写更快, 而且用户以后
    直接改 json 也认得同一个格式。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加日程")
        self.setStyleSheet(
            f"QDialog{{background:{PaperCard.PAPER};}}"
            f"QLabel{{font-family:{PaperCard.FONT}; font-size:13px;"
            f" color:{PaperCard.INK};}}"
            f"QLineEdit,QComboBox,QSpinBox{{font-family:{PaperCard.FONT};"
            f" font-size:13px; padding:3px 6px;}}"
        )
        form = QFormLayout(self)
        form.setContentsMargins(16, 14, 16, 12)
        form.setSpacing(9)

        self.title_edit = QLineEdit(self)
        self.title_edit.setPlaceholderText("开会 / 交房租 / 给妈妈打电话…")
        form.addRow("做什么", self.title_edit)

        self.date_edit = QLineEdit(self)
        self.date_edit.setPlaceholderText("2026-09-16，也可以只写 09-16")
        today = datetime.now().date()
        self.date_edit.setText(today.isoformat())
        form.addRow("哪天", self.date_edit)

        self.time_edit = QLineEdit(self)
        self.time_edit.setPlaceholderText("14:30（留空按早上 9 点）")
        form.addRow("几点", self.time_edit)

        self.repeat_box = QComboBox(self)
        for key in pet_schedule.REPEATS:
            self.repeat_box.addItem(pet_schedule.REPEAT_NAMES[key], key)
        form.addRow("重复", self.repeat_box)

        self.remind_box = QSpinBox(self)
        self.remind_box.setRange(0, 24 * 60)
        self.remind_box.setValue(pet_schedule.DEFAULT_REMIND_BEFORE)
        self.remind_box.setSuffix(" 分钟前提早说")
        self.remind_box.setSpecialValueText("到点才说")
        form.addRow("提醒", self.remind_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("加进日程")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("算了")
        buttons.accepted.connect(self._try_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _try_accept(self):
        if not self.title_edit.text().strip():
            QMessageBox.information(self, "还差一点", "总得有个名字吧？")
            return
        if pet_schedule.parse_when(self.date_edit.text(), self.time_edit.text()) is None:
            QMessageBox.warning(
                self, "日期看不懂",
                "日期写成 2026-09-16 或者 09-16，时间写成 14:30。")
            return
        self.accept()

    def values(self) -> dict:
        return {
            "title": self.title_edit.text().strip(),
            "date": self.date_edit.text().strip(),
            "time": self.time_edit.text().strip(),
            "repeat": self.repeat_box.currentData(),
            "remind_before_minutes": self.remind_box.value(),
        }


class TrayController(QSystemTrayIcon):
    """系统托盘: 承担退出入口与桌宠总控 (参考 Qt 官方 systray 示例)。"""

    def __init__(self, pets: list[PetWindow]):
        super().__init__(_tray_icon())
        self.pets = pets
        self.setToolTip("PhotoPet 桌宠")
        # 注意: pets 是 main() 里的同一个列表对象, 导入新宠物要 append 进去
        # (aboutToQuit 里的 save_all 引用的是它, 这样新宠物也能被存档)
        self._build_menu()
        self.activated.connect(self._on_activated)

        # 盯着 pets/ 目录: 制作向导做完一只、或你自己放进去一只, 都自动冒出来
        self._known_dirs = {os.path.abspath(p.pet_dir) for p in pets}
        self.watch_timer = QTimer(self)
        self.watch_timer.timeout.connect(self._scan_new_pets)
        self.watch_timer.start(5000)

        self.show()

    def _scan_new_pets(self):
        """每 5 秒看一眼 pets/, 出现新的资源包就加进来。

        只认"带 pet.json 的目录" —— 生成器最后一步才写 pet.json, 所以不会
        撞见写了一半的包; 制作向导更是全程写在 .building-xxx 暂存目录里,
        点完成才挪到 pets/ 下。
        """
        root = app_paths.pets_dir()
        if not os.path.isdir(root):
            return
        for name in sorted(os.listdir(root)):
            if name.startswith("."):          # 暂存目录, 还没做好
                continue
            d = os.path.join(root, name)
            key = os.path.abspath(d)
            if not os.path.isdir(d) or key in self._known_dirs:
                continue
            if not os.path.exists(os.path.join(d, "pet.json")):
                continue
            self._known_dirs.add(key)
            try:
                pet = self.add_pet(d)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                # 记录之后再撤销: 多半是"正在往这个目录里写"还没写完,
                # 5 秒后再看一次就可能好了。真损坏的包也只会每 5 秒多花一次
                # JSON 解析, 代价可以忽略。
                self._known_dirs.discard(key)
                continue
            try:
                pet.say(f"又有新伙伴来啦：{pet.config.get('name', name)} 🎉")
            except Exception:                     # noqa: BLE001
                pass

    def open_wizard(self):
        """打开制作向导 (单独进程 —— 免得把 rembg 那一大坨依赖拖进桌宠进程)。"""
        wizard_py = os.path.join(ROOT, "wizard.py")
        if not os.path.exists(wizard_py):
            QMessageBox.warning(None, APP_NAME, "找不到 wizard.py。")
            return
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        runner = pythonw if os.path.exists(pythonw) else sys.executable
        try:
            subprocess.Popen([runner, wizard_py], cwd=ROOT)
        except OSError as e:
            QMessageBox.warning(None, APP_NAME, f"打不开向导：{e}")

    def _wizard_needs_source(self):
        """发布版 exe 里没有 rembg (打进去体积会从 140MB 涨到 1GB+)。"""
        QMessageBox.information(
            None, APP_NAME,
            "制作桌宠需要「源码方式」运行 —— 抠图依赖（rembg）大约 1GB，"
            "打进发布版会让体积翻好几倍，所以发布版只管用、不管做。\n\n"
            "想自己做一只的话，把仓库 clone 下来：\n"
            "    pip install -r requirements.txt\n"
            "    python wizard.py\n\n"
            "详见 README 的「从头做一个属于你的」。")

    def _build_menu(self):
        """重建菜单。导入新宠物后要重建一次, 否则菜单里看不到它。"""
        menu = QMenu()
        self.menu = menu
        pets = self.pets
        for i, pet in enumerate(pets):
            name = pet.config.get("name", f"Pet{i+1}")
            act = menu.addAction(f"显示/隐藏 {name}")
            act.triggered.connect(
                lambda _, p=pet: p.setVisible(not p.isVisible()))
        menu.addSeparator()
        menu.addAction("全部现身", self._show_all)

        # 大小档位 (Word 风格百分比, 选中即固定; 滚轮缩放已移除)
        zoom_menu = menu.addMenu("📏 大小")
        self.zoom_group = QActionGroup(zoom_menu)
        self.zoom_group.setExclusive(True)
        for step in ZOOM_STEPS:
            act = zoom_menu.addAction(f"{int(step * 100)}%")
            act.setCheckable(True)
            act.setChecked(abs(pets[0].zoom - step) < 0.01)
            act.triggered.connect(lambda _, s=step: self._set_zoom(s))
            self.zoom_group.addAction(act)

        # 鼠标穿透的关闭入口必须放在托盘: 穿透后宠物自身点不到,
        # 只靠宠物右键菜单会把自己锁死在穿透状态里
        self.ct_act = menu.addAction("👻 鼠标穿透")
        self.ct_act.setCheckable(True)
        self.ct_act.triggered.connect(self._toggle_click_through)

        menu.addSeparator()
        # 做一个新的 / 分享接收别人做的
        if app_paths.IS_FROZEN:
            menu.addAction("✨ 制作新桌宠…", self._wizard_needs_source)
        else:
            menu.addAction("✨ 制作新桌宠…", self.open_wizard)
        # 秋招入口也放托盘一份: 宠物右键菜单很长, 新功能容易找不到
        if pets:
            menu.addAction("🎓 抓宣讲会（秋招）",
                           lambda: pets[0].fetch_jobfair_async())
        menu.addAction("📦 导入桌宠 (.pet)…", self.import_pack)
        if len(pets) == 1:
            menu.addAction("📤 导出这只桌宠…", lambda: self.export_pack(pets[0]))
        elif pets:
            ex_menu = menu.addMenu("📤 导出桌宠…")
            for pet in pets:
                name = pet.config.get("name", "未命名")
                ex_menu.addAction(name, lambda _, p=pet: self.export_pack(p))
        if sys.platform == "win32":
            assoc = menu.addAction("🔗 双击 .pet 直接安装")
            assoc.setCheckable(True)
            assoc.setChecked(is_pet_associated())
            assoc.triggered.connect(self._toggle_assoc)

        menu.addSeparator()
        menu.addAction("退出", self._quit)

        # 菜单状态随宠物实际状态刷新 (两处都能切换穿透, 避免勾选不同步)
        menu.aboutToShow.connect(self._sync_menu)
        self.setContextMenu(menu)

    def _toggle_assoc(self, checked: bool):
        """把 .pet 关联到本程序, 这样别人发来的资源包双击就能装。"""
        if set_pet_association(checked):
            QMessageBox.information(
                None, APP_NAME,
                "好啦～ 以后别人发来的 .pet 双击就能装进桌宠。"
                if checked else "已取消 .pet 的文件关联。")
        else:
            QMessageBox.warning(None, APP_NAME, "当前系统不支持设置文件关联。")
        self._build_menu()                  # 刷新勾选状态

    # ---------- 资源包导入 / 导出 ----------
    def add_pet(self, pet_dir: str) -> PetWindow:
        """把新宠物加进正在运行的这一批 (不用重启)。"""
        dirs = [p.pet_dir for p in self.pets] + [pet_dir]
        pet = PetWindow(pet_dir, all_pet_dirs=dirs)
        pet.show()
        self.pets.append(pet)
        for p in self.pets:                 # 开机自启动要把新宠物也带上
            p.all_pet_dirs = dirs
        self._build_menu()
        return pet

    def import_pack(self):
        """选一个 .pet 文件装进来。"""
        path, _ = QFileDialog.getOpenFileName(
            None, "选择桌宠资源包", os.path.expanduser("~"),
            "桌宠资源包 (*.pet);;所有文件 (*)")
        if not path:
            return
        try:
            meta = pet_pack.peek(path)      # 先看看是什么, 让人有个确认的机会
        except pet_pack.PetPackError as e:
            QMessageBox.warning(None, APP_NAME, f"这个文件不能用：\n{e}")
            return
        title = meta.get("name") or os.path.basename(path)
        states = "、".join(meta.get("states") or []) or "无"
        sounds = "、".join(meta.get("sounds") or []) or "无"
        if QMessageBox.question(
                None, APP_NAME,
                f"要导入这只桌宠吗？\n\n名字：{title}\n动作：{states}\n音效：{sounds}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes) != QMessageBox.StandardButton.Yes:
            return

        new_name = None
        try:
            got = pet_pack.import_pet(path, app_paths.pets_dir())
        except pet_pack.PetPackError as e:
            if "已经有叫" not in str(e):
                QMessageBox.warning(None, APP_NAME, f"导入失败：\n{e}")
                return
            # 同名了: 让用户决定换名字还是覆盖
            box = QMessageBox(QMessageBox.Icon.Question, APP_NAME,
                              f"{e}\n\n要怎么办？")
            overwrite = box.addButton("覆盖旧的", QMessageBox.ButtonRole.DestructiveRole)
            rename = box.addButton("换个名字", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is overwrite:
                got = pet_pack.import_pet(path, app_paths.pets_dir(), overwrite=True)
            elif box.clickedButton() is rename:
                name, ok = QInputDialog.getText(None, APP_NAME, "新名字：")
                if not ok or not name.strip():
                    return
                got = pet_pack.import_pet(path, app_paths.pets_dir(),
                                          new_name=name.strip())
            else:
                return
        except OSError as e:
            QMessageBox.warning(None, APP_NAME, f"写入失败：{e}")
            return

        self.add_pet(got)
        QMessageBox.information(
            None, APP_NAME, f"已经住进来啦～\n\n{pet_pack.describe(got)}")

    def export_pack(self, pet: PetWindow):
        """把一只宠物导出成 .pet 发给别人。"""
        name = pet.config.get("name") or os.path.basename(pet.pet_dir)
        default = os.path.join(os.path.expanduser("~"), f"{name}.pet")
        path, _ = QFileDialog.getSaveFileName(
            None, "导出到", default, "桌宠资源包 (*.pet)")
        if not path:
            return
        if not path.lower().endswith(".pet"):
            path += ".pet"
        try:
            out, n = pet_pack.export_pet(pet.pet_dir, path)
        except pet_pack.PetPackError as e:
            QMessageBox.warning(None, APP_NAME, f"导出失败：\n{e}")
            return
        except OSError as e:
            QMessageBox.warning(None, APP_NAME, f"写文件失败：{e}")
            return
        QMessageBox.information(
            None, APP_NAME,
            f"已导出到：\n{out}\n\n"
            f"共 {n} 个文件，{os.path.getsize(out)/1048576:.1f} MB。\n"
            "养成存档（好感度、陪伴时长）已自动剔除，"
            "发给别人不会带上你的记录。")

    def _sync_menu(self):
        self.ct_act.setChecked(any(p.click_through for p in self.pets))

    def _toggle_click_through(self):
        """托盘里切换穿透: 只要有任意一只开着, 就全部关掉 (方便脱困)。"""
        target = not any(p.click_through for p in self.pets)
        for p in self.pets:
            p.set_click_through(target)
        self.ct_act.setChecked(target)

    def _set_zoom(self, zoom: float):
        """大小档位: 对所有桌宠生效。"""
        for p in self.pets:
            p.set_zoom(zoom)

    def _quit(self):
        # 养成数值与窗口位置/大小的保存统一由 app.aboutToQuit 处理 (见 main()):
        # 这样从宠物右键菜单退出也能存档, 不会绕过保存逻辑
        QApplication.quit()

    def _show_all(self):
        for p in self.pets:
            p.show()

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_all()


def save_all(pets: list[PetWindow]):
    """退出前保存所有桌宠: 养成数值 + 陪伴时长 + 窗口位置/大小 + 日程。

    挂在 app.aboutToQuit 上, 因此托盘退出、宠物右键退出、
    系统注销/关机都会走到这里, 不会因为退出路径不同而丢存档。
    """
    schedule().mark_exit()      # 记下退出时间, 下次开机用它算"错过了哪些安排"
    for p in pets:
        p.status.save()
        p.work.save()
        p.save_geometry()
        prompt = getattr(p, "_rest_prompt", None)
        if prompt is not None:
            prompt.close_if_open()
        card = getattr(p, "_profile_card", None)
        if card is not None:
            card.close_card()


def default_pet_dirs() -> list[str]:
    """没给 --pet 时: 自动加载资源包目录下所有宠物。

    用 app_paths.pets_dir() 而不是写死相对路径 —— 打包成 exe 后 ROOT 会指向
    PyInstaller 的临时解压目录, 写死的话双击 exe 必然找不到资源包。

    以前还默认写死 pets/demo (仓库里并不存在), 不带参数启动必然失败。
    """
    pets_root = app_paths.pets_dir()
    if os.path.isdir(pets_root):
        found = sorted(d for d in os.listdir(pets_root)
                       if os.path.isdir(os.path.join(pets_root, d)))
        if found:
            return [os.path.join(pets_root, d) for d in found]
    return [os.path.join(pets_root, "demo")]


def _acquire_single_instance():
    """单实例锁: 开机自启 + 手动双击不该起两批桌宠。

    QLockFile 会记下持有者的 PID, 上次崩溃留下的陈旧锁能被自动清理,
    所以不用担心"崩过一次以后就打不开了"。
    """
    from PyQt6.QtCore import QLockFile
    lock = QLockFile(os.path.join(app_paths.user_data_dir(), "photopet.lock"))
    if lock.tryLock(100):
        return lock
    return None


def _pet_open_command() -> tuple[str, str]:
    """双击 .pet 时执行的命令, 以及用作图标的程序路径。

    优先用装好的 exe; 源码运行时退回 pythonw + main.py。
    """
    exe = sys.executable if getattr(sys, "frozen", False) else \
        os.path.join(ROOT, APP_NAME + ".exe")
    if os.path.exists(exe):
        return f'"{exe}" "%1"', exe
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    runner = pythonw if os.path.exists(pythonw) else sys.executable
    launcher = os.path.join(ROOT, "runtime", "main.py")
    return f'"{runner}" "{launcher}" "%1"', runner


def is_pet_associated() -> bool:
    """.pet 现在是不是关联到本程序。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\.pet") as k:
            return winreg.QueryValueEx(k, "")[0] == PET_PROG_ID
    except (ImportError, OSError):
        return False


def set_pet_association(enabled: bool) -> bool:
    """把 .pet 关联到本程序 (HKCU, 不需要管理员, 随时可撤销)。

    关联后别人发来的 .pet 双击就能装进来 —— 这是"资源包能流动"的关键一步。
    """
    try:
        import winreg
        classes = winreg.HKEY_CURRENT_USER, r"Software\Classes"
        if enabled:
            command, icon = _pet_open_command()
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                                  r"Software\Classes\.pet") as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, PET_PROG_ID)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                                  rf"Software\Classes\{PET_PROG_ID}") as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "PhotoPet 桌宠资源包")
                winreg.SetValueEx(k, "FriendlyTypeName", 0, winreg.REG_SZ,
                                  "PhotoPet 桌宠资源包")
            with winreg.CreateKey(
                    winreg.HKEY_CURRENT_USER,
                    rf"Software\Classes\{PET_PROG_ID}\DefaultIcon") as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f"{icon},0")
            with winreg.CreateKey(
                    winreg.HKEY_CURRENT_USER,
                    rf"Software\Classes\{PET_PROG_ID}\shell\open\command") as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, command)
        else:
            import shutil as _sh
            for sub in (PET_PROG_ID, ".pet"):
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                                     rf"Software\Classes\{sub}\shell\open\command")
                except OSError:
                    pass
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                                     rf"Software\Classes\{sub}\DefaultIcon")
                except OSError:
                    pass
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                                     rf"Software\Classes\{sub}\shell\open")
                except OSError:
                    pass
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                                     rf"Software\Classes\{sub}\shell")
                except OSError:
                    pass
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                                     rf"Software\Classes\{sub}")
                except OSError:
                    pass
            del _sh
        return True
    except (ImportError, OSError):
        return False                        # 非 Windows 或权限受限, 静默降级


def _already_running_notice(seconds: int = 6):
    """已经在运行时给个提示, 但**几秒后自己关掉**。

    用会一直等在那里的模态框很不礼貌 —— 尤其是开机自启那一路顺带被触发时,
    用户得跑回来点一下确定。所以给它加个自动关闭。
    """
    box = QMessageBox(QMessageBox.Icon.Information, APP_NAME,
                      "桌宠已经在运行啦～\n\n"
                      "在系统托盘图标上右键就能找到它"
                      "（看不到的话点一下托盘的小箭头）。")
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.setWindowFlags(box.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    QTimer.singleShot(seconds * 1000, box.accept)
    box.exec()


def _no_pets_message() -> str:
    """没有资源包时的提示。打包成 exe 后不能再教用户敲 python 命令。"""
    if app_paths.IS_FROZEN:
        return ("没有可用的桌宠资源包。\n\n"
                f"请把桌宠资源包文件夹放到:\n{app_paths.pets_dir()}\n\n"
                "然后再启动。资源包可以用生成器(photo_to_pet.py)制作，"
                "详见项目的素材制作指南。")
    return ("没有可用的桌宠资源包。\n\n"
            "请先生成一只:\n"
            "    python generator/photo_to_pet.py --photo 你的照片.jpg --name mypet\n"
            "然后启动:\n"
            "    python runtime/main.py --pet pets/mypet")


def _import_dropped_packs(argv: list[str]) -> list[str]:
    """处理"把 .pet 拖到 exe 图标上"—— Windows 会把文件路径塞进命令行。

    必须在单实例锁**之前**做: 已经开着桌宠时再双击一个 .pet, 应该照样
    把宠物装进去, 而不是被锁挡掉什么都不发生。
    返回装好的目录名列表。
    """
    installed = []
    for arg in argv:
        if not arg.lower().endswith(".pet") or not os.path.exists(arg):
            continue
        try:
            got = pet_pack.import_pet(arg, app_paths.pets_dir(), overwrite=True)
            installed.append(os.path.basename(got))
        except (pet_pack.PetPackError, OSError) as e:
            QMessageBox.warning(None, APP_NAME,
                                f"导入 {os.path.basename(arg)} 失败：\n{e}")
    return installed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pet", nargs="+", default=None,
                    help="一个或多个桌宠资源包目录 (默认: pets/ 下全部)")
    ap.add_argument("--allow-multiple", action="store_true",
                    help="允许多开 (默认同一时间只跑一个实例)")
    args = ap.parse_args()

    app_paths.fix_console_encoding()
    app = QApplication(sys.argv)
    # 关键: 托盘常驻时, 关闭窗口不能退出程序 (参考官方示例)
    app.setQuitOnLastWindowClosed(False)
    # 应用级图标: 不设的话对话窗/设置窗的标题栏左上角是 Qt 的空白默认图标
    # (托盘图标是单独设的, 不会自动带到普通窗口上)
    app.setWindowIcon(_tray_icon())

    # 首次运行: 准备一只可用的宠物 (全新 clone 时装示例包; 只读安装时搬到可写位置)
    installed = app_paths.ensure_pets_available()
    if installed:
        print(f"[PhotoPet] 已准备好示例桌宠: {installed}")

    pet_dirs = [app_paths.resolve_pet_dir(d) for d in (args.pet or default_pet_dirs())]

    # 拖到图标上的 .pet 先装 (要早于单实例锁, 否则已经开着桌宠时拖进来会毫无反应)
    dropped = _import_dropped_packs([a for a in sys.argv[1:] if not a.startswith("-")])

    lock = None
    if not args.allow_multiple:
        lock = _acquire_single_instance()
        if lock is None:
            if dropped:
                QMessageBox.information(
                    None, APP_NAME,
                    "已经装好啦：" + "、".join(dropped) +
                    "\n\n桌宠正在运行中，重启它就能看到新宠物"
                    "（右键托盘图标 → 退出，再双击图标启动）。")
            else:
                _already_running_notice()
            sys.exit(0)

    pets = []
    for pet_dir in pet_dirs:
        try:
            pets.append(PetWindow(pet_dir, all_pet_dirs=pet_dirs))
        except (FileNotFoundError, json.JSONDecodeError) as err:
            QMessageBox.warning(None, APP_NAME, f"加载 {pet_dir} 失败:\n{err}")

    if not pets:
        QMessageBox.warning(None, APP_NAME, _no_pets_message())
        sys.exit(1)
    for p in pets:
        p.show()

    app.aboutToQuit.connect(lambda: save_all(pets))

    # tray 与 lock 必须保持引用存活到事件循环结束
    tray = TrayController(pets)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
