"""CSRF / SMB 凭据文件安全测试。"""
from app import create_app
from app.db import db
from app.models import MediaSource
from app.services import smb


def _app(tmp_path, **overrides):
    config = {
        "WORKER_ENABLED": False,
        "DATA_DIR": tmp_path / "data",
        "DB_PATH": tmp_path / "data" / "test.db",
        "CSRF_ENABLED": True,
    }
    config.update(overrides)
    return create_app(config)


def test_csrf_blocks_browser_post_without_token(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    # 浏览器场景：有 Origin
    response = client.post(
        "/api/assets/upscale", json={}, headers={"Origin": "http://192.168.2.24:5080"}
    )
    assert response.status_code == 403


def test_csrf_accepts_cookie_token(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    client.get("/library")  # 种下 csrf_token Cookie
    token = client.get_cookie("csrf_token").value
    response = client.post(
        "/api/assets/upscale",
        json={},
        headers={"Origin": "http://192.168.2.24:5080", "X-CSRF-Token": token},
    )
    assert response.status_code == 202


def test_smb_password_is_not_stored_in_db(tmp_path, monkeypatch):
    app = _app(tmp_path, TESTING=True)
    monkeypatch.setattr(smb, "smb_available", lambda: True)
    monkeypatch.setattr(smb, "_run_mount", lambda *a, **k: (True, "挂载成功"))
    with app.app_context():
        result = smb.add_smb_source(
            app, name="SMB", host="192.168.2.10", share="av",
            mount_path="/mnt/smb/x", username="u", password="secret",
            auto_scan=False,
        )
        assert result["ok"] is True
        row = db.session.query(MediaSource).one()
        assert row.smb_password is None
        cred = tmp_path / "data" / "smb" / f"source_{row.id}.cred"
        assert cred.is_file()
        assert "password=secret" in cred.read_text()
        assert cred.stat().st_mode & 0o777 == 0o600
