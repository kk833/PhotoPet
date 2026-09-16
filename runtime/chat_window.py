"""聊天窗口 + API 设置窗口 (v0.12)。

替代原来那个 `QInputDialog` 一问一答: 现在有真正的对话窗口、**边生成边显示**,
而且宠物是**想出一句说一句**的 —— 第一句话一出来就开口, 不用等整段回复生成完。

设计参考 (只借设计, 不搬代码):
- Pet-GPT(Hanzoe) 的 PyQt 对话窗口: QScrollArea + 逐条消息 widget、回车发送、
  Shift+Enter 换行、发消息时禁用输入框
- Open-LLM-VTuber 的 SentenceDivider: 把 token 流按**句末标点**切成完整句子再交给
  TTS, 而不是整段生成完再读 —— 这是"流式朗读"的关键

线程模型: `AIChat.chat_stream` 在后台线程跑, 通过本模块的 Qt 信号把结果送回主线程。
PyQt 里跨线程 emit 信号是安全的 (Qt 自动排队), 直接操作控件才会随机崩。
"""
import threading

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                             QMessageBox, QPlainTextEdit, QPushButton,
                             QScrollArea, QVBoxLayout, QCheckBox, QFormLayout,
                             QDialogButtonBox)

from pet_ai import AIChat, load_ai_config, save_ai_config
from ai_tools import Toolbox
import pet_memory
import pet_task

# 和纸质卡片同一套配色, 免得聊天窗和档案卡像是两个软件
PAPER = "#F7F1E3"
INK = "#3B352F"
LABEL = "#8B8371"
HINT = "#B3AA96"
FONT = "'KaiTi','STKaiti','Kaiti SC','SimSun',serif"

BASE_QSS = (
    f"QDialog{{background:{PAPER};}}"
    f"QLabel{{font-family:{FONT}; font-size:14px; color:{INK};}}"
    f"QPlainTextEdit{{font-family:{FONT}; font-size:14px; background:#FFFDF7;"
    f" border:1px solid #DDD2BC; border-radius:6px; padding:6px; color:{INK};}}"
    f"QPushButton{{font-family:{FONT}; font-size:13px; padding:5px 12px;"
    f" border:1px solid #DDD2BC; border-radius:6px; background:#FFFDF7;"
    f" color:{INK};}}"
    f"QPushButton:hover{{background:#F1E9D6;}}"
    f"QPushButton:disabled{{color:{HINT};}}"
)


class MessageBubble(QLabel):
    """一条消息。用户靠右、宠物靠左, 一眼分得清谁说的。"""

    def __init__(self, text: str, mine: bool, parent=None):
        super().__init__(text, parent)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setStyleSheet(
            f"QLabel{{font-family:{FONT}; font-size:14px; color:{INK};"
            f" background:{'#EFE3C8' if mine else '#FFFDF7'};"
            f" border:1px solid {'#E3D5B4' if mine else '#E8E0CC'};"
            f" border-radius:8px; padding:7px 10px;}}"
        )
        self.setMaximumWidth(300)


