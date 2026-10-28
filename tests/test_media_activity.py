"""媒体库（新增/扫描/启停/删除）、自动扫描偏好、活动页测试。"""
import json
from pathlib import Path

import pytest

from app import create_app
from app.db import db


@pytest.fixture()
def app(tmp_path):
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
            "NAS_ROOTS": [],
        }
    )
    yield app
    with app.app_context():
        db.session.remove()


@pytest.fixture()
def client(app):
    return app.test_client()


def test_bootstrap_env_sources_once(app, tmp_path, monkeypatch):
    from app.services import media_sources

    root = tmp_path / "mnt1"
    root.mkdir()
    with app.app_context():
        monkeypatch.setitem(app.config, "NAS_ROOTS", [str(root)])
        created = media_sources.bootstrap_env_sources(app)
        assert created == 1
        assert len(media_sources.list_sources(app)) == 1
        # 第二次不再重复导入
        assert media_sources.bootstrap_env_sources(app) == 0


def test_bootstrap_imports_existing_volumes(app):
    from app.models import LibraryVolume
    from app.services import media_sources

    with app.app_context():
        db.session.add(LibraryVolume(kind="nas", ref="/data/old_nas", name="老库"))
        db.session.commit()
        media_sources.bootstrap_env_sources(app)
        sources = media_sources.list_sources(app)
        assert any(s["path"] == "/data/old_nas" and s["origin"] == "index" for s in sources)


def test_add_source_auto_scan_enqueues_job(app, tmp_path):
    from app.models import Job
    from app.services import media_sources, settings

    root = tmp_path / "lib1"
    root.mkdir()
    with app.app_context():
        settings.save_preferences(app, auto_scan_new_source=True)
        res = media_sources.add_source(app, name="库1", path=str(root))
        assert res["ok"] is True and res["job_id"]
        job = db.session.get(Job, res["job_id"])
        assert job.kind == "scan_nas"
        payload = json.loads(job.payload)
        assert payload["source_id"] == res["source"]["id"]
        assert res["source"]["last_scan_status"] == "queued"


def test_add_source_without_auto_scan(app, tmp_path):
    from app.models import Job
    from app.services import media_sources, settings

    root = tmp_path / "lib2"
    root.mkdir()
    with app.app_context():
        settings.save_preferences(app, auto_scan_new_source=False)
        res = media_sources.add_source(app, name="库2", path=str(root))
        assert res["ok"] is True and res["job_id"] is None
        assert Job.query.filter_by(kind="scan_nas").count() == 0


def test_add_source_validation(app, tmp_path):
    from app.services import media_sources

    root = tmp_path / "dup"
    root.mkdir()
    with app.app_context():
        assert media_sources.add_source(app, name="", path="")["ok"] is False
        assert media_sources.add_source(app, name="x", path="relative/path")["ok"] is False
        assert media_sources.add_source(app, name="a", path=str(root))["ok"] is True
        assert media_sources.add_source(app, name="b", path=str(root))["ok"] is False
        # 不存在路径：允许登记，但提示/不自动扫描
        missing = tmp_path / "missing"
        res = media_sources.add_source(app, name="m", path=str(missing))
        assert res["ok"] is True and res.get("warning")


def test_scan_toggle_delete_source(app, tmp_path):
    from app.models import LibraryFile, LibraryVolume
    from app.services import media_sources

    root = tmp_path / "lib3"
    root.mkdir()
    with app.app_context():
        res = media_sources.add_source(app, name="库3", path=str(root))
        sid = res["source"]["id"]
        assert media_sources.scan_source(app, sid)["ok"] is True
        assert media_sources.set_enabled(app, sid, False)["source"]["enabled"] is False
        # 造一条索引，验证 purge
        vol = LibraryVolume(kind="nas", ref=res["source"]["path"])
        db.session.add(vol)
        db.session.flush()
        db.session.add(LibraryFile(volume_id=vol.id, rel_path="a.mp4", name="a.mp4"))
        db.session.commit()
        out = media_sources.delete_source(app, sid, purge=True)
        assert out["ok"] is True and out["purged_files"] == 1
        assert LibraryVolume.query.filter_by(ref=res["source"]["path"]).first() is None


