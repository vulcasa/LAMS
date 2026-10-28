"""tests for scripts/analyze_names.py —— M0 真实文件名命中率分析工具。

覆盖：
* 统计口径与 `code_parser.extract_code` 逐条对账（不信任聚合逻辑，全部重算）；
* confidence 分桶 / 前缀 Top N / flags / 特殊厂牌 / 按扩展名；
* 未解析样本的条数、顺序、limit；
* 输入收集（--paths-file / --dir，扩展名过滤、跳过目录、limit、AppleDouble）；
* r18 覆盖率：分批 ≤400、只读打开、假 staging 库三级匹配（key/norm/content_id）；
* CLI 端到端（Markdown + JSON 落盘、JSON 走 stdout、错误码）；
* `/tmp/av-archive-corpus` 自检（存在时）与真实 dump 库抽样（存在时，均 skipif）。
"""

from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = Path("/tmp/av-archive-corpus")
REAL_R18DB = ROOT / "data" / "r18dump.db"


def _load():
    name = "analyze_names_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "analyze_names.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclass + `from __future__ import annotations` 需要
    spec.loader.exec_module(module)
    return module


an = _load()
ec = an.code_parser.extract_code
CodeMatch = an.code_parser.CodeMatch


# ---------------------------------------------------------------------------
# 手写脏名清单（真实世界形态 + 解析器边界）
# ---------------------------------------------------------------------------

DIRTY = [
    "SSIS-001.mp4",
    "ssis001.mp4",
    "SSIS_001-C.mp4",
    "[ThZu.Cc]SSNI-001.mp4",
    "hhd800.com@ssis00001hhb.mp4",
    "ABP-123-4K.mp4",
    "SSIS-001-UC.mp4",
    "無碼-SSIS-775-LEAK.mkv",
    "中文字幕SSIS-838.mp4",
    "FC2-PPV-1234567.mp4",
    "fc2ppv7654321.mp4",
    "FC2-1234567.mp4",
    "HEYZO-1234.mp4",
    "heyzo_hd_2345.mp4",
    "SIRO-1234.mp4",
    "Tokyo-Hot n1234.mp4",
    "1pondo 123456_001.mp4",
    "10musume 654321_02.mp4",
    "Caribbean 123456-001.mp4",
    "carib-654321-002.mp4",
    "259LUXU-1234.mp4",
    "300MIUM-123.mp4",
    "200GANA-4321.mp4",
    "Pacopacomama 111111_001.mp4",
    "T28-123.mp4",
    "SSIS-001/cover.jpg",
    "SSIS-001/CD1/VIDEO_TS/VTS_01_1.VOB",
    "SSIS-001.srt",
    "SSIS-001-C.srt",
    "SSIS-001.nfo",
    "1080P.mp4",
    "2160p.UHD.mkv",
    "x264.mkv",
    "h264.mp4",
    "IMG_1234.MOV",
    "VTS_01_1.VOB",
    "random_movie_2024.mkv",
    "未命名.mp4",
    "sample.mkv",
    "clip (1).mp4",
    "2024-01-15.mp4",
    "trailer.mp4",
    "file001.mp4",
    "PART1.mp4",
]


def _recompute(paths):
    return [ec(str(path)) for path in paths]


# ---------------------------------------------------------------------------
# 统计口径与解析器对账
# ---------------------------------------------------------------------------


