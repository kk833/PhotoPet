"""第四梯队功能 (v0.5): AI 对话 + 离线语音。

- AIChat: OpenAI 兼容接口对话 (参考 DeepSeek 官方 SDK 用法)。
  支持 DeepSeek / OpenAI / 任何兼容 base_url, 多轮上下文,
  人设 system prompt 来自 pet.json 的 persona 字段。
- PetTTS: 可插拔语音合成 (v0.8)。
  * edge 后端: 微软 Edge 神经网络语音, 音质远好于系统 SAPI5, 免费无需 key。
    合成结果按文本哈希缓存到本地, 同一句话第二次说是读文件 —— 零延迟且断网可用。
  * sapi 后端: pyttsx3 调用系统 SAPI5, 完全离线, 作为兜底。
  两个后端都在后台线程工作; 出声回调交给调用方切回主线程 (见 PetTTS.on_speak)。
"""
import hashlib
import json
import os
import queue
import sys
import threading
import time

import app_paths
import ai_tools

# 配置文件放用户数据目录 (源码运行时=仓库根, 打包后=exe 旁边或 %APPDATA%)
CONFIG_PATH = app_paths.config_path()
# 合成缓存放用户主目录, 不进仓库 (pets/ 与 ai_config.json 都是私有的)
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".photopet", "tts_cache")
# 启动自检用的一句话 (会被缓存, 所以只在第一次联网)
SELFTEST_TEXT = "语音自检"
# 连续失败降级后, 隔多久再试一次神经语音。
# 网络抖动是暂时的 —— 以前一旦降级就"本次会话不再用它", 结果宠物跑久了
# 遇上几次网络抖动就永久变成系统 SAPI5 老声音, 用户只听到"声音变难听了"。
EDGE_RETRY_SEC = 300

DEFAULT_CONFIG = {
    "api_key": "",
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "tts_enabled": False,
    "tts_engine": "auto",              # auto | edge | sapi
    "tts_voice": "zh-CN-XiaoyiNeural",  # edge 音色; sapi 时留空用系统默认
    "tts_rate": 0,                      # 语速偏移百分比, 如 10 = 快 10%
    "tts_cache_max_mb": 50,             # 语音缓存上限 (MB); 超了删最久没用过的; 0 = 不限
    "max_history": 10,
    "max_tokens": 400,                  # 单次回复上限; 助手场景 200 太短了
    # ---- 说话的时机 (v0.8.1): 默认定时说话全关, 只在你互动/久坐时开口 ----
    "chatter_seconds": 0,               # 定时闲聊间隔秒数; 0 = 关 (原为每 15 秒一句)
    # 让大模型"自己想说点什么"的间隔秒数; 0 = 关 (默认关, 见 pet-quiet-by-default)
    "ai_chatter_seconds": 0,
    "ai_chatter_daily_limit": 20,       # 一天最多主动说几次, 免得烧 token + 招人烦
    "random_action_minutes": 5,         # 随机小动作间隔分钟; 0 = 关。只动不出声
    "hungry_nag_minutes": 10,           # 饿了主动乞食的最小间隔分钟; 0 = 不主动乞食
    "rest_reminder_enabled": True,      # 久坐提醒 (v0.9 起会弹小窗让你选要不要睡)
    "rest_reminder_minutes": 45,        # 连续用电脑满这么多分钟就提醒
    "rest_reminder_idle_seconds": 60,   # 离开(无键鼠输入)超过这么久就重新计时
    "rest_snooze_minutes": 15,          # 选"再干一会儿"后, 多久再提醒
    "rest_prompt_seconds": 40,          # 劝休息小窗多久没人理就自动消失(算再干一会儿)
    "work_log_enabled": True,           # 统计"陪了你多久"(睡觉=今天收工)
    "talk_animation": True,             # 说话时切 animation/talk.gif (有素材才生效)
    "click_hold_ms": 2500,             # 点一下之后, click 动画保持多久再回待机
    # ---- 秋招模式 (v0.19) ----
    "jobfair_url": "",                 # 你学校的就业网宣讲会列表页（留空 = 没配）
    "jobfair_days_ahead": 7,            # 只关心未来几天内的宣讲会
    "jobfair_auto_hours": 0,            # 每隔几小时自动同步一次; 0 = 关 (默认关)
    # ---- 天气提醒 (v0.22): 只在下雨前提醒收衣服 ----
    "weather_city": "",                 # 留空 = 关 (不知道你在哪个城市就别瞎提醒)
    "weather_within_hours": 2,          # 未来几小时内下雨才提醒
    "weather_min_probability": 60,      # 降水概率到这个数才算"会下"
    "weather_check_minutes": 30,        # 多久看一眼预报
    # ---- 清理 (v0.24) ----
    "schedule_keep_past_days": 7,       # 过去的一次性日程保留几天; 0 = 不自动清
}

