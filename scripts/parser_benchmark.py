#!/usr/bin/env python3
"""番号解析器基准（真实 r18 dvd_id + 命名变体）—— 防回归用。

背景：`data/r18dump.db` 的 `derived_video.dvd_id` 是 190 万条**真实**番号，
比合成语料更能暴露解析器盲区（老 DMM 单字母号、数字前缀号、6 位日期号……）。
本脚本抽样 N 条真实 dvd_id，按真实下载站的命名习惯造若干变体文件名，跑
`app/services/code_parser.py`，输出：

* **主变体可解析率**（`{dvd_id}.mp4`，目标 ≥97%）
* 分变体可解析率（下载站前缀 / 中字后缀 / 1080p / 小写 / 下划线）
* **归一化一致率**（解析出的番号经 `r18.dvd_id_norm` 后与 dvd_id 是否等价）——
  这个比"可解析"更严格，直接决定后续能不能在 dump 里命中
* 失败样本（解析不出 / 解析错）与前缀分布

用法
----
    python3 scripts/parser_benchmark.py                         # 默认 2000 条，Markdown 到 stdout
    python3 scripts/parser_benchmark.py --limit 5000 --out /tmp/parser-bench.md --json /tmp/parser-bench.json
    python3 scripts/parser_benchmark.py --fail-under 97          # 低于目标时 exit 1（给 CI/回归用）

依赖：纯标准库；复用（不修改）`app/services/code_parser.py` 与 `app/services/r18.py`，
两者按文件路径加载以避免 `app/__init__.py` 的 Flask 依赖。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
_SERVICES_DIR = ROOT / "app" / "services"
DEFAULT_DB = ROOT / "data" / "r18dump.db"

TARGET_RATE = 0.97  # 主变体可解析率目标
DEFAULT_LIMIT = 2000
DEFAULT_FAILURE_SAMPLES = 25


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError("无法加载模块：%s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclass + future annotations 需要
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


code_parser = _load_module("_av_code_parser", _SERVICES_DIR / "code_parser.py")
try:
    r18 = _load_module("_av_r18", _SERVICES_DIR / "r18.py")
except Exception:  # pragma: no cover
    r18 = None  # type: ignore[assignment]


class BenchmarkError(RuntimeError):
    """可预期的用户错误（库不存在/缺表）→ CLI exit 2。"""


# ---------------------------------------------------------------------------
# 命名变体（模拟真实下载站命名）
# ---------------------------------------------------------------------------

_DMM_STD_RE = re.compile(r"[A-Z]{2,6}-\d{3}")


def _is_dmm_std(dvd_id: str) -> bool:
    """`PREFIX-123` 这种标准 3 位写法才适合造"粘连小写"变体。"""
    return _DMM_STD_RE.fullmatch(dvd_id) is not None


def build_variants(dvd_id: str) -> List[Dict[str, str]]:
    """给一个真实 dvd_id 造若干命名变体（variant 名, 文件名）。"""
    variants = [
        {"variant": "clean", "filename": "%s.mp4" % dvd_id},
        {"variant": "download-prefix", "filename": "[ThZu.Cc]%s.mkv" % dvd_id},
        {"variant": "chinese-sub", "filename": "%s-C.mp4" % dvd_id},
        {"variant": "hi-res", "filename": "%s-1080p.mp4" % dvd_id},
        {"variant": "lower", "filename": "%s.mp4" % dvd_id.lower()},
        {"variant": "underscore", "filename": "%s.mp4" % dvd_id.replace("-", "_")},
    ]
    if _is_dmm_std(dvd_id):
        # 下载站常见的粘连小写写法：SSIS-001 -> ssis001
        variants.append({"variant": "glued", "filename": "%s.mp4" % dvd_id.replace("-", "").lower()})
    return variants


# ---------------------------------------------------------------------------
# 基准核心
# ---------------------------------------------------------------------------

def _norm(value: Optional[str]) -> Optional[str]:
    if r18 is not None:
        return r18.dvd_id_norm(value)
    if not value:
        return None
    return "".join(ch for ch in value.lower() if ch.isalnum()) or None


def run_benchmark(
    dvd_ids: Sequence[str],
    *,
    failure_samples: int = DEFAULT_FAILURE_SAMPLES,
) -> Dict[str, Any]:
    """对 dvd_id 列表跑变体基准，返回可直接 JSON 序列化的报告 dict。"""
    started = time.perf_counter()
    rows: List[Dict[str, Any]] = []
    variant_stats: Dict[str, Dict[str, Any]] = {}
    primary_parse = 0
    primary_match = 0
    failure_no_parse: List[Dict[str, str]] = []
    failure_mismatch: List[Dict[str, Any]] = []
    failed_prefixes: Counter = Counter()
    counts = Counter()

    for dvd_id in dvd_ids:
        expected_norm = _norm(dvd_id)
        primary_ok = primary_match_ok = None
        for item in build_variants(dvd_id):
            variant = item["variant"]
            filename = item["filename"]
            stats = variant_stats.setdefault(
                variant, {"variant": variant, "total": 0, "parseable": 0, "norm_match": 0}
            )
            match = code_parser.extract_code(filename)
            parsed = match.code
            ok = parsed is not None
            norm_ok = ok and _norm(parsed) == expected_norm
            stats["total"] += 1
            stats["parseable"] += 1 if ok else 0
            stats["norm_match"] += 1 if norm_ok else 0
            counts["total"] += 1
            counts["parseable"] += 1 if ok else 0
            counts["norm_match"] += 1 if norm_ok else 0
            if not ok:
                failed_prefixes[_code_prefix(dvd_id)] += 1
                if len(failure_no_parse) < failure_samples:
                    failure_no_parse.append(
                        {"dvd_id": dvd_id, "variant": variant, "filename": filename}
                    )
            elif not norm_ok and len(failure_mismatch) < failure_samples:
                failure_mismatch.append(
                    {"dvd_id": dvd_id, "variant": variant, "filename": filename,
                     "parsed": parsed, "confidence": match.confidence}
                )
            if variant == "clean":
                primary_ok = ok
                primary_match_ok = norm_ok
        if primary_ok:
            primary_parse += 1
        if primary_match_ok:
            primary_match += 1
        rows.append({
            "dvd_id": dvd_id,
            "primary_parseable": bool(primary_ok),
            "primary_norm_match": bool(primary_match_ok),
        })

    total_primary = len(dvd_ids)
    variants = sorted(variant_stats.values(), key=lambda v: (-v["total"], v["variant"]))
    for stats in variants:
        stats["parse_rate"] = _rate(stats["parseable"], stats["total"])
        stats["match_rate"] = _rate(stats["norm_match"], stats["total"])
    primary_rate = _rate(primary_parse, total_primary)
    report: Dict[str, Any] = {
        "generated_at": _iso_now(),
        "target_rate": TARGET_RATE,
        "dvd_ids": total_primary,
        "primary": {
            "total": total_primary,
            "parseable": primary_parse,
            "parse_rate": primary_rate,
            "norm_match": primary_match,
            "match_rate": _rate(primary_match, total_primary),
            "passed": primary_rate >= TARGET_RATE,
        },
        "overall_variants": {
            "total": counts["total"],
            "parseable": counts["parseable"],
            "parse_rate": _rate(counts["parseable"], counts["total"]),
            "norm_match": counts["norm_match"],
            "match_rate": _rate(counts["norm_match"], counts["total"]),
        },
        "variants": variants,
        "failures": {
            "no_parse": failure_no_parse,
            "mismatch": failure_mismatch,
        },
        "failure_prefixes": [
            {"prefix": prefix, "count": count}
            for prefix, count in failed_prefixes.most_common(15)
        ],
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    return report


def _code_prefix(dvd_id: str) -> str:
    """失败样本按前缀归类（与 analyze_names 同口径：`FC2-PPV`/`TOKYO-HOT` 不拆）。"""
    parts = str(dvd_id).split("-")
    head = parts[0].upper()
    if len(parts) >= 2 and head in ("FC2", "TOKYO") and parts[1].upper() in ("PPV", "HOT"):
        return head + "-" + parts[1].upper()
    return head


def _rate(numerator: int, denominator: int) -> float:
    return float(numerator) / denominator if denominator else 0.0


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _pct(value: float) -> str:
    return "%.2f%%" % (value * 100.0)


# ---------------------------------------------------------------------------
# 抽样 / 库访问
# ---------------------------------------------------------------------------

def sample_dvd_ids(db_path: str, limit: int = DEFAULT_LIMIT, offset: int = 0) -> List[str]:
    r"""从 staging 库按 rowid 顺序抽 N 个真实 dvd_id（确定性，便于回归对比）。"""
    path = Path(db_path)
    if not path.is_file():
        raise BenchmarkError("r18 staging 库不存在：%s（先跑 scripts/import_dump.py）" % path)
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    except sqlite3.Error as exc:
        raise BenchmarkError("打开 r18 staging 库失败：%s（%s）" % (path, exc))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        rows = conn.execute(
            "SELECT dvd_id FROM derived_video "
            "WHERE dvd_id IS NOT NULL AND dvd_id != '' "
            "ORDER BY rowid LIMIT ? OFFSET ?",
            (int(limit), int(offset)),
        ).fetchall()
    except sqlite3.Error as exc:
        raise BenchmarkError("r18 staging 库缺少 derived_video / 查询失败：%s" % exc)
    finally:
        conn.close()
    return [str(row[0]) for row in rows]


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def render_markdown(report: Dict[str, Any], *, db_path: str = "") -> str:
    out: List[str] = []
    add = out.append
    primary = report["primary"]
    overall = report["overall_variants"]

    add("# 番号解析器基准（真实 r18 dvd_id）")
    add("")
    add("> 库：`%s` ｜ 抽样：**%d** 条真实 dvd_id ｜ 目标：主变体可解析率 ≥ %s"
        % (db_path or "<memory>", report["dvd_ids"], _pct(report["target_rate"])))
    add("> 生成时间：%s ｜ 耗时 %.2fs ｜ 解析器：`app/services/code_parser.py`"
        % (report["generated_at"], report["elapsed_seconds"]))
    add("")

    add("## 0. 结论")
    add("")
    verdict = "**PASS** ✅" if primary["passed"] else "**未达标** ❌"
    add("| 指标 | 数值 | 目标 | 结论 |")
    add("|---|---|---|---|")
    add("| 主变体（`{dvd_id}.mp4`）可解析率 | **%s**（%d/%d） | ≥%s | %s |"
        % (_pct(primary["parse_rate"]), primary["parseable"], primary["total"],
           _pct(report["target_rate"]), verdict))
    add("| 主变体归一化一致率 | %s（%d/%d） | — | 解析对了但归一化不一致会漏 dump 匹配 |"
        % (_pct(primary["match_rate"]), primary["norm_match"], primary["total"]))
    add("| 全部变体可解析率 | %s（%d/%d） | — | 含下载站前缀/后缀/大小写变体 |"
        % (_pct(overall["parse_rate"]), overall["parseable"], overall["total"]))
    add("| 全部变体归一化一致率 | %s（%d/%d） | — | |"
        % (_pct(overall["match_rate"]), overall["norm_match"], overall["total"]))
    add("")

    add("## 1. 分变体结果")
    add("")
    add("| 变体 | 样本数 | 可解析 | 可解析率 | 归一化一致 | 一致率 |")
    add("|---|---|---|---|---|---|")
    for stats in report["variants"]:
        add("| %s | %d | %d | %s | %d | %s |"
            % (stats["variant"], stats["total"], stats["parseable"],
               _pct(stats["parse_rate"]), stats["norm_match"], _pct(stats["match_rate"])))
    add("")

    add("## 2. 失败样本")
    add("")
    failures = report["failures"]
    add("### 2.1 完全解析不出（最多 %d 条示例）" % len(failures["no_parse"]))
    add("")
    if failures["no_parse"]:
        add("```text")
        for item in failures["no_parse"]:
            add("%-28s [%s] %s" % (item["dvd_id"], item["variant"], item["filename"]))
        add("```")
    else:
        add("_（没有）_")
    add("")
    add("### 2.2 解析出但归一化不一致（最多 %d 条示例）" % len(failures["mismatch"]))
    add("")
    if failures["mismatch"]:
        add("```text")
        for item in failures["mismatch"]:
            add("%-28s -> %-24s (%d) [%s]"
                % (item["dvd_id"], item["parsed"], item["confidence"], item["variant"]))
        add("```")
    else:
        add("_（没有）_")
    add("")

    add("## 3. 失败前缀 Top")
    add("")
    if report["failure_prefixes"]:
        add("| 前缀 | 失败变体次数 |")
        add("|---|---|")
        for item in report["failure_prefixes"]:
            add("| %s | %d |" % (item["prefix"], item["count"]))
    else:
        add("_（没有失败）_")
    add("")
    add("## 4. 已知刻意不覆盖（避免误伤真实文件名）")
    add("")
    add("- `ADV-R0632` / `WBR-R033` 型：字母前缀 + `R` + 数字，2000 条里约 0.1%，"
        "支持它会牺牲 `-A/-B` 分片语义。")
    add("- `CHS-*` / `SP-*` / `SUB-*` / `IMG-*`：真实 dump 里确实有这些 label，但作为"
        "文件名前缀会和字幕标记（chs/sub）、sample（sp）、相机文件（IMG_1234）冲突，"
        "保留在噪声黑名单 —— 宁可漏也别把相机照片当番号。")
    add("- 单字母 + 2 位数字（`B-26`）、7 字母前缀（`MAXAVRF-001`）：形态太少，"
        "放宽会带来 `X-1`/`HOLIDAY-2024` 这类误判。")
    add("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _write_text(target: str, text: str) -> None:
    if target == "-":
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="parser_benchmark.py",
        description="番号解析器基准：真实 r18 dvd_id + 命名变体，输出可解析率与失败样本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  %(prog)s --limit 2000 --out /tmp/parser-bench.md --json /tmp/parser-bench.json\n"
            "  %(prog)s --limit 5000 --fail-under 97\n"
        ),
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), metavar="PATH",
                        help="r18 staging 库（默认 data/r18dump.db，只读）")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, metavar="N",
                        help="抽样 dvd_id 数（默认 %d）" % DEFAULT_LIMIT)
    parser.add_argument("--offset", type=int, default=0, metavar="N",
                        help="从第 N 条开始抽（换批次回归用）")
    parser.add_argument("--out", default="-", metavar="PATH",
                        help="Markdown 输出（缺省 stdout；`-` 表示 stdout）")
    parser.add_argument("--json", nargs="?", const="@auto", default=None, metavar="PATH",
                        help="JSON 输出（缺省：--out 是文件时写同名 .json，否则占 stdout）")
    parser.add_argument("--failure-samples", type=int, default=DEFAULT_FAILURE_SAMPLES, metavar="N",
                        help="失败样本贴多少条（默认 %d）" % DEFAULT_FAILURE_SAMPLES)
    parser.add_argument("--fail-under", type=float, default=None, metavar="PCT",
                        help="主变体可解析率低于该百分比时 exit 1（CI 用，如 97）")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit <= 0:
        parser.error("--limit 必须 > 0")
    if args.offset < 0:
        parser.error("--offset 不能为负")

    try:
        dvd_ids = sample_dvd_ids(args.db, limit=args.limit, offset=args.offset)
        if not dvd_ids:
            raise BenchmarkError("抽样为空：库里没有可用的 dvd_id（或 offset 越界）")
        report = run_benchmark(dvd_ids, failure_samples=max(0, args.failure_samples))
    except BenchmarkError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 2

    markdown = render_markdown(report, db_path=args.db)
    json_text = json.dumps(report, ensure_ascii=False, indent=2)

    md_target = args.out or "-"
    json_target: Optional[str]
    if args.json is None:
        json_target = None
    elif args.json != "@auto":
        json_target = args.json
    elif md_target != "-":
        json_target = str(Path(md_target).with_suffix(".json"))
    else:
        json_target = "-"

    if json_target == "-" and md_target == "-":
        sys.stderr.write(
            "提示：JSON 已输出到 stdout，Markdown 省略；要同时保留报告请加 --out report.md\n"
        )
        sys.stdout.write(json_text + "\n")
    else:
        _write_text(md_target, markdown)
        if json_target is not None:
            _write_text(json_target, json_text)

    if args.fail_under is not None and report["primary"]["parse_rate"] * 100.0 < args.fail_under:
        sys.stderr.write(
            "未达标：主变体可解析率 %s < --fail-under %.2f%%\n"
            % (_pct(report["primary"]["parse_rate"]), args.fail_under)
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
