"""片库：列表 / 筛选 / 详情 / 用户上传封面与剧照。"""
import io
import json
import os
import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import unquote

from PIL import Image
from flask import Blueprint, abort, current_app, jsonify, render_template, request, url_for
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload

from app.config import Config
from app.db import db
from app.services.media_guard import guard_json
from app.models import (
    Asset,
    Actress,
    Director,
    Genre,
    Label,
    LibraryFile,
    LibraryVolume,
    Maker,
    Series,
    Subtitle,
    Title,
    TitleActress,
    TitleEditHistory,
    TitleGenre,
)

bp = Blueprint("library", __name__)
# 桌面最大宽度下网格是 7 列；49 = 7×7，满页不会剩最后一格
PER_PAGE = 49
_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 60.0


def _cached(key: str, factory, ttl: float = _CACHE_TTL):
    """极简进程内缓存：片库统计/筛选项在 TTL 内复用。"""
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    value = factory()
    _CACHE[key] = (now, value)
    return value


def _clear_cache() -> None:
    _CACHE.clear()


def _top(query, limit: int | None = None):
    """单实体查询返回实体本身，多列查询返回 Row，这里统一成实体/首列。

    limit=None 表示全部（筛选下拉必须包含所有 tag/演员/导演，不能只取前 40）。
    """
    page = query if limit is None else query.limit(limit)
    out = []
    for row in page.all():
        out.append(row if hasattr(row, "id") else row[0])
    return out


SORT_OPTIONS = {
    "date_desc": "上映时间 新 → 旧",
    "date_asc": "上映时间 旧 → 新",
    "code_asc": "番号 A → Z",
    "code_desc": "番号 Z → A",
    "added_desc": "最近入库",
}


def _order_by(sort: str):
    if sort == "date_asc":
        return (
            func.coalesce(Title.release_date, func.date("9999-12-31")).asc(),
            Title.dvd_id.asc(),
        )
    if sort == "code_asc":
        return (Title.dvd_id.asc(), Title.id.asc())
    if sort == "code_desc":
        return (Title.dvd_id.desc(), Title.id.desc())
    if sort == "added_desc":
        return (Title.id.desc(),)
    return (
        func.coalesce(Title.release_date, func.date("1900-01-01")).desc(),
        Title.id.desc(),
    )


def _filter_options():
    actresses = _top(
        db.session.query(Actress)
        .join(TitleActress, TitleActress.actress_id == Actress.id)
        .group_by(Actress.id)
        .order_by(func.count(TitleActress.title_id).desc())
    )
    makers = _top(
        db.session.query(Maker).join(Title, Title.maker_id == Maker.id)
        .group_by(Maker.id).order_by(func.count(Title.id).desc())
    )
    series = _top(
        db.session.query(Series).join(Title, Title.series_id == Series.id)
        .group_by(Series.id).order_by(func.count(Title.id).desc())
    )
    genres = _top(
        db.session.query(Genre).join(TitleGenre, TitleGenre.genre_id == Genre.id)
        .group_by(Genre.id).order_by(func.count(TitleGenre.title_id).desc())
    )
    directors = _top(
        db.session.query(Director).join(Title, Title.director_id == Director.id)
        .group_by(Director.id).order_by(func.count(Title.id).desc())
    )
    years = [
        r[0]
        for r in db.session.query(func.strftime("%Y", Title.release_date))
        .filter(Title.release_date.isnot(None))
        .distinct()
        .order_by(func.strftime("%Y", Title.release_date).desc())
        .all()
        if r[0]
    ]
    return {
        "actresses": actresses,
        "makers": makers,
        "series": series,
        "genres": genres,
        "directors": directors,
        "years": years,
    }


