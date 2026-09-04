# -*- coding: utf-8 -*-
"""回复引擎：意图分析 + 生成回复 + 每个联系人的对话记忆。"""
import json
import os
import re
import threading
from collections import deque

from .config import app_dir
from .llm import LLMClient, LLMError
from .stickers import StickerLibrary
from .style_learner import ContactStyleLearner


class ReplyResult:
    def __init__(self, analysis="", reply="", sticker=None,
                 needs_human=False, reason="", raw=""):
        self.analysis = analysis
        self.reply = reply
        self.sticker = sticker
        self.needs_human = needs_human
        self.reason = reason
        self.raw = raw


class ReplyEngine:
    def __init__(self, config, log=print):
        self.config = config
        self.log = log
        self._data_dir = os.path.dirname(getattr(config, "path", "")) or app_dir()
        self.llm = LLMClient(config.get("api", default={}), log=log)
        sticker_dir = config.get("stickers", "dir", default="assets/stickers")
        if not os.path.isabs(sticker_dir):
            sticker_dir = os.path.join(app_dir(), sticker_dir)
        self.stickers = StickerLibrary(sticker_dir)
        self.stickers.ensure_dirs()
        self._hist_lock = threading.Lock()
        self.histories = {}  # {联系人: deque([{"role","content"}])}
        learning = config.get("style_learning", default={}) or {}
        self.style_learner = ContactStyleLearner(
            os.path.join(self._data_dir, "style_profiles.json"),
            enabled=learning.get("enabled", True),
            max_samples=learning.get("max_samples", 80),
            log=log,
        )
        self._load_history()

    # ---------- 历史 ----------
    @property
    def history_path(self):
        return os.path.join(self._data_dir, "history.json")

    def _load_history(self):
        try:
            if os.path.exists(self.history_path):
                with open(self.history_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for name, msgs in data.items():
                        d = deque(msgs, maxlen=self._limit())
                        self.histories[name] = d
        except Exception as e:
            self.log("warn", f"读取历史对话失败（已忽略）：{e}")

    def _limit(self) -> int:
        try:
            return max(4, int(self.config.get("reply", "history_limit", default=16)))
        except Exception:
            return 16

    def save_history(self):
        with self._hist_lock:
            try:
                data = {name: list(msgs) for name, msgs in self.histories.items()}
                with open(self.history_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
            except Exception as e:
                self.log("warn", f"保存历史对话失败：{e}")

    def record(self, name: str, role: str, text: str):
        if not text:
            return
        with self._hist_lock:
            d = self.histories.setdefault(
                name, deque(maxlen=self._limit())
            )
            d.append({"role": role, "content": text})
        self.save_history()

    def clear_history(self, name: str = None):
        with self._hist_lock:
            if name:
                self.histories.pop(name, None)
            else:
                self.histories.clear()
        self.save_history()
        self.style_learner.clear(name)

    def learn_contact_style(self, name: str, text: str):
        """观察对方原始文本；只落盘统计特征。"""
        self.style_learner.enabled = bool(
            self.config.get("style_learning", "enabled", default=True))
        self.style_learner.observe(name, text)

    # ---------- 生成 ----------
    def build_messages(self, name: str, incoming: str, image_path: str = None) -> list:
        cats = list(self.stickers.categories().keys()) if \
            self.config.get("stickers", "enabled", default=True) else []
        favorite_cfg = self.config.get("wechat_favorites", default={}) or {}
        if self.config.get("stickers", "enabled", default=True) and \
                favorite_cfg.get("enabled", True):
            favorite_cats = list((favorite_cfg.get("categories") or {}).keys())
            cats.extend(favorite_cats or ["微信收藏"])
        cats = list(dict.fromkeys(cats))
        learned_style = self.style_learner.summary(
            name,
            self.config.get("style_learning", "min_samples", default=3),
        )
        sys_prompt = build_prompt_cached(self.config, name, cats, learned_style)
        messages = [{"role": "system", "content": sys_prompt}]
        with self._hist_lock:
            messages.extend(list(self.histories.get(name, ())))
        incoming = incoming or "（对方没有说话）"
        if image_path:
            incoming = (incoming + "\n（已附上相关图片或视频封面）").strip()
        messages.append({"role": "user", "content": incoming})
        return messages

    def generate(self, name: str, incoming: str, image_path: str = None) -> ReplyResult:
        messages = self.build_messages(name, incoming, image_path)
        raw = self.llm.chat(messages, image_path=image_path)
        parsed = self.parse_output(raw)
        parsed["reply"] = self._sanitize_reply(parsed.get("reply", ""))
        if not self.config.get("reply", "tone_particles", default=True):
            parsed["reply"] = self._strip_tone_particles(parsed["reply"])
        if not self.config.get("stickers", "enabled", default=True):
            parsed["sticker"] = None
            parsed["reply"] = self._strip_expression_codes(parsed["reply"])
        return ReplyResult(raw=raw, **parsed)

    @staticmethod
    def _sanitize_reply(text: str) -> str:
        """禁用敬语；不足 8 个有效字符的短回复不使用标点。"""
        text = str(text or "").strip()
        text = text.replace("您们", "你们").replace("您", "你")
        text = text.replace("贵公司", "你们公司").replace("贵校", "你们学校")
        text = text.replace("贵单位", "你们单位").replace("贵方", "你们")
        text = text.replace("阁下", "你").replace("先生", "").replace("女士", "")
        units = re.findall(r"[\u3400-\u9fffA-Za-z0-9]", text)
        if len(units) < 8:
            text = re.sub(r"[，。！？!?、；;：:,.~～…—]+", "", text)
            text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _strip_expression_codes(text: str) -> str:
        codes = (
            "微笑|捂脸|憨笑|破涕为笑|偷笑|呲牙|强|抱拳|握手|愉快|"
            "爱心|让我看看"
        )
        return re.sub(rf"\[(?:{codes})\]", "", text or "").strip()

    @staticmethod
    def _strip_tone_particles(text: str) -> str:
        """只清理句末语气助词，避免误删词语内部的同形字。"""
        original = str(text or "").strip()
        cleaned = re.sub(
            r"[啊呀呢吧嘛吗呗哇啦咯哟哦噢欸诶]+(?=\s*[，。！？!?、；;：:,.~～…—]|\s*$)",
            "", original,
        )
        cleaned = re.sub(r"\s+([，。！？!?、；;：:,.~～…—])", r"\1", cleaned)
        cleaned = cleaned.strip()
        return cleaned or original

    # ---------- 解析 ----------
    @staticmethod
    def parse_output(raw: str) -> dict:
        """把大模型输出解析成 dict；解析失败时把全文当回复（兜底）。"""
        raw = (raw or "").strip()
        text = raw
        # 去掉 markdown 代码块
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
        if m:
            text = m.group(1)
        data = None
        try:
            data = json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                try:
                    data = json.loads(m.group(0))
                except Exception:
                    data = None
        if isinstance(data, dict) and "reply" in data:
            reply = str(data.get("reply") or "").strip()
            # 去掉包在外面的引号
            if len(reply) >= 2 and reply[0] == reply[-1] and reply[0] in "\"'":
                reply = reply[1:-1].strip()
            sticker = data.get("sticker")
            if isinstance(sticker, str):
                sticker = sticker.strip() or None
            elif sticker is not True:
                sticker = None
            return {
                "analysis": str(data.get("analysis") or "").strip(),
                "reply": reply,
                "sticker": sticker,
                "needs_human": bool(data.get("needs_human")),
                "reason": str(data.get("reason") or "").strip(),
            }
        # 兜底：把整段文本当作回复
        fallback = raw.strip()
        if fallback.startswith("{") and fallback.endswith("}"):
            fallback = ""  # 明明想输出 JSON 却失败了，宁可不发
        return {"analysis": "", "reply": fallback, "sticker": None,
                "needs_human": False, "reason": ""}


def build_prompt_cached(config, name, cats, learned_style=""):
    # 每次 monkey-patch 深拷贝 relationship 可能变化，直接构建即可（很轻量）
    from .prompts import build_system_prompt
    return build_system_prompt(config, name, cats, learned_style)
