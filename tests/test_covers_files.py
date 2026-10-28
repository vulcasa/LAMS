"""封面缺失筛选/补齐入口、文件页 UX 的回归测试。"""
from pathlib import Path

import pytest

from io import BytesIO

from PIL import Image

from app import create_app
from app.db import db
from app.models import Asset, Title


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path / "data",
            "DB_PATH": tmp_path / "data" / "test.db",
            "WORKER_ENABLED": False,
            "LTFS_MOUNT": str(tmp_path / "tape"),
            "BROWSE_ROOTS": [str(tmp_path)],
        }
    )
    (tmp_path / "tape").mkdir()
    with app.app_context():
        db.session.add(Title(dvd_id="SSIS-001", title_ja="x", cover_local="covers/SSIS-001.jpg"))
        db.session.add(Title(dvd_id="SSIS-002", title_ja="y"))  # 缺封面
        db.session.commit()
        title = Title.query.filter_by(dvd_id="SSIS-001").one()
        db.session.add_all(
            [
                Asset(title_id=title.id, kind="still", idx=1,
                      local_path="stills/SSIS-001/01.jpg"),
                Asset(title_id=title.id, kind="still", idx=2,
                      local_path="stills/SSIS-001/02.jpg"),
            ]
        )
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()


@pytest.fixture()
def client(app):
    return app.test_client()


def test_library_cover_filter(client):
    html = client.get("/library?has_cover=0").get_data(as_text=True)
    assert "SSIS-002" in html and "SSIS-001" not in html
    html = client.get("/library?has_cover=1").get_data(as_text=True)
    assert "SSIS-001" in html and "SSIS-002" not in html
    html = client.get("/library").get_data(as_text=True)
    assert "缺封面" in html


def test_cover_fill_api_enqueues(client):
    r = client.post("/api/covers/fill", json={"limit": 5})
    assert r.status_code == 202
    assert r.get_json()["job_id"] > 0


def test_detail_page_shows_stills_above_files(client, app):
    html = client.get("/library/SSIS-001").get_data(as_text=True)
    assert "剧照（2）" in html
    assert "stills/SSIS-001/01.jpg" in html
    # 剧照区块必须在「文件位置」之前
    assert html.index("stills-grid") < html.index("文件位置")


def test_files_page_has_root_selector_and_refresh(client):
    html = client.get("/files").get_data(as_text=True)
    assert 'id="c-root"' in html
    assert "刷新磁带" in html


def test_javdb_cover_parse():
    from app.services.javdb import parse_detail_html

    html = """
    <html><body>
      <h2 class="title"><span class="current-title">中文标题</span></h2>
      <div class="panel-block"><strong>番號:</strong><span class="value">SSIS-001</span></div>
      <img class="video-cover" src="//c0.jdbstatic.com/covers/abc.jpg">
    </body></html>
    """
    detail = parse_detail_html(html, code="SSIS-001")
    assert detail is not None
    assert detail.cover_url == "https://c0.jdbstatic.com/covers/abc.jpg"


def _png_bytes(color=(200, 30, 30), size=(12, 12)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def test_upload_cover_and_stills(client, app):
    with app.app_context():
        title_id = Title.query.filter_by(dvd_id="SSIS-001").one().id

    r = client.post(
        f"/api/titles/{title_id}/cover",
        data={"file": (BytesIO(_png_bytes()), "my-cover.png")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] is True
    with app.app_context():
        title = db.session.get(Title, title_id)
        assert title.cover_local == "covers/SSIS-001.jpg"
        assert (Path(app.config["COVER_DIR"]) / "SSIS-001.jpg").is_file()

    r = client.post(
        f"/api/titles/{title_id}/stills",
        data={
            "files": [
                (BytesIO(_png_bytes((10, 200, 10))), "s1.png"),
                (BytesIO(_png_bytes((10, 10, 200))), "s2.png"),
            ]
        },
        content_type="multipart/form-data",
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["count"] == 2
    with app.app_context():
        stills = Asset.query.filter_by(title_id=title_id, kind="still").all()
        assert len(stills) == 4  # fixture 已有 2 张
        assert (Path(app.config["STILL_DIR"]) / "SSIS-001" / "03.jpg").is_file()
        assert (Path(app.config["STILL_DIR"]) / "SSIS-001" / "04.jpg").is_file()


def test_upload_rejects_non_image(client, app):
    with app.app_context():
        title_id = Title.query.filter_by(dvd_id="SSIS-001").one().id
    r = client.post(
        f"/api/titles/{title_id}/cover",
        data={"file": (BytesIO(b"not an image"), "evil.png")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 400
    assert "无法识别" in r.get_json()["error"]


def test_detail_cover_and_stills_use_lightbox(client):
    html = client.get("/library/SSIS-001").get_data(as_text=True)
    assert 'id="lightbox"' in html
    assert "openLightbox(" in html
    assert 'class="lightbox-item' in html
    assert "cover-lightbox" in html
    assert 'target="_blank"' not in html
