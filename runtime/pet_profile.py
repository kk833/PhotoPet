"""人物档案 (v0.11)。

给桌宠一个"人"的身份 —— 姓名、称号、生日、相识日、相恋日。这些不是养成数值,
是**纪念意义**: 陪伴第几天、生日还有几天、在一起多久。

设计参考 (只借设计, 不搬代码):
- DyberPet 的 `days`/`last_opened`: 陪伴天数按"天"结算, 同一天不重复计
- pet-reminder 的事件字段: 生日 = 一条"每年重复"的普通事件, 提醒时刻 =
  事件时间 − 提前量。本模块只算"还有几天", 开口时机留给提醒层
- AIRI 把生日写进人格提示词: 生日属于"人设"本身, 不是提醒器的配置项

字段放在 pet.json 的 `profile` 块 —— **不能占用 `persona`**: 那是喂给 AIChat 的
system prompt 字符串 (pet_ai.py:117), 改成结构化对象会让 AI 对话直接出错。

时间语义:
- `meet_date` 不填时退回 work.json 的 first_day, 所以"陪伴第 N 天"通常有值
- 生日/纪念日按**每年重复**算; 今天正好是当天时返回 0 天, 而不是 364/365
- 2/29 在平年按 2/28 过 —— 概率很低, 但不能让面板显示"还有 1460 天"
- 只写月日 (如 "03-14") 也能算倒计时, 但缺年份就算不出"已经多少天"
- `birthday_calendar: "lunar"` 时生日按**农历**过: 农历生日对应的公历日每年都变
  (农历十月十九 = 2025-12-08 / 2026-11-27 / 2027-11-16), 换算见 pet_lunar

这个模块不读时钟以外的任何状态、不改任何数值 —— 档案是只读展示。
"""
import calendar
import json
import os
from datetime import date

import pet_lunar

# 生日用闰年做校验基准, 好让 "02-29" 这种只写月日的写法能通过
_LEAP_BASE_YEAR = 2000


def parse_month_day(text) -> tuple[int, int, int | None] | None:
    """解析 "YYYY-MM-DD" / "MM-DD" -> (月, 日, 年或 None)。格式不对返回 None。

    宽容处理常见的分隔符差异 ("2026/09/14"、"2026.9.14" 都认), 因为档案是
    用户手写进 pet.json 的, 为一个斜杠让面板空着不值得。
    """
    if not isinstance(text, str):
        return None
    parts = [p for p in text.strip().replace("/", "-").replace(".", "-")
             .split("-") if p != ""]
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        month, day, year = nums[0], nums[1], None
    elif len(nums) == 3:
        year, month, day = nums
    else:
        return None
    if not (1 <= month <= 12) or not (1 <= day <= 31):
        return None
    probe_year = _LEAP_BASE_YEAR if year is None else year
    if day > calendar.monthrange(probe_year, month)[1]:
        return None
    return month, day, year


