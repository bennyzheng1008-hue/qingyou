# -*- coding: utf-8 -*-
"""冒烟测试：不需要微信和大模型 API，验证本地逻辑。运行：python tests/smoke_test.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL += 1
        print(f"  ✘ {name}  {detail}")


def test_config():
    from app.config import Config, MAX_MONITORED_CONTACTS
    from app.llm import PRESETS
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(os.path.join(d, "config.json"))
        cfg.set("k1", "api", "model")
        cfg.upsert_contact("张三", "朋友")
        cfg.upsert_contact("张三", "老板")  # 更新关系
        cfg.save()
        cfg2 = Config(os.path.join(d, "config.json"))
        check("配置保存/加载", cfg2.get("api", "model") == "k1")
        check("联系人去重更新", cfg2.relationship_of("张三") == "老板")
        for i in range(MAX_MONITORED_CONTACTS - 1):
            cfg2.upsert_contact(f"对象{i}")
        check("监控对象上限为五个",
              not cfg2.upsert_contact("第六个") and
              len(cfg2.contacts()) == MAX_MONITORED_CONTACTS)
        check("默认策略-视频手动", cfg2.get("policy", "video") == "manual")
        check("默认策略-图片自动", cfg2.get("policy", "image") == "auto")
        check("DeepSeek极速模型",
              PRESETS["DeepSeek 极速（Flash）"]["model"] == "deepseek-v4-flash")
        check("DeepSeek深度模型",
              PRESETS["DeepSeek 深度（Pro）"]["model"] == "deepseek-v4-pro")
        check("默认群聊仅被@回复",
              cfg2.get("reply", "group_only_at") is True)
        check("默认回复状态为一般",
              cfg2.get("reply", "reply_mode") == "一般")
        cfg2.remove_contact("张三")
        check("移除联系人",
              all(c["name"] != "张三" for c in cfg2.contacts()))


def test_prompts():
    from app.config import Config
    from app.prompts import build_system_prompt, describe_message
    cfg = Config(os.path.join(tempfile.gettempdir(), "no_such_dir_cfg.json"))
    cfg.data["contacts"] = [{"name": "小李", "relationship": "女朋友"}]
    p = build_system_prompt(cfg, "小李", ["开心", "卖萌"])
    check("提示词包含关系", "女朋友" in p)
    check("提示词包含表情分类", "开心" in p and "卖萌" in p)
    check("提示词包含JSON协议", '"needs_human"' in p)
    check("提示词包含处境判断", "回复前先在心里完成一次处境判断" in p)
    check("提示词包含语气模仿", "语气模仿机制" in p and "客服腔" in p)
    check("提示词包含恶意引导防护", "恶意引导防护" in p and "API Key" in p)
    check("提示词禁止敬语", "禁止用“您、贵方、阁下、先生、女士”" in p)
    check("提示词包含回复长度", "【回复长度】" in p)
    check("提示词包含一般状态", "【当前回复状态：一般】" in p)
    check("默认允许语气助词", "【语气助词】已开启" in p)
    mode_markers = {
        "敷衍": "2~10 个字",
        "热情": "自然追问一句",
        "严谨": "优先核对事实",
    }
    for mode, marker in mode_markers.items():
        cfg.data["reply"]["reply_mode"] = mode
        mode_prompt = build_system_prompt(cfg, "小李", [])
        check(f"{mode}状态规则生效",
              f"【当前回复状态：{mode}】" in mode_prompt and marker in mode_prompt)
    check("恶意引导触发防御规则",
          "立即进入防御模式" in p and "轻度脏话" in p and
          "不能威胁伤害" in p and "普通分歧" in p)
    check("图片要求分析具体内容", "人物、文字、物品、场景和情绪" in p)
    check("图片允许自然反问", "可以顺势反问一句" in p)
    check("视频封面限制推理", "不要假装看过完整视频" in p)
    p2 = build_system_prompt(cfg, "小李", [])
    check("无表情库时不含分类", "可选表情包分类" not in p2)
    cfg.data["stickers"]["enabled"] = False
    cfg.data["reply"]["tone_particles"] = False
    p3 = build_system_prompt(cfg, "小李", ["开心"])
    check("关闭表情后明确禁止携带", "表情包功能已关闭" in p3 and "sticker 必须填 null" in p3)
    check("关闭语气助词后明确禁止", "【语气助词】已关闭" in p3)
    check("消息描述-视频", "【视频】" in describe_message("video", ""))
    check("消息描述-文本", describe_message("text", "你好") == "你好")
    p4 = build_system_prompt(cfg, "测试对象", [], "已观察 8 条；偏短句；常见语气词：呀")
    check("渐进语气画像进入提示词", "渐进语气画像" in p4 and "偏短句" in p4)
    check("表情包先识字后看图", "先做文字识别" in p4 and "没有文字" in p4)


def test_style_learning():
    from app.style_learner import ContactStyleLearner
    d = tempfile.mkdtemp()
    learner = ContactStyleLearner(os.path.join(d, "styles.json"), log=lambda *a: None)
    learner.observe("朋友", "好呀")
    learner.observe("朋友", "行呀")
    learner.observe("朋友", "哈哈可以呀")
    summary = learner.summary("朋友", 3)
    check("语气学习渐进生效", "已观察 3 条" in summary and "语气词" in summary)
    with open(os.path.join(d, "styles.json"), "r", encoding="utf-8") as f:
        saved = f.read()
    check("语气画像不保存原文", "哈哈可以呀" not in saved and "total_chars" in saved)


def test_vision_batch():
    from app.llm import LLMClient
    d = tempfile.mkdtemp()
    paths = []
    for i in range(2):
        path = os.path.join(d, f"{i}.png")
        with open(path, "wb") as f:
            f.write(b"image")
        paths.append(path)
    client = LLMClient({"model": "text", "vision_enabled": True,
                        "vision_model": "vision"})
    messages, model = client._apply_vision(
        [{"role": "user", "content": "看图"}], paths)
    check("多图视觉输入", model == "vision" and len(messages[-1]["content"]) == 3)


def test_parse():
    from app.reply_engine import ReplyEngine
    p = ReplyEngine.parse_output
    r = p('{"analysis":"想约你","reply":"周末有空呀[憨笑]","sticker":"开心","needs_human":false,"reason":""}')
    check("标准JSON", r["reply"] == "周末有空呀[憨笑]" and r["sticker"] == "开心")
    r = p('```json\n{"analysis":"a","reply":"好的","sticker":null,"needs_human":true,"reason":"借钱"}\n```')
    check("代码块JSON+转人工", r["needs_human"] and r["reason"] == "借钱")
    r = p('好的，我想想：{"reply":"晚点回你","analysis":"x","sticker":null,"needs_human":false,"reason":""} 嗯')
    check("前后杂文字JSON", r["reply"] == "晚点回你")
    r = p("直接回复不按格式来")
    check("非JSON兜底", r["reply"] == "直接回复不按格式来")
    r = p('{"analysis":"a","reply":"\\"引号测试\\"","sticker":null,"needs_human":false,"reason":""}')
    check("回复去外引号", r["reply"] == "引号测试")
    r = p("")
    check("空输出", r["reply"] == "")


def test_engine_mock():
    from app.config import Config
    from app.reply_engine import ReplyEngine
    d = tempfile.mkdtemp()
    cfg = Config(os.path.join(d, "c.json"))
    cfg.data["stickers"]["enabled"] = False
    cfg.data["contacts"] = [{"name": "测试对象", "relationship": "朋友"}]

    engine = ReplyEngine(cfg, log=lambda *a: None)
    engine.llm = type("MockLLM", (), {
        "chat": staticmethod(lambda messages, image_path=None:
                             json.dumps({"analysis": "测试", "reply": "收到啦",
                                         "sticker": "开心", "needs_human": False,
                                         "reason": ""}, ensure_ascii=False))
    })()
    res = engine.generate("测试对象", "你好")
    check("模拟生成回复", res.reply == "收到啦")
    check("关闭表情强制清除模型选择", res.sticker is None)
    cfg.data["reply"]["tone_particles"] = False
    res_without_particles = engine.generate("测试对象", "你好")
    check("关闭语气助词后自动清理", res_without_particles.reply == "收到")
    check("只清理句末不误伤词语内部",
          engine._strip_tone_particles("你呢边处理好吧，明天说") == "你呢边处理好，明天说")
    check("短回复自动去标点",
          engine._sanitize_reply("您好！") == "你好")
    check("回复自动去敬语",
          engine._sanitize_reply("贵方明天下午方便沟通吗？") == "你们明天下午方便沟通吗？")
    check("称呼敬语强制清除",
          engine._sanitize_reply("王先生，您好！") == "王你好")
    check("关闭表情清除文字表情代码",
          engine._strip_expression_codes("行吧[捂脸]") == "行吧")
    check("长回复保留标点",
          engine._sanitize_reply("今天下午三点以后，我应该都有时间。") .endswith("。"))
    engine.record("测试对象", "user", "你好")
    engine.record("测试对象", "assistant", "收到啦")
    msgs = engine.build_messages("测试对象", "在吗")
    check("历史进入上下文", any(m.get("content") == "收到啦" for m in msgs))
    check("新消息进入上下文", msgs[-1]["content"] == "在吗")
    engine2 = ReplyEngine(cfg, log=lambda *a: None)
    check("历史持久化重载", engine2.histories.get("测试对象") is not None)
    engine.clear_history()
    engine2.clear_history()


def test_split():
    from app.config import Config
    from app.bot import WeChatBot
    cfg = Config(os.path.join(tempfile.gettempdir(), "split_cfg.json"))
    cfg.data["reply"]["split_long"] = True
    cfg.data["reply"]["split_threshold"] = 20
    bot = WeChatBot(cfg, ui_queue=None)
    parts = bot._split_reply("短消息")
    check("短消息不拆", parts == ["短消息"])
    long = "今天天气很不错呢，我们下午三点在公园门口集合吧！记得带上水壶，别迟到啦。"
    parts = bot._split_reply(long)
    check("长消息拆分", len(parts) == 2 and "".join(parts) == long, str(parts))
    cfg.data["reply"]["split_long"] = False
    check("关闭拆分", bot._split_reply(long) == [long])
    cfg.data["reply"]["reply_mode"] = "一般"
    check("一般状态保持快速等待", bot._human_delay_bounds() == (1.0, 2.0))
    cfg.data["reply"]["reply_mode"] = "严谨"
    check("严谨状态延长思考等待", bot._human_delay_bounds() == (3.0, 5.0))


def test_video_cover():
    from app.bot import WeChatBot
    from app.config import Config

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        check("视频封面-FFmpeg可用", False, "未找到 ffmpeg")
        return
    with tempfile.TemporaryDirectory() as d:
        video = os.path.join(d, "sample.mp4")
        subprocess.run(
            [ffmpeg, "-y", "-f", "lavfi", "-i",
             "color=c=blue:s=320x240:d=1", "-pix_fmt", "yuv420p", video],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=True, timeout=20)
        msg = type("Video", (), {
            "download": lambda self, **kwargs: video,
            "path": video,
        })()
        cfg = Config(os.path.join(d, "config.json"))
        bot = WeChatBot(cfg, ui_queue=None)
        bot._save_dir = d
        cover = bot._download_video_cover(msg)
        check("视频自动提取封面",
              bool(cover and os.path.exists(cover) and cover.endswith(".jpg")))


def test_message_compat():
    from app.bot import WeChatBot
    from app.config import Config

    msg_dict = type("Msg", (), {
        "chat_info": {"chat_type": "group", "chat_name": "测试群"}
    })()
    msg_func = type("Msg", (), {
        "chat_info": staticmethod(
            lambda: {"chat_type": "friend", "chat_name": "小李"})
    })()
    check("消息聊天信息-字典接口",
          WeChatBot._message_chat_info(msg_dict)["chat_type"] == "group")
    check("消息聊天信息-函数接口",
          WeChatBot._message_chat_info(msg_func)["chat_type"] == "friend")
    check("群聊@窄空格兼容", WeChatBot._is_mentioned("@\u2005。 你好", "。"))
    check("群聊未@不触发", not WeChatBot._is_mentioned("大家好", "。"))
    cfg = Config(os.path.join(tempfile.gettempdir(), "policy_cfg.json"))
    cfg.data["policy"]["video"] = "manual"
    bot = WeChatBot(cfg, ui_queue=None)
    check("视频手动开关生效",
          bot._message_policy("friend", "video") == "manual")
    check("图片默认进入大模型",
          bot._message_policy("friend", "image") == "auto")
    cfg.data["policy"]["video"] = "auto"
    check("视频可切换为自动回复",
          bot._message_policy("group", "video") == "auto")
    check("表情包占位符不当文字", WeChatBot._emotion_text("[动画表情]") == "")
    check("表情包自带文字可识别",
          WeChatBot._emotion_text("今天不想上班") == "今天不想上班")

    gate_bot = WeChatBot(cfg, ui_queue=None)
    gate_bot.my_name = "。"
    group_info = {"chat_type": "group", "chat_name": "测试群"}
    plain = type("Msg", (), {
        "type": "text", "attr": "friend", "content": "大家好",
        "sender": "小李", "chat_info": group_info,
    })()
    mentioned = type("Msg", (), {
        "type": "text", "attr": "friend", "content": "@\u2005。 在吗",
        "sender": "小李", "chat_info": group_info,
    })()
    gate_bot._route("测试群", plain)
    check("群聊未@不会入回复队列", not gate_bot._buffers)
    gate_bot._route("测试群", mentioned)
    check("群聊被@进入回复队列", bool(gate_bot._buffers))

    session_info = {
        "name": "测试对象", "time": "12:00", "content": "你好",
        "isnew": True, "new_count": 1,
    }
    bot.chats = {"测试对象": None}
    bot.wx = type("FakeWx", (), {
        "GetSession": staticmethod(lambda: [session_info])
    })()
    consumed = []
    bot._consume_unread = lambda name, count: consumed.append((name, count))
    bot._prime_session_baseline()
    bot._poll_sessions()
    check("启动基线不打开历史未读会话", consumed == [])
    session_info["content"] = "第二条"
    session_info["isnew"] = True
    session_info["new_count"] = 1
    bot._poll_sessions()
    check("启动后的新消息正常触发", consumed == [("测试对象", 1)])
    bot._poll_sessions()
    check("相同会话摘要不会重复抢占窗口", consumed == [("测试对象", 1)])

    # 内容和时间不变时，未读数变化也必须触发；读取失败不能提前确认消息。
    session_info.update({"time": "12:01", "content": "重复内容",
                         "isnew": False, "new_count": 0})
    bot._prime_session_baseline()
    session_info.update({"isnew": True, "new_count": 2})
    attempts = []
    def flaky_consume(name, count):
        attempts.append((name, count))
        if len(attempts) == 1:
            raise RuntimeError("临时读取失败")
    bot._consume_unread = flaky_consume
    old_claim = bot._session_claims["测试对象"]
    bot._poll_sessions()
    check("读取失败不会误标为已处理",
          bot._session_claims["测试对象"] == old_claim)
    bot._session_retry_at["测试对象"] = 0
    bot._poll_sessions()
    check("读取失败后会自动重试", len(attempts) == 2 and
          bot._session_claims["测试对象"] != old_claim)

    session_info.update({"time": "12:02", "content": "连接时内容",
                         "isnew": False, "new_count": 0})
    bot._connect_claims["测试对象"] = bot._session_signature(session_info)
    session_info.update({"time": "12:03", "content": "启动前新消息",
                         "isnew": True, "new_count": 1})
    consumed.clear()
    bot._consume_unread = lambda name, count: consumed.append((name, count))
    bot._prime_session_baseline()
    bot._poll_sessions()
    check("连接后启动前的消息不会漏回",
          consumed == [("测试对象", 1)])

    send_attempts = []
    def flaky_send(*_args, **_kwargs):
        send_attempts.append(1)
        if len(send_attempts) < 3:
            raise RuntimeError("发送控件暂时不可用")
    bot.wx = type("FakeSendWx", (), {"SendMsg": staticmethod(flaky_send)})()
    bot.chats = {"测试对象": None}
    check("微信发送失败会自动重试",
          bot._send_text("测试对象", "你好") and len(send_attempts) == 3)


def test_stickers():
    from app.stickers import StickerLibrary
    d = tempfile.mkdtemp()
    lib = StickerLibrary(d)
    lib.ensure_dirs()
    check("自动建分类目录", os.path.isdir(os.path.join(d, "开心")))
    cat = os.path.join(d, "开心")
    with open(os.path.join(cat, "a.png"), "wb") as f:
        f.write(b"x")
    check("识别分类", "开心" in lib.categories())
    check("按分类取图", lib.pick("开心").endswith("a.png"))
    check("模糊匹配", lib.pick("开心点") is not None)
    check("空库随机兜底", lib.pick("不存在") is not None)


def test_gui():
    try:
        from app.config import Config
        from app.gui import App
        d = tempfile.mkdtemp()
        app = App(Config(os.path.join(d, "c.json")))
        app.update()
        app._apply_settings()
        app._on_close()
        check("GUI 启动/关闭", True)
    except Exception as e:
        check("GUI 启动/关闭", False, repr(e))


if __name__ == "__main__":
    print("== 配置 ==")
    test_config()
    print("== 提示词 ==")
    test_prompts()
    test_style_learning()
    test_vision_batch()
    print("== 输出解析 ==")
    test_parse()
    print("== 回复引擎(模拟LLM) ==")
    test_engine_mock()
    print("== 拆分与表情包 ==")
    test_split()
    test_video_cover()
    test_message_compat()
    test_stickers()
    print("== 界面 ==")
    test_gui()
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    sys.exit(1 if FAIL else 0)
