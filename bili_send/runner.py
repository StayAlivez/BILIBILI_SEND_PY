"""后台发送线程：按设定间隔抽取弹幕并发送。

界面通过事件队列拿到状态、房间信息、进度与倒计时；
真正的停止使用 ``threading.Event``，点击「停止」后最多等一次请求超时。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

from .client import BilibiliClient, BilibiliError, RoomInfo
from .config import MODE_LABELS, AppConfig
from .logbus import success
from .messages import MessagePool

log = logging.getLogger(__name__)

# 连续失败多少次后自动停止，避免白白刷请求
MAX_CONSECUTIVE_FAILURES = 5


class DanmakuRunner:
    """一个直播间对应一个 Runner，停止后不可复用。"""

    def __init__(
        self,
        config: AppConfig,
        pool: MessagePool,
        events: "queue.Queue[dict[str, Any]]",
        client: BilibiliClient | None = None,
    ) -> None:
        self._config = config
        self._pool = pool
        self._events = events
        self._client = client or BilibiliClient(config.cookie, config.timeout_seconds)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="danmaku-sender", daemon=True)
        self.total = 0
        self.ok = 0
        self.failed = 0
        self.next_send_at: float | None = None
        self.last_error: str | None = None

    # ---------------------------------------------------------------- 生命周期
    @property
    def running(self) -> bool:
        return self._thread.is_alive() and not self._stop.is_set()

    def start(self) -> None:
        if self._thread.is_alive():
            return
        self.emit("status", state="starting")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float = 3.0) -> None:
        if self._thread.is_alive():
            self._thread.join(timeout)

    def emit(self, kind: str, **payload: Any) -> None:
        data: dict[str, Any] = {"kind": kind, **payload}
        data.setdefault("time", time.time())
        self._events.put(data)

    # -------------------------------------------------------------------- 主循环
    def _loop(self) -> None:
        cfg = self._config
        client = self._client
        room: RoomInfo | None = None
        error: str | None = None

        try:
            room = client.resolve_room(cfg.room_id)
        except BilibiliError as exc:
            error = f"获取直播间信息失败：{exc}"
        except Exception as exc:  # noqa: BLE001 - 后台线程绝不能把异常抛给 Tk
            error = f"获取直播间信息失败：{exc}"
            log.exception("解析直播间时出现未预期错误")

        if room is not None:
            status = client.check_login()
            if status.known and not status.logged_in:
                error = f"Cookie 已失效，B 站未登录：{status.message}。请重新复制 Cookie 后再发送。"
            elif status.known:
                log.info("登录账号：%s", status.uname or "已登录")
            else:
                log.warning("无法确认登录状态（%s），直接尝试发送。", status.message)

        if room is not None and error is None:
            self.emit("room", room=room)
            log.info(
                "开始向直播间 %s（%s）发送：间隔 %g 秒，%s，共 %d 条可用弹幕",
                room.room_id,
                room.title or "标题未知",
                cfg.interval_seconds,
                MODE_LABELS.get(cfg.mode, cfg.mode),
                len(self._pool),
            )
            self.emit("status", state="running")
            error = self._send_loop(cfg, room)

        self.emit(
            "status",
            state="stopped",
            message=error,
            total=self.total,
            ok=self.ok,
            failed=self.failed,
        )
        self.last_error = error
        self.next_send_at = None
        if error:
            log.error("发送已停止：%s", error)
        else:
            log.info("发送已停止：共 %d 条，成功 %d 条，失败 %d 条", self.total, self.ok, self.failed)

    def _send_loop(self, cfg: AppConfig, room: RoomInfo) -> str | None:
        """发送循环；返回非 None 表示异常停止的原因。"""
        streak = 0
        while not self._stop.is_set():
            item = self._pool.pick()
            result = self._client.send_danmaku(
                item.text,
                room.room_id,
                color=cfg.color,
                fontsize=cfg.fontsize,
                mode=cfg.danmaku_mode,
            )
            self.total += 1

            if result.ok:
                self.ok += 1
                streak = 0
                success(log, "第 %d 条发送成功：%s", self.total, item.text)
            else:
                self.failed += 1
                streak += 1
                log.error("第 %d 条发送失败：%s", self.total, result.detail)
                if result.fatal:
                    return "登录态已失效，已自动停止发送。请更新 Cookie 后重新启动。"
                if streak >= MAX_CONSECUTIVE_FAILURES:
                    return f"连续 {streak} 次发送失败，已自动停止发送。"

            self.emit("progress", total=self.total, ok=self.ok, failed=self.failed)

            self.next_send_at = time.monotonic() + cfg.interval_seconds
            self.emit("schedule", next_at=self.next_send_at, interval=cfg.interval_seconds)
            if self._stop.wait(cfg.interval_seconds):
                break
        return None