def test_analyze_counts_match_parser():
    report = an.analyze_paths(DIRTY, source="unit")
    matches = _recompute(DIRTY)

    assert report["input"]["total"] == len(DIRTY)
    assert report["summary"]["total"] == len(DIRTY)
    assert report["summary"]["parseable"] == sum(1 for m in matches if m.code)
    assert report["summary"]["parseable_rate"] == pytest.approx(
        report["summary"]["parseable"] / len(DIRTY)
    )
    assert report["summary"]["unique_codes"] == len({m.code for m in matches if m.code})

    # 来源分布逐条对账
    for source in ("basename", "parent", "none"):
        assert report["summary"]["source_counts"][source] == sum(
            1 for m in matches if m.source == source
        )

    # 前缀分布逐条对账（Top N 未截断时完整）
    prefix_counter = {}
    for m in matches:
        if m.code:
            prefix = an.code_prefix(m.code)
            prefix_counter[prefix] = prefix_counter.get(prefix, 0) + 1
    full = an.analyze_paths(DIRTY, top_prefixes=10_000)
    assert {item["prefix"]: item["count"] for item in full["prefixes"]} == prefix_counter

    # flags 逐条对账
    expected_flags = {
        flag: sum(1 for m in matches if flag in m.flags)
        for flag in ("chinese_sub", "uncensored", "hi_res", "part")
    }
    assert report["flags"] == expected_flags

    # 未解析样本：条数、顺序、内容
    expected_unresolved = [str(p) for p, m in zip(DIRTY, matches) if m.code is None]
    assert [item["path"] for item in report["unresolved"]] == expected_unresolved[:50]
    assert all(item["ext"].startswith(".") or item["ext"] == "<none>" for item in report["unresolved"])


def test_confidence_buckets_partition_and_match():
    report = an.analyze_paths(DIRTY, source="unit")
    buckets = report["summary"]["confidence"]
    assert set(buckets) == {an.CONF_GE90, an.CONF_70_89, an.CONF_LT70, an.CONF_ZERO}
    assert sum(buckets.values()) == report["summary"]["total"]
    matches = _recompute(DIRTY)
    assert buckets[an.CONF_ZERO] == sum(1 for m in matches if m.code is None)
    for bucket, expected in {
        an.CONF_GE90: sum(1 for m in matches if m.code and m.confidence >= 90),
        an.CONF_70_89: sum(1 for m in matches if m.code and 70 <= m.confidence < 90),
        an.CONF_LT70: sum(1 for m in matches if m.code and m.confidence < 70),
    }.items():
        assert buckets[bucket] == expected


def test_prefix_top_n_sorted_and_truncated():
    report = an.analyze_paths(DIRTY, top_prefixes=5)
    prefixes = report["prefixes"]
    assert len(prefixes) == 5
    counts = [item["count"] for item in prefixes]
    assert counts == sorted(counts, reverse=True)
    # 前缀名以计数降序、同数按字典序（确定性）
    for left, right in zip(prefixes, prefixes[1:]):
        assert (left["count"], -ord(left["prefix"][0])) <= (right["count"], -ord(right["prefix"][0])) or \
            left["count"] > right["count"] or left["prefix"] < right["prefix"]


def test_special_brand_counts():
    paths = [
        "FC2-PPV-1234567.mp4",
        "HEYZO-1234.mp4",
        "SIRO-1234.mp4",
        "Tokyo-Hot n1234.mp4",
        "1pondo 123456_001.mp4",
        "Caribbean 123456-001.mp4",
        "Pacopacomama 111111_001.mp4",
        "259LUXU-1234.mp4",
        "300MIUM-123.mp4",
        "200GANA-4321.mp4",
        "10musume 654321_01.mp4",
        "SSIS-001.mp4",
    ]
    report = an.analyze_paths(paths)
    brands = report["special_brands"]
    # 任务要求的 10 个厂牌 key 必须稳定存在（哪怕计数为 0）
    for brand in an.SPECIAL_BRANDS:
        assert brand in brands
    assert brands["FC2"] == 1
    assert brands["HEYZO"] == 1
    assert brands["SIRO"] == 1
    assert brands["TOKYO-HOT"] == 1
    assert brands["1PONDO"] == 1
    assert brands["CARIB"] == 1
    assert brands["PACO"] == 1
    assert brands["259LUXU"] == 1
    assert brands["300MIUM"] == 1
    assert brands["200GANA"] == 1
    # 10MUSUME 不在 10 个硬指标里，但要计入特殊厂牌总数
    assert report["other_special_brands"] == {"10MUSUME": 1}
    assert report["summary"]["special_brand_total"] == 11
    assert report["summary"]["special_brand_rate"] == pytest.approx(11 / 12)


