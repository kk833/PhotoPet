# -*- coding: utf-8 -*-
"""从素材视频里抽取音轨，做成资源包的语音包 (sounds/)。

你让 AI 生成的动画视频通常自带音轨（哈欠声、咀嚼声、戳一下的反应声）。
本脚本把音轨抽出来、统一响度，按状态名存成 sounds/<状态>.mp3，
桌宠切到该状态时就会播放 —— 不需要另外去找音效。

用法:
    python scripts/extract_audio.py --list                  # 先看会抽出什么，不写文件
    python scripts/extract_audio.py --pet pets/mypet        # 抽取并安装
    python scripts/extract_audio.py --pet pets/mypet --src 素材库
    python scripts/extract_audio.py --map "我的视频.mp4:sleep" --pet pets/mypet

依赖: imageio-ffmpeg (自带 ffmpeg 可执行文件), numpy
      pip install imageio-ffmpeg numpy
"""
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 中文控制台是 GBK: 打印 emoji 会抛 UnicodeEncodeError, 而且往往是在活儿都干完之后
# 的最后一行提示上抛 —— 看起来像"失败了"。这一层顺带兜住另一种情况: 打印的内容里有
# 生僻字或 emoji 文件名。
sys.path.insert(0, os.path.join(ROOT, "runtime"))
import app_paths  # noqa: E402
app_paths.fix_console_encoding()

VIDEO_EXT = (".mp4", ".mov", ".avi", ".webm", ".mkv")
TARGET_PEAK = 0.85          # 统一响度后的目标峰值, 留点余量避免削波
SILENT_PEAK = 0.05          # 峰值低于此值视为"这段视频本来就没声音"
MAX_GAIN = 6.0              # 放大倍数上限, 免得把底噪也放成噪音

# 按关键词猜状态。注意顺序: 先匹配更具体的 sleep_loop, 再匹配 sleep。
# "睡觉循环" 太泛(打哈欠的视频名里也有), 所以 sleep_loop 只认 "纯睡觉"
STATE_KEYWORDS = [
    ("sleep_loop", ("纯睡觉", "睡眠循环", "睡loop")),
    ("sleep", ("打哈欠", "哈欠", "入睡")),
    ("eat", ("进食", "吃饭", "咀嚼", "投喂")),
    ("click", ("轻戳", "戳", "点击", "点一下")),
    ("pet", ("抚摸", "摸摸")),
    ("idle", ("待机", "idle")),
]


def guess_state(filename: str) -> str | None:
    low = filename.lower()
    for state, keys in STATE_KEYWORDS:
        if any(k.lower() in low for k in keys):
            return state
    return None


def extract_wav(ffmpeg: str, src: str, dst: str) -> bool:
    r = subprocess.run(
        [ffmpeg, "-y", "-i", src, "-vn", "-ac", "1", "-ar", "22050", dst,
         "-hide_banner", "-loglevel", "error"],
        capture_output=True, text=True, errors="replace")
    return os.path.exists(dst) and os.path.getsize(dst) > 1000


def normalize(wav_path: str) -> float | None:
    """统一响度到 TARGET_PEAK, 返回原始峰值; 静音返回 None。"""
    import wave
    import numpy as np
    with wave.open(wav_path) as w:
        sr, n = w.getframerate(), w.getnframes()
        data = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
    if data.size == 0:
        return None
    peak = float(np.abs(data).max())
    if peak < SILENT_PEAK:
        return None
    gain = min(TARGET_PEAK / peak, MAX_GAIN)
    out = np.clip(data * gain, -1.0, 1.0)
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((out * 32767).astype(np.int16).tobytes())
    return peak


