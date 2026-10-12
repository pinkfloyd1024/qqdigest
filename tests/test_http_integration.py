"""HTTP 上报链路集成测试。

真实验证：ReportServer 监听 -> 收到 OneBot 事件 POST -> 解析入库。
使用临时端口（port=0）与临时数据库，不与生产实例冲突。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqdigest.onebot import ReportServer, parse_event
from qqdigest.store import Store
from tests.test_core import make_cfg


def _event(**kw) -> dict:
    base = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 5001,
        "user_id": 20001,
        "self_id": 1000,
        "time": int(time.time()),
        "message": [{"type": "text", "data": {"text": "集成测试消息"}}],
        "sender": {"user_id": 20001, "nickname": "测试员"},
    }
    base.update(kw)
    return base


class TestReportServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        # port=0 -> 由系统分配空闲端口
        base = make_cfg(root)
        self.cfg = replace(base, onebot=replace(base.onebot, report_port=0))
        self.store = Store(self.cfg.db_path)

        def handler(event):
            record, _ = parse_event(event, self.cfg)
            if record is not None:
                self.store.insert_messages([record])

        self.server = ReportServer(self.cfg, handler)
        self.server.start_background()
        self.url = self.server.address
        time.sleep(0.1)

    def tearDown(self):
        self.server.stop()
        self.store.close()
        self.tmp.cleanup()

    def _post(self, payload: object, url: str | None = None):
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url or self.url, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_health_endpoint(self):
        health = self.url.rsplit("/", 1)[0] + "/health"
        with urllib.request.urlopen(health, timeout=5) as resp:
            self.assertEqual(resp.status, 200)

    def test_message_event_is_stored(self):
        status, payload = self._post(_event())
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("retcode"), 0)
        self.assertEqual(self.store.count_messages(), 1)
        row = self.store.fetch_messages(0, int(time.time()) + 10)[0]
        self.assertEqual(row["peer_id"], "20001")
        self.assertEqual(row["text"], "集成测试消息")

    def test_duplicate_event_not_double_counted(self):
        self._post(_event())
        self._post(_event())
        self.assertEqual(self.store.count_messages(), 1)

    def test_filtered_group_event_not_stored(self):
        self._post(_event(message_type="group", group_id=999))
        self.assertEqual(self.store.count_messages(), 0)

    def test_meta_event_returns_ok(self):
        status, payload = self._post({"post_type": "meta_event", "meta_event_type": "heartbeat"})
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("retcode"), 0)
        self.assertEqual(self.store.count_messages(), 0)

    def test_bad_json_rejected(self):
        req = urllib.request.Request(
            self.url, data=b"not-json", headers={"Content-Type": "application/json"}
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_wrong_path_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(_event(), url=self.url.rsplit("/", 1)[0] + "/wrong")
        self.assertEqual(ctx.exception.code, 404)

    def test_chunked_body_accepted(self):
        """★ 关键回归测试：NapCat 用 Node 的 http 客户端上报时走 chunked 编码。

        未设置 Content-Length 时 Node 会退化为 Transfer-Encoding: chunked，
        而 BaseHTTPRequestHandler 不会自动解码。只按 Content-Length 读取会拿到
        空 body 并回 400 —— 真实环境中的表现就是"NapCat 一直在报
        Unexpected status code: 400，一条消息都收不到"。
        """
        import socket
        from urllib.parse import urlparse

        parsed = urlparse(self.url)
        payload = json.dumps(_event(message_id=9001), ensure_ascii=False).encode("utf-8")

        # 手工构造分块请求体：每 20 字节一块
        chunks = [payload[i : i + 20] for i in range(0, len(payload), 20)]
        body = b"".join(
            f"{len(c):X}\r\n".encode("ascii") + c + b"\r\n" for c in chunks
        ) + b"0\r\n\r\n"

        request = (
            f"POST {parsed.path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Content-Type: application/json\r\n"
            "Transfer-Encoding: chunked\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii") + body

        with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as sock:
            sock.sendall(request)
            response = b""
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                response += data

        status_line = response.split(b"\r\n", 1)[0]
        self.assertIn(b"200", status_line, f"分块请求被拒绝：{status_line!r}")
        self.assertEqual(self.store.count_messages(), 1, "分块请求体没有被正确解析入库")

    def test_content_length_still_works(self):
        """回归：常规 Content-Length 请求不能被改动破坏。"""
        status, payload = self._post(_event(message_id=9002))
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("retcode"), 0)
        self.assertEqual(self.store.count_messages(), 1)

    def test_second_server_on_same_port_is_rejected(self):
        """端口已被占用时必须明确报错。

        背景：Windows 上 SO_REUSEADDR 允许两个进程绑定同一端口，于是新实例
        "启动成功"却收不到任何上报（连接被旧实例接走），排查时看到的是旧代码的
        行为——一个极难发现的幽灵进程问题。这里锁死"必须报错"。
        """
        import socket

        from urllib.parse import urlparse

        parsed = urlparse(self.url)
        if parsed.port in (None, 0):
            self.skipTest("未使用固定端口")
        # 用真实绑定到的端口构造配置，模拟"第二个实例想绑同一端口"
        cfg2 = replace(self.cfg, onebot=replace(self.cfg.onebot, report_port=parsed.port))
        with self.assertRaises(OSError) as ctx:
            ReportServer(cfg2, lambda _e: None)
        self.assertIn("已经有服务", str(ctx.exception))

    def test_probe_reports_busy_port(self):
        from urllib.parse import urlparse

        from qqdigest.onebot import _port_in_use

        parsed = urlparse(self.url)
        self.assertTrue(_port_in_use(parsed.hostname, parsed.port))
        # 一个几乎不可能被占用的端口
        self.assertFalse(_port_in_use("127.0.0.1", 59321))


class TestReportServerAuth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        base = make_cfg(root)
        self.cfg = replace(base, onebot=replace(base.onebot, report_port=0, report_token="s3cret"))
        self.store = Store(self.cfg.db_path)

        def handler(event):
            record, _ = parse_event(event, self.cfg)
            if record is not None:
                self.store.insert_messages([record])

        self.server = ReportServer(self.cfg, handler)
        self.server.start_background()
        self.url = self.server.address
        time.sleep(0.1)

    def tearDown(self):
        self.server.stop()
        self.store.close()
        self.tmp.cleanup()

    def _post(self, headers: dict | None = None, url: str | None = None):
        body = json.dumps(_event()).encode("utf-8")
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request(url or self.url, data=body, headers=h)
        return urllib.request.urlopen(req, timeout=5)

    def test_missing_token_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post()
        self.assertEqual(ctx.exception.code, 401)
        self.assertEqual(self.store.count_messages(), 0)

    def test_bearer_token_accepted(self):
        status = self._post({"Authorization": "Bearer s3cret"}).status
        self.assertEqual(status, 200)
        self.assertEqual(self.store.count_messages(), 1)

    def test_query_token_accepted(self):
        status = self._post(url=f"{self.url}?token=s3cret").status
        self.assertEqual(status, 200)

    def test_wrong_token_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post({"Authorization": "Bearer nope"})
        self.assertEqual(ctx.exception.code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
