# -*- coding: utf-8 -*-
"""轻友 - 启动入口。"""
import logging
import os
import sys
import time


def check_env():
    if sys.version_info < (3, 9):
        print("需要 Python 3.9 及以上版本（wxauto4 需要 3.9~3.13，推荐 3.13）")
        input("按回车退出…")
        sys.exit(1)
    try:
        import requests  # noqa: F401
    except ImportError:
        print("缺少 requests 库，请执行：pip install requests")
    try:
        import importlib.util
        if importlib.util.find_spec("wxauto4") is None:
            print("[提示] 未安装 wxauto4，将无法连接微信。"
                  "请运行项目目录里的「安装依赖.bat」，或：py -3.13 -m pip install wxauto4")
    except Exception:
        pass


def setup_logging():
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    handler = logging.FileHandler(
        os.path.join(log_dir, time.strftime("bot_%Y%m%d.log")),
        encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # 屏蔽第三方库的 DEBUG 刷屏
    logging.getLogger("wxauto4").setLevel(logging.WARNING)


check_env()
setup_logging()

from app.config import Config  # noqa: E402
from app.gui import App  # noqa: E402

if __name__ == "__main__":
    App(Config()).mainloop()
