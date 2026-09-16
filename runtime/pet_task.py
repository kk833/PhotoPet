"""任务 (v0.16) —— 让它记住"事儿", 不只是"事实"。

**和日程的区别**（这两件事经常被混为一谈）:
- 日程(`pet_schedule`)回答"**什么时候**要做什么" —— 有确定时间点, 到点提醒
- 任务(`pet_task`)回答"**有什么事还没做完**" —— 有状态、有进度、可能压根没有截止时间
  ("准备答辩"是个任务, 但你说不出它是几点的会)

一条任务也可能挂着日程: `due` 是期望做完的时间, 到点了它就该主动开口。

**为什么这是"agent 的分水岭"**: 只会调工具的聊天机器人能"记一条日程", 但记不住
"你在推进一件事"。有了任务层, 它才能说"你上周说这周要交材料, 交了吗" ——
这是跨会话的目标感, 也是 [[工作助手]] 这个定位真正需要的东西。

主动开口的口径 (见 pet-quiet-by-default): 任务**逾期**或**今天到期**时开口,
一天最多说一次同一条 (fired 去重), 不做定时闲聊。

存档: `<用户数据目录>/tasks.json` —— 和日程/记忆一样放在用户级目录,
人和机器都能读能改。
"""
import json
import os
import uuid
from datetime import date, datetime

import pet_text

STATES = ("open", "doing", "done", "dropped")
STATE_NAMES = {
    "open": "待办",
    "doing": "进行中",
    "done": "已完成",
    "dropped": "已放弃",
}
MAX_INJECT_CHARS = 1200        # 注入 system prompt 的上限, 任务多了只带最近的
MAX_OPEN_IN_PROMPT = 8         # 最多列几条


class Task:
    """一件事。steps 是可选的子步骤 —— 多步任务有个落脚的地方。"""

    def __init__(self, data: dict):
        self.id = str(data.get("id") or uuid.uuid4().hex[:8])
        self.title = str(data.get("title") or "（未命名）").strip()
        state = str(data.get("state") or "open").strip().lower()
        self.state = state if state in STATES else "open"
        self.created = str(data.get("created") or date.today().isoformat())
        self.due = str(data.get("due") or "").strip()
        raw_steps = data.get("steps") or []
        self.steps = [str(s).strip() for s in raw_steps if str(s).strip()] \
            if isinstance(raw_steps, list) else []
        raw_done = data.get("done_steps") or []
        self.done_steps = sorted({int(i) for i in raw_done
                                  if isinstance(i, int) or str(i).isdigit()}) \
            if isinstance(raw_done, list) else []
        self.done_steps = [i for i in self.done_steps if 0 <= i < len(self.steps)]
        self.note = str(data.get("note") or "").strip()

    @property
    def is_open(self) -> bool:
        return self.state in ("open", "doing")

    @property
    def due_date(self) -> date | None:
        if not self.due:
            return None
        try:
            parts = [int(p) for p in self.due.replace("/", "-").split("-")]
        except ValueError:
            return None
        try:
            if len(parts) == 2:
                return date(date.today().year, parts[0], parts[1])
            if len(parts) == 3:
                return date(*parts)
        except ValueError:
            return None
        return None

    def days_left(self, today: date | None = None) -> int | None:
        d = self.due_date
        if d is None:
            return None
        return (d - (today or date.today())).days

    def progress(self) -> str:
        """进度的人话: '3/5' 或 ''。"""
        if not self.steps:
            return ""
        return f"{len(self.done_steps)}/{len(self.steps)}"

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "state": self.state,
            "created": self.created, "due": self.due, "steps": self.steps,
            "done_steps": self.done_steps, "note": self.note,
        }

    def describe(self) -> str:
        """一行摘要, 给模型和卡片共用。"""
        bits = [self.title]
        if self.state == "doing":
            bits.append("进行中")
        if self.steps:
            bits.append(f"进度 {self.progress()}")
        left = self.days_left()
        if left is not None and self.is_open:
            if left < 0:
                bits.append(f"已经逾期 {abs(left)} 天")
            elif left == 0:
                bits.append("今天到期")
            else:
                bits.append(f"还有 {left} 天")
        return " · ".join(bits)


