r"""r18.dev 每周 SQL dump —— 解析 / staging 导入 / 覆盖率统计。

背景
----
r18.dev 每周二发布一份 PostgreSQL ``pg_dump``（plain 格式，纯 COPY 数据，
gzip 后约 262 MiB，解压约 1.3 GB / 1900 万行）。``derived_video`` 有 190 万+
部，但用户实际片库只有 2–4 万部——所以**不把全量灌进主库**，而是把需要的表
批量导入独立的 staging 库（默认 ``data/r18dump.db``），主库只按用户实际番号
命中后物化（见 docs/PLAN.md 3.2 / 4.2）。

本模块只依赖标准库（``sqlite3`` / ``re`` / ``gzip``），不 import Flask，
可以被 CLI（系统 python3）和测试独立使用。三层 API：

1. COPY 文本格式解析
   ``iter_copy_sections`` / ``iter_copy_records`` / ``decode_copy_field`` /
   ``unescape_copy_bytes``。
   pg_dump 的 NULL 是 ``\N``；``\\`` ``\t`` ``\n`` ``\r`` ``\b`` ``\f`` ``\v``
   以及八进制（``\ooo``）/十六进制（``\xhh``）转义都会还原。dump 里的非 ASCII
   字符是原样 UTF-8（不是 ``\ooo``），但多字节字符也可能由多个八进制转义拼成，
   所以先还原成 bytes、再整体 decode('utf-8')。
2. staging 库构建
   ``import_dump()``：幂等（先写 ``*.tmp`` 再 ``os.replace`` 原子替换）；
   meta 表记录 dump 文件名/日期/sha256/importer 版本/``PRAGMA user_version``；
   批量 ``executemany`` + 每 1 万行一个事务；索引在全部数据灌完后统一建。
3. 只读使用辅助
   ``open_dump_db`` / ``lookup_by_dvd_ids`` / ``coverage_stats`` /
   ``gallery_image_count`` / ``dvd_id_norm``。

番号归一化（重要）
------------------
staging 里 ``derived_video`` 除保留 dump 原值 ``dvd_id`` 外，额外存两列查询键：

* ``dvd_id_norm``：番号归一化键，见 :func:`dvd_id_norm`（大写、数字部分补零到
  3 位、去分隔符，例如 ``100TV-031`` → ``100tv031``；``IHKD-06`` → ``ihkd006``）；
* ``content_id_norm``：content_id 去分隔符后的键（无 dvd_id 的素人/无码条目
  只能靠它匹配，例如 ``h_1234abc`` → ``h1234abc``）。

两列都有索引，主库物化时按归一化键 IN 查询即可（:func:`lookup_by_dvd_ids`）。
"""
from __future__ import annotations

import gzip
import hashlib
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

