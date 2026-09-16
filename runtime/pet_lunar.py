"""农历换算 (v0.11) —— 为了"生日按农历过"这一件事。

有人过公历生日, 有人过农历生日。农历生日对应的公历日期**每年都不一样**
(比如农历十月十九: 2024-11-19 / 2025-12-08 / 2026-11-27), 所以"还有几天生日"
必须查表换算, 不能拿公历月日去减。

**为什么不用现成的农历库当依赖**:
- `lunardate` 在 PyPI 上标 MIT, 但源码头部明确写着 derived from the GPLv2 `lunar`
  project —— 本项目是 MIT, 抄它的表或把它打进 exe 都会污染许可, 所以不用
- `borax`(MIT)/`cnlunar`(MIT) 可以用, 但为了 exe 不背额外依赖, 这里只**用它生成
  数据、把事实数据内嵌**。数据是天文事实(每年正月初一落在哪天、哪个月大哪个月小、
  哪一年有闰月), 编码格式是本项目自己的, 没有复制任何库的表结构

数据范围 1900~2099。超出范围返回 None, 由调用方当作"没填"处理 —— 一只桌宠不该
因为用户手滑写了 1832 年就起不来。
(末年是 2099 而不是 2100: 算一年的月大月小要用到**下一年的正月初一**, 而源数据只到
2100, 所以 2100 那年的月长算不出来。没有桌宠能活到 2100, 不值得为它特殊处理。)

编码(三段字符串, 共约 2.2KB):
- `_NEW_YEAR`: 每年正月初一相对 1900-01-31 的天数, 逗号分隔
- `_LEAP`:     每年的闰月月号, 0 = 没闰月; a/b/c = 闰十月/十一月/十二月
- `_LENGTHS`:  每年 13 位的位掩码(十六进制), 第 i 位 = 1 表示"该年第 i 个月 30 天"
               (月份顺序含闰月: 闰月紧跟在它所闰的那个月之后)
"""
from datetime import date, timedelta

YEAR_MIN, YEAR_MAX = 1900, 2099
BASE = date(1900, 1, 31)        # 1900 年正月初一

# --- 数据段由 scripts/gen_lunar_table.py 生成, 勿手改 (要改跑那个脚本) ---
_NEW_YEAR = "0,384,738,1093,1476,1830,2185,2569,2923,3278,3662,4016,4400,4754,5108,5492,5846,6201,6585,6940,7324,7678,8032,8416,8770,9124,9509,9863,10218,10602,10956,11339,11693,12048,12432,12787,13141,13525,13879,14263,14617,14971,15355,15710,16064,16449,16803,17157,17541,17895,18279,18633,18988,19372,19726,20081,20465,20819,21202,21557,21911,22295,22650,23004,23388,23743,24096,24480,24835,25219,25573,25928,26312,26666,27020,27404,27758,28142,28496,28851,29235,29590,29944,30328,30682,31066,31420,31774,32158,32513,32868,33252,33606,33960,34343,34698,35082,35436,35791,36175,36529,36883,37267,37621,37976,38360,38714,39099,39453,39807,40191,40545,40899,41283,41638,42022,42376,42731,43115,43469,43823,44207,44561,44916,45300,45654,46038,46392,46746,47130,47485,47839,48223,48578,48962,49316,49670,50054,50408,50762,51146,51501,51856,52240,52594,52978,53332,53686,54070,54424,54779,55163,55518,55902,56256,56610,56993,57348,57702,58086,58441,58795,59179,59533,59917,60271,60626,61010,61364,61719,62103,62457,62841,63195,63549,63933,64288,64642,65026,65381,65735,66119,66473,66857,67211,67566,67950,68304,68659,69042,69396,69780,70134,70489,70873,71228,71582,71966,72320,72674"
_LEAP = "800500400206005002070050040020600500307006004002070050030800600400307005004080060040a006005003080050040020700500409006004002060050030b006005002070050030800600400307005004080060040030700500408006004002"
_LENGTHS = "16d2,752,ea5,164a,64b,a9b,1556,56a,b59,1752,752,1b25,b25,a4b,14ab,2ad,56b,b69,da9,1d92,e92,d25,1a4d,a56,2b6,15b5,6d4,ea9,1e92,e92,d26,52b,a57,12b6,b5a,6d4,ec9,749,1693,a93,52b,a5b,aad,56a,1b55,ba4,b49,1a93,a95,152d,536,aad,15aa,5b2,da5,1d4a,d4a,a95,a97,556,ab5,ad5,6d2,ea5,ea5,64a,c97,a9b,155a,56a,b69,1752,b52,b25,164b,a4b,14ab,2ad,56d,b69,da9,d92,1d25,d25,1a4d,a56,2b6,5b5,6d5,ea9,1e92,e92,d26,a56,a57,14d6,35a,6d5,16c9,749,693,152b,52b,a5b,155a,56a,1b55,ba4,b49,1a93,a95,52d,aad,ab5,15aa,5d2,da5,1d4a,d4a,c95,152e,556,ab5,15b2,6d2,ea5,725,64b,c97,cab,55a,ad6,b69,1752,b52,b25,1a4b,a4b,4ab,55b,5ad,b6a,1b52,d92,1d25,d25,a55,14ad,4b6,5b5,daa,ec9,1e92,e92,d26,a56,a57,4d6,6d5,755,749,e93,693,152b,52b,a5b,155a,56a,b65,174a,b4a,1a95,a95,52d,aad,ab5,5aa,ba5,da5,d4a,1c95,c96,194e,556,ab5,15b2,6d2,ea5,e4a,64b,c97,4ab,55b,ad6,b6a,752,1725,b25,a8b,149b"

