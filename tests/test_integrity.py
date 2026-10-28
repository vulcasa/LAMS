"""文件 SHA-256 计算 / 抽检 / manifest 包含哈希。"""
import hashlib

from app import create_app
from app.db import db
from app.models import LibraryFile, LibraryVolume, Title
from app.services.audit import _db_file_entries
from app.services.integrity import hash_library_files


def _app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path / "data",
            "DB_PATH": tmp_path / "data" / "test.db",
            "WORKER_ENABLED": False,
        }
    )


def test_hash_store_and_verify_mismatch(tmp_path):
    nas = tmp_path / "nas"
    nas.mkdir()
    video = nas / "SSIS-001.mp4"
    video.write_bytes(b"hello world")
    app = _app(tmp_path)
    with app.app_context():
        volume = LibraryVolume(kind="nas", ref=str(nas))
        db.session.add(volume)
        db.session.flush()
        title = Title(dvd_id="SSIS-001")
        db.session.add(title)
        db.session.flush()
        row = LibraryFile(volume_id=volume.id, rel_path="SSIS-001.mp4",
                          name="SSIS-001.mp4", size=11, title_id=title.id)
        db.session.add(row)
        db.session.commit()

        stored = hash_library_files(app, volume_id=volume.id, verify=False)
        assert stored["hashed"] == 1
        assert row.sha256 == hashlib.sha256(b"hello world").hexdigest()
        # manifest 读取行时带 sha256
        assert _db_file_entries(volume.id)[0]["sha256"] == row.sha256

        video.write_bytes(b"tampered")
        checked = hash_library_files(app, volume_id=volume.id, verify=True)
        assert checked["mismatched"] == 1
        assert checked["ok"] is False


def test_hash_with_frames_calls_extractor(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    nas.mkdir()
    (nas / "SSIS-001.mp4").write_bytes(b"video")
    app = _app(tmp_path)
    with app.app_context():
        volume = LibraryVolume(kind="nas", ref=str(nas))
        db.session.add(volume)
        db.session.flush()
        title = Title(dvd_id="SSIS-001")
        db.session.add(title)
        db.session.flush()
        db.session.add(
            LibraryFile(
                volume_id=volume.id, rel_path="SSIS-001.mp4",
                name="SSIS-001.mp4", size=5, title_id=title.id,
            )
        )
        db.session.commit()
        called = {}

        def fake_extract(app_arg, title_id, **kwargs):
            called["title_id"] = title_id
            called["allow_tape"] = kwargs.get("allow_tape")
            return {"ok": True, "frames": 6, "errors": []}

        from app.services import video_frames

        monkeypatch.setattr(video_frames, "extract_frames", fake_extract)
        stats = hash_library_files(
            app, volume_id=volume.id, verify=False,
            with_frames=True, max_videos=1,
        )
        assert stats["frames_titles"] == 1
        assert stats["frames"] == 6
        assert called["title_id"] == title.id
        assert called["allow_tape"] is True


def test_hash_reports_total_callback(tmp_path):
    nas = tmp_path / "nas"
    nas.mkdir()
    (nas / "a.mp4").write_bytes(b"a")
    app = _app(tmp_path)
    with app.app_context():
        volume = LibraryVolume(kind="nas", ref=str(nas))
        db.session.add(volume)
        db.session.flush()
        db.session.add(LibraryFile(volume_id=volume.id, rel_path="a.mp4",
                                   name="a.mp4", size=1))
        db.session.commit()
        totals = []
        hash_library_files(app, volume_id=volume.id, verify=False,
                           on_total=totals.append)
        assert totals == [1]