# 常见 edge 音色, 供 UI 提示 / 文档参考 (edge-tts --list-voices 可看全部)
EDGE_VOICES = {
    "zh-CN-XiaoxiaoNeural": "晓晓 · 女 · 温暖大气",
    "zh-CN-XiaoyiNeural": "晓伊 · 女 · 活泼俏皮",
    "zh-CN-YunxiNeural": "云希 · 男 · 阳光少年",
    "zh-CN-YunxiaNeural": "云夏 · 男 · 软萌可爱",
    "zh-CN-YunyangNeural": "云扬 · 男 · 沉稳专业",
    "zh-CN-YunjianNeural": "云健 · 男 · 激情",
    "zh-CN-liaoning-XiaobeiNeural": "辽宁小北 · 女 · 东北话",
    "zh-CN-shaanxi-XiaoniNeural": "陕西小妮 · 女 · 陕西话",
    "zh-HK-HiuGaaiNeural": "晓佳 · 女 · 粤语",
    "zh-HK-WanLungNeural": "云龙 · 男 · 粤语",
    "zh-TW-HsiaoChenNeural": "晓臻 · 女 · 台湾",
    "zh-TW-YunJheNeural": "云哲 · 男 · 台湾",
}


def save_ai_config(cfg: dict) -> bool:
    """把配置写回 ai_config.json (给「API 设置」窗口用)。

    只覆盖传进来的键, 其余保留 —— 免得设置窗口只改了个 key, 把别人的音色
    和其他开关全冲掉。
    """
    merged = dict(load_ai_config())
    merged.update(cfg or {})
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


class SentenceSplitter:
    """把流式文本切成**完整句子**, 好让宠物"想出一句说一句"。

    为什么需要它: 语音是排队念的, 每句都要单独合成。等整段回复生成完再读,
    首字要等好几秒; 而按标点切句后, 第一句话一出来就能开口。

    注意不要按逗号切: "你好，我是小王" 会被切成两句听起来很怪。切开的地方
    必须是真的句末 (。！？…换行), 逗号只在句子太长时才兜底切开。
    """

    ENDS = "。！？!?…\n"
    SOFT = "，,、；;"
    # 32 而不是更长: 这是"多快能开口"和"停顿自不自然"的折中。60 字的门槛下,
    # 一句 50 字的回复会整段等到生成完才出声 —— 实测过, 那等于没做流式朗读
    MAX_LEN = 32

    def __init__(self):
        self.buf = ""

    def feed(self, delta: str) -> list[str]:
        """喂一段新文本, 返回这次凑出来的完整句子 (可能 0~n 句)。"""
        self.buf += delta
        out = []
        while True:
            idx = self._next_break()
            if idx < 0:
                break
            piece = self.buf[:idx + 1].strip()
            self.buf = self.buf[idx + 1:]
            if piece:
                out.append(piece)
        return out

    def _next_break(self) -> int:
        hard = [self.buf.find(c) for c in self.ENDS if c in self.buf]
        if hard:
            return min(hard)
        if len(self.buf) >= self.MAX_LEN:          # 一直没句号 -> 按逗号兜底
            soft = [self.buf.find(c) for c in self.SOFT if c in self.buf]
            if soft:
                return min(soft)
        return -1

    def flush(self) -> str | None:
        """收尾: 把没说完的残句拿出来。"""
        rest = self.buf.strip()
        self.buf = ""
        return rest or None


