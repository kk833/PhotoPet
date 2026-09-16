"""日程 (v0.12) —— 把"下一个安排"交给桌宠记着。

三件事：**存事件**、**算下一次什么时候**、**什么时候该开口**。

设计参考 (只借设计, 不搬代码):
- kaiCATs/pet-reminder 的事件字段: title / 年月日时分 / remind_before_minutes /
  repeat(no_repeat|every_day|every_week|every_month|every_year), 以及用
  `notified = {事件id: 日期}` 做"这次已经提醒过"的去重
- Zoria-Lind/desktop-pet-reminder(MIT) 的两个产品细节: **提前量预告**(prewarn)而不是
  到点才叫; **开机时把错过的提醒汇总成一条**, 而不是逐个重放(防轰炸)

存档: `<用户数据目录>/schedule.json` —— **不放在 pets/<名字>/ 下**, 因为日程是
"人的事"不是"某只宠物的事": 同时养两只宠物时不该各提醒一遍。

时间语义:
- 全部用**本地时间**, 不存时区 (桌宠够用; 将来要同步 Google 日历再说)
- `time` 省略时按全天处理, 提醒落在 `DEFAULT_ALL_DAY_HOUR` 点
- 重复事件的边界: "每月 31 号"遇到没有 31 号的月份**顺延到当月最后一天**,
  而不是跳过整个月 —— 跳过的话"每月最后一天交房租"就没法表达了
  (注意这和 dateutil.rrule 的默认行为相反, rrule 是跳过)
"""
import calendar
import json
import os
import time
import uuid
from datetime import date, datetime, timedelta

REPEATS = ("none", "daily", "weekly", "monthly", "yearly")
REPEAT_NAMES = {
    "none": "只一次",
    "daily": "每天",
    "weekly": "每周",
    "monthly": "每月",
    "yearly": "每年",
}
DEFAULT_ALL_DAY_HOUR = 9        # 全天事件在几点提醒
DEFAULT_REMIND_BEFORE = 15      # 默认提前几分钟开口
MAX_LOOKAHEAD_STEPS = 400       # 重复事件往前找的步数上限, 防死循环


# ---------- 时间小工具 ----------
def _clamp_to_month(year: int, month: int, day: int) -> int:
    """把"某月某日"夹进该月实际天数 (2 月没有 31 号 -> 28/29)。"""
    return min(day, calendar.monthrange(year, month)[1])