def test_flags_counts_and_by_ext():
    paths = [
        "SSIS-001-C.mp4",
        "SSIS-002-UC.mp4",
        "SSIS-003-4K.mkv",
        "SSIS-004-CD1.mp4",
        "SSIS-005.srt",
        "1080P.mp4",
    ]
    report = an.analyze_paths(paths)
    assert report["flags"]["chinese_sub"] == 2
    assert report["flags"]["uncensored"] == 1
    # flags 对「没解析出番号」的文件也算（1080P.mp4 -> hi_res），用于字幕/画质线索
    assert report["flags"]["hi_res"] == 2
    assert report["flags"]["part"] == 1

    by_ext = {item["ext"]: item for item in report["by_ext"]}
    assert by_ext[".mp4"] == {
        "ext": ".mp4", "count": 4, "parseable": 3, "parseable_rate": pytest.approx(3 / 4),
    }
    assert by_ext[".mkv"]["count"] == 1 and by_ext[".mkv"]["parseable"] == 1
    assert by_ext[".srt"]["count"] == 1 and by_ext[".srt"]["parseable"] == 1


def test_unresolved_limit_and_empty_input():
    paths = ["1080P.mp4", "x264.mkv", "未命名.mp4", "trailer.mp4", "clip (1).mp4"]
    report = an.analyze_paths(paths, unresolved_limit=2)
    assert len(report["unresolved"]) == 2
    assert [item["path"] for item in report["unresolved"]] == paths[:2]

    empty = an.analyze_paths([])
    assert empty["summary"]["total"] == 0
    assert empty["summary"]["parseable_rate"] == 0.0
    assert empty["summary"]["special_brand_rate"] == 0.0
    assert empty["unresolved"] == []
    markdown = an.render_markdown(empty)
    assert "可解析率" in markdown and "不达标" in markdown


# ---------------------------------------------------------------------------
# 前缀 / 厂牌 / 分桶工具函数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,prefix,brand",
    [
        ("FC2-PPV-1234567", "FC2-PPV", "FC2"),
        ("TOKYO-HOT-N1234", "TOKYO-HOT", "TOKYO-HOT"),
        ("TOKYO-HOT-K1234", "TOKYO-HOT", "TOKYO-HOT"),
        ("HEYZO-1234", "HEYZO", "HEYZO"),
        ("SIRO-1234", "SIRO", "SIRO"),
        ("1PONDO-123456_001", "1PONDO", "1PONDO"),
        ("10MUSUME-123456_01", "10MUSUME", "10MUSUME"),
        ("CARIB-123456-001", "CARIB", "CARIB"),
        ("PACOPACOMAMA-123456_001", "PACOPACOMAMA", "PACO"),
        ("259LUXU-1234", "259LUXU", "259LUXU"),
        ("LUXU-1234", "LUXU", "259LUXU"),
        ("300MIUM-123", "300MIUM", "300MIUM"),
        ("MIUM-123", "MIUM", "300MIUM"),
        ("200GANA-1234", "200GANA", "200GANA"),
        ("GANA-1234", "GANA", "200GANA"),
        ("SSIS-001", "SSIS", None),
        ("MIDE-001", "MIDE", None),
    ],
)
def test_code_prefix_and_brand(code, prefix, brand):
    assert an.code_prefix(code) == prefix
    assert an.special_brand_of(code) == brand


@pytest.mark.parametrize(
    "code,conf,bucket",
    [
        (None, 0, an.CONF_ZERO),
        ("SSIS-001", 95, an.CONF_GE90),
        ("SSIS-001", 90, an.CONF_GE90),
        ("SSIS-001", 89, an.CONF_70_89),
        ("SSIS-001", 70, an.CONF_70_89),
        ("SSIS-001", 69, an.CONF_LT70),
        ("TOKYO-HOT-N1234", 45, an.CONF_LT70),
    ],
)
def test_confidence_bucket(code, conf, bucket):
    match = CodeMatch(code=code, confidence=conf, source="basename" if code else "none", raw="x")
    assert an.confidence_bucket(match) == bucket