def load_ai_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = DEFAULT_CONFIG | json.load(f)
    except (json.JSONDecodeError, OSError):
        cfg = dict(DEFAULT_CONFIG)
    return cfg


def prune_cache(max_mb: float = 50.0) -> tuple[int, int]:
    """语音缓存超过上限就删最旧的。返回 (删掉的个数, 释放的字节)。

    为什么需要: 每说过一句**不同**的话就存一个 mp3 —— 这是整套里唯一没有上限的东西
    （实测跑两天就 2.4 MB / 101 个）。删掉没有任何副作用: 下次说到同一句，
    能联网就重新合成，断网就走系统语音。

    按**最近访问时间**（mtime）删，而不是"最早创建的"：命中缓存时我们会 touch 一下，
    所以常说的那几句（比如久坐提醒、开场白）不会被误删。
    """
    if max_mb <= 0 or not os.path.isdir(CACHE_DIR):
        return 0, 0
    limit = int(max_mb * 1024 * 1024)
    entries = []
    total = 0
    try:
        for name in os.listdir(CACHE_DIR):
            if not name.endswith(".mp3"):
                continue
            full = os.path.join(CACHE_DIR, name)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, full))
            total += stat.st_size
    except OSError:
        return 0, 0
    if total <= limit:
        return 0, 0
    entries.sort()                      # 最久没用过的排前面
    removed = freed = 0
    # 一次多删一点（降到上限的 80%），免得每说一句都要再清一次
    target = int(limit * 0.8)
    for _, size, full in entries:
        if total <= target:
            break
        try:
            os.remove(full)
        except OSError:
            continue
        total -= size
        removed += 1
        freed += size
    if removed:
        log_stderr(f"[PhotoPet] 语音缓存超过 {max_mb:.0f}MB，"
                   f"清掉 {removed} 个最久没用过的（释放 {freed / 1048576:.1f}MB）")
    return removed, freed


def clean_for_speech(text: str) -> str:
    """去掉颜文字/emoji, 只保留会被念出来的字符 (音色念符号很怪)。"""
    return "".join(ch for ch in text
                   if ch.isascii() or "一" <= ch <= "鿿").strip()


def log_stderr(msg: str):
    """往 stderr 打一行。打包成 --windowed 的 exe 时 sys.stderr 可能是 None。"""
    stream = sys.stderr
    if stream is None:
        return
    try:
        print(msg, file=stream, flush=True)
    except (OSError, ValueError):
        pass


def estimate_seconds(text: str) -> float:
    """估算这句话大约要说多久(秒)。

    用途: 说话动画(口型)需要一个"什么时候算说完"的时长基准。
    edge 后端能拿到音频文件, 但首次未必立刻知道时长; sapi 后端则完全没有文件,
    所以统一用字数估算兜底 (中文约 5 字/秒, 含标点停顿), 夹在 1~15 秒。
    """
    if not text:
        return 1.0
    return max(1.0, min(15.0, len(text) / 5.0))


