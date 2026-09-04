# -*- coding: utf-8 -*-
"""微信封装层：基于 wxauto4（免费版）驱动 PC 版微信 4.x。

通过会话列表检测未读消息，绕开免费版只能监听一个对象的限制。
仅在发现未读时打开对应聊天，避免持续操作微信窗口和鼠标。
"""
import importlib.util
import logging
import os
import random
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime

from .config import MAX_MONITORED_CONTACTS, app_dir
from .prompts import describe_message
from .wechat_favorites import WeChatFavoriteStickers

logger = logging.getLogger("wxbot")

HAS_WXAUTO4 = importlib.util.find_spec("wxauto4") is not None


def patch_wxauto4_compat():
    """补齐 wxauto4 41.1.7 安装包遗漏的增量消息读取接口。"""
    from wxauto4.wx import Chat
    from wxauto4.ui import chatbox as chatbox_module

    if hasattr(Chat, "GetNewMessage"):
        return False

    def get_new_message(chat):
        box = chat._api._chat_api
        if not box.msgbox.Exists(0):
            return []

        controls = box.msgbox.GetChildren()
        now_ids = tuple(ctrl.runtimeid for ctrl in controls)
        if not now_ids:
            return []

        used_ids = tuple(box.used_msg_ids or ())
        current_count = len(now_ids)
        last_count = chatbox_module.LAST_MSG_COUNT.get(box.id, 0)

        # 第一次读取只建立基线，不把历史记录当新消息。
        if not used_ids:
            chatbox_module.USED_MSG_IDS[box.id] = now_ids[-100:]
            chatbox_module.LAST_MSG_COUNT[box.id] = current_count
            return []

        if current_count > last_count:
            candidate_ids = now_ids[-(current_count - last_count):]
        else:
            used_set = set(used_ids)
            candidate_ids = tuple(mid for mid in now_ids if mid not in used_set)

        chatbox_module.USED_MSG_IDS[box.id] = now_ids[-100:]
        chatbox_module.LAST_MSG_COUNT[box.id] = current_count
        if not candidate_ids:
            return []

        candidate_set = set(candidate_ids)
        return [
            chatbox_module.parse_msg(ctrl, box)
            for ctrl in controls
            if ctrl.runtimeid in candidate_set and
            ctrl.ControlTypeName == "ListItemControl"
        ]

    Chat.GetNewMessage = get_new_message
    return True


def ensure_wechat_window_visible():
    """若微信主窗口被收起到托盘，尝试恢复显示。返回 hwnd 或 None。"""
    try:
        import win32con
        import win32gui
    except ImportError:
        return None
    target = None

    def _cb(hwnd, _):
        nonlocal target
        try:
            if win32gui.GetWindowText(hwnd) == "微信" and \
                    "Qt" in win32gui.GetClassName(hwnd):
                l, t, r, b = win32gui.GetWindowRect(hwnd)
                if (r - l) * (b - t) > 100000:  # 过滤弹窗小窗
                    target = hwnd
        except Exception:
            pass

    win32gui.EnumWindows(_cb, None)
    if target and not win32gui.IsWindowVisible(target):
        try:
            win32gui.ShowWindow(target, win32con.SW_RESTORE)
            time.sleep(0.8)
        except Exception:
            pass
    return target


