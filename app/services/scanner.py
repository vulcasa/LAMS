"""NAS / 磁带扫描器：把目录树增量编目到 ``library_volume`` / ``library_file``。

契约见 ``docs/CONTRACTS.md`` §1/§2，要点复述：

- 卷按 ``(kind, ref)`` 复用：NAS 的 ref 是 ``str(Path(root).resolve())``，磁带是条码。
  每次扫描更新 ``last_scan_at / last_scan_status / used_bytes``。
- ``os.walk`` 遍历，不跟目录符号链接，跳过 ``Config.SKIP_DIRS``；
  单文件 ``os.stat`` 失败只记 ``errors``，不中断整批。
- 分类：``Config.VIDEO_EXTS`` 是视频；**其余文件一律当 sidecar 入库**
  （``Config.SUBTITLE_EXTS`` + ``.nfo`` 是字幕/metadata sidecar，封面 jpg、
  VIDEO_TS 里的 .vob 等也一并入库）。这么定有两个原因：
  1) 契约要求字幕 sidecar 也要入库（``is_sidecar=True``），后续 materialize 用
     ``code_guess`` 把它们挂到同一部 Title 上；
  2) 只有「所有文件都入库」，``deleted`` 软删检测和合成语料的 manifest 口径
     才完整（nas 346 文件 = 297 视频扩展名 + 49 sidecar，其中 30 条故意垃圾名 →
     no_code；manifest 把 3 条怪异扩展名的垃圾名也记成 video，所以它标称 300）。
- 番号/置信度/flags 全部来自 ``app.services.code_parser.extract_code(rel_path)``；
  flags 落成 ``",".join(sorted(flags))``。
- 增量：``(volume_id, rel_path)`` 已存在 → size/mtime 都没变记 ``unchanged``
  （只刷 ``seen_at``），变了记 ``updated``；不存在记 ``new``。
  本轮没见到的旧行写 ``deleted_at``（软删，只数第一次被软删的行）；
  再次出现时清空 ``deleted_at``。
- 事务：每 500 行左右 commit 一次（``CHUNK_ROWS``），不是一文件一 commit。
  预加载本卷已有行 + 批量 insert/update，1 万文件扫描 ≈ 1-2s。

设计取舍：

- **不跟符号链接**：``os.walk(followlinks=False)``，指向目录的软链不会被展开
  （NAS 上做「最近下载」软链很常见，展开会重复计数甚至成环）。
- 额外跳过「点开头」的目录和文件：``._manifest``（盘点写回磁带的 manifest）、
  ``.DS_Store``、``.@__thumb`` 这类不是媒体，不能进片库；也避免磁带盘点写完
  manifest 后下一次扫描把它当成新文件。
- 缺失 root 直接 ``ValueError``，**绝不**把整卷文件软删（挂载点没挂上 / NAS
  断连时静默清空片库是最坏的结果）。
- 递归时个别目录读失败（``os.walk`` 的 onerror）只记 errors，且**该目录下的旧行
  不软删**：目录只是暂时读不到，文件其实还在，等下次扫到再更新。
"""

from __future__ import annotations

import logging
import os
import stat as stat_module
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import update

from app.config import Config
from app.db import db
from app.models import LibraryFile, LibraryVolume, utcnow
from app.services.code_parser import FLAG_CHINESE_SUB, extract_code

__all__ = ["ScanStats", "scan_nas", "scan_tape"]

log = logging.getLogger(__name__)

#: 每攒够这么多行 commit 一次（契约要求 500~1000）
CHUNK_ROWS = 500
#: 每处理这么多文件回调一次 progress
PROGRESS_FILES = 500
#: errors 列表最多保留多少条（坏盘/掉线时避免把内存和任务结果撑爆）
MAX_ERRORS = 200

#: 进度回调签名：progress(done: int, message: str = "") -> None
ProgressFn = Callable[..., None]


