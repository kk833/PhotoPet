# -*- coding: utf-8 -*-
"""PhotoPet 制作向导 —— 不用碰命令行，做一只自己的桌宠 (v0.10)。

用法:
    python wizard.py

五个步骤: 选照片 → 起名字 → 生成形象 → 加动作素材 → 完成。
向导本身只是界面, 真正的活儿全部复用已有的、验证过的工具:

    generator/photo_to_pet.py   抠图 + 三态派生
    scripts/add_asset.py        视频/图片 → 动画 GIF、音频 → 音效
    scripts/extract_audio.py    从动画视频里抽音轨当音效
    runtime/pet_pack.py         资源包校验 / 导出 .pet

为什么这个只能在源码方式下跑: 抠图要 rembg + onnxruntime(含模型) 差不多 1GB,
把它打进发布的 exe 会让体积从 140MB 涨到 1GB+。所以发布版只管"用",
想"做"就用源码方式(见 README)。
"""
import os
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if not getattr(sys, "frozen", False):
    # 源码运行: 把各子目录加进搜索路径 (打包后模块是平铺的, 不需要)
    for _sub in ("runtime", "generator", "scripts"):
        sys.path.insert(0, os.path.join(_HERE, _sub))

import app_paths

ROOT = app_paths.BUNDLE_DIR       # 打进 exe 的只读资源 / 源码仓库根


def pets_root() -> str:
    """宠物资源包目录。必须走 app_paths —— 打包后 __file__ 指向临时解压目录,
    写在那儿宠物一关就没了 (这个坑当初在 runtime 上踩过一次)。"""
    return app_paths.pets_dir()

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPixmap, QIcon, QDesktopServices
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import (
    QApplication, QWizard, QWizardPage, QLabel, QLineEdit, QPushButton,
    QFileDialog, QVBoxLayout, QHBoxLayout, QComboBox, QCheckBox, QPlainTextEdit,
    QProgressBar, QMessageBox, QGroupBox, QListWidget, QListWidgetItem,
    QAbstractItemView, QRadioButton, QButtonGroup, QFrame)

# 可选的抠图模型: 名字 -> (说明, 大概体积)
MODELS = [
    ("u2net", "轻量快速（约 170MB，低配电脑选这个）"),
    ("bria-rmbg", "高质量（约 1GB，毛发边缘最好）"),
]
# 动作素材可对应的状态名 (也能自己填)
KNOWN_STATES = ["idle", "click", "eat", "sleep", "sleep_loop", "talk", "pet"]
# 制作期间用的暂存目录前缀 (以 . 开头, 桌宠的目录监视会跳过)
STAGING_PREFIX = ".building-"


class State:
    """向导各页之间共享的数据。"""

    def __init__(self):
        self.photo = ""
        self.name = ""
        self.model = "u2net"
        self.pet_dir = ""
        self.assets: list[dict] = []      # {path, state, status}
        self.extract_audio = True
        self.make_pack = False
        self.pack_path = ""


def _icon() -> QIcon:
    p = os.path.join(ROOT, "runtime", "icon.png")   # 打进 exe 的只读资源
    return QIcon(p) if os.path.exists(p) else QIcon()


def _open_path(path: str):
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))


