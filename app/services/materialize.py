"""番号 → r18 staging 元数据物化。

职责（见 docs/CONTRACTS.md §3）
------------------------------
用户片库里的 ``LibraryFile.code_guess`` 只是从文件名猜出来的番号；本模块把它
批量匹配到独立的 r18 staging 库（``data/r18dump.db``，只读），把命中的元数据
物化进主库：

* ``Title``：日/英标题、发售日、时长、厂牌/系列/导演外键、封面 URL；
* ``Actress``（女优 + 男优 ``role='male'``）、``Genre``、``Director``、``Maker``、
  ``Label``、``Series``；
* ``Asset(kind='still')``：只生成剧照 URL 行，**不下载**图片（资产任务负责下载）；
* 回写 ``LibraryFile.title_id``（包括该番号的字幕 sidecar 文件）。

设计要点
--------
1. **批量**：所有候选番号一次 ``lookup_by_dvd_ids``（三级匹配，见 r18 模块），
   再逐番号物化；staging 是 190 万行的库，绝不做逐条查询。
2. **多版本合并**：同一番号在 dump 里常有多条（digital / mono / rental 等）。
   先按 ``dvd_id_key`` 去重成一组，再按「``site_id=2`` + ``title_ja`` 非空」优先
   选主版本；``title_ja`` 以主版本（日文）为准，其余字段取主版本、缺失时从同组
   其它版本补齐。匹配阶段不会把 ``DOG-01`` / ``DOG-001`` 这类不同 key 的行混进
   同一组；物化后的 Title 身份用归一化 ``dvd_id``（契约 §3.4）。
3. **幂等**：字段只在值变化时才写；关联表 / 剧照资产只补缺；重复跑第二次
   ``titles_created == 0``、``titles_updated == 0``、``files_linked == 0``。
4. **manual_lock**：``Title.manual_lock=True`` 的行不覆盖已有字段，只补空
   （关联/剧照也只补缺），并在统计里记 ``manual_locked_skipped``。
5. **空值不倒退**：dump 里某字段为 NULL 时，不会把主库里已有的值清空
   （例如 dump 没有 ``title_ja`` 的旧片，不会擦掉人工补的日文标题）。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Sequence

from sqlalchemy import func

from app.config import Config
from app.db import db
from app.models import (
    Actress,
    Asset,
    Director,
    Genre,
    Label,
    LibraryFile,
    Maker,
    Series,
    Title,
    TitleActress,
    TitleGenre,
)
from app.services.r18 import (
    canonical_dvd_id,
    dvd_id_key,
    gallery_image_count,
    lookup_by_dvd_ids,
    open_dump_db,
)

logger = logging.getLogger(__name__)

__all__ = ["MaterializeStats", "materialize"]

#: DMM 图片 CDN 前缀（契约 §3.6）
PICS_BASE = "https://pics.dmm.co.jp/"
#: 单部剧照 URL 上限（契约 §3.6）
MAX_STILLS = 20
#: 每多少个番号 commit 一次（不是每个番号一次）
_COMMIT_EVERY = 200
#: staging 关联查询的 content_id 批量（本模块逐番号查，留作防御）
_STILL_SUFFIX_RE = re.compile(r"^(?P<base>.*)-(?P<idx>\d+)$")
_RELEASE_DATE_RE = re.compile(r"^(?P<y>\d{4})(?:[-/](?P<m>\d{1,2}))?(?:[-/](?P<d>\d{1,2}))?$")

#: service_code 的稳定排序（digital 元数据最全，mono 次之，rental 最后）
_SERVICE_RANK = {"digital": 0, "mono": 1, "rental": 2}


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
@dataclass
class MaterializeStats:
    """一次物化的统计（契约 §3 的字段 + 便于报告的未命中番号列表）。"""

    codes_considered: int = 0
    matched: int = 0
    titles_created: int = 0
    titles_updated: int = 0
    files_linked: int = 0
    no_dump_match: int = 0
    titles_stubbed: int = 0
    low_confidence_skipped: int = 0
    manual_locked_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    unmatched_codes: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _chunked(items: Sequence[str], size: int = 400) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _is_blank(value) -> bool:
    return value is None or value == ""


def _assign(obj, attr: str, value, *, fill_only: bool = False) -> bool:
    """给 ORM 对象赋非空值；``fill_only`` 时只补空。返回是否真的改了。"""
    if _is_blank(value):
        return False
    current = getattr(obj, attr, None)
    if fill_only and not _is_blank(current):
        return False
    if current == value:
        return False
    setattr(obj, attr, value)
    return True


def _dump_date(conn: sqlite3.Connection) -> str | None:
    """staging meta 里的 dump 日期（写入 Title.source_dump_date）。"""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'dump_date'").fetchone()
    except sqlite3.Error:  # pragma: no cover - meta 表缺失的旧库
        return None
    if row is None:
        return None
    value = row["value"] if isinstance(row, sqlite3.Row) else row[0]
    return str(value) if value else None


def _staging_ready(conn: sqlite3.Connection) -> str | None:
    """检查 staging 库是否可查；不可用时返回错误信息（不抛到 UI）。"""
    try:
        conn.execute("SELECT 1 FROM derived_video LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        return f"staging 库不可用（不是 r18 staging 库？）: {exc}"
    return None


def _parse_release_date(value) -> date | None:
    """把 dump 的 ``YYYY-MM-DD`` / ``YYYY-MM`` / ``YYYY`` 解析成 date。"""
    if _is_blank(value):
        return None
    match = _RELEASE_DATE_RE.match(str(value).strip())
    if match is None:
        return None
    year = int(match.group("y"))
    month = int(match.group("m") or 1)
    day = int(match.group("d") or 1)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _cover_url(jacket: str | None) -> str | None:
    """``jacket_full_url`` → 绝对 URL（契约 §3.6）。"""
    if _is_blank(jacket):
        return None
    path = str(jacket).strip().lstrip("/")
    if path.startswith(("http://", "https://")):
        return path
    if path.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
        return f"{PICS_BASE}{path}"
    return f"{PICS_BASE}{path}.jpg"


def _still_urls(first: str | None, last: str | None) -> list[tuple[int, str]]:
    """按 ``gallery_full_first/last`` 生成剧照 ``(idx, url)``；只建行不下载。"""
    count = gallery_image_count(first, last)
    if not count or count <= 0:
        return []
    match = _STILL_SUFFIX_RE.match(str(first).strip())
    if match is None:
        return []
    base = match.group("base")
    return [
        (idx, f"{PICS_BASE}{base}-{idx}.jpg")
        for idx in range(1, min(count, MAX_STILLS) + 1)
    ]


def _version_rank(row: sqlite3.Row):
    """单个 dump 版本行的优先级（越小越优先）。

    契约 §3.3：优先 ``site_id=2`` + ``title_ja`` 非空；之后再偏好有封面/剧照、
    ``digital`` 版本（元数据最全）。
    """
    title_ja = row["title_ja"]
    return (
        0 if (row["site_id"] == 2 and title_ja) else 1,
        0 if title_ja else 1,
        0 if row["site_id"] == 2 else 1,
        0 if row["jacket_full_url"] else 1,
        0 if (row["gallery_full_first"] and row["gallery_full_last"]) else 1,
        _SERVICE_RANK.get(row["service_code"], 9),
        str(row["content_id"] or ""),
    )


def _pick_versions(rows: Sequence[sqlite3.Row], code: str) -> list[sqlite3.Row]:
    """同番号多版本：按 ``dvd_id_key`` 去重，返回选中分组（已按优先级排序）。

    优先选与输入番号 ``dvd_id_key`` 完全一致的分组（``lookup_by_dvd_ids`` 的
    ``dvd_id_norm`` 补零级可能同时召回 ``DOG-01`` 和 ``DOG-001`` 两个不同
    番号的分组，这里必须只取一个）；没有精确分组时选「组内最佳行最优」的组。
    """
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        key = row["dvd_id_key"] or dvd_id_key(row["dvd_id"]) or ""
        groups.setdefault(key, []).append(row)
    if not groups:
        return []
    wanted = dvd_id_key(code)
    if wanted and wanted in groups:
        chosen = wanted
    else:
        chosen = min(groups, key=lambda k: min(_version_rank(r) for r in groups[k]))
    return sorted(groups[chosen], key=_version_rank)


def _merged(row_list: Sequence[sqlite3.Row], column: str):
    """按版本优先级取第一个非空值。"""
    for row in row_list:
        value = row[column]
        if not _is_blank(value):
            return value
    return None


# ---------------------------------------------------------------------------
# staging 关联查询
# ---------------------------------------------------------------------------
_ACTRESS_SQL = (
    "SELECT a.id, a.name_kanji, a.name_kana, a.name_romaji, a.image_url "
    "FROM derived_video_actress va JOIN derived_actress a ON a.id = va.actress_id "
    "WHERE va.content_id = ? "
    "ORDER BY va.ordinality IS NULL, va.ordinality, a.id"
)
_ACTOR_SQL = (
    "SELECT a.id, a.name_kanji, a.name_kana "
    "FROM derived_video_actor va JOIN derived_actor a ON a.id = va.actor_id "
    "WHERE va.content_id = ? "
    "ORDER BY va.ordinality IS NULL, va.ordinality, a.id"
)
_CATEGORY_SQL = (
    "SELECT c.id, c.name_en, c.name_ja "
    "FROM derived_video_category vc JOIN derived_category c ON c.id = vc.category_id "
    "WHERE vc.content_id = ? ORDER BY c.id"
)
_DIRECTOR_SQL = (
    "SELECT d.id, d.name_kanji, d.name_kana, d.name_romaji "
    "FROM derived_video_director vd JOIN derived_director d ON d.id = vd.director_id "
    "WHERE vd.content_id = ? ORDER BY d.id"
)


def _related(conn: sqlite3.Connection, sql: str, content_ids: Sequence[str]) -> list[sqlite3.Row]:
    """按 content_id 顺序查关联表；主版本没有数据时回退到同组其它版本。"""
    for content_id in content_ids:
        if not content_id:
            continue
        rows = conn.execute(sql, (content_id,)).fetchall()
        if rows:
            return list(rows)
    return []


class _StagingCache:
    """staging 字典表行（maker/label/...）的单次运行缓存。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._rows: dict[tuple[str, int], sqlite3.Row | None] = {}

    def row(self, table: str, row_id) -> sqlite3.Row | None:
        if row_id is None:
            return None
        key = (table, int(row_id))
        if key not in self._rows:
            self._rows[key] = self.conn.execute(
                f"SELECT * FROM {table} WHERE id = ?", (int(row_id),)
            ).fetchone()
        return self._rows[key]


