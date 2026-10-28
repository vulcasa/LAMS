#!/usr/bin/env python3
"""真实文件名命中率分析工具（M0 硬门槛）。

用户给一批真实文件名（或 NAS 目录）后，本工具立刻产出一份可直接下判断的报告：

1. **可解析率** —— 多少文件能抽出番号、confidence 分布如何、来源（basename/parent）；
2. **r18 dump 覆盖率** —— 可解析番号里多少能在本地 staging 库命中（`--r18db`）；
3. **FC2/素人（特殊厂牌）占比** —— 决定要不要建独立刮削管道。

用法
----
    # 1) 文件名清单（一行一个完整路径或文件名；`-` = stdin）
    python3 scripts/analyze_names.py --paths-file names.txt --out /tmp/m0-report.md --json

    # 2) 直接扫目录（只收视频扩展名，不读文件内容）
    python3 scripts/analyze_names.py --dir /mnt/user/av --limit 1000 \\
        --r18db data/r18dump.db --out /tmp/m0-report.md --json /tmp/m0-report.json

    # 3) 只要机器可读结果（JSON 占 stdout；Markdown 自动省略，加 --out 可同时产出）
    python3 scripts/analyze_names.py --paths-file names.txt --json | jq .summary

    # 4) 用户给的样本：find /mnt/user/av -type f | head -500 | python3 scripts/analyze_names.py - \\
    #    （`-` 是 --paths-file 的 stdin 简写）

输出
----
* Markdown（`--out`，缺省 stdout）：给人看，含「结论」三条硬指标 + 建议；
* JSON（`--json [PATH]`）：给程序。`--json` 不带路径时：若 `--out` 是文件则写同名
  `.json`，否则 JSON 占 stdout（Markdown 省略，stderr 提示）。
  Markdown 和 JSON **不会同时写 stdout**，避免污染管道。

口径（重要）
------------
* `--dir` 默认**只收视频扩展名**（`app/config.py` 的 `VIDEO_EXTS`）；字幕/nfo 属于
  附件，默认不计入命中率分母。需要时加 `--include-sidecars`。
* `--paths-file` 不做扩展名过滤（用户给什么算什么）。
* 可解析率分母 = 收集到的文件数；r18 覆盖率主指标 = **唯一番号**命中率（另附
  文件加权），因为一个番号常有多个文件（正片 + 字幕 + 分碟）。
* 特殊厂牌按归一化番号前缀识别；`10MUSUME` 等不在硬指标 10 个里的归入
  `other_special_brands`，但计入「素人/特殊厂牌占比」。

依赖：纯标准库（可选 sqlite3 走 `--r18db`）。复用（不修改）
`app/services/code_parser.py` 与 `app/services/r18.py`；为了不触发
`app/__init__.py` 的 Flask 依赖，两个模块按**文件路径**加载。
"""

from __future__ import annotations

import argparse
import functools
import importlib.util
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
_SERVICES_DIR = ROOT / "app" / "services"

# ---------------------------------------------------------------------------
# 复用解析器（按文件路径加载，不 import app.*，避免 Flask 依赖）
# ---------------------------------------------------------------------------


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - 防御
        raise ImportError("无法加载模块：%s" % path)
    module = importlib.util.module_from_spec(spec)
    # 必须先登记到 sys.modules：目标模块用了 `from __future__ import annotations`
    # + dataclass，dataclasses 解析类型注解时会回头 sys.modules[cls.__module__]。
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


code_parser = _load_module("_av_code_parser", _SERVICES_DIR / "code_parser.py")
try:  # r18 只在 --r18db 时需要；纯标准库，正常都能加载
    r18 = _load_module("_av_r18", _SERVICES_DIR / "r18.py")
except Exception:  # pragma: no cover
    r18 = None  # type: ignore[assignment]


class AnalyzeError(RuntimeError):
    """可预期的用户错误（输入不存在、r18 库打不开等）→ CLI 以 exit 2 结束。"""


# ---------------------------------------------------------------------------
# 扩展名 / 跳过目录（与 app/config.py 保持一致；那边加载失败时用本地默认值）
# ---------------------------------------------------------------------------

_DEFAULT_VIDEO_EXTS = frozenset(
    {
        ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".ts", ".m2ts",
        ".rmvb", ".rm", ".flv", ".webm", ".iso", ".m4v", ".mpg", ".mpeg",
    }
)
_DEFAULT_SUBTITLE_EXTS = frozenset({".srt", ".ass", ".ssa", ".sub", ".vtt"})
_DEFAULT_SIDECAR_EXTS = frozenset({".nfo"})
_DEFAULT_SKIP_DIRS = frozenset(
    {
        ".@__thumb", "@eaDir", "#recycle", ".Trash", "System Volume Information",
        ".git", ".venv", "__pycache__", "node_modules",
    }
)


