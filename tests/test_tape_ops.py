"""磁带高层操作（tape_ops）与适配器设备信息测试。

覆盖（对应 task-16）：

- ``tape_adapter`` 新增接口：``device_info`` / ``device_list`` / ``volume_label``
  （DirectAdapter 解析伪造的 ltfs 输出；HttpAdapter 对 v1 JSON 的映射）；
- ``tape_ops.mount_tape``：成功登记卷 + status=mounted + last_mounted_at + 操作日志；
  条码不符 / 实际条码 UNKNOWN → **自动 unmount+eject** 并记 failed、不登记卷；
  mount 失败 / 参数非法 / 适配器异常均返回 ``{ok: False}`` 不抛；
- ``unmount_tape`` / ``eject_tape``：状态流转（unmounted/ejected）、失败写 last_error、
  eject 前自动卸载；
- ``current_status`` / ``device_snapshot`` / ``list_tape_volumes`` / ``list_operations``；
- 操作日志：command/stdout/stderr 记录、超 8KB 截断、duration_ms；
- 单驱动器语义：挂新带时清掉库里其它"残留 mounted"卷。

**全程不碰真实设备**：用临时目录里生成的假 ``ltfs`` / ``umount`` 可执行脚本
（通过 ``DirectAdapter(ltfs_bin=..., umount_bin=...)`` 注入），脚本从控制目录读
输出/返回码并记录调用参数；``os.path.ismount`` 用 monkeypatch 模拟挂载成功。

运行（项目根 = av-archive/）::

    .venv/bin/python -m pytest tests/test_tape_ops.py -q
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import create_app
from app.db import db
from app.models import LibraryFile, LibraryVolume, TapeOperation, utcnow
from app.services import tape_adapter
from app.services.tape_adapter import (
    DirectAdapter,
    HttpAdapter,
    parse_device_info_text,
    parse_device_list_text,
)
from app.services.tape_ops import (
    MAX_OUTPUT_BYTES,
    current_status,
    device_snapshot,
    eject_tape,
    list_operations,
    list_tape_volumes,
    mount_tape,
    unmount_tape,
)

# 假设备脚本：从 $FAKE_TAPE_DIR 读 <scenario>.{out,err,rc}，并把调用记录到 calls.log。
_FAKE_SCRIPT = """#!/bin/sh
FAKE_DIR="${FAKE_TAPE_DIR:?FAKE_TAPE_DIR not set}"
name="$(basename "$0")"
printf '%s %s\\n' "$name" "$*" >> "$FAKE_DIR/calls.log"
run() {
  key="$1"
  if [ -f "$FAKE_DIR/$key.out" ]; then cat "$FAKE_DIR/$key.out"; fi
  if [ -f "$FAKE_DIR/$key.err" ]; then cat "$FAKE_DIR/$key.err" >&2; fi
  rc=0
  if [ -f "$FAKE_DIR/$key.rc" ]; then rc="$(cat "$FAKE_DIR/$key.rc")"; fi
  exit "$rc"
}
case "$*" in
  *device_list*) run device_list ;;
  *device_info*) run device_info ;;
  *release_device*) run eject ;;
  *)
    if [ "$name" = "umount" ]; then run umount; else run mount; fi
    ;;
esac
"""

DEVICE_INFO_OUT = """\
Device: /dev/sg10
  Vendor: HP
  Model: Ultrium 6-HH
  Serial Number: HU1234
  Firmware Version: 3.1.0
