# -*- coding: utf-8 -*-
"""大模型客户端：OpenAI 兼容接口，按用户要求仅支持 DeepSeek 与豆包（火山方舟）。"""
import base64
import os

import requests

# 供应商预设（base_url 均为 OpenAI 兼容地址）
PRESETS = {
    "DeepSeek 极速（Flash）": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-flash",
        "vision_model": "deepseek-v4-flash-vision-exp",
        "key_url": "https://platform.deepseek.com/api_keys",
    },
    "DeepSeek 深度（Pro）": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-pro",
        "vision_model": "deepseek-v4-flash-vision-exp",
        "key_url": "https://platform.deepseek.com/api_keys",
    },
    "豆包（火山方舟）": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-seed-1-6-250615",
        "vision_model": "doubao-seed-1-6-250615",
        "key_url": "https://console.volcengine.com/ark",
    },
}


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, api_cfg: dict, log=print):
        self.cfg = api_cfg or {}
        self.log = log

    # ---------- 内部 ----------
    def _url(self) -> str:
        base = (self.cfg.get("base_url") or "").strip().rstrip("/")
        if not base:
            raise LLMError("未配置 API 地址（base_url）")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def _headers(self) -> dict:
        key = (self.cfg.get("api_key") or "").strip()
        if not key:
            raise LLMError("未配置 API Key")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    @staticmethod
    def _image_part(image_path: str) -> dict:
        ext = os.path.splitext(image_path)[1].lower().lstrip(".") or "png"
        if ext == "jpg":
            ext = "jpeg"
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:image/{ext};base64,{b64}"},
        }

    def _apply_vision(self, messages: list, image_path):
        """若启用视觉模型，把一张或多张图片塞进最后一条 user 消息。"""
        model = self.cfg.get("model")
        if not image_path:
            return messages, model
        if not self.cfg.get("vision_enabled"):
            return messages, model
        vm = (self.cfg.get("vision_model") or "").strip() or model
        if not messages or messages[-1]["role"] != "user":
            return messages, vm
        last = messages[-1]
        paths = image_path if isinstance(image_path, (list, tuple)) else [image_path]
        paths = [p for p in paths if p and os.path.isfile(p)][:4]
        if not paths:
            return messages, model
        parts = [{"type": "text", "text": last.get("content", "") or "请看图回复"}]
        parts.extend(self._image_part(path) for path in paths)
        messages = messages[:-1] + [{"role": "user", "content": parts}]
        return messages, vm

    # ---------- 对外 ----------
    def chat(self, messages: list, image_path=None) -> str:
        url = self._url()
        messages, model = self._apply_vision(list(messages), image_path)
        payload = {
            "model": model,
            "messages": messages,
            "temperature": float(self.cfg.get("temperature", 0.8)),
            "max_tokens": int(self.cfg.get("max_tokens", 600)),
        }
        last_err = None
        for attempt in (1, 2):  # 网络抖动重试一次
            try:
                resp = requests.post(
                    url,
                    json=payload,
                    headers=self._headers(),
                    timeout=int(self.cfg.get("timeout", 60)),
                )
                if resp.status_code == 200:
                    data = resp.json()
                    try:
                        text = data["choices"][0]["message"]["content"]
                    except (KeyError, IndexError, TypeError):
                        raise LLMError(f"接口返回格式异常：{str(data)[:300]}")
                    return (text or "").strip()
                body = resp.text[:300]
                raise LLMError(f"接口返回 HTTP {resp.status_code}：{body}")
            except LLMError:
                raise
            except requests.RequestException as e:
                last_err = e
                if attempt == 1:
                    self.log("warn", f"调用大模型网络异常，正在重试：{e}")
        raise LLMError(f"调用大模型失败：{last_err}")

    def test(self):
        """测试连通性，返回 (ok, 描述)。"""
        try:
            text = self.chat(
                [{"role": "user", "content": "请回复两个字：收到"}],
            )
            return True, f"连接成功，模型回复：{text[:50]}"
        except LLMError as e:
            return False, str(e)
        except Exception as e:
            return False, f"未知错误：{e}"
