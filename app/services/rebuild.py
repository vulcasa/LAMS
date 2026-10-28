"""从磁带 manifest 重建主库索引（灾难恢复：数据库全丢 / 换机器）。

场景
----
磁带是唯一副本。每次盘点都会在磁带根写 ``._manifest/<barcode>.json``
（格式见 :mod:`app.services.audit` 的 ``write_manifest``）。本模块只靠这些文件
重建 ``library_volume`` + ``library_file``，让 ``materialize`` 之后能按番号重新
关联影片元数据。

设计
----
- **manifest 解析复用** :func:`app.services.audit.read_manifest`：schema=1 和旧格式
  （没有 schema / 裸 files 列表 / ``path``+``code_guess`` 键名）都由它归一化。
- **路径灵活**：``paths`` 里可以混放
  1) manifest 文件本身；2) 挂载根目录（自动找 ``<root>/._manifest/*.json``）；
  3) 备份目录（直接放 ``<barcode>.json``）；4) ``._manifest`` 目录本身。
  重复路径按真实路径去重。普通目录里不像 manifest 的 json（例如语料的
  ``manifest.json``）会被忽略，避免造出垃圾卷。
- **按 barcode upsert**：``LibraryVolume(kind='tape', ref=barcode)``；同一卷的多份
  manifest 取 ``generated_at`` 最新的那份决定卷级元数据（name/used_bytes/时间），
  文件条目取并集（后者的同 rel_path 条目覆盖前者）——这样老扫描 + 新扫描都在时
  以新为准，且不会把新盘少扫的文件从库里拿走。
- **文件 upsert**：``(volume_id, rel_path)``；保留 ``code/confidence/flags/size/mtime``，
  ``is_sidecar`` 按 ``Config.VIDEO_EXTS`` 推断（非视频即 sidecar，与 scanner 一致），
  ``name`` 取 rel_path 的 basename；软删的旧行被 manifest 重新确认时清 ``deleted_at``
  复活。
- **不软删**：manifest 未列出的行（含其它卷、NAS 卷、本卷多出来的行）一律不碰——
  重建只做“补和修”，不做“删”。数据库丢光时本来也没有可删的。
- **幂等**：同一批 manifest 重复导入 → ``files_created == 0``、``files_updated == 0``，
  行数不变。mtime 比较截到秒（manifest 只存秒，DB 里是文件系统微秒），避免每次
  都误判 update。
- **dry-run**：只统计将要新增/更新多少行，**一个字节也不写库**。
- 大卷（1 万文件）用 executemany 分块（1000 行/批）提交，进度回调和 commit 对齐，
  jobs 的 progress 回调自己 commit 也不会打断事务。
"""
from __future__ import annotations

import dataclasses
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from sqlalchemy import insert, update

from app.config import Config
from app.db import db
from app.models import LibraryFile, LibraryVolume, utcnow
from app.services.audit import MANIFEST_DIRNAME, read_manifest

logger = logging.getLogger(__name__)

__all__ = [
    "RebuildStats",
    "discover_manifests",
    "rebuild_from_manifests",
]

#: 每批 executemany 的行数（大卷别一个文件一次 commit）
CHUNK_ROWS = 1000
#: 每处理这么多文件回调一次 progress
PROGRESS_FILES = 1000
#: errors 最多保留条数
MAX_ERRORS = 100
#: 重建来源写进 LibraryVolume.last_scan_status，UI 一眼能看出不是扫描出来的
REBUILD_STATUS = "manifest"

#: 普通目录里的 json 至少要有这些键之一才当 manifest（避免把语料 manifest.json 等
#: 无关文件当成"空磁带"建卷）
_MANIFEST_MARKERS = {"schema", "barcode", "files", "file_count", "stats"}
#: manifest 顶层保留键（其余键视为附加元数据，比如 name）
_GENERATED_AT_KEYS = ("generated_at", "timestamp")


