"""给大模型用的本地工具 (v0.13) —— 让宠物从"只会说"变成"能做事"。

你说"明天下午三点提醒我开会", 它自己调 `add_event` 把日程记上; 问"我接下来有啥安排",
它调 `list_events` 再用人话答你。

**安全边界是刻意画死的**: 只开放**日程**和**宠物自己的动作**, 不做文件读写、不执行
命令。调研里 PetGPT 那套 read/write/edit 文件工具确实强, 但一旦模型能写文件,
路径沙箱(normpath + 根目录校验)、用户确认弹窗、误删防护就全都得跟上 ——
而桌宠并不需要这些能力, 不开就没有这些风险。

设计参考 Open-LLM-VTuber 的 ToolManager/ToolExecutor: schema 用 OpenAI 的
function calling 格式, 执行结果**统一转成文本喂回模型**, 解析失败也返回错误文本
(让它自己改), 而不是抛异常中断整轮对话。
"""
import json
import sys

from datetime import datetime

import pet_resume
import pet_schedule
import pet_task
import pet_text
import pet_web


def _log(msg: str):
    """往 stderr 打一行 (打包成 --windowed 时 stderr 可能是 None)。"""
    stream = sys.stderr
    if stream is None:
        return
    try:
        print(f"[tools] {msg}", file=stream, flush=True)
    except (OSError, ValueError):
        pass

# 工具调用的最大轮数: 防止模型来回调不停 (正常一两轮就结束了)
MAX_TOOL_ROUNDS = 4
# 挑宣讲会时简历最多带多少字: 一页简历 + 项目经历差不多就这些,
# 再长也不会让判断更准, 只是白烧 token
MAX_RESUME_CHARS = 3500


