"""收藏星标与片库筛选测试。"""
from app import create_app
from app.db import db
from app.models import Title


def _app(tmp_path):
    return create_app({"TESTING": True, "WORKER_ENABLED": False,
                       "DATA_DIR": tmp_path / "data", "DB_PATH": tmp_path / "data" / "test.db"})


def test_toggle_favorite_and_filter(tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        first = Title(dvd_id="FAV-001", title_ja="收藏测试一")
        second = Title(dvd_id="FAV-002", title_ja="收藏测试二")
        db.session.add_all([first, second]); db.session.commit()
        first_id, second_id = first.id, second.id

    client = app.test_client()
    response = client.post(f"/api/titles/{first_id}/favorite")
    assert response.status_code == 200
    assert response.get_json()["favorite"] is True

    html = client.get("/library?favorite=1").get_data(as_text=True)
    assert "FAV-001" in html
    assert "FAV-002" not in html
    html = client.get("/library?favorite=0").get_data(as_text=True)
    assert "FAV-002" in html
    assert "FAV-001" not in html

    detail = client.get("/library/FAV-001").get_data(as_text=True)
    assert 'id="favoriteBtn"' in detail
    assert 'class="favorite-star active"' in detail
