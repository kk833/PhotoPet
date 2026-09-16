"""资源包导入 / 导出 (.pet) —— 让定制成果能流动起来 (v0.10)。

现在给别人分享桌宠要"把文件夹发给对方，对方放进 pets/"，这里把它变成
一个 `.pet` 文件（其实就是 zip）：导出打包、导入解包，双击 / 拖到 exe 上即可。

两个必须守住的东西：

1. **存档绝不跟着走**。`status.json`（养成数值）和 `work.json`（陪伴时长）是
   你和这只宠物的私人记录，导出时一律剔除 —— 不能因为分享形象就把好感度送人。
2. **导入的是别人给的压缩包，要当作不可信输入处理**。所以有 zip slip 防护
   （拒绝 `..`、绝对路径、反斜杠、符号链接）、只接受白名单目录、限制体积。
"""
import json
import os
import zipfile

# 资源包里允许被打包 / 解包的内容
ALLOWED_TOP = ("pet.json", "avatar", "animation", "sounds", "talk")
# 私人存档, 永远不进 .pet
SAVE_FILES = ("status.json", "work.json")
# 导入时的体积上限 (解压后), 防止一个畸形压缩包把磁盘写满
MAX_TOTAL_MB = 300
MAX_FILES = 3000


class PetPackError(Exception):
    """导入/导出失败, 消息可以直接给用户看。"""


# ---------------------------------------------------------------- 校验
def validate(pet_dir: str) -> list[str]:
    """检查一个资源包能不能用, 返回问题列表 (空 = 没问题)。"""
    problems = []
    if not os.path.isdir(pet_dir):
        return [f"不是目录: {pet_dir}"]
    pet_json = os.path.join(pet_dir, "pet.json")
    if not os.path.exists(pet_json):
        problems.append("缺少 pet.json")
    else:
        try:
            with open(pet_json, encoding="utf-8") as f:
                json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            problems.append(f"pet.json 读不了: {e}")

    av = os.path.join(pet_dir, "avatar")
    anim = os.path.join(pet_dir, "animation")
    has_avatar = os.path.isdir(av) and any(
        f.lower().endswith(".png") for f in os.listdir(av))
    has_anim = os.path.isdir(anim) and any(
        f.lower().endswith(".gif") for f in os.listdir(anim))
    if not has_avatar and not has_anim:
        problems.append("既没有 avatar/*.png 也没有 animation/*.gif，桌宠会显示成占位图")
    return problems


def peek(pet_path: str) -> dict:
    """不完整解压就读出资源包信息 (名字/动作数/有无音效), 用于导入前展示。"""
    try:
        with zipfile.ZipFile(pet_path) as zf:
            entries = _entries(zf)
            meta = {"name": None, "states": [], "sounds": [], "has_json": False}
            for rel in entries:
                if rel == "pet.json":
                    meta["has_json"] = True
                    try:
                        with zf.open(entries[rel]) as f:
                            meta["name"] = json.load(f).get("name")
                    except (json.JSONDecodeError, OSError, KeyError):
                        pass
                elif rel.startswith("animation/") and rel.endswith(".gif"):
                    meta["states"].append(os.path.splitext(os.path.basename(rel))[0])
                elif rel.startswith("sounds/"):
                    meta["sounds"].append(os.path.splitext(os.path.basename(rel))[0])
            return meta
    except (zipfile.BadZipFile, OSError) as e:
        raise PetPackError(f"不是有效的 .pet 文件: {e}") from e


# ---------------------------------------------------------------- 内部工具
def _iter_pack_files(pet_dir: str):
    """产出要打包的 (绝对路径, 归档内相对路径), 自动跳过存档与杂物。"""
    for name in sorted(os.listdir(pet_dir)):
        if name in SAVE_FILES or name.startswith(".") or name.endswith(".bak"):
            continue
        full = os.path.join(pet_dir, name)
        if os.path.isfile(full):
            if name in ALLOWED_TOP:
                yield full, name
        elif os.path.isdir(full) and name in ALLOWED_TOP:
            for dp, _dirs, files in os.walk(full):
                for f in sorted(files):
                    if f.startswith(".") or f.endswith(".bak"):
                        continue
                    fp = os.path.join(dp, f)
                    yield fp, os.path.relpath(fp, pet_dir).replace(os.sep, "/")


