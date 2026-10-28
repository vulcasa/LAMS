"""备份：创建/列表/校验/下载/删除（合并 v1 backup 页，但修掉 v1 的恢复 bug）。"""
import tempfile
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request, send_from_directory

bp = Blueprint("backup", __name__)


def _service():
    from app.services import backup

    return backup


def _backup_dir():
    app = current_app._get_current_object()
    path = Path(app.config["DATA_DIR"]) / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _full_service():
    from app.services import full_backup

    return full_backup


@bp.route("/backup")
def index():
    backups = []
    full_backups = []
    error = None
    app = current_app._get_current_object()
    try:
        backups = _service().list_backups(app)
        full_backups = _full_service().list_full_backups(app)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    return render_template(
        "backup/index.html", backups=backups, full_backups=full_backups,
        error=error, dir=_backup_dir(),
    )


@bp.route("/api/backup/list")
def api_list():
    try:
        return jsonify({"backups": _service().list_backups(current_app._get_current_object())})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


@bp.route("/api/backup/create", methods=["POST"])
def api_create():
    try:
        result = _service().create_backup(current_app._get_current_object())
        return jsonify(result), (200 if result.get("ok", True) else 500)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@bp.route("/api/backup/verify", methods=["POST"])
def api_verify():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name 必填"}), 400
    try:
        return jsonify(_service().verify_backup(current_app._get_current_object(), name))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@bp.route("/api/backup/delete", methods=["POST"])
def api_delete():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name 必填"}), 400
    try:
        result = _service().delete_backup(current_app._get_current_object(), name)
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@bp.route("/api/backup/full/list")
def api_full_list():
    try:
        return jsonify({"backups": _full_service().list_full_backups(current_app._get_current_object())})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


@bp.route("/api/backup/create-full", methods=["POST"])
def api_create_full():
    try:
        result = _full_service().create_full_backup(current_app._get_current_object())
        return jsonify(result), (200 if result.get("ok", True) else 500)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@bp.route("/api/backup/full/verify", methods=["POST"])
def api_full_verify():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name 必填"}), 400
    try:
        return jsonify(_full_service().verify_full_backup(
            current_app._get_current_object(), name))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@bp.route("/api/backup/full/delete", methods=["POST"])
def api_full_delete():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name 必填"}), 400
    try:
        result = _full_service().delete_full_backup(
            current_app._get_current_object(), name)
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


def _run_restore(zip_path: Path):
    app = current_app._get_current_object()
    return _full_service().restore_full_backup(app, zip_path)


@bp.route("/api/backup/restore", methods=["POST"])
def api_restore():
    """支持 name=已有完整备份，或上传 .zip 文件。"""
    try:
        if "file" in request.files:
            upload = request.files.get("file")
            if upload is None or not upload.filename:
                return jsonify({"ok": False, "error": "未选择备份文件"}), 400
            if not upload.filename.lower().endswith(".zip"):
                return jsonify({"ok": False, "error": "只支持 .zip 完整备份"}), 400
            tmp = tempfile.NamedTemporaryFile(
                prefix=".restore-upload-", suffix=".zip",
                dir=_backup_dir(), delete=False,
            )
            temp_name = Path(tmp.name)
            try:
                upload.save(str(temp_name))
                result = _run_restore(temp_name)
            finally:
                temp_name.unlink(missing_ok=True)
            return jsonify(result)
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"ok": False, "error": "name 必填或上传 zip"}), 400
        app = current_app._get_current_object()
        path = _full_service().full_backup_path(app, name)
        result = _run_restore(path)
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@bp.route("/backup/download/<path:name>")
def download(name):
    # 只允许备份目录内的普通文件名，防目录穿越
    if "/" in name or "\\" in name or name.startswith("."):
        return jsonify({"error": "非法文件名"}), 400
    return send_from_directory(_backup_dir(), name, as_attachment=True)
