"""合并版路由测试：磁带页/文件浏览安全/日志/备份 API/导航。"""
from pathlib import Path

import pytest

from app import create_app
from app.db import db


@pytest.fixture()
def app(tmp_path):
    browse = tmp_path / "browse"
    browse.mkdir()
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path / "data",
            "DB_PATH": tmp_path / "data" / "test.db",
            "MEDIA_DIR": tmp_path / "data" / "media",
            "COVER_DIR": tmp_path / "data" / "media" / "covers",
            "STILL_DIR": tmp_path / "data" / "media" / "stills",
            "AVATAR_DIR": tmp_path / "data" / "media" / "avatars",
            "SUBTITLE_DIR": tmp_path / "data" / "media" / "subtitles",
            "THUMB_DIR": tmp_path / "data" / "media" / "thumbs",
            "DUMP_DIR": tmp_path / "data" / "dump",
            "WORKER_ENABLED": False,
            "TAPE_ADAPTER": "direct",
            "LTFS_DEVICE": str(tmp_path / "no-such-device"),
            "LTFS_MOUNT": str(tmp_path / "tape"),
            "BROWSE_ROOTS": [str(browse)],
        }
    )
    (tmp_path / "tape").mkdir(exist_ok=True)
    (browse / "a.txt").write_text("hello")
    (browse / "sub").mkdir()
    (browse / "sub" / "b.txt").write_text("world")
    yield app
    with app.app_context():
        db.session.remove()


@pytest.fixture()
def client(app):
    return app.test_client()


def test_tape_page_renders_without_device(client):
    r = client.get("/tapes")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "磁带控制" in html
    assert "未检测到" in html


def test_tape_status_api_degrades(client):
    r = client.get("/api/tape/status")
    assert r.status_code == 200
    data = r.get_json()
    # 查询本身成功（ok=True），只是没检测到设备
    assert data["ok"] is True
    assert data["device_exists"] is False
    assert data["adapter"] == "DirectAdapter"


def test_tape_mount_without_device_returns_400(client):
    r = client.post("/api/tape/mount", json={"expected_barcode": "LTO6-0001"})
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_files_page_and_browse(client):
    r = client.get("/files")
    assert r.status_code == 200
    r = client.get("/api/files/browse", query_string={"side": "container", "path": ""})
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    names = {e["name"] for e in data["entries"]}
    assert {"a.txt", "sub"} <= names


def test_files_browse_rejects_escape(client, tmp_path):
    r = client.get("/api/files/browse", query_string={"side": "container", "path": "/etc"})
    assert r.status_code == 400
    assert "越界" in r.get_json()["error"]
    r = client.get(
        "/api/files/browse",
        query_string={"side": "container", "path": str(tmp_path / "browse" / ".." / "x")},
    )
    assert r.status_code == 400


def test_files_browse_tape_not_mounted(client):
    r = client.get("/api/files/browse", query_string={"side": "tape", "path": ""})
    assert r.status_code == 400
    assert "未挂载" in r.get_json()["error"]


def test_transfer_validation_and_enqueue(client):
    r = client.post("/api/files/transfer", json={"direction": "bad", "sources": ["/x"], "target": "/y"})
    assert r.status_code == 400
    r = client.post(
        "/api/files/transfer",
        json={"direction": "container_to_tape", "sources": [], "target": "/y"},
    )
    assert r.status_code == 400
    r = client.post(
        "/api/files/transfer",
        json={
            "direction": "container_to_tape",
            "sources": ["/x/a.mp4"],
            "target": "/media/tape",
        },
    )
    assert r.status_code == 202
    assert r.get_json()["job_id"] > 0


def test_logs_page(client):
    r = client.get("/logs")
    assert r.status_code == 200
    assert "操作日志" in r.get_data(as_text=True)


def test_backup_create_verify_delete(client):
    r = client.post("/api/backup/create", json={})
    assert r.status_code == 200
    data = r.get_json()
    name = data["name"]
    assert data["bytes"] > 0

    r = client.get("/api/backup/list")
    names = [b["name"] for b in r.get_json()["backups"]]
    assert name in names

    r = client.post("/api/backup/verify", json={"name": name})
    v = r.get_json()
    assert v["ok"] is True and v["sha256_ok"] is True and v["integrity"] == "ok"

    r = client.post("/api/backup/delete", json={"name": name})
    assert r.get_json()["deleted"] is True
    r = client.post("/api/backup/delete", json={"name": "../evil.db"})
    assert r.status_code in (400, 500)


def test_nav_contains_merge_pages(client):
    html = client.get("/library").get_data(as_text=True)
    for label in ("磁带", "文件", "日志", "备份"):
        assert f">{label}<" in html


def test_schema_migration_adds_tape_columns(app):
    from sqlalchemy import text

    with app.app_context():
        cols = {
            row[1]
            for row in db.session.execute(text("PRAGMA table_info(library_volume)"))
        }
    assert {"status", "device_path", "last_mounted_at", "last_error"} <= cols


def test_mount_returns_busy_when_drive_locked(app):
    import threading

    from app.services.tape_ops import acquire_drive, release_drive

    assert acquire_drive() is True
    result = {}

    def call():
        with app.test_client() as c:
            result["r"] = c.post("/api/tape/mount", json={"expected_barcode": "FB1186"})

    try:
        t = threading.Thread(target=call)
        t.start()
        t.join(timeout=10)
    finally:
        release_drive()

    r = result["r"]
    assert r.status_code == 400
    data = r.get_json()
    assert data["ok"] is False and data.get("busy") is True
    assert "正在进行" in data["message"]


def test_eject_disabled_returns_clear_error(app):
    from app.services.tape_ops import eject_tape

    app.config["AUTO_EJECT"] = False
    with app.app_context():
        result = eject_tape(app)
    assert result["ok"] is False
    assert "禁用" in result["message"]
