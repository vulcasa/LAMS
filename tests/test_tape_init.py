"""磁带初始化/ltfsck 恢复的高层逻辑测试（不碰真实设备）。"""
from __future__ import annotations

from pathlib import Path

from app import create_app
from app.db import db
from app.models import LibraryVolume, TapeOperation
from app.services import tape_ops
from app.services.tape_adapter import TapeAdapter, TapeStatus


class FakeAdapter(TapeAdapter):
    def __init__(self, *, mounted=False, mount_ok=True, mount_barcode="FB1186",
                 mount_error="", init_ok=True, recover_ok=True):
        super().__init__(mount_point="/tmp/fake-tape")
        self.mounted = mounted
        self.mount_ok = mount_ok
        self.mount_barcode = mount_barcode
        self.mount_error = mount_error
        self.init_ok = init_ok
        self.recover_ok = recover_ok
        self.init_calls = []
        self.recover_calls = []

    def status(self):
        return TapeStatus(mounted=self.mounted, barcode=None,
                          device_exists=True, device_path="/dev/sg10")

    def mount(self):
        self._record_output(command="ltfs", stdout="",
                            stderr=self.mount_error)
        return self.mount_ok, ("挂载成功" if self.mount_ok else self.mount_error), (
            self.mount_barcode if self.mount_ok else None
        )

    def unmount(self):
        return True, "卸载成功"

    def eject(self):
        return True, "弹出成功"

    def initialize(self, barcode, *, force=False):
        self.init_calls.append((barcode, force))
        return self.init_ok, ("初始化成功" if self.init_ok else "初始化失败"), barcode

    def recover_list(self):
        return True, "找到 1 个回滚点", [{"generation": "12", "raw": "12 2026-01-01"}]

    def recover_index(self, generation=None, *, deep=False):
        self.recover_calls.append((generation, deep))
        return self.recover_ok, ("恢复成功" if self.recover_ok else "恢复失败")


def _app(tmp_path):
    return create_app({
        "TESTING": True, "WORKER_ENABLED": False, "CSRF_ENABLED": False,
        "DATA_DIR": tmp_path / "data", "DB_PATH": tmp_path / "data" / "test.db",
    })


def _uninit_context(app, adapter=None, barcode="FB1186"):
    fake = adapter or FakeAdapter(
        mount_ok=False,
        mount_error=f"LTFS11176E not an LTFS volume\nBarcode = {barcode}",
    )
    with app.app_context():
        result = tape_ops.mount_tape(app, adapter=fake)
    assert result["needs_init"] is True
    return result["operation_id"]


def test_classify_mount_failure():
    assert tape_ops.classify_mount_failure(
        {"stderr": "LTFS11176E The ANSI label indicates this is not an LTFS volume."}
    ) == "uninitialized"
    assert tape_ops.classify_mount_failure(
        {"stderr": "Cannot read ANSI label: read failed"}
    ) == "uninitialized"
    assert tape_ops.classify_mount_failure(
        {"stderr": "LTFS17168E Cannot read volume: medium is not partitioned."}
    ) == "uninitialized"
    assert tape_ops.classify_mount_failure(
        {"stderr": "write protected"}, "挂载失败"
    ) == "unknown"


def test_initialize_and_register_volume(tmp_path):
    app = _app(tmp_path)
    context_id = _uninit_context(app, barcode="FB1186")
    fake = FakeAdapter(mount_barcode="FB1186")
    with app.app_context():
        result = tape_ops.initialize_tape(
            app, "fb1186", confirmed=True, force=True,
            mount_operation_id=context_id, adapter=fake
        )
        assert result["ok"] is True
        assert result["barcode"] == "FB1186"
        assert fake.init_calls == [("FB1186", True)]
        assert LibraryVolume.query.filter_by(kind="tape", ref="FB1186").count() == 1
        op = TapeOperation.query.filter_by(op_type="init").one()
        assert op.status == "success"


