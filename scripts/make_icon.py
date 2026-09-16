# -*- coding: utf-8 -*-
"""生成程序图标 (runtime/icon.png + runtime/icon.ico)。

图标会被三处用到: exe 文件图标、桌面快捷方式、系统托盘。
托盘图标优先读 runtime/icon.png, 读不到就用代码里画的爪印兜底。

用法:
    python scripts/make_icon.py                    # 画一个默认爪印图标
    python scripts/make_icon.py --image 我的图.png   # 用自己的图 (会裁成方形并加圆角底)
    python scripts/make_icon.py --bg "#7cc4ff"     # 换底色

依赖: pillow
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 中文控制台是 GBK: 打印 emoji 会抛 UnicodeEncodeError, 而且往往是在活儿都干完之后
# 的最后一行提示上抛 —— 看起来像"失败了"。统一转成 UTF-8 + 容错输出。
sys.path.insert(0, os.path.join(ROOT, "runtime"))
import app_paths  # noqa: E402
app_paths.fix_console_encoding()

OUT_PNG = os.path.join(ROOT, "runtime", "icon.png")
OUT_ICO = os.path.join(ROOT, "runtime", "icon.ico")
SIZES = [16, 24, 32, 48, 64, 128, 256]


def hex_rgb(s: str) -> tuple:
    s = s.lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def rounded_bg(size: int, top: tuple, bottom: tuple, radius_ratio=0.22):
    """竖向渐变 + 圆角的正方形底。"""
    from PIL import Image, ImageDraw
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        grad.putpixel((0, y), tuple(
            int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    grad = grad.resize((size, size))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * radius_ratio), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def draw_paw(size: int, color=(90, 59, 26)):
    """画一个爪印 (和托盘兜底图标同一套形状, 保持观感一致)。

    脚趾左右对称地排成一道弧, 外侧的略低 —— 不对称看起来就像画歪了。
    """
    from PIL import Image, ImageDraw
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    u = size / 100.0                     # 以 100 为设计基准
    d.ellipse([31 * u, 52 * u, 69 * u, 86 * u], fill=color + (255,))     # 掌垫
    for cx, cy, r in ((28, 39, 8.5), (43, 28, 9),                # 四个脚趾
                      (57, 28, 9), (72, 39, 8.5)):               # 对称弧
        rr = r * u
        d.ellipse([cx * u - rr, cy * u - rr, cx * u + rr, cy * u + rr],
                  fill=color + (255,))
    return layer


def from_image(path: str, size: int, bg_top: tuple, bg_bottom: tuple,
               pad_ratio=0.07):
    """用一张图当图标: 先裁掉四周的透明边距, 再等比缩放居中放到圆角底上。

    不裁边距的话, 立绘周围那一圈空白会让角色在图标里显得又小又偏。
    """
    from PIL import Image
    base = rounded_bg(size, bg_top, bg_bottom)
    src = Image.open(path).convert("RGBA")
    bbox = src.getbbox()                 # 只保留有内容的区域 (含 alpha)
    if bbox:
        src = src.crop(bbox)
    inner = int(size * (1 - 2 * pad_ratio))
    # 注意: thumbnail() 只会缩小、不会放大, 小图会留在角落里显得很小,
    # 所以这里自己算缩放比, 让内容铺满可用区域
    scale = min(inner / max(1, src.width), inner / max(1, src.height))
    src = src.resize((max(1, round(src.width * scale)),
                      max(1, round(src.height * scale))), Image.LANCZOS)
    base.alpha_composite(
        src, ((size - src.width) // 2, (size - src.height) // 2))
    return base


def build_icon(image: str | None, bg_top: tuple, bg_bottom: tuple) -> bool:
    try:
        from PIL import Image
    except ImportError:
        print("需要 pillow:  pip install pillow")
        return False
    if image:
        if not os.path.exists(image):
            print(f"找不到图片: {image}")
            return False
        master = from_image(image, 256, bg_top, bg_bottom)
        kind = f"用自己的图: {os.path.basename(image)}"
    else:
        master = rounded_bg(256, bg_top, bg_bottom)
        master.alpha_composite(draw_paw(256))
        kind = "默认爪印"

    os.makedirs(os.path.dirname(OUT_PNG), exist_ok=True)
    master.save(OUT_PNG)
    # .ico 里塞多档尺寸, 任务栏/桌面/列表视图都清晰
    master.save(OUT_ICO, format="ICO",
                sizes=[(s, s) for s in SIZES if s <= 256])
    print(f"图标已生成 ({kind}):")
    print(f"  {OUT_PNG}   256x256")
    print(f"  {OUT_ICO}   {', '.join(str(s) for s in SIZES)}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", help="用自己的图片做图标 (png/jpg)")
    ap.add_argument("--bg", default="#ffd9a0", help="底色(上) 如 #ffd9a0")
    ap.add_argument("--bg2", default="#ffb35c", help="底色(下), 做竖向渐变")
    args = ap.parse_args()
    ok = build_icon(args.image, hex_rgb(args.bg), hex_rgb(args.bg2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