@bp.route("/library")
def index():
    q = (request.args.get("q") or "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(max(request.args.get("per_page", PER_PAGE, type=int), 12), 200)
    sort = request.args.get("sort") or "date_desc"
    if sort not in SORT_OPTIONS:
        sort = "date_desc"

    query = Title.query
    if q:
        from app.services.search import search_title_ids

        fts_ids = search_title_ids(q)
        like = f"%{q}%"
        like_cond = or_(
            Title.dvd_id.ilike(like),
            Title.title_zh.ilike(like),
            Title.title_ja.ilike(like),
            Title.title_en.ilike(like),
        )
        if fts_ids and (q.isdigit() or len(q) <= 5):
            # 数字/短词（如 536）FTS 可能只返回部分命中，联表 LIKE 兜底
            query = query.filter(or_(Title.id.in_(fts_ids), like_cond))
        elif fts_ids:
            query = query.filter(Title.id.in_(fts_ids))
        else:
            query = query.filter(like_cond)

    for param, fk in (
        ("maker", Title.maker_id),
        ("series", Title.series_id),
        ("director", Title.director_id),
    ):
        val = request.args.get(param, type=int)
        if val:
            query = query.filter(fk == val)

    favorite = request.args.get("favorite", type=int)
    if favorite == 1:
        query = query.filter(Title.is_favorite.is_(True))
    elif favorite == 0:
        query = query.filter(Title.is_favorite.is_(False))

    actress = request.args.get("actress", type=int)
    if actress:
        query = query.filter(
            Title.id.in_(
                db.session.query(TitleActress.title_id).filter(
                    TitleActress.actress_id == actress
                )
            )
        )

    genre = request.args.get("genre", type=int)
    if genre:
        query = query.filter(
            Title.id.in_(
                db.session.query(TitleGenre.title_id).filter(
                    TitleGenre.genre_id == genre
                )
            )
        )

    year = request.args.get("year")
    if year and year.isdigit():
        query = query.filter(func.strftime("%Y", Title.release_date) == year)

    medium = request.args.get("medium")
    if medium in ("tape", "nas"):
        query = query.filter(
            Title.id.in_(
                db.session.query(LibraryFile.title_id)
                .join(LibraryVolume, LibraryVolume.id == LibraryFile.volume_id)
                .filter(
                    LibraryVolume.kind == medium,
                    LibraryFile.title_id.isnot(None),
                    LibraryFile.deleted_at.is_(None),
                )
            )
        )

    tape_title_ids = (
        db.session.query(LibraryFile.title_id)
        .join(LibraryVolume, LibraryVolume.id == LibraryFile.volume_id)
        .filter(
            LibraryVolume.kind == "tape",
            LibraryFile.title_id.isnot(None),
            LibraryFile.deleted_at.is_(None),
        )
    )
    archived = request.args.get("archived")
    if archived == "1":
        query = query.filter(Title.id.in_(tape_title_ids))
    elif archived == "0":
        query = query.filter(~Title.id.in_(tape_title_ids))

    has_cover = request.args.get("has_cover")
    if has_cover == "1":
        query = query.filter(Title.cover_local.isnot(None))
    elif has_cover == "0":
        query = query.filter(
            or_(Title.cover_local.is_(None), func.trim(Title.cover_local) == "")
        )

    has_sub = request.args.get("has_sub")
    if has_sub == "1":
        query = query.filter(Title.has_chinese_sub.is_(True))
    elif has_sub == "0":
        query = query.filter(Title.has_chinese_sub.is_(False))

    def page_url(pno):
        args = request.args.to_dict()
        args["page"] = pno
        return url_for("library.index", **args)

    review = request.args.get("review")
    if review == "1":
        subq = (
            db.session.query(LibraryFile.title_id.label("tid"))
            .filter(
                LibraryFile.title_id.isnot(None),
                LibraryFile.deleted_at.is_(None),
                LibraryFile.confidence.isnot(None),
            )
            .group_by(LibraryFile.title_id)
            .having(func.max(LibraryFile.confidence) < Config.CODE_AUTO_MIN)
            .having(func.max(LibraryFile.confidence) >= Config.CODE_REVIEW_MIN)
            .subquery()
        )
        query = query.filter(Title.id.in_(db.session.query(subq.c.tid)))

    pagination = query.options(joinedload(Title.maker)).order_by(*_order_by(sort)).paginate(
        page=page, per_page=per_page, error_out=False
    )

    # 每部影片的文件置信度（用于「待抽检」角标）
    ids = [t.id for t in pagination.items]
    conf_map = {}
    if ids:
        rows = (
            db.session.query(LibraryFile.title_id, func.max(LibraryFile.confidence))
            .filter(
                LibraryFile.title_id.in_(ids),
                LibraryFile.deleted_at.is_(None),
                LibraryFile.confidence.isnot(None),
            )
            .group_by(LibraryFile.title_id)
            .all()
        )
        conf_map = {tid: int(mx) for tid, mx in rows if mx is not None}
    review_ids = {
        tid
        for tid, mx in conf_map.items()
        if Config.CODE_REVIEW_MIN <= mx < Config.CODE_AUTO_MIN
    }
    archived_ids = set()
    if ids:
        archived_ids = {
            row[0]
            for row in (
                db.session.query(LibraryFile.title_id)
                .join(LibraryVolume, LibraryVolume.id == LibraryFile.volume_id)
                .filter(
                    LibraryFile.title_id.in_(ids),
                    LibraryVolume.kind == "tape",
                    LibraryFile.deleted_at.is_(None),
                )
                .distinct()
                .all()
            )
            if row[0] is not None
        }

    cache_key = "total:" + "&".join(
        f"{key}={value}" for key, value in sorted(request.args.items())
        if key not in ("page", "per_page", "sort")
    )
    total = (
        query.count()
        if current_app.config.get("TESTING")
        else _cached(cache_key, query.count, ttl=15.0)
    )
    missing_cover = (
        Title.query.filter(
            or_(Title.cover_local.is_(None), func.trim(Title.cover_local) == "")
        ).count()
        if current_app.config.get("TESTING")
        else _cached(
            "missing_cover",
            lambda: Title.query.filter(
                or_(Title.cover_local.is_(None), func.trim(Title.cover_local) == "")
            ).count(),
        )
    )
    return render_template(
        "library/index.html",
        pagination=pagination,
        titles=pagination.items,
        q=q,
        options=(
            _filter_options()
            if current_app.config.get("TESTING")
            else _cached("filter_options", _filter_options)
        ),
        args=request.args,
        sort=sort,
        sort_options=SORT_OPTIONS,
        total=total,
        missing_cover=missing_cover,
        page_url=page_url,
        conf_map=conf_map,
        review_ids=review_ids,
        archived_ids=archived_ids,
    )


@bp.route("/library/<path:dvd_id>")
def detail(dvd_id):
    code = unquote(dvd_id).strip().upper()
    title = Title.query.filter(func.upper(Title.dvd_id) == code).first()
    if not title:
        abort(404)

    files = (
        LibraryFile.query.filter_by(title_id=title.id)
        .filter(LibraryFile.deleted_at.is_(None))
        .all()
    )
    tapes = sorted({f.volume.ref for f in files if f.volume and f.volume.kind == "tape"})
    nas_roots = sorted({f.volume.ref for f in files if f.volume and f.volume.kind == "nas"})
    subtitles = Subtitle.query.filter_by(title_id=title.id).all()
    actresses = (
        Actress.query.join(TitleActress, TitleActress.actress_id == Actress.id)
        .filter(TitleActress.title_id == title.id, TitleActress.role == "female")
        .all()
    )
    males = (
        Actress.query.join(TitleActress, TitleActress.actress_id == Actress.id)
        .filter(TitleActress.title_id == title.id, TitleActress.role == "male")
        .all()
    )
    genres = (
        Genre.query.join(TitleGenre, TitleGenre.genre_id == Genre.id)
        .filter(TitleGenre.title_id == title.id)
        .all()
    )
    screenshots = (
        Asset.query.filter_by(title_id=title.id, kind="screenshot")
        .filter(Asset.local_path.isnot(None), Asset.local_path != "")
        .order_by(Asset.idx.is_(None), Asset.idx, Asset.id)
        .limit(30)
        .all()
    )
    stills = (
        Asset.query.filter_by(title_id=title.id, kind="still")
        .filter(Asset.local_path.isnot(None), Asset.local_path != "")
        .order_by(Asset.idx.is_(None), Asset.idx, Asset.id)
        .limit(40)
        .all()
    )
    histories = (
        TitleEditHistory.query.filter_by(title_id=title.id)
        .order_by(TitleEditHistory.id.desc())
        .limit(20)
        .all()
    )
    return render_template(
        "library/detail.html",
        title=title,
        histories=histories,
        files=files,
        tapes=tapes,
        nas_roots=nas_roots,
        subtitles=subtitles,
        actresses=actresses,
        males=males,
        genres=genres,
        stills=stills,
        screenshots=screenshots,
    )


# ---------------------------------------------------------------------------
# 用户自行上传封面 / 剧照（DMM/JavDB 都没有图时的兜底）
# ---------------------------------------------------------------------------
_UPLOAD_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_MAX_STILLS_PER_REQUEST = 20


def _safe_stem(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value or "").strip("._") or "title"


def _media_relative(path: Path) -> str:
    media_dir = Path(current_app.config["MEDIA_DIR"])
    return os.path.relpath(path, media_dir).replace(os.sep, "/")


def _save_uploaded_image(file_storage, target: Path) -> None:
    """校验并保存为 JPEG（统一格式，顺手压到 2200px 内）。失败抛 ValueError。"""
    filename = (file_storage.filename or "").lower()
    if not filename:
        raise ValueError("文件名为空")
    if Path(filename).suffix not in _UPLOAD_EXTS:
        raise ValueError("只支持 jpg / png / webp / gif / bmp 图片")
    data = file_storage.read(_MAX_UPLOAD_BYTES + 1)
    if not data:
        raise ValueError("文件为空")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise ValueError("图片超过 25MB")
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"无法识别为图片: {type(exc).__name__}") from exc

    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[-1])
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")
    image.thumbnail((2200, 2200))
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, "JPEG", quality=90, optimize=True)


