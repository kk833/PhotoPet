# -*- coding: utf-8 -*-
"""重新生成 runtime/pet_lunar.py 里的农历数据表 (开发者工具, 不进 exe)。

    pip install borax
    python scripts/gen_lunar_table.py           # 直接改写 runtime/pet_lunar.py
    python scripts/gen_lunar_table.py --check   # 只校验现状, 不改文件

什么时候需要跑它: 基本不需要。农历数据是固定的天文事实, 1900~2099 已经不会再变。
只有在**想扩大年份范围**时才需要重跑 (注意 borax 只到 2100)。

为什么要单独写个生成脚本, 而不是直接依赖 borax:
  - 发布版是打包成 exe 给不想装 Python 的人用的, 少一个依赖少一层麻烦
  - 但也因此, 这张表必须**可重现** —— 否则以后没人说得清那串数字是怎么来的

许可说明 (重要):
  - `borax` 是 MIT, 用它生成数据没问题
  - 但**不要**改用 `lunardate`: 它在 PyPI 上标 MIT, 源码头部却写明 derived from
    the GPLv2 `lunar` project。本项目是 MIT, 抄它的表或打进 exe 都会污染许可
"""
import argparse
import io
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "runtime"))

import app_paths  # noqa: E402
app_paths.fix_console_encoding()

import pet_lunar  # noqa: E402

TARGET = os.path.join(app_paths.BUNDLE_DIR, "runtime", "pet_lunar.py")
Y0, Y1 = pet_lunar.YEAR_MIN, pet_lunar.YEAR_MAX
BASE = pet_lunar.BASE


def build() -> tuple[str, str, str]:
    """从 borax 生成三段数据字符串。"""
    from borax.calendars.lunardate import LunarDate

    def is_leap(y: int, m: int) -> bool:
        try:
            return LunarDate(y, m, 1, 1).leap == 1
        except Exception:                    # noqa: BLE001  该年没闰这个月 -> 抛异常
            return False

    new_year, leap, lengths = [], [], []
    for y in range(Y0, Y1 + 1):
        new_year.append((LunarDate(y, 1, 1).to_solar_date() - BASE).days)
        lp = next((m for m in range(1, 13) if is_leap(y, m)), 0)
        leap.append(lp)
        order = []
        for m in range(1, 13):
            order.append((m, 0))
            if lp == m:
                order.append((m, 1))
        # 末月的月长要用**下一年的正月初一**, 所以 borax 的年份上限必须比 YEAR_MAX 大
        starts = [LunarDate(y, m, 1, l).to_solar_date() for m, l in order]
        starts.append(LunarDate(y + 1, 1, 1).to_solar_date())
        bits = 0
        for i in range(len(order)):
            if (starts[i + 1] - starts[i]).days == 30:
                bits |= (1 << i)
        lengths.append(bits)
    return (",".join(str(v - new_year[0]) for v in new_year),
            "".join("0123456789abc"[v] for v in leap),
            ",".join(format(v, "x") for v in lengths))


def verify() -> int:
    """把当前 runtime/pet_lunar.py 与 borax 逐日比对 (含闰月月长)。"""
    from borax.calendars.lunardate import LunarDate
    bad = 0
    d, end = date(Y0, 1, 31), date(Y1, 12, 31)
    n = 0
    while d <= end:
        ref = LunarDate.from_solar_date(d.year, d.month, d.day)
        if pet_lunar.solar_to_lunar(d) != (ref.year, ref.month, ref.day, bool(ref.leap)):
            print(f"  [X] 公历 {d} 换算不符")
            bad += 1
        n += 1
        d = date.fromordinal(d.toordinal() + 1)
    for y in range(Y0, Y1 + 1):
        for m, is_leap, days in pet_lunar.months_of(y):
            if pet_lunar.lunar_to_solar(y, m, days, is_leap) is None:
                print(f"  [X] 农历 {y}-{m} 第 {days} 天不存在")
                bad += 1
            if pet_lunar.lunar_to_solar(y, m, days + 1, is_leap) is not None:
                print(f"  [X] 农历 {y}-{m} 多了第 {days + 1} 天")
                bad += 1
    print(f"逐日比对 {n} 天 + 月长上下界, 不符 {bad} 处")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验, 不改文件")
    args = ap.parse_args()
    if args.check:
        return 1 if verify() else 0

    ny, le, ln = build()
    src = io.open(TARGET, encoding="utf-8").read()
    for name, value in (("_NEW_YEAR", ny), ("_LEAP", le), ("_LENGTHS", ln)):
        pat = re.compile(rf'^{name} = ".*?"$', re.M)
        if not pat.search(src):
            print(f"在 {TARGET} 里找不到 {name} 那一行")
            return 1
        src = pat.sub(f'{name} = "{value}"', src)
    io.open(TARGET, "w", encoding="utf-8").write(src)
    print(f"已写入 {TARGET}")
    print(f"  年份 {Y0}~{Y1}: 偏移表 {len(ny)} 字符 / 闰月 {len(le)} / 月长 {len(ln)}")
    print(f"  闰月年 {sum(1 for c in le if c != '0')} 个")
    return 1 if verify() else 0


if __name__ == "__main__":
    sys.exit(main())