__all__ = [
    # 解析
    "CopyFormatError",
    "CopySection",
    "NULL_TOKEN",
    "unescape_copy_bytes",
    "decode_copy_field",
    "parse_copy_header",
    "iter_copy_sections",
    "iter_copy_records",
    "open_dump",
    # 番号/图片工具
    "dvd_id_key",
    "dvd_id_norm",
    "canonical_dvd_id",
    "content_id_norm",
    "gallery_image_count",
    # 导入
    "TableSpec",
    "TableStat",
    "ImportResult",
    "CORE_TABLES",
    "KNOWN_DUMP_TABLES",
    "import_dump",
    # 只读使用
    "open_dump_db",
    "lookup_by_dvd_ids",
    "coverage_stats",
    "IMPORTER_VERSION",
    "SCHEMA_VERSION",
    "DEFAULT_BATCH_SIZE",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
IMPORTER_VERSION = "1.0.0"
# staging 库结构版本，写进 PRAGMA user_version；结构不兼容变更时 +1
SCHEMA_VERSION = 1
DEFAULT_BATCH_SIZE = 10_000

NULL_TOKEN = b"\\N"
_BACKSLASH = 0x5C
_NEWLINE = 0x0A
_CARRIAGE = 0x0D
_OCTAL_ZERO = 0x30
_OCTAL_SEVEN = 0x37
_HEX_LOWER = 0x78  # 'x'
_HEX_DIGITS = frozenset(b"0123456789abcdefABCDEF")

# COPY text 格式里的单字符转义
_SIMPLE_ESCAPES = {
    ord("b"): 0x08,   # backspace
    ord("f"): 0x0C,   # form feed
    ord("n"): 0x0A,   # newline
    ord("r"): 0x0D,   # carriage return
    ord("t"): 0x09,   # tab
    ord("v"): 0x0B,   # vertical tab
    _BACKSLASH: _BACKSLASH,
}

_COPY_HEADER_RE = re.compile(
    rb"^COPY +(?:public\.)?(?P<table>[A-Za-z_][A-Za-z0-9_]*) *"
    rb"\((?P<cols>[^)]*)\) *FROM +stdin;\s*$"
)

# 番号：PREFIX（至少含一个字母，允许内含数字，如 T28 / 100TV）+ 可选分隔符 + 数字尾巴
_DVD_ID_RE = re.compile(r"^(?P<prefix>[A-Za-z0-9]*[A-Za-z])[ _\-]*(?P<num>\d+)$")
_NON_ALNUM_RE = re.compile(r"[^0-9A-Za-z]+")
_SUFFIX_INDEX_RE = re.compile(r"^(?P<base>.*)-(\d+)$")


class CopyFormatError(ValueError):
    """COPY 数据块格式错误（字段数不符 / 列缺失 / 转义非法）。"""


# ---------------------------------------------------------------------------
# 1. COPY 文本格式解析
# ---------------------------------------------------------------------------
def unescape_copy_bytes(raw: bytes) -> bytes:
    r"""还原 PostgreSQL COPY text 格式的转义，返回原始字节。

    支持 ``\\`` ``\b`` ``\f`` ``\n`` ``\r`` ``\t`` ``\v``、八进制 ``\ooo``
    （1–3 位）与十六进制 ``\xhh``（1–2 位）；其它 ``\c`` 还原为 ``c``。
    ``\ooo`` 拼出的多字节 UTF-8 由调用方统一 decode。
    """
    if _BACKSLASH not in raw:
        return raw
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        j = raw.find(b"\\", i)
        if j < 0:
            out += raw[i:]
            break
        if j > i:
            out += raw[i:j]
        i = j + 1
        if i >= n:  # 末尾孤立的反斜杠（合法 dump 不会出现，容错）
            out.append(_BACKSLASH)
            break
        c = raw[i]
        simple = _SIMPLE_ESCAPES.get(c)
        if simple is not None:
            out.append(simple)
            i += 1
        elif _OCTAL_ZERO <= c <= _OCTAL_SEVEN:
            value = 0
            start = i
            while i < n and i - start < 3 and _OCTAL_ZERO <= raw[i] <= _OCTAL_SEVEN:
                value = value * 8 + (raw[i] - _OCTAL_ZERO)
                i += 1
            out.append(value & 0xFF)
        elif c == _HEX_LOWER:
            value = 0
            start = i + 1
            i = start
            while i < n and i - start < 2 and raw[i] in _HEX_DIGITS:
                value = value * 16 + int(chr(raw[i]), 16)
                i += 1
            if i == start:  # "\x" 后面不是十六进制 → 原样保留
                out.append(c)
                i = start
            else:
                out.append(value & 0xFF)
        else:
            out.append(c)
            i += 1
    return bytes(out)


def decode_copy_field(raw: bytes) -> str | None:
    """把 COPY 的一个原始字段还原成 ``str``（``\\N`` → ``None``，空字段 → ``''``）。"""
    if raw == NULL_TOKEN:
        return None
    if _BACKSLASH in raw:
        raw = unescape_copy_bytes(raw)
    return raw.decode("utf-8")


def parse_copy_header(line: bytes) -> tuple[str, tuple[str, ...]] | None:
    """解析 ``COPY public.tbl (col, ...) FROM stdin;``，不是 COPY 行则返回 None。"""
    match = _COPY_HEADER_RE.match(line)
    if match is None:
        return None
    table = match.group("table").decode("ascii")
    columns = []
    for part in match.group("cols").split(b","):
        name = part.strip()
        if len(name) >= 2 and name[:1] == b'"' and name[-1:] == b'"':
            name = name[1:-1]
        columns.append(name.decode("utf-8"))
    return table, tuple(columns)


@dataclass
class CopySection:
    """dump 里的一个 ``COPY ... FROM stdin;`` 数据块。

    ``rows`` 是生成器，产出**原始 bytes 元组**（NULL 仍是 ``b'\\N'``，等调用方
    用 :func:`decode_copy_field` 或列类型转换器处理）。生成器直接读同一个文件
    句柄，所以**必须把 rows 消费完再取下一个 section**（:func:`iter_copy_sections`
    的契约）。
    """

    table: str
    columns: tuple[str, ...]
    rows: Iterator[tuple[bytes, ...]]
    line_no: int

    def __iter__(self) -> Iterator[tuple[bytes, ...]]:
        return self.rows


def iter_copy_sections(fh) -> Iterator[CopySection]:
    """流式遍历 pg_dump 里的所有 COPY 数据块（不读入内存，不整文件 readlines）。"""
    line_no = 0
    while True:
        line = fh.readline()
        if not line:
            return
        line_no += 1
        header = parse_copy_header(line)
        if header is None:
            continue
        table, columns = header
        yield CopySection(
            table=table,
            columns=columns,
            rows=_iter_section_rows(fh, table, columns, line_no),
            line_no=line_no,
        )


def _iter_section_rows(
    fh, table: str, columns: tuple[str, ...], start_line_no: int
) -> Iterator[tuple[bytes, ...]]:
    """读一个 COPY 块的数据行，直到 ``\\.`` 终止行。"""
    ncols = len(columns)
    line_no = start_line_no
    for line in fh:
        line_no += 1
        if line[:2] == b"\\." and line.rstrip(b"\r\n") == b"\\.":
            return
        line = line.rstrip(b"\r\n")
        fields = line.split(b"\t")
        if len(fields) != ncols:
            raise CopyFormatError(
                f"{table} 第 {line_no} 行字段数 {len(fields)} != {ncols}"
                f"（行首: {line[:80]!r}）"
            )
        yield tuple(fields)


def open_dump(path: str | os.PathLike) -> object:
    """按魔数自动识别 gzip，返回二进制文件对象（调用方负责 close）。"""
    path = Path(path)
    with open(path, "rb") as probe:
        magic = probe.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rb")
    return open(path, "rb")


def iter_copy_records(
    source: str | os.PathLike | object,
    tables: Iterable[str] | None = None,
) -> Iterator[tuple[str, tuple[str, ...], tuple[str | None, ...]]]:
    """解码后的 COPY 遍历：``(table, columns, decoded_row)``，方便分析/测试。

    ``source`` 可以是路径（自动识别 .gz）或已打开的二进制文件对象。
    """
    wanted = set(tables) if tables is not None else None
    close = False
    if isinstance(source, (str, os.PathLike)):
        fh = open_dump(source)
        close = True
    else:
        fh = source
    try:
        for section in iter_copy_sections(fh):
            if wanted is not None and section.table not in wanted:
                for _ in section.rows:  # 跳过但必须消费，保持文件游标正确
                    pass
                continue
            for raw in section.rows:
                yield section.table, section.columns, tuple(
                    decode_copy_field(field) for field in raw
                )
    finally:
        if close:
            fh.close()


# ---------------------------------------------------------------------------
# 2. 番号 / 图片工具
# ---------------------------------------------------------------------------
def dvd_id_norm(value: str | None) -> str | None:
    """番号归一化查询键（小写 + 去分隔符 + 数字部分补零到 3 位）。

    用于「用户文件名里的番号」和「dump 里的 dvd_id」之间的匹配，**只做字符串级
    归一化**，不识别厂牌、不剥 ``-C/-CH/-UC/-4K/1080p`` 等噪声（那是番号解析器
    的职责，见 docs/PLAN.md 4.1；解析器的输出可以直接喂给本函数）。

    >>> dvd_id_norm("100TV-031")
    '100tv031'
    >>> dvd_id_norm("IHKD-06")
    'ihkd006'
    >>> dvd_id_norm("SSIS001")
    'ssis001'
    >>> dvd_id_norm("") is None
    True
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    match = _DVD_ID_RE.match(text)
    if match is not None:
        prefix = match.group("prefix").upper()
        number = match.group("num")
        if len(number) < 3:
            number = number.zfill(3)
        return _NON_ALNUM_RE.sub("", f"{prefix}-{number}").lower()
    key = _NON_ALNUM_RE.sub("", text).lower()
    return key or None


def dvd_id_key(value: str | None) -> str | None:
    """番号的「精确」查询键：只统一大小写 / 去分隔符，**不补零**。

    与 :func:`dvd_id_norm` 配合做两级匹配：先 key 精确匹配，未命中再退回补零
    norm。两者必须分开——dump 里 ``DOG-01`` 和 ``DOG-001`` 是两部不同的片，
    补零会把它俩并成一个键（实测 dump 有这种数据）。

    >>> dvd_id_key("100TV-031")
    '100tv031'
    >>> dvd_id_key("DOG-01")
    'dog01'
    >>> dvd_id_key("DOG-001")
    'dog001'
    """
    if not value:
        return None
    key = _NON_ALNUM_RE.sub("", value.strip()).lower()
    return key or None


def canonical_dvd_id(value: str | None) -> str | None:
    """番号的规范展示形态 ``PREFIX-NNN``（数字补零到 3 位）；识别不了则大写原串。

    >>> canonical_dvd_id("ihkd-6")
    'IHKD-006'
    >>> canonical_dvd_id("UD321R")
    'UD321R'
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    match = _DVD_ID_RE.match(text)
    if match is None:
        return text.upper()
    prefix = match.group("prefix").upper()
    number = match.group("num")
    if len(number) < 3:
        number = number.zfill(3)
    return f"{prefix}-{number}"


def content_id_norm(value: str | None) -> str | None:
    """content_id 的归一化查询键（小写、去 ``_-/`` 等分隔符）。"""
    if not value:
        return None
    key = _NON_ALNUM_RE.sub("", value.strip()).lower()
    return key or None


def gallery_image_count(first: str | None, last: str | None) -> int | None:
    """由 gallery first/last 相对路径推算剧照张数。

    dump 里 first/last 形如 ``digital/video/xxx/xxxjp-1`` / ``...jp-10``，
    序号即张数序号；同一 basename 时 ``count = last - first + 1``。
    ``-0``（DMM 对「没有剧照」的占位）与 basename 不一致时返回 None/0。

    >>> gallery_image_count("a/xjp-1", "a/xjp-10")
    10
    >>> gallery_image_count("a/xjp-1", "a/xjp-0")
    0
    >>> gallery_image_count(None, None) is None
    True
    """
    if not first or not last:
        return None
    m_first = _SUFFIX_INDEX_RE.match(first.strip())
    m_last = _SUFFIX_INDEX_RE.match(last.strip())
    if m_first is None or m_last is None:
        return None
    if m_first.group("base") != m_last.group("base"):
        return None
    start = int(m_first.group(2))
    end = int(m_last.group(2))
    if start < 1 or end < start:
        return 0 if end == 0 else None
    return end - start + 1


# ---------------------------------------------------------------------------
# 3. staging 表定义
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TableSpec:
    """一张 staging 表的 schema 描述（列类型决定 COPY 字段转换器）。"""

    name: str
    columns: tuple[tuple[str, str], ...]  # (列名, SQLite 类型)
    extra_columns: tuple[tuple[str, str], ...] = ()
    indexes: tuple[tuple[str, str], ...] = ()  # (索引名, 列)
    post: Callable[[list], tuple] | None = None  # 计算附加列的钩子

    @property
    def all_columns(self) -> tuple[tuple[str, str], ...]:
        return self.columns + self.extra_columns

    def create_sql(self) -> str:
        body = ", ".join(f"{name} {sqltype}" for name, sqltype in self.all_columns)
        return f"CREATE TABLE IF NOT EXISTS {self.name} ({body})"

    def create_index_sql(self) -> list[str]:
        return [
            f"CREATE INDEX IF NOT EXISTS {index_name} "
            f"ON {self.name} ({column})"
            for index_name, column in self.indexes
        ]

    def insert_sql(self) -> str:
        columns = ", ".join(name for name, _ in self.all_columns)
        marks = ", ".join("?" * len(self.all_columns))
        return f"INSERT INTO {self.name} ({columns}) VALUES ({marks})"


def _idx(table: str, column: str) -> tuple[str, str]:
    return (f"idx_{table}_{column}", column)


def _video_post(row: list) -> tuple:
    """derived_video 附加三列查询键：精确 key / 补零 norm / content_id norm。"""
    dvd_id = row[1]
    content_id = row[0]
    row.append(dvd_id_key(dvd_id))
    row.append(dvd_id_norm(dvd_id))
    row.append(content_id_norm(content_id))
    return tuple(row)


_TABLE_SPECS: dict[str, TableSpec] = {}


def _register(spec: TableSpec) -> TableSpec:
    _TABLE_SPECS[spec.name] = spec
    return spec


_register(TableSpec("derived_actor", (("id", "INTEGER"), ("name_kanji", "TEXT"), ("name_kana", "TEXT")),
                   indexes=(_idx("actor", "id"),)))
_register(TableSpec("derived_actress",
                    (("id", "INTEGER"), ("name_romaji", "TEXT"), ("image_url", "TEXT"),
                     ("name_kanji", "TEXT"), ("name_kana", "TEXT")),
                    indexes=(_idx("actress", "id"),)))
_register(TableSpec("derived_author", (("id", "INTEGER"), ("name_kanji", "TEXT"), ("name_kana", "TEXT")),
                    indexes=(_idx("author", "id"),)))
_register(TableSpec("derived_category", (("id", "INTEGER"), ("name_en", "TEXT"), ("name_ja", "TEXT")),
                    indexes=(_idx("category", "id"),)))
_register(TableSpec("derived_director",
                    (("id", "INTEGER"), ("name_kanji", "TEXT"), ("name_kana", "TEXT"),
                     ("name_romaji", "TEXT")),
                    indexes=(_idx("director", "id"),)))
_register(TableSpec("derived_label", (("id", "INTEGER"), ("name_en", "TEXT"), ("name_ja", "TEXT")),
                    indexes=(_idx("label", "id"),)))
_register(TableSpec("derived_maker", (("id", "INTEGER"), ("name_en", "TEXT"), ("name_ja", "TEXT")),
                    indexes=(_idx("maker", "id"),)))
_register(TableSpec("derived_series", (("id", "INTEGER"), ("name_en", "TEXT"), ("name_ja", "TEXT")),
                    indexes=(_idx("series", "id"),)))
_register(TableSpec("derived_site", (("id", "INTEGER"), ("name", "TEXT")),
                    indexes=(_idx("site", "id"),)))
_register(TableSpec(
    "derived_video",
    (
        ("content_id", "TEXT"),
        ("dvd_id", "TEXT"),
        ("title_en", "TEXT"),
        ("title_ja", "TEXT"),
        ("comment_en", "TEXT"),
        ("comment_ja", "TEXT"),
        ("runtime_mins", "INTEGER"),
        ("release_date", "TEXT"),
        ("sample_url", "TEXT"),
        ("maker_id", "INTEGER"),
        ("label_id", "INTEGER"),
        ("series_id", "INTEGER"),
        ("jacket_full_url", "TEXT"),
        ("jacket_thumb_url", "TEXT"),
        ("gallery_full_first", "TEXT"),
        ("gallery_full_last", "TEXT"),
        ("gallery_thumb_first", "TEXT"),
        ("gallery_thumb_last", "TEXT"),
        ("site_id", "INTEGER"),
        ("service_code", "TEXT"),
    ),
    extra_columns=(
        ("dvd_id_key", "TEXT"),
        ("dvd_id_norm", "TEXT"),
        ("content_id_norm", "TEXT"),
    ),
    indexes=(
        _idx("video", "dvd_id"),
        _idx("video", "dvd_id_key"),
        _idx("video", "dvd_id_norm"),
        _idx("video", "content_id"),
        _idx("video", "content_id_norm"),
    ),
    post=_video_post,
))
_register(TableSpec("derived_video_actor",
                    (("content_id", "TEXT"), ("actor_id", "INTEGER"), ("ordinality", "INTEGER"),
                     ("release_date", "TEXT")),
                    indexes=(_idx("video_actor", "content_id"), _idx("video_actor", "actor_id"))))
_register(TableSpec("derived_video_actress",
                    (("content_id", "TEXT"), ("actress_id", "INTEGER"), ("ordinality", "INTEGER"),
                     ("release_date", "TEXT")),
                    indexes=(_idx("video_actress", "content_id"), _idx("video_actress", "actress_id"))))
_register(TableSpec("derived_video_author",
                    (("content_id", "TEXT"), ("author_id", "INTEGER")),
                    indexes=(_idx("video_author", "content_id"), _idx("video_author", "author_id"))))
_register(TableSpec("derived_video_category",
                    (("content_id", "TEXT"), ("category_id", "INTEGER"), ("release_date", "TEXT")),
                    indexes=(_idx("video_category", "content_id"), _idx("video_category", "category_id"))))
_register(TableSpec("derived_video_director",
                    (("content_id", "TEXT"), ("director_id", "INTEGER")),
                    indexes=(_idx("video_director", "content_id"), _idx("video_director", "director_id"))))

#: 默认导入的表（其他表只在报告里计数，不进 staging）
CORE_TABLES: tuple[str, ...] = tuple(_TABLE_SPECS)

#: dump 里已知但不进 staging 的表（原始 source 表 / 只有英译的机翻表，体积大且对
#: 本地片库物化没用；行数仍会统计进 meta 和覆盖率报告）
SKIPPED_TABLES: tuple[str, ...] = (
    "machine_translation",
    "source_dmm_histrion",
    "source_dmm_trailer",
    "source_dmm_video_histrion",
)

#: 已知的全部 dump 表（报告里用来标注「已知但跳过」）
KNOWN_DUMP_TABLES: tuple[str, ...] = CORE_TABLES + SKIPPED_TABLES

_CONVERTER_BY_TYPE = {
    "TEXT": decode_copy_field,
    "INTEGER": lambda raw: None if raw in (NULL_TOKEN, b"") else int(raw),
}


def _build_row_mapper(spec: TableSpec, source_columns: Sequence[str]) -> Callable[[tuple], tuple]:
    """按 dump 实际列顺序生成「原始 bytes 行 → SQLite 参数元组」的转换函数。

    dump 列顺序若将来变化（r18 改 schema），这里会自动按列名重排；缺列则报错。
    """
    name_to_index = {name: index for index, name in enumerate(source_columns)}
    missing = [name for name, _ in spec.columns if name not in name_to_index]
    if missing:
        raise CopyFormatError(
            f"{spec.name}: dump 缺少列 {missing}；dump 实际列={list(source_columns)}"
        )
    order = [name_to_index[name] for name, _ in spec.columns]
    converters = tuple(_CONVERTER_BY_TYPE.get(sqltype, decode_copy_field)
                       for _, sqltype in spec.columns)
    post = spec.post
    identity = order == list(range(len(order))) and len(order) == len(source_columns)
    all_text = all(sqltype == "TEXT" for _, sqltype in spec.columns)

    if identity and post is None and all_text:
        def mapper(raw: tuple) -> tuple:
            return tuple(map(decode_copy_field, raw))
        return mapper

    if identity and post is None:
        def mapper(raw: tuple) -> tuple:
            return tuple(conv(value) for conv, value in zip(converters, raw))
        return mapper

    if identity and post is not None:
        def mapper(raw: tuple) -> tuple:
            return post([conv(value) for conv, value in zip(converters, raw)])
        return mapper

    if post is None:
        def mapper(raw: tuple) -> tuple:
            return tuple(conv(raw[index]) for conv, index in zip(converters, order))
        return mapper

    def mapper(raw: tuple) -> tuple:
        return post([conv(raw[index]) for conv, index in zip(converters, order)])
    return mapper


# ---------------------------------------------------------------------------
# 4. staging 导入
# ---------------------------------------------------------------------------
@dataclass
class TableStat:
    """单表导入统计。"""

    table: str
    rows: int = 0
    seconds: float = 0.0
    imported: bool = False

    def as_dict(self) -> dict:
        return {
            "table": self.table,
            "rows": self.rows,
            "seconds": round(self.seconds, 3),
            "imported": self.imported,
        }


@dataclass
class ImportResult:
    """一次 import_dump 的完整结果（CLI / 报告都从这里取数）。"""

    sql_path: Path
    out_path: Path
    tables: dict[str, TableStat]
    duration_s: float
    imported_rows: int
    total_rows: int
    sha256: str | None = None
    index_seconds: dict[str, float] = field(default_factory=dict)
    analyze_seconds: float = 0.0
    db_bytes: int = 0

    def as_dict(self) -> dict:
        return {
            "sql_path": str(self.sql_path),
            "out_path": str(self.out_path),
            "duration_s": round(self.duration_s, 3),
            "imported_rows": self.imported_rows,
            "total_rows": self.total_rows,
            "sha256": self.sha256,
            "db_bytes": self.db_bytes,
            "index_seconds": {k: round(v, 3) for k, v in self.index_seconds.items()},
            "analyze_seconds": round(self.analyze_seconds, 3),
            "tables": {name: stat.as_dict() for name, stat in self.tables.items()},
        }


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _create_meta_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )


