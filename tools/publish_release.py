"""创建/更新 GitHub Release 并上传发行包。

凭据来源（按顺序）：
1. 环境变量 GITHUB_TOKEN / GH_TOKEN；
2. `git credential fill`（本机 Git Credential Manager 已登录 GitHub 时可直接取到）。

用法：
    py -3 tools/publish_release.py --tag v1.0.0 \
        --asset dist/BILIBILI_SEND_PY_v1.0.0_portable_win64.zip \
        --notes docs/release-v1.0.0.md --proxy http://127.0.0.1:7897

加 --dry-run 只检查凭据和现有 Release，不做任何修改。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"
UPLOAD = "https://uploads.github.com"
DEFAULT_REPO = "StayAlivez/BILIBILI_SEND_PY"
USER_AGENT = "bilibili-send-release"


def read_version() -> str:
    import re

    text = (ROOT / "bili_send" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


def git_credential_token() -> str:
    try:
        result = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True,
            text=True,
            timeout=30,
            cwd=ROOT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for line in result.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    return ""


def get_token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if token:
        return token.strip()
    token = git_credential_token()
    if token:
        return token.strip()
    raise SystemExit(
        "拿不到 GitHub 凭据。请先在能弹出登录窗口的终端里执行一次 git push 完成登录，"
        "或设置环境变量 GITHUB_TOKEN 后重试。"
    )


def build_opener(proxy: str | None) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def api(opener, method: str, url: str, token: str, payload: dict | None = None, raw: bytes | None = None,
        content_type: str | None = None, timeout: int = 120):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    }
    data: bytes | None = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif raw is not None:
        data = raw
        headers["Content-Type"] = content_type or "application/octet-stream"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"GitHub API {method} {url} 失败：HTTP {exc.code}\n{detail[:600]}") from exc
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        return {}


def main() -> int:
    version = read_version()
    parser = argparse.ArgumentParser(description="创建 GitHub Release 并上传资产")
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"owner/repo，默认 {DEFAULT_REPO}")
    parser.add_argument("--tag", default=f"v{version}")
    parser.add_argument("--name", default=f"BILIBILI_SEND_PY v{version}")
    parser.add_argument("--notes", type=Path, help="Release 说明 Markdown 文件")
    parser.add_argument("--asset", type=Path, required=True, help="要上传的压缩包")
    parser.add_argument("--proxy", help="访问 GitHub 的代理，例如 http://127.0.0.1:7897")
    parser.add_argument("--dry-run", action="store_true", help="只检查，不创建/上传")
    args = parser.parse_args()

    token = get_token()
    opener = build_opener(args.proxy)

    user = api(opener, "GET", f"{API}/user", token)
    print(f"已登录 GitHub：{user.get('login', '?')}（{args.repo}）")

    asset = args.asset.resolve()
    if not asset.exists():
        raise SystemExit(f"找不到要上传的文件：{asset}")

    body = ""
    if args.notes and args.notes.exists():
        body = args.notes.read_text(encoding="utf-8-sig").strip()

    existing = None
    try:
        existing = api(opener, "GET", f"{API}/repos/{args.repo}/releases/tags/{args.tag}", token)
    except SystemExit as exc:
        if "HTTP 404" not in str(exc):
            raise

    if existing:
        print(f"Release {args.tag} 已存在（id={existing['id']}），将更新说明并替换同名资产")
        release_id = existing["id"]
        if not args.dry_run:
            api(opener, "PATCH", f"{API}/repos/{args.repo}/releases/{release_id}", token,
                payload={"name": args.name, "body": body})
    else:
        print(f"创建 Release {args.tag}")
        if args.dry_run:
            release_id = 0
        else:
            release = api(opener, "POST", f"{API}/repos/{args.repo}/releases", token, payload={
                "tag_name": args.tag,
                "name": args.name,
                "body": body,
                "draft": False,
                "prerelease": False,
            })
            release_id = release["id"]

    if args.dry_run:
        print(f"[dry-run] 将上传 {asset.name}（{asset.stat().st_size / 1024 / 1024:.1f} MB）")
        return 0

    assets = api(opener, "GET", f"{API}/repos/{args.repo}/releases/{release_id}/assets", token)
    for item in assets:
        if item.get("name") == asset.name:
            print(f"删除同名旧资产：{item['name']}")
            api(opener, "DELETE", item["url"], token)

    print(f"上传 {asset.name}（{asset.stat().st_size / 1024 / 1024:.1f} MB）…")
    data = asset.read_bytes()
    quote_name = urllib.parse.quote(asset.name)
    uploaded = api(
        opener,
        "POST",
        f"{UPLOAD}/repos/{args.repo}/releases/{release_id}/assets?name={quote_name}",
        token,
        raw=data,
        content_type="application/zip",
        timeout=600,
    )
    print(f"上传完成：{uploaded.get('browser_download_url', '(无下载地址)')}")
    print(f"Release 页面：https://github.com/{args.repo}/releases/tag/{args.tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
