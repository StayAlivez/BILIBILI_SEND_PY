"""直播间表情：拉取、权限标记与图片磁盘缓存。

缓存目录：``cache/emoticons/<room>_<uid>/``
* ``meta.json``：表情包与 ``perm``（按账号区分，账号换了要重新拉）
* ``images/``：按 ``emoticon_unique`` 命名的图片
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict
from pathlib import Path

from .client import BilibiliClient, Emoticon, EmoticonPackage
from .config import CACHE_DIR

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 6 * 3600


def _safe_name(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z_.-]+", "_", value or "")
    return value.strip("._")[:80] or "emoticon"


class EmoticonCache:
    def __init__(self, room_id: int, uid: str) -> None:
        key = f"{int(room_id)}_{_safe_name(uid or 'anon')}"
        self.room_id = int(room_id)
        self.uid = uid or "anon"
        self.root = CACHE_DIR / "emoticons" / key
        self.meta_path = self.root / "meta.json"
        self.image_dir = self.root / "images"

    # ---------------------------------------------------------------- 元数据
    def load(self, force: bool = False) -> list[EmoticonPackage] | None:
        if force or not self.meta_path.exists():
            return None
        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        fetched_at = float(data.get("fetched_at") or 0)
        if time.time() - fetched_at > CACHE_TTL_SECONDS:
            return None
        raw_packages = data.get("packages") or []
        # 旧缓存没有 pkg_type，无法判断能否拼文字，直接重新拉取
        if any("pkg_type" not in raw for raw in raw_packages):
            return None
        packages: list[EmoticonPackage] = []
        for raw in raw_packages:
            items = []
            for emo in raw.get("emoticons") or []:
                try:
                    items.append(Emoticon(**emo))
                except TypeError:
                    continue
            packages.append(
                EmoticonPackage(
                    pkg_id=int(raw.get("pkg_id") or 0),
                    name=str(raw.get("name") or ""),
                    perm=bool(raw.get("perm")),
                    emoticons=items,
                    pkg_type=int(raw.get("pkg_type") or 1),
                )
            )
        return packages

    def save(self, packages: list[EmoticonPackage]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": time.time(),
            "packages": [
                {
                    "pkg_id": pkg.pkg_id,
                    "name": pkg.name,
                    "perm": pkg.perm,
                    "pkg_type": pkg.pkg_type,
                    "emoticons": [asdict(emo) for emo in pkg.emoticons],
                }
                for pkg in packages
            ],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        tmp = self.meta_path.with_name(self.meta_path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.meta_path)

    def fetched_at(self) -> float:
        try:
            return float(json.loads(self.meta_path.read_text(encoding="utf-8")).get("fetched_at") or 0)
        except (OSError, ValueError):
            return 0.0

    def fetch(self, client: BilibiliClient, force: bool = False) -> list[EmoticonPackage]:
        cached = self.load(force=force)
        if cached is not None:
            return cached
        packages = client.get_emoticons(self.room_id)
        self.save(packages)
        return packages

    # ------------------------------------------------------------------ 图片
    def image_path(self, emoticon: Emoticon) -> Path:
        suffix = Path((emoticon.url or "").split("?")[0]).suffix.lower()
        if suffix not in (".png", ".gif", ".jpg", ".jpeg", ".webp"):
            suffix = ".png"
        return self.image_dir / f"{_safe_name(emoticon.unique)}{suffix}"

    def ensure_image(self, client: BilibiliClient, emoticon: Emoticon) -> Path | None:
        path = self.image_path(emoticon)
        if path.exists() and path.stat().st_size > 0:
            return path
        if not emoticon.url:
            return None
        try:
            data = client.download_bytes(emoticon.url)
        except Exception as exc:  # noqa: BLE001 - 图片失败不影响表情列表
            log.debug("下载表情图片失败 %s：%s", emoticon.unique, exc)
            return None
        if not data:
            return None
        self.image_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return path

    def count_images(self) -> int:
        try:
            return sum(1 for item in self.image_dir.iterdir() if item.is_file() and item.suffix != ".tmp")
        except OSError:
            return 0
