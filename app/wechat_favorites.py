# -*- coding: utf-8 -*-
"""微信“自定义表情”适配层。

优先使用 wxauto 未来可能提供的 SendEmotion；当前 wxauto4
没有该公开方法时，通过微信原生表情面板选中收藏表情。
"""
import os
import random
import re
import time


class FavoriteStickerError(RuntimeError):
    pass


class _StickerClicked(Exception):
    pass


def parse_category_mapping(value):
    """解析“开心:0,1,2；无语:3-5”，返回 {str: [int]} 。"""
    if isinstance(value, dict):
        result = {}
        for name, indices in value.items():
            clean = []
            for item in indices if isinstance(indices, (list, tuple)) else []:
                try:
                    number = int(item)
                    if number >= 0 and number not in clean:
                        clean.append(number)
                except (TypeError, ValueError):
                    pass
            if str(name).strip() and clean:
                result[str(name).strip()] = clean
        return result

    result = {}
    for block in re.split(r"[;；\n]+", str(value or "")):
        if not block.strip() or ":" not in block and "：" not in block:
            continue
        name, raw = re.split(r"[:：]", block, maxsplit=1)
        name = name.strip()
        indices = []
        for token in re.split(r"[,，\s]+", raw.strip()):
            if not token:
                continue
            match = re.fullmatch(r"(\d+)\s*[-~～]\s*(\d+)", token)
            if match:
                lo, hi = sorted((int(match.group(1)), int(match.group(2))))
                indices.extend(range(lo, hi + 1))
            elif token.isdigit():
                indices.append(int(token))
        clean = list(dict.fromkeys(i for i in indices if i >= 0))
        if name and clean:
            result[name] = clean
    return result


def format_category_mapping(mapping):
    parts = []
    for name, indices in parse_category_mapping(mapping).items():
        parts.append(f"{name}:" + ",".join(str(i) for i in indices))
    return "；".join(parts)


