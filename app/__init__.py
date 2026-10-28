"""Flask 应用工厂。"""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import secrets

from flask import Flask, abort, jsonify, render_template, request, url_for

from .config import Config
from .db import db


try:  # 容器里可能没有 tzdata，退化成固定 UTC+8
    from zoneinfo import ZoneInfo

    BEIJING_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover
    BEIJING_TZ = timezone(timedelta(hours=8))


def bjtime(value, fmt: str = "%Y-%m-%d %H:%M:%S"):
    """把库里存的 UTC 时间显示成北京时间（模板过滤器）。"""
    if value is None or value == "":
        return "—"
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BEIJING_TZ).strftime(fmt)


def create_app(overrides=None):
    app = Flask(__name__)
    app.jinja_env.filters["bjtime"] = bjtime

    csrf_cookie = "csrf_token"

    def _csrf_token_from_cookie() -> str:
        return (request.cookies.get(csrf_cookie) or "").strip()

    from .services.mobile import is_mobile_request, mobile_endpoint_allowed
    from .services.monitor import monitor as _monitor

    @app.context_processor
    def _inject_mobile_flag():
        return {"is_mobile": is_mobile_request()}

    @app.before_request
    def _mobile_gate():
        from flask import jsonify, render_template, request

        if not is_mobile_request():
            return None
        endpoint = request.endpoint
        if mobile_endpoint_allowed(endpoint):
            return None
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "移动端仅支持片库和正在播放"}), 403
        if endpoint is None:
            return None
        return render_template("mobile_blocked.html"), 403

    @app.before_request
    def _monitor_begin():
        _monitor.begin_request()

    @app.teardown_request
    def _monitor_end(_exc):
        _monitor.end_request()

    @app.before_request
    def _csrf_protect():
        if not app.config.get("CSRF_ENABLED", True):
            return None
        if app.config.get("TESTING"):
            return None
        if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
            return None
        expected = _csrf_token_from_cookie()
        # 非浏览器/API 客户端（无 Origin/Referer，且还没拿到 CSRF Cookie）不做拦截；
        # 浏览器首次 GET 会种下 Cookie，之后的写操作必须有正确 Header。
        if not expected and not (request.headers.get("Origin") or request.headers.get("Referer")):
            return None
        supplied = (
            request.headers.get("X-CSRF-Token")
            or request.form.get("csrf_token")
            or ""
        ).strip()
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "CSRF 校验失败，请刷新页面后重试"}), 403
            abort(403)
        return None

    @app.after_request
    def _csrf_issue(response):
        if (
            app.config.get("CSRF_ENABLED", True)
            and not app.config.get("TESTING")
            and request.method in ("GET", "HEAD")
            and not _csrf_token_from_cookie()
        ):
            token = secrets.token_urlsafe(32)
            response.set_cookie(
                csrf_cookie, token, max_age=7 * 24 * 3600,
                httponly=False, samesite="Lax", path="/",
            )
        return response
    app.config.from_object(Config)
    if overrides:
        app.config.update(overrides)
        # 测试/嵌入场景覆盖了 DATA_DIR 时，新目录配置要跟着走（否则会写回仓库 data/）
        if "DATA_DIR" in overrides:
            data_dir = Path(app.config["DATA_DIR"])
            for key, rel in (
                ("MANIFEST_DIR", "manifests"),
                ("MEDIA_DIR", "media"),
                ("DUMP_DIR", "dump"),
            ):
                if key not in overrides:
                    app.config[key] = data_dir / rel
            for key, sub in (
                ("COVER_DIR", "covers"),
                ("STILL_DIR", "stills"),
                ("AVATAR_DIR", "avatars"),
                ("SUBTITLE_DIR", "subtitles"),
                ("THUMB_DIR", "thumbs"),
            ):
                if key not in overrides:
                    app.config[key] = data_dir / "media" / sub

    data_dir = Path(app.config["DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    for key in (
        "MEDIA_DIR", "COVER_DIR", "STILL_DIR", "AVATAR_DIR",
        "SUBTITLE_DIR", "THUMB_DIR", "DUMP_DIR", "MANIFEST_DIR",
    ):
        Path(app.config[key]).mkdir(parents=True, exist_ok=True)

    if not app.config.get("SQLALCHEMY_DATABASE_URI"):
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{app.config['DB_PATH']}"
    app.config.setdefault("SQLALCHEMY_ENGINE_OPTIONS", {"connect_args": {"timeout": 30}})

    db.init_app(app)
    from . import models  # noqa: F401  注册模型

    with app.app_context():
        db.create_all()
        from .db import ensure_schema

        ensure_schema()
        _ensure_fts()
        try:
            from .services.search import fts_ready, rebuild_title_fts

            if not fts_ready():
                rebuild_title_fts(app)
        except Exception:  # noqa: BLE001 - 索引失败不影响启动/搜索会走 LIKE
            pass
        try:
            from .services.smb import migrate_plaintext_credentials

            migrate_plaintext_credentials(app)
        except Exception:  # noqa: BLE001
            pass

    from .routes import register_blueprints

    register_blueprints(app)

    # 后台任务 worker（线程；gunicorn 多 worker 也安全，领取是原子的）
    from .services.jobs import start_worker

    start_worker(app)

    @app.template_filter("thumb_url")
    def thumb_url(cover_local):
        """优先用缩略图（thumbs/<stem>_cover.jpg），没有就回退原图。"""
        if not cover_local:
            return ""
        stem = Path(cover_local).stem
        cand = Path(app.config["THUMB_DIR"]) / f"{stem}_cover.jpg"
        if cand.exists():
            return url_for("pages.media", filename=f"thumbs/{cand.name}")
        return url_for("pages.media", filename=cover_local)

    @app.errorhandler(404)
    def not_found(_e):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "not found"}), 404
        return render_template("404.html"), 404

    @app.errorhandler(500)
    def server_error(_e):
        return render_template("error.html", code=500, message="服务器内部错误"), 500

    return app


def _ensure_fts():
    """FTS5 虚表（SQLAlchemy 不管理虚表，这里手工建）。"""
    conn = db.engine.raw_connection()
    try:
        conn.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS title_fts USING fts5(
                dvd_id, title_ja, title_zh, actress_names, genres,
                tokenize='unicode61'
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
