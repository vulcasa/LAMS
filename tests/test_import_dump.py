"""r18.dev dump COPY 解析与 staging 导入的测试。

fixture 是手写的 ~25 行 pg_dump 片段（含 CREATE TABLE 干扰语句、``\\.`` 终止行），
覆盖：
- ``\\N`` NULL vs 空字符串 ``''``；
- ``\\\\`` / ``\\t`` / ``\\n`` / ``\\r`` 转义；
- 八进制 ``\\346\\227\\245`` 拼出的多字节 UTF-8（日本語）与十六进制 ``\\x41``；
- 转义后的字面量 ``\\N``（dump 里写作 ``\\\\N``）；
- dvd_id 的各种形态（正常/小写/两位数字/空串/NULL）与 content_id 兜底。

跑法（项目根 = av-archive/）::

    .venv/bin/python -m pytest tests/test_import_dump.py -q
"""
from __future__ import annotations

import gzip
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_r18():
    """按文件路径加载 r18 模块（避免连带 import Flask 应用包）。"""
    module_path = ROOT / "app" / "services" / "r18.py"
    spec = importlib.util.spec_from_file_location("avlib_r18_under_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


r18 = _load_r18()


# ---------------------------------------------------------------------------
# 手写 fixture（字段值用 raw string 写 COPY 转义，字段之间是真 tab）
# ---------------------------------------------------------------------------
_VIDEO_COLUMNS = (
    "content_id", "dvd_id", "title_en", "title_ja", "comment_en", "comment_ja",
    "runtime_mins", "release_date", "sample_url", "maker_id", "label_id",
    "series_id", "jacket_full_url", "jacket_thumb_url", "gallery_full_first",
    "gallery_full_last", "gallery_thumb_first", "gallery_thumb_last",
    "site_id", "service_code",
)

_VIDEO_ROWS = [
    # 1. 常规行：多个 NULL，gallery jp-1..jp-5 → 4 张
    (
        "100tv00031", "100TV-031", "This Is What Happened", r"\N", r"\N", r"\N",
        "13", "2019-07-21", r"\N", "6733", "46007", "222184",
        "digital/video/100tv00031/100tv00031pl",
        "digital/video/100tv00031/100tv00031ps",
        "digital/video/100tv00031/100tv00031jp-1",
        "digital/video/100tv00031/100tv00031jp-5",
        "digital/video/100tv00031/100tv00031-1",
        "digital/video/100tv00031/100tv00031-5",
        "2", "digital",
    ),
    # 2. 转义行：title_en 里有 \t，comment_en 里有 \n，comment_ja 里有 \r；
    #    gallery jp-1..jp-10 → 10 张；jacket_thumb 为 NULL
    (
        "0snis00584", "SNIS-584", r"tab\there", "波多野結衣", r"line1\nline2",
        r"carriage\rreturn", "120", "2016-01-07", "https://example.invalid/sample",
        "1", "2", "3",
        "digital/video/0snis00584/0snis00584pl",
        r"\N",
        "digital/video/0snis00584/0snis00584jp-1",
        "digital/video/0snis00584/0snis00584jp-10",
        "digital/video/0snis00584/0snis00584-1",
        "digital/video/0snis00584/0snis00584-10",
        "2", "digital",
    ),
    # 3. dvd_id 为 NULL、title_en 为空字段、八进制/十六进制转义、字面量 \N
    (
        "h_1234abc", r"\N", "", r"\346\227\245\346\234\254\350\252\236",
        r"\x41\x42", r"\\N", r"\N", r"\N", r"\N", r"\N", r"\N", r"\N",
        r"\N", r"\N", r"\N", r"\N", r"\N", r"\N", "1", "digital",
    ),
    # 4. 两位数字番号（归一化补零测试）、gallery 终止于 -0 → 0 张
    (
        "15ihkd00006", "IHKD-06", "Lower Case Id", r"\N", r"\N", r"\N",
        "89", "2020-02-02", r"\N", "5", "6", "7",
        "digital/video/15ihkd00006/15ihkd00006pl",
        "digital/video/15ihkd00006/15ihkd00006ps",
        "digital/video/15ihkd00006/15ihkd00006-1",
        "digital/video/15ihkd00006/15ihkd00006-0",
        "digital/video/15ihkd00006/15ihkd00006-1",
        "digital/video/15ihkd00006/15ihkd00006-0",
        "2", "digital",
    ),
    # 5. 只有日文标题、日文原样 UTF-8、gallery jp-1..jp-12 → 12 张
    (
        "140m919", "M-919", r"\N", "彼女のカノジョと*20", r"\N", r"\N",
        "122", "2008-06-27", r"\N", "40145", "789", "6596",
        "digital/video/140m00919/140m00919pl",
        "digital/video/140m00919/140m00919ps",
        "digital/video/140m00919/140m00919jp-1",
        "digital/video/140m00919/140m00919jp-12",
        "digital/video/140m00919/140m00919-1",
        "digital/video/140m00919/140m00919-12",
        "2", "digital",
    ),
    # 6. dvd_id 是空字符串（不是 NULL）、其余几乎全空
    (
        "kiray_017_akari", "", r"\N", r"\N", r"\N", r"\N", r"\N", r"\N",
        r"\N", r"\N", r"\N", r"\N", r"\N", r"\N", r"\N", r"\N", r"\N", r"\N",
        "1", "digital",
    ),
]

_ACTRESS_COLUMNS = ("id", "name_romaji", "image_url", "name_kanji", "name_kana")
_ACTRESS_ROWS = [
    ("1", "Mari", "mari4", r"\N", r"\N"),
    ("366", r"\N", r"\N", "雨野夕紀", "あまのゆうき"),
    ("442", r"\N", r"\N", "岡本かおり", "おかもとかおり"),
    ("500", "Some", "img/500.jpg", r"\N", r"\N"),
    ("7", r"\N", r"\N", r"\N", r"\N"),
]

_CATEGORY_COLUMNS = ("id", "name_en", "name_ja")
_CATEGORY_ROWS = [
    ("29", "G*******g", r"\N"),
    ("102", "Big Tits", "巨乳"),
]

_VIDEO_ACTRESS_COLUMNS = ("content_id", "actress_id", "ordinality", "release_date")
_VIDEO_ACTRESS_ROWS = [
    ("100tv00031", "23130", "1", "2019-06-01"),
    ("0snis00584", "1025419", "1", "2016-01-07"),
    ("0snis00584", "26562", "2", "2016-01-07"),
    ("140m919", "366", "1", "2008-06-27"),
]

_VIDEO_CATEGORY_COLUMNS = ("content_id", "category_id", "release_date")
_VIDEO_CATEGORY_ROWS = [
    ("100tv00031", "102", "2019-06-01"),
    ("100tv00031", "1027", "2019-06-01"),
    ("0snis00584", "102", "2016-01-07"),
    ("h_1234abc", "2006", r"\N"),
]

_MT_COLUMNS = ("id", "source_ja", "target_en", "timestamp")
_MT_ROWS = [
    ("1", "日本語のタイトル", "A Japanese title", "2024-01-01 00:00:00+00"),
    ("2", "こんにちは", "hello", "2024-01-01 00:00:00+00"),
]

_TRAILER_COLUMNS = ("content_id", "url", "timestamp")
_TRAILER_ROWS = [
    ("100tv00031", "https://example.invalid/trailer1.mp4", "2024-01-01 00:00:00+00"),
    ("0snis00584", "https://example.invalid/trailer2.mp4", "2024-01-01 00:00:00+00"),
]

_PRELUDE = """--
-- PostgreSQL database dump（测试用精简 fixture）
--
SET statement_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;

CREATE TABLE public.derived_video (
    content_id character varying NOT NULL,
    dvd_id character varying,
    title_en character varying,
    title_ja character varying
);
"""


def build_fixture_sql() -> str:
    """拼出 fixture dump 文本（字段之间是真 tab，字段内部保留 COPY 转义）。"""
    parts: list[str] = [_PRELUDE]

    def section(table: str, columns, rows) -> None:
        parts.append(f"COPY public.{table} ({', '.join(columns)}) FROM stdin;")
        for row in rows:
            assert len(row) == len(columns), (table, len(row), len(columns))
            parts.append("\t".join(row))
        parts.append("\\.")
        parts.append("")

    section("derived_actress", _ACTRESS_COLUMNS, _ACTRESS_ROWS)
    section("derived_category", _CATEGORY_COLUMNS, _CATEGORY_ROWS)
    section("derived_video", _VIDEO_COLUMNS, _VIDEO_ROWS)
    section("derived_video_actress", _VIDEO_ACTRESS_COLUMNS, _VIDEO_ACTRESS_ROWS)
    section("derived_video_category", _VIDEO_CATEGORY_COLUMNS, _VIDEO_CATEGORY_ROWS)
    section("machine_translation", _MT_COLUMNS, _MT_ROWS)
    section("source_dmm_trailer", _TRAILER_COLUMNS, _TRAILER_ROWS)
    parts.append("-- PostgreSQL database dump complete")
    return "\n".join(parts) + "\n"


@pytest.fixture()
def fixture_sql(tmp_path: Path) -> Path:
    path = tmp_path / "fixture.sql"
    path.write_text(build_fixture_sql(), encoding="utf-8")
    return path


@pytest.fixture()
def imported_db(tmp_path: Path, fixture_sql: Path):
    out = tmp_path / "staging.db"
    result = r18.import_dump(fixture_sql, out, compute_sha256=False)
    conn = sqlite3.connect(out)
    conn.row_factory = sqlite3.Row
    try:
        yield result, conn, out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. 转义 / NULL 解析
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (rb"\\", b"\\"),
        (rb"\t", b"\t"),
        (rb"\n", b"\n"),
        (rb"\r", b"\r"),
        (rb"\b", b"\b"),
        (rb"\f", b"\f"),
        (rb"\v", b"\v"),
        (rb"\346\227\245\346\234\254\350\252\236", "日本語".encode("utf-8")),
        (rb"\x41\x42", b"AB"),
        (rb"\0601", b"01"),  # 八进制最多取 3 位：\060='0' 然后 '1'
        (rb"\q", b"q"),      # 未知转义 → 原字符
        (rb"plain ascii", b"plain ascii"),
        (rb"", b""),
    ],
)
def test_unescape_copy_bytes(raw: bytes, expected: bytes) -> None:
    assert r18.unescape_copy_bytes(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (rb"\N", None),
        (b"", ""),
        (rb"\\N", "\\N"),          # 转义后的字面量 \N
        (rb"\346\227\245", "日"),   # 八进制拼出的 UTF-8
        (rb"a\tb", "a\tb"),
        (b"plain", "plain"),
    ],
)
def test_decode_copy_field(raw: bytes, expected) -> None:
    assert r18.decode_copy_field(raw) == expected


