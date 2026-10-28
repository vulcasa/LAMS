"""Job 队列 / API 契约测试（不依赖业务服务模块）。"""
import json

import pytest

from app import create_app
from app.db import db
from app.models import Job
from app.services import jobs


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "DATA_DIR": tmp_path,
            "DB_PATH": tmp_path / "test.db",
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
    yield app
    with app.app_context():
        db.session.remove()


@pytest.fixture()
def client(app):
    return app.test_client()


def test_enqueue_and_claim(app):
    with app.app_context():
        jid = jobs.enqueue("scan_nas", {"root": "/mnt/av"})
        job = jobs._claim_next()
        assert job is not None and job.id == jid
        assert job.status == "running"
        # 已领取的任务不会被再领一次
        assert jobs._claim_next() is None


def test_run_job_done(app):
    calls = {}

    @jobs.register("_test_ok")
    def _ok(app_, job_id, payload, progress):
        calls["payload"] = payload
        progress(1, "半程")
        return {"ok": True, "n": 1}

    with app.app_context():
        jid = jobs.enqueue("_test_ok", {"x": 1})
        job = jobs._claim_next()
        jobs.run_job(job, app)
        row = db.session.get(Job, jid)
        assert row.status == "done"
        assert json.loads(row.result) == {"ok": True, "n": 1}
        assert calls["payload"] == {"x": 1}


def test_run_job_unknown_kind_marks_failed(app):
    with app.app_context():
        jid = jobs.enqueue("does_not_exist", {})
        job = jobs._claim_next()
        jobs.run_job(job, app)
        assert db.session.get(Job, jid).status == "failed"


def test_run_job_exception_marks_failed(app):
    @jobs.register("_test_boom")
    def _boom(app_, job_id, payload, progress):
        raise RuntimeError("boom")

    with app.app_context():
        jid = jobs.enqueue("_test_boom", {})
        job = jobs._claim_next()
        jobs.run_job(job, app)
        row = db.session.get(Job, jid)
        assert row.status == "failed"
        assert "boom" in row.error


def test_api_scan_nas_validation(client):
    r = client.post("/api/scan/nas", json={})
    assert r.status_code == 400
    r = client.post("/api/scan/nas", json={"root": "/mnt/av"})
    assert r.status_code == 202
    assert r.get_json()["job_id"] > 0


def test_api_scan_tape_requires_barcode(client):
    assert client.post("/api/scan/tape", json={"mount": "/media/tape"}).status_code == 400
    assert client.post("/api/scan/tape", json={"barcode": "LTO6-0001"}).status_code == 202


def test_api_jobs_list(client):
    client.post("/api/materialize", json={})
    r = client.get("/api/jobs?limit=5")
    assert r.status_code == 200
    data = r.get_json()["jobs"]
    assert data and data[0]["kind"] == "materialize"


def test_api_summary_and_volumes(client):
    assert client.get("/api/summary").status_code == 200
    r = client.get("/api/volumes")
    assert r.status_code == 200 and "volumes" in r.get_json()


def test_job_pause_resume_stop_endpoints(app):
    from app.models import Job

    with app.app_context():
        job = Job(kind="test", payload="{}", status="running")
        db.session.add(job)
        db.session.commit()
        job_id = job.id

        client = app.test_client()
        r = client.post(f"/api/jobs/{job_id}/pause")
        assert r.status_code == 200
        db.session.refresh(job)
        assert job.status == "paused"

        r = client.post(f"/api/jobs/{job_id}/resume")
        assert r.status_code == 200
        db.session.refresh(job)
        assert job.status == "running"

        r = client.post(f"/api/jobs/{job_id}/stop")
        assert r.status_code == 200
        db.session.refresh(job)
        assert job.cancel_requested is True


def test_stop_pending_job_marks_cancelled(app):
    from app.models import Job

    with app.app_context():
        job = Job(kind="test", payload="{}", status="pending")
        db.session.add(job)
        db.session.commit()
        job_id = job.id
        r = app.test_client().post(f"/api/jobs/{job_id}/stop")
        assert r.status_code == 200
        db.session.refresh(job)
        assert job.status == "cancelled"
        assert job.cancel_requested is True


def test_activity_page_has_job_controls(app):
    html = app.test_client().get("/activity").get_data(as_text=True)
    assert "jobControl(" in html
    assert "暂停" in html
    assert "停止" in html
