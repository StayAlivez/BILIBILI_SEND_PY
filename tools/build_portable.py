"""构建 Windows 免安装发行包（内置 Python 运行时 + tkinter）。

做三件事：
1. 把项目文件复制到 dist/<名称>/；
2. 解压官方 embeddable Python，并从本机完整版 Python 补上 tkinter / Tcl / Tk；
3. 校验运行时可用后打成 zip。

用法（在项目根目录）：
    py -3 tools/build_portable.py --python-home "C:\\...\\pythoncore-3.11-64"
    py -3 tools/build_portable.py --python-home ... --embeddable-zip .cache/python-3.11.9-embed-amd64.zip

不传 --embeddable-zip 时会自动下载到 .cache/；需要代理时加 --proxy。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PY_VERSION = "3.11.9"
APP_ITEMS = (
    "bili_send",
    "messages",
    "docs",
    "tests",
    "main.py",
    "run.bat",
    "debug.bat",
    "README.md",
    "config.example.json",
)
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.txt", ".idea", "logs", "config.json", ".tmp")


def read_version() -> str:
    text = (ROOT / "bili_send" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("无法从 bili_send/__init__.py 读取版本号")
    return match.group(1)


def find_python_home(py_version: str) -> Path:
    short = ".".join(py_version.split(".")[:2])
    try:
        result = subprocess.run(
            ["py", f"-{short}", "-c", "import sys; print(sys.base_prefix)"],
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"找不到 Python {short}，请用 --python-home 指定完整版安装目录：{exc}") from exc
    return Path(result.stdout.strip())


def check_python_home(home: Path) -> None:
    required = (
        "python.exe",
        "DLLs/_tkinter.pyd",
        "DLLs/tcl86t.dll",
        "DLLs/tk86t.dll",
        "tcl/tcl8.6",
        "tcl/tk8.6",
        "Lib/tkinter/__init__.py",
    )
    missing = [item for item in required if not (home / item).exists()]
    if missing:
        raise SystemExit(f"Python 目录不完整，缺少：{', '.join(missing)}（需要带 tkinter 的完整安装）")


def download_embeddable(py_version: str, dest: Path, proxy: str | None) -> Path:
    if dest.exists() and dest.stat().st_size > 1_000_000:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://www.python.org/ftp/python/{py_version}/python-{py_version}-embed-amd64.zip"
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    print(f"下载 {url}")
    with opener.open(url, timeout=180) as response, open(dest, "wb") as handle:
        while chunk := response.read(1 << 20):
            handle.write(chunk)
    return dest


def stage_app(dist: Path) -> None:
    if dist.exists():
        shutil.rmtree(dist)
    dist.mkdir(parents=True)
    for name in APP_ITEMS:
        source = ROOT / name
        target = dist / name
        if source.is_dir():
            shutil.copytree(source, target, ignore=IGNORE)
        elif source.exists():
            shutil.copy2(source, target)
        else:
            raise SystemExit(f"缺少项目文件：{source}")


def stage_runtime(dist: Path, python_home: Path, embed_zip: Path) -> Path:
    runtime = dist / "runtime"
    runtime.mkdir()
    with zipfile.ZipFile(embed_zip) as archive:
        archive.extractall(runtime)

    # embeddable 包不带 tkinter，从完整版补齐（必须同为 3.11.x）
    shutil.copytree(python_home / "Lib" / "tkinter", runtime / "tkinter", ignore=IGNORE)
    for name in ("_tkinter.pyd", "tcl86t.dll", "tk86t.dll"):
        shutil.copy2(python_home / "DLLs" / name, runtime / name)
    shutil.copytree(python_home / "tcl", runtime / "tcl", ignore=IGNORE)

    (runtime / "README.txt").write_text(
        "内置 Python 运行时（官方 embeddable 包 + tkinter/Tcl/Tk）。\n"
        f"Python：{python_version(runtime)}\n"
        "由 tools/build_portable.py 生成，请勿手动修改。\n",
        encoding="utf-8",
    )
    return runtime


def python_version(runtime: Path) -> str:
    result = subprocess.run(
        [str(runtime / "python.exe"), "-c", "import sys; print(sys.version.split()[0])"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def verify_runtime(runtime: Path) -> None:
    env = os.environ.copy()
    env["TCL_LIBRARY"] = str(runtime / "tcl" / "tcl8.6")
    env["TK_LIBRARY"] = str(runtime / "tcl" / "tk8.6")
    code = (
        "import tkinter, tkinter.font, tkinter.messagebox, tkinter.filedialog\n"
        "print('TkVersion', tkinter.TkVersion)\n"
        "print('tcl', tkinter.Tcl().eval('info patchlevel'))\n"
    )
    result = subprocess.run([str(runtime / "python.exe"), "-c", code], env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit("内置运行时自检失败：\n" + (result.stderr or result.stdout))
    print("运行时自检通过：" + " / ".join(line.strip() for line in result.stdout.splitlines()))


def clean_pycache(path: Path) -> None:
    """自检会生成 __pycache__，打包前清掉，保持发行包干净。"""
    for cache in path.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    for stale in path.rglob("*.pyc"):
        stale.unlink(missing_ok=True)


def write_readme(dist: Path, version: str, py_version: str) -> None:
    text = f"""BILIBILI_SEND_PY v{version} 免安装版（Windows x64）