# ---------------------------------------------------------------------------
# 主库字典表的 get-or-create（按 r18_id 复用）
# ---------------------------------------------------------------------------
def _local_ref(model, r18_id, values: dict, cache: dict, *, lookup_field: str | None = None):
    """按 staging id 复用主库字典行；没有行时按名字兜底后新建。

    ``values`` 是「主库字段名 → staging 值」，只写非空值；已有行会顺手用 dump
    的新值补齐/更新名称字段（dump 是名称的权威来源）。
    """
    if r18_id is None:
        return None
    key = (model.__tablename__, int(r18_id))
    if key in cache:
        return cache[key]

    obj = model.query.filter_by(r18_id=int(r18_id)).first()
    if obj is None and lookup_field is not None:
        name = values.get(lookup_field)
        if name:
            # 旧库/人工建的同名行（没有 r18_id）直接收编，避免重复字典项
            obj = (
                model.query.filter_by(**{lookup_field: name})
                .filter(model.r18_id.is_(None))
                .first()
            )
            if obj is not None:
                obj.r18_id = int(r18_id)

    created = False
    if obj is None:
        obj = model(r18_id=int(r18_id))
        db.session.add(obj)
        created = True

    for attr, value in values.items():
        _assign(obj, attr, value)
    if created:
        db.session.flush()
    cache[key] = obj
    return obj