class AIChat:
    """多轮 AI 对话。未配置 api_key 时 available=False, UI 自动隐藏入口。"""

    def __init__(self, persona: str = ""):
        self.cfg = load_ai_config()
        self.history: list[dict] = []
        self.persona = persona or "你是用户的可爱桌面宠物，说话简短俏皮，喜欢用颜文字。"
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.cfg.get("api_key"))

    def chat(self, user_text: str, on_reply=None):
        """后台线程请求 LLM, 完成后回调 on_reply(text)。"""
        if not self.available:
            if on_reply:
                on_reply("我还没配置 AI 大脑，请在 ai_config.json 填入 api_key 哦")
            return

        def worker():
            try:
                reply = self._request(user_text)
            except Exception as e:  # noqa: BLE001
                reply = f"(连线失败: {e.__class__.__name__}，检查网络和 api_key)"
            if on_reply:
                on_reply(reply)

        threading.Thread(target=worker, daemon=True).start()

    def chat_stream(self, user_text: str, on_delta=None, on_sentence=None,
                    on_done=None, toolbox=None):
        """流式对话 (v0.12), 可选带**工具调用** (v0.13)。回调都在后台线程被调用。

        调用方负责把回调转成 Qt 信号再碰界面 (PyQt 里跨线程 emit 信号是安全的,
        直接操作控件才会随机崩)。

        - `on_delta(全文)`   每收到一小段就报一次全文, 用来实时刷界面
        - `on_sentence(句子)` 凑出一个完整句子就报一次, 用来"想出一句说一句"
        - `on_done(全文, 错误)` 结束时报一次; 错误是空串表示成功
        - `toolbox` 传 ai_tools.Toolbox 就开工具调用; 模型说"要调工具", 本地执行完
          把结果喂回去再让它接着说, 最多来回 MAX_TOOL_ROUNDS 轮 (防死循环)
        """
        if not self.available:
            if on_done:
                on_done("", "还没配置 api_key")
            return

        def worker():
            full, err = "", ""
            splitter = SentenceSplitter()
            try:
                from openai import OpenAI
                client = OpenAI(api_key=self.cfg["api_key"],
                                base_url=self.cfg["base_url"])
                # 开工具时把"今天几号"喂给模型 —— 没这个它算不对"明天""下周三"
                system = self.persona + (toolbox.system_extra() if toolbox else "")
                with self._lock:
                    self.history.append({"role": "user", "content": user_text})
                    messages = ([{"role": "system", "content": system}]
                                + self.history[-self.cfg["max_history"] * 2:])
                rounds = ai_tools.MAX_TOOL_ROUNDS if toolbox else 1
                for _ in range(rounds):
                    text, calls = self._stream_round(
                        client, messages, splitter, on_delta, on_sentence,
                        full, toolbox)
                    full += text
                    if not calls:
                        break
                    messages.append({"role": "assistant",
                                     "content": text or None,
                                     "tool_calls": calls})
                    for call in calls:
                        result = toolbox.call(call["function"]["name"],
                                              call["function"]["arguments"])
                        messages.append({"role": "tool",
                                         "tool_call_id": call["id"],
                                         "content": result})
                tail = splitter.flush()
                if tail and on_sentence:
                    on_sentence(tail)
                with self._lock:
                    # 只把"用户说的 + 最终回复"记进上下文, 工具来回不占历史
                    self.history.append({"role": "assistant", "content": full})
            except Exception as e:                       # noqa: BLE001
                err = f"{e.__class__.__name__}: {e}"
            if on_done:
                on_done(full, err)

        threading.Thread(target=worker, daemon=True).start()

    def _stream_round(self, client, messages, splitter, on_delta, on_sentence,
                      prefix, toolbox) -> tuple[str, list[dict]]:
        """跑一轮流式请求, 返回 (这轮的文本, 这轮请求的工具调用)。

        工具调用的参数是**分片**送来的 (delta.tool_calls[i].function.arguments
        是一段段 JSON 字符串), 必须按 index 拼起来才是完整参数 —— 这里写错的话
        表现是"模型偶尔调工具失败", 极难复现。
        """
        kwargs = dict(model=self.cfg["model"], messages=messages, stream=True,
                      max_tokens=int(self.cfg.get("max_tokens", 400)))
        if toolbox:
            kwargs["tools"] = toolbox.schemas()
        stream = client.chat.completions.create(**kwargs)
        text = ""
        calls: dict[int, dict] = {}
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None) or ""
            if piece:
                text += piece
                if on_delta:
                    on_delta(prefix + text)
                for sentence in splitter.feed(piece):
                    if on_sentence:
                        on_sentence(sentence)
            for tc in (getattr(delta, "tool_calls", None) or []):
                slot = calls.setdefault(tc.index, {
                    "id": "", "type": "function",
                    "function": {"name": "", "arguments": ""}})
                if getattr(tc, "id", None):
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["function"]["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        slot["function"]["arguments"] += fn.arguments
        return text, list(calls.values())

    def small_talk(self, context: str, on_reply=None):
        """主动搭话: 拿一段临时上下文让模型**现想一句** (v0.15)。

        和 chat() 的区别: 走单独的 system prompt, 而且**不进对话历史** ——
        否则宠物自己嘟囔的几句会污染你的聊天上下文, 你再问它"刚才说啥"它会答非所问。
        """
        if not self.available:
            if on_reply:
                on_reply("")
            return

        def worker():
            text = ""
            try:
                from openai import OpenAI
                client = OpenAI(api_key=self.cfg["api_key"],
                                base_url=self.cfg["base_url"])
                system = (self.persona +
                          "\n\n现在你要主动说一句话, 不要问'有什么可以帮你'这类客套话, "
                          "别复述上面的信息, 就当随口一提。20 字以内, 用中文。")
                resp = client.chat.completions.create(
                    model=self.cfg["model"],
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": context}],
                    max_tokens=60)
                text = (resp.choices[0].message.content or "").strip()
            except Exception:                            # noqa: BLE001
                text = ""                                # 主动搭话失败就该安静, 不报错
            if on_reply:
                on_reply(text)

        threading.Thread(target=worker, daemon=True).start()

    def test_connection(self) -> tuple[bool, str]:
        """设置窗口的"测试连接"。阻塞式, 调用方自己放线程里跑。"""
        if not self.available:
            return False, "还没填 api_key"
        try:
            from openai import OpenAI
            client = OpenAI(api_key=self.cfg["api_key"],
                            base_url=self.cfg["base_url"])
            resp = client.chat.completions.create(
                model=self.cfg["model"],
                messages=[{"role": "user", "content": "回复两个字：你好"}],
                max_tokens=8)
            got = (resp.choices[0].message.content or "").strip()
            return True, f"连上了，模型回：{got}"
        except Exception as e:                           # noqa: BLE001
            return False, f"{e.__class__.__name__}: {e}"

    def _request(self, user_text: str) -> str:
        # 延迟导入 openai, 未安装且未配置时不影响其他功能
        from openai import OpenAI
        client = OpenAI(api_key=self.cfg["api_key"],
                        base_url=self.cfg["base_url"])
        with self._lock:
            self.history.append({"role": "user", "content": user_text})
            messages = ([{"role": "system", "content": self.persona}]
                        + self.history[-self.cfg["max_history"] * 2:])
            resp = client.chat.completions.create(
                model=self.cfg["model"], messages=messages,
                max_tokens=int(self.cfg.get("max_tokens", 400)))
            reply = (resp.choices[0].message.content or "").strip()
            self.history.append({"role": "assistant", "content": reply})
            return reply


class _EdgeBackend:
    """edge-tts 神经网络语音 (微软 Edge 的在线接口, 免费无需 key)。

    合成到本地缓存文件, 由调用方播放; 同一句话第二次说是读文件, 零延迟且断网可用。
    断网/接口变更/未装库时 synth() 返回 None, 由 PetTTS 回退到 sapi。
    """

    name = "edge"

    def __init__(self, voice: str, rate_pct: int):
        self.voice = voice or DEFAULT_CONFIG["tts_voice"]
        self.rate = "%+d%%" % int(rate_pct)
        self._mod = None
        self.last_error = ""        # 最近一次失败原因, 排查用 (见 PetTTS.describe)
        self._reported = False      # 只往 stderr 报一次, 不刷屏

    def prepare(self) -> bool:
        try:
            import edge_tts
        except ImportError:
            return False
        self._mod = edge_tts
        return True

    def _cache_path(self, text: str) -> str:
        key = "%s|%s|%s" % (self.voice, self.rate, text)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        return os.path.join(CACHE_DIR, digest + ".mp3")

    def synth(self, text: str) -> str | None:
        """返回可播放的音频路径; 失败返回 None。命中缓存则不联网。"""
        path = self._cache_path(text)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            # 命中缓存时更新访问时间 —— 清理是按"最近用过"删的，
            # 不 touch 的话 mtime 一直是创建时间，常说的老台词会被当成冷门删掉
            try:
                os.utime(path, None)
            except OSError:
                pass
            return path
        try:
            import asyncio
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = path + ".part"
            asyncio.run(self._mod.Communicate(
                text, self.voice, rate=self.rate).save(tmp))
            if os.path.getsize(tmp) <= 0:
                os.remove(tmp)
                return None
            os.replace(tmp, path)
            return path
        except Exception as e:  # noqa: BLE001  断网/接口变更/无写权限 -> 交给兜底
            # 把原因记下来: 以前这里静默吞掉, 结果打包成 exe 后语音悄悄降级成
            # 系统 SAPI5 老语音, 用户只听到"声音变难听了"却无从排查
            self.last_error = f"{type(e).__name__}: {e}"
            if not self._reported:
                self._reported = True
                log_stderr("[PhotoPet] 神经语音合成失败, 本句改用系统语音 —— "
                           f"原因: {self.last_error}")
            return None


class _SapiBackend:
    """pyttsx3 (Windows SAPI5) 离线语音, 直接出声, 作为离线兜底。"""

    name = "sapi"

    def __init__(self, voice: str = "", rate_pct: int = 0):
        self._engine = None
        # SAPI5 的 rate 是词/分钟 (默认 200), 按百分比折算
        self._rate = max(50, int(200 * (1 + rate_pct / 100.0)))

    def prepare(self) -> bool:
        try:
            import pyttsx3
            self._engine = pyttsx3.init()
            self._engine.setProperty("rate", self._rate)
            return True
        except Exception:  # noqa: BLE001  未装库或无语音引擎时静默禁用
            self._engine = None
            return False

    def speak(self, text: str) -> bool:
        if self._engine is None:
            return False
        try:
            self._engine.say(text)
            self._engine.runAndWait()
            return True
        except Exception:  # noqa: BLE001
            return False


class PetTTS:
    """可插拔语音合成。后台线程串行处理, 音频与时长通过 on_speak 回调交给调用方。

    两个标志必须分开, 否则「语音开关」形同虚设:
    - engine_ready: 至少一个后端可用 (真能出声)
    - on: 用户开关 (右键菜单「语音开关」或 ai_config.json 的 tts_enabled)
    speak() 只在两者同时为真时朗读。

    配置 (ai_config.json):
    - tts_engine: auto(默认, 优先 edge, 失败回退 sapi) / edge / sapi
    - tts_voice:  edge 音色, 见 EDGE_VOICES
    - tts_rate:   语速偏移百分比 (10 = 快 10%)

    注意: on_speak(path, est_seconds) 是在**工作线程**里调用的, 调用方必须自己
    切回主线程再碰 GUI (PetWindow 用 Qt 信号做这件事)。
    """

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self._cfg = cfg            # 留一份: 清理缓存这类"顺带的事"要从里面读配置
        self._want = str(cfg.get("tts_engine", "auto")).lower()
        voice = str(cfg.get("tts_voice", "") or DEFAULT_CONFIG["tts_voice"])
        try:
            rate = int(cfg.get("tts_rate", 0))
        except (TypeError, ValueError):
            rate = 0

        self.engine_ready = False
        self.on = False
        self.engine_name = "none"
        self.edge_available = False
        self.init_done = False        # 工作线程完成后端初始化后置 True
        # callable(path_or_None, est_seconds), 在**工作线程**被调用 ——
        # path 为 None 表示这次是 sapi 直接发声(没有音频文件), 只能用估算时长
        self.on_speak = None
        self._edge = _EdgeBackend(voice, rate)
        self._sapi = _SapiBackend(voice, rate)
        self._edge_fails = 0
        self._edge_disabled = False
        self._edge_retry_at = 0.0     # 降级冷却到期时间, 到点自动再试 edge
        self._q: queue.Queue[str] = queue.Queue()
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def _edge_usable(self) -> bool:
        """edge 现在能不能试。降级只是**暂时**的: 冷却时间一过就自动重试。"""
        if not self._edge_disabled:
            return True
        if time.time() >= self._edge_retry_at:
            self._edge_disabled = False
            self._edge_fails = 0
            log_stderr("[PhotoPet] 再试一次神经语音…")
            return True
        return False

    def _loop(self):
        # 后端初始化放在工作线程, 避免拖慢桌宠启动
        use_edge = self._want in ("auto", "edge") and self._edge.prepare()
        sapi_ok = self._sapi.prepare() if self._want in ("auto", "sapi") else False
        if self._want == "edge" and not use_edge:
            sapi_ok = sapi_ok or self._sapi.prepare()   # 指定 edge 但不可用时兜底

        # 语音缓存清理放在工作线程做（要列目录、删文件，别拖慢启动）。
        # 上限从配置读，用户把它设成 0 就不清
        try:
            prune_cache(float((self._cfg or {}).get("tts_cache_max_mb", 50) or 0))
        except Exception:                                # noqa: BLE001
            pass

        self.edge_available = use_edge
        self.engine_ready = use_edge or sapi_ok
        self.engine_name = "edge" if use_edge else ("sapi" if sapi_ok else "none")
        self.init_done = True

        # 启动自检: 试合成一句固定的话, 把结果写到 stderr。
        # 打包成 exe 后语音会悄悄降级成系统 SAPI5 (听起来"变难听了"),
        # 而 synth() 以前把异常全吞了 —— 这里是唯一能看到原因的途径。
        if use_edge:
            probe = self._edge.synth(SELFTEST_TEXT)
            if probe:
                log_stderr(f"[PhotoPet] 神经语音就绪 · {self._edge.voice}")
            else:
                log_stderr("[PhotoPet] 神经语音不可用, 已回退系统语音 —— "
                           f"原因: {self._edge.last_error}")
        elif sapi_ok:
            log_stderr("[PhotoPet] 只找到系统语音 (edge-tts 未安装或被禁用)")

        if not self.engine_ready:
            log_stderr("[PhotoPet] 没有任何可用的语音引擎")
            return

        while True:
            text = self._q.get()
            played = False
            est = estimate_seconds(text)
            if use_edge and self._edge_usable():
                path = self._edge.synth(text)
                if path:
                    self._edge_fails = 0
                    if self.on_speak:
                        try:
                            self.on_speak(path, est)
                            played = True
                        except Exception:  # noqa: BLE001  播放失败仍可回退
                            played = False
                else:
                    # 网络抖动/接口变更: 这一句改用 sapi; 连续失败则本次会话不再试 edge
                    self._edge_fails += 1
                    if self._edge_fails >= 3:
                        self._edge_disabled = True
                        self._edge_retry_at = time.time() + EDGE_RETRY_SEC
                        log_stderr(
                            f"[PhotoPet] 神经语音连续失败 {self._edge_fails} 次, "
                            f"{EDGE_RETRY_SEC // 60} 分钟后再自动重试; 期间用系统语音")
            if not played and sapi_ok:
                # sapi 没有音频文件, 也要通知一声, 否则说话动画永远不触发
                if self.on_speak:
                    try:
                        self.on_speak(None, est)
                    except Exception:  # noqa: BLE001
                        pass
                self._sapi.speak(text)

    @property
    def active_engine(self) -> str:
        """此刻真正在用的后端 (engine_name 是启动时的首选, 降级后仍是 edge)。"""
        if self.engine_name == "edge" and not self._edge_disabled:
            return "edge"
        return "sapi" if self.engine_ready else "none"

    def describe(self) -> str:
        """菜单/提示里显示当前在用什么声音。"""
        if not self.init_done:
            return "语音初始化中…"      # 启动头一两秒菜单就打开时别误报"不可用"
        if not self.engine_ready:
            return "语音不可用 (pip install edge-tts)"
        if self.engine_name == "edge":
            if self._edge_disabled:
                return "系统语音 · 神经语音不可用已降级"
            label = EDGE_VOICES.get(self._edge.voice, self._edge.voice)
            if self._edge_fails:
                # 音色名写错 / 网络抖动都会走到这里, 让用户看得见
                return f"神经语音 · {label} (上次失败, 正用系统语音兜底)"
            return "神经语音 · " + label
        return "系统语音 · SAPI5"

    def speak(self, text: str):
        if self.on and self.engine_ready:
            clean = clean_for_speech(text)
            if clean:
                self._q.put(clean)
