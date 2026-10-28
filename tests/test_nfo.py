"""NFO 元数据解析与优先应用测试。"""
from pathlib import Path

from app import create_app
from app.db import db
from app.models import LibraryFile, LibraryVolume, Title
from app.services import nfo


def _app(tmp_path):
    return create_app({
        "TESTING": True,
        "DATA_DIR": tmp_path / "data",
        "DB_PATH": tmp_path / "data" / "test.db",
        "WORKER_ENABLED": False,
    })


def test_parse_nfo_kodi_fields(tmp_path):
    path = tmp_path / "movie.nfo"
    path.write_text('''<?xml version="1.0" encoding="UTF-8"?>
<movie>
  <title>テスト作品</title>
  <originaltitle>Original Title</originaltitle>
  <num>ABP-536</num>
  <premiered>2024-03-15</premiered>
  <runtime>120</runtime>
  <rating>8.5</rating>
  <studio>テスト片商</studio>
  <director>監督A</director>
  <genre>単体作品</genre>
  <genre>巨乳</genre>
  <actor><name>女優A</name><role>female</role></actor>
</movie>''', encoding="utf-8")
    data = nfo.parse_nfo(path)
    assert data.code == "ABP-536"
    assert data.title == "テスト作品"
    assert data.release_date.isoformat() == "2024-03-15"
    assert data.runtime_mins == 120
    assert data.rating == 8.5
    assert data.studio == "テスト片商"
    assert data.directors == ["監督A"]
    assert data.genres == ["単体作品", "巨乳"]
    assert data.actresses == [("女優A", "female")]


def test_apply_nfo_creates_locked_title_and_links(tmp_path):
    app = _app(tmp_path)
    nas = tmp_path / "nas" / "ABP-536"
    nas.mkdir(parents=True)
    (nas / "ABP-536.mp4").write_bytes(b"video")
    (nas / "movie.nfo").write_text('''<movie>
      <title>NFO 标题</title><num>ABP-536</num><year>2024</year>
      <studio>NFO 片商</studio><genre>NFO 题材</genre>
      <actor><name>NFO 女優</name></actor>
    </movie>''', encoding="utf-8")
    with app.app_context():
        volume = LibraryVolume(kind="nas", ref=str(tmp_path / "nas"))
        db.session.add(volume); db.session.flush()
        item = LibraryFile(volume_id=volume.id, rel_path="ABP-536/ABP-536.mp4",
                           name="ABP-536.mp4", size=5, code_guess="ABP-536")
        db.session.add(item); db.session.commit()
        result = nfo.apply_nfo_for_file(app, item)
        assert result["ok"] is True
        db.session.refresh(item)
        title = db.session.get(Title, item.title_id)
        assert title.dvd_id == "ABP-536"
        assert title.title_ja == "NFO 标题"
        assert title.manual_lock is True
        assert title.rating_source is None
        assert title.maker is not None and title.maker.name_ja == "NFO 片商"
        assert title.genres.count() == 1
        assert title.actresses.count() == 1
