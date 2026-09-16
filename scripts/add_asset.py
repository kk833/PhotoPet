# -*- coding: utf-8 -*-
"""
素材安装向导 —— 傻瓜式素材整合工具
用法（二选一）:
  1. 拖放: 把豆包/即梦生成的视频(mp4/mov)或图片(png/jpg)或音频(mp3/wav)
     直接拖到本文件图标上
  2. 命令行: python scripts\\add_asset.py <文件路径> <状态名>
     例: python scripts\\add_asset.py eat.mp4 eat
自动完成: 视频→GIF转换、缩放、命名、放入正确目录、校验生效。
依赖: 仅音频直接复制；视频转 GIF 需要可选项 Pillow（pip install pillow）,
      更完整的转换建议用 ezgif.com（脚本会给出直达链接）。
"""
import os
import sys
import shutil
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 中文控制台是 GBK: 打印 emoji 会抛 UnicodeEncodeError, 而且往往是在活儿都干完之后
# 的最后一行提示上抛 —— 看起来像"失败了"。统一转成 UTF-8 + 容错输出。这一层顺带
# 兜住另一种情况: 打印的内容里有生僻字或 emoji 文件名。
sys.path.insert(0, os.path.join(ROOT, "runtime"))
import app_paths  # noqa: E402
app_paths.fix_console_encoding()

VIDEO_EXT = (".mp4", ".mov", ".avi", ".webm", ".mkv")
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
AUD_EXT = (".mp3", ".wav", ".ogg")
GIF_MAX_W = 320
KNOWN_STATES = ["idle", "click", "eat", "sleep", "sleep_loop", "talk", "pet"]
# 会一直循环播放的状态: 这些的首尾接缝要缝好, 否则每转一圈跳一下。
# sleep.gif 不在此列 —— 它只播一遍然后交接给 sleep_loop, 首尾本来就不该一样。
LOOP_STATES = {"idle", "sleep_loop", "talk"}


def pick_pet_dir():
    pets = os.path.join(ROOT, "pets")
    if not os.path.isdir(pets):
        print("[X] 没找到 pets 目录，请先运行生成器创建桌宠")
        sys.exit(1)
    dirs = [d for d in os.listdir(pets)
            if os.path.isdir(os.path.join(pets, d))]
    if not dirs:
        print("[X] pets 下没有任何桌宠，先运行 photo_to_pet.py 生成")
        sys.exit(1)
    if len(dirs) == 1:
        return os.path.join(pets, dirs[0])
    print("检测到多个桌宠:")
    for i, d in enumerate(dirs, 1):
        print("  %d. %s" % (i, d))
    n = input("选择编号 (回车=1): ").strip() or "1"
    try:
        return os.path.join(pets, dirs[int(n) - 1])
    except (ValueError, IndexError):
        print("无效选择")
        sys.exit(1)