def test_parse_copy_header() -> None:
    header = r18.parse_copy_header(
        b'COPY public.derived_video (content_id, dvd_id, "timestamp") FROM stdin;'
    )
    assert header == ("derived_video", ("content_id", "dvd_id", "timestamp"))
    assert r18.parse_copy_header(b"SET client_encoding = 'UTF8';") is None


def test_iter_copy_records_decodes_fixture(fixture_sql: Path) -> None:
    records = list(r18.iter_copy_records(fixture_sql))
    tables = [table for table, _columns, _row in records]
    assert tables.count("derived_video") == len(_VIDEO_ROWS)
    assert tables.count("machine_translation") == len(_MT_ROWS)

    by_content = {row[0]: row for table, _c, row in records if table == "derived_video"}
    first = by_content["100tv00031"]
    assert first[3] is None          # title_ja NULL
    assert first[6] == "13"          # runtime 是字符串（原始层不解类型）
    second = by_content["0snis00584"]
    assert second[2] == "tab\there"  # \t → 真 tab
    assert second[4] == "line1\nline2"
    assert second[5] == "carriage\rreturn"
    third = by_content["h_1234abc"]
    assert third[1] is None          # dvd_id NULL
    assert third[2] == ""            # 空字段 ≠ NULL
    assert third[3] == "日本語"       # 八进制拼出的多字节 UTF-8
    assert third[4] == "AB"          # \x41\x42
    assert third[5] == "\\N"         # 字面量 \N


