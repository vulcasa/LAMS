"""视频截图预留功能的基础测试。"""
import pytest

from app import create_app
from app.db import db
from app.models import LibraryFile, LibraryVolume, Title
from app.services import video_frames


def test_find_video_file_on_nas(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path / "data",
            "DB_PATH": tmp_path / "data" / "test.db",
            "WORKER_ENABLED": False,
        }
    )
    nas = tmp_path / "nas"
    (nas / "SSIS-001").mkdir(parents=True)
    video = nas / "SSIS-001" / "SSIS-001.mp4"
    video.write_bytes(b"x")
    with app.app_context():
        volume = LibraryVolume(kind="nas", ref=str(nas))
        db.session.add(volume)
        db.session.flush()
        title = Title(dvd_id="SSIS-001")
        db.session.add(title)
        db.session.flush()
        db.session.add(
            LibraryFile(
                volume_id=volume.id,
                rel_path="SSIS-001/SSIS-001.mp4",
                name="SSIS-001.mp4",
                size=1,
                title_id=title.id,
            )
        )
        db.session.commit()
        assert video_frames.find_video_file(app, title) == video


def test_extract_frames_reports_missing_ffmpeg(tmp_path, monkeypatch):
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path / "data",
            "DB_PATH": tmp_path / "data" / "test.db",
            "WORKER_ENABLED": False,
        }
    )
    monkeypatch.setattr(video_frames, "ffmpeg_available", lambda: False)
    with app.app_context():
        title = Title(dvd_id="SSIS-001")
        db.session.add(title)
        db.session.commit()
        result = video_frames.extract_frames(app, title.id)
    assert result["ok"] is False
    assert any("ffmpeg" in error for error in result["errors"])


def test_frame_timestamps_evenly_spaced():
    from app.services.video_frames import frame_timestamps

    stamps = frame_timestamps(600.0, 6)
    assert len(stamps) == 6
    assert stamps == [50.0, 150.0, 250.0, 350.0, 450.0, 550.0]
    assert all(
        round(b - a, 6) == 100.0 for a, b in zip(stamps, stamps[1:])
    )
    assert len(frame_timestamps(600.0, 1)) == 1


def test_frame_count_setting(tmp_path):
    from app.services import settings
    from app.services.video_frames import frame_count_setting

    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path / "data",
            "DB_PATH": tmp_path / "data" / "test.db",
            "WORKER_ENABLED": False,
        }
    )
    with app.app_context():
        settings.set_value(settings.KEY_VIDEO_FRAME_COUNT, "8")
        assert frame_count_setting(app) == 8
        settings.set_value(settings.KEY_VIDEO_FRAME_COUNT, "24")
        assert frame_count_setting(app) == 24
        settings.set_value(settings.KEY_VIDEO_FRAME_COUNT, "999")
        assert frame_count_setting(app) == 60


def test_unknown_duration_spreads_over_full_fallback():
    from app.services.video_frames import frame_timestamps

    stamps = frame_timestamps(None, 6, fallback_duration=7200)
    assert len(stamps) == 6
    # 不再只取前 60 秒；覆盖到接近全片末尾
    assert stamps[0] == 600.0
    assert stamps[-1] == 6600.0
    assert all(b > a for a, b in zip(stamps, stamps[1:]))


def test_resolve_duration_uses_file_size_estimate(tmp_path, monkeypatch):
    from app.services import video_frames

    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path / "data",
            "DB_PATH": tmp_path / "data" / "test.db",
            "WORKER_ENABLED": False,
            "VIDEO_ASSUMED_MBPS": 8,
        }
    )
    video = tmp_path / "big.mp4"
    video.write_bytes(b"x" * 8_000_000)  # 8MB / 8Mbps ≈ 8s → 低于 60s，走 fallback
    monkeypatch.setattr(video_frames, "_duration", lambda path: None)
    with app.app_context():
        duration, source = video_frames.resolve_duration(app, video)
    assert source in ("size", "fallback")
    assert duration >= 60


def test_extract_frames_per_video_file(tmp_path, monkeypatch):
    from pathlib import Path

    from app.models import Asset

    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path / "data",
            "DB_PATH": tmp_path / "data" / "test.db",
            "MEDIA_DIR": tmp_path / "data" / "media",
            "WORKER_ENABLED": False,
        }
    )
    nas = tmp_path / "nas"
    (nas / "SSIS-001").mkdir(parents=True)
    for name in ("part1.mp4", "part2.mp4"):
        (nas / "SSIS-001" / name).write_bytes(b"video")
    with app.app_context():
        volume = LibraryVolume(kind="nas", ref=str(nas))
        db.session.add(volume)
        db.session.flush()
        title = Title(dvd_id="SSIS-001")
        db.session.add(title)
        db.session.flush()
        for index, name in enumerate(("part1.mp4", "part2.mp4"), start=1):
            db.session.add(
                LibraryFile(
                    volume_id=volume.id,
                    rel_path=f"SSIS-001/{name}",
                    name=name,
                    size=5,
                    title_id=title.id,
                )
            )
        db.session.commit()

        class Result:
            returncode = 0
            stderr = ""
            stdout = ""

        def fake_run(command, **kwargs):
            Path(command[-1]).write_bytes(b"jpg")
            return Result()

        monkeypatch.setattr(video_frames, "ffmpeg_available", lambda: True)
        monkeypatch.setattr(video_frames.subprocess, "run", fake_run)
        monkeypatch.setattr(video_frames, "_duration", lambda path: 600.0)
        totals = []
        result = video_frames.extract_frames(
            app, title.id, count=2, on_total=totals.append
        )
        assert result["files"] == 2
        assert result["frames"] == 4
        assert totals == [4]
        assert Asset.query.filter_by(title_id=title.id, kind="screenshot").count() == 4


