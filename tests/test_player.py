"""正在播放页面/源文件 Range 测试（使用临时 NAS 视频）。"""
from pathlib import Path

from app import create_app
from app.db import db
from app.models import LibraryFile, LibraryVolume, Title


def _app(tmp_path):
    return create_app({
        "TESTING": True,
        "DATA_DIR": tmp_path / "data",
        "DB_PATH": tmp_path / "data" / "test.db",
        "WORKER_ENABLED": False,
    })


def test_player_page_and_range(tmp_path):
    app = _app(tmp_path)
    nas = tmp_path / "nas"
    nas.mkdir()
    (nas / "ABP-001.mp4").write_bytes(b"0123456789" * 100)
    with app.app_context():
        volume = LibraryVolume(kind="nas", ref=str(nas))
        db.session.add(volume); db.session.flush()
        title = Title(dvd_id="ABP-001", title_ja="测试影片")
        db.session.add(title); db.session.flush()
        item = LibraryFile(volume_id=volume.id, rel_path="ABP-001.mp4",
                           name="ABP-001.mp4", size=1000, title_id=title.id)
        db.session.add(item); db.session.commit()
        file_id, title_id = item.id, title.id

    client = app.test_client()
    page = client.get(f"/client-player?title_id={title_id}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "正在播放" in html
    assert "ABP-001" in html
    source = client.get(f"/source/{file_id}", headers={"Range": "bytes=0-9"})
    assert source.status_code == 206
    assert source.data == b"0123456789"
    assert source.headers.get("Content-Range")


def test_player_title_filter_and_detail_button(tmp_path):
    app = _app(tmp_path)
    nas = tmp_path / "nas"
    nas.mkdir()
    (nas / "MULTI-001-part1.mp4").write_bytes(b"a" * 100)
    (nas / "MULTI-001-part2.mp4").write_bytes(b"b" * 100)
    with app.app_context():
        volume = LibraryVolume(kind="nas", ref=str(nas))
        db.session.add(volume); db.session.flush()
        title = Title(dvd_id="MULTI-001", title_ja="多文件影片")
        db.session.add(title); db.session.flush()
        first = LibraryFile(volume_id=volume.id, rel_path="MULTI-001-part1.mp4",
                            name="MULTI-001-part1.mp4", size=100, title_id=title.id)
        second = LibraryFile(volume_id=volume.id, rel_path="MULTI-001-part2.mp4",
                             name="MULTI-001-part2.mp4", size=100, title_id=title.id)
        db.session.add_all([first, second]); db.session.commit()
        title_id, first_id, second_id = title.id, first.id, second.id

    client = app.test_client()
    html = client.get(f"/client-player?title_id={title_id}").get_data(as_text=True)
    assert "MULTI-001" in html
    assert html.count("data-file-id=") == 3  # 播放器当前文件 + 列表两个文件
    assert f'data-file-id="{first_id}"' in html
    assert f'data-file-id="{second_id}"' in html

    detail = client.get("/library/MULTI-001").get_data(as_text=True)
    assert "播放影片" in detail
    assert 'id="playMovieBtn"' in detail


def test_playback_check_playable_unsupported_and_tape(tmp_path):
    app = _app(tmp_path)
    nas = tmp_path / "nas"
    nas.mkdir()
    (nas / "GOOD-001.mp4").write_bytes(b"fake")
    (nas / "BAD-001.mkv").write_bytes(b"fake")
    tape_dir = tmp_path / "tape"
    tape_dir.mkdir()
    with app.app_context():
        nas_volume = LibraryVolume(kind="nas", ref=str(nas))
        tape_volume = LibraryVolume(kind="tape", ref="FB9999")
        db.session.add_all([nas_volume, tape_volume]); db.session.flush()
        for code, volume, name in (
            ("GOOD-001", nas_volume, "GOOD-001.mp4"),
            ("BAD-001", nas_volume, "BAD-001.mkv"),
            ("TAPE-001", tape_volume, "TAPE-001.mp4"),
        ):
            title = Title(dvd_id=code)
            db.session.add(title); db.session.flush()
            db.session.add(LibraryFile(volume_id=volume.id, rel_path=name,
                                       name=name, size=4, title_id=title.id))
        db.session.commit()
        ids = {code: db.session.query(Title).filter_by(dvd_id=code).one().id
               for code in ("GOOD-001", "BAD-001", "TAPE-001")}

    client = app.test_client()
    good = client.get(f"/api/titles/{ids['GOOD-001']}/playback").get_json()
    assert good["status"] == "playable"
    assert "title_id=" in good["url"]
    bad = client.get(f"/api/titles/{ids['BAD-001']}/playback").get_json()
    assert bad["status"] == "playable"
    assert "client_player" in bad["url"] or "client-player" in bad["url"]
    tape = client.get(f"/api/titles/{ids['TAPE-001']}/playback").get_json()
    assert tape["status"] == "tape"
    assert "FB9999" in tape["message"]


def test_recent_play_is_per_title_and_limited(tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        title_a = Title(dvd_id="REC-001")
        title_b = Title(dvd_id="REC-002")
        db.session.add_all([title_a, title_b]); db.session.commit()
        a_id, b_id = title_a.id, title_b.id
    client = app.test_client()
    assert client.post("/api/player/recent", json={"title_id": a_id}).status_code == 200
    assert client.post("/api/player/recent", json={"title_id": a_id}).status_code == 200
    assert client.post("/api/player/recent", json={"title_id": b_id}).status_code == 200
    items = client.get("/api/player/recent").get_json()["items"]
    assert len(items) == 2
    assert items[0]["dvd_id"] == "REC-002"
    assert items[1]["play_count"] == 2
    assert all("file_id" not in item for item in items)


def test_player_has_recent_tab(tmp_path):
    app = _app(tmp_path)
    html = app.test_client().get("/client-player").get_data(as_text=True)
    assert "最近播放" in html


def test_player_playlist_has_no_header_or_close(tmp_path):
    app = _app(tmp_path)
    html = app.test_client().get("/client-player").get_data(as_text=True)
    assert 'id="playlistClose"' not in html
    assert "playlist-head" not in html
    assert "个视频</span>" not in html


def test_client_player_page(tmp_path):
    app = _app(tmp_path)
    nas = tmp_path / "nas"; nas.mkdir(); (nas / "C-001.mkv").write_bytes(b"x")
    with app.app_context():
        volume = LibraryVolume(kind="nas", ref=str(nas))
        db.session.add(volume); db.session.flush()
        title = Title(dvd_id="C-001")
        db.session.add(title); db.session.flush()
        db.session.add(LibraryFile(volume_id=volume.id, rel_path="C-001.mkv",
                                   name="C-001.mkv", size=1, title_id=title.id))
        db.session.commit()
    with app.app_context():
        title_id = db.session.query(Title).filter_by(dvd_id="C-001").one().id
    html = app.test_client().get(f"/client-player?title_id={title_id}").get_data(as_text=True)
    assert "movi-player" in html
    assert "element.slim.global.js" in html
    assert "客户端解码" in html
    # 直接访问导航不应加载全库或自动选片
    empty = app.test_client().get("/client-player").get_data(as_text=True)
    assert "data-file-id=" not in empty
    assert "还没有选择影片" in empty


def test_legacy_playing_redirects_to_client(tmp_path):
    app = _app(tmp_path)
    response = app.test_client().get("/playing?title_id=123")
    assert response.status_code == 302
    assert "/client-player" in response.headers["Location"]
