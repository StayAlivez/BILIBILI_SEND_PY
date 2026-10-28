"""配置读写：config.json 的加载/保存，以及项目内相对路径的解析。

配置结构与 Java 版 application.yml 中的 ``BiLiBiLi`` 段一一对应：
cookie、房间号、发送间隔、弹幕样式、消息列表文件。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 项目根目录（bili_send 的上一级）
APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_MESSAGES_FILE = "messages/weighted.json"

MODE_WEIGHTED = "weighted"
MODE_UNIFORM = "uniform"
MODES = (MODE_WEIGHTED, MODE_UNIFORM)
MODE_LABELS = {MODE_WEIGHTED: "按权重随机", MODE_UNIFORM: "完全随机"}

MIN_INTERVAL_SECONDS = 1.0
MAX_INTERVAL_SECONDS = 3600.0
MIN_FONT_SIZE = 12
MAX_FONT_SIZE = 64
MAX_COLOR = 0xFFFFFF


def resolve_path(value: str | Path) -> Path:
    """把配置里的相对路径解析到项目根目录。"""
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = APP_DIR / path
    return path.resolve()


@dataclass
class AppConfig:
    """界面与发送线程共享的全部配置。"""

    cookie: str = ""
    room_id: int = 22676119
    interval_seconds: float = 30.0
    mode: str = MODE_WEIGHTED
    messages_file: str = DEFAULT_MESSAGES_FILE
    avoid_repeat: bool = True
    color: int = 5566168
    fontsize: int = 25
    danmaku_mode: int = 1
    timeout_seconds: float = 5.0
    # 仅用于界面回显上一次连接到的直播间信息
    room_title: str = ""
    anchor_name: str = ""

    @property
    def messages_path(self) -> Path:
        return resolve_path(self.messages_file)

    def copy(self) -> "AppConfig":
        """返回一份独立副本，避免发送线程读到界面后续修改的值。"""
        return AppConfig.from_dict(asdict(self))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        cfg = cls()
        for field in fields(cls):
            if field.name not in data:
                continue
            value = data[field.name]
            try:
                if field.name in _INT_FIELDS:
                    value = int(value)
                elif field.name in _FLOAT_FIELDS:
                    value = float(value)
                elif field.name in _BOOL_FIELDS:
                    value = bool(value)
                elif field.name in _STR_FIELDS:
                    value = "" if value is None else str(value)
            except (TypeError, ValueError):
                log.warning("配置项 %s=%r 无法解析，使用默认值", field.name, value)
                continue
            setattr(cfg, field.name, value)

        cfg.mode = cfg.mode if cfg.mode in MODES else MODE_WEIGHTED
        cfg.interval_seconds = _clamp(cfg.interval_seconds, MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS)
        cfg.fontsize = int(_clamp(cfg.fontsize, MIN_FONT_SIZE, MAX_FONT_SIZE))
        cfg.color = int(_clamp(cfg.color, 0, MAX_COLOR))
        cfg.timeout_seconds = _clamp(cfg.timeout_seconds, 1.0, 30.0)
        if cfg.room_id <= 0:
            cfg.room_id = 22676119
        return cfg


_INT_FIELDS = {"room_id", "color", "fontsize", "danmaku_mode"}
_FLOAT_FIELDS = {"interval_seconds", "timeout_seconds"}
_BOOL_FIELDS = {"avoid_repeat"}
_STR_FIELDS = {"cookie", "mode", "messages_file", "room_title", "anchor_name"}


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
