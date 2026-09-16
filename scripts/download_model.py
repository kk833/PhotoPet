# -*- coding: utf-8 -*-
"""
模型下载脚本 v4 —— 自动适配 rembg 模型目录 + 国内镜像优先 + 大小校验 + 断点续传
用法:
  python scripts/download_model.py --model u2net        # 176MB 抠图模型 (推荐)
  python scripts/download_model.py --model bria-rmbg    # 977MB 高质量模型
"""
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 中文控制台是 GBK: 打印 emoji 会抛 UnicodeEncodeError, 而且往往是在活儿都干完之后
# 的最后一行提示上抛 —— 看起来像"失败了"。这个脚本尤其需要这一层: 下载失败时的
# 提示会被编码错误整个盖掉, 最后只看到一个莫名其妙的 UnicodeEncodeError。
sys.path.insert(0, os.path.join(ROOT, "runtime"))
import app_paths  # noqa: E402
app_paths.fix_console_encoding()

MODEL_SOURCES = {
    "u2net": {
        "min_bytes": 160 * 1024 * 1024,
        "urls": [
            "https://hf-mirror.com/tomjackson2023/rembg/resolve/main/u2net.onnx",
            "https://modelscope.cn/models/AI-ModelScope/u2net/resolve/master/u2net.onnx",
            "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx",
        ],
    },
    "bria-rmbg": {
        "min_bytes": 900 * 1024 * 1024,
        "urls": [
            "https://hf-mirror.com/briaai/RMBG-1.4/resolve/main/onnx/model.onnx",
            "https://github.com/danielgatis/rembg/releases/download/v0.0.0/bria-rmbg.onnx",
        ],
    },
}


def detect_model_dir():
    """自动检测 rembg 实际使用的模型目录（新版用 ~/.u2net，旧版用 ~/.rembg/models）。"""
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".u2net"),
        os.path.join(home, ".rembg", "models"),
    ]
    for d in candidates:
        if os.path.isdir(d) and any(f.endswith(".onnx") for f in os.listdir(d)):
            return d
    return candidates[0]


def human(n):
    return "%.1fMB" % (n / 1024 / 1024)


def download(url, dest, min_bytes):
    tmp = dest + ".part"
    start = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    req = urllib.request.Request(url)
    if start > 0:
        req.add_header("Range", "bytes=%d-" % start)
    req.add_header("User-Agent", "Mozilla/5.0")
    with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "ab" if start else "wb") as f:
        total = resp.headers.get("Content-Length")
        if total:
            total = int(total) + start
        downloaded = start
        last = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            now = time.time()
            if now - last > 2:
                last = now
                if total:
                    sys.stdout.write("\r  %3d%%  %s / %s " % (downloaded * 100 // total, human(downloaded), human(total)))
                else:
                    sys.stdout.write("\r  %s " % human(downloaded))
                sys.stdout.flush()
    size = os.path.getsize(tmp)
    if size < min_bytes:
        os.remove(tmp)
        raise IOError("文件不完整 (%s < 预期 %s)" % (human(size), human(min_bytes)))
    os.replace(tmp, dest)
    return size


def main():
    arg = "u2net"
    if "--model" in sys.argv:
        i = sys.argv.index("--model")
        if i + 1 < len(sys.argv):
            arg = sys.argv[i + 1]
    if arg not in MODEL_SOURCES:
        print("未知模型: %s，可选: %s" % (arg, ", ".join(MODEL_SOURCES)))
        sys.exit(1)

    info = MODEL_SOURCES[arg]
    model_dir = detect_model_dir()
    os.makedirs(model_dir, exist_ok=True)
    dest_a = os.path.join(model_dir, arg + ".onnx")
    dest_b = os.path.join(model_dir, arg, arg + ".onnx")

    if os.path.exists(dest_a) and os.path.getsize(dest_a) >= info["min_bytes"]:
        print("[%s] 已存在且完整，跳过下载" % arg)
    else:
        print("下载模型: %s  保存到: %s" % (arg, dest_a))
        print("(模型目录自动检测: %s)" % model_dir)
        ok = False
        for i, url in enumerate(info["urls"]):
            print("[源 %d/%d] %s" % (i + 1, len(info["urls"]), url.split("/")[2]))
            try:
                size = download(url, dest_a, info["min_bytes"])
                print("\n  [OK] 下载完成 %s" % human(size))
                ok = True
                break
            except Exception as e:
                print("\n  [X] 该源失败: %s" % e)
        if not ok:
            print("\n全部源失败。请用浏览器手动下载上面任意 URL，")
            print("重命名为 %s.onnx 放到 %s" % (arg, model_dir))
            sys.exit(1)

    # 双写：兼容 rembg 新旧两种目录布局，保证一定能被找到
    os.makedirs(os.path.dirname(dest_b), exist_ok=True)
    try:
        if not os.path.exists(dest_b) or os.path.getsize(dest_b) != os.path.getsize(dest_a):
            import shutil
            shutil.copyfile(dest_a, dest_b)
            print("[OK] 已同步到兼容路径: %s" % dest_b)
    except Exception as e:
        print("(同步兼容路径跳过: %s)" % e)

    print("\n全部就绪！运行: python generator\\photo_to_pet.py --photo 你的照片.jpg --name mypet --model u2net")


if __name__ == "__main__":
    main()
