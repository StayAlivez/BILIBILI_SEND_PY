"""日志汇聚：文件日志 + 界面日志共用同一条 logging 通道。

发送线程、网络层只负责 ``log.info`` 等调用；界面通过 ``QueueLogHandler``
把同一条记录显示到「运行日志」页签，因此不需要额外的事件通道。
"""

from __future__ import annotations

import logging
import queue
from typing import Any

# 成功级别：用于让「发送成功」在界面日志里显示为绿色
SUCCESS_LEVEL = 25
logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")

# 界面轮询消费的日志队列
LOG_QUEUE: "queue.Queue[dict[str, Any]]" = queue.Queue()


def success(logger: logging.Logger, message: str, *args: Any, **kwargs: Any) -> None:
    """以 SUCCESS 级别记录一条日志。"""
    logger.log(SUCCESS_LEVEL, message, *args, **kwargs)


class QueueLogHandler(logging.Handler):
    """把日志记录投递到界面队列。"""

    def __init__(self, target: "queue.Queue[dict[str, Any]]" = LOG_QUEUE) -> None:
        super().__init__()
        self.target = target

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.target.put({"level": record.levelname.lower(), "message": self.format(record)})
        except Exception:  # noqa: BLE001 - 日志本身不能影响主流程
            self.handleError(record)
