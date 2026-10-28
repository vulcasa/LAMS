"""活动页「正在播放」会话测试。"""
from app import create_app
from app.db import db
from app.models import LibraryFile, LibraryVolume, Title


def test_playback_session_appears_in_activity(tmp_path):
    app = create_app({"TESTING": True, "WORKER_ENABLED": False,
                      "DATA_DIR": tmp_path / "data", "DB_PATH": tmp_path / "data" / "test.db"})
    nas = tmp_path / "nas"; nas.mkdir(); (nas / "PLAY-001.mp4").write_bytes(b"x")
    with app.app_context():
        volume = LibraryVolume(kind="nas", ref=str(nas))
        db.session.add(volume); db.session.flush()
        title = Title(dvd_id="PLAY-001", title_ja="正在播放测试")
        db.session.add(title); db.session.flush()
        item = LibraryFile(volume_id=volume.id, rel_path="PLAY-001.mp4",
                           name="PLAY-001.mp4", size=1, title_id=title.id)
        db.session.add(item); db.session.commit()
        title_id, file_id = title.id, item.id

    client = app.test_client()
    response = client.post("/api/player/session", json={
        "session_id": "test-session",
        "title_id": title_id,
        "file_id": file_id,
        "position": 120,
        "duration": 600,
        "paused": False,
    })
    assert response.status_code == 200
    activity = client.get("/api/activity").get_json()
    playing = activity["playing"]
    assert len(playing) == 1
    assert playing[0]["dvd_id"] == "PLAY-001"
    assert playing[0]["file_name"] == "PLAY-001.mp4"
    assert playing[0]["percent"] == 20.0
    assert playing[0]["ip"]


def test_playback_session_allowed_on_mobile(tmp_path):
    app = create_app({"TESTING": True, "WORKER_ENABLED": False,
                      "DATA_DIR": tmp_path / "data", "DB_PATH": tmp_path / "data" / "test.db"})
    with app.app_context():
        title = Title(dvd_id="MOBILE-001")
        db.session.add(title); db.session.flush()
        volume = LibraryVolume(kind="nas", ref=str(tmp_path))
        db.session.add(volume); db.session.flush()
        item = LibraryFile(volume_id=volume.id, rel_path="x.mp4", name="x.mp4",
                           size=1, title_id=title.id)
        db.session.add(item); db.session.commit()
        title_id, file_id = title.id, item.id
    ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148"
    response = app.test_client().post("/api/player/session", json={
        "session_id": "m", "title_id": title_id, "file_id": file_id,
        "position": 0, "duration": 0, "paused": True,
    }, headers={"User-Agent": ua})
    assert response.status_code == 200
