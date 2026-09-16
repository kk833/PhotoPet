# -*- coding: utf-8 -*-
"""生成 README 顶部的演示动图 (docs/images/demo.gif)。

它不是录屏，而是**用真实素材合成的**：拿资源包里 idle.gif 的帧、按运行时
真实的缩放比例摆放，再叠上气泡和劝休息小窗。好处是不用真的录屏、也不会有
桌面上的隐私内容；缺点是它演示的是"设计效果"而不是逐像素的实拍。

想要真实录屏的话，用 ScreenToGif / ShareX 录一段替换掉这个文件即可。

用法:
    python scripts/make_demo_gif.py
    python scripts/make_demo_gif.py --pet demo-pet --out docs/images/demo.gif
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

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
]


def find_font(size: int):
    from PIL import ImageFont
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def rounded(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill,
                           outline=outline, width=width)


def speech_bubble(size, text, font, max_w=250):
    """带小尾巴的气泡, 高度随文字行数变化。"""
    from PIL import Image, ImageDraw
    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    # 手动折行 (中文按字折)
    lines, cur = [], ""
    for ch in text:
        if tmp.textlength(cur + ch, font=font) > max_w - 24:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    lh = font.size + 8
    w = int(max(tmp.textlength(l, font=font) for l in lines)) + 24
    h = lh * len(lines) + 18
    img = Image.new("RGBA", (w + 8, h + 14), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rounded(d, [0, 0, w, h], 12, (255, 255, 255, 242))
    d.polygon([(24, h - 1), (44, h - 1), (30, h + 12)],
              fill=(255, 255, 255, 242))          # 小尾巴
    for i, line in enumerate(lines):
        d.text((12, 9 + i * lh), line, font=font, fill=(51, 51, 51, 255))
    return img


def rest_panel(text_font, btn_font, pad: int):
    """劝休息小窗 (和程序里的样子一致)。

    尺寸按文字实际宽度算出来 —— 写死尺寸会在放大倍数变化时立刻overflow。
    """
    from PIL import Image, ImageDraw
    lines = ["我都陪你工作 1 小时 47 分了，", "你也需要休息呀，放下手中的工作吧~"]
    # 演示图里不放 emoji: PIL 需要中英双字体分段绘制才画得出, 对一张演示图不值得。
    # 真实程序用 Qt 渲染, 那两个 emoji 是正常显示的。
    b_sleep, b_later = "好，一起睡", "再干一会儿"
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    lh = text_font.size + pad * 0.5
    text_w = max(probe.textlength(s, font=text_font) for s in lines)
    b1 = probe.textlength(b_sleep, font=btn_font)
    b2 = probe.textlength(b_later, font=btn_font)
    bw, bh = pad, btn_font.size + pad
    row_w = b1 + b2 + bw * 3
    w = int(max(text_w, row_w)) + pad * 2
    h = int(lh * len(lines) + bh + pad * 2.2)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rounded(d, [0, 0, w - 1, h - 1], int(pad * 0.9), (255, 252, 245, 248))
    for i, s in enumerate(lines):
        d.text((pad, pad * 0.8 + i * lh), s, font=text_font,
               fill=(51, 51, 51, 255))
    by = int(pad * 0.8 + lh * len(lines) + pad * 0.3)
    rounded(d, [pad, by, pad + b1 + bw, by + bh], int(pad * 0.6),
            (255, 217, 160, 255))
    d.text((pad + bw / 2, by + bh * 0.16), b_sleep, font=btn_font,
           fill=(60, 40, 10, 255))
    x2 = pad + b1 + bw * 2
    rounded(d, [x2, by, x2 + b2 + bw, by + bh], int(pad * 0.6),
            (238, 238, 238, 255))
    d.text((x2 + bw / 2, by + bh * 0.16), b_later, font=btn_font,
           fill=(51, 51, 51, 255))
    return img


def build(pet_dir: str, out: str, scale: int = 1) -> int:
    from PIL import Image, ImageSequence
    import numpy as np

    idle = os.path.join(pet_dir, "animation", "idle.gif")
    if not os.path.exists(idle):
        print(f"找不到 {idle}")
        return 1

    W, H = 460 * scale, 250 * scale
    # 背景: 模拟桌面的一点点层次 (纯色 + 柔和渐变), 不放任何真实桌面内容
    bg = Image.new("RGB", (W, H))
    for y in range(H):
        t = y / max(1, H - 1)
        bg.paste((int(236 - 26 * t), int(240 - 24 * t), int(246 - 18 * t)),
                 (0, y, W, y + 1))

    # 宠物: 按运行时真实比例 (base_height=160 等比) 摆放
    frames = [f.convert("RGBA").copy() for f in ImageSequence.Iterator(Image.open(idle))]
    pet_h = 160 * scale
    pet_w = int(pet_h * frames[0].width / frames[0].height)
    pet_frames = [f.resize((pet_w, pet_h), Image.LANCZOS) for f in frames]
    pet_x, pet_y = W - pet_w - 26 * scale, H - pet_h - 10 * scale

    title_font = find_font(26 * scale)
    sub_font = find_font(13 * scale)
    body_font = find_font(12 * scale)
    btn_font = find_font(11 * scale)
    bubble = speech_bubble((W, H), "嗨，我是小陪，以后就住你桌面上啦~", body_font)
    panel = rest_panel(body_font, btn_font, 10 * scale)

    # 左侧标题: 顺便把留白填掉, 让这张图能当 README 的横幅用
    from PIL import ImageDraw
    title_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    td = ImageDraw.Draw(title_layer)
    td.text((30 * scale, 34 * scale), "PhotoPet 桌宠", font=title_font,
            fill=(58, 48, 40, 255))
    td.text((32 * scale, 34 * scale + title_font.size * 1.5),
            "用一张照片，生成属于你的专属桌宠", font=sub_font,
            fill=(120, 108, 96, 255))
    td.text((32 * scale, 34 * scale + title_font.size * 1.5 + sub_font.size * 1.9),
            "互动会说话 · 久坐会劝你休息 · 睡觉=今天收工", font=sub_font,
            fill=(122, 140, 120, 255))

    # 时间轴: 前 60% 说话, 后 40% 弹劝休息小窗
    total = 22
    talk_end = 13
    shots = []
    for i in range(total):
        im = bg.copy()
        im.paste(title_layer, (0, 0), title_layer)
        pf = pet_frames[i % len(pet_frames)]
        im.paste(pf, (pet_x, pet_y), pf)
        if i < talk_end:
            shown = int(len("嗨，我是小陪，以后就住你桌面上啦~")
                        * (i + 1) / talk_end)
            b = speech_bubble((W, H), "嗨，我是小陪，以后就住你桌面上啦~"[:shown] or " ",
                              body_font)
            bx = max(6 * scale, pet_x + pet_w // 2 - b.width // 2)
            im.paste(b, (bx, pet_y - b.height + 6 * scale), b)
        else:
            px = max(6 * scale, pet_x + pet_w // 2 - panel.width // 2)
            py = max(6 * scale, min(pet_y - panel.height + 10 * scale,
                                    H - panel.height - 6 * scale))
            im.paste(panel, (px, py), panel)
        shots.append(im.convert("P", palette=Image.ADAPTIVE, colors=128))

    os.makedirs(os.path.dirname(out), exist_ok=True)
    shots[0].save(out, save_all=True, append_images=shots[1:], duration=260,
                  loop=0, optimize=True, disposal=2)
    size = os.path.getsize(out)
    print(f"演示图已生成: {out}  ({W}x{H}, {total} 帧, {size/1024:.0f} KB)")
    if size > 2 * 1024 * 1024:
        print("  偏大了, README 里的图建议 2MB 以内; 可以减小 scale 或帧数")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pet", default=os.path.join(ROOT, "demo-pet"))
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "images", "demo.gif"))
    ap.add_argument("--scale", type=int, default=2, help="整体放大倍数 (清晰度)")
    args = ap.parse_args()
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        print("需要 pillow:  pip install pillow")
        return 1
    return build(args.pet, args.out, args.scale)


if __name__ == "__main__":
    sys.exit(main())
