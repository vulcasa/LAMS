"""标题全文搜索（SQLite FTS5，title_fts）。"""
from __future__ import annotations

import re

from sqlalchemy import text

from app.db import db
from app.models import Actress, Genre, Title, TitleActress, TitleGenre

__all__ = ["rebuild_title_fts", "search_title_ids", "fts_ready"]

_TOKEN_RE = re.compile(r"[0-9A-Za-z_]+|[\u3040-\u30ff\u3400-\u9fff]+")


def _row_values(title: Title, actress_names: str, genre_names: str):
    return (
        title.id,
        title.dvd_id or "",
        title.title_ja or "",
        title.title_zh or "",
        actress_names,
        genre_names,
    )


def _rebuild_into_temp(app, batch: int) -> int:
    """在 title_fts_new 里重建，成功后替换旧表；失败不会破坏已有索引。"""
    try:
        db.session.execute(text("DROP TABLE IF EXISTS title_fts_new"))
        db.session.execute(
            text(
                "CREATE VIRTUAL TABLE title_fts_new USING fts5("
                "dvd_id, title_ja, title_zh, actress_names, genres, content='')"
            )
        )
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        db.session.execute(text("DROP TABLE IF EXISTS title_fts_new"))
        db.session.execute(
            text(
                "CREATE VIRTUAL TABLE title_fts_new USING fts5("
                "dvd_id, title_ja, title_zh, actress_names, genres, content='')"
            )
        )
        db.session.commit()

    actress_map: dict[int, list[str]] = {}
    for tid, name in (
        db.session.query(TitleActress.title_id, Actress.name_kanji)
        .join(Actress, Actress.id == TitleActress.actress_id)
        .filter(TitleActress.role == "female")
        .all()
    ):
        if name:
            actress_map.setdefault(tid, []).append(name)
    genre_map: dict[int, list[str]] = {}
    for tid, name in (
        db.session.query(TitleGenre.title_id, Genre.genre_zh)
        .join(Genre, Genre.id == TitleGenre.genre_id)
        .all()
    ):
        if name:
            genre_map.setdefault(tid, []).append(name)

    count = 0
    for title in Title.query.order_by(Title.id).yield_per(batch):
        for attempt in range(5):
            try:
                db.session.execute(
                    text(
                        "INSERT INTO title_fts_new(rowid, dvd_id, title_ja, title_zh, "
                        "actress_names, genres) "
                        "VALUES (:id, :dvd, :ja, :zh, :actresses, :genres)"
                    ),
                    {
                        "id": title.id,
                        "dvd": title.dvd_id or "",
                        "ja": title.title_ja or "",
                        "zh": title.title_zh or "",
                        "actresses": " ".join(actress_map.get(title.id, [])),
                        "genres": " ".join(genre_map.get(title.id, [])),
                    },
                )
                break
            except Exception:  # noqa: BLE001 - database locked 重试
                db.session.rollback()
                if attempt == 4:
                    raise
                import time as _time

                _time.sleep(0.2 * (attempt + 1))
        count += 1
        if count % batch == 0:
            db.session.commit()

    for attempt in range(5):
        try:
            db.session.commit()
            db.session.execute(text("DROP TABLE IF EXISTS title_fts"))
            db.session.execute(text("ALTER TABLE title_fts_new RENAME TO title_fts"))
            db.session.commit()
            return count
        except Exception:  # noqa: BLE001
            db.session.rollback()
            if attempt == 4:
                raise
            import time as _time

            _time.sleep(0.3 * (attempt + 1))
    return count


def rebuild_title_fts(app=None, *, batch: int = 500) -> dict:
    """全量重建 title_fts（内容物化）。返回 {ok, titles}。"""
    last_error = None
    for attempt in range(3):
        try:
            count = _rebuild_into_temp(app, batch)
            return {"ok": True, "titles": count}
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            db.session.rollback()
            import time as _time

            _time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"FTS 重建失败: {type(last_error).__name__}: {last_error}")


def fts_ready() -> bool:
    try:
        return bool(db.session.execute(text("SELECT 1 FROM title_fts LIMIT 1")).first())
    except Exception:  # noqa: BLE001
        return False


def search_title_ids(query: str, *, limit: int = 2000) -> list[int] | None:
    """返回命中的 Title.id 列表；FTS 不可用/无结果时返回 None 交给 LIKE 兜底。"""
    tokens = _TOKEN_RE.findall(query or "")
    if not tokens:
        return None
    terms = " AND ".join(f'"{token}"*' for token in tokens)
    try:
        rows = db.session.execute(
            text(
                "SELECT rowid FROM title_fts WHERE title_fts MATCH :q "
                "ORDER BY rank LIMIT :limit"
            ),
            {"q": terms, "limit": int(limit)},
        ).all()
    except Exception:  # noqa: BLE001 - FTS 语法/表异常走兜底
        return None
    return [row[0] for row in rows] or None