def _normalize(name: str) -> str:
    """只做两件事: 反斜杠转正斜杠、去掉开头的 "./"。

    绝不能用 lstrip("./") —— 它会把 `../../evil` 的前导点斜杠**整段吃掉**,
    结果"上跳"痕迹被抹平、安全检查反而被绕过 (看起来没逃逸只是因为后面还有
    一道绝对路径兜底)。归一化必须如实保留 `..`, 交给检查去拒绝。
    """
    n = str(name).replace("\\", "/")
    while n.startswith("./"):
        n = n[2:]
    return n


def _entries(zf: zipfile.ZipFile) -> dict[str, str]:
    """返回 {归一化后的相对路径: zip 内的原始条目名} (只含文件)。

    同时剥掉"多包了一层目录"的前缀 —— 很多人会把整个文件夹压进去,
    于是条目是 `我的桌宠/pet.json` 而不是 `pet.json`。
    名字映射只在这里做一次, 各处复用, 免得像之前那样对不上。
    """
    pairs = [(_normalize(i.filename), i.filename) for i in zf.infolist()
             if not i.is_dir() and _normalize(i.filename)]
    if not pairs:
        return {}
    tops = {n.split("/")[0] for n, _ in pairs}
    prefix = ""
    if len(tops) == 1:
        only = tops.pop()
        # 只有"所有条目都在同一个顶层目录下、而且它本身不是合法顶层名"时才剥
        if only not in ALLOWED_TOP and all(n.startswith(only + "/") for n, _ in pairs):
            prefix = only + "/"
    return {n[len(prefix):]: raw for n, raw in pairs}


def _check_safe(zf: zipfile.ZipFile) -> None:
    """zip slip 防护 + 符号链接 + 体积限制。导入的是别人的文件, 不能信。

    检查的是**原始条目名**(归一化之后仍然保留 `..`), 所以不能被绕过。
    """
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        norm = _normalize(info.filename)
        if not norm:
            continue
        if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":"):
            raise PetPackError(f"压缩包里有绝对路径: {info.filename}")
        if any(part == ".." for part in norm.split("/")):
            raise PetPackError(f"压缩包里有上跳路径: {info.filename}")
        # 符号链接 (unix 模式下外部属性高 16 位是文件类型)
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise PetPackError(f"压缩包里有符号链接: {info.filename}")
        total += info.file_size
    if total > MAX_TOTAL_MB * 1024 * 1024:
        raise PetPackError(
            f"压缩包解压后有 {total/1048576:.0f}MB，超过 {MAX_TOTAL_MB}MB 上限")


# ---------------------------------------------------------------- 导出
def export_pet(pet_dir: str, out_path: str) -> tuple[str, int]:
    """把资源包打成 .pet。返回 (输出路径, 文件数)。"""
    problems = validate(pet_dir)
    if problems:
        raise PetPackError("这个资源包有问题：\n  " + "\n  ".join(problems))

    files = list(_iter_pack_files(pet_dir))
    if not files:
        raise PetPackError("资源包里没有可导出的内容")
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for full, rel in files:
            zf.write(full, rel)
    return out_path, len(files)


# ---------------------------------------------------------------- 导入
def import_pet(pet_path: str, pets_root: str, overwrite: bool = False,
               new_name: str | None = None) -> str:
    """把 .pet 装进 pets_root, 返回装好的目录。

    同名资源包已存在时: overwrite=False 会抛错 (让调用方去问用户),
    new_name 可以指定一个新名字。
    """
    try:
        zf = zipfile.ZipFile(pet_path)
    except (zipfile.BadZipFile, OSError) as e:
        raise PetPackError(f"不是有效的 .pet 文件: {e}") from e

    with zf:
        _check_safe(zf)                      # 先做安全体检 (看原始条目名)
        entries = _entries(zf)               # {相对路径: 原始条目名}
        if len(entries) > MAX_FILES:
            raise PetPackError(f"压缩包里文件太多（>{MAX_FILES}）")
        if "pet.json" not in entries:
            raise PetPackError("这不是桌宠资源包：里面没有 pet.json")
        if not any(r.startswith(("avatar/", "animation/")) for r in entries):
            raise PetPackError("资源包里没有任何形象素材（avatar/ 或 animation/）")

        # 定名字: 优先显式指定, 其次读 pet.json 里的 name, 最后用文件名
        base = new_name
        if not base:
            try:
                with zf.open(entries["pet.json"]) as f:
                    base = json.load(f).get("name")
            except (json.JSONDecodeError, OSError, KeyError):
                base = None
        base = _safe_name(base or os.path.splitext(os.path.basename(pet_path))[0])

        target = os.path.join(pets_root, base)
        if os.path.exists(target):
            if not overwrite:
                raise PetPackError(f"已经有叫「{base}」的桌宠了")
            # 覆盖时把旧的整包清掉: 存档跟着被覆盖的那只走, 不该留下来混进新的
            import shutil
            shutil.rmtree(target, ignore_errors=True)

        os.makedirs(target, exist_ok=True)
        for rel, raw in entries.items():
            if rel.split("/")[0] not in ALLOWED_TOP:    # 白名单之外直接丢弃
                continue
            dst = os.path.join(target, *rel.split("/"))
            # 最后一道: 确认落到 target 里面 (前面的检查已经排除 .., 这里是保险)
            if not os.path.abspath(dst).startswith(os.path.abspath(target) + os.sep):
                raise PetPackError(f"路径越界: {rel}")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with zf.open(raw) as src, open(dst, "wb") as out:
                out.write(src.read())
    return target