def _add_months(dt: datetime, months: int) -> datetime:
    total = (dt.year * 12 + dt.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    return dt.replace(year=year, month=month,
                      day=_clamp_to_month(year, month, dt.day))


def _add_years(dt: datetime, years: int) -> datetime:
    year = dt.year + years
    return dt.replace(year=year, day=_clamp_to_month(year, dt.month, dt.day))


def parse_when(date_text: str, time_text: str = "") -> datetime | None:
    """"2026-09-16" + "14:30" -> datetime。也认 "MM-DD"(年份取今年)。

    返回 None 表示格式不对 —— 手写的 json 什么都有可能, 不该让桌宠起不来。
    """
    if not isinstance(date_text, str):
        return None
    parts = [p for p in date_text.strip().replace("/", "-").replace(".", "-")
             .split("-") if p != ""]
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        year, month, day = date.today().year, nums[0], nums[1]
    elif len(nums) == 3:
        year, month, day = nums
    else:
        return None
    hour, minute = DEFAULT_ALL_DAY_HOUR, 0
    if isinstance(time_text, str) and time_text.strip():
        tparts = time_text.strip().replace("：", ":").split(":")
        try:
            hour = int(tparts[0])
            minute = int(tparts[1]) if len(tparts) > 1 else 0
        except (ValueError, IndexError):
            return None
    try:
        return datetime(year, month, _clamp_to_month(year, month, day),
                        hour, minute)
    except ValueError:
        return None


def format_when(dt: datetime, with_date: bool = True, today: date | None = None) -> str:
    """人话时间: '今天 14:30' / '明天 09:00' / '9 月 20 日 14:30'。"""
    today = today or date.today()
    delta = (dt.date() - today).days
    if not with_date:
        return dt.strftime("%H:%M")
    if delta == 0:
        return f"今天 {dt:%H:%M}"
    if delta == 1:
        return f"明天 {dt:%H:%M}"
    if delta == 2:
        return f"后天 {dt:%H:%M}"
    if delta < 0:
        return f"{dt.month} 月 {dt.day} 日 {dt:%H:%M}"
    return f"{dt.month} 月 {dt.day} 日 {dt:%H:%M}"


def humanize_gap(minutes: int) -> str:
    """还有多久: 90 -> '1 小时 30 分'。"""
    if minutes < 0:
        minutes = 0
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时 {mins} 分" if mins else f"{hours} 小时"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时" if hours else f"{days} 天"


def group_by_time(items: list) -> list[tuple[datetime, list]]:
    """把同一时刻的安排并成一组。items 需已按时间排好序 (upcoming 就是)。

    为什么不直接排两行: 同时段的两场宣讲会是**冲突**关系, 排成两行看起来
    像"先开一场再开一场", 会让人误判自己两个都能去。
    """
    out: list[tuple[datetime, list]] = []
    for ev, when in items:
        if out and out[-1][0] == when:
            out[-1][1].append(ev)
        else:
            out.append((when, [ev]))
    return out


def titles_text(events: list, sep: str = "、", max_items: int = 3) -> str:
    """把同一时刻的几件事说成一行: 'A、B' / 'A 等 3 场'。"""
    names = [e.title for e in events]
    if len(names) <= max_items:
        return sep.join(names)
    return f"{sep.join(names[:max_items - 1])} 等 {len(names)} 场"


class Event:
    """一条日程。字段照 pet-reminder 的设计, 手写 json 也看得懂。"""

    def __init__(self, data: dict):
        self.id = str(data.get("id") or uuid.uuid4().hex[:8])
        self.title = str(data.get("title") or "（未命名）").strip()
        self.date = str(data.get("date") or "")
        self.time = str(data.get("time") or "")
        self.repeat = str(data.get("repeat") or "none").strip().lower()
        if self.repeat not in REPEATS:
            self.repeat = "none"
        try:
            self.remind_before = int(data.get("remind_before_minutes",
                                              DEFAULT_REMIND_BEFORE))
        except (TypeError, ValueError):
            self.remind_before = DEFAULT_REMIND_BEFORE
        self.remind_before = max(0, self.remind_before)
        self.note = str(data.get("note") or "").strip()
        # 详情页链接 (v0.23)。抓宣讲会时能拿到, 用户问"这场的详细信息"时要去读它。
        # 加字段对老存档是安全的: 缺这个键就是空串, 不影响读取
        self.url = str(data.get("url") or "").strip()
        self.enabled = bool(data.get("enabled", True))
        self.start = parse_when(self.date, self.time)

    @property
    def valid(self) -> bool:
        return self.start is not None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "date": self.date,
            "time": self.time, "repeat": self.repeat,
            "remind_before_minutes": self.remind_before,
            "note": self.note, "url": self.url, "enabled": self.enabled,
        }

    def describe_repeat(self) -> str:
        if self.repeat == "none":
            return ""
        weekday = "一二三四五六日"[self.start.weekday()] if self.start else ""
        if self.repeat == "weekly":
            return f"每周{weekday}"
        return REPEAT_NAMES[self.repeat]

    def next_occurrence(self, after: datetime) -> datetime | None:
        """after(含)之后最近一次发生的时间。已停用/单次且已过期 -> None。"""
        if not self.enabled or self.start is None:
            return None
        base = self.start
        if self.repeat == "none":
            return base if base >= after else None
        if self.repeat in ("daily", "weekly"):
            step = timedelta(days=1 if self.repeat == "daily" else 7)
            if base >= after:
                return base
            # 按整天数跳, 再对齐到 after 之后的第一刻
            span = (after - base) // step
            cand = base + step * max(0, span)
            while cand < after:
                cand += step
            return cand
        if self.repeat == "monthly":
            if base >= after:
                return base          # 还没开始, 下次就是它自己 (别提前拉到本月)
            cand = base.replace(year=after.year, month=after.month,
                                day=_clamp_to_month(after.year, after.month,
                                                    base.day))
            if cand < after:
                cand = _add_months(cand, 1)
            return cand
        if self.repeat == "yearly":
            if base >= after:
                return base          # 同上: 未来的第一次就照原样返回
            cand = _add_years(base, after.year - base.year)
            if cand < after:
                cand = _add_years(cand, 1)
            return cand
        return None


