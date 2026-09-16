"""抓高校就业网的宣讲会 (v0.19) —— 秋招模式的数据来源。

以「江西省大学生就业服务平台」的学校子站为例（很多江西高校共用这套 CMS）:
    形如  https://<省级平台域名>/<学校代码>/teachin/index

**默认地址是空的**: 这是一套全国通用的工具, 默认值不该指向某一所学校 ——
而且写死一个学校还会顺带暴露"作者是哪个学校的"。地址由用户自己在
「秋招设置」里填, 没填时抓取会提示去填。

**这个站的列表不是普通 HTML**: CMS 把列表内容 base64 + zlib 压缩后塞在页面的
`<script>` 里（国内 CMS 常见的 "jz" 混淆），形如
`Base64.decode(unzip("eJy...").substr(77)).substr(40)`。所以要按这个顺序解:
    zlib 解压 -> 去掉前 77 字符 -> base64 解码 -> 去掉前 40 字符 -> 真正的列表 HTML
解出来以后结构很规整，一条对应一个 `<ul class="infoList teachinList">`。
用标准库就够，不需要 requests / beautifulsoup。

**这个源不稳定**（实测同一地址连抓 4 次: 20/20/0/0 条，像是多台后端节点缓存不一致），
所以: 必须带重试; 抓不到要**如实返回失败**, 不能假装"今天没有宣讲会" ——
秋招里"悄悄漏掉一场"比"报错"严重得多。

**不过滤原则**（用户明确要求）: 除了"日期在今天之后"这一个安全过滤, **不做任何
筛选**。宁可多报几条不相关的, 也不能因为"看着不像目标行业"就丢掉一场 ——
漏掉的机会补不回来, 多看的几秒不值钱。要筛由用户自己加关键词（默认空 = 不筛）。
"""
import re
import urllib.error
import urllib.request
from datetime import date, datetime

import pet_web

# 留空: 由用户在自己的「⚙ 秋招设置」里填（见模块文档里的理由）
DEFAULT_URL = ""
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PhotoPet/1.0"
TIMEOUT = 20
MAX_RETRIES = 6          # 源约一半的请求返回空, 多试几次能拿到

# 一条宣讲会: <ul class="infoList teachinList"> … </ul>
ITEM_RE = re.compile(r'<ul class="infoList teachinList">(.*?)</ul>', re.S)
LINK_RE = re.compile(r'<a\s+href="([^"]+)"[^>]*title="([^"]*)"', re.S)
PLACE_RE = re.compile(r'<li class="span5">(.*?)</li>', re.S)
WHEN_RE = re.compile(r'<li>\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*'
                     r'(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})', re.S)
ONLINE_RE = re.compile(r'class="status-text">\s*([^<]+?)\s*<')


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).replace("&nbsp;", " ").strip()


def decode_list(html: str) -> str:
    """把页面里那坨压缩数据解成真正的列表 HTML（转调公用实现，只有一份解码逻辑）。"""
    return pet_web.decode_hidden_payload(html)


def parse_teachins(list_html: str) -> list[dict]:
    """从解出来的列表 HTML 里抽宣讲会。字段: 公司/日期/时间/地点/线上线下/链接。"""
    out = []
    for block in ITEM_RE.findall(list_html):
        link = LINK_RE.search(block)
        when = WHEN_RE.search(block)
        if not link or not when:
            continue
        y, mo, d, h1, m1, h2, m2 = (int(g) for g in when.groups())
        try:
            day = date(y, mo, d)
        except ValueError:
            continue
        place = PLACE_RE.search(block)
        online = ONLINE_RE.search(block)
        out.append({
            "id": (re.search(r"/id/(\d+)", link.group(1)) or [None, ""])[1]
                  if re.search(r"/id/(\d+)", link.group(1)) else "",
            "title": link.group(2).strip() or _strip_tags(block)[:40],
            "date": day.isoformat(),
            "time": f"{h1:02d}:{m1:02d}",
            "end_time": f"{h2:02d}:{m2:02d}",
            "place": _strip_tags(place.group(1)) if place else "",
            "online": "线上" if (online and "线上" in online.group(1)) else "线下",
            "url": link.group(1),
        })
    return out


def fetch_page(url: str, retries: int = 2, timeout: int = TIMEOUT) -> str:
    """抓一个页面（转调公用实现，抓取逻辑只有一份）。"""
    return pet_web.fetch_page(url, retries=retries, timeout=timeout)


def html_to_text(html: str) -> str:
    """HTML -> 可读正文（转调公用实现）。"""
    return pet_web.html_to_text(html)


def looks_like_jobfair(text: str) -> bool:
    """粗判这页是不是宣讲会/招聘会列表 —— 用来告诉用户"这网址对不对"。"""
    hits = sum(1 for k in ("宣讲", "招聘会", "双选", "校招", "毕业生", "用人单位")
               if k in text)
    return hits >= 2


