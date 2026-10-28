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
from pathlib import Path
from unittest import mock

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
    LoginStatus,
    RoomInfo,
    SendResult,
    cookie_warnings,
    parse_cookie,
)
from bili_send.config import AppConfig, load_config, save_config  # noqa: E402
from bili_send.messages import (  # noqa: E402
    MessageFileError,
    MessageItem,
    MessagePool,
    load_messages,
    save_messages,
)
from bili_send.runner import DanmakuRunner  # noqa: E402

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

    def __init__(self, results: list[SendResult] | None = None, login: LoginStatus | None = None) -> None:
        self.sent: list[str] = []
        self._results = results or []
        self._login = login or LoginStatus(known=True, logged_in=True, uname="测试用户", uid="1")

    def check_login(self) -> LoginStatus:
        return self._login

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
