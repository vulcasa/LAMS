"""把 r18.dev 每周 dump 导入独立 staging SQLite 库。

用法::

    # 完整导入（默认写入 data/r18dump.db，幂等重建）
    python -m scripts.import_dump --sql /tmp/r18dump/latest.sql --out data/r18dump.db

    # 也支持 gzip（按魔数自动识别）
    python -m scripts.import_dump --sql /tmp/r18dump/latest.sql.gz

    # 只统计已有 staging 库的覆盖率（不重新导入）
    python -m scripts.import_dump --stats-only data/r18dump.db

    # 导入后顺便输出覆盖率 JSON（research/dump-coverage.md 的数字都来自它）
    python -m scripts.import_dump --sql ... --stats-json research/dump-stats.json

为什么按文件路径加载 ``app/services/r18.py``：本脚本只需要标准库的解析/导入
能力，不应连带 import Flask 应用包（``app/__init__.py`` 会拉 Flask/SQLAlchemy），
这样系统 ``python3`` 不装依赖也能跑。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "data" / "r18dump.db"


def _load_r18():
    """按文件路径加载 r18 模块（不触发 app 包的 Flask 依赖）。"""
    module_path = ROOT / "app" / "services" / "r18.py"
    spec = importlib.util.spec_from_file_location("avlib_r18", module_path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"无法加载 {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"  # pragma: no cover


def _fmt_rows(rows: int) -> str:
    return f"{rows:,}"


def _print_import_report(result, skipped_tables) -> None:
    label = "表"
    print()
    print(f"{label:<24}{'行数':>14}{'耗时(s)':>10}  状态")
    print("-" * 62)
    for name, stat in result.tables.items():
        state = "imported" if stat.imported else "skipped (不建表)"
        print(f"{name:<24}{_fmt_rows(stat.rows):>14}{stat.seconds:>10.2f}  {state}")
    print("-" * 62)
    print(f"{'合计':<24}{_fmt_rows(result.total_rows):>14}{result.duration_s:>10.2f}")
    index_total = sum(result.index_seconds.values())
    print(f"索引: {sum(1 for _ in result.index_seconds)} 张表 / {index_total:.2f}s"
          f"；库大小 {_human_bytes(result.db_bytes)}")
    if result.analyze_seconds:
        print(f"ANALYZE: {result.analyze_seconds:.2f}s")
    if result.sha256:
        print(f"dump sha256: {result.sha256}")
    if skipped_tables:
        print(f"未建表（只计数）: {', '.join(skipped_tables)}")


def _print_stats_summary(stats: dict) -> None:
    meta = stats.get("meta") or {}
    print()
    print("=== r18 dump 覆盖率摘要 ===")
    print(f"dump: {meta.get('dump_file', '?')}  date={meta.get('dump_date', '?')}"
          f"  导入于 {meta.get('imported_at', '?')}")
    if stats.get("tables"):
        print("\n[表行数]")
        for name, info in stats["tables"].items():
            flag = "" if info["imported"] else "  (skipped，仅计数)"
            print(f"  {name:<24}{_fmt_rows(info['rows']):>14}{flag}")
    video = stats.get("video") or {}
    if video:
        print("\n[derived_video]")
        print(f"  总行数                 {_fmt_rows(video['rows'])}")
        print(f"  dvd_id 可用             {_fmt_rows(video['dvd_id_usable'])}"
              f"  ({video['dvd_id_usable_ratio']:.2%})"
              f"  null={_fmt_rows(video['dvd_id_null'])} empty={_fmt_rows(video['dvd_id_empty'])}")
        print(f"  dvd_id 归一化后唯一值    {_fmt_rows(video['dvd_id_norm_distinct'])}"
              f"（重复键 {_fmt_rows(video['dvd_id_norm_duplicated'])} 个）")
        print(f"  title_ja 非空           {_fmt_rows(video['title_ja_nonempty'])}"
              f"  ({video['title_ja_nonempty_ratio']:.2%})")
        print(f"  title_en 非空           {_fmt_rows(video['title_en_nonempty'])}"
              f"  ({video['title_en_nonempty_ratio']:.2%})")
        print(f"  ja+en 都有              {_fmt_rows(video['title_ja_and_en_nonempty'])}"
              f"  ({video['title_ja_and_en_nonempty'] / video['rows']:.2%})")
        print(f"  release_date / runtime  "
              f"{_fmt_rows(video['release_date_notnull'])} / {_fmt_rows(video['runtime_mins_notnull'])}")
        print(f"  发行日期范围            {video['release_date_min']} ~ {video['release_date_max']}")
    actress = stats.get("actress") or {}
    if actress:
        print("\n[derived_actress]")
        for column in ("name_kanji", "name_kana", "name_romaji", "image_url"):
            print(f"  {column:<12} 非空 {_fmt_rows(actress[column + '_nonempty']):>8}"
                  f"  ({actress[column + '_nonempty_ratio']:.2%})")
    category = stats.get("category") or {}
    if category:
        print("\n[derived_category]")
        print(f"  name_ja 非空  {_fmt_rows(category['name_ja_nonempty'])}"
              f"  ({category['name_ja_nonempty_ratio']:.2%})"
              f" / name_en 非空 {_fmt_rows(category['name_en_nonempty'])}"
              f" ({category['name_en_nonempty_ratio']:.2%})")
    gallery = stats.get("gallery") or {}
    if gallery:
        print("\n[gallery 剧照张数分布]")
        for bucket, count in gallery.get("count_distribution", {}).items():
            print(f"  {bucket:>6} 张: {_fmt_rows(count)}")
    prefixes = stats.get("dvd_id_prefixes") or []
    if prefixes:
        print("\n[dvd_id 前缀 Top 15]")
        print("  " + ", ".join(f"{item['prefix']}({_fmt_rows(item['count'])})"
                              for item in prefixes[:15]))
    chinese = stats.get("chinese") or {}
    if chinese:
        print(f"\n[中文检测] 抽 {_fmt_rows(chinese['english_fields_scanned'])} 条英文标题/简介，"
              f"含 CJK 字符 {chinese['cjk_in_english_fields']} 条")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把 r18.dev 的 pg_dump 导入独立 staging SQLite 库（不污染主库）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--sql", help="dump 路径（.sql 或 .sql.gz）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="目标 staging SQLite 路径")
    parser.add_argument("--tables", default=None,
                        help="逗号分隔的表名（默认全部 core 表）")
    parser.add_argument("--batch-size", type=int, default=10_000,
                        help="每个事务插入的行数")
    parser.add_argument("--no-index", action="store_true", help="不建索引（调试用）")
    parser.add_argument("--analyze", action="store_true", help="导入后跑 ANALYZE")
    parser.add_argument("--no-sha256", action="store_true", help="不计算 dump sha256")
    parser.add_argument("--progress", action="store_true", help="打印导入进度")
    parser.add_argument("--stats-only", metavar="DB",
                        help="不导入，只对已有 staging 库输出覆盖率统计")
    parser.add_argument("--stats-json", metavar="PATH",
                        help="把覆盖率统计写成 JSON（导入完成后或 --stats-only 时）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    r18 = _load_r18()

    if not args.stats_only and not args.sql:
        print("错误: 需要 --sql（或用 --stats-only 分析已有库）", file=sys.stderr)
        return 2

    if not args.stats_only:
        sql_path = Path(args.sql).expanduser()
        out_path = Path(args.out).expanduser()
        if not sql_path.is_file():
            print(f"错误: dump 不存在: {sql_path}", file=sys.stderr)
            return 2
        selected = None
        if args.tables:
            selected = [name.strip() for name in args.tables.split(",") if name.strip()]
        progress = None
        if args.progress:
            last = {"t": 0.0}

            def progress(table, rows, elapsed):
                now = time.monotonic()
                if now - last["t"] >= 1.0:
                    last["t"] = now
                    sys.stderr.write(f"\r  {table}: {rows:,} 行 ({elapsed:.1f}s)   ")
                    sys.stderr.flush()

        print(f"导入 dump: {sql_path} ({_human_bytes(sql_path.stat().st_size)})")
        print(f"目标库:   {out_path}")
        try:
            result = r18.import_dump(
                sql_path,
                out_path,
                tables=selected,
                batch_size=args.batch_size,
                progress=progress,
                build_indexes=not args.no_index,
                analyze=args.analyze,
                compute_sha256=not args.no_sha256,
            )
        except (ValueError, OSError) as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 1
        if args.progress:
            sys.stderr.write("\r" + " " * 70 + "\r")
            sys.stderr.flush()
        skipped = [name for name, stat in result.tables.items() if not stat.imported]
        _print_import_report(result, skipped)
        db_path = out_path
    else:
        db_path = Path(args.stats_only).expanduser()

    if args.stats_json or args.stats_only:
        conn = r18.open_dump_db(db_path)
        try:
            stats = r18.coverage_stats(conn)
        finally:
            conn.close()
        stats["db_path"] = str(db_path)
        if args.stats_json:
            stats_path = Path(args.stats_json).expanduser()
            stats_path.parent.mkdir(parents=True, exist_ok=True)
            stats_path.write_text(
                json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(f"\n覆盖率统计已写入: {stats_path}")
        _print_stats_summary(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