def _occurs_in(year: int, month: int, day: int) -> date:
    """某个"每年重复"的日子落在指定年份的哪一天。2/29 在平年落到 2/28。"""
    day = min(day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def next_occurrence(month: int, day: int, today: date) -> tuple[date, int]:
    """下一次(含今天)的日期, 以及还有几天。今天就是当天时返回 0。"""
    for year in (today.year, today.year + 1):
        when = _occurs_in(year, month, day)
        if when >= today:
            return when, (when - today).days
    # 逻辑上到不了 (次年的同月日一定 >= 今天), 留个稳妥的兜底
    when = _occurs_in(today.year + 1, month, day)
    return when, (when - today).days


def _age(year: int, month: int, day: int, today: date) -> int | None:
    """今年的周岁 (生日还没到就减 1)。年份填得比现在还晚 -> None。"""
    age = today.year - year
    if (month, day) > (today.month, today.day):
        age -= 1
    return age if age >= 0 else None


class PetProfile:
    """一只宠物的人物档案。只读: 不改状态、不触发说话 (见 pet-quiet-by-default)。"""

    def __init__(self, pet_dir: str, config: dict | None = None,
                 first_day: str = ""):
        self.pet_dir = pet_dir
        self.config = config or {}
        raw = self.config.get("profile")
        if not isinstance(raw, dict):
            raw = {}                     # 写错格式当作没填, 不让桌宠起不来
        self.raw = raw

        self.name = str(raw.get("name")
                        or self.config.get("name") or "").strip()
        self.title = str(raw.get("title") or "").strip()
        self.intro = str(raw.get("intro") or "").strip()
        self.birthday = parse_month_day(raw.get("birthday"))
        # 生日按哪个历过: "solar"(默认) / "lunar"。见下面 lunar_birth 的解析规则
        self.birthday_calendar = str(
            raw.get("birthday_calendar") or "solar").strip().lower()
        self.meet_date = parse_month_day(raw.get("meet_date"))
        self.love_date = parse_month_day(raw.get("love_date"))
        self.first_day = str(first_day or "").strip() or self._read_first_day()
        # 相识日: 没填就退回陪伴记录的第一天 —— "陪伴第 N 天"是这只宠物**一定**有的
        # 信息, 不该因为档案空着就连它一起不显示。
        # 注意: 用户只写月日 ("09-10") 时**不要**退回 first_day —— 那是他自己填的相识日,
        # 拿 first_day 顶替会把"陪伴第 N 天"算成一个错的起点; 天数算不出就不显示,
        # 但周年倒计时照样能用 (_days_since 自己会因缺年份返回 None)。
        self.lunar_birth, self.lunar_birth_year = self._parse_lunar_birth()
        self.meet = self.meet_date or parse_month_day(self.first_day)

    def _parse_lunar_birth(self) -> tuple[tuple[int, int, bool] | None, int | None]:
        """农历生日 -> ((农历月, 农历日, 是否闰月), 出生农历年)。公历生日返回 (None, None)。

        `birthday_calendar: "lunar"` 时 birthday 有两种写法, 按**有没有年份**区分:
        - `"2001-12-03"` (带年) -> 当作**公历出生日**, 自动推出对应的农历月日。
          用户通常只知道公历出生日期、但按农历过生日, 这种写法最省事
        - `"10-19"` (只写月日) -> 当作**农历月日**本身。适合只记得农历生日的长辈
          这种写法没有年份, 所以算不出年龄
        """
        if not self.birthday or self.birthday_calendar not in ("lunar", "农历", "阴历"):
            return None, None
        month, day, year = self.birthday
        if year is None:
            return (month, day, False), None
        try:
            conv = pet_lunar.solar_to_lunar(date(year, month, day))
        except ValueError:
            return None, None
        if not conv:
            return None, None           # 超出生效范围 -> 退回按公历显示
        return (conv[1], conv[2], conv[3]), conv[0]

    # ---------- 兜底: 没填相识日就用陪伴记录的第一天 ----------
    def _read_first_day(self) -> str:
        try:
            with open(os.path.join(self.pet_dir, "work.json"),
                      encoding="utf-8") as f:
                return str(json.load(f).get("first_day", "") or "")
        except (OSError, json.JSONDecodeError, ValueError):
            return ""

    # ---------- 天数 ----------
    @staticmethod
    def _days_since(start: tuple[int, int, int | None], today: date) -> int | None:
        """从某天到今天 (含头含尾) 是第几天。缺年份/日期在未来 -> None。"""
        if not start or start[2] is None:
            return None
        try:
            begin = date(start[2], start[0], start[1])
        except ValueError:
            return None
        if begin > today:
            return None
        return (today - begin).days + 1

    def days_together(self, today: date | None = None) -> int | None:
        """相识第 N 天 (含今天)。优先 profile.meet_date, 否则 work.json.first_day。"""
        return self._days_since(self.meet, today or date.today())

    def days_in_love(self, today: date | None = None) -> int | None:
        today = today or date.today()
        return self._days_since(self.love_date, today)

    # ---------- 展示文本 ----------
    @staticmethod
    def _date_text(parsed: tuple[int, int, int | None]) -> str:
        month, day, year = parsed
        return (f"{year}-{month:02d}-{day:02d}" if year is not None
                else f"{month} 月 {day} 日")

    def _countdown_text(self, parsed, today: date, anniversary: bool = False) -> str:
        """倒计时文案。纪念日要写明是"周年" —— 光说"还有 37 天"没人知道是什么日子。"""
        _, days_left = next_occurrence(parsed[0], parsed[1], today)
        if days_left == 0:
            return "🎉 今天就是周年" if anniversary else "🎉 就是今天"
        # 用「距周年」而不是「周年还有」: 后者 4 个字正好卡在卡片宽度上,
        # 会把"还有"拆到下一行 ("周年还 / 有 364 天")
        return (f"距周年 {days_left} 天" if anniversary
                else f"还有 {days_left} 天")

    def birthday_text(self, today: date | None = None) -> str:
        """例: '2000-03-14 · 26 岁 · 还有 180 天' / '农历十月十九 · 公历 11-27 · 24 岁 · 还有 73 天'"""
        if not self.birthday:
            return ""
        today = today or date.today()
        if self.lunar_birth:
            return self._lunar_birthday_text(today)
        bits = [self._date_text(self.birthday)]
        if self.birthday[2] is not None:
            # 注意顺序: birthday 存的是 (月, 日, 年), _age 要的是 (年, 月, 日)
            month, day, year = self.birthday
            age = _age(year, month, day, today)
            if age is not None:
                bits.append(f"{age} 岁")
        bits.append(self._countdown_text(self.birthday, today))
        return " · ".join(bits)

    def _lunar_birthday_text(self, today: date) -> str:
        month, day, is_leap = self.lunar_birth
        named = pet_lunar.format_lunar(month, day, is_leap)
        when = pet_lunar.next_lunar_date(month, day, today, prefer_leap=is_leap)
        if when is None:
            return named            # 取不到日期就只报名字, 不编一个出来
        days_left = (when - today).days
        # 农历生日对应的公历日**每年都变**, 所以必须把"这次是哪天"写出来,
        # 否则用户拿到"还有 73 天"也不知道该在哪天订蛋糕。
        # 落今年写"今年", 落明年就直接写年份 —— 只写 "11-16" 会被当成今年那个
        # 已经过去的 11 月 16 日 (生日刚过的第二天就会看到这种情况)
        if when.year == today.year:
            head = f"{named} · 今年 {when.month:02d}-{when.day:02d}"
        else:
            head = f"{named} · {when.year}-{when.month:02d}-{when.day:02d}"
        second = []
        age = self._lunar_age(when, days_left)
        if age is not None:
            second.append(f"{age} 岁")
        second.append("🎉 就是今天" if days_left == 0 else f"还有 {days_left} 天")
        lines = [head, " · ".join(second)]
        # 必须把**公历出生日**也写出来: 否则卡片上从头到尾没有用户填的那个日期,
        # 看到"今年 11-27"会以为程序把公历生日算错了 (真实踩过 —— 用户填的是
        # 公历 12-03, 而农历十月十九今年落在 11-27, 两个数字对不上就懵了)
        if self.birthday and self.birthday[2] is not None:
            month, day, year = self.birthday
            lines.append(f"公历 {year}-{month:02d}-{day:02d} 出生")
        return "\n".join(lines)

    def _lunar_age(self, when: date, days_left: int) -> int | None:
        """农历年龄: 过生日那天长一岁, 所以生日没到要减 1。"""
        if self.lunar_birth_year is None:
            return None
        occ = pet_lunar.solar_to_lunar(when)
        if not occ:
            return None
        age = occ[0] - self.lunar_birth_year - (1 if days_left > 0 else 0)
        return age if age >= 0 else None

    def meet_text(self, today: date | None = None) -> str:
        """例: '2026-09-14 · 第 2 天 · 相识纪念日还有 364 天'"""
        if not self.meet:
            return ""
        today = today or date.today()
        bits = [self._date_text(self.meet)]
        days = self.days_together(today)
        if days is not None:
            bits.append(f"第 {days} 天")
        bits.append(self._countdown_text(self.meet, today, anniversary=True))
        return " · ".join(bits)

    def love_text(self, today: date | None = None) -> str:
        if not self.love_date:
            return ""
        today = today or date.today()
        bits = [self._date_text(self.love_date)]
        days = self.days_in_love(today)
        if days is not None:
            bits.append(f"第 {days} 天")
        bits.append(self._countdown_text(self.love_date, today, anniversary=True))
        return " · ".join(bits)

    # ---------- 给 UI 的行 ----------
    @property
    def filled(self) -> bool:
        """填过任何一项没有。全空时面板改成显示填写指引。"""
        return bool(self.title or self.intro or self.birthday
                    or self.meet_date or self.love_date)

    def rows(self, today: date | None = None) -> list[tuple[str, str, str]]:
        """(图标, 标签, 值)。UI 只负责摆, 文案逻辑都在这里, 方便脱离 Qt 测。"""
        today = today or date.today()
        # 没填相识日时那一行来自 work.json 的 first_day, 是**宠物**陪伴你的天数,
        # 不是"你认识她多久"。两者混在一张卡上会自相矛盾 (相识第 2 天 vs 相恋第
        # 1060 天), 所以兜底来的那行改叫「陪伴」
        meet_label = "相识" if self.meet_date else "陪伴"
        out = []
        for icon, label, parsed, text in (
            ("🎂", "生日", self.birthday, self.birthday_text(today)),
            ("🗓", meet_label, self.meet, self.meet_text(today)),
            ("💗", "相恋", self.love_date, self.love_text(today)),
        ):
            if parsed and text:
                out.append((icon, label, text))
        return out

    def header(self) -> str:
        """'小陪 · 你的专属桌宠' (只有称号时也不留空点)。"""
        return " · ".join(p for p in (self.name, self.title) if p)