"""

DEVICE_LIST_OUT = """\
ltfs: device list:
10: /dev/sg10
11: /dev/sg11
"""


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture()
def app(tmp_path):
    data_dir = tmp_path / "data"
    device = tmp_path / "sg10"
    device.write_text("", encoding="utf-8")
    mount_point = tmp_path / "tape-mount"
    mount_point.mkdir()
    application = create_app(
        {
            "TESTING": True,
            "WORKER_ENABLED": False,
            "TAPE_ADAPTER": "direct",
            "LTFS_DEVICE": str(device),
            "LTFS_MOUNT": str(mount_point),
            "DATA_DIR": data_dir,
            "DB_PATH": data_dir / "avlib.db",
            "MEDIA_DIR": data_dir / "media",
            "COVER_DIR": data_dir / "media" / "covers",
            "STILL_DIR": data_dir / "media" / "stills",
            "AVATAR_DIR": data_dir / "media" / "avatars",
            "SUBTITLE_DIR": data_dir / "media" / "subtitles",
            "THUMB_DIR": data_dir / "media" / "thumbs",
            "DUMP_DIR": data_dir / "dump",
        }
    )
    with application.app_context():
        yield application
        db.session.remove()
        db.engine.dispose()


class FakeTapeTools:
    """临时假 ltfs/umount 脚本 + 控制目录 + 调用记录。"""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.ctl = tmp_path / "fake-tape"
        self.ctl.mkdir()
        (self.ctl / "calls.log").write_text("", encoding="utf-8")
        monkeypatch.setenv("FAKE_TAPE_DIR", str(self.ctl))
        self.ltfs = self._install("ltfs")
        self.umount = self._install("umount")
        self.mount_point = tmp_path / "tape-mount"
        self.mount_point.mkdir(exist_ok=True)
        self.device = tmp_path / "sg10"
        self.device.write_text("", encoding="utf-8")
        # 模拟挂载成功的挂载点（DirectAdapter.mount 会复查 ismount）
        self.mounted_path: str | None = None
        monkeypatch.setattr(tape_adapter.os.path, "ismount", self._fake_ismount)

    def _fake_ismount(self, path) -> bool:
        return self.mounted_path is not None and os.path.realpath(str(path)) == self.mounted_path

    def _install(self, name: str) -> Path:
        path = self.ctl / name
        path.write_text(_FAKE_SCRIPT, encoding="utf-8")
        path.chmod(0o755)
        return path

    # --- 场景配置 -------------------------------------------------------
    def configure(self, key: str, *, out: str | None = None, err: str | None = None,
                  rc: int | None = None) -> None:
        for suffix, value in (("out", out), ("err", err), ("rc", rc)):
            path = self.ctl / f"{key}.{suffix}"
            if value is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(str(value), encoding="utf-8")

    def mount_ok(self, barcode: str = "FB1186") -> None:
        self.configure(
            "mount",
            out=f"1c LTFS17227I Tape attribute: Barcode = {barcode}\nmounted\n",
        )
        self.mounted_path = os.path.realpath(str(self.mount_point))

    def mount_fail(self, err: str = "ltfs: 设备忙", rc: int = 1) -> None:
        self.configure("mount", out="", err=err, rc=rc)

    def umount_ok(self) -> None:
        self.configure("umount", out="", err="", rc=0)

    def umount_fail(self, err: str = "umount: target is busy", rc: int = 1) -> None:
        self.configure("umount", out="", err=err, rc=rc)

    def eject_ok(self) -> None:
        self.configure("eject", out="released\n", err="", rc=0)

    def eject_fail(self, err: str = "ltfs: device busy", rc: int = 1) -> None:
        self.configure("eject", out="", err=err, rc=rc)

    def device_info_ok(self) -> None:
        self.configure("device_info", out=DEVICE_INFO_OUT)

    def device_info_fail(self, err: str = "ltfs: cannot open device", rc: int = 1) -> None:
        self.configure("device_info", out="", err=err, rc=rc)

    def device_list_ok(self) -> None:
        self.configure("device_list", out=DEVICE_LIST_OUT)

    def device_list_fail(self, err: str = "ltfs: device_list failed", rc: int = 1) -> None:
        self.configure("device_list", out="", err=err, rc=rc)

    # --- 查询 -----------------------------------------------------------
    def calls(self) -> list[str]:
        return [line for line in (self.ctl / "calls.log").read_text(encoding="utf-8").splitlines() if line]

    def reset_calls(self) -> None:
        (self.ctl / "calls.log").write_text("", encoding="utf-8")


@pytest.fixture()
def tools(tmp_path, monkeypatch):
    return FakeTapeTools(tmp_path, monkeypatch)


def make_adapter(app, tools: FakeTapeTools, *, device: str | None = None) -> DirectAdapter:
    return DirectAdapter(
        device=device or str(tools.device),
        mount_point=str(tools.mount_point),
        timeout=10,
        ltfs_bin=str(tools.ltfs),
        umount_bin=str(tools.umount),
    )


def ops(app) -> list[TapeOperation]:
    return TapeOperation.query.order_by(TapeOperation.id).all()


def latest_op() -> TapeOperation | None:
    return TapeOperation.query.order_by(TapeOperation.id.desc()).first()


def volume(barcode: str) -> LibraryVolume | None:
    return LibraryVolume.query.filter_by(kind="tape", ref=barcode).first()


def mounted_volumes() -> list[LibraryVolume]:
    return LibraryVolume.query.filter_by(kind="tape", status="mounted").all()


def make_volume(barcode: str, *, status: str = "unknown", files: int = 0) -> LibraryVolume:
    vol = LibraryVolume(kind="tape", ref=barcode, name=f"磁带 {barcode}", status=status)
    db.session.add(vol)
    db.session.commit()
    for index in range(files):
        db.session.add(
            LibraryFile(volume_id=vol.id, rel_path=f"{barcode}-{index}.mp4",
                        name=f"{barcode}-{index}.mp4")
        )
    db.session.commit()
    return vol


# ===========================================================================
# 1. 适配器：device_info / device_list / volume_label
# ===========================================================================
def test_direct_device_info_parses_model_serial_firmware(app, tools):
    tools.device_info_ok()
    adapter = make_adapter(app, tools)

    info = adapter.device_info()

    assert info["model"] == "Ultrium 6-HH"
    assert info["serial_number"] == "HU1234"
    assert info["firmware_version"] == "3.1.0"
    assert info["error"] is None
    assert "Ultrium 6-HH" in info["raw"]
    # 操作输出被记录，供 tape_ops 写日志
    assert "device_info" in (adapter.last_output["command"] or "")
    assert tools.calls() == [f"ltfs -o devname={tools.device} -o device_info"]


def test_direct_device_info_failure_returns_error_dict(app, tools):
    tools.device_info_fail()
    adapter = make_adapter(app, tools)

    info = adapter.device_info()

    assert info["model"] is None and info["serial_number"] is None
    assert info["error"] and "cannot open device" in info["error"]


def test_direct_device_list_parses_sg_paths(app, tools):
    tools.device_list_ok()
    adapter = make_adapter(app, tools)

    devices = adapter.device_list()

    assert [item["path"] for item in devices] == ["/dev/sg10", "/dev/sg11"]
    assert devices[0]["raw"].endswith("/dev/sg10")
    assert tools.calls() == ["ltfs -o device_list"]


def test_direct_device_list_failure_and_empty(app, tools):
    adapter = make_adapter(app, tools)
    tools.device_list_fail()
    assert adapter.device_list() == []

    tools.configure("device_list", out="ltfs: no devices found\n", rc=0)
    assert adapter.device_list() == []


def test_direct_volume_label(app, tools):
    adapter = make_adapter(app, tools)
    assert adapter.volume_label() is None

    (tools.mount_point / ".volume_label").write_text("FB1186\n", encoding="utf-8")
    assert adapter.volume_label() == "FB1186"


def test_http_device_info_and_list_mapping(app):
    adapter = HttpAdapter(base_url="http://v1:5001", timeout=1)
    calls: list[tuple[str, str]] = []
    payloads = {
        "/api/tape/device-info": {
            "success": True,
            "info": {"model": "HP LTO-6", "serial_number": "HU999",
                     "firmware_version": "1.2.3"},
        },
        "/api/tape/device-list": {
            "success": True,
            "devices": [
                {"path": "/dev/sg10", "model": "Unknown",
                 "serial_number": "UNKNOWN", "firmware_version": "1.00"},
                {"path": "/dev/sg11"},
                {"model": "no path"},
            ],
        },
    }

    def fake_call(method, path, *, action, payload=None, timeout=None):
        calls.append((method, path))
        return payloads[path], None

    adapter._call = fake_call  # type: ignore[method-assign]

    info = adapter.device_info()
    devices = adapter.device_list()

    assert info["model"] == "HP LTO-6"
    assert info["serial_number"] == "HU999"
    assert info["firmware_version"] == "1.2.3"
    assert info["error"] is None
    assert [item["path"] for item in devices] == ["/dev/sg10", "/dev/sg11"]
    # v1 的 "Unknown"/"UNKNOWN" 占位值统一成 None
    assert devices[0]["model"] is None
    assert devices[0]["serial_number"] is None
    assert calls == [
        ("GET", "/api/tape/device-info"),
        ("GET", "/api/tape/device-list"),
    ]
    assert "device-list" in (adapter.last_output["command"] or "")


def test_http_device_info_and_list_error(app):
    adapter = HttpAdapter(base_url="http://v1:5001", timeout=1)
    adapter._call = lambda *a, **k: (None, "HTTP 500：炸了")  # type: ignore[method-assign]

    info = adapter.device_info()
    devices = adapter.device_list()

    assert info["model"] is None
    assert "HTTP 500" in info["error"]
    assert devices == []


def test_parse_device_helpers_tolerate_messy_output():
    info = parse_device_info_text(
        "  model =  Ultrium 7  \nserial:  ABC\nfirmware version:9.9\nVendor: HP\n"
    )
    assert info == {
        "model": "Ultrium 7",
        "serial_number": "ABC",
        "firmware_version": "9.9",
    }

    devices = parse_device_list_text(
        "ltfs: heading /dev/sg99\n  10: /dev/sg10\n10 again: /dev/sg10\n11: /dev/sg11\n"
    )
    # 标题行跳过、同一路径去重
    assert [item["path"] for item in devices] == ["/dev/sg10", "/dev/sg11"]


# ===========================================================================
# 2. mount_tape
# ===========================================================================
def test_mount_tape_success_registers_volume_and_operation(app, tools):
    tools.mount_ok("FB1186")
    adapter = make_adapter(app, tools)

    result = mount_tape(app, expected_barcode="FB1186", adapter=adapter)

    assert result["ok"] is True
    assert result["barcode"] == "FB1186"
    assert result["status"] == "mounted"
    assert result["created_volume"] is True

    vol = volume("FB1186")
    assert vol is not None
    assert vol.status == "mounted"
    assert vol.device_path == str(tools.device)
    assert vol.last_mounted_at is not None
    assert vol.last_error is None
    assert result["volume_id"] == vol.id

    operation = latest_op()
    assert operation.op_type == "mount"
    assert operation.status == "success"
    assert operation.barcode == "FB1186"
    assert operation.volume_id == vol.id
    assert operation.finished_at is not None
    assert operation.duration_ms is not None and operation.duration_ms >= 0
    assert "devname=" in (operation.command or "")
    assert "FB1186" in (operation.stdout or "")


def test_mount_tape_without_expected_uses_actual_barcode(app, tools):
    tools.mount_ok("FB2186")
    adapter = make_adapter(app, tools)

    result = mount_tape(app, adapter=adapter)

    assert result["ok"] is True
    assert result["barcode"] == "FB2186"
    assert volume("FB2186").status == "mounted"


def test_mount_tape_barcode_mismatch_unmounts_without_eject(app, tools):
    tools.mount_ok("FB1186")
    tools.umount_ok()
    make_volume("FB9999", status="mounted")
    adapter = make_adapter(app, tools)

    result = mount_tape(app, expected_barcode="FB9999", adapter=adapter)

    assert result["ok"] is False
    assert "条码不符" in result["message"]
    assert "FB9999" in result["message"] and "FB1186" in result["message"]
    # 只卸载，绝不自动弹出；实际盘没有登记成卷
    calls = tools.calls()
    assert calls[0].startswith("ltfs ")
    assert calls[1].startswith("umount ")
    assert not any("release_device" in line for line in calls)
    assert volume("FB9999").status == "unknown"
    assert "条码不符" in (volume("FB9999").last_error or "")
    assert mounted_volumes() == []
    operation = latest_op()
    assert operation.op_type == "mount" and operation.status == "failed"
    assert "条码不符" in (operation.message or "")


def test_mount_tape_unknown_barcode_keeps_mounted_for_manual_barcode(app, tools):
    tools.configure("mount", out="mounted but no barcode\n")  # 解析不出条码 → UNKNOWN
    tools.mounted_path = os.path.realpath(str(tools.mount_point))
    adapter = make_adapter(app, tools)

    result = mount_tape(app, adapter=adapter)

    assert result["ok"] is False
    assert result["needs_manual_barcode"] is True
    assert "实际条码不可用" in result["message"]
    # 读不到条码时保持挂载，不再自动卸载/弹出
    assert [line.split()[0] for line in tools.calls()] == ["ltfs"]
    assert not any("release_device" in line for line in tools.calls())
    assert LibraryVolume.query.filter_by(kind="tape").count() == 0
    assert latest_op().status == "failed"

    manual = mount_tape(app, manual_barcode="FB1234", manual_confirm=True, adapter=adapter)
    assert manual["ok"] is True
    assert manual["barcode"] == "FB1234"
    assert manual["manual_barcode"] is True
    assert volume("FB1234").status == "mounted"
    assert [line.split()[0] for line in tools.calls()] == ["ltfs"]  # 没有再次 mount


def test_mount_tape_device_failure_keeps_db_unchanged(app, tools):
    tools.mount_fail("ltfs: no tape loaded", rc=1)
    make_volume("FB1186", status="ejected")
    adapter = make_adapter(app, tools)

    result = mount_tape(app, expected_barcode="FB1186", adapter=adapter)

    assert result["ok"] is False
    assert "no tape loaded" in result["message"]
    assert volume("FB1186").status == "ejected"  # 没挂上，原状态不动
    assert "no tape loaded" in (volume("FB1186").last_error or "")
    assert len(tools.calls()) == 1  # 只有 mount 一次，没有 unload/eject cleanup
    operation = latest_op()
    assert operation.op_type == "mount" and operation.status == "failed"
    assert "no tape loaded" in (operation.stderr or "ltfs: no tape loaded")


def test_mount_tape_rejects_unusable_expected_before_touching_device(app, tools):
    adapter = make_adapter(app, tools)

    result = mount_tape(app, expected_barcode="UNKNOWN", adapter=adapter)

    assert result["ok"] is False
    assert "预期条码不可用" in result["message"]
    assert tools.calls() == []
    assert TapeOperation.query.count() == 0


def test_mount_tape_clears_stale_mounted_volumes(app, tools):
    stale = make_volume("OLD001", status="mounted")
    tools.mount_ok("FB1186")
    adapter = make_adapter(app, tools)

    result = mount_tape(app, adapter=adapter)

    assert result["ok"] is True
    assert volume("OLD001").status == "unknown"
    assert volume("FB1186").status == "mounted"
    assert [item.ref for item in mounted_volumes()] == ["FB1186"]


def test_mount_tape_reuses_volume_row(app, tools):
    tools.mount_ok("FB1186")
    adapter = make_adapter(app, tools)

    first = mount_tape(app, adapter=adapter)
    second = mount_tape(app, adapter=adapter)

    assert first["ok"] and second["ok"]
    assert LibraryVolume.query.filter_by(kind="tape", ref="FB1186").count() == 1
    assert first["volume_id"] == second["volume_id"]
    assert TapeOperation.query.filter_by(op_type="mount").count() == 2


def test_mount_tape_truncates_huge_output(app, tools):
    payload = "Tape attribute: Barcode = FB1186\n" + ("X" * 20000)
    tools.configure("mount", out=payload)
    tools.mounted_path = os.path.realpath(str(tools.mount_point))
    adapter = make_adapter(app, tools)

    result = mount_tape(app, adapter=adapter)

    assert result["ok"] is True
    operation = latest_op()
    assert operation.stdout is not None
    assert "已截断" in operation.stdout
    assert len(operation.stdout.encode("utf-8")) <= MAX_OUTPUT_BYTES + 200
    assert len(operation.stdout) < len(payload)


# ===========================================================================
# 3. unmount_tape
# ===========================================================================
def test_unmount_tape_success_updates_volume_and_log(app, tools):
    tools.umount_ok()
    vol = make_volume("FB1186", status="mounted")
    adapter = make_adapter(app, tools)

    result = unmount_tape(app, adapter=adapter)

    assert result["ok"] is True
    assert result["status"] == "unmounted"
    assert result["barcode"] == "FB1186"
    assert volume("FB1186").status == "unmounted"
    assert volume("FB1186").last_error is None

    operation = latest_op()
    assert operation.op_type == "unmount" and operation.status == "success"
    assert str(tools.umount) in (operation.command or "")
    assert operation.volume_id == vol.id


def test_unmount_tape_failure_records_last_error(app, tools):
    tools.umount_fail("umount: target is busy")
    vol = make_volume("FB1186", status="mounted")
    adapter = make_adapter(app, tools)

    result = unmount_tape(app, adapter=adapter)

    assert result["ok"] is False
    assert "target is busy" in result["message"]
    assert volume("FB1186").status == "mounted"
    assert "target is busy" in (volume("FB1186").last_error or "")
    assert latest_op().status == "failed"


def test_unmount_tape_without_known_volume_still_works(app, tools):
    tools.umount_ok()
    adapter = make_adapter(app, tools)

    result = unmount_tape(app, adapter=adapter)

    assert result["ok"] is True
    assert result["barcode"] is None
    assert latest_op().status == "success"


# ===========================================================================
# 4. eject_tape
# ===========================================================================
def test_eject_tape_auto_unmounts_when_mounted(app, tools):
    tools.device_info_ok()
    tools.configure("mount", out="Tape attribute: Barcode = FB1186\n")
    tools.mounted_path = os.path.realpath(str(tools.mount_point))
    tools.umount_ok()
    tools.eject_ok()
    vol = make_volume("FB1186", status="mounted")
    adapter = make_adapter(app, tools)

    result = eject_tape(app, adapter=adapter)

    assert result["ok"] is True
    assert result["status"] == "ejected"
    assert "自动卸载" in result["message"]
    calls = tools.calls()
    assert calls[0].startswith("umount ")
    assert "release_device" in calls[-1]
    assert volume("FB1186").status == "ejected"
    operation = latest_op()
    assert operation.op_type == "eject" and operation.status == "success"
    assert "release_device" in (operation.command or "")


def test_eject_tape_when_not_mounted(app, tools):
    tools.eject_ok()
    make_volume("FB1186", status="mounted")  # 库里可能是脏的 mounted，但驱动器没挂
    adapter = make_adapter(app, tools)

    result = eject_tape(app, adapter=adapter)

    assert result["ok"] is True
    assert result["status"] == "ejected"
    assert all("umount" not in call for call in tools.calls())
    assert volume("FB1186").status == "ejected"


def test_eject_tape_failure_records_error(app, tools):
    tools.eject_fail("ltfs: device busy")
    vol = make_volume("FB1186", status="mounted")
    adapter = make_adapter(app, tools)

    result = eject_tape(app, adapter=adapter)

    assert result["ok"] is False
    assert "device busy" in result["message"]
    assert volume("FB1186").status == "mounted"
    assert "device busy" in (volume("FB1186").last_error or "")
    assert latest_op().status == "failed"


def test_eject_tape_aborts_when_auto_unmount_fails(app, tools):
    tools.mounted_path = os.path.realpath(str(tools.mount_point))
    tools.umount_fail("umount: target is busy")
    tools.eject_ok()
    vol = make_volume("FB1186", status="mounted")
    adapter = make_adapter(app, tools)

    result = eject_tape(app, adapter=adapter)

    assert result["ok"] is False
    assert "自动卸载失败" in result["message"]
    assert all("release_device" not in call for call in tools.calls())  # 没敢 eject
    assert volume("FB1186").status == "mounted"
    assert latest_op().status == "failed"


# ===========================================================================
# 5. current_status / device_snapshot
# ===========================================================================
def test_current_status_reports_device_and_current_volume(app, tools):
    tools.device_info_ok()
    tools.device_list_ok()
    tools.mounted_path = os.path.realpath(str(tools.mount_point))
    (tools.mount_point / ".volume_label").write_text("FB1186\n", encoding="utf-8")
    vol = make_volume("FB1186", status="mounted", files=2)
    adapter = make_adapter(app, tools)

    status = current_status(app, adapter=adapter, include_device_details=True)

    assert status["ok"] is True
    assert status["mounted"] is True
    assert status["barcode"] == "FB1186"
    assert status["device_exists"] is True
    assert status["device_path"] == str(tools.device)
    assert status["volume_label"] == "FB1186"
    assert status["adapter"] == "DirectAdapter"
    assert status["device_info"]["model"] == "Ultrium 6-HH"
    assert [item["path"] for item in status["device_list"]] == ["/dev/sg10", "/dev/sg11"]
    assert status["current_volume"]["ref"] == "FB1186"
    assert status["current_volume"]["status"] == "mounted"
    assert status["files"] == 2
    assert status["status"]["mounted"] is True


def test_current_status_skips_device_info_by_default(app, tools):
    """默认状态路径不能执行 device_info/device_list（真机会卡住设备）。"""
    tools.device_info_ok()
    tools.device_list_ok()
    adapter = make_adapter(app, tools)

    status = current_status(app, adapter=adapter)

    assert status["ok"] is True
    assert status["device_info"] == {}
    assert status["device_list"] == []
    joined = " ".join(tools.calls())
    assert "device_info" not in joined
    assert "device_list" not in joined


def test_current_status_when_device_missing(app, tools):
    tools.device_info_fail("ltfs: cannot open device")
    tools.device_list_fail()
    adapter = make_adapter(app, tools, device=str(tools.device.parent / "missing-sg"))

    status = current_status(app, adapter=adapter)

    assert status["ok"] is True
    assert status["device_exists"] is False
    assert status["mounted"] is False
    assert status["barcode"] is None
    assert status["current_volume"] is None
    assert "未检测到" in status["message"]


def test_device_snapshot_is_alias_of_current_status(app, tools):
    tools.device_info_ok()
    tools.device_list_ok()
    adapter = make_adapter(app, tools)

    snapshot = device_snapshot(app, adapter=adapter)

    for key in ("ok", "status", "device_info", "device_list", "mounted",
                "device_exists", "barcode", "current_volume"):
        assert key in snapshot


# ===========================================================================
# 6. 列表
# ===========================================================================
def test_list_tape_volumes_counts_files_and_ignores_nas(app):
    tape = make_volume("FB1186", status="mounted", files=2)
    deleted = LibraryFile(volume_id=tape.id, rel_path="gone.mp4", name="gone.mp4")
    db.session.add(deleted)
    db.session.commit()
    deleted.deleted_at = utcnow()
    db.session.commit()
    nas = LibraryVolume(kind="nas", ref="/mnt/user/av", name="NAS")
    db.session.add(nas)
    db.session.commit()

    volumes = list_tape_volumes(app)

    assert [item["ref"] for item in volumes] == ["FB1186"]
    assert volumes[0]["files"] == 2  # 软删的不算
    assert volumes[0]["status"] == "mounted"
    assert all(item["kind"] == "tape" for item in volumes)


def test_list_operations_filters_and_limits(app, tools):
    tools.mount_ok("FB1186")
    tools.umount_ok()
    adapter = make_adapter(app, tools)
    mount_tape(app, adapter=adapter)
    unmount_tape(app, adapter=adapter)

    all_ops = list_operations(app)
    assert [item["op_type"] for item in all_ops] == ["unmount", "mount"]  # 新→旧
    assert all(item["command"] for item in all_ops)

    only_mount = list_operations(app, op_type="mount")
    assert [item["op_type"] for item in only_mount] == ["mount"]

    limited = list_operations(app, limit=1)
    assert len(limited) == 1
    assert limited[0]["op_type"] == "unmount"


# ===========================================================================
# 7. 适配器异常兜底：对外函数绝不抛
# ===========================================================================
class BrokenAdapter:
    mount_point = "/tmp/broken-tape"

    def __init__(self, *, method="mount"):
        self.method = method
        self.last_output = {"command": None, "stdout": "", "stderr": ""}

    def _boom(self, *args, **kwargs):
        raise RuntimeError("adapter exploded")

    def mount(self):
        return self._boom()

    def unmount(self):
        return self._boom()

    def eject(self):
        return self._boom()

    def status(self):
        return self._boom()

    def device_info(self):
        return self._boom()

    def device_list(self):
        return self._boom()

    def volume_label(self):
        return self._boom()


def test_tape_ops_never_raise_when_adapter_explodes(app):
    broken = BrokenAdapter()

    mount_result = mount_tape(app, adapter=broken)
    unmount_result = unmount_tape(app, adapter=broken)
    eject_result = eject_tape(app, adapter=broken)
    status_result = current_status(app, adapter=broken)

    assert mount_result["ok"] is False
    assert "adapter exploded" in mount_result["message"]
    assert unmount_result["ok"] is False
    assert "adapter exploded" in unmount_result["message"]
    assert eject_result["ok"] is False
    assert "adapter exploded" in eject_result["message"]
    assert status_result["ok"] is False
    assert status_result["mounted"] is False


def test_mount_tape_operation_log_marked_failed_on_adapter_exception(app):
    mount_tape(app, adapter=BrokenAdapter())

    operation = latest_op()
    assert operation.op_type == "mount"
    assert operation.status == "failed"
    assert "adapter exploded" in (operation.message or "")


# ===========================================================================
# 7.5 连字符条码（用户自贴标签 LTO6-0001 之类）
# ===========================================================================
def test_mount_tape_hyphenated_barcode(app, tools):
    """假 ltfs 输出 LTO6-0001：不能被正则截成 LTO6，防串盘不能误杀。"""
    tools.mount_ok("LTO6-0001")
    adapter = make_adapter(app, tools)

    result = mount_tape(app, expected_barcode="LTO6-0001", adapter=adapter)

    assert result["ok"] is True
    assert result["barcode"] == "LTO6-0001"
    assert volume("LTO6-0001").status == "mounted"
    assert "LTO6-0001" in (latest_op().stdout or "")


def test_mount_tape_hyphenated_mismatch_aborts(app, tools):
    tools.mount_ok("LTO6-0002")
    tools.umount_ok()
    tools.eject_ok()
    adapter = make_adapter(app, tools)

    result = mount_tape(app, expected_barcode="LTO6-0001", adapter=adapter)

    assert result["ok"] is False
    assert "条码不符" in result["message"]
    assert "LTO6-0001" in result["message"] and "LTO6-0002" in result["message"]
    assert LibraryVolume.query.filter_by(kind="tape").count() == 0
    assert latest_op().status == "failed"


# ===========================================================================
# 8. 与路由联调（Lead 的 app/routes/tape.py，不碰真实设备）
# ===========================================================================
def test_tape_routes_end_to_end_with_fake_ltfs(app, tools):
    """通过 Lead 写的路由完整走一遍 mount → status → device → unmount → eject。"""
    app.config.update(
        {
            "TAPE_ADAPTER": "direct",
            "LTFS_DEVICE": str(tools.device),
            "LTFS_MOUNT": str(tools.mount_point),
            "LTFS_BIN": str(tools.ltfs),
            "UMOUNT_BIN": str(tools.umount),
        }
    )
    tools.mount_ok("FB1186")
    tools.umount_ok()
    tools.eject_ok()
    tools.device_info_ok()
    tools.device_list_ok()
    client = app.test_client()

    mounted = client.post("/api/tape/mount", json={"expected_barcode": "FB1186"})
    assert mounted.status_code == 200, mounted.get_data(as_text=True)
    payload = mounted.get_json()
    assert payload["ok"] is True
    assert payload["barcode"] == "FB1186"

    status = client.get("/api/tape/status")
    assert status.status_code == 200
    status_data = status.get_json()
    assert status_data["mounted"] is True
    assert status_data["barcode"] == "FB1186"
    assert status_data["device_exists"] is True

    device = client.get("/api/tape/device")
    assert device.status_code == 200
    device_data = device.get_json()
    assert device_data["device_info"]["model"] == "Ultrium 6-HH"
    assert device_data["device_list"][0]["path"] == "/dev/sg10"

    page = client.get("/tapes")
    assert page.status_code == 200
    assert "FB1186" in page.get_data(as_text=True)

    unmounted = client.post("/api/tape/unmount", json={})
    assert unmounted.status_code == 200
    assert unmounted.get_json()["ok"] is True
    assert volume("FB1186").status == "unmounted"

    ejected = client.post("/api/tape/eject", json={})
    assert ejected.status_code == 200
    assert ejected.get_json()["ok"] is True
    assert volume("FB1186").status == "ejected"

    ops_response = client.get("/api/tape/ops?limit=10")
    assert ops_response.status_code == 200
    op_types = [item["op_type"] for item in ops_response.get_json()["ops"]]
    assert "mount" in op_types and "unmount" in op_types and "eject" in op_types


def test_tape_mount_route_returns_400_on_failure(app, tools):
    app.config.update(
        {
            "TAPE_ADAPTER": "direct",
            "LTFS_DEVICE": str(tools.device),
            "LTFS_MOUNT": str(tools.mount_point),
            "LTFS_BIN": str(tools.ltfs),
            "UMOUNT_BIN": str(tools.umount),
        }
    )
    tools.mount_fail("ltfs: no tape loaded", rc=1)

    response = app.test_client().post("/api/tape/mount", json={})

    assert response.status_code == 400
    assert response.get_json()["ok"] is False
