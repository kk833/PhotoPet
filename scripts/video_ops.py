# -*- coding: utf-8 -*-
"""视频帧处理: 背景自适应抠像 + 硬切检测与平滑 (v0.10.2)。

原来 add_asset.py 里的抠像是**针对绿幕写死**的（只看绿色优势度），
用户给白底、蓝底或者别的纯色就抠不干净。这里换成自适应的做法：

1. 从每帧的四周边框**采样出背景色**（不假设它是什么颜色）
2. 按"到背景色的距离"生成软 alpha
3. **只把与画面边缘连通的相似色区域判为背景** —— 这一步很关键：
   否则穿白衣服的人站在白墙前, 衣服会跟背景一起被抠掉
4. 补主体内部的小洞、去掉散落的小块
5. 去溢色（只在背景**有颜色倾向**时做；白/灰背景没有溢色可言）

另外提供硬切处理：AI 生成的视频里常有把两个镜头直接接在一起的硬切，
逐帧看就是一个突变（"抽搐一下"）。检测出来之后用抖动溶解摊平，
每个像素在过渡中只翻转一次、且不产生新颜色。

需要 numpy / scipy；处理视频还需要 imageio 与 imageio-ffmpeg。
"""
import numpy as np


# ---------------------------------------------------------------- 背景分析
def sample_bg_color(frames: np.ndarray, border_ratio: float = 0.04) -> np.ndarray:
    """从每帧的四周边框采样背景色, 取中位数（抗单帧噪声）。

    frames: (N, H, W, 3) float32。绝大多数素材里, 画面最外圈一定是背景。
    """
    h, w = frames.shape[1], frames.shape[2]
    bw = max(2, int(min(h, w) * border_ratio))
    border = np.concatenate([
        frames[:, :bw, :].reshape(-1, 3),
        frames[:, -bw:, :].reshape(-1, 3),
        frames[:, :, :bw].reshape(-1, 3),
        frames[:, :, -bw:].reshape(-1, 3),
    ], axis=0)
    return np.median(border, axis=0)


def border_uniformity(frames: np.ndarray, bg: np.ndarray | None = None) -> float:
    """边框像素到背景色的平均距离。越小说明背景越"纯"（越适合色度抠像）。

    用它来判断该走色度抠像（快、稳）还是逐帧 rembg（慢、但任意背景都能用）。
    """
    bg = sample_bg_color(frames) if bg is None else bg
    h, w = frames.shape[1], frames.shape[2]
    bw = max(2, int(min(h, w) * 0.04))
    border = np.concatenate([
        frames[:, :bw, :].reshape(-1, 3), frames[:, -bw:, :].reshape(-1, 3),
        frames[:, :, :bw].reshape(-1, 3), frames[:, :, -bw:].reshape(-1, 3),
    ], axis=0)
    return float(np.linalg.norm(border - bg, axis=-1).mean())


def describe_background(frames: np.ndarray) -> str:
    bg = sample_bg_color(frames)
    u = border_uniformity(frames, bg)
    if u < 12:
        kind = "纯色背景"
    elif u < 35:
        kind = "接近纯色（有渐变/噪点）"
    else:
        kind = "复杂背景"
    return (f"{kind}，背景色约 RGB({bg[0]:.0f},{bg[1]:.0f},{bg[2]:.0f})，"
            f"边框均匀度 {u:.1f}")


# ---------------------------------------------------------------- 抠像
def drop_detached_specks(subj: np.ndarray, gap: int = 6) -> np.ndarray:
    """去掉"离主体很远的小碎块"。

    AI 生成视频的角落里常有「AI生成」水印/角标: 它是浅色的, 和绿幕差得远,
    于是被当成"主体"留下来, 最后桌宠边上就挂着一个淡淡的角标。

    判据用**距离**而不是面积: 主体旁边脱落的小部件（一缕发丝、小配饰）离主体
    很近, 膨胀几格就能碰到, 于是保留; 水印孤零零待在角上, 怎么膨胀都碰不到主体。
    """
    from scipy import ndimage as ndi

    lab, n = ndi.label(subj)
    if n <= 1:
        return subj
    sizes = ndi.sum(subj, lab, index=range(1, n + 1))
    main = int(np.argmax(sizes)) + 1
    near = ndi.binary_dilation(lab == main, iterations=gap)
    touching = np.unique(lab[near])            # 向量化: 别一个个 component 去比
    return np.isin(lab, touching) & subj


