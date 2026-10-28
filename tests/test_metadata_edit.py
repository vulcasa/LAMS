"""详情页逐字段编辑（Plex 风格解锁编辑）后端测试。"""
from datetime import date

import pytest

from app import create_app
from app.db import db
from app.models import Actress, Genre, Maker, Title, TitleActress, TitleGenre


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path / "data",
            "DB_PATH": tmp_path / "data" / "test.db",
            "WORKER_ENABLED": False,
            "LTFS_MOUNT": str(tmp_path / "tape"),
        }
    )
    (tmp_path / "tape").mkdir()
    with app.app_context():
        db.session.add(Title(dvd_id="SSIS-001", title_ja="旧标题"))
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()


@pytest.fixture()
def client(app):
    return app.test_client()


def _title(app):
    with app.app_context():
        return db.session.query(Title).filter_by(dvd_id="SSIS-001").one()


def _patch(client, title_id, field, value):
    return client.patch(
        f"/api/titles/{title_id}/metadata", json={"field": field, "value": value}
    )


def test_detail_has_lock_buttons(client):
    title = _title(client.application)
    html = client.get("/library/SSIS-001").get_data(as_text=True)
    assert 'class="lock-btn"' in html
    assert 'data-field="title_zh"' in html
    assert 'data-field="actresses"' in html and 'data-field="genres"' in html


def test_edit_text_date_runtime_rating(client, app):
    title = _title(app)
    for field, value in (
        ("title_zh", "新中文标题"),
        ("release_date", "2024-03-05"),
        ("runtime_mins", "125"),
        ("rating", "4.35"),
    ):
        response = _patch(client, title.id, field, value)
        assert response.status_code == 200, response.get_data(as_text=True)
    with app.app_context():
        row = db.session.get(Title, title.id)
        assert row.title_zh == "新中文标题"
        assert row.release_date == date(2024, 3, 5)
        assert row.runtime_mins == 125
        assert row.rating == 4.35
        assert row.manual_lock is True


def test_edit_relations_creates_and_links(client, app):
    title = _title(app)
    for field, value in (
        ("maker", "新片商"),
        ("label", "新厂牌"),
        ("series", "新系列"),
        ("director", "新导演"),
    ):
        assert _patch(client, title.id, field, value).status_code == 200
    assert _patch(client, title.id, "actresses", "演员甲、演员乙").status_code == 200
    assert _patch(client, title.id, "genres", "巨乳、制服").status_code == 200

    with app.app_context():
        row = db.session.get(Title, title.id)
        assert row.maker.name_ja == "新片商"
        assert row.label.name_ja == "新厂牌"
        assert row.series.name_ja == "新系列"
        assert row.director.name_kanji == "新导演"
        names = [
            a.display_name()
            for a in Actress.query.join(TitleActress, TitleActress.actress_id == Actress.id)
            .filter(TitleActress.title_id == row.id, TitleActress.role == "female")
            .all()
        ]
        assert names == ["演员甲", "演员乙"]
        genres = [
            g.display_name()
            for g in Genre.query.join(TitleGenre, TitleGenre.genre_id == Genre.id)
            .filter(TitleGenre.title_id == row.id)
            .all()
        ]
        assert genres == ["巨乳", "制服"]


def test_edit_reuses_existing_relation(client, app):
    with app.app_context():
        maker = Maker(name_ja="已有片商")
        db.session.add(maker)
        db.session.commit()
        maker_id = maker.id
        title = db.session.query(Title).filter_by(dvd_id="SSIS-001").one()
        title_id = title.id
    assert _patch(client, title_id, "maker", "已有片商").status_code == 200
    with app.app_context():
        assert db.session.get(Title, title_id).maker_id == maker_id
        assert Maker.query.count() == 1  # 没有重复创建


def test_invalid_values_rejected(client, app):
    title = _title(app)
    assert _patch(client, title.id, "release_date", "2024-13-40").status_code == 400
    assert _patch(client, title.id, "rating", "99").status_code == 400
    assert _patch(client, title.id, "runtime_mins", "-5").status_code == 400
    assert _patch(client, title.id, "nope", "x").status_code == 400


def test_import_url_row_is_locked_by_default(client):
    html = client.get("/library/SSIS-001").get_data(as_text=True)
    assert 'id="meta-url"' in html and "disabled" in html
    assert 'id="import-lock"' in html
    assert "unlockImport()" in html


def test_edit_history_and_undo(client, app):
    title = _title(app)
    assert _patch(client, title.id, "title_zh", "第一次修改").status_code == 200
    assert _patch(client, title.id, "title_zh", "第二次修改").status_code == 200

    history = client.get(f"/api/titles/{title.id}/history").get_json()["history"]
    assert len(history) == 2
    latest = history[0]["id"]

    r = client.post(f"/api/titles/{title.id}/history/{latest}/undo")
    assert r.status_code == 200
    with app.app_context():
        row = db.session.get(Title, title.id)
        assert row.title_zh == "第一次修改"
        assert row.manual_lock is True