def test_iter_copy_records_can_filter_tables(fixture_sql: Path) -> None:
    records = list(r18.iter_copy_records(fixture_sql, tables={"derived_actress"}))
    assert {table for table, _c, _r in records} == {"derived_actress"}
    assert len(records) == len(_ACTRESS_ROWS)


# ---------------------------------------------------------------------------
# 2. 番号 / 图片工具
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("100TV-031", "100tv031"),
        ("100tv031", "100tv031"),
        ("SNIS-584", "snis584"),
        ("SSIS001", "ssis001"),
        ("SSIS-001", "ssis001"),
        ("IHKD-06", "ihkd006"),
        ("ihkd-6", "ihkd006"),
        ("M-919", "m919"),
        ("T28-123", "t28123"),       # 前缀含数字：走兜底 key，不补零
        ("h_1234abc", "h1234abc"),
        ("kiray_017_akari", "kiray017akari"),
        ("UD321R", "ud321r"),
        ("", None),
        (None, None),
        ("   ", None),
    ],
)
def test_dvd_id_norm(value, expected) -> None:
    assert r18.dvd_id_norm(value) == expected


def test_dvd_id_norm_padding_makes_variants_meet() -> None:
    assert r18.dvd_id_norm("ABC-1") == r18.dvd_id_norm("abc-001") == "abc001"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("100TV-031", "100tv031"),
        ("100tv031", "100tv031"),
        ("DOG-01", "dog01"),
        ("DOG-001", "dog001"),     # 补零 norm 会并成 dog001，精确 key 不会
        ("IHKD-06", "ihkd06"),
        ("SSIS001", "ssis001"),
        ("UD321R", "ud321r"),
        (None, None),
        ("   ", None),
    ],
)
def test_dvd_id_key(value, expected) -> None:
    assert r18.dvd_id_key(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ihkd-6", "IHKD-006"),
        ("100tv-031", "100TV-031"),
        ("UD321R", "UD321R"),
        (None, None),
    ],
)
def test_canonical_dvd_id(value, expected) -> None:
    assert r18.canonical_dvd_id(value) == expected


