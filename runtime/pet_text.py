"""文本匹配小工具 —— 模型给的"关键词"和用户存的"标题"对不上时用的。

来由: 端到端测出过一个最阴的失败 —— 用户说"把开会的提醒取消掉", 模型拿
「**开会的提醒**」这个描述去调 delete_event, 而标题只有「开会」两个字, 精确包含
匹配没命中、返回了错误, **而模型无视错误照样回"已经删掉了"**。用户以为删了其实还在。

所以凡是"模型指代某条已有数据"的地方, 都得先精确匹配、再按最长公共子串兜底。
"""
MIN_LCS = 2         # 至少两字才谈得上"像"


def best_match(key: str, items: list, text_of=None) -> list:
    """在 items 里找最像 key 的那几个 (可能多个并列)。找不到返回 []。

    `text_of` 不给就默认把元素当字符串。

    **门槛是相对的, 不是固定的两个字**: 中文里"公司""会议"这类词几乎每个标题都有,
    固定阈值会让"不存在的公司"匹配上一堆不相干的东西 ——
    实测就是这么发现问题的（拿"不存在的公司"去查, 回了一句"有 32 条都对得上"）。
    所以要求公共子串**至少占到较短那方的一半**:
      "吉安生益" vs "吉安生益电子有限公司" (4/4) ✓
      "开会的提醒" vs "开会" (2/2) ✓
      "不存在的公司" vs "吉安生益电子有限公司" (2/6) ✗ 挡掉
    """
    key = (key or "").strip()
    if not key or not items:
        return []
    get = text_of or (lambda x: str(x))
    exact = [it for it in items if key in get(it)]
    if exact:
        return exact
    scored = [(_score(key, get(it)), it) for it in items]
    top = max(score for score, _ in scored)
    if top <= 0:
        return []
    return [it for score, it in scored if score == top]


def _score(key: str, text: str) -> int:
    """够格的相似度（不够格返回 0）。"""
    if not text:
        return 0
    lcs = _lcs(key, text)
    need = max(MIN_LCS, (min(len(key), len(text)) + 1) // 2)
    return lcs if lcs >= need else 0


def _lcs(a: str, b: str) -> int:
    """最长公共子串长度。标题都很短, 朴素算法足够。"""
    best = 0
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            best = max(best, k)
    return best