# ---------------------------------------------------------------------------
# 结果统计
# ---------------------------------------------------------------------------
@dataclass
class RebuildStats:
    """一次 manifest 重建的结果（字段只增不改，方便 CLI/Job 序列化）。"""

    dry_run: bool = False
    manifests_found: int = 0       # 发现的 manifest 文件数
    manifests_imported: int = 0    # 成功解析并导入的
    manifests_failed: int = 0      # 解析失败/条码不可用被跳过的
    volumes_created: int = 0
    volumes_updated: int = 0
    files_created: int = 0
    files_updated: int = 0
    files_unchanged: int = 0
    files_skipped: int = 0         # manifest 里条目本身不合法（缺 rel_path 等）
    videos: int = 0
    sidecars: int = 0
    with_code: int = 0
    total_bytes: int = 0
    barcodes: list[str] = field(default_factory=list)
    per_volume: dict[str, int] = field(default_factory=dict)
    elapsed_s: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _add_error(stats: RebuildStats, message: str) -> None:
    if len(stats.errors) < MAX_ERRORS:
        stats.errors.append(message)
    elif len(stats.errors) == MAX_ERRORS:
        stats.errors.append(f"... 错误过多，已省略后续条目")


def _make_progress(progress) -> Callable[[int, str], None]:
    if progress is None:
        return lambda done, message="": None

    def emit(done: int, message: str = "") -> None:
        try:
            progress(int(done), message)
        except Exception:  # pragma: no cover - 回调实现问题不该拖垮重建
            logger.warning("progress 回调异常", exc_info=True)

    return emit


def _parse_datetime(value: Any) -> datetime | None:
    """manifest 里的时间（ISO 字符串 / epoch / datetime）→ naive UTC datetime。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        if not text:
            return None
        # 老格式可能存 epoch 秒（数字字符串）
        try:
            if text.replace(".", "", 1).lstrip("-").isdigit():
                return datetime.fromtimestamp(float(text), timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            pass
        candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            parsed = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y%m%d%H%M%S"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if parsed is None:
                return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _mtime_key(value: datetime | None) -> datetime | None:
    """比较 mtime 时截到秒（manifest 只有秒，DB 里是文件系统微秒）。"""
    return value.replace(microsecond=0) if value is not None else None


def manifest_generated_at(manifest: dict) -> datetime | None:
    for key in _GENERATED_AT_KEYS:
        parsed = _parse_datetime(manifest.get(key))
        if parsed is not None:
            return parsed
    return None


def _normalize_barcode(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _barcode_of(manifest: dict) -> str | None:
    """条码：manifest 里显式写的优先（read_manifest 已用文件名做过兜底）。"""
    barcode = _normalize_barcode(manifest.get("barcode"))
    if barcode and barcode.upper() not in ("UNKNOWN", "NONE"):
        return barcode
    return None


def _is_video(name: str, video_exts: set[str]) -> bool:
    return Path(name).suffix.lower() in video_exts


# ---------------------------------------------------------------------------
# manifest 发现 / 读取
# ---------------------------------------------------------------------------
def discover_manifests(paths: Iterable[str | Path]) -> tuple[list[Path], list[str], set[Path]]:
    """把用户给的路径展开成 manifest 文件列表。

    返回 ``(manifest_files, errors, explicitly_named)``；``explicitly_named`` 是用户
    直接点名的文件（不受"目录里必须像 manifest 才收"的过滤限制）。
    """
    # 容错：允许直接传单个 str/Path（不然会被当成字符序列逐个遍历）
    if isinstance(paths, (str, Path)):
        paths = [paths]
    found: list[Path] = []
    errors: list[str] = []
    explicit: set[Path] = set()
    seen: set[Path] = set()

    def add(path: Path, *, named: bool) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        found.append(path)
        if named:
            explicit.add(resolved)

    for raw in paths or []:
        if raw is None:
            continue
        path = Path(str(raw)).expanduser()
        if path.is_file():
            add(path, named=True)
            continue
        if path.is_dir():
            candidates: list[Path] = []
            manifest_dir = path if path.name == MANIFEST_DIRNAME else path / MANIFEST_DIRNAME
            if manifest_dir.is_dir():
                candidates.extend(sorted(manifest_dir.glob("*.json")))
            if path.name != MANIFEST_DIRNAME:
                # 备份目录：manifest 直接躺在目录里（<barcode>.json）
                candidates.extend(sorted(path.glob("*.json")))
            if not candidates:
                errors.append(f"目录下没有 manifest: {path}")
                continue
            for candidate in candidates:
                add(candidate, named=False)
            continue
        errors.append(f"路径不存在: {path}")
    return found, errors, explicit


def _looks_like_manifest(path: Path) -> bool:
    """普通目录里的 json：有 manifest 特征键才算（防把语料 manifest.json 当磁带）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(raw, dict):
        return False
    return bool(_MANIFEST_MARKERS & set(raw))


