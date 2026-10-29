"""文件：容器/磁带双面板浏览 + 传输（合并 v1 的 file-browser）。"""
import os
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request

from app.services import jobs

bp = Blueprint("files", __name__)


def _roots():
    """容器侧可浏览根（BROWSE_ROOTS + 「介质」页登记的媒体库路径）。"""
    try:
        from app.services.transfer import container_roots

        return [str(p) for p in container_roots(current_app._get_current_object())]
    except Exception:  # noqa: BLE001
        return [str(p) for p in current_app.config.get("BROWSE_ROOTS", [])]


def _tape_mount() -> Path:
    return Path(current_app.config.get("LTFS_MOUNT"))


def _mounted() -> bool:
    """与传输服务共用同一挂载判定，避免两个页面结论不一致。"""
    try:
        from app.services.transfer import tape_is_mounted

        return bool(tape_is_mounted(current_app._get_current_object()))
    except Exception:  # noqa: BLE001
        return os.path.ismount(_tape_mount())


def _within(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except Exception:  # noqa: BLE001
        return False


def _side_base(side: str) -> Path | None:
    if side == "tape":
        return _tape_mount()
    roots = _roots()
    if not roots:
        return None
    return Path(roots[0])


def _safe_browse_path(side: str, raw: str) -> tuple[Path | None, str | None]:
    """返回 (绝对路径, 错误信息)。所有访问必须落在允许的根内。"""
    if side not in ("container", "tape"):
        return None, "side 只能是 container 或 tape"
    raw = (raw or "").strip()
    if side == "tape":
        base = _tape_mount()
        if not _mounted():
            return None, f"磁带未挂载（{base}）"
        target = Path(raw) if raw else base
        if not _within(target, base):
            return None, "路径越界：只能浏览磁带挂载点内"
        return target, None

    roots = [Path(r) for r in _roots()]
    if not roots:
        return None, "未配置 AVLIB_BROWSE_ROOTS"
    target = Path(raw) if raw else roots[0]
    if not any(_within(target, r) for r in roots):
        return None, f"路径越界：只允许 {', '.join(map(str, roots))}"
    return target, None


def _listing(path: Path):
    entries = []
    try:
        names = sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError as exc:
        return None, f"无法读取 {path}: {exc}"
    for entry in names:
        if entry.name.startswith("."):
            continue
        try:
            st = entry.stat()
            is_dir = entry.is_dir()
        except OSError:
            continue
        entries.append(
            {
                "name": entry.name,
                "path": str(Path(entry.path)),
                "is_dir": is_dir,
                "size": 0 if is_dir else st.st_size,
                "mtime": st.st_mtime,
            }
        )
    return entries, None


@bp.route("/files")
def index():
    return render_template(
        "files/index.html",
        roots=_roots(),
        tape_mount=str(_tape_mount()),
        tape_mounted=_mounted(),
    )


@bp.route("/api/files/browse")
def api_browse():
    side = request.args.get("side", "container")
    raw = request.args.get("path", "")
    path, err = _safe_browse_path(side, raw)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    entries, err = _listing(path)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    parent = None
    if side == "tape":
        base = _tape_mount()
        parent = str(path.parent) if path.resolve() != base.resolve() else None
    else:
        base = next((Path(r) for r in _roots() if _within(path, Path(r))), None)
        parent = str(path.parent) if base and path.resolve() != base.resolve() else None
    return jsonify(
        {
            "ok": True,
            "side": side,
            "path": str(path),
            "parent": parent,
            "entries": entries,
            "mounted": _mounted() if side == "tape" else None,
        }
    )


@bp.route("/api/files/transfer", methods=["POST"])
def api_transfer():
    data = request.get_json(silent=True) or {}
    direction = data.get("direction")
    sources = data.get("sources") or []
    target = (data.get("target") or "").strip()
    move = bool(data.get("move"))
    if direction not in ("container_to_tape", "tape_to_container"):
        return jsonify({"error": "direction 非法"}), 400
    if not sources or not target:
        return jsonify({"error": "sources / target 必填"}), 400
    job_id = jobs.enqueue(
        "transfer",
        {"direction": direction, "sources": sources, "target": target, "move": move},
    )
    return jsonify({"job_id": job_id}), 202


@bp.route("/api/files/transfers")
def api_transfers():
    from app.models import Transfer, utcnow

    rows = Transfer.query.order_by(Transfer.id.desc()).limit(50).all()
    now = utcnow()
    transfers = []
    for t in rows:
        total = int(t.total_bytes or 0)
        done = int(t.done_bytes or 0)
        pct = (100.0 * done / total) if total > 0 else (100.0 if t.status == "done" else 0.0)
        pct = max(0.0, min(100.0, pct))
        elapsed_s = None
        eta_s = None
        if t.started_at is not None:
            try:
                elapsed_s = max(0.0, (now - t.started_at).total_seconds())
            except TypeError:
                elapsed_s = None
        if t.status == "running" and elapsed_s and done > 0 and total > done:
            rate = done / elapsed_s
            if rate > 0:
                eta_s = (total - done) / rate
        elif t.status == "done":
            eta_s = 0.0
        transfers.append({
            "id": t.id,
            "direction": t.direction,
            "move": t.move,
            "status": t.status,
            "file_count": t.file_count,
            "total_bytes": total,
            "done_bytes": done,
            "percent": round(pct, 1),
            "elapsed_s": round(elapsed_s, 1) if elapsed_s is not None else None,
            "eta_s": round(eta_s, 1) if eta_s is not None else None,
            "current_file": t.current_file,
            "error": t.error,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "finished_at": t.finished_at.isoformat() if t.finished_at else None,
        })
    return jsonify({"transfers": transfers})
