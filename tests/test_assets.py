"""封面 / 剧照 / 缩略图下载器的测试（**全部 mock，不联网**）。

用 ``FakeSession`` 注入 ``app.services.assets._make_session``，覆盖：

1. 封面 + 剧照下载、缩略图生成（宽 400）、Title.cover_local / Asset.local_path /
   Asset.bytes 回写、字节统计；
2. 幂等：第二次全部跳过、不再发请求、文件不变；
3. ``force=True`` 重新下载；
4. DMM 老片 302 到 ``now_printing.jpg`` 占位图 → 视为缺失（不落盘、不回写）；
5. 404 / 超时 / 非图片 Content-Type → 记 failed 不抛，坏的不影响好的；
6. 已有本地文件（手工放入）→ 认领进 DB、不发请求；缩略图缺失时用本地封面补生成；
7. ``only_codes`` / ``limit`` 过滤；
8. CLI ``scripts.download_assets.main`` 的 0 条与主库缺失分支。

跑法（项目根 = av-archive/）::

    .venv/bin/python -m pytest tests/test_assets.py -q
"""
from __future__ import annotations

import sys
from io import BytesIO
from pathlib import Path

import pytest
import requests
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.db import db  # noqa: E402
from app.models import Asset, Title  # noqa: E402
from app.services import assets  # noqa: E402
from app.services.assets import download_for_titles, make_thumbnail  # noqa: E402


# ---------------------------------------------------------------------------
# fake HTTP
# ---------------------------------------------------------------------------
def _jpeg_bytes(width: int = 800, height: int = 1000,
                color=(90, 40, 60), quality: int = 90) -> bytes:
    image = Image.new("RGB", (width, height), color)
    buf = BytesIO()
    image.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


class FakeResponse:
    def __init__(self, url, status_code=200, content=b"", headers=None,
                 content_type="image/jpeg"):
        self.url = url
        self.status_code = status_code
        self.content = content
        self.headers = dict(headers) if headers is not None else {
            "Content-Type": content_type
        }


class FakeSession:
    """按 URL 路由响应的 ``requests.Session`` 替身。

    route 值可以是 :class:`FakeResponse`、异常实例（``requests.Timeout()``）
    或 ``callable(url) -> FakeResponse``。
    """

    def __init__(self):
        self.routes: dict = {}
        self.calls: list[str] = []

    def get(self, url, timeout=None, **kwargs):  # noqa: ANN001, ARG002
        self.calls.append(url)
        handler = self.routes.get(url)
        if handler is None:
            return FakeResponse(url, 404, b"", content_type="text/html")
        if isinstance(handler, BaseException):
            raise handler
        if callable(handler):
            return handler(url)
        return handler

    def close(self):  # pragma: no cover - 测试替身
        pass


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "DATA_DIR": tmp_path,
            "DB_PATH": tmp_path / "avlib.db",
            "MEDIA_DIR": tmp_path / "media",
            "COVER_DIR": tmp_path / "media" / "covers",
            "STILL_DIR": tmp_path / "media" / "stills",
            "AVATAR_DIR": tmp_path / "media" / "avatars",
            "SUBTITLE_DIR": tmp_path / "media" / "subtitles",
            "THUMB_DIR": tmp_path / "media" / "thumbs",
            "DUMP_DIR": tmp_path / "dump",
            "WORKER_ENABLED": False,
            # 测试调参：零限速、失败不重试、短超时
            "ASSET_WORKERS": 2,
            "ASSET_MIN_INTERVAL": 0,
            "ASSET_CONNECT_TIMEOUT": 1,
            "ASSET_READ_TIMEOUT": 1,
            "ASSET_RETRIES": 0,
        }
    )
    yield application
    with application.app_context():
        db.session.remove()


