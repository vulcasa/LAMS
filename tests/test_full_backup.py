"""完整备份（数据库 + manifest + 封面/剧照/抽帧）测试。"""
import json
import sqlite3
import zipfile
from pathlib import Path

from app import create_app
from app.db import db
from app.models import Title
from app.services.full_backup import (
    create_full_backup,
    list_full_backups,
    restore_full_backup,
)


def _app(tmp_path):
    return create_app({
        "TESTING": True, "WORKER_ENABLED": False, "CSRF_ENABLED": False,
        "DATA_DIR": tmp_path,
        "DB_PATH": tmp_path / "avlib.db",
        "MEDIA_DIR": tmp_path / "media",
        "COVER_DIR": tmp_path / "media" / "covers",
        "STILL_DIR": tmp_path / "media" / "stills",
        "MANIFEST_DIR": tmp_path / "manifests",
    })


def _seed(app, tmp_path):
    with app.app_context():
        db.create_all()
        db.session.add(Title(dvd_id="FULL-001", title_ja="完整备份测试"))
        db.session.commit()
    for rel, data in (
        ("covers/FULL-001.jpg", b"cover"),
        ("stills/FULL-001/01.jpg", b"still"),
        ("screenshots/FULL-001/01.jpg", b"frame"),
        ("manifests/FB0001.manifest.json", b'{"barcode":"FB0001"}'),
    ):
        root = tmp_path if rel.startswith("manifests/") else tmp_path / "media"
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def test_create_full_backup_contains_all_assets(tmp_path):
    app = _app(tmp_path)
    _seed(app, tmp_path)
    result = create_full_backup(app, keep=2)
    path = Path(result["path"])
    assert path.is_file() and path.suffix == ".zip"
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        assert "database/avlib.db" in names
        assert "manifests/FB0001.manifest.json" in names
        assert "media/covers/FULL-001.jpg" in names
        assert "media/stills/FULL-001/01.jpg" in names
        assert "media/screenshots/FULL-001/01.jpg" in names
        meta = json.loads(zf.read("backup.json"))
    assert meta["counts"] == {"manifests": 1, "covers": 1, "stills": 1, "screenshots": 1}
    listed = list_full_backups(app)
    assert listed and listed[0]["name"] == result["name"]


def test_restore_full_backup_restores_database_and_assets(tmp_path):
    app = _app(tmp_path)
    _seed(app, tmp_path)
    result = create_full_backup(app, keep=2)

    # 篡改当前数据和资产
    with app.app_context():
        title = Title.query.filter_by(dvd_id="FULL-001").one()
        title.title_ja = "已改动"
        db.session.commit()
    (tmp_path / "media" / "covers" / "FULL-001.jpg").write_bytes(b"changed")

    with app.app_context():
        restored = restore_full_backup(app, Path(result["path"]))
    assert restored["ok"] and restored["database"]
    assert restored["assets"] == 4

    conn = sqlite3.connect(tmp_path / "avlib.db")
    try:
        value = conn.execute(
            "SELECT title_ja FROM title WHERE dvd_id='FULL-001'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert value == "完整备份测试"
    assert (tmp_path / "media" / "covers" / "FULL-001.jpg").read_bytes() == b"cover"
    assert (tmp_path / "media" / "screenshots" / "FULL-001" / "01.jpg").read_bytes() == b"frame"