class _TitleIndex:
    """主库已有 Title 的 ``dvd_id_key`` 索引，避免重复建 Title。"""

    def __init__(self) -> None:
        self.by_key: dict[str, Title] = {}
        self.by_canon: dict[str, Title] = {}
        for title in Title.query.all():
            self.add(title)

    def add(self, title: Title) -> None:
        key = dvd_id_key(title.dvd_id)
        if key:
            self.by_key.setdefault(key, title)
        canon = canonical_dvd_id(title.dvd_id)
        if canon:
            self.by_canon.setdefault(canon, title)

    def find(self, code: str) -> Title | None:
        key = dvd_id_key(code)
        title = self.by_key.get(key) if key else None
        if title is not None:
            return title
        # 旧行可能是不补零/带分隔符的写法：再用归一化形态兜一层（契约 §3.4）
        canon = canonical_dvd_id(code)
        return self.by_canon.get(canon) if canon else None

    def resolve_new_dvd_id(self, code: str, dump_dvd_id: str | None) -> str:
        """新 Title 的 ``dvd_id``：契约 §3.4 的归一化形态 ``PREFIX-NNN``。"""
        candidate = canonical_dvd_id(code) or canonical_dvd_id(dump_dvd_id) or ""
        if candidate and candidate not in self.by_canon:
            return candidate
        # canonical 已被别的 key 占用（既有行不是归一化写法）：退回输入的大写形态
        for alt in ((code or "").strip().upper(), (dump_dvd_id or "").strip().upper()):
            if not alt or alt in self.by_canon:
                continue
            if dvd_id_key(alt) and dvd_id_key(alt) not in self.by_key:
                return alt
        raise ValueError(f"无法为番号 {code} 找到不重复的 dvd_id（候选 {candidate}）")