def safe_name(name: str) -> str:
    """把名字清理成安全、合法的目录名 (也用于校验用户输入的宠物名)。"""
    cleaned = "".join(c for c in str(name)
                      if c not in r'\/:*?"<>|').strip().strip(".")
    return cleaned[:40] or "imported"


# 兼容内部旧调用
_safe_name = safe_name


def describe(pet_dir: str) -> str:
    """给用户看的一句话概况。"""
    try:
        with open(os.path.join(pet_dir, "pet.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        cfg = {}
    states, sounds = [], []
    anim = os.path.join(pet_dir, "animation")
    if os.path.isdir(anim):
        states = [os.path.splitext(f)[0] for f in sorted(os.listdir(anim))
                  if f.lower().endswith(".gif")]
    snd = os.path.join(pet_dir, "sounds")
    if os.path.isdir(snd):
        sounds = [os.path.splitext(f)[0] for f in sorted(os.listdir(snd))
                  if os.path.splitext(f)[1].lower() in (".mp3", ".wav", ".ogg")]
    size = sum(os.path.getsize(os.path.join(dp, f))
               for dp, _, fs in os.walk(pet_dir) for f in fs)
    parts = [f"名字: {cfg.get('name', '(未命名)')}"]
    parts.append(f"体积: {size/1048576:.1f} MB")
    parts.append("动作: " + ("、".join(states) if states else "无(只有静态图)"))
    parts.append("音效: " + ("、".join(sounds) if sounds else "无"))
    return "\n".join(parts)


# ---------------------------------------------------------------- 命令行
def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="桌宠资源包导入/导出 (.pet)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="把资源包打成 .pet")
    e.add_argument("pet_dir")
    e.add_argument("out", nargs="?", help="输出文件 (默认 <名字>.pet)")

    i = sub.add_parser("import", help="把 .pet 装进 pets/")
    i.add_argument("pet_file")
    i.add_argument("--pets", default=None, help="pets 目录")
    i.add_argument("--as", dest="new_name", default=None, help="换个名字装")
    i.add_argument("--force", action="store_true", help="同名时覆盖")

    v = sub.add_parser("check", help="检查一个资源包")
    v.add_argument("pet_dir")

    args = ap.parse_args(argv)
    if args.cmd == "export":
        out = args.out or os.path.splitext(os.path.basename(
            os.path.normpath(args.pet_dir)))[0] + ".pet"
        path, n = export_pet(args.pet_dir, out)
        print(f"已导出 {path}（{n} 个文件，{os.path.getsize(path)/1048576:.1f} MB）")
        print("存档(status.json/work.json)已自动剔除，发给别人不会带上你的养成记录。")
        return 0
    if args.cmd == "import":
        root = args.pets
        if not root:
            import app_paths
            root = app_paths.pets_dir()
        meta = peek(args.pet_file)
        print("即将导入:", meta.get("name") or os.path.basename(args.pet_file))
        got = import_pet(args.pet_file, root, overwrite=args.force,
                         new_name=args.new_name)
        print("已装入:", got)
        print(describe(got))
        return 0
    problems = validate(args.pet_dir)
    if problems:
        print("有问题：")
        for p in problems:
            print("  -", p)
        return 1
    print("资源包看起来没问题：")
    print(describe(args.pet_dir))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