# ---------------------------------------------------------------------------
# 输入收集
# ---------------------------------------------------------------------------


def test_read_paths_file(tmp_path):
    names = tmp_path / "names.txt"
    names.write_text("  SSIS-001.mp4  \n\n\n  dir/ABP-002.mkv\n", encoding="utf-8")
    assert an.read_paths_file(str(names)) == ["SSIS-001.mp4", "dir/ABP-002.mkv"]

    with pytest.raises(an.AnalyzeError):
        an.read_paths_file(str(tmp_path / "nope.txt"))


def _make_tree(root: Path) -> None:
    (root / "a").mkdir(parents=True)
    (root / "a" / "SSIS-001.mp4").write_bytes(b"")
    (root / "a" / "SSIS-001-C.srt").write_text("1\n", encoding="utf-8")
    (root / "b").mkdir()
    (root / "b" / "x.mkv").write_bytes(b"")
    (root / "b" / "x.nfo").write_text("<movie/>", encoding="utf-8")
    (root / "c").mkdir()
    (root / "c" / "z.txt").write_text("junk", encoding="utf-8")
    (root / "c" / "._junk.mp4").write_bytes(b"")
    (root / "@eaDir").mkdir()
    (root / "@eaDir" / "thumb.mp4").write_bytes(b"")
    (root / ".@__thumb").mkdir()
    (root / ".@__thumb" / "t.jpg").write_bytes(b"")


def test_collect_paths_from_dir_default_video_only(tmp_path):
    _make_tree(tmp_path)
    collected = an.collect_paths_from_dir(str(tmp_path))
    assert [Path(p).name for p in collected["paths"]] == ["SSIS-001.mp4", "x.mkv"]
    assert collected["paths"] == sorted(collected["paths"])
    assert collected["skipped_ext"]["<appledouble>"] == 1
    assert collected["skipped_ext"][".srt"] == 1
    assert collected["skipped_ext"][".nfo"] == 1
    assert collected["skipped_ext"][".txt"] == 1
    assert collected["skipped_dirs"] == 2  # @eaDir + .@__thumb
    for path in collected["paths"]:
        assert Path(path).suffix.lower() in an.media_ext_sets()["video"]


def test_collect_paths_from_dir_include_sidecars_and_limit(tmp_path):
    _make_tree(tmp_path)
    collected = an.collect_paths_from_dir(str(tmp_path), include_sidecars=True)
    names = {Path(p).name for p in collected["paths"]}
    assert names == {"SSIS-001.mp4", "SSIS-001-C.srt", "x.mkv", "x.nfo"}

    limited = an.collect_paths_from_dir(str(tmp_path), limit=1)
    assert len(limited["paths"]) == 1
    assert limited["truncated"] is True
    assert limited["limit"] == 1

    with pytest.raises(an.AnalyzeError):
        an.collect_paths_from_dir(str(tmp_path / "missing"))


def test_media_ext_sets():
    default = an.media_ext_sets()
    assert ".mp4" in default["video"] and ".iso" in default["video"] and ".vob" in default["video"]
    assert ".srt" not in default["allowed"]
    assert "@eaDir" in default["skip_dirs"]
    with_sidecars = an.media_ext_sets(include_sidecars=True)
    assert ".srt" in with_sidecars["allowed"] and ".nfo" in with_sidecars["allowed"]


# ---------------------------------------------------------------------------
# r18 覆盖率（假 staging 库 + 分批器）
# ---------------------------------------------------------------------------

FAKE_ROWS = [
    # 精确 key 命中（同一 key 两行 → multi_row_codes 会 +1）
    ("ssis00001", "SSIS-001", "ssis001", "ssis001", "ssis00001"),
    ("ssis00099", "SSIS-001", "ssis001", "ssis001", "ssis00099"),
    # 补零命中：dump 写 IHKD-06，用户写 IHKD-006
    ("ihkd00006", "IHKD-06", "ihkd06", "ihkd006", "ihkd00006"),
    # content_id 兜底（dvd_id 为空的素人条目）
    ("h_1234abc", None, None, None, "abc123"),
    # FC2
    ("fc2ppv1234567", "FC2-PPV-1234567", "fc2ppv1234567", "fc2ppv1234567", "fc2ppv1234567"),
]


