"""操作日志：磁带操作 + 后台任务（合并 v1 的 logs 页）。"""
from flask import Blueprint, jsonify, render_template, request

from app.db import db
from app.models import Job, SmbOperation, TapeOperation

bp = Blueprint("logs", __name__)


@bp.route("/logs")
def index():
    op_type = request.args.get("type") or ""
    status = request.args.get("status") or ""
    q = TapeOperation.query
    if op_type:
        q = q.filter(TapeOperation.op_type == op_type)
    if status:
        q = q.filter(TapeOperation.status == status)
    ops = q.order_by(TapeOperation.id.desc()).limit(200).all()

    jq = Job.query
    if status:
        jq = jq.filter(Job.status == status)
    job_list = jq.order_by(Job.id.desc()).limit(1000).all()

    smb_q = SmbOperation.query
    if op_type in ("add", "mount", "unmount"):
        smb_q = smb_q.filter(SmbOperation.action == op_type)
    elif op_type:
        smb_q = smb_q.filter(SmbOperation.id < 0)
    if status:
        smb_q = smb_q.filter(SmbOperation.status == status)
    smb_ops = smb_q.order_by(SmbOperation.id.desc()).limit(100).all()

    types = {
        r[0]
        for r in db.session.query(TapeOperation.op_type).distinct().order_by(TapeOperation.op_type)
    }
    types.update({"add", "mount", "unmount"})
    return render_template(
        "logs/index.html",
        ops=ops,
        smb_ops=smb_ops,
        jobs=job_list,
        types=sorted(types),
        args=request.args,
    )


@bp.route("/api/logs/tape")
def api_tape_logs():
    limit = max(1, min(request.args.get("limit", 100, type=int), 1000))
    rows = TapeOperation.query.order_by(TapeOperation.id.desc()).limit(limit).all()
    return jsonify(
        {
            "ops": [
                {
                    "id": o.id,
                    "op_type": o.op_type,
                    "status": o.status,
                    "barcode": o.barcode,
                    "message": o.message,
                    "stdout": (o.stdout or "")[:2000],
                    "stderr": (o.stderr or "")[:2000],
                    "started_at": o.started_at.isoformat() if o.started_at else None,
                    "finished_at": o.finished_at.isoformat() if o.finished_at else None,
                    "duration_ms": o.duration_ms,
                }
                for o in rows
            ]
        }
    )