def test_content_id_norm() -> None:
    assert r18.content_id_norm("h_1234abc") == "h1234abc"
    assert r18.content_id_norm("0snis00584") == "0snis00584"
    assert r18.content_id_norm(None) is None


@pytest.mark.parametrize(
    ("first", "last", "expected"),
    [
        ("digital/video/x/xjp-1", "digital/video/x/xjp-10", 10),
        ("digital/video/x/xjp-5", "digital/video/x/xjp-5", 1),
        ("digital/video/x/xjp-1", "digital/video/x/xjp-0", 0),
        ("digital/video/x/xjp-3", "digital/video/x/xjp-1", None),
        ("digital/video/x/xjp-1", "digital/video/y/yjp-3", None),
        (None, None, None),
        ("", "", None),
    ],
)
def test_gallery_image_count(first, last, expected) -> None:
    assert r18.gallery_image_count(first, last) == expected


# ---------------------------------------------------------------------------
# 3. 导入端到端
# ---------------------------------------------------------------------------
def test_import_dump_end_to_end(imported_db) -> None:
    result, conn, out = imported_db
    assert out.is_file()
    assert result.tables["derived_video"].rows == len(_VIDEO_ROWS)
    assert result.tables["derived_video"].imported is True
    assert result.tables["derived_actress"].rows == len(_ACTRESS_ROWS)
    # 跳过表也要计数
    assert result.tables["machine_translation"].rows == 2
    assert result.tables["machine_translation"].imported is False
    assert result.imported_rows == (
        len(_VIDEO_ROWS) + len(_ACTRESS_ROWS) + len(_CATEGORY_ROWS)
        + len(_VIDEO_ACTRESS_ROWS) + len(_VIDEO_CATEGORY_ROWS)
    )

    counts = {
        "derived_video": len(_VIDEO_ROWS),
        "derived_actress": len(_ACTRESS_ROWS),
        "derived_category": len(_CATEGORY_ROWS),
        "derived_video_actress": len(_VIDEO_ACTRESS_ROWS),
        "derived_video_category": len(_VIDEO_CATEGORY_ROWS),
    }
    for table, expected in counts.items():
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == expected
    # 跳过表不应该建表
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "machine_translation" not in tables
    assert "source_dmm_trailer" not in tables

    # 具体值：NULL / 空串 / 转义 / 类型转换
    row = conn.execute(
        "SELECT * FROM derived_video WHERE content_id = '0snis00584'"
    ).fetchone()
    assert row["dvd_id"] == "SNIS-584"
    assert row["title_en"] == "tab\there"
    assert row["title_ja"] == "波多野結衣"
    assert row["comment_en"] == "line1\nline2"
    assert row["comment_ja"] == "carriage\rreturn"
    assert row["runtime_mins"] == 120          # INTEGER 列
    assert row["release_date"] == "2016-01-07"  # 日期按 TEXT 保存
    assert row["jacket_thumb_url"] is None
    assert row["dvd_id_key"] == "snis584"
    assert row["dvd_id_norm"] == "snis584"
    assert row["content_id_norm"] == "0snis00584"

    octal_row = conn.execute(
        "SELECT * FROM derived_video WHERE content_id = 'h_1234abc'"
    ).fetchone()
    assert octal_row["dvd_id"] is None
    assert octal_row["title_en"] == ""
    assert octal_row["title_ja"] == "日本語"
    assert octal_row["comment_en"] == "AB"
    assert octal_row["comment_ja"] == "\\N"
    assert octal_row["content_id_norm"] == "h1234abc"
    assert octal_row["dvd_id_norm"] is None

    # 两位数字番号补零
    row = conn.execute(
        "SELECT dvd_id_norm FROM derived_video WHERE dvd_id = 'IHKD-06'"
    ).fetchone()
    assert row["dvd_id_norm"] == "ihkd006"

    # 索引真的建了
    indexes = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )}
    for expected_index in (
        "idx_video_dvd_id", "idx_video_dvd_id_key", "idx_video_dvd_id_norm",
        "idx_video_content_id_norm",
        "idx_video_actress_actress_id", "idx_video_category_category_id",
    ):
        assert expected_index in indexes


