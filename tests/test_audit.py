"""盘点流程测试：假 v1 API + 临时"挂载目录" + 假 scanner，全程离线。

覆盖（对应 task-8 验收清单）：

- 正常流：mount → scan → manifest → unmount → eject，AuditRun 状态机逐级推进到 done；
- 条码不符 / 实际条码 UNKNOWN / 用户传 UNKNOWN：**中止（aborted）且不扫描**，并退出磁带；
- HTTP 500 / 超时：mount 失败落到 failed，异常不冒泡到调用方；
- manifest：原子写、写后读回、内容格式 + code/confidence/flags 补全、1 万文件量级、
  旧格式/损坏文件读回返回 None 而不抛；
- 失败态：扫描抛异常、写 manifest 失败、eject 失败都记 failed，且不会留下"看起来完成"
  的 run（done 只在最后一步写入）。
"""
from __future__ import annotations

import json
import threading
import time
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from flask import Flask

from app.config import Config
from app.db import db
from app.models import AuditRun, LibraryFile, LibraryVolume
from app.services import audit
from app.services.audit import (
    MANIFEST_DIRNAME,
    MANIFEST_SCHEMA,
    STATUS_ABORTED,
    STATUS_DONE,
    STATUS_FAILED,
    read_manifest,
    run_audit,
    write_manifest,
)
from app.services.tape_adapter import HttpAdapter

