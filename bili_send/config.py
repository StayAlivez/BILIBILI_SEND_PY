"""配置读写：config.json 的加载/保存，以及项目内相对路径的解析。

v1.2 起支持多任务：每个任务（直播间）有独立的 Cookie、间隔、内容来源。
配置文件同时保留一份“当前任务”的顶层字段，方便旧版本配置自动迁移。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 项目根目录（bili_send 的上一级）
APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = APP_DIR / "config.json"
CACHE_DIR = APP_DIR / "cache"
DEFAULT_MESSAGES_FILE = "messages/weighted.json"

MODE_WEIGHTED = "weighted"
MODE_UNIFORM = "uniform"
MODES = (MODE_WEIGHTED, MODE_UNIFORM)
MODE_LABELS = {MODE_WEIGHTED: "按权重随机", MODE_UNIFORM: "完全随机"}

SOURCE_POOL = "pool"
SOURCE_NOVEL = "novel"
SOURCES = (SOURCE_POOL, SOURCE_NOVEL)
SOURCE_LABELS = {SOURCE_POOL: "弹幕池", SOURCE_NOVEL: "说书人"}

MIN_INTERVAL_SECONDS = 1.0
MAX_INTERVAL_SECONDS = 3600.0
MIN_FONT_SIZE = 12
MAX_FONT_SIZE = 64
MAX_COLOR = 0xFFFFFF
MIN_NOVEL_CHUNK = 5
MAX_NOVEL_CHUNK = 20
DEFAULT_ROOM_ID = 22676119


def resolve_path(value: str | Path) -> Path:
    """把配置里的相对路径解析到项目根目录。"""
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = APP_DIR / path
    return path.resolve()


def new_task_id() -> str:
    return uuid.uuid4().hex[:10]


@dataclass
class TaskConfig:
    """一个直播间的发送任务（多账号 / 多直播间）。"""

    id: str = ""
    name: str = ""
    room_id: int = DEFAULT_ROOM_ID
    cookie: str = ""
    interval_seconds: float = 30.0
    mode: str = MODE_WEIGHTED
    source: str = SOURCE_POOL
    messages_file: str = DEFAULT_MESSAGES_FILE
    novel_path: str = ""
    novel_index: int = 0
    novel_chunk_size: int = MAX_NOVEL_CHUNK
    color: int = 5566168
    fontsize: int = 25
    danmaku_mode: int = 1
    avoid_repeat: bool = True
    # 仅用于界面回显
    room_title: str = ""
    anchor_name: str = ""

    @property
    def display_name(self) -> str:
        return self.name or f"直播间 {self.room_id}"

    @property
    def messages_path(self) -> Path:
        return resolve_path(self.messages_file)

    @property
    def novel_file(self) -> Path | None:
        return resolve_path(self.novel_path) if self.novel_path else None

    def copy(self) -> "TaskConfig":
        return TaskConfig.from_dict(asdict(self))

    def storage_copy(self) -> "TaskConfig":
        cfg = self.copy()
        cfg.cookie = ""
        return cfg

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskConfig":
        cfg = cls()
        for item in fields(cls):
            if item.name not in data:
                continue
            value = data[item.name]
            try:
                if item.name in _TASK_INT_FIELDS:
                    value = int(value)
                elif item.name in _TASK_FLOAT_FIELDS:
                    value = float(value)
                elif item.name in _TASK_BOOL_FIELDS:
                    value = bool(value)
                elif item.name in _TASK_STR_FIELDS:
                    value = "" if value is None else str(value)
            except (TypeError, ValueError):
                log.warning("任务配置项 %s=%r 无法解析，使用默认值", item.name, value)
                continue
            setattr(cfg, item.name, value)
        _normalize_task(cfg)
        return cfg

    @classmethod
    def from_legacy(cls, app: "AppConfig", name: str = "") -> "TaskConfig":
        return cls(
            id=new_task_id(),
            name=name or f"直播间 {app.room_id}",
            room_id=app.room_id,
            cookie=app.cookie,
            interval_seconds=app.interval_seconds,
            mode=app.mode,
            messages_file=app.messages_file,
            color=app.color,
            fontsize=app.fontsize,
            danmaku_mode=app.danmaku_mode,
            avoid_repeat=app.avoid_repeat,
            room_title=app.room_title,
            anchor_name=app.anchor_name,
        )


@dataclass
class AppConfig:
    """全局配置 + 任务列表。"""

    cookie: str = ""
    save_cookie: bool = True
    room_id: int = DEFAULT_ROOM_ID
    interval_seconds: float = 30.0
    mode: str = MODE_WEIGHTED
    messages_file: str = DEFAULT_MESSAGES_FILE
    avoid_repeat: bool = True
    color: int = 5566168
    fontsize: int = 25
    danmaku_mode: int = 1
    timeout_seconds: float = 5.0
    room_title: str = ""
    anchor_name: str = ""
    tasks: list[TaskConfig] = field(default_factory=list)
    active_task_id: str = ""

    # ------------------------------------------------------------------ 任务
    @property
    def messages_path(self) -> Path:
        return resolve_path(self.messages_file)

    @property
    def active_task(self) -> TaskConfig:
        for task in self.tasks:
            if task.id == self.active_task_id:
                return task
        if not self.tasks:
            self.tasks = [TaskConfig.from_legacy(self, "主直播间")]
            self.active_task_id = self.tasks[0].id
        self.active_task_id = self.tasks[0].id
        return self.tasks[0]

    def task_by_id(self, task_id: str) -> TaskConfig | None:
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    def add_task(self, task: TaskConfig | None = None) -> TaskConfig:
        task = task or TaskConfig.from_legacy(self, f"直播间 {self.room_id}")
        task.id = task.id or new_task_id()
        task.name = task.name or f"直播间 {task.room_id}"
        self.tasks.append(task)
        self.active_task_id = task.id
        return task

    def remove_task(self, task_id: str) -> bool:
        if len(self.tasks) <= 1:
            return False
        for index, task in enumerate(self.tasks):
            if task.id == task_id:
                del self.tasks[index]
                if self.active_task_id == task_id:
                    self.active_task_id = self.tasks[max(0, index - 1)].id
                return True
        return False

    def sync_from_active_task(self) -> None:
        """把当前任务镜像到顶层字段，兼容旧版配置读取。"""
        task = self.active_task
        self.room_id = task.room_id
        self.cookie = task.cookie
        self.interval_seconds = task.interval_seconds
        self.mode = task.mode
        self.color = task.color
        self.fontsize = task.fontsize
        self.danmaku_mode = task.danmaku_mode
        self.avoid_repeat = task.avoid_repeat
        self.room_title = task.room_title
        self.anchor_name = task.anchor_name

    # ------------------------------------------------------------------ 读写
    def copy(self) -> "AppConfig":
        """返回一份独立副本，避免发送线程读到界面后续修改的值。"""
        return AppConfig.from_dict(asdict(self))

    def storage_copy(self) -> "AppConfig":
        """写盘用的副本：关闭「保存 Cookie」时清空所有任务的 Cookie。"""
        cfg = self.copy()
        if not cfg.save_cookie:
            cfg.cookie = ""
            for task in cfg.tasks:
                task.cookie = ""
        return cfg

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        cfg = cls()
        for item in fields(cls):
            if item.name in ("tasks", "active_task_id") or item.name not in data:
                continue
            value = data[item.name]
            try:
                if item.name in _INT_FIELDS:
                    value = int(value)
                elif item.name in _FLOAT_FIELDS:
                    value = float(value)
                elif item.name in _BOOL_FIELDS:
                    value = bool(value)
                elif item.name in _STR_FIELDS:
                    value = "" if value is None else str(value)
            except (TypeError, ValueError):
                log.warning("配置项 %s=%r 无法解析，使用默认值", item.name, value)
                continue
            setattr(cfg, item.name, value)

        _normalize_app(cfg)

        raw_tasks = data.get("tasks")
        if isinstance(raw_tasks, list):
            for item in raw_tasks:
                if isinstance(item, dict):
                    cfg.tasks.append(TaskConfig.from_dict(item))
        if not cfg.tasks:
            cfg.tasks = [TaskConfig.from_legacy(cfg, f"直播间 {cfg.room_id}")]

        active = str(data.get("active_task_id") or "")
        ids = {task.id for task in cfg.tasks}
        cfg.active_task_id = active if active in ids else cfg.tasks[0].id
        return cfg


def _normalize_task(task: TaskConfig) -> None:
    task.id = task.id or new_task_id()
    task.room_id = task.room_id if task.room_id > 0 else DEFAULT_ROOM_ID
    task.name = task.name.strip() or f"直播间 {task.room_id}"
    task.mode = task.mode if task.mode in MODES else MODE_WEIGHTED
    task.source = task.source if task.source in SOURCES else SOURCE_POOL
    task.interval_seconds = _clamp(task.interval_seconds, MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS)
    task.fontsize = int(_clamp(task.fontsize, MIN_FONT_SIZE, MAX_FONT_SIZE))
    task.color = int(_clamp(task.color, 0, MAX_COLOR))
    task.novel_chunk_size = int(_clamp(task.novel_chunk_size, MIN_NOVEL_CHUNK, MAX_NOVEL_CHUNK))
    task.novel_index = max(0, task.novel_index)


def _normalize_app(cfg: AppConfig) -> None:
    cfg.mode = cfg.mode if cfg.mode in MODES else MODE_WEIGHTED
    cfg.interval_seconds = _clamp(cfg.interval_seconds, MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS)
    cfg.fontsize = int(_clamp(cfg.fontsize, MIN_FONT_SIZE, MAX_FONT_SIZE))
    cfg.color = int(_clamp(cfg.color, 0, MAX_COLOR))
    cfg.timeout_seconds = _clamp(cfg.timeout_seconds, 1.0, 30.0)
    if cfg.room_id <= 0:
        cfg.room_id = DEFAULT_ROOM_ID


_INT_FIELDS = {"room_id", "color", "fontsize", "danmaku_mode"}
_FLOAT_FIELDS = {"interval_seconds", "timeout_seconds"}
_BOOL_FIELDS = {"avoid_repeat", "save_cookie"}
_STR_FIELDS = {"cookie", "mode", "messages_file", "room_title", "anchor_name"}

_TASK_INT_FIELDS = {"room_id", "color", "fontsize", "danmaku_mode", "novel_index", "novel_chunk_size"}
_TASK_FLOAT_FIELDS = {"interval_seconds"}
_TASK_BOOL_FIELDS = {"avoid_repeat"}
_TASK_STR_FIELDS = {
    "id", "name", "cookie", "mode", "source", "messages_file",
    "novel_path", "room_title", "anchor_name",
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    """读取配置；文件缺失或损坏时回退到默认值，不让程序启动失败。"""
    if not path.exists():
        log.info("未找到 %s，使用默认配置", path.name)
        return AppConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        log.warning("读取 %s 失败：%s，使用默认配置", path.name, exc)
        return AppConfig()
    except ValueError as exc:
        backup = path.with_suffix(path.suffix + ".bad")
        try:
            path.replace(backup)
            log.warning("配置文件不是合法 JSON（%s），已备份为 %s", exc, backup.name)
        except OSError:
            log.warning("配置文件不是合法 JSON（%s），使用默认配置", exc)
        return AppConfig()
    if not isinstance(raw, dict):
        log.warning("%s 的内容不是 JSON 对象，使用默认配置", path.name)
        return AppConfig()
    return AppConfig.from_dict(raw)


def save_config(cfg: AppConfig, path: Path = CONFIG_PATH) -> None:
    """原子写入配置，写入失败时抛 OSError 交给界面提示。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
