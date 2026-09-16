"""我的简历 (v0.26) —— 让它认识你，然后替你挑宣讲会。

PDF / Word 简历读成纯文本存在 `resume.txt`，之后：

- 聊天里问「哪些宣讲会适合我」时，它把**近期宣讲会的详情页**和这份简历一起看一遍，
  给出「这几场值得去 + 为什么」
- 以后问「帮我写个自我介绍」「我适合投什么」也能用得上

**三个刻意的设计**:

1. **不往每轮对话里塞**。简历几千字，每轮都注入会白烧 token；只在真正要做匹配时
   才拿出来（对比一下 [[pet_memory]] 那点记忆量才适合每轮注入）。
2. **只存纯文本**，不存原始 PDF —— 文件里可能有你要投的所有公司、手机号、住址，
   留一份够用的文本就行，别把原件也复制一份到程序目录里。
3. **匹配只标注、不删除**（见 match 的文档）：用户明确要求过宣讲会"宁多报不漏"，
   所以推荐结果**不会**动日程里任何一条，只是额外告诉你哪几场值得去。
"""
import io
import os

import pet_import

MAX_CHARS = 8000            # 存进来的上限，够放一页简历 + 项目经历


def default_path() -> str:
    import app_paths
    return os.path.join(app_paths.user_data_dir(), "resume.txt")


def save_from_file(src: str, dest: str | None = None) -> tuple[str, str]:
    """从 PDF/Word 读进来并存下。返回 (存下来的文本, 错误说明)。"""
    dest = dest or default_path()
    try:
        result = pet_import.read_file(src)
    except ValueError as e:
        return "", str(e)
    if result.empty or len(result.text.strip()) < 30:
        return "", "这份文件里没读到什么内容，是不是空的？"
    text = result.text.strip()[:MAX_CHARS]
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with io.open(dest, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        return "", f"存不下来：{e}"
    return text, ""


def load(path: str | None = None) -> str:
    """读回简历正文。没有就返回空串。"""
    try:
        with io.open(path or default_path(), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def has_resume(path: str | None = None) -> bool:
    return bool(load(path).strip())


def forget(path: str | None = None) -> bool:
    """删掉简历（用户说"忘掉我的简历"时用）。"""
    target = path or default_path()
    try:
        os.remove(target)
        return True
    except OSError:
        return False


def headline(text: str | None = None) -> str:
    """从简历里抽一行"这是谁"—— 只用来给用户确认"我读到了什么"，不做别的。

    只取最前面的非空行（简历第一行通常是姓名），并截断 —— 不要把整份简历
    回显到屏幕上（旁边有人看着的时候很尴尬）。
    """
    text = text if text is not None else load()
    if not text.strip():
        return ""
    for line in text.splitlines():
        line = line.strip()
        if 2 <= len(line) <= 40:
            return line
    return ""


def render_for_matching() -> str:
    """给"挑宣讲会"用的简历正文。没简历就返回空串。"""
    text = load()
    if not text.strip():
        return ""
    return (f"【我的简历】\n{text}\n"
            f"（以上是用户自己的简历。挑宣讲会时**只做推荐和排序，"
            f"绝对不要建议删掉任何一场** —— 用户说过漏掉一场比多看几场严重。）")