def _load_manifest(path: Path, *, explicit: bool) -> tuple[dict | None, str | None]:
    """读一个 manifest；失败返回 ``(None, 错误信息)``，不抛异常。"""
    trusted = explicit or path.parent.name == MANIFEST_DIRNAME
    if not trusted and not _looks_like_manifest(path):
        return None, f"不是磁带 manifest，已忽略: {path.name}"
    manifest = read_manifest(path)
    if manifest is None:
        return None, f"manifest 无法解析（JSON 损坏或格式不支持）: {path}"
    return manifest, None


# ---------------------------------------------------------------------------
# 数据库 upsert
# ---------------------------------------------------------------------------
def _load_existing_files(volume_id: int) -> dict[str, dict]:
    """预取该卷已有行（纯元组，避免 1 万个 ORM 对象进 identity map）。"""
    rows = (
        db.session.query(
            LibraryFile.id,
            LibraryFile.rel_path,
            LibraryFile.name,
            LibraryFile.size,
            LibraryFile.mtime,
            LibraryFile.code_guess,
            LibraryFile.confidence,
            LibraryFile.flags,
            LibraryFile.is_sidecar,
            LibraryFile.deleted_at,
        )
        .filter(LibraryFile.volume_id == volume_id)
        .all()
    )
    result: dict[str, dict] = {}
    for row in rows:
        result[row.rel_path] = {
            "id": row.id,
            "name": row.name,
            "size": row.size,
            "mtime": row.mtime,
            "code_guess": row.code_guess,
            "confidence": row.confidence,
            "flags": row.flags,
            "is_sidecar": bool(row.is_sidecar),
            "deleted_at": row.deleted_at,
        }
    return result


def _entry_to_values(entry: dict, video_exts: set[str]) -> dict | None:
    """manifest 文件条目 → 可直接写库的字段 dict（非法条目返回 None）。"""
    rel_path = entry.get("rel_path")
    if not rel_path:
        return None
    rel_path = str(rel_path)
    name = rel_path.rsplit("/", 1)[-1] or rel_path
    code = entry.get("code")
    code = str(code).strip() if code is not None and str(code).strip() else None
    confidence = entry.get("confidence")
    try:
        confidence = int(confidence) if confidence is not None else 0
    except (TypeError, ValueError):
        confidence = 0
    flags = entry.get("flags")
    flags = str(flags) if flags not in (None, "") else None
    size = entry.get("size")
    try:
        size = int(size) if size is not None else None
    except (TypeError, ValueError):
        size = None
    return {
        "rel_path": rel_path,
        "name": name,
        "size": size,
        "mtime": _parse_datetime(entry.get("mtime")),
        "code_guess": code,
        "confidence": confidence,
        "flags": flags,
        "is_sidecar": not _is_video(name, video_exts),
    }


def _values_changed(old: dict, new: dict) -> bool:
    if old["deleted_at"] is not None:
        return True  # 软删的行被 manifest 重新确认 → 复活也算 change
    return (
        old["name"] != new["name"]
        or old["size"] != new["size"]
        or _mtime_key(old["mtime"]) != _mtime_key(new["mtime"])
        or old["code_guess"] != new["code_guess"]
        or (old["confidence"] or 0) != new["confidence"]
        or old["flags"] != new["flags"]
        or bool(old["is_sidecar"]) != new["is_sidecar"]
    )


def _flush_rows(
    new_rows: list[dict], update_rows: list[dict], *, dry_run: bool
) -> None:
    if dry_run:
        return
    if new_rows:
        db.session.execute(insert(LibraryFile), new_rows)
    if update_rows:
        stmt = update(LibraryFile).execution_options(synchronize_session=False)
        db.session.execute(stmt, update_rows)
    db.session.commit()


