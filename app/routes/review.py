"""人工修正 / 待确认 Blueprint。

页面与 JSON API 都是**同步执行**（单次人工操作数据量很小），不走 Job 队列，
方便页面提交后立刻看到「更新了几个文件 / 挂到哪个 Title」。

路由：
- ``GET  /review``                    待确认分组页面
- ``GET  /api/review/pending``        待确认分组 JSON
- ``POST /api/review/assign``         {raw_key, code, file_ids?, apply_materialize?}
- ``POST /api/review/reassign``       {file_ids, target_code}（合并到已有番号）

不要在 ``app/routes/__init__.py`` 里注册（Lead 统一注册）；蓝图名 ``review``。
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict

from flask import Blueprint, current_app, jsonify, render_template, request, url_for

from app.db import db
from app.models import CodeAlias, Title
from app.services.review import (
    FILE_IDS_LIMIT,
    assign_code,
    pending_items,
    pending_summary,
    pending_volumes,
    reassign_files,
)

logger = logging.getLogger(__name__)

bp = Blueprint("review", __name__)

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


def _body() -> dict:
    return request.get_json(silent=True) or {}


def _app():
    return current_app._get_current_object()


def _limit_arg(default: int = DEFAULT_LIMIT) -> int:
    value = request.args.get("limit", default, type=int)
    if value is None:
        value = default
    return min(max(int(value), 1), MAX_LIMIT)


def _int_arg(name: str, default: int) -> int:
    value = request.args.get(name, default, type=int)
    return default if value is None else int(value)


def _confidence_band(app, name: str | None) -> tuple[int, int, bool, str]:
    """页面置信度筛选 → (min, max, strict, normalized_name)。

    空值/all 保持原兜底语义：低置信度 + 自动未命中；
    其它区间使用严格区间，避免“90+”混入低置信度。
    """
    name = (name or "").strip()
    auto_min = int(app.config.get("CODE_AUTO_MIN", 90) or 90)
    bands = {
        "": (0, auto_min - 1, False, ""),
        "all": (0, auto_min - 1, False, "all"),
        "low": (0, auto_min - 1, True, "low"),
        "0-59": (0, 59, True, "0-59"),
        "60-69": (60, 69, True, "60-69"),
        "70-79": (70, 79, True, "70-79"),
        "80-89": (80, auto_min - 1, True, "80-89"),
        "90+": (auto_min, 100, True, "90+"),
    }
    if name not in bands:
        return bands[""]
    return bands[name]


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
@bp.route("/review")
def page():
    limit = _limit_arg()
    page_no = max(1, _int_arg("page", 1))
    app = _app()
    min_conf, max_conf, conf_strict, confidence = _confidence_band(
        app, request.args.get("confidence")
    )
    volume_raw = (request.args.get("volume") or "").strip()
    volume_id = int(volume_raw) if volume_raw.isdigit() else None
    summary = pending_summary(
        app, min_confidence=min_conf, max_confidence=max_conf,
        confidence_strict=conf_strict, volume_id=volume_id,
    )
    offset = (page_no - 1) * limit
    groups = pending_items(
        app, limit=limit, offset=offset,
        min_confidence=min_conf, max_confidence=max_conf,
        confidence_strict=conf_strict, volume_id=volume_id,
    )
    pages = max(1, math.ceil(summary.pending_groups / max(1, limit))) if summary.pending_groups else 1
    page_no = min(page_no, pages)
    if page_no != (offset // max(1, limit) + 1):
        offset = (page_no - 1) * limit
        groups = pending_items(
            app, limit=limit, offset=offset,
            min_confidence=min_conf, max_confidence=max_conf,
            confidence_strict=conf_strict, volume_id=volume_id,
        )

    def page_url(pno):
        args = request.args.to_dict()
        args["page"] = pno
        return url_for("review.page", **args)

    window = []
    if pages:
        window = sorted({
            1, pages, page_no,
            *range(max(1, page_no - 2), min(pages, page_no + 2) + 1),
        })
    page_range = []
    for index, value in enumerate(window):
        if index and value - window[index - 1] > 1:
            page_range.append(None)
        page_range.append(value)
    aliases = (
        CodeAlias.query.order_by(CodeAlias.id.desc()).limit(30).all()
    )
    title_options = [
        row[0]
        for row in Title.query.order_by(Title.id.desc())
        .with_entities(Title.dvd_id)
        .limit(300)
        .all()
        if row[0]
    ]
    return render_template(
        "review/list.html",
        groups=groups,
        summary=summary,
        aliases=aliases,
        limit=limit,
        page=page_no,
        pages=pages,
        page_range=page_range,
        page_url=page_url,
        args=request.args,
        title_options=title_options,
        file_ids_limit=FILE_IDS_LIMIT,
        confidence=confidence,
        volume=volume_raw if volume_id is not None else "",
        volume_options=pending_volumes(app),
        confidence_options=[
            ("", "全部置信度"),
            ("low", "低置信度（低于自动阈值）"),
            ("0-59", "0–59"),
            ("60-69", "60–69"),
            ("70-79", "70–79"),
            ("80-89", "80–89"),
            ("90+", "90 以上"),
        ],
    )


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------
@bp.route("/api/review/pending")
def api_pending():
    app = _app()
    limit = _limit_arg()
    confidence_arg = request.args.get("confidence")
    if confidence_arg is not None:
        min_confidence, max_confidence, conf_strict, _ = _confidence_band(app, confidence_arg)
    else:
        min_confidence = _int_arg("min_confidence", 0)
        max_confidence = _int_arg("max_confidence", 89)
        conf_strict = False
    volume_raw = (request.args.get("volume") or "").strip()
    volume_id = int(volume_raw) if volume_raw.isdigit() else None
    offset = max(0, _int_arg("offset", 0))
    groups = pending_items(
        app, limit=limit, offset=offset, min_confidence=min_confidence,
        max_confidence=max_confidence, confidence_strict=conf_strict,
        volume_id=volume_id,
    )
    summary = pending_summary(
        app, min_confidence=min_confidence, max_confidence=max_confidence,
        confidence_strict=conf_strict, volume_id=volume_id,
    )
    return jsonify({
        "groups": [group.to_dict() for group in groups],
        "summary": asdict(summary),
        "limit": limit,
        "truncated": summary.pending_groups > len(groups),
    })


@bp.route("/api/review/assign", methods=["POST"])
def api_assign():
    data = _body()
    raw_key = str(data.get("raw_key") or "").strip()
    code = str(data.get("code") or "").strip()
    file_ids = data.get("file_ids")
    if file_ids is not None and not isinstance(file_ids, list):
        return jsonify({"error": "file_ids 必须是数组"}), 400
    if not code:
        return jsonify({"error": "code（目标番号）必填"}), 400
    if not raw_key and not file_ids:
        return jsonify({"error": "raw_key 或 file_ids 至少给一个"}), 400
    if not raw_key and file_ids == []:
        return jsonify({"error": "file_ids 为空"}), 400

    try:
        result = assign_code(
            _app(),
            raw_key,
            code,
            file_ids=[int(fid) for fid in file_ids] if file_ids else None,
            apply_materialize=bool(data.get("apply_materialize", True)),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 - 页面/接口不吐 500 堆栈
        logger.exception("review assign 失败")
        db.session.rollback()
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500
    return jsonify(result)


@bp.route("/api/review/batch-assign", methods=["POST"])
def api_batch_assign():
    data = _body()
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return jsonify({"error": "items 必填（非空数组）"}), 400
    if len(items) > 500:
        return jsonify({"error": "单次最多 500 组"}), 400

    from app.services import jobs

    job_id = jobs.enqueue("review_batch_assign", {"items": items})
    return jsonify({"ok": True, "job_id": job_id}), 202


@bp.route("/api/review/reassign", methods=["POST"])
def api_reassign():
    data = _body()
    file_ids = data.get("file_ids")
    target_code = str(data.get("target_code") or data.get("code") or "").strip()
    if not isinstance(file_ids, list) or not file_ids:
        return jsonify({"error": "file_ids 必填（数组）"}), 400
    if not target_code:
        return jsonify({"error": "target_code（目标番号）必填"}), 400

    try:
        result = reassign_files(
            [int(fid) for fid in file_ids], target_code, app=_app(),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        logger.exception("review reassign 失败")
        db.session.rollback()
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500
    return jsonify(result)