def key_frames(frames: np.ndarray, bg: np.ndarray | None = None,
               soft_lo: float = 30.0, soft_hi: float = 95.0,
               border_connected: bool = True,
               drop_specks: bool = True) -> np.ndarray:
    """生成软 alpha（0~1）。frames: (N,H,W,3) float32 -> (N,H,W) float32。

    border_connected=True 时只把**与画面边缘连通**的相似色区域当背景，
    这样主体上恰好和背景同色的部分（白衣服 vs 白墙）不会被误抠 ——
    前提是它们之间有一条能看出来的边界（真实照片里总是有：轮廓、阴影、
    抗锯齿边）。如果主体和背景同色而且**无缝直接相连**，那在图像上就是
    同一块区域，任何算法都无从区分, 这种情况只能靠用户换一张素材。
    """
    from scipy import ndimage as ndi

    bg = sample_bg_color(frames) if bg is None else np.asarray(bg, np.float32)
    dist = np.linalg.norm(frames - bg, axis=-1)                 # (N,H,W)
    t = np.clip((dist - soft_lo) / max(1.0, soft_hi - soft_lo), 0.0, 1.0)

    if not border_connected:
        alpha = t.copy()
    else:
        similar = dist < soft_hi
        alpha = np.ones_like(dist)
        for i in range(similar.shape[0]):
            lab, n = ndi.label(similar[i])
            if n == 0:
                continue
            edge = set(lab[0, :]) | set(lab[-1, :]) | set(lab[:, 0]) | set(lab[:, -1])
            edge.discard(0)
            if edge:
                region = np.isin(lab, list(edge))
                alpha[i] = np.where(region, t[i], 1.0)

    # 主体补洞 + 去散块: 让 mask 稳定, 不在内部留针孔
    subj = alpha > 0.5
    subj = ndi.binary_fill_holes(subj)
    lab, n = ndi.label(subj)
    if n > 1:
        areas = ndi.sum(subj, lab, index=range(1, n + 1))
        keep = [i + 1 for i, a in enumerate(areas) if a >= 0.0008 * subj[0].size]
        if keep:
            subj = np.isin(lab, keep)
    if drop_specks:
        # 逐帧做: 水印位置固定, 每帧都会被同一套判据清掉
        #
        # 曾经还试过按"浅色 + 低饱和 + 静止"去认水印并抹掉, 实测**不能用**:
        # 在两条没有水印的素材上误报, 各吃掉 5% 的主体（浅粉裙子被当成水印）,
        # 同时又漏掉了两处真水印。在干净素材上损坏主体, 比留一个淡淡的角标糟得多,
        # 所以只保留这条"离主体远"的安全判据 —— 贴着主体的水印识别不了,
        # 那种情况请重新导出无水印的素材, 或先用编辑器把角标裁掉。
        subj = np.stack([drop_detached_specks(s) for s in subj])
    alpha = np.where(subj, np.maximum(alpha, 0.9), np.minimum(alpha, 0.45))
    return alpha.astype(np.float32)


def remove_spill(frames: np.ndarray, alpha: np.ndarray,
                 bg: np.ndarray | None = None) -> np.ndarray:
    """去掉边缘半透明像素上的背景色"染色"。

    只对**有颜色倾向**的背景做（绿幕/蓝幕）；白底灰底的溢色是伪概念,
    硬去反而会把画面拉偏。做法: 找出背景最突出的通道, 把它钳到另外两通道
    的最大值（绿幕时就是原来的"绿通道压回 max(r,b)"）。
    """
    bg = sample_bg_color(frames) if bg is None else np.asarray(bg, np.float32)
    hi, lo = float(bg.max()), float(bg.min())
    saturation = (hi - lo) / max(1.0, hi)
    if saturation < 0.25:                 # 白/灰/黑背景, 没有溢色
        return frames
    out = frames.copy()
    dom = int(np.argmax(bg))
    others = [c for c in range(3) if c != dom]
    ceiling = np.maximum(out[..., others[0]], out[..., others[1]])
    edge = (alpha > 0.02) & (alpha < 0.98)
    out[..., dom] = np.where(edge, np.minimum(out[..., dom], ceiling), out[..., dom])
    return out