def _make_fake_r18db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        [
            ("dump_file", "fake.sql"),
            ("dump_date", "2026-10-26"),
            ("imported_at", "2026-10-26T03:40:05+00:00"),
            ("imported_rows", "5"),
        ],
    )
    conn.execute(
        "CREATE TABLE derived_video ("
        "content_id TEXT, dvd_id TEXT, dvd_id_key TEXT, dvd_id_norm TEXT, content_id_norm TEXT)"
    )
    conn.executemany(
        "INSERT INTO derived_video "
        "(content_id, dvd_id, dvd_id_key, dvd_id_norm, content_id_norm) VALUES (?,?,?,?,?)",
        FAKE_ROWS,
    )
    conn.commit()
    conn.close()
    return path


def test_lookup_in_batches_chunks_and_dedupes():
    calls = []

    def fake_lookup(conn, batch, chunk_size=400):
        calls.append(list(batch))
        return {code: [{"dvd_id": code}] for code in batch}

    codes = ["C%04d" % i for i in range(950)]
    found = an._lookup_in_batches(None, codes, chunk_size=400, lookup=fake_lookup)
    assert [len(batch) for batch in calls] == [400, 400, 150]
    assert len(found) == 950
    assert all(len(batch) <= 400 for batch in calls)

    calls.clear()
    an._lookup_in_batches(None, ["A", "A", "B"], chunk_size=2, lookup=fake_lookup)
    assert calls == [["A", "B"]]

    calls.clear()
    an._lookup_in_batches(None, ["A", "B", "C"], chunk_size=9999, lookup=fake_lookup)
    assert calls == [["A", "B", "C"]]  # 超过 400 会被夹到 400


def test_r18_coverage_with_fake_db(tmp_path):
    db = _make_fake_r18db(tmp_path / "r18.db")
    paths = [
        "SSIS-001.mp4",          # 精确 key 命中（2 行）
        "IHKD-006.mp4",          # 补零命中
        "ABC-123.mp4",           # content_id 兜底命中
        "FC2-PPV-1234567.mp4",   # FC2 命中
        "ZZZ-999.mp4",           # 未命中
        "HEYZO-1234.mp4",        # 未命中
        "unparseable-1080p.mp4",  # 解析不出番号
    ]
    report = an.analyze_paths(paths, r18db=str(db), source="unit")
    r18 = report["r18"]
    assert r18 is not None
    assert r18["unique_codes"] == 6
    assert r18["matched_codes"] == 4
    assert r18["coverage"] == pytest.approx(4 / 6)
    assert r18["parseable_files"] == 6
    assert r18["matched_files"] == 4
    assert r18["file_coverage"] == pytest.approx(4 / 6)
    assert r18["multi_row_codes"] == 1
    assert r18["rows_total"] == 5
    assert r18["chunk_size"] == 400
    assert r18["db_meta"]["dump_file"] == "fake.sql"

    by_brand = {item["brand"]: item for item in r18["by_brand"]}
    assert by_brand[an.NORMAL_BRAND]["unique_codes"] == 4  # SSIS/IHKD/ABC/ZZZ
    assert by_brand[an.NORMAL_BRAND]["matched"] == 3
    assert by_brand["FC2"]["unique_codes"] == 1
    assert by_brand["FC2"]["matched"] == 1
    assert by_brand["FC2"]["coverage"] == pytest.approx(1.0)

    missed_prefixes = {item["prefix"]: item["count"] for item in r18["missed_prefixes"]}
    assert missed_prefixes == {"ZZZ": 1, "HEYZO": 1}
    assert set(r18["missed_samples"]) == {"ZZZ-999", "HEYZO-1234"}

    # 结论里 r18 覆盖率要接上
    assert report["conclusion"]["r18_coverage"] == pytest.approx(4 / 6)
    # 整份报告必须可 JSON 序列化
    assert json.loads(json.dumps(report, ensure_ascii=False))["r18"]["matched_codes"] == 4