================================================

内置 Python {py_version} 运行时（含 tkinter），不需要另外安装 Python。

使用方法
--------
1. 解压整个文件夹到任意位置（路径可以有空格和中文）。
2. 双击 run.bat 启动图形界面。
3. 填房间号、粘贴 B 站 Cookie，点「开始发送」。

如果双击 run.bat 没有反应
----------------------
请改双击 debug.bat，它会把报错显示在控制台里。
也可以把控制台里的错误信息反馈到：
https://github.com/StayAlivez/BILIBILI_SEND_PY/issues

目录说明
--------
run.bat       启动（无控制台窗口）
debug.bat     启动（保留控制台，便于看报错）
main.py       程序入口
bili_send/    源码
messages/     弹幕消息列表
runtime/      内置 Python 运行时，请勿删除
config.json   运行后自动生成，保存 Cookie 和设置
logs/         运行日志

注意
----
- 仅支持 Windows 10 / 11 64 位。
- Cookie 会明文保存在 config.json，请勿把该文件发给别人。
- 本工具仅供个人学习，请遵守 B 站社区规则，账号风险自负。
"""
    (dist / "README.txt").write_text(text, encoding="utf-8-sig")
    (dist / "VERSION.txt").write_text(
        json.dumps(
            {"version": version, "python": py_version, "built_at": dt.datetime.now().isoformat(timespec="seconds")},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def make_zip(dist: Path) -> Path:
    out = dist.parent / f"{dist.name}.zip"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(dist.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(dist.parent))
    return out


def dir_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description="构建内置 Python 的 Windows 免安装包")
    parser.add_argument("--python-home", type=Path, help="本机完整版 Python 安装目录（需带 tkinter）")
    parser.add_argument("--python-version", default=DEFAULT_PY_VERSION, help=f"embeddable 版本，默认 {DEFAULT_PY_VERSION}")
    parser.add_argument("--embeddable-zip", type=Path, help="已下载的 embeddable zip（默认自动下载到 .cache/）")
    parser.add_argument("--proxy", help="下载 embeddable 包时使用的代理，例如 http://127.0.0.1:7897")
    parser.add_argument("--out", type=Path, default=ROOT / "dist", help="输出目录，默认 dist/")
    parser.add_argument("--no-zip", action="store_true", help="只生成目录，不压缩")
    args = parser.parse_args()

    version = read_version()
    python_home = args.python_home or find_python_home(args.python_version)
    check_python_home(python_home)

    embed_zip = args.embeddable_zip
    if embed_zip is None:
        embed_zip = ROOT / ".cache" / f"python-{args.python_version}-embed-amd64.zip"
    embed_zip = download_embeddable(args.python_version, Path(embed_zip), args.proxy)

    dist = args.out / f"BILIBILI_SEND_PY_v{version}_portable_win64"
    print(f"版本：{version}｜Python：{args.python_version}｜源码：{python_home}")
    stage_app(dist)
    runtime = stage_runtime(dist, python_home, embed_zip)
    verify_runtime(runtime)
    clean_pycache(dist)
    write_readme(dist, version, args.python_version)

    print(f"目录：{dist}（{dir_size(dist) / 1024 / 1024:.1f} MB）")
    if not args.no_zip:
        archive = make_zip(dist)
        print(f"压缩包：{archive}（{archive.stat().st_size / 1024 / 1024:.1f} MB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