@dataclass
class _RunContext:
    conn: sqlite3.Connection
    dump_date: str | None
    titles: _TitleIndex
    staging: _StagingCache
    refs: dict = field(default_factory=dict)


@dataclass
class _Outcome:
    matched: bool = False
    created: bool = False
    updated: bool = False
    manual_locked: bool = False
    stubbed: bool = False
    files_linked: int = 0


# ---------------------------------------------------------------------------
# 单番号物化
# ---------------------------------------------------------------------------
_STUB_EXCLUDED_PREFIXES = frozenset(
    {"HEYZO", "LUXU", "SIRO", "GACHI", "MAAN", "MIAA", "1PONDO"}
)
_FC2_CODE_RE = re.compile(r"^FC2(?:-PPV)?-\d{5,}$")


def _stub_eligible(code: str) -> bool:
    """高置信度但 dump 没收录的番号，是否值得建一个占位条目。"""
    code = (code or "").strip().upper()
    # FC2-PPV-1234567 这类私有番号单独建条目，便于后续用官方页面 URL 导入元数据
    if _FC2_CODE_RE.match(code):
        return True
    if not re.fullmatch(r"[A-Z][A-Z0-9]{1,5}-\d{3,5}[A-Z]?", code):
        return False
    return code.split("-", 1)[0] not in _STUB_EXCLUDED_PREFIXES


def _materialize_stub(ctx: _RunContext, code: str, files: Sequence[LibraryFile]) -> _Outcome:
    """没有 r18 元数据时建最小 Title，让文件先进片库（封面/剧照可后补上传）。"""
    title = db.session.query(Title).filter(func.upper(Title.dvd_id) == code.upper()).first()
    created = False
    if title is None:
        title = Title(dvd_id=code)
        db.session.add(title)
        db.session.flush()
        created = True
    linked = 0
    for library_file in files:
        if library_file.title_id is None:
            library_file.title_id = title.id
            linked += 1
    return _Outcome(matched=False, created=created, stubbed=True, files_linked=linked)


