"""读取影片目录里的 NFO（Kodi/Jellyfin 风格），其元数据优先于 dump/JavDB。

- 同一目录常有一个 ``movie.nfo``/``folder.nfo`` 或与视频同名的 ``.nfo``；
- NFO 的 title/发行日期/时长/评分/片商/导演/题材/演员/系列会写入 Title；
- 写入后 Title.manual_lock=True，后续 dump 物化只补空字段，不覆盖 NFO；
- 只处理当前可访问的 NAS/SMB/已挂载磁带路径；解析失败只记错误。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
import posixpath
from pathlib import Path

from app.db import db
from app.models import (
    Actress, Director, Genre, Label, LibraryFile, LibraryVolume, Maker,
    Series, Title, TitleActress, TitleGenre, utcnow,
)

_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".ts", ".flv", ".webm"}
_CODE_RE = re.compile(r"^(?:\d{2,5})?[A-Z][A-Z0-9]{1,7}-\d{2,5}[A-Z]?$", re.I)


@dataclass
class NfoData:
    code: str | None = None
    title: str | None = None
    original_title: str | None = None
    plot: str | None = None
    release_date: date | None = None
    runtime_mins: int | None = None
    rating: float | None = None
    studio: str | None = None
    label: str | None = None
    series: str | None = None
    directors: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    actresses: list[tuple[str, str]] = field(default_factory=list)  # name, role


def _text(node, *names):
    for name in names:
        child = node.find(name)
        if child is not None and (child.text or "").strip():
            return child.text.strip()
    return None


def _int(value):
    if value is None:
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group()) if match else None


def _date(value):
    if not value:
        return None
    match = re.search(r"(\d{4})(?:[-/](\d{1,2}))?(?:[-/](\d{1,2}))?", str(value))
    if not match:
        return None
    year, month, day = match.groups()
    try:
        return date(int(year), int(month or 1), int(day or 1))
    except ValueError:
        return date(int(year), 1, 1)


def parse_nfo(path: Path | str) -> NfoData | None:
    try:
        root = ET.parse(str(path)).getroot()
    except (ET.ParseError, OSError):
        return None
    data = NfoData()
    code = _text(root, "num", "dvdid", "id")
    if not code:
        for node in root.findall("uniqueid"):
            value = (node.text or "").strip()
            if value and _CODE_RE.match(value):
                code = value
                break
    if code and not _CODE_RE.match(code):
        code = None
    data.code = code.upper() if code else None
    data.title = _text(root, "title")
    data.original_title = _text(root, "originaltitle")
    data.plot = _text(root, "plot", "outline")
    data.release_date = _date(_text(root, "premiered", "releasedate", "year"))
    runtime = _text(root, "runtime")
    if runtime:
        mins = _int(runtime)
        # Kodi runtime 是分钟；有的 NFO 用秒
        if mins and mins > 600:
            mins = round(mins / 60)
        data.runtime_mins = mins
    rating = _text(root, "rating")
    if not rating:
        node = root.find("ratings/rating/value")
        if node is not None:
            rating = node.text
    if rating:
        try:
            data.rating = float(str(rating).strip())
        except ValueError:
            pass
    data.studio = _text(root, "studio", "maker")
    data.label = _text(root, "label")
    data.series = _text(root, "set/name", "series")
    for node in root.findall("director"):
        name = (node.text or "").strip() or _text(node, "name")
        if name:
            data.directors.append(name)
    for node in root.findall("genre"):
        name = (node.text or "").strip()
        if name:
            data.genres.append(name)
    for node in root.findall("actor"):
        name = (node.text or "").strip() or _text(node, "name")
        role = (_text(node, "role") or "").strip().lower()
        if name:
            data.actresses.append((name, "male" if role in ("male", "actor") else "female"))
    return data


def find_nfo_for_file(app, library_file: LibraryFile) -> Path | None:
    volume = library_file.volume
    if volume is None:
        return None
    if (volume.kind or "") == "tape":
        from app.config import Config

        base = Path(app.config.get("LTFS_MOUNT", Config.LTFS_MOUNT))
    elif volume.ref:
        base = Path(volume.ref)
    else:
        return None
    video = base / library_file.rel_path
    folder = video.parent
    candidates = [
        video.with_suffix(".nfo"),
        folder / "movie.nfo",
        folder / "folder.nfo",
    ]
    try:
        candidates.extend(sorted(folder.glob("*.nfo")))
    except OSError:
        pass
    seen = set()
    for path in candidates:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _find_or_create(model, **values):
    for key, value in values.items():
        if value:
            row = model.query.filter(getattr(model, key) == value).first()
            if row is not None:
                return row
    return model(**{k: v for k, v in values.items() if v})


def _apply_to_title(title: Title, data: NfoData) -> None:
    # 手动锁：之后 dump 只补空，不覆盖 NFO
    title.manual_lock = True
    if data.title:
        if data.title != (title.title_ja or ""):
            title.title_ja = data.title
        if title.title_zh is None and data.original_title and data.original_title != data.title:
            title.title_zh = data.original_title
    if data.original_title and not title.title_en:
        title.title_en = data.original_title
    if data.release_date:
        title.release_date = data.release_date
    if data.runtime_mins:
        title.runtime_mins = data.runtime_mins
    if data.rating is not None:
        title.rating = data.rating
        title.rating_source = "nfo"
    if data.studio:
        maker = _find_or_create(Maker, name_ja=data.studio)
        db.session.add(maker); db.session.flush()
        title.maker_id = maker.id
    if data.label:
        label = _find_or_create(Label, name_ja=data.label)
        db.session.add(label); db.session.flush()
        title.label_id = label.id
    if data.series:
        series = _find_or_create(Series, name_ja=data.series)
        db.session.add(series); db.session.flush()
        title.series_id = series.id
    if data.directors:
        director = _find_or_create(Director, name_kanji=data.directors[0])
        db.session.add(director); db.session.flush()
        title.director_id = director.id
    if data.genres:
        existing = {g.genre_id for g in TitleGenre.query.filter_by(title_id=title.id).all()}
        for name in data.genres:
            genre = _find_or_create(Genre, genre_ja=name, genre_zh=name)
            db.session.add(genre); db.session.flush()
            if genre.id not in existing:
                db.session.add(TitleGenre(title_id=title.id, genre_id=genre.id, source="nfo"))
                existing.add(genre.id)
    if data.actresses:
        existing = {a.actress_id for a in TitleActress.query.filter_by(title_id=title.id).all()}
        for name, role in data.actresses:
            actress = _find_or_create(Actress, name_kanji=name)
            db.session.add(actress); db.session.flush()
            if actress.id not in existing:
                db.session.add(TitleActress(title_id=title.id, actress_id=actress.id, role=role))
                existing.add(actress.id)


def apply_nfo_for_file(app, library_file: LibraryFile) -> dict:
    """读取一个文件所在目录的 NFO；有就建立/更新 Title 并把同目录文件挂上。"""
    path = find_nfo_for_file(app, library_file)
    if path is None:
        return {"ok": False, "skipped": True}
    data = parse_nfo(path)
    if data is None:
        return {"ok": False, "error": f"NFO 解析失败: {path}"}
    code = data.code or library_file.code_guess
    if not code:
        return {"ok": False, "error": f"NFO 没有番号且文件名无番号: {path}"}
    code = code.strip().upper()
    from sqlalchemy import func

    title = Title.query.filter(func.upper(Title.dvd_id) == code).first()
    created = False
    if title is None:
        title = Title(dvd_id=code)
        db.session.add(title)
        db.session.flush()
        created = True
    _apply_to_title(title, data)
    folder = str(Path(library_file.rel_path).parent)
    linked = 0
    folder_prefix = folder.rstrip("/") + "/" if folder else ""
    siblings_query = (
        LibraryFile.query.filter_by(volume_id=library_file.volume_id, deleted_at=None)
        .filter(LibraryFile.is_sidecar.is_(False))
    )
    if folder_prefix:
        siblings_query = siblings_query.filter(LibraryFile.rel_path.like(folder_prefix + "%"))
    siblings = siblings_query.all()
    for item in siblings:
        if str(Path(item.rel_path).parent) != folder:
            continue
        if Path(item.rel_path).suffix.lower() not in _VIDEO_EXTS:
            continue
        if item.title_id is None:
            item.title_id = title.id
            linked += 1
    db.session.commit()
    return {"ok": True, "title_id": title.id, "created": created,
            "linked": linked, "nfo": str(path)}


def apply_nfo_for_volume(app, volume_id: int, limit: int | None = None) -> dict:
    """扫描一个卷里的视频文件夹，有 NFO 就写入 Title；同目录只处理一次。"""
    files = (
        LibraryFile.query.filter_by(volume_id=volume_id, deleted_at=None)
        .filter(LibraryFile.is_sidecar.is_(False))
        .order_by(LibraryFile.id)
        .all()
    )
    folders = set()
    stats = {"titles": 0, "linked": 0, "errors": []}
    for item in files:
        if Path(item.rel_path).suffix.lower() not in _VIDEO_EXTS:
            continue
        folder = posixpath.dirname(item.rel_path or "")
        if folder in folders:
            continue
        folders.add(folder)
        try:
            result = apply_nfo_for_file(app, item)
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            stats["errors"].append(f"{folder}: {type(exc).__name__}: {exc}")
            continue
        if result.get("ok"):
            stats["titles"] += 1
            stats["linked"] += int(result.get("linked") or 0)
        if limit and len(folders) >= int(limit):
            break
    return stats


def apply_nfo_for_all(app, limit: int | None = None) -> dict:
    stats = {"volumes": 0, "titles": 0, "linked": 0, "errors": []}
    for volume in LibraryVolume.query.order_by(LibraryVolume.id).all():
        result = apply_nfo_for_volume(app, volume.id, limit=limit)
        stats["volumes"] += 1
        stats["titles"] += int(result.get("titles") or 0)
        stats["linked"] += int(result.get("linked") or 0)
        stats["errors"].extend(result.get("errors") or [])
    return stats
