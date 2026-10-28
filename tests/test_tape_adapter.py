"""磁带适配器测试：假 v1 HTTP API + 假挂载目录 + 直连命令的单元测试。

覆盖（全部离线，不碰真实网络/真实设备）：

HttpAdapter
- mount 成功返回实际条码（v1 的 mount 无参数，只能"挂当前插入的盘"）
- mount 业务失败（400 success=false）/ HTTP 500 / 非 JSON 响应 / 请求超时 / 连不上
- unmount / eject 成功与失败；且都带 JSON body（v1 路由内部 request.get_json()）
- status：已挂载 / 无磁带（404）/ 服务器不可达；device-status
- get_adapter 的 http/direct/未知配置分支；URL 尾斜杠归一化；默认不走系统代理

DirectAdapter（备用直连）
- 命令超时 **kill 子进程**（v1 D6 缺陷的回归测试：超时后子进程不能再写文件）
- 命令缺失 / 返回码非 0 / 超时
- mount 后复查 ismount（命令假成功要拒绝）
- 条码解析：ltfs 输出三种格式 + .volume_label 兜底
- unmount/eject 的命令参数（eject 带 release_device、不带挂载点）

TapeAdapter.scan
- 延迟 import scanner；测试 monkeypatch 注入假 scanner（并行开发期 scanner 可能不存在）
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from app.services import tape_adapter
from app.services.tape_adapter import (
    DirectAdapter,
    HttpAdapter,
    TapeStatus,
    barcode_is_usable,
    barcode_matches,
    get_adapter,
    normalize_barcode,
    parse_barcode_from_text,
    read_volume_label,
)

# ---------------------------------------------------------------------------
# 假 v1 API（ltfs-webui）
# ---------------------------------------------------------------------------
class _TestHTTPServer(ThreadingHTTPServer):
    """本地测试服务器：跳过 HTTPServer.server_bind 里的 getfqdn 反向解析。

    macOS / 无 DNS 环境下 ``socket.getfqdn("127.0.0.1")`` 可能卡几十秒，测试不用等它。
    """

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class FakeV1:
    """可调状态的假 ltfs-webui：POST 端点受 ``mode`` 控制。"""

    def __init__(self) -> None:
        self.mode = "ok"  # ok | fail | error | timeout | badjson
        self.barcode = "FB1186"
        self.mounted = True
        self.device_exists = True
        self.device_path = "/dev/sg10"
        self.delay = 0.6
        self.calls: list[tuple[str, str]] = []
        self.post_bodies: list[tuple[str, dict]] = []


def _make_handler(state: FakeV1):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # 静音测试日志
            pass

        def _send(self, status: int, payload=None, *, raw: bytes | None = None) -> None:
            body = raw if raw is not None else json.dumps(payload).encode()
            self.send_response(status)
            if raw is None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                parsed = json.loads(raw)
            except ValueError:
                return {"__raw__": raw.decode("utf-8", "replace")}
            return parsed if isinstance(parsed, dict) else {"__value__": parsed}

        # -- GET ---------------------------------------------------------
        def do_GET(self):
            state.calls.append(("GET", self.path))
            if self.path == "/api/tape/current":
                if not state.mounted:
                    self._send(404, {"success": False, "message": "无磁带信息"})
                    return
                self._send(
                    200,
                    {
                        "success": True,
                        "tape": {
                            "id": 1,
                            "barcode": state.barcode,
                            "status": "mounted",
                            "capacity": "2.5TB",
                            "used_space": "0GB",
                        },
                    },
                )
            elif self.path == "/api/tape/device-status":
                self._send(
                    200,
                    {
                        "success": True,
                        "device_exists": state.device_exists,
                        "device_path": state.device_path,
                    },
                )
            else:
                self._send(404, {"success": False, "message": "not found"})

        # -- POST --------------------------------------------------------
        def do_POST(self):
            body = self._read_json()
            state.calls.append(("POST", self.path))
            state.post_bodies.append((self.path, body))
            if self.path not in ("/api/tape/mount", "/api/tape/unmount", "/api/tape/eject"):
                self._send(404, {"success": False, "message": "not found"})
                return
            mode = state.mode
            if mode == "timeout":
                time.sleep(state.delay)
            if mode == "error":
                self._send(500, {"success": False, "message": "数据库挂了"})
                return
            if mode == "badjson":
                self._send(200, raw=b"<html>not json</html>")
                return
            if mode == "fail":
                self._send(400, {"success": False, "message": "操作失败：没有插入磁带"})
                return
            if self.path == "/api/tape/mount":
                state.mounted = True
                self._send(
                    200,
                    {
                        "success": True,
                        "message": "磁带挂载成功",
                        "operation_id": "op-1",
                        "tape": {"id": 1, "barcode": state.barcode, "status": "mounted"},
                    },
                )
            else:
                state.mounted = False
                self._send(200, {"success": True, "message": "操作成功"})

    return Handler


@pytest.fixture
def fake_v1():
    """起一个本地假 v1，yield ``(base_url, state)``。"""
    state = FakeV1()
    server = _TestHTTPServer(("127.0.0.1", 0), _make_handler(state))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base_url, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def session():
    sess = requests.Session()
    sess.trust_env = False  # 别让系统代理干扰 127.0.0.1
    yield sess
    sess.close()


def _http_adapter(base_url: str, session, *, timeout: float = 1.0, **kwargs) -> HttpAdapter:
    return HttpAdapter(base_url=base_url, timeout=timeout, session=session, **kwargs)


def _closed_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


# ===========================================================================
# HttpAdapter：mount / unmount / eject
# ===========================================================================
def test_http_mount_success_returns_actual_barcode(fake_v1, session):
    base_url, state = fake_v1
    adapter = _http_adapter(base_url, session)

    ok, message, barcode = adapter.mount()

    assert (ok, barcode) == (True, "FB1186")
    assert "挂载成功" in message
    # v1 的 mount 没有参数：绝不能带 barcode（带参也不会被 v1 接受）
    assert ("POST", "/api/tape/mount") in state.calls
    assert state.post_bodies == [("/api/tape/mount", {})]


def test_http_mount_business_failure(fake_v1, session):
    base_url, state = fake_v1
    state.mode = "fail"

    ok, message, barcode = _http_adapter(base_url, session).mount()

    assert ok is False
    assert barcode is None
    assert "没有插入磁带" in message


def test_http_mount_http_500(fake_v1, session):
    base_url, state = fake_v1
    state.mode = "error"

    ok, message, barcode = _http_adapter(base_url, session).mount()

    assert ok is False
    assert barcode is None
    assert "HTTP 500" in message


def test_http_mount_non_json_response(fake_v1, session):
    base_url, state = fake_v1
    state.mode = "badjson"

    ok, message, _barcode = _http_adapter(base_url, session).mount()

    assert ok is False
    assert "JSON" in message


def test_http_mount_timeout_does_not_raise(fake_v1, session):
    base_url, state = fake_v1
    state.mode = "timeout"
    state.delay = 0.6

    started = time.time()
    ok, message, barcode = _http_adapter(base_url, session, timeout=0.15).mount()
    elapsed = time.time() - started

    assert ok is False and barcode is None
    assert "超时" in message
    assert elapsed < 1.0


def test_http_unmount_eject_success_and_json_body(fake_v1, session):
    base_url, state = fake_v1
    adapter = _http_adapter(base_url, session)

    ok_unmount, message_unmount = adapter.unmount()
    ok_eject, message_eject = adapter.eject()

    assert (ok_unmount, ok_eject) == (True, True)
    assert "成功" in message_unmount and "成功" in message_eject
    # v1 的 unmount/eject 路由会 request.get_json()，必须带 JSON body，否则 v1 500
    assert [path for path, _ in state.post_bodies] == ["/api/tape/unmount", "/api/tape/eject"]
    assert all(body == {} for _, body in state.post_bodies)


def test_http_unmount_eject_failure(fake_v1, session):
    base_url, state = fake_v1
    state.mode = "error"

    ok_unmount, _msg_unmount = _http_adapter(base_url, session).unmount()
    ok_eject, msg_eject = _http_adapter(base_url, session).eject()

    assert (ok_unmount, ok_eject) == (False, False)
    assert "HTTP 500" in msg_eject


# ===========================================================================
# HttpAdapter：status / 不可达 / 配置
# ===========================================================================
def test_http_status_mounted(fake_v1, session):
    base_url, _state = fake_v1
    status = _http_adapter(base_url, session).status()

    assert isinstance(status, TapeStatus)
    assert status.mounted is True
    assert status.barcode == "FB1186"
    assert status.device_exists is True
    assert status.device_path == "/dev/sg10"
    assert "current" in status.raw


def test_http_status_no_tape_404_is_not_an_error(fake_v1, session):
    base_url, state = fake_v1
    state.mounted = False
    status = _http_adapter(base_url, session).status()

    assert status.mounted is False
    assert status.barcode is None
    assert status.device_exists is True
    assert "errors" not in status.raw


def test_http_status_server_unreachable_never_raises(session):
    adapter = HttpAdapter(base_url=f"http://127.0.0.1:{_closed_port()}", timeout=0.3, session=session)

    status = adapter.status()
    ok, message, barcode = adapter.mount()

    assert status.mounted is False
    assert status.barcode is None
    assert status.raw["errors"]  # 错误只记录在 raw，不抛
    assert (ok, barcode) == (False, None)
    assert "连不上" in message


def test_http_base_url_trailing_slash_normalized(fake_v1, session):
    base_url, state = fake_v1
    adapter = _http_adapter(base_url + "/", session)

    ok, _message, _barcode = adapter.mount()

    assert ok is True
    assert ("POST", "/api/tape/mount") in state.calls  # 不会出现双斜杠 /api//tape
    assert adapter.base_url == base_url


def test_http_adapter_default_session_ignores_proxy_env():
    adapter = HttpAdapter(base_url="http://127.0.0.1:1", timeout=0.1)
    try:
        assert adapter.session.trust_env is False
    finally:
        adapter.session.close()


# ===========================================================================
# get_adapter / 条码工具函数
# ===========================================================================
def test_get_adapter_http_default(monkeypatch):
    monkeypatch.setattr(tape_adapter.Config, "LTFS_API_BASE", "http://v1:5001", raising=False)
    adapter = get_adapter({"TAPE_ADAPTER": "http", "LTFS_TIMEOUT": 7})

    assert isinstance(adapter, HttpAdapter)
    assert adapter.base_url == "http://v1:5001"
    assert adapter.timeout == 7.0


def test_get_adapter_direct():
    adapter = get_adapter(
        {
            "TAPE_ADAPTER": "direct",
            "LTFS_DEVICE": "/dev/sg9",
            "LTFS_MOUNT": "/media/tape",
            "LTFS_TIMEOUT": "42",
        }
    )

    assert isinstance(adapter, DirectAdapter)
    assert adapter.device == "/dev/sg9"
    assert adapter.mount_point == "/media/tape"
    assert adapter.timeout == 42.0


def test_get_adapter_unknown_kind_raises():
    with pytest.raises(ValueError):
        get_adapter({"TAPE_ADAPTER": "carrier-pigeon"})


def test_get_adapter_defaults_to_direct_after_v1_merge():
    """合并 v1 后默认适配器是 direct（设备由本服务自己独占）。"""
    adapter = get_adapter(None)
    assert isinstance(adapter, DirectAdapter)

    # 显式选择 http 仍然可用（过渡期让 v1 继续独占驱动器）
    http_adapter = get_adapter({"TAPE_ADAPTER": "http"})
    assert isinstance(http_adapter, HttpAdapter)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("  ", None),
        ("  FB1186 ", "FB1186"),
        ("fb1186", "fb1186"),
    ],
)
def test_normalize_barcode(raw, expected):
    assert normalize_barcode(raw) == expected


@pytest.mark.parametrize(
    ("value", "usable"),
    [
        ("FB1186", True),
        ("unknown", False),
        ("UNKNOWN", False),
        ("None", False),
        ("", False),
        (None, False),
    ],
)
def test_barcode_is_usable(value, usable):
    assert barcode_is_usable(value) is usable


@pytest.mark.parametrize(
    ("actual", "expected", "matched"),
    [
        ("FB1186", "FB1186", True),
        ("fb1186", "FB1186", True),
        (" FB1186 ", "fb1186", True),
        ("FB1186", "FB1199", False),
        ("UNKNOWN", "UNKNOWN", False),  # 两边都是 UNKNOWN 也必须判不匹配
        ("UNKNOWN", None, False),
        ("FB1186", None, True),  # 不指定预期时只要有可用条码即可
    ],
)
def test_barcode_matches(actual, expected, matched):
    assert barcode_matches(actual, expected) is matched


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1c LTFS17227I Tape attribute: Barcode = FB1186", "FB1186"),
        ("barcode: AB1234\n", "AB1234"),
        ("Volume Label: LBL77", "LBL77"),
        # 用户标签可能带连字符：不能被 [A-Z0-9]+ 截成 LTO6
        ("Tape attribute: Barcode = LTO6-TEST", "LTO6-TEST"),
        ("Tape attribute: Barcode = LTO6-0001", "LTO6-0001"),
        ("barcode: AB-123\n", "AB-123"),
        ("barcode: AB-123-\n", "AB-123"),  # 首尾 - 去掉
        ("barcode: -----\n", None),  # 全是 - → 无效
        ("ltfs: no barcode here", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_barcode_from_text(text, expected):
    assert parse_barcode_from_text(text) == expected


def test_read_volume_label(tmp_path):
    mount = tmp_path / "tape"
    mount.mkdir()
    (mount / ".volume_label").write_text("FB1186\n", encoding="utf-8")

    assert read_volume_label(mount) == "FB1186"
    assert read_volume_label(tmp_path / "missing") is None


# ===========================================================================
# DirectAdapter
# ===========================================================================
def test_direct_exec_timeout_kills_child(tmp_path):
    """v1 D6 回归：超时后子进程必须被杀掉，不能留下幽灵 ltfs。"""
    marker = tmp_path / "orphan-marker.txt"
    code = (
        "import pathlib, time; "
        "time.sleep(1.0); "
        f"pathlib.Path({str(marker)!r}).write_text('still alive')"
    )
    adapter = DirectAdapter(device="/dev/null", timeout=0.2)

    started = time.time()
    rc, _out, err, timed_out = adapter._exec([sys.executable, "-c", code])
    elapsed = time.time() - started

    assert timed_out is True
    assert rc == 124
    assert "超时" in err
    assert elapsed < 1.0
    time.sleep(1.1)  # 等过原本的 sleep 时间，确认子进程没活下来
    assert not marker.exists(), "子进程没有被 kill，超时后仍在执行"


def test_direct_exec_missing_binary():
    rc, _out, err, timed_out = DirectAdapter(timeout=1)._exec(["/nonexistent/ltfs-xyz"])

    assert (rc, timed_out) == (127, False)
    assert "命令不存在" in err


def test_direct_exec_success():
    rc, out, _err, timed_out = DirectAdapter(timeout=5)._exec(
        [sys.executable, "-c", "print('hello-ltfs')"]
    )

    assert (rc, timed_out) == (0, False)
    assert "hello-ltfs" in out


def test_direct_mount_success_parses_barcode(tmp_path, monkeypatch):
    mount = tmp_path / "tape"
    adapter = DirectAdapter(device="/dev/sg10", mount_point=mount, timeout=5)
    calls: list[list[str]] = []

    def fake_exec(argv):
        calls.append(argv)
        return 0, "1c LTFS17227I Tape attribute: Barcode = FB1186\n", "", False

    monkeypatch.setattr(adapter, "_exec", fake_exec)
    monkeypatch.setattr(tape_adapter.os.path, "ismount", lambda _path: True)

    ok, message, barcode = adapter.mount()

    assert (ok, barcode) == (True, "FB1186")
    assert "挂载成功" in message
    assert calls == [["ltfs", "-o", "devname=/dev/sg10", str(mount)]]
    assert mount.is_dir()  # 挂载点会先创建


def test_direct_mount_rejects_fake_success(tmp_path, monkeypatch):
    """命令 rc=0 但 ismount 为假 → 不能当成挂载成功（v1 曾出现假 success）。"""
    adapter = DirectAdapter(device="/dev/sg10", mount_point=tmp_path / "tape")
    monkeypatch.setattr(adapter, "_exec", lambda argv: (0, "", "", False))
    monkeypatch.setattr(tape_adapter.os.path, "ismount", lambda _path: False)

    ok, message, barcode = adapter.mount()

    assert (ok, barcode) == (False, None)
    assert "未挂载" in message


def test_direct_mount_command_failure(tmp_path, monkeypatch):
    adapter = DirectAdapter(device="/dev/sg10", mount_point=tmp_path / "tape")
    monkeypatch.setattr(adapter, "_exec", lambda argv: (1, "", "ltfs: 设备忙", False))

    ok, message, _barcode = adapter.mount()

    assert ok is False
    assert "设备忙" in message


def test_direct_mount_timeout_message(tmp_path, monkeypatch):
    adapter = DirectAdapter(device="/dev/sg10", mount_point=tmp_path / "tape")
    monkeypatch.setattr(adapter, "_exec", lambda argv: (124, "", "命令超时（>0.2s），已终止: ltfs", True))

    ok, message, _barcode = adapter.mount()

    assert ok is False
    assert "超时" in message


def test_direct_mount_barcode_from_volume_label(tmp_path, monkeypatch):
    mount = tmp_path / "tape"
    (mount).mkdir()
    (mount / ".volume_label").write_text("LBL77", encoding="utf-8")
    adapter = DirectAdapter(device="/dev/sg10", mount_point=mount)
    monkeypatch.setattr(adapter, "_exec", lambda argv: (0, "mounted ok", "", False))
    monkeypatch.setattr(tape_adapter.os.path, "ismount", lambda _path: True)

    ok, _message, barcode = adapter.mount()

    assert ok is True
    assert barcode == "LBL77"


def test_direct_unmount_eject_command_arguments(tmp_path, monkeypatch):
    mount = tmp_path / "tape"
    adapter = DirectAdapter(device="/dev/sg10", mount_point=mount)
    calls: list[list[str]] = []

    def fake_exec(argv):
        calls.append(argv)
        return 0, "", "", False

    monkeypatch.setattr(adapter, "_exec", fake_exec)

    ok_unmount, _ = adapter.unmount()
    ok_eject, _ = adapter.eject()

    assert (ok_unmount, ok_eject) == (True, True)
    assert calls[0] == ["umount", str(mount)]
    # 与 v1 一致：eject 带 release_device 且不带挂载点
    assert calls[1] == ["ltfs", "-o", "devname=/dev/sg10", "-o", "release_device"]


def test_direct_status(tmp_path, monkeypatch):
    device = tmp_path / "sg10"
    device.write_text("", encoding="utf-8")
    mount = tmp_path / "tape"
    mount.mkdir()
    (mount / ".volume_label").write_text("FB1186\n", encoding="utf-8")
    adapter = DirectAdapter(device=str(device), mount_point=mount)

    monkeypatch.setattr(tape_adapter.os.path, "ismount", lambda _path: True)
    status = adapter.status()
    assert status.mounted is True
    assert status.barcode == "FB1186"
    assert status.device_exists is True
    assert status.device_path == str(device)

    # 设备不存在 + 未挂载
    missing = DirectAdapter(device=str(tmp_path / "nope"), mount_point=tmp_path / "nope-mount")
    monkeypatch.setattr(tape_adapter.os.path, "ismount", lambda _path: False)
    status_missing = missing.status()
    assert status_missing.mounted is False
    assert status_missing.barcode is None
    assert status_missing.device_exists is False


# ===========================================================================
# TapeAdapter.scan：延迟 import scanner（并行开发期可注入假 scanner）
# ===========================================================================
def test_scan_delegates_to_scanner(monkeypatch):
    recorded = {}

    def fake_scan_tape(app, mount_path, *, barcode, progress=None):
        recorded.update(app=app, mount_path=mount_path, barcode=barcode, progress=progress)
        return "scan-stats-sentinel"

    monkeypatch.setattr(
        tape_adapter, "_import_scanner", lambda: SimpleNamespace(scan_tape=fake_scan_tape)
    )
    sentinel_app = object()
    adapter = HttpAdapter(base_url="http://v1:5001", app=sentinel_app, timeout=1)

    def cb(done, message=""):
        pass

    result = adapter.scan("/media/tape", "FB1186", progress=cb)

    assert result == "scan-stats-sentinel"
    assert recorded == {
        "app": sentinel_app,
        "mount_path": "/media/tape",
        "barcode": "FB1186",
        "progress": cb,
    }


def test_scan_requires_app(monkeypatch):
    monkeypatch.setattr(
        tape_adapter,
        "_import_scanner",
        lambda: SimpleNamespace(scan_tape=lambda *a, **k: None),
    )
    adapter = HttpAdapter(base_url="http://v1:5001", timeout=1)

    with pytest.raises(RuntimeError, match="app"):
        adapter.scan("/media/tape", "FB1186")


def test_import_scanner_missing_module_reports_runtime_error(monkeypatch):
    """scanner.py 不存在时给清晰错误（audit 会记为 failed），而不是 ImportError 裸奔。"""
    import app.services as services_pkg

    monkeypatch.delitem(sys.modules, "app.services.scanner", raising=False)
    monkeypatch.delattr(services_pkg, "scanner", raising=False)
    monkeypatch.setitem(sys.modules, "app.services.scanner", None)

    with pytest.raises(RuntimeError, match="scanner"):
        tape_adapter._import_scanner()


def test_scan_propagates_scanner_import_error_as_runtime_error(monkeypatch):
    def broken_import():
        raise RuntimeError("scanner 模块不可用：缺少 app/services/scanner.py，无法扫描磁带")

    monkeypatch.setattr(tape_adapter, "_import_scanner", broken_import)
    adapter = HttpAdapter(base_url="http://v1:5001", app=object(), timeout=1)

    with pytest.raises(RuntimeError, match="scanner"):
        adapter.scan("/media/tape", "FB1186")