# ------------------------------------------------------------------ ① 选照片
class PhotoPage(QWizardPage):
    def __init__(self, st: State):
        super().__init__()
        self.st = st
        self.setTitle("① 选一张照片")
        self.setSubTitle("用自己的照片、家里毛孩子的照片、或者任何你喜欢的角色都行。\n"
                         "背景杂乱也没关系，下一步会自动抠掉。")
        self.setAcceptDrops(True)

        lay = QVBoxLayout(self)
        self.preview = QLabel("把照片拖到这里，或者点下面的按钮选一张")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(220)
        self.preview.setFrameShape(QFrame.Shape.StyledPanel)
        self.preview.setStyleSheet("color:#888; background:#fafafa; border-radius:8px;")
        lay.addWidget(self.preview)

        row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("还没选照片")
        self.path_edit.setReadOnly(True)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path_edit)
        row.addWidget(browse)
        lay.addLayout(row)
        self.path_edit.textChanged.connect(self.completeChanged.emit)

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "选一张照片", os.path.expanduser("~"),
            "图片 (*.jpg *.jpeg *.png *.webp *.bmp)")
        if p:
            self._set_photo(p)

    def _set_photo(self, path: str):
        self.st.photo = path
        self.path_edit.setText(path)
        pm = QPixmap(path)
        if not pm.isNull():
            self.preview.setPixmap(pm.scaled(
                self.preview.width() or 420, 260,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        else:
            self.preview.setText("这张图读不出来，换一张试试？")
        self.completeChanged.emit()

    # 支持直接把图片拖进来
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if p.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp")):
                self._set_photo(p)
                break

    def isComplete(self):
        return bool(self.st.photo and os.path.exists(self.st.photo))


# ------------------------------------------------------------------ ② 起名字
class NamePage(QWizardPage):
    def __init__(self, st: State):
        super().__init__()
        self.st = st
        self.setTitle("② 给它起个名字")
        self.setSubTitle("名字会显示在托盘菜单里，也用来当资源包的文件夹名。")

        lay = QVBoxLayout(self)
        self.name_edit = QLineEdit("我的桌宠")
        self.name_edit.textChanged.connect(self._changed)
        lay.addWidget(QLabel("名字："))
        lay.addWidget(self.name_edit)

        lay.addSpacing(12)
        lay.addWidget(QLabel("抠图模型："))
        self.group = QButtonGroup(self)
        for i, (mid, desc) in enumerate(MODELS):
            rb = QRadioButton(f"{mid} —— {desc}")
            rb.setChecked(mid == "u2net")
            rb.toggled.connect(self._changed)
            self.group.addButton(rb, i)
            lay.addWidget(rb)

        self.hint = QLabel()
        self.hint.setStyleSheet("color:#666;")
        self.hint.setWordWrap(True)
        lay.addSpacing(12)
        lay.addWidget(self.hint)
        lay.addStretch(1)

    def _changed(self):
        self.st.name = self.name_edit.text().strip()
        btn = self.group.checkedId()
        if btn >= 0:
            self.st.model = MODELS[btn][0]
        import pet_pack
        safe = pet_pack.safe_name(self.st.name) if self.st.name else ""
        exists = bool(safe) and os.path.isdir(os.path.join(pets_root(), safe))
        msg = f"会保存到：{os.path.join(pets_root(), safe)}" if safe else ""
        if exists:
            msg += "\n⚠ 已经有同名的资源包了，继续的话会覆盖它"
        self.hint.setText(msg)
        self.completeChanged.emit()

    def isComplete(self):
        import pet_pack
        return bool(self.st.name and pet_pack.safe_name(self.st.name) == self.st.name)


# ------------------------------------------------------------------ ③ 生成形象
class GeneratePage(QWizardPage):
    finished_one = pyqtSignal(bool, str, str)     # ok, 错误信息, pet_dir

    def __init__(self, st: State):
        super().__init__()
        self.st = st
        self.setTitle("③ 生成形象")
        self.setSubTitle("抠掉背景，并派生出待机 / 点击 / 睡觉三个状态。\n"
                         "第一次会下载模型，可能要等一会儿。")
        self.finished_one.connect(self._on_done)

        lay = QVBoxLayout(self)
        self.status = QLabel("点下面的按钮开始")
        lay.addWidget(self.status)

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)                    # 不确定进度(抠图没有进度回调)
        self.bar.setVisible(False)
        lay.addWidget(self.bar)

        self.go = QPushButton("开始生成")
        self.go.clicked.connect(self._start)
        lay.addWidget(self.go)

        self.previews = QHBoxLayout()
        self.captions = {}
        for key, label in (("idle", "待机"), ("click", "点击"), ("sleep", "睡觉")):
            box = QVBoxLayout()
            pic = QLabel("—")
            pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pic.setMinimumSize(120, 140)
            pic.setFrameShape(QFrame.Shape.StyledPanel)
            pic.setStyleSheet("background:#fafafa; border-radius:6px;")
            cap = QLabel(label)
            cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
            box.addWidget(pic)
            box.addWidget(cap)
            self.captions[key] = pic
            self.previews.addLayout(box)
        lay.addLayout(self.previews)
        lay.addStretch(1)

    def _start(self):
        self.go.setEnabled(False)
        self.bar.setVisible(True)
        self.status.setText("正在抠图…（第一次要下载模型，几分钟都算正常）")
        # 假的进度文案, 让人知道它没死
        self._tips = ["正在抠图…", "正在清理边缘…", "正在派生三个状态…",
                      "马上就好…"]
        self._tip_i = 0
        self._tip_timer = QTimer(self)
        self._tip_timer.timeout.connect(self._next_tip)
        self._tip_timer.start(6000)

        st = self.st
        import pet_pack
        # 先做在"暂存目录"里, 点完成时才挪成正式资源包。
        # 否则桌宠的目录监视会在向导才做到一半时就把这只宠物冒出来,
        # 而且是缺动画的半成品。
        staging_dir = os.path.join(pets_root(), STAGING_PREFIX + pet_pack.safe_name(st.name))
        out = staging_dir

        def work():
            try:
                import photo_to_pet
                photo_to_pet.generate(st.photo, os.path.basename(out),
                                      out_root=pets_root(),
                                      model=st.model)
                self.finished_one.emit(True, "", out)
            except Exception as e:                       # noqa: BLE001
                self.finished_one.emit(False, f"{type(e).__name__}: {e}", "")

        threading.Thread(target=work, daemon=True).start()

    def _next_tip(self):
        self._tip_i += 1
        if self._tip_i < len(self._tips):
            self.status.setText(self._tips[self._tip_i])

    def _on_done(self, ok: bool, err: str, pet_dir: str):
        self._tip_timer.stop()
        self.bar.setVisible(False)
        self.go.setEnabled(True)
        if not ok:
            self.go.setText("重试")
            self.status.setText("生成失败：" + err)
            QMessageBox.warning(
                self, "生成失败",
                f"{err}\n\n可以试试：\n"
                "· 换成 u2net 模型（更小、下载更稳）\n"
                "· 先跑 python scripts/download_model.py --model u2net\n"
                "· 确认网络能访问模型下载源")
            return
        self.st.pet_dir = pet_dir
        self.go.setText("重新生成")
        self.status.setText(f"生成好了！保存在 {pet_dir}")
        for key, pic in self.captions.items():
            p = os.path.join(pet_dir, "avatar", f"{key}.png")
            pm = QPixmap(p)
            if not pm.isNull():
                pic.setPixmap(pm.scaled(120, 140,
                                        Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation))
        self.completeChanged.emit()

    def isComplete(self):
        return bool(self.st.pet_dir and os.path.isdir(self.st.pet_dir))


# ------------------------------------------------------------------ ④ 动作素材
class AssetPage(QWizardPage):
    # (ok, 结果信息) —— 在工作线程里发, 主线程收
    one_done = pyqtSignal(str, bool, str)
    batch_done = pyqtSignal()
    progress = pyqtSignal(str)          # 转换过程中的进度文案

    def __init__(self, st: State):
        super().__init__()
        self.st = st
        self.setTitle("④ 加动作素材（可选）")
        self.setSubTitle("把用即梦/豆包生成的视频或图片拖进来，它就会动起来。\n"
                         "也可以跳过 —— 只有静态图也能用，只是不会动。")
        self.setAcceptDrops(True)
        self.one_done.connect(self._on_one)
        self.batch_done.connect(self._on_batch)
        self.progress.connect(self._on_progress)

        lay = QVBoxLayout(self)
        tip = QLabel("文件名里带「打哈欠/纯睡觉/进食/轻戳/待机」这类词时，"
                     "状态会自动认出来；认不出就在右边自己选。")
        tip.setStyleSheet("color:#666;")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        lay.addWidget(self.list)

        row = QHBoxLayout()
        add = QPushButton("添加视频 / 图片…")
        add.clicked.connect(self._browse)
        rm = QPushButton("移除选中")
        rm.clicked.connect(self._remove_selected)
        clear = QPushButton("清空")
        clear.clicked.connect(self._clear)
        row.addWidget(add)
        row.addWidget(rm)
        row.addWidget(clear)
        lay.addLayout(row)

        self.audio_cb = QCheckBox("顺便从视频里抽出音效（推荐）")
        self.audio_cb.setChecked(True)
        self.audio_cb.toggled.connect(lambda v: setattr(self.st, "extract_audio", v))
        lay.addWidget(self.audio_cb)

        self.go = QPushButton("开始导入")
        self.go.clicked.connect(self._start)
        lay.addWidget(self.go)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        lay.addStretch(1)

    # --- 列表管理 ---
    def _add_file(self, path: str, state: str | None = None):
        if not state:
            try:
                import extract_audio
                state = extract_audio.guess_state(os.path.basename(path)) or ""
            except Exception:                                # noqa: BLE001
                state = ""
        item = QListWidgetItem()
        w = QFrame()
        h = QHBoxLayout(w)
        h.setContentsMargins(4, 2, 4, 2)
        h.addWidget(QLabel(os.path.basename(path)), 1)
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItems(KNOWN_STATES)
        combo.setCurrentText(state)
        combo.setFixedWidth(120)
        h.addWidget(QLabel("状态:"))
        h.addWidget(combo)
        item.setSizeHint(w.sizeHint())
        self.list.addItem(item)
        self.list.setItemWidget(item, w)
        self.st.assets.append({"path": path, "state": state, "status": "",
                               "row": item, "combo": combo})

    def _browse(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选动作素材", os.path.expanduser("~"),
            "视频或图片 (*.mp4 *.mov *.avi *.webm *.mkv *.png *.jpg *.jpeg *.webp)")
        for f in files:
            self._add_file(f)

    def _remove_selected(self):
        for item in self.list.selectedItems():
            row = self.list.row(item)
            self.list.takeItem(row)
            self.st.assets = [a for a in self.st.assets if a.get("row") is not item]

    def _clear(self):
        self.list.clear()
        self.st.assets.clear()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if p.lower().endswith((".mp4", ".mov", ".avi", ".webm", ".mkv",
                                   ".png", ".jpg", ".jpeg", ".webp")):
                self._add_file(p)

    # --- 执行 ---
    def _start(self):
        if not self.st.assets:
            QMessageBox.information(self, "还没加素材", "先添加几个视频或图片吧。")
            return
        for a in self.st.assets:
            a["state"] = a["combo"].currentText().strip().lower() or "idle"
            a["status"] = "等待中"
        self.go.setEnabled(False)
        self.status.setText("开始导入…")

        def work():
            import add_asset
            ok_n = 0
            for a in self.st.assets:
                src, state = a["path"], a["state"]
                try:
                    ext = os.path.splitext(src)[1].lower()
                    if ext in add_asset.VIDEO_EXT:
                        ok = add_asset.install_video(
                            src, self.st.pet_dir, state,
                            on_progress=lambda m, f=os.path.basename(src):
                                self.progress.emit(f"{f}: {m}"))
                        if ok and self.st.extract_audio:
                            self._extract_audio(src, state)
                    elif ext in add_asset.IMG_EXT:
                        ok = add_asset.install_image(src, self.st.pet_dir, state)
                    else:
                        ok = False
                    self.one_done.emit(src, bool(ok), "" if ok else "格式不支持")
                    ok_n += 1 if ok else 0
                except Exception as e:                       # noqa: BLE001
                    self.one_done.emit(src, False, f"{type(e).__name__}: {e}")
            self.batch_done.emit()

        threading.Thread(target=work, daemon=True).start()

    def _extract_audio(self, video: str, state: str):
        """从动画视频里抽音轨, 存成 sounds/<状态>.mp3。"""
        try:
            import extract_audio
            import imageio_ffmpeg
            import numpy as np                      # noqa: F401
            sounds = os.path.join(self.st.pet_dir, "sounds")
            os.makedirs(sounds, exist_ok=True)
            tmp = os.path.join(sounds, f".{state}.tmp.wav")
            ff = imageio_ffmpeg.get_ffmpeg_exe()
            if not extract_audio.extract_wav(ff, video, tmp):
                return
            peak = extract_audio.normalize(tmp)
            if peak is None:
                os.remove(tmp)
                return                          # 这段视频没声音, 跳过
            extract_audio.to_mp3(ff, tmp, os.path.join(sounds, state + ".mp3"))
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:                           # noqa: BLE001
            pass                                    # 抽音效失败不影响动画

    def _on_progress(self, text: str):
        self.status.setText(text)

    def _on_one(self, src: str, ok: bool, msg: str):
        for a in self.st.assets:
            if a["path"] == src:
                a["status"] = "✓" if ok else f"✗ {msg}"
                w = self.list.itemWidget(a["row"])
                if w:
                    w.setStyleSheet("" if ok else "background:#ffecec;")
                break
        self.status.setText("导入中… " + "、".join(
            f"{os.path.basename(a['path'])} {a['status']}" for a in self.st.assets))

    def _on_batch(self):
        self.go.setEnabled(True)
        good = sum(1 for a in self.st.assets if a["status"].startswith("✓"))
        self.status.setText(f"完成：{good}/{len(self.st.assets)} 个素材导入成功。"
                            "（失败的通常是缺 imageio-ffmpeg，见仓库 README）")

    def isComplete(self):
        return True                                # 这步可以跳过


# ------------------------------------------------------------------ ⑤ 完成
class FinishPage(QWizardPage):
    def __init__(self, st: State):
        super().__init__()
        self.st = st
        self.setTitle("⑤ 完成")
        self.setSubTitle("桌宠做好啦！下面这些可以改，也可以直接点完成。")

        lay = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("background:#f7f7f7; padding:10px; border-radius:8px;")
        lay.addWidget(self.summary)

        lay.addWidget(QLabel("台词（一行一句，和它互动时随机说）："))
        self.dialogues = QPlainTextEdit()
        self.dialogues.setPlaceholderText("嗨，我是你的专属桌宠！\n记得休息一下眼睛哦~")
        self.dialogues.setMaximumHeight(96)
        lay.addWidget(self.dialogues)

        lay.addWidget(QLabel("人设（用 AI 聊天时它的性格设定）："))
        self.persona = QLineEdit()
        self.persona.setPlaceholderText("你是用户的可爱桌面宠物，说话简短俏皮…")
        lay.addWidget(self.persona)

        row = QHBoxLayout()
        self.pack_cb = QCheckBox("导出一个 .pet 文件（方便发给别人）")
        row.addWidget(self.pack_cb)
        lay.addLayout(row)

        self.launch_cb = QCheckBox("完成后启动桌宠（已经在运行的会自动出现）")
        self.launch_cb.setChecked(True)
        lay.addWidget(self.launch_cb)

        btns = QHBoxLayout()
        open_dir = QPushButton("打开宠物文件夹")
        open_dir.clicked.connect(self._open_dir)
        btns.addWidget(open_dir)
        btns.addStretch(1)
        lay.addLayout(btns)
        lay.addStretch(1)

    def initializePage(self):
        import pet_pack
        self.summary.setText(pet_pack.describe(self.st.pet_dir))
        if not self.dialogues.toPlainText():
            try:
                import json
                cfg = json.load(open(os.path.join(self.st.pet_dir, "pet.json"),
                                     encoding="utf-8"))
                self.dialogues.setPlainText("\n".join(cfg.get("dialogues", [])))
                self.persona.setText(cfg.get("persona", ""))
            except (OSError, ValueError):
                pass

    def _open_dir(self):
        if self.st.pet_dir:
            _open_path(self.st.pet_dir)

    def save_extras(self):
        """把台词/人设写回 pet.json。"""
        import json
        p = os.path.join(self.st.pet_dir, "pet.json")
        try:
            with open(p, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            return
        lines = [l.strip() for l in self.dialogues.toPlainText().splitlines()
                 if l.strip()]
        if lines:
            cfg["dialogues"] = lines
        if self.persona.text().strip():
            cfg["persona"] = self.persona.text().strip()
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass


# ------------------------------------------------------------------ 向导
class Wizard(QWizard):
    def __init__(self):
        super().__init__()
        self.st = State()
        self.setWindowTitle("PhotoPet 制作向导")
        self.setWindowIcon(_icon())
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.resize(680, 560)

        self.photo_page = PhotoPage(self.st)
        self.name_page = NamePage(self.st)
        self.gen_page = GeneratePage(self.st)
        self.asset_page = AssetPage(self.st)
        self.finish_page = FinishPage(self.st)
        for p in (self.photo_page, self.name_page, self.gen_page,
                  self.asset_page, self.finish_page):
            self.addPage(p)
        self.setButtonText(QWizard.WizardButton.FinishButton, "完成")
        self.setButtonText(QWizard.WizardButton.NextButton, "下一步 ▶")
        self.setButtonText(QWizard.WizardButton.BackButton, "◀ 上一步")
        self.setButtonText(QWizard.WizardButton.CancelButton, "取消")

    def _finalize(self) -> bool:
        """把暂存目录挪成正式资源包 (pets/<名字>)。返回是否成功。"""
        import json
        import shutil
        import pet_pack
        st = self.st
        target = os.path.join(pets_root(), pet_pack.safe_name(st.name))
        if os.path.abspath(target) == os.path.abspath(st.pet_dir):
            return True
        if os.path.exists(target):
            box = QMessageBox(QMessageBox.Icon.Warning, "同名冲突",
                              f"已经有 pets/{os.path.basename(target)} 了。\n\n"
                              "覆盖会**删掉它现有的养成存档**（好感度、陪伴时长）。"
                              "想保留就先取消，回上一步改个名字。")
            yes = box.addButton("覆盖", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is not yes:
                return False
            shutil.rmtree(target, ignore_errors=True)
        try:
            shutil.move(st.pet_dir, target)
        except OSError as e:
            QMessageBox.warning(self, "保存失败", f"{e}")
            return False
        st.pet_dir = target
        # 暂存期间 pet.json 里的名字是 .building-xxx, 改回真名
        p = os.path.join(target, "pet.json")
        try:
            cfg = json.load(open(p, encoding="utf-8"))
            cfg["name"] = st.name
            with open(p, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except (OSError, ValueError):
            pass
        return True

    def accept(self):
        """点完成: 落位 → 保存台词 → 导出 .pet(可选) → 启动桌宠(可选)。"""
        if not self._finalize():
            return                              # 用户取消了, 别关窗口
        self.finish_page.save_extras()
        import pet_pack
        if self.finish_page.pack_cb.isChecked():
            default = os.path.join(os.path.expanduser("~"),
                                   f"{pet_pack.safe_name(self.st.name)}.pet")
            path, _ = QFileDialog.getSaveFileName(
                self, "导出 .pet 到", default, "桌宠资源包 (*.pet)")
            if path:
                if not path.lower().endswith(".pet"):
                    path += ".pet"
                try:
                    out, n = pet_pack.export_pet(self.st.pet_dir, path)
                    QMessageBox.information(
                        self, "已导出",
                        f"{out}\n\n{n} 个文件。存档（好感度/陪伴时长）已剔除，"
                        "发给别人不会带上你的记录。")
                except (pet_pack.PetPackError, OSError) as e:
                    QMessageBox.warning(self, "导出失败", str(e))
        if self.finish_page.launch_cb.isChecked():
            self._launch()
        super().accept()

    def _launch(self):
        """启动桌宠。

        打包成"创作版" exe 时, 自己不是运行时 —— 要找同目录下的
        PhotoPet.exe 来启动; 找不到就只提示, 不当场炸。
        """
        if getattr(sys, "frozen", False):
            runtime_exe = os.path.join(os.path.dirname(sys.executable),
                                       "PhotoPet.exe")
            if os.path.exists(runtime_exe):
                try:
                    subprocess.Popen([runtime_exe, "--pet", self.st.pet_dir])
                    return
                except OSError:
                    pass
            QMessageBox.information(
                self, "做好啦",
                "宠物已保存到：\n" + self.st.pet_dir + "\n\n"
                "把 PhotoPet.exe（运行版）放到同一个文件夹，"
                "或者双击桌面上的 PhotoPet 图标就能看到它。")
            return
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        runner = pythonw if os.path.exists(pythonw) else sys.executable
        try:
            subprocess.Popen([runner, os.path.join(ROOT, "runtime", "main.py"),
                              "--pet", self.st.pet_dir], cwd=ROOT)
        except OSError:
            pass


def _clean_stale_staging():
    """清掉上次向导中途退出留下的暂存目录 (超过 10 分钟的才算)。"""
    import shutil
    pets = pets_root()
    if not os.path.isdir(pets):
        return
    now = time.time()
    for name in os.listdir(pets):
        if not name.startswith(STAGING_PREFIX):
            continue
        p = os.path.join(pets, name)
        try:
            if now - os.path.getmtime(p) > 600:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


def _ensure_model() -> bool:
    """把打进 exe 的抠图模型放到 rembg 会去找的位置。

    创作版 exe 里自带 u2net.onnx (167MB), 这样用户第一次抠图不用等下载 ——
    国内直连 GitHub 下模型很痛苦, 这也是 scripts/download_model.py 存在的原因。
    rembg 只认 ~/.u2net/u2net.onnx, 所以这里复制过去 (已经有一份就不动)。
    """
    import shutil
    bundled = os.path.join(ROOT, "models", "u2net.onnx")
    if not os.path.exists(bundled):
        return False                       # 源码运行: 让 rembg 自己去下/用已有的
    target_dir = os.path.join(os.path.expanduser("~"), ".u2net")
    target = os.path.join(target_dir, "u2net.onnx")
    if os.path.exists(target) and os.path.getsize(target) >= 160 * 1024 * 1024:
        return True
    try:
        os.makedirs(target_dir, exist_ok=True)
        shutil.copyfile(bundled, target)
        return True
    except OSError:
        return False


def _selftest() -> int:
    """不开界面, 直接跑一遍"抠图 → 生成三态", 用来验证打包后的 exe 真能干活。

    用法:  PhotoPet-Creator.exe --selftest
    """
    import tempfile
    tmp = tempfile.mkdtemp(prefix="photopet_selftest_")
    try:
        from PIL import Image, ImageDraw
        photo = os.path.join(tmp, "test.png")
        im = Image.new("RGB", (400, 400), (90, 200, 110))     # 纯色背景
        d = ImageDraw.Draw(im)
        d.ellipse([120, 60, 280, 340], fill=(220, 60, 60))    # 一个"主体"
        im.save(photo)
        if _ensure_model():
            print("[自检] 抠图模型就绪")
        import photo_to_pet
        out = os.path.join(tmp, "pets")
        photo_to_pet.generate(photo, "selftest", out_root=out, model="u2net")
        for name in ("idle", "click", "sleep"):
            p = os.path.join(out, "selftest", "avatar", f"{name}.png")
            if not os.path.exists(p):
                print(f"[自检] [X] 缺少 {name}.png")
                return 1
        # 确认真的抠掉了背景 (四角应该透明)
        a = Image.open(os.path.join(out, "selftest", "avatar", "idle.png")).convert("RGBA")
        corners = [a.getpixel((1, 1)), a.getpixel((a.width - 2, 1)),
                   a.getpixel((1, a.height - 2)), a.getpixel((a.width - 2, a.height - 2))]
        if all(c[3] < 20 for c in corners):
            print("[自检] [OK] 抠图成功 (四角透明), 三态齐全")
            return 0
        print(f"[自检] [X] 背景没抠干净, 四角 alpha = {[c[3] for c in corners]}")
        return 1
    except Exception as e:                                  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"[自检] [X] 失败: {type(e).__name__}: {e}")
        return 1
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    app_paths.fix_console_encoding()
    if "--selftest" in sys.argv:
        return _selftest()
    try:
        import photo_to_pet  # noqa: F401  提前确认依赖在, 免得走到第③步才报错
    except ImportError as e:
        print(f"缺少制作所需的依赖: {e}")
        print("请先安装:  pip install -r requirements.txt")
        return 1
    app = QApplication(sys.argv)
    app.setApplicationName("PhotoPet 向导")
    _ensure_model()
    _clean_stale_staging()
    w = Wizard()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
