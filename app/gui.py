# -*- coding: utf-8 -*-
"""图形界面：tkinter 实现，四个页签 + 实时日志 + 手动回复队列。"""
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .bot import HAS_WXAUTO4, WeChatBot
from .config import MAX_MONITORED_CONTACTS, app_dir
from .llm import PRESETS
from .reply_engine import ReplyEngine
from .wechat_favorites import format_category_mapping, parse_category_mapping

FONT = ("Microsoft YaHei UI", 10)
FONT_S = ("Microsoft YaHei UI", 9)
FONT_B = ("Microsoft YaHei UI", 10, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 19, "bold")

BG = "#F7F5EE"
SURFACE = "#EEF3EB"
INK = "#24332B"
MUTED = "#708077"
ACCENT = "#557A64"
ACCENT_HOVER = "#466B56"
SOFT = "#DDE9DF"
DANGER = "#9C655D"


class RoundedButton(tk.Canvas):
    """无额外依赖的圆角按钮，兼容常用的 ttk.Button 调用方式。"""

    def __init__(self, master, text, command=None, state="normal",
                 variant="soft", width=None, height=36, **kwargs):
        self.text = text
        self.command = command
        self.state = state
        self.variant = variant
        self._hover = False
        lines = str(text).splitlines() or [""]
        guessed = max(len(line) for line in lines) * 14 + 34
        self._button_width = width or max(96, guessed)
        self._button_height = max(height, 30 + (len(lines) - 1) * 18)
        super().__init__(master, width=self._button_width, height=self._button_height,
                         bg=self._master_bg(master), highlightthickness=0,
                         bd=0, cursor="hand2", **kwargs)
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._invoke)
        self.bind("<Configure>", lambda _e: self._draw())
        self._draw()

    @staticmethod
    def _master_bg(master):
        try:
            return master.cget("background")
        except Exception:
            return BG

    def _palette(self):
        palettes = {
            "primary": (ACCENT, ACCENT_HOVER, "#FFFFFF"),
            "danger": ("#EADBD7", "#DFC9C3", "#6E3D37"),
            "soft": (SOFT, "#D1E0D4", INK),
            "quiet": ("#ECEAE2", "#E1DED3", INK),
        }
        return palettes.get(self.variant, palettes["soft"])

    def _draw(self):
        self.delete("all")
        w = max(2, self.winfo_width())
        h = max(2, self.winfo_height())
        normal, hover, fg = self._palette()
        fill = "#DADDD8" if self.state == "disabled" else (hover if self._hover else normal)
        fg = "#9AA29D" if self.state == "disabled" else fg
        radius = min(18, h // 2)
        self.create_rectangle(radius, 1, w-radius, h-1, fill=fill, outline="")
        self.create_rectangle(1, radius, w-1, h-radius, fill=fill, outline="")
        self.create_oval(1, 1, radius * 2, radius * 2, fill=fill, outline="")
        self.create_oval(w-radius * 2, 1, w-1, radius * 2, fill=fill, outline="")
        self.create_oval(1, h-radius * 2, radius * 2, h-1, fill=fill, outline="")
        self.create_oval(w-radius * 2, h-radius * 2, w-1, h-1, fill=fill, outline="")
        self.create_text(w // 2, h // 2, text=self.text, fill=fg, font=FONT_B,
                         justify="center")

    def _set_hover(self, value):
        self._hover = value and self.state != "disabled"
        self._draw()

    def _invoke(self, _event=None):
        if self.state != "disabled" and self.command:
            self.command()

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, dict):
            kwargs.update(cnf)
        if "state" in kwargs:
            self.state = kwargs.pop("state")
        if "text" in kwargs:
            self.text = kwargs.pop("text")
        if "command" in kwargs:
            self.command = kwargs.pop("command")
        if kwargs:
            super().configure(**kwargs)
        self._draw()

    config = configure

POLICY_TYPES = [
    ("text", "文字"), ("voice", "语音"), ("emotion", "表情包"),
    ("quote", "引用"), ("link", "链接"), ("image", "图片"),
    ("video", "视频"), ("file", "文件"),
]