@bp.route("/api/titles/<int:title_id>/cover", methods=["POST"])
def upload_cover(title_id: int):
    title = db.session.get(Title, title_id)
    if title is None:
        return jsonify({"ok": False, "error": "影片不存在"}), 404
    file_storage = request.files.get("file") or request.files.get("cover")
    if file_storage is None:
        return jsonify({"ok": False, "error": "没有收到图片文件"}), 400
    target = Path(current_app.config["COVER_DIR"]) / f"{_safe_stem(title.dvd_id)}.jpg"
    try:
        _save_uploaded_image(file_storage, target)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    title.cover_local = _media_relative(target)
    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "cover_local": title.cover_local,
            "url": url_for("pages.media", filename=title.cover_local),
        }
    )


@bp.route("/api/titles/<int:title_id>/stills", methods=["POST"])
def upload_stills(title_id: int):
    title = db.session.get(Title, title_id)
    if title is None:
        return jsonify({"ok": False, "error": "影片不存在"}), 404
    files = [
        item
        for item in (request.files.getlist("files") or request.files.getlist("file"))
        if item is not None and item.filename
    ]
    if not files:
        return jsonify({"ok": False, "error": "没有收到图片文件"}), 400
    if len(files) > _MAX_STILLS_PER_REQUEST:
        return jsonify({"ok": False, "error": f"一次最多上传 {_MAX_STILLS_PER_REQUEST} 张"}), 400

    still_dir = Path(current_app.config["STILL_DIR"]) / _safe_stem(title.dvd_id)
    existing = (
        db.session.query(func.max(Asset.idx))
        .filter(Asset.title_id == title.id, Asset.kind == "still")
        .scalar()
        or 0
    )
    saved: list[dict] = []
    index = int(existing)
    for file_storage in files:
        index += 1
        target = still_dir / f"{index:02d}.jpg"
        while target.exists():  # 文件已存在但 DB 没登记：顺延，绝不覆盖用户图
            index += 1
            target = still_dir / f"{index:02d}.jpg"
        try:
            _save_uploaded_image(file_storage, target)
        except ValueError as exc:
            return jsonify({"ok": False, "error": f"{file_storage.filename}: {exc}"}), 400
        rel = _media_relative(target)
        asset = Asset(title_id=title.id, kind="still", idx=index, local_path=rel)
        db.session.add(asset)
        saved.append({"idx": index, "local_path": rel})
    db.session.commit()
    return jsonify({"ok": True, "count": len(saved), "stills": saved})


