"""移动端访问限制与片库移动布局测试。"""
from app import create_app
from app.db import db

MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"


def _app(tmp_path):
    return create_app({"TESTING": True, "WORKER_ENABLED": False,
                       "DATA_DIR": tmp_path / "data", "DB_PATH": tmp_path / "data" / "test.db"})


def test_mobile_only_library_and_player(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    headers = {"User-Agent": MOBILE_UA}
    assert client.get("/library", headers=headers).status_code == 200
    assert client.get("/client-player", headers=headers).status_code == 200
    assert client.get("/tapes", headers=headers).status_code == 403
    assert client.get("/activity", headers=headers).status_code == 403
    assert client.get("/api/jobs", headers=headers).status_code == 403
    blocked = client.get("/activity", headers=headers).get_data(as_text=True)
    assert "移动端仅开放「片库」和「正在播放」" in blocked
    library = client.get("/library", headers=headers).get_data(as_text=True)
    assert "pages.activity" not in library


def test_desktop_nav_unchanged(tmp_path):
    app = _app(tmp_path)
    html = app.test_client().get("/library").get_data(as_text=True)
    assert "磁带" in html and "活动" in html and "设置" in html


def test_mobile_library_has_filter_toggle(tmp_path):
    app = _app(tmp_path)
    html = app.test_client().get("/library", headers={"User-Agent": MOBILE_UA}).get_data(as_text=True)
    assert 'id="mobileFilterToggle"' in html


def test_mobile_can_queue_video_frames_from_detail(tmp_path):
    from app.models import Title
    app = _app(tmp_path)
    with app.app_context():
        title = Title(dvd_id="MOBILE-FRAME-001")
        db.session.add(title); db.session.commit()
        title_id = title.id
    response = app.test_client().post(
        f"/api/titles/{title_id}/video-frames",
        headers={"User-Agent": MOBILE_UA},
    )
    assert response.status_code == 202
