"""SMB 运行时挂载：参数校验 / 登记媒体库 / 密码不外泄。"""
from pathlib import Path

import pytest

from app import create_app
from app.db import db
from app.models import MediaSource
from app.services import smb


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path / "data",
            "DB_PATH": tmp_path / "data" / "test.db",
            "WORKER_ENABLED": False,
        }
    )
    yield app
    with app.app_context():
        db.session.remove()


def test_requires_cifs_utils(app, monkeypatch):
    monkeypatch.setattr(smb, "smb_available", lambda: False)
    with app.app_context():
        result = smb.add_smb_source(
            app, name="x", host="192.168.2.10", share="av",
            mount_path="", username="u", password="p",
        )
    assert result["ok"] is False
    assert "cifs-utils" in result["error"]


def test_add_smb_source_registers_without_password(app, monkeypatch):
    from app.services import media_sources

    monkeypatch.setattr(smb, "smb_available", lambda: True)
    monkeypatch.setattr(smb, "_run_mount", lambda *a, **k: (True, "挂载成功"))
    with app.app_context():
        result = smb.add_smb_source(
            app, name="SMB 库", host="192.168.2.10", share="av",
            mount_path="/mnt/smb/test", username="user", password="secret",
            options="vers=3.0", auto_scan=False,
        )
    assert result["ok"] is True
    with app.app_context():
        row = db.session.query(MediaSource).one()
        assert row.origin == "smb"
        assert row.smb_host == "192.168.2.10"
        info = media_sources._source_dict(app, row)
        assert info["smb"]["host"] == "192.168.2.10"
        assert "password" not in info["smb"]
        assert "smb_password" not in info


def test_invalid_host_rejected(app):
    with app.app_context():
        result = smb.add_smb_source(
            app, name="x", host="bad host\n", share="av",
            mount_path="", username="u", password="p",
        )
    assert result["ok"] is False
    assert "IP" in result["error"] or "主机" in result["error"]


def test_parse_mountinfo_detects_only_cifs():
    text = (
        "36 25 0:32 / / rw,relatime - overlay overlay rw\n"
        "99 36 0:50 / /mnt/av/番号已整理 rw,relatime - cifs //192.168.2.8/Adults rw\n"
    )
    assert smb.parse_mountinfo(text, "/mnt/av/番号已整理") is True
    assert smb.parse_mountinfo(text, "/mnt/av") is False
    bind = "99 36 0:50 / /mnt/av/番号已整理 rw - ext4 /dev/sda1 rw\n"
    assert smb.parse_mountinfo(bind, "/mnt/av/番号已整理") is False


def test_smb_operation_is_logged(app, monkeypatch):
    from app.models import SmbOperation

    monkeypatch.setattr(smb, "smb_available", lambda: True)
    monkeypatch.setattr(smb, "_run_mount", lambda *a, **k: (True, "挂载成功"))
    with app.app_context():
        smb.add_smb_source(
            app, name="SMB", host="192.168.2.10", share="av",
            mount_path="/mnt/smb/x", username="u", password="secret",
            auto_scan=False,
        )
        row = db.session.query(SmbOperation).one()
        assert row.action == "add"
        assert row.status == "success"
        assert row.host == "192.168.2.10"


def test_mount_command_does_not_pass_t_flag(tmp_path, monkeypatch):
    calls = {}

    def fake_run(command, **kwargs):
        calls["command"] = command

        class Result:
            returncode = 0
            stderr = ""
            stdout = ""

        return Result()

    monkeypatch.setattr(smb, "smb_available", lambda: True)
    monkeypatch.setattr(smb, "_is_mounted", lambda path: True)
    monkeypatch.setattr(smb.subprocess, "run", fake_run)
    ok, message = smb._run_mount("192.168.2.8", "av", tmp_path / "mnt", "u", "p", "vers=3.0")
    assert ok is True, message
    assert "-t" not in calls["command"]
    assert calls["command"][1].startswith("//192.168.2.8/av")