class ChatWindow(QDialog):
    """对话窗口。关掉它宠物还在 (桌宠才是本体, 窗口只是入口)。"""

    # 后台线程 -> 主线程的三条通道
    delta_ready = pyqtSignal(str)       # 全文(每次更新)
    sentence_ready = pyqtSignal(str)    # 一个完整句子(拿去朗读)
    reply_done = pyqtSignal(str, str)   # (全文, 错误)

    def __init__(self, pet):
        super().__init__(None)
        self.pet = pet
        self.ai: AIChat = pet.ai
        self.setWindowTitle("和我说说话")
        self.setStyleSheet(BASE_QSS)
        self.resize(400, 470)
        self._streaming_label: MessageBubble | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 10)
        lay.setSpacing(8)

        # 消息区
        self.area = QScrollArea(self)
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QFrame.Shape.NoFrame)
        self.area.setStyleSheet("QScrollArea{background:transparent;}")
        self.holder = QFrame(self.area)
        self.holder.setStyleSheet("background:transparent;")
        self.msg_lay = QVBoxLayout(self.holder)
        self.msg_lay.setContentsMargins(2, 2, 2, 2)
        self.msg_lay.setSpacing(8)
        self.msg_lay.addStretch(1)
        self.area.setWidget(self.holder)
        lay.addWidget(self.area, 1)

        if not self.ai.available:
            self._add_system("还没配置 API。点下面的「⚙ 设置」填一个 key 就能聊了。")

        # 输入区
        self.input = QPlainTextEdit(self)
        self.input.setFixedHeight(64)
        self.input.setPlaceholderText("回车发送，Shift+回车换行…")
        lay.addWidget(self.input)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.send_btn = QPushButton("发送", self)
        self.send_btn.clicked.connect(self.send)
        settings_btn = QPushButton("⚙ 设置", self)
        settings_btn.clicked.connect(self.open_settings)
        clear_btn = QPushButton("清空对话", self)
        clear_btn.clicked.connect(self.clear)
        row.addWidget(self.send_btn)
        row.addWidget(settings_btn)
        row.addWidget(clear_btn)
        row.addStretch(1)
        lay.addLayout(row)

        self.delta_ready.connect(self._on_delta)
        self.sentence_ready.connect(self._on_sentence)
        self.reply_done.connect(self._on_done)
        self.input.installEventFilter(self)

    # ---------- 输入 ----------
    def eventFilter(self, obj, event):
        """回车发送 / Shift+回车换行。"""
        if obj is self.input and event.type() == event.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    return False                     # 交给控件自己换行
                self.send()
                return True
        return super().eventFilter(obj, event)

    def send(self):
        text = self.input.toPlainText().strip()
        if not text or self.send_btn.isEnabled() is False:
            return
        self.input.clear()
        self._add_message(text, mine=True)
        if not self.ai.available:
            self._add_system("还没配置 API，去「⚙ 设置」里填 api_key。")
            return
        self.send_btn.setEnabled(False)
        self._streaming_label = self._add_message("…", mine=False)
        self.ai.chat_stream(
            text,
            on_delta=lambda full: self.delta_ready.emit(full),
            on_sentence=lambda s: self.sentence_ready.emit(s),
            on_done=lambda full, err: self.reply_done.emit(full, err),
            toolbox=Toolbox(self.pet.schedule_ref(), self.pet,
                             pet_memory.Memory(pet_memory.default_path()),
                             pet_task.TaskStore(pet_task.default_path())),
        )

    # ---------- 后台线程送回来的 ----------
    def _on_delta(self, full: str):
        if self._streaming_label is not None:
            self._streaming_label.setText(full)
            self._scroll_to_bottom()

    def _on_sentence(self, sentence: str):
        """凑出一句就说一句 —— 宠物不等整段生成完。"""
        pet = self.pet
        if pet is not None:
            pet.say(sentence)

    def _on_done(self, full: str, err: str):
        self.send_btn.setEnabled(True)
        if err:
            if self._streaming_label is not None:
                self._streaming_label.setText(f"（没连上：{err}）")
            self._add_system("检查一下网络和 api_key，或者点「⚙ 设置」测一下连接。")
        elif not full and self._streaming_label is not None:
            self._streaming_label.setText("（它没说话，再问一次试试）")
        self._streaming_label = None
        self._scroll_to_bottom()
        self.input.setFocus()

    # ---------- 消息 ----------
    def _add_message(self, text: str, mine: bool) -> MessageBubble:
        bubble = MessageBubble(text, mine, self.holder)
        wrap = QHBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        if mine:
            wrap.addStretch(1)
            wrap.addWidget(bubble)
        else:
            wrap.addWidget(bubble)
            wrap.addStretch(1)
        self.msg_lay.insertLayout(self.msg_lay.count() - 1, wrap)
        QTimer.singleShot(0, self._scroll_to_bottom)
        return bubble

    def _add_system(self, text: str):
        lbl = QLabel(text, self.holder)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"QLabel{{color:{HINT}; font-family:{FONT}; font-size:12px;}}")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.msg_lay.insertWidget(self.msg_lay.count() - 1, lbl)

    def _scroll_to_bottom(self):
        bar = self.area.verticalScrollBar()
        bar.setValue(bar.maximum())

    def clear(self):
        """清空的是**界面**, 顺带把上下文也清掉 —— 半清不楚更让人困惑。"""
        while self.msg_lay.count() > 1:
            item = self.msg_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            else:
                sub = item.layout()
                if sub is not None:
                    while sub.count():
                        c = sub.takeAt(0)
                        if c.widget():
                            c.widget().deleteLater()
                    sub.deleteLater()
        self.ai.history.clear()
        self._streaming_label = None
        # 说清楚"清掉的到底是什么" —— 很多人不知道上下文和长期记忆是两回事
        self._add_system("对话清掉了，它不记得我们刚才聊过什么。\n"
                         "（它记在心里的那些事还在，要忘掉得跟它说「忘掉 XX」）")

    def open_settings(self):
        dlg = SettingsDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.ai.cfg = load_ai_config()
            self.ai.history.clear()          # 换了模型/key, 老上下文没意义了
            self._add_system("设置保存好了，接着聊。")