class WeChatBot:
    def __init__(self, config, ui_queue):
        self.config = config
        self.ui_queue = ui_queue
        self.wx = None
        self.favorite_stickers = None
        self.my_name = ""
        self.chats = {}          # {聊天名: None}，作为需要监控的名称集合
        self.engine = None
        self._stop_evt = threading.Event()
        self._stop_evt.set()     # start() 前不检查会话
        self._thread = None
        self._buf_lock = threading.Lock()
        self._buffers = {}       # {聊天名: {"items": [(msg, ts)], "last": ts}}
        self._chat_locks = {}
        self._seen = {}          # {聊天名: {"ids": set, "hashes": set}}
        self._chat_fail = {}     # {聊天名: 连续读取失败次数}
        self._session_claims = {}  # 记录会话摘要，发现新内容时触发处理
        self._connect_claims = {}  # 连接后、启动监听前的变化也不能漏掉
        self._session_retry_at = {}  # 读取失败后短暂退避，但不丢失消息
        self._wx_lock = threading.Lock()  # 序列化对微信窗口的 UI 操作
        self._save_dir = os.path.join(app_dir(), "logs", "media")

    # ---------- 事件 ----------
    def emit(self, kind, **kw):
        kw["kind"] = kind
        try:
            self.ui_queue.put_nowait(kw)
        except Exception:
            pass

    def log(self, level, text):
        text = str(text)
        getattr(logger, level if level in ("info", "warning", "error") else "info",
                logger.info)(text)
        self.emit("log", level=level, text=text)

    # ---------- 连接 ----------
    def connect(self):
        if not HAS_WXAUTO4:
            raise RuntimeError(
                "未安装 wxauto4 库。请先运行「安装依赖.bat」，"
                "或手动执行：py -3.13 -m pip install wxauto4"
            )
        from wxauto4 import WeChat
        from wxauto4.param import WxParam

        WxParam.TELEMETRY_ENABLED = False
        ensure_wechat_window_visible()
        time.sleep(0.5)
        with self._wx_lock:
            self.log("info", "正在连接微信客户端…")
            self.wx = WeChat(ads=False, resize=bool(
                self.config.get("window", "resize", default=True)))
            self.favorite_stickers = WeChatFavoriteStickers(self.wx, self.log)
            # 当前免费版只允许一个 AddListenChat；改由会话列表统一监控。
            try:
                self.wx.StopListening()
            except Exception:
                pass
            try:
                me = self.wx.GetMyInfo() or {}
                self.my_name = me.get("display_name") or \
                    me.get("nickname") or "我"
            except Exception:
                self.my_name = "我"
        self.log("info", f"已连接微信，当前账号昵称：{self.my_name}")
        sessions = self.get_sessions()
        self._connect_claims = {
            item["name"]: self._session_signature(item) for item in sessions
        }
        return sessions

    def get_sessions(self):
        """读取当前会话列表（用于界面选择聊天对象）。"""
        with self._wx_lock:
            sessions = self.wx.GetSession() or []
        result = []
        for s in sessions:
            try:
                info = s.info if hasattr(s, "info") else s
                if isinstance(info, dict):
                    result.append({
                        "name": info.get("name", ""),
                        "time": info.get("time", ""),
                        "content": info.get("content", ""),
                        "new_count": info.get("new_count", 0),
                        "isnew": bool(info.get("isnew")),
                        "ismute": bool(info.get("ismute")),
                    })
                else:
                    name = getattr(s, "name", "") or str(s)
                    result.append({"name": name, "time": "", "content": "",
                                   "new_count": 0, "ismute": False})
            except Exception:
                continue
        result = [r for r in result if r["name"]]
        self.emit("sessions", list=result)
        return result

    # ---------- 监听对象 ----------
    def _find_subwindow(self, name):
        """在所有独立聊天窗口里找指定聊天。"""
        try:
            for w in self.wx.GetAllSubWindow() or []:
                try:
                    info = w.ChatInfo() or {}
                    if info.get("chat_name") == name:
                        return w
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def add_listen(self, name):
        """登记监控对象；实际监听由会话列表完成，不受单对象限制。"""
        name = str(name or "").strip()
        if not name:
            return False
        if name not in self.chats and len(self.chats) >= MAX_MONITORED_CONTACTS:
            self.log("warning", f"监控对象最多 {MAX_MONITORED_CONTACTS} 个，已跳过：{name}")
            return False
        self.chats[name] = None
        self._seen.setdefault(name, {"ids": set(), "hashes": set()})
        self._chat_fail[name] = 0
        self.log("info", f"已登记监控：{name}")
        return True

    def remove_listen(self, name):
        chat = self.chats.pop(name, None)
        self._seen.pop(name, None)
        self._chat_fail.pop(name, None)
        self._session_claims.pop(name, None)
        self._session_retry_at.pop(name, None)
        self.log("info", f"已停止监听：{name}")

    # ---------- 启停 ----------
    def attach_engine(self, engine):
        self.engine = engine

    def start(self):
        if not self.wx:
            raise RuntimeError("尚未连接微信")
        if not self.chats:
            raise RuntimeError("请先添加要监听的聊天对象")
        self._session_claims.clear()
        self._stop_evt.clear()
        # 启动前的未读可能很多。这里只记录当前摘要，不打开任何聊天窗口；
        # 否则会逐个 ChatWith，持续抢占微信主界面，导致用户无法正常操作。
        self._prime_session_baseline()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="wxbot-loop")
        self._thread.start()
        self.emit("monitor", running=True)
        self.log("info", f"自动回复已开始运行 ✔（监听 {len(self.chats)} 个对象）")

    def stop(self):
        self._stop_evt.set()
        self.emit("monitor", running=False)
        self.log("info", "自动回复已停止")

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive() \
            and not self._stop_evt.is_set()

    # ---------- 主循环：检查会话未读；仅有新消息时才切换窗口 ----------
    def _loop(self):
        interval = 1.0
        try:
            interval = max(0.5, float(
                self.config.get("reply", "poll_interval", default=1.0)))
        except Exception:
            pass
        while not self._stop_evt.is_set():
            try:
                self._poll_sessions()
                self._flush_due()
            except Exception as e:
                self.log("warning", f"会话检查异常：{e}")
            self._stop_evt.wait(interval)
        self.log("info", "监听循环已退出")

    @staticmethod
    def _session_info(session):
        info = session.info if hasattr(session, "info") else session
        return info if isinstance(info, dict) else {}

    @staticmethod
    def _session_signature(info):
        """未读状态和数量也属于变化，避免同一分钟相同内容被漏掉。"""
        try:
            count = max(0, int(info.get("new_count") or 0))
        except Exception:
            count = 0
        unread = bool(info.get("isnew")) or count > 0
        return info.get("time"), info.get("content"), unread, count

    def _prime_session_baseline(self):
        """记录启动时的会话状态，不读取或打开历史未读会话。"""
        with self._wx_lock:
            sessions = self.wx.GetSession() or []
        monitored = set(self.chats)
        for session in sessions:
            info = self._session_info(session)
            name = str(info.get("name") or "").strip()
            if name in monitored:
                current = self._session_signature(info)
                connected = self._connect_claims.get(name)
                # 连接后、点击开始前收到的消息属于本次运行，不能当历史跳过。
                self._session_claims[name] = (
                    connected if connected is not None and connected != current
                    else current
                )

    def _poll_sessions(self):
        with self._wx_lock:
            sessions = self.wx.GetSession() or []
        monitored = set(self.chats)
        for session in sessions:
            info = self._session_info(session)
            name = str(info.get("name") or "").strip()
            if name not in monitored:
                continue
            try:
                count = max(0, int(info.get("new_count") or 0))
            except Exception:
                count = 0
            signature = self._session_signature(info)
            previous = self._session_claims.get(name)
            if previous is None:
                # 运行中新增的监控对象同样先建基线，避免误回历史未读。
                self._session_claims[name] = signature
                continue
            if previous == signature:
                continue
            if time.monotonic() < self._session_retry_at.get(name, 0):
                continue
            try:
                self._consume_unread(name, max(1, count))
                # 读取成功后才能确认摘要；失败则保留旧值，下一轮自动重试。
                self._session_claims[name] = signature
                self._chat_fail[name] = 0
                self._session_retry_at.pop(name, None)
            except Exception as e:
                failures = self._chat_fail.get(name, 0) + 1
                self._chat_fail[name] = failures
                retry_delay = min(5.0, float(failures))
                self._session_retry_at[name] = time.monotonic() + retry_delay
                self.log("warning", f"读取未读消息失败（{name}）：{e}；"
                                     f"{retry_delay:.0f} 秒后重试")

    def _consume_unread(self, name, count):
        """打开有未读的会话，并只路由本次新增的对方消息。"""
        ensure_wechat_window_visible()
        with self._wx_lock:
            switched = self.wx.ChatWith(name, exact=True)
            if not switched:
                raise RuntimeError("无法打开聊天窗口")
            messages = self.wx.GetAllMessage() or []

        state = self._seen.setdefault(name, {"ids": set(), "hashes": set()})
        eligible = [m for m in messages
                    if (getattr(m, "attr", "") or "") in ("friend", "")]
        fresh = [m for m in eligible
                 if getattr(m, "id", None) and
                 getattr(m, "id", None) not in state["ids"]]
        if not state["ids"]:
            fresh = eligible[-max(1, count):]
        elif fresh:
            fresh = fresh[-max(1, count):]

        for m in messages:
            mid = getattr(m, "id", None)
            if mid:
                state["ids"].add(mid)
        if len(state["ids"]) > 1200:
            state["ids"] = set(list(state["ids"])[-500:])

        for message in fresh:
            self._route(name, message)

    def _reconnect(self):
        from wxauto4 import WeChat
        from wxauto4.param import WxParam
        WxParam.TELEMETRY_ENABLED = False
        ensure_wechat_window_visible()
        time.sleep(1)
        with self._wx_lock:
            self.wx = WeChat(ads=False, resize=bool(
                self.config.get("window", "resize", default=True)))
            try:
                self.wx.StopListening()
            except Exception:
                pass
        old = list(self.chats.keys())
        self.chats.clear()
        self._seen.clear()
        for name in old:
            self.add_listen(name)
        self.log("info", "重连成功，监听已恢复")

    # ---------- 消息路由 ----------
    @staticmethod
    def _message_chat_info(m):
        """兼容 chat_info 为字典（41.1.7）或可调用方法的版本。"""
        value = getattr(m, "chat_info", {})
        if callable(value):
            value = value()
        return value if isinstance(value, dict) else {}

    def _message_policy(self, chat_type, mtype):
        """遵守消息类型开关；文字和图片默认自动，视频默认手动。"""
        policy = self.config.get("policy", mtype, default=None)
        if policy is None:
            policy = "auto" if mtype in ("text", "image") else "manual"
        return policy

    @staticmethod
    def _is_mentioned(content, nickname):
        """判断群消息是否 @ 当前账号，兼容昵称旁的特殊空白。"""
        if not content or not nickname:
            return False
        compact = re.sub(r"[\s\u2005\u2009\u202f]+", "", str(content))
        return f"@{nickname}" in compact

    def _route(self, chat_name, m):
        mtype = getattr(m, "type", "") or "other"
        attr = getattr(m, "attr", "") or ""
        content = getattr(m, "content", "") or ""

        # 自己发的消息：写入记忆（本人在微信里手动回复的内容也让大模型知道）
        if attr == "self":
            if mtype in ("text", "quote") and content.strip():
                if self.engine:
                    self.engine.record(chat_name, "assistant", content.strip())
            return
        if attr not in ("friend", ""):
            return

        # 先识别私聊/群聊，再决定是否直接交给大模型。
        try:
            info = self._message_chat_info(m)
        except Exception:
            info = {}
        chat_type = info.get("chat_type", "friend")
        sender = getattr(m, "sender", "") or ""

        policy = self._message_policy(chat_type, mtype)
        if policy != "auto":
            self._to_manual(chat_name, mtype, content, "该类型设置为人工回复")
            return

        if chat_type == "group" and \
                self.config.get("reply", "group_only_at", default=True):
            if not self._is_mentioned(content, self.my_name):
                return

        with self._buf_lock:
            buf = self._buffers.setdefault(chat_name, {"items": [], "last": 0})
            buf["items"].append((m, time.time()))
            buf["last"] = time.time()
        sender_log = f"（{sender}）" if chat_type == "group" and sender else ""
        self.log("recv", f"收到 {chat_name}{sender_log}："
                          f"{self._preview(mtype, content)}")

    def _flush_due(self):
        quiet = 1.0
        try:
            quiet = float(self.config.get("reply", "quiet_seconds", default=1))
        except Exception:
            pass
        now = time.time()
        due = []
        with self._buf_lock:
            for name, buf in list(self._buffers.items()):
                if buf["items"] and now - buf["last"] >= quiet:
                    items = buf["items"]
                    buf["items"] = []
                    due.append((name, items))
        for name, items in due:
            t = threading.Thread(target=self._process, args=(name, items),
                                 daemon=True, name=f"reply-{name}")
            t.start()

    def _to_manual(self, name, mtype, content, reason):
        preview = self._preview(mtype, content)
        self.log("warn", f"[需人工回复] {name}：{preview}（{reason}）")
        self.emit("manual", time=datetime.now().strftime("%H:%M:%S"),
                  name=name, mtype=mtype, preview=preview, reason=reason)

    @staticmethod
    def _preview(mtype, content):
        content = (content or "").strip().replace("\n", " ")
        tag = {"video": "[视频]", "image": "[图片]", "voice": "[语音]",
               "file": "[文件]", "emotion": "[表情包]", "link": "[链接]",
               "quote": "[引用]"}.get(mtype, f"[{mtype}]")
        if mtype in ("text", "quote", "link"):
            return content[:60] if content else tag
        return f"{tag} {content[:40]}".strip()

    # ---------- 生成并发送 ----------
    def _process(self, name, items):
        lock = self._chat_locks.setdefault(name, threading.Lock())
        with lock:
            try:
                self._process_locked(name, items)
            except Exception as e:
                self.log("error", f"自动回复 {name} 失败：{e}")

    def _process_locked(self, name, items):
        engine = self.engine
        if engine is None:
            self.log("error", "回复引擎未初始化，忽略消息")
            return

        auto_items = []
        for m, ts in items:
            mtype = getattr(m, "type", "") or "other"
            info = self._message_chat_info(m)
            policy = self._message_policy(
                info.get("chat_type", "friend"), mtype)
            if policy == "auto":
                auto_items.append(m)
            else:
                self._to_manual(name, mtype, getattr(m, "content", ""),
                                "该类型设置为人工回复")
        if not auto_items:
            return

        # ---- 组装给大模型看的消息描述 ----
        vision_cfg = self.config.get("api", default={}) or {}
        image_paths = []
        lines = []
        multi = len(auto_items) > 1
        for idx, m in enumerate(auto_items, 1):
            mtype = getattr(m, "type", "") or "other"
            content = getattr(m, "content", "") or ""
            prefix = f"{idx}. " if multi else ""
            info = self._message_chat_info(m)
            sender = getattr(m, "sender", "") or ""
            if info.get("chat_type") == "group" and sender:
                prefix += f"{sender}："
            if mtype in ("text", "quote") and info.get("chat_type") != "group":
                engine.learn_contact_style(name, content)
            if mtype == "voice":
                text = self._voice_text(m)
                lines.append(prefix + describe_message("voice", text))
                continue
            if mtype == "quote":
                qc = getattr(m, "quote_content", "") or ""
                qn = getattr(m, "quote_nickname", "") or ""
                if qc:
                    content = f"{content}（引用 {qn}：{qc}）"
            if mtype == "image" and vision_cfg.get("vision_enabled"):
                p = self._download_image(m)
                if p:
                    image_paths.append(p)
                    lines.append(prefix + "【图片】（已附原图，请分析画面内容后自然回复）")
                    continue
            if mtype == "emotion":
                emotion_text = self._emotion_text(content)
                if emotion_text and info.get("chat_type") != "group":
                    engine.learn_contact_style(name, emotion_text)
                if vision_cfg.get("vision_enabled") and \
                        self.config.get("stickers", "ocr_enabled", default=True):
                    p = self._download_image(m)
                    if p:
                        image_paths.append(p)
                        hint = (f"，消息自带文字：{emotion_text}" if emotion_text else "")
                        lines.append(
                            prefix + "【表情包】（已附原图：先识别图中文字；有文字就按文字消息理解，"
                            f"无文字再按普通图片和情绪处理{hint}）")
                        continue
                lines.append(prefix + describe_message("emotion", emotion_text))
                continue
            if mtype == "video" and vision_cfg.get("vision_enabled"):
                p = self._download_video_cover(m)
                if p:
                    image_paths.append(p)
                    lines.append(prefix + "【视频】（已附视频封面，只能根据封面有限推断）")
                    continue
            lines.append(prefix + describe_message(mtype, content))
        incoming = "\n".join(lines)
        if multi:
            incoming = f"对方连发了 {len(auto_items)} 条消息：\n" + incoming

        # ---- 调大模型 ----
        result = None
        last_error = None
        for attempt in range(1, 4):
            try:
                candidate = engine.generate(name, incoming, image_path=image_paths)
                if not candidate.needs_human and not candidate.reply.strip() and \
                        not candidate.sticker:
                    raise RuntimeError("模型返回了空回复")
                result = candidate
                break
            except Exception as e:
                last_error = e
                if attempt < 3 and not self._stop_evt.is_set():
                    self.log("warning", f"生成回复失败（{name}），"
                                         f"正在进行第 {attempt + 1} 次尝试：{e}")
                    self._stop_evt.wait(float(attempt))
        if result is None:
            reason = f"连续 3 次生成失败：{last_error}"
            self.log("error", f"自动回复失败（{name}）：{reason}")
            self._to_manual(name, "text", incoming, reason)
            return
        if result.analysis:
            self.log("info", f"意图分析（{name}）：{result.analysis}")
        # 成功理解后的对方消息进入长期上下文；重试期间只记录一次。
        engine.record(name, "user", incoming)

        # ---- 需要人工 ----
        if result.needs_human:
            if self.config.get("reply", "complex_ack", default=True) and result.reply:
                self._human_delay()
                self._send_text(name, result.reply)
                engine.record(name, "assistant", result.reply)
            self._to_manual(name, "text", "（大模型判断需本人处理）" +
                            (result.reason or ""), "大模型判断需本人处理")
            return

        # ---- 拟人化延迟后发送 ----
        self._human_delay()
        sent_parts = []
        for part in self._split_reply(result.reply):
            if part:
                if self._send_text(name, part):
                    sent_parts.append(part)
                time.sleep(random.uniform(1.0, 2.5))

        # ---- 表情包 ----
        sticker_sent = None
        if result.sticker and \
                self.config.get("stickers", "enabled", default=True):
            favorite_cfg = self.config.get("wechat_favorites", default={}) or {}
            favorite_categories = favorite_cfg.get("categories") or {}
            use_favorite = favorite_cfg.get("enabled", True) and (
                result.sticker == "微信收藏" or result.sticker in favorite_categories)
            if use_favorite and self._send_favorite_sticker(name, result.sticker):
                sticker_sent = result.sticker
            else:
                path = engine.stickers.pick(result.sticker)
                if path and self._send_file(name, path):
                    sticker_sent = result.sticker
                elif not sent_parts:
                    self.log("warn", f"没有可用的表情包（{name}）")

        if sent_parts or sticker_sent:
            recorded = "\n".join(sent_parts)
            if sticker_sent and not recorded:
                recorded = "（发了一张表情包）"
            engine.record(name, "assistant", recorded)
            self.emit("sent", name=name,
                      text="\n".join(sent_parts) +
                           (f" ＋表情包[{sticker_sent}]" if sticker_sent else ""))
            self.log("sent", f"已回复 {name}：{self._preview('text', recorded)}")
        elif result.reply or result.sticker:
            self._to_manual(name, "text", incoming, "微信发送连续失败")

    # ---------- 发送细节 ----------
    def _voice_text(self, m):
        try:
            t = m.to_text()
            if t and str(t).strip():
                return str(t).strip()
        except Exception:
            pass
        return ""

    def _download_image(self, m):
        try:
            os.makedirs(self._save_dir, exist_ok=True)
            res = m.download(dir_path=self._save_dir)
            p = self._downloaded_path(m, res)
            if p:
                return p
        except Exception as e:
            self.log("warn", f"下载图片失败：{e}")
        return None

    @staticmethod
    def _emotion_text(content):
        """提取消息对象已提供的表情包文字，过滤占位符/XML/路径。"""
        value = str(content or "").strip()
        if not value:
            return ""
        compact = value.lower().replace(" ", "")
        placeholders = {
            "[动画表情]", "[表情]", "[表情包]", "动画表情", "表情", "表情包",
            "[emotion]", "[emoji]",
        }
        if compact in placeholders or compact.startswith("<?xml") or \
                compact.startswith("<msg") or re.match(r"^[a-z]:[\\/]", value, re.I):
            return ""
        # 至少包含一个可读文字或数字，避免把哈希、链接等元数据当聊天内容。
        if not re.search(r"[\u3400-\u9fffA-Za-z0-9]", value) or \
                re.match(r"^(https?://|[0-9a-f]{24,})", compact):
            return ""
        return value[:120]

    @staticmethod
    def _downloaded_path(message, result):
        if isinstance(result, str) and os.path.exists(result):
            return result
        if isinstance(result, dict):
            data = result.get("data") or {}
            p = data.get("path") or data.get("file") or ""
            if p and os.path.exists(p):
                return p
        p = getattr(message, "path", "") or ""
        return p if p and os.path.exists(p) else None

    def _download_video_cover(self, m):
        """下载视频并用 FFmpeg 提取靠前的一帧作为推理封面。"""
        try:
            os.makedirs(self._save_dir, exist_ok=True)
            res = m.download(dir_path=self._save_dir, original=False, timeout=15)
            video_path = self._downloaded_path(m, res)
            if not video_path:
                self.log("warn", "视频下载失败，改为仅根据消息类型回复")
                return None
            if os.path.splitext(video_path)[1].lower() in (".jpg", ".jpeg", ".png", ".webp"):
                return video_path
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                self.log("warn", "未找到 FFmpeg，无法提取视频封面")
                return None
            cover_path = os.path.join(
                self._save_dir, f"video_cover_{time.time_ns()}.jpg")
            subprocess.run(
                [ffmpeg, "-y", "-ss", "0.2", "-i", video_path,
                 "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2",
                 cover_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=20, check=True)
            return cover_path if os.path.exists(cover_path) else None
        except Exception as e:
            self.log("warn", f"提取视频封面失败：{e}")
            return None

    def _human_delay_bounds(self):
        try:
            lo = float(self.config.get("reply", "delay_min", default=1))
            hi = float(self.config.get("reply", "delay_max", default=2))
        except Exception:
            lo, hi = 1, 2
        lo, hi = sorted((max(0.5, lo), max(0.5, hi)))
        if self.config.get("reply", "reply_mode", default="一般") == "严谨":
            # 严谨模式留出更长的检查时间，其他模式保持快速响应。
            lo, hi = max(lo, 3.0), max(hi, 5.0)
        return lo, hi

    def _human_delay(self):
        lo, hi = self._human_delay_bounds()
        delay = random.uniform(lo, hi)
        self.log("info", f"等待 {delay:.1f} 秒后回复（模拟真人）")
        end = time.time() + delay
        while time.time() < end and not self._stop_evt.is_set():
            time.sleep(min(0.5, max(0.05, end - time.time())))

    def _split_reply(self, text):
        """长回复按标点拆成两条发送，更像真人。"""
        text = (text or "").strip()
        if not text:
            return []
        split_enabled = self.config.get("reply", "split_long", default=True)
        try:
            threshold = int(self.config.get("reply", "split_threshold", default=80))
        except Exception:
            threshold = 80
        if not split_enabled or len(text) <= threshold:
            return [text]
        mid = len(text) // 2
        ends = [m.end() for m in re.finditer(r"[。！？!?~～\n；;，,]", text)]
        if ends:
            cut = min(ends, key=lambda p: abs(p - mid))
            cut = min(max(cut, 4), len(text) - 2)
        else:
            cut = mid
        return [text[:cut].strip(), text[cut:].strip()]

    def _send_text(self, name, text):
        last_error = None
        for attempt in range(1, 4):
            try:
                with self._wx_lock:
                    chat = self.chats.get(name)
                    if chat is not None:
                        chat.SendMsg(text)
                    else:
                        self.wx.SendMsg(text, who=name, exact=True)
                return True
            except Exception as e:
                last_error = e
                if attempt < 3 and not self._stop_evt.is_set():
                    self.log("warning", f"发送文字失败（{name}），"
                                         f"正在进行第 {attempt + 1} 次尝试：{e}")
                    self._stop_evt.wait(float(attempt))
        self.log("error", f"发送文字连续失败（{name}）：{last_error}")
        return False

    def _send_file(self, name, path):
        with self._wx_lock:
            chat = self.chats.get(name)
            try:
                if chat is not None:
                    chat.SendFiles(path)
                else:
                    self.wx.SendFiles(path, who=name, exact=True)
                return True
            except Exception:
                try:
                    self.wx.SendFiles(path, who=name, exact=True)
                    return True
                except Exception as e:
                    self.log("error", f"发送表情包失败（{name}）：{e}")
                    return False

    def _send_favorite_sticker(self, name, category):
        if not self.favorite_stickers:
            return False
        favorite_cfg = self.config.get("wechat_favorites", default={}) or {}
        try:
            with self._wx_lock:
                return self.favorite_stickers.send(
                    name, category,
                    favorite_cfg.get("categories") or {},
                    favorite_cfg.get("last_count", 0),
                )
        except Exception as e:
            self.log("warning", f"微信收藏表情发送失败，将改用本地表情：{e}")
            return False

    def sync_favorite_stickers(self):
        if not self.favorite_stickers:
            raise RuntimeError("请先连接微信")
        cache_dir = os.path.join(app_dir(), "data", "wechat_favorites")
        with self._wx_lock:
            report = self.favorite_stickers.sync(cache_dir)
        count = int(report.get("count") or 0)
        self.config.set(count, "wechat_favorites", "last_count")
        self.config.save()
        return report

    # ---------- 供手动处理 ----------
    def open_chat(self, name):
        """打开聊天窗口，方便本人手动回复。"""
        with self._wx_lock:
            try:
                self.wx.ChatWith(name, exact=True)
                self.log("info", f"已在微信中打开聊天窗口：{name}")
                return True
            except Exception as e:
                self.log("error", f"打开聊天窗口失败（{name}）：{e}")
                return False
