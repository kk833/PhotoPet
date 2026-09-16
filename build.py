# -*- coding: utf-8 -*-
"""把桌宠打包成 Windows exe (v0.9)。

用法:
    python build.py                 # 打包到 dist/PhotoPet/ (推荐, 启动快)
    python build.py --onefile       # 打成单个 exe (启动慢几秒, 但只有一个文件)
    python build.py --with-pets     # 顺便把 pets/ 一起复制进去 (注意隐私!)
    python build.py --clean         # 先清掉 build/ 与 dist/ 再打

为什么默认是 onedir 而不是 onefile:
    onefile 每次启动都要把 ~80MB 解压到临时目录再运行。桌宠是常驻 + 开机自启的,
    意味着每次开机都慢几秒、杀软每次都扫一遍。onedir 启动快得多, 分发时压成 zip 一样方便。

体积控制:
    生成器用的 rembg / onnxruntime / numpy / scipy / imageio 全部排除 ——
    打进去体积会从 ~80MB 涨到 1GB+。运行时只需要 PyQt6。
"""
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

# 和其它入口 (wizard.py / generator / scripts) 保持一致: 中文控制台是 GBK,
# 输出里出现 GBK 打不出的字符会在**成功的最后一行**上抛 UnicodeEncodeError,
# 看起来像打包失败, 其实已经打好了。
sys.path.insert(0, os.path.join(ROOT, "runtime"))
import app_paths      # noqa: E402
app_paths.fix_console_encoding()

ENTRY = os.path.join(ROOT, "runtime", "main.py")
APP_NAME = "PhotoPet"

# 这些只有生成器/工具脚本用得到, 运行时不需要 —— 排掉能省下 1GB 左右
EXCLUDES = [
    "rembg", "onnxruntime", "onnxruntime_gpu", "numpy", "scipy",
    "imageio", "imageio_ffmpeg", "PIL", "matplotlib", "tkinter",
    "pandas", "IPython", "pytest", "setuptools", "pip",
]

# pyttsx3 用 importlib 动态加载驱动, PyInstaller 静态分析抓不到
HIDDEN = [
    "pyttsx3.drivers", "pyttsx3.drivers.sapi5", "comtypes",
    "PyQt6.QtMultimedia", "PyQt6.QtMultimediaWidgets",
]

# Qt 的插件目录: 少哪一个都会静默失效(没有报错, 但功能没了)
QT_PLUGINS = ["platforms", "imageformats", "multimedia", "mediaservice", "styles"]


def build(onefile: bool, with_pets: bool, clean: bool) -> int:
    if clean:
        for d in ("build", "dist"):
            p = os.path.join(ROOT, d)
            if os.path.isdir(p):
                shutil.rmtree(p)
                print(f"已清理 {d}/")

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", APP_NAME,
        "--windowed",                       # 不弹黑框 (GUI 程序)
        "--onefile" if onefile else "--onedir",
        "--distpath", os.path.join(ROOT, "dist"),
        "--workpath", os.path.join(ROOT, "build"),
        "--specpath", os.path.join(ROOT, "build"),
    ]
    for m in EXCLUDES:
        args += ["--exclude-module", m]
    for m in HIDDEN:
        args += ["--hidden-import", m]

    # exe 文件图标 (同时决定桌面快捷方式的图标)
    icon_ico = os.path.join(ROOT, "runtime", "icon.ico")
    if os.path.exists(icon_ico):
        args += ["--icon", icon_ico]
    else:
        print("提示: 没有 runtime/icon.ico, exe 会是默认图标。")
        print("      想要图标先跑:  python scripts/make_icon.py")

    # 托盘图标: 把 png 也带上 (缺失时程序会用 QPainter 兜底画一个)
    icon_png = os.path.join(ROOT, "runtime", "icon.png")
    if os.path.exists(icon_png):
        args += ["--add-data", f"{icon_png}{os.pathsep}runtime"]

    args.append(ENTRY)

    print("执行:", " ".join(args[:6]), "...")
    r = subprocess.run(args, cwd=ROOT)
    if r.returncode != 0:
        print("\n打包失败。")
        return r.returncode

    out_dir = os.path.join(ROOT, "dist", APP_NAME) if not onefile \
        else os.path.join(ROOT, "dist")
    print(f"\n构建完成: {out_dir}")

    if with_pets:
        src = os.path.join(ROOT, "pets")
        dst = os.path.join(out_dir, "pets")
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
            print(f"已复制 pets/ -> {dst}")
            print("  [!] 资源包里含你的照片/形象, 分享给别人前请确认这是你想公开的内容")
        else:
            print("没找到 pets/, 跳过")

    print("\n下一步:")
    print(f"  1. 把桌宠资源包文件夹放进 {os.path.join(out_dir, 'pets')}")
    print(f"  2. 双击 {os.path.join(out_dir, APP_NAME + '.exe')} 启动")
    print("  3. 想分享就把整个文件夹压成 zip (zip 里不含你的 ai_config.json)")
    print("\n注意: 首次运行会在 exe 旁边生成 ai_config.json (你的 key 只存本地)。")
    return 0


