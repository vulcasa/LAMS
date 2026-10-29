"""JSON API：任务入队 + 状态查询 + 介质列表。"""
import json

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import func

from app.db import db
from app.models import Job, LibraryFile, LibraryVolume, Title
from app.services import jobs

bp = Blueprint("api", __name__, url_prefix="/api")


def _body() -> dict:
    return request.get_json(silent=True) or {}


def _job_view(job: Job) -> dict:
    def loads(s):
        try:
            return json.loads(s) if s else None
        except Exception:  # noqa: BLE001
            return None

    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "progress": job.progress,
        "total": job.total,
        "message": job.message,
        "error": job.error,
        "result": loads(job.result),
        "payload": loads(job.payload),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


@bp.route("/scan/nas", methods=["POST"])
def scan_nas():
    data = _body()
    root = (data.get("root") or "").strip()
    if not root:
        return jsonify({"error": "root 必填"}), 400
    job_id = jobs.enqueue("scan_nas", {"root": root, "name": data.get("name")})
    return jsonify({"job_id": job_id}), 202


@bp.route("/scan/tape", methods=["POST"])
def scan_tape():
    data = _body()
    mount = (data.get("mount") or str(current_app.config["LTFS_MOUNT"])).strip()
    barcode = (data.get("barcode") or "").strip()
    if not barcode:
        return jsonify({"error": "barcode 必填（先在「磁带」页挂载，或确认当前插入的磁带条码）"}), 400
    job_id = jobs.enqueue(
        "scan_tape",
        {"mount": mount, "barcode": barcode, "expected_barcode": data.get("expected_barcode")},
    )
    return jsonify({"job_id": job_id}), 202


@bp.route("/audit/tape", methods=["POST"])
def audit_tape():
    data = _body()
    barcode = (data.get("barcode") or "").strip()
    if not barcode:
        return jsonify({"error": "barcode 必填"}), 400
    payload = {
        "barcode": barcode,
        "expected_barcode": data.get("expected_barcode") or barcode,
        "write_manifest": data.get("write_manifest", True),
    }
    job_id = jobs.enqueue("audit_tape", payload)
    return jsonify({"job_id": job_id}), 202


@bp.route("/materialize", methods=["POST"])
def materialize():
    data = _body()
    job_id = jobs.enqueue(
        "materialize",
        {"refresh": bool(data.get("refresh")), "limit": data.get("limit")},
    )
    return jsonify({"job_id": job_id}), 202


@bp.route("/javdb/fill", methods=["POST"])
def javdb_fill():
    data = _body()
    job_id = jobs.enqueue("javdb_fill", {"limit": int(data.get("limit") or 100)})
    return jsonify({"job_id": job_id}), 202


@bp.route("/subtitle/sync", methods=["POST"])
def subtitle_sync():
    job_id = jobs.enqueue("subtitle_sync", {})
    return jsonify({"job_id": job_id}), 202


@bp.route("/subtitle/fetch", methods=["POST"])
def subtitle_fetch():
    data = _body()
    codes = [c for c in (data.get("codes") or []) if c]
    if not codes:
        return jsonify({"error": "codes 必填"}), 400
    job_id = jobs.enqueue("subtitle_fetch", {"codes": codes})
    return jsonify({"job_id": job_id}), 202


@bp.route("/covers/fill", methods=["POST"])
def covers_fill():
    data = _body()
    job_id = jobs.enqueue("cover_fill", {"limit": int(data.get("limit") or 30)})
    return jsonify({"job_id": job_id}), 202


@bp.route("/nfo/import", methods=["POST"])
def nfo_import():
    limit = min(max(int((_body().get("limit") or 200)), 1), 2000)
    job_id = jobs.enqueue("nfo_import", {"limit": limit})
    return jsonify({"job_id": job_id}), 202


@bp.route("/video-frames/batch", methods=["POST"])
def video_frames_batch():
    from app.models import Job

    data = _body()
    limit = min(max(int(data.get("limit") or 50), 1), 500)
    active = Job.query.filter(
        Job.kind == "video_frames_batch",
        Job.status.in_(("pending", "running")),
    ).first()
    if active is not None:
        return jsonify({"job_id": active.id, "duplicate": True}), 202
    job_id = jobs.enqueue("video_frames_batch", {"limit": limit})
    return jsonify({"job_id": job_id}), 202


