"""Tkinter 图形界面（v1.2：多直播间 / 权限 / 表情 / 说书人）。

页面：
* 发送：当前任务的房间、Cookie、间隔、来源、颜色/模式、单条弹幕；
* 多直播间：任务列表，逐任务或全部启动/停止；
* 表情：当前直播间的专属表情，显示权限并缓存，可加入弹幕池；
* 说书人：小说分段、进度；
* 消息库、运行日志。
"""

from __future__ import annotations

import base64
import logging
import os
import queue
import re
import shutil
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any

from .client import (
    BilibiliClient,
    BilibiliError,
    DanmakuConfig,
    DanmakuOption,
    Emoticon,
    HistoryDanmaku,
    LoginStatus,
    RoomInfo,
    cookie_warnings,
    parse_cookie,
)
from .config import (
    APP_DIR,
    CACHE_DIR,
    DEFAULT_MESSAGES_FILE,
    MAX_COLOR,
    MAX_FONT_SIZE,
    MAX_INTERVAL_SECONDS,
    MAX_NOVEL_CHUNK,
    MIN_FONT_SIZE,
    MIN_INTERVAL_SECONDS,
    MIN_NOVEL_CHUNK,
    MODE_UNIFORM,
    MODE_WEIGHTED,
    SOURCE_LABELS,
    SOURCE_NOVEL,
    SOURCE_POOL,
    AppConfig,
    TaskConfig,
    load_config,
    resolve_path,
    save_config,
)
from .emoticons import EmoticonCache
from .logbus import LOG_QUEUE, success
from .messages import (
    MAX_DANMAKU_LENGTH,
    MessageFileError,
    MessageItem,
    MessagePool,
    load_messages,
    save_messages,
)
from .novel import MAX_CHUNK, NovelBook, NovelError, load_book, preview
from .runner import DanmakuRunner
from .sources import MessageSource, NovelSource, PoolSource

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

POSITION_NAMES = {1: "滚动", 4: "底部", 5: "顶部"}
POLL_MS = 120
TASK_REFRESH_EVERY = 4  # 每 4 次轮询刷新一次任务表（约 0.5 秒）
HISTORY_POLL_SECONDS = 2.0
HISTORY_MAX_ROWS = 500


@dataclass
class Runtime:
    runner: DanmakuRunner
    source: MessageSource
    status: str = "starting"
    total: int = 0
    ok: int = 0
    failed: int = 0
    progress: str = ""
    next_at: float | None = None