def test_initialize_rejects_registered_barcode_even_force(tmp_path):
    app = _app(tmp_path)
    context_id = _uninit_context(app, barcode="FB1186")
    fake = FakeAdapter()
    with app.app_context():
        db.session.add(LibraryVolume(kind="tape", ref="FB1186", name="旧带"))
        db.session.commit()
        result = tape_ops.initialize_tape(
            app, "FB1186", confirmed=True, force=True,
            mount_operation_id=context_id, adapter=fake
        )
        assert result["ok"] is False
        assert result["code"] == "already_registered"
        assert fake.init_calls == []


def test_initialize_rejects_barcode_mismatch(tmp_path):
    app = _app(tmp_path)
    context_id = _uninit_context(app, barcode="FB1186")
    fake = FakeAdapter(mount_barcode="OTHER1")
    with app.app_context():
        result = tape_ops.initialize_tape(
            app, "FB1186", confirmed=True,
            mount_operation_id=context_id, adapter=fake
        )
        assert result["ok"] is False
        assert result["code"] == "barcode_mismatch"
        assert LibraryVolume.query.filter_by(kind="tape", ref="OTHER1").count() == 0


def test_mount_failure_returns_init_hint_and_registered_flag(tmp_path):
    app = _app(tmp_path)
    fake = FakeAdapter(
        mount_ok=False,
        mount_error="LTFS11176E The ANSI label indicates this is not an LTFS volume.\nBarcode = FB1186",
    )
    with app.app_context():
        result = tape_ops.mount_tape(app, adapter=fake)
        assert result["ok"] is False
        assert result["needs_init"] is True
        assert result["detected_barcode"] == "FB1186"
        assert result["barcode_registered"] is False

        db.session.add(LibraryVolume(kind="tape", ref="FB1186"))
        db.session.commit()
        result = tape_ops.mount_tape(app, adapter=fake)
        assert result["barcode_registered"] is True


def test_recover_list_and_index(tmp_path):
    app = _app(tmp_path)
    fake = FakeAdapter()
    with app.app_context():
        listed = tape_ops.recover_list_tape(app, adapter=fake)
        assert listed["ok"] is True
        assert listed["points"][0]["generation"] == "12"
        result = tape_ops.recover_tape_index(
            app, generation="12", confirmed=True, adapter=fake
        )
        assert result["ok"] is True
        assert result["barcode"] == "FB1186"
        assert fake.recover_calls == [("12", False)]
        assert TapeOperation.query.filter_by(op_type="recover").one().status == "success"


def test_initialize_rejects_input_mismatch_with_detected_barcode(tmp_path):
    app = _app(tmp_path)
    fake = FakeAdapter(mount_ok=False, mount_barcode=None,
                       mount_error="LTFS11176E not an LTFS volume\\nBarcode = READ01")
    with app.app_context():
        first = tape_ops.mount_tape(app, adapter=fake)
        assert first["needs_init"] is True
        result = tape_ops.initialize_tape(
            app, "OTHER1", confirmed=True,
            mount_operation_id=first["operation_id"], adapter=FakeAdapter()
        )
        assert result["ok"] is False
        assert result["code"] == "barcode_mismatch"


def test_initialize_requires_recent_mount_context(tmp_path):
    app = _app(tmp_path)
    fake = FakeAdapter()
    with app.app_context():
        result = tape_ops.initialize_tape(app, "FB1186", confirmed=True, adapter=fake)
        assert result["ok"] is False
        assert result["code"] == "missing_mount_context"
        assert fake.init_calls == []


def test_recover_requires_generation(tmp_path):
    app = _app(tmp_path)
    fake = FakeAdapter()
    with app.app_context():
        result = tape_ops.recover_tape_index(app, confirmed=True, adapter=fake)
        assert result["ok"] is False
        assert result["code"] == "missing_generation"
        assert fake.recover_calls == []