# ---------------------------------------------------------------------------
# 详情页逐字段编辑（Plex 风格：默认锁定，解锁后 PATCH 保存）
# ---------------------------------------------------------------------------
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")


def _split_names(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,，、/]+", value or "") if part.strip()]


def _find_or_create_by_name(model, fields: tuple[str, ...], name: str, *, create_field: str):
    """按名称（大小写不敏感）找实体，找不到就新建；返回实体。"""
    lowered = name.lower()
    query = model.query
    conditions = [func.lower(getattr(model, field)) == lowered for field in fields]
    obj = query.filter(or_(*conditions)).first()
    if obj is not None:
        return obj
    obj = model(**{create_field: name})
    db.session.add(obj)
    db.session.flush()
    return obj


def _relation_value(field: str, text: str):
    """把用户输入的名称转成关联实体（空字符串 → None）。"""
    relation_map = {
        "maker": (Maker, ("name_ja", "name_en"), "name_ja"),
        "label": (Label, ("name_ja", "name_en"), "name_ja"),
        "series": (Series, ("name_ja", "name_en"), "name_ja"),
        "director": (Director, ("name_kanji", "name_kana", "name_romaji"), "name_kanji"),
        "actress": (Actress, ("name_kanji", "name_kana", "name_romaji"), "name_kanji"),
        "genre": (Genre, ("genre_ja", "genre_zh", "genre_en"), "genre_zh"),
    }
    model, fields, create_field = relation_map[field]
    name = (text or "").strip()
    if not name:
        return None
    # 日文/中文名优先写日文字段，纯英文写英文字段
    if not _CJK_RE.search(name):
        if field == "genre":
            create_field = "genre_ja"
        elif field in ("maker", "label", "series"):
            create_field = "name_en"
        else:
            create_field = "name_romaji"
    return _find_or_create_by_name(model, fields, name, create_field=create_field)


