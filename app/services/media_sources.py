"""媒体库（挂载点）管理：用户可添加多个 NAS 路径，可选自动扫描。"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import func

from app.config import Config
from app.db import db
from app.models import LibraryFile, LibraryVolume, MediaSource
from app.services import settings


def _normalize_path(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    p = Path(os.path.expanduser(raw))
    try:
        return str(p.resolve())
    except OSError:
        return str(p.absolute())


def _is_smb_mounted(path: str) -> bool:
    try:
        from app.services.smb import _is_mounted

        return _is_mounted(Path(path))
    except Exception:  # noqa: BLE001
        return False


def _source_dict(app, row: MediaSource) -> dict:
    volume = LibraryVolume.query.filter_by(kind=row.kind, ref=row.path).first()
    files = 0
    bytes_ = 0
    if volume is not None:
        files = (
            LibraryFile.query.filter(
                LibraryFile.volume_id == volume.id, LibraryFile.deleted_at.is_(None)
            ).count()
        )
        bytes_ = int(
            db.session.query(func.coalesce(func.sum(LibraryFile.size), 0))
            .filter(
                LibraryFile.volume_id == volume.id, LibraryFile.deleted_at.is_(None)
            )
            .scalar()
            or 0
        )
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "path": row.path,
        "enabled": bool(row.enabled),
        "auto_scan": bool(row.auto_scan),
        "origin": row.origin,
        "last_scan_at": row.last_scan_at.isoformat() if row.last_scan_at else None,
        "last_scan_status": row.last_scan_status,
        "last_error": row.last_error,
        "last_job_id": row.last_job_id,
        "files": files,
        "bytes": bytes_,
        "volume_id": volume.id if volume else None,
        "exists": os.path.isdir(row.path),
        "smb": (
            {
                "host": row.smb_host,
                "share": row.smb_share,
                "username": row.smb_username,
                "mounted": _is_smb_mounted(row.path),
                "has_password": bool(row.smb_password),
            }
            if row.origin == "smb" else None
        ),
    }


def list_sources(app) -> list[dict]:
    rows = MediaSource.query.order_by(MediaSource.id).all()
    return [_source_dict(app, row) for row in rows]


def bootstrap_env_sources(app) -> int:
    """首次运行时把环境变量里的 NAS_ROOTS 导入成媒体库；只做一次。"""
    if settings.get_bool(settings.KEY_SOURCES_BOOTSTRAPPED, False):
        return 0
    created = 0
    for raw in Config.NAS_ROOTS + list(app.config.get("NAS_ROOTS") or []):
        path = _normalize_path(str(raw))
        if not path or MediaSource.query.filter_by(path=path).first():
            continue
        db.session.add(
            MediaSource(
                name=Path(path).name or path,
                kind="nas",
                path=path,
                origin="env",
                auto_scan=True,
            )
        )
        created += 1
    # 已扫描过但还没有媒体库记录的 NAS 根，也补成媒体库（老用户升级友好）
    for vol in LibraryVolume.query.filter_by(kind="nas").all():
        if not vol.ref or MediaSource.query.filter_by(path=vol.ref).first():
            continue
        db.session.add(
            MediaSource(
                name=vol.name or Path(vol.ref).name or vol.ref,
                kind="nas",
                path=vol.ref,
                origin="index",
                auto_scan=False,
                last_scan_at=vol.last_scan_at,
                last_scan_status=vol.last_scan_status or "ok",
            )
        )
        created += 1
    settings.set_bool(settings.KEY_SOURCES_BOOTSTRAPPED, True)
    db.session.commit()
    return created


def add_source(app, *, name: str, path: str) -> dict:
    from app.services import jobs

    raw = (path or "").strip()
    if not raw:
        return {"ok": False, "error": "路径不能为空"}
    if not os.path.isabs(os.path.expanduser(raw)):
        return {"ok": False, "error": "必须填绝对路径（容器内路径，如 /mnt/av2）"}
    norm = _normalize_path(raw)
    if not norm:
        return {"ok": False, "error": "路径无效"}
    if MediaSource.query.filter_by(path=norm).first():
        return {"ok": False, "error": f"该路径已存在：{norm}"}

    exists = os.path.isdir(norm)
    row = MediaSource(
        name=(name or Path(norm).name or norm).strip(),
        kind="nas",
        path=norm,
        enabled=True,
        auto_scan=True,
        origin="manual",
        last_scan_status="added" if exists else "missing",
        last_error=None if exists else f"路径不可访问：{norm}",
    )
    db.session.add(row)
    db.session.commit()

    result = {"ok": True, "source": _source_dict(app, row), "job_id": None}
    if settings.get_preferences(app)["auto_scan_new_source"]:
        if not exists:
            result["warning"] = "路径当前不可访问，未自动扫描；挂载好后再点「扫描」"
        else:
            job_id = jobs.enqueue(
                "scan_nas", {"root": norm, "name": row.name, "source_id": row.id}
            )
            row.last_job_id = job_id
            row.last_scan_status = "queued"
            db.session.commit()
            result["job_id"] = job_id
            result["source"]["last_job_id"] = job_id
            result["source"]["last_scan_status"] = "queued"
    return result


def scan_source(app, source_id: int) -> dict:
    from app.services import jobs

    row = db.session.get(MediaSource, source_id)
    if row is None:
        return {"ok": False, "error": "媒体库不存在"}
    if not os.path.isdir(row.path):
        row.last_scan_status = "missing"
        row.last_error = f"路径不可访问：{row.path}"
        db.session.commit()
        return {"ok": False, "error": row.last_error, "source": _source_dict(app, row)}
    job_id = jobs.enqueue(
        "scan_nas", {"root": row.path, "name": row.name, "source_id": row.id}
    )
    row.last_job_id = job_id
    row.last_scan_status = "queued"
    row.last_error = None
    db.session.commit()
    return {"ok": True, "job_id": job_id, "source": _source_dict(app, row)}


def set_enabled(app, source_id: int, enabled: bool) -> dict:
    row = db.session.get(MediaSource, source_id)
    if row is None:
        return {"ok": False, "error": "媒体库不存在"}
    row.enabled = bool(enabled)
    db.session.commit()
    return {"ok": True, "source": _source_dict(app, row)}


def update_scan_status(app, source_id: int, status: str, *, job_id: int | None = None, error: str | None = None) -> None:
    row = db.session.get(MediaSource, source_id)
    if row is None:
        return
    row.last_scan_status = status
    if job_id is not None:
        row.last_job_id = job_id
    if status == "ok":
        from app.models import utcnow

        row.last_scan_at = utcnow()
        row.last_error = None
    if error:
        row.last_error = str(error)[:1000]
    db.session.commit()


def delete_source(app, source_id: int, *, purge: bool = False) -> dict:
    row = db.session.get(MediaSource, source_id)
    if row is None:
        return {"ok": False, "error": "媒体库不存在"}
    purged_files = 0
    if purge:
        volume = LibraryVolume.query.filter_by(kind=row.kind, ref=row.path).first()
        if volume is not None:
            purged_files = LibraryFile.query.filter_by(volume_id=volume.id).delete()
            db.session.delete(volume)
    name = row.name
    source_id = row.id
    origin = row.origin
    db.session.delete(row)
    db.session.commit()
    if origin == "smb":
        try:
            from pathlib import Path as _Path

            cred = _Path(app.config.get("DATA_DIR", Config.DATA_DIR)) / "smb" / f"source_{source_id}.cred"
            cred.unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True, "deleted": name, "purged_files": purged_files}
