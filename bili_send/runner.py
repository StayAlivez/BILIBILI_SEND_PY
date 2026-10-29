"""后台发送线程：每个任务一个线程，各自使用自己的 Cookie 和内容来源。

支持：
* 弹幕池随机 / 按权重抽取；
* 说书人按顺序朗读（每发一条前进一段，并回调保存进度）；
* 发送前检查 B 站返回的可用颜色/模式（顶部/底部、彩色有身份门槛）；
* 直播间专属表情（带 ``emoticon_unique`` 发送）。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

from .client import BilibiliClient, BilibiliError, DanmakuConfig, RoomInfo
from .config import MODE_LABELS, AppConfig, TaskConfig
from .logbus import success
from .messages import MessagePool
from .sources import MessageSource, PoolSource

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 5


class DanmakuRunner:
    """一个任务对应一个 Runner；停止后不可复用。"""

    def __init__(
        self,
        task: TaskConfig | AppConfig,
        source: MessageSource | MessagePool,
        events: "queue.Queue[dict[str, Any]]",
        client: BilibiliClient | None = None,
        on_progress: Callable[[str, int], None] | None = None,
    ) -> None:
        if isinstance(task, AppConfig):
            task = task.active_task.copy()
        if isinstance(source, MessagePool):
            source = PoolSource(source)
        self.task = task.copy()
        self.task_id = self.task.id
        self.source = source
        self._events = events
        self._client = client or BilibiliClient(self.task.cookie, timeout=5.0)
        self._on_progress = on_progress
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"sender-{self.task_id}", daemon=True)
        self.total = 0
        self.ok = 0
        self.failed = 0
        self.next_send_at: float | None = None
        self.last_error: str | None = None
        self.completed = False
        self.dm_config = DanmakuConfig()
        self.effective_color = self.task.color
        self.effective_mode = self.task.danmaku_mode

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
        data: dict[str, Any] = {
            "kind": kind,
            "task_id": self.task_id,
            "task_name": self.task.display_name,
            **payload,
        }
        self._events.put(data)

    @property
    def prefix(self) -> str:
        return f"[{self.task.display_name}] "

    # ------------------------------------------------------------------ 主循环
    def _loop(self) -> None:
        task = self.task
        client = self._client
        room: RoomInfo | None = None
        error: str | None = None
        completed = False

        try:
            room = client.resolve_room(task.room_id)
        except BilibiliError as exc:
            error = f"获取直播间信息失败：{exc}"
        except Exception as exc:  # noqa: BLE001 - 后台线程不能把异常抛给 Tk
            error = f"获取直播间信息失败：{exc}"
            log.exception("%s解析直播间时出现未预期错误", self.prefix)

        if room is not None:
            status = client.check_login()
            if status.known and not status.logged_in:
                error = f"Cookie 已失效，B 站未登录：{status.message}。请重新复制 Cookie。"
            elif status.known:
                log.info("%s登录账号：%s", self.prefix, status.uname or "已登录")
            else:
                log.warning("%s无法确认登录状态（%s），直接尝试发送。", self.prefix, status.message)

        if room is not None and error is None:
            self.dm_config = client.get_danmaku_config(room.room_id)
            self.emit("permissions", config=self.dm_config)
            self._apply_permissions(room)
            bind = getattr(self.source, "bind", None)
            if callable(bind):
                bind(client, room.room_id)

            self.emit("room", room=room)
            log.info(
                "%s开始向 %s（%s）发送：间隔 %g 秒，%s，%s",
                self.prefix,
                room.room_id,
                room.title or "标题未知",
                task.interval_seconds,
                MODE_LABELS.get(task.mode, "说书人" if task.source == "novel" else task.mode),
                self.source.progress_text(),
            )
            if self.dm_config.fetched:
                modes = "、".join(item.name for item in self.dm_config.enabled_modes)
                log.info("%s账号身份：%s｜可用模式：%s", self.prefix, self.dm_config.guard_text, modes)

            self.emit("status", state="running", source=self.source.progress_text())
            error, completed = self._send_loop(room)

        self.emit(
            "status",
            state="stopped",
            message=error,
            completed=completed,
            total=self.total,
            ok=self.ok,
            failed=self.failed,
            source=self.source.progress_text(),
        )
        self.last_error = error
        self.completed = completed
        self.next_send_at = None
        if error:
            log.error("%s发送已停止：%s", self.prefix, error)
        elif completed:
            log.info("%s发送完成：共 %d 条，成功 %d 条", self.prefix, self.total, self.ok)
        else:
            log.info("%s发送已停止：共 %d 条，成功 %d 条，失败 %d 条", self.prefix, self.total, self.ok, self.failed)

    def _apply_permissions(self, room: RoomInfo) -> None:
        """按接口返回的可用项修正颜色/模式，避免普通用户误选舰长专属。"""
        task = self.task
        config = self.dm_config
        if not config.fetched:
            log.warning("%s无法获取弹幕权限配置，按当前设置尝试发送。", self.prefix)
            return

        modes = {item.value: item for item in config.enabled_modes}
        if task.danmaku_mode not in modes:
            fallback = 1 if 1 in modes else (next(iter(modes), 1))
            log.warning(
                "%s当前账号不能使用「%s」弹幕（需要舰长/房管等身份），已改用「%s」。",
                self.prefix,
                {1: "滚动", 4: "底部", 5: "顶部"}.get(task.danmaku_mode, task.danmaku_mode),
                modes.get(fallback).name if fallback in modes else "滚动",
            )
            self.effective_mode = fallback
        else:
            self.effective_mode = task.danmaku_mode

        colors = {item.value: item for item in config.enabled_colors}
        if task.color not in colors:
            fallback = 16777215 if 16777215 in colors else (next(iter(colors), 16777215))
            log.warning(
                "%s当前账号不能使用该颜色，已改用「%s」（彩色弹幕可能需要舰长身份）。",
                self.prefix,
                colors.get(fallback).name if fallback in colors else "白色",
            )
            self.effective_color = fallback
        else:
            self.effective_color = task.color

    def _send_loop(self, room: RoomInfo) -> tuple[str | None, bool]:
        task = self.task
        streak = 0
        completed = False
        while not self._stop.is_set():
            if self.source.finished:
                completed = True
                break
            try:
                item = self.source.next()
            except StopIteration:
                completed = True
                break

            if item is None:
                # 复读机在当前窗口内没有合适弹幕：不发送，等下一个间隔继续统计
                self.emit("progress", total=self.total, ok=self.ok, failed=self.failed,
                          source=self.source.progress_text())
                self.next_send_at = time.monotonic() + task.interval_seconds
                self.emit("schedule", next_at=self.next_send_at, interval=task.interval_seconds)
                if self._stop.wait(task.interval_seconds):
                    break
                continue

            result = self._client.send_danmaku(
                item.text,
                room.room_id,
                color=self.effective_color,
                fontsize=task.fontsize,
                mode=self.effective_mode,
                emoticon_unique=item.emoticon_unique,
                emoticon_text=item.emoticon_name or item.emoticon_unique,
                emoticon_combinable=item.emoticon_combinable,
            )
            self.total += 1

            if result.ok:
                self.ok += 1
                streak = 0
                self.source.advance()
                success(
                    log,
                    "%s第 %d 条发送成功：%s",
                    self.prefix,
                    self.total,
                    item.display_text,
                    extra={"emote_unique": item.emoticon_unique},
                )
                if self.source.finished:
                    completed = True
            else:
                self.failed += 1
                streak += 1
                log.error("%s第 %d 条发送失败：%s", self.prefix, self.total, result.detail)
                if result.fatal:
                    return "登录态已失效，已自动停止发送。请更新 Cookie 后重新启动。", False
                if streak >= MAX_CONSECUTIVE_FAILURES:
                    return f"连续 {streak} 次发送失败，已自动停止发送。", False

            self.emit(
                "progress",
                total=self.total,
                ok=self.ok,
                failed=self.failed,
                source=self.source.progress_text(),
            )
            if self._on_progress:
                try:
                    self._on_progress(self.task_id, self.source.progress_text())
                except Exception:  # noqa: BLE001
                    log.exception("%s保存进度失败", self.prefix)
            if completed:
                break

            self.next_send_at = time.monotonic() + task.interval_seconds
            self.emit("schedule", next_at=self.next_send_at, interval=task.interval_seconds)
            if self._stop.wait(task.interval_seconds):
                break
        return None, completed