def test_frames_keep_original_resolution_by_default(tmp_path, monkeypatch):
    from pathlib import Path

    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path / "data",
            "DB_PATH": tmp_path / "data" / "test.db",
            "MEDIA_DIR": tmp_path / "data" / "media",
            "WORKER_ENABLED": False,
        }
    )
    nas = tmp_path / "nas"
    nas.mkdir()
    (nas / "a.mp4").write_bytes(b"video")
    with app.app_context():
        volume = LibraryVolume(kind="nas", ref=str(nas))
        db.session.add(volume)
        db.session.flush()
        title = Title(dvd_id="ORIG-001")
        db.session.add(title)
        db.session.flush()
        db.session.add(LibraryFile(volume_id=volume.id, rel_path="a.mp4",
                                   name="a.mp4", size=5, title_id=title.id))
        db.session.commit()

        class Result:
            returncode = 0
            stderr = ""

        commands = []

        def fake_run(command, **kwargs):
            commands.append(list(command))
            Path(command[-1]).write_bytes(b"jpg")
            return Result()

        monkeypatch.setattr(video_frames, "ffmpeg_available", lambda: True)
        monkeypatch.setattr(video_frames.subprocess, "run", fake_run)
        monkeypatch.setattr(video_frames, "_duration", lambda path: 600.0)
        video_frames.extract_frames(app, title.id, count=1)
        assert "-vf" not in commands[0]
        assert video_frames.frame_width_setting(app) == 0

        commands.clear()
        video_frames.extract_frames(app, title.id, count=1, width=1920)
        assert "scale=1920:-2" in commands[0]


def test_detail_has_back_button():
    from pathlib import Path

    html = Path(__file__).resolve().parents[1].joinpath(
        "app/templates/library/detail.html"
    ).read_text(encoding="utf-8")
    assert "扫描视频截图（预留）" not in html
    assert 'id="frame-modal"' in html
    assert "alert('已入队视频截图任务" not in html


def test_video_screenshots_use_six_column_grid():
    from pathlib import Path

    html = Path(__file__).resolve().parents[1].joinpath(
        "app/templates/library/detail.html"
    ).read_text(encoding="utf-8")
    assert 'class="frames-grid"' in html


def test_titles_missing_screenshots_excludes_tape_and_existing(tmp_path):
    from app.models import Asset

    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path / "data",
            "DB_PATH": tmp_path / "data" / "test.db",
            "WORKER_ENABLED": False,
        }
    )
    nas = tmp_path / "nas"
    tape = tmp_path / "tape"
    nas.mkdir(); tape.mkdir()
    (nas / "A-001.mp4").write_bytes(b"x")
    (tape / "B-001.mp4").write_bytes(b"x")
    with app.app_context():
        nas_volume = LibraryVolume(kind="nas", ref=str(nas))
        tape_volume = LibraryVolume(kind="tape", ref=str(tape))
        db.session.add_all([nas_volume, tape_volume]); db.session.flush()
        nas_title = Title(dvd_id="A-001")
        tape_title = Title(dvd_id="B-001")
        with_shot = Title(dvd_id="C-001")
        db.session.add_all([nas_title, tape_title, with_shot]); db.session.flush()
        db.session.add_all([
            LibraryFile(volume_id=nas_volume.id, rel_path="A-001.mp4", name="A-001.mp4",
                        size=1, title_id=nas_title.id),
            LibraryFile(volume_id=tape_volume.id, rel_path="B-001.mp4", name="B-001.mp4",
                        size=1, title_id=tape_title.id),
            LibraryFile(volume_id=nas_volume.id, rel_path="C-001.mp4", name="C-001.mp4",
                        size=1, title_id=with_shot.id),
            Asset(title_id=with_shot.id, kind="screenshot", idx=1, local_path="x.jpg"),
        ])
        db.session.commit()
        titles = video_frames.titles_missing_screenshots(app, limit=10)
        assert [title.dvd_id for title in titles] == ["A-001"]


def test_detail_has_top_right_back_button():
    from pathlib import Path

    html = Path(__file__).resolve().parents[1].joinpath(
        "app/templates/library/detail.html"
    ).read_text(encoding="utf-8")
    assert "function goBack()" in html
    assert "← 返回" in html
    assert "detail-toolbar" in html


def test_batch_frames_skips_titles_with_existing_shots(tmp_path, monkeypatch):
    from app.models import Asset
    from app.services import jobs
    from app.services import video_frames

    app = create_app({"TESTING": True, "WORKER_ENABLED": False,
                      "DATA_DIR": tmp_path / "data", "DB_PATH": tmp_path / "data" / "test.db"})
    nas = tmp_path / "nas"; nas.mkdir(); (nas / "BATCH-001.mp4").write_bytes(b"x")
    with app.app_context():
        volume = LibraryVolume(kind="nas", ref=str(nas))
        db.session.add(volume); db.session.flush()
        title = Title(dvd_id="BATCH-001")
        db.session.add(title); db.session.flush()
        db.session.add(LibraryFile(volume_id=volume.id, rel_path="BATCH-001.mp4",
                                   name="BATCH-001.mp4", size=1, title_id=title.id))
        db.session.add(Asset(title_id=title.id, kind="screenshot", idx=1, local_path="x.jpg"))
        db.session.commit()
        calls = []
        monkeypatch.setattr(video_frames, "extract_frames",
                            lambda *a, **k: calls.append("called") or {"frames": 0, "errors": []})
        result = jobs._handlers["video_frames_batch"](
            app, 999999, {"limit": 50}, lambda done, msg="": None
        )
        assert calls == []
        assert result["ok"] is True
