# -*- coding: utf-8 -*-
"""配置管理：加载 / 保存 config.json，带默认值深合并。"""
import copy
import json
import os
import sys
import threading


MAX_MONITORED_CONTACTS = 5


def app_dir() -> str:
    """程序所在目录（兼容打包后的 exe）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


DEFAULTS = {
    "api": {
        "provider": "DeepSeek 极速（Flash）",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-v4-flash",
        "temperature": 0.8,
        "max_tokens": 600,
        "timeout": 60,
        "vision_enabled": True,
        "vision_model": "deepseek-v4-flash-vision-exp",
    },
    "persona": {
        "user_name": "",
        "style": "口语化、自然、简短，带点幽默感",
        "extra": "",
    },
    "reply": {
        "delay_min": 1,
        "delay_max": 2,
        "quiet_seconds": 1,
        "history_limit": 16,
        "split_long": True,
        "split_threshold": 80,
        "group_only_at": True,
        "complex_ack": True,
        "poll_interval": 1.0,
        "reply_length": "跟随对方",
        "tone_particles": True,
        "reply_mode": "一般",
    },
    "style_learning": {
        "enabled": True,
        "min_samples": 3,
        "max_samples": 80,
    },
    "stickers": {
        "enabled": True,
        "dir": "assets/stickers",
        "ocr_enabled": True,
    },
    # 各类消息的处理策略：auto=大模型自动回复，manual=进入手动回复队列
    "policy": {
        "text": "auto",
        "emotion": "auto",
        "quote": "auto",
        "link": "auto",
        "voice": "auto",
        "image": "auto",
        "video": "manual",
        "file": "manual",
        "other": "manual",
    },
    # 监控的聊天对象 [{"name": str, "relationship": str}]
    "contacts": [],
    "window": {
        "resize": True,  # wxauto4 需要拉大微信窗口才能可靠读取消息
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


class Config:
    def __init__(self, path: str = None):
        self.dir = app_dir()
        self.path = path or os.path.join(self.dir, "config.json")
        self._lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    # ---------- 读写 ----------
    def load(self):
        with self._lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        stored = json.load(f)
                    _deep_merge(self.data, stored or {})
                    contacts = self.data.get("contacts", [])
                    if isinstance(contacts, list):
                        # 旧配置可能超过限制；运行时只保留前五个，避免高频轮询。
                        self.data["contacts"] = contacts[:MAX_MONITORED_CONTACTS]
                except Exception:
                    # 配置损坏时保留默认值，避免启动崩溃
                    pass

    def save(self):
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    # ---------- 快捷访问 ----------
    def get(self, *keys, default=None):
        node = self.data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def set(self, value, *keys):
        node = self.data
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value

    # ---------- 联系人 ----------
    def contacts(self) -> list:
        contacts = self.data.get("contacts", [])
        return contacts[:MAX_MONITORED_CONTACTS] if isinstance(contacts, list) else []

    def relationship_of(self, name: str) -> str:
        for c in self.contacts():
            if c.get("name") == name:
                return c.get("relationship", "") or "朋友"
        return "朋友"

    def upsert_contact(self, name: str, relationship: str = ""):
        with self._lock:
            for c in self.data["contacts"]:
                if c.get("name") == name:
                    if relationship:
                        c["relationship"] = relationship
                    return True
            if len(self.data["contacts"]) >= MAX_MONITORED_CONTACTS:
                return False
            self.data["contacts"].append(
                {"name": name, "relationship": relationship or ""}
            )
            return True

    def remove_contact(self, name: str):
        with self._lock:
            self.data["contacts"] = [
                c for c in self.data.get("contacts", []) if c.get("name") != name
            ]