class Schedule:
    """全部日程 + 提醒去重状态。存一份 json, 人和机器都能读。"""

    def __init__(self, path: str):
        self.path = path
        self.events: list[Event] = []
        # {事件id: 已提醒过的那次发生时间(ISO)} —— 同一次不重复开口
        self.fired: dict[str, str] = {}
        self.last_seen = ""      # 上次退出时间, 用来算"错过了哪些"
        self.load()

    # ---------- 持久化 ----------
    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return          # 存档坏了就当空的, 不能让桌宠起不来
        for raw in (data.get("events") or []):
            if isinstance(raw, dict):
                ev = Event(raw)
                if ev.valid:
                    self.events.append(ev)
        fired = data.get("fired")
        if isinstance(fired, dict):
            self.fired = {str(k): str(v) for k, v in fired.items()}
        self.last_seen = str(data.get("last_seen") or "")

    def save(self):
        """写盘。**故意不动 last_seen** —— 它是"上次退出时间", 只在退出时更新。

        一开始写成每次 save 都刷新, 结果加个日程、提醒一次都会把它顶成"现在",
        开机补报就永远查不出"这段时间错过了什么" (被单测抓出来的)。
        """
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({
                    "events": [e.to_dict() for e in self.events],
                    "fired": self.fired,
                    "last_seen": self.last_seen,
                }, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ---------- 增删改 ----------
    def add(self, title: str, date_text: str, time_text: str = "",
            repeat: str = "none", remind_before: int = DEFAULT_REMIND_BEFORE,
            note: str = "", url: str = "") -> Event | None:
        ev = Event({"title": title, "date": date_text, "time": time_text,
                    "repeat": repeat, "remind_before_minutes": remind_before,
                    "note": note, "url": url})
        if not ev.valid:
            return None
        self.events.append(ev)
        self.save()
        return ev

    def find(self, keyword: str) -> list:
        """按标题找（先精确包含、再模糊），和删日程/找任务同一套。"""
        import pet_text
        return pet_text.best_match(keyword, self.enabled_events(),
                                   text_of=lambda e: e.title)

    def cleanup(self, keep_days: int = 7, today: date | None = None) -> int:
        """清掉**早就过去的一次性日程**，返回清掉的条数。

        两条边界想清楚才动的手:
        - **重复日程永远不清**: daily/weekly/monthly/yearly 永远有下一次,
          而且下次时间是从原始起点算出来的 —— 删了等于把整条重复规则也删了
        - **最近过去的留着**: 用户可能想回看"上周去了哪些宣讲会"。
          所以只清 `keep_days` 天以前的 (默认 7 天)

        秋招季一天十几场宣讲会, 不清的话 schedule.json 会一直涨。
        """
        today = today or date.today()
        cutoff = today - timedelta(days=max(0, keep_days))
        keep = []
        removed = 0
        for ev in self.events:
            old_once = (ev.repeat == "none" and ev.start is not None
                        and ev.start.date() < cutoff)
            if old_once:
                self.fired.pop(ev.id, None)     # 提醒去重记录一起清掉
                removed += 1
            else:
                keep.append(ev)
        if removed:
            self.events = keep
            self.save()
        return removed

    def stale_count(self, keep_days: int = 7, today: date | None = None) -> int:
        """有多少条是"已经过去、可以清了"的（给菜单显示用，不改数据）。"""
        today = today or date.today()
        cutoff = today - timedelta(days=max(0, keep_days))
        return sum(1 for ev in self.events
                   if ev.repeat == "none" and ev.start is not None
                   and ev.start.date() < cutoff)

    def has_similar(self, title: str, date_text: str, time_text: str = "") -> bool:
        """已经有一条一模一样的了吗 (导入文件时用来去重)。

        比的是"标题 + 解析后的时间", 不是原始日期字符串 —— 文件里写
        `2026/9/20`, 日程里存 `2026-09-20`, 字符串不同但是同一天。
        """
        target = parse_when(date_text, time_text)
        if target is None:
            return False
        for ev in self.events:
            if ev.title == title and ev.start == target:
                return True
        return False

    def remove(self, event_id: str) -> bool:
        before = len(self.events)
        self.events = [e for e in self.events if e.id != event_id]
        self.fired.pop(event_id, None)
        if len(self.events) != before:
            self.save()
            return True
        return False

    def enabled_events(self) -> list[Event]:
        return [e for e in self.events if e.enabled and e.valid]

    # ---------- 查询 ----------
    def upcoming(self, now: datetime | None = None,
                 limit: int = 0) -> list[tuple[Event, datetime]]:
        """所有事件的下一次发生时间, 按时间排序。"""
        now = now or datetime.now()
        out = []
        for ev in self.enabled_events():
            when = ev.next_occurrence(now)
            if when is not None:
                out.append((ev, when))
        out.sort(key=lambda pair: pair[1])
        return out[:limit] if limit else out

    def grouped(self, now: datetime | None = None,
                limit: int = 4) -> list[tuple[datetime, list]]:
        """按时刻分组的接下来安排: [(时刻, [事件, …]), …]。

        秋招季很常见: 同一时间开两场宣讲会。这种要**并排显示成一条**,
        而不是排成两行让人以为是两件事 —— 它们本来就是冲突的。
        """
        return group_by_time(self.upcoming(now, limit=limit))

    def next_event(self, now: datetime | None = None):
        """(事件, 发生时间, 还有几分钟)。没有日程返回 None。"""
        now = now or datetime.now()
        items = self.upcoming(now, limit=1)
        if not items:
            return None
        ev, when = items[0]
        return ev, when, int((when - now).total_seconds() // 60)

    def due(self, now: datetime | None = None) -> list[tuple[Event, datetime]]:
        """这一刻该开口提醒的 (事件, 发生时间)。

        判定: 进入"提前量"窗口就开始提醒 —— 而不是等到点上才叫, 那是闹钟不是助手。
        每次发生只提醒一次, 靠 fired 去重 (重启也不会重复开口)。
        """
        now = now or datetime.now()
        out = []
        for ev in self.enabled_events():
            when = ev.next_occurrence(now)
            if when is None:
                continue
            start = when - timedelta(minutes=ev.remind_before)
            # 用 <= when 而不是 < when: 桌宠正好在整点这一分钟启动时也该开口
            # (不然"14:30 的会"在 14:30 启动就永远不提醒了)
            if start <= now <= when and self.fired.get(ev.id) != when.isoformat():
                out.append((ev, when))
        return out

    def mark_fired(self, ev: Event, when: datetime):
        self.fired[ev.id] = when.isoformat()
        self.save()

    def mark_exit(self):
        """退出时记下时间 —— 下次开机靠它算"这段时间错过了哪些安排"。"""
        self.last_seen = datetime.now().isoformat(timespec="seconds")
        self.save()

    # ---------- 离线/关机期间错过的 ----------
    def missed_since_last_seen(self, now: datetime | None = None,
                               max_items: int = 20) -> list[tuple[Event, datetime]]:
        """上次退出之后到现在, 已经过去、但从没提醒过的安排。

        开机时汇总成**一条**说给用户听 (Zoria-Lind 的做法: 不逐个重放, 防轰炸)。
        """
        now = now or datetime.now()
        if not self.last_seen:
            return []
        try:
            since = datetime.fromisoformat(self.last_seen)
        except ValueError:
            return []
        if since >= now:
            return []
        out = []
        for ev in self.enabled_events():
            cursor = since
            for _ in range(MAX_LOOKAHEAD_STEPS):
                when = ev.next_occurrence(cursor)
                if when is None or when >= now:
                    break
                if self.fired.get(ev.id) != when.isoformat():
                    out.append((ev, when))
                cursor = when + timedelta(minutes=1)
            if len(out) >= max_items:
                break
        out.sort(key=lambda pair: pair[1])
        return out[:max_items]

    def summary_lines(self, now: datetime | None = None,
                      limit: int = 4) -> list[str]:
        """卡片上要显示的行 (时间 + 标题 + 重复标记)。"""
        now = now or datetime.now()
        lines = []
        for ev, when in self.upcoming(now, limit=limit):
            rep = ev.describe_repeat()
            suffix = f"（{rep}）" if rep else ""
            lines.append(f"{format_when(when, today=now.date())}  {ev.title}{suffix}")
        return lines


def default_path() -> str:
    import app_paths
    return os.path.join(app_paths.user_data_dir(), "schedule.json")


def now_ts() -> float:
    return time.time()
