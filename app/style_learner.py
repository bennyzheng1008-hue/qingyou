# -*- coding: utf-8 -*-
"""联系人语气画像：只保存统计特征，不额外保存聊天原文。"""
import json
import os
import re
import threading


_PARTICLES = ("啊", "呀", "呢", "吧", "嘛", "呗", "啦", "咯", "哦", "诶")
_EXPRESSIONS = ("哈哈", "笑死", "好滴", "好哒", "行", "嗯嗯", "绝了", "真的假的", "救命", "晚安")


class ContactStyleLearner:
    """用缓慢累积的统计量描述联系人语气，避免对单条消息过拟合。"""

    def __init__(self, path: str, enabled=True, max_samples=80, log=print):
        self.path = path
        self.enabled = bool(enabled)
        self.max_samples = max(12, int(max_samples or 80))
        self.log = log
        self._lock = threading.RLock()
        self.data = {}
        self._load()

    @staticmethod
    def _blank():
        return {
            "samples": 0,
            "total_chars": 0,
            "short": 0,
            "questions": 0,
            "exclaims": 0,
            "no_punctuation": 0,
            "line_breaks": 0,
            "particles": {},
            "expressions": {},
        }

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    value = json.load(f)
                if isinstance(value, dict):
                    self.data = value
        except Exception as exc:
            self.log("warn", f"读取语气画像失败（已忽略）：{exc}")

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as exc:
            self.log("warn", f"保存语气画像失败：{exc}")

    @staticmethod
    def _clean(text: str) -> str:
        value = re.sub(r"\s+", " ", str(text or "")).strip()
        if not value or value.startswith("【") or len(value) > 500:
            return ""
        return value

    def observe(self, name: str, text: str):
        value = self._clean(text)
        if not self.enabled or not name or not value:
            return
        chars = len(re.findall(r"[\u3400-\u9fffA-Za-z0-9]", value))
        if chars == 0:
            return
        with self._lock:
            p = self.data.setdefault(name, self._blank())
            # 到达上限后用衰减保持画像能跟上近期语气变化。
            if int(p.get("samples", 0)) >= self.max_samples:
                for key in ("samples", "total_chars", "short", "questions",
                            "exclaims", "no_punctuation", "line_breaks"):
                    p[key] = int(float(p.get(key, 0)) * 0.75)
                for bucket in ("particles", "expressions"):
                    p[bucket] = {k: int(v * 0.75) for k, v in p.get(bucket, {}).items()
                                 if int(v * 0.75) > 0}
            p["samples"] = int(p.get("samples", 0)) + 1
            p["total_chars"] = int(p.get("total_chars", 0)) + chars
            p["short"] = int(p.get("short", 0)) + int(chars <= 12)
            p["questions"] = int(p.get("questions", 0)) + int(bool(re.search(r"[？?]", value)))
            p["exclaims"] = int(p.get("exclaims", 0)) + int(bool(re.search(r"[！!]", value)))
            p["no_punctuation"] = int(p.get("no_punctuation", 0)) + int(
                not bool(re.search(r"[，。！？!?、；;：:,.~～…]", value)))
            p["line_breaks"] = int(p.get("line_breaks", 0)) + int("\n" in str(text or ""))
            for token in _PARTICLES:
                if token in value:
                    p.setdefault("particles", {})[token] = int(
                        p.get("particles", {}).get(token, 0)) + value.count(token)
            for token in _EXPRESSIONS:
                if token.lower() in value.lower():
                    p.setdefault("expressions", {})[token] = int(
                        p.get("expressions", {}).get(token, 0)) + value.lower().count(token.lower())
            self._save()

    def summary(self, name: str, min_samples=3) -> str:
        if not self.enabled:
            return ""
        with self._lock:
            p = dict(self.data.get(name) or {})
        n = int(p.get("samples", 0))
        if n < max(1, int(min_samples or 3)):
            return ""
        avg = max(1, round(int(p.get("total_chars", 0)) / n))
        confidence = "初步适应" if n < 8 else ("逐渐稳定" if n < 20 else "较稳定")
        traits = [f"已观察 {n} 条（{confidence}）", f"平均约 {avg} 字"]
        if int(p.get("short", 0)) / n >= 0.6:
            traits.append("偏短句")
        if int(p.get("no_punctuation", 0)) / n >= 0.6:
            traits.append("常省略句末标点")
        if int(p.get("questions", 0)) / n >= 0.35:
            traits.append("较常用问句")
        if int(p.get("exclaims", 0)) / n >= 0.3:
            traits.append("感叹语气较明显")
        particles = sorted(p.get("particles", {}).items(), key=lambda x: x[1], reverse=True)[:3]
        expressions = sorted(p.get("expressions", {}).items(), key=lambda x: x[1], reverse=True)[:3]
        if particles:
            traits.append("常见语气词：" + "、".join(k for k, _ in particles))
        if expressions:
            traits.append("常见表达：" + "、".join(k for k, _ in expressions))
        return "；".join(traits)

    def clear(self, name: str = None):
        with self._lock:
            if name:
                self.data.pop(name, None)
            else:
                self.data.clear()
            self._save()