def fetch_teachins(url: str = DEFAULT_URL, days_ahead: int = 14,
                   max_pages: int = 2, log=None) -> tuple[list[dict], str]:
    """用**内置规则**抓宣讲会（只对这套 CMS 有效）。返回 (列表, 错误说明)。

    只保留**今天及以后**的（过去的宣讲会没有意义）—— 这是唯一的过滤。
    按 (日期, 时间, 公司) 去重后排序。

    认不出来的网站会返回 ([], 说明)，调用方应该改走大模型那条路
    （见 main.py 的 fetch_jobfair_async）。
    """
    if not (url or "").strip():
        # 默认地址是空的（见模块文档：面向全国的工具不该默认指向某一所学校）。
        # 这里必须自己挡住 —— 交给 urllib 会抛 "unknown url type"，
        # 调用方拿到一个莫名其妙的异常，而不是"你还没填地址"
        return [], "还没配就业网地址（在「⚙ 秋招设置」里填）"
    today = date.today()
    horizon = (today.toordinal() + days_ahead)
    seen: dict[str, dict] = {}
    last_err = ""
    not_this_cms = False
    for page in range(1, max_pages + 1):
        target = url if page == 1 else f"{url}?page={page}"
        # 第一页值得多试（源不稳定，约一半请求返回空）；后续页拿不到就算了 ——
        # 第一页已经有 20 条了，为了第二页反复敲人家的服务器不礼貌
        attempts = MAX_RETRIES if page == 1 else 2
        # 后续页用更短的超时: 实测第二页常常直接挂到超时, 20 秒 × 2 次要等 40 秒,
        # 而第一页已经有 20 条了 —— 为了多拿一页让用户干等 40 秒不值
        timeout = TIMEOUT if page == 1 else 8
        html = ""
        for attempt in range(attempts):
            try:
                req = urllib.request.Request(target, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    html = resp.read().decode("utf-8", "replace")
            except (urllib.error.URLError, OSError, ValueError) as e:
                last_err = f"{e.__class__.__name__}: {e}"
                html = ""
            decoded = decode_list(html) if html else ""
            items = parse_teachins(decoded) if decoded else []
            if items:
                break                       # 拿到数据就停, 不再重试
            if html and not decoded:
                # 页面能打开、但里面**没有这套 CMS 的压缩数据** -> 说明这个地址
                # 根本不是这套系统（比如给了学校首页）。这时重试毫无意义,
                # 只是把 6 × 20 秒的超时全撞一遍。
                # 注意要连带跳出**翻页**循环: 只 break 内层的话, 第 2 页还会再抓
                # 一次同样的慢页面（实测这一处漏掉就让整个流程多花 17 秒）
                last_err = "这个页面不是就业网的宣讲会列表（换更具体的地址，或交给大模型读）"
                not_this_cms = True
                break
            if log:
                log(f"[宣讲会] 第 {page} 页第 {attempt + 1} 次没拿到数据，重试…")
        if not_this_cms:
            break
        if not items:
            last_err = last_err or "服务器一直返回空数据"
        for item in items:
            try:
                day = date.fromisoformat(item["date"])
            except ValueError:
                continue
            if today <= day <= date.fromordinal(horizon):
                seen[item.get("url") or f'{item["date"]}{item["title"]}'] = item
    out = sorted(seen.values(), key=lambda x: (x["date"], x["time"], x["title"]))
    if not out:
        return [], last_err or "没抓到数据（今天可能确实没有新的宣讲会）"
    return out, ""


def to_events(items: list[dict], remind_before: int = 30) -> list[dict]:
    """转成日程事件（喂给已有的预览/导入流程）。

    `remind_before` 默认 30 分钟（比普通日程的 15 分钟更早）: 宣讲会要提前到场
    占座、还要找教室, 15 分钟常常不够。
    """
    return [{
        # 只给线上标注: 线下是常态, 每条都挂个"（线下）"会把标题撑得老长,
        # 而"线上"是真有用的信息 —— 那意味着你能在电脑前听
        "title": (f'{i["title"]}（线上）' if i["online"] == "线上" else i["title"]),
        "date": i["date"],
        "time": i["time"],
        "repeat": "none",
        # 提前量必须放进字典里带出去 —— 一开始只把它写成函数参数, 结果下游
        # (_preview_import) 拿不到, 全都退回默认 15 分钟 (被端到端测试抓到)
        "remind_before_minutes": remind_before,
        # 详情页链接也要存下来 —— 用户问"这场宣讲会的详细信息"时要靠它去读
        "url": i.get("url") or "",
        "note": (f'{i["place"]} {i["time"]}-{i["end_time"]}'.strip()
                 if i["place"] else f'{i["time"]}-{i["end_time"]}'),
    } for i in items]


def summarize(items: list[dict]) -> str:
    """卡片/气泡用的一句话摘要。"""
    if not items:
        return "没有查到宣讲会"
    days = {}
    for i in items:
        days[i["date"]] = days.get(i["date"], 0) + 1
    today = date.today().isoformat()
    tomorrow = date.fromordinal(date.today().toordinal() + 1).isoformat()
    bits = []
    if days.get(today):
        bits.append(f"今天 {days[today]} 场")
    if days.get(tomorrow):
        bits.append(f"明天 {days[tomorrow]} 场")
    rest = sum(v for k, v in days.items() if k not in (today, tomorrow))
    if rest:
        bits.append(f"之后 {rest} 场")
    return f"共 {len(items)} 场宣讲会：" + "、".join(bits)


def now() -> datetime:
    return datetime.now()
