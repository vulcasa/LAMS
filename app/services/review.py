"""人工修正 / 待确认工作流。

解决自动管道处理不了的情况：

* **FC2 / HEYZO / LUXU / SIRO / CARIB / 素人**：番号形态不在 r18 dump 的
  ``dvd_id`` 里，自动物化必然未命中，需要人工确认或挂到已有 Title；
* **低置信度**（``confidence < CODE_AUTO_MIN``）：扫描器不敢自动入库的文件；
* **多番号合集**：一个文件里塞了多个番号，解析器只能给低分（见 code_parser）；
* **命名完全无法识别**（``code_guess is None``）：只能人工给番号。

本模块提供三件事（页面/API 见 ``app/routes/review.py``）：

A. :func:`pending_items` —— 待确认分组列表（未关联文件按 ``code_guess`` 分组）；
B. :func:`assign_code` —— 人工指派番号：写 ``CodeAlias`` 学习映射、批量改
   ``LibraryFile.code_guess`` / ``confidence=95``，并立即物化；
C. :func:`reassign_files` —— 把选中文件合并挂到已有 Title（没有就尝试物化建），
   同时把原 code_guess 记进 ``CodeAlias``。

「待确认」的判定
----------------
未关联（``title_id IS NULL``）的正文文件里，其实有两个来源：

1. **低置信度**：``confidence < CODE_AUTO_MIN``，自动物化从没尝试过；
2. **自动未命中**：``confidence >= CODE_AUTO_MIN`` 但物化跑完仍没挂上 Title
   （dump 里没有这个番号，典型如 FC2/素人/新片）。

两者都要进兜底入口，所以 ``pending_items`` 默认返回**全部**未关联文件；参数
``max_confidence`` 只用来收窄第 1 类（例如只看 ``<70`` 的），第 2 类不受它限制。
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Iterable, Sequence

from flask import current_app

from app.db import db
from app.models import CodeAlias, LibraryFile, LibraryVolume, Title
from app.services.r18 import canonical_dvd_id, dvd_id_key
from app.services.collections import collection_of
from app.services.source_classify import detect_source

logger = logging.getLogger(__name__)

__all__ = [
    "PendingGroup",
    "ReviewStats",
    "pending_items",
    "pending_summary",
    "pending_volumes",
    "assign_code",
    "reassign_files",
]

#: 人工指派后写入的置信度（足够高，后续自动流程不会再改动）
MANUAL_CONFIDENCE = 95
#: 每组样本路径条数
SAMPLE_LIMIT = 5
#: 单组返回的 file_ids 上限（防止超大分组把 JSON 撑爆；页面按组指派用）
FILE_IDS_LIMIT = 1000


# ---------------------------------------------------------------------------
# 待确认分组
# ---------------------------------------------------------------------------
@dataclass
class PendingGroup:
    """一组待确认文件（同一个原始 code_guess）。"""

    code_guess: str | None
    files: int
    confidence_min: int
    confidence_max: int
    samples: list[str] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)
    file_ids: list[int] = field(default_factory=list)
    #: 命中的 CodeAlias 目标番号（如果有）：提示用户“上次已指派过”
    alias_code: str | None = None
    #: 无番号分组的来源类别（CosplayJAV / FC2 / 欧美厂牌…），有番号的组为 None
    source_key: str | None = None
    source_label: str | None = None
    #: 无番号分组的父目录（一部作品一个目录；不同目录不合并）
    folder: str | None = None
    #: 所属顶层合集（如 蚊香社无码流出），无番号组才有
    collection: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReviewStats:
    """待确认工作流的总览数字（页面顶部展示）。"""

    pending_files: int = 0
    pending_groups: int = 0
    low_confidence_files: int = 0
    auto_unmatched_files: int = 0
    aliases: int = 0


def _conf(value) -> int:
    return int(value) if value is not None else 0


def _within_window(confidence, *, min_confidence: int, max_confidence: int,
                   auto_min: int, strict: bool = False) -> bool:
    """待确认文件的置信度窗口。

    ``strict=True`` 时是纯区间过滤，用于页面上的“置信度”筛选；
    默认模式保留自动未命中项（confidence >= auto_min），避免人工兜底入口丢项。
    """
    conf = _conf(confidence)
    if conf < min_confidence:
        return False
    if strict:
        return conf <= max_confidence
    return conf <= max_confidence or conf >= auto_min


def _volume_label(volume: LibraryVolume | None) -> str:
    if volume is None:
        return "（未知介质）"
    if volume.kind == "tape":
        return f"磁带 {volume.ref}"
    label = (volume.name or volume.ref or "").strip()
    if label.upper().startswith("NAS"):
        return label
    return f"NAS {label}"


def pending_items(app, *, limit: int = 200, offset: int = 0,
                  min_confidence: int = 0,
                  max_confidence: int = 89,
                  confidence_strict: bool = False,
                  volume_id: int | None = None) -> list[PendingGroup]:
    """待确认列表：未关联文件按原始 ``code_guess`` 分组。

    * 只取 ``deleted_at IS NULL``、``is_sidecar=False``、``title_id IS NULL`` 的文件；
    * ``code_guess is None``（含空串）单独一组，排在最后；
    * 有 code_guess 的组在前，按文件数降序（同数量按 code_guess 升序稳定排序）；
    * 每组返回文件数、置信度范围、前 5 条 rel_path 样本、所在卷、file_ids。
    """
    auto_min = int(app.config.get("CODE_AUTO_MIN", 90))
    limit = max(0, int(limit))

    rows = (
        db.session.query(LibraryFile, LibraryVolume)
        .outerjoin(LibraryVolume, LibraryVolume.id == LibraryFile.volume_id)
        .filter(
            LibraryFile.deleted_at.is_(None),
            LibraryFile.is_sidecar.is_(False),
            LibraryFile.title_id.is_(None),
        )
        .order_by(LibraryFile.id)
        .all()
    )
    if volume_id is not None:
        rows = [(f, v) for f, v in rows if (v.id if v is not None else None) == volume_id]

    aliases = {row.raw_key: row.code for row in CodeAlias.query.all()}
    grouped: dict[str | None, dict] = {}
    for library_file, volume in rows:
        if not _within_window(
            library_file.confidence,
            min_confidence=min_confidence,
            max_confidence=max_confidence,
            auto_min=auto_min,
            strict=confidence_strict,
        ):
            continue
        code = (library_file.code_guess or "").strip() or None
        source_key, source_label = detect_source(library_file.rel_path)
        folder = None
        if code is None:
            folder = os.path.dirname(library_file.rel_path or "")
            group_key = f"__source__:{source_key}:{folder}"
        else:
            group_key = code
        bucket = grouped.setdefault(group_key, {
            "code": code,
            "source_key": source_key,
            "source_label": source_label,
            "folder": folder,
            "collection": (collection_of(library_file.rel_path) if code is None else None),
            "confidence": [],
            "samples": [],
            "volumes": [],
            "file_ids": [],
        })
        bucket["confidence"].append(_conf(library_file.confidence))
        if len(bucket["samples"]) < SAMPLE_LIMIT:
            bucket["samples"].append(library_file.rel_path)
        label = _volume_label(volume)
        if label not in bucket["volumes"]:
            bucket["volumes"].append(label)
        if len(bucket["file_ids"]) < FILE_IDS_LIMIT:
            bucket["file_ids"].append(library_file.id)

    groups: list[PendingGroup] = [
        PendingGroup(
            code_guess=bucket["code"],
            files=len(bucket["confidence"]),
            confidence_min=min(bucket["confidence"]),
            confidence_max=max(bucket["confidence"]),
            samples=bucket["samples"],
            volumes=bucket["volumes"],
            file_ids=bucket["file_ids"],
            alias_code=aliases.get(bucket["code"]) if bucket["code"] else None,
            source_key=bucket["source_key"],
            source_label=bucket["source_label"],
            folder=bucket["folder"],
            collection=bucket.get("collection"),
        )
        for bucket in grouped.values()
    ]
    groups.sort(
        key=lambda g: (
            g.code_guess is None,
            -g.files,
            g.code_guess or f"{g.source_label or ''}:{g.folder or ''}",
        )
    )
    start = max(0, int(offset))
    return groups[start:start + max(1, int(limit))]


def pending_volumes(app) -> list[dict]:
    """列出有未关联文件的卷，供“所在卷”筛选使用。"""
    rows = (
        db.session.query(LibraryVolume)
        .join(LibraryFile, LibraryFile.volume_id == LibraryVolume.id)
        .filter(
            LibraryFile.deleted_at.is_(None),
            LibraryFile.is_sidecar.is_(False),
            LibraryFile.title_id.is_(None),
        )
        .all()
    )
    seen: dict[int, dict] = {}
    for volume in rows:
        if volume.id not in seen:
            seen[volume.id] = {"id": volume.id, "label": _volume_label(volume)}
    return sorted(seen.values(), key=lambda item: item["label"])


def pending_summary(app, *, min_confidence: int = 0,
                    max_confidence: int = 89,
                    confidence_strict: bool = False,
                    volume_id: int | None = None) -> ReviewStats:
    """待确认总览（不受 limit 影响）。"""
    auto_min = int(app.config.get("CODE_AUTO_MIN", 90))
    rows = (
        db.session.query(
            LibraryFile.confidence, LibraryFile.code_guess, LibraryFile.rel_path,
            LibraryFile.volume_id,
        )
        .filter(
            LibraryFile.deleted_at.is_(None),
            LibraryFile.is_sidecar.is_(False),
            LibraryFile.title_id.is_(None),
        )
        .all()
    )
    stats = ReviewStats()
    groups: set[tuple[str, str]] = set()
    for confidence, code_guess, rel_path, row_volume_id in rows:
        if volume_id is not None and row_volume_id != volume_id:
            continue
        if not _within_window(confidence, min_confidence=min_confidence,
                              max_confidence=max_confidence, auto_min=auto_min,
                              strict=confidence_strict):
            continue
        stats.pending_files += 1
        code = (code_guess or "").strip()
        if code:
            groups.add(("code", code))
        else:
            parent = os.path.dirname(rel_path or "")
            groups.add(("source", f"{detect_source(rel_path)[0]}:{parent}"))
        if _conf(confidence) < auto_min:
            stats.low_confidence_files += 1
        else:
            stats.auto_unmatched_files += 1
    stats.pending_groups = len(groups)
    stats.aliases = db.session.query(CodeAlias).count()
    return stats


# ---------------------------------------------------------------------------
# Title / alias 小工具
# ---------------------------------------------------------------------------
def _find_title(code: str | None) -> Title | None:
    """按番号找已有 Title（先精确/大写/归一化，最后按 dvd_id_key 兜底扫描）。"""
    text = (code or "").strip()
    if not text:
        return None
    key = dvd_id_key(text)
    candidates = list(dict.fromkeys(
        value for value in (text, text.upper(), canonical_dvd_id(text)) if value
    ))
    rows = Title.query.filter(Title.dvd_id.in_(candidates)).all() if candidates else []
    for title in rows:
        if dvd_id_key(title.dvd_id) == key:
            return title
    if rows:
        return rows[0]
    for title in Title.query.all():
        if dvd_id_key(title.dvd_id) == key:
            return title
    return None


def _resolve_target_code(code: str) -> str:
    """把人工输入的目标番号规范成主库 Title.dvd_id 形态。"""
    text = (code or "").strip()
    if not text:
        raise ValueError("目标番号不能为空")
    title = _find_title(text)
    if title is not None:
        return title.dvd_id
    return canonical_dvd_id(text) or text.upper()


def _upsert_alias(raw_key: str, code: str) -> bool:
    """写/更新 CodeAlias；返回是否新建。"""
    alias = CodeAlias.query.filter_by(raw_key=raw_key).first()
    if alias is None:
        db.session.add(CodeAlias(raw_key=raw_key, code=code))
        return True
    if alias.code != code:
        alias.code = code
    return False


def _run_materialize(app, code: str) -> dict:
    """延迟 import materialize 并立刻物化单个番号（同步、数据量小）。"""
    try:
        from app.services.materialize import materialize

        stats = materialize(app, codes=[code], min_confidence=0)
    except Exception as exc:  # noqa: BLE001 - 人工操作不能因为物化失败而 500
        logger.exception("review: 物化 %s 失败", code)
        return {"error": f"{type(exc).__name__}: {exc}"}
    return asdict(stats)


def _select_files(raw_key: str | None,
                  file_ids: Sequence[int] | None) -> list[LibraryFile]:
    """待指派文件：优先按 file_ids；否则按 code_guess == raw_key。

    只动**还没挂 Title**的文件（已挂的属于 ``reassign_files`` 的职责），
    避免出现「code_guess 改了但 title_id 还指着旧 Title」的不一致。
    """
    if file_ids is not None:
        ids = list(dict.fromkeys(int(fid) for fid in file_ids if fid is not None))
        if not ids:
            return []
        return (
            LibraryFile.query
            .filter(
                LibraryFile.id.in_(ids),
                LibraryFile.deleted_at.is_(None),
                LibraryFile.title_id.is_(None),
            )
            .order_by(LibraryFile.id)
            .all()
        )
    key = (raw_key or "").strip()
    if not key:
        raise ValueError("raw_key 或 file_ids 至少给一个")
    return (
        LibraryFile.query
        .filter(
            LibraryFile.deleted_at.is_(None),
            LibraryFile.title_id.is_(None),
            LibraryFile.code_guess == key,
        )
        .order_by(LibraryFile.id)
        .all()
    )


def _apply_manual_code(library_file: LibraryFile, code: str) -> bool:
    """给文件打上人工番号（code_guess + confidence=95 + manual_code）。"""
    changed = False
    if library_file.code_guess != code:
        library_file.code_guess = code
        changed = True
    if library_file.confidence != MANUAL_CONFIDENCE:
        library_file.confidence = MANUAL_CONFIDENCE
        changed = True
    if not getattr(library_file, "manual_code", False):
        library_file.manual_code = True
        changed = True
    return changed


# ---------------------------------------------------------------------------
# B. 人工指派
# ---------------------------------------------------------------------------
def assign_code(app, raw_key: str, code: str, *, file_ids: Sequence[int] | None = None,
                apply_materialize: bool = True) -> dict:
    """人工把一组文件指派到某个番号。

    步骤：写 ``CodeAlias(raw_key → code)``（幂等 upsert）→ 批量改文件
    ``code_guess=code`` / ``confidence=95`` → （默认）立即物化该番号。

    返回 ``{files_updated, alias_created, title_id, materialize, code, errors}``：

    * ``files_updated``：真正发生变化的文件数（重复指派同目标时为 0，幂等）；
    * ``alias_created``：是否新建了 CodeAlias；
    * ``title_id``：物化/匹配后挂上的 Title id（dump 没命中则为 None）；
    * ``materialize``：materialize 的统计 dict（未执行则为 None）。
    """
    target = _resolve_target_code(code)
    key = (raw_key or "").strip()
    files = _select_files(key or None, file_ids)
    if not files and not key:
        raise ValueError("没有可指派的文件（raw_key 不存在或 file_ids 为空）")

    result: dict = {
        "code": target,
        "files_updated": 0,
        "alias_created": False,
        "title_id": None,
        "materialize": None,
        "errors": [],
    }

    # 原始键 == 目标番号 时不用写 alias（自映射没有意义，也会制造噪音）
    if key and key != target:
        result["alias_created"] = _upsert_alias(key, target)

    for library_file in files:
        if _apply_manual_code(library_file, target):
            result["files_updated"] += 1
    db.session.commit()

    if apply_materialize:
        result["materialize"] = _run_materialize(app, target)
        if isinstance(result["materialize"], dict) and result["materialize"].get("error"):
            result["errors"].append(str(result["materialize"]["error"]))

    title = _find_title(target)
    result["title_id"] = title.id if title is not None else None
    if title is None and apply_materialize:
        result["errors"].append("该番号在 r18 dump 中未命中，Title 未建立；文件已改番号，待人工补元数据")
    elif title is None:
        result["errors"].append("目标番号在主库暂无 Title（apply_materialize=False，未触发物化）")
    return result


# ---------------------------------------------------------------------------
# C. 合并到已有番号
# ---------------------------------------------------------------------------
def reassign_files(file_ids: Sequence[int], target_code: str, *,
                   app=None) -> dict:
    """把选中文件挂到已有 Title（没有就先 materialize），并学习 alias。

    * 原 ``code_guess`` 写进 ``CodeAlias``（下次扫描/指派可复用）；
    * 文件改为目标番号、``confidence=95``、``title_id`` 指向目标 Title；
    * 目标番号在主库和 dump 都不存在时，只改番号并返回错误提示（不抛）。
    """
    if app is None:
        app = current_app._get_current_object()

    ids = list(dict.fromkeys(int(fid) for fid in file_ids if fid is not None))
    if not ids:
        raise ValueError("file_ids 不能为空")

    target = (target_code or "").strip()
    if not target:
        raise ValueError("目标番号不能为空")

    title = _find_title(target)
    code = title.dvd_id if title is not None else (canonical_dvd_id(target) or target.upper())

    files = (
        LibraryFile.query
        .filter(LibraryFile.id.in_(ids), LibraryFile.deleted_at.is_(None))
        .order_by(LibraryFile.id)
        .all()
    )
    result: dict = {
        "code": code,
        "files_updated": 0,
        "alias_created": 0,
        "title_id": title.id if title is not None else None,
        "materialize": None,
        "errors": [],
    }
    if not files:
        result["errors"].append("没有找到这些文件（可能已删除）")
        return result

    # 1) 学习别名：原 code_guess -> 目标番号（先记，再改文件）
    #    已经是目标番号的文件不写「自映射」，否则重复合并会不断新增 alias
    seen_raw: set[str] = set()
    for library_file in files:
        raw = (library_file.code_guess or "").strip()
        if not raw or raw == code or raw in seen_raw:
            continue
        seen_raw.add(raw)
        if _upsert_alias(raw, code):
            result["alias_created"] += 1

    changed_ids: set[int] = set()

    def _touch(library_file: LibraryFile, title_id: int | None) -> None:
        changed = _apply_manual_code(library_file, code)
        if title_id is not None and library_file.title_id != title_id:
            library_file.title_id = title_id
            changed = True
        if changed:
            changed_ids.add(library_file.id)

    if title is None:
        # 目标 Title 不存在：先落番号再尝试物化建 Title
        for library_file in files:
            _touch(library_file, None)
        db.session.commit()
        result["materialize"] = _run_materialize(app, code)
        if isinstance(result["materialize"], dict) and result["materialize"].get("error"):
            result["errors"].append(str(result["materialize"]["error"]))
        title = _find_title(code)
        result["title_id"] = title.id if title is not None else None

    if title is not None:
        for library_file in files:
            _touch(library_file, title.id)
    else:
        result["errors"].append(
            "目标番号在 r18 dump 中未命中，Title 未建立；文件已改番号，待人工补元数据"
        )

    result["files_updated"] = len(changed_ids)
    db.session.commit()
    return result
