# -*- coding: utf-8 -*-
"""从现有资源包生成一份"可以进 git 仓库"的示例资源包 (v0.9)。

为什么需要它:
1. 示例包要跟着仓库发布, 所以**不能带存档**(status.json / work.json) —— 那是个人数据
2. 原始动画一套 16MB 上下, 放进 git 仓库会让 clone 变慢。这里压到一半左右:
   - 宽度降到 240px: 运行时显示高度上限是 base_height(160) × zoom(1.8) = 288px,
     而 240 宽对应的动画高是 319px —— **任何缩放下都不会被放大**, 等于无损
   - 颜色数降到 64: 扁平卡通用 64 色几乎看不出差别 (实测与原始的平均可见差 1.1,
     而动画自身的帧间变化是 6.6)
3. 顺便把 pet.json 里的文案打磨一下, 让示例包更像个"作品"

用法:
    python scripts/make_demo_pack.py                          # mypet -> pets/demo
    python scripts/make_demo_pack.py --src pets/mypet --out pets/demo
    python scripts/make_demo_pack.py --width 320 --colors 128  # 要更高画质就调大
"""
import argparse
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 中文控制台是 GBK: 打印 emoji 会抛 UnicodeEncodeError, 而且往往是在活儿都干完之后
# 的最后一行提示上抛 —— 看起来像"失败了"。统一转成 UTF-8 + 容错输出。
sys.path.insert(0, os.path.join(ROOT, "runtime"))
import app_paths  # noqa: E402
app_paths.fix_console_encoding()

KEEP_FILES = ("pet.json",)
STRIP_FILES = ("status.json", "work.json")          # 存档绝不能进仓库
SOUND_EXT = (".mp3", ".wav", ".ogg")

DEMO_PET_JSON = {
    "name": "小陪",
    "size": [160, 160],
    "base_height": 160,
    "persona": ("你是用户的桌面宠物小陪，说话简短俏皮、温柔陪伴，"
                "喜欢用颜文字，会关心用户有没有按时休息。"),
    "dialogues": [
        "嗨，我是小陪，以后就住你桌面上啦~",
        "记得多喝水哦，我去给你倒（假装）",
        "今天也要加油鸭！我在这儿陪着你",
        "摸摸我会有惊喜哦",
        "写累了就看看窗外，20 秒就好~",
    ],
    "ai_action_map": {
        "开心|高兴|哈哈|喜欢|好耶": "click",
        "睡觉|晚安|困|休息": "sleep",
        "你好|嗨|早上好|在吗": "idle",
    },
    # 人物档案示例 (v0.11): 不写 meet_date, 让"陪伴第 N 天"自动走 work.json 的
    # first_day —— 示例包自己就说明了"相识日不填也有"
    "profile": {
        "title": "你的专属桌宠",
        "birthday": "03-14",
        "intro": "从一张照片里长出来的小家伙，喜欢待在你屏幕的角落。",
    },
}


def compress_gif(src: str, dst: str, width: int, colors: int) -> tuple[int, int]:
    """重编码 GIF: 缩宽 + 减色, 保留帧数与时长, 维持 1-bit 透明。返回 (原大小, 新大小)。"""
    from PIL import Image, ImageSequence
    import numpy as np

    before = os.path.getsize(src)          # 必须在覆盖保存**之前**读, 否则读到的是新文件
    im = Image.open(src)
    frames = [f.convert("RGBA").copy() for f in ImageSequence.Iterator(im)]
    duration = im.info.get("duration", 80)
    out = []
    for fr in frames:
        if width and fr.width != width:
            fr = fr.resize((width, max(1, round(fr.height * width / fr.width))),
                           Image.LANCZOS)
        a = np.asarray(fr)
        alpha = a[..., 3]
        q = fr.convert("RGB").quantize(colors=colors - 1, method=Image.MEDIANCUT)
        arr = np.asarray(q).copy()
        arr[alpha < 100] = colors - 1          # 透明索引用最后一个
        pf = Image.fromarray(arr, "P")
        pal = list(q.palette.palette) + [0, 0, 0] * (768 - len(q.palette.palette))
        pal[(colors - 1) * 3:(colors - 1) * 3 + 3] = [0, 0, 0]
        pf.putpalette(pal[:768])
        out.append(pf)
    out[0].save(dst, save_all=True, append_images=out[1:], duration=duration,
                loop=0, transparency=colors - 1, disposal=2, optimize=True)
    return before, os.path.getsize(dst)