@functools.lru_cache(maxsize=1)
def _load_config():
    """按文件路径加载 app/config.py（纯标准库模块，不会拉起 Flask）。"""
    try:
        module = _load_module("_av_config", ROOT / "app" / "config.py")
        return getattr(module, "Config", None)
    except Exception:  # pragma: no cover - 缺 config 也要能用
        return None


@functools.lru_cache(maxsize=4)
def media_ext_sets(include_sidecars: bool = False) -> Dict[str, frozenset]:
    cfg = _load_config()
    video = frozenset(getattr(cfg, "VIDEO_EXTS", None) or _DEFAULT_VIDEO_EXTS)
    subtitles = frozenset(getattr(cfg, "SUBTITLE_EXTS", None) or _DEFAULT_SUBTITLE_EXTS)
    sidecars = frozenset(_DEFAULT_SIDECAR_EXTS)
    skip = frozenset(getattr(cfg, "SKIP_DIRS", None) or ()) | _DEFAULT_SKIP_DIRS
    allowed: set = set(video)
    if include_sidecars:
        allowed |= set(subtitles) | set(sidecars)
    return {"video": video, "subtitles": subtitles, "sidecars": sidecars,
            "allowed": frozenset(allowed), "skip_dirs": skip}


# ---------------------------------------------------------------------------
# 番号前缀 / 特殊厂牌
# ---------------------------------------------------------------------------

# 任务要求单列的 10 个特殊厂牌
SPECIAL_BRANDS: Tuple[str, ...] = (
    "FC2", "HEYZO", "SIRO", "TOKYO-HOT", "1PONDO",
    "CARIB", "PACO", "259LUXU", "300MIUM", "200GANA",
)

# 前缀 -> 厂牌标签（同一厂牌的带数字前缀/不带数字前缀写法都归到一起）
SPECIAL_BRAND_BY_PREFIX: Dict[str, str] = {
    "FC2-PPV": "FC2",
    "HEYZO": "HEYZO",
    "SIRO": "SIRO",
    "TOKYO-HOT": "TOKYO-HOT",
    "1PONDO": "1PONDO",
    "CARIB": "CARIB",
    "PACOPACOMAMA": "PACO",
    "259LUXU": "259LUXU",
    "LUXU": "259LUXU",
    "300MIUM": "300MIUM",
    "MIUM": "300MIUM",
    "200GANA": "200GANA",
    "GANA": "200GANA",
}

# 不在 10 个硬指标里、但同属素人/无码家族的厂牌（只计数，不单列进硬指标表）
OTHER_SPECIAL_BRAND_BY_PREFIX: Dict[str, str] = {
    "10MUSUME": "10MUSUME",
}

NORMAL_BRAND = "普通（DMM 系）"


def code_prefix(code: str) -> str:
    """取番号前缀（`FC2-PPV-123` -> `FC2-PPV`，`TOKYO-HOT-N1234` -> `TOKYO-HOT`）。"""
    parts = str(code).split("-")
    head = parts[0].upper()
    if len(parts) >= 2 and head in ("FC2", "TOKYO") and parts[1].upper() in ("PPV", "HOT"):
        return head + "-" + parts[1].upper()
    return head


def special_brand_of(code: str) -> Optional[str]:
    prefix = code_prefix(code)
    if prefix in SPECIAL_BRAND_BY_PREFIX:
        return SPECIAL_BRAND_BY_PREFIX[prefix]
    if prefix in OTHER_SPECIAL_BRAND_BY_PREFIX:
        return OTHER_SPECIAL_BRAND_BY_PREFIX[prefix]
    return None


# ---------------------------------------------------------------------------
# confidence 分桶 / 阈值
# ---------------------------------------------------------------------------

CONF_GE90 = "ge90"
CONF_70_89 = "70_89"
CONF_LT70 = "lt70"
CONF_ZERO = "zero"
CONF_BUCKETS: Tuple[str, ...] = (CONF_GE90, CONF_70_89, CONF_LT70, CONF_ZERO)
CONF_BUCKET_LABELS = {
    CONF_GE90: "≥90（可直接自动入库）",
    CONF_70_89: "70–89（可用，建议抽检）",
    CONF_LT70: "0<conf<70（待确认）",
    CONF_ZERO: "0 / 未解析",
}

# 结论阈值（写死并写进报告，避免每次口径漂移）
PARSE_RATE_GOOD = 0.95
PARSE_RATE_OK = 0.85
R18_COVERAGE_GOOD = 0.90
R18_COVERAGE_OK = 0.75
SPECIAL_SHARE_PIPE = 0.10   # ≥10% 必须独立管道
SPECIAL_SHARE_LIGHT = 0.03  # 3–10% 建议轻量管道

DEFAULT_R18_CHUNK = 400
MAX_R18_CHUNK = 400  # r18.lookup_by_dvd_ids 单批上限
DEFAULT_TOP_PREFIXES = 30
DEFAULT_UNRESOLVED_LIMIT = 50


