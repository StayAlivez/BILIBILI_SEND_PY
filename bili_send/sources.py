"""发送内容来源：弹幕池（随机）与说书人（按顺序）。

Runner 只依赖 :class:`MessageSource` 这三个方法，方便以后扩展。
"""

from __future__ import annotations

from typing import Callable, Protocol

from .messages import MessageItem, MessagePool
from .novel import NovelBook, clamp_index


class MessageSource(Protocol):
    def next(self) -> MessageItem: ...

    def advance(self) -> None: ...

    @property
    def finished(self) -> bool: ...

    def progress_text(self) -> str: ...


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