class App(tk.Tk):
    def __init__(self, config):
        super().__init__()
        self.config_obj = config
        self.bot = None
        self.engine = None
        self.monitoring = False
        self.ui_queue = queue.Queue()
        self._sessions = []
        self._manual_items = {}
        self._manual_seq = 0
        self._flash_on = False
        self._flash_job = None
        self._visible_sessions = []
        self._mon_names = []

        self.title("轻友")
        self._set_app_icon()
        self.geometry("1040x820")
        self.minsize(940, 720)
        self.configure(background=BG)
        self._init_style()

        container = ttk.Frame(self, padding=(18, 14))
        container.pack(fill="both", expand=True)

        # ---------- 顶栏 ----------
        header = tk.Frame(container, background=SURFACE, padx=22, pady=17)
        header.pack(fill="x", pady=(0, 14))
        header.grid_columnconfigure(0, weight=1)
        brand = tk.Frame(header, background=SURFACE)
        brand.grid(row=0, column=0, columnspan=2, sticky="ew")
        tk.Label(brand, text="轻友", background=SURFACE,
                 foreground=INK, font=FONT_TITLE, anchor="w").pack(fill="x")
        state_row = tk.Frame(brand, background=SURFACE)
        state_row.pack(fill="x", pady=(5, 0))
        tk.Label(state_row, text="把消息交给我，你先去生活。", background=SURFACE,
                 foreground=MUTED, font=FONT_S).pack(side="left")
        self.lbl_state = tk.Label(state_row, text="● 未连接", fg=DANGER,
                                  bg=SURFACE, font=FONT_B)
        self.lbl_state.pack(side="left", padx=16)
        actions = tk.Frame(header, background=SURFACE)
        actions.grid(row=1, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self.btn_connect = self._button(actions, text="连接微信",
                                        command=self._connect, variant="soft")
        self.btn_connect.pack(side="left")
        self.btn_start = self._button(actions, text="开始自动回复",
                                      command=self._start_monitor, state="disabled",
                                      variant="primary", width=132)
        self.btn_start.pack(side="left", padx=(10, 0))
        self.btn_stop = self._button(actions, text="暂停",
                                     command=self._stop_monitor, state="disabled",
                                     variant="danger", width=82)
        self.btn_stop.pack(side="left", padx=(10, 0))

        # ---------- 页签 ----------
        self.nb = ttk.Notebook(container)
        self.nb.pack(fill="both", expand=True)
        self.tab_contacts = ttk.Frame(self.nb, padding=8)
        self.tab_model = ttk.Frame(self.nb, padding=8)
        self.tab_reply = ttk.Frame(self.nb, padding=8)
        self.tab_run = ttk.Frame(self.nb, padding=8)
        self.nb.add(self.tab_contacts, text=" 聊天对象 ")
        self.nb.add(self.tab_model, text=" 大模型设置 ")
        self.nb.add(self.tab_reply, text=" 回复设置 ")
        self.nb.add(self.tab_run, text=" 运行监控 ")

        self._build_tab_contacts()
        self._build_tab_model()
        self._build_tab_reply()
        self._build_tab_run()

        # ---------- 底部 ----------
        bottom = ttk.Frame(container, padding=(2, 10, 2, 0))
        bottom.pack(fill="x")
        self.lbl_status = tk.Label(bottom, text="就绪。先连接微信，再选择聊天对象。",
                                   anchor="w", font=FONT_S, fg=MUTED, bg=BG)
        self.lbl_status.pack(side="left", fill="x", expand=True)
        self._button(bottom, text="保存设置", command=self._apply_settings,
                     variant="primary").pack(side="right")
        self._button(bottom, text="打开日志目录",
                     command=lambda: os.startfile(os.path.join(app_dir(), "logs")),
                     variant="quiet", width=118).pack(side="right", padx=8)

        if not HAS_WXAUTO4:
            self._log("error", "未检测到 wxauto4 库：请先运行项目目录里的「安装依赖.bat」"
                               "（需 Python 3.13），或执行 py -3.13 -m pip install wxauto4")

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._load_settings()
        self.after(150, self._pump)

    # ================= 样式 =================
    def _set_app_icon(self):
        """让窗口、任务栏和快捷方式使用统一的轻友品牌图标。"""
        branding = os.path.join(app_dir(), "assets", "branding")
        ico_path = os.path.join(branding, "qingyou.ico")
        png_path = os.path.join(branding, "qingyou-icon.png")
        try:
            if os.path.exists(ico_path):
                self.iconbitmap(ico_path)
        except Exception:
            pass
        try:
            if os.path.exists(png_path):
                self._app_icon_image = tk.PhotoImage(file=png_path)
                self.iconphoto(True, self._app_icon_image)
        except Exception:
            pass

    def _init_style(self):
        self.option_add("*Font", FONT)
        self.option_add("*Background", BG)
        self.option_add("*Foreground", INK)
        self.option_add("*selectBackground", "#BFD3C4")
        self.option_add("*selectForeground", INK)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=INK)
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 8))
        style.configure("TNotebook.Tab", padding=(18, 9), font=FONT_B,
                        background="#EAE8E0", foreground=MUTED, borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", SOFT)],
                  foreground=[("selected", INK)])
        style.configure("Treeview", rowheight=30, font=FONT_S,
                        background="#FCFBF7", fieldbackground="#FCFBF7",
                        borderwidth=0)
        style.configure("Treeview.Heading", font=FONT_B, background=SOFT,
                        foreground=INK, relief="flat", padding=6)
        style.configure("TLabelframe", background=BG, bordercolor="#D8DDD5",
                        relief="flat", padding=9)
        style.configure("TLabelframe.Label", font=FONT_B, background=BG,
                        foreground=ACCENT)
        style.configure("TEntry", fieldbackground="#FFFEFA", padding=6)
        style.configure("TCombobox", fieldbackground="#FFFEFA", padding=5)
        style.configure("TCheckbutton", background=BG, foreground=INK, padding=4)

    def _section(self, parent, title):
        outer = ttk.Frame(parent)
        outer.pack(fill="x", pady=(2, 14))
        ttk.Label(outer, text=title, font=("Microsoft YaHei UI", 11, "bold"),
                  foreground=ACCENT).pack(fill="x")
        tk.Frame(outer, height=1, background="#D9DED6").pack(fill="x", pady=(7, 10))
        body = ttk.Frame(outer)
        body.pack(fill="x")
        return body

    def _button(self, parent, text, command=None, state="normal",
                variant="soft", width=None, **kwargs):
        return RoundedButton(parent, text=text, command=command, state=state,
                             variant=variant, width=width, **kwargs)

    # ================= 页签1：聊天对象 =================
    def _build_tab_contacts(self):
        tab = self.tab_contacts
        pane = ttk.Panedwindow(tab, orient="horizontal")
        pane.pack(fill="both", expand=True)

        left = ttk.Labelframe(pane, text=" 微信当前会话列表 ", padding=6)
        right = ttk.Labelframe(
            pane, text=f" 监控中的对象（最多 {MAX_MONITORED_CONTACTS} 个） ", padding=6)
        pane.add(left, weight=1)
        pane.add(right, weight=1)
        self.after_idle(lambda: pane.sashpos(0, max(420, pane.winfo_width() // 2)))

        sf = ttk.Frame(left)
        sf.pack(fill="x")
        ttk.Label(sf, text="搜索:").pack(side="left")
        self.var_search = tk.StringVar()
        self.var_search.trace_add("write", lambda *_: self._render_sessions())
        ttk.Entry(sf, textvariable=self.var_search).pack(
            side="left", fill="x", expand=True, padx=4)
        self._button(left, text="刷新会话列表", command=self._refresh_sessions).pack(
            fill="x", pady=6)
        self.lst_sessions = tk.Listbox(left, selectmode="extended",
                                       activestyle="none", font=FONT_S)
        self.lst_sessions.pack(fill="both", expand=True)

        rf = ttk.Frame(right)
        rf.pack(fill="x")
        ttk.Label(rf, text="你们的关系:").pack(side="left")
        self.var_rel = tk.StringVar()
        ttk.Entry(rf, textvariable=self.var_rel, width=14).pack(
            side="left", padx=4, fill="x", expand=True)
        self._button(right, text="添加所选会话", command=self._add_selected,
                     variant="primary").pack(fill="x", pady=6)
        self.lst_mon = tk.Listbox(right, font=FONT_S, activestyle="none")
        self.lst_mon.pack(fill="both", expand=True, pady=(2, 4))
        bf = ttk.Frame(right)
        bf.pack(fill="x")
        self._button(bf, text="移除所选", command=self._remove_selected).pack(
            side="left", expand=True, fill="x")
        self._button(bf, text="清空该对象记忆",
                     command=self._clear_history, variant="quiet",
                     width=150).pack(side="left", padx=(8, 0))
        ttk.Label(right, text="提示：先在左侧多选（按住 Ctrl），填好关系再添加。\n"
                              "「关系」会告诉大模型该用什么语气和分寸。",
                  font=FONT_S, foreground="#777", wraplength=330,
                  justify="left").pack(fill="x", pady=(6, 0))
        self._render_monitored()

    def _render_sessions(self):
        kw = self.var_search.get().strip()
        self.lst_sessions.delete(0, "end")
        self._visible_sessions = []
        for s in self._sessions:
            name = s.get("name", "")
            if kw and kw not in name:
                continue
            self._visible_sessions.append(s)
            mark = " [免打扰]" if s.get("ismute") else ""
            last = (s.get("content") or "")[:22]
            self.lst_sessions.insert("end", f"{name}{mark}   {last}")

    def _refresh_sessions(self):
        if not self.bot or not self.bot.wx:
            messagebox.showinfo("提示", "请先点击左上角「① 连接微信」")
            return
        def work():
            try:
                self.bot.get_sessions()
            except Exception as e:
                self.bot.log("error", f"刷新会话列表失败：{e}")
        threading.Thread(target=work, daemon=True).start()

    def _add_selected(self):
        idxs = self.lst_sessions.curselection()
        if not idxs:
            messagebox.showinfo("提示", "请先在左侧列表选择会话（可按住 Ctrl 多选）")
            return
        rel = self.var_rel.get().strip()
        current = {c["name"] for c in self.config_obj.contacts()}
        selected = [self._visible_sessions[i]["name"] for i in idxs]
        new_names = {name for name in selected if name not in current}
        available = MAX_MONITORED_CONTACTS - len(current)
        if len(new_names) > available:
            messagebox.showwarning(
                "超过监控上限",
                f"最多只能监控 {MAX_MONITORED_CONTACTS} 个对象。\n"
                f"当前已有 {len(current)} 个，还可添加 {available} 个。",
            )
            return
        for i in idxs:
            s = self._visible_sessions[i]
            self.config_obj.upsert_contact(s["name"], rel)
        self.config_obj.save()
        self._render_monitored()
        self._status(f"已添加 {len(idxs)} 个聊天对象，记得「保存设置」")

    def _remove_selected(self):
        idxs = self.lst_mon.curselection()
        names = [self._mon_names[i] for i in idxs if i < len(self._mon_names)]
        for n in names:
            self.config_obj.remove_contact(n)
        self.config_obj.save()
        self._render_monitored()

    def _clear_history(self):
        idxs = self.lst_mon.curselection()
        if not idxs:
            messagebox.showinfo("提示", "请先在监控列表中选择对象")
            return
        if self.engine:
            for i in idxs:
                if i < len(self._mon_names):
                    self.engine.clear_history(self._mon_names[i])
            messagebox.showinfo("完成", "已清空该对象的对话记忆")

    def _render_monitored(self):
        self.lst_mon.delete(0, "end")
        self._mon_names = []
        for c in self.config_obj.contacts():
            rel = c.get("relationship") or "朋友"
            self._mon_names.append(c["name"])
            self.lst_mon.insert("end", f"{c['name']}  （{rel}）")

    # ================= 页签2：大模型 =================
    def _build_tab_model(self):
        tab = self.tab_model
        lf = self._section(tab, "API 配置（支持 DeepSeek / 豆包）")

        row1 = ttk.Frame(lf); row1.pack(fill="x", pady=3)
        ttk.Label(row1, text="供应商:").pack(side="left")
        self.var_provider = tk.StringVar()
        self.cmb_provider = ttk.Combobox(
            row1, textvariable=self.var_provider, state="readonly",
            values=list(PRESETS.keys()), width=24)
        self.cmb_provider.pack(side="left", padx=4)
        self.cmb_provider.bind("<<ComboboxSelected>>", self._on_provider)
        ttk.Label(row1, text="模型:").pack(side="left", padx=(16, 0))
        self.var_model = tk.StringVar()
        ttk.Entry(row1, textvariable=self.var_model, width=28).pack(side="left", padx=4)

        row2 = ttk.Frame(lf); row2.pack(fill="x", pady=3)
        ttk.Label(row2, text="API 地址:").pack(side="left")
        self.var_base = tk.StringVar()
        ttk.Entry(row2, textvariable=self.var_base).pack(
            side="left", padx=4, fill="x", expand=True)

        row3 = ttk.Frame(lf); row3.pack(fill="x", pady=3)
        ttk.Label(row3, text="API Key:").pack(side="left")
        self.var_key = tk.StringVar()
        self.ent_key = ttk.Entry(row3, textvariable=self.var_key, show="•")
        self.ent_key.pack(side="left", padx=4, fill="x", expand=True)
        self.var_show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(row3, text="显示", variable=self.var_show_key,
                        command=lambda: self.ent_key.config(
                            show="" if self.var_show_key.get() else "•")).pack(side="left")

        row4 = ttk.Frame(lf); row4.pack(fill="x", pady=3)
        ttk.Label(row4, text="温度:").pack(side="left")
        self.var_temp = tk.DoubleVar(value=0.8)
        ttk.Scale(row4, from_=0.2, to=1.4, variable=self.var_temp,
                  length=180).pack(side="left", padx=6)
        ttk.Label(row4, text="超时(秒):").pack(side="left", padx=(16, 0))
        self.var_timeout = tk.StringVar(value="60")
        ttk.Entry(row4, textvariable=self.var_timeout, width=6).pack(side="left", padx=4)
        ttk.Label(row4, text="max_tokens:").pack(side="left", padx=(16, 0))
        self.var_maxtok = tk.StringVar(value="600")
        ttk.Entry(row4, textvariable=self.var_maxtok, width=8).pack(side="left", padx=4)
        self.btn_test = self._button(row4, text="测试连接", command=self._test_llm,
                                     variant="soft")
        self.btn_test.pack(side="left", padx=16)
        self.lbl_test = tk.Label(lf, text="", font=FONT_S, fg="#555",
                                 wraplength=760, justify="left")
        self.lbl_test.pack(fill="x", pady=(2, 0))

        lf2 = self._section(tab, "图片识别（可选，仅支持视觉模型）")
        rowv = ttk.Frame(lf2); rowv.pack(fill="x", pady=2)
        self.var_vision = tk.BooleanVar(value=False)
        ttk.Checkbutton(rowv, text="对方发图片时，把图片交给视觉模型看图回复",
                        variable=self.var_vision).pack(side="left")
        ttk.Label(rowv, text="视觉模型:").pack(side="left", padx=(16, 0))
        self.var_vmodel = tk.StringVar()
        ttk.Entry(rowv, textvariable=self.var_vmodel, width=26).pack(side="left", padx=4)
        ttk.Label(lf2, text="图片、视频封面和表情包会交给视觉模型。表情包优先识别画面文字；"
                            "没有文字时再按图片内容和情绪理解。", font=FONT_S,
                  foreground="#777", wraplength=760, justify="left").pack(fill="x")

    def _on_provider(self, _evt=None):
        p = PRESETS.get(self.var_provider.get())
        if p:
            self.var_base.set(p["base_url"])
            self.var_model.set(p["model"])
            self.var_vmodel.set(p.get("vision_model", ""))

    def _test_llm(self):
        self._apply_settings()
        api = dict(self.config_obj.get("api", default={}))
        self.btn_test.config(state="disabled")
        self.lbl_test.config(text="测试中，请稍候…")

        def work():
            from .llm import LLMClient
            ok, msg = LLMClient(api, log=lambda *a: None).test()
            self.ui_queue.put({"kind": "test", "ok": ok, "msg": msg})
        threading.Thread(target=work, daemon=True).start()

    # ================= 页签3：回复设置 =================
    def _build_tab_reply(self):
        host = self.tab_reply
        canvas = tk.Canvas(host, background=BG, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(host, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        tab = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=tab, anchor="nw")
        tab.bind("<Configure>", lambda _e: canvas.configure(
            scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(
            window_id, width=e.width))
        self.reply_canvas = canvas
        lf = self._section(tab, "人设（决定情商和语气的关键）")
        r1 = ttk.Frame(lf); r1.pack(fill="x", pady=2)
        ttk.Label(r1, text="你的昵称:").pack(side="left")
        self.var_username = tk.StringVar()
        ttk.Entry(r1, textvariable=self.var_username, width=16).pack(side="left", padx=4)
        ttk.Label(r1, text="整体风格:").pack(side="left", padx=(16, 0))
        self.var_style = tk.StringVar()
        ttk.Entry(r1, textvariable=self.var_style).pack(side="left", padx=4,
                                                        fill="x", expand=True)
        r2 = ttk.Frame(lf); r2.pack(fill="x", pady=2)
        ttk.Label(r2, text="人设补充:").pack(side="left")
        self.txt_extra = tk.Text(lf, height=3, font=FONT_S, wrap="word")
        self.txt_extra.pack(fill="x", pady=2)
        ttk.Label(lf, text="例：最近在赶项目比较忙；讨厌别人借钱；和老板说话要客气些。",
                  font=FONT_S, foreground="#777").pack(fill="x")

        lf2 = self._section(tab, "节奏与拟人化")
        g1 = ttk.Frame(lf2); g1.pack(fill="x", pady=2)
        ttk.Label(g1, text="回复前延迟(秒):").pack(side="left")
        self.var_dmin = tk.StringVar(value="1")
        ttk.Entry(g1, textvariable=self.var_dmin, width=4).pack(side="left", padx=3)
        ttk.Label(g1, text="~").pack(side="left")
        self.var_dmax = tk.StringVar(value="2")
        ttk.Entry(g1, textvariable=self.var_dmax, width=4).pack(side="left", padx=3)
        ttk.Label(g1, text="   连续消息合并等待(秒):").pack(side="left", padx=(16, 0))
        self.var_quiet = tk.StringVar(value="1")
        ttk.Entry(g1, textvariable=self.var_quiet, width=4).pack(side="left", padx=3)
        ttk.Label(g1, text="   记忆条数:").pack(side="left", padx=(16, 0))
        self.var_hist = tk.StringVar(value="16")
        ttk.Entry(g1, textvariable=self.var_hist, width=4).pack(side="left", padx=3)
        g2 = ttk.Frame(lf2); g2.pack(fill="x", pady=2)
        self.var_split = tk.BooleanVar(value=True)
        ttk.Checkbutton(g2, text="长回复拆成两条发送（更像真人）",
                        variable=self.var_split).pack(side="left")
        self.var_gat = tk.BooleanVar(value=True)
        ttk.Checkbutton(g2, text="群聊仅在被@时回复",
                        variable=self.var_gat).pack(
            side="left", padx=16)
        self.var_cx = tk.BooleanVar(value=True)
        ttk.Checkbutton(g2, text="遇到复杂情况先过渡回复再转人工",
                        variable=self.var_cx).pack(side="left", padx=16)
        g3 = ttk.Frame(lf2); g3.pack(fill="x", pady=2)
        ttk.Label(g3, text="回复内容长度:").pack(side="left")
        self.var_reply_length = tk.StringVar(value="跟随对方")
        ttk.Combobox(g3, textvariable=self.var_reply_length, state="readonly",
                     values=["跟随对方", "简短", "适中", "详细"], width=10).pack(
                         side="left", padx=4)
        ttk.Label(g3, text="少于 8 个字时自动去掉标点",
                  font=FONT_S, foreground="#777").pack(side="left", padx=12)
        g4 = ttk.Frame(lf2); g4.pack(fill="x", pady=2)
        ttk.Label(g4, text="回复状态:").pack(side="left")
        self.var_reply_mode = tk.StringVar(value="一般")
        ttk.Combobox(
            g4, textvariable=self.var_reply_mode, state="readonly",
            values=["敷衍", "一般", "热情", "严谨"], width=8,
        ).pack(side="left", padx=(4, 16))
        self.var_tone_particles = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            g4, text="允许使用语气助词（啊、呀、呢、吧、嘛等）",
            variable=self.var_tone_particles,
        ).pack(side="left")
        g5 = ttk.Frame(lf2); g5.pack(fill="x", pady=(4, 0))
        self.var_style_learning = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            g5, text="渐进学习对方语气",
            variable=self.var_style_learning,
        ).pack(side="left")
        ttk.Label(g5, text="仅在本地保存长度、语气词、标点和笑声等统计；样本越多越稳定。",
                  font=FONT_S, foreground=MUTED).pack(side="left", padx=12)

        lf3 = self._section(tab, "消息类型策略")
        grid = ttk.Frame(lf3); grid.pack(fill="x")
        self.policy_vars = {}
        for i, (key, label) in enumerate(POLICY_TYPES):
            r, c = divmod(i, 4)
            ttk.Label(grid, text=f"{label}:").grid(row=r, column=c * 2,
                                                   padx=(0 if c == 0 else 14, 3),
                                                   sticky="e")
            v = tk.StringVar()
            cmb = ttk.Combobox(grid, textvariable=v, state="readonly",
                               values=["自动回复", "手动回复"], width=9)
            cmb.grid(row=r, column=c * 2 + 1, sticky="w")
            self.policy_vars[key] = v
        ttk.Label(lf3, text="视频默认手动；改为自动后会先提取封面、识图推理，再生成回复。",
                  font=FONT_S, foreground="#777").pack(fill="x", pady=(4, 0))

        lf4 = self._section(tab, "表情包")
        s1 = ttk.Frame(lf4); s1.pack(fill="x", pady=2)
        self.var_stk = tk.BooleanVar(value=True)
        ttk.Checkbutton(s1, text="启用表情包（关闭后不发送表情包或表情代码）",
                        variable=self.var_stk).pack(side="left")
        self.var_sticker_ocr = tk.BooleanVar(value=True)
        ttk.Checkbutton(s1, text="识别收到的表情包文字",
                        variable=self.var_sticker_ocr).pack(side="left", padx=(16, 0))
        self._button(s1, text="打开表情包目录",
                     command=lambda: self._open_sticker_dir(), variant="quiet",
                     width=130).pack(side="right")
        s2 = ttk.Frame(lf4); s2.pack(fill="x", pady=(5, 2))
        self.var_wechat_favorites = tk.BooleanVar(value=True)
        ttk.Checkbutton(s2, text="优先使用微信收藏表情",
                        variable=self.var_wechat_favorites).pack(side="left")
        ttk.Label(s2, text="分类索引:").pack(side="left", padx=(16, 4))
        self.var_wechat_categories = tk.StringVar()
        ttk.Entry(s2, textvariable=self.var_wechat_categories).pack(
            side="left", fill="x", expand=True)
        self.btn_sync_favorites = self._button(
            s2, text="同步收藏", command=self._sync_favorites,
            variant="soft", width=104, height=32)
        self.btn_sync_favorites.pack(side="right", padx=(8, 0))
        self._button(s2, text="查看索引", command=self._open_favorite_cache,
                     variant="quiet", width=96, height=32).pack(side="right", padx=(8, 0))
        ttk.Label(lf4, text="先连接微信再同步；会在本机建立索引。可写“开心:0,1；无语:2-4”细分语气。"
                            "收到的表情有字按文字理解，无字按图片理解。",
                  font=FONT_S, foreground="#777").pack(fill="x")

    def _open_sticker_dir(self):
        d = os.path.join(app_dir(), "assets", "stickers")
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    def _open_favorite_cache(self):
        d = os.path.join(app_dir(), "data", "wechat_favorites")
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    def _sync_favorites(self):
        if not self.bot:
            messagebox.showinfo("请先连接", "请先点击顶部的「连接微信」")
            return
        self._apply_settings()
        self.btn_sync_favorites.config(state="disabled", text="同步中…")

        def work():
            try:
                report = self.bot.sync_favorite_stickers()
                count = int(report.get("count") or 0)
                captured = int(report.get("captured") or 0)
                mapping = self.config_obj.get(
                    "wechat_favorites", "categories", default={}) or {}

                def done():
                    self.var_wechat_categories.set(format_category_mapping(mapping))
                    self.btn_sync_favorites.config(state="normal", text="同步收藏")
                    messagebox.showinfo(
                        "同步完成",
                        f"已识别 {count} 个微信收藏表情，"
                        f"并保存 {captured} 张本地预览。")
                self.after(0, done)
            except Exception as e:
                error = str(e)
                self.after(0, lambda err=error: (
                    self.btn_sync_favorites.config(state="normal", text="同步收藏"),
                    messagebox.showerror("同步失败", err)))
        threading.Thread(target=work, daemon=True).start()

    # ================= 页签4：运行监控 =================
    def _build_tab_run(self):
        tab = self.tab_run
        top = ttk.Frame(tab); top.pack(fill="x", pady=(0, 4))
        self._button(top, text="模拟测试（不会发送）",
                     command=self._open_sim, variant="soft", width=158).pack(side="left")
        self._button(top, text="清空日志", command=self._clear_log,
                     variant="quiet").pack(side="left", padx=8)

        self.txt_log = ScrolledText(tab, font=("Consolas", 9), state="disabled",
                                    wrap="word", height=14)
        self.txt_log.pack(fill="both", expand=True)
        for tag, color in (("info", "#333333"), ("recv", "#6a1b9a"),
                           ("sent", "#1565c0"), ("warn", "#e65100"),
                           ("error", "#c62828")):
            self.txt_log.tag_config(tag, foreground=color)
        self._log("info", "欢迎！流程：① 连接微信 → ② 在「聊天对象」页添加对象 → "
                          "③ 配置 API Key → ④ 开始自动回复。")

        mf = ttk.Labelframe(tab, text=" 待手动回复（视频/图片/文件等，或大模型判断需本人处理） ",
                            padding=4)
        mf.pack(fill="x", pady=(6, 0))
        cols = ("time", "name", "mtype", "preview")
        self.tree_manual = ttk.Treeview(mf, columns=cols, show="headings", height=5)
        for cid, text, w in (("time", "时间", 70), ("name", "联系人", 130),
                             ("mtype", "类型", 70), ("preview", "内容/原因", 480)):
            self.tree_manual.heading(cid, text=text)
            self.tree_manual.column(cid, width=w, anchor="w")
        self.tree_manual.pack(fill="x", side="left", expand=True)
        bf = ttk.Frame(mf); bf.pack(side="left", fill="y", padx=4)
        self._button(bf, text="打开聊天窗口\n自己去回复",
                     command=self._manual_open, width=138, height=52).pack(fill="x", pady=2)
        self._button(bf, text="标记已处理",
                     command=self._manual_done, variant="quiet", width=138).pack(fill="x", pady=4)

    def _clear_log(self):
        self.txt_log.config(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.config(state="disabled")

    # ================= 模拟测试 =================
    def _open_sim(self):
        names = [c["name"] for c in self.config_obj.contacts()]
        if not names:
            messagebox.showinfo("提示", "请先在「聊天对象」页添加至少一个对象")
            return
        self._apply_settings()
        win = tk.Toplevel(self)
        win.title("模拟测试：看看大模型会怎么回")
        win.geometry("560x420")
        win.transient(self)
        row = ttk.Frame(win, padding=8); row.pack(fill="x")
        ttk.Label(row, text="联系人:").pack(side="left")
        cmb = ttk.Combobox(row, values=names, state="readonly", width=16)
        cmb.current(0); cmb.pack(side="left", padx=4)
        ttk.Label(row, text="模拟对方发来:").pack(side="left", padx=(12, 0))
        ent = ttk.Entry(row); ent.pack(side="left", fill="x", expand=True, padx=4)
        ent.insert(0, "在吗？周末有空吗")
        out = ScrolledText(win, font=FONT_S, wrap="word")
        out.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        def run():
            name = cmb.get()
            text = ent.get().strip() or "（空白）"
            out.config(state="normal")
            out.insert("end", f"\n>> 测试中：[{name}] 收到「{text}」…\n")
            out.config(state="disabled")
            engine = self._sim_engine()
            try:
                res = engine.generate(name, text)
                msg = (f"【意图分析】{res.analysis or '—'}\n"
                       f"【将回复】{res.reply or '（无文字）'}\n"
                       f"【表情包】{res.sticker or '不发送'}\n"
                       f"【转人工】{'是 - ' + res.reason if res.needs_human else '否'}\n")
            except Exception as e:
                msg = f"调用失败：{e}\n请检查 API Key / 网络 / 模型名。"
            out.config(state="normal")
            out.insert("end", msg)
            out.see("end")
            out.config(state="disabled")
        self._button(win, text="生成回复", variant="primary", width=130,
                     command=lambda: threading.Thread(
                         target=run, daemon=True).start()).pack(pady=(0, 12))

    def _sim_engine(self):
        if self.engine:
            return self.engine
        eng = ReplyEngine(self.config_obj,
                          log=lambda lvl, txt: self._log(lvl, "[模拟] " + txt))
        return eng

    # ================= 连接 / 启停 =================
    def _connect(self):
        self._apply_settings()
        self.btn_connect.config(state="disabled")
        self._status("正在连接微信…")

        def work():
            try:
                bot = WeChatBot(self.config_obj, self.ui_queue)
                sessions = bot.connect()
                self.bot = bot
                self.ui_queue.put({"kind": "connected", "ok": True,
                                   "n": len(sessions)})
            except Exception as e:
                self.ui_queue.put({"kind": "connected", "ok": False,
                                   "msg": str(e)})
        threading.Thread(target=work, daemon=True).start()

    def _start_monitor(self):
        if not self.config_obj.contacts():
            messagebox.showinfo("提示", "请先在「聊天对象」页添加要自动回复的对象")
            return
        self._apply_settings()
        api = self.config_obj.get("api", default={})
        if not (api.get("api_key") or "").strip():
            messagebox.showwarning("缺少 API Key", "请先在「大模型设置」页填写 API Key")
            self.nb.select(self.tab_model)
            return
        self.btn_start.config(state="disabled")
        self._status("正在启动监听…")

        def work():
            try:
                if self.engine is None:
                    self.engine = ReplyEngine(self.config_obj, log=self.bot.log)
                    self.bot.attach_engine(self.engine)
                added = []
                failed = []
                for c in self.config_obj.contacts():
                    name = c["name"]
                    if self.bot.add_listen(name):
                        added.append(name)
                    else:
                        failed.append(name)
                if not added:
                    raise RuntimeError(
                        "所有聊天对象都添加监听失败，请刷新会话列表后重新选择")
                if failed:
                    self._botlog(
                        "warn", "以下对象未找到，已跳过：" + "、".join(failed))
                self.bot.start()
            except Exception as e:
                self.ui_queue.put({"kind": "monitor_err", "msg": str(e)})
        threading.Thread(target=work, daemon=True).start()

    def _stop_monitor(self):
        if self.bot:
            self.bot.stop()

    # ================= 设置读写 =================
    def _apply_settings(self):
        cfg = self.config_obj
        cfg.set(self.var_provider.get(), "api", "provider")
        cfg.set(self.var_base.get().strip(), "api", "base_url")
        cfg.set(self.var_key.get().strip(), "api", "api_key")
        cfg.set(self.var_model.get().strip(), "api", "model")
        cfg.set(round(self.var_temp.get(), 2), "api", "temperature")
        cfg.set(self._int(self.var_timeout, 60), "api", "timeout")
        cfg.set(self._int(self.var_maxtok, 600), "api", "max_tokens")
        cfg.set(bool(self.var_vision.get()), "api", "vision_enabled")
        cfg.set(self.var_vmodel.get().strip(), "api", "vision_model")

        cfg.set(self.var_username.get().strip(), "persona", "user_name")
        cfg.set(self.var_style.get().strip(), "persona", "style")
        cfg.set(self.txt_extra.get("1.0", "end").strip(), "persona", "extra")

        cfg.set(self._int(self.var_dmin, 1), "reply", "delay_min")
        cfg.set(self._int(self.var_dmax, 2), "reply", "delay_max")
        cfg.set(self._int(self.var_quiet, 1), "reply", "quiet_seconds")
        cfg.set(self._int(self.var_hist, 16), "reply", "history_limit")
        cfg.set(bool(self.var_split.get()), "reply", "split_long")
        cfg.set(bool(self.var_gat.get()), "reply", "group_only_at")
        cfg.set(bool(self.var_cx.get()), "reply", "complex_ack")
        cfg.set(self.var_reply_length.get() or "跟随对方",
                "reply", "reply_length")
        cfg.set(bool(self.var_tone_particles.get()),
                "reply", "tone_particles")
        cfg.set(self.var_reply_mode.get() or "一般",
                "reply", "reply_mode")
        cfg.set(bool(self.var_style_learning.get()),
                "style_learning", "enabled")

        cfg.set(bool(self.var_stk.get()), "stickers", "enabled")
        cfg.set(bool(self.var_sticker_ocr.get()), "stickers", "ocr_enabled")
        cfg.set(bool(self.var_wechat_favorites.get()),
                "wechat_favorites", "enabled")
        cfg.set(parse_category_mapping(self.var_wechat_categories.get()),
                "wechat_favorites", "categories")
        for key, var in self.policy_vars.items():
            cfg.set("auto" if var.get() == "自动回复" else "manual",
                    "policy", key)
        cfg.save()

    def _load_settings(self):
        cfg = self.config_obj
        api = cfg.get("api", default={})
        self.var_provider.set(api.get("provider") or next(iter(PRESETS)))
        self.var_base.set(api.get("base_url", ""))
        self.var_key.set(api.get("api_key", ""))
        self.var_model.set(api.get("model", ""))
        self.var_temp.set(float(api.get("temperature", 0.8)))
        self.var_timeout.set(str(api.get("timeout", 60)))
        self.var_maxtok.set(str(api.get("max_tokens", 600)))
        self.var_vision.set(bool(api.get("vision_enabled")))
        self.var_vmodel.set(api.get("vision_model", "") or "")

        persona = cfg.get("persona", default={})
        self.var_username.set(persona.get("user_name", ""))
        self.var_style.set(persona.get("style", ""))
        self.txt_extra.insert("1.0", persona.get("extra", ""))

        rep = cfg.get("reply", default={})
        self.var_dmin.set(str(rep.get("delay_min", 1)))
        self.var_dmax.set(str(rep.get("delay_max", 2)))
        self.var_quiet.set(str(rep.get("quiet_seconds", 1)))
        self.var_hist.set(str(rep.get("history_limit", 16)))
        self.var_split.set(bool(rep.get("split_long", True)))
        self.var_gat.set(bool(rep.get("group_only_at", True)))
        self.var_cx.set(bool(rep.get("complex_ack", True)))
        self.var_reply_length.set(rep.get("reply_length", "跟随对方"))
        self.var_tone_particles.set(bool(rep.get("tone_particles", True)))
        mode = rep.get("reply_mode", "一般")
        self.var_reply_mode.set(mode if mode in ("敷衍", "一般", "热情", "严谨")
                                else "一般")
        self.var_style_learning.set(bool(
            cfg.get("style_learning", "enabled", default=True)))

        for key, var in self.policy_vars.items():
            var.set("自动回复" if cfg.get("policy", key, default="manual") == "auto"
                    else "手动回复")
        self.var_stk.set(bool(cfg.get("stickers", "enabled", default=True)))
        self.var_sticker_ocr.set(bool(
            cfg.get("stickers", "ocr_enabled", default=True)))
        favorite_cfg = cfg.get("wechat_favorites", default={}) or {}
        self.var_wechat_favorites.set(bool(favorite_cfg.get("enabled", True)))
        self.var_wechat_categories.set(format_category_mapping(
            favorite_cfg.get("categories") or {}))

    @staticmethod
    def _int(var, default):
        try:
            return int(float(var.get()))
        except Exception:
            return default

    # ================= 事件泵 =================
    def _pump(self):
        try:
            while True:
                ev = self.ui_queue.get_nowait()
                try:
                    self._handle_event(ev)
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.after(150, self._pump)

    def _handle_event(self, ev):
        kind = ev.get("kind")
        if kind == "log":
            self._log(ev.get("level", "info"), ev.get("text", ""))
        elif kind == "sessions":
            self._sessions = ev.get("list", [])
            self._render_sessions()
        elif kind == "connected":
            self._on_connected(ev)
        elif kind == "test":
            self.btn_test.config(state="normal")
            self.lbl_test.config(
                text=("✔ " if ev["ok"] else "✘ ") + ev["msg"],
                fg="#2e7d32" if ev["ok"] else "#c62828")
        elif kind == "monitor":
            self._on_monitor_state(bool(ev.get("running")))
        elif kind == "monitor_err":
            self.btn_start.config(state="normal" if self.bot and self.bot.wx else "disabled")
            self._log("error", f"启动失败：{ev.get('msg')}")
            self._status("启动失败，请查看运行监控日志")
        elif kind == "manual":
            self._add_manual(ev)
        elif kind == "sent":
            pass  # 已通过日志展示
        elif kind == "status":
            self._status(ev.get("text", ""))

    def _on_connected(self, ev):
        self.btn_connect.config(state="normal")
        if ev.get("ok"):
            self.lbl_state.config(text="● 已连接", fg="#2e7d32")
            self.btn_start.config(state="normal")
            self._status(f"连接成功，读到 {ev.get('n', 0)} 个会话。"
                         "去「聊天对象」页添加要自动回复的人，然后点「开始自动回复」。")
            self.nb.select(self.tab_contacts)
            self._log("info", "微信连接成功 ✔")
        else:
            self.lbl_state.config(text="● 未连接", fg="#c62828")
            self._log("error", f"连接失败：{ev.get('msg')}")
            self._status("连接失败，请确认微信已登录且主窗口已打开（不能收起到托盘）")

    def _on_monitor_state(self, running):
        self.monitoring = running
        if running:
            self.lbl_state.config(text="● 自动回复中", fg="#1565c0")
            self.btn_stop.config(state="normal")
            self.btn_start.config(state="disabled")
            self.btn_connect.config(state="disabled")
            self._status("自动回复运行中：只回复监控名单里的对象；视频/图片等会进入下方手动队列")
        else:
            if self.bot and self.bot.wx:
                self.lbl_state.config(text="● 已连接", fg="#2e7d32")
                self.btn_start.config(state="normal")
                self.btn_connect.config(state="normal")
            self.btn_stop.config(state="disabled")
            self._status("已停止")

    # ================= 手动队列 =================
    def _add_manual(self, ev):
        self._manual_seq += 1
        iid = f"m{self._manual_seq}"
        self._manual_items[iid] = ev
        self.tree_manual.insert("", "end", iid=iid, values=(
            ev.get("time", ""), ev.get("name", ""),
            ev.get("mtype", ""), f"{ev.get('preview','')}  —{ev.get('reason','') or ''}"))
        self._update_manual_badge()
        self.bell()
        if self._flash_job is None:
            self._flash_title()

    def _manual_open(self):
        sel = self.tree_manual.selection()
        if not sel or not self.bot:
            return
        ev = self._manual_items.get(sel[0])
        if ev:
            self.bot.open_chat(ev.get("name", ""))

    def _manual_done(self):
        for iid in self.tree_manual.selection():
            self.tree_manual.delete(iid)
            self._manual_items.pop(iid, None)
        self._update_manual_badge()

    def _update_manual_badge(self):
        n = len(self._manual_items)
        self.nb.tab(self.tab_run,
                    text=f" 运行监控 {('（⚠%d条待手动）' % n) if n else ''} ")

    def _flash_title(self):
        if not self._manual_items:
            self.title("轻友")
            self._flash_job = None
            return
        self._flash_on = not self._flash_on
        base = "轻友"
        self.title(("⚠⚠ 待人工回复 ⚠⚠ — " if self._flash_on else "") + base)
        self._flash_job = self.after(900, self._flash_title)

    # ================= 杂项 =================
    def _botlog(self, level, text):
        self.emit_log(level, text)

    def emit_log(self, level, text):
        self.ui_queue.put({"kind": "log", "level": level, "text": str(text)})

    def _log(self, level, text):
        tag = level if level in ("recv", "sent") else \
            {"warning": "warn"}.get(level, level if level in
                                    ("info", "error", "warn") else "info")
        ts = time.strftime("%H:%M:%S")
        self.txt_log.config(state="normal")
        self.txt_log.insert("end", f"[{ts}] {text}\n", tag)
        self.txt_log.see("end")
        self.txt_log.config(state="disabled")

    def _status(self, text):
        self.lbl_status.config(text=text)

    def _on_close(self):
        try:
            if self.bot:
                self.bot.stop()
            if self.engine:
                self.engine.save_history()
            self._apply_settings()
        except Exception:
            pass
        self.destroy()
