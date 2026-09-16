"""天气提醒 (v0.22) —— 只在"要下雨了"这种事上开口。

**数据源选 Open-Meteo**（https://open-meteo.com）：免费、**不需要注册也不需要 key**、
逐小时预报里直接有降水概率和降水量。对比过几个：和风天气要注册拿 key，
中国天气网没有官方接口只能爬页面（不稳），wttr.in 能用但字段少。
Open-Meteo 对"要下雨了吗"这个问题刚好够用，而且没有密钥要管。

**产品口径**（见 pet-quiet-by-default）: **不做每天播报天气** —— 那是定时闲聊，
用户明确反对。只在一件事上开口: **快下雨了, 该收衣服 / 该带伞**。
这也解释了为什么这里没有"今天 25 度晴"这种话。

去重: 同一场雨只提醒一次（记下"提醒到的那场雨是哪一小时"）—— 否则每小时检查
一次就会每小时喊一遍。
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import pet_web

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

DEFAULT_WITHIN_HOURS = 2        # 未来几小时内下雨才提醒（收衣服的时间窗）
DEFAULT_MIN_PROBABILITY = 60    # 降水概率到这个数才算"会下"
DEFAULT_MIN_MM = 0.1            # 光有概率不算，还得有实际降水量
ALERT_COOLDOWN_HOURS = 3        # 同一场雨别反复喊


class Hour:
    """一小时。"""

    def __init__(self, when: datetime, probability: int, mm: float,
                 code: int = 0):
        self.when = when
        self.probability = probability      # 降水概率 %
        self.mm = mm                        # 降水量 mm
        self.code = code                    # WMO 天气代码


def _get_json(url: str, params: dict) -> dict | None:
    full = url + "?" + urllib.parse.urlencode(params)
    text = pet_web.fetch_page(full, retries=2, timeout=15)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def geocode(city: str) -> tuple[float, float, str] | None:
    """城市名 -> (纬度, 经度, 标准名)。查不到返回 None。"""
    city = (city or "").strip()
    if not city:
        return None
    data = _get_json(GEOCODE_URL,
                     {"name": city, "count": 1, "language": "zh",
                      "format": "json"})
    results = (data or {}).get("results") or []
    if not results:
        return None
    top = results[0]
    try:
        return float(top["latitude"]), float(top["longitude"]), \
            str(top.get("name") or city)
    except (KeyError, TypeError, ValueError):
        return None


def fetch_hours(lat: float, lon: float, hours: int = 12) -> list[Hour]:
    """取未来若干小时的预报。取不到返回空列表。"""
    data = _get_json(FORECAST_URL, {
        "latitude": round(lat, 3), "longitude": round(lon, 3),
        "hourly": "precipitation_probability,precipitation,weather_code",
        "forecast_days": 2, "timezone": "auto",
    })
    hourly = (data or {}).get("hourly") or {}
    times = hourly.get("time") or []
    probs = hourly.get("precipitation_probability") or []
    mms = hourly.get("precipitation") or []
    codes = hourly.get("weather_code") or []
    out = []
    for i, stamp in enumerate(times):
        try:
            when = datetime.fromisoformat(str(stamp))
        except ValueError:
            continue
        out.append(Hour(
            when,
            int(probs[i] or 0) if i < len(probs) else 0,
            float(mms[i] or 0) if i < len(mms) else 0.0,
            int(codes[i] or 0) if i < len(codes) else 0,
        ))
    now = datetime.now()
    return [h for h in out if h.when >= now.replace(minute=0, second=0,
                                                    microsecond=0)][:hours]


def find_rain(hours: list[Hour], now: datetime | None = None,
              within_hours: int = DEFAULT_WITHIN_HOURS,
              min_probability: int = DEFAULT_MIN_PROBABILITY,
              min_mm: float = DEFAULT_MIN_MM) -> Hour | None:
    """未来 within_hours 小时内会下雨吗？返回最先下雨的那一小时。

    "会下雨"要同时满足**概率**和**降水量**: 光看概率的话，"60% 概率 0.0mm"
    也会报（那是"可能下但没量"），下雨天把人喊去收衣服结果没下，几次之后
    这个提醒就被无视了 —— 提醒的价值在于准。
    """
    now = now or datetime.now()
    limit = now + timedelta(hours=within_hours)
    for hour in hours:
        if hour.when < now.replace(minute=0, second=0, microsecond=0):
            continue
        if hour.when > limit:
            break
        if hour.probability >= min_probability and hour.mm >= min_mm:
            return hour
    return None


def _gap_text(minutes: int) -> str:
    if minutes <= 30:
        return "马上"
    if minutes < 90:
        return "1 小时内"
    hours = round(minutes / 60)
    return f"大概 {hours} 小时后"


def alert_text(hour: Hour, now: datetime | None = None) -> str:
    """该说的那句话。"""
    now = now or datetime.now()
    minutes = max(0, int((hour.when - now).total_seconds() // 60))
    when = _gap_text(minutes)
    if hour.probability >= 80:
        return f"{when}要下雨了（{hour.probability}%），记得收衣服！"
    return f"{when}可能有雨（{hour.probability}%），阳台上的东西收一下？"


def describe_hours(hours: list[Hour]) -> str:
    """给排查用的一句话（不影响正常使用）。"""
    if not hours:
        return "没取到预报"
    rainy = [h for h in hours if h.probability >= DEFAULT_MIN_PROBABILITY]
    if not rainy:
        return f"未来 {len(hours)} 小时没有明显降水"
    first = rainy[0]
    return (f"未来 {len(hours)} 小时里有 {len(rainy)} 个时段可能下雨，"
            f"最早 {first.when:%H:%M}（{first.probability}%，{first.mm:.1f}mm）")


class WeatherWatch:
    """盯着一场雨: 该提醒时给一句话, 不该说话时保持安静。

    把状态（上次提醒的是哪场雨、上次查到的坐标）单独存一个小文件,
    这样重启桌宠不会因为"忘了说过"而对同一场雨再喊一遍。
    """

    def __init__(self, state_path: str):
        self.state_path = state_path
        self.last_alert = ""        # 上次提醒到的那一小时（ISO）
        self.lat = None
        self.lon = None
        self.place = ""             # 解析出来的标准地名
        self.load()

    # ---------- 状态 ----------
    def load(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        self.last_alert = str(data.get("last_alert") or "")
        self.lat = data.get("lat")
        self.lon = data.get("lon")
        self.place = str(data.get("place") or "")

    def save(self):
        import os
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump({"last_alert": self.last_alert, "lat": self.lat,
                           "lon": self.lon, "place": self.place},
                          f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ---------- 主逻辑 ----------
    def resolve(self, city: str) -> bool:
        """把城市名换成坐标（换城市时才查一次，之后用缓存）。"""
        city = (city or "").strip()
        if not city:
            return False
        if self.lat is not None and self.lon is not None and self.place == city:
            return True
        got = geocode(city)
        if not got:
            return False
        self.lat, self.lon, self.place = got
        if self.place != city:          # 存标准名，下次好比对
            self.place = city
        self.save()
        return True

    def check(self, city: str, now: datetime | None = None,
              within_hours: int = DEFAULT_WITHIN_HOURS,
              min_probability: int = DEFAULT_MIN_PROBABILITY) -> str:
        """该提醒就返回一句话，否则返回空串（包括：没配城市、查不到、没雨、说过了）。"""
        if not city or not self.resolve(city):
            return ""
        hours = fetch_hours(self.lat, self.lon, hours=within_hours + 6)
        if not hours:
            return ""                   # 网络不通就安静，不要拿天气烦人
        rain = find_rain(hours, now, within_hours, min_probability)
        if rain is None:
            return ""
        key = rain.when.isoformat()
        if self.last_alert == key:
            return ""                   # 这场雨说过了（每小时查一次也不会重复喊）
        self.last_alert = key
        self.save()
        return alert_text(rain, now)

    def summary(self) -> str:
        """给设置窗口看的当前状态。"""
        if self.lat is None:
            return "还没查过"
        hours = fetch_hours(self.lat, self.lon, hours=12)
        return (f"{self.place}：" if self.place else "") + describe_hours(hours)


def default_state_path() -> str:
    import os

    import app_paths
    return os.path.join(app_paths.user_data_dir(), "weather.json")


def now() -> datetime:
    return datetime.now()