def _rebuild_one_volume(
    app,
    barcode: str,
    entries: list[dict],
    latest_manifest: dict,
    stats: RebuildStats,
    emit: Callable[[int, str], None],
    done_before: int,
) -> int:
    """导入一盘磁带，返回已处理文件数。"""
    cfg = getattr(app, "config", {}) or {}
    video_exts = set(cfg.get("VIDEO_EXTS") or Config.VIDEO_EXTS)

    volume = LibraryVolume.query.filter_by(kind="tape", ref=barcode).first()
    dry_run = stats.dry_run
    created_volume = volume is None
    if created_volume:
        name = latest_manifest.get("name") or f"磁带 {barcode}"
        if not dry_run:
            volume = LibraryVolume(kind="tape", ref=barcode, name=str(name), enabled=True)
            db.session.add(volume)
            db.session.commit()  # 先拿 id（文件行要用），也立刻固定卷的存在
        else:
            volume = None
    elif volume.name is None:
        if not dry_run:
            volume.name = barcode

    if created_volume:
        existing: dict[str, dict] = {}
    else:
        existing = _load_existing_files(volume.id)

    new_rows: list[dict] = []
    update_rows: list[dict] = []
    pending = 0
    processed = 0
    now = utcnow()
    volume_bytes = 0

    for entry in entries:
        values = _entry_to_values(entry, video_exts)
        processed += 1
        if values is None:
            stats.files_skipped += 1
            continue
        rel_path = values["rel_path"]
        volume_bytes += values["size"] or 0
        stats.videos += 0 if values["is_sidecar"] else 1
        stats.sidecars += 1 if values["is_sidecar"] else 0
        if values["code_guess"]:
            stats.with_code += 1

        old = existing.get(rel_path)
        if old is None:
            stats.files_created += 1
            if not dry_run:
                row = dict(values)
                row["volume_id"] = volume.id
                row["seen_at"] = now
                row["deleted_at"] = None
                new_rows.append(row)
                pending += 1
        elif _values_changed(old, values):
            stats.files_updated += 1
            if not dry_run:
                update_rows.append(
                    {
                        "id": old["id"],
                        "name": values["name"],
                        "size": values["size"],
                        "mtime": values["mtime"],
                        "code_guess": values["code_guess"],
                        "confidence": values["confidence"],
                        "flags": values["flags"],
                        "is_sidecar": values["is_sidecar"],
                        "deleted_at": None,
                        "seen_at": now,
                    }
                )
                pending += 1
        else:
            stats.files_unchanged += 1

        if (pending >= CHUNK_ROWS or processed % PROGRESS_FILES == 0) and not dry_run:
            _flush_rows(new_rows, update_rows, dry_run=False)
            new_rows = []
            update_rows = []
            pending = 0
        if processed % PROGRESS_FILES == 0:
            emit(
                done_before + processed,
                f"{barcode}: 已处理 {processed}/{len(entries)} 文件",
            )

    if not dry_run:
        _flush_rows(new_rows, update_rows, dry_run=False)

    # --- 卷级元数据（以最新 manifest 为准） -----------------------------
    generated_at = manifest_generated_at(latest_manifest) or now
    used_bytes = volume_bytes
    if created_volume:
        stats.volumes_created += 1
        stats.barcodes.append(barcode)
        if not dry_run:
            volume.last_scan_at = generated_at
            volume.last_scan_status = REBUILD_STATUS
            volume.used_bytes = used_bytes
            db.session.commit()
    else:
        changed = (
            volume.used_bytes != used_bytes or volume.last_scan_status != REBUILD_STATUS
        )
        if not dry_run:
            volume.last_scan_at = generated_at
            volume.last_scan_status = REBUILD_STATUS
            volume.used_bytes = used_bytes
            db.session.commit()
        if changed:
            stats.volumes_updated += 1
        if barcode not in stats.barcodes:
            stats.barcodes.append(barcode)

    stats.total_bytes += used_bytes
    stats.per_volume[barcode] = processed
    return processed


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def rebuild_from_manifests(
    app,
    paths: Iterable[str | Path] | None = None,
    *,
    progress=None,
    dry_run: bool = False,
) -> RebuildStats:
    """从 manifest 文件/目录重建 ``library_volume`` + ``library_file``。

    ``paths``：manifest 文件、挂载根目录（含 ``._manifest``）、备份目录、或混放列表。
    ``progress(done, message="")``：进度回调；``dry_run=True`` 时只统计不写库。

    返回 :class:`RebuildStats`；**单个 manifest 失败不中断整批**（错误收集在
    ``stats.errors``）。
    """
    started = time.time()
    stats = RebuildStats(dry_run=bool(dry_run))
    emit = _make_progress(progress)

    if not paths:
        # 默认读本地 manifest 目录（新布局），不再要求插磁带
        paths = [app.config.get("MANIFEST_DIR") or (Path(str(app.config.get("DATA_DIR"))) / "manifests")]
    manifest_files, discover_errors, explicit = discover_manifests(paths)
    stats.manifests_found = len(manifest_files)
    for message in discover_errors:
        _add_error(stats, message)

    # --- 读 + 归一化 + 按 barcode 分组（同卷多份取最新为准，文件取并集）------
    groups: dict[str, list[tuple[datetime | None, dict]]] = {}
    order: list[str] = []
    for path in manifest_files:
        manifest, error = _load_manifest(path, explicit=path.resolve() in explicit)
        if error is not None:
            stats.manifests_failed += 1
            _add_error(stats, error)
            continue
        assert manifest is not None
        barcode = _barcode_of(manifest)
        if barcode is None:
            stats.manifests_failed += 1
            _add_error(stats, f"manifest 条码不可用（UNKNOWN/空），已跳过: {path.name}")
            continue
        stats.manifests_imported += 1
        if barcode not in groups:
            groups[barcode] = []
            order.append(barcode)
        groups[barcode].append((manifest_generated_at(manifest), manifest))

    total_files = sum(
        len(manifest.get("files") or [])
        for per_volume in groups.values()
        for _generated, manifest in per_volume
    )
    emit(0, f"开始重建：{len(groups)} 盘 / {total_files} 个文件条目")
    done = 0

    for barcode in order:
        manifests = sorted(
            groups[barcode],
            key=lambda item: item[0] or datetime.min,
        )
        merged: dict[str, dict] = {}
        for _generated, manifest in manifests:
            for entry in manifest.get("files") or []:
                if isinstance(entry, dict) and entry.get("rel_path"):
                    merged[str(entry["rel_path"])] = entry
        entries = list(merged.values())
        entries.sort(key=lambda item: item["rel_path"])

        try:
            processed = _rebuild_one_volume(
                app, barcode, entries, manifests[-1][1], stats, emit, done
            )
            done += processed
        except Exception as exc:  # 单卷失败不拖垮整批
            logger.exception("重建卷失败: %s", barcode)
            stats.files_skipped += len(entries)
            _add_error(stats, f"{barcode}: 重建失败 {type(exc).__name__}: {exc}")

    stats.elapsed_s = time.time() - started
    emit(done, f"重建完成：{len(stats.barcodes)} 盘 / 共处理 {done} 文件条目")
    return stats


