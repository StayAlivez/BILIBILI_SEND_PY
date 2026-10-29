"""B 站直播弹幕客户端（仅用标准库，无需 requests）。

负责：Cookie 解析、CSRF（bili_jct）提取、真实房间号解析、发送弹幕、
以及把 B 站返回码翻译成看得懂的提示。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

API_SEND = "https://api.live.bilibili.com/msg/send"
API_ROOM_INIT = "https://api.live.bilibili.com/room/v1/Room/room_init"
API_ROOM_INFO = "https://api.live.bilibili.com/room/v1/Room/get_info"
API_ANCHOR_INFO = "https://api.live.bilibili.com/live_user/v1/UserInfo/get_anchor_in_room"
API_NAV = "https://api.bilibili.com/x/web-interface/nav"
API_DM_CONFIG = "https://api.live.bilibili.com/xlive/web-room/v1/dM/GetDMConfigByGroup"
API_USER_INFO = "https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByUser"
API_EMOTICONS = "https://api.live.bilibili.com/xlive/web-ucenter/v2/emoticon/GetEmoticons"
API_HISTORY = "https://api.live.bilibili.com/xlive/web-room/v1/dM/gethistory"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 常见返回码 -> 人话解释
CODE_HINTS: dict[int, str] = {
    0: "发送成功",
    -101: "未登录或登录已过期，请重新复制 Cookie 里的 SESSDATA",
    -111: "CSRF 校验失败，Cookie 里的 bili_jct 缺失或与登录态不匹配",
    -400: "请求被拒绝（常见原因：Cookie 已失效、账号受限或弹幕内容含屏蔽词）",
    -403: "没有发言权限或被风控",
    -500: "未达到使用条件（如顶部/底部弹幕、专属颜色需要舰长）或服务器异常",
    -509: "请求太频繁被限流，请调大发送间隔",
    10030: "弹幕内容不被接受（常见原因：含屏蔽词、超长或账号等级不足）",
    10031: "发送过于频繁，请调大发送间隔",
    1003212: "弹幕超出长度限制",
    10065107: "该颜色不可用（可能需要舰长/提督等身份）",
}

# 这些错误重试没有意义，发送线程应直接停下
FATAL_CODES = frozenset({-101, -111})

POSITION_ROLLING = 1
POSITION_BOTTOM = 4
POSITION_TOP = 5


class BilibiliError(Exception):
    """B 站接口调用失败的基类。"""


class NetworkError(BilibiliError):
    """网络层错误：超时、DNS、连接被重置等。"""


class ApiError(BilibiliError):
    """接口返回了错误 HTTP 状态或非 JSON 内容。"""

    def __init__(self, message: str, status: int = 0, payload: Any = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload
        self.body = body


@dataclass
class SendResult:
    ok: bool
    code: int
    message: str
    hint: str = ""
    raw: Any = None

    @property
    def fatal(self) -> bool:
        return self.code in FATAL_CODES

    @property
    def detail(self) -> str:
        text = f"{self.message}（code={self.code}）"
        if self.hint:
            text += f" —— {self.hint}"
        return text


@dataclass
class RoomInfo:
    room_id: int
    short_id: int = 0
    title: str = ""
    anchor: str = ""
    live_status: int = 0
    area: str = ""
    online: int = 0

    @property
    def live_text(self) -> str:
        return "直播中" if self.live_status == 1 else "未开播"


@dataclass
class HistoryDanmaku:
    """直播间最近弹幕（用于 +1 / 复读）。"""

    uid: str
    nickname: str
    text: str
    timeline: str = ""
    dm_type: int = 0
    is_admin: bool = False


@dataclass
class LoginStatus:
    """Cookie 登录态；``known`` 为 False 表示网络失败、无法判断。"""

    known: bool = False
    logged_in: bool = False
    uname: str = ""
    uid: str = ""
    message: str = ""

    @property
    def display(self) -> str:
        if not self.logged_in:
            return "未登录"
        return f"{self.uname}（UID {self.uid}）" if self.uname else f"已登录（UID {self.uid}）"


@dataclass
class DanmakuOption:
    """一种可用的弹幕颜色或显示模式。"""

    value: int
    name: str
    enabled: bool = True


@dataclass
class DanmakuConfig:
    """当前账号在该直播间可用的颜色和模式（由 B 站接口返回）。"""

    modes: list[DanmakuOption] = field(default_factory=list)
    colors: list[DanmakuOption] = field(default_factory=list)
    guard_level: int = 0
    fetched: bool = False
    message: str = ""

    @property
    def enabled_modes(self) -> list[DanmakuOption]:
        return [item for item in self.modes if item.enabled]

    @property
    def enabled_colors(self) -> list[DanmakuOption]:
        return [item for item in self.colors if item.enabled]

    @property
    def guard_text(self) -> str:
        return {1: "总督", 2: "提督", 3: "舰长"}.get(self.guard_level, "普通用户")


@dataclass
class Emoticon:
    """一个直播间表情。"""

    unique: str
    name: str
    emoji: str
    url: str
    perm: bool
    pkg_name: str = ""
    unlock_text: str = ""
    is_dynamic: bool = False
    combinable: bool = False


@dataclass
class EmoticonPackage:
    pkg_id: int
    name: str
    perm: bool
    emoticons: list[Emoticon] = field(default_factory=list)
    pkg_type: int = 1


@dataclass
class ParsedCookie:
    values: dict[str, str] = field(default_factory=dict)

    @property
    def sessdata(self) -> str:
        return self.values.get("SESSDATA", "")

    @property
    def csrf(self) -> str:
        return self.values.get("bili_jct", "")

    @property
    def uid(self) -> str:
        return self.values.get("DedeUserID", "")


def parse_cookie(raw: str) -> dict[str, str]:
    """把浏览器里复制的 Cookie 字符串切成字典（允许换行、分号分隔）。"""
    text = (raw or "").strip()
    if text.lower().startswith("cookie:"):
        text = text[7:]
    cookies: dict[str, str] = {}
    for chunk in text.replace("\r", ";").replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if chunk.lower().startswith("cookie:"):
            chunk = chunk[7:].strip()
        if not chunk or "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    return cookies


def cookie_warnings(raw: str) -> list[str]:
    """检查 Cookie 是否足以发送弹幕，返回给界面显示的提示。"""
    parsed = parse_cookie(raw)
    if not parsed:
        return ["还没有填写 Cookie，请先在浏览器登录 B 站后复制粘贴。"]
    warnings: list[str] = []
    if not parsed.get("SESSDATA"):
        warnings.append("缺少 SESSDATA，说明 Cookie 里没有登录态")
    if not parsed.get("bili_jct"):
        warnings.append("缺少 bili_jct（发送弹幕必需的 CSRF 令牌）")
    return warnings


MASK = "••••••••"


def mask_cookie(raw: str) -> str:
    """把 Cookie 值替换成圆点，只保留常用键名，便于界面默认打码显示。"""
    parsed = parse_cookie(raw)
    if not parsed:
        return ""
    important = [name for name in ("SESSDATA", "bili_jct", "DedeUserID") if name in parsed]
    parts = [f"{name}={MASK}" for name in important]
    rest = len(parsed) - len(important)
    if parts:
        text = "   ".join(parts)
        if rest:
            text += f"   （另有 {rest} 项已隐藏）"
        return text
    return f"（{len(parsed)} 项 Cookie 已隐藏）"


class BilibiliClient:
    """封装弹幕发送所需的 HTTP 细节。"""

    def __init__(
        self,
        cookie: str,
        timeout: float = 5.0,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.cookie = (cookie or "").strip()
        self.timeout = max(1.0, float(timeout))
        self.user_agent = user_agent
        self.parsed = ParsedCookie(parse_cookie(self.cookie))

    # ------------------------------------------------------------------ 属性
    @property
    def csrf(self) -> str:
        return self.parsed.csrf

    @property
    def uid(self) -> str:
        return self.parsed.uid

    @property
    def has_login(self) -> bool:
        return bool(self.parsed.sessdata)

    # -------------------------------------------------------------- HTTP 层
    def _open(self, url: str, data: bytes | None = None, referer: str | None = None) -> bytes:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": referer or "https://live.bilibili.com/",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            headers["Origin"] = "https://live.bilibili.com"

        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise ApiError(f"HTTP {exc.code}", status=exc.code, payload=_maybe_json(body), body=body) from exc
        except urllib.error.URLError as exc:
            raise NetworkError(f"网络不可用：{getattr(exc, 'reason', exc)}") from exc
        except TimeoutError as exc:
            raise NetworkError("请求超时") from exc

    def _json(self, url: str, data: bytes | None = None, referer: str | None = None) -> dict[str, Any]:
        body = self._open(url, data=data, referer=referer)
        text = body.decode("utf-8", "replace")
        payload = _maybe_json(text)
        if not isinstance(payload, dict):
            raise ApiError("接口返回了非 JSON 内容", body=text, payload=None)
        return payload

    # ------------------------------------------------------------------ 接口
    def check_login(self) -> LoginStatus:
        """用 nav 接口确认 Cookie 是否仍然有效（只读请求，不会发弹幕）。"""
        try:
            data = self._json(API_NAV, referer="https://www.bilibili.com/")
        except BilibiliError as exc:
            return LoginStatus(known=False, message=str(exc))

        payload = data.get("data") or {}
        code = _to_int(data.get("code"), -1)
        logged_in = code == 0 and bool(payload.get("isLogin"))
        message = str(data.get("message") or "")
        if not logged_in and not message:
            message = "Cookie 已失效或未登录"
        return LoginStatus(
            known=True,
            logged_in=logged_in,
            uname=str(payload.get("uname") or ""),
            uid=str(payload.get("mid") or ""),
            message=message,
        )

    def get_danmaku_config(self, room_id: int) -> DanmakuConfig:
        """获取当前账号在该直播间能用的颜色和模式（顶部/底部、彩色都有身份门槛）。"""
        try:
            data = self._json(f"{API_DM_CONFIG}?room_id={int(room_id)}")
        except BilibiliError as exc:
            return DanmakuConfig(fetched=False, message=str(exc))
        payload = data.get("data") or {}

        modes: list[DanmakuOption] = []
        for item in payload.get("mode") or []:
            value = _to_int(item.get("mode"), 0)
            if value <= 0:
                continue
            modes.append(
                DanmakuOption(value, str(item.get("name") or f"模式{value}"), _to_int(item.get("status"), 1) == 1)
            )

        colors: list[DanmakuOption] = []
        seen: set[int] = set()
        for group in payload.get("group") or []:
            for item in group.get("color") or []:
                value = _to_int(item.get("color"), 0)
                if value in seen:
                    continue
                seen.add(value)
                colors.append(
                    DanmakuOption(value, str(item.get("name") or f"#{value:06x}"), _to_int(item.get("status"), 1) == 1)
                )

        if not modes:
            modes = [DanmakuOption(POSITION_ROLLING, "滚动", True)]
        if not colors:
            colors = [DanmakuOption(16777215, "白色", True)]
        return DanmakuConfig(modes, colors, guard_level=self.get_guard_level(room_id), fetched=True)

    def get_guard_level(self, room_id: int) -> int:
        """0 普通用户，3 舰长，2 提督，1 总督；拿不到时按 0 处理。"""
        try:
            data = self._json(f"{API_USER_INFO}?room_id={int(room_id)}")
        except BilibiliError:
            return 0
        payload = data.get("data") or {}
        info = payload.get("info") or payload
        return _to_int(info.get("guard_level"), 0)

    def get_emoticons(self, room_id: int) -> list[EmoticonPackage]:
        """获取直播间表情包；每个表情带 ``perm`` 表示当前账号能否使用。"""
        data = self._json(
            f"{API_EMOTICONS}?platform=pc&room_id={int(room_id)}",
            referer=f"https://live.bilibili.com/{room_id}",
        )
        code = _to_int(data.get("code"), -1)
        if code != 0:
            raise ApiError(str(data.get("message") or "获取表情失败"), payload=data)
        payload = data.get("data") or {}

        packages: list[EmoticonPackage] = []
        for pkg in payload.get("data") or []:
            pkg_name = str(pkg.get("pkg_name") or "")
            pkg_type = _to_int(pkg.get("pkg_type"), 1)
            # web 端只有 pkg_type == 3 的表情会插进输入框，可和文字一起发；
            # 其他类型走 dm_type=1 直发，服务端不接受附加文字。
            combinable = pkg_type == 3
            items: list[Emoticon] = []
            for emo in pkg.get("emoticons") or []:
                unique = str(emo.get("emoticon_unique") or "")
                if not unique:
                    continue
                url = str(emo.get("url") or "")
                if url.startswith("//"):
                    url = "https:" + url
                perm = _to_int(emo.get("perm"), 0) == 1
                unlock = str(emo.get("unlock_show_text") or "")
                if not unlock and not perm:
                    gift = _to_int(emo.get("unlock_need_gift"), 0)
                    level = _to_int(emo.get("unlock_need_level"), 0)
                    unlock = f"需礼物 {gift}" if gift else (f"需等级 {level}" if level else "未解锁")
                items.append(
                    Emoticon(
                        unique=unique,
                        name=str(emo.get("emoji") or emo.get("descript") or unique),
                        emoji=str(emo.get("emoji") or ""),
                        url=url,
                        perm=perm,
                        pkg_name=pkg_name,
                        unlock_text=unlock,
                        is_dynamic=_to_int(emo.get("is_dynamic"), 0) == 1,
                        combinable=combinable,
                    )
                )
            packages.append(
                EmoticonPackage(
                    pkg_id=_to_int(pkg.get("pkg_id"), 0),
                    name=pkg_name,
                    perm=_to_int(pkg.get("pkg_perm"), 0) == 1,
                    emoticons=items,
                    pkg_type=pkg_type,
                )
            )
        return packages

    def get_room_history(self, room_id: int, limit: int = 60) -> list[HistoryDanmaku]:
        """拉取直播间最近弹幕；返回按时间正序的列表。"""
        data = self._json(
            f"{API_HISTORY}?roomid={int(room_id)}&room_type=0",
            referer=f"https://live.bilibili.com/{room_id}",
        )
        code = _to_int(data.get("code"), -1)
        if code != 0:
            raise ApiError(str(data.get("message") or "获取弹幕失败"), payload=data)
        payload = data.get("data") or {}
        entries: list[HistoryDanmaku] = []
        seen: set[tuple[str, str, str]] = set()
        for section, admin in (("room", False), ("admin", True)):
            for row in payload.get(section) or []:
                text = str(row.get("text") or "").strip()
                if not text:
                    continue
                uid = str(row.get("uid") or "")
                timeline = str(row.get("timeline") or "")
                key = (uid, text, timeline)
                if key in seen:
                    continue
                seen.add(key)
                entries.append(
                    HistoryDanmaku(
                        uid=uid,
                        nickname=str(row.get("nickname") or ""),
                        text=text,
                        timeline=timeline,
                        dm_type=_to_int(row.get("dm_type"), 0),
                        is_admin=admin or _to_int(row.get("isadmin"), 0) == 1,
                    )
                )
        entries.sort(key=lambda item: item.timeline)
        return entries[-max(1, int(limit)):]

    def download_bytes(self, url: str) -> bytes:
        """下载表情图片（走同一个 UA/Referer，避免 CDN 403）。"""
        return self._open(url, referer="https://live.bilibili.com/")

    def resolve_room(self, room_id: int) -> RoomInfo:
        """把短号/房间号统一成发送弹幕要用的真实房间号，并带出标题等信息。"""
        requested = int(room_id)
        info = RoomInfo(room_id=requested)
        try:
            init = self._json(f"{API_ROOM_INIT}?id={requested}").get("data") or {}
        except BilibiliError as exc:
            log.warning("解析房间号失败，按原样使用 %s：%s", requested, exc)
            return info

        real_id = _to_int(init.get("room_id"), requested)
        info.room_id = real_id
        info.short_id = _to_int(init.get("short_id"), 0)
        info.live_status = _to_int(init.get("live_status"), 0)

        try:
            detail = self._json(f"{API_ROOM_INFO}?room_id={real_id}").get("data") or {}
            info.title = str(detail.get("title") or "")
            info.area = str(detail.get("area_name") or "")
            info.online = _to_int(detail.get("online"), 0)
            info.live_status = _to_int(detail.get("live_status"), info.live_status)
        except BilibiliError as exc:
            log.debug("获取房间详情失败：%s", exc)

        try:
            anchor = (self._json(f"{API_ANCHOR_INFO}?roomid={real_id}").get("data") or {}).get("info") or {}
            info.anchor = str(anchor.get("uname") or "")
        except BilibiliError as exc:
            log.debug("获取主播信息失败：%s", exc)

        return info

    def send_danmaku(
        self,
        msg: str,
        room_id: int,
        color: int = 5566168,
        fontsize: int = 25,
        mode: int = POSITION_ROLLING,
        emoticon_unique: str = "",
        emoticon_text: str = "",
        emoticon_combinable: bool = False,
    ) -> SendResult:
        """发送一条弹幕（文字或直播间表情），任何失败都转成 :class:`SendResult`。

        网页端抓包结论：

        * 可拼接文字的表情（``pkg_type == 3``）：把触发词 ``[emoji]`` 当作普通文字发送，
          例如 ``[妙][哇] 333``，不带 ``dm_type``。
        * 不可拼接的表情：``msg`` 直接填 ``emoticon_unique``，带 ``dm_type=1`` 和
          ``emoticonOptions=[object Object]``，服务端不接受附加文字。
        """
        text = " ".join(str(msg).split())
        if emoticon_unique and emoticon_combinable:
            trigger = (emoticon_text or emoticon_unique).strip()
            if not (trigger.startswith("[") and trigger.endswith("]")):
                trigger = f"[{trigger}]"
            text = trigger + (f" {text}" if text else "")
        elif emoticon_unique:
            if text:
                return SendResult(False, -1, "该表情不支持拼接文字")
            text = emoticon_unique
        if not text:
            return SendResult(False, -1, "弹幕内容为空")
        if not self.csrf:
            return SendResult(False, -111, "Cookie 里缺少 bili_jct，无法发送", CODE_HINTS[-111])

        payload = {
            "bubble": "0",
            "msg": text,
            "color": str(int(color)),
            "mode": str(int(mode)),
            "fontsize": str(int(fontsize)),
            "rnd": str(int(time.time())),
            "roomid": str(int(room_id)),
            "csrf": self.csrf,
            "csrf_token": self.csrf,
            "data_extend": '{"trackid":"-99998"}',
            "web_location": "444.8",
        }
        if emoticon_unique and not emoticon_combinable:
            payload["dm_type"] = "1"
            payload["emoticonOptions"] = "[object Object]"
        body = urllib.parse.urlencode(payload).encode("utf-8")
        referer = f"https://live.bilibili.com/{room_id}"

        try:
            data = self._json(API_SEND, data=body, referer=referer)
        except ApiError as exc:
            error_payload = exc.payload if isinstance(exc.payload, dict) else {}
            code = _to_int(error_payload.get("code"), -1)
            message = str(error_payload.get("message") or error_payload.get("msg") or exc)
            return SendResult(False, code, message, CODE_HINTS.get(code, ""), error_payload)
        except NetworkError as exc:
            return SendResult(False, -1, f"网络错误：{exc}", "检查网络或代理后重试")

        code = _to_int(data.get("code"), -1)
        message = str(data.get("message") or data.get("msg") or "")
        if code == 0:
            return SendResult(True, 0, message or "发送成功", "", data)
        return SendResult(False, code, message or "未知错误", CODE_HINTS.get(code, ""), data)


def _maybe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