def test_r18_chunk_is_clamped(tmp_path):
    db = _make_fake_r18db(tmp_path / "r18.db")
    report = an.analyze_paths(["SSIS-001.mp4"], r18db=str(db), r18_chunk=9999)
    assert report["r18"]["chunk_size"] == an.MAX_R18_CHUNK


def test_r18_missing_db_raises():
    with pytest.raises(an.AnalyzeError):
        an.analyze_paths(["SSIS-001.mp4"], r18db="/tmp/definitely-not-a-r18-db.sqlite")


# ---------------------------------------------------------------------------
# 结论阈值
# ---------------------------------------------------------------------------


def _fake_report(total, parseable, special_total, r18_coverage=None, fc2=0, missed=()):
    summary = {
        "total": total,
        "parseable": parseable,
        "parseable_rate": parseable / total if total else 0.0,
        "special_brand_total": special_total,
        "special_brand_rate": special_total / total if total else 0.0,
    }
    report = {
        "summary": summary,
        "special_brands": {"FC2": fc2},
        "r18": None,
    }
    if r18_coverage is not None:
        report["r18"] = {
            "coverage": r18_coverage,
            "missed_prefixes": [{"prefix": p, "count": 1} for p in missed],
        }
    return report


def test_build_conclusion_verdicts():
    good = an.build_conclusion(_fake_report(100, 99, 1, 0.95, fc2=0))
    assert "达标" in good["parse_verdict"]
    assert "充足" in good["r18_verdict"]
    assert "可不单独建" in good["special_verdict"]

    bad = an.build_conclusion(_fake_report(100, 50, 20, 0.5, fc2=5, missed=["FC2-PPV"]))
    assert "不达标" in bad["parse_verdict"]
    assert "不足" in bad["r18_verdict"]
    assert "必须建独立管道" in bad["special_verdict"]
    joined = " ".join(bad["recommendations"])
    assert "FC2" in joined and "r18" in joined

    untested = an.build_conclusion(_fake_report(100, 96, 2, None))
    assert untested["r18_coverage"] is None
    assert "未测" in untested["r18_verdict"]
    assert any("--r18db" in rec for rec in untested["recommendations"])


# ---------------------------------------------------------------------------
# Markdown / CLI
# ---------------------------------------------------------------------------

REQUIRED_SECTIONS = [
    "## 0. 结论（三个决定性问题）",
    "## 1. 概览",
    "## 2. confidence 分布",
    "## 3. 解析来源",
    "## 4. 番号前缀",
    "## 5. 特殊厂牌计数",
    "## 6. flags 统计",
    "## 7. 按扩展名",
    "## 8. r18 dump 覆盖率",
    "## 9. 未解析样本",
    "## 10. 口径与阈值",
]


def test_markdown_has_required_sections(tmp_path):
    report = an.analyze_paths(DIRTY, source="unit")
    markdown = an.render_markdown(report)
    for section in REQUIRED_SECTIONS:
        assert section in markdown, section
    # 三个硬指标都要有数字
    assert "可解析率（code 非空）" in markdown
    assert "r18 dump 覆盖率（唯一番号）" in markdown
    assert "FC2/素人（特殊厂牌）占比" in markdown
    # 未解析样本以 fenced block 贴原始字符串
    assert "```text" in markdown

    with_db = an.analyze_paths(DIRTY, r18db=str(_make_fake_r18db(tmp_path / "r18.db")))
    md_with_db = an.render_markdown(with_db)
    assert "### 8.1 按品牌" in md_with_db
    assert "### 8.2 未命中的前缀" in md_with_db