def _write_meta(conn: sqlite3.Connection, items: dict[str, object]) -> None:
    rows = [(str(key), "" if value is None else str(value)) for key, value in items.items()]
    conn.execute("BEGIN")
    conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", rows)
    conn.commit()


def _default_progress(table: str, rows: int, elapsed: float) -> None:  # pragma: no cover
    pass


def import_dump(
    sql_path: str | os.PathLike,
    out_path: str | os.PathLike,
    *,
    tables: Sequence[str] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    progress: Callable[[str, int, float], None] | None = None,
    build_indexes: bool = True,
    analyze: bool = False,
    compute_sha256: bool = True,
) -> ImportResult:
    """把 r18.dev dump 导入独立 staging 库（幂等：重建并原子替换 ``out_path``）。

    参数
    ----
    sql_path: dump 路径（``.sql`` 或 ``.sql.gz``，按魔数识别）。
    out_path: 目标 SQLite 路径；已存在则整体重建（先写 ``<out>.tmp`` 再替换）。
    tables: 要导入的表名；默认 :data:`CORE_TABLES`（其余表只计数）。
    batch_size: 每个事务的行数（默认 1 万）。
    progress: 可选回调 ``(table, rows_so_far, elapsed)``，每批调用一次。
    build_indexes: 灌完数据后统一建索引（默认 True）。
    analyze: 是否跑 ``ANALYZE``（默认 False；大库会多花几十秒）。
    compute_sha256: 是否记录 dump 的 sha256（默认 True，1.3GB 约 1 秒）。
    """
    sql_path = Path(sql_path)
    out_path = Path(out_path)
    if not sql_path.is_file():
        raise FileNotFoundError(f"dump 文件不存在: {sql_path}")
    if sql_path.resolve() == out_path.resolve():
        raise ValueError("sql_path 与 out_path 不能是同一个文件")

    selected = tuple(tables) if tables is not None else CORE_TABLES
    unknown = [name for name in selected if name not in _TABLE_SPECS]
    if unknown:
        raise ValueError(
            f"不支持的表 {unknown}；可选: {list(_TABLE_SPECS)}（跳过表: {list(SKIPPED_TABLES)}）"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    for stale in (tmp_path, Path(str(tmp_path) + "-journal"), Path(str(tmp_path) + "-wal"),
                  Path(str(tmp_path) + "-shm")):
        if stale.exists():
            stale.unlink()

    dump_stat = sql_path.stat()
    started_at = _utc_iso()
    t_start = time.perf_counter()
    sha256 = _sha256_file(sql_path) if compute_sha256 else None

    conn = sqlite3.connect(str(tmp_path), isolation_level=None)
    result = ImportResult(
        sql_path=sql_path,
        out_path=out_path,
        tables={},
        duration_s=0.0,
        imported_rows=0,
        total_rows=0,
        sha256=sha256,
    )
    try:
        # 灌数据阶段：关 journal / synchronous，独占锁，尽量快
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -262144")  # 256 MB
        conn.execute("PRAGMA locking_mode = EXCLUSIVE")
        _create_meta_table(conn)

        created_tables: set[str] = set()
        dump = open_dump(sql_path)
        try:
            for section in iter_copy_sections(dump):
                spec = _TABLE_SPECS.get(section.table)
                if spec is None or section.table not in selected:
                    t_table = time.perf_counter()
                    rows = 0
                    for _ in section.rows:
                        rows += 1
                    result.tables[section.table] = TableStat(
                        table=section.table,
                        rows=rows,
                        seconds=time.perf_counter() - t_table,
                        imported=False,
                    )
                    continue

                t_table = time.perf_counter()
                if section.table not in created_tables:
                    conn.execute(spec.create_sql())
                    created_tables.add(section.table)
                mapper = _build_row_mapper(spec, section.columns)
                insert_sql = spec.insert_sql()
                batch: list[tuple] = []
                rows = 0
                for raw in section.rows:
                    batch.append(mapper(raw))
                    if len(batch) >= batch_size:
                        conn.execute("BEGIN")
                        conn.executemany(insert_sql, batch)
                        conn.commit()
                        rows += len(batch)
                        batch.clear()
                        if progress is not None:
                            progress(section.table, rows, time.perf_counter() - t_table)
                if batch:
                    conn.execute("BEGIN")
                    conn.executemany(insert_sql, batch)
                    conn.commit()
                    rows += len(batch)
                    batch.clear()
                result.tables[section.table] = TableStat(
                    table=section.table,
                    rows=rows,
                    seconds=time.perf_counter() - t_table,
                    imported=True,
                )
                result.imported_rows += rows
        finally:
            dump.close()

        result.total_rows = sum(stat.rows for stat in result.tables.values())

        # 索引阶段（数据灌完再建，比边灌边维护快一个数量级）
        if build_indexes:
            for name in created_tables:
                spec = _TABLE_SPECS[name]
                if not spec.indexes:
                    continue
                t_index = time.perf_counter()
                for statement in spec.create_index_sql():
                    conn.execute(statement)
                conn.commit()
                result.index_seconds[name] = time.perf_counter() - t_index

        if analyze:
            t_analyze = time.perf_counter()
            conn.execute("ANALYZE")
            conn.commit()
            result.analyze_seconds = time.perf_counter() - t_analyze

        duration_s = time.perf_counter() - t_start
        result.duration_s = duration_s
        result.db_bytes = tmp_path.stat().st_size

        # 收尾：关掉 OFF journal，写 meta / user_version
        try:
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.DatabaseError:  # pragma: no cover - 极端情况下忽略
            pass
        meta: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "importer_version": IMPORTER_VERSION,
            "user_version": SCHEMA_VERSION,
            "dump_file": sql_path.name,
            "dump_path": str(sql_path),
            "dump_size_bytes": dump_stat.st_size,
            "dump_mtime": datetime.fromtimestamp(dump_stat.st_mtime, timezone.utc)
                .replace(microsecond=0).isoformat(),
            "dump_date": datetime.fromtimestamp(dump_stat.st_mtime, timezone.utc)
                .strftime("%Y-%m-%d"),
            "dump_sha256": sha256 or "",
            "imported_at": started_at,
            "duration_s": round(duration_s, 3),
            "imported_rows": result.imported_rows,
            "total_rows": result.total_rows,
            "db_bytes": result.db_bytes,
            "batch_size": batch_size,
            "indexes_built": 1 if build_indexes else 0,
            "index_duration_s": round(sum(result.index_seconds.values()), 3),
            "analyze_duration_s": round(result.analyze_seconds, 3),
        }
        for name, stat in result.tables.items():
            meta[f"rows:{name}"] = stat.rows
            meta[f"seconds:{name}"] = round(stat.seconds, 3)
            meta[f"imported:{name}"] = 1 if stat.imported else 0
        _write_meta(conn, meta)
        conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
        conn.close()
        conn = None  # type: ignore[assignment]
        os.replace(tmp_path, out_path)
    except BaseException:
        if conn is not None:
            conn.close()
        for stale in (tmp_path, Path(str(tmp_path) + "-journal")):
            try:
                if stale.exists():
                    stale.unlink()
            except OSError:  # pragma: no cover
                pass
        raise
    return result