def test_sources_api_and_preferences(client, app, tmp_path):
    root = tmp_path / "api-lib"
    root.mkdir()
    r = client.post("/api/sources", json={"name": "API库", "path": str(root)})
    assert r.status_code == 200, r.get_json()
    sid = r.get_json()["source"]["id"]

    assert client.get("/api/sources").status_code == 200
    assert client.post(f"/api/sources/{sid}/scan", json={}).status_code == 200
    assert client.post(f"/api/sources/{sid}/enabled", json={"enabled": False}).status_code == 200
    assert client.delete(f"/api/sources/{sid}?purge=0").status_code == 200

    r = client.post("/api/settings/preferences", json={"auto_scan_new_source": False})
    assert r.status_code == 200 and r.get_json()["preferences"]["auto_scan_new_source"] is False


def test_sources_page_lists_media_and_no_v1(client, app, tmp_path):
    root = tmp_path / "page-lib"
    root.mkdir()
    client.post("/api/sources", json={"name": "页面库", "path": str(root)})
    html = client.get("/sources").get_data(as_text=True)
    assert "媒体库" in html and "页面库" in html
    assert "新增介质" in html
    assert "ltfs-webui" not in html and "LTFS_API_BASE" not in html


def test_activity_page_and_api(client, app):
    from app.models import Job, TapeOperation

    with app.app_context():
        db.session.add(
            Job(
                kind="scan_nas",
                status="running",
                progress=120,
                total=400,
                message="扫描中",
            )
        )
        db.session.add(TapeOperation(op_type="mount", status="running", barcode="FB1186"))
        db.session.commit()

    html = client.get("/activity").get_data(as_text=True)
    assert "活动" in html and "正在活动" in html

    r = client.get("/api/activity")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert any(j["kind"] == "scan_nas" and j["progress"] == 120 for j in data["running"])
    assert data["tape_ops"] and data["tape_ops"][0]["op_type"] == "mount"


def test_nav_has_activity(client):
    html = client.get("/library").get_data(as_text=True)
    assert ">活动<" in html


def test_activity_card_shows_percentage(app):
    html = app.test_client().get("/activity").get_data(as_text=True)
    assert "act-pct" in html
    assert "${pct}%" in html


def test_smb_logs_and_ltfs_cards_location(app):
    client = app.test_client()
    logs = client.get("/logs").get_data(as_text=True)
    assert "SMB 操作" in logs
    sources = client.get("/sources").get_data(as_text=True)
    tapes = client.get("/tapes").get_data(as_text=True)
    assert "LTFS 设备" not in sources
    assert "LTFS 设备" in tapes
    assert "LTFS 命令" in tapes


def test_media_count_includes_tapes_and_barcode_select(app):
    client = app.test_client()
    sources = client.get("/sources").get_data(as_text=True)
    assert "媒体库（含磁带）" in sources
    tapes = client.get("/tapes").get_data(as_text=True)
    assert 'id="expect-barcode"' in tapes
    assert "不校验（默认）" in tapes


def test_scrape_has_batch_video_frames_button(app):
    html = app.test_client().get("/scrape").get_data(as_text=True)
    assert "批量生成截图" in html


def test_monitor_api_and_activity_panel(app):
    client = app.test_client()
    data = client.get("/api/monitor").get_json()
    assert data["ok"] is True
    assert "cpu_percent" in data
    assert {"load", "memory", "network", "users"} <= set(data)
    html = client.get("/activity").get_data(as_text=True)
    assert "monitor-pane" in html
    assert "mon-cpu-chart" in html
    assert "setInterval(refreshMonitor, 5000)" in html