class SettingsDialog(QDialog):
    """API 设置。目前只做 DeepSeek 这一家, 但 base_url 可改, 兼容任何
    OpenAI 兼容接口 (换成别的服务商只要填地址和模型名)。
    """

    test_done = pyqtSignal(bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("API 设置")
        self.setStyleSheet(BASE_QSS)
        self.resize(380, 250)
        cfg = load_ai_config()

        form = QFormLayout(self)
        form.setContentsMargins(14, 14, 14, 12)
        form.setSpacing(9)

        self.key_edit = QLineEdit(str(cfg.get("api_key", "")), self)
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("sk-…")
        show = QCheckBox("显示", self)
        show.toggled.connect(lambda on: self.key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        key_row = QHBoxLayout()
        key_row.addWidget(self.key_edit, 1)
        key_row.addWidget(show)
        form.addRow("API Key", key_row)

        self.url_edit = QLineEdit(str(cfg.get("base_url", "")), self)
        form.addRow("接口地址", self.url_edit)

        self.model_edit = QLineEdit(str(cfg.get("model", "")), self)
        form.addRow("模型", self.model_edit)

        self.status = QLabel("key 只存在本机的 ai_config.json 里，不会上传。", self)
        self.status.setWordWrap(True)
        self.status.setStyleSheet(
            f"QLabel{{color:{HINT}; font-family:{FONT}; font-size:11px;}}")
        form.addRow(self.status)

        row = QHBoxLayout()
        self.test_btn = QPushButton("测试连接", self)
        self.test_btn.clicked.connect(self._test)
        row.addWidget(self.test_btn)
        row.addStretch(1)
        form.addRow(row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self.test_done.connect(self._show_test)

    def _collect(self) -> dict:
        return {
            "api_key": self.key_edit.text().strip(),
            "base_url": self.url_edit.text().strip() or "https://api.deepseek.com/v1",
            "model": self.model_edit.text().strip() or "deepseek-chat",
        }

    def _test(self):
        self.test_btn.setEnabled(False)
        self.status.setText("正在试…")
        values = self._collect()
        save_ai_config(values)          # 先存再测, 否则测的还是旧配置
        ai = AIChat()

        def worker():
            ok, msg = ai.test_connection()
            self.test_done.emit(ok, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _show_test(self, ok: bool, msg: str):
        self.test_btn.setEnabled(True)
        self.status.setText(("✓ " if ok else "✗ ") + msg)
        self.status.setStyleSheet(
            f"QLabel{{color:{'#3F7A46' if ok else '#A4462F'};"
            f" font-family:{FONT}; font-size:11px;}}")
        if self.status.parent() is not None:
            self.status.parent().adjustSize()

    def _save(self):
        if not self.key_edit.text().strip():
            QMessageBox.information(self, "还差一点", "没有 key 的话，宠物就没法接大模型。")
            return
        save_ai_config(self._collect())
        self.accept()
