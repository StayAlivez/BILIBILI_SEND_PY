"""弹幕消息库：解析 JSON 消息文件，并按权重或等概率抽取。

兼容两种写法（原 Java 项目里三种 json 都存在）：

* 纯文本数组：``["好听", "666"]``
* 带权重数组：``[{"msg": "好听", "weight": 2}]``
* 单个对象：``{"msg": "好听", "weight": 1}``
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import MODE_UNIFORM, MODE_WEIGHTED

log = logging.getLogger(__name__)

# B 站普通用户单条弹幕的长度上限（字符数）
MAX_DANMAKU_LENGTH = 20


class MessageFileError(Exception):
    """消息文件不存在、格式错误或没有可用内容。"""


@dataclass
class MessageItem:
    text: str
    weight: int = 1
    emoticon_unique: str = ""
    emoticon_url: str = ""
    emoticon_name: str = ""
    emoticon_combinable: bool = False

    @property
    def is_emoticon(self) -> bool:
        return bool(self.emoticon_unique)

    @property
    def display_text(self) -> str:
        if self.is_emoticon:
            name = (self.emoticon_name or self.emoticon_unique).strip()
            if name.startswith("[") and name.endswith("]"):
                name = name[1:-1]
            return f"表情：{name}" + (f" + {self.text}" if self.text else "")
        return self.text

    @property
    def usable(self) -> bool:
        """权重 > 0 且内容非空（或带表情）才会参与抽取。"""
        return self.weight > 0 and (bool(self.text.strip()) or self.is_emoticon)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"msg": self.text, "weight": self.weight}
        if self.is_emoticon:
            data["emoticon_unique"] = self.emoticon_unique
            data["emoticon_url"] = self.emoticon_url
            data["emoticon_name"] = self.emoticon_name
            data["emoticon_combinable"] = self.emoticon_combinable
        return data


def load_messages(path: Path) -> list[MessageItem]:
    """读取消息文件；格式不对时抛 :class:`MessageFileError`。"""
    if not path.exists():
        raise MessageFileError(f"消息文件不存在：{path}")
    try:
        raw_text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise MessageFileError(f"读取消息文件失败：{exc}") from exc
    try:
        data = json.loads(raw_text)
    except ValueError as exc:
        raise MessageFileError(f"JSON 格式错误：{exc}") from exc

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise MessageFileError('消息文件应为 JSON 数组，或单个 {"msg": ..., "weight": ...} 对象')

    items: list[MessageItem] = []
    for index, entry in enumerate(data, start=1):
        item = _parse_entry(entry, index)
        if item is not None:
            items.append(item)
    if not items:
        raise MessageFileError("消息文件里没有可用的弹幕内容")
    return items


def _parse_entry(entry: Any, index: int) -> MessageItem | None:
    emoticon_unique = ""
    emoticon_url = ""
    emoticon_name = ""
    emoticon_combinable = False
    if isinstance(entry, str):
        text, weight = entry, 1
    elif isinstance(entry, dict):
        text = entry.get("msg") or entry.get("text") or entry.get("content") or ""
        weight = entry.get("weight", 1)
        emoticon_unique = str(entry.get("emoticon_unique") or "")
        emoticon_url = str(entry.get("emoticon_url") or "")
        emoticon_name = str(entry.get("emoticon_name") or "")
        if "emoticon_combinable" in entry:
            emoticon_combinable = bool(entry.get("emoticon_combinable"))
        elif emoticon_unique and text:
            # 旧数据没有该字段：带文字的旧表情按可拼接处理，避免丢字
            emoticon_combinable = True
        try:
            weight = int(weight)
        except (TypeError, ValueError):
            log.warning("第 %d 条消息的权重 %r 不是整数，按 1 处理", index, weight)
            weight = 1
    else:
        log.warning("第 %d 条消息类型不支持（%s），已跳过", index, type(entry).__name__)
        return None

    # 弹幕不能换行，顺手把连续空白压成一个空格
    text = " ".join(str(text).split())
    if not text and not emoticon_unique:
        log.warning("第 %d 条消息内容为空，已跳过", index)
        return None
    if text and len(text) > MAX_DANMAKU_LENGTH:
        log.warning(
            "第 %d 条消息有 %d 个字，超过 B 站 %d 字上限，可能发送失败：%s",
            index, len(text), MAX_DANMAKU_LENGTH, text[:12] + "…",
        )
    return MessageItem(text, weight, emoticon_unique, emoticon_url, emoticon_name, emoticon_combinable)


def save_messages(path: Path, items: Iterable[MessageItem]) -> None:
    """把消息列表写回 JSON（统一使用带权重的格式）。"""
    payload = [item.to_dict() for item in items]
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


class MessagePool:
    """按配置的策略抽取下一条弹幕。"""

    def __init__(
        self,
        items: Sequence[MessageItem],
        mode: str = MODE_WEIGHTED,
        avoid_repeat: bool = True,
        rng: random.Random | None = None,
    ) -> None:
        self._items = [item for item in items if item.usable]
        if not self._items:
            raise ValueError("没有可用的弹幕：请检查内容是否为空、权重是否都小于等于 0")
        self.mode = mode if mode in (MODE_WEIGHTED, MODE_UNIFORM) else MODE_WEIGHTED
        self.avoid_repeat = avoid_repeat
        self._random = rng or random.Random()
        self._last: MessageItem | None = None

    def __len__(self) -> int:
        return len(self._items)

    @property
    def total_weight(self) -> int:
        return sum(item.weight for item in self._items)

    def pick(self) -> MessageItem:
        candidates = self._items
        weights = [item.weight for item in candidates]
        if self.avoid_repeat and len(candidates) > 1 and self._last is not None:
            pairs = [(item, item.weight) for item in candidates if item is not self._last]
            candidates = [pair[0] for pair in pairs]
            weights = [pair[1] for pair in pairs]

        if self.mode == MODE_UNIFORM:
            chosen = self._random.choice(candidates)
        else:
            chosen = self._random.choices(candidates, weights=weights, k=1)[0]
        self._last = chosen
        return chosen
