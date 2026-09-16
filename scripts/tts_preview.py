# -*- coding: utf-8 -*-
"""语音试听工具 —— 挑一个好听的声音，不用改代码。

用法:
    python scripts/tts_preview.py                      # 列出所有可选音色
    python scripts/tts_preview.py --voice Yunxi        # 试听 (可只写音色名的一部分)
    python scripts/tts_preview.py --all                # 依次试听全部音色, 挑着听
    python scripts/tts_preview.py --voice Xiaoyi --text "今天也要加油鸭" --rate 10
    python scripts/tts_preview.py --voice Xiaoyi --out 试听.mp3   # 只导出不播放

选好后把音色全名填进项目根目录 ai_config.json 的 tts_voice 即可。
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "runtime"))

# 中文控制台是 GBK: 打印 emoji 会抛 UnicodeEncodeError, 而且往往是在活儿都干完之后
# 的最后一行提示上抛 —— 看起来像"失败了"。统一转成 UTF-8 + 容错输出。
import app_paths  # noqa: E402
app_paths.fix_console_encoding()

from pet_ai import EDGE_VOICES, CACHE_DIR, _EdgeBackend  # noqa: E402

DEFAULT_TEXT = "嗨，我是你的专属桌宠！摸摸我会有惊喜哦，记得休息一下眼睛~"


def match_voice(needle: str) -> str | None:
    """按全名或一部分匹配音色 (不区分大小写)。"""
    if not needle:
        return None
    low = needle.lower()
    for full in EDGE_VOICES:
        if low == full.lower():
            return full
    hits = [f for f in EDGE_VOICES if low in f.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        print(f"没有匹配 {needle!r} 的音色。用 --list 看全部可选值。")
        return None
    print(f"{needle!r} 匹配到多个: {', '.join(hits)}")
    return None


def play(path: str) -> None:
    """用 Qt 播放 (和桌宠运行时同一套播放器, 听感一致)。"""
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QUrl, QTimer
    from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput

    app = QApplication.instance() or QApplication([])
    player = QMediaPlayer()
    out = QAudioOutput()
    out.setVolume(0.9)
    player.setAudioOutput(out)
    player.mediaStatusChanged.connect(
        lambda st: app.quit()
        if st == QMediaPlayer.MediaStatus.EndOfMedia else None)
    player.setSource(QUrl.fromLocalFile(os.path.abspath(path)))
    player.play()
    QTimer.singleShot(20000, app.quit)   # 兜底: 别卡死
    app.exec()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", help="音色全名或一部分, 如 Xiaoyi / 陕西 / zh-HK")
    ap.add_argument("--text", default=DEFAULT_TEXT, help="要念的台词")
    ap.add_argument("--rate", type=int, default=0, help="语速偏移百分比")
    ap.add_argument("--out", help="导出到指定文件 (给 .mp3 会连同 .txt 台词一起存)")
    ap.add_argument("--all", action="store_true", help="依次试听全部音色")
    ap.add_argument("--list", action="store_true", help="只列出音色")
    args = ap.parse_args()

    if args.list or (not args.voice and not args.all):
        print("可选音色 (填进 ai_config.json 的 tts_voice):\n")
        for full, label in EDGE_VOICES.items():
            print(f"  {full:<32} {label}")
        print("\n方言: 东北话 liaoning-Xiaobei / 陕西话 shaanxi-Xiaoni / 粤语 zh-HK-*")
        print("更多音色: edge-tts --list-voices | findstr zh")
        print("\n试听: python scripts/tts_preview.py --voice Xiaoyi")
        return 0

    targets = list(EDGE_VOICES) if args.all else [match_voice(args.voice)]
    if args.all:
        print(f"依次试听 {len(targets)} 个音色 (Ctrl+C 可中断)\n")

    for full in targets:
        if not full:
            return 1
        label = EDGE_VOICES.get(full, full)
        print(f">> {label}  ({full})")
        be = _EdgeBackend(full, args.rate)
        if not be.prepare():
            print("  缺少 edge-tts, 先: pip install edge-tts")
            return 1
        path = be.synth(args.text)
        if not path:
            print("  合成失败 (断网? 音色名不对? 用 --list 确认)")
            continue
        if args.out:
            import shutil
            dst = os.path.abspath(args.out)
            shutil.copyfile(path, dst)
            with open(os.path.splitext(dst)[0] + ".txt", "w",
                      encoding="utf-8") as f:
                f.write(args.text)
            print(f"  已导出: {dst}")
            continue
        print(f"  缓存: {os.path.basename(path)}")
        play(path)
        if args.all:
            print()

    if not args.out:
        print(f"\n缓存目录: {CACHE_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