def install_video(src, pet_dir, state, on_progress=None):
    """视频 → GIF：背景自适应抠像 + 硬切自动抹平 + 缩放 + 15fps。

    v0.10.2 起不再只认绿幕:
    - 从画面四周**采样出背景色**, 按颜色距离生成软 alpha, 白底/蓝底/任何纯色都行
    - 只把**与画面边缘连通**的相似色区域当背景, 免得白衣服跟着白墙一起被抠掉
    - 背景不够干净（复杂背景）时自动改用 rembg 逐帧抠, 并做时域平滑抑制抖动
    - AI 视频里常见的**硬切**会被自动检测出来并用抖动溶解抹平（就是那种"抽搐一下"）

    on_progress(msg): 可选回调, 用于在界面上显示进度。
    需 imageio/imageio-ffmpeg/numpy/scipy/pillow（复杂背景还需要 rembg）。
    """
    anim = os.path.join(pet_dir, "animation")
    os.makedirs(anim, exist_ok=True)
    dest = os.path.join(anim, state + ".gif")

    def say(msg):
        print(msg)
        if on_progress:
            try:
                on_progress(msg)
            except Exception:                       # noqa: BLE001
                pass

    try:
        import imageio.v3 as iio
        import numpy as np
        from PIL import Image
        import video_ops as vo
    except ImportError:
        print("[!] 缺少转换依赖，自动转 GIF 需要:")
        print("    pip install imageio imageio-ffmpeg numpy scipy pillow")
        print("  安装后重跑本脚本即可。也可以手动用 https://ezgif.com 转换，")
        print("  下载后重命名为 %s.gif 放到 %s" % (state, anim))
        bak = os.path.join(anim, state + "_原始视频" + os.path.splitext(src)[1])
        shutil.copyfile(src, bak)
        print("  原视频已备份到 %s" % bak)
        return False

    # ---- 读帧 + 缩放 ----
    raw = list(iio.imiter(src))
    step = 2 if len(raw) <= 240 else max(2, len(raw) // 120)   # ~15fps, 长视频降采样
    picked = raw[::step]
    frames = []
    for fr in picked:
        im = Image.fromarray(fr).convert("RGB")
        # 用 int() 截断而不是 round(): 同一个资源包里所有状态动画的尺寸必须一模一样,
        # 而早先的素材是按截断生成的（720x960 -> 320x426, 而 round 会得到 427）。
        # 差这 1 像素, 切状态时窗口宽度就会差 1px。
        im = im.resize((GIF_MAX_W, max(1, int(im.height * GIF_MAX_W / im.width))),
                       Image.LANCZOS)
        frames.append(np.asarray(im, np.float32))
    rgb = np.stack(frames)
    say("读出 %d 帧（原 %d 帧）" % (len(frames), len(raw)))

    # ---- 抠像: 先用颜色抠一遍, 看结果再决定要不要动用 rembg ----
    bg = vo.sample_bg_color(rgb)
    say(vo.describe_background(rgb))
    alpha = vo.key_frames(rgb, bg)
    rgb = vo.remove_spill(rgb, alpha, bg)
    ok_chroma, removed = vo.chroma_usable(rgb, alpha, bg)
    if ok_chroma:
        say("背景是干净的纯色，颜色抠像直接搞定（边框抠净 %.0f%%）" % (removed * 100))
    else:
        say("颜色抠像只抠掉边框的 %.0f%%，背景应该比较复杂，"
            "改用 rembg 逐帧抠图（会慢一些）…" % (removed * 100))
        try:
            alpha2 = _key_with_rembg(rgb, say)
            ok2, removed2 = vo.chroma_usable(rgb, alpha2, bg)
            if removed2 >= removed:                 # 只在确实更好时才换
                alpha = alpha2
                say("rembg 抠图完成（边框抠净 %.0f%%）" % (removed2 * 100))
            else:
                say("rembg 结果还不如颜色抠像，保留原结果")
        except Exception as e:                      # noqa: BLE001
            say("rembg 不可用（%s），沿用颜色抠像" % type(e).__name__)

    # ---- 硬切: 在 RGB 上检测(避开逐帧抠图的 alpha 抖动), 再抹平整段 RGBA ----
    cuts = vo.detect_cuts(rgb)
    rgba = vo.to_rgba(rgb, alpha)
    if cuts:
        rgba, added = vo.smooth_all_cuts(rgba, cuts)
        say("检测到 %d 处硬切（镜头突变），已插入 %d 帧过渡抹平" % (len(cuts), added))
    else:
        say("没有检测到硬切")

    # ---- 循环接缝: 会一直循环的状态, 首尾对不上就会每次转圈跳一下 ----
    if state in LOOP_STATES:
        rgba, added = vo.close_loop_seam(rgba)
        if added:
            say("循环接缝明显，已在首尾之间插入 %d 帧过渡缝合" % added)
        else:
            say("循环接缝正常，不需要处理")

    # ---- 量化成 GIF ----
    pframes = []
    for f in rgba:
        im4 = Image.fromarray(f, "RGBA")
        a = np.asarray(im4.getchannel("A"))
        q = im4.convert("RGB").quantize(colors=255, method=Image.MEDIANCUT)
        arr = np.asarray(q).copy()
        arr[a < 100] = 255
        pf = Image.fromarray(arr, "P")
        pal = list(q.palette.palette)
        pal += [0, 0, 0] * (768 - len(pal))
        pal[765:768] = [0, 0, 0]
        pf.putpalette(pal[:768])
        pframes.append(pf)
    pframes[0].save(dest, save_all=True, append_images=pframes[1:],
                    duration=66, loop=0, transparency=255, disposal=2)
    say("✔ 视频已转 GIF: %s (%d 帧, %.0f KB)" %
        (dest, len(pframes), os.path.getsize(dest) / 1024))
    return True


def _key_with_rembg(frames_rgb, on_progress=None):
    """复杂背景: 逐帧交给 rembg, 再对 alpha 做时域中值, 抑制逐帧抖动。

    逐帧独立抠图会让边缘"沙沙"地闪 —— 因为每帧的 mask 互不知情。
    对相邻 3 帧取中值能把大部分闪烁压掉, 代价是动作边界略微变软。
    """
    import numpy as np
    from PIL import Image
    from rembg import new_session, remove

    session = new_session("u2net")
    alphas = []
    total = len(frames_rgb)
    for i, fr in enumerate(frames_rgb):
        out = remove(Image.fromarray(fr.astype(np.uint8)), session=session)
        alphas.append(np.asarray(out)[..., 3].astype(np.float32) / 255.0)
        if on_progress and i % 10 == 0:
            on_progress("rembg 抠图中… %d/%d" % (i + 1, total))
    a = np.stack(alphas)
    if len(a) >= 3:
        a = np.stack([np.median(a[max(0, i - 1):i + 2], axis=0)
                      for i in range(len(a))])
    return a


def install_image(src, pet_dir, state):
    anim = os.path.join(pet_dir, "animation")
    os.makedirs(anim, exist_ok=True)
    dest_png = os.path.join(anim, state + ".png")
    try:
        from PIL import Image
        im = Image.open(src).convert("RGBA")
        if im.width > GIF_MAX_W:
            im = im.resize((GIF_MAX_W, int(im.height * GIF_MAX_W / im.width)))
        im.save(dest_png)
        print("[OK] 静态图已安装: %s" % dest_png)
        return True
    except ImportError:
        shutil.copyfile(src, os.path.join(anim, state + os.path.splitext(src)[1]))
        print("[OK] 已复制到 %s (建议 pip install pillow 以自动缩放)" % anim)
        return True


def install_audio(src, pet_dir, state):
    snd = os.path.join(pet_dir, "sounds")
    os.makedirs(snd, exist_ok=True)
    ext = os.path.splitext(src)[1].lower()
    dest = os.path.join(snd, state + ext)
    shutil.copyfile(src, dest)
    print("[OK] 音频已安装: %s" % dest)
    return True


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print("用法: 把素材文件拖到本脚本图标上，或:")
        print("  python scripts\\add_asset.py <文件> [状态名]")
        print("可选状态: %s（其他名字也可以，作为自定义状态）" % ", ".join(KNOWN_STATES))
        sys.exit(0 if os.environ.get("PAUSE_ONLY") else 1)
    src = args[0]
    if not os.path.isfile(src):
        print("[X] 文件不存在: %s" % src)
        sys.exit(1)
    state = args[1] if len(args) > 1 else input(
        "这个素材对应什么状态? (%s): " % ", ".join(KNOWN_STATES)).strip().lower()
    if not state:
        state = "idle"
    pet_dir = pick_pet_dir()
    ext = os.path.splitext(src)[1].lower()
    if ext in VIDEO_EXT:
        ok = install_video(src, pet_dir, state)
    elif ext in IMG_EXT:
        ok = install_image(src, pet_dir, state)
    elif ext in AUD_EXT:
        ok = install_audio(src, pet_dir, state)
    else:
        print("[X] 不支持的格式: %s (支持视频/图片/音频)" % ext)
        sys.exit(1)
    print()
    print("完成！重启桌宠生效:")
    print("  python runtime\\main.py --pet pets\\%s" % os.path.basename(pet_dir))
    if ok and ext in VIDEO_EXT + IMG_EXT:
        print("提示: 若转换后背景不是透明的，用 ezgif.com 的绿幕/透明工具处理")
        print("      原始文件再重新跑一次本脚本即可。")
    if os.name == "nt" and os.environ.get("PAUSE_ONLY"):
        input("按回车关闭...")


if __name__ == "__main__":
    main()