def test_import_meta_and_user_version(imported_db, fixture_sql: Path) -> None:
    _result, conn, _out = imported_db
    meta = {row[0]: row[1] for row in conn.execute("SELECT key, value FROM meta")}
    assert meta["dump_file"] == "fixture.sql"
    assert meta["dump_date"] == r18.datetime.fromtimestamp(
        fixture_sql.stat().st_mtime, r18.timezone.utc
    ).strftime("%Y-%m-%d")
    assert meta["user_version"] == str(r18.SCHEMA_VERSION)
    assert meta["schema_version"] == str(r18.SCHEMA_VERSION)
    assert meta["importer_version"] == r18.IMPORTER_VERSION
    assert meta["rows:derived_video"] == str(len(_VIDEO_ROWS))
    assert meta["imported:machine_translation"] == "0"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == r18.SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_import_is_idempotent(tmp_path: Path, fixture_sql: Path) -> None:
    out = tmp_path / "staging.db"
    first = r18.import_dump(fixture_sql, out, compute_sha256=False)
    second = r18.import_dump(fixture_sql, out, compute_sha256=False)
    assert first.imported_rows == second.imported_rows
    conn = sqlite3.connect(out)
    try:
        assert conn.execute("SELECT COUNT(*) FROM derived_video").fetchone()[0] == len(_VIDEO_ROWS)
        # meta 不会重复追加
        assert conn.execute(
            "SELECT COUNT(*) FROM meta WHERE key = 'dump_file'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_import_batch_boundaries(tmp_path: Path, fixture_sql: Path) -> None:
    out = tmp_path / "staging.db"
    result = r18.import_dump(
        fixture_sql, out, batch_size=2, compute_sha256=False
    )
    assert result.tables["derived_video"].rows == len(_VIDEO_ROWS)
    conn = sqlite3.connect(out)
    try:
        assert conn.execute("SELECT COUNT(*) FROM derived_video").fetchone()[0] == len(_VIDEO_ROWS)
        assert conn.execute("SELECT COUNT(*) FROM derived_video_actress").fetchone()[0] == 4
    finally:
        conn.close()


def test_import_gzip_source(tmp_path: Path, fixture_sql: Path) -> None:
    gz_path = tmp_path / "fixture.sql.gz"
    with open(fixture_sql, "rb") as src, gzip.open(gz_path, "wb") as dst:
        dst.write(src.read())
    out = tmp_path / "staging.db"
    result = r18.import_dump(gz_path, out, compute_sha256=True)
    assert result.tables["derived_video"].rows == len(_VIDEO_ROWS)
    assert result.sha256 is not None and len(result.sha256) == 64


def test_import_selected_tables_only(tmp_path: Path, fixture_sql: Path) -> None:
    out = tmp_path / "staging.db"
    result = r18.import_dump(
        fixture_sql, out, tables=["derived_actress"], compute_sha256=False
    )
    assert result.tables["derived_actress"].imported is True
    # 没选中的表只计数、不建表
    assert result.tables["derived_video"].imported is False
    assert result.tables["derived_video"].rows == len(_VIDEO_ROWS)
    conn = sqlite3.connect(out)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "derived_actress" in tables
        assert "derived_video" not in tables
    finally:
        conn.close()


def test_import_unknown_table_raises(tmp_path: Path, fixture_sql: Path) -> None:
    with pytest.raises(ValueError, match="不支持的表"):
        r18.import_dump(fixture_sql, tmp_path / "x.db", tables=["no_such_table"])


def test_import_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        r18.import_dump(tmp_path / "missing.sql", tmp_path / "x.db")


def test_import_malformed_row_raises_and_cleans_temp(tmp_path: Path) -> None:
    bad_row = "\t".join(str(i) for i in range(19))  # 少一列
    sql = (
        "COPY public.derived_video (" + ", ".join(_VIDEO_COLUMNS) + ") FROM stdin;\n"
        + bad_row + "\n"
        + "\\.\n"
    )
    sql_path = tmp_path / "bad.sql"
    sql_path.write_text(sql, encoding="utf-8")
    out = tmp_path / "bad.db"
    with pytest.raises(r18.CopyFormatError, match="derived_video"):
        r18.import_dump(sql_path, out, compute_sha256=False)
    assert not out.exists()
    assert not (tmp_path / "bad.db.tmp").exists()


def test_dump_schema_change_reorders_columns(tmp_path: Path) -> None:
    """dump 列顺序变化时按列名重排（r18 改 schema 时不至于灌错列）。"""
    shuffled_columns = tuple(reversed(_VIDEO_COLUMNS))
    original = dict(zip(_VIDEO_COLUMNS, _VIDEO_ROWS[0]))
    shuffled_row = tuple(original[column] for column in shuffled_columns)
    sql = (
        "COPY public.derived_video (" + ", ".join(shuffled_columns) + ") FROM stdin;\n"
        + "\t".join(shuffled_row) + "\n\\.\n"
    )
    sql_path = tmp_path / "reordered.sql"
    sql_path.write_text(sql, encoding="utf-8")
    out = tmp_path / "reordered.db"
    result = r18.import_dump(sql_path, out, compute_sha256=False)
    assert result.tables["derived_video"].rows == 1
    conn = sqlite3.connect(out)
    try:
        row = conn.execute(
            "SELECT content_id, dvd_id, title_en, title_ja, runtime_mins, site_id "
            "FROM derived_video"
        ).fetchone()
        assert row == ("100tv00031", "100TV-031", "This Is What Happened", None, 13, 2)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. 只读查询与覆盖率统计
# ---------------------------------------------------------------------------
def test_open_dump_db_is_read_only(tmp_path: Path, imported_db) -> None:
    _result, _conn, out = imported_db
    conn = r18.open_dump_db(out)
    try:
        assert conn.execute("SELECT COUNT(*) FROM derived_video").fetchone()[0] == len(_VIDEO_ROWS)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM derived_video")
    finally:
        conn.close()


def test_lookup_by_dvd_ids(imported_db) -> None:
    _result, _conn, out = imported_db
    conn = r18.open_dump_db(out)
    try:
        found = r18.lookup_by_dvd_ids(
            conn, ["100TV-031", "ihkd-6", "h_1234abc", "nope-999"]
        )
    finally:
        conn.close()
    assert set(found) == {"100TV-031", "ihkd-6", "h_1234abc"}
    assert [row["content_id"] for row in found["100TV-031"]] == ["100tv00031"]
    # IHKD-06 在 dump 里是两位数字，归一化补零后命中
    assert [row["content_id"] for row in found["ihkd-6"]] == ["15ihkd00006"]
    # dvd_id 为 NULL 的条目靠 content_id 兜底命中
    assert [row["content_id"] for row in found["h_1234abc"]] == ["h_1234abc"]


def test_lookup_prefers_exact_key_over_padded(tmp_path: Path) -> None:
    """精确 key 优先：DOG-001 不会连带命中 DOG-01（两部不同的片）。"""
    rows = [
        ("189dog01", "DOG-01", "おじさん", r"\N", r"\N", r"\N", r"\N", "2010-01-01",
         r"\N", r"\N", r"\N", r"\N", r"\N", r"\N", r"\N", r"\N", r"\N", r"\N",
         "2", "mono"),
        ("436dog001", "DOG-001", "盗撮", r"\N", r"\N", r"\N", r"\N", "2012-01-01",
         r"\N", r"\N", r"\N", r"\N", r"\N", r"\N", r"\N", r"\N", r"\N", r"\N",
         "2", "digital"),
    ]
    sql = (
        "COPY public.derived_video (" + ", ".join(_VIDEO_COLUMNS) + ") FROM stdin;\n"
        + "\n".join("\t".join(row) for row in rows) + "\n\\.\n"
    )
    sql_path = tmp_path / "dog.sql"
    sql_path.write_text(sql, encoding="utf-8")
    out = tmp_path / "dog.db"
    r18.import_dump(sql_path, out, compute_sha256=False)
    conn = r18.open_dump_db(out)
    try:
        exact = r18.lookup_by_dvd_ids(conn, ["DOG-001"])
        assert [row["content_id"] for row in exact["DOG-001"]] == ["436dog001"]
        exact01 = r18.lookup_by_dvd_ids(conn, ["DOG-01"])
        assert [row["content_id"] for row in exact01["DOG-01"]] == ["189dog01"]
        # 用户写 DOG-1（dump 里没有这个精确番号）→ 退到补零 norm，两条都命中（有歧义）
        fallback = r18.lookup_by_dvd_ids(conn, ["DOG-1"])
        assert {row["content_id"] for row in fallback["DOG-1"]} == {"189dog01", "436dog001"}
    finally:
        conn.close()


def test_lookup_ssis001_variants(tmp_path: Path) -> None:
    """验收要求：SSIS-001 / ssis001 / SSIS001 都应命中同一 dump 番号，content_id 可兜底。"""

    def row(**kwargs) -> tuple:
        values = [r"\N"] * len(_VIDEO_COLUMNS)
        for name, value in kwargs.items():
            values[_VIDEO_COLUMNS.index(name)] = value
        return tuple(values)

    rows = [
        row(content_id="ssis00001", dvd_id="SSIS-001", title_ja="デビュー作", site_id="2",
            service_code="digital"),
        row(content_id="ssis001", dvd_id="SSIS-001", title_ja="デビュー作", site_id="2",
            service_code="mono"),
        # dvd_id 为 NULL 的条目只能靠 content_id 兜底
        row(content_id="ssis00002", title_ja="素人作品", site_id="2", service_code="digital"),
    ]
    sql = (
        "COPY public.derived_video (" + ", ".join(_VIDEO_COLUMNS) + ") FROM stdin;\n"
        + "\n".join("\t".join(values) for values in rows) + "\n\\.\n"
    )
    sql_path = tmp_path / "ssis.sql"
    sql_path.write_text(sql, encoding="utf-8")
    out = tmp_path / "ssis.db"
    r18.import_dump(sql_path, out, compute_sha256=False)
    conn = r18.open_dump_db(out)
    try:
        for code in ("SSIS-001", "ssis001", "SSIS001", "ssis-001"):
            found = r18.lookup_by_dvd_ids(conn, [code])
            assert {row["content_id"] for row in found[code]} == {"ssis00001", "ssis001"}, code

        fallback = r18.lookup_by_dvd_ids(conn, ["ssis00002"])
        assert [row["content_id"] for row in fallback["ssis00002"]] == ["ssis00002"]

        miss = r18.lookup_by_dvd_ids(conn, ["SSIS-99999"])
        assert miss == {}
    finally:
        conn.close()


def test_lookup_by_dvd_ids_empty_input(imported_db) -> None:
    _result, _conn, out = imported_db
    conn = r18.open_dump_db(out)
    try:
        assert r18.lookup_by_dvd_ids(conn, []) == {}
        assert r18.lookup_by_dvd_ids(conn, [""]) == {}
    finally:
        conn.close()


def test_coverage_stats_on_fixture(imported_db) -> None:
    _result, conn, _out = imported_db
    stats = r18.coverage_stats(conn)
    video = stats["video"]
    assert video["rows"] == len(_VIDEO_ROWS)
    assert video["dvd_id_null"] == 1
    assert video["dvd_id_empty"] == 1
    assert video["dvd_id_usable"] == len(_VIDEO_ROWS) - 2
    # title_en 非空：行 1/2/4 → 3；title_ja 非空：行 2/3/5 → 3
    assert video["title_en_nonempty"] == 3
    assert video["title_ja_nonempty"] == 3
    assert video["dvd_id_key_distinct"] == 4
    assert video["dvd_id_padding_collisions"] == 0
    assert video["title_ja_and_en_nonempty"] == 1  # 只有行 2
    actress = stats["actress"]
    assert actress["name_kanji_nonempty"] == 2
    assert actress["name_kana_nonempty"] == 2
    assert actress["name_romaji_nonempty"] == 2
    assert actress["image_url_nonempty"] == 2  # 行 1 "mari4" / 行 4 "img/500.jpg"
    assert actress["all_names_empty"] == 1
    gallery = stats["gallery"]
    assert gallery["full_first_nonnull"] == 4
    assert gallery["count_distribution"]["0"] == 1
    assert gallery["count_distribution"]["1-5"] == 1
    assert gallery["count_distribution"]["6-10"] == 1
    assert gallery["count_distribution"]["11-15"] == 1
    prefixes = {item["prefix"]: item["count"] for item in stats["dvd_id_prefixes"]}
    assert prefixes["100TV"] == 1
    assert prefixes["SNIS"] == 1
    assert prefixes["IHKD"] == 1
    assert prefixes["M"] == 1
    assert stats["dvd_id_no_dash"] == 0