_OFFSETS = [int(v) for v in _NEW_YEAR.split(",")]
_LEAP_MONTHS = [int(c, 16) for c in _LEAP]
_LENGTH_BITS = [int(v, 16) for v in _LENGTHS.split(",")]


def _check_range(year: int) -> bool:
    return YEAR_MIN <= year <= YEAR_MAX


# ---------- 中文写法 (「农历十月十九」) ----------
_MONTH_NAMES = ["", "正月", "二月", "三月", "四月", "五月", "六月",
                "七月", "八月", "九月", "十月", "十一月", "十二月"]
_CN = "一二三四五六七八九十"


def day_name(day: int) -> str:
    """1~30 -> 初一…初十 / 十一…十九 / 二十 / 廿一…廿九 / 三十。

    用「廿」不用「二十一」是农历的通用写法, 看着才像那么回事。
    """
    if not 1 <= day <= 30:
        return ""
    if day < 10:
        return "初" + _CN[day - 1]
    if day == 10:
        return "初十"
    if day < 20:
        return "十" + _CN[day - 11]
    if day == 20:
        return "二十"
    if day < 30:
        return "廿" + _CN[day - 21]
    return "三十"


def month_name(month: int) -> str:
    return _MONTH_NAMES[month] if 1 <= month <= 12 else ""


def format_lunar(month: int, day: int, is_leap: bool = False) -> str:
    """(月, 日, 是否闰月) -> '农历十月十九' / '农历闰五月十五'。非法返回 ''。"""
    if not month_name(month) or not day_name(day):
        return ""
    return f"农历{'闰' if is_leap else ''}{month_name(month)}{day_name(day)}"


def new_year_date(year: int) -> date | None:
    """某农历年的正月初一对应的公历日。"""
    if not _check_range(year):
        return None
    return BASE + timedelta(days=_OFFSETS[year - YEAR_MIN])


def leap_month(year: int) -> int:
    """某农历年的闰月月号 (0 = 没有闰月)。"""
    return _LEAP_MONTHS[year - YEAR_MIN] if _check_range(year) else 0


def months_of(year: int) -> list[tuple[int, bool, int]]:
    """某农历年的月份表: [(月号, 是否闰月, 天数), …], 按时间顺序。

    有闰月时, 闰月紧跟在同名月份后面 (闰六月排在六月之后)。
    """
    if not _check_range(year):
        return []
    bits = _LENGTH_BITS[year - YEAR_MIN]
    lp = leap_month(year)
    out = []
    for m in range(1, 13):
        for is_leap in ((False, True) if lp == m else (False,)):
            idx = len(out)
            out.append((m, is_leap, 30 if bits >> idx & 1 else 29))
    return out


def month_days(year: int, month: int, is_leap: bool = False) -> int:
    """某个月有多少天 (29 或 30)。找不到该月返回 0。"""
    for m, lp, days in months_of(year):
        if m == month and lp == is_leap:
            return days
    return 0


def has_leap_month(year: int, month: int) -> bool:
    return leap_month(year) == month


def solar_to_lunar(d: date) -> tuple[int, int, int, bool] | None:
    """公历 -> (农历年, 月, 日, 是否闰月)。超出数据范围返回 None。"""
    year = d.year
    # 正月初一之前的日子属于上一个农历年 (公历 1 月大多是这种情况)
    ny = new_year_date(year)
    if ny is None:
        return None
    if d < ny:
        year -= 1
        ny = new_year_date(year)
        if ny is None:
            return None
    left = (d - ny).days
    for month, is_leap, days in months_of(year):
        if left < days:
            return year, month, left + 1, is_leap
        left -= days
    return None                 # 数据表有洞才会走到这儿


def lunar_to_solar(year: int, month: int, day: int,
                   is_leap: bool = False) -> date | None:
    """(农历年, 月, 日) -> 公历日。该年没有这个日期就返回 None。"""
    ny = new_year_date(year)
    if ny is None or day < 1:
        return None
    left = 0
    for m, lp, days in months_of(year):
        if m == month and lp == is_leap:
            return ny + timedelta(days=left + day - 1) if day <= days else None
        left += days
    return None


def next_lunar_date(month: int, day: int, today: date,
                    prefer_leap: bool = False) -> date | None:
    """农历 (月, 日) 在 today 当天或之后**最近一次**落到的公历日。

    两个现实里会遇到的情况, 这里都做了兜底:
    - 该农历年这个月只有 29 天, 而生日是三十 -> 退到当月最后一天 (过廿九)
    - 出生在闰月的人, 遇到没有闰月的年份 -> 按非闰的同名月份过
    找不到 (超出 1900~2100) 返回 None。
    """
    for year in (today.year, today.year + 1, today.year + 2):
        # 农历年可能跨公历两年, 所以往前多看一年
        for y in (year - 1, year):
            when = _resolve_in_year(y, month, day, prefer_leap)
            if when is not None and when >= today:
                return when
    return None


def _resolve_in_year(year: int, month: int, day: int,
                     prefer_leap: bool) -> date | None:
    """在某个农历年里, 把 (月, 日) 解析成公历日。"""
    if not _check_range(year):
        return None
    is_leap = prefer_leap and has_leap_month(year, month)
    days = month_days(year, month, is_leap)
    if not days:                      # 该年没有这个月 (闰月年才会出现)
        is_leap = False
        days = month_days(year, month, False)
        if not days:
            return None
    return lunar_to_solar(year, month, min(day, days), is_leap)
