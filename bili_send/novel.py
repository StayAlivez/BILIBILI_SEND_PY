"""说书人模式：把小说文本切成不超过 20 字的弹幕，按顺序朗读。

只做文本处理，发送进度由任务配置里的 ``novel_index`` 保存。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MAX_CHUNK = 20
_BREAK_CHARS = "。！？!?；;…，,、：: "
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")


class NovelError(Exception):
    """小说读取或切分失败。"""


def read_text(path: Path) -> str:
    for encoding in _ENCODINGS:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            raise NovelError(f"读取小说失败：{exc}") from exc
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        raise NovelError(f"读取小说失败：{exc}") from exc


def split_paragraph(paragraph: str, size: int = MAX_CHUNK) -> list[str]:
    """按标点优先、长度兜底把一段文字切成 ≤ size 的小段。"""
    size = max(1, min(int(size), MAX_CHUNK))
    text = " ".join(paragraph.split())
    if not text:
        return []

    chunks: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(length, start + size)
        if end < length:
            # 优先在长度范围内最后一个标点后断开，避免把词切断
            for index in range(end - 1, start, -1):
                if text[index] in _BREAK_CHARS:
                    end = index + 1
                    break
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        start = end
    return chunks


def split_text(text: str, size: int = MAX_CHUNK) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    chunks: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        chunks.extend(split_paragraph(line, size))
    return chunks


@dataclass
class NovelBook:
    path: Path
    chunks: list[str]

    def __len__(self) -> int:
        return len(self.chunks)

    @property
    def title(self) -> str:
        return self.path.stem


def load_book(path: Path, size: int = MAX_CHUNK) -> NovelBook:
    path = Path(path)
    if not path.exists():
        raise NovelError(f"小说文件不存在：{path}")
    text = read_text(path)
    chunks = split_text(text, size)
    if not chunks:
        raise NovelError("小说里没有可发送的文字")
    log.info("已加载小说《%s》：%d 段（每段 ≤ %d 字）", path.stem, len(chunks), size)
    return NovelBook(path=path, chunks=chunks)


def clamp_index(index: int, total: int) -> int:
    if total <= 0:
        return 0
    return max(0, min(int(index), total - 1))


def preview(text: str, width: int = 24) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= width else text[: width - 1] + "…"