def _metadata_display(field: str, title: Title) -> str:
    if field in ("title_zh", "title_ja"):
        return (getattr(title, field) or "").strip() or "—"
    if field == "release_date":
        return title.release_date.isoformat() if title.release_date else "—"
    if field == "runtime_mins":
        return f"{title.runtime_mins} 分钟" if title.runtime_mins else "—"
    if field == "rating":
        return f"{title.rating:.2f}" if title.rating is not None else "—"
    if field in ("maker", "label", "series", "director"):
        obj = getattr(title, field)
        return obj.display_name() if obj is not None else "—"
    if field == "actresses":
        names = [
            actress.display_name()
            for actress in Actress.query.join(
                TitleActress, TitleActress.actress_id == Actress.id
            )
            .filter(TitleActress.title_id == title.id, TitleActress.role == "female")
            .all()
        ]
        return "、".join(names) or "—"
    if field == "genres":
        names = [
            genre.display_name()
            for genre in Genre.query.join(TitleGenre, TitleGenre.genre_id == Genre.id)
            .filter(TitleGenre.title_id == title.id)
            .all()
        ]
        return "、".join(names) or "—"
    return "—"


def _metadata_snapshot(title: Title) -> dict:
    return {
        "title_zh": title.title_zh,
        "title_ja": title.title_ja,
        "release_date": title.release_date.isoformat() if title.release_date else None,
        "runtime_mins": title.runtime_mins,
        "rating": title.rating,
        "maker_id": title.maker_id,
        "label_id": title.label_id,
        "series_id": title.series_id,
        "director_id": title.director_id,
        "actresses": [
            row[0]
            for row in db.session.query(TitleActress.actress_id)
            .filter_by(title_id=title.id, role="female")
            .all()
        ],
        "genres": [
            row[0]
            for row in db.session.query(TitleGenre.genre_id).filter_by(title_id=title.id).all()
        ],
    }


def _restore_metadata(title: Title, snapshot: dict) -> None:
    title.title_zh = snapshot.get("title_zh")
    title.title_ja = snapshot.get("title_ja")
    release = snapshot.get("release_date")
    title.release_date = date.fromisoformat(release) if release else None
    title.runtime_mins = snapshot.get("runtime_mins")
    title.rating = snapshot.get("rating")
    title.maker_id = snapshot.get("maker_id")
    title.label_id = snapshot.get("label_id")
    title.series_id = snapshot.get("series_id")
    title.director_id = snapshot.get("director_id")
    TitleActress.query.filter_by(title_id=title.id, role="female").delete()
    for actress_id in snapshot.get("actresses") or []:
        if db.session.get(Actress, actress_id) is not None:
            db.session.add(TitleActress(title_id=title.id, actress_id=actress_id, role="female"))
    TitleGenre.query.filter_by(title_id=title.id).delete()
    for genre_id in snapshot.get("genres") or []:
        if db.session.get(Genre, genre_id) is not None:
            db.session.add(TitleGenre(title_id=title.id, genre_id=genre_id))