# ---------------------------------------------------------------------------
# 5. 只读使用辅助
# ---------------------------------------------------------------------------
def open_dump_db(path: str | os.PathLike) -> sqlite3.Connection:
    """只读打开 staging 库（URI ``mode=ro`` + ``query_only``）。"""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"staging 库不存在: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def lookup_by_dvd_ids(
    conn: sqlite3.Connection,
    codes: Iterable[str],
    *,
    chunk_size: int = 400,
) -> dict[str, list[sqlite3.Row]]:
    """按「用户番号」批量查 staging 里的 ``derived_video`` 行。

    三级匹配（已命中的番号不再进入下一级）：

    1. ``dvd_id_key`` 精确匹配：大小写 / 分隔符不敏感但不补零，等价性最可靠；
    2. ``dvd_id_norm`` 补零匹配：覆盖 dump ``IHKD-06`` vs 用户 ``IHKD-006`` 这类
       写法差异；补零有歧义（见 :func:`dvd_id_key` 的说明），所以只作为第二级；
    3. ``content_id_norm`` 兜底：dvd_id 为空的素人/无码条目只能靠 content_id 命中。

    返回 ``{输入番号: [row, ...]}``；未命中的输入不在结果里。
    """
    wanted = [code for code in codes if code]
    if not wanted:
        return {}
    found: dict[str, list[sqlite3.Row]] = {}
    stages = (
        ("dvd_id_key", dvd_id_key),
        ("dvd_id_norm", dvd_id_norm),
        ("content_id_norm", content_id_norm),
    )
    remaining = wanted
    for column, key_func in stages:
        if not remaining:
            break
        key_to_codes: dict[str, list[str]] = {}
        for code in remaining:
            key = key_func(code)
            if key:
                key_to_codes.setdefault(key, []).append(code)
        keys = list(key_to_codes)
        for start in range(0, len(keys), chunk_size):
            chunk = keys[start:start + chunk_size]
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT * FROM derived_video WHERE {column} IN ({marks})", chunk
            ):
                for code in key_to_codes.get(row[column], ()):
                    found.setdefault(code, []).append(row)
        remaining = [code for code in remaining if code not in found]
    return found