@dataclass
class ScanStats:
    """一次扫描的结果（字段顺序/名称即契约，不要改）。"""

    volume_id: int
    kind: str                 # 'nas' | 'tape'
    root: str
    files_seen: int = 0
    videos: int = 0
    sidecars: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0          # 本次标记 deleted_at 的旧行
    with_code: int = 0
    low_confidence: int = 0   # 0 < confidence < CODE_AUTO_MIN
    no_code: int = 0
    chinese_sub_files: int = 0
    elapsed_s: float = 0.0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _naive_utc(timestamp: float) -> datetime:
    """文件系统 mtime(float) → naive UTC datetime（模型统一存 naive UTC）。"""
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).replace(tzinfo=None)


def _call_progress(progress: Optional[ProgressFn], done: int, message: str = "") -> None:
    if progress is not None:
        progress(done, message)


def _under_failed_dir(rel_path: str, failed_dirs: list[str]) -> bool:
    """rel_path 是否落在本轮读取失败的目录下。``""`` 表示 root 本身失败（整卷保留）。"""
    for failed in failed_dirs:
        if not failed or rel_path == failed or rel_path.startswith(failed + "/"):
            return True
    return False


class _ErrorBag:
    """收集单文件错误：只留前 MAX_ERRORS 条，末尾补一条汇总。"""

    def __init__(self) -> None:
        self.items: list[str] = []
        self.count = 0

    def add(self, message: str) -> None:
        self.count += 1
        if len(self.items) < MAX_ERRORS:
            self.items.append(message)

    def finish(self) -> list[str]:
        if self.count > len(self.items):
            self.items.append(f"... 共 {self.count} 条错误，已省略 {self.count - len(self.items)} 条")
        return self.items


class _Batch:
    """批量写缓冲：插入 / 更新 / 只刷 seen_at / 复活 / 软删 分桶，一次 commit 全落。"""

    __slots__ = ("inserts", "updates", "touched_ids", "revived_ids", "deleted_ids", "commits")

    def __init__(self) -> None:
        self.inserts: list[dict] = []
        self.updates: list[dict] = []
        self.touched_ids: list[int] = []
        self.revived_ids: list[int] = []
        self.deleted_ids: list[int] = []
        self.commits = 0

    @property
    def pending(self) -> int:
        return (
            len(self.inserts)
            + len(self.updates)
            + len(self.touched_ids)
            + len(self.revived_ids)
            + len(self.deleted_ids)
        )

    def flush(self, *, seen_at: datetime, deleted_at: Optional[datetime] = None) -> None:
        """把缓冲里的行写库并 commit；没有待写行时是 no-op。"""
        if not self.pending:
            return
        if self.inserts:
            db.session.bulk_insert_mappings(LibraryFile, self.inserts)
        if self.updates:
            db.session.bulk_update_mappings(LibraryFile, self.updates)
        if self.touched_ids:
            db.session.execute(
                update(LibraryFile)
                .where(LibraryFile.id.in_(self.touched_ids))
                .values(seen_at=seen_at)
                .execution_options(synchronize_session=False)
            )
        if self.revived_ids:
            db.session.execute(
                update(LibraryFile)
                .where(LibraryFile.id.in_(self.revived_ids))
                .values(seen_at=seen_at, deleted_at=None)
                .execution_options(synchronize_session=False)
            )
        if self.deleted_ids:
            db.session.execute(
                update(LibraryFile)
                .where(LibraryFile.id.in_(self.deleted_ids))
                .values(deleted_at=deleted_at or utcnow())
                .execution_options(synchronize_session=False)
            )
        db.session.commit()
        self.commits += 1
        self.inserts.clear()
        self.updates.clear()
        self.touched_ids.clear()
        self.revived_ids.clear()
        self.deleted_ids.clear()


