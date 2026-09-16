"""宠物养成状态系统 (v0.3)。

设计参考开源项目 DyberPet 的养成模型:
- 饱食度 (hunger): 随时间衰减, 喂食回复
- 心情 (mood): 随时间衰减, 互动回复
- 好感度 (favor): 只增不减, 累计升级
- 状态持久化: JSON 存档, 关闭后数值保留 (含离线衰减补算)

状态影响行为:
- mood >= 70: 开心, 台词偏活泼
- 30 <= mood < 70: 平静
- mood < 30 或 hunger < 30: 乞食/低落气泡
"""
import json
import os
import time

MAX_STAT = 100
FAVOR_PER_LEVEL = 100  # 每级好感度所需


class PetStatus:
    """一只宠物的养成数值。每只 PetWindow 独立持有, 存到资源包目录。"""

    def __init__(self, pet_dir: str):
        self.save_path = os.path.join(pet_dir, "status.json")
        self.hunger = MAX_STAT    # 饱食度 (越高越饱)
        self.mood = MAX_STAT      # 心情
        self.favor = 0            # 好感度 (累计, 不衰减)
        self.last_ts = time.time()
        self.load()

    # ---------- 持久化 (参考 Tamagotchi 类项目的 save.json 模式) ----------
    def load(self):
        if not os.path.exists(self.save_path):
            return
        try:
            with open(self.save_path, encoding="utf-8") as f:
                d = json.load(f)
            self.hunger = float(d.get("hunger", MAX_STAT))
            self.mood = float(d.get("mood", MAX_STAT))
            self.favor = float(d.get("favor", 0))
            self.last_ts = float(d.get("last_ts", time.time()))
            # 离线衰减: 按离开时长补算 (封顶 8 小时, 避免缺席惩罚过重)
            offline = min(time.time() - self.last_ts, 8 * 3600)
            hours = offline / 3600
            self.hunger = max(0, self.hunger - hours * 6)
            self.mood = max(0, self.mood - hours * 4)
        except (json.JSONDecodeError, ValueError, OSError):
            pass  # 存档损坏时用默认值

    def save(self):
        self.last_ts = time.time()
        try:
            with open(self.save_path, "w", encoding="utf-8") as f:
                json.dump({
                    "hunger": round(self.hunger, 1),
                    "mood": round(self.mood, 1),
                    "favor": round(self.favor, 1),
                    "last_ts": self.last_ts,
                }, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ---------- 衰减 (参考 DyberPet StatusManager: 定时器驱动) ----------
    def tick(self, minutes: float):
        """每分钟调用一次; hunger 衰减快于 mood。"""
        self.hunger = max(0, self.hunger - minutes * 0.8)
        self.mood = max(0, self.mood - minutes * 0.4)

    # ---------- 互动 ----------
    def feed(self):
        """喂食: 饱食 +30, 心情 +10, 好感 +5。"""
        self.hunger = min(MAX_STAT, self.hunger + 30)
        self.mood = min(MAX_STAT, self.mood + 10)
        return self.add_favor(5)

    def petted(self):
        """被点击/抚摸: 心情 +5, 好感 +2。"""
        self.mood = min(MAX_STAT, self.mood + 5)
        return self.add_favor(2)

    def add_favor(self, amount: float) -> bool:
        """好感度增加, 跨过等级阈值时返回 True (触发升级气泡)。"""
        before = int(self.favor // FAVOR_PER_LEVEL)
        self.favor += amount
        after = int(self.favor // FAVOR_PER_LEVEL)
        return after > before

    # ---------- 查询 ----------
    @property
    def level(self) -> int:
        return int(self.favor // FAVOR_PER_LEVEL) + 1

    @property
    def is_hungry(self) -> bool:
        return self.hunger < 30

    @property
    def is_sad(self) -> bool:
        return self.mood < 30

    def mood_tier(self) -> str:
        if self.is_hungry:
            return "hungry"
        if self.is_sad:
            return "sad"
        if self.mood >= 70:
            return "happy"
        return "calm"