def _materialize_one(ctx: _RunContext, code: str, rows: Sequence[sqlite3.Row],
                     files: Sequence[LibraryFile],
                     translator: Callable[[str], str | None] | None = None) -> _Outcome:
    versions = _pick_versions(rows, code)
    if not versions:
        # 人工确认过的番号即使 dump 没收录，也建占位 Title，让它离开待确认。
        manual = any(getattr(item, "manual_code", False) for item in files)
        if files and (manual or _stub_eligible(code)):
            return _materialize_stub(ctx, code, files)
        return _Outcome(matched=False)

    primary = versions[0]
    content_ids: list[str] = []
    for row in versions:
        cid = row["content_id"]
        if cid and cid not in content_ids:
            content_ids.append(cid)

    # 关联数据按主版本 content_id 查，主版本没有时回退到同组其它版本
    actress_rows = _related(ctx.conn, _ACTRESS_SQL, content_ids)
    actor_rows = _related(ctx.conn, _ACTOR_SQL, content_ids)
    category_rows = _related(ctx.conn, _CATEGORY_SQL, content_ids)
    director_rows = _related(ctx.conn, _DIRECTOR_SQL, content_ids)

    # ---- 字典表 ----
    maker = None
    maker_id = _merged(versions, "maker_id")
    if maker_id is not None:
        row = ctx.staging.row("derived_maker", maker_id)
        if row is not None:
            maker = _local_ref(
                Maker, maker_id,
                {"name_ja": row["name_ja"], "name_en": row["name_en"]},
                ctx.refs, lookup_field="name_ja",
            )

    label = None
    label_id = _merged(versions, "label_id")
    if label_id is not None:
        row = ctx.staging.row("derived_label", label_id)
        if row is not None:
            label = _local_ref(
                Label, label_id,
                {"name_ja": row["name_ja"], "name_en": row["name_en"]},
                ctx.refs, lookup_field="name_ja",
            )

    series = None
    series_id = _merged(versions, "series_id")
    if series_id is not None:
        row = ctx.staging.row("derived_series", series_id)
        if row is not None:
            series = _local_ref(
                Series, series_id,
                {"name_ja": row["name_ja"], "name_en": row["name_en"]},
                ctx.refs, lookup_field="name_ja",
            )

    director = None
    if director_rows:
        drow = director_rows[0]
        director = _local_ref(
            Director, drow["id"],
            {"name_kanji": drow["name_kanji"], "name_kana": drow["name_kana"],
             "name_romaji": drow["name_romaji"]},
            ctx.refs, lookup_field="name_kanji",
        )

    # ---- Title：找已有行或新建 ----
    title = ctx.titles.find(code)
    created = title is None
    if created:
        dvd_id = ctx.titles.resolve_new_dvd_id(code, primary["dvd_id"])
        title = Title(dvd_id=dvd_id)
        db.session.add(title)
        db.session.flush()
        ctx.titles.add(title)

    fill_only = bool(title.manual_lock)

    title_ja = _merged(versions, "title_ja")
    fields = {
        "content_id": _merged(versions, "content_id"),
        "title_ja": title_ja,
        "title_en": _merged(versions, "title_en"),
        "release_date": _parse_release_date(_merged(versions, "release_date")),
        "runtime_mins": _merged(versions, "runtime_mins"),
        "maker_id": maker.id if maker is not None else None,
        "label_id": label.id if label is not None else None,
        "series_id": series.id if series is not None else None,
        "director_id": director.id if director is not None else None,
        "cover_url": _cover_url(_merged(versions, "jacket_full_url")),
        "source_dump_date": ctx.dump_date,
    }
    changed = False
    for attr, value in fields.items():
        if _assign(title, attr, value, fill_only=fill_only):
            changed = True

    # 中文标题留空（dump 无中文，后续翻译/字幕流程补）；只有调用方显式给了
    # translator 才在有空缺时填。
    if translator is not None and _is_blank(title.title_zh):
        zh = None
        try:
            zh = translator(title.title_ja) if title.title_ja else None
        except Exception:  # noqa: BLE001 - 翻译失败不能中断整批
            logger.warning("title_zh translator 失败: %s", code, exc_info=True)
        if zh:
            title.title_zh = zh
            changed = True

    # 中文字幕标记：该番号任一文件 flags 含 chinese_sub → True（只置 True）
    has_chinese_sub = any(
        "chinese_sub" in (f.flags or "").split(",") for f in files
    )
    if has_chinese_sub and not title.has_chinese_sub:
        title.has_chinese_sub = True
        changed = True

    # ---- 关联：女优 / 男优 ----
    existing_pairs = {
        row.actress_id
        for row in TitleActress.query.filter_by(title_id=title.id).all()
    }
    for role, related_rows in (("female", actress_rows), ("male", actor_rows)):
        for arow in related_rows:
            values = {
                "name_kanji": arow["name_kanji"],
                "name_kana": arow["name_kana"],
            }
            # derived_actress 有 name_romaji / image_url；derived_actor 没有。
            # 注意 image_url 是 staging 的头像 key（如 ``aoi_tukasa.jpg``），不是
            # 绝对 URL；留给后续头像资产任务拼 CDN，这里不猜 URL。
            if "name_romaji" in arow.keys():
                values["name_romaji"] = arow["name_romaji"]
            if "image_url" in arow.keys():
                values["image_url"] = arow["image_url"]
            actress = _local_ref(
                Actress, arow["id"], values, ctx.refs, lookup_field="name_kanji"
            )
            if actress is None or actress.id in existing_pairs:
                continue
            db.session.add(
                TitleActress(title_id=title.id, actress_id=actress.id, role=role)
            )
            existing_pairs.add(actress.id)
            changed = True

    # ---- 关联：题材 ----
    existing_genres = {
        row.genre_id
        for row in TitleGenre.query.filter_by(title_id=title.id).all()
    }
    for crow in category_rows:
        genre = _local_ref(
            Genre, crow["id"],
            {"genre_ja": crow["name_ja"], "genre_en": crow["name_en"]},
            ctx.refs, lookup_field="genre_ja",
        )
        if genre is None or genre.id in existing_genres:
            continue
        db.session.add(TitleGenre(title_id=title.id, genre_id=genre.id, source="r18"))
        existing_genres.add(genre.id)
        changed = True

    # ---- 剧照 URL 行（只建行，不下载）----
    still_rows = _still_urls(
        _merged(versions, "gallery_full_first"),
        _merged(versions, "gallery_full_last"),
    )
    if still_rows:
        existing_stills = {
            asset.idx: asset
            for asset in Asset.query.filter_by(title_id=title.id, kind="still").all()
        }
        for idx, url in still_rows:
            if idx in existing_stills:
                continue
            db.session.add(Asset(title_id=title.id, kind="still", idx=idx, url=url))
            changed = True

    # ---- 回写文件 title_id（含字幕 sidecar）----
    linked = 0
    for library_file in files:
        if library_file.title_id != title.id:
            library_file.title_id = title.id
            linked += 1

    return _Outcome(
        matched=True,
        created=created,
        updated=(not created) and changed,
        manual_locked=(not created) and fill_only,
        files_linked=linked,
    )


