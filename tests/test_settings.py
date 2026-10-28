"""设置页（运行时代理配置）与只读扫描测试。"""
import builtins
import hashlib
import os
import shutil
import time
from pathlib import Path

import pytest

from app import create_app
from app.config import Config
from app.db import db


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path / "data",
            "DB_PATH": tmp_path / "data" / "test.db",
            "MEDIA_DIR": tmp_path / "data" / "media",
            "COVER_DIR": tmp_path / "data" / "media" / "covers",
            "STILL_DIR": tmp_path / "data" / "media" / "stills",
            "AVATAR_DIR": tmp_path / "data" / "media" / "avatars",
            "SUBTITLE_DIR": tmp_path / "data" / "media" / "subtitles",
            "THUMB_DIR": tmp_path / "data" / "media" / "thumbs",
            "DUMP_DIR": tmp_path / "data" / "dump",
            "WORKER_ENABLED": False,
        }
    )
    yield app
    with app.app_context():
        db.session.remove()


@pytest.fixture()
def client(app):
    return app.test_client()


def _hash_tree(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = (
                p.stat().st_size,
                int(p.stat().st_mtime),
                hashlib.sha256(p.read_bytes()).hexdigest(),
            )
    return out


# ---------------------------------------------------------------------------
# 代理设置
# ---------------------------------------------------------------------------
def test_env_fallback_when_settings_disabled(app, monkeypatch):
    from app.services import settings

    monkeypatch.setattr(Config, "HTTP_PROXY", "http://env-proxy:8080")
    with app.app_context():
        proxy = settings.get_proxy(app)
        assert proxy["source"] == "env"
        assert proxy["url"] == "http://env-proxy:8080"
        assert settings.request_proxies(app) == {
            "http": "http://env-proxy:8080",
            "https": "http://env-proxy:8080",
        }


def test_settings_take_precedence_and_validation(app, monkeypatch):
    from app.services import settings

    monkeypatch.setattr(Config, "HTTP_PROXY", "http://env-proxy:8080")
    with app.app_context():
        bad = settings.save_proxy(app, enabled=True, host="", port="")
        assert bad["ok"] is False and bad["errors"]

        bad = settings.save_proxy(app, enabled=True, host="127.0.0.1", port="99999")
        assert bad["ok"] is False

        ok = settings.save_proxy(app, enabled=True, host="http://127.0.0.1", port="7897")
        assert ok["ok"] is True
        proxy = settings.get_proxy(app)
        assert proxy["source"] == "settings"
        assert proxy["url"] == "http://127.0.0.1:7897"
        assert proxy["host"] == "127.0.0.1"

        # 关闭后回退 env
        settings.save_proxy(app, enabled=False, host="", port="")
        assert settings.get_proxy(app)["source"] == "env"


def test_host_port_pasted_together(app):
    from app.services import settings

    with app.app_context():
        res = settings.save_proxy(app, enabled=True, host="192.168.2.66:7890", port="")
        assert res["ok"] is True
        assert res["proxy"]["url"] == "http://192.168.2.66:7890"


def test_api_settings_save_and_get(client, app):
    r = client.get("/api/settings")
    assert r.status_code == 200 and "proxy" in r.get_json()

    r = client.post(
        "/api/settings/proxy",
        json={"enabled": True, "host": "10.0.0.2", "port": "1080"},
    )
    assert r.status_code == 200
    proxy = r.get_json()["proxy"]
    assert proxy["url"] == "http://10.0.0.2:1080"

    r = client.post("/api/settings/proxy", json={"enabled": True, "host": "", "port": ""})
    assert r.status_code == 400
    assert r.get_json()["errors"]


def test_api_proxy_test_mocked(client, app, monkeypatch):
    from app.services import settings

    class FakeResp:
        status_code = 403

    class FakeSession:
        def __init__(self):
            self.headers = {}
            self.proxies = {}
            self.trust_env = True

        def get(self, *a, **kw):
            return FakeResp()

    monkeypatch.setattr(settings.requests, "Session", FakeSession)
    with app.app_context():
        settings.save_proxy(app, enabled=True, host="127.0.0.1", port="7897")
    r = client.post("/api/settings/proxy/test", json={})
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True and data["status_code"] == 403


def test_scraping_sessions_use_saved_proxy(app):
    from app.services import settings
    from app.services.assets import _make_session
    from app.services.javdb import _new_session as javdb_new_session
    from app.services.subtitle import _session as subtitle_session

    with app.app_context():
        settings.save_proxy(app, enabled=True, host="127.0.0.1", port="7897")
        proxy = settings.proxy_url(app)
        for session in (
            javdb_new_session(proxy),
            subtitle_session(proxy),
            _make_session(app),
        ):
            assert session.proxies.get("https") == "http://127.0.0.1:7897"


# ---------------------------------------------------------------------------
# NAS 只读扫描
# ---------------------------------------------------------------------------
def test_scan_nas_is_read_only(app, tmp_path, monkeypatch):
    from app.services.scanner import scan_nas

    root = tmp_path / "nas"
    (root / "SSIS-001").mkdir(parents=True)
    (root / "SSIS-001" / "SSIS-001.mp4").write_bytes(b"x" * 1024)
    (root / "SSIS-001" / "SSIS-001-C.srt").write_text("sub")
    (root / "FC2").mkdir()
    (root / "FC2" / "FC2-PPV-1234567.mp4").write_bytes(b"y" * 2048)
    before = _hash_tree(root)

    forbidden = []

    def no_write_open(file, mode="r", *a, **kw):
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            forbidden.append((file, mode))
            raise AssertionError(f"扫描不应以写模式打开文件: {file} ({mode})")
        return builtins.open(file, mode, *a, **kw)

    monkeypatch.setattr("builtins.open", no_write_open)

    with app.app_context():
        stats = scan_nas(app, str(root))

    assert stats.files_seen >= 3
    assert not forbidden, forbidden
    after = _hash_tree(root)
    assert before == after, "扫描后 NAS 文件内容/时间不应改变"


def test_scan_nas_does_not_create_files(app, tmp_path):
    from app.services.scanner import scan_nas

    root = tmp_path / "nas2"
    root.mkdir()
    (root / "SSIS-002.mp4").write_bytes(b"z")
    names_before = sorted(p.name for p in root.iterdir())

    with app.app_context():
        scan_nas(app, str(root))

    assert sorted(p.name for p in root.iterdir()) == names_before


# ---------------------------------------------------------------------------
# 页面内容（不再出现 v1 的地址）
# ---------------------------------------------------------------------------
def test_sources_page_has_no_v1_address(client):
    html = client.get("/sources").get_data(as_text=True)
    assert "ltfs-webui" not in html
    assert "LTFS_API_BASE" not in html
    assert "只读" in html


def test_settings_page_has_proxy_form_and_no_v1_address(client):
    html = client.get("/settings").get_data(as_text=True)
    assert "代理" in html
    assert "proxy-host" in html
    assert "ltfs-webui" not in html
    assert "LTFS_API_BASE" not in html