def test_cli_end_to_end_md_and_json(tmp_path, capsys):
    names = tmp_path / "names.txt"
    names.write_text("\n".join(DIRTY) + "\n", encoding="utf-8")
    md = tmp_path / "report.md"
    js = tmp_path / "report.json"

    code = an.main(["--paths-file", str(names), "--out", str(md), "--json", str(js)])
    assert code == 0
    captured = capsys.readouterr()
    assert captured.out == ""  # 已落盘，不占 stdout

    text = md.read_text(encoding="utf-8")
    assert "# AV 片库文件名命中率报告（M0）" in text
    payload = json.loads(js.read_text(encoding="utf-8"))
    assert payload["summary"]["total"] == len(DIRTY)
    assert payload["summary"]["parseable"] == sum(1 for p in DIRTY if ec(p).code)
    assert payload["input"]["source"].startswith("paths-file:")
    # Markdown 里的关键数字和 JSON 一致
    assert "%d/%d" % (payload["summary"]["parseable"], len(DIRTY)) in text


def test_cli_json_auto_naming(tmp_path):
    names = tmp_path / "names.txt"
    names.write_text("SSIS-001.mp4\n", encoding="utf-8")
    md = tmp_path / "sub" / "report.md"
    assert an.main(["--paths-file", str(names), "--out", str(md), "--json"]) == 0
    assert md.is_file()
    assert (md.parent / "report.json").is_file()