# ---------------------------------------------------------------------------
# 候选收集
# ---------------------------------------------------------------------------
def _file_scope_query(*, refresh: bool, code_filter: Sequence[str] | None):
    """候选 LibraryFile 的基础过滤（契约 §3.1）。"""
    query = LibraryFile.query.filter(
        LibraryFile.deleted_at.is_(None),
        LibraryFile.is_sidecar.is_(False),
        LibraryFile.code_guess.isnot(None),
        LibraryFile.code_guess != "",
    )
    if code_filter is not None:
        query = query.filter(LibraryFile.code_guess.in_(list(code_filter)))
    if not refresh:
        query = query.filter(LibraryFile.title_id.is_(None))
    return query


def _collect_codes(*, refresh: bool, min_confidence: int, code_filter: Sequence[str] | None):
    """返回 (候选番号列表, 低置信度跳过的番号数)。"""
    query = _file_scope_query(refresh=refresh, code_filter=code_filter)
    rows = (
        query.filter(LibraryFile.confidence >= min_confidence)
        .with_entities(LibraryFile.code_guess)
        .distinct()
        .all()
    )
    codes = sorted({row[0] for row in rows if row[0]})

    all_scope = _file_scope_query(refresh=refresh, code_filter=code_filter)
    low_rows = (
        all_scope.filter(
            (LibraryFile.confidence.is_(None)) | (LibraryFile.confidence < min_confidence)
        )
        .with_entities(LibraryFile.code_guess)
        .distinct()
        .all()
    )
    low_count = len({row[0] for row in low_rows if row[0]} - set(codes))
    return codes, low_count