# ---------------------------------------------------------------- 创作版
# 创作版 = 制作向导 + 抠图依赖(rembg/onnxruntime) + 自带模型。
# 目的是"不装 Python 也能做宠物" —— 代价是体积大几倍。
CREATOR_EXCLUDES = [
    # scipy 只用得到 ndimage 的几个形态学函数, 其余子模块动辄几十 MB
    "scipy.linalg", "scipy.sparse", "scipy.optimize", "scipy.signal",
    "scipy.stats", "scipy.interpolate", "scipy.integrate", "scipy.spatial",
    "scipy.cluster", "scipy.fft", "scipy.io", "scipy.odr", "scipy.datasets",
    "scipy.misc",
    # 跟这个功能完全无关的
    "matplotlib", "pandas", "tkinter", "IPython", "pytest", "pip",
    "setuptools", "torch", "tensorflow", "cv2", "numba", "notebook",
]
CREATOR_HIDDEN = [
    "rembg", "rembg.sessions", "rembg.sessions.u2net",
    "onnxruntime", "onnxruntime.capi", "onnxruntime.capi._pybind_state",
    "scipy.ndimage", "scipy._lib._ccallback",
    "imageio.v3", "imageio_ffmpeg", "PIL._imaging",
    "PyQt6.QtMultimedia",
]


def _copy_bundled_model() -> bool:
    """把 ~/.u2net/u2net.onnx 复制到 models/ 以便打进创作版。

    创作版自带模型, 用户第一次抠图就不用等 167MB 下载。
    """
    src = os.path.join(os.path.expanduser("~"), ".u2net", "u2net.onnx")
    if not os.path.exists(src):
        alt = os.path.join(os.path.expanduser("~"), ".rembg", "models", "u2net.onnx")
        src = alt if os.path.exists(alt) else ""
    if not src:
        print("提示: 本机没有 u2net 模型, 创作版将不带模型 (用户首次抠图需联网下载)")
        return False
    dst_dir = os.path.join(ROOT, "models")
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, "u2net.onnx")
    if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
        return True
    print(f"复制抠图模型以便打包: {os.path.getsize(src)/1048576:.0f} MB")
    shutil.copyfile(src, dst)
    return True