class Toolbox:
    """工具注册表。拿一个 schedule 和一个 pet (可选), 就能生成 schema 并执行。"""

    def __init__(self, sched, pet=None, memory=None, tasks=None):
        self.sched = sched
        self.pet = pet
        self.memory = memory
        self.tasks = tasks

    def system_extra(self) -> str:
        """拼在 persona 后面的上下文: 今天几号 + 记得的事 + 还没做完的事。"""
        text = system_context()
        if self.memory is not None:
            text += self.memory.render_for_prompt()
        if self.tasks is not None:
            text += self.tasks.render_for_prompt()
        return text

    # ---------- schema ----------
    def schemas(self) -> list[dict]:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "add_event",
                    "description": "给用户记一条日程/提醒。用户说'提醒我''安排''记一下'时用这个。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "要做什么，比如'开会'"},
                            "date": {"type": "string",
                                     "description": "日期，格式 YYYY-MM-DD。必须换算成绝对日期"},
                            "time": {"type": "string",
                                     "description": "24 小时制时间 HH:MM，比如 15:00。不知道就留空"},
                            "repeat": {"type": "string",
                                       "enum": list(pet_schedule.REPEATS),
                                       "description": "重复方式：none 只一次 / daily 每天 / "
                                                      "weekly 每周 / monthly 每月 / yearly 每年"},
                            "remind_before_minutes": {
                                "type": "integer",
                                "description": "提前多少分钟提醒，默认 15"},
                        },
                        "required": ["title", "date"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_events",
                    "description": "查用户接下来的日程安排。用户问'我接下来要干什么''有什么安排'时用。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "limit": {"type": "integer", "description": "返回几条，默认 3"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_event",
                    "description": "按标题关键词删掉一条日程。用户说'取消''不用提醒了'时用。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "要删的那条日程的标题或其中几个字"},
                        },
                        "required": ["title"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "pet_action",
                    "description": "让桌宠做个动作。用户说'喂你''去睡觉''醒醒''摸摸'时用。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string",
                                       "enum": ["feed", "sleep", "wake", "pat"],
                                       "description": "feed 吃东西 / sleep 睡觉 / wake 醒来 / pat 被摸"},
                        },
                        "required": ["action"],
                    },
                },
            },
        ]
        tools.append({
            "type": "function",
            "function": {
                "name": "match_events",
                "description": "结合用户的简历，从近期宣讲会里挑出值得他去的几场。"
                               "用户问'哪些宣讲会适合我''我该去哪个'时用它。"
                               "只会推荐，不会删除或修改任何日程。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days": {"type": "integer",
                                 "description": "看未来几天内的，默认 7"},
                        "limit": {"type": "integer",
                                  "description": "最多读几场的详情页，默认 8（读页面慢，别贪多）"},
                    },
                },
            },
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "read_event",
                "description": "查某条日程/宣讲会的详细信息（招聘岗位、要求、公司介绍等）。"
                               "用户问'XX那场的详细信息''这个宣讲会招什么岗位'时用它。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string",
                                  "description": "那条日程的标题或其中几个字，比如'长城电源'"},
                    },
                    "required": ["title"],
                },
            },
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "read_url",
                "description": "读一个网页的内容。用户发来链接、或者说'帮我看看这个页面'"
                               "'这个链接讲了什么'时用它。只能读公开的 http/https 网页，"
                               "读不了内网地址和需要登录的页面。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "完整的网址，要带 http(s)://"},
                    },
                    "required": ["url"],
                },
            },
        })
        if self.tasks is not None:
            tools += [
                {
                    "type": "function",
                    "function": {
                        "name": "add_task",
                        "description": "记一件**要做的事**（待办）。和 add_event 的区别："
                                       "add_event 是'几点几分做什么'的安排，add_task 是"
                                       "'这件事还没做完'。用户说'我要做''得准备''别忘了'时用它。"
                                       "如果这件事有明确的时间点，两个都要调。",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "description": "要做的事，一句话"},
                                "due": {"type": "string",
                                        "description": "期望做完的日期 YYYY-MM-DD，没有就留空"},
                                "steps": {"type": "array", "items": {"type": "string"},
                                          "description": "子步骤（可选）。事情复杂就拆几步"},
                            },
                            "required": ["title"],
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "list_tasks",
                        "description": "查还没做完的事。用户问'我有什么事要做''还有什么没弄'时用。",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "include_done": {"type": "boolean",
                                                 "description": "连做完的一起列，默认否"},
                            },
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "update_task",
                        "description": "更新一件事的状态或进度：做完了、开始了、某一步打勾了。",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "description": "那件事的标题或其中几个字"},
                                "state": {"type": "string",
                                          "enum": ["open", "doing", "done", "dropped"],
                                          "description": "改成什么状态（可选）"},
                                "done_step": {"type": "string",
                                              "description": "完成的那一步：写序号或步骤里的几个字"},
                                "note": {"type": "string", "description": "补一句备注（可选）"},
                            },
                            "required": ["title"],
                        },
                    },
                },
            ]
        if self.memory is not None:
            tools += [
                {
                    "type": "function",
                    "function": {
                        "name": "remember",
                        "description": "把用户告诉你的、值得长期记住的事记下来（偏好、习惯、"
                                       "重要的人和事）。用户说'记住''以后''我喜欢'时用。"
                                       "不要记寒暄和一次性的闲聊。",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string",
                                         "description": "要记的那件事，一句话说清"},
                            },
                            "required": ["text"],
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "forget",
                        "description": "忘掉之前记过的某件事。用户说'忘掉''别记着''删掉你记的'时用。",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "keyword": {"type": "string", "description": "要忘掉的那件事的关键词"},
                            },
                            "required": ["keyword"],
                        },
                    },
                },
            ]
        return tools

    # ---------- 执行 ----------
    def call(self, name: str, args_json: str) -> str:
        """执行一个工具, 返回**给模型看的文本结果**。永不抛异常。"""
        result = self._call_inner(name, args_json)
        # 留一行日志: 模型到底调没调工具、调成什么样, 出问题时这是唯一线索
        # (最坑的失败是"模型没调工具却回'已经办好了'", 没日志根本查不出来)
        _log(f"{name}({args_json}) -> {result[:80]}")
        return result

    def _call_inner(self, name: str, args_json: str) -> str:
        try:
            args = json.loads(args_json) if args_json and args_json.strip() else {}
            if not isinstance(args, dict):
                return f"参数格式不对，应该是个对象，收到：{args_json!r}"
        except json.JSONDecodeError as e:
            return f"参数不是合法 JSON（{e}），请重新调用一次，参数写成 JSON 对象。"
        try:
            handler = getattr(self, f"_do_{name}", None)
            if handler is None:
                return f"没有名为 {name} 的工具。"
            return handler(**args)
        except TypeError as e:                     # 参数名/个数不对
            return f"调用 {name} 的参数不对（{e}），请按 schema 重新调用。"
        except Exception as e:                     # noqa: BLE001  工具出错不该中断整轮对话
            return f"{name} 执行失败：{e.__class__.__name__}: {e}"

    # ---------- 各工具 ----------
    def _do_add_event(self, title: str = "", date: str = "", time: str = "",
                      repeat: str = "none", remind_before_minutes: int = None) -> str:
        if not title:
            return "缺少 title（要做什么），请重新调用。"
        if repeat not in pet_schedule.REPEATS:
            repeat = "none"
        minutes = (pet_schedule.DEFAULT_REMIND_BEFORE
                   if remind_before_minutes is None else int(remind_before_minutes))
        ev = self.sched.add(title, date, time, repeat=repeat,
                            remind_before=minutes)
        if ev is None:
            return (f"日期 {date!r} / 时间 {time!r} 我没看懂，没记上。"
                    f"请换成 YYYY-MM-DD 和 HH:MM 再调一次。")
        when = ev.next_occurrence(datetime.now())
        human = pet_schedule.format_when(when) if when else ev.date
        rep = ev.describe_repeat()
        tail = f"（{rep}）" if rep else ""
        return f"已经记上了：{human} {ev.title}{tail}，提前 {minutes} 分钟提醒。"

    def _do_list_events(self, limit: int = 3) -> str:
        try:
            limit = max(1, min(10, int(limit)))
        except (TypeError, ValueError):
            limit = 3
        items = self.sched.upcoming(limit=limit)
        if not items:
            return "接下来没有任何安排，日程是空的。"
        lines = []
        for ev, when in items:
            line = f"{pet_schedule.format_when(when)} {ev.title}"
            if ev.describe_repeat():
                line += f"（{ev.describe_repeat()}）"
            if ev.url:
                # 把详情页链接一起给模型: 它需要时可以直接 read_url 去读
                line += f"[详情页 {ev.url}]"
            lines.append(line)
        return "接下来的安排：\n" + "\n".join(lines)

    def _do_delete_event(self, title: str = "") -> str:
        key = (title or "").strip()
        if not key:
            return "缺少 title，请说明要删哪一条。"
        events = self.sched.events
        if not events:
            return "现在没有任何日程，没得删。"
        # 先精确包含; 再按最长公共子串模糊找 —— 模型常常把标题写成一整句描述,
        # 比如用"开会的提醒"来指代标题只有"开会"的那条。只做精确匹配的话会
        # 找不到 -> 返回错误 -> **模型无视错误照样回"已经删掉了"** (实测踩到,
        # 用户以为删了其实还在, 这是最坏的一种失败)
        hits = pet_text.best_match(key, events, text_of=lambda e: e.title)
        if not hits:
            names = "、".join(e.title for e in events[:3])
            return (f"没找到和「{key}」对得上的日程。现有的是：{names}。"
                    f"如果确实要删，请用 list_events 里显示的准确标题。")
        if len(hits) > 1:
            names = "、".join(e.title for e in hits[:3])
            return f"有 {len(hits)} 条都对得上（{names}），请说得更具体一点。"
        self.sched.remove(hits[0].id)
        return f"已经把「{hits[0].title}」删掉了。"

    def _do_add_task(self, title: str = "", due: str = "", steps=None,
                     note: str = "") -> str:
        if self.tasks is None:
            return "现在没开待办功能。"
        if not title:
            return "缺少 title（要做什么），请重新调用。"
        step_list = [str(s) for s in steps] if isinstance(steps, list) else []
        task = self.tasks.add(title, due=due, steps=step_list, note=note)
        tail = f"，{task.describe()}" if due else ""
        if step_list:
            tail += f"，拆了 {len(step_list)} 步"
        return f"记下了待办：{task.title}{tail}。"

    def _do_list_tasks(self, include_done: bool = False) -> str:
        if self.tasks is None:
            return "现在没开待办功能。"
        opened = self.tasks.sorted_open()
        lines = [f"{i}. {t.describe()}" for i, t in enumerate(opened, 1)]
        if include_done:
            done = [t for t in self.tasks.tasks if t.state in ("done", "dropped")]
            lines += [f"（{pet_task.STATE_NAMES[t.state]}）{t.title}" for t in done[:5]]
        if not lines:
            return "你手头没有待办，挺清爽的。"
        return "还没做完的事：\n" + "\n".join(lines)

    def _do_update_task(self, title: str = "", state: str = "",
                        done_step=None, note: str = "") -> str:
        if self.tasks is None:
            return "现在没开待办功能。"
        hits = self.tasks.find(title)
        if not hits:
            return (f"没找到和「{title}」对得上的待办。"
                    f"当前待办是：{'、'.join(t.title for t in self.tasks.sorted_open()[:5]) or '（空）'}")
        if len(hits) > 1:
            return (f"有 {len(hits)} 条都对得上（{'、'.join(t.title for t in hits[:3])}），"
                    f"请说得更具体。")
        task = hits[0]
        done_bits = []
        if done_step is not None and str(done_step).strip():
            done_bits.append(self.tasks.complete_step(task, done_step))
        if state:
            if not self.tasks.set_state(task, state):
                return f"状态 {state!r} 不认识，只能是 open/doing/done/dropped。"
            done_bits.append(f"状态改成了「{pet_task.STATE_NAMES[state]}」")
        if note:
            task.note = str(note).strip()[:200]
            self.tasks.save()
            done_bits.append("备注记上了")
        if not done_bits:
            return f"「{task.title}」当前：{task.describe()}。你想改什么？"
        return f"「{task.title}」：" + "，".join(done_bits) + "。"

    def _do_read_event(self, title: str = "") -> str:
        """查某条日程的详情 —— 比如"长城电源那场宣讲会的详细信息"。

        抓宣讲会时把详情页链接存进了日程, 这里就用它去读那个页面。
        **一个工具搞定, 不用模型先 list_events 再 read_url 转两圈。**
        """
        hits = self.sched.find(title)
        if not hits:
            return (f"日程里没有叫「{title}」的安排。"
                    f"可以先调 list_events 看看都有什么。")
        if len(hits) > 1:
            names = "、".join(e.title for e in hits[:3])
            return f"有 {len(hits)} 条都对得上（{names}），说得更具体一点。"
        ev = hits[0]
        if not ev.url:
            return (f"「{ev.title}」这条日程里没有存详情页链接"
                    f"（手动加的日程都没有）。如果用户手上有链接，让他发给我，"
                    f"我用 read_url 去读。")
        text, err = pet_web.read_url(ev.url)
        if err:
            return f"找到「{ev.title}」的详情页了（{ev.url}），但读不出来：{err}"
        return (f"这是「{ev.title}」的详情页正文（{ev.url}）：\n---\n{text}\n---\n"
                f"（挑重点回答用户，别逐字念）")

    def _do_match_events(self, days: int = 7, limit: int = 8) -> str:
        """挑出"值得你去"的宣讲会（v0.26）。

        **为什么一次读完而不是一场一次**: 秋招一天十几场，一场一调用就是十几轮 ——
        又慢又贵。这里一次把近期宣讲会的详情页读完、连同简历一次性交给模型，
        它一轮就能给出推荐。每个页面只截前 600 字（看出"这家做什么、招什么方向"够了）。

        **只推荐，不删任何东西**：用户明确要求宣讲会"宁可多报，不可漏报"，
        所以这个工具不改日程，只是告诉用户哪几场值得去。
        """
        if self.pet is None:
            return "现在读不了简历。"
        resume = pet_resume.load()
        if not resume.strip():
            return ("用户还没给我简历 —— 让他右键「📅 日程 / 待办」→"
                    "「📄 导入我的简历…」，把 PDF 或 Word 简历给我。"
                    "在那之前我只能按公司名瞎猜，不如不做。")

        try:
            days = max(1, min(30, int(days)))
            limit = max(1, min(12, int(limit)))
        except (TypeError, ValueError):
            days = 7
            limit = 8

        now = datetime.now()
        upcoming = [(ev, when) for ev, when in self.sched.upcoming(limit=60)
                    if (when - now).days <= days]
        if not upcoming:
            return f"接下来 {days} 天没有安排，没什么可挑的。"
        # 只有带详情页链接的才读得到内容（手动加的日程没有链接）
        with_url = [(ev, when) for ev, when in upcoming if ev.url][:limit]
        if not with_url:
            return ("接下来的安排里没有带详情页链接的 —— 只有从就业网抓来的宣讲会"
                    "才有链接。先让用户抓一次宣讲会。")

        # 并发抓页面: 串行抓 8 个页面要十几秒，并发一两秒就够
        texts: dict[str, str] = {}

        def grab(ev):
            text, _ = pet_web.read_url(ev.url)
            texts[ev.url] = text

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(grab, [ev for ev, _ in with_url]))

        blocks = []
        for i, (ev, when) in enumerate(with_url, 1):
            body = (texts.get(ev.url) or "").strip()
            if len(body) > 600:
                body = body[:600] + "…"
            head = (f"{i}. {when:%m-%d %H:%M} {ev.title}"
                    + (f"（{ev.note}）" if ev.note else ""))
            blocks.append(head + ("\n   详情页：\n   " + body.replace("\n", "\n   ")
                                  if body else "\n   （详情页没读到内容）"))

        skipped = len(upcoming) - len(with_url)
        tail = (f"\n（另外还有 {skipped} 场没有详情页链接，没读；"
                f"它们仍然在日程里，一场都没删）" if skipped else "")

        return ("【我的简历】\n" + resume.strip()[:MAX_RESUME_CHARS] + "\n\n"
                "【近期宣讲会】（已读详情页）\n" + "\n".join(blocks) + tail + "\n\n"
                "请挑出**值得这位用户去**的几场，每场用一句话说明理由"
                "（比如'做服务器电源，和你电源方向的实习对得上'）。"
                "**不要建议删掉任何一场** —— 用户宁可多跑几场，也不想因为"
                "判断失误错过机会。说不准的就直说不准。")

    def _do_read_url(self, url: str = "") -> str:
        """读网页给模型看。

        **这是让大模型能"上网"的唯一入口，所以地址必须先过安全关**：
        模型给的地址不可信（可能被诱导去读你家里的路由器、公司内网），
        所以走 pet_web.is_safe_url 挡掉内网/本机地址，再抓。
        """
        text, err = pet_web.read_url(url)
        if err:
            return f"读不了这个地址：{err}"
        return (f"这是 {url} 的正文（已去掉脚本和样式）：\n---\n{text}\n---\n"
                f"（根据这段内容回答用户，别逐字念）")

    def _do_remember(self, text: str = "") -> str:
        if self.memory is None:
            return "现在没开记忆功能。"
        return self.memory.add(text)

    def _do_forget(self, keyword: str = "") -> str:
        if self.memory is None:
            return "现在没开记忆功能。"
        return self.memory.forget(keyword)

    def _do_pet_action(self, action: str = "") -> str:
        if self.pet is None:
            return "现在没法做动作。"
        table = {
            "feed": ("喂它吃东西", lambda p: p.feed()),
            "sleep": ("让它睡觉", lambda p: p.apply_state("sleep")),
            "wake": ("叫醒它", lambda p: p.apply_state("idle")),
            "pat": ("摸摸它", lambda p: p.apply_state("click")),
        }
        if action not in table:
            return f"不认识的动作 {action!r}，只能是 feed/sleep/wake/pat 之一。"
        label, fn = table[action]
        fn(self.pet)
        return f"已经{label}了。"


def system_context(now: datetime | None = None) -> str:
    """给模型的"今天几号"上下文 —— 没有它模型算不对'明天''下周三'。"""
    now = now or datetime.now()
    weekday = "一二三四五六日"[now.weekday()]
    return (f"\n\n【当前时间】{now:%Y-%m-%d %H:%M} 星期{weekday}。\n"
            f"用户说相对日期（明天、下周三、晚上七点）时，你要自己换算成绝对日期再调工具。\n"
            f"记完日程回话要简短，一两句就好。\n"
            f"始终用中文回答，别夹英文。")