def _load_files_by_code(codes: Sequence[str]) -> dict[str, list[LibraryFile]]:
    """一次取出候选番号的全部未删除文件（视频 + 字幕 sidecar）。"""
    result: dict[str, list[LibraryFile]] = {}
    for chunk in _chunked(list(codes)):
        files = (
            LibraryFile.query.filter(
                LibraryFile.deleted_at.is_(None),
                LibraryFile.code_guess.in_(list(chunk)),
            )
            .order_by(LibraryFile.id)
            .all()
        )
        for library_file in files:
            result.setdefault(library_file.code_guess, []).append(library_file)
    return result


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def materialize(
    app,
    *,
    dump_db_path: str | None = None,
    min_confidence: int | None = None,
    limit: int | None = None,
    codes: Iterable[str] | None = None,
    refresh: bool = False,
    progress: Callable[[int, str], None] | None = None,
    on_total: Callable[[int], None] | None = None,
    translator: Callable[[str], str | None] | None = None,
) -> MaterializeStats:
    """把片库番号物化成 Title / 关联 / 剧照 URL。

    参数
    ----
    app : Flask
        调用方负责 ``app.app_context()``（契约 §1）。
    dump_db_path : str | None
        staging 库路径；默认 ``app.config['R18_DUMP_DB']`` / ``Config.R18_DUMP_DB``。
    min_confidence : int | None
        番号置信度阈值；默认 ``Config.CODE_AUTO_MIN``（90）。
    limit : int | None
        本次最多处理多少个番号（批量跑用），按番号排序后截断。
    codes : Iterable[str] | None
        只处理这些番号（在「未关联或 refresh」的候选集里再过滤，契约 §3.1）。
    refresh : bool
        True 时连同已关联 title_id 的文件一起重新物化（用于补全旧标题）。
    progress : callable | None
        ``progress(done, message="")``，每个番号一次 + 结束时一次。
    translator : callable | None
        可选 ``translator(日文标题) -> 中文标题``；默认 None 不调用，``title_zh`` 留空。

    返回 :class:`MaterializeStats`；单番号异常收集进 ``errors``，不中断整批。
    """
    started = time.perf_counter()
    stats = MaterializeStats()

    if min_confidence is None:
        min_confidence = app.config.get("CODE_AUTO_MIN", Config.CODE_AUTO_MIN)
    if dump_db_path:
        dump_path = Path(dump_db_path)
    else:
        dump_path = Path(app.config.get("R18_DUMP_DB") or Config.R18_DUMP_DB)

    code_filter = None
    if codes is not None:
        code_filter = list(dict.fromkeys(code for code in codes if code))

    # 1) 候选番号
    candidate_codes, stats.low_confidence_skipped = _collect_codes(
        refresh=refresh, min_confidence=int(min_confidence), code_filter=code_filter,
    )
    if limit is not None:
        candidate_codes = candidate_codes[: max(int(limit), 0)]
    stats.codes_considered = len(candidate_codes)
    if on_total is not None:
        try:
            on_total(len(candidate_codes))
        except Exception:  # noqa: BLE001
            pass
    if not candidate_codes:
        stats.elapsed_s = round(time.perf_counter() - started, 3)
        if progress is not None:
            progress(0, "没有需要物化的番号")
        return stats

    # 2) 打开 staging（只读）并批量查番号
    try:
        conn = open_dump_db(dump_path)
    except (FileNotFoundError, sqlite3.Error) as exc:
        stats.errors.append(f"staging 库不可用: {exc}")
        stats.codes_considered = 0
        stats.elapsed_s = round(time.perf_counter() - started, 3)
        return stats

    try:
        problem = _staging_ready(conn)
        if problem:
            stats.errors.append(problem)
            stats.codes_considered = 0
            stats.elapsed_s = round(time.perf_counter() - started, 3)
            return stats

        ctx = _RunContext(
            conn=conn,
            dump_date=_dump_date(conn),
            titles=_TitleIndex(),
            staging=_StagingCache(conn),
        )
        matched_rows = lookup_by_dvd_ids(conn, candidate_codes)
        files_by_code = _load_files_by_code(candidate_codes)

        if progress is not None:
            progress(0, f"开始物化 {len(candidate_codes)} 个番号")

        for index, code in enumerate(candidate_codes):
            outcome = _Outcome()
            try:
                with db.session.begin_nested():
                    outcome = _materialize_one(
                        ctx, code, matched_rows.get(code, ()),
                        files_by_code.get(code, ()), translator=translator,
                    )
            except Exception as exc:  # noqa: BLE001 - 单番号失败不中断整批
                logger.exception("物化番号 %s 失败", code)
                stats.errors.append(f"{code}: {type(exc).__name__}: {exc}")
            else:
                if outcome.matched:
                    stats.matched += 1
                else:
                    stats.no_dump_match += 1
                    stats.unmatched_codes.append(code)
                if outcome.created:
                    stats.titles_created += 1
                elif outcome.updated:
                    stats.titles_updated += 1
                if outcome.stubbed:
                    stats.titles_stubbed += 1
                if outcome.manual_locked:
                    stats.manual_locked_skipped += 1
                stats.files_linked += outcome.files_linked

            if (index + 1) % _COMMIT_EVERY == 0:
                db.session.commit()
            if progress is not None:
                progress(index + 1, code)

        db.session.commit()
    finally:
        conn.close()

    stats.elapsed_s = round(time.perf_counter() - started, 3)
    logger.info(
        "materialize 完成: considered=%s matched=%s created=%s updated=%s "
        "files_linked=%s unmatched=%s errors=%s elapsed=%.3fs",
        stats.codes_considered, stats.matched, stats.titles_created,
        stats.titles_updated, stats.files_linked, stats.no_dump_match,
        len(stats.errors), stats.elapsed_s,
    )
    if progress is not None:
        progress(
            stats.codes_considered,
            f"完成：命中 {stats.matched}/{stats.codes_considered}，"
            f"新建 {stats.titles_created}，更新 {stats.titles_updated}",
        )
    return stats