@bp.route("/search/rebuild", methods=["POST"])
def search_rebuild():
    job_id = jobs.enqueue("fts_rebuild", {})
    return jsonify({"job_id": job_id}), 202


@bp.route("/integrity/hash", methods=["POST"])
def integrity_hash():
    data = _body()
    payload = {}
    for key in ("volume_id", "title_id", "limit", "max_videos", "frame_count"):
        if data.get(key) is not None:
            payload[key] = int(data[key])
    payload["verify"] = bool(data.get("verify", True))
    payload["with_frames"] = bool(data.get("with_frames", False))
    job_id = jobs.enqueue("hash_verify", payload)
    return jsonify({"job_id": job_id}), 202


@bp.route("/assets/upscale", methods=["POST"])
def assets_upscale():
    data = _body()
    payload = {}
    if data.get("limit"):
        payload["limit"] = int(data["limit"])
    job_id = jobs.enqueue("assets_upscale", payload)
    return jsonify({"job_id": job_id}), 202


@bp.route("/assets/sync", methods=["POST"])
def assets_sync():
    data = _body()
    job_id = jobs.enqueue(
        "asset_sync",
        {
            "limit": data.get("limit"),
            "codes": data.get("codes"),
            "force": bool(data.get("force")),
        },
    )
    return jsonify({"job_id": job_id}), 202


@bp.route("/rebuild/manifest", methods=["POST"])
def rebuild_manifest():
    data = _body()
    paths = data.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    paths = [p for p in paths if p]
    payload = {"paths": paths}
    if data.get("from_db"):
        payload["from_db"] = True
    job_id = jobs.enqueue("manifest_rebuild", payload)
    return jsonify({"job_id": job_id}), 202


@bp.route("/jobs")
def list_jobs():
    limit = min(max(request.args.get("limit", 20, type=int), 1), 200)
    status = request.args.get("status")
    q = Job.query
    if request.args.get("active") in ("1", "true", "yes"):
        q = q.filter(Job.status.in_(("pending", "running", "paused")))
    elif status:
        q = q.filter(Job.status == status)
    rows = q.order_by(Job.id.desc()).limit(limit).all()
    return jsonify({"jobs": [_job_view(j) for j in rows]})


@bp.route("/jobs/<int:job_id>")
def get_job(job_id):
    job = db.session.get(Job, job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(_job_view(job))


@bp.route("/volumes")
def volumes():
    rows = (
        db.session.query(
            LibraryVolume,
            func.count(LibraryFile.id),
            func.coalesce(func.sum(LibraryFile.size), 0),
        )
        .outerjoin(
            LibraryFile,
            (LibraryFile.volume_id == LibraryVolume.id) & (LibraryFile.deleted_at.is_(None)),
        )
        .group_by(LibraryVolume.id)
        .all()
    )
    return jsonify(
        {
            "volumes": [
                {
                    "id": v.id,
                    "kind": v.kind,
                    "ref": v.ref,
                    "name": v.name,
                    "files": n,
                    "bytes": int(b or 0),
                    "last_scan_at": v.last_scan_at.isoformat() if v.last_scan_at else None,
                    "last_scan_status": v.last_scan_status,
                }
                for v, n, b in rows
            ]
        }
    )


@bp.route("/summary")
def summary():
    def count(model, *where):
        q = db.session.query(func.count(model.id))
        for w in where:
            q = q.filter(w)
        return int(q.scalar() or 0)

    return jsonify(
        {
            "titles": count(Title),
            "files": count(LibraryFile, LibraryFile.deleted_at.is_(None)),
            "with_code": count(
                LibraryFile,
                LibraryFile.deleted_at.is_(None),
                LibraryFile.code_guess.isnot(None),
            ),
            "linked": count(
                LibraryFile,
                LibraryFile.deleted_at.is_(None),
                LibraryFile.title_id.isnot(None),
            ),
            "jobs_pending": count(Job, Job.status == "pending"),
            "jobs_running": count(Job, Job.status == "running"),
        }
    )
