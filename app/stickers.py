# -*- coding: utf-8 -*-
"""表情包库：assets/stickers/<分类>/xxx.jpg|png|gif，大模型选分类，随机抽一张发送。"""
import os
import random

IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

# 首次运行时自动创建的默认分类
DEFAULT_CATEGORIES = ["打招呼", "开心", "卖萌", "认同", "拒绝", "委屈", "摸鱼"]


class StickerLibrary:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir

    # ---------- 目录 ----------
    def ensure_dirs(self):
        os.makedirs(self.base_dir, exist_ok=True)
        readme = os.path.join(self.base_dir, "使用说明.txt")
        if not os.path.exists(readme):
            with open(readme, "w", encoding="utf-8") as f:
                f.write(
                    "把表情包图片按分类放进子文件夹里（jpg / png / gif 均可）。\n"
                    "例如：开心/1.jpg、摸鱼/摸鱼2.gif\n"
                    "大模型回复时会自动选择合适的分类，并从对应文件夹随机抽一张发送。\n"
                    "文件夹名就是「分类名」，可以在回复设置里关掉表情包功能。\n"
                )
        for cat in DEFAULT_CATEGORIES:
            d = os.path.join(self.base_dir, cat)
            if not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "把表情包放这里.txt"), "w", encoding="utf-8") as f:
                    f.write(f"把「{cat}」主题的表情包图片放到这个文件夹。")

    # ---------- 查询 ----------
    def categories(self) -> dict:
        """返回 {分类名: [图片路径, ...]}，只保留非空分类。"""
        result = {}
        if not os.path.isdir(self.base_dir):
            return result
        for name in sorted(os.listdir(self.base_dir)):
            d = os.path.join(self.base_dir, name)
            if not os.path.isdir(d):
                continue
            files = [
                os.path.join(d, f)
                for f in os.listdir(d)
                if os.path.splitext(f)[1].lower() in IMG_EXTS
            ]
            if files:
                result[name] = files
        return result

    def pick(self, category: str):
        """按分类抽一张表情包；分类不存在时先模糊匹配，再退化为随机。"""
        cats = self.categories()
        if not cats:
            return None
        category = (category or "").strip()
        if category in cats:
            return random.choice(cats[category])
        # 模糊匹配：分类名互相包含
        for name, files in cats.items():
            if category and (category in name or name in category):
                return random.choice(files)
        # 兜底：随便来一张
        all_files = [f for files in cats.values() for f in files]
        return random.choice(all_files)
