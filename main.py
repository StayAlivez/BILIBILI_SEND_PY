"""BILIBILI_SEND Python 版入口。

    python main.py          打开图形界面（默认，等价于原来在 IDEA 里跑测试）
    python main.py --cli    不开界面，命令行后台发送，Ctrl+C 停止
"""

from __future__ import annotations

import argparse
import logging
import queue
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bili_send.config import APP_DIR, load_config  # noqa: E402
from bili_send.logbus import LOG_QUEUE, QueueLogHandler  # noqa: E402  (顺带注册 SUCCESS 级别)

log = logging.getLogger("bili_send")


def setup_logging(gui: bool) -> None:
    """文件日志始终开启；界面模式下再挂一个投递到 GUI 的 handler。"""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    log_dir = APP_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "bili_send.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if gui:
        ui_handler = QueueLogHandler(LOG_QUEUE)
        ui_handler.setLevel(logging.INFO)
        ui_handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(ui_handler)
    else:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO)
        console.setFormatter(formatter)
        root.addHandler(console)


def run_gui() -> int:
    try:
        from bili_send.gui import BiliSendApp
    except ImportError as exc:
        print("无法加载图形界面（tkinter）：", exc)
        print("请安装 python.org 的官方 Python 3.9+（自带 tkinter），不要用 Microsoft Store 版本。")
        return 1
    app = BiliSendApp()
    app.mainloop()
    return 0


def run_cli() -> int:
    from bili_send.messages import MessageFileError, MessagePool, load_messages
    from bili_send.runner import DanmakuRunner

    cfg = load_config()
    if not cfg.cookie:
        print("config.json 里还没有 Cookie：请先运行一次图形界面填写，或手动编辑 config.json。")
        return 2

    try:
        items = load_messages(cfg.messages_path)
    except MessageFileError as exc:
        print(f"消息文件读取失败：{exc}")
        return 2

    try:
        pool = MessagePool(items, mode=cfg.mode, avoid_repeat=cfg.avoid_repeat)
    except ValueError as exc:
        print(f"没有可用弹幕：{exc}")
        return 2

    events: "queue.Queue[dict]" = queue.Queue()
    runner = DanmakuRunner(cfg, pool, events)
    runner.start()
    print(
        f"正在启动：房间 {cfg.room_id}，间隔 {cfg.interval_seconds:g} 秒，"
        f"共 {len(pool)} 条可用弹幕。按 Ctrl+C 停止。"
    )
    try:
        while runner.running:
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n正在停止…")
        runner.stop()
    runner.join(5.0)
    return 1 if runner.last_error else 0


def _enable_dpi_awareness() -> None:
    """Windows 高 DPI 下让界面不发虚。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
    except ImportError:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - 老系统没有 shcore
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _ensure_utf8_stdio() -> None:
    """命令行模式在 GBK 控制台下也能正常输出中文。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BILIBILI_SEND 直播弹幕助手（Python 版）")
    parser.add_argument("--cli", action="store_true", help="不打开界面，直接在命令行运行")
    args = parser.parse_args(argv)

    _ensure_utf8_stdio()
    _enable_dpi_awareness()
    setup_logging(gui=not args.cli)
    log.info("BILIBILI_SEND Python 版启动，模式：%s", "命令行" if args.cli else "图形界面")
    if args.cli:
        return run_cli()
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
