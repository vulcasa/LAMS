"""文件内容哈希与抽检（母带位衰减检测）。

- 对可访问的 LibraryFile 计算 SHA-256（流式 1MiB），写回 ``library_file.sha256``；
- ``verify=True`` 时与已存哈希比对，不一致/文件缺失记入 errors；
- 磁带文件需要先挂载；只读取，不改文件。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from app.config import Config
from app.db import db
from app.models import LibraryFile, LibraryVolume, utcnow

__all__ = ["hash_library_files", "file_abs_path"]

CHUNK = 1024 * 1024


def file_abs_path(app, library_file: LibraryFile) -> Path | None:
    volume = library_file.volume
    if volume is None:
        return None
    if (volume.kind or "") == "tape":
        return Path(app.config.get("LTFS_MOUNT", Config.LTFS_MOUNT)) / library_file.rel_path
    if volume.ref:
        return Path(volume.ref) / library_file.rel_path
    return None


def _is_video(rel_path: str) -> bool:
    return Path(rel_path or "").suffix.lower() in {
        ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".ts", ".flv", ".webm"
    }


def _maybe_extract_frames(app, library_file: LibraryFile, stats: dict,
                          frame_count: int | None) -> None:
    try:
        from app.services.video_frames import extract_frames

        # 已有截图就跳过，避免重复抽帧
        from app.models import Asset

        existing = Asset.query.filter_by(
            title_id=library_file.title_id, kind="screenshot"
        ).count()
        if existing > 0:
            return
        result = extract_frames(
            app, library_file.title_id, count=frame_count, allow_tape=True
        )
        if result.get("frames"):
            stats["frames_titles"] += 1
            stats["frames"] += int(result["frames"])
        elif result.get("errors"):
            stats["frame_errors"].append(
                f"{library_file.rel_path}: {'; '.join(result['errors'][:2])}"
            )
    except Exception as exc:  # noqa: BLE001 - 抽帧失败不影响哈希
        stats["frame_errors"].append(f"{library_file.rel_path}: {type(exc).__name__}: {exc}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def hash_library_files(
    app,
    *,
    volume_id: int | None = None,
    title_id: int | None = None,
    limit: int | None = None,
    verify: bool = False,
    with_frames: bool = False,
    max_videos: int | None = None,
    frame_count: int | None = None,
    on_total=None,
    progress=None,
) -> dict:
    stats = {
        "ok": False,
        "considered": 0,
        "hashed": 0,
        "verified": 0,
        "mismatched": 0,
        "missing": 0,
        "skipped": 0,
        "frames_titles": 0,
        "frames": 0,
        "frame_errors": [],
        "errors": [],
    }
    query = LibraryFile.query.filter(LibraryFile.deleted_at.is_(None))
    if volume_id:
        query = query.filter(LibraryFile.volume_id == volume_id)
    if title_id:
        query = query.filter(LibraryFile.title_id == title_id)
    query = query.order_by(LibraryFile.id)
    if limit:
        query = query.limit(int(limit))
    rows = query.all()
    stats["considered"] = len(rows)
    if on_total is not None:
        try:
            on_total(len(rows))
        except Exception:  # noqa: BLE001
            pass
    for index, library_file in enumerate(rows, start=1):
        path = file_abs_path(app, library_file)
        if path is None:
            stats["skipped"] += 1
            continue
        try:
            if not path.is_file():
                if verify:
                    stats["missing"] += 1
                    stats["errors"].append(f"文件不可访问: {library_file.rel_path}")
                else:
                    stats["skipped"] += 1
                continue
            current = _sha256(path)
            if verify and library_file.sha256:
                if current != library_file.sha256:
                    stats["mismatched"] += 1
                    stats["errors"].append(
                        f"哈希不一致: {library_file.rel_path}（已存 {library_file.sha256[:12]}…，"
                        f"当前 {current[:12]}…）"
                    )
                else:
                    stats["verified"] += 1
            else:
                library_file.sha256 = current
                stats["hashed"] += 1
        except OSError as exc:
            stats["errors"].append(f"{library_file.rel_path}: {exc}")

        # 同一次挂载会话里：哈希完顺手抽帧（方案 A；磁带会很慢）
        if with_frames and library_file.title_id and _is_video(library_file.rel_path):
            if max_videos is None or stats["frames_titles"] < int(max_videos):
                _maybe_extract_frames(app, library_file, stats, frame_count)

        if progress is not None:
            progress(index, f"哈希 {index}/{len(rows)}")
        if index % 100 == 0:
            db.session.commit()
    db.session.commit()
    stats["ok"] = stats["mismatched"] == 0 and stats["missing"] == 0
    return stats
