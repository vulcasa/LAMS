"""SQLAlchemy 实例与 SQLite 调优。

注意：不开 foreign_keys（v1 经验：自引用批量删除会崩；本服务自己保证一致性），
开 WAL 让「后台扫描写 + 页面读」可以并发。
"""
from sqlalchemy import event
from sqlalchemy.engine import Engine
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    # 只对 sqlite 生效（其它后端没有这些 pragma）
    module = type(dbapi_conn).__module__
    if "sqlite" not in module:
        return
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=OFF")
    finally:
        cur.close()


# 轻量 schema 迁移：create_all 只建缺失表，不会给已有表加列；这里补 ALTER。
# 新增列时在这里登记（SQLite 的 ADD COLUMN 是常数时间，可重复执行）。
COLUMN_MIGRATIONS = {
    "library_volume": [
        ("status", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("device_path", "TEXT"),
        ("last_mounted_at", "DATETIME"),
        ("last_error", "TEXT"),
    ],
    "library_file": [
        ("flags", "TEXT"),
        ("is_sidecar", "BOOLEAN NOT NULL DEFAULT 0"),
        ("manual_code", "BOOLEAN NOT NULL DEFAULT 0"),
    ],
    "title": [
        ("javdb_miss_at", "DATETIME"),
        ("javdb_miss_count", "INTEGER DEFAULT 0"),
        ("cover_failed_at", "DATETIME"),
        ("cover_fail_count", "INTEGER DEFAULT 0"),
        ("is_favorite", "BOOLEAN NOT NULL DEFAULT 0"),
    ],
    "asset": [
        ("failed_at", "DATETIME"),
        ("fail_count", "INTEGER DEFAULT 0"),
    ],
    "job": [
        ("cancel_requested", "BOOLEAN NOT NULL DEFAULT 0"),
    ],
    "media_source": [
        ("smb_host", "TEXT"),
        ("smb_share", "TEXT"),
        ("smb_username", "TEXT"),
        ("smb_password", "TEXT"),
        ("smb_options", "TEXT"),
        ("smb_mounted_at", "DATETIME"),
    ],
}


def ensure_schema():
    """给已有库补列（幂等）。在 create_all() 之后调用。"""
    conn = db.engine.raw_connection()
    try:
        for table, columns in COLUMN_MIGRATIONS.items():
            try:
                existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            except Exception:  # noqa: BLE001 - 表不存在就跳过（create_all 会建）
                continue
            if not existing:
                continue
            for name, ddl in columns:
                if name not in existing:
                    conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}')
        conn.commit()
    finally:
        conn.close()


FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS title_fts USING fts5(
    dvd_id, title_ja, title_zh, actress_names, genres,
    content='', tokenize='unicode61'
);
"""


def init_db(app):
    db.init_app(app)
    # FTS 表建在真实 DB 里（SQLAlchemy 不认虚表，这里手工建）
    with app.app_context():
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "connect_args": {"timeout": 30},
        }
        conn = db.engine.raw_connection()
        try:
            conn.executescript(FTS_SCHEMA)
            conn.commit()
        finally:
            conn.close()
