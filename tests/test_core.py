"""核心逻辑单元测试（不访问网络）。

运行：py -3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import queue
import shutil
import sys
import time
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 临时文件放在项目目录内，避免受限环境下系统临时目录不可写
TEST_TMP_ROOT = Path(__file__).resolve().parent / ".tmp"


def _case_dir(name: str) -> Path:
    """每个测试用例一个干净的目录（不用 tempfile，跨 Python 版本更稳）。"""
    path = TEST_TMP_ROOT / name
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def tearDownModule() -> None:
    shutil.rmtree(TEST_TMP_ROOT, ignore_errors=True)

from bili_send.client import (  # noqa: E402
    BilibiliClient,
    DanmakuConfig,
    DanmakuOption,
    HistoryDanmaku,
    LoginStatus,
    RoomInfo,
    SendResult,
    cookie_warnings,
    mask_cookie,
    parse_cookie,
)
from bili_send.config import (  # noqa: E402
    SOURCE_NOVEL,
    SOURCE_POOL,
    SOURCE_REPEAT,
    AppConfig,
    TaskConfig,
    load_config,
    save_config,
)
from bili_send.messages import (  # noqa: E402
    MessageFileError,
    MessageItem,
    MessagePool,
    load_messages,
    save_messages,
)
from bili_send.novel import NovelBook, load_book, split_text  # noqa: E402
from bili_send.runner import DanmakuRunner  # noqa: E402
from bili_send.sources import NovelSource, PoolSource, RepeatSource  # noqa: E402

GOOD_COOKIE = "SESSDATA=abc%2C123; bili_jct=deadbeef; DedeUserID=57796390"


class FakeResponse:
    """最小化的 urlopen 返回值替身。"""

    def __init__(self, payload: dict | str) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self._data = text.encode("utf-8")

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class TempJsonMixin(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = _case_dir(f"{type(self).__name__}_{self._testMethodName}")
        self.path = self.dir / "messages.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, data: object) -> Path:
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return self.path


class MessageFileTests(TempJsonMixin):
    def test_plain_string_array(self) -> None:
        self.write(["好听", "666"])
        items = load_messages(self.path)
        self.assertEqual([item.text for item in items], ["好听", "666"])
        self.assertEqual([item.weight for item in items], [1, 1])

    def test_weighted_array(self) -> None:
        self.write([{"msg": "好听", "weight": 3}, {"msg": "666", "weight": 1}])
        items = load_messages(self.path)
        self.assertEqual(items[0].weight, 3)
        self.assertEqual(items[1].text, "666")

    def test_single_object_is_accepted(self) -> None:
        # 原项目里的 3.json 就是单个对象
        self.write({"msg": "猫雷最弱", "weight": 10})
        items = load_messages(self.path)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].weight, 10)

    def test_whitespace_and_empty_are_cleaned(self) -> None:
        self.write(["  好\n听  ", "", "   "])
        items = load_messages(self.path)
        self.assertEqual([item.text for item in items], ["好 听"])

    def test_missing_text_field_skipped(self) -> None:
        self.write([{"weight": 5}])
        with self.assertRaises(MessageFileError):
            load_messages(self.path)

    def test_bad_json_raises(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(MessageFileError):
            load_messages(self.path)

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(MessageFileError):
            load_messages(self.dir / "nope.json")

    def test_save_roundtrip(self) -> None:
        items = [MessageItem("好听", 2), MessageItem("666", 1)]
        save_messages(self.path, items)
        again = load_messages(self.path)
        self.assertEqual([(i.text, i.weight) for i in again], [("好听", 2), ("666", 1)])


class MessagePoolTests(unittest.TestCase):
    def test_zero_weight_is_excluded(self) -> None:
        pool = MessagePool([MessageItem("a", 0), MessageItem("b", 1)])
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool.pick().text, "b")

    def test_all_zero_raises(self) -> None:
        with self.assertRaises(ValueError):
            MessagePool([MessageItem("a", 0), MessageItem("", 5)])

    def test_uniform_never_repeats_consecutively(self) -> None:
        pool = MessagePool([MessageItem("a"), MessageItem("b")], mode="uniform", avoid_repeat=True)
        last = None
        for _ in range(40):
            picked = pool.pick()
            self.assertNotEqual(picked.text, last)
            last = picked.text

    def test_weighted_respects_probability(self) -> None:
        import random

        pool = MessagePool(
            [MessageItem("heavy", 99), MessageItem("light", 1)],
            mode="weighted",
            avoid_repeat=False,
            rng=random.Random(1),
        )
        picks = [pool.pick().text for _ in range(500)]
        self.assertGreater(picks.count("heavy"), picks.count("light") * 10)


class CookieTests(unittest.TestCase):
    def test_parse_multiline(self) -> None:
        cookies = parse_cookie("a=1;\nb=2\ncookie: c=3")
        self.assertEqual(cookies["a"], "1")
        self.assertEqual(cookies["b"], "2")
        self.assertEqual(cookies["c"], "3")

    def test_value_with_equals_sign(self) -> None:
        cookies = parse_cookie("bili_ticket=abc.def==; SESSDATA=x")
        self.assertEqual(cookies["bili_ticket"], "abc.def==")

    def test_warnings_for_incomplete_cookie(self) -> None:
        warnings = cookie_warnings("SESSDATA=only")
        self.assertTrue(any("bili_jct" in item for item in warnings))

    def test_good_cookie_has_no_warnings(self) -> None:
        self.assertEqual(cookie_warnings(GOOD_COOKIE), [])

    def test_empty_cookie_warns(self) -> None:
        self.assertTrue(cookie_warnings(""))

    def test_mask_hides_values(self) -> None:
        masked = mask_cookie(GOOD_COOKIE)
        self.assertIn("SESSDATA=", masked)
        self.assertIn("bili_jct=", masked)
        self.assertIn("••••••••", masked)
        self.assertNotIn("abc%2C123", masked)
        self.assertNotIn("deadbeef", masked)
        self.assertNotIn("57796390", masked)

    def test_mask_empty_cookie(self) -> None:
        self.assertEqual(mask_cookie(""), "")

    def test_mask_unknown_keys_only(self) -> None:
        masked = mask_cookie("foo=bar; baz=qux")
        self.assertIn("2 项 Cookie 已隐藏", masked)
        self.assertNotIn("bar", masked)
        self.assertNotIn("qux", masked)


class ClientSendTests(unittest.TestCase):
    def test_missing_csrf_returns_error(self) -> None:
        client = BilibiliClient("SESSDATA=only")
        result = client.send_danmaku("hi", 1)
        self.assertFalse(result.ok)
        self.assertTrue(result.fatal)

    def test_successful_send_posts_form(self) -> None:
        captured: dict = {}

        def fake_urlopen(request, timeout=0):
            captured["request"] = request
            captured["body"] = request.data.decode("utf-8")
            return FakeResponse({"code": 0, "message": "发送成功"})

        client = BilibiliClient(GOOD_COOKIE)
        with mock.patch("bili_send.client.urllib.request.urlopen", fake_urlopen):
            result = client.send_danmaku("好听 & 666", 12345, color=16777215, fontsize=30, mode=5)

        self.assertTrue(result.ok)
        self.assertEqual(result.code, 0)
        body = captured["body"]
        self.assertIn("msg=%E5%A5%BD%E5%90%AC+%26+666", body)
        self.assertIn("roomid=12345", body)
        self.assertIn("csrf=deadbeef", body)
        self.assertIn("mode=5", body)
        self.assertEqual(captured["request"].headers["Cookie"], GOOD_COOKIE)

    def test_api_error_code_is_translated(self) -> None:
        def fake_urlopen(request, timeout=0):
            return FakeResponse({"code": -101, "message": "账号未登录"})

        client = BilibiliClient(GOOD_COOKIE)
        with mock.patch("bili_send.client.urllib.request.urlopen", fake_urlopen):
            result = client.send_danmaku("hi", 1)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, -101)
        self.assertTrue(result.fatal)
        self.assertIn("SESSDATA", result.hint)

    def test_http_error_body_is_parsed(self) -> None:
        def fake_urlopen(request, timeout=0):
            body = io.BytesIO(json.dumps({"code": -412, "message": "请求被拦截"}).encode("utf-8"))
            raise urllib.error.HTTPError(request.full_url, 412, "Precondition Failed", {}, body)

        client = BilibiliClient(GOOD_COOKIE)
        with mock.patch("bili_send.client.urllib.request.urlopen", fake_urlopen):
            result = client.send_danmaku("hi", 1)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, -412)
        self.assertEqual(result.message, "请求被拦截")

    def test_network_error_returns_result(self) -> None:
        def fake_urlopen(request, timeout=0):
            raise urllib.error.URLError("boom")

        client = BilibiliClient(GOOD_COOKIE)
        with mock.patch("bili_send.client.urllib.request.urlopen", fake_urlopen):
            result = client.send_danmaku("hi", 1)
        self.assertFalse(result.ok)
        self.assertIn("网络错误", result.message)

    def test_check_login_when_logged_in(self) -> None:
        def fake_urlopen(request, timeout=0):
            return FakeResponse({"code": 0, "data": {"isLogin": True, "uname": "猫雷", "mid": 57796390}})

        client = BilibiliClient(GOOD_COOKIE)
        with mock.patch("bili_send.client.urllib.request.urlopen", fake_urlopen):
            status = client.check_login()
        self.assertTrue(status.known)
        self.assertTrue(status.logged_in)
        self.assertEqual(status.display, "猫雷（UID 57796390）")

    def test_check_login_when_expired(self) -> None:
        def fake_urlopen(request, timeout=0):
            return FakeResponse({"code": -101, "message": "账号未登录", "data": {"isLogin": False}})

        client = BilibiliClient(GOOD_COOKIE)
        with mock.patch("bili_send.client.urllib.request.urlopen", fake_urlopen):
            status = client.check_login()
        self.assertTrue(status.known)
        self.assertFalse(status.logged_in)
        self.assertEqual(status.display, "未登录")

    def test_check_login_network_failure_is_unknown(self) -> None:
        def fake_urlopen(request, timeout=0):
            raise urllib.error.URLError("offline")

        client = BilibiliClient(GOOD_COOKIE)
        with mock.patch("bili_send.client.urllib.request.urlopen", fake_urlopen):
            status = client.check_login()
        self.assertFalse(status.known)
        self.assertIn("网络", status.message)


class FakeClient:
    """供 Runner 测试使用的假客户端。"""

    def __init__(
        self,
        results: list[SendResult] | None = None,
        login: LoginStatus | None = None,
        dm: DanmakuConfig | None = None,
    ) -> None:
        self.sent: list[str] = []
        self._results = results or []
        self._login = login or LoginStatus(known=True, logged_in=True, uname="测试用户", uid="1")
        self._dm = dm

    def check_login(self) -> LoginStatus:
        return self._login

    def get_danmaku_config(self, room_id: int) -> DanmakuConfig:
        return self._dm or DanmakuConfig(
            modes=[DanmakuOption(1, "滚动", True), DanmakuOption(5, "顶部", True)],
            colors=[DanmakuOption(16777215, "白色", True), DanmakuOption(5566168, "青绿", True)],
            fetched=True,
        )

    def resolve_room(self, room_id: int) -> RoomInfo:
        return RoomInfo(room_id=room_id, title="测试直播间", live_status=1)

    def send_danmaku(self, msg: str, room_id: int, **_kwargs: object) -> SendResult:
        self.sent.append(msg)
        if self._results:
            return self._results.pop(0)
        return SendResult(True, 0, "发送成功")


class RunnerTests(unittest.TestCase):
    def _make_runner(self, client: FakeClient) -> tuple[DanmakuRunner, "queue.Queue[dict]"]:
        cfg = AppConfig(cookie=GOOD_COOKIE, room_id=1, interval_seconds=0.03)
        pool = MessagePool([MessageItem("a"), MessageItem("b")], avoid_repeat=False)
        events: "queue.Queue[dict]" = queue.Queue()
        return DanmakuRunner(cfg, pool, events, client=client), events

    @staticmethod
    def _wait_for(predicate, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_sends_and_stops(self) -> None:
        client = FakeClient()
        runner, events = self._make_runner(client)
        runner.start()
        self.assertTrue(self._wait_for(lambda: len(client.sent) >= 3))
        runner.stop()
        runner.join(2.0)

        self.assertFalse(runner.running)
        self.assertGreaterEqual(runner.total, 3)
        self.assertEqual(runner.failed, 0)
        self.assertEqual(runner.total, len(client.sent))

        statuses = [event for event in _drain(events) if event.get("kind") == "status"]
        self.assertEqual(statuses[0]["state"], "starting")
        self.assertIn("running", [event["state"] for event in statuses])
        self.assertEqual(statuses[-1]["state"], "stopped")
        self.assertIsNone(statuses[-1]["message"])

    def test_fatal_code_stops_automatically(self) -> None:
        client = FakeClient([SendResult(False, -101, "账号未登录")])
        runner, events = self._make_runner(client)
        runner.start()
        self.assertTrue(self._wait_for(lambda: not runner.running))
        runner.join(1.0)

        self.assertEqual(len(client.sent), 1)
        stopped = [event for event in _drain(events) if event.get("kind") == "status"][-1]
        self.assertEqual(stopped["state"], "stopped")
        self.assertIn("登录态", stopped["message"] or "")

    def test_expired_cookie_prevents_sending(self) -> None:
        client = FakeClient(login=LoginStatus(known=True, logged_in=False, message="账号未登录"))
        runner, events = self._make_runner(client)
        runner.start()
        self.assertTrue(self._wait_for(lambda: not runner.running))
        runner.join(1.0)

        self.assertEqual(client.sent, [])
        stopped = [event for event in _drain(events) if event.get("kind") == "status"][-1]
        self.assertEqual(stopped["state"], "stopped")
        self.assertIn("Cookie", stopped["message"] or "")


def _drain(events: "queue.Queue[dict]") -> list[dict]:
    items = []
    while True:
        try:
            items.append(events.get_nowait())
        except queue.Empty:
            return items


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = _case_dir(f"{type(self).__name__}_{self._testMethodName}")
        self.path = self.dir / "config.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_roundtrip(self) -> None:
        cfg = AppConfig(cookie=GOOD_COOKIE, room_id=42, interval_seconds=15, mode="uniform")
        save_config(cfg, self.path)
        loaded = load_config(self.path)
        self.assertEqual(loaded.cookie, GOOD_COOKIE)
        self.assertEqual(loaded.room_id, 42)
        self.assertEqual(loaded.mode, "uniform")
        self.assertEqual(loaded.interval_seconds, 15)

    def test_missing_file_uses_defaults(self) -> None:
        cfg = load_config(self.path)
        self.assertEqual(cfg.mode, "weighted")
        self.assertEqual(cfg.interval_seconds, 30)

    def test_corrupt_file_falls_back_and_backs_up(self) -> None:
        self.path.write_text("{{{", encoding="utf-8")
        cfg = load_config(self.path)
        self.assertEqual(cfg.room_id, 22676119)
        self.assertTrue(self.path.with_suffix(".json.bad").exists())

    def test_values_are_clamped_and_sanitized(self) -> None:
        self.path.write_text(
            json.dumps({"interval_seconds": 0, "fontsize": 999, "mode": "weird", "room_id": "abc"}),
            encoding="utf-8",
        )
        cfg = load_config(self.path)
        self.assertEqual(cfg.interval_seconds, 1.0)
        self.assertEqual(cfg.fontsize, 64)
        self.assertEqual(cfg.mode, "weighted")
        self.assertEqual(cfg.room_id, 22676119)

    def test_storage_copy_drops_cookie_when_save_disabled(self) -> None:
        cfg = AppConfig(cookie=GOOD_COOKIE, save_cookie=False)
        self.assertEqual(cfg.storage_copy().cookie, "")
        cfg.save_cookie = True
        self.assertEqual(cfg.storage_copy().cookie, GOOD_COOKIE)

    def test_save_cookie_defaults_to_true(self) -> None:
        self.assertTrue(AppConfig().save_cookie)

    def test_save_cookie_roundtrip(self) -> None:
        save_config(AppConfig(cookie=GOOD_COOKIE, save_cookie=False), self.path)
        loaded = load_config(self.path)
        self.assertFalse(loaded.save_cookie)


class TaskConfigTests(unittest.TestCase):
    def test_legacy_config_migrates_to_one_task(self) -> None:
        cfg = AppConfig.from_dict({"cookie": GOOD_COOKIE, "room_id": 123, "interval_seconds": 15})
        self.assertEqual(len(cfg.tasks), 1)
        task = cfg.active_task
        self.assertEqual(task.room_id, 123)
        self.assertEqual(task.cookie, GOOD_COOKIE)
        self.assertEqual(task.interval_seconds, 15)

    def test_tasks_roundtrip(self) -> None:
        cfg = AppConfig.from_dict(
            {
                "tasks": [
                    {
                        "id": "a", "name": "A", "room_id": 1, "cookie": "c1",
                        "source": SOURCE_NOVEL, "novel_path": "b.txt", "novel_index": 5,
                    },
                    {"id": "b", "name": "B", "room_id": 2, "cookie": "c2"},
                ],
                "active_task_id": "b",
            }
        )
        self.assertEqual(len(cfg.tasks), 2)
        self.assertEqual(cfg.active_task.id, "b")
        self.assertEqual(cfg.tasks[0].source, SOURCE_NOVEL)
        self.assertEqual(cfg.tasks[0].novel_index, 5)

    def test_storage_copy_clears_task_cookies(self) -> None:
        cfg = AppConfig.from_dict(
            {"save_cookie": False, "tasks": [{"id": "a", "cookie": "secret"}]}
        )
        copied = cfg.storage_copy()
        self.assertEqual(copied.cookie, "")
        self.assertEqual(copied.tasks[0].cookie, "")

    def test_task_values_are_clamped(self) -> None:
        task = TaskConfig.from_dict({"novel_chunk_size": 99, "interval_seconds": 0, "source": "?"})
        self.assertEqual(task.novel_chunk_size, 20)
        self.assertEqual(task.interval_seconds, 1.0)
        self.assertEqual(task.source, SOURCE_POOL)


class NovelTests(TempJsonMixin):
    def test_split_respects_length(self) -> None:
        text = "这是一个很长很长的句子。" * 20
        chunks = split_text(text, 20)
        self.assertTrue(chunks)
        self.assertTrue(all(len(chunk) <= 20 for chunk in chunks))

    def test_split_prefers_punctuation(self) -> None:
        chunks = split_text("第一句话说完了。第二句话。", 10)
        self.assertTrue(chunks[0].endswith("。"))

    def test_load_gbk_book(self) -> None:
        self.path.write_bytes("你好。世界。".encode("gb18030"))
        book = load_book(self.path, 20)
        self.assertGreaterEqual(len(book), 1)
        self.assertEqual(book.title, "messages")

    def test_novel_source_advances_and_reports(self) -> None:
        book = NovelBook(Path("book.txt"), ["一", "二", "三"])
        seen: list[int] = []
        source = NovelSource(book, 1, on_advance=seen.append)
        self.assertEqual(source.next().text, "二")
        source.advance()
        self.assertEqual(source.index, 2)
        self.assertEqual(seen, [2])
        source.advance()
        self.assertTrue(source.finished)
        self.assertIn("已播完", source.progress_text())


class EmoticonMessageTests(TempJsonMixin):
    def test_emoticon_roundtrip(self) -> None:
        save_messages(
            self.path,
            [MessageItem("", 2, "room_1_2", "https://x/y.png", "妙")],
        )
        items = load_messages(self.path)
        self.assertTrue(items[0].is_emoticon)
        self.assertEqual(items[0].weight, 2)
        self.assertIn("表情", items[0].display_text)


class ClientPermissionTests(unittest.TestCase):
    def test_danmaku_config_parses_modes_colors_and_guard(self) -> None:
        def fake_urlopen(request, timeout=0):
            url = request.full_url
            if "GetDMConfigByGroup" in url:
                return FakeResponse(
                    {
                        "code": 0,
                        "data": {
                            "mode": [
                                {"name": "滚动", "mode": 1, "status": 1},
                                {"name": "顶部", "mode": 5, "status": 0},
                            ],
                            "group": [
                                {
                                    "name": "普",
                                    "color": [
                                        {"name": "白色", "color": "16777215", "status": 1},
                                        {"name": "红色", "color": "16711680", "status": 0},
                                    ],
                                }
                            ],
                        },
                    }
                )
            if "getInfoByUser" in url:
                return FakeResponse({"code": 0, "data": {"info": {"guard_level": 3}}})
            return FakeResponse({"code": 0})

        client = BilibiliClient(GOOD_COOKIE)
        with mock.patch("bili_send.client.urllib.request.urlopen", fake_urlopen):
            config = client.get_danmaku_config(1)
        self.assertTrue(config.fetched)
        self.assertEqual(config.guard_level, 3)
        self.assertEqual([item.value for item in config.enabled_modes], [1])
        self.assertEqual([item.value for item in config.enabled_colors], [16777215])
        self.assertEqual(config.guard_text, "舰长")

    def test_emoticons_parse_permission_and_url(self) -> None:
        def fake_urlopen(request, timeout=0):
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "data": [
                            {
                                "pkg_id": 7,
                                "pkg_name": "房间表情",
                                "pkg_perm": 1,
                                "pkg_type": 3,
                                "emoticons": [
                                    {
                                        "emoticon_unique": "room_1_2",
                                        "emoji": "妙",
                                        "url": "//i0.hdslb.com/a.png",
                                        "perm": 1,
                                        "is_dynamic": 0,
                                    },
                                    {
                                        "emoticon_unique": "room_1_3",
                                        "emoji": "哭",
                                        "url": "https://i0.hdslb.com/b.png",
                                        "perm": 0,
                                        "unlock_show_text": "需舰长",
                                    },
                                ],
                            }
                        ]
                    },
                }
            )

        client = BilibiliClient(GOOD_COOKIE)
        with mock.patch("bili_send.client.urllib.request.urlopen", fake_urlopen):
            packages = client.get_emoticons(1)
        self.assertEqual(len(packages), 1)
        self.assertEqual(packages[0].pkg_type, 3)
        first, second = packages[0].emoticons
        self.assertTrue(first.perm)
        self.assertTrue(first.combinable)
        self.assertEqual(first.url, "https://i0.hdslb.com/a.png")
        self.assertFalse(second.perm)
        self.assertEqual(second.unlock_text, "需舰长")

    def test_emoticon_send_payload_direct(self) -> None:
        captured: dict = {}

        def fake_urlopen(request, timeout=0):
            captured["body"] = request.data.decode("utf-8")
            return FakeResponse({"code": 0, "message": "发送成功"})

        client = BilibiliClient(GOOD_COOKIE)
        with mock.patch("bili_send.client.urllib.request.urlopen", fake_urlopen):
            result = client.send_danmaku(
                "", 123, emoticon_unique="room_1_2", emoticon_text="妙"
            )
        self.assertTrue(result.ok)
        payload = parse_qs(captured["body"])
        self.assertEqual(payload["msg"], ["room_1_2"])
        self.assertEqual(payload["dm_type"], ["1"])
        self.assertEqual(payload["emoticonOptions"], ["[object Object]"])
        self.assertNotIn("emoticon_unique", payload)

    def test_emoticon_send_combinable_uses_text(self) -> None:
        captured: dict = {}

        def fake_urlopen(request, timeout=0):
            captured["body"] = request.data.decode("utf-8")
            return FakeResponse({"code": 0, "message": "发送成功"})

        client = BilibiliClient(GOOD_COOKIE)
        with mock.patch("bili_send.client.urllib.request.urlopen", fake_urlopen):
            result = client.send_danmaku(
                "666", 123, emoticon_unique="emoji_1", emoticon_text="[妙]",
                emoticon_combinable=True,
            )
        self.assertTrue(result.ok)
        payload = parse_qs(captured["body"])
        self.assertEqual(payload["msg"], ["[妙] 666"])
        self.assertNotIn("dm_type", payload)
        self.assertNotIn("emoticonOptions", payload)

    def test_non_combinable_emoticon_rejects_text(self) -> None:
        result = BilibiliClient(GOOD_COOKIE).send_danmaku(
            "666", 123, emoticon_unique="room_1_2", emoticon_text="妙"
        )
        self.assertFalse(result.ok)
        self.assertIn("不支持拼接", result.message)


class NovelRunnerTests(unittest.TestCase):
    @staticmethod
    def _wait_for(predicate, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_novel_runs_in_order_and_completes(self) -> None:
        client = FakeClient()
        task = TaskConfig(
            id="t1", name="说书", room_id=1, cookie=GOOD_COOKIE,
            interval_seconds=0.02, source=SOURCE_NOVEL,
        )
        book = NovelBook(Path("book.txt"), ["一", "二", "三"])
        events: "queue.Queue[dict]" = queue.Queue()
        runner = DanmakuRunner(task, NovelSource(book, 0), events, client=client)
        runner.start()
        self.assertTrue(self._wait_for(lambda: not runner.running))
        runner.join(1.0)
        self.assertEqual(client.sent, ["一", "二", "三"])
        self.assertTrue(runner.completed)

    def test_permission_fallback(self) -> None:
        dm = DanmakuConfig(
            modes=[DanmakuOption(1, "滚动", True)],
            colors=[DanmakuOption(16777215, "白色", True)],
            fetched=True,
        )
        client = FakeClient(dm=dm)
        task = TaskConfig(
            id="t1", name="测试", room_id=1, cookie=GOOD_COOKIE,
            interval_seconds=0.02, color=16711680, danmaku_mode=5,
        )
        book = NovelBook(Path("book.txt"), ["一"])
        events: "queue.Queue[dict]" = queue.Queue()
        runner = DanmakuRunner(task, NovelSource(book, 0), events, client=client)
        runner.start()
        self.assertTrue(self._wait_for(lambda: not runner.running))
        self.assertEqual(runner.effective_mode, 1)
        self.assertEqual(runner.effective_color, 16777215)


class RepeatSourceTests(unittest.TestCase):
    class HistoryClient:
        uid = "999"

        def __init__(self, items: list[HistoryDanmaku]) -> None:
            self.items = items
            self.calls = 0

        def get_room_history(self, room_id: int, limit: int = 60) -> list[HistoryDanmaku]:
            self.calls += 1
            return list(self.items)

    @staticmethod
    def _ts(seconds_ago: float) -> str:
        return (datetime.now() - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%d %H:%M:%S")

    def test_picks_most_frequent_in_window(self) -> None:
        client = self.HistoryClient(
            [
                HistoryDanmaku("1", "A", "666", self._ts(1)),
                HistoryDanmaku("2", "B", "666", self._ts(2)),
                HistoryDanmaku("3", "C", "888", self._ts(3)),
                HistoryDanmaku("4", "D", "太旧了", self._ts(30)),
                HistoryDanmaku("999", "自己", "666", self._ts(1)),
                HistoryDanmaku("5", "E", "[妙]", self._ts(1), dm_type=1),
            ]
        )
        source = RepeatSource(10, limit=100).bind(client, 1)
        item = source.next()
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.text, "666")
        self.assertEqual(source.last_count, 2)
        source.advance()
        self.assertEqual(source.sent, 1)
        self.assertIn("666", source.progress_text())

    def test_no_available_messages_returns_none(self) -> None:
        client = self.HistoryClient(
            [
                HistoryDanmaku("1", "A", "旧", self._ts(60)),
                HistoryDanmaku("2", "B", "[妙]", self._ts(1), dm_type=1),
            ]
        )
        source = RepeatSource(10).bind(client, 1)
        self.assertIsNone(source.next())
        self.assertIn("等待", source.progress_text())

    def test_source_roundtrip_and_history_interval_clamp(self) -> None:
        task = TaskConfig.from_dict({"source": SOURCE_REPEAT, "interval_seconds": 12})
        self.assertEqual(task.source, SOURCE_REPEAT)
        self.assertEqual(task.interval_seconds, 12)
        cfg = AppConfig.from_dict({"history_interval_seconds": 99})
        self.assertEqual(cfg.history_interval_seconds, 60)
        cfg = AppConfig.from_dict({"history_interval_seconds": 0})
        self.assertEqual(cfg.history_interval_seconds, 1)


class EmptyRepeatRunnerTests(unittest.TestCase):
    class EmptySource:
        finished = False

        def next(self):
            return None

        def advance(self) -> None:
            return None

        def progress_text(self) -> str:
            return "复读机：等待弹幕"

    def test_runner_skips_round_when_source_has_no_message(self) -> None:
        client = FakeClient()
        task = TaskConfig(
            id="t1", name="复读", room_id=1, cookie=GOOD_COOKIE,
            interval_seconds=0.02, source=SOURCE_REPEAT,
        )
        events: "queue.Queue[dict]" = queue.Queue()
        runner = DanmakuRunner(task, self.EmptySource(), events, client=client)
        runner.start()
        time.sleep(0.12)
        runner.stop()
        runner.join(1.0)
        self.assertEqual(client.sent, [])
        self.assertFalse(runner.running)


if __name__ == "__main__":
    unittest.main(verbosity=2)
