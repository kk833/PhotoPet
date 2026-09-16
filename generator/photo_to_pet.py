"""照片 → 桌宠资源包生成器。

流程:
1. (可选) photo2cartoon Q版卡通化  [cartoonize.use_photo2cartoon]
2. rembg 自动抠图去背景
3. 裁剪/缩放，生成 idle / click / sleep 三态 (简单变换: 缩放+旋转+变暗)
4. 写出 pets/<name>/pet.json

用法:
    python photo_to_pet.py --photo my.jpg --name mypet
"""
import argparse
import json
import os
import sys
from PIL import Image, ImageEnhance, ImageFilter

from cartoonize import stylize  # 可选卡通化

# 中文控制台是 GBK: 打印 emoji 会抛 UnicodeEncodeError, 而且**是在活儿都干完之后**
# 的最后一行"资源包已生成"上抛 —— 看起来像生成失败了, 其实早就成了。
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runtime"))
import app_paths  # noqa: E402
app_paths.fix_console_encoding()

try:
    from rembg import remove
    HAS_REMBG = True
except ImportError:
    HAS_REMBG = False


def cutout(img: Image.Image, model: str = "bria-rmbg") -> Image.Image:
    """去背景，返回 RGBA。

    模型选择 (--model):
    - bria-rmbg (默认): 高质量 SOTA，毛发边缘最好，模型约 1GB
    - u2net: 轻量经典，快 3~5 倍，模型约 170MB，适合低配电脑

    首次使用若本地无模型会自动下载；国内用户建议先运行
    scripts/download_model.py 走镜像加速。
    """
    if not HAS_REMBG:
        return img.convert("RGBA")
    from rembg import new_session
    session = new_session(model)
    return remove(img, session=session)


def fit(img: Image.Image, size=(512, 512), margin=0.10) -> Image.Image:
    """等比缩放到画布内, 居中贴到透明画布。

    - 画布默认 512 (原先 160 太小: 桌面端放大到 180% 就发虚)
    - 四周留 margin 余量, 让 click 状态的旋转不会切掉边角
    """
    if img.width <= 0 or img.height <= 0:
        return Image.new("RGBA", size, (0, 0, 0, 0))
    inner = (max(1, int(size[0] * (1 - 2 * margin))),
             max(1, int(size[1] * (1 - 2 * margin))))
    img = img.copy()
    img.thumbnail(inner, Image.LANCZOS)
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    canvas.paste(img, ((size[0] - img.width) // 2, (size[1] - img.height) // 2), img)
    return canvas


def make_states(base: Image.Image):
    """由一张立绘派生三个状态。可替换为多表情 AI 生成。"""
    idle = base
    click = idle.rotate(-6, expand=False, resample=Image.BICUBIC)
    click = ImageEnhance.Brightness(click).enhance(1.08)
    sleep = idle.filter(ImageFilter.GaussianBlur(0.6))
    sleep = ImageEnhance.Brightness(sleep).enhance(0.75)
    return {"idle": idle, "click": click, "sleep": sleep}


def generate(photo: str, name: str, out_root="pets", use_cartoon=False, model="bria-rmbg"):
    img = Image.open(photo).convert("RGB")

    if use_cartoon:
        img = stylize(img)  # photo2cartoon Q版化，需要按其 README 配置模型

    img = cutout(img, model=model)
    base = fit(img)
    states = make_states(base)

    pet_dir = os.path.join(out_root, name)
    av_dir = os.path.join(pet_dir, "avatar")
    os.makedirs(av_dir, exist_ok=True)
    for state, im in states.items():
        im.save(os.path.join(av_dir, f"{state}.png"))

    config = {
        "name": name,
        "size": [160, 160],      # 运行时窗口初始尺寸
        "base_height": 160,      # 人物显示高度基准 (运行时所有状态按它等比缩放)
        "dialogues": [
            "嗨，我是你的专属桌宠!",
            "记得休息一下眼睛哦~",
            "今天也要加油鸭!",
            "摸摸我会有惊喜哦",
        ],
    }
    with open(os.path.join(pet_dir, "pet.json"), "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    print(f"[OK] 资源包已生成: {pet_dir}")
    print(f"   启动: python runtime/main.py --pet {pet_dir}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--photo", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--cartoon", action="store_true", help="启用 photo2cartoon 卡通化")
    ap.add_argument("--model", choices=["bria-rmbg", "u2net"], default="bria-rmbg",
                    help="抠图模型: bria-rmbg=高质量(默认) / u2net=轻量快速")
    args = ap.parse_args()
    generate(args.photo, args.name, use_cartoon=args.cartoon, model=args.model)