@pytest.fixture()
def fake_session(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(assets, "_make_session", lambda app: session)  # noqa: ARG005
    return session


def _media(app) -> Path:
    return Path(app.config["MEDIA_DIR"])


def _add_title(code: str, cover_url: str | None = None, **kwargs) -> Title:
    title = Title(dvd_id=code, cover_url=cover_url, **kwargs)
    db.session.add(title)
    db.session.flush()
    return title


def _add_stills(title: Title, urls) -> None:
    for idx, url in enumerate(urls, start=1):
        db.session.add(Asset(title_id=title.id, kind="still", idx=idx, url=url))


def _ok(url: str) -> FakeResponse:
    return FakeResponse(url, 200, _jpeg_bytes())


# ---------------------------------------------------------------------------
# 1. 成功：封面 + 剧照 + 缩略图 + DB 回写
# ---------------------------------------------------------------------------
def test_downloads_cover_stills_and_thumbnail(app, fake_session):
    with app.app_context():
        cover_url = "https://pics.test/digital/video/tst00001/tst00001pl.jpg"
        still_urls = [
            "https://pics.test/digital/video/tst00001/tst00001jp-1.jpg",
            "https://pics.test/digital/video/tst00001/tst00001jp-2.jpg",
        ]
        fake_session.routes[cover_url] = _ok(cover_url)
        for url in still_urls:
            fake_session.routes[url] = FakeResponse(url, 200, _jpeg_bytes(640, 480))
        title = _add_title("TST-001", cover_url=cover_url)
        _add_stills(title, still_urls)
        db.session.commit()

        stats = download_for_titles(app)

        assert stats.considered == 1
        assert stats.downloaded == 3
        assert stats.covers == 1
        assert stats.stills == 2
        assert stats.thumbs == 1
        assert stats.skipped == 0
        assert stats.failed == 0 and stats.errors == []
        assert stats.bytes > 0
        assert sorted(fake_session.calls) == sorted([cover_url, *still_urls])

        media = _media(app)
        cover_path = media / "covers" / "TST-001.jpg"
        thumb_path = media / "thumbs" / "TST-001_cover.jpg"
        still_paths = [media / "stills" / "TST-001" / f"0{i}.jpg" for i in (1, 2)]
        assert all(p.is_file() and p.stat().st_size > 0
                   for p in [cover_path, thumb_path, *still_paths])
        # 缩略图宽 400（800x1000 → 400x500），JPEG
        with Image.open(thumb_path) as image:
            assert image.size == (400, 500)
            assert image.format == "JPEG"
        # 字节统计 = 实际落盘大小（封面 + 剧照 + 缩略图）
        on_disk = cover_path.stat().st_size + thumb_path.stat().st_size + sum(
            p.stat().st_size for p in still_paths
        )
        assert stats.bytes == on_disk

        db.session.expire_all()
        reloaded = Title.query.filter_by(dvd_id="TST-001").one()
        assert reloaded.cover_local == "covers/TST-001.jpg"
        stills = Asset.query.order_by(Asset.idx).all()
        assert [a.local_path for a in stills] == [
            "stills/TST-001/01.jpg", "stills/TST-001/02.jpg",
        ]
        assert all(a.bytes and a.bytes > 0 for a in stills)


# ---------------------------------------------------------------------------
# 2. 幂等
# ---------------------------------------------------------------------------
def test_second_run_is_idempotent(app, fake_session):
    with app.app_context():
        cover_url = "https://pics.test/a/apl.jpg"
        still_url = "https://pics.test/a/ajp-1.jpg"
        fake_session.routes[cover_url] = _ok(cover_url)
        fake_session.routes[still_url] = _ok(still_url)
        title = _add_title("IDM-001", cover_url=cover_url)
        _add_stills(title, [still_url])
        db.session.commit()

        first = download_for_titles(app)
        assert first.downloaded == 2
        calls_after_first = len(fake_session.calls)
        media = _media(app)
        cover = media / "covers" / "IDM-001.jpg"
        thumb = media / "thumbs" / "IDM-001_cover.jpg"
        still = media / "stills" / "IDM-001" / "01.jpg"
        cover_bytes, still_bytes = cover.read_bytes(), still.read_bytes()

        second = download_for_titles(app)

        assert second.considered == 1
        assert second.downloaded == 0
        assert second.bytes == 0
        # 封面 + 剧照 + 缩略图 都跳过
        assert second.skipped == 3
        assert len(fake_session.calls) == calls_after_first  # 没再发请求
        assert cover.read_bytes() == cover_bytes
        assert still.read_bytes() == still_bytes
        assert thumb.is_file()


def test_force_redownloads_everything(app, fake_session):
    with app.app_context():
        cover_url = "https://pics.test/b/bpl.jpg"
        fake_session.routes[cover_url] = _ok(cover_url)
        title = _add_title("FRC-001", cover_url=cover_url)
        db.session.commit()

        download_for_titles(app)
        calls_before = len(fake_session.calls)
        stats = download_for_titles(app, force=True)

        assert stats.covers == 1
        assert stats.thumbs == 1
        assert stats.skipped == 0
        assert len(fake_session.calls) == calls_before + 1
        assert (_media(app) / "covers" / "FRC-001.jpg").is_file()


# ---------------------------------------------------------------------------
# 3. 占位图 / 404 / 超时 / 非图片
# ---------------------------------------------------------------------------
def test_now_printing_placeholder_is_missing(app, fake_session):
    with app.app_context():
        stale_url = "https://pics.dmm.co.jp/digital/video/mide00001/mide00001pl.jpg"
        placeholder_url = (
            "https://imgsrc.dmm.com/pics/mono/movie/n/now_printing/now_printing.jpg?h=800"
        )
        # 占位图也是合法 image/jpeg，只能靠最终 URL 判断
        fake_session.routes[stale_url] = FakeResponse(
            placeholder_url, 200, _jpeg_bytes(800, 800)
        )
        _add_title("MIDE-001", cover_url=stale_url)
        db.session.commit()

        stats = download_for_titles(app)

        assert stats.placeholders == 1
        assert stats.failed == 1
        assert stats.downloaded == 0
        assert stats.covers == 0
        assert not (_media(app) / "covers" / "MIDE-001.jpg").exists()
        assert not (_media(app) / "thumbs" / "MIDE-001_cover.jpg").exists()
        db.session.expire_all()
        assert Title.query.filter_by(dvd_id="MIDE-001").one().cover_local is None
        assert any("now_printing" in message for message in stats.errors)


def test_404_and_timeout_are_recorded_not_raised(app, fake_session):
    with app.app_context():
        missing_url = "https://pics.test/c/missing.jpg"
        slow_url = "https://pics.test/c/slow.jpg"
        ok_url = "https://pics.test/c/ok.jpg"
        fake_session.routes[missing_url] = FakeResponse(
            missing_url, 404, b"", content_type="text/html"
        )
        fake_session.routes[slow_url] = requests.Timeout("read timed out")
        fake_session.routes[ok_url] = _ok(ok_url)
        # 两张坏图 + 一张好图（坏图不影响好图）
        _add_title("BAD-404", cover_url=missing_url)
        _add_title("BAD-TIMEOUT", cover_url=slow_url)
        _add_title("GOOD-001", cover_url=ok_url)
        db.session.commit()

        stats = download_for_titles(app)

        assert stats.considered == 3
        assert stats.downloaded == 1
        assert stats.covers == 1
        assert stats.failed == 2
        assert len(stats.errors) == 2
        assert any("404" in message for message in stats.errors)
        assert any("超时" in message for message in stats.errors)
        assert (_media(app) / "covers" / "GOOD-001.jpg").is_file()
        assert not (_media(app) / "covers" / "BAD-404.jpg").exists()
        assert not (_media(app) / "covers" / "BAD-TIMEOUT.jpg").exists()


def test_non_image_content_type_is_missing(app, fake_session):
    with app.app_context():
        url = "https://pics.test/d/html.jpg"
        fake_session.routes[url] = FakeResponse(
            url, 200, b"<html>blocked</html>", content_type="text/html"
        )
        _add_title("HTML-001", cover_url=url)
        db.session.commit()

        stats = download_for_titles(app)

        assert stats.downloaded == 0
        assert stats.failed == 1
        assert stats.placeholders == 1
        assert not (_media(app) / "covers" / "HTML-001.jpg").exists()


# ---------------------------------------------------------------------------
# 4. 已有本地文件：认领 + 缩略图补生成
# ---------------------------------------------------------------------------
def test_existing_files_are_adopted_without_network(app, fake_session):
    with app.app_context():
        title = _add_title("LOC-001", cover_url="https://pics.test/e/epl.jpg")
        still_url = "https://pics.test/e/ejp-1.jpg"
        _add_stills(title, [still_url])
        db.session.commit()

        media = _media(app)
        cover_abs = media / "covers" / "LOC-001.jpg"
        still_abs = media / "stills" / "LOC-001" / "01.jpg"
        cover_abs.parent.mkdir(parents=True, exist_ok=True)
        still_abs.parent.mkdir(parents=True, exist_ok=True)
        cover_abs.write_bytes(_jpeg_bytes(600, 900))
        still_abs.write_bytes(_jpeg_bytes(640, 480))

        stats = download_for_titles(app)

        assert stats.downloaded == 0
        assert stats.skipped == 2          # 封面 + 剧照
        assert stats.thumbs == 1           # 缩略图缺失 → 用本地封面补生成
        assert fake_session.calls == []    # 完全没联网
        assert (_media(app) / "thumbs" / "LOC-001_cover.jpg").is_file()
        db.session.expire_all()
        reloaded = Title.query.filter_by(dvd_id="LOC-001").one()
        assert reloaded.cover_local == "covers/LOC-001.jpg"
        asset = Asset.query.one()
        assert asset.local_path == "stills/LOC-001/01.jpg"
        assert asset.bytes == still_abs.stat().st_size


# ---------------------------------------------------------------------------
# 5. only_codes / limit / progress
# ---------------------------------------------------------------------------
def test_only_codes_filter(app, fake_session):
    with app.app_context():
        urls = {}
        for code in ("AAA-001", "BBB-001", "CCC-001"):
            url = f"https://pics.test/{code}/pl.jpg"
            urls[code] = url
            fake_session.routes[url] = _ok(url)
            _add_title(code, cover_url=url)
        db.session.commit()

        filtered = download_for_titles(app, only_codes=["BBB-001"])

        assert filtered.considered == 1
        assert filtered.covers == 1
        assert fake_session.calls == [urls["BBB-001"]]
        assert not (_media(app) / "covers" / "AAA-001.jpg").exists()
        assert not (_media(app) / "covers" / "CCC-001.jpg").exists()


def test_limit_processes_pending_titles_batch_by_batch(app, fake_session):
    """UI「50 部/批」语义：limit 优先挑还有缺图的，连点几批能推进到全部完成。"""
    with app.app_context():
        for code in ("BAT-001", "BAT-002", "BAT-003"):
            url = f"https://pics.test/{code}/pl.jpg"
            fake_session.routes[url] = _ok(url)
            _add_title(code, cover_url=url)
        db.session.commit()

        first = download_for_titles(app, limit=1)
        second = download_for_titles(app, limit=1)
        third = download_for_titles(app, limit=1)
        fourth = download_for_titles(app, limit=1)

        assert [first.covers, second.covers, third.covers] == [1, 1, 1]
        assert {first.considered, second.considered, third.considered} == {1}
        assert fourth.considered == 0            # 全部完成，没有待办
        assert fourth.downloaded == 0
        media = _media(app)
        for code in ("BAT-001", "BAT-002", "BAT-003"):
            assert (media / "covers" / f"{code}.jpg").is_file()
            assert (media / "thumbs" / f"{code}_cover.jpg").is_file()


def test_progress_callback_reports_titles(app, fake_session):
    with app.app_context():
        for code in ("PRG-001", "PRG-002"):
            url = f"https://pics.test/{code}/pl.jpg"
            fake_session.routes[url] = _ok(url)
            _add_title(code, cover_url=url)
        db.session.commit()

        progress_calls = []
        download_for_titles(
            app, progress=lambda done, msg="": progress_calls.append((done, msg))
        )

        assert progress_calls
        assert progress_calls[-1][0] == 2
        assert progress_calls[-1][1].startswith("完成")


# ---------------------------------------------------------------------------
# 6. 纯剧照标题（无封面）+ 缩略图工具
# ---------------------------------------------------------------------------
def test_still_only_title_has_no_thumbnail(app, fake_session):
    with app.app_context():
        url = "https://pics.test/f/fjp-1.jpg"
        fake_session.routes[url] = _ok(url)
        title = _add_title("STL-001", cover_url=None)
        _add_stills(title, [url])
        db.session.commit()

        stats = download_for_titles(app)

        assert stats.downloaded == 1
        assert stats.stills == 1
        assert stats.covers == 0
        assert stats.thumbs == 0
        assert not (_media(app) / "thumbs" / "STL-001_cover.jpg").exists()
        db.session.expire_all()
        assert Title.query.filter_by(dvd_id="STL-001").one().cover_local is None


def test_make_thumbnail_reencodes_to_width_400():
    data = _jpeg_bytes(1000, 1500)
    thumb = make_thumbnail(data)
    with Image.open(BytesIO(thumb)) as image:
        assert image.size == (400, 600)
        assert image.format == "JPEG"


# ---------------------------------------------------------------------------
# 7. CLI
# ---------------------------------------------------------------------------
def _make_app(data_dir: Path):
    return create_app({
        "DATA_DIR": data_dir,
        "DB_PATH": data_dir / "avlib.db",
        "MEDIA_DIR": data_dir / "media",
        "COVER_DIR": data_dir / "media" / "covers",
        "STILL_DIR": data_dir / "media" / "stills",
        "AVATAR_DIR": data_dir / "media" / "avatars",
        "SUBTITLE_DIR": data_dir / "media" / "subtitles",
        "THUMB_DIR": data_dir / "media" / "thumbs",
        "DUMP_DIR": data_dir / "dump",
        "WORKER_ENABLED": False,
    })


def test_cli_limit_zero_and_missing_db(tmp_path, capsys):
    from scripts.download_assets import main

    data_dir = tmp_path / "cli-data"
    app = _make_app(data_dir)
    with app.app_context():
        _add_title("CLI-001", cover_url="https://pics.test/cli.jpg")
        db.session.commit()
        db.session.remove()

    code = main(["--data-dir", str(data_dir), "--limit", "0", "--quiet"])
    assert code == 0
    out = capsys.readouterr().out
    assert "considered=0" in out

    missing = tmp_path / "no-db"
    code = main(["--data-dir", str(missing), "--quiet"])
    assert code == 2


def test_still_upscale_helper():
    from io import BytesIO
    from PIL import Image
    from app.services.assets import _upscale_still_bytes

    source = BytesIO()
    Image.new("RGB", (800, 450), (10, 20, 30)).save(source, "JPEG")
    enlarged = _upscale_still_bytes(source.getvalue(), 1920)
    image = Image.open(BytesIO(enlarged))
    assert image.width == 1920
    assert image.height == 1080