def build_creator(clean: bool) -> int:
    """打包"创作版": 制作向导 + 抠图依赖 + 自带模型。"""
    has_model = _copy_bundled_model()
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", "PhotoPet-Creator",
        "--windowed",
        "--onedir",
        "--distpath", os.path.join(ROOT, "dist-creator"),
        "--workpath", os.path.join(ROOT, "build"),
        "--specpath", os.path.join(ROOT, "build"),
    ]
    for m in CREATOR_EXCLUDES:
        args += ["--exclude-module", m]
    for m in CREATOR_HIDDEN:
        args += ["--hidden-import", m]
    # 向导要 import 这三个目录里的模块 (app_paths/pet_pack、photo_to_pet、
    # add_asset/extract_audio/video_ops)。源码运行靠 sys.path 找得到,
    # 但 PyInstaller 静态分析看不到 —— 必须显式给出路径, 否则打包后一跑就
    # ModuleNotFoundError: No module named 'app_paths'。
    for sub in ("runtime", "generator", "scripts"):
        args += ["--paths", os.path.join(ROOT, sub)]
    for m in ("app_paths", "pet_pack", "photo_to_pet", "cartoonize",
              "add_asset", "extract_audio", "video_ops", "make_icon"):
        args += ["--hidden-import", m]
    icon_png = os.path.join(ROOT, "runtime", "icon.png")
    if os.path.exists(icon_png):
        args += ["--add-data", f"{icon_png}{os.pathsep}runtime"]
    if has_model:
        args += ["--add-data",
                 f"{os.path.join(ROOT, 'models')}{os.pathsep}models"]
    args.append(os.path.join(ROOT, "wizard.py"))

    print("执行:", " ".join(args[:6]), "... (这一步比较久, 依赖多)")
    r = subprocess.run(args, cwd=ROOT)
    if r.returncode != 0:
        print("\n打包失败。")
        return r.returncode

    out = os.path.join(ROOT, "dist-creator", "PhotoPet-Creator")
    size = sum(os.path.getsize(os.path.join(dp, f))
               for dp, _, fs in os.walk(out) for f in fs)
    print(f"\n构建完成: {out}   共 {size/1048576:.0f} MB")
    print("\n下一步 (强烈建议先自检, 确认打包后真能做抠图):")
    print(f'  "{os.path.join(out, "PhotoPet-Creator.exe")}" --selftest')
    print("自检通过后, 把整个文件夹压成 zip 分发即可。")
    return 0


def create_desktop_shortcut(target: str) -> bool:
    """在桌面建一个带图标的快捷方式, 双击即启动。"""
    name = APP_NAME
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "$d = [Environment]::GetFolderPath('Desktop'); "
        f"$lnk = $ws.CreateShortcut((Join-Path $d '{name}.lnk')); "
        f"$lnk.TargetPath = '{target}'; "
        f"$lnk.WorkingDirectory = '{os.path.dirname(target)}'; "
        f"$lnk.IconLocation = '{target},0'; "
        "$lnk.Description = 'PhotoPet 桌宠'; "
        "$lnk.Save(); "
        "Write-Output (Join-Path $d '" + name + ".lnk')"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            print(f"已在桌面创建快捷方式: {r.stdout.strip()}")
            return True
        print("创建快捷方式失败:", (r.stderr or "").strip()[:200])
    except Exception as e:  # noqa: BLE001
        print("创建快捷方式失败:", e)
    return False


def install(shortcut: bool = True) -> int:
    """把打包结果放到项目根目录, 和源码共用同一份 pets/ 与存档。

    为什么放项目根而不是 %LOCALAPPDATA%: 那样 exe 会用自己的一份 pets/,
    你的好感度/养成进度会和源码模式那份**分裂成两份**。放根目录则
    "双击图标启动"和"python 启动"是同一只宠物、同一份存档。
    """
    src = os.path.join(ROOT, "dist", APP_NAME)
    if not os.path.isdir(src):
        print("没找到打包结果, 先跑:  python build.py")
        return 1
    for name in (APP_NAME + ".exe", "_internal"):
        s, d = os.path.join(src, name), os.path.join(ROOT, name)
        if not os.path.exists(s):
            continue
        if os.path.isdir(s):
            shutil.rmtree(d, ignore_errors=True)
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)
    target = os.path.join(ROOT, APP_NAME + ".exe")
    print(f"\n已安装到: {target}")
    print("  和源码共用 pets/ 与 ai_config.json, 所以存档是同一份")
    if shortcut:
        create_desktop_shortcut(target)
    print("\n以后直接双击桌面的 PhotoPet 图标就能启动, 不用再碰 python。")
    print("  想重新打包:  python build.py && python build.py --install")
    return 0


