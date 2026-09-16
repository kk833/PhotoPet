"""长期记忆 (v0.15) —— 让它记住你的事, 跨会话不忘。

设计参考 PetGPT 的 workspace 文件方案 (SOUL / USER / MEMORY 三个 markdown):
**就是一两个纯文本文件**, 每轮把内容拼进 system prompt, 由模型自己往里记。

为什么不上向量库: 桌宠这点记忆量 (几千字以内) 全文本注入**准确率 100%、零依赖、
零 embedding 成本**; 向量检索在这个量级是杀鸡用牛刀 (调研里 AIRI 那个记忆 PR
用的是 sqlite + embeddings, 但它面向的是海量记忆)。

**安全边界**: 文件名由代码写死, 模型只能提供"记什么", 不能指定路径 ——
所以不存在路径穿越、写到别处去的风险。这是刻意的: 不做通用的文件写工具。

**隐私**: 记忆内容每轮都会随对话发给模型服务商。所以提示词里明确写了不许记
密码/银行卡/身份证这类东西, 文件本身也是纯文本, 用户随时能打开看、能删。
"""
import os
import re
from datetime import date

HEADER = "# 关于你的事\n"
# 注入上限: 超了只取最近的若干条 (记忆越长, 每轮请求越贵, 而且更容易跑题)
MAX_CHARS = 3000
MAX_ITEM_CHARS = 200        # 单条太长的截断, 防止一句闲聊把整个文件撑爆

# 这些内容不许记 —— 写进提示词, 也在这里拦一道
FORBIDDEN = ("密码", "口令", "银行卡", "信用卡", "身份证", "社保", "验证码",
             "私钥", "api key", "apikey", "token", "护照")


class Memory:
    """一份长期记忆 (存在用户数据目录的 MEMORY.md, 人和模型都能读)。"""

    def __init__(self, path: str):
        self.path = path
        self.items: list[str] = []
        self.load()

    # ---------- 读写 ----------
    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            return
        self.items = [ln.strip() for ln in raw.splitlines()
                      if ln.strip().startswith("- ")]

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(HEADER)
                f.write("\n".join(self.items))
                f.write("\n")
        except OSError:
            pass

    # ---------- 记 ----------
    @staticmethod
    def looks_sensitive(text: str) -> str | None:
        """命中了就不许记。返回命中的那个词, 没命中返回 None。"""
        low = text.lower()
        for word in FORBIDDEN:
            if word in low:
                return word
        return None

    def add(self, text: str) -> str:
        """记一条。返回**给模型看的**结果说明。"""
        text = re.sub(r"\s+", " ", (text or "").strip())
        if not text:
            return "内容为空，没记。"
        hit = self.looks_sensitive(text)
        if hit:
            return (f"这条涉及「{hit}」，属于不该记的信息，我没记。"
                    f"提醒用户这类内容不要交给我保存。")
        if len(text) > MAX_ITEM_CHARS:
            text = text[:MAX_ITEM_CHARS] + "…"
        entry = f"- {date.today().isoformat()} {text}"
        # 完全相同的不重复记 (模型有时会反复记同一件事)
        if any(entry[2:] == old[2:] for old in self.items):
            return f"这件事之前已经记过了：「{text}」。"
        self.items.append(entry)
        self.save()
        return f"记住了：{text}（现在一共记着 {len(self.items)} 条）"

    def forget(self, keyword: str) -> str:
        """按关键词删掉记过的内容 (整行删, 不做模糊替换)。"""
        key = (keyword or "").strip()
        if not key:
            return "没说删哪条。"
        hits = [ln for ln in self.items if key in ln]
        if not hits:
            return f"我没记过和「{key}」有关的事。"
        self.items = [ln for ln in self.items if key not in ln]
        self.save()
        return f"忘掉了 {len(hits)} 条和「{key}」有关的记录。"

    # ---------- 读 ----------
    def render_for_prompt(self) -> str:
        """拼进 system prompt 的那段。没记忆就返回空串。"""
        if not self.items:
            return ""
        kept, total = [], 0
        for line in reversed(self.items):        # 从最近的往前装
            if total + len(line) > MAX_CHARS:
                break
            kept.append(line)
            total += len(line)
        kept.reverse()
        dropped = len(self.items) - len(kept)
        body = "\n".join(kept)
        head = (f"\n\n【你记得的关于用户的事】\n{body}\n"
                f"（这些是你之前记下的，自然地用起来，别照本宣科念出来。\n"
                f"要记新的就用 remember 工具；用户说忘了什么用 forget。）")
        if dropped:
            head += f"\n（更早的 {dropped} 条太久没用到，没放进来）"
        return head

    def summary(self) -> str:
        return f"记着 {len(self.items)} 条" if self.items else "还没记过什么"


def default_path() -> str:
    import app_paths
    return os.path.join(app_paths.user_data_dir(), "MEMORY.md")