def to_rgba(frames: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """(N,H,W,3) + (N,H,W) -> (N,H,W,4) uint8。"""
    rgb = np.clip(frames, 0, 255)
    a = np.clip(alpha * 255.0, 0, 255)[..., None]
    # 注意用 concatenate 而不是 dstack: 输入是 4 维 (N,H,W,C),
    # dstack 只对 3 维有效, 会把 (N,H,W,3) 和 (N,H,W) 拼不上
    return np.concatenate([rgb, a], axis=-1).astype(np.uint8)


# ---------------------------------------------------------------- 硬切
def frame_diffs(frames: np.ndarray) -> np.ndarray:
    """相邻帧的平均绝对差, 长度 N-1。"""
    a = frames.astype(np.float32)
    if a.shape[-1] == 4:                  # RGBA: 只比 RGB, alpha 单独算
        rgb = np.abs(a[1:, ..., :3] - a[:-1, ..., :3]).mean(axis=(1, 2, 3))
        al = np.abs(a[1:, ..., 3] - a[:-1, ..., 3]).mean(axis=(1, 2))
        return rgb + al
    return np.abs(a[1:] - a[:-1]).mean(axis=(1, 2, 3))


def detect_cuts(frames: np.ndarray, ratio: float = 4.0, min_abs: float = 25.0,
                spike: float = 1.6) -> list[int]:
    """找硬切。返回帧号列表, 表示 frames[i-1] -> frames[i] 之间突变。

    三个条件同时满足才算硬切:
    1. 明显高于本片的正常帧间变化（ratio 倍中位数）
    2. 绝对值也够大（min_abs）—— 太小的"跳"在 320px 动画里本来就看不太出来,
       抹平它只会白白增加帧数和体积
    3. **是个尖峰**: 比左右相邻的帧间差都大出 spike 倍

    第 3 条是必需的: "轻戳"这类快速动作会让连续好几帧的帧间差都很高
    (实测 34.3 / 30.0 / 34.0), 只按阈值判会把它当硬切去抹平 —— 而那正是
    动画该有的利落感。硬切是**瞬间**的, 两侧必然落回正常水平。

    注意: **要传 RGB 帧来检测**。逐帧抠图产生的 alpha 抖动是 mask 在闪,
    不是镜头突变, 算进去会到处误报。
    """
    d = frame_diffs(frames)
    if d.size == 0:
        return []
    med = float(np.median(d))
    thr = max(min_abs, med * ratio)
    cuts = []
    for i, v in enumerate(d):
        if v <= thr:
            continue
        prev = d[i - 1] if i > 0 else 0.0
        nxt = d[i + 1] if i + 1 < len(d) else 0.0
        if v > max(float(prev), float(nxt)) * spike:
            cuts.append(int(i + 1))
    return cuts


def chroma_usable(frames: np.ndarray, alpha: np.ndarray,
                  bg: np.ndarray | None = None,
                  soft_hi: float = 95.0) -> tuple[bool, float]:
    """颜色抠像在这段素材上够不够用 —— 直接看**结果**, 而不是猜。

    判据: 在画面最外圈里, **那些看起来就像背景的像素**, 有没有被抠掉。
    为什么不能直接算"边框整体抠净率": 主体常常会伸到画面下边缘(宠物坐下、
    躺下), 那些像素本来就不是背景, 算进分母会让一根干净的绿幕也判成"复杂背景",
    白白去跑慢几十倍的 rembg。
    """
    bg = sample_bg_color(frames) if bg is None else np.asarray(bg, np.float32)
    a = np.asarray(alpha)
    h, w = a.shape[-2], a.shape[-1]
    bw = max(2, int(min(h, w) * 0.03))
    rgb_b = np.concatenate([frames[..., :bw, :, :].reshape(-1, 3),
                            frames[..., -bw:, :, :].reshape(-1, 3),
                            frames[..., :, :bw, :].reshape(-1, 3),
                            frames[..., :, -bw:, :].reshape(-1, 3)], axis=0)
    a_b = np.concatenate([a[..., :bw, :].ravel(), a[..., -bw:, :].ravel(),
                          a[..., :, :bw].ravel(), a[..., :, -bw:].ravel()])
    looks_like_bg = np.linalg.norm(rgb_b - bg, axis=-1) < soft_hi
    # 边框里大部分像素都不像这个"背景色" -> 背景根本不是单一颜色,
    # 说明这是复杂背景（真实房间之类）, 颜色抠像不适用
    if looks_like_bg.mean() < 0.5:
        return False, float(looks_like_bg.mean())
    removed = float((a_b[looks_like_bg] < 0.25).mean())
    # 阈值 0.85 的余量很大: 实测真绿幕是 90~98%, 而复杂背景只有 7% 上下
    return removed >= 0.85, removed


def smooth_cut(rgba_i: np.ndarray, rgba_j: np.ndarray, n: int = 10,
               seed: int = 20260915) -> list[np.ndarray]:
    """在两帧之间插入 n 张交叉溶解（溶镜）过渡帧。

    这里踩过一次坑, 记下来: 最初用的是**抖动溶解**（每个像素按固定噪声图逐个
    翻转）。它的数值指标非常漂亮 —— 每步的像素差都压进了动画自身的正常幅度 ——
    但**肉眼一看是花屏**: 两个姿势差太远（站着打哈欠 vs 躺下）, 混起来就是一层
    噪点状的双重曝光。**数值好看不等于好看。**

    交叉溶解（两帧按比例混合）会产生重影, 但那是电影里常见的溶镜观感, 比噪声
    舒服得多。代价: GIF 只有 1-bit 透明, 混合后的 alpha 过阈值时轮廓会有一次
    台阶, 这是可以接受的。
    """
    rng = np.random.default_rng(seed)
    noise = rng.random(rgba_i.shape[:2])         # 固定的阈值图, 全程不变
    out = []
    for k in range(1, n + 1):
        t = k / (n + 1.0)
        f = (rgba_i.astype(np.float32) * (1.0 - t)
             + rgba_j.astype(np.float32) * t)
        out.append(np.clip(f, 0, 255).round().astype(np.uint8))
    return out


def close_loop_seam(frames: np.ndarray, target: float = 1.5,
                    min_n: int = 3, max_n: int = 10
                    ) -> tuple[np.ndarray, int]:
    """把循环动画的**首尾接缝**用交叉溶解缝合。

    会一直循环播放的动画(待机、睡着后的循环段), 如果源素材首尾没闭合成一个
    完美的环, 每转一圈都会看到一次跳变 —— 实测你的 sleeping loop 每 6.4 秒
    就跳一次（接缝 7.52, 是内部最大帧间差 3.15 的 2.4 倍）。

    做法是在"末帧 → 首帧"之间插几帧过渡: 播到末帧后溶回开头, 再回绕到真正的
    首帧时两边已经几乎一样了。插入帧数按接缝大小自适应, 目标是每步变化降到
    target 以下 —— 小接缝插 3 帧, 大接缝最多插 max_n 帧。

    返回 (新帧序列, 插入了多少帧)。接缝本来就够小就原样返回。
    """
    if len(frames) < 4:
        return frames, 0
    a = frames[-1].astype(np.float32)
    b = frames[0].astype(np.float32)
    seam = float(np.abs(a - b).mean())
    if seam <= target:
        return frames, 0
    n = int(min(max_n, max(min_n, round(seam / target))))
    mids = smooth_cut(frames[-1], frames[0], n)
    return np.concatenate([frames, np.stack(mids)]), n


def smooth_all_cuts(frames: np.ndarray, cuts: list[int],
                    n: int = 10) -> tuple[np.ndarray, int]:
    """把所有硬切都摊平。返回 (新帧序列, 插入了多少帧)。"""
    if not cuts:
        return frames, 0
    out, prev = [], 0
    for c in cuts:
        out.extend(frames[prev:c])
        if c - 1 >= 0 and c < len(frames):
            out.extend(smooth_cut(frames[c - 1], frames[c], n))
        prev = c
    out.extend(frames[prev:])
    return np.stack(out), len(out) - len(frames)
