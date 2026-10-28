"""专用 404 页面测试。"""
from app import create_app


def _app(tmp_path):
    return create_app({
        "TESTING": True, "WORKER_ENABLED": False, "CSRF_ENABLED": False,
        "DATA_DIR": tmp_path / "data", "DB_PATH": tmp_path / "data" / "test.db",
    })


def test_404_uses_dedicated_page(tmp_path):
    html = _app(tmp_path).test_client().get("/no-such-page").get_data(as_text=True)
    assert "404" in html
    assert "页面不存在" in html
    assert "返回片库" in html
    assert "/no-such-page" in html


def test_api_404_is_json(tmp_path):
    response = _app(tmp_path).test_client().get("/api/no-such-endpoint")
    assert response.status_code == 404
    assert response.is_json
    assert response.get_json()["ok"] is False