def build(src: str, out: str, width: int, colors: int) -> int:
    if not os.path.isdir(src):
        print(f"找不到源资源包: {src}")
        return 1
    if os.path.abspath(src) == os.path.abspath(out):
        print("源和目标是同一个目录, 换个 --out")
        return 1

    # 先清掉上一次的产物, 免得删掉的素材还留在里面
    for sub in ("avatar", "animation", "sounds"):
        d = os.path.join(out, sub)
        if os.path.isdir(d):
            shutil.rmtree(d)
    for sub in ("avatar", "animation", "sounds"):
        s = os.path.join(src, sub)
        if not os.path.isdir(s):
            continue
        d = os.path.join(out, sub)
        os.makedirs(d, exist_ok=True)
        for fn in os.listdir(s):
            # .bak 是迭代过程留下的旧版本, 不该跟着示例包进仓库
            if fn.endswith(".bak") or fn.startswith("."):
                continue
            sp = os.path.join(s, fn)
            if os.path.isfile(sp):
                shutil.copy2(sp, os.path.join(d, fn))
    print(f"已复制 avatar / animation / sounds -> {out} (跳过 .bak 等杂物)")

    # 动画压缩
    anim_dir = os.path.join(out, "animation")
    total_old = total_new = 0
    if os.path.isdir(anim_dir):
        for fn in sorted(os.listdir(anim_dir)):
            if not fn.lower().endswith(".gif"):
                continue
            p = os.path.join(anim_dir, fn)
            old, new = compress_gif(p, p, width, colors)
            total_old += old
            total_new += new
            print(f"  {fn:<18} {old/1048576:5.2f} -> {new/1048576:5.2f} MB")
    if total_old:
        print(f"  动画合计 {total_old/1048576:.1f} -> {total_new/1048576:.1f} MB "
              f"(省 {100*(1-total_new/total_old):.0f}%)")

    # pet.json: 用打磨过的示例文案; 源里的自定义字段(如 personas)保留
    cfg = {}
    src_pet = os.path.join(src, "pet.json")
    if os.path.exists(src_pet):
        try:
            cfg = json.load(open(src_pet, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cfg = {}
    cfg.update(DEMO_PET_JSON)
    with open(os.path.join(out, "pet.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"已写入示例 pet.json (名字: {cfg['name']})")

    # 存档绝不带出去
    for fn in STRIP_FILES:
        p = os.path.join(out, fn)
        if os.path.exists(p):
            os.remove(p)
            print(f"  已剔除存档文件 {fn}")

    size = sum(os.path.getsize(os.path.join(dp, f))
               for dp, _, fs in os.walk(out) for f in fs)
    print(f"\n示例包就绪: {out}  共 {size/1048576:.1f} MB")
    print("检查一下有没有混进你的个人存档:")
    for dp, _, fs in os.walk(out):
        for f in fs:
            if f in STRIP_FILES:
                print(f"  [!] 发现 {os.path.join(dp, f)}")
    print("(上面没有输出就是干净的)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(ROOT, "pets", "mypet"),
                    help="源资源包 (你自己的那份)")
    ap.add_argument("--out", default=os.path.join(ROOT, "pets", "demo"),
                    help="输出的示例包目录")
    ap.add_argument("--width", type=int, default=240,
                    help="动画宽度 (240 在最大缩放下也不会被放大)")
    ap.add_argument("--colors", type=int, default=64, help="动画颜色数")
    args = ap.parse_args()
    try:
        from PIL import Image  # noqa: F401
        import numpy           # noqa: F401
    except ImportError:
        print("需要 pillow 和 numpy:  pip install pillow numpy")
        return 1
    return build(args.src, args.out, args.width, args.colors)


if __name__ == "__main__":
    sys.exit(main())
