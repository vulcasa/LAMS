"""自动轮询下载缺失封面/剧照 + 设置开关的回归测试。"""
import pytest

from app import create_app
from app.db import db
from app.models import Asset, Job, Title
from app.services import jobs, settings


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path / "data",
            "DB_PATH": tmp_path / "data" / "test.db",
            "WORKER_ENABLED": False,
            "ASSET_AUTO_POLL_SECONDS": 0,
            "ASSET_AUTO_BATCH": 7,
        }
    )
    yield app
    with app.app_context():
        db.session.remove()


def _add_missing_title(app, *, cover_url="https://example.com/a.jpg"):
    with app.app_context():
        title = Title(dvd_id="SSIS-001", cover_url=cover_url)
        db.session.add(title)
        db.session.commit()
        return title.id


def test_missing_summary_counts_actionable(app):
    _add_missing_title(app)
    with app.app_context():
        summary = __import__("app.services.assets", fromlist=["missing_assets_summary"]).missing_assets_summary(app)
        assert summary["covers_with_url"] == 1
        assert summary["actionable"] >= 1


def test_auto_poll_disabled_by_setting(app):
    _add_missing_title(app)
    with app.app_context():
        settings.set_bool(settings.KEY_AUTO_ASSETS, False)
        assert jobs.maybe_auto_assets(app, force=True) is None
        assert Job.query.count() == 0


def test_auto_poll_enqueues_batch(app):
    _add_missing_title(app)
    with app.app_context():
        job_id = jobs.maybe_auto_assets(app, force=True)
        assert job_id is not None
        job = db.session.get(Job, job_id)
        assert job.kind == "cover_fill"
        assert '"auto": true' in job.payload
        assert '"limit": 7' in job.payload


def test_auto_poll_does_not_duplicate_active_job(app):
    _add_missing_title(app)
    with app.app_context():
        first = jobs.maybe_auto_assets(app, force=True)
        assert first is not None
        assert jobs.maybe_auto_assets(app, force=True) is None


def test_auto_poll_continues_after_batch(app):
    _add_missing_title(app)
    with app.app_context():
        first = jobs.maybe_auto_assets(app, force=True)
        job = db.session.get(Job, first)
        job.status = "done"
        db.session.commit()
        second = jobs.maybe_auto_assets(app, force=True)
        assert second is not None and second != first


def test_auto_poll_noop_when_nothing_missing(app):
    with app.app_context():
        assert jobs.maybe_auto_assets(app, force=True) is None


def test_preferences_toggle(app):
    client = app.test_client()
    response = client.post(
        "/api/settings/preferences",
        json={"auto_scan_new_source": True, "auto_asset_poll": False},
    )
    assert response.status_code == 200
    assert response.get_json()["preferences"]["auto_asset_poll"] is False
    with app.app_context():
        assert settings.get_bool(settings.KEY_AUTO_ASSETS, True) is False


def test_scrape_has_one_unified_button(app):
    html = app.test_client().get("/scrape").get_data(as_text=True)
    assert "下载缺失封面/剧照" in html
    assert "补齐缺封面" not in html


def test_failed_assets_stop_auto_polling(app, monkeypatch):
    """DMM 占位图/下载失败要打标记；自动轮询不再无限重试同一批。"""
    from app.models import Asset, utcnow
    from app.services import assets

    with app.app_context():
        title = Title(dvd_id="SSIS-002", cover_url="https://example.com/c.jpg")
        db.session.add(title)
        db.session.commit()
        asset = Asset(title_id=title.id, kind="still", idx=1,
                      url="https://example.com/s.jpg")
        db.session.add(asset)
        db.session.commit()
        title_id, asset_id = title.id, asset.id

    monkeypatch.setattr(
        assets,
        "_fetch_image",
        lambda *a, **k: assets._FetchResult(False, reason="DMM 占位图 now_printing",
                                           placeholder=True),
    )
    with app.app_context():
        stats = assets.download_for_titles(app, limit=1)
        assert stats.failed >= 1
        title = db.session.get(Title, title_id)
        asset = db.session.get(Asset, asset_id)
        assert title.cover_failed_at is not None
        assert asset.failed_at is not None

        # 失败标记生效后：不再算作自动轮询的缺口
        summary = assets.missing_assets_summary(app)
        assert summary["covers_with_url"] == 0
        assert summary["stills"] == 0
        assert summary["actionable"] == 0
        assert jobs.maybe_auto_assets(app, force=True) is None


def test_auto_materialize_after_scan_setting(app):
    from app.db import db
    from app.models import Job
    from app.services import jobs, settings

    with app.app_context():
        # 默认开启：无活动物化任务时入队
        job_id = jobs._maybe_enqueue_materialize(app)
        assert job_id is not None
        assert db.session.get(Job, job_id).kind == "materialize"

        # 已有物化任务：不再重复
        assert jobs._maybe_enqueue_materialize(app) is None

        # 关闭后不再入队
        settings.set_bool(settings.KEY_AUTO_MATERIALIZE, False)
        db.session.query(Job).filter(Job.kind == "materialize").delete()
        db.session.commit()
        assert jobs._maybe_enqueue_materialize(app) is None


def test_recover_stale_running_jobs(app):
    from app.db import db
    from app.models import Job
    from app.services import jobs

    with app.app_context():
        row = Job(kind="materialize", payload="{}", status="running", progress=10)
        db.session.add(row)
        db.session.commit()
        assert jobs.recover_stale_jobs(app) == 1
        db.session.refresh(row)
        assert row.status == "failed"
        assert "中断" in (row.error or "")


def test_auto_poll_queues_behind_other_job_without_jumping(app):
    from app.db import db
    from app.models import Job
    from app.services import jobs

    _add_missing_title(app)
    with app.app_context():
        user_job = Job(kind="materialize", payload="{}", status="pending")
        db.session.add(user_job)
        db.session.commit()
        auto_id = jobs.maybe_auto_assets(app, force=True)
        assert auto_id is not None
        assert auto_id > user_job.id


def test_start_worker_threads_alive_and_stop(app):
    from app.services import jobs

    old_started, old_threads, old_stop = jobs._started, jobs._threads, jobs._stop
    try:
        jobs._started = False
        jobs._threads = []
        jobs._stop = __import__("threading").Event()
        app.config["WORKER_THREADS"] = 2
        app.config["WORKER_ENABLED"] = True
        jobs.start_worker(app)
        import time

        time.sleep(0.2)
        assert jobs.worker_status()["alive"] is True
        assert len(jobs._threads) == 2
        assert all(thread.is_alive() for thread in jobs._threads)
    finally:
        jobs.stop_worker()
        jobs._started = old_started
        jobs._threads = old_threads
        jobs._stop = old_stop


def test_auto_poll_runs_alongside_frame_job(app):
    from app.db import db
    from app.models import Job
    from app.services import jobs

    _add_missing_title(app)
    with app.app_context():
        db.session.add(Job(kind="video_frames_batch", payload="{}", status="running"))
        db.session.commit()
        assert jobs.maybe_auto_assets(app, force=True) is not None