def _ensure_volume(
    *,
    kind: str,
    ref: str,
    name: Optional[str],
    update_name: bool = False,
) -> LibraryVolume:
    """按 (kind, ref) 找卷，没有就建；返回已 commit 的卷（有 id）。"""
    volume = LibraryVolume.query.filter_by(kind=kind, ref=ref).first()
    if volume is None:
        volume = LibraryVolume(kind=kind, ref=ref, name=name or ref, enabled=True)
        db.session.add(volume)
    elif update_name and name and volume.name != name:
        volume.name = name
    if volume.name is None:
        volume.name = name or ref
    volume.last_scan_at = utcnow()
    volume.last_scan_status = "running"
    db.session.commit()
    return volume


def _mark_volume_failed(volume_id: int) -> None:
    """扫描中途异常时把卷状态标 failed（不回滚已 commit 的增量结果）。"""
    db.session.rollback()
    try:
        db.session.execute(
            update(LibraryVolume)
            .where(LibraryVolume.id == volume_id)
            .values(last_scan_status="failed", last_scan_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        db.session.commit()
    except Exception:  # noqa: BLE001  标状态失败不能盖掉原始异常
        db.session.rollback()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _scan(
    app,
    *,
    kind: str,
    ref: str,
    root_path: str,
    name: Optional[str],
    update_name: bool = False,
    progress: Optional[ProgressFn] = None,
) -> ScanStats:
    started = time.monotonic()

    # --- 根路径校验（必须在建卷/软删之前，防「挂载点没挂上 → 整卷被软删」）---
    root = Path(str(root_path)).expanduser()
    try:
        root = root.resolve()
    except OSError as exc:  # pragma: no cover - 极端路径异常
        raise ValueError(f"无法解析扫描根路径 {root_path!r}: {exc}") from exc
    if not root.is_dir():
        raise ValueError(f"扫描根不存在或不是目录: {root_path}")
    root_str = str(root)

    cfg = app.config
    video_exts = set(cfg.get("VIDEO_EXTS") or Config.VIDEO_EXTS)
    skip_dirs = set(cfg.get("SKIP_DIRS") or Config.SKIP_DIRS)
    auto_min = int(cfg.get("CODE_AUTO_MIN") or Config.CODE_AUTO_MIN)

    errors = _ErrorBag()
    volume = _ensure_volume(kind=kind, ref=ref, name=name, update_name=update_name)

    files_seen = videos = sidecars = 0
    new = updated = unchanged = deleted = 0
    with_code = low_confidence = no_code = chinese_sub_files = 0
    used_bytes = 0

    seen_at = utcnow()
    batch = _Batch()

    # 预加载本卷已有行（rel_path -> 只读元组），避免一文件一 SELECT
    existing: dict[str, tuple] = {}
    rows = db.session.query(
        LibraryFile.id,
        LibraryFile.rel_path,
        LibraryFile.size,
        LibraryFile.mtime,
        LibraryFile.deleted_at,
        LibraryFile.code_guess,
        LibraryFile.confidence,
        LibraryFile.flags,
    ).filter(LibraryFile.volume_id == volume.id)
    for row_id, rel_path, size, mtime, deleted_at, code_guess, confidence, flags in rows:
        existing[rel_path] = (
            row_id, size, mtime, deleted_at, code_guess, confidence, flags
        )

    seen_rel: set[str] = set()
    # 本轮「目录级」读取失败的相对路径（"" = root 本身）。这些目录下的旧行不能软删：
    # 目录只是暂时读不到（NAS 抖动 / 权限 / LTFS 卡带），文件其实还在。
    walk_failed_dirs: list[str] = []

    def _on_walk_error(exc: OSError) -> None:
        filename = getattr(exc, "filename", "") or root_str
        try:
            rel_dir = os.path.relpath(filename, root_str).replace(os.sep, "/")
        except ValueError:  # pragma: no cover - 跨盘符等极端情况
            rel_dir = ""
        if rel_dir == ".":
            rel_dir = ""
        walk_failed_dirs.append(rel_dir)
        errors.add(f"{filename}: {exc}")

    try:
        for dirpath, dirnames, filenames in os.walk(
            root_str, topdown=True, onerror=_on_walk_error, followlinks=False
        ):
            # 原地剪枝：SKIP_DIRS（NAS 缩略图/回收站）+ 点开头目录（._manifest 等）
            dirnames[:] = sorted(
                d for d in dirnames if d not in skip_dirs and not d.startswith(".")
            )
            for filename in sorted(filenames):
                if not filename or filename.startswith("."):
                    continue
                full = os.path.join(dirpath, filename)
                rel = os.path.relpath(full, root_str).replace(os.sep, "/")
                # 先登记「见过」，这样 stat 失败的既有行不会被误软删
                seen_rel.add(rel)
                try:
                    st = os.stat(full)
                except OSError as exc:
                    errors.add(f"{rel}: {exc}")
                    continue
                if not stat_module.S_ISREG(st.st_mode):
                    continue  # 设备/FIFO/socket 之类，不是文件
                files_seen += 1
                size = int(st.st_size)
                used_bytes += size
                mtime = _naive_utc(st.st_mtime)

                is_video = os.path.splitext(filename)[1].lower() in video_exts
                if is_video:
                    videos += 1
                else:
                    sidecars += 1

                match = extract_code(rel)
                code = match.code
                confidence = int(match.confidence)
                flags = ",".join(sorted(match.flags))
                if code:
                    with_code += 1
                else:
                    no_code += 1
                if 0 < confidence < auto_min:
                    low_confidence += 1
                if FLAG_CHINESE_SUB in match.flags:
                    chinese_sub_files += 1

                prev = existing.get(rel)
                if prev is None:
                    batch.inserts.append(
                        {
                            "volume_id": volume.id,
                            "rel_path": rel,
                            "name": filename,
                            "size": size,
                            "mtime": mtime,
                            "code_guess": code,
                            "confidence": confidence,
                            "flags": flags,
                            "is_sidecar": not is_video,
                            "seen_at": seen_at,
                            "deleted_at": None,
                        }
                    )
                    new += 1
                else:
                    (
                        prev_id, prev_size, prev_mtime, prev_deleted_at,
                        prev_code, prev_conf, prev_flags,
                    ) = prev
                    parser_changed = (
                        code != prev_code
                        or confidence != prev_conf
                        or flags != prev_flags
                    )
                    if size != prev_size or mtime != prev_mtime or parser_changed:
                        batch.updates.append(
                            {
                                "id": prev_id,
                                "size": size,
                                "mtime": mtime,
                                "code_guess": code,
                                "confidence": confidence,
                                "flags": flags,
                                "is_sidecar": not is_video,
                                "seen_at": seen_at,
                                "deleted_at": None,
                            }
                        )
                        updated += 1
                    else:
                        # size/mtime 都没变 → unchanged；如果之前被软删过，顺手复活
                        if prev_deleted_at is not None:
                            batch.revived_ids.append(prev_id)
                        else:
                            batch.touched_ids.append(prev_id)
                        unchanged += 1

                if batch.pending >= CHUNK_ROWS:
                    batch.flush(seen_at=seen_at)
                if files_seen % PROGRESS_FILES == 0:
                    # progress 回调可能自己 commit（jobs 就是这样），先把缓冲落干净
                    batch.flush(seen_at=seen_at)
                    _call_progress(
                        progress,
                        files_seen,
                        f"{kind} 扫描中：{files_seen} 文件 / {videos} 视频 / {with_code} 有番号",
                    )

        # 本轮没见到的旧行 → 软删（只统计第一次被软删的）。
        # 例外：本轮有目录读取失败时，失败目录下的旧行保留 —— 否则 NAS 抖一下、
        # 挂载点半残就会把整卷「静默清空」，重建成本极高。
        deleted_at = utcnow()
        for rel, (prev_id, _size, _mtime, prev_deleted_at, *_rest) in existing.items():
            if rel in seen_rel or prev_deleted_at is not None:
                continue
            if _under_failed_dir(rel, walk_failed_dirs):
                continue
            batch.deleted_ids.append(prev_id)
            deleted += 1
        batch.flush(seen_at=seen_at, deleted_at=deleted_at)

        elapsed = time.monotonic() - started
        volume.used_bytes = used_bytes
        volume.last_scan_at = utcnow()
        volume.last_scan_status = "ok" if errors.count == 0 else "partial"
        db.session.commit()

    except BaseException:
        _mark_volume_failed(volume.id)
        raise

    stats = ScanStats(
        volume_id=volume.id,
        kind=kind,
        root=root_str,
        files_seen=files_seen,
        videos=videos,
        sidecars=sidecars,
        new=new,
        updated=updated,
        unchanged=unchanged,
        deleted=deleted,
        with_code=with_code,
        low_confidence=low_confidence,
        no_code=no_code,
        chinese_sub_files=chinese_sub_files,
        elapsed_s=round(elapsed, 4),
        errors=errors.finish(),
    )
    log.info(
        "scan %s %s: files=%d videos=%d sidecars=%d new=%d updated=%d unchanged=%d "
        "deleted=%d with_code=%d no_code=%d errors=%d elapsed=%.2fs",
        kind,
        ref,
        stats.files_seen,
        stats.videos,
        stats.sidecars,
        stats.new,
        stats.updated,
        stats.unchanged,
        stats.deleted,
        stats.with_code,
        stats.no_code,
        len(stats.errors),
        stats.elapsed_s,
    )
    _call_progress(progress, files_seen, f"{kind} 扫描完成：{files_seen} 文件")
    return stats


def scan_nas(
    app,
    root_path: str,
    *,
    volume_name: Optional[str] = None,
    progress: Optional[ProgressFn] = None,
) -> ScanStats:
    """扫描一个 NAS 根目录（增量、软删），返回 :class:`ScanStats`。

    调用方负责开 ``app.app_context()``。
    """
    root = Path(str(root_path)).expanduser()
    try:
        resolved = root.resolve()
    except OSError as exc:  # pragma: no cover
        raise ValueError(f"无法解析扫描根路径 {root_path!r}: {exc}") from exc
    return _scan(
        app,
        kind="nas",
        ref=str(resolved),
        root_path=str(resolved),
        name=volume_name or resolved.name or str(resolved),
        update_name=bool(volume_name),
        progress=progress,
    )


def scan_tape(
    app,
    mount_path: str,
    *,
    barcode: str,
    expected_barcode: Optional[str] = None,
    progress: Optional[ProgressFn] = None,
) -> ScanStats:
    """扫描已挂载的磁带（增量、软删），返回 :class:`ScanStats`。

    条码强校验（防串盘）：
    - ``barcode`` 为空 / ``UNKNOWN`` → ``ValueError``；
    - 给了 ``expected_barcode`` 且与实际不符 → ``ValueError``。

    这两种情况都在建卷/写任何 ``library_file`` **之前**抛出。
    """
    actual = (barcode or "").strip()
    if not actual:
        raise ValueError("磁带条码为空，拒绝扫描（防串盘）")
    if actual.upper() == "UNKNOWN":
        raise ValueError("磁带条码是 UNKNOWN，拒绝扫描（防串盘）")
    if expected_barcode is not None:
        expected = str(expected_barcode).strip()
        if expected and actual != expected:
            raise ValueError(f"磁带条码不符：实际 {actual!r}，预期 {expected!r}（拒绝扫描）")
    return _scan(
        app,
        kind="tape",
        ref=actual,
        root_path=str(Path(str(mount_path)).expanduser()),
        name=actual,
        update_name=False,
        progress=progress,
    )