def rebuild_from_db(app, *, progress=None, dry_run: bool = False) -> RebuildStats:
    """从数据库里的 ``TapeManifest`` 重建索引（文件副本丢失时的兜底）。"""
    from app.models import TapeManifest
    from app.services.audit import read_manifest_from_db

    stats = RebuildStats(dry_run=bool(dry_run))
    emit = _make_progress(progress)
    rows = TapeManifest.query.order_by(TapeManifest.id).all()
    stats.manifests_found = len(rows)
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        manifest = read_manifest_from_db(app, row.barcode)
        if manifest is None:
            stats.manifests_failed += 1
            _add_error(stats, f"数据库 manifest 解析失败: {row.barcode}")
            continue
        stats.manifests_imported += 1
        grouped.setdefault(str(manifest.get("barcode") or row.barcode), []).append(manifest)

    done = 0
    for barcode, manifests in grouped.items():
        merged: dict[str, dict] = {}
        for manifest in manifests:
            for entry in manifest.get("files") or []:
                if isinstance(entry, dict) and entry.get("rel_path"):
                    merged[str(entry["rel_path"])] = entry
        entries = list(merged.values())
        try:
            done += _rebuild_one_volume(
                app, barcode, entries, manifests[-1], stats, emit, done
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("从 DB 重建卷失败: %s", barcode)
            stats.files_skipped += len(entries)
            _add_error(stats, f"{barcode}: 重建失败 {type(exc).__name__}: {exc}")
    emit(done, f"从数据库重建完成：{len(stats.barcodes)} 盘")
    return stats