@bp.route("/api/titles/<int:title_id>/metadata", methods=["PATCH", "POST"])
def update_metadata(title_id: int):
    title = db.session.get(Title, title_id)
    if title is None:
        return jsonify({"ok": False, "error": "影片不存在"}), 404
    data = request.get_json(silent=True) or {}
    field = (data.get("field") or "").strip()
    raw = data.get("value")
    text = "" if raw is None else str(raw).strip()

    before = _metadata_snapshot(title)
    try:
        if field in ("title_zh", "title_ja"):
            if len(text) > 512:
                raise ValueError("标题太长（最多 512 字）")
            setattr(title, field, text or None)
        elif field == "release_date":
            if text:
                try:
                    parsed = date.fromisoformat(text)
                except ValueError as exc:
                    raise ValueError("日期格式应为 YYYY-MM-DD") from exc
                if parsed.year < 1900 or parsed.year > 2100:
                    raise ValueError("日期年份超出范围")
                title.release_date = parsed
            else:
                title.release_date = None
        elif field == "runtime_mins":
            if text:
                minutes = int(float(text))
                if minutes < 0 or minutes > 1000:
                    raise ValueError("时长应在 0–1000 分钟之间")
                title.runtime_mins = minutes or None
            else:
                title.runtime_mins = None
        elif field == "rating":
            if text:
                score = float(text)
                if score < 0 or score > 10:
                    raise ValueError("评分应在 0–10 之间")
                title.rating = round(score, 2)
            else:
                title.rating = None
        elif field in ("maker", "label", "series", "director"):
            obj = _relation_value(field, text)
            setattr(title, f"{field}_id", obj.id if obj is not None else None)
        elif field == "actresses":
            names = _split_names(text)
            TitleActress.query.filter_by(title_id=title.id, role="female").delete()
            for name in names[:40]:
                actress = _relation_value("actress", name)
                if actress is not None:
                    db.session.add(
                        TitleActress(title_id=title.id, actress_id=actress.id, role="female")
                    )
        elif field == "genres":
            names = _split_names(text)
            TitleGenre.query.filter_by(title_id=title.id).delete()
            for name in names[:60]:
                genre = _relation_value("genre", name)
                if genre is not None:
                    db.session.add(TitleGenre(title_id=title.id, genre_id=genre.id))
        else:
            return jsonify({"ok": False, "error": "不支持的字段"}), 400
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    # 人工编辑后锁住整条记录：后续自动刮削只补空，不覆盖用户改过的内容
    title.manual_lock = True
    after = _metadata_snapshot(title)
    if before != after:
        db.session.add(
            TitleEditHistory(
                title_id=title.id,
                field=field,
                before_json=json.dumps(before, ensure_ascii=False),
                after_json=json.dumps(after, ensure_ascii=False),
            )
        )
    db.session.commit()
    _clear_cache()
    try:
        from app.services.search import rebuild_title_fts

        rebuild_title_fts(current_app._get_current_object())
    except Exception:  # noqa: BLE001
        pass
    return jsonify(
        {
            "ok": True,
            "field": field,
            "value": _metadata_display(field, title),
            "manual_lock": bool(title.manual_lock),
        }
    )


@bp.route("/api/titles/<int:title_id>/import-url", methods=["POST"])
def import_metadata_url(title_id: int):
    """从官方作品页导入元数据（FC2/CosplayJAV/欧美厂牌通用兜底）。"""
    from app.services.url_meta import fc2_article_url, fetch_url_metadata

    title = db.session.get(Title, title_id)
    if title is None:
        return jsonify({"ok": False, "error": "影片不存在"}), 404
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url and data.get("fc2"):
        url = fc2_article_url(title.dvd_id) or ""
        if not url:
            return jsonify({"ok": False, "error": "该番号不是 FC2 格式"}), 400
    if not url:
        return jsonify({"ok": False, "error": "请填写作品页 URL"}), 400

    meta = fetch_url_metadata(current_app._get_current_object(), url)
    if not meta.get("ok"):
        return jsonify({"ok": False, "error": meta.get("error") or "抓取失败"}), 400

    fetched_title = (meta.get("title") or "").strip()
    if fetched_title:
        if _CJK_RE.search(fetched_title) and not (title.title_zh or "").strip():
            title.title_zh = fetched_title[:512]
        else:
            title.title_ja = fetched_title[:512]
    if meta.get("date"):
        try:
            title.release_date = date.fromisoformat(meta["date"])
        except ValueError:
            pass
    if meta.get("image") and not (title.cover_local or "").strip():
        title.cover_url = meta["image"]
    title.manual_lock = True
    db.session.commit()

    cover_downloaded = False
    if meta.get("image") and not (title.cover_local or "").strip():
        try:
            from app.services.assets import download_for_titles

            stats = download_for_titles(
                current_app._get_current_object(), limit=1, only_codes=[title.dvd_id]
            )
            cover_downloaded = bool(stats.covers)
        except Exception:  # noqa: BLE001
            cover_downloaded = False

    return jsonify(
        {
            "ok": True,
            "matched": meta,
            "title": title.title_zh or title.title_ja or "",
            "date": title.release_date.isoformat() if title.release_date else None,
            "cover_downloaded": cover_downloaded,
            "manual_lock": True,
        }
    )