# ---------------------------------------------------------------------------
# 6. 覆盖率统计（研究报告用，全部是 SQL 实测值，不做估算）
# ---------------------------------------------------------------------------
def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _count(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def _nonempty(conn: sqlite3.Connection, table: str, column: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return _count(
        conn,
        f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL AND TRIM({column}) <> ''",
    )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _meta_dict(conn: sqlite3.Connection) -> dict[str, str]:
    if not _table_exists(conn, "meta"):
        return {}
    return {row[0]: row[1] for row in conn.execute("SELECT key, value FROM meta")}


def _dvd_id_prefixes(conn: sqlite3.Connection, limit: int = 30) -> tuple[list[dict], int]:
    """dvd_id 前缀分布 Top N（前缀 = '-' 前的部分；无 '-' 时取前导字母）。"""
    counts: dict[str, int] = {}
    no_dash = 0
    unknown = 0
    total = 0
    for (dvd_id,) in conn.execute(
        "SELECT dvd_id FROM derived_video WHERE dvd_id IS NOT NULL AND dvd_id <> ''"
    ):
        total += 1
        value = dvd_id.strip()
        if not value:
            unknown += 1
            continue
        if "-" in value:
            prefix = value.split("-", 1)[0].upper()
        else:
            no_dash += 1
            match = re.match(r"[A-Za-z]+", value)
            prefix = match.group(0).upper() if match else "(other)"
        counts[prefix] = counts.get(prefix, 0) + 1
    top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return (
        [{"prefix": prefix, "count": count} for prefix, count in top],
        no_dash,
    )


_GALLERY_BUCKETS = ((0, 0), (1, 5), (6, 10), (11, 15), (16, 20), (21, 10**9))


def coverage_stats(conn: sqlite3.Connection) -> dict:
    """对 staging 库算一份覆盖率统计（全部为实测值，供 research/dump-coverage.md）。"""
    meta = _meta_dict(conn)
    stats: dict = {
        "generated_at": _utc_iso(),
        "meta": meta,
        "tables": {},
        "video": {},
        "actress": {},
        "category": {},
        "maker": {},
        "label": {},
        "series": {},
        "director": {},
        "gallery": {},
        "dvd_id_prefixes": {},
        "chinese": {},
    }

    for table in KNOWN_DUMP_TABLES:
        if not _table_exists(conn, table):
            continue
        stats["tables"][table] = {
            "rows": _count(conn, f"SELECT COUNT(*) FROM {table}"),
            "seconds": float(meta.get(f"seconds:{table}", 0) or 0),
            "imported": meta.get(f"imported:{table}") == "1",
        }

    if _table_exists(conn, "derived_video"):
        video = stats["video"]
        total = _count(conn, "SELECT COUNT(*) FROM derived_video")
        video["rows"] = total
        video["dvd_id_null"] = _count(conn, "SELECT COUNT(*) FROM derived_video WHERE dvd_id IS NULL")
        video["dvd_id_empty"] = _count(
            conn, "SELECT COUNT(*) FROM derived_video WHERE dvd_id = ''"
        )
        video["dvd_id_usable"] = total - video["dvd_id_null"] - video["dvd_id_empty"]
        video["dvd_id_usable_ratio"] = _ratio(video["dvd_id_usable"], total)
        video["dvd_id_key_distinct"] = _count(
            conn, "SELECT COUNT(DISTINCT dvd_id_key) FROM derived_video WHERE dvd_id_key IS NOT NULL"
        )
        video["dvd_id_key_duplicated"] = _count(
            conn,
            "SELECT COUNT(*) FROM (SELECT dvd_id_key FROM derived_video "
            "WHERE dvd_id_key IS NOT NULL GROUP BY dvd_id_key HAVING COUNT(*) > 1)",
        )
        # 补零把「不同原始番号」并成一个 key 的组数（如 DOG-01 vs DOG-001），
        # 这是两级匹配（精确 key 优先）存在的原因
        video["dvd_id_padding_collisions"] = _count(
            conn,
            "SELECT COUNT(*) FROM (SELECT dvd_id_norm FROM derived_video "
            "WHERE dvd_id_norm IS NOT NULL GROUP BY dvd_id_norm "
            "HAVING COUNT(DISTINCT dvd_id_key) > 1)",
        )
        video["dvd_id_rows_padding_collisions"] = _count(
            conn,
            "SELECT COUNT(*) FROM derived_video WHERE dvd_id_norm IN ("
            "SELECT dvd_id_norm FROM derived_video WHERE dvd_id_norm IS NOT NULL "
            "GROUP BY dvd_id_norm HAVING COUNT(DISTINCT dvd_id_key) > 1)",
        )
        video["dvd_id_norm_distinct"] = _count(
            conn, "SELECT COUNT(DISTINCT dvd_id_norm) FROM derived_video WHERE dvd_id_norm IS NOT NULL"
        )
        video["dvd_id_norm_duplicated"] = _count(
            conn,
            "SELECT COUNT(*) FROM (SELECT dvd_id_norm FROM derived_video "
            "WHERE dvd_id_norm IS NOT NULL GROUP BY dvd_id_norm HAVING COUNT(*) > 1)",
        )
        video["content_id_null"] = _count(
            conn, "SELECT COUNT(*) FROM derived_video WHERE content_id IS NULL"
        )
        for column in (
            "title_en", "title_ja", "comment_en", "comment_ja",
            "sample_url", "jacket_full_url", "jacket_thumb_url",
            "gallery_full_first", "gallery_thumb_first",
        ):
            rows = _nonempty(conn, "derived_video", column)
            video[f"{column}_nonempty"] = rows
            video[f"{column}_nonempty_ratio"] = _ratio(rows, total)
        video["title_ja_and_en_nonempty"] = _count(
            conn,
            "SELECT COUNT(*) FROM derived_video WHERE "
            "title_ja IS NOT NULL AND TRIM(title_ja) <> '' AND "
            "title_en IS NOT NULL AND TRIM(title_en) <> ''",
        )
        video["title_ja_or_en_nonempty"] = _count(
            conn,
            "SELECT COUNT(*) FROM derived_video WHERE "
            "title_ja IS NOT NULL AND TRIM(title_ja) <> '' OR "
            "title_en IS NOT NULL AND TRIM(title_en) <> ''",
        )
        video["runtime_mins_notnull"] = _count(
            conn, "SELECT COUNT(*) FROM derived_video WHERE runtime_mins IS NOT NULL"
        )
        video["release_date_notnull"] = _count(
            conn, "SELECT COUNT(*) FROM derived_video WHERE release_date IS NOT NULL AND release_date <> ''"
        )
        video["release_date_min"], video["release_date_max"] = (
            conn.execute(
                "SELECT MIN(release_date), MAX(release_date) FROM derived_video "
                "WHERE release_date IS NOT NULL AND release_date <> ''"
            ).fetchone()
            or (None, None)
        )
        video["site_distribution"] = [
            {"site_id": row[0], "count": row[1]}
            for row in conn.execute(
                "SELECT site_id, COUNT(*) FROM derived_video GROUP BY site_id ORDER BY COUNT(*) DESC"
            )
        ]
        video["service_code_distribution"] = [
            {"service_code": row[0], "count": row[1]}
            for row in conn.execute(
                "SELECT service_code, COUNT(*) FROM derived_video "
                "GROUP BY service_code ORDER BY COUNT(*) DESC"
            )
        ]

        # gallery 形态：first/last 都是相对路径，按序号差统计张数分布
        gallery = stats["gallery"]
        gallery["full_first_nonnull"] = _nonempty(conn, "derived_video", "gallery_full_first")
        gallery["full_last_nonnull"] = _nonempty(conn, "derived_video", "gallery_full_last")
        gallery["thumb_first_nonnull"] = _nonempty(conn, "derived_video", "gallery_thumb_first")
        gallery["full_first_null"] = total - (
            _count(conn, "SELECT COUNT(*) FROM derived_video WHERE gallery_full_first IS NOT NULL")
        )
        bucket_counts = {str(low if low == high else f"{low}+" if high >= 10**9 else f"{low}-{high}"): 0
                         for low, high in _GALLERY_BUCKETS}
        counts_dist: dict[int, int] = {}
        bad_pairs = 0
        samples = []
        for first, last, thumb_first, thumb_last in conn.execute(
            "SELECT gallery_full_first, gallery_full_last, gallery_thumb_first, gallery_thumb_last "
            "FROM derived_video"
        ):
            count = gallery_image_count(first, last)
            if count is None:
                if first is not None or last is not None:
                    bad_pairs += 1
                continue
            counts_dist[count] = counts_dist.get(count, 0) + 1
            if len(samples) < 5 and first:
                samples.append({"first": first, "last": last,
                                "thumb_first": thumb_first, "thumb_last": thumb_last,
                                "count": count})
            for low, high in _GALLERY_BUCKETS:
                if low <= count <= high:
                    key = str(low if low == high else f"{low}+" if high >= 10**9 else f"{low}-{high}")
                    bucket_counts[key] += 1
                    break
        gallery["count_distribution"] = bucket_counts
        gallery["count_histogram"] = {str(k): v for k, v in sorted(counts_dist.items())}
        gallery["unparsable_pairs"] = bad_pairs
        gallery["samples"] = samples

        stats["dvd_id_prefixes"], stats["dvd_id_no_dash"] = _dvd_id_prefixes(conn)

        # 中文检测：英文/日文列里出现 CJK 的比例（r18 dump 已知只有日文原文 + 英文
        # 机翻，这里用实测样本来证明「没有中文」）。
        cjk = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
        chinese = stats["chinese"]
        cjk_in_en = 0
        scanned = 0
        for title_en, comment_en in conn.execute(
            "SELECT title_en, comment_en FROM derived_video LIMIT 200000"
        ):
            scanned += 1
            if title_en and cjk.search(title_en):
                cjk_in_en += 1
            elif comment_en and cjk.search(comment_en):
                cjk_in_en += 1
        chinese["english_fields_scanned"] = scanned
        chinese["cjk_in_english_fields"] = cjk_in_en

    if _table_exists(conn, "derived_actress"):
        total = _count(conn, "SELECT COUNT(*) FROM derived_actress")
        actress = stats["actress"]
        actress["rows"] = total
        for column in ("name_kanji", "name_kana", "name_romaji", "image_url"):
            rows = _nonempty(conn, "derived_actress", column)
            actress[f"{column}_nonempty"] = rows
            actress[f"{column}_nonempty_ratio"] = _ratio(rows, total)
        actress["all_names_empty"] = _count(
            conn,
            "SELECT COUNT(*) FROM derived_actress WHERE "
            "(name_kanji IS NULL OR TRIM(name_kanji) = '') AND "
            "(name_kana IS NULL OR TRIM(name_kana) = '') AND "
            "(name_romaji IS NULL OR TRIM(name_romaji) = '')",
        )
        actress["kanji_and_kana_nonempty"] = _count(
            conn,
            "SELECT COUNT(*) FROM derived_actress WHERE "
            "name_kanji IS NOT NULL AND TRIM(name_kanji) <> '' AND "
            "name_kana IS NOT NULL AND TRIM(name_kana) <> ''",
        )
        actress["kanji_only"] = _count(
            conn,
            "SELECT COUNT(*) FROM derived_actress WHERE "
            "name_kanji IS NOT NULL AND TRIM(name_kanji) <> '' AND "
            "(name_kana IS NULL OR TRIM(name_kana) = '')",
        )

    for table, key in (("derived_category", "category"), ("derived_maker", "maker"),
                       ("derived_label", "label"), ("derived_series", "series")):
        if _table_exists(conn, table):
            total = _count(conn, f"SELECT COUNT(*) FROM {table}")
            block = stats[key]
            block["rows"] = total
            for column in ("name_ja", "name_en"):
                rows = _nonempty(conn, table, column)
                block[f"{column}_nonempty"] = rows
                block[f"{column}_nonempty_ratio"] = _ratio(rows, total)

    if _table_exists(conn, "derived_director"):
        total = _count(conn, "SELECT COUNT(*) FROM derived_director")
        block = stats["director"]
        block["rows"] = total
        for column in ("name_kanji", "name_kana", "name_romaji"):
            rows = _nonempty(conn, "derived_director", column)
            block[f"{column}_nonempty"] = rows
            block[f"{column}_nonempty_ratio"] = _ratio(rows, total)

    return stats
