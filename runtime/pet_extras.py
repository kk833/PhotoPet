"""第一档体验增强 (v0.6)。

- set_click_through: Windows 下用 WS_EX_TRANSPARENT 扩展样式实现
  精确的鼠标穿透开关 (参考社区共识: 比 Qt 的 WindowTransparentForInput
  可靠, 且不重建窗口、不闪烁)。非 Windows 平台返回 False 由调用方降级。
- 动作映射: ai_action_map 保存于 pet.json, AI 回复命中关键词时
  触发对应动作 (参考 DesktopFriends 的 AI 驱动表情设计)。
"""
import sys

DEFAULT_ACTION_MAP = {
    "开心|高兴|哈哈|喜欢": "click",
    "睡觉|晚安|困": "sleep",
    "你好|嗨|早上好": "idle",
}


def set_click_through(hwnd: int, enabled: bool) -> bool:
    """开关鼠标穿透。仅 Windows 有效。"""
    if sys.platform != "win32" or not hwnd:
        return False
    import ctypes
    from ctypes import wintypes
    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.SetWindowLongW.restype = ctypes.c_long
    handle = wintypes.HWND(hwnd)
    style = user32.GetWindowLongW(handle, GWL_EXSTYLE)
    if enabled:
        style |= WS_EX_TRANSPARENT | WS_EX_LAYERED
    else:
        style &= ~WS_EX_TRANSPARENT
    user32.SetWindowLongW(handle, GWL_EXSTYLE, style)
    return True


def match_action(text: str, action_map: dict | None) -> str | None:
    """AI 回复文本 → 动作名。键为 | 分隔的关键词。"""
    import re
    for pattern, action in (action_map or DEFAULT_ACTION_MAP).items():
        for kw in pattern.split("|"):
            if kw and kw in text:
                return action
    return None
