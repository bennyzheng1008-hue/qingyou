# -*- coding: utf-8 -*-
"""微信集成自检：连接 → 监听「文件传输助手」→ 发一条自检消息 → 验证回调捕获。

只对「文件传输助手」（你自己的备忘通道）操作，不会给任何真人发消息。
运行前请确认：微信已登录且主窗口已打开。执行：py -3.13 tests\\selftest_wechat.py
"""
import os
import queue
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from app.config import Config
from app.bot import WeChatBot

cfg = Config()
bot = WeChatBot(cfg, queue.Queue())
bot.log = lambda lvl, txt: print(f"[{lvl}] {txt}")

routed = []
_orig_route = bot._route


def spy_route(name, m):
    routed.append((getattr(m, "attr", "?"), getattr(m, "type", "?"),
                   repr(getattr(m, "content", ""))[:50]))
    print("route:", name, routed[-1])
    return _orig_route(name, m)


bot._route = spy_route

sessions = bot.connect()
ok_add = bot.add_listen("文件传输助手")
bot.start()
print("== 已启动监听 ==")

time.sleep(5)  # 等待 2 轮预热基线
ok_send = bot._send_text("文件传输助手",
                         "【自检】微信自动回复助手自检消息（发给自己的测试，可忽略）")
print("send:", ok_send)
time.sleep(8)
bot.stop()

echo_ok = any(r[0] == "self" and "自检" in r[2] for r in routed)
print("\n==== 自检结果 ====")
print("连接:", "通过" if sessions else "失败")
print("监听添加:", "通过" if ok_add else "失败")
print("发送:", "通过" if ok_send else "失败")
print("回调捕获新消息:", "通过" if echo_ok else "失败")
sys.exit(0 if (sessions and ok_add and ok_send and echo_ok) else 1)
