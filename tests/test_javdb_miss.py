"""JavDB 未命中标记：后台记录、冷却期内不重复刮削。"""
from datetime import timedelta

import pytest

from app import create_app
from app.db import db
from app.models import Title, utcnow
from app.services import javdb


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path / "data",
            "DB_PATH": tmp_path / "data" / "test.db",
            "WORKER_ENABLED": False,
            "JAVDB_RATE_SECONDS": 0,
        }
    )
    with app.app_context():
        db.session.add(Title(dvd_id="NMSL-019"))
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()


def test_miss_is_marked_and_skipped_on_next_run(app, monkeypatch):
    monkeypatch.setattr(javdb, "_fetch_impl", lambda *a, **k: None)
    with app.app_context():
        first = javdb.fill_gaps(app, limit=10, rate=0)
        assert first["considered"] == 1
        assert first["no_result"] == 1
        assert first["miss_marked"] == 1
        title = db.session.query(Title).filter_by(dvd_id="NMSL-019").one()
        assert title.javdb_miss_at is not None
        assert title.javdb_miss_count == 1

        # 第二次：冷却期内直接跳过，不再发请求
        second = javdb.fill_gaps(app, limit=10, rate=0)
        assert second["considered"] == 0
        assert second["no_result"] == 0


def test_miss_marker_expires_after_retry_days(app, monkeypatch):
    monkeypatch.setattr(javdb, "_fetch_impl", lambda *a, **k: None)
    with app.app_context():
        javdb.fill_gaps(app, limit=10, rate=0)
        title = db.session.query(Title).filter_by(dvd_id="NMSL-019").one()
        title.javdb_miss_at = utcnow() - timedelta(days=31)
        db.session.commit()

        def hit(code, **kwargs):
            return javdb.JavdbDetail(code=code, url="", title="命中")

        monkeypatch.setattr(javdb, "_fetch_impl", hit)
        stats = javdb.fill_gaps(app, limit=10, rate=0)
        assert stats["considered"] == 1
        assert stats["filled"] == 1
        title = db.session.query(Title).filter_by(dvd_id="NMSL-019").one()
        assert title.javdb_miss_at is None
        assert not title.javdb_miss_count


def test_explicit_only_codes_bypasses_miss_marker(app, monkeypatch):
    monkeypatch.setattr(javdb, "_fetch_impl", lambda *a, **k: None)
    with app.app_context():
        javdb.fill_gaps(app, limit=10, rate=0)  # 先标记未命中
        retry = javdb.fill_gaps(app, limit=10, rate=0, only_codes=["NMSL-019"])
        assert retry["considered"] == 1  # 人工指定番号仍然强制重试
