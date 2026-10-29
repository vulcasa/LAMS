"""磁带：状态、挂载/卸载/弹出、操作日志（合并 ltfs-webui 后的入口）。"""
import shutil

from flask import Blueprint, current_app, jsonify, render_template, request
from sqlalchemy import func

from app.db import db
from app.models import LibraryFile, LibraryVolume, TapeOperation

bp = Blueprint("tape", __name__)


def _tape_volumes():
    rows = (
        db.session.query(LibraryVolume, func.count(LibraryFile.id))
        .outerjoin(
            LibraryFile,
            (LibraryFile.volume_id == LibraryVolume.id) & (LibraryFile.deleted_at.is_(None)),
        )
        .filter(LibraryVolume.kind == "tape")
        .group_by(LibraryVolume.id)
        .order_by(LibraryVolume.ref)
        .all()
    )
    return [{"volume": v, "files": n} for v, n in rows]


def _ops(limit=100):
    return (
        TapeOperation.query.order_by(TapeOperation.id.desc()).limit(max(1, min(limit, 500))).all()
    )


@bp.route("/tapes")
def index():
    status = {}
    info = {}
    error = None
    try:
        from app.services.tape_ops import current_status

        status = current_status(current_app._get_current_object()) or {}
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    return render_template(
        "tape/index.html",
        status=status,
        info=info,
        error=error,
        volumes=_tape_volumes(),
        barcode_options=[
            row.ref
            for row in LibraryVolume.query.filter(
                LibraryVolume.kind == "tape",
                LibraryVolume.last_mounted_at.isnot(None),
            ).order_by(LibraryVolume.last_mounted_at.desc()).all()
        ],
        ops=_ops(30),
        adapter=current_app.config.get("TAPE_ADAPTER"),
        device=current_app.config.get("LTFS_DEVICE"),
        mount=str(current_app.config.get("LTFS_MOUNT")),
        ltfs_available=bool(shutil.which("ltfs")),
    )


@bp.route("/api/tape/status")
def api_status():
    try:
        from app.services.tape_ops import current_status

        return jsonify(current_status(current_app._get_current_object()))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@bp.route("/api/tape/mount", methods=["POST"])
def api_mount():
    data = request.get_json(silent=True) or {}
    try:
        from app.services.tape_ops import mount_tape

        result = mount_tape(
            current_app._get_current_object(),
            expected_barcode=(data.get("expected_barcode") or "").strip() or None,
            manual_barcode=(data.get("manual_barcode") or "").strip() or None,
            manual_confirm=bool(data.get("manual_confirm")),
        )
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@bp.route("/api/tape/unmount", methods=["POST"])
def api_unmount():
    try:
        from app.services.tape_ops import unmount_tape

        result = unmount_tape(current_app._get_current_object())
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@bp.route("/api/tape/eject", methods=["POST"])
def api_eject():
    try:
        from app.services.tape_ops import eject_tape

        result = eject_tape(current_app._get_current_object())
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@bp.route("/api/tape/recover/list", methods=["POST"])
def api_recover_list():
    try:
        from app.services.tape_ops import recover_list_tape

        return jsonify(recover_list_tape(current_app._get_current_object()))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": f"{type(exc).__name__}: {exc}", "points": []}), 500


@bp.route("/api/tape/recover", methods=["POST"])
def api_recover():
    data = request.get_json(silent=True) or {}
    try:
        from app.services.tape_ops import recover_tape_index

        raw_generation = data.get("generation")
        generation = str(raw_generation).strip() if raw_generation is not None else None
        result = recover_tape_index(
            current_app._get_current_object(),
            generation=generation or None,
            deep=bool(data.get("deep")),
            confirmed=bool(data.get("confirmed")),
        )
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": f"{type(exc).__name__}: {exc}"}), 500


@bp.route("/api/tape/init", methods=["POST"])
def api_init():
    data = request.get_json(silent=True) or {}
    try:
        from app.services.tape_ops import initialize_tape

        raw_operation_id = data.get("mount_operation_id")
        try:
            mount_operation_id = int(raw_operation_id) if raw_operation_id is not None else None
        except (TypeError, ValueError):
            mount_operation_id = None
        result = initialize_tape(
            current_app._get_current_object(),
            (data.get("barcode") or "").strip(),
            confirmed=bool(data.get("confirmed")),
            force=bool(data.get("force")),
            mount_operation_id=mount_operation_id,
        )
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": f"{type(exc).__name__}: {exc}"}), 500


@bp.route("/api/tape/device")
def api_device():
    try:
        from app.services.tape_ops import device_snapshot

        return jsonify(device_snapshot(current_app._get_current_object()))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@bp.route("/api/tape/ops")
def api_ops():
    limit = request.args.get("limit", 50, type=int)
    return jsonify(
        {
            "ops": [
                {
                    "id": o.id,
                    "op_type": o.op_type,
                    "status": o.status,
                    "barcode": o.barcode,
                    "message": o.message,
                    "started_at": o.started_at.isoformat() if o.started_at else None,
                    "duration_ms": o.duration_ms,
                }
                for o in _ops(limit)
            ]
        }
    )