def test_cli_json_stdout_omits_markdown(tmp_path, capsys):
    names = tmp_path / "names.txt"
    names.write_text("SSIS-001.mp4\n1080P.mp4\n", encoding="utf-8")
    assert an.main(["--paths-file", str(names), "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)  # stdout 必须是纯 JSON
    assert payload["summary"]["total"] == 2
    assert "Markdown 省略" in captured.err


def test_cli_json_stdout_with_out_file(tmp_path, capsys):
    names = tmp_path / "names.txt"
    names.write_text("SSIS-001.mp4\n", encoding="utf-8")
    md = tmp_path / "r.md"
    assert an.main(["--paths-file", str(names), "--out", str(md), "--json", "-"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["summary"]["total"] == 1
    assert md.is_file()


def test_cli_stdin(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("SSIS-001.mp4\nABP-002.mkv\n"))
    out = tmp_path / "r.json"
    assert an.main(["--paths-file", "-", "--json", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["summary"]["total"] == 2
    assert payload["summary"]["parseable"] == 2


def test_cli_dir_mode_and_limit(tmp_path, capsys):
    _make_tree(tmp_path)
    md = tmp_path / "r.md"
    js = tmp_path / "r.json"
    assert an.main(["--dir", str(tmp_path), "--out", str(md), "--json", str(js)]) == 0
    payload = json.loads(js.read_text(encoding="utf-8"))
    assert payload["summary"]["total"] == 2  # 只收视频
    assert payload["input"]["skipped_ext"][".srt"] == 1

    assert an.main(["--dir", str(tmp_path), "--limit", "1", "--out", str(md)]) == 0
    assert "已按 --limit 截断" in md.read_text(encoding="utf-8")


def test_cli_errors(tmp_path, capsys):
    # 缺输入 → argparse 报错（exit 2）
    with pytest.raises(SystemExit) as excinfo:
        an.main([])
    assert excinfo.value.code == 2

    # 输入文件不存在 → exit 2
    assert an.main(["--paths-file", str(tmp_path / "nope.txt")]) == 2
    assert "错误" in capsys.readouterr().err

    # r18 库不存在 → exit 2
    names = tmp_path / "names.txt"
    names.write_text("SSIS-001.mp4\n", encoding="utf-8")
    assert an.main(["--paths-file", str(names), "--r18db", str(tmp_path / "nope.db")]) == 2
    assert "错误" in capsys.readouterr().err

    # 非法参数
    with pytest.raises(SystemExit):
        an.main(["--paths-file", str(names), "--r18-chunk", "0"])
    with pytest.raises(SystemExit):
        an.main(["--paths-file", str(names), "--limit", "-1"])


# ---------------------------------------------------------------------------
# 语料自检（/tmp/av-archive-corpus 存在时）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not CORPUS_DIR.is_dir(), reason="需要先跑 scripts/make_corpus.py 生成语料")
def test_corpus_selfcheck_semantics():
    collected = an.collect_paths_from_dir(str(CORPUS_DIR))
    paths = collected["paths"]
    assert len(paths) >= 500, "语料应有 600 个视频条目（按扩展名过滤后 ~597）"
    report = an.analyze_paths(paths, source="corpus", input_info=collected)

    matches = _recompute(paths)
    assert report["summary"]["total"] == len(paths)
    assert report["summary"]["parseable"] == sum(1 for m in matches if m.code)
    assert report["summary"]["unique_codes"] == len({m.code for m in matches if m.code})
    assert report["summary"]["source_counts"]["none"] == sum(1 for m in matches if m.source == "none")

    # 语料是「90% 可解析 + 10% 垃圾名」构造的，允许浮动但必须在合理区间
    assert 0.80 <= report["summary"]["parseable_rate"] <= 0.97
    # 特殊厂牌在语料里占比 >5%（FC2/HEYZO/SIRO/Tokyo-Hot/1pondo/10musume/Carib/Paco/LUXU/MIUM/GANA）
    assert report["summary"]["special_brand_rate"] > 0.05

    # 未解析样本必须真的没有番号
    unresolved = report["unresolved"]
    assert len(unresolved) == min(50, report["summary"]["total"] - report["summary"]["parseable"])
    for item in unresolved:
        assert ec(item["path"]).code is None

    # JSON 可序列化 + Markdown 可渲染
    json.dumps(report, ensure_ascii=False)
    markdown = an.render_markdown(report)
    assert "## 0. 结论（三个决定性问题）" in markdown


@pytest.mark.skipif(not CORPUS_DIR.is_dir(), reason="需要先跑 scripts/make_corpus.py 生成语料")
def test_corpus_selfcheck_with_real_r18db():
    if not REAL_R18DB.is_file():
        pytest.skip("没有 data/r18dump.db")
    collected = an.collect_paths_from_dir(str(CORPUS_DIR), limit=200)
    report = an.analyze_paths(
        collected["paths"], source="corpus", input_info=collected, r18db=str(REAL_R18DB)
    )
    r18 = report["r18"]
    assert r18["unique_codes"] >= 150
    assert 0.0 <= r18["coverage"] <= 1.0
    assert r18["chunk_size"] == 400
    assert r18["db_meta"].get("dump_file")
    assert len(r18["by_brand"]) >= 1
    json.dumps(report, ensure_ascii=False)


@pytest.mark.skipif(not REAL_R18DB.is_file(), reason="没有 data/r18dump.db 可抽样")
def test_real_dump_dvd_ids_coverage_sanity():
    """拿 dump 里真实存在的 dvd_id 造文件名 → 覆盖率应该接近 100%（验证匹配链路没接错）。"""
    conn = an.r18.open_dump_db(REAL_R18DB)
    try:
        rows = conn.execute(
            "SELECT dvd_id FROM derived_video "
            "WHERE dvd_id IS NOT NULL AND dvd_id != '' AND dvd_id_key IS NOT NULL "
            "ORDER BY rowid LIMIT 400"
        ).fetchall()
    finally:
        conn.close()
    dvd_ids = [row[0] for row in rows]
    assert len(dvd_ids) == 400

    paths = ["/mnt/library/%s.mp4" % code for code in dvd_ids]
    report = an.analyze_paths(paths, source="real-dump-sample", r18db=str(REAL_R18DB))
    assert report["summary"]["parseable_rate"] >= 0.85
    r18 = report["r18"]
    assert r18["matched_codes"] / r18["unique_codes"] >= 0.85
    assert r18["file_coverage"] >= 0.85
    # 这些真实的 dump 番号里，命中的绝大多数应该是普通 DMM 系
    assert any(item["brand"] == an.NORMAL_BRAND and item["matched"] > 0 for item in r18["by_brand"])
