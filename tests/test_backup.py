"""数据库备份 / 校验 / 列表 / 删除的测试（临时 DB + tmp 备份目录）。

覆盖：

1. 创建：sqlite backup API 生成 ``avlib-YYYYmmdd-HHMMSS.db`` + ``.sha256`` 边车，
   备份内容包含已提交数据（WAL 里的数据也能备到，证明不是 cp）；
2. 列表：name/size/mtime/sha256/path；无目录返回空；边车缺失时 sha256=None；
3. 校验：正常通过；边车损坏/不匹配/缺失；非法 SQLite 文件；空文件；文件不存在；
4. 删除：备份 + 边车一起删；不存在返回 deleted=False；
5. 目录穿越：``../x``、子目录、绝对路径、非备份名、指向目录外的软链 → ValueError；
6. 轮换：``keep=2`` 保留最新两份（连同边车），同秒多次备份自动加后缀；
7. 恢复说明：明确「停容器 → 覆盖 → 启动」，并提示要一起处理 -wal/-shm。

跑法（项目根 = av-archive/）::

    .venv/bin/python -m pytest tests/test_backup.py -q
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.db import db  # noqa: E402
from app.models import Title  # noqa: E402
from app.services import backup as backup_service  # noqa: E402
from app.services.backup import (  # noqa: E402
    BACKUP_NAME_RE,
    backup_dir,
    create_backup,
    delete_backup,
    list_backups,
    restore_notes,
    verify_backup,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "DATA_DIR": tmp_path,
            "DB_PATH": tmp_path / "avlib.db",
            "MEDIA_DIR": tmp_path / "media",
            "COVER_DIR": tmp_path / "media" / "covers",
            "STILL_DIR": tmp_path / "media" / "stills",
            "AVATAR_DIR": tmp_path / "media" / "avatars",
            "SUBTITLE_DIR": tmp_path / "media" / "subtitles",
            "THUMB_DIR": tmp_path / "media" / "thumbs",
            "DUMP_DIR": tmp_path / "dump",
            "WORKER_ENABLED": False,
        }
    )
    yield application
    with application.app_context():
        db.session.remove()


@pytest.fixture()
def backups(app) -> Path:
    return backup_dir(app)


def _seed_titles(app, count: int = 3) -> list[str]:
    codes = []
    with app.app_context():
        for index in range(count):
            code = f"BKP-{index + 1:03d}"
            db.session.add(Title(dvd_id=code, title_ja=f"备份测试 {index}"))
            codes.append(code)
        db.session.commit()
    return codes


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _write_backup_file(directory: Path, name: str, data: bytes,
                       *, sidecar_content: str | None = None) -> Path:
    path = directory / name
    path.write_bytes(data)
    if sidecar_content is not None:
        (directory / (name + ".sha256")).write_text(sidecar_content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. 创建 + 列表 + 校验
# ---------------------------------------------------------------------------
def test_create_backup_writes_db_sidecar_and_lists_it(app):
    codes = _seed_titles(app)
    directory = backup_dir(app)

    result = create_backup(app, keep=5)

    name = result["name"]
    assert BACKUP_NAME_RE.match(name)
    path = Path(result["path"])
    assert path.is_file()
    assert path.parent == directory
    assert result["bytes"] == path.stat().st_size > 0
    assert result["sha256"] == _sha256(path)
    assert result["removed"] == []
    assert result["keep"] == 5
    assert result["elapsed_s"] >= 0

    sidecar = Path(result["sidecar"])
    assert sidecar.is_file()
    assert sidecar.read_text(encoding="utf-8") == f"{result['sha256']}  {name}\n"

    # 备份里真的有已提交的数据
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT count(*) FROM title").fetchone()[0] == len(codes)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()

    entries = list_backups(app)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["name"] == name
    assert entry["path"] == str(path)
    assert entry["size"] == result["bytes"]
    assert entry["sha256"] == result["sha256"]
    assert entry["has_sidecar"] is True
    assert entry["mtime"]

    verified = verify_backup(app, name)
    assert verified["ok"] is True
    assert verified["sha256_ok"] is True
    assert verified["integrity"] == "ok"
    assert verified["errors"] == []
    assert verified["sha256"] == result["sha256"]


def test_create_backup_defaults_to_data_dir_backups(app):
    _seed_titles(app, count=1)
    result = create_backup(app)
    assert Path(result["path"]).parent == Path(app.config["DATA_DIR"]) / "backups"
    assert result["keep"] >= 1


def test_create_backup_missing_source_raises(app):
    with app.app_context():
        db.session.remove()
    Path(app.config["DB_PATH"]).unlink()
    with pytest.raises(FileNotFoundError):
        create_backup(app)


# ---------------------------------------------------------------------------
# 2. 轮换 / 同秒冲突
# ---------------------------------------------------------------------------
def test_rotation_keeps_newest_and_removes_sidecar(app):
    _seed_titles(app, count=1)

    first = create_backup(app, keep=10)
    time.sleep(0.01)
    second = create_backup(app, keep=10)
    time.sleep(0.01)
    third = create_backup(app, keep=2)

    assert len({first["name"], second["name"], third["name"]}) == 3  # 同秒也唯一
    names = {entry["name"] for entry in list_backups(app)}
    assert names == {second["name"], third["name"]}
    assert first["name"] in third["removed"]
    assert not (backup_dir(app) / first["name"]).exists()
    assert not (backup_dir(app) / (first["name"] + ".sha256")).exists()


def test_rotation_uses_config_backup_keep(app):
    _seed_titles(app, count=1)
    app.config["BACKUP_KEEP"] = 2

    for _ in range(3):
        time.sleep(0.01)
        create_backup(app)

    assert len(list_backups(app)) == 2


# ---------------------------------------------------------------------------
# 3. 校验错误分支
# ---------------------------------------------------------------------------
def test_verify_detects_corrupted_sidecar(app):
    _seed_titles(app, count=1)
    result = create_backup(app, keep=5)
    sidecar = backup_dir(app) / (result["name"] + ".sha256")

    # 1) 边车格式非法
    sidecar.write_text("not-a-sha256\n", encoding="utf-8")
    broken = verify_backup(app, result["name"])
    assert broken["ok"] is False
    assert broken["sha256_ok"] is False
    assert any("边车" in error for error in broken["errors"])
    assert broken["integrity"] == "ok"  # 文件本身还是好的

    # 2) 边车是合法 hex 但内容不对
    sidecar.write_text(f"{'0' * 64}  {result['name']}\n", encoding="utf-8")
    mismatched = verify_backup(app, result["name"])
    assert mismatched["ok"] is False
    assert mismatched["sha256_ok"] is False
    assert any("不一致" in error for error in mismatched["errors"])


def test_verify_detects_missing_sidecar(app):
    _seed_titles(app, count=1)
    result = create_backup(app)
    (backup_dir(app) / (result["name"] + ".sha256")).unlink()

    verified = verify_backup(app, result["name"])

    assert verified["ok"] is False
    assert verified["sha256_ok"] is False
    assert any("边车" in error for error in verified["errors"])
    entry = list_backups(app)[0]
    assert entry["sha256"] is None and entry["has_sidecar"] is False


def test_verify_rejects_invalid_sqlite_file(app):
    _seed_titles(app, count=1)
    directory = backup_dir(app)
    directory.mkdir(parents=True, exist_ok=True)
    name = "avlib-20260101-120000.db"
    path = _write_backup_file(directory, name, b"this is not a sqlite database")
    (directory / (name + ".sha256")).write_text(f"{_sha256(path)}  {name}\n",
                                                encoding="utf-8")

    verified = verify_backup(app, name)

    assert verified["ok"] is False
    assert verified["sha256_ok"] is True       # 文件没被动过
    assert verified["integrity"] == "invalid"
    assert any("SQLite" in error for error in verified["errors"])


def test_verify_rejects_empty_file(app):
    _seed_titles(app, count=1)
    directory = backup_dir(app)
    directory.mkdir(parents=True, exist_ok=True)
    name = "avlib-20260101-120001.db"
    _write_backup_file(directory, name, b"",
                       sidecar_content=f"{hashlib.sha256(b'').hexdigest()}  {name}\n")

    verified = verify_backup(app, name)

    assert verified["ok"] is False
    assert verified["integrity"] == "invalid"
    assert any("为空" in error for error in verified["errors"])


def test_verify_missing_file(app):
    verified = verify_backup(app, "avlib-20260101-120002.db")
    assert verified["ok"] is False
    assert verified["integrity"] == "missing"
    assert any("不存在" in error for error in verified["errors"])


# ---------------------------------------------------------------------------
# 4. 删除
# ---------------------------------------------------------------------------
def test_delete_backup_removes_db_and_sidecar(app):
    _seed_titles(app, count=1)
    result = create_backup(app, keep=5)
    path = Path(result["path"])
    sidecar = Path(result["sidecar"])

    deleted = delete_backup(app, result["name"])

    assert deleted["deleted"] is True
    assert deleted["bytes"] == result["bytes"]
    assert deleted["sidecar_deleted"] is True
    assert not path.exists() and not sidecar.exists()
    assert list_backups(app) == []


def test_delete_missing_backup_is_not_an_error(app):
    deleted = delete_backup(app, "avlib-20200101-000000.db")
    assert deleted["deleted"] is False
    assert deleted["errors"]


# ---------------------------------------------------------------------------
# 5. 目录穿越 / 非法名字
# ---------------------------------------------------------------------------
def test_path_traversal_is_rejected(app):
    _seed_titles(app, count=1)
    directory = backup_dir(app)
    directory.mkdir(parents=True, exist_ok=True)
    outside = Path(app.config["DATA_DIR"]) / "secret.db"
    outside.write_bytes(b"do not touch")

    bad_names = [
        "../secret.db",
        "sub/avlib-20260101-120000.db",
        str(outside),
        "/etc/passwd",
        "avlib.db",
        "notes.txt",
        "avlib-20260101-120000.db/../x.db",
    ]
    for name in bad_names:
        with pytest.raises(ValueError):
            verify_backup(app, name)
        with pytest.raises(ValueError):
            delete_backup(app, name)
    assert outside.exists()

    # 目录内的软链指向目录外 → resolve 后父目录不对，也要拒绝
    link_name = "avlib-20260101-120000.db"
    os.symlink(outside, directory / link_name)
    with pytest.raises(ValueError):
        delete_backup(app, link_name)
    with pytest.raises(ValueError):
        verify_backup(app, link_name)
    assert outside.exists()

    # 正常名字可以用（这里文件不存在，只验证不抛 ValueError）
    missing = verify_backup(app, "avlib-20200101-000000.db")
    assert missing["ok"] is False


def test_list_backups_missing_dir_returns_empty(app, tmp_path):
    missing = tmp_path / "no-such-backups"
    assert list_backups(app, dest_dir=missing) == []
    assert missing.is_dir()  # 列表页顺手建目录，方便 UI 直接显示


def test_backup_dir_with_spaces_and_chinese(app, tmp_path):
    directory = tmp_path / "备份 目录 with spaces"
    _seed_titles(app, count=1)

    result = create_backup(app, dest_dir=directory, keep=2)
    verified = verify_backup(app, result["name"], dest_dir=directory)
    deleted = delete_backup(app, result["name"], dest_dir=directory)

    assert verified["ok"] is True
    assert deleted["deleted"] is True
    assert list_backups(app, dest_dir=directory) == []


# ---------------------------------------------------------------------------
# 6. 恢复说明
# ---------------------------------------------------------------------------
def test_restore_notes_documents_offline_restore():
    notes = restore_notes()
    joined = "\n".join(notes)

    assert len(notes) >= 4
    assert "docker compose stop" in joined
    assert "avlib.db-wal" in joined
    assert "-shm" in joined
    assert "在线恢复" in joined  # 明确说明不自动在线恢复


def test_backup_name_regex_matches_created_names():
    names = [
        "avlib-20261026-120000.db",
        "avlib-20261026-120000-02.db",
    ]
    assert all(BACKUP_NAME_RE.match(name) for name in names)
    assert not BACKUP_NAME_RE.match("avlib-20261026-120000.db.sha256")
    assert not BACKUP_NAME_RE.match("../avlib-20261026-120000.db")