class WeChatFavoriteStickers:
    def __init__(self, wx, log=None):
        self.wx = wx
        self.log = log or (lambda *_args: None)

    def capability(self):
        if callable(getattr(self.wx, "SendEmotion", None)):
            return "native"
        return "panel" if self._emoji_button() is not None else "unavailable"

    def sync(self, cache_dir=None):
        """扫描自定义表情数量，并可选生成仅本地可见的预览缓存。"""
        if self.capability() == "native":
            return {"mode": "native", "count": 0, "captured": 0}
        panel, sticker_list = self._open_custom_panel()
        captured = 0
        try:
            if cache_dir:
                os.makedirs(cache_dir, exist_ok=True)
                for filename in os.listdir(cache_dir):
                    if re.fullmatch(r"favorite_\d{3}\.png", filename):
                        try:
                            os.remove(os.path.join(cache_dir, filename))
                        except OSError:
                            pass

            def capture(index, control):
                nonlocal captured
                if not cache_dir or not self._is_safely_visible(panel, control):
                    return
                path = os.path.join(cache_dir, f"favorite_{index:03d}.png")
                if os.path.exists(path):
                    return
                try:
                    saved = bool(control.CaptureToImage(path))
                except Exception:
                    saved = False
                if not saved:
                    try:
                        from PIL import ImageGrab
                        rect = control.BoundingRectangle
                        ImageGrab.grab(bbox=(rect.left, rect.top,
                                             rect.right, rect.bottom)).save(path)
                        saved = True
                    except Exception:
                        saved = False
                if saved:
                    captured += 1

            count = self._walk(sticker_list, capture)
            return {"mode": "panel", "count": count, "captured": captured}
        finally:
            self._close_panel()

    def send(self, who, category, mapping=None, last_count=0):
        mapping = parse_category_mapping(mapping)
        indices = list(mapping.get(str(category or "").strip(), ()))
        # 新版 wxauto 如果加入正式接口，无需走面板。
        if callable(getattr(self.wx, "SendEmotion", None)):
            index = random.choice(indices) if indices else 0
            self._send_native(index, who)
            return True

        panel, sticker_list = self._open_custom_panel(who)
        try:
            if indices:
                index = random.choice(indices)
                found = self._seek_and_click(panel, sticker_list, index)
            else:
                found = self._click_random_visible(panel, sticker_list)
            if not found:
                raise FavoriteStickerError("未找到指定的收藏表情")
            time.sleep(0.2)
            return True
        finally:
            self._close_panel()

    def _send_native(self, index, who):
        fn = self.wx.SendEmotion
        try:
            fn(emotion_index=index, who=who)
        except TypeError:
            try:
                fn(index, who=who)
            except TypeError:
                self.wx.ChatWith(who, exact=True)
                fn(index)

    def _emoji_button(self):
        try:
            toolbar = self.wx.ChatBox.tools
            for group in toolbar.GetChildren():
                for control in group.GetChildren():
                    if "发送表情" in str(control.Name or ""):
                        return control
        except Exception:
            return None
        return None

    def _open_custom_panel(self, who=None):
        if who:
            self.wx.ChatWith(who, exact=True)
            time.sleep(0.6)
        self._focus_wechat()
        button = self._emoji_button()
        if button is None:
            raise FavoriteStickerError("当前微信版本未暴露表情按钮")
        button.Click(simulateMove=True, show_window=True, waitTime=0.3)
        panel = self._wait_panel()
        try:
            custom = next(
                c for c in self._flatten(panel)
                if c.ControlTypeName == "TabItemControl" and
                str(c.Name or "").strip() == "自定义表情"
            )
            custom.Click()
            time.sleep(0.25)
            sticker_list = next(
                c for c in self._flatten(panel)
                if c.ControlTypeName == "ListControl"
            )
            sticker_list.SetFocus()
            sticker_list.WheelUp(wheelTimes=99)
            time.sleep(0.25)
            return panel, sticker_list
        except Exception as exc:
            self._close_panel()
            raise FavoriteStickerError("无法打开微信的自定义表情页") from exc

    def _wait_panel(self):
        from wxauto4.uia import uiautomation as auto
        pid = getattr(getattr(self.wx, "_api", None), "pid", None)
        end = time.time() + 4.0
        while time.time() < end:
            for control in self._flatten(auto.GetRootControl()):
                try:
                    if control.AutomationId == "EmoticonPopover" and \
                            (pid is None or control.ProcessId == pid):
                        return control
                except Exception:
                    pass
            time.sleep(0.1)
        raise FavoriteStickerError("表情面板打开超时")

    def _focus_wechat(self):
        """切换会话后确保主窗口可交互，否则微信会忽略弹出按钮。"""
        try:
            import win32con
            import win32gui
            hwnd = self.wx._api.control.NativeWindowHandle
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.35)
        except Exception:
            pass

    @staticmethod
    def _flatten(control):
        result = []
        try:
            for level in control.GetAllProgeny():
                result.extend(level)
        except Exception:
            pass
        return result

    @staticmethod
    def _items(sticker_list):
        result = []
        try:
            for control in sticker_list.GetChildren():
                rect = control.BoundingRectangle
                if rect.right - rect.left >= 40 and rect.bottom - rect.top >= 40:
                    result.append(control)
        except Exception:
            pass
        return sorted(result, key=lambda c: (
            c.BoundingRectangle.top, c.BoundingRectangle.left))

    @staticmethod
    def _key(control):
        try:
            return tuple(control.runtimeid)
        except Exception:
            rect = control.BoundingRectangle
            return (rect.left, rect.top, rect.right, rect.bottom, control.Name)

    def _walk(self, sticker_list, visit=None):
        seen = {}
        stale = 0
        for _ in range(120):
            added = 0
            for control in self._items(sticker_list):
                key = self._key(control)
                if key not in seen:
                    seen[key] = len(seen)
                    added += 1
                if visit:
                    visit(seen[key], control)
            stale = stale + 1 if added == 0 else 0
            if stale >= 4:
                break
            sticker_list.WheelDown(wheelTimes=1)
            time.sleep(0.1)
        return len(seen)

    def _seek_and_click(self, panel, sticker_list, target):
        def visit(index, control):
            if index == target and self._is_safely_visible(panel, control):
                control.Click()
                raise _StickerClicked()

        try:
            self._walk(sticker_list, visit)
        except _StickerClicked:
            return True
        return False

    def _click_random_visible(self, panel, sticker_list):
        items = [c for c in self._items(sticker_list)
                 if self._is_safely_visible(panel, c)]
        if not items:
            return False
        random.choice(items).Click()
        return True

    @staticmethod
    def _is_safely_visible(panel, control):
        try:
            pr = panel.BoundingRectangle
            cr = control.BoundingRectangle
            cx, cy = (cr.left + cr.right) // 2, (cr.top + cr.bottom) // 2
            # 底部约 90px 是分类栏，不把被它遮挡的缩略图当可见项。
            return pr.left < cx < pr.right and pr.top + 35 < cy < pr.bottom - 90
        except Exception:
            return False

    @staticmethod
    def _close_panel():
        try:
            from wxauto4.uia import uiautomation as auto
            if any(getattr(c, "AutomationId", "") == "EmoticonPopover"
                   for level in auto.GetRootControl().GetAllProgeny()
                   for c in level):
                auto.SendKeys("{Esc}")
        except Exception:
            pass