@bp.route("/api/titles/<int:title_id>/video-frames", methods=["POST"])
def video_frames(title_id: int):
    """预留：把 ffmpeg 抽帧任务入队。"""
    from app.services import jobs

    title = db.session.get(Title, title_id)
    if title is None:
        return jsonify({"ok": False, "error": "影片不存在"}), 404
    job_id = jobs.enqueue("video_frames", {"title_id": title.id})
    return jsonify({"ok": True, "job_id": job_id}), 202


@bp.route("/api/titles/<int:title_id>/favorite", methods=["POST"])
def toggle_favorite(title_id: int):
    title = db.session.get(Title, title_id)
    if title is None:
        return jsonify({"ok": False, "error": "影片不存在"}), 404
    title.is_favorite = not bool(title.is_favorite)
    db.session.commit()
    _clear_cache()
    return jsonify({"ok": True, "favorite": bool(title.is_favorite)})


@bp.route("/api/titles/<int:title_id>/playback")
@guard_json
def title_playback(title_id: int):
    from app.config import Config

    title = db.session.get(Title, title_id)
    if title is None:
        return jsonify({"ok": False, "status": "missing", "message": "影片不存在"}), 404
    files = (
        LibraryFile.query.filter_by(title_id=title.id, deleted_at=None)
        .filter(LibraryFile.is_sidecar.is_(False))
        .order_by(LibraryFile.id)
        .all()
    )
    videos = [item for item in files if Path(item.name).suffix.lower() in {
        ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".ts", ".flv", ".webm",
    }]
    if not videos:
        return jsonify({"ok": True, "status": "unavailable",
                        "message": "该影片没有可播放的视频文件。"})
    mount = Path(current_app.config.get("LTFS_MOUNT", Config.LTFS_MOUNT))
    local = []
    tape = []
    for item in videos:
        volume = item.volume
        if volume is None:
            continue
        if (volume.kind or "") == "tape":
            tape.append((item, str(volume.ref or "未知")))
            continue
        if not volume.ref:
            continue
        try:
            path = (Path(volume.ref) / item.rel_path).resolve()
            if path.is_file():
                local.append((item, path))
        except OSError:
            continue
    if not local and tape:
        barcodes = sorted({barcode for _item, barcode in tape})
        joined = "、".join(barcodes)
        return jsonify({
            "ok": True, "status": "tape",
            "message": f"该文件位于 {joined} 编号的磁带内，请插入磁带后播放。",
            "barcodes": barcodes,
        })
    if not local:
        return jsonify({"ok": True, "status": "unavailable",
                        "message": "该影片的文件当前不可访问，请检查 NAS/SMB 挂载。"})
    return jsonify({
        "ok": True, "status": "playable",
        "url": url_for("pages.client_player", title_id=title.id),
        "files": len(local),
        "mode": "client",
    })


@bp.route("/api/titles/<int:title_id>/history")
def title_history(title_id: int):
    title = db.session.get(Title, title_id)
    if title is None:
        return jsonify({"ok": False, "error": "影片不存在"}), 404
    rows = (
        TitleEditHistory.query.filter_by(title_id=title_id)
        .order_by(TitleEditHistory.id.desc())
        .limit(50)
        .all()
    )
    return jsonify(
        {
            "ok": True,
            "history": [
                {"id": row.id, "field": row.field,
                 "created_at": row.created_at.isoformat() if row.created_at else None}
                for row in rows
            ],
        }
    )


@bp.route("/api/titles/<int:title_id>/history/<int:history_id>/undo", methods=["POST"])
def undo_title_history(title_id: int, history_id: int):
    title = db.session.get(Title, title_id)
    row = db.session.get(TitleEditHistory, history_id)
    if title is None or row is None or row.title_id != title_id:
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    try:
        snapshot = json.loads(row.before_json or "{}")
        _restore_metadata(title, snapshot)
        title.manual_lock = True
        db.session.commit()
        _clear_cache()
        try:
            from app.services.search import rebuild_title_fts

            rebuild_title_fts(current_app._get_current_object())
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
    return jsonify({"ok": True, "field": row.field})