# ---------------------------------------------------------------------------
# fixture：独立临时 app（不依赖 create_app 里的路由，避免并行开发期路由模块缺失）
# ---------------------------------------------------------------------------
@pytest.fixture
def app(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "avlib-test.db"
    test_app = Flask("avlib_audit_test")
    test_app.config.from_object(Config)
    test_app.config.update(
        {
            "TESTING": True,
            "DATA_DIR": data_dir,
            "DB_PATH": db_path,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
            "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"timeout": 30}},
            # audit 从 config 读挂载点；假适配器也读它
            "LTFS_MOUNT": str(tmp_path / "tape"),
            "TAPE_ADAPTER": "http",
            "LTFS_API_BASE": "http://127.0.0.1:1",
            "LTFS_TIMEOUT": 1,
        }
    )
    db.init_app(test_app)
    ctx = test_app.app_context()
    ctx.push()
    db.create_all()
    try:
        yield test_app
    finally:
        db.session.remove()
        db.drop_all()
        ctx.pop()


@pytest.fixture
def mount_dir(app) -> Path:
    path = Path(app.config["LTFS_MOUNT"])
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# 假 scanner 的 ScanStats（scanner.py 并行开发中，这里用同构的 SimpleNamespace）
# ---------------------------------------------------------------------------
def make_stats(*, volume_id=None, files_seen=0, new=0, **overrides):
    payload = {
        "volume_id": volume_id,
        "kind": "tape",
        "root": "",
        "files_seen": files_seen,
        "videos": files_seen,
        "sidecars": 0,
        "new": new,
        "updated": 0,
        "unchanged": 0,
        "deleted": 0,
        "with_code": 0,
        "low_confidence": 0,
        "no_code": 0,
        "chinese_sub_files": 0,
        "elapsed_s": 0.01,
        "errors": [],
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def make_file(rel_path: str, *, code="SSIS-001", confidence=95, flags="") -> LibraryFile:
    return LibraryFile(
        volume_id=0,
        rel_path=rel_path,
        name=Path(rel_path).name,
        size=123,
        code_guess=code,
        confidence=confidence,
        flags=flags,
    )


def seed_volume(app, barcode: str, files: list[LibraryFile]) -> LibraryVolume:
    volume = LibraryVolume(kind="tape", ref=barcode, name=f"磁带 {barcode}")
    db.session.add(volume)
    db.session.commit()
    for item in files:
        item.volume_id = volume.id
        db.session.add(item)
    db.session.commit()
    return volume


def latest_run() -> AuditRun | None:
    return db.session.query(AuditRun).order_by(AuditRun.id.desc()).first()


# ---------------------------------------------------------------------------
# 假适配器（只替换硬件操作；scan 由测试注入）
# ---------------------------------------------------------------------------
class _TestHTTPServer(ThreadingHTTPServer):
    """本地测试服务器：跳过 HTTPServer.server_bind 里的 getfqdn 反向解析（可能卡几十秒）。"""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class FakeAdapter:
    def __init__(
        self,
        app,
        mount_point,
        *,
        actual_barcode="FB1186",
        mount_ok=True,
        mount_message="磁带挂载成功",
        scan_stats=None,
        scan_exc=None,
        unmount_ok=True,
        eject_ok=True,
        eject_message="磁带弹出成功",
    ):
        self.app = app
        self.mount_point = str(mount_point)
        self.actual_barcode = actual_barcode
        self.mount_ok = mount_ok
        self.mount_message = mount_message
        self.scan_stats = scan_stats
        self.scan_exc = scan_exc
        self.unmount_ok = unmount_ok
        self.eject_ok = eject_ok
        self.eject_message = eject_message
        self.calls: list[str] = []
        self.run_statuses: list[str] = []
        self.scan_args = None

    def _record_status(self) -> None:
        run = latest_run()
        if run is not None:
            self.run_statuses.append(run.status)

    def mount(self):
        self.calls.append("mount")
        self._record_status()
        return self.mount_ok, self.mount_message, self.actual_barcode

    def unmount(self):
        self.calls.append("unmount")
        return self.unmount_ok, "" if self.unmount_ok else "umount: target busy"

    def eject(self):
        self.calls.append("eject")
        return self.eject_ok, self.eject_message if self.eject_ok else "device busy"

    def scan(self, mount_path, barcode, *, progress=None):
        self.calls.append("scan")
        self.scan_args = (mount_path, barcode)
        self._record_status()
        if self.scan_exc is not None:
            raise self.scan_exc
        return self.scan_stats if self.scan_stats is not None else make_stats()


# ---------------------------------------------------------------------------
# 迷你假 v1 API（HTTP 端到端用）
# ---------------------------------------------------------------------------
class MiniV1:
    def __init__(self) -> None:
        self.mode = "ok"  # ok | error | timeout
        self.barcode = "FB1186"
        self.delay = 0.6
        self.calls: list[tuple[str, str]] = []


def _make_mini_handler(state: MiniV1):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state.calls.append(("GET", self.path))
            if self.path == "/api/tape/current":
                self._send(200, {"success": True, "tape": {"barcode": state.barcode, "status": "mounted"}})
            elif self.path == "/api/tape/device-status":
                self._send(200, {"success": True, "device_exists": True, "device_path": "/dev/sg10"})
            else:
                self._send(404, {"success": False, "message": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            state.calls.append(("POST", self.path))
            if state.mode == "timeout":
                time.sleep(state.delay)
            if state.mode == "error":
                self._send(500, {"success": False, "message": "数据库挂了"})
                return
            if self.path == "/api/tape/mount":
                self._send(200, {"success": True, "message": "磁带挂载成功",
                                 "tape": {"barcode": state.barcode, "status": "mounted"}})
            else:
                self._send(200, {"success": True, "message": "操作成功"})

    return Handler


@pytest.fixture
def mini_v1():
    state = MiniV1()
    server = _TestHTTPServer(("127.0.0.1", 0), _make_mini_handler(state))
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


def _http_adapter(base_url: str, timeout: float = 1.0) -> HttpAdapter:
    session = requests.Session()
    session.trust_env = False
    return HttpAdapter(base_url=base_url, timeout=timeout, session=session)


def _progress_collector():
    events: list[tuple[int, str]] = []

    def progress(done: int, message: str = "") -> None:
        events.append((done, message))

    return events, progress


# ===========================================================================
# 1. 正常流
# ===========================================================================
def test_run_audit_success_full_flow(app, mount_dir):
    volume = seed_volume(
        app,
        "FB1186",
        [
            make_file("SSIS-001/SSIS-001.mp4"),
            make_file("SSIS-001/SSIS-001.srt"),
        ],
    )
    (mount_dir / "SSIS-001").mkdir()
    (mount_dir / "SSIS-001" / "SSIS-001.mp4").write_bytes(b"x" * 123)
    (mount_dir / "SSIS-001" / "SSIS-001.srt").write_bytes(b"y" * 45)

    adapter = FakeAdapter(
        app,
        mount_dir,
        scan_stats=make_stats(volume_id=volume.id, files_seen=2, new=2),
    )
    events, progress = _progress_collector()

    report = run_audit(
        app,
        "FB1186",
        expected_barcode="FB1186",
        adapter=adapter,
        progress=progress,
    )

    assert report["ok"] is True
    assert report["status"] == STATUS_DONE
    assert report["actual_barcode"] == "FB1186"
    assert report["volume_id"] == volume.id
    assert report["files_seen"] == 2
    assert report["files_new"] == 2
    assert report["errors"] == []
    assert report["run_id"] is not None

    # 顺序：mount → scan → unmount → eject；扫描时状态是 scanning，挂载时是 mounting
    assert adapter.calls == ["mount", "scan", "unmount"]
    assert adapter.run_statuses == ["mounting", "scanning"]
    assert adapter.scan_args == (str(mount_dir), "FB1186")

    # AuditRun 落库状态正确
    db.session.expire_all()
    run = db.session.get(AuditRun, report["run_id"])
    assert run.status == STATUS_DONE
    assert run.actual_barcode == "FB1186"
    assert run.expected_barcode == "FB1186"
    assert run.volume_id == volume.id
    assert run.files_seen == 2
    assert run.files_new == 2
    assert run.started_at is not None and run.finished_at is not None
    assert run.error is None

    # manifest 落盘 + 读回 + 内容
    manifest_file = Path(report["manifest_path"])
    assert manifest_file == Path(app.config["MANIFEST_DIR"]) / "FB1186.json"
    assert manifest_file.is_file()
    assert not (mount_dir / MANIFEST_DIRNAME).exists()  # 不再写磁带
    manifest = read_manifest(manifest_file)
    assert manifest is not None
    from app.models import TapeManifest

    db_row = db.session.query(TapeManifest).filter_by(barcode="FB1186").first()
    assert db_row is not None and db_row.file_count == 2
    assert manifest["schema"] == MANIFEST_SCHEMA
    assert manifest["barcode"] == "FB1186"
    assert manifest["stats"]["files_seen"] == 2
    assert manifest["file_count"] == 2
    by_rel = {item["rel_path"]: item for item in manifest["files"]}
    assert set(by_rel) == {"SSIS-001/SSIS-001.mp4", "SSIS-001/SSIS-001.srt"}
    assert by_rel["SSIS-001/SSIS-001.mp4"]["size"] == 123
    assert by_rel["SSIS-001/SSIS-001.mp4"]["code"] == "SSIS-001"
    assert by_rel["SSIS-001/SSIS-001.mp4"]["confidence"] == 95
    assert by_rel["SSIS-001/SSIS-001.mp4"]["mtime"] is None or str(
        by_rel["SSIS-001/SSIS-001.mp4"]["mtime"]
    ).startswith("20")

    # 进度：0 → 100，单调不减
    assert events[0][0] == 0
    assert events[-1][0] == 100
    assert [done for done, _ in events] == sorted(done for done, _ in events)


def test_run_audit_status_sequence_is_persisted(app, mount_dir):
    """挂载前 run 已落库为 pending/mounting（容器崩溃也能看出未完成）。"""
    adapter = FakeAdapter(app, mount_dir, scan_stats=make_stats(files_seen=0, new=0))

    report = run_audit(app, "FB1186", adapter=adapter)

    assert report["status"] == STATUS_DONE
    # FakeAdapter 在 mount/scan 两个时点记录状态，说明这些状态真的写进库里了
    assert adapter.run_statuses == ["mounting", "scanning"]
    assert latest_run().status == STATUS_DONE


# ===========================================================================
# 2. 防串盘：条码不符 / UNKNOWN 中止
# ===========================================================================
def test_run_audit_aborts_on_barcode_mismatch(app, mount_dir):
    adapter = FakeAdapter(app, mount_dir, actual_barcode="FB9999")

    report = run_audit(app, "FB1186", expected_barcode="FB1186", adapter=adapter)

    assert report["ok"] is False
    assert report["status"] == STATUS_ABORTED
    assert report["actual_barcode"] == "FB9999"
    assert any("条码不符" in err for err in report["errors"])
    assert "scan" not in adapter.calls  # 绝不能扫描别的盘
    assert adapter.calls == ["mount", "unmount"]  # 只卸载，弹出由用户手动执行
    assert not (mount_dir / MANIFEST_DIRNAME).exists()
    assert latest_run().status == STATUS_ABORTED


def test_run_audit_aborts_on_unknown_actual_barcode(app, mount_dir):
    adapter = FakeAdapter(app, mount_dir, actual_barcode="UNKNOWN")

    report = run_audit(app, "FB1186", adapter=adapter)

    assert report["status"] == STATUS_ABORTED
    assert any("实际条码不可用" in err for err in report["errors"])
    assert "scan" not in adapter.calls
    assert adapter.calls == ["mount", "unmount"]


@pytest.mark.parametrize("declared", ["UNKNOWN", "", "  ", "none"])
def test_run_audit_rejects_unknown_declared_barcode_before_touching_device(app, mount_dir, declared):
    adapter = FakeAdapter(app, mount_dir)

    report = run_audit(app, declared, adapter=adapter)

    assert report["status"] == STATUS_ABORTED
    assert adapter.calls == []  # 连 mount 都不发
    assert latest_run().status == STATUS_ABORTED


def test_run_audit_aborts_when_expected_differs_from_declared(app, mount_dir):
    adapter = FakeAdapter(app, mount_dir, actual_barcode="FB1186")

    report = run_audit(app, "FB1186", expected_barcode="FB0000", adapter=adapter)

    assert report["status"] == STATUS_ABORTED
    assert "scan" not in adapter.calls


def test_run_audit_barcode_compare_is_case_insensitive(app, mount_dir):
    adapter = FakeAdapter(app, mount_dir, actual_barcode="FB1186",
                          scan_stats=make_stats(files_seen=0, new=0))

    report = run_audit(app, " fb1186 ", expected_barcode="FB1186", adapter=adapter)

    assert report["status"] == STATUS_DONE
    assert adapter.scan_args == (str(mount_dir), "FB1186")


# ===========================================================================
# 3. 失败态
# ===========================================================================
def test_run_audit_mount_failure(app, mount_dir):
    adapter = FakeAdapter(app, mount_dir, mount_ok=False, mount_message="没有插入磁带")

    report = run_audit(app, "FB1186", adapter=adapter)

    assert report["status"] == STATUS_FAILED
    assert any("挂载失败" in err for err in report["errors"])
    assert adapter.calls == ["mount"]  # 没挂上就不卸载/不扫描
    assert latest_run().status == STATUS_FAILED


def test_run_audit_scan_exception_marks_failed(app, mount_dir):
    adapter = FakeAdapter(app, mount_dir, scan_exc=RuntimeError("磁盘读错误"))

    report = run_audit(app, "FB1186", adapter=adapter)

    assert report["status"] == STATUS_FAILED
    assert any("扫描失败" in err and "磁盘读错误" in err for err in report["errors"])
    assert adapter.calls == ["mount", "scan", "unmount"]
    assert not (mount_dir / MANIFEST_DIRNAME).exists()
    assert latest_run().status == STATUS_FAILED


def test_run_audit_scanner_value_error_is_aborted(app, mount_dir):
    adapter = FakeAdapter(app, mount_dir, scan_exc=ValueError("条码 UNKNOWN，拒绝扫描"))

    report = run_audit(app, "FB1186", adapter=adapter)

    assert report["status"] == STATUS_ABORTED
    assert any("扫描中止" in err for err in report["errors"])
    assert adapter.calls == ["mount", "scan", "unmount"]


def test_run_audit_manifest_failure_marks_failed(app, mount_dir, monkeypatch):
    (mount_dir / "a.mp4").write_bytes(b"x")
    adapter = FakeAdapter(app, mount_dir, scan_stats=make_stats(files_seen=1, new=1))

    def boom(*args, **kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(audit, "_write_manifest", boom)

    report = run_audit(app, "FB1186", adapter=adapter)

    assert report["status"] == STATUS_FAILED
    assert any("写 manifest 失败" in err for err in report["errors"])
    assert adapter.calls == ["mount", "scan", "unmount"]
    assert report["manifest_path"] is None
    run = latest_run()
    assert run.status == STATUS_FAILED
    assert "No space left" in (run.error or "")


def test_run_audit_manifest_readback_failure_marks_failed(app, mount_dir, monkeypatch):
    (mount_dir / "a.mp4").write_bytes(b"x")
    adapter = FakeAdapter(app, mount_dir, scan_stats=make_stats(files_seen=1, new=1))
    monkeypatch.setattr(audit, "read_manifest", lambda *a, **k: None)

    report = run_audit(app, "FB1186", adapter=adapter)

    assert report["status"] == STATUS_FAILED
    assert any("读回校验失败" in err for err in report["errors"])


def test_run_audit_never_ejects(app, mount_dir):
    """盘点只负责卸载；弹出必须由用户手动点击按钮。"""
    (mount_dir / "a.mp4").write_bytes(b"x")
    adapter = FakeAdapter(
        app, mount_dir, scan_stats=make_stats(files_seen=1, new=1), eject_ok=False
    )

    report = run_audit(app, "FB1186", adapter=adapter)

    assert report["status"] == STATUS_DONE
    assert report["ok"] is True
    assert adapter.calls == ["mount", "scan", "unmount"]
    assert "未弹出" in report["message"]
    assert report["manifest_path"] is not None


def test_run_audit_unmount_failure_marks_failed_without_eject(app, mount_dir):
    (mount_dir / "a.mp4").write_bytes(b"x")
    adapter = FakeAdapter(
        app, mount_dir, scan_stats=make_stats(files_seen=1, new=1), unmount_ok=False
    )

    report = run_audit(app, "FB1186", adapter=adapter)

    assert report["status"] == STATUS_FAILED
    assert any("卸载失败" in err for err in report["errors"])
    assert adapter.calls == ["mount", "scan", "unmount"]


def test_run_audit_without_manifest(app, mount_dir):
    (mount_dir / "a.mp4").write_bytes(b"x")
    adapter = FakeAdapter(app, mount_dir, scan_stats=make_stats(files_seen=1, new=1))

    report = run_audit(app, "FB1186", adapter=adapter, write_manifest=False)

    assert report["status"] == STATUS_DONE
    assert report["manifest_path"] is None
    assert not (mount_dir / MANIFEST_DIRNAME).exists()
    assert read_manifest(mount_dir) is None


# ===========================================================================
# 4. HTTP 适配器接入（假 v1 的 500 / 超时 / 端到端成功）
# ===========================================================================
def test_run_audit_http_mount_500(app, mount_dir, mini_v1):
    base_url, state = mini_v1
    state.mode = "error"
    adapter = _http_adapter(base_url, timeout=1)

    report = run_audit(app, "FB1186", adapter=adapter)

    assert report["status"] == STATUS_FAILED
    assert any("HTTP 500" in err for err in report["errors"])
    assert latest_run().status == STATUS_FAILED


def test_run_audit_http_mount_timeout(app, mount_dir, mini_v1):
    base_url, state = mini_v1
    state.mode = "timeout"
    state.delay = 0.6
    adapter = _http_adapter(base_url, timeout=0.15)

    started = time.time()
    report = run_audit(app, "FB1186", adapter=adapter)
    elapsed = time.time() - started

    assert report["status"] == STATUS_FAILED
    assert any("超时" in err for err in report["errors"])
    assert elapsed < 2.0


def test_run_audit_http_end_to_end(app, mount_dir, mini_v1):
    """HTTP mount → 扫描（假 scanner）→ manifest → HTTP unmount/eject。"""
    base_url, state = mini_v1
    (mount_dir / "sub").mkdir()
    (mount_dir / "sub" / "IPX-001.mp4").write_bytes(b"z" * 7)

    class HttpScanAdapter(HttpAdapter):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.scan_args = None

        def scan(self, mount_path, barcode, *, progress=None):
            self.scan_args = (mount_path, barcode)
            return make_stats(files_seen=1, new=1)

    adapter = HttpScanAdapter(
        base_url=base_url,
        timeout=2,
        mount_point=mount_dir,
        session=_http_adapter(base_url).session,
    )

    report = run_audit(app, "FB1186", expected_barcode="FB1186", adapter=adapter)

    assert report["status"] == STATUS_DONE
    assert adapter.scan_args == (str(mount_dir), "FB1186")
    paths = [path for _method, path in state.calls]
    # 新增的状态探测会先访问 v1 状态接口；真正的生命周期仍是 mount→unmount→eject
    lifecycle = [p for p in paths if p in (
        "/api/tape/mount", "/api/tape/unmount", "/api/tape/eject")]
    # mini v1 的 current 固定返回 mounted，所以本用例走“复用挂载”分支；盘点只卸载不弹出。
    assert report["reused_mount"] is True
    assert lifecycle == ["/api/tape/unmount"]
    manifest = read_manifest(app)
    assert manifest is not None
    assert manifest["file_count"] == 1
    assert manifest["files"][0]["rel_path"] == "sub/IPX-001.mp4"


# ===========================================================================
# 5. write_manifest / read_manifest 单元测试
# ===========================================================================
def test_write_manifest_atomic_and_roundtrip(tmp_path):
    mount = tmp_path / "tape"
    (mount / "A").mkdir(parents=True)
    (mount / "A" / "SSIS-001.mp4").write_bytes(b"x" * 10)
    (mount / "top.srt").write_bytes(b"y" * 3)
    stats = make_stats(volume_id=9, files_seen=2, new=1)
    rows = [
        {"rel_path": "A/SSIS-001.mp4", "code_guess": "SSIS-001",
         "confidence": 95, "flags": "chinese_sub"},
        {"rel_path": "top.srt", "code_guess": None, "confidence": None, "flags": None},
    ]

    path = write_manifest(mount, "FB1186", stats, {"files": rows, "adapter": "http"})

    assert path == str(mount / "FB1186.json")  # 本地目录直写，不再放 ._manifest
    assert Path(path).is_file()
    assert not any(item.name.endswith(".tmp") for item in mount.iterdir())

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["schema"] == MANIFEST_SCHEMA
    assert data["barcode"] == "FB1186"
    assert data["adapter"] == "http"  # 额外元数据进顶层
    assert data["stats"]["volume_id"] == 9
    entries = {item["rel_path"]: item for item in data["files"]}
    assert entries["A/SSIS-001.mp4"]["code"] == "SSIS-001"
    assert entries["A/SSIS-001.mp4"]["flags"] == "chinese_sub"
    assert entries["top.srt"]["code"] is None
    assert data["file_count"] == 2
    # 文件按 rel_path 排序，便于 diff
    assert [item["rel_path"] for item in data["files"]] == ["A/SSIS-001.mp4", "top.srt"]

    # read_manifest 精确条码读取 = 归一化结构
    manifest = read_manifest(mount, "FB1186")
    assert manifest is not None
    assert manifest["barcode"] == "FB1186"
    assert manifest["file_count"] == 2


def test_write_manifest_one_ten_thousand_files(tmp_path, monkeypatch):
    """1 万文件量级：一次 JSON 序列化 + rename，不爆内存、不留临时文件。"""
    mount = tmp_path / "tape"
    mount.mkdir()

    def fake_walk(_mount):
        return [
            {
                "rel_path": f"dir-{i // 1000:02d}/FILE-{i:05d}.mp4",
                "size": i,
                "mtime": "2026-10-26T00:00:00",
                "code": None,
                "confidence": None,
                "flags": None,
            }
            for i in range(10_000)
        ]

    monkeypatch.setattr(audit, "_walk_entries", fake_walk)

    path = write_manifest(mount, "BIG001", make_stats(files_seen=10_000, new=10_000))

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["file_count"] == 10_000
    assert len(data["files"]) == 10_000
    assert path == str(mount / "BIG001.json")
    leftovers = list(mount.iterdir())
    assert [item.name for item in leftovers] == ["BIG001.json"]


def test_read_manifest_missing_returns_none(tmp_path):
    assert read_manifest(tmp_path / "no-such-mount") is None
    empty = tmp_path / "tape-empty"
    empty.mkdir()
    assert read_manifest(empty) is None


def test_read_manifest_corrupted_returns_none(tmp_path):
    mount = tmp_path / "tape"
    manifest_dir = mount / MANIFEST_DIRNAME
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "FB1186.json").write_text("{not valid json", encoding="utf-8")

    assert read_manifest(mount) is None
    assert read_manifest(mount, "FB1186") is None


def test_read_manifest_old_format_is_normalized(tmp_path):
    """旧格式：没有 schema、文件条目用 path/code_guess 键名，也要能读。"""
    mount = tmp_path / "tape"
    manifest_dir = mount / MANIFEST_DIRNAME
    manifest_dir.mkdir(parents=True)
    old = {
        "barcode": "OLD01",
        "files": [
            {"path": "A/OLD-001.mp4", "size": 5, "code_guess": "OLD-001", "confidence": 88}
        ],
    }
    (manifest_dir / "OLD01.json").write_text(json.dumps(old), encoding="utf-8")

    manifest = read_manifest(mount, "OLD01")

    assert manifest is not None
    assert manifest["schema"] == 0  # 旧格式没有 schema
    assert manifest["barcode"] == "OLD01"
    assert manifest["file_count"] == 1
    entry = manifest["files"][0]
    assert entry["rel_path"] == "A/OLD-001.mp4"
    assert entry["code"] == "OLD-001"
    assert entry["confidence"] == 88


def test_read_manifest_local_dir_and_legacy_layout(tmp_path):
    local = tmp_path / "manifests"
    local.mkdir()
    (local / "AAA.json").write_text(
        json.dumps({"schema": 1, "barcode": "AAA", "files": []}), encoding="utf-8"
    )
    (local / "BBB.json").write_text(
        json.dumps({"schema": 1, "barcode": "BBB", "files": []}), encoding="utf-8"
    )
    assert read_manifest(local, "AAA")["barcode"] == "AAA"
    assert read_manifest(local, "BBB")["barcode"] == "BBB"
    # 旧磁带布局仍要能读（兼容存量带）
    legacy = tmp_path / "old-tape"
    (legacy / MANIFEST_DIRNAME).mkdir(parents=True)
    (legacy / MANIFEST_DIRNAME / "OLD01.json").write_text(
        json.dumps({"schema": 1, "barcode": "OLD01", "files": []}), encoding="utf-8"
    )
    assert read_manifest(legacy, "OLD01")["barcode"] == "OLD01"


def test_write_manifest_creates_local_dir(tmp_path):
    target = tmp_path / "fresh-manifests"
    path = write_manifest(target, "FB1186", make_stats())
    assert Path(path).is_file()
    assert Path(path).parent == target


def test_manifest_skips_manifest_dir_and_junk_paths(tmp_path):
    mount = tmp_path / "tape"
    (mount / MANIFEST_DIRNAME).mkdir(parents=True)
    (mount / MANIFEST_DIRNAME / "old.json").write_text("{}", encoding="utf-8")
    (mount / "@eaDir").mkdir()
    (mount / "@eaDir" / "junk.mp4").write_bytes(b"junk")
    (mount / ".hidden-dir").mkdir()
    (mount / ".hidden-dir" / "hidden.mp4").write_bytes(b"hidden")
    (mount / ".DS_Store").write_bytes(b"junk")
    (mount / "real.mp4").write_bytes(b"real")

    path = write_manifest(mount, "FB1186", make_stats())

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    # 与 scanner/v1 的扫描范围一致：点开头路径 + SKIP_DIRS 都不进 manifest
    assert [item["rel_path"] for item in data["files"]] == ["real.mp4"]
    assert data["file_count"] == 1


def test_run_audit_eject_disabled_stops_after_unmount(app, mount_dir):
    """机柜门会挡住磁带时：盘点只卸载、不弹出，仍算成功。"""
    volume = seed_volume(
        app,
        "FB1186",
        [make_file("SSIS-001/SSIS-001.mp4")],
    )
    (mount_dir / "SSIS-001").mkdir()
    (mount_dir / "SSIS-001" / "SSIS-001.mp4").write_bytes(b"x" * 10)

    adapter = FakeAdapter(
        app,
        mount_dir,
        scan_stats=make_stats(volume_id=volume.id, files_seen=1, new=1),
    )
    report = run_audit(
        app, "FB1186", expected_barcode="FB1186", adapter=adapter, eject=False
    )

    assert report["ok"] is True
    assert report["status"] == STATUS_DONE
    assert adapter.calls == ["mount", "scan", "unmount"]  # 没有 eject
    assert "未弹出" in report["message"]


def test_run_audit_reuses_already_mounted_tape(app, mount_dir):
    """用户先挂载再点盘点：复用挂载，不再第二次 mount。"""
    volume = seed_volume(app, "FB1186", [make_file("SSIS-001/SSIS-001.mp4")])
    (mount_dir / "SSIS-001").mkdir()
    (mount_dir / "SSIS-001" / "SSIS-001.mp4").write_bytes(b"x")

    class MountedAdapter(FakeAdapter):
        def status(self):
            return SimpleNamespace(mounted=True, barcode="FB1186")

        def mount(self):
            raise AssertionError("already mounted tape must not call mount again")

    adapter = MountedAdapter(
        app, mount_dir,
        scan_stats=make_stats(volume_id=volume.id, files_seen=1, new=1),
    )
    report = run_audit(
        app, "FB1186", expected_barcode="FB1186", adapter=adapter,
        write_manifest=False,
    )
    assert report["ok"] is True
    assert report["reused_mount"] is True
    assert "mount" not in adapter.calls
    assert adapter.calls == ["scan", "unmount"]


def test_run_audit_reuses_mount_when_volume_label_missing(app, mount_dir):
    """已挂载但 .volume_label 读不到时，用库中已校验条码兜底。"""
    volume = seed_volume(app, "FB1186", [make_file("SSIS-001/SSIS-001.mp4")])
    (mount_dir / "SSIS-001").mkdir()
    (mount_dir / "SSIS-001" / "SSIS-001.mp4").write_bytes(b"x")

    class NoLabelMountedAdapter(FakeAdapter):
        def status(self):
            return SimpleNamespace(mounted=True, barcode=None)

        def mount(self):
            raise AssertionError("already mounted tape must not call mount again")

    adapter = NoLabelMountedAdapter(
        app, mount_dir,
        scan_stats=make_stats(volume_id=volume.id, files_seen=1, new=1),
    )
    report = run_audit(app, "FB1186", expected_barcode="FB1186", adapter=adapter,
                       write_manifest=False, eject=False)
    assert report["ok"] is True
    assert report["actual_barcode"] == "FB1186"
    assert report["reused_mount"] is True
    assert adapter.calls == ["scan", "unmount"]