def confidence_bucket(match) -> str:
    if match.code is None:
        return CONF_ZERO
    conf = int(match.confidence)
    if conf >= 90:
        return CONF_GE90
    if conf >= 70:
        return CONF_70_89
    return CONF_LT70


def _rate(numerator: int, denominator: int) -> float:
    return float(numerator) / denominator if denominator else 0.0


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else "%.1f%%" % (value * 100.0)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _iso_mtime(path: Path) -> Optional[str]:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 输入收集
# ---------------------------------------------------------------------------


def read_paths_file(path: str) -> List[str]:
    """读一行一个路径；空行忽略。`-` 表示 stdin。"""
    if path == "-":
        lines = sys.stdin.read().splitlines()
    else:
        file_path = Path(path)
        if not file_path.is_file():
            raise AnalyzeError("路径清单不存在：%s" % file_path)
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return [line.strip() for line in lines if line.strip()]


def collect_paths_from_dir(
    root: str,
    *,
    limit: int = 0,
    include_sidecars: bool = False,
    exts: Optional[Dict[str, frozenset]] = None,
) -> Dict[str, Any]:
    """os.walk 收集媒体文件名（不读内容）。返回 paths + 统计信息。"""
    root_path = Path(root)
    if not root_path.is_dir():
        raise AnalyzeError("目录不存在：%s" % root_path)
    ext_sets = exts or media_ext_sets(include_sidecars=include_sidecars)
    allowed = ext_sets["allowed"]
    skip_dirs = ext_sets["skip_dirs"]

    paths: List[str] = []
    scanned = 0
    skipped_ext: Counter = Counter()
    skipped_dirs = 0
    truncated = False

    for dirpath, dirnames, filenames in os.walk(root_path):
        kept_dirs = []
        for name in sorted(dirnames):
            if name in skip_dirs or name.startswith("._"):
                skipped_dirs += 1
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs  # 原地剪枝，避免走进 .@__thumb / @eaDir

        for name in sorted(filenames):
            scanned += 1
            if name.startswith("._"):  # macOS AppleDouble 垃圾文件
                skipped_ext["<appledouble>"] += 1
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in allowed:
                skipped_ext[ext or "<none>"] += 1
                continue
            paths.append(os.path.join(dirpath, name))
            if limit and len(paths) >= limit:
                truncated = True
                break
        if truncated:
            break

    paths.sort()
    return {
        "paths": paths,
        "scanned_files": scanned,
        "eligible_files": len(paths),
        "skipped_ext": dict(skipped_ext.most_common()),
        "skipped_dirs": skipped_dirs,
        "limit": limit or None,
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# r18 dump 覆盖统计（只读、分批 ≤400）
# ---------------------------------------------------------------------------


def _r18_meta(conn) -> Dict[str, str]:
    """读 staging 的 meta 表（不存在就返回空 dict，不影响覆盖率主流程）。"""
    try:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
    except Exception:
        return {}
    wanted = {"dump_file", "dump_date", "imported_at", "imported_rows", "dump_size_bytes"}
    return {
        str(row[0]): str(row[1])
        for row in rows
        if str(row[0]) in wanted
    }


def _lookup_in_batches(conn, codes: Sequence[str], *, chunk_size: int = DEFAULT_R18_CHUNK,
                       lookup=None) -> Dict[str, Any]:
    """把番号按 ≤chunk_size 分批查 r18 staging 库（去重、保留首次出现顺序）。"""
    chunk = max(1, min(int(chunk_size), MAX_R18_CHUNK))
    func = lookup
    if func is None:
        if r18 is None:  # pragma: no cover - r18 是纯标准库，几乎不会失败
            raise AnalyzeError("app/services/r18.py 加载失败，无法做 r18 覆盖统计")
        func = r18.lookup_by_dvd_ids
    unique: List[str] = list(dict.fromkeys(c for c in codes if c))
    found: Dict[str, Any] = {}
    for start in range(0, len(unique), chunk):
        batch = unique[start : start + chunk]
        found.update(func(conn, batch, chunk_size=chunk))
    return found


def collect_r18_coverage(
    records: Sequence[Tuple[str, Any]],
    db_path: str,
    *,
    chunk_size: int = DEFAULT_R18_CHUNK,
    missed_prefix_limit: int = 15,
    missed_sample_limit: int = 20,
) -> Dict[str, Any]:
    """对可解析番号做 r18 staging 覆盖统计（只读打开、分批查询）。"""
    if r18 is None:  # pragma: no cover
        raise AnalyzeError("app/services/r18.py 不可用，无法统计 r18 覆盖率")
    path = Path(db_path)
    if not path.is_file():
        raise AnalyzeError("r18 staging 库不存在：%s（先用 scripts/import_dump.py 导入）" % path)

    code_file_counts: Counter = Counter()
    codes: List[str] = []
    for _path, match in records:
        if match.code:
            code_file_counts[match.code] += 1
            if code_file_counts[match.code] == 1:
                codes.append(match.code)

    chunk = max(1, min(int(chunk_size), MAX_R18_CHUNK))
    try:
        conn = r18.open_dump_db(path)
    except Exception as exc:
        raise AnalyzeError("r18 staging 库打开失败：%s（%s）" % (path, exc))
    try:
        meta = _r18_meta(conn)
        found = _lookup_in_batches(conn, codes, chunk_size=chunk)
    finally:
        conn.close()

    matched = set(found)
    matched_files = sum(count for code, count in code_file_counts.items() if code in matched)
    parseable_files = sum(code_file_counts.values())

    # 按品牌统计（唯一番号）
    brand_total: Counter = Counter()
    brand_matched: Counter = Counter()
    for code in codes:
        brand = special_brand_of(code) or NORMAL_BRAND
        brand_total[brand] += 1
        if code in matched:
            brand_matched[brand] += 1
    by_brand = [
        {
            "brand": brand,
            "unique_codes": brand_total[brand],
            "matched": brand_matched[brand],
            "coverage": _rate(brand_matched[brand], brand_total[brand]),
        }
        for brand in sorted(brand_total, key=lambda b: (-brand_total[b], b))
    ]

    missed = [code for code in codes if code not in matched]
    missed_prefix_counter = Counter(code_prefix(code) for code in missed)

    return {
        "db": str(path),
        "db_size_bytes": path.stat().st_size,
        "db_mtime": _iso_mtime(path),
        "db_meta": meta,
        "chunk_size": chunk,
        "unique_codes": len(codes),
        "matched_codes": len(matched),
        "coverage": _rate(len(matched), len(codes)),
        "parseable_files": parseable_files,
        "matched_files": matched_files,
        "file_coverage": _rate(matched_files, parseable_files),
        "multi_row_codes": sum(1 for rows in found.values() if len(rows) > 1),
        "rows_total": sum(len(rows) for rows in found.values()),
        "by_brand": by_brand,
        "missed_prefixes": [
            {"prefix": prefix, "count": count}
            for prefix, count in missed_prefix_counter.most_common(missed_prefix_limit)
        ],
        "missed_samples": missed[:missed_sample_limit],
    }


# ---------------------------------------------------------------------------
# 核心分析
# ---------------------------------------------------------------------------


def _ext_of(path: str) -> str:
    return os.path.splitext(path)[1].lower() or "<none>"


def analyze_paths(
    paths: Iterable[str],
    *,
    source: str = "<memory>",
    input_info: Optional[Dict[str, Any]] = None,
    r18db: Optional[str] = None,
    r18_chunk: int = DEFAULT_R18_CHUNK,
    top_prefixes: int = DEFAULT_TOP_PREFIXES,
    unresolved_limit: int = DEFAULT_UNRESOLVED_LIMIT,
) -> Dict[str, Any]:
    """解析 + 统计，返回可直接 JSON 序列化的报告 dict。"""
    started = time.perf_counter()
    records: List[Tuple[str, Any]] = []
    for raw in paths:
        path_text = str(raw)
        records.append((path_text, code_parser.extract_code(path_text)))

    total = len(records)
    conf_counts: Counter = Counter()
    source_counts: Counter = Counter()
    prefix_counts: Counter = Counter()
    brand_counts: Counter = Counter({brand: 0 for brand in SPECIAL_BRANDS})
    other_brand_counts: Counter = Counter()
    flag_counts: Counter = Counter({"chinese_sub": 0, "uncensored": 0, "hi_res": 0, "part": 0})
    ext_stats: Dict[str, Dict[str, int]] = {}
    unresolved: List[Dict[str, str]] = []
    codes_seen: set = set()
    parseable = 0

    for path_text, match in records:
        ext = _ext_of(path_text)
        bucket = confidence_bucket(match)
        conf_counts[bucket] += 1
        source_counts[match.source] += 1
        stats = ext_stats.setdefault(ext, {"count": 0, "parseable": 0})
        stats["count"] += 1

        for flag in match.flags:
            if flag in flag_counts:
                flag_counts[flag] += 1

        if match.code:
            parseable += 1
            stats["parseable"] += 1
            codes_seen.add(match.code)
            prefix_counts[code_prefix(match.code)] += 1
            brand = special_brand_of(match.code)
            if brand is None:
                pass
            elif brand in brand_counts:
                brand_counts[brand] += 1
            else:
                other_brand_counts[brand] += 1
        elif len(unresolved) < unresolved_limit:
            unresolved.append({"path": path_text, "ext": ext,
                               "reason": "no-code" if match.source == "none" else match.source})

    special_total = sum(brand_counts.values()) + sum(other_brand_counts.values())

    by_ext = [
        {
            "ext": ext,
            "count": stats["count"],
            "parseable": stats["parseable"],
            "parseable_rate": _rate(stats["parseable"], stats["count"]),
        }
        for ext, stats in sorted(ext_stats.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
    ][:15]

    info: Dict[str, Any] = {
        "source": source,
        "total": total,
        "scanned_files": (input_info or {}).get("scanned_files", total),
        "skipped_ext": (input_info or {}).get("skipped_ext", {}),
        "skipped_dirs": (input_info or {}).get("skipped_dirs", 0),
        "limit": (input_info or {}).get("limit"),
        "truncated": (input_info or {}).get("truncated", False),
    }

    report: Dict[str, Any] = {
        "generated_at": _iso_now(),
        "input": info,
        "summary": {
            "total": total,
            "parseable": parseable,
            "parseable_rate": _rate(parseable, total),
            "unique_codes": len(codes_seen),
            "confidence": {bucket: conf_counts[bucket] for bucket in CONF_BUCKETS},
            "source_counts": {
                key: source_counts[key] for key in ("basename", "parent", "none")
            },
            "special_brand_total": special_total,
            "special_brand_rate": _rate(special_total, total),
        },
        "prefixes": [
            {"prefix": prefix, "count": count}
            for prefix, count in sorted(prefix_counts.items(), key=lambda kv: (-kv[1], kv[0]))[
                :top_prefixes
            ]
        ],
        "special_brands": {brand: brand_counts[brand] for brand in SPECIAL_BRANDS},
        "other_special_brands": dict(sorted(other_brand_counts.items())),
        "flags": dict(flag_counts),
        "by_ext": by_ext,
        "unresolved": unresolved,
        "r18": None,
    }

    if r18db:
        report["r18"] = collect_r18_coverage(records, r18db, chunk_size=r18_chunk)

    report["conclusion"] = build_conclusion(report)
    report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return report


def build_conclusion(report: Dict[str, Any]) -> Dict[str, Any]:
    """把三个决定性问题提炼成 verdict + 建议列表。"""
    summary = report["summary"]
    total = summary["total"]
    parse_rate = summary["parseable_rate"]
    r18_section = report.get("r18")
    r18_coverage = r18_section["coverage"] if r18_section else None
    special_rate = summary["special_brand_rate"]

    if parse_rate >= PARSE_RATE_GOOD:
        parse_verdict = "达标（≥95%，可进入 M1）"
    elif parse_rate >= PARSE_RATE_OK:
        parse_verdict = "可用（85–95%）：进 M1，但要抽检 + alias 学习"
    else:
        parse_verdict = "不达标（<85%）：先补解析器盲区，别急着进 M1"

    if r18_coverage is None:
        r18_verdict = "未测（没给 --r18db）"
    elif r18_coverage >= R18_COVERAGE_GOOD:
        r18_verdict = "充足（≥90%）：r18 dump 可当元数据底座"
    elif r18_coverage >= R18_COVERAGE_OK:
        r18_verdict = "够用（75–90%）：缺的走 JavDB 补缺口"
    else:
        r18_verdict = "不足（<75%）：元数据底座不够，需要补源/独立管道"

    if special_rate >= SPECIAL_SHARE_PIPE:
        special_verdict = "必须建独立管道（≥10%）"
    elif special_rate >= SPECIAL_SHARE_LIGHT:
        special_verdict = "建议轻量独立管道（3–10%）"
    else:
        special_verdict = "可不单独建（<3%，content_id 兜底即可）"

    recommendations: List[str] = []
    if total == 0:
        recommendations.append("样本为空：先确认输入（--paths-file / --dir / --limit）。")
    if parse_rate < PARSE_RATE_GOOD:
        recommendations.append(
            "可解析率 %s 未达 %d%% 目标：先看第 8 节「未解析样本」和「按扩展名」表，"
            "把高频盲区补进 code_parser 的规则/alias 表，再重跑本工具。"
            % (_pct(parse_rate), int(PARSE_RATE_GOOD * 100))
        )
    if r18_section is None:
        recommendations.append(
            "还没测 r18 覆盖率：M0 决策前跑一次 `--r18db data/r18dump.db`。"
        )
    else:
        if r18_coverage is not None and r18_coverage < R18_COVERAGE_GOOD:
            top_missed = "、".join(
                "%s(%d)" % (item["prefix"], item["count"])
                for item in r18_section["missed_prefixes"][:5]
            )
            recommendations.append(
                "r18 覆盖率 %s 低于 %d%%：优先看未命中前缀（%s），"
                "决定 JavDB 补缺口的优先级；FC2 不在 r18 dump 里是已知缺口。"
                % (_pct(r18_coverage), int(R18_COVERAGE_GOOD * 100), top_missed or "无")
            )
        fc2 = report["special_brands"].get("FC2", 0)
        if fc2:
            recommendations.append(
                "FC2 占 %d 个文件（%s）：r18 dump 不覆盖 FC2，必须单独管道"
                "（javdb f=fc2 / FC2 内容站）。" % (fc2, _pct(_rate(fc2, total)))
            )
    if special_rate >= SPECIAL_SHARE_PIPE:
        recommendations.append(
            "素人/特殊厂牌占比 %s ≥ %d%%：必须建独立刮削管道，否则元数据缺口会拖垮片库质量。"
            % (_pct(special_rate), int(SPECIAL_SHARE_PIPE * 100))
        )
    elif special_rate >= SPECIAL_SHARE_LIGHT:
        recommendations.append(
            "素人/特殊厂牌占比 %s（3–10%%）：建议做轻量管道，或至少按 content_id 兜底。"
            % _pct(special_rate)
        )
    else:
        recommendations.append(
            "素人/特殊厂牌占比 %s <3%%：可以不单独建管道，按 content_id 兜底即可。"
            % _pct(special_rate)
        )
    if not recommendations:
        recommendations.append("三项指标全部达标，可以进入 M1（服务骨架）。")

    return {
        "parseable_rate": parse_rate,
        "parse_verdict": parse_verdict,
        "r18_coverage": r18_coverage,
        "r18_verdict": r18_verdict,
        "special_brand_rate": special_rate,
        "special_verdict": special_verdict,
        "recommendations": recommendations,
    }


# ---------------------------------------------------------------------------
# Markdown 渲染
# ---------------------------------------------------------------------------


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def render_markdown(report: Dict[str, Any]) -> str:
    """把报告 dict 渲染成给人看的 Markdown。"""
    summary = report["summary"]
    conclusion = report["conclusion"]
    info = report["input"]
    out: List[str] = []
    add = out.append

    add("# AV 片库文件名命中率报告（M0）")
    add("")
    add("> 输入：`%s` ｜ 收集 %d 个文件（扫描 %d 个）%s"
        % (
            info["source"],
            info["total"],
            info.get("scanned_files", info["total"]),
            "，已按 --limit 截断" if info.get("truncated") else "",
        ))
    add("> 生成时间：%s ｜ 耗时 %.2fs ｜ 解析器：`app/services/code_parser.py`"
        % (report["generated_at"], report["elapsed_seconds"]))
    add("")

    # --- 结论 ---
    add("## 0. 结论（三个决定性问题）")
    add("")
    r18 = report.get("r18")
    r18_cov = conclusion["r18_coverage"]
    add(_table(
        ["指标", "数值", "判读"],
        [
            ["可解析率（code 非空）",
             "**%s**（%d/%d）" % (_pct(summary["parseable_rate"]), summary["parseable"], summary["total"]),
             conclusion["parse_verdict"]],
            ["r18 dump 覆盖率（唯一番号）",
             "**%s**（%d/%d）" % (_pct(r18_cov), r18["matched_codes"], r18["unique_codes"]) if r18
             else "**未测**",
             conclusion["r18_verdict"]],
            ["FC2/素人（特殊厂牌）占比",
             "**%s**（%d/%d 文件）" % (_pct(conclusion["special_brand_rate"]),
                                     summary["special_brand_total"], summary["total"]),
             conclusion["special_verdict"]],
        ],
    ))
    add("")
    add("**建议**：")
    add("")
    for index, rec in enumerate(conclusion["recommendations"], 1):
        add("%d. %s" % (index, rec))
    add("")

    # --- 概览 ---
    add("## 1. 概览")
    add("")
    add(_table(
        ["项", "数值"],
        [
            ["总数", summary["total"]],
            ["可解析（code 非空）", summary["parseable"]],
            ["可解析率", _pct(summary["parseable_rate"])],
            ["唯一番号数", summary["unique_codes"]],
            ["特殊厂牌文件数", summary["special_brand_total"]],
            ["特殊厂牌占比", _pct(summary["special_brand_rate"])],
        ],
    ))
    add("")

    # --- confidence 分布 ---
    add("## 2. confidence 分布")
    add("")
    total = summary["total"] or 1
    rows = []
    for bucket in CONF_BUCKETS:
        count = summary["confidence"][bucket]
        rows.append([CONF_BUCKET_LABELS[bucket], count, _pct(_rate(count, total))])
    add(_table(["分档", "文件数", "占比"], rows))
    add("")

    # --- 来源 ---
    add("## 3. 解析来源")
    add("")
    add(_table(
        ["来源", "文件数", "占比"],
        [
            [key, value, _pct(_rate(value, total))]
            for key, value in summary["source_counts"].items()
        ],
    ))
    add("")

    # --- 前缀 ---
    add("## 4. 番号前缀 Top %d" % len(report["prefixes"]))
    add("")
    if report["prefixes"]:
        add(_table(
            ["前缀", "文件数"],
            [[item["prefix"], item["count"]] for item in report["prefixes"]],
        ))
    else:
        add("_（没有可解析番号）_")
    add("")

    # --- 特殊厂牌 ---
    add("## 5. 特殊厂牌计数")
    add("")
    brand_rows = [[brand, count] for brand, count in report["special_brands"].items()]
    for brand, count in report["other_special_brands"].items():
        brand_rows.append(["%s（其他素人）" % brand, count])
    brand_rows.append(["**合计**", summary["special_brand_total"]])
    add(_table(["厂牌", "文件数"], brand_rows))
    add("")

    # --- flags ---
    add("## 6. flags 统计")
    add("")
    add(_table(
        ["flag", "文件数", "占比"],
        [[flag, count, _pct(_rate(count, total))] for flag, count in report["flags"].items()],
    ))
    add("")

    # --- 按扩展名 ---
    add("## 7. 按扩展名（Top 15）")
    add("")
    if report["by_ext"]:
        add(_table(
            ["扩展名", "文件数", "可解析", "可解析率"],
            [
                [item["ext"], item["count"], item["parseable"], _pct(item["parseable_rate"])]
                for item in report["by_ext"]
            ],
        ))
    else:
        add("_（没有文件）_")
    add("")

    # --- r18 ---
    add("## 8. r18 dump 覆盖率")
    add("")
    if r18 is None:
        add("_未提供 `--r18db`，本节跳过。_")
    else:
        meta = r18.get("db_meta") or {}
        add("> 库：`%s`（%.1f MB，mtime %s）"
            % (r18["db"], r18["db_size_bytes"] / 1024.0 / 1024.0, r18.get("db_mtime") or "?"))
        if meta:
            add("> dump：`%s` ｜ 导入时间：%s ｜ 行数：%s"
                % (meta.get("dump_file", "?"), meta.get("imported_at", "?"),
                   meta.get("imported_rows", "?")))
        add("")
        add(_table(
            ["口径", "命中", "总数", "覆盖率"],
            [
                ["唯一番号", r18["matched_codes"], r18["unique_codes"], _pct(r18["coverage"])],
                ["文件加权", r18["matched_files"], r18["parseable_files"], _pct(r18["file_coverage"])],
            ],
        ))
        add("")
        if r18.get("multi_row_codes"):
            add("> 注意：%d 个番号在 dump 里命中多行（补零/多版本），取第一条不影响统计，"
                "但归组时需去重。" % r18["multi_row_codes"])
            add("")
        add("### 8.1 按品牌")
        add("")
        add(_table(
            ["品牌", "唯一番号", "命中", "覆盖率"],
            [
                [item["brand"], item["unique_codes"], item["matched"], _pct(item["coverage"])]
                for item in r18["by_brand"]
            ],
        ))
        add("")
        add("### 8.2 未命中的前缀 Top %d" % len(r18["missed_prefixes"]))
        add("")
        if r18["missed_prefixes"]:
            add(_table(
                ["前缀", "未命中番号数"],
                [[item["prefix"], item["count"]] for item in r18["missed_prefixes"]],
            ))
        else:
            add("_（全部命中）_")
        add("")
        if r18.get("missed_samples"):
            add("未命中番号样例：%s" % "、".join("`%s`" % code for code in r18["missed_samples"]))
            add("")

    # --- 未解析样本 ---
    add("## 9. 未解析样本（前 %d 条，按出现顺序）" % len(report["unresolved"]))
    add("")
    if report["unresolved"]:
        add("```text")
        for item in report["unresolved"]:
            add(item["path"])
        add("```")
    else:
        add("_（没有未解析样本）_")
    add("")

    # --- 口径 ---
    add("## 10. 口径与阈值")
    add("")
    add("- 可解析率 = code 非空的文件数 / 收集到的文件数；`--dir` 默认只收视频扩展名"
        "（字幕/nfo 是附件，加 `--include-sidecars` 才计入）。")
    add("- r18 覆盖率主指标按**唯一番号**算（一个番号多个文件不重复计），另附文件加权。")
    add("- 特殊厂牌按归一化番号前缀识别：%s。" % "、".join(SPECIAL_BRANDS))
    add("- 阈值（写死，避免口径漂移）：可解析率 ≥%d%% 达标 / ≥%d%% 可用；"
        "r18 覆盖 ≥%d%% 充足 / ≥%d%% 够用；特殊厂牌 ≥%d%% 必须独立管道。"
        % (
            int(PARSE_RATE_GOOD * 100), int(PARSE_RATE_OK * 100),
            int(R18_COVERAGE_GOOD * 100), int(R18_COVERAGE_OK * 100),
            int(SPECIAL_SHARE_PIPE * 100),
        ))
    add("- confidence 建议阈值：≥90 自动入库；70–89 自动但抽检；<70 进待确认。")
    add("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _write_text(target: str, text: str) -> None:
    if target == "-":
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _resolve_json_target(args) -> Optional[str]:
    if args.json is None:
        return None
    if args.json != "@auto":
        return args.json
    if args.out and args.out != "-":
        return str(Path(args.out).with_suffix(".json"))
    return "-"


def _gather(args) -> Tuple[List[str], Dict[str, Any]]:
    """按 --paths-file / --dir 收集输入，返回 (paths, input_info)。"""
    if args.paths_file:
        lines = read_paths_file(args.paths_file)
        truncated = bool(args.limit) and len(lines) > args.limit
        if args.limit:
            lines = lines[: args.limit]
        info = {
            "source": "paths-file:stdin" if args.paths_file == "-" else "paths-file:%s" % args.paths_file,
            "scanned_files": len(lines),
            "skipped_ext": {},
            "skipped_dirs": 0,
            "limit": args.limit or None,
            "truncated": truncated,
        }
        return lines, info

    collected = collect_paths_from_dir(
        args.dir, limit=args.limit, include_sidecars=args.include_sidecars
    )
    info = {
        "source": "dir:%s" % args.dir,
        "scanned_files": collected["scanned_files"],
        "skipped_ext": collected["skipped_ext"],
        "skipped_dirs": collected["skipped_dirs"],
        "limit": collected["limit"],
        "truncated": collected["truncated"],
    }
    return collected["paths"], info


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="analyze_names.py",
        description="真实文件名命中率分析（M0）：可解析率 / r18 覆盖率 / FC2+素人占比",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  %(prog)s --paths-file names.txt --out /tmp/m0.md --json\n"
            "  %(prog)s --dir /mnt/user/av --limit 1000 --r18db data/r18dump.db --out /tmp/m0.md\n"
            "  find /mnt/user/av -type f | head -500 | %(prog)s --paths-file - --json | jq .summary\n"
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--paths-file", metavar="PATH",
                       help="一行一个完整路径或文件名；`-` 表示从 stdin 读")
    group.add_argument("--dir", metavar="PATH", help="递归扫描目录（只收媒体扩展名，不读内容）")
    parser.add_argument("--out", default="-", metavar="PATH",
                        help="Markdown 报告输出路径（缺省 stdout；`-` 表示 stdout）")
    parser.add_argument("--json", nargs="?", const="@auto", default=None, metavar="PATH",
                        help="同时输出 JSON（缺省：--out 是文件时写同名 .json，否则占 stdout）")
    parser.add_argument("--r18db", default=None, metavar="PATH",
                        help="r18 staging 库（只读；推荐 data/r18dump.db）")
    parser.add_argument("--r18-chunk", type=int, default=DEFAULT_R18_CHUNK, metavar="N",
                        help="r18 查询批大小（上限 %d，默认 %d）" % (MAX_R18_CHUNK, DEFAULT_R18_CHUNK))
    parser.add_argument("--limit", type=int, default=0, metavar="N",
                        help="最多分析 N 个文件（0=全部；M0 抽样时用，如 500/1000）")
    parser.add_argument("--include-sidecars", action="store_true",
                        help="--dir 时把字幕/nfo 也算进分母（默认只算视频）")
    parser.add_argument("--top-prefixes", type=int, default=DEFAULT_TOP_PREFIXES, metavar="N",
                        help="前缀 Top N（默认 %d）" % DEFAULT_TOP_PREFIXES)
    parser.add_argument("--unresolved-limit", type=int, default=DEFAULT_UNRESOLVED_LIMIT, metavar="N",
                        help="报告里贴多少条未解析样本（默认 %d）" % DEFAULT_UNRESOLVED_LIMIT)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.limit < 0:
        parser.error("--limit 不能为负数")
    if args.r18_chunk < 1:
        parser.error("--r18-chunk 必须 >= 1")

    try:
        paths, info = _gather(args)
        report = analyze_paths(
            paths,
            source=info["source"],
            input_info=info,
            r18db=args.r18db,
            r18_chunk=args.r18_chunk,
            top_prefixes=max(1, args.top_prefixes),
            unresolved_limit=max(0, args.unresolved_limit),
        )
    except AnalyzeError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 2

    markdown = render_markdown(report)
    json_text = json.dumps(report, ensure_ascii=False, indent=2)

    md_target = args.out or "-"
    json_target = _resolve_json_target(args)

    if json_target == "-" and md_target == "-":
        # JSON 占 stdout，Markdown 只能去文件（否则管道被污染）
        sys.stderr.write(
            "提示：JSON 已输出到 stdout，Markdown 省略；"
            "要同时保留人看的报告请加 --out report.md\n"
        )
        sys.stdout.write(json_text + "\n")
        return 0

    _write_text(md_target, markdown)
    if json_target is not None:
        _write_text(json_target, json_text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
