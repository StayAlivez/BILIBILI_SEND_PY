"""发送内容来源：弹幕池（随机）与说书人（按顺序）。

Runner 只依赖 :class:`MessageSource` 这三个方法，方便以后扩展。
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Callable, Protocol

from .client import BilibiliClient, BilibiliError
from .messages import MAX_DANMAKU_LENGTH, MessageItem, MessagePool
from .novel import NovelBook, clamp_index

log = logging.getLogger(__name__)


class MessageSource(Protocol):
    def next(self) -> MessageItem | None: ...

    def advance(self) -> None: ...

    @property
    def finished(self) -> bool: ...

    def progress_text(self) -> str: ...


def _parse_timeline(value: str) -> datetime | None:
    text = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%H:%M:%S":
            now = datetime.now()
            parsed = parsed.replace(year=now.year, month=now.month, day=now.day)
        return parsed
    return None


class PoolSource:
    """从消息库里按权重/等概率抽取。"""

    def __init__(self, pool: MessagePool) -> None:
        self.pool = pool

    def next(self) -> MessageItem:
        return self.pool.pick()

    def advance(self) -> None:
        return None

    @property
    def finished(self) -> bool:
        return False

    def progress_text(self) -> str:
        return f"弹幕池 {len(self.pool)} 条"


class RepeatSource:
    """复读机：统计最近 ``window_seconds`` 秒内频率最高的弹幕并复读。

    Runner 在房间解析完成后调用 :meth:`bind`，把已登录的客户端传进来。
    没有合适弹幕时返回 ``None``，Runner 会等到下一个间隔再试。
    """

    def __init__(self, window_seconds: float = 30.0, limit: int = 100) -> None:
        self.window_seconds = max(1.0, float(window_seconds))
        self.limit = max(20, int(limit))
        self.client: BilibiliClient | None = None
        self.room_id = 0
        self.last_text = ""
        self.last_count = 0
        self.sent = 0
        self.last_error = ""

    def bind(self, client: BilibiliClient, room_id: int) -> "RepeatSource":
        self.client = client
        self.room_id = int(room_id)
        return self

    def next(self) -> MessageItem | None:
        if self.client is None or not self.room_id:
            return None
        try:
            items = self.client.get_room_history(self.room_id, self.limit)
        except BilibiliError as exc:
            self.last_error = str(exc)
            log.warning("复读机获取直播弹幕失败：%s", exc)
            return None
        cutoff = datetime.now() - timedelta(seconds=self.window_seconds)
        own_uid = str(getattr(self.client, "uid", "") or "")
        counts: Counter[str] = Counter()
        latest: dict[str, int] = {}
        for index, item in enumerate(items):
            if own_uid and item.uid == own_uid:
                continue
            if item.dm_type == 1:
                continue
            text = item.text.strip()
            if not text or len(text) > MAX_DANMAKU_LENGTH:
                continue
            when = _parse_timeline(item.timeline)
            if when is not None and when < cutoff:
                continue
            counts[text] += 1
            latest[text] = index
        if not counts:
            self.last_text = ""
            self.last_count = 0
            return None
        best = max(counts, key=lambda text: (counts[text], latest[text]))
        self.last_text = best
        self.last_count = counts[best]
        return MessageItem(best)

    def advance(self) -> None:
        self.sent += 1

    @property
    def finished(self) -> bool:
        return False

    def progress_text(self) -> str:
        if self.last_text:
            return f"复读机：最近 {self.window_seconds:g} 秒最高频「{self.last_text}」×{self.last_count}"
        return f"复读机：等待最近 {self.window_seconds:g} 秒的直播间弹幕…"


class NovelSource:
    """按顺序朗读小说；每成功发送一条就前进一段并回调保存进度。"""

    def __init__(
        self,
        book: NovelBook,
        index: int = 0,
        on_advance: Callable[[int], None] | None = None,
    ) -> None:
        self.book = book
        self.index = clamp_index(index, len(book))
        self.on_advance = on_advance

    def next(self) -> MessageItem:
        if self.finished:
            raise StopIteration("小说已经播完")
        return MessageItem(self.book.chunks[self.index])

    def advance(self) -> None:
        if self.index < len(self.book.chunks):
            self.index += 1
            if self.on_advance:
                self.on_advance(self.index)

    @property
    def finished(self) -> bool:
        return self.index >= len(self.book.chunks)

    @property
    def total(self) -> int:
        return len(self.book.chunks)

    def progress_text(self) -> str:
        total = max(1, len(self.book.chunks))
        done = min(self.index, len(self.book.chunks))
        percent = int(done * 100 / total)
        if done >= len(self.book.chunks):
            return f"《{self.book.title}》已播完（{len(self.book.chunks)} 段）"
        return f"《{self.book.title}》第 {done + 1}/{len(self.book.chunks)} 段（{percent}%）"
