"""统一路径解析 (v0.9): 源码运行与打包成 exe 后都能找到对的东西。

打包后有两类路径**必须分开**, 混用是 PyInstaller 项目最常见的翻车点:

- ``BUNDLE_DIR``: 打进 exe 里的只读资源。PyInstaller 会把它们解压到临时目录
  (``sys._MEIPASS``), 程序退出就删。
- ``user_data_dir()``: ``pets/``、``ai_config.json`` 这些属于**用户**的东西,
  既要读也要写, 必须放在可写且能长期存在的位置。

用户数据目录采用「便携优先、APPDATA 兜底」:

1. exe 旁边可写, 或旁边已经放着 ``pets/`` / ``ai_config.json`` → 就用 exe 旁边
   (绿色版: 解压到任意目录直接用, 存档也跟着资源包走)
2. 否则(exe 被装在 Program Files 这类只读位置) → ``%APPDATA%/PhotoPet/``

源码运行时永远走第 1 种(仓库根目录), 行为和打包前完全一致。
"""
import os
import shutil
import sys

APP_NAME = "PhotoPet"


def fix_console_encoding():
    """让控制台输出不再因为编码而崩。

    Windows 中文控制台默认 GBK, 打印 emoji/✓ 会抛 UnicodeEncodeError ——
    而且往往是活儿都干完了才在最后一行成功提示上抛, 看起来像"失败了"。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass        # 打包成 --windowed 时可能根本没有控制台


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


if _is_frozen():
    # 打进 exe 的只读资源
    BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    # exe 自己所在的目录
    EXE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    # runtime/app_paths.py -> 上一级是 runtime/, 再上一级是仓库根
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    BUNDLE_DIR = ROOT
    EXE_DIR = ROOT

IS_FROZEN = _is_frozen()
_USER_DIR: str | None = None


def _writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".photopet_write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("")
        os.remove(probe)
        return True
    except OSError:
        return False


def _use_portable() -> bool:
    """exe 旁边能不能直接当用户数据目录用。"""
    if not IS_FROZEN:
        return True                      # 源码运行: 就用仓库根, 和以前一样
    if os.path.isdir(os.path.join(EXE_DIR, "pets")) or \
            os.path.exists(os.path.join(EXE_DIR, "ai_config.json")):
        return _writable(EXE_DIR)        # 已经是绿色版目录, 只要可写就用它
    return _writable(EXE_DIR)


def user_data_dir() -> str:
    """用户数据目录 (pets/、ai_config.json 都在这里)。"""
    global _USER_DIR
    if _USER_DIR:
        return _USER_DIR
    if _use_portable():
        _USER_DIR = EXE_DIR
    else:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        _USER_DIR = os.path.join(base, APP_NAME)
        os.makedirs(_USER_DIR, exist_ok=True)
    return _USER_DIR


def pets_dir() -> str:
    return os.path.join(user_data_dir(), "pets")


def config_path() -> str:
    return os.path.join(user_data_dir(), "ai_config.json")


def resource_path(*parts: str) -> str:
    """打进 exe 里的只读资源 (图标等)。"""
    return os.path.join(BUNDLE_DIR, *parts)


def is_portable() -> bool:
    """当前是不是"绿色版"布局 (数据就在 exe 旁边)。"""
    return user_data_dir() == EXE_DIR


def ensure_pets_available() -> str | None:
    """首次运行时准备一只可用的宠物。返回新建的目录, 什么都没做则返回 None。

    两条兜底路径:
    1. **只读安装**: exe 被装在 Program Files 这类位置时, 用户数据目录回落到
       %APPDATA%, 于是看不到旁边自带的 pets/ —— 把它们复制过去。
    2. **全新 clone**: 仓库里 pets/ 是被忽略的(含个人照片), 所以新人 clone 下来
       一只宠物都没有。此时把随仓库发布的示例包 demo-pet/ 复制成 pets/demo,
       让"clone 下来直接跑"就能看到宠物、也才有东西可以玩。
       复制而不是直接用, 是为了让存档落在 pets/ 里(那里不进仓库)。
    """
    target = pets_dir()
    if os.path.isdir(target) and os.listdir(target):
        return None                      # 已经有资源包了, 不打扰 (_包括你自己的机器)

    # 路径 1: exe 旁边就带着 pets/
    src = os.path.join(EXE_DIR, "pets")
    if (os.path.isdir(src) and os.listdir(src)
            and os.path.dirname(os.path.abspath(src)) != os.path.abspath(target)):
        try:
            os.makedirs(target, exist_ok=True)
            for name in os.listdir(src):
                s, d = os.path.join(src, name), os.path.join(target, name)
                if os.path.isdir(s) and not os.path.exists(d):
                    shutil.copytree(s, d)
            if os.listdir(target):
                return target
        except OSError:
            pass                          # 复制失败就算了, 上层会给出提示

    # 路径 2: 随仓库发布的示例包 (源码运行在仓库根, 打包后在 exe 旁边)
    for base in (BUNDLE_DIR, EXE_DIR):
        demo = os.path.join(base, "demo-pet")
        if not os.path.isdir(demo) or not os.path.exists(os.path.join(demo, "pet.json")):
            continue
        dst = os.path.join(target, "demo")
        try:
            os.makedirs(target, exist_ok=True)
            shutil.copytree(demo, dst, dirs_exist_ok=True)
            return dst
        except OSError:
            return None
    return None


def resolve_pet_dir(arg: str) -> str:
    """把 ``--pet`` 的参数解析成真实目录。

    相对路径先从当前工作目录找; 找不到再当成"资源包名字"到 pets/ 下面找 ——
    这样双击 exe / 开机自启时 ``--pet pets/mypet`` 也能正确落到资源包上。
    """
    if os.path.isabs(arg) or os.path.isdir(arg):
        return arg
    candidate = os.path.join(pets_dir(), os.path.basename(os.path.normpath(arg)))
    return candidate if os.path.isdir(candidate) else arg
