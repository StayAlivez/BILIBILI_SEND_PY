"""Tkinter 图形界面。

三个页签：发送（配置 + 启停）、消息库（增删改查 + 存取 JSON）、运行日志。
所有网络请求都在后台线程执行，界面只通过队列刷新，点击「开始发送」即启动，
对应原 Java 版在 IDEA 里跑 ``BilibiliSendApplicationTests`` 的用法。
"""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any

from .client import BilibiliClient, BilibiliError, RoomInfo, cookie_warnings, mask_cookie, parse_cookie
from .config import (
    APP_DIR,
    DEFAULT_MESSAGES_FILE,
    MAX_COLOR,
    MAX_FONT_SIZE,
    MAX_INTERVAL_SECONDS,
    MIN_FONT_SIZE,
    MIN_INTERVAL_SECONDS,
    MODE_UNIFORM,
    MODE_WEIGHTED,
    AppConfig,
    load_config,
    resolve_path,
    save_config,
)
from .logbus import LOG_QUEUE, success
from .messages import (
    MAX_DANMAKU_LENGTH,
    MessageFileError,
    MessageItem,
    MessagePool,
    load_messages,
    save_messages,
)
from .runner import DanmakuRunner

log = logging.getLogger(__name__)

COLOR_PRESETS: list[tuple[str, int]] = [
    ("默认青绿", 5566168),
    ("白色", 16777215),
    ("红色", 16711680),
    ("橙色", 16744192),
    ("黄色", 16776960),
    ("绿色", 65280),
    ("青色", 65535),
    ("蓝色", 255),
    ("紫色", 10499184),
    ("粉色", 16740540),
]

POSITION_PRESETS: list[tuple[str, int]] = [
    ("滚动", 1),
    ("顶部", 5),
    ("底部", 4),
]

AUTOSCROLL_DEFAULT = True
POLL_MS = 120


