"""媒体库变动监测：定期对已启用的 NAS/SMB 媒体库做增量扫描。

不是 inotify：SMB 对 inotify 支持不可靠，这里用固定间隔轮询 + scanner 的增量逻辑。
检测到新增/变化文件后，扫描任务会走现有流程自动物化并刷新片库。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from app.db import db
from app.models import Job, LibraryVolume, MediaSource, utcnow

_started = False
_thread = None


def _active_roots() -> set[str]:
    try:
        rows = Job.query.filter(
            Job.kind == "scan_nas", Job.status.in_(("pending", "running"))
        ).all()
        roots = set()
        for row in rows:
            try:
                payload = json.loads(row.payload or "{}")
                if payload.get("root"):
                    roots.add(str(Path(payload["root"]).expanduser().resolve()))
            except Exception:  # noqa: BLE001
                continue
        return roots
    except Exception:  # noqa: BLE001
        return set()


def _due(last, interval: int) -> bool:
    if last is None:
        return True
    try:
        return (utcnow() - last).total_seconds() >= interval
    except TypeError:
        return True


def scan_once(app) -> list[int]:
    """检查一次媒体库；返回新入队的扫描 job id。"""
    from app.services import jobs
    from app.services.settings import (
        KEY_AUTO_REFRESH_SOURCES, KEY_SOURCE_REFRESH_MINUTES, get_bool, get_int,
    )

    if not get_bool(KEY_AUTO_REFRESH_SOURCES, False):
        return []
    # 抽帧/哈希/物化等媒体任务进行时，自动扫描让路，避免抢 worker / DB 锁
    blocking = Job.query.filter(
        Job.kind.in_((
            "video_frames", "video_frames_batch", "hash_verify",
            "materialize", "transfer", "audit_tape", "scan_tape",
        )),
        Job.status.in_(("pending", "running")),
    ).first()
    if blocking is not None:
        return []
    interval = max(60, int(get_int(KEY_SOURCE_REFRESH_MINUTES, 120)) * 60)
    smb_auto_refresh = get_bool("smb_auto_refresh", False)
    active = _active_roots()
    seen_paths: set[str] = set()
    queued: list[int] = []

    sources = MediaSource.query.filter(MediaSource.enabled.is_(True)).all()
    for source in sources:
        if not source.auto_scan:
            continue
        path = Path(str(source.path or "")).expanduser()
        try:
            resolved = path.resolve()
        except OSError:
            continue
        key = str(resolved)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        if source.origin == "smb":
            if not smb_auto_refresh:
                continue
            try:
                from app.services.smb import _is_mounted

                if not _is_mounted(path):
                    continue
            except Exception:  # noqa: BLE001
                continue
        try:
            if not resolved.is_dir():
                continue
        except OSError:
            continue
        if key in active or not _due(source.last_scan_at, interval):
            continue
        job_id = jobs.enqueue("scan_nas", {
            "root": str(resolved), "name": source.name,
            "source_id": source.id,
        })
        active.add(key)
        queued.append(job_id)

    volumes = LibraryVolume.query.filter(
        LibraryVolume.kind == "nas", LibraryVolume.enabled.is_(True)
    ).all()
    for volume in volumes:
        if not volume.ref:
            continue
        try:
            resolved = Path(volume.ref).expanduser().resolve()
        except OSError:
            continue
        key = str(resolved)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        try:
            if not resolved.is_dir():
                continue
        except OSError:
            continue
        if key in active or not _due(volume.last_scan_at, interval):
            continue
        job_id = jobs.enqueue("scan_nas", {"root": str(resolved), "name": volume.name})
        active.add(key)
        queued.append(job_id)

    return queued


def start(app) -> None:
    global _started, _thread
    if _started:
        return
    if app.config.get("TESTING") or not app.config.get("WORKER_ENABLED", True):
        return
    _started = True

    def loop():
        while True:
            try:
                with app.app_context():
                    scan_once(app)
            except Exception:  # noqa: BLE001
                db.session.rollback()
            time.sleep(10)

    _thread = threading.Thread(target=loop, name="avlib-media-watch", daemon=True)
    _thread.start()
