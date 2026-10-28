"""官方页面 URL 导入元数据测试（解析 + 写回）。"""
import pytest

from app import create_app
from app.db import db
from app.models import Title
from app.services.url_meta import fc2_article_url, parse_url_metadata


def test_parse_opengraph_and_jsonld():
    html = """
    <html><head>
      <meta property="og:title" content="Scene Title">
      <meta property="og:image" content="//cdn.example.com/cover.jpg">
      <meta property="article:published_time" content="2024-05-06T12:00:00Z">
    </head><body></body></html>
    """
    data = parse_url_metadata(html, "https://studio.example/scene/1")
    assert data["title"] == "Scene Title"
    assert data["image"] == "https://cdn.example.com/cover.jpg"
    assert data["date"] == "2024-05-06"
    assert data["site"] == "studio.example"


def test_fc2_article_url():
    assert fc2_article_url("FC2-PPV-1234567") == (
        "https://adult.contents.fc2.com/article/1234567/"
    )
    assert fc2_article_url("SSIS-001") is None


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path / "data",
            "DB_PATH": tmp_path / "data" / "test.db",
            "WORKER_ENABLED": False,
        }
    )
    with app.app_context():
        db.session.add(Title(dvd_id="FC2-PPV-1234567"))
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()


def test_import_url_updates_title(app, monkeypatch):
    from app.services import url_meta

    with app.app_context():
        title_id = db.session.query(Title).one().id

    monkeypatch.setattr(
        url_meta,
        "fetch_url_metadata",
        lambda *a, **k: {
            "ok": True,
            "title": "FC2 作品标题",
            "image": None,
            "date": "2024-06-01",
            "url": "https://adult.contents.fc2.com/article/1234567/",
        },
    )
    response = app.test_client().post(
        f"/api/titles/{title_id}/import-url",
        json={"url": "https://adult.contents.fc2.com/article/1234567/"},
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    with app.app_context():
        title = db.session.get(Title, title_id)
        assert title.title_zh == "FC2 作品标题"
        assert title.release_date.isoformat() == "2024-06-01"
        assert title.manual_lock is True