class BiliSendApp(tk.Tk):
    """主窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self.cfg: AppConfig = load_config()
        self.events: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.runner: DanmakuRunner | None = None
        self.items: list[MessageItem] = []
        self.pool: MessagePool | None = None
        self._loaded_messages_path: Path | None = None
        self._next_send_at: float | None = None
        self._stats = {"total": 0, "ok": 0, "failed": 0}
        self._lockable: list[tk.Widget] = []
        self._running = False
        self._cookie_hidden = True
        self._cookie_value = ""
        self.report_callback_exception = self._on_callback_error  # type: ignore[assignment]

        self._setup_window()
        self._setup_style()
        self._build_ui()
        self._load_config_into_ui()
        self._refresh_cookie_status()
        self._refresh_message_tree()
        self._refresh_stats()
        self.after(POLL_MS, self._pump)
        log.info("界面已就绪，配置目录：%s", APP_DIR)

    # ---------------------------------------------------------------- 窗口与样式
    def _setup_window(self) -> None:
        self.title("BILIBILI_SEND · 直播弹幕助手（Python 版）")
        width, height = 1080, 820
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2 - 20)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(940, 680)

    def _setup_style(self) -> None:
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(name).configure(family="Microsoft YaHei UI", size=10)
            except tk.TclError:
                pass

        style = ttk.Style(self)
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

        style.configure("Muted.TLabel", foreground="#6b7280")
        style.configure("Good.TLabel", foreground="#15803d")
        style.configure("Bad.TLabel", foreground="#b42318")
        style.configure("Head.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Status.TLabel", padding=(6, 2))
        style.configure("Treeview", rowheight=26)

    # ------------------------------------------------------------------ 布局
    def _build_ui(self) -> None:
        self._build_statusbar()

        container = ttk.Frame(self, padding=(12, 10, 12, 6))
        container.pack(fill="both", expand=True)

        self.notebook = ttk.Notebook(container)
        self.notebook.pack(fill="both", expand=True)

        self.tab_send = ttk.Frame(self.notebook, padding=12)
        self.tab_messages = ttk.Frame(self.notebook, padding=12)
        self.tab_log = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.tab_send, text="  发送  ")
        self.notebook.add(self.tab_messages, text="  消息库  ")
        self.notebook.add(self.tab_log, text="  运行日志  ")

        self._build_send_tab()
        self._build_messages_tab()
        self._build_log_tab()

    def _build_statusbar(self) -> None:
        self.state_var = tk.StringVar(value="已停止")
        self.stats_var = tk.StringVar(value="已发送 0 · 成功 0 · 失败 0")
        self.countdown_var = tk.StringVar(value="下次发送：—")
        self.status_room_var = tk.StringVar(value="未连接直播间")

        bar = ttk.Frame(self, padding=(12, 2, 12, 8))
        bar.pack(side="bottom", fill="x")
        ttk.Separator(self).pack(side="bottom", fill="x")

        self.state_label = ttk.Label(bar, textvariable=self.state_var, style="Status.TLabel")
        self.state_label.pack(side="left")
        ttk.Label(bar, text="│", style="Muted.TLabel").pack(side="left")
        ttk.Label(bar, textvariable=self.stats_var, style="Status.TLabel").pack(side="left")
        ttk.Label(bar, text="│", style="Muted.TLabel").pack(side="left")
        ttk.Label(bar, textvariable=self.countdown_var, style="Status.TLabel").pack(side="left")
        ttk.Label(bar, textvariable=self.status_room_var, style="Muted.TLabel").pack(side="right")

    def _build_send_tab(self) -> None:
        tab = self.tab_send
        tab.columnconfigure(0, weight=1)

        # --- 直播间 ---------------------------------------------------------
        room_box = ttk.LabelFrame(tab, text=" 直播间 ", padding=10)
        room_box.grid(row=0, column=0, sticky="ew")
        room_box.columnconfigure(2, weight=1)

        ttk.Label(room_box, text="房间号").grid(row=0, column=0, sticky="w")
        self.room_var = tk.StringVar()
        self.room_entry = ttk.Entry(room_box, textvariable=self.room_var, width=16)
        self.room_entry.grid(row=0, column=1, padx=(8, 10), sticky="w")

        self.detect_btn = ttk.Button(room_box, text="检测直播间", command=self._on_detect_room)
        self.detect_btn.grid(row=0, column=2, sticky="w")

        self.room_info_var = tk.StringVar(value="点「检测直播间」可查询真实房间号、标题与开播状态。")
        self.room_info_label = ttk.Label(
            room_box, textvariable=self.room_info_var, style="Muted.TLabel", wraplength=900, justify="left"
        )
        self.room_info_label.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))

        # --- Cookie ---------------------------------------------------------
        cookie_box = ttk.LabelFrame(tab, text=" 账号 Cookie（需包含 SESSDATA 与 bili_jct） ", padding=10)
        cookie_box.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        cookie_box.columnconfigure(0, weight=1)

        text_frame = ttk.Frame(cookie_box)
        text_frame.grid(row=0, column=0, sticky="ew")
        text_frame.columnconfigure(0, weight=1)

        self.cookie_text = tk.Text(
            text_frame,
            height=4,
            wrap="char",
            relief="solid",
            borderwidth=1,
            font=("Consolas", 9),
            state="disabled",
        )
        self.cookie_text.grid(row=0, column=0, sticky="ew")
        cookie_scroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.cookie_text.yview)
        cookie_scroll.grid(row=0, column=1, sticky="ns")
        self.cookie_text.configure(yscrollcommand=cookie_scroll.set)
        self.cookie_text.bind("<KeyRelease>", lambda _event: self._refresh_cookie_status())
        self.cookie_text.bind("<FocusOut>", lambda _event: self._refresh_cookie_status())

        cookie_options = ttk.Frame(cookie_box)
        cookie_options.grid(row=1, column=0, sticky="ew", pady=(6, 0))

        self.show_cookie_var = tk.BooleanVar(value=False)
        self.show_cookie_check = ttk.Checkbutton(
            cookie_options, text="显示 Cookie", variable=self.show_cookie_var,
            command=self._on_toggle_show_cookie,
        )
        self.show_cookie_check.pack(side="left")

        self.save_cookie_var = tk.BooleanVar(value=True)
        self.save_cookie_check = ttk.Checkbutton(
            cookie_options, text="保存 Cookie 到 config.json", variable=self.save_cookie_var,
            command=self._on_toggle_save_cookie,
        )
        self.save_cookie_check.pack(side="left", padx=(18, 0))

        ttk.Label(
            cookie_options, text="默认打码显示；录屏演示时建议取消保存", style="Muted.TLabel"
        ).pack(side="left", padx=(14, 0))

        cookie_tools = ttk.Frame(cookie_box)
        cookie_tools.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(cookie_tools, text="粘贴剪贴板", command=self._paste_cookie).pack(side="left")
        ttk.Button(cookie_tools, text="清空", command=self._clear_cookie).pack(side="left", padx=6)
        self.cookie_status_var = tk.StringVar()
        self.cookie_status_label = ttk.Label(
            cookie_tools, textvariable=self.cookie_status_var, style="Muted.TLabel", wraplength=680
        )
        self.cookie_status_label.pack(side="left", padx=(12, 0))

        # --- 发送设置 -------------------------------------------------------
        set_box = ttk.LabelFrame(tab, text=" 发送设置 ", padding=10)
        set_box.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        set_box.columnconfigure(1, weight=1)

        ttk.Label(set_box, text="发送间隔（秒）").grid(row=0, column=0, sticky="w")
        self.interval_var = tk.StringVar()
        self.interval_spin = ttk.Spinbox(
            set_box, from_=1, to=3600, increment=1, width=8, textvariable=self.interval_var
        )
        self.interval_spin.grid(row=0, column=1, sticky="w", padx=(8, 18))
        self.interval_spin.bind("<FocusOut>", lambda _event: self._normalize_interval())
        self.interval_spin.bind("<Return>", lambda _event: self._normalize_interval())

        ttk.Label(set_box, text="抽取方式").grid(row=0, column=2, sticky="w")
        mode_frame = ttk.Frame(set_box)
        mode_frame.grid(row=0, column=3, columnspan=3, sticky="w", padx=(8, 0))
        self.mode_var = tk.StringVar(value=MODE_WEIGHTED)
        ttk.Radiobutton(mode_frame, text="按权重随机", value=MODE_WEIGHTED, variable=self.mode_var).pack(side="left")
        ttk.Radiobutton(mode_frame, text="完全随机", value=MODE_UNIFORM, variable=self.mode_var).pack(
            side="left", padx=(14, 0)
        )

        ttk.Label(set_box, text="消息文件").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.msg_path_var = tk.StringVar()
        self.msg_path_entry = ttk.Entry(set_box, textvariable=self.msg_path_var)
        self.msg_path_entry.grid(row=1, column=1, columnspan=4, sticky="ew", padx=(8, 6), pady=(10, 0))
        file_actions = ttk.Frame(set_box)
        file_actions.grid(row=1, column=5, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Button(file_actions, text="浏览…", command=self._browse_messages).pack(side="left")
        ttk.Button(file_actions, text="重新加载", command=self._reload_messages_clicked).pack(
            side="left", padx=(8, 0)
        )

        ttk.Label(set_box, text="弹幕颜色").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.color_var = tk.StringVar()
        self.color_combo = ttk.Combobox(
            set_box,
            textvariable=self.color_var,
            values=[f"{name} ({value})" for name, value in COLOR_PRESETS],
            width=20,
        )
        self.color_combo.grid(row=2, column=1, sticky="w", padx=(8, 18), pady=(10, 0))

        ttk.Label(set_box, text="字号").grid(row=2, column=2, sticky="w", pady=(10, 0))
        self.font_var = tk.StringVar()
        self.font_spin = ttk.Spinbox(
            set_box, from_=MIN_FONT_SIZE, to=MAX_FONT_SIZE, increment=1, width=6, textvariable=self.font_var
        )
        self.font_spin.grid(row=2, column=3, sticky="w", padx=(8, 18), pady=(10, 0))

        ttk.Label(set_box, text="位置").grid(row=2, column=4, sticky="e", pady=(10, 0))
        self.position_var = tk.StringVar()
        self.position_combo = ttk.Combobox(
            set_box,
            textvariable=self.position_var,
            values=[f"{name} ({value})" for name, value in POSITION_PRESETS],
            width=12,
            state="readonly",
        )
        self.position_combo.grid(row=2, column=5, columnspan=2, sticky="w", padx=(8, 0), pady=(10, 0))

        self.avoid_repeat_var = tk.BooleanVar(value=True)
        self.avoid_repeat_check = ttk.Checkbutton(
            set_box, text="避免连续两条相同弹幕", variable=self.avoid_repeat_var
        )
        self.avoid_repeat_check.grid(row=3, column=0, columnspan=4, sticky="w", pady=(10, 0))

        # --- 操作按钮 -------------------------------------------------------
        actions = ttk.Frame(tab)
        actions.grid(row=3, column=0, sticky="ew", pady=(14, 0))

        self.start_btn = tk.Button(
            actions,
            text="▶  开始发送",
            command=self._on_start,
            bg="#16a34a",
            fg="white",
            activebackground="#15803d",
            activeforeground="white",
            disabledforeground="#d1d5db",
            relief="flat",
            padx=20,
            pady=6,
            font=("Microsoft YaHei UI", 10, "bold"),
            cursor="hand2",
        )
        self.start_btn.pack(side="left")

        self.stop_btn = tk.Button(
            actions,
            text="■  停止",
            command=self._on_stop,
            bg="#dc2626",
            fg="white",
            activebackground="#b91c1c",
            activeforeground="white",
            disabledforeground="#d1d5db",
            relief="flat",
            padx=20,
            pady=6,
            font=("Microsoft YaHei UI", 10, "bold"),
            state="disabled",
            cursor="hand2",
        )
        self.stop_btn.pack(side="left", padx=10)

        self.test_btn = ttk.Button(actions, text="发送一条测试", command=self._on_test_send)
        self.test_btn.pack(side="left")

        self.hint_var = tk.StringVar(
            value=f"提示：B 站普通用户单条弹幕上限 {MAX_DANMAKU_LENGTH} 字；发送过快会被限流（建议 ≥ 10 秒）。"
        )
        ttk.Label(tab, textvariable=self.hint_var, style="Muted.TLabel", wraplength=980, justify="left").grid(
            row=4, column=0, sticky="w", pady=(12, 0)
        )

        tips = ttk.LabelFrame(tab, text=" 小贴士 ", padding=10)
        tips.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        for line in (
            "Cookie：浏览器登录 bilibili.com → F12「网络」→ 任选一个请求 → 复制请求头里的 Cookie 整串，粘贴到上方。",
            "频率：建议间隔 ≥ 10 秒；太快会被限流（返回 -509 / 10031），连续失败 5 次会自动停止。",
            "停止：点「停止」会立即中断等待，正在发送的那一条最多再等一次请求超时（默认 5 秒）。",
            "声明：仅供个人学习使用，请遵守 B 站社区规则，账号风险自负。",
        ):
            ttk.Label(tips, text="• " + line, style="Muted.TLabel", wraplength=980, justify="left").pack(
                anchor="w", pady=1
            )

        # 运行期间需要锁定（不可编辑）的控件
        self._lockable = [
            self.room_entry,
            self.detect_btn,
            self.cookie_text,
            self.interval_spin,
            self.msg_path_entry,
            self.color_combo,
            self.font_spin,
            self.position_combo,
            self.avoid_repeat_check,
            self.show_cookie_check,
            self.save_cookie_check,
        ]
        for widget in mode_frame.winfo_children():
            self._lockable.append(widget)

    def _build_messages_tab(self) -> None:
        tab = self.tab_messages
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        self.msg_summary_var = tk.StringVar(value="尚未加载消息")
        ttk.Label(tab, textvariable=self.msg_summary_var).grid(row=0, column=0, sticky="w", pady=(0, 8))

        body = ttk.Frame(tab)
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        columns = ("idx", "text", "weight", "state")
        self.tree = ttk.Treeview(body, columns=columns, show="headings", selectmode="browse")
        for column, title, width, anchor, stretch in (
            ("idx", "#", 50, "center", False),
            ("text", "弹幕内容", 460, "w", True),
            ("weight", "权重", 70, "center", False),
            ("state", "状态", 130, "center", False),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, anchor=anchor, stretch=stretch)
        self.tree.grid(row=0, column=0, sticky="nsew")

        tree_scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        editor = ttk.LabelFrame(tab, text=" 编辑单条弹幕 ", padding=10)
        editor.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        editor.columnconfigure(1, weight=1)

        ttk.Label(editor, text="内容").grid(row=0, column=0, sticky="nw")
        self.msg_text = tk.Text(editor, height=3, wrap="char", relief="solid", borderwidth=1)
        self.msg_text.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(8, 0))

        weight_row = ttk.Frame(editor)
        weight_row.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Label(weight_row, text="权重").pack(side="left")
        self.msg_weight_var = tk.StringVar(value="1")
        self.msg_weight_spin = ttk.Spinbox(
            weight_row, from_=0, to=999, increment=1, width=6, textvariable=self.msg_weight_var
        )
        self.msg_weight_spin.pack(side="left", padx=(8, 14))
        ttk.Label(
            weight_row, text="权重越大越常出现；0 表示暂不参与抽取", style="Muted.TLabel"
        ).pack(side="left")

        button_bar = ttk.Frame(editor)
        button_bar.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(10, 0))

        self.msg_buttons: list[ttk.Button] = []
        self._selection_buttons: list[ttk.Button] = []
        for text, command, needs_selection in (
            ("新增", self._on_message_add, False),
            ("更新选中", self._on_message_update, True),
            ("删除选中", self._on_message_delete, True),
            ("清空输入", self._clear_editor, False),
        ):
            button = ttk.Button(button_bar, text=text, command=command)
            button.pack(side="left", padx=(0, 8))
            self.msg_buttons.append(button)
            if needs_selection:
                self._selection_buttons.append(button)

        tools = ttk.Frame(tab)
        tools.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self._message_tool_buttons: list[ttk.Button] = []
        for text, command in (
            ("保存到文件", self._save_messages_clicked),
            ("另存为…", self._save_messages_as),
            ("导入 JSON…", self._import_messages),
            ("打开项目目录", self._open_folder),
        ):
            button = ttk.Button(tools, text=text, command=command)
            button.pack(side="left", padx=(0, 8))
            self._message_tool_buttons.append(button)

    def _build_log_tab(self) -> None:
        tab = self.tab_log
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        tools = ttk.Frame(tab)
        tools.grid(row=0, column=0, sticky="ew")
        ttk.Button(tools, text="清空", command=self._clear_log).pack(side="left")
        ttk.Button(tools, text="导出…", command=self._export_log).pack(side="left", padx=8)
        self.autoscroll_var = tk.BooleanVar(value=AUTOSCROLL_DEFAULT)
        ttk.Checkbutton(tools, text="自动滚动", variable=self.autoscroll_var).pack(side="right")
        ttk.Label(tools, text="日志同时写入 logs/bili_send.log", style="Muted.TLabel").pack(side="right", padx=12)

        self.log_text = scrolledtext.ScrolledText(
            tab,
            height=20,
            wrap="word",
            relief="solid",
            borderwidth=1,
            state="disabled",
            font=("Microsoft YaHei UI", 9),
            spacing1=1,
            spacing3=1,
        )
        self.log_text.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.log_text.tag_configure("time", foreground="#9ca3af")
        self.log_text.tag_configure("info", foreground="#1f2937")
        self.log_text.tag_configure("success", foreground="#15803d")
        self.log_text.tag_configure("warn", foreground="#b45309")
        self.log_text.tag_configure("error", foreground="#b91c1c")

    # ------------------------------------------------------------- 配置 <-> 界面
    def _load_config_into_ui(self) -> None:
        cfg = self.cfg
        self.room_var.set(str(cfg.room_id))
        self.save_cookie_var.set(bool(cfg.save_cookie))
        self._set_cookie_value(cfg.cookie, hide=True)
        self.interval_var.set(_format_number(cfg.interval_seconds))
        self.mode_var.set(cfg.mode)
        self.msg_path_var.set(cfg.messages_file)
        self.color_var.set(self._color_label(cfg.color))
        self.font_var.set(str(cfg.fontsize))
        self.position_var.set(self._position_label(cfg.danmaku_mode))
        self.avoid_repeat_var.set(cfg.avoid_repeat)
        if cfg.room_title or cfg.anchor_name:
            self.room_info_var.set(
                f"上次连接：{cfg.room_title or '标题未知'}" + (f" · 主播 {cfg.anchor_name}" if cfg.anchor_name else "")
            )
        self._load_messages_into_state(initial=True)

    def _current_messages_path(self) -> Path:
        text = self.msg_path_var.get().strip() or DEFAULT_MESSAGES_FILE
        return resolve_path(text)

    def _collect_config(self, silent: bool = False) -> bool:
        """把界面上的值收敛成 AppConfig，校验失败返回 False。"""

        def fail(message: str) -> bool:
            if not silent:
                messagebox.showerror("设置不完整", message)
            return False

        room_text = self.room_var.get().strip()
        if not room_text.isdigit() or int(room_text) <= 0:
            return fail("房间号必须是正整数，例如 22676119。")

        cookie = self._cookie_text_now()
        if not cookie and not silent:
            return fail("请先填写账号 Cookie（需要包含 SESSDATA 与 bili_jct）。")

        try:
            interval = float(self.interval_var.get())
        except ValueError:
            return fail("发送间隔必须是数字。")
        interval = min(max(interval, MIN_INTERVAL_SECONDS), MAX_INTERVAL_SECONDS)

        try:
            color = self._parse_color(self.color_var.get())
        except ValueError as exc:
            return fail(str(exc))

        try:
            fontsize = int(self.font_var.get())
        except ValueError:
            return fail("字号必须是整数。")
        fontsize = int(min(max(fontsize, MIN_FONT_SIZE), MAX_FONT_SIZE))

        try:
            position = self._parse_position(self.position_var.get())
        except ValueError as exc:
            return fail(str(exc))

        mode = self.mode_var.get()
        if mode not in (MODE_WEIGHTED, MODE_UNIFORM):
            mode = MODE_WEIGHTED

        self.cfg = AppConfig(
            cookie=cookie,
            save_cookie=bool(self.save_cookie_var.get()),
            room_id=int(room_text),
            interval_seconds=interval,
            mode=mode,
            messages_file=self._store_path(self._current_messages_path()),
            avoid_repeat=bool(self.avoid_repeat_var.get()),
            color=color,
            fontsize=fontsize,
            danmaku_mode=position,
            timeout_seconds=self.cfg.timeout_seconds,
            room_title=self.cfg.room_title,
            anchor_name=self.cfg.anchor_name,
        )
        self.interval_var.set(_format_number(interval))
        self.font_var.set(str(fontsize))
        return True

    @staticmethod
    def _store_path(path: Path) -> str:
        resolved = path.resolve()
        try:
            return resolved.relative_to(APP_DIR).as_posix()
        except ValueError:
            return str(resolved)

    def _normalize_interval(self) -> None:
        try:
            value = float(self.interval_var.get())
        except ValueError:
            value = self.cfg.interval_seconds
        value = min(max(value, MIN_INTERVAL_SECONDS), MAX_INTERVAL_SECONDS)
        self.interval_var.set(_format_number(value))

    # ------------------------------------------------------------- 颜色 / 位置
    @staticmethod
    def _color_label(value: int) -> str:
        for name, preset in COLOR_PRESETS:
            if preset == value:
                return f"{name} ({value})"
        return f"自定义 ({value})"

    @staticmethod
    def _parse_color(text: str) -> int:
        text = text.strip()
        hex_match = re.search(r"#[0-9a-fA-F]{6}", text)
        if hex_match:
            value = int(hex_match.group(0)[1:], 16)
        else:
            digits = re.findall(r"\d+", text)
            if not digits:
                raise ValueError("弹幕颜色请选择预设，或填写 0–16777215 的十进制颜色值。")
            value = int(digits[-1])
        if not 0 <= value <= MAX_COLOR:
            raise ValueError("弹幕颜色必须在 0–16777215 之间。")
        return value

    @staticmethod
    def _position_label(value: int) -> str:
        for name, preset in POSITION_PRESETS:
            if preset == value:
                return f"{name} ({value})"
        return f"滚动 ({POSITION_PRESETS[0][1]})"

    @staticmethod
    def _parse_position(text: str) -> int:
        digits = re.findall(r"\d+", text)
        if digits:
            value = int(digits[-1])
            if value in (1, 4, 5):
                return value
        raise ValueError("弹幕位置只能是 滚动(1)、顶部(5) 或 底部(4)。")

    # ---------------------------------------------------------------- Cookie
    def _set_cookie_value(self, value: str, hide: bool = True) -> None:
        """设置 Cookie 内容；hide=True 时界面上只显示打码文本。"""
        self._cookie_value = (value or "").strip()
        self._cookie_hidden = hide
        self._render_cookie_text()

    def _render_cookie_text(self) -> None:
        widget = self.cookie_text
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        if self._cookie_hidden:
            widget.insert("1.0", mask_cookie(self._cookie_value))
        else:
            widget.insert("1.0", self._cookie_value)
        widget.configure(state="normal" if not self._cookie_hidden and not self._running else "disabled")

    def _cookie_text_now(self) -> str:
        if self._cookie_hidden:
            return self._cookie_value
        return self.cookie_text.get("1.0", "end").strip()

    def _sync_cookie_value(self) -> None:
        if not self._cookie_hidden:
            self._cookie_value = self.cookie_text.get("1.0", "end").strip()

    def _on_toggle_show_cookie(self) -> None:
        if self._running:
            return
        if self.show_cookie_var.get():
            self._cookie_hidden = False
        else:
            self._sync_cookie_value()
            self._cookie_hidden = True
        self._render_cookie_text()
        self._refresh_cookie_status()

    def _on_toggle_save_cookie(self) -> None:
        self.cfg.save_cookie = bool(self.save_cookie_var.get())
        if self.save_cookie_var.get():
            log.info("已开启 Cookie 保存，下次启动会自动填入")
        else:
            self._scrub_saved_cookie()
            log.info("已关闭 Cookie 保存，并清除 config.json 中的 Cookie")

    def _scrub_saved_cookie(self) -> None:
        cfg = self.cfg.copy()
        cfg.cookie = ""
        cfg.save_cookie = False
        try:
            save_config(cfg)
        except OSError as exc:
            log.warning("清除 config.json 中的 Cookie 失败：%s", exc)

    def _paste_cookie(self) -> None:
        try:
            text = self.clipboard_get()
        except tk.TclError:
            messagebox.showinfo("剪贴板为空", "剪贴板里没有文本内容。")
            return
        self._set_cookie_value(text, hide=self._cookie_hidden)
        self._refresh_cookie_status()
        log.info("已从剪贴板粘贴 Cookie（%d 个键）", len(parse_cookie(text)))

    def _clear_cookie(self) -> None:
        self._set_cookie_value("", hide=self._cookie_hidden)
        self._refresh_cookie_status()

    def _refresh_cookie_status(self) -> None:
        raw = self._cookie_text_now()
        warnings = cookie_warnings(raw)
        if not raw:
            self.cookie_status_var.set("尚未填写 Cookie")
            self.cookie_status_label.configure(style="Muted.TLabel")
            return
        if warnings:
            self.cookie_status_var.set("⚠ " + "；".join(warnings))
            self.cookie_status_label.configure(style="Bad.TLabel")
            return
        if self._cookie_hidden:
            self.cookie_status_var.set("✔ Cookie 可用（已打码）")
        else:
            uid = parse_cookie(raw).get("DedeUserID", "")
            self.cookie_status_var.set(f"✔ Cookie 可用（UID {uid}）" if uid else "✔ Cookie 可用")
        self.cookie_status_label.configure(style="Good.TLabel")

    # ------------------------------------------------------------- 消息库操作
    def _refresh_message_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        usable = 0
        for index, item in enumerate(self.items):
            if item.usable:
                usable += 1
            state = "可用" if item.usable else "权重 0（不参与）"
            if len(item.text) > MAX_DANMAKU_LENGTH:
                state += " · 超长"
            self.tree.insert("", "end", iid=str(index), values=(index + 1, item.text, item.weight, state))
        if self.items:
            self.msg_summary_var.set(
                f"共 {len(self.items)} 条，其中 {usable} 条参与抽取 · 文件：{self.cfg.messages_path}"
            )
        self._update_message_buttons()

    def _update_message_buttons(self) -> None:
        running = self._running
        has_selection = bool(self.tree.selection())
        for button in self.msg_buttons + self._message_tool_buttons:
            needs_selection = button in self._selection_buttons
            disabled = running or (needs_selection and not has_selection)
            try:
                button.configure(state="disabled" if disabled else "normal")
            except tk.TclError:
                pass

    def _on_tree_select(self, _event: tk.Event) -> None:
        selection = self.tree.selection()
        if not selection:
            self._update_message_buttons()
            return
        index = int(selection[0])
        if not 0 <= index < len(self.items):
            self._update_message_buttons()
            return
        item = self.items[index]
        self.msg_text.delete("1.0", "end")
        self.msg_text.insert("1.0", item.text)
        self.msg_weight_var.set(str(item.weight))
        self._update_message_buttons()

    def _read_editor(self) -> tuple[str, int]:
        text = " ".join(self.msg_text.get("1.0", "end").split())
        try:
            weight = int(self.msg_weight_var.get())
        except ValueError:
            weight = 1
        weight = max(0, weight)
        if len(text) > MAX_DANMAKU_LENGTH:
            log.warning("这条弹幕有 %d 个字，超过 B 站 %d 字上限：%s", len(text), MAX_DANMAKU_LENGTH, text)
        return text, weight

    def _selected_index(self) -> int | None:
        selection = self.tree.selection()
        if not selection:
            return None
        index = int(selection[0])
        return index if 0 <= index < len(self.items) else None

    def _on_message_add(self) -> None:
        text, weight = self._read_editor()
        if not text:
            messagebox.showwarning("内容为空", "请先输入弹幕内容。")
            return
        self.items.append(MessageItem(text, weight))
        self._refresh_message_tree()
        self._clear_editor()
        log.info("新增弹幕：%s（权重 %d）", text, weight)

    def _on_message_update(self) -> None:
        index = self._selected_index()
        if index is None:
            messagebox.showinfo("未选中", "请先在列表里选中一条弹幕。")
            return
        text, weight = self._read_editor()
        if not text:
            messagebox.showwarning("内容为空", "请先输入弹幕内容。")
            return
        self.items[index] = MessageItem(text, weight)
        self._refresh_message_tree()
        self.tree.selection_set(str(index))
        log.info("已更新第 %d 条弹幕：%s（权重 %d）", index + 1, text, weight)

    def _on_message_delete(self) -> None:
        index = self._selected_index()
        if index is None:
            messagebox.showinfo("未选中", "请先在列表里选中一条弹幕。")
            return
        item = self.items[index]
        if not messagebox.askyesno("删除弹幕", f"确定删除这条弹幕吗？\n\n{item.text}"):
            return
        del self.items[index]
        self._refresh_message_tree()
        self._clear_editor()
        log.info("已删除弹幕：%s", item.text)

    def _clear_editor(self) -> None:
        self.msg_text.delete("1.0", "end")
        self.msg_weight_var.set("1")
        self.tree.selection_remove(*self.tree.selection())

    def _load_messages_into_state(self, initial: bool = False) -> bool:
        path = self._current_messages_path()
        try:
            items = load_messages(path)
        except MessageFileError as exc:
            self.items = []
            self.tree.delete(*self.tree.get_children())
            self.msg_summary_var.set(f"消息加载失败：{exc}")
            log.warning("消息文件加载失败：%s", exc)
            if not initial:
                messagebox.showerror("读取消息失败", str(exc))
            return False
        self.items = items
        self.cfg.messages_file = self._store_path(path)
        self._loaded_messages_path = path
        self._refresh_message_tree()
        log.info("已加载 %d 条弹幕消息：%s", len(items), path)
        return True

    def _reload_messages_clicked(self) -> None:
        self._load_messages_into_state()

    def _browse_messages(self) -> None:
        path = filedialog.askopenfilename(
            title="选择消息 JSON 文件",
            initialdir=str(APP_DIR / "messages"),
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        self.msg_path_var.set(self._store_path(Path(path)))
        self._load_messages_into_state()

    def _save_messages_clicked(self) -> None:
        path = self._current_messages_path()
        try:
            save_messages(path, self.items)
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self.cfg.messages_file = self._store_path(path)
        self._loaded_messages_path = path
        self._refresh_message_tree()
        success(log, "已保存 %d 条弹幕到 %s", len(self.items), path)

    def _save_messages_as(self) -> None:
        path = filedialog.asksaveasfilename(
            title="另存为 JSON 文件",
            initialdir=str(APP_DIR / "messages"),
            defaultextension=".json",
            filetypes=[("JSON 文件", "*.json")],
        )
        if not path:
            return
        try:
            save_messages(Path(path), self.items)
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self.msg_path_var.set(self._store_path(Path(path)))
        self.cfg.messages_file = self._store_path(Path(path))
        self._loaded_messages_path = Path(path).resolve()
        self._refresh_message_tree()
        success(log, "已另存为 %s", path)

    def _import_messages(self) -> None:
        path = filedialog.askopenfilename(
            title="导入消息 JSON 文件",
            initialdir=str(APP_DIR / "messages"),
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        if self.items and not messagebox.askyesno("导入消息", "导入会替换当前消息列表，继续吗？"):
            return
        try:
            items = load_messages(Path(path))
        except MessageFileError as exc:
            messagebox.showerror("导入失败", str(exc))
            return
        self.items = items
        self.msg_path_var.set(self._store_path(Path(path)))
        self.cfg.messages_file = self._store_path(Path(path))
        self._loaded_messages_path = Path(path).resolve()
        self._refresh_message_tree()
        success(log, "已导入 %d 条弹幕：%s", len(items), path)

    @staticmethod
    def _open_folder() -> None:
        try:
            os.startfile(str(APP_DIR))  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("打开失败", str(exc))

    # ------------------------------------------------------------ 启停 / 测试
    def _on_start(self) -> None:
        if self.runner is not None and self.runner.running:
            return
        if not self._collect_config():
            return

        warnings = cookie_warnings(self.cfg.cookie)
        if warnings:
            messagebox.showwarning("Cookie 不完整", "\n".join(warnings) + "\n\n请更新 Cookie 后再开始发送。")
            return

        if not self.items:
            if not self._load_messages_into_state():
                return
        elif self._loaded_messages_path != self._current_messages_path():
            if not self._load_messages_into_state():
                return

        try:
            self.pool = MessagePool(self.items, mode=self.cfg.mode, avoid_repeat=self.cfg.avoid_repeat)
        except ValueError as exc:
            messagebox.showerror("没有可用弹幕", str(exc))
            return

        try:
            save_config(self.cfg.storage_copy())
        except OSError as exc:
            log.warning("保存配置失败：%s", exc)

        self._stats = {"total": 0, "ok": 0, "failed": 0}
        self._next_send_at = None
        self._refresh_stats()

        self.runner = DanmakuRunner(self.cfg.copy(), self.pool, self.events)
        self.runner.start()
        self._set_running_ui(True)
        self.state_var.set("启动中…")
        self.state_label.configure(style="Status.TLabel")
        log.info(
            "正在启动发送：房间 %s，间隔 %s 秒，%s",
            self.cfg.room_id,
            _format_number(self.cfg.interval_seconds),
            "按权重随机" if self.cfg.mode == MODE_WEIGHTED else "完全随机",
        )

    def _on_stop(self) -> None:
        if self.runner is None:
            return
        self.runner.stop()
        self.stop_btn.configure(state="disabled")
        self.state_var.set("正在停止…")
        log.info("已请求停止发送，等待当前请求结束…")

    def _on_test_send(self) -> None:
        if not self._collect_config():
            return
        warnings = cookie_warnings(self.cfg.cookie)
        if warnings:
            messagebox.showwarning("Cookie 不完整", "\n".join(warnings))
            return

        text, _weight = self._read_editor()
        if not text and self.items:
            text = self.items[0].text
        if not text:
            messagebox.showinfo("没有内容", "请先在「消息库」里输入或选中一条弹幕。")
            return

        self.test_btn.configure(state="disabled")
        log.info("测试发送：%s", text)
        cfg = self.cfg.copy()

        def worker() -> None:
            try:
                client = BilibiliClient(cfg.cookie, cfg.timeout_seconds)
                status = client.check_login()
                if status.known and not status.logged_in:
                    log.error("测试发送未执行：%s", status.message)
                    self.events.put({"kind": "test_done", "ok": False})
                    return
                room_id = cfg.room_id
                try:
                    room_id = client.resolve_room(cfg.room_id).room_id
                except BilibiliError:
                    pass
                result = client.send_danmaku(
                    text, room_id, color=cfg.color, fontsize=cfg.fontsize, mode=cfg.danmaku_mode
                )
                if result.ok:
                    success(log, "测试发送成功（房间 %s）：%s", room_id, text)
                else:
                    log.error("测试发送失败：%s", result.detail)
                self.events.put({"kind": "test_done", "ok": result.ok})
            except Exception:  # noqa: BLE001 - 后台线程异常要显示出来
                log.exception("测试发送出现未预期错误")
                self.events.put({"kind": "test_done", "ok": False})

        threading.Thread(target=worker, name="test-send", daemon=True).start()

    def _on_detect_room(self) -> None:
        room_text = self.room_var.get().strip()
        if not room_text.isdigit() or int(room_text) <= 0:
            messagebox.showerror("房间号无效", "房间号必须是正整数，例如 22676119。")
            return
        cookie = self._cookie_text_now()
        if not cookie:
            messagebox.showinfo("缺少 Cookie", "查询直播间信息也需要 Cookie，请先填写。")
            return

        self.room_info_var.set("正在查询直播间…")
        self.detect_btn.configure(state="disabled")
        room_id = int(room_text)

        def worker() -> None:
            try:
                client = BilibiliClient(cookie, timeout=8.0)
                status = client.check_login()
                if status.known and status.logged_in:
                    log.info("当前登录账号：%s", status.display)
                elif status.known:
                    log.warning("当前 Cookie 未登录：%s", status.message)
                room = client.resolve_room(room_id)
            except BilibiliError as exc:
                self.events.put({"kind": "room_error", "message": str(exc)})
                self.events.put({"kind": "detect_done"})
                return
            except Exception:  # noqa: BLE001 - 后台线程异常要显示出来
                log.exception("查询直播间出现未预期错误")
                self.events.put({"kind": "room_error", "message": "查询异常，详见运行日志"})
                self.events.put({"kind": "detect_done"})
                return
            self.events.put({"kind": "room", "room": room})
            self.events.put({"kind": "detect_done"})

        threading.Thread(target=worker, name="room-detect", daemon=True).start()

    # ------------------------------------------------------------- 事件与状态
    def _pump(self) -> None:
        try:
            while True:
                record = LOG_QUEUE.get_nowait()
                self._append_log(record.get("level", "info"), record.get("message", ""))
        except queue.Empty:
            pass

        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass

        self._tick_countdown()
        self.after(POLL_MS, self._pump)

    def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "status":
            self._on_runner_status(event)
        elif kind == "room":
            room: RoomInfo = event["room"]
            self._show_room(room)
        elif kind == "room_error":
            self.room_info_var.set(f"查询失败：{event.get('message', '未知错误')}")
            self.detect_btn.configure(state="normal")
            log.warning("查询直播间失败：%s", event.get("message"))
        elif kind == "detect_done":
            self.detect_btn.configure(state="normal")
        elif kind == "progress":
            self._stats = {
                "total": int(event.get("total", 0)),
                "ok": int(event.get("ok", 0)),
                "failed": int(event.get("failed", 0)),
            }
            self._refresh_stats()
        elif kind == "schedule":
            self._next_send_at = event.get("next_at")
        elif kind == "test_done":
            self.test_btn.configure(state="normal")

    def _on_runner_status(self, event: dict[str, Any]) -> None:
        state = event.get("state")
        if state == "starting":
            self.state_var.set("启动中…")
        elif state == "running":
            self.state_var.set("运行中")
            self.state_label.configure(style="Good.TLabel")
            self._set_running_ui(True)
        elif state == "stopped":
            self._next_send_at = None
            self._stats = {
                "total": int(event.get("total", self._stats["total"])),
                "ok": int(event.get("ok", self._stats["ok"])),
                "failed": int(event.get("failed", self._stats["failed"])),
            }
            self._refresh_stats()
            self.state_var.set("已停止")
            self.state_label.configure(style="Status.TLabel")
            self._set_running_ui(False)
            message = event.get("message")
            if message:
                self.state_var.set("已停止（异常）")
                self.state_label.configure(style="Bad.TLabel")
                messagebox.showerror("发送已停止", message)
        elif state == "error":
            self.state_var.set("已停止（异常）")
            self.state_label.configure(style="Bad.TLabel")
            self._set_running_ui(False)

    def _show_room(self, room: RoomInfo) -> None:
        parts = [f"房间 {room.room_id}"]
        if room.short_id and room.short_id != room.room_id:
            parts.append(f"短号 {room.short_id}")
        parts.append(room.live_text)
        if room.title:
            parts.append(f"「{room.title}」")
        if room.anchor:
            parts.append(f"主播 {room.anchor}")
        if room.area:
            parts.append(f"分区 {room.area}")
        if room.online:
            parts.append(f"人气 {room.online}")
        self.room_info_var.set(" · ".join(parts))
        self.status_room_var.set(f"直播间 {room.room_id} · {room.live_text}")
        self.cfg.room_title = room.title
        self.cfg.anchor_name = room.anchor
        success(log, "已连接直播间 %s（%s · %s）", room.room_id, room.title or "标题未知", room.live_text)

    def _tick_countdown(self) -> None:
        if self.runner is not None and self.runner.running:
            if self._next_send_at:
                remaining = max(0.0, self._next_send_at - time.monotonic())
                self.countdown_var.set(f"下次发送：{remaining:.0f} 秒后")
            else:
                self.countdown_var.set("下次发送：准备中…")
        else:
            self.countdown_var.set("下次发送：—")

    def _refresh_stats(self) -> None:
        self.stats_var.set(
            f"已发送 {self._stats['total']} · 成功 {self._stats['ok']} · 失败 {self._stats['failed']}"
        )

    def _set_running_ui(self, running: bool) -> None:
        self._running = running
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.test_btn.configure(state="disabled" if running else "normal")
        for widget in self._lockable:
            if not isinstance(widget, (tk.Text, ttk.Widget)):
                continue
            if widget is self.cookie_text:
                state = "normal" if not running and not self._cookie_hidden else "disabled"
            elif running:
                state = "disabled"
            elif widget is self.position_combo:
                state = "readonly"
            else:
                state = "normal"
            try:
                widget.configure(state=state)
            except tk.TclError:
                continue
        self._update_message_buttons()

    # ------------------------------------------------------------------ 日志
    def _append_log(self, level: str, message: str) -> None:
        tag = {
            "debug": "info",
            "info": "info",
            "success": "success",
            "warning": "warn",
            "warn": "warn",
            "error": "error",
            "critical": "error",
        }.get(level, "info")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] ", ("time",))
        self.log_text.insert("end", message + "\n", (tag,))
        self.log_text.configure(state="disabled")
        if self.autoscroll_var.get():
            self.log_text.see("end")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _export_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="导出运行日志",
            initialdir=str(APP_DIR),
            initialfile=f"bili_send_{time.strftime('%Y%m%d_%H%M%S')}.log",
            defaultextension=".log",
            filetypes=[("日志文件", "*.log"), ("文本文件", "*.txt")],
        )
        if not path:
            return
        try:
            Path(path).write_text(self.log_text.get("1.0", "end"), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc))
            return
        success(log, "日志已导出：%s", path)

    # ------------------------------------------------------------------ 收尾
    def _on_callback_error(self, exc_type: type, exc_value: BaseException, tb: Any) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, tb))
        log.error("界面出现未捕获异常：\n%s", text)
        messagebox.showerror("程序异常", f"界面出现未捕获异常：\n{exc_value}\n\n详情见运行日志。")

    def _on_close(self) -> None:
        if self.runner is not None and self.runner.running:
            if not messagebox.askyesno("退出", "弹幕还在发送中，确定要停止并退出吗？"):
                return
            self.runner.stop()
            self.runner.join(3.0)
        self._collect_config(silent=True)
        try:
            save_config(self.cfg.storage_copy())
        except OSError as exc:
            log.warning("保存配置失败：%s", exc)
        self.destroy()


def _format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def run() -> None:
    """启动界面主循环。"""
    app = BiliSendApp()
    app.mainloop()