def to_mp3(ffmpeg: str, wav: str, dst: str) -> bool:
    """转 mp3; 该 ffmpeg 没有 mp3 编码器时退回 wav (桌宠两种都支持)。"""
    r = subprocess.run(
        [ffmpeg, "-y", "-i", wav, "-codec:a", "libmp3lame", "-b:a", "128k",
         dst, "-hide_banner", "-loglevel", "error"],
        capture_output=True, text=True, errors="replace")
    if os.path.exists(dst) and os.path.getsize(dst) > 500:
        return True
    fallback = os.path.splitext(dst)[0] + ".wav"
    shutil.copyfile(wav, fallback)
    print(f"    (没有 mp3 编码器, 已存为 {os.path.basename(fallback)})")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pet", default=None, help="资源包目录, 如 pets/mypet")
    ap.add_argument("--src", default=os.path.join(ROOT, "素材库"),
                    help="放着视频的目录 (默认 素材库/)")
    ap.add_argument("--map", action="append", default=[],
                    help="手动指定 视频文件:状态名, 可重复")
    ap.add_argument("--list", action="store_true", help="只列出会做什么, 不写文件")
    ap.add_argument("--trim", type=float, default=0.0,
                    help="只取前 N 秒 (默认整段)")
    args = ap.parse_args()

    try:
        import imageio_ffmpeg
        import numpy  # noqa: F401
    except ImportError as e:
        print(f"缺依赖: {e}\n  pip install imageio-ffmpeg numpy")
        return 1
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    if not args.pet:
        pets = os.path.join(ROOT, "pets")
        found = [d for d in sorted(os.listdir(pets))
                 if os.path.isdir(os.path.join(pets, d))] if os.path.isdir(pets) else []
        if len(found) == 1:
            args.pet = os.path.join(pets, found[0])
        else:
            print("请用 --pet 指定资源包目录 (pets/ 下有多个)" if found
                  else "没找到资源包, 先用 photo_to_pet.py 生成一个")
            return 1

    # 收集 视频 -> 状态 的对应关系
    jobs = []
    for spec in args.map:
        if ":" not in spec:
            print(f"--map 格式应为 视频文件:状态名, 收到 {spec!r}")
            return 1
        name, state = spec.rsplit(":", 1)
        p = name if os.path.isabs(name) else os.path.join(args.src, name)
        jobs.append((p, state.strip()))
    for fn in sorted(os.listdir(args.src)) if os.path.isdir(args.src) else []:
        if not fn.lower().endswith(VIDEO_EXT):
            continue
        p = os.path.join(args.src, fn)
        if any(os.path.abspath(p) == os.path.abspath(j[0]) for j in jobs):
            continue
        st = guess_state(fn)
        if st:
            jobs.append((p, st))

    if not jobs:
        print(f"{args.src} 里没有能识别出状态的视频。")
        print("用 --map \"视频文件名.mp4:sleep\" 手动指定。")
        return 1

    if not args.list:
        os.makedirs(os.path.join(args.pet, "sounds"), exist_ok=True)
    print(f"目标资源包: {args.pet}\n")
    tmp = os.path.join(ROOT, ".audio_tmp.wav")
    installed = skipped = 0
    done: dict[str, str] = {}          # 状态 -> 已采用的视频, 避免互相覆盖
    for src, state in jobs:
        if not os.path.exists(src):
            print(f"  {state:<12} 跳过: 找不到 {src}")
            continue
        if state in done:
            print(f"  {state:<12} 跳过: 已用 {done[state]}, "
                  f"多个视频对应同一状态时用 --map 指定")
            continue
        if args.trim > 0:
            dst_wav = tmp
            subprocess.run([ffmpeg, "-y", "-i", src, "-vn", "-t", str(args.trim),
                            "-ac", "1", "-ar", "22050", dst_wav,
                            "-hide_banner", "-loglevel", "error"],
                           capture_output=True)
        else:
            dst_wav = tmp
            if not extract_wav(ffmpeg, src, dst_wav):
                print(f"  {state:<12} 抽取失败: {os.path.basename(src)}")
                continue
        peak = normalize(dst_wav)
        if peak is None:
            print(f"  {state:<12} 跳过: 这段视频没有音轨内容 (峰值太低, 只有底噪)")
            skipped += 1
            continue
        if args.list:
            print(f"  {state:<12} <- {os.path.basename(src)}  (峰值 {peak:.3f})")
            continue
        dst = os.path.join(args.pet, "sounds", state + ".mp3")
        to_mp3(ffmpeg, dst_wav, dst)
        size = os.path.getsize(os.path.splitext(dst)[0] +
                               (".mp3" if os.path.exists(dst) else ".wav"))
        print(f"  [OK] {state:<12} <- {os.path.basename(src)}  "
              f"(峰值 {peak:.3f} -> {TARGET_PEAK}, {size/1024:.0f} KB)")
        done[state] = os.path.basename(src)
        installed += 1
    if os.path.exists(tmp):
        os.remove(tmp)

    if args.list:
        print("\n加 --pet 参数即可真正写入。")
        return 0
    print(f"\n完成: 安装 {installed} 个, 跳过 {skipped} 个")
    print("重启桌宠, 点一下宠物 / 投喂 / 睡觉 就能听到。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