class BiliSendApp(tk.Tk):
    """主窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self.cfg: AppConfig = load_config()
        self.cfg.active_task  # 确保至少有一个任务
        self.events: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.runtimes: dict[str, Runtime] = {}
        self.items: list[MessageItem] = []
        self._loaded_messages_path: Path | None = None
        self._cookie_hidden = True
        self._cookie_value = ""
        self._pump_id: str | None = None
        self._poll_count = 0
        self._task_ids: list[str] = []
        self._suspend_task_event = False
        self._dm_cache: dict[tuple[str, int, str], DanmakuConfig] = {}
        self._color_options: list[DanmakuOption] = []
        self._mode_options: list[DanmakuOption] = []
        self._emoticon_packages: list[Any] = []
        self._emoticon_images: dict[str, Path] = {}
        self._emote_photos: list[tk.PhotoImage] = []
        self._emote_emoticons: list[Emoticon] = []
        self._collapsed_packages: set[int] = set()
        self._editor_emoticon: Emoticon | None = None
        self._single_emoticon: Emoticon | None = None
        self._emote_room_id = 0
        self._emote_task_id = ""
        self._emote_photo: tk.PhotoImage | None = None
        self._single_emote_photo: tk.PhotoImage | None = None
        self._log_photos: list[tk.PhotoImage] = []
        self._history_items: list[HistoryDanmaku] = []
        self._history_task_id = ""
        self._history_keys: set[tuple[str, str, str]] = set()
        self._history_live = False
        self._history_stop: threading.Event | None = None
        self._history_thread: threading.Thread | None = None
        self._novel_book: NovelBook | None = None
        self._novel_task_id: str | None = None
        self._running_ui = False
        self.report_callback_exception = self._on_callback_error  # type: ignore[assignment]

        self._setup_window()
        self._setup_style()
        self._build_ui()
        self._load_active_task_into_widgets()
        self._load_messages_into_state(initial=True)
        self._refresh_task_tree()
        self._refresh_task_selector()
        self._refresh_cookie_status()
        self._reset_permission_ui()
        self._refresh_stats()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._pump_id = self.after(POLL_MS, self._pump)
        log.info("界面已就绪，配置目录：%s", APP_DIR)

    # ---------------------------------------------------------------- 窗口与样式
    def _setup_window(self) -> None:
        self.title("BILIBILI_SEND · 直播弹幕助手（Python 版）")
        width, height = 1180, 900
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 2 - 20)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(1000, 740)

    def _setup_style(self) -> None:
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
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
        style.configure("Status.TLabel", padding=(6, 2))
        style.configure("Treeview", rowheight=26)

    # ------------------------------------------------------------------ 布局
    def _build_ui(self) -> None:
        self._build_statusbar()
        container = ttk.Frame(self, padding=(12, 10, 12, 6))
        container.pack(fill="both", expand=True)
        self.notebook = ttk.Notebook(container)
        self.notebook.pack(fill="both", expand=True)
        self.tab_send = ttk.Frame(self.notebook, padding=10)
        self.tab_tasks = ttk.Frame(self.notebook, padding=10)
        self.tab_emoticons = ttk.Frame(self.notebook, padding=10)
        self.tab_novel = ttk.Frame(self.notebook, padding=10)
        self.tab_messages = ttk.Frame(self.notebook, padding=10)
        self.tab_history = ttk.Frame(self.notebook, padding=10)
        self.tab_log = ttk.Frame(self.notebook, padding=10)
        for tab, text in (
            (self.tab_send, "  发送  "),
            (self.tab_tasks, "  多直播间  "),
            (self.tab_emoticons, "  表情  "),
            (self.tab_novel, "  说书人  "),
            (self.tab_messages, "  消息库  "),
            (self.tab_history, "  直播间弹幕  "),
            (self.tab_log, "  运行日志  "),
        ):
            self.notebook.add(tab, text=text)
        self._build_send_tab()
        self._build_tasks_tab()
        self._build_emoticons_tab()
        self._build_novel_tab()
        self._build_messages_tab()
        self._build_history_tab()
        self._build_log_tab()
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

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

    # ------------------------------------------------------------------ 发送页
    def _build_send_tab(self) -> None:
        tab = self.tab_send
        tab.columnconfigure(0, weight=1)

        task_bar = ttk.Frame(tab)
        task_bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(task_bar, text="当前任务").pack(side="left")
        self.task_combo = ttk.Combobox(task_bar, state="readonly", width=26)
        self.task_combo.pack(side="left", padx=(8, 6))
        self.task_combo.bind("<<ComboboxSelected>>", self._on_task_selected)
        for text, command in (
            ("新建", self._on_task_new),
            ("复制", self._on_task_copy),
            ("删除", self._on_task_delete),
        ):
            ttk.Button(task_bar, text=text, command=command).pack(side="left", padx=(0, 6))
        ttk.Button(task_bar, text="全部停止", command=self._on_stop_all).pack(side="right")
        ttk.Button(task_bar, text="全部启动", command=self._on_start_all).pack(side="right", padx=(0, 6))

        room_box = ttk.LabelFrame(tab, text=" 直播间 ", padding=10)
        room_box.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        room_box.columnconfigure(3, weight=1)
        ttk.Label(room_box, text="任务名").grid(row=0, column=0, sticky="w")
        self.task_name_var = tk.StringVar()
        self.task_name_entry = ttk.Entry(room_box, textvariable=self.task_name_var, width=18)
        self.task_name_entry.grid(row=0, column=1, padx=(6, 16), sticky="w")
        ttk.Label(room_box, text="房间号").grid(row=0, column=2, sticky="w")
        self.room_var = tk.StringVar()
        self.room_entry = ttk.Entry(room_box, textvariable=self.room_var, width=14)
        self.room_entry.grid(row=0, column=3, padx=(6, 8), sticky="w")
        self.detect_btn = ttk.Button(room_box, text="检测直播间 / 权限", command=self._on_detect_room)
        self.detect_btn.grid(row=0, column=4, sticky="w")
        self.room_info_var = tk.StringVar(value="检测后会显示主播、开播状态，以及当前账号可用的弹幕颜色/模式。")
        ttk.Label(room_box, textvariable=self.room_info_var, style="Muted.TLabel", wraplength=1050, justify="left").grid(
            row=1, column=0, columnspan=5, sticky="w", pady=(6, 0)
        )
        self.perm_var = tk.StringVar(value="权限：未检测")
        ttk.Label(room_box, textvariable=self.perm_var, style="Muted.TLabel", wraplength=1050, justify="left").grid(
            row=2, column=0, columnspan=5, sticky="w", pady=(2, 0)
        )

        cookie_box = ttk.LabelFrame(tab, text=" 账号 Cookie（当前任务独立） ", padding=10)
        cookie_box.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        cookie_box.columnconfigure(0, weight=1)
        text_frame = ttk.Frame(cookie_box)
        text_frame.grid(row=0, column=0, sticky="ew")
        text_frame.columnconfigure(0, weight=1)
        self.cookie_entry = ttk.Entry(text_frame, show="•", font=("Consolas", 10))
        self.cookie_entry.grid(row=0, column=0, sticky="ew")
        self.cookie_entry.bind("<KeyRelease>", lambda _e: self._refresh_cookie_status())
        self.cookie_entry.bind("<FocusOut>", lambda _e: self._on_cookie_focus_out())
        self.cookie_entry.bind("<<Paste>>", lambda _e: self.after_idle(self._on_cookie_pasted))
        self.cookie_text = tk.Text(
            text_frame, height=4, wrap="char", relief="solid", borderwidth=1,
            font=("Consolas", 9), state="disabled",
        )
        self.cookie_text.grid(row=0, column=0, sticky="ew")
        self.cookie_scroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.cookie_text.yview)
        self.cookie_text.configure(yscrollcommand=self.cookie_scroll.set)
        self.cookie_scroll.grid(row=0, column=1, sticky="ns")
        self.cookie_text.grid_remove()
        self.cookie_scroll.grid_remove()
        self.cookie_text.bind("<KeyRelease>", lambda _e: self._refresh_cookie_status())
        self.cookie_text.bind("<FocusOut>", lambda _e: self._on_cookie_focus_out())
        self.cookie_text.bind("<<Paste>>", lambda _e: self.after_idle(self._on_cookie_pasted))

        options = ttk.Frame(cookie_box)
        options.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.show_cookie_var = tk.BooleanVar(value=False)
        self.show_cookie_check = ttk.Checkbutton(
            options, text="显示 Cookie", variable=self.show_cookie_var, command=self._on_toggle_show_cookie
        )
        self.show_cookie_check.pack(side="left")
        self.save_cookie_var = tk.BooleanVar(value=True)
        self.save_cookie_check = ttk.Checkbutton(
            options, text="保存所有任务 Cookie 到 config.json", variable=self.save_cookie_var,
            command=self._on_toggle_save_cookie,
        )
        self.save_cookie_check.pack(side="left", padx=(16, 0))
        ttk.Label(options, text="打码时也能直接输入/粘贴", style="Muted.TLabel").pack(side="left", padx=(12, 0))

        tools = ttk.Frame(cookie_box)
        tools.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(tools, text="粘贴剪贴板", command=self._paste_cookie).pack(side="left")
        ttk.Button(tools, text="清空", command=self._clear_cookie).pack(side="left", padx=6)
        self.cookie_status_var = tk.StringVar()
        self.cookie_status_label = ttk.Label(tools, textvariable=self.cookie_status_var, style="Muted.TLabel", wraplength=760)
        self.cookie_status_label.pack(side="left", padx=(12, 0))

        set_box = ttk.LabelFrame(tab, text=" 发送设置 ", padding=10)
        set_box.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        set_box.columnconfigure(1, weight=1)
        set_box.columnconfigure(6, weight=1)

        ttk.Label(set_box, text="内容来源").grid(row=0, column=0, sticky="w")
        source_frame = ttk.Frame(set_box)
        source_frame.grid(row=0, column=1, columnspan=2, sticky="w", padx=(6, 0))
        self.source_var = tk.StringVar(value=SOURCE_POOL)
        self.source_pool_radio = ttk.Radiobutton(
            source_frame, text="弹幕池", value=SOURCE_POOL, variable=self.source_var, command=self._on_source_changed
        )
        self.source_pool_radio.pack(side="left")
        self.source_novel_radio = ttk.Radiobutton(
            source_frame, text="说书人", value=SOURCE_NOVEL, variable=self.source_var, command=self._on_source_changed
        )
        self.source_novel_radio.pack(side="left", padx=(10, 0))

        ttk.Label(set_box, text="发送间隔（秒）").grid(row=0, column=3, sticky="w")
        self.interval_var = tk.StringVar()
        self.interval_spin = ttk.Spinbox(set_box, from_=1, to=3600, width=8, textvariable=self.interval_var)
        self.interval_spin.grid(row=0, column=4, sticky="w", padx=(6, 16))
        self.interval_spin.bind("<FocusOut>", lambda _e: self._normalize_interval())
        self.interval_spin.bind("<Return>", lambda _e: self._normalize_interval())

        ttk.Label(set_box, text="抽取方式").grid(row=0, column=5, sticky="e")
        mode_frame = ttk.Frame(set_box)
        mode_frame.grid(row=0, column=6, sticky="w", padx=(6, 0))
        self.mode_var = tk.StringVar(value=MODE_WEIGHTED)
        self.mode_weighted_radio = ttk.Radiobutton(
            mode_frame, text="按权重", value=MODE_WEIGHTED, variable=self.mode_var
        )
        self.mode_weighted_radio.pack(side="left")
        self.mode_uniform_radio = ttk.Radiobutton(
            mode_frame, text="完全随机", value=MODE_UNIFORM, variable=self.mode_var
        )
        self.mode_uniform_radio.pack(side="left", padx=(8, 0))

        self.pool_frame = ttk.Frame(set_box)
        self.pool_frame.grid(row=1, column=0, columnspan=7, sticky="ew", pady=(8, 0))
        self.pool_frame.columnconfigure(1, weight=1)
        ttk.Label(self.pool_frame, text="消息文件").grid(row=0, column=0, sticky="w")
        self.messages_path_var = tk.StringVar()
        self.messages_entry = ttk.Entry(self.pool_frame, textvariable=self.messages_path_var)
        self.messages_entry.grid(row=0, column=1, sticky="ew", padx=(6, 6))
        ttk.Button(self.pool_frame, text="浏览…", command=self._browse_messages).grid(row=0, column=2)
        ttk.Button(self.pool_frame, text="重新加载", command=self._reload_messages_clicked).grid(row=0, column=3, padx=(6, 0))

        self.novel_frame = ttk.Frame(set_box)
        self.novel_frame.grid(row=1, column=0, columnspan=7, sticky="ew", pady=(8, 0))
        self.novel_frame.columnconfigure(1, weight=1)
        ttk.Label(self.novel_frame, text="小说文件").grid(row=0, column=0, sticky="w")
        self.novel_path_var = tk.StringVar()
        self.novel_path_entry = ttk.Entry(self.novel_frame, textvariable=self.novel_path_var)
        self.novel_path_entry.grid(row=0, column=1, sticky="ew", padx=(6, 6))
        ttk.Button(self.novel_frame, text="选择…", command=self._browse_novel).grid(row=0, column=2)
        self.novel_progress_var = tk.StringVar(value="进度：未加载")
        ttk.Label(self.novel_frame, textvariable=self.novel_progress_var, style="Muted.TLabel").grid(
            row=0, column=3, padx=(12, 0)
        )
        self.novel_frame.grid_remove()

        ttk.Label(set_box, text="弹幕颜色").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.color_var = tk.StringVar()
        self.color_combo = ttk.Combobox(set_box, textvariable=self.color_var, width=20)
        self.color_combo.grid(row=2, column=1, sticky="w", padx=(6, 16), pady=(8, 0))
        ttk.Label(set_box, text="字号").grid(row=2, column=2, sticky="e", pady=(8, 0))
        self.font_var = tk.StringVar()
        self.font_spin = ttk.Spinbox(set_box, from_=MIN_FONT_SIZE, to=MAX_FONT_SIZE, width=6, textvariable=self.font_var)
        self.font_spin.grid(row=2, column=3, sticky="w", padx=(6, 16), pady=(8, 0))
        ttk.Label(set_box, text="位置").grid(row=2, column=4, sticky="e", pady=(8, 0))
        self.position_var = tk.StringVar()
        self.position_combo = ttk.Combobox(set_box, textvariable=self.position_var, width=14, state="readonly")
        self.position_combo.grid(row=2, column=5, columnspan=2, sticky="w", padx=(6, 0), pady=(8, 0))
        self.avoid_repeat_var = tk.BooleanVar(value=True)
        self.avoid_repeat_check = ttk.Checkbutton(
            set_box, text="避免连续两条相同弹幕", variable=self.avoid_repeat_var
        )
        self.avoid_repeat_check.grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))

        actions = ttk.Frame(tab)
        actions.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        self.start_btn = tk.Button(
            actions, text="▶  开始发送", command=self._on_start_active, bg="#16a34a", fg="white",
            activebackground="#15803d", activeforeground="white", disabledforeground="#d1d5db",
            relief="flat", padx=18, pady=6, font=("Microsoft YaHei UI", 10, "bold"), cursor="hand2",
        )
        self.start_btn.pack(side="left")
        self.stop_btn = tk.Button(
            actions, text="■  停止", command=self._on_stop_active, bg="#dc2626", fg="white",
            activebackground="#b91c1c", activeforeground="white", disabledforeground="#d1d5db",
            relief="flat", padx=18, pady=6, font=("Microsoft YaHei UI", 10, "bold"),
            state="disabled", cursor="hand2",
        )
        self.stop_btn.pack(side="left", padx=8)
        self.test_btn = ttk.Button(actions, text="发送一条测试", command=self._on_test_send)
        self.test_btn.pack(side="left")

        single = ttk.LabelFrame(tab, text=" 单条弹幕（自定义内容，不影响弹幕池） ", padding=8)
        single.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        single.columnconfigure(0, weight=1)
        self.single_var = tk.StringVar()
        self.single_entry = ttk.Entry(single, textvariable=self.single_var)
        self.single_entry.grid(row=0, column=0, sticky="ew")
        self.single_entry.bind("<KeyRelease>", lambda _e: self._refresh_single_count())
        self.single_count_var = tk.StringVar(value=f"0/{MAX_DANMAKU_LENGTH}")
        ttk.Label(single, textvariable=self.single_count_var, style="Muted.TLabel").grid(row=0, column=1, padx=(8, 8))
        self.single_btn = ttk.Button(single, text="发送", command=self._on_single_send)
        self.single_btn.grid(row=0, column=2)

        self.single_emote_var = tk.StringVar(value="未选择表情")
        emote_info = ttk.Frame(single)
        emote_info.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.single_emote_thumb = ttk.Label(emote_info)
        self.single_emote_thumb.pack(side="left")
        ttk.Label(emote_info, textvariable=self.single_emote_var, style="Muted.TLabel").pack(
            side="left", padx=(6, 0)
        )
        emote_actions = ttk.Frame(single)
        emote_actions.grid(row=1, column=1, columnspan=2, sticky="e", pady=(6, 0))
        ttk.Button(emote_actions, text="选择表情…", command=self._choose_single_emoticon).pack(side="left")
        ttk.Button(emote_actions, text="清除表情", command=lambda: self._set_single_emoticon(None)).pack(
            side="left", padx=(6, 0)
        )

        self.task_hint_var = tk.StringVar()
        ttk.Label(tab, textvariable=self.task_hint_var, style="Muted.TLabel", wraplength=1060, justify="left").grid(
            row=6, column=0, sticky="w", pady=(8, 0)
        )

        tips = ttk.LabelFrame(tab, text=" 小贴士 ", padding=8)
        tips.grid(row=7, column=0, sticky="ew", pady=(8, 0))
        for line in (
            "每个任务有独立 Cookie，可多账号 / 多直播间同时发送；「全部启动」会为每个任务开一个线程。",
            "顶部 / 底部弹幕和彩色弹幕通常需要舰长或房管身份，发送前会自动检测并退回可用项。",
            "说书人每成功发送一段就保存进度，关闭程序后再点「开始发送」会从上次的位置继续。",
            "直播间专属表情需要登录后拉取，按接口返回的 perm 标记可用 / 锁定，可一键加入弹幕池。",
        ):
            ttk.Label(tips, text="• " + line, style="Muted.TLabel", wraplength=1060, justify="left").pack(anchor="w")

        self._send_lockable = [
            self.task_name_entry, self.room_entry, self.detect_btn, self.cookie_entry, self.cookie_text,
            self.interval_spin, self.messages_entry, self.color_combo, self.font_spin,
            self.position_combo, self.avoid_repeat_check, self.source_pool_radio, self.source_novel_radio,
            self.mode_weighted_radio, self.mode_uniform_radio, self.show_cookie_check,
        ]

    # ------------------------------------------------------------------ 任务页
    def _build_tasks_tab(self) -> None:
        tab = self.tab_tasks
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        head = ttk.Frame(tab)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(head, text="每个任务使用独立 Cookie，可多账号 / 多直播间同时发送。").pack(side="left")
        ttk.Button(head, text="刷新", command=self._refresh_task_tree).pack(side="right")

        columns = ("name", "room", "account", "source", "interval", "status", "stats", "progress")
        self.task_tree = ttk.Treeview(tab, columns=columns, show="headings", selectmode="browse")
        for column, title, width, anchor in (
            ("name", "任务", 150, "w"),
            ("room", "房间", 90, "center"),
            ("account", "账号", 90, "center"),
            ("source", "来源", 80, "center"),
            ("interval", "间隔", 65, "center"),
            ("status", "状态", 90, "center"),
            ("stats", "发送/成功/失败", 130, "center"),
            ("progress", "进度", 260, "w"),
        ):
            self.task_tree.heading(column, text=title)
            self.task_tree.column(column, width=width, anchor=anchor, stretch=(column == "progress"))
        self.task_tree.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(tab, orient="vertical", command=self.task_tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.task_tree.configure(yscrollcommand=scroll.set)
        self.task_tree.bind("<<TreeviewSelect>>", self._on_task_tree_select)
        self.task_tree.bind("<Double-1>", lambda _e: self._activate_selected_task())

        tools = ttk.Frame(tab)
        tools.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        for text, command in (
            ("新增任务", self._on_task_new),
            ("复制选中", self._on_task_copy),
            ("删除选中", self._on_task_delete),
            ("启动选中", self._on_start_selected),
            ("停止选中", self._on_stop_selected),
            ("全部启动", self._on_start_all),
            ("全部停止", self._on_stop_all),
        ):
            ttk.Button(tools, text=text, command=command).pack(side="left", padx=(0, 6))

    # ------------------------------------------------------------------ 表情页
    def _build_emoticons_tab(self) -> None:
        tab = self.tab_emoticons
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        head = ttk.Frame(tab)
        head.grid(row=0, column=0, sticky="ew")
        self.emote_header_var = tk.StringVar(value="当前任务：—")
        ttk.Label(head, textvariable=self.emote_header_var).pack(side="left")
        self.emote_fetch_btn = ttk.Button(head, text="拉取直播间表情", command=lambda: self._fetch_emoticons(False))
        self.emote_fetch_btn.pack(side="right")
        ttk.Button(head, text="强制刷新", command=lambda: self._fetch_emoticons(True)).pack(side="right", padx=(0, 6))
        self.emote_only_perm_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            head, text="只看可用", variable=self.emote_only_perm_var, command=self._render_emoticons_last
        ).pack(side="right", padx=(0, 12))

        self.emote_status_var = tk.StringVar(value="尚未拉取表情。拉取后会缓存到本地，下次秒开。")
        ttk.Label(tab, textvariable=self.emote_status_var, style="Muted.TLabel", wraplength=1080, justify="left").grid(
            row=1, column=0, sticky="w", pady=(6, 6)
        )

        body = ttk.Frame(tab)
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.emote_canvas = tk.Canvas(body, highlightthickness=0)
        self.emote_canvas.grid(row=0, column=0, sticky="nsew")
        emote_scroll = ttk.Scrollbar(body, orient="vertical", command=self.emote_canvas.yview)
        emote_scroll.grid(row=0, column=1, sticky="ns")
        self.emote_canvas.configure(yscrollcommand=emote_scroll.set)
        self.emote_inner = ttk.Frame(self.emote_canvas)
        self.emote_window = self.emote_canvas.create_window((0, 0), window=self.emote_inner, anchor="nw")
        self.emote_inner.bind(
            "<Configure>", lambda _e: self.emote_canvas.configure(scrollregion=self.emote_canvas.bbox("all"))
        )
        self.emote_canvas.bind(
            "<Configure>", lambda event: self.emote_canvas.itemconfigure(self.emote_window, width=event.width)
        )
        self.emote_canvas.bind_all("<MouseWheel>", self._on_emote_wheel)
        self.emote_hint_var = tk.StringVar(value="点击可用表情会加入弹幕池；锁定表情会显示解锁条件。")
        ttk.Label(tab, textvariable=self.emote_hint_var, style="Muted.TLabel").grid(
            row=3, column=0, sticky="w", pady=(6, 0)
        )

    def _on_emote_wheel(self, event: tk.Event) -> None:
        if self.notebook.index("current") == 2:
            self.emote_canvas.yview_scroll(int(-event.delta / 120), "units")

    # ------------------------------------------------------------------ 说书人页
    def _build_novel_tab(self) -> None:
        tab = self.tab_novel
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        head = ttk.Frame(tab)
        head.grid(row=0, column=0, sticky="ew")
        self.novel_header_var = tk.StringVar(value="当前任务：—")
        ttk.Label(head, textvariable=self.novel_header_var).pack(side="left")
        self.novel_size_var = tk.StringVar(value=str(MAX_CHUNK))
        ttk.Spinbox(
            head, from_=MIN_NOVEL_CHUNK, to=MAX_NOVEL_CHUNK, width=5, textvariable=self.novel_size_var
        ).pack(side="right")
        ttk.Label(head, text="每段字数").pack(side="right", padx=(8, 4))

        path_bar = ttk.Frame(tab)
        path_bar.grid(row=1, column=0, sticky="ew", pady=(8, 6))
        path_bar.columnconfigure(1, weight=1)
        ttk.Label(path_bar, text="小说").grid(row=0, column=0, sticky="w")
        self.novel_tab_path_var = tk.StringVar()
        ttk.Entry(path_bar, textvariable=self.novel_tab_path_var).grid(row=0, column=1, sticky="ew", padx=(6, 6))
        ttk.Button(path_bar, text="选择…", command=self._browse_novel).grid(row=0, column=2)
        ttk.Button(path_bar, text="加载 / 切分", command=self._load_novel_clicked).grid(row=0, column=3, padx=(6, 0))

        body = ttk.Frame(tab)
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.novel_tree = ttk.Treeview(body, columns=("idx", "text"), show="headings", selectmode="browse")
        self.novel_tree.heading("idx", text="#")
        self.novel_tree.heading("text", text="分段内容")
        self.novel_tree.column("idx", width=60, anchor="center", stretch=False)
        self.novel_tree.column("text", width=800, anchor="w", stretch=True)
        self.novel_tree.grid(row=0, column=0, sticky="nsew")
        novel_scroll = ttk.Scrollbar(body, orient="vertical", command=self.novel_tree.yview)
        novel_scroll.grid(row=0, column=1, sticky="ns")
        self.novel_tree.configure(yscrollcommand=novel_scroll.set)

        foot = ttk.Frame(tab)
        foot.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.novel_info_var = tk.StringVar(value="尚未加载小说")
        ttk.Label(foot, textvariable=self.novel_info_var, style="Muted.TLabel").pack(side="left")
        self.novel_bar = ttk.Progressbar(foot, length=220, maximum=100)
        self.novel_bar.pack(side="left", padx=(12, 12))
        ttk.Button(foot, text="从头开始", command=self._novel_restart).pack(side="right")
        ttk.Button(foot, text="从上次继续", command=self._novel_continue).pack(side="right", padx=(0, 6))
        ttk.Button(foot, text="设为当前任务内容", command=self._novel_set_source).pack(side="right", padx=(0, 6))

    # ------------------------------------------------------------------ 消息页
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
        self.tree = ttk.Treeview(body, columns=("idx", "text", "weight", "state"), show="headings", selectmode="browse")
        for column, title, width, anchor, stretch in (
            ("idx", "#", 50, "center", False),
            ("text", "弹幕内容", 520, "w", True),
            ("weight", "权重", 70, "center", False),
            ("state", "状态", 150, "center", False),
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
        self.msg_weight_spin = ttk.Spinbox(weight_row, from_=0, to=999, width=6, textvariable=self.msg_weight_var)
        self.msg_weight_spin.pack(side="left", padx=(8, 14))
        ttk.Label(weight_row, text="权重越大越常出现；0 表示暂不参与", style="Muted.TLabel").pack(side="left")

        self.editor_emote_row = ttk.Frame(editor)
        self.editor_emote_row.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        self.editor_emote_var = tk.StringVar()
        self.editor_emote_thumb = ttk.Label(self.editor_emote_row)
        self.editor_emote_thumb.pack(side="left")
        ttk.Label(self.editor_emote_row, textvariable=self.editor_emote_var, style="Muted.TLabel").pack(
            side="left", padx=(6, 0)
        )
        self.editor_emote_clear = ttk.Button(
            self.editor_emote_row, text="移除表情", command=self._clear_editor_emote
        )
        self.editor_emote_clear.pack(side="left", padx=(8, 0))
        self.editor_emote_row.grid_remove()

        button_bar = ttk.Frame(editor)
        button_bar.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        self.msg_buttons: list[ttk.Button] = []
        self._selection_buttons: list[ttk.Button] = []
        for text, command, needs_selection in (
            ("新增", self._on_message_add, False),
            ("更新选中", self._on_message_update, True),
            ("删除选中", self._on_message_delete, True),
            ("发送选中", self._on_message_send, True),
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

    # ------------------------------------------------------------------ 直播间弹幕
    def _build_history_tab(self) -> None:
        tab = self.tab_history
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        head = ttk.Frame(tab)
        head.grid(row=0, column=0, sticky="ew")
        self.history_header_var = tk.StringVar(value="当前任务：—")
        ttk.Label(head, textvariable=self.history_header_var).pack(side="left")
        self.history_status_var = tk.StringVar(value="打开此页会自动开始实时滚动。")
        ttk.Label(head, textvariable=self.history_status_var, style="Muted.TLabel").pack(side="left", padx=12)
        self.history_fetch_btn = ttk.Button(head, text="开始实时", command=self._toggle_history_live)
        self.history_fetch_btn.pack(side="right")

        tools = ttk.Frame(tab)
        tools.grid(row=1, column=0, sticky="ew", pady=(8, 6))
        self.history_repeat_btn = ttk.Button(
            tools, text="复读选中 (+1)", command=self._repeat_history, state="disabled"
        )
        self.history_repeat_btn.pack(side="left")
        ttk.Button(tools, text="清空列表", command=self._clear_history).pack(side="left", padx=(8, 0))
        ttk.Label(tools, text=f"每 {HISTORY_POLL_SECONDS:g} 秒刷新；双击弹幕直接复读", style="Muted.TLabel").pack(
            side="left", padx=12
        )
        self.history_autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(tools, text="自动滚动", variable=self.history_autoscroll_var).pack(side="right")

        body = ttk.Frame(tab)
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.history_tree = ttk.Treeview(
            body, columns=("time", "user", "text", "type"), show="headings", selectmode="browse"
        )
        for column, title, width, anchor, stretch in (
            ("time", "时间", 120, "center", False),
            ("user", "用户", 140, "w", False),
            ("text", "弹幕内容", 620, "w", True),
            ("type", "类型", 90, "center", False),
        ):
            self.history_tree.heading(column, text=title)
            self.history_tree.column(column, width=width, anchor=anchor, stretch=stretch)
        self.history_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.history_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.history_tree.configure(yscrollcommand=scroll.set)
        self.history_tree.bind("<<TreeviewSelect>>", self._on_history_select)
        self.history_tree.bind("<Double-1>", self._on_history_double)

    def _refresh_history_header(self) -> None:
        task = self._active_task()
        self.history_header_var.set(f"当前任务：{task.display_name}｜房间 {task.room_id}")
        if self._history_task_id and self._history_task_id != task.id:
            self._stop_history_live(quiet=True)
            self._history_task_id = ""
            self._render_history([])
            self.history_status_var.set("任务已切换，已停止实时。")
        if self.notebook.index("current") == 5 and not self._history_live:
            self.after(80, self._start_history_live)

    def _toggle_history_live(self) -> None:
        if self._history_live:
            self._stop_history_live()
        else:
            self._start_history_live()

    def _start_history_live(self) -> None:
        if self._history_live or not self.winfo_exists():
            return
        if self.notebook.index("current") != 5:
            return
        if not self._sync_active_task_from_widgets(silent=True):
            self.history_status_var.set("请先补全房间号和 Cookie。")
            return
        task = self._active_task().copy()
        if not task.cookie:
            self.history_status_var.set("实时弹幕需要当前任务的 Cookie。")
            return

        self._stop_history_live(quiet=True)
        if self._history_task_id != task.id:
            self._render_history([])
        self._history_task_id = task.id
        stop = threading.Event()
        self._history_stop = stop
        self._history_live = True
        self.history_fetch_btn.configure(text="停止实时")
        self.history_status_var.set("正在连接直播间弹幕…")

        def worker() -> None:
            client = BilibiliClient(task.cookie, timeout=10.0)
            try:
                room = client.resolve_room(task.room_id)
                while not stop.is_set():
                    try:
                        items = client.get_room_history(room.room_id, 60)
                    except BilibiliError as exc:
                        self.events.put(
                            {"kind": "history_live_error", "message": str(exc), "task_id": task.id}
                        )
                        return
                    self.events.put(
                        {"kind": "history_live", "items": items, "task_id": task.id}
                    )
                    if stop.wait(HISTORY_POLL_SECONDS):
                        return
            except BilibiliError as exc:
                self.events.put(
                    {"kind": "history_live_error", "message": str(exc), "task_id": task.id}
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("实时弹幕异常")
                self.events.put(
                    {"kind": "history_live_error", "message": str(exc), "task_id": task.id}
                )

        thread = threading.Thread(target=worker, name="room-history-live", daemon=True)
        self._history_thread = thread
        thread.start()

    def _stop_history_live(self, quiet: bool = False) -> None:
        was_live = self._history_live
        self._history_live = False
        if self._history_stop is not None:
            self._history_stop.set()
            self._history_stop = None
        self._history_thread = None
        self.history_fetch_btn.configure(text="开始实时")
        if not quiet and was_live:
            self.history_status_var.set("实时已停止。")

    def _append_history_items(self, items: list[HistoryDanmaku]) -> None:
        added = 0
        for item in items:
            key = (item.uid, item.text, item.timeline)
            if key in self._history_keys:
                continue
            self._history_keys.add(key)
            index = len(self._history_items)
            self._history_items.append(item)
            kind = "表情" if item.dm_type == 1 else ("房管" if item.is_admin else "弹幕")
            self.history_tree.insert(
                "", "end", iid=str(index),
                values=(item.timeline, item.nickname or item.uid, item.text, kind),
            )
            added += 1
        if len(self._history_items) > HISTORY_MAX_ROWS:
            self._render_history(self._history_items[-HISTORY_MAX_ROWS:])
        elif added and self.history_autoscroll_var.get():
            self.history_tree.see(str(len(self._history_items) - 1))
        self.history_status_var.set(
            f"实时滚动中 · 每 {HISTORY_POLL_SECONDS:g} 秒 · 共 {len(self._history_items)} 条"
        )

    def _render_history(self, items: list[HistoryDanmaku]) -> None:
        for iid in self.history_tree.get_children():
            self.history_tree.delete(iid)
        self._history_items = list(items)
        self._history_keys = {(item.uid, item.text, item.timeline) for item in items}
        for index, item in enumerate(items):
            kind = "表情" if item.dm_type == 1 else ("房管" if item.is_admin else "弹幕")
            self.history_tree.insert(
                "", "end", iid=str(index),
                values=(item.timeline, item.nickname or item.uid, item.text, kind),
            )
        if items:
            self.history_status_var.set(f"共 {len(items)} 条；双击或选中后点「复读选中」。")
        self._on_history_select(None)

    def _on_history_select(self, _event: Any) -> None:
        state = "normal" if self.history_tree.selection() else "disabled"
        self.history_repeat_btn.configure(state=state)

    def _on_history_double(self, event: tk.Event) -> None:
        row = self.history_tree.identify_row(event.y)
        if not row:
            return
        self.history_tree.selection_set(row)
        self._repeat_history()

    def _repeat_history(self) -> None:
        selection = self.history_tree.selection()
        if not selection:
            messagebox.showinfo("未选中", "请先选中一条弹幕。")
            return
        try:
            index = int(selection[0])
            item = self._history_items[index]
        except (ValueError, IndexError):
            return
        if not item.text.strip():
            return
        if item.dm_type == 1:
            messagebox.showinfo("表情弹幕", "这条是表情弹幕，历史接口拿不到表情标识，暂不支持复读。")
            return
        self._send_payload_async(
            item.text, None, target="history", display=f"复读 {item.nickname or item.uid}：{item.text}"
        )

    def _clear_history(self) -> None:
        self._stop_history_live(quiet=True)
        self._render_history([])
        self.history_status_var.set("已清空并停止实时，点「开始实时」重新连接。")

    # ------------------------------------------------------------------ 日志页
    def _build_log_tab(self) -> None:
        tab = self.tab_log
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        tools = ttk.Frame(tab)
        tools.grid(row=0, column=0, sticky="ew")
        ttk.Button(tools, text="清空", command=self._clear_log).pack(side="left")
        ttk.Button(tools, text="导出…", command=self._export_log).pack(side="left", padx=8)
        self.autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(tools, text="自动滚动", variable=self.autoscroll_var).pack(side="right")
        ttk.Label(tools, text="日志同时写入 logs/bili_send.log", style="Muted.TLabel").pack(side="right", padx=12)
        self.log_text = scrolledtext.ScrolledText(
            tab, height=20, wrap="word", relief="solid", borderwidth=1, state="disabled",
            font=("Microsoft YaHei UI", 9), spacing1=1, spacing3=1,
        )
        self.log_text.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.log_text.tag_configure("time", foreground="#9ca3af")
        self.log_text.tag_configure("info", foreground="#1f2937")
        self.log_text.tag_configure("success", foreground="#15803d")
        self.log_text.tag_configure("warn", foreground="#b45309")
        self.log_text.tag_configure("error", foreground="#b91c1c")

    # ------------------------------------------------------------------ 任务状态
    def _active_task(self) -> TaskConfig:
        return self.cfg.active_task

    def _task_by_id(self, task_id: str) -> TaskConfig | None:
        return self.cfg.task_by_id(task_id)

    def _refresh_task_selector(self) -> None:
        self._task_ids = [task.id for task in self.cfg.tasks]
        self._suspend_task_event = True
        self.task_combo.configure(values=[task.display_name for task in self.cfg.tasks])
        active = self._active_task()
        self.task_combo.current(self._task_ids.index(active.id) if active.id in self._task_ids else 0)
        self._suspend_task_event = False

    def _on_task_selected(self, _event: tk.Event) -> None:
        if self._suspend_task_event:
            return
        index = self.task_combo.current()
        if 0 <= index < len(self._task_ids):
            self._switch_task(self._task_ids[index])

    def _on_task_tree_select(self, _event: tk.Event) -> None:
        selection = self.task_tree.selection()
        if not selection:
            return
        self._switch_task(selection[0])

    def _activate_selected_task(self) -> None:
        selection = self.task_tree.selection()
        if selection:
            self._switch_task(selection[0])
            self.notebook.select(self.tab_send)

    def _switch_task(self, task_id: str, force: bool = False) -> None:
        if task_id == self._active_task().id and not force:
            return
        if not force:
            self._sync_active_task_from_widgets(silent=True)
            self._save_messages_quietly()
        self.cfg.active_task_id = task_id
        self._reload_active_ui()
        log.info("切换到任务：%s", self._active_task().display_name)

    def _reload_active_ui(self) -> None:
        self._load_active_task_into_widgets()
        self._load_messages_into_state(initial=True)
        self._reset_permission_ui()
        cached = self._dm_cache.get(self._dm_key(self._active_task()))
        if cached:
            self._apply_dm_config(cached)
        self._refresh_task_selector()
        self._refresh_task_tree()
        active_id = self._active_task().id
        if active_id in self.task_tree.get_children():
            self.task_tree.selection_set(active_id)
        self._refresh_novel_summary()
        self._refresh_emote_header()
        self._refresh_history_header()
        self._refresh_buttons()

    def _dm_key(self, task: TaskConfig) -> tuple[str, int, str]:
        return (task.id, task.room_id, task.cookie)

    def _on_task_new(self) -> None:
        self._sync_active_task_from_widgets(silent=True)
        task = self._active_task().copy()
        task.id = ""
        task.name = f"直播间 {task.room_id}"
        task.source = SOURCE_POOL
        task.novel_path = ""
        task.novel_index = 0
        task.room_title = ""
        task.anchor_name = ""
        task = TaskConfig.from_dict(task.__dict__)
        self.cfg.tasks.append(task)
        self._switch_task(task.id)
        self._save_config_quietly()
        self._refresh_task_selector()
        self._refresh_task_tree()
        log.info("新建任务：%s", task.display_name)

    def _on_task_copy(self) -> None:
        self._sync_active_task_from_widgets(silent=True)
        task = self._active_task().copy()
        task.id = ""
        task.name = f"{task.display_name} 副本"
        task = TaskConfig.from_dict(task.__dict__)
        self.cfg.tasks.append(task)
        self._switch_task(task.id)
        self._save_config_quietly()
        self._refresh_task_selector()
        self._refresh_task_tree()
        log.info("复制任务：%s", task.display_name)

    def _on_task_delete(self) -> None:
        task = self._active_task()
        if len(self.cfg.tasks) <= 1:
            messagebox.showinfo("无法删除", "至少要保留一个任务。")
            return
        if not messagebox.askyesno("删除任务", f"确定删除「{task.display_name}」吗？"):
            return
        self._sync_active_task_from_widgets(silent=True)
        self._stop_task(task.id)
        self.cfg.remove_task(task.id)
        self._reload_active_ui()
        self._save_config_quietly()
        log.info("已删除任务：%s", task.display_name)

    # ------------------------------------------------------------------ 表单读写
    def _sync_task_best_effort(self) -> None:
        task = self._active_task()
        task.name = self.task_name_var.get().strip() or f"直播间 {task.room_id}"
        try:
            room = int(self.room_var.get().strip())
            if room > 0:
                task.room_id = room
        except ValueError:
            pass
        task.cookie = self._cookie_text_now()
        try:
            interval = float(self.interval_var.get())
            task.interval_seconds = min(max(interval, MIN_INTERVAL_SECONDS), MAX_INTERVAL_SECONDS)
        except ValueError:
            pass
        task.mode = self.mode_var.get() if self.mode_var.get() in (MODE_WEIGHTED, MODE_UNIFORM) else MODE_WEIGHTED
        task.source = self.source_var.get() if self.source_var.get() in (SOURCE_POOL, SOURCE_NOVEL) else SOURCE_POOL
        try:
            task.color = self._parse_color(self.color_var.get())
        except ValueError:
            pass
        try:
            task.fontsize = int(self.font_var.get())
        except ValueError:
            pass
        try:
            task.danmaku_mode = self._parse_position(self.position_var.get())
        except ValueError:
            pass
        task.avoid_repeat = bool(self.avoid_repeat_var.get())
        task.messages_file = self._store_path(self._current_messages_path())
        if self.novel_path_var.get().strip():
            task.novel_path = self._store_path(resolve_path(self.novel_path_var.get().strip()))

    def _sync_active_task_from_widgets(self, silent: bool = False) -> bool:
        def fail(message: str) -> bool:
            if not silent:
                messagebox.showerror("设置不完整", message)
            return False

        task = self._active_task()
        room_text = self.room_var.get().strip()
        if not room_text.isdigit() or int(room_text) <= 0:
            return fail("房间号必须是正整数。")
        cookie = self._cookie_text_now()
        if not cookie and not silent:
            return fail("请先填写当前任务的 Cookie（需要 SESSDATA 与 bili_jct）。")
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

        old_room, old_cookie = task.room_id, task.cookie
        task.name = self.task_name_var.get().strip() or f"直播间 {int(room_text)}"
        task.room_id = int(room_text)
        task.cookie = cookie
        task.interval_seconds = interval
        task.mode = self.mode_var.get() if self.mode_var.get() in (MODE_WEIGHTED, MODE_UNIFORM) else MODE_WEIGHTED
        task.source = self.source_var.get() if self.source_var.get() in (SOURCE_POOL, SOURCE_NOVEL) else SOURCE_POOL
        task.color = color
        task.fontsize = fontsize
        task.danmaku_mode = position
        task.avoid_repeat = bool(self.avoid_repeat_var.get())
        task.messages_file = self._store_path(self._current_messages_path())
        if self.novel_path_var.get().strip():
            task.novel_path = self._store_path(resolve_path(self.novel_path_var.get().strip()))
        if old_room != task.room_id or old_cookie != task.cookie:
            self._reset_permission_ui()
        self.interval_var.set(_format_number(interval))
        self.font_var.set(str(fontsize))
        return True

    def _load_active_task_into_widgets(self) -> None:
        task = self._active_task()
        self.task_name_var.set(task.display_name)
        self.room_var.set(str(task.room_id))
        self.save_cookie_var.set(bool(self.cfg.save_cookie))
        self._set_cookie_value(task.cookie, hide=True)
        self.interval_var.set(_format_number(task.interval_seconds))
        self.mode_var.set(task.mode)
        self.source_var.set(task.source)
        self.avoid_repeat_var.set(task.avoid_repeat)
        self.messages_path_var.set(self.cfg.messages_file)
        self.novel_path_var.set(task.novel_path)
        self.color_var.set(self._color_label(task.color))
        self.font_var.set(str(task.fontsize))
        self.position_var.set(self._position_label(task.danmaku_mode))
        self.room_info_var.set(
            f"上次连接：{task.room_title or '未知'}（{task.anchor_name or '主播未知'}）"
            if (task.room_title or task.anchor_name)
            else "检测后会显示主播、开播状态，以及当前账号可用的弹幕颜色/模式。"
        )
        self._on_source_changed()
        self._refresh_single_count()
        self._refresh_novel_summary()
        self._refresh_emote_header()
        self._refresh_history_header()
        self.task_hint_var.set(
            f"当前任务：{task.display_name}｜来源：{SOURCE_LABELS.get(task.source, task.source)}｜"
            f"间隔：{_format_number(task.interval_seconds)} 秒"
        )

    # ------------------------------------------------------------------ 来源
    def _on_source_changed(self) -> None:
        novel = self.source_var.get() == SOURCE_NOVEL
        locked = self._active_running()
        if novel:
            self.pool_frame.grid_remove()
            self.novel_frame.grid()
        else:
            self.novel_frame.grid_remove()
            self.pool_frame.grid()
        for widget in (self.mode_weighted_radio, self.mode_uniform_radio, self.messages_entry):
            try:
                widget.configure(state="disabled" if (novel or locked) else "normal")
            except tk.TclError:
                pass

    def _active_running(self) -> bool:
        runtime = self.runtimes.get(self._active_task().id)
        return bool(runtime and runtime.runner.running)

    # ------------------------------------------------------------------ 主题/选择
    def _on_source_label(self) -> str:
        return "说书人" if self.source_var.get() == SOURCE_NOVEL else "弹幕池"

    # ------------------------------------------------------------------ Cookie
    def _set_cookie_value(self, value: str, hide: bool = True) -> None:
        self._cookie_value = (value or "").strip()
        self._cookie_hidden = hide
        self._render_cookie_widget()

    def _render_cookie_widget(self) -> None:
        value = self._cookie_value
        if self._cookie_hidden:
            self.cookie_entry.delete(0, "end")
            self.cookie_entry.insert(0, value)
            self.cookie_entry.icursor("end")
            self.cookie_entry.grid()
            self.cookie_entry.configure(state="normal")
            self.cookie_text.grid_remove()
            self.cookie_scroll.grid_remove()
        else:
            self.cookie_text.configure(state="normal")
            self.cookie_text.delete("1.0", "end")
            self.cookie_text.insert("1.0", value)
            self.cookie_text.mark_set("insert", "end")
            self.cookie_text.grid()
            self.cookie_text.configure(state="normal")
            self.cookie_entry.grid_remove()
            self.cookie_scroll.grid()

    def _cookie_text_now(self) -> str:
        if self._cookie_hidden:
            return self.cookie_entry.get().strip()
        return self.cookie_text.get("1.0", "end").strip()

    def _sync_cookie_value(self) -> None:
        self._cookie_value = self._cookie_text_now()

    def _on_cookie_focus_out(self) -> None:
        self._refresh_cookie_status()
        self._autosave_cookie()

    def _on_cookie_pasted(self) -> None:
        self._refresh_cookie_status()
        self._autosave_cookie()

    def _autosave_cookie(self) -> None:
        if not self.save_cookie_var.get():
            return
        if not self._sync_active_task_from_widgets(silent=True):
            return
        self._save_config_quietly()

    def _on_toggle_show_cookie(self) -> None:
        self._sync_cookie_value()
        self._cookie_hidden = not bool(self.show_cookie_var.get())
        self._render_cookie_widget()
        self._refresh_cookie_status()
        if self._cookie_hidden:
            self.cookie_entry.focus_set()
            self.cookie_entry.icursor("end")
        else:
            self.cookie_text.focus_set()

    def _on_toggle_save_cookie(self) -> None:
        enabled = bool(self.save_cookie_var.get())
        self.cfg.save_cookie = enabled
        if not enabled:
            for task in self.cfg.tasks:
                task.cookie = ""
            self.cfg.cookie = ""
            self._save_config_quietly()
            log.info("已关闭 Cookie 保存，所有任务已保存的 Cookie 已清除")
        else:
            self._sync_active_task_from_widgets(silent=True)
            self._save_config_quietly()
            log.info("已开启 Cookie 保存")
        self._refresh_cookie_status()

    def _paste_cookie(self) -> None:
        try:
            text = self.clipboard_get()
        except tk.TclError:
            messagebox.showinfo("剪贴板为空", "剪贴板里没有文本内容。")
            return
        self._set_cookie_value(text, hide=self._cookie_hidden)
        self._refresh_cookie_status()
        self._autosave_cookie()
        log.info("任务「%s」已粘贴 Cookie（%d 个键）", self._active_task().display_name, len(parse_cookie(text)))
        if not self.save_cookie_var.get():
            log.info("「保存 Cookie」未勾选，关闭程序后不会记住这次粘贴")

    def _clear_cookie(self) -> None:
        self._set_cookie_value("", hide=self._cookie_hidden)
        self._refresh_cookie_status()
        self._autosave_cookie()

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
            saved = "已保存" if self.save_cookie_var.get() else "未保存"
            self.cookie_status_var.set(f"✔ Cookie 可用（已打码 · {saved}）")
        else:
            uid = parse_cookie(raw).get("DedeUserID", "")
            self.cookie_status_var.set(f"✔ Cookie 可用（UID {uid}）" if uid else "✔ Cookie 可用")
        self.cookie_status_label.configure(style="Good.TLabel")

    # ------------------------------------------------------------------ 权限
    def _reset_permission_ui(self) -> None:
        self._color_options = [DanmakuOption(value, name, True) for name, value in COLOR_PRESETS]
        self._mode_options = [DanmakuOption(value, name, True) for name, value in POSITION_PRESETS]
        task = self._active_task()
        self._set_color_options(task.color)
        self._set_mode_options(task.danmaku_mode)
        self.perm_var.set("权限：未检测（点击「检测直播间 / 权限」获取当前账号可用的颜色与模式）")

    def _set_color_options(self, selected: int) -> None:
        self.color_combo.configure(values=[f"{item.name} ({item.value})" for item in self._color_options])
        value = next((item.value for item in self._color_options if item.value == selected), None)
        if value is None and self._color_options:
            value = self._color_options[0].value
        self.color_var.set(self._color_label(value if value is not None else selected))

    def _set_mode_options(self, selected: int) -> None:
        self.position_combo.configure(values=[f"{item.name} ({item.value})" for item in self._mode_options])
        value = next((item.value for item in self._mode_options if item.value == selected), None)
        if value is None and self._mode_options:
            value = self._mode_options[0].value
        self.position_var.set(self._position_label(value if value is not None else selected))

    def _apply_dm_config(self, config: DanmakuConfig) -> None:
        if not config.fetched:
            self.perm_var.set(f"权限：获取失败（{config.message or '网络错误'}），将按当前设置尝试")
            return
        task = self._active_task()
        self._color_options = config.enabled_colors or [DanmakuOption(16777215, "白色", True)]
        self._mode_options = config.enabled_modes or [DanmakuOption(1, "滚动", True)]
        self._set_color_options(task.color)
        self._set_mode_options(task.danmaku_mode)
        self._dm_cache[self._dm_key(task)] = config
        modes = "、".join(item.name for item in config.enabled_modes) or "滚动"
        self.perm_var.set(
            f"权限：{config.guard_text}｜可用模式：{modes}｜可用颜色：{len(config.enabled_colors)} 种"
            "（顶部/底部与彩色弹幕通常需要舰长或房管身份）"
        )
        if task.danmaku_mode not in [item.value for item in config.enabled_modes]:
            self.perm_var.set(self.perm_var.get() + "｜当前选择不可用，发送时会自动改用滚动")

    def _on_detect_room(self) -> None:
        if not self._sync_active_task_from_widgets(silent=True):
            messagebox.showerror("设置不完整", "房间号或 Cookie 不完整。")
            return
        task = self._active_task().copy()
        if not task.cookie:
            messagebox.showinfo("缺少 Cookie", "检测权限需要当前任务的 Cookie。")
            return
        self.detect_btn.configure(state="disabled")
        self.room_info_var.set("正在查询直播间与权限…")

        def worker() -> None:
            client = BilibiliClient(task.cookie, timeout=8.0)
            try:
                room = client.resolve_room(task.room_id)
                login = client.check_login()
                dm = client.get_danmaku_config(room.room_id)
            except BilibiliError as exc:
                self.events.put({"kind": "detect_error", "task_id": task.id, "message": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                log.exception("检测直播间异常")
                self.events.put({"kind": "detect_error", "task_id": task.id, "message": str(exc)})
                return
            self.events.put(
                {"kind": "detected", "task_id": task.id, "room": room, "login": login, "dm": dm}
            )

        threading.Thread(target=worker, name="detect", daemon=True).start()

    # ------------------------------------------------------------------ 启动/停止
    def _build_source(self, task: TaskConfig, interactive: bool = True) -> tuple[MessageSource | None, str]:
        if task.source == SOURCE_NOVEL:
            if not task.novel_path:
                return None, "说书人模式需要先选择小说文件"
            try:
                book = load_book(resolve_path(task.novel_path), task.novel_chunk_size)
            except NovelError as exc:
                return None, str(exc)
            index = min(max(0, task.novel_index), len(book))
            if index >= len(book):
                if interactive and not messagebox.askyesno(
                    "说书人进度", f"《{book.title}》已经播完（{len(book)} 段）。\n要从头重新开始吗？"
                ):
                    return None, "已取消"
                index = 0
            return NovelSource(book, index), ""
        try:
            items = load_messages(self.cfg.messages_path)
            pool = MessagePool(items, mode=task.mode, avoid_repeat=task.avoid_repeat)
        except (MessageFileError, ValueError) as exc:
            return None, str(exc)
        return PoolSource(pool), ""

    def _start_task(self, task_id: str, interactive: bool = True) -> bool:
        runtime = self.runtimes.get(task_id)
        if runtime and runtime.runner.running:
            return True
        task = self._task_by_id(task_id)
        if task is None:
            return False
        if task_id == self._active_task().id:
            if not self._sync_active_task_from_widgets(silent=not interactive):
                if interactive:
                    messagebox.showerror("设置不完整", "请先补全当前任务的房间号、Cookie 和间隔。")
                return False
            task = self._task_by_id(task_id)
        task = task.copy()
        if not task.cookie:
            if interactive:
                messagebox.showwarning("缺少 Cookie", f"任务「{task.display_name}」还没有 Cookie。")
            log.warning("任务「%s」没有 Cookie，已跳过", task.display_name)
            return False
        source, error = self._build_source(task, interactive=interactive)
        if source is None:
            if interactive:
                messagebox.showerror("无法启动", error)
            log.warning("任务「%s」无法启动：%s", task.display_name, error)
            return False
        client = BilibiliClient(task.cookie, timeout=5.0)
        runner = DanmakuRunner(task, source, self.events, client)
        self.runtimes[task_id] = Runtime(runner=runner, source=source)
        runner.start()
        self._save_config_quietly()
        self._refresh_task_tree()
        self._refresh_buttons()
        self._refresh_stats()
        log.info("任务「%s」已启动", task.display_name)
        return True

    def _stop_task(self, task_id: str) -> None:
        runtime = self.runtimes.get(task_id)
        if runtime and runtime.runner.running:
            runtime.runner.stop()
            log.info("任务「%s」正在停止…", runtime.runner.task.display_name)
            self._refresh_task_tree()

    def _on_start_active(self) -> None:
        self._start_task(self._active_task().id, interactive=True)

    def _on_stop_active(self) -> None:
        self._stop_task(self._active_task().id)

    def _on_start_selected(self) -> None:
        selection = self.task_tree.selection()
        if not selection:
            messagebox.showinfo("未选中", "请先在任务列表里选一个任务。")
            return
        self._start_task(selection[0], interactive=True)

    def _on_stop_selected(self) -> None:
        selection = self.task_tree.selection()
        if selection:
            self._stop_task(selection[0])

    def _on_start_all(self) -> None:
        started = 0
        for task in list(self.cfg.tasks):
            if self._start_task(task.id, interactive=False):
                started += 1
        log.info("已启动 %d/%d 个任务", started, len(self.cfg.tasks))
        self._refresh_task_tree()

    def _on_stop_all(self) -> None:
        for task in list(self.cfg.tasks):
            self._stop_task(task.id)

    # ------------------------------------------------------------------ 单条发送
    def _refresh_single_count(self) -> None:
        text = self.single_var.get()
        self.single_count_var.set(f"{len(text)}/{MAX_DANMAKU_LENGTH}")

    def _emote_thumb(self, unique: str, size: int = 22) -> tk.PhotoImage | None:
        path = self._emoticon_images.get(unique)
        if not path or not path.exists():
            path = None
            for candidate in (CACHE_DIR / "emoticons").glob("*/images/*"):
                if candidate.stem == unique:
                    path = candidate
                    break
        if not path or not path.exists():
            return None
        try:
            photo = tk.PhotoImage(data=base64.b64encode(path.read_bytes()))
        except tk.TclError:
            return None
        factor = max(1, int(photo.width() // max(1, size)))
        if factor > 1:
            photo = photo.subsample(factor)
        return photo

    def _set_single_emoticon(self, emo: Emoticon | None) -> None:
        self._single_emoticon = emo
        if emo:
            self._single_emote_photo = self._emote_thumb(emo.unique, 24)
            if self._single_emote_photo:
                self.single_emote_thumb.configure(image=self._single_emote_photo)
            else:
                self.single_emote_thumb.configure(image="")
            if emo.combinable:
                self.single_emote_var.set(f"已选表情：{_emote_label(emo.name)}（可拼文字）")
                self.single_entry.configure(state="normal")
            else:
                self.single_emote_var.set(f"已选表情：{_emote_label(emo.name)}（仅表情，不能拼文字）")
                self.single_var.set("")
                self._refresh_single_count()
                self.single_entry.configure(state="disabled")
        else:
            self._single_emote_photo = None
            self.single_emote_thumb.configure(image="")
            self.single_emote_var.set("未选择表情")
            self.single_entry.configure(state="normal")

    def _choose_single_emoticon(self) -> None:
        task = self._active_task()
        if not self._emoticon_packages or self._emote_task_id != task.id:
            messagebox.showinfo("没有表情", "请先在「表情」页拉取当前直播间的表情。")
            return
        options = [
            (pkg, emo)
            for pkg in self._emoticon_packages
            for emo in pkg.emoticons
            if emo.perm
        ]
        if not options:
            messagebox.showinfo("没有可用表情", "当前账号在这个直播间没有可用表情。")
            return
        win = tk.Toplevel(self)
        win.title("选择表情")
        win.transient(self)
        win.grab_set()
        frame = ttk.Frame(win, padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"共 {len(options)} 个可用表情（双击选择）").pack(anchor="w")
        listbox = tk.Listbox(frame, width=56, height=18)
        listbox.pack(fill="both", expand=True, pady=(6, 6))
        for pkg, emo in options:
            tag = "可拼文字" if emo.combinable else "仅表情"
            listbox.insert("end", f"{pkg.name} / {_emote_label(emo.name)}（{tag}）")

        def choose(_event=None) -> None:
            selection = listbox.curselection()
            if selection:
                self._set_single_emoticon(options[selection[0]][1])
            win.destroy()

        listbox.bind("<Double-Button-1>", choose)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="确定", command=choose).pack(side="right")
        ttk.Button(buttons, text="取消", command=win.destroy).pack(side="right", padx=(0, 6))
        win.wait_window()

    def _on_single_send(self) -> None:
        text = self.single_var.get().strip()
        emo = self._single_emoticon
        if emo and not emo.combinable:
            text = ""
        if not text and not emo:
            messagebox.showinfo("内容为空", "请输入弹幕内容，或先选择一个表情。")
            return
        display = f"表情 {_emote_label(emo.name)}" if (emo and not text) else (text or "")
        if emo and text:
            display = f"[{_emote_label(emo.name)}] {text}"
        self._send_payload_async(text, emo, target="single", display=display)

    def _send_payload_async(
        self, text: str, emo: Emoticon | None, target: str, display: str
    ) -> None:
        if not self._sync_active_task_from_widgets(silent=True):
            messagebox.showerror("设置不完整", "请先补全房间号和 Cookie。")
            return
        task = self._active_task().copy()
        if not task.cookie:
            messagebox.showwarning("缺少 Cookie", "当前任务没有 Cookie。")
            return
        if emo and not emo.combinable and text:
            messagebox.showwarning("不能拼文字", f"表情「{_emote_label(emo.name)}」只能单独发送，不能拼接文字。")
            return
        if len(text) > MAX_DANMAKU_LENGTH:
            messagebox.showwarning("太长", f"B 站单条弹幕上限 {MAX_DANMAKU_LENGTH} 字。")
            return
        if target == "single":
            self.single_btn.configure(state="disabled")

        def worker() -> None:
            client = BilibiliClient(task.cookie, timeout=8.0)
            emote_unique = emo.unique if emo else ""
            try:
                room = client.resolve_room(task.room_id)
                status = client.check_login()
                if status.known and not status.logged_in:
                    self.events.put(
                        {
                            "kind": "single", "ok": False, "text": display,
                            "message": status.message, "target": target, "emote_unique": emote_unique,
                        }
                    )
                    return
                dm = client.get_danmaku_config(room.room_id)
                color, mode = _pick_color_mode(task, dm)
                result = client.send_danmaku(
                    text,
                    room.room_id,
                    color=color,
                    fontsize=task.fontsize,
                    mode=mode,
                    emoticon_unique=emote_unique,
                    emoticon_text=emo.name if emo else "",
                    emoticon_combinable=bool(emo and emo.combinable),
                )
                self.events.put(
                    {
                        "kind": "single", "ok": result.ok, "text": display,
                        "message": result.detail, "target": target, "emote_unique": emote_unique,
                    }
                )
            except BilibiliError as exc:
                self.events.put(
                    {
                        "kind": "single", "ok": False, "text": display,
                        "message": str(exc), "target": target, "emote_unique": emote_unique,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("单条发送异常")
                self.events.put(
                    {
                        "kind": "single", "ok": False, "text": display,
                        "message": str(exc), "target": target, "emote_unique": emote_unique,
                    }
                )

        threading.Thread(target=worker, name="single-send", daemon=True).start()

    def _on_test_send(self) -> None:
        if self._single_emoticon or self.single_var.get().strip():
            self._on_single_send()
            return
        for item in self.items:
            if item.text.strip():
                self.single_var.set(item.text)
                self._refresh_single_count()
                self._on_single_send()
                return
        messagebox.showinfo("没有内容", "先在「单条弹幕」里输入内容。")

    # ------------------------------------------------------------------ 任务表
    def _task_row_values(self, task: TaskConfig) -> tuple:
        runtime = self.runtimes.get(task.id)
        uid = parse_cookie(task.cookie).get("DedeUserID", "")
        account = f"…{uid[-4:]}" if uid else ("未配置" if not task.cookie else "已配置")
        status = "已停止"
        stats = "0 / 0 / 0"
        progress = "—"
        if runtime:
            status = {
                "starting": "启动中",
                "running": "运行中",
                "stopped": "已停止",
            }.get(runtime.status, runtime.status)
            if runtime.runner.running:
                status = "运行中"
            if runtime.status == "stopped" and runtime.runner.last_error:
                status = "异常停止"
            elif runtime.status == "stopped" and runtime.runner.completed:
                status = "已播完"
            stats = f"{runtime.total} / {runtime.ok} / {runtime.failed}"
            progress = runtime.progress or runtime.source.progress_text()
        elif task.source == SOURCE_NOVEL and task.novel_path:
            progress = f"进度：第 {task.novel_index + 1} 段"
        return (
            task.display_name,
            task.room_id,
            account,
            SOURCE_LABELS.get(task.source, task.source),
            f"{_format_number(task.interval_seconds)}s",
            status,
            stats,
            progress,
        )

    def _refresh_task_tree(self) -> None:
        existing = set(self.task_tree.get_children())
        ids = {task.id for task in self.cfg.tasks}
        for item in existing - ids:
            self.task_tree.delete(item)
        for task in self.cfg.tasks:
            values = self._task_row_values(task)
            if task.id in existing:
                self.task_tree.item(task.id, values=values)
            else:
                self.task_tree.insert("", "end", iid=task.id, values=values)
        active = self._active_task().id
        if not self.task_tree.selection() and active in self.task_tree.get_children():
            self.task_tree.selection_set(active)

    def _refresh_buttons(self) -> None:
        runtime = self.runtimes.get(self._active_task().id)
        running = bool(runtime and runtime.runner.running)
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        for widget in self._send_lockable:
            try:
                widget.configure(state="disabled" if running else "normal")
            except tk.TclError:
                pass
        self._on_source_changed()
        self._update_message_buttons()

    # ------------------------------------------------------------------ 事件循环
    def _pump(self) -> None:
        try:
            while True:
                record = LOG_QUEUE.get_nowait()
                self._append_log(
                    record.get("level", "info"),
                    record.get("message", ""),
                    record.get("emote_unique", ""),
                )
        except queue.Empty:
            pass
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass
        self._poll_count += 1
        if self._poll_count % TASK_REFRESH_EVERY == 0:
            self._refresh_task_tree()
        self._tick_countdown()
        self._pump_id = self.after(POLL_MS, self._pump)

    def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        task_id = event.get("task_id", "")
        if kind == "status":
            self._on_runner_status(event)
        elif kind == "progress":
            runtime = self.runtimes.get(task_id)
            if runtime:
                runtime.total = int(event.get("total", runtime.total))
                runtime.ok = int(event.get("ok", runtime.ok))
                runtime.failed = int(event.get("failed", runtime.failed))
                runtime.progress = str(event.get("source") or runtime.progress)
                task = self._task_by_id(task_id)
                if task and task.source == SOURCE_NOVEL and isinstance(runtime.source, NovelSource):
                    task.novel_index = runtime.source.index
                    self._save_config_quietly()
                    if task.id == self._active_task().id:
                        self._refresh_novel_summary()
                        if (
                            self._novel_book
                            and task.novel_path
                            and str(self._novel_book.path) == str(resolve_path(task.novel_path))
                        ):
                            self._render_novel()
            self._refresh_stats()
        elif kind == "schedule":
            runtime = self.runtimes.get(task_id)
            if runtime:
                runtime.next_at = event.get("next_at")
        elif kind == "permissions":
            config: DanmakuConfig = event["config"]
            task = self._task_by_id(task_id)
            if task:
                self._dm_cache[self._dm_key(task)] = config
                if task.id == self._active_task().id:
                    self._apply_dm_config(config)
        elif kind == "room":
            room: RoomInfo = event["room"]
            task = self._task_by_id(task_id)
            if task:
                task.room_title = room.title
                task.anchor_name = room.anchor
                if task.id == self._active_task().id:
                    self._show_room(room)
                    self._refresh_novel_summary()
        elif kind == "detected":
            self._on_detected(event)
        elif kind == "detect_error":
            self.detect_btn.configure(state="normal")
            self.room_info_var.set(f"检测失败：{event.get('message')}")
            log.warning("检测直播间失败：%s", event.get("message"))
        elif kind == "history_live":
            if not self._history_live or (task_id and task_id != self._active_task().id):
                return
            self._append_history_items(list(event.get("items") or []))
        elif kind == "history_live_error":
            if task_id and task_id != self._active_task().id:
                return
            self._stop_history_live(quiet=True)
            self.history_status_var.set(f"实时已停止：{event.get('message', '')}")
            log.warning("实时弹幕停止：%s", event.get("message", ""))
        elif kind == "single":
            if event.get("target") == "single":
                self.single_btn.configure(state="normal")
                if event.get("ok"):
                    self.single_var.set("")
                    self._set_single_emoticon(None)
                    self._refresh_single_count()
            if event.get("ok"):
                success(
                    log,
                    "单条弹幕发送成功：%s",
                    event.get("text", ""),
                    extra={"emote_unique": event.get("emote_unique", "")},
                )
            else:
                log.error("单条弹幕发送失败：%s", event.get("message", ""))
        elif kind == "emoticons":
            self._render_emoticons(
                event.get("packages") or [],
                event.get("images") or {},
                int(event.get("room_id") or 0),
                str(event.get("task_id") or ""),
            )
        elif kind == "emoticon_progress":
            self.emote_status_var.set(str(event.get("message") or ""))
        elif kind == "emoticon_error":
            self.emote_fetch_btn.configure(state="normal")
            self.emote_status_var.set(f"拉取失败：{event.get('message')}")
            log.warning("拉取表情失败：%s", event.get("message"))

    def _on_runner_status(self, event: dict[str, Any]) -> None:
        task_id = event.get("task_id", "")
        state = event.get("state")
        runtime = self.runtimes.get(task_id)
        if runtime:
            runtime.status = state if state in ("starting", "running", "stopped") else runtime.status
            runtime.total = int(event.get("total", runtime.total))
            runtime.ok = int(event.get("ok", runtime.ok))
            runtime.failed = int(event.get("failed", runtime.failed))
            if state == "stopped":
                runtime.progress = str(event.get("source") or runtime.progress)
                runtime.next_at = None
                self._refresh_stats()
                self._refresh_task_tree()
                message = event.get("message")
                task = self._task_by_id(task_id)
                if message and task and task.id == self._active_task().id:
                    messagebox.showerror("发送已停止", message)
                elif message:
                    log.error("任务「%s」：%s", event.get("task_name"), message)
        self._refresh_buttons()

    def _on_detected(self, event: dict[str, Any]) -> None:
        self.detect_btn.configure(state="normal")
        room: RoomInfo = event["room"]
        login: LoginStatus = event["login"]
        dm: DanmakuConfig = event["dm"]
        task = self._task_by_id(event.get("task_id", ""))
        if task:
            task.room_title = room.title
            task.anchor_name = room.anchor
            self._dm_cache[self._dm_key(task)] = dm
        if task and task.id == self._active_task().id:
            self._show_room(room)
            self._apply_dm_config(dm)
            if login.known:
                self.perm_var.set(self.perm_var.get() + f"｜账号：{login.uname or '已登录'}")
        log.info(
            "检测完成：%s（%s）｜%s｜可用模式 %d 种｜可用颜色 %d 种",
            room.room_id,
            room.title or "标题未知",
            dm.guard_text if dm.fetched else "权限未知",
            len(dm.enabled_modes),
            len(dm.enabled_colors),
        )

    def _tick_countdown(self) -> None:
        runtime = self.runtimes.get(self._active_task().id)
        if runtime and runtime.runner.running:
            self.state_var.set("运行中")
            self.state_label.configure(style="Good.TLabel")
            if runtime.next_at:
                remaining = max(0.0, runtime.next_at - time.monotonic())
                self.countdown_var.set(f"下次发送：{remaining:.0f} 秒后")
            else:
                self.countdown_var.set("下次发送：发送中…")
        else:
            self.state_var.set("已停止")
            self.state_label.configure(style="Status.TLabel")
            self.countdown_var.set("下次发送：—")
        running = sum(1 for item in self.runtimes.values() if item.runner.running)
        if running:
            self.status_room_var.set(f"运行中任务：{running}/{len(self.cfg.tasks)}")
        else:
            self.status_room_var.set(self.status_room_var.get() if self.status_room_var.get().startswith("直播间") else "无运行中任务")

    def _refresh_stats(self) -> None:
        runtime = self.runtimes.get(self._active_task().id)
        if runtime:
            self.stats_var.set(f"已发送 {runtime.total} · 成功 {runtime.ok} · 失败 {runtime.failed}")
        else:
            self.stats_var.set("已发送 0 · 成功 0 · 失败 0")

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

    # ------------------------------------------------------------------ 表情
    def _refresh_emote_header(self) -> None:
        task = self._active_task()
        self.emote_header_var.set(f"当前任务：{task.display_name}｜房间 {task.room_id}")
        if self._emote_task_id and self._emote_task_id != task.id:
            self._emote_task_id = ""
            self._emote_room_id = 0
            self._emoticon_packages = []
            self._emoticon_images = {}
            self._render_emoticons([], {}, 0, "")
            self.emote_status_var.set("任务已切换，请重新拉取表情。")
        self.novel_header_var.set(f"当前任务：{task.display_name}")

    def _fetch_emoticons(self, force: bool) -> None:
        if not self._sync_active_task_from_widgets(silent=True):
            messagebox.showerror("设置不完整", "请先填写当前任务的房间号和 Cookie。")
            return
        task = self._active_task().copy()
        if not task.cookie:
            messagebox.showinfo("缺少 Cookie", "拉取直播间表情需要当前任务的 Cookie。")
            return
        self.emote_fetch_btn.configure(state="disabled")
        self.emote_status_var.set("正在拉取表情列表…")

        def worker() -> None:
            client = BilibiliClient(task.cookie, timeout=8.0)
            try:
                room = client.resolve_room(task.room_id)
                status = client.check_login()
                if status.known and not status.logged_in:
                    self.events.put({"kind": "emoticon_error", "message": status.message})
                    return
                cache = EmoticonCache(room.room_id, client.uid)
                packages = cache.fetch(client, force=force)
                total = sum(len(pkg.emoticons) for pkg in packages)
                self.events.put(
                    {"kind": "emoticon_progress", "message": f"表情 {total} 个，正在缓存图片…"}
                )
                images: dict[str, Path] = {}
                all_emoticons = [emo for pkg in packages for emo in pkg.emoticons]

                def download(emo: Emoticon):
                    return emo.unique, cache.ensure_image(client, emo)

                with ThreadPoolExecutor(max_workers=4) as pool:
                    for index, (unique, path) in enumerate(pool.map(download, all_emoticons), 1):
                        if path:
                            images[unique] = path
                        if index % 10 == 0:
                            self.events.put(
                                {
                                    "kind": "emoticon_progress",
                                    "message": f"正在缓存表情 {index}/{total}…",
                                }
                            )
                self.events.put(
                    {
                        "kind": "emoticons",
                        "packages": packages,
                        "images": images,
                        "room_id": room.room_id,
                        "task_id": task.id,
                    }
                )
                self.events.put(
                    {
                        "kind": "emoticon_progress",
                        "message": f"已缓存 {len(packages)} 个表情包 / {total} 个表情（图片 {len(images)} 个）",
                    }
                )
            except BilibiliError as exc:
                self.events.put({"kind": "emoticon_error", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001
                log.exception("拉取表情异常")
                self.events.put({"kind": "emoticon_error", "message": str(exc)})

        threading.Thread(target=worker, name="emoticons", daemon=True).start()

    def _render_emoticons_last(self) -> None:
        self._render_emoticons(
            self._emoticon_packages, self._emoticon_images, self._emote_room_id, self._emote_task_id
        )

    def _render_emoticons(
        self, packages: list[Any], images: dict[str, Path], room_id: int = 0, task_id: str = ""
    ) -> None:
        if task_id and task_id != self._active_task().id:
            return
        self._emoticon_packages = packages
        self._emoticon_images = images
        if room_id:
            self._emote_room_id = int(room_id)
        if task_id:
            self._emote_task_id = task_id
        self.emote_fetch_btn.configure(state="normal")
        for child in self.emote_inner.winfo_children():
            child.destroy()
        self._emote_photos.clear()
        self._emote_emoticons.clear()
        if not packages:
            ttk.Label(self.emote_inner, text="没有拉取到表情（可能该直播间没有专属表情）。").pack(anchor="w")
            return
        only_perm = bool(self.emote_only_perm_var.get())
        for pkg in packages:
            emoticons = [emo for emo in pkg.emoticons if not only_perm or emo.perm]
            if not emoticons:
                continue
            usable = sum(1 for emo in pkg.emoticons if emo.perm)
            frame = ttk.Frame(self.emote_inner, relief="groove", borderwidth=1)
            frame.pack(fill="x", pady=4)
            header = ttk.Frame(frame)
            header.pack(fill="x", padx=6, pady=(4, 0))
            ttk.Label(
                header,
                text=f"{pkg.name}（可用 {usable}/{len(pkg.emoticons)}，{'可拼文字' if pkg.pkg_type == 3 else '仅表情'}）",
                font=("Microsoft YaHei UI", 10, "bold"),
            ).pack(side="left")
            ttk.Button(
                header, text="保存到本地", command=lambda p=pkg: self._save_emote_package(p)
            ).pack(side="right")
            collapse_btn = ttk.Button(header, text="收起", width=5)
            collapse_btn.pack(side="right", padx=(0, 6))

            grid = ttk.Frame(frame)
            collapsed = pkg.pkg_id in self._collapsed_packages
            if collapsed:
                collapse_btn.configure(text="展开")
            else:
                grid.pack(fill="x", padx=6, pady=6)
            collapse_btn.configure(
                command=lambda pid=pkg.pkg_id, g=grid, b=collapse_btn: self._toggle_emote_package(pid, g, b)
            )

            for index, emo in enumerate(emoticons):
                self._emote_emoticons.append(emo)
                photo = None
                path = images.get(emo.unique)
                if path and path.exists():
                    try:
                        photo = tk.PhotoImage(data=base64.b64encode(path.read_bytes()))
                        self._emote_photos.append(photo)
                    except tk.TclError:
                        photo = None
                text = _emote_label(emo.name) if emo.perm else f"{_emote_label(emo.name)}（锁）"
                button = tk.Button(
                    grid, text=text, image=photo, compound="top",
                    relief="flat", cursor="hand2" if emo.perm else "arrow",
                    fg="#1f2937" if emo.perm else "#9ca3af",
                    bg="#f8fafc", activebackground="#e2e8f0",
                    padx=2, pady=2,
                    command=lambda item=emo: self._add_emoticon_to_pool(item),
                )
                button.grid(row=index // 8, column=index % 8, padx=2, pady=2)
        self.emote_status_var.set(
            f"已加载 {len(packages)} 个表情包，点击可用表情加入弹幕池；锁定表情会提示解锁条件。"
        )

    def _toggle_emote_package(self, pkg_id: int, grid: ttk.Frame, button: ttk.Button) -> None:
        if pkg_id in self._collapsed_packages:
            grid.pack(fill="x", padx=6, pady=6)
            button.configure(text="收起")
            self._collapsed_packages.discard(pkg_id)
        else:
            grid.pack_forget()
            button.configure(text="展开")
            self._collapsed_packages.add(pkg_id)

    def _save_emote_package(self, pkg: Any) -> None:
        room = self._emote_room_id or self._active_task().room_id
        folder = APP_DIR / "exports" / "emoticons" / f"{room}_{_safe_filename(pkg.name)}"
        folder.mkdir(parents=True, exist_ok=True)
        saved = 0
        meta: list[dict[str, Any]] = []
        for emo in pkg.emoticons:
            path = self._emoticon_images.get(emo.unique)
            if not path or not path.exists():
                continue
            base = _safe_filename(emo.emoji or emo.name or emo.unique)
            target = folder / (base + path.suffix)
            counter = 2
            while target.exists():
                target = folder / f"{base}_{counter}{path.suffix}"
                counter += 1
            try:
                shutil.copy2(path, target)
            except OSError as exc:
                log.warning("保存表情图片失败 %s：%s", emo.name, exc)
                continue
            meta.append(
                {
                    "name": emo.name,
                    "emoji": emo.emoji,
                    "unique": emo.unique,
                    "perm": emo.perm,
                    "unlock": emo.unlock_text,
                    "file": target.name,
                }
            )
            saved += 1
        if not saved:
            messagebox.showinfo("没有图片", "表情图片还没有缓存，请先点「拉取直播间表情」。")
            return
        import json

        (folder / "emoticons.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self.emote_status_var.set(f"已保存 {saved} 个表情到 {folder}")
        log.info("表情包「%s」已保存到 %s", pkg.name, folder)
        try:
            os.startfile(str(folder))  # type: ignore[attr-defined]
        except OSError:
            pass

    def _add_emoticon_to_pool(self, emo: Emoticon) -> None:
        if not emo.perm:
            self.emote_hint_var.set(f"「{_emote_label(emo.name)}」不可用：{emo.unlock_text or '未解锁'}")
            return
        if not self.items:
            self._load_messages_into_state(initial=True)
        for item in self.items:
            if item.emoticon_unique == emo.unique:
                self.emote_hint_var.set(f"「{_emote_label(emo.name)}」已经在弹幕池里了。")
                return
        self.items.append(
            MessageItem(
                text="",
                weight=1,
                emoticon_unique=emo.unique,
                emoticon_url=emo.url,
                emoticon_name=emo.name,
                emoticon_combinable=emo.combinable,
            )
        )
        self._save_messages_quietly()
        self._refresh_message_tree()
        tag = "可拼文字" if emo.combinable else "仅表情"
        self.emote_hint_var.set(f"已加入弹幕池：{_emote_label(emo.name)}（{emo.pkg_name}，{tag}）")
        log.info("已将表情「%s」加入弹幕池", _emote_label(emo.name))

    # ------------------------------------------------------------------ 说书人
    def _browse_novel(self) -> None:
        path = filedialog.askopenfilename(
            title="选择小说文本",
            initialdir=str(APP_DIR),
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        self.novel_path_var.set(self._store_path(Path(path)))
        self.novel_tab_path_var.set(self.novel_path_var.get())
        self._load_novel(Path(path))

    def _load_novel_clicked(self) -> None:
        path = self.novel_tab_path_var.get().strip() or self.novel_path_var.get().strip()
        if not path:
            messagebox.showinfo("没有小说", "请先选择小说文件。")
            return
        self.novel_path_var.set(self._store_path(resolve_path(path)))
        self._load_novel(resolve_path(path))

    def _load_novel(self, path: Path) -> None:
        try:
            size = int(self.novel_size_var.get())
        except ValueError:
            size = MAX_CHUNK
        size = max(MIN_NOVEL_CHUNK, min(MAX_NOVEL_CHUNK, size))
        try:
            book = load_book(path, size)
        except NovelError as exc:
            messagebox.showerror("加载失败", str(exc))
            return
        self._novel_book = book
        task = self._active_task()
        if task.novel_chunk_size != size:
            task.novel_index = 0
        task.novel_path = self._store_path(path)
        task.novel_chunk_size = size
        if task.novel_index >= len(book):
            task.novel_index = 0
        self._novel_task_id = task.id
        self.novel_path_var.set(task.novel_path)
        self.novel_tab_path_var.set(task.novel_path)
        self._render_novel()
        self._save_config_quietly()
        log.info("说书人已加载《%s》，共 %d 段", book.title, len(book))

    def _render_novel(self) -> None:
        for item in self.novel_tree.get_children():
            self.novel_tree.delete(item)
        book = self._novel_book
        if not book:
            self.novel_info_var.set("尚未加载小说")
            self.novel_bar["value"] = 0
            return
        task = self._active_task()
        index = min(max(0, task.novel_index), len(book) - 1)
        window_start = max(0, index - 200)
        window_end = min(len(book), index + 401)
        for position in range(window_start, window_end):
            self.novel_tree.insert("", "end", iid=str(position), values=(position + 1, preview(book.chunks[position], 60)))
        if str(index) in self.novel_tree.get_children():
            self.novel_tree.selection_set(str(index))
            self.novel_tree.see(str(index))
        self._update_novel_progress(index, len(book), book.title)

    def _update_novel_progress(self, index: int, total: int, title: str = "") -> None:
        percent = int(index * 100 / total) if total else 0
        self.novel_bar["value"] = percent
        name = title or (self._novel_book.title if self._novel_book else "")
        self.novel_info_var.set(f"{name}：第 {min(index + 1, total)}/{total} 段（{percent}%）")

    def _refresh_novel_summary(self) -> None:
        task = self._active_task()
        if task.source == SOURCE_NOVEL and task.novel_path:
            self.novel_progress_var.set(f"进度：第 {task.novel_index + 1} 段（下次从这里继续）")
            if self._novel_book and str(self._novel_book.path) == str(resolve_path(task.novel_path)):
                self._update_novel_progress(task.novel_index, len(self._novel_book), self._novel_book.title)
        elif task.source == SOURCE_NOVEL:
            self.novel_progress_var.set("进度：尚未选择小说")
        else:
            self.novel_progress_var.set("进度：当前任务未使用说书人")
        if task.novel_path and not self.novel_tab_path_var.get():
            self.novel_tab_path_var.set(task.novel_path)

    def _novel_restart(self) -> None:
        task = self._active_task()
        task.novel_index = 0
        self._save_config_quietly()
        self._render_novel()
        self._refresh_novel_summary()
        log.info("说书人进度已重置为第 1 段")

    def _novel_continue(self) -> None:
        task = self._active_task()
        if not task.novel_path:
            messagebox.showinfo("没有小说", "当前任务还没有选择小说。")
            return
        if self._novel_book is None or str(self._novel_book.path) != str(resolve_path(task.novel_path)):
            self._load_novel(resolve_path(task.novel_path))
        self._render_novel()
        log.info("说书人将从第 %d 段继续", task.novel_index + 1)

    def _novel_set_source(self) -> None:
        if not self._novel_book:
            messagebox.showinfo("没有小说", "请先加载小说。")
            return
        task = self._active_task()
        task.source = SOURCE_NOVEL
        task.novel_path = self._store_path(self._novel_book.path)
        task.novel_chunk_size = int(self.novel_size_var.get() or MAX_CHUNK)
        self.source_var.set(SOURCE_NOVEL)
        self.novel_path_var.set(task.novel_path)
        self._on_source_changed()
        self._save_config_quietly()
        self._refresh_novel_summary()
        log.info("任务「%s」已切换为说书人：《%s》", task.display_name, self._novel_book.title)

    # ------------------------------------------------------------------ 消息库
    def _current_messages_path(self) -> Path:
        text = self.messages_path_var.get().strip() or DEFAULT_MESSAGES_FILE
        return resolve_path(text)

    def _refresh_message_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        usable = 0
        for index, item in enumerate(self.items):
            if item.usable:
                usable += 1
            state = "可用" if item.usable else "权重 0（不参与）"
            if item.is_emoticon:
                state = "表情 · " + state
            elif len(item.text) > MAX_DANMAKU_LENGTH:
                state += " · 超长"
            self.tree.insert(
                "", "end", iid=str(index), values=(index + 1, item.display_text, item.weight, state)
            )
        if self.items:
            self.msg_summary_var.set(
                f"共 {len(self.items)} 条，其中 {usable} 条参与抽取 · 文件：{self.cfg.messages_path}"
            )
        self._update_message_buttons()

    def _update_message_buttons(self) -> None:
        running = any(item.runner.running for item in self.runtimes.values())
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
        self.msg_text.configure(state="normal")
        self.msg_text.delete("1.0", "end")
        self.msg_text.insert("1.0", item.text)
        if item.is_emoticon:
            self._set_editor_emoticon(
                Emoticon(
                    item.emoticon_unique,
                    item.emoticon_name or item.emoticon_unique,
                    item.emoticon_name,
                    item.emoticon_url,
                    True,
                    combinable=item.emoticon_combinable,
                )
            )
        else:
            self._set_editor_emoticon(None)
        self.msg_weight_var.set(str(item.weight))
        self._update_message_buttons()

    def _set_editor_emoticon(self, emo: Emoticon | None) -> None:
        self._editor_emoticon = emo
        self.msg_text.configure(state="normal")
        if emo:
            self._emote_photo = self._emote_thumb(emo.unique, 22)
            if self._emote_photo:
                self.editor_emote_thumb.configure(image=self._emote_photo)
            else:
                self.editor_emote_thumb.configure(image="")
            if emo.combinable:
                self.editor_emote_var.set(f"已选表情：{_emote_label(emo.name)}（可拼文字）")
            else:
                self.editor_emote_var.set(f"已选表情：{_emote_label(emo.name)}（仅表情，不能拼文字）")
                self.msg_text.delete("1.0", "end")
                self.msg_text.configure(state="disabled")
            self.editor_emote_row.grid()
        else:
            self._emote_photo = None
            self.editor_emote_thumb.configure(image="")
            self.editor_emote_var.set("")
            self.editor_emote_row.grid_remove()
            self.msg_text.configure(state="normal")

    def _clear_editor_emote(self) -> None:
        self._set_editor_emoticon(None)

    def _read_editor(self) -> tuple[str, int]:
        text = " ".join(self.msg_text.get("1.0", "end").split())
        try:
            weight = int(self.msg_weight_var.get())
        except ValueError:
            weight = 1
        weight = max(0, weight)
        if len(text) > MAX_DANMAKU_LENGTH:
            log.warning("这条弹幕有 %d 个字，超过 B 站 %d 字上限", len(text), MAX_DANMAKU_LENGTH)
        return text, weight

    def _selected_index(self) -> int | None:
        selection = self.tree.selection()
        if not selection:
            return None
        index = int(selection[0])
        return index if 0 <= index < len(self.items) else None

    def _on_message_add(self) -> None:
        text, weight = self._read_editor()
        emo = self._editor_emoticon
        if not text and not emo:
            messagebox.showwarning("内容为空", "请先输入弹幕内容，或选择一个表情。")
            return
        if emo:
            if not emo.combinable and text:
                messagebox.showwarning("不能拼文字", f"表情「{_emote_label(emo.name)}」只能单独发送。")
                return
            self.items.append(
                MessageItem(text, weight, emo.unique, emo.url, emo.name, emo.combinable)
            )
        else:
            self.items.append(MessageItem(text, weight))
        self._refresh_message_tree()
        self._clear_editor()
        log.info("新增弹幕：%s（权重 %d）", text or f"表情 {_emote_label(emo.name) if emo else ''}", weight)

    def _on_message_update(self) -> None:
        index = self._selected_index()
        if index is None:
            messagebox.showinfo("未选中", "请先在列表里选中一条弹幕。")
            return
        text, weight = self._read_editor()
        emo = self._editor_emoticon
        if not text and not emo:
            messagebox.showwarning("内容为空", "请先输入弹幕内容，或保留一个表情。")
            return
        if emo:
            if not emo.combinable and text:
                messagebox.showwarning("不能拼文字", f"表情「{_emote_label(emo.name)}」只能单独发送。")
                return
            self.items[index] = MessageItem(
                text, weight, emo.unique, emo.url, emo.name, emo.combinable
            )
        else:
            self.items[index] = MessageItem(text, weight)
        self._refresh_message_tree()
        self.tree.selection_set(str(index))
        log.info("已更新第 %d 条弹幕：%s", index + 1, self.items[index].display_text)

    def _on_message_send(self) -> None:
        index = self._selected_index()
        if index is None:
            messagebox.showinfo("未选中", "请先在列表里选中一条弹幕或表情。")
            return
        item = self.items[index]
        emo = (
            Emoticon(
                item.emoticon_unique,
                item.emoticon_name or item.emoticon_unique,
                item.emoticon_name,
                item.emoticon_url,
                True,
                combinable=item.emoticon_combinable,
            )
            if item.is_emoticon
            else None
        )
        self._send_payload_async(
            item.text if (not emo or emo.combinable) else "",
            emo,
            target="library",
            display=item.display_text,
        )

    def _on_message_delete(self) -> None:
        index = self._selected_index()
        if index is None:
            messagebox.showinfo("未选中", "请先在列表里选中一条弹幕。")
            return
        item = self.items[index]
        if not messagebox.askyesno("删除弹幕", f"确定删除这条弹幕吗？\n\n{item.display_text}"):
            return
        del self.items[index]
        self._refresh_message_tree()
        self._clear_editor()
        log.info("已删除弹幕：%s", item.display_text)

    def _clear_editor(self) -> None:
        self.msg_text.configure(state="normal")
        self.msg_text.delete("1.0", "end")
        self.msg_weight_var.set("1")
        self._set_editor_emoticon(None)
        self.tree.selection_remove(*self.tree.selection())

    def _load_messages_into_state(self, initial: bool = False) -> bool:
        path = self._current_messages_path()
        if initial and not path.exists():
            default = resolve_path(DEFAULT_MESSAGES_FILE)
            if default.exists():
                log.warning("消息文件 %s 不存在，已回退到默认 %s", path, default)
                path = default
                self.messages_path_var.set(self._store_path(default))
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
        self.messages_path_var.set(self._store_path(Path(path)))
        self._load_messages_into_state()

    def _save_messages_quietly(self) -> None:
        if not self.items:
            return
        try:
            save_messages(self._current_messages_path(), self.items)
            self.cfg.messages_file = self._store_path(self._current_messages_path())
        except OSError as exc:
            log.warning("保存消息失败：%s", exc)

    def _save_messages_clicked(self) -> None:
        path = self._current_messages_path()
        try:
            save_messages(path, self.items)
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self.cfg.messages_file = self._store_path(path)
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
        self.messages_path_var.set(self._store_path(Path(path)))
        self.cfg.messages_file = self._store_path(Path(path))
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
        self.messages_path_var.set(self._store_path(Path(path)))
        self.cfg.messages_file = self._store_path(Path(path))
        self._refresh_message_tree()
        success(log, "已导入 %d 条弹幕：%s", len(items), path)

    @staticmethod
    def _open_folder() -> None:
        try:
            os.startfile(str(APP_DIR))  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("打开失败", str(exc))

    # ------------------------------------------------------------------ 字段工具
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
            value = self._active_task().interval_seconds
        value = min(max(value, MIN_INTERVAL_SECONDS), MAX_INTERVAL_SECONDS)
        self.interval_var.set(_format_number(value))

    @staticmethod
    def _color_label(value: int) -> str:
        for name, preset in COLOR_PRESETS:
            if preset == value:
                return f"{name} ({value})"
        return f"颜色 ({value})"

    @staticmethod
    def _parse_color(text: str) -> int:
        text = text.strip()
        hex_match = re.search(r"#[0-9a-fA-F]{6}", text)
        if hex_match:
            value = int(hex_match.group(0)[1:], 16)
        else:
            digits = re.findall(r"\d+", text)
            if not digits:
                raise ValueError("弹幕颜色请从下拉列表选择，或填写 0–16777215 的十进制值。")
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

    # ------------------------------------------------------------------ 配置保存
    def _save_config_quietly(self) -> None:
        try:
            self.cfg.sync_from_active_task()
            save_config(self.cfg.storage_copy())
        except OSError as exc:
            log.warning("保存配置失败：%s", exc)

    # ------------------------------------------------------------------ 日志
    def _append_log(self, level: str, message: str, emote_unique: str = "") -> None:
        tag = {
            "debug": "info", "info": "info", "success": "success",
            "warning": "warn", "warn": "warn", "error": "error", "critical": "error",
        }.get(level, "info")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] ", ("time",))
        self.log_text.insert("end", message, (tag,))
        if emote_unique:
            photo = self._emote_thumb(emote_unique, 18)
            if photo:
                self.log_text.insert("end", " ")
                self.log_text.image_create("end", image=photo)
                self._log_photos.append(photo)
                if len(self._log_photos) > 300:
                    del self._log_photos[:100]
        self.log_text.insert("end", "\n")
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

    def _on_tab_changed(self, _event: tk.Event) -> None:
        current = self.notebook.index("current")
        if current == 2:
            self._refresh_emote_header()
        elif current == 3:
            self._refresh_novel_summary()
        elif current == 5:
            self._refresh_history_header()
            self._start_history_live()
        elif self._history_live:
            self._stop_history_live()

    def _on_callback_error(self, exc_type: type, exc_value: BaseException, tb: Any) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, tb))
        log.error("界面出现未捕获异常：\n%s", text)
        messagebox.showerror("程序异常", f"界面出现未捕获异常：\n{exc_value}\n\n详情见运行日志。")

    def _on_close(self) -> None:
        self._stop_history_live(quiet=True)
        for runtime in list(self.runtimes.values()):
            runtime.runner.stop()
        for runtime in list(self.runtimes.values()):
            runtime.runner.join(2.0)
        self._sync_active_task_from_widgets(silent=True)
        self._save_messages_quietly()
        try:
            self.cfg.sync_from_active_task()
            save_config(self.cfg.storage_copy())
        except OSError as exc:
            log.warning("保存配置失败：%s", exc)
        if self._pump_id is not None:
            try:
                self.after_cancel(self._pump_id)
            except tk.TclError:
                pass
            self._pump_id = None
        self.destroy()


def _pick_color_mode(task: TaskConfig, config: DanmakuConfig) -> tuple[int, int]:
    """按接口能力挑一个可用颜色和模式，发送前兜底。"""
    color = task.color
    mode = task.danmaku_mode
    if config.fetched:
        colors = {item.value for item in config.enabled_colors}
        modes = {item.value for item in config.enabled_modes}
        if color not in colors:
            color = 16777215 if 16777215 in colors else next(iter(colors), 16777215)
        if mode not in modes:
            mode = 1 if 1 in modes else next(iter(modes), 1)
    return color, mode


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", (value or "").strip()).strip("._")
    return cleaned[:60] or "emoticon"


def _emote_label(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("[") and text.endswith("]"):
        return text[1:-1] or text
    return text


def _format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def run() -> None:
    """启动界面主循环。"""
    app = BiliSendApp()
    app.mainloop()