class TaskStore:
    """全部任务 + 主动提醒的去重状态。"""

    def __init__(self, path: str):
        self.path = path
        self.tasks: list[Task] = []
        self.fired: dict[str, str] = {}       # {任务id: 已提醒过的日期}
        self.load()

    # ---------- 持久化 ----------
    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return                      # 存档坏了就当空的, 不能让桌宠起不来
        for raw in (data.get("tasks") or []):
            if isinstance(raw, dict) and str(raw.get("title") or "").strip():
                self.tasks.append(Task(raw))
        fired = data.get("fired")
        if isinstance(fired, dict):
            self.fired = {str(k): str(v) for k, v in fired.items()}

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"tasks": [t.to_dict() for t in self.tasks],
                           "fired": self.fired},
                          f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ---------- 增删改 ----------
    def add(self, title: str, due: str = "", steps=None, note: str = "") -> Task:
        task = Task({"title": title, "due": due, "steps": steps or [],
                     "note": note})
        self.tasks.append(task)
        self.save()
        return task

    def find(self, keyword: str) -> list[Task]:
        """按关键词找 (先精确包含, 再模糊) —— 和删日程同一套, 理由见 pet_text。"""
        return pet_text.best_match(keyword, self.open_tasks(),
                                   text_of=lambda t: t.title) \
            or pet_text.best_match(keyword, self.tasks, text_of=lambda t: t.title)

    def set_state(self, task: Task, state: str) -> bool:
        if state not in STATES:
            return False
        task.state = state
        if state in ("done", "dropped"):
            self.fired.pop(task.id, None)
        self.save()
        return True

    def complete_step(self, task: Task, step) -> str:
        """把某个子步骤打勾。step 可以是序号(从 1 数)或步骤里的几个字。"""
        if not task.steps:
            return "这件事没有子步骤。"
        idx = None
        if isinstance(step, int) or (isinstance(step, str) and step.strip().isdigit()):
            n = int(step)
            if 1 <= n <= len(task.steps):
                idx = n - 1
        else:
            hits = pet_text.best_match(str(step), list(enumerate(task.steps)),
                                       text_of=lambda pair: pair[1])
            if hits:
                idx = hits[0][0]
        if idx is None:
            return f"没找到那一步。现有步骤：{'、'.join(task.steps)}"
        if idx in task.done_steps:
            return f"「{task.steps[idx]}」之前已经打勾了。"
        task.done_steps.append(idx)
        task.done_steps.sort()
        if len(task.done_steps) == len(task.steps):
            task.state = "done"
        elif task.state == "open":
            task.state = "doing"
        self.save()
        left = len(task.steps) - len(task.done_steps)
        tail = "全部做完了！" if not left else f"还剩 {left} 步。"
        return f"「{task.steps[idx]}」打勾了，{tail}"

    def remove(self, task: Task):
        self.tasks = [t for t in self.tasks if t.id != task.id]
        self.fired.pop(task.id, None)
        self.save()

    # ---------- 查询 ----------
    def open_tasks(self) -> list[Task]:
        return [t for t in self.tasks if t.is_open]

    def sorted_open(self) -> list[Task]:
        """有截止时间的排前面 (逾期的最前), 其余按创建时间。"""
        def key(t: Task):
            left = t.days_left()
            return (0 if left is not None else 1,
                    left if left is not None else 0,
                    t.created)
        return sorted(self.open_tasks(), key=key)

    def due_checks(self, today: date | None = None) -> list[Task]:
        """这一刻该主动开口的任务: **逾期**或**今天到期**, 且今天还没说过。

        这就是"有事要办"那条开口事由的判定 —— 不做定时闲聊, 只在真有事时说。
        """
        today = today or date.today()
        out = []
        for task in self.sorted_open():
            left = task.days_left(today)
            if left is None or left > 0:
                continue
            if self.fired.get(task.id) == today.isoformat():
                continue
            out.append(task)
        return out

    def mark_fired(self, task: Task, today: date | None = None):
        self.fired[task.id] = (today or date.today()).isoformat()
        self.save()

    def render_for_prompt(self) -> str:
        """拼进 system prompt 的任务清单。没任务就返回空串。"""
        opened = self.sorted_open()
        if not opened:
            return ""
        lines, total = [], 0
        for task in opened[:MAX_OPEN_IN_PROMPT]:
            line = f"- {task.describe()}"
            if total + len(line) > MAX_INJECT_CHARS:
                break
            lines.append(line)
            total += len(line)
        rest = len(opened) - len(lines)
        head = ("\n\n【你记着的待办】\n" + "\n".join(lines))
        if rest > 0:
            head += f"\n（还有 {rest} 条没列出来）"
        head += ("\n用户问'我要做什么'时照这个答；他说做完了/不做了，"
                 "就用 update_task 改状态。别主动念叨这些。")
        return head

    def summary(self) -> str:
        opened = self.open_tasks()
        if not opened:
            return "没有待办"
        overdue = [t for t in opened if (t.days_left() or 1) < 0]
        if overdue:
            return f"{len(opened)} 条待办（{len(overdue)} 条逾期）"
        return f"{len(opened)} 条待办"


def default_path() -> str:
    import app_paths
    return os.path.join(app_paths.user_data_dir(), "tasks.json")


def now() -> datetime:
    return datetime.now()