README_TXT = """PhotoPet 桌宠 —— 双击 PhotoPet.exe 即可启动，不需要安装 Python。

第一次启动会发生什么：
  · 自动把自带的示例桌宠 demo-pet/ 装到 pets/demo/，你就能立刻看到宠物
  · 在 exe 旁边生成 ai_config.json（AI 对话与语音的配置，可留空不用）

怎么换成你自己的宠物：
  1. 用项目里的生成器做一只（见仓库 README 的「从头做一个属于你的」）
  2. 把做好的资源包文件夹放进 pets/ 目录
  3. 重启桌宠

托盘图标（屏幕右下角）右键可以：显示/隐藏、大小档位、鼠标穿透、退出。
宠物身上右键可以：投喂、番茄钟、语音开关、睡觉/醒来、开机自启动。

卸载：直接删掉整个文件夹。所有存档都在 pets/<名字>/ 里，跟着文件夹走。
"""


def make_release(out_dir: str | None = None) -> int:
    """组装可直接分发的 zip: exe + _internal + 示例资源包 + 使用说明。

    CI 里就是调这个, 所以本地跑一次 = 提前验证 CI 能不能成功。
    """
    src = os.path.join(ROOT, "dist", APP_NAME)
    exe = os.path.join(src, APP_NAME + ".exe")
    if not os.path.exists(exe):
        print("没找到打包结果, 先跑:  python build.py")
        return 1

    # 示例资源包放在 exe 旁边 —— 首次启动时程序会自动装到 pets/ 里
    demo_src = os.path.join(ROOT, "demo-pet")
    demo_dst = os.path.join(src, "demo-pet")
    if os.path.isdir(demo_src):
        shutil.rmtree(demo_dst, ignore_errors=True)
        shutil.copytree(demo_src, demo_dst)
        print("已放入示例资源包 demo-pet/")
    else:
        print("提示: 没有 demo-pet/, 用户首次启动会看不到宠物")

    with open(os.path.join(src, "使用说明.txt"), "w", encoding="utf-8") as f:
        f.write(README_TXT)
    print("已写入 使用说明.txt")

    out_dir = out_dir or os.path.join(ROOT, "dist")
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, f"{APP_NAME}-windows-x64")
    zip_path = shutil.make_archive(base, "zip", root_dir=os.path.dirname(src),
                                  base_dir=APP_NAME)
    size = os.path.getsize(zip_path)
    print(f"\n可分发压缩包: {zip_path}  ({size/1048576:.1f} MB)")
    print("发给别人: 对方解压后双击 PhotoPet.exe 即可, 不用装 Python")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--onefile", action="store_true",
                    help="打成单个 exe (启动慢, 但只有一个文件)")
    ap.add_argument("--with-pets", action="store_true",
                    help="把 pets/ 一起复制进产物 (含个人形象, 分享前想清楚)")
    ap.add_argument("--clean", action="store_true", help="先清理再打包")
    ap.add_argument("--install", action="store_true",
                    help="打包后装到项目根目录, 并创建桌面快捷方式")
    ap.add_argument("--no-shortcut", action="store_true",
                    help="配合 --install: 只安装不建快捷方式")
    ap.add_argument("--release", action="store_true",
                    help="组装可直接分发的 zip (exe + 示例包 + 使用说明)")
    ap.add_argument("--creator", action="store_true",
                    help="打包创作版 (制作向导 + 抠图依赖 + 自带模型, 约 500MB)")
    ap.add_argument("--out-dir", default=None, help="配合 --release: zip 输出目录")
    args = ap.parse_args()
    if args.release:
        return make_release(args.out_dir)
    if args.creator:
        return build_creator(args.clean)
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("需要先安装打包工具:  pip install \"pyinstaller>=6.11\"")
        return 1
    if args.install:
        return install(shortcut=not args.no_shortcut)
    return build(args.onefile, args.with_pets, args.clean)


if __name__ == "__main__":
    sys.exit(main())
