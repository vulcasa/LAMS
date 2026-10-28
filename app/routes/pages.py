"""介质、刮削、设置、健康检查。"""
import json
import mimetypes
from pathlib import Path

from flask import abort, Blueprint, current_app, jsonify, redirect, render_template, request, send_file, url_for
from sqlalchemy import func, or_, text

from app.db import db
from app.models import (
    Actress,
    Genre,
    Job,
    LibraryFile,
    LibraryVolume,
    MediaSource,
    ScrapeTask,
    Subtitle,
    TapeOperation,
    Title,
    utcnow,
)

bp = Blueprint("pages", __name__)


@bp.route("/")
def home():
    return redirect(url_for("library.index"))


def _volume_stats():
    rows = (
        db.session.query(
            LibraryVolume,
            func.count(LibraryFile.id),
            func.coalesce(func.sum(LibraryFile.size), 0),
        )
        .outerjoin(
            LibraryFile,
            (LibraryFile.volume_id == LibraryVolume.id)
            & (LibraryFile.deleted_at.is_(None)),
        )
        .group_by(LibraryVolume.id)
        .order_by(LibraryVolume.kind, LibraryVolume.ref)
        .all()
    )
    return [
        {
            "volume": v,
            "files": n,
            "bytes": b,
        }
        for v, n, b in rows
    ]


@bp.route("/sources")
def sources():
    import os
    import shutil

    from app.services import media_sources, settings as settings_service

    app = current_app._get_current_object()
    media_sources.bootstrap_env_sources(app)
    conf = app.config
    mount = str(conf.get("LTFS_MOUNT"))
    tape_volumes = [row for row in _volume_stats() if row["volume"].kind == "tape"]
    source_list = media_sources.list_sources(app)
    return render_template(
        "pages/sources.html",
        sources=source_list,
        media_count=len(source_list) + len(tape_volumes),
        enabled_count=(
            sum(1 for item in source_list if item.get("enabled"))
            + sum(1 for row in tape_volumes if row["volume"].enabled)
        ),
        prefs=settings_service.get_preferences(app),
        volumes=tape_volumes,
        adapter=conf.get("TAPE_ADAPTER"),
        device=conf.get("LTFS_DEVICE"),
        mount=mount,
        ltfs_available=bool(shutil.which("ltfs")),
        tape_mounted=os.path.ismount(mount),
        browse_roots=conf.get("BROWSE_ROOTS", []),
    )


# ---------------------------------------------------------------------------
# 媒体库（挂载点）API
# ---------------------------------------------------------------------------
@bp.route("/api/sources")
def api_sources():
    from app.services import media_sources

    return jsonify({"sources": media_sources.list_sources(current_app._get_current_object())})


@bp.route("/api/sources", methods=["POST"])
def api_add_source():
    from app.services import media_sources

    data = request.get_json(silent=True) or {}
    result = media_sources.add_source(
        current_app._get_current_object(),
        name=(data.get("name") or "").strip(),
        path=(data.get("path") or "").strip(),
    )
    return jsonify(result), (200 if result.get("ok") else 400)


@bp.route("/api/sources/<int:source_id>/scan", methods=["POST"])
def api_scan_source(source_id):
    from app.services import media_sources

    result = media_sources.scan_source(current_app._get_current_object(), source_id)
    return jsonify(result), (200 if result.get("ok") else 400)


@bp.route("/api/sources/<int:source_id>/enabled", methods=["POST"])
def api_toggle_source(source_id):
    from app.services import media_sources

    data = request.get_json(silent=True) or {}
    result = media_sources.set_enabled(
        current_app._get_current_object(), source_id, bool(data.get("enabled"))
    )
    return jsonify(result), (200 if result.get("ok") else 400)


@bp.route("/api/sources/smb", methods=["POST"])
def api_add_smb_source():
    from app.services import smb

    data = request.get_json(silent=True) or {}
    result = smb.add_smb_source(
        current_app._get_current_object(),
        name=(data.get("name") or "").strip(),
        host=(data.get("host") or "").strip(),
        share=(data.get("share") or "").strip(),
        mount_path=(data.get("mount_path") or "").strip(),
        username=(data.get("username") or "").strip(),
        password=data.get("password") or "",
        options=(data.get("options") or "").strip(),
        auto_scan=bool(data.get("auto_scan", True)),
    )
    return jsonify(result), (200 if result.get("ok") else 400)


@bp.route("/api/sources/<int:source_id>/smb/mount", methods=["POST"])
def api_mount_smb_source(source_id):
    from app.services import smb

    result = smb.mount_source(current_app._get_current_object(), source_id)
    return jsonify(result), (200 if result.get("ok") else 400)


@bp.route("/api/sources/<int:source_id>/smb/unmount", methods=["POST"])
def api_unmount_smb_source(source_id):
    from app.services import smb

    result = smb.unmount_source(current_app._get_current_object(), source_id)
    return jsonify(result), (200 if result.get("ok") else 400)


@bp.route("/api/sources/<int:source_id>", methods=["DELETE"])
def api_delete_source(source_id):
    from app.services import media_sources

    purge = request.args.get("purge") in ("1", "true", "yes")
    result = media_sources.delete_source(
        current_app._get_current_object(), source_id, purge=purge
    )
    return jsonify(result), (200 if result.get("ok") else 400)


# ---------------------------------------------------------------------------
# 活动（Plex 风格）
# ---------------------------------------------------------------------------
KIND_LABELS = {
    "scan_nas": "扫描 NAS",
    "scan_tape": "扫描磁带",
    "audit_tape": "盘点磁带",
    "materialize": "元数据物化",
    "javdb_fill": "JavDB 补缺",
    "asset_sync": "下载封面/剧照",
    "subtitle_sync": "字幕标记",
    "subtitle_fetch": "字幕抓取",
    "transfer": "文件传输",
    "manifest_rebuild": "重建索引",
}
BYTE_KINDS = {"transfer", "asset_sync"}


def _job_activity(job: Job) -> dict:
    started = job.started_at or job.created_at
    elapsed = None
    if started:
        end = job.finished_at or utcnow()
        elapsed = max(0, int((end - started).total_seconds()))
    result = None
    if job.result:
        try:
            result = json.loads(job.result)
        except Exception:  # noqa: BLE001
            result = None
    summary = None
    if isinstance(result, dict):
        for key in ("files_seen", "new", "matched", "titles_created", "filled", "downloaded", "file_count"):
            if key in result:
                summary = f"{key}={result[key]}"
                break
    return {
        "id": job.id,
        "kind": job.kind,
        "label": KIND_LABELS.get(job.kind, job.kind),
        "status": job.status,
        "cancel_requested": bool(job.cancel_requested),
        "progress": job.progress or 0,
        "total": job.total or 0,
        "unit": "bytes" if job.kind in BYTE_KINDS else "items",
        "message": job.message,
        "error": (job.error or "").splitlines()[0] if job.error else None,
        "started_at": started.isoformat() if started else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "elapsed_s": elapsed,
        "summary": summary,
    }


def _job_or_404(job_id):
    job = db.session.get(Job, int(job_id))
    return job


@bp.route("/api/jobs/<int:job_id>/pause", methods=["POST"])
def pause_job(job_id):
    job = _job_or_404(job_id)
    if job is None:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    if job.status != "running":
        return jsonify({"ok": False, "error": "只能暂停运行中的任务"}), 400
    job.status = "paused"
    db.session.commit()
    return jsonify({"ok": True, "status": "paused"})


@bp.route("/api/jobs/<int:job_id>/resume", methods=["POST"])
def resume_job(job_id):
    job = _job_or_404(job_id)
    if job is None:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    if job.status != "paused":
        return jsonify({"ok": False, "error": "只能开始已暂停的任务"}), 400
    job.status = "running"
    db.session.commit()
    return jsonify({"ok": True, "status": "running"})


@bp.route("/api/jobs/<int:job_id>/stop", methods=["POST"])
def stop_job(job_id):
    job = _job_or_404(job_id)
    if job is None:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    if job.status in ("done", "failed", "cancelled"):
        return jsonify({"ok": False, "error": "任务已经结束"}), 400
    if job.status == "pending":
        job.status = "cancelled"
        job.cancel_requested = True
        job.finished_at = utcnow()
        job.error = "用户停止"
        db.session.commit()
        return jsonify({"ok": True, "status": "cancelled"})
    job.cancel_requested = True
    job.message = (job.message or "") + "（正在停止…）"
    db.session.commit()
    return jsonify({"ok": True, "status": job.status, "stopping": True})


@bp.route("/api/monitor")
def api_monitor():
    from app.services.monitor import snapshot

    return jsonify(snapshot())


VIDEO_SUFFIXES = (
    ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".ts", ".flv", ".webm",
)


def _player_entries(limit: int = 500, title_id: int | None = None):
    from sqlalchemy import or_

    video_filter = or_(
        *[LibraryFile.name.ilike(f"%{suffix}") for suffix in VIDEO_SUFFIXES]
    )
    query = (
        db.session.query(LibraryFile, Title, LibraryVolume)
        .join(Title, Title.id == LibraryFile.title_id)
        .join(LibraryVolume, LibraryVolume.id == LibraryFile.volume_id)
        .filter(
            LibraryFile.deleted_at.is_(None),
            LibraryFile.is_sidecar.is_(False),
            LibraryVolume.kind != "tape",
            video_filter,
        )
    )
    if title_id:
        query = query.filter(Title.id == int(title_id)).order_by(LibraryFile.id)
    else:
        query = query.order_by(Title.id.desc(), LibraryFile.id).limit(
            max(1, min(int(limit), 1000))
        )
    rows = query.all()
    entries = []
    for library_file, title, volume in rows:
        # 这里不能逐个 stat：SMB 卡住会把播放页渲染也拖死
        cover = (
            url_for("pages.media", filename=title.cover_local)
            if title.cover_local else None
        )
        entries.append({
            "file_id": library_file.id,
            "title_id": title.id,
            "dvd_id": title.dvd_id,
            "title": title.display_title(),
            "name": library_file.name,
            "rel_path": library_file.rel_path,
            "size": library_file.size or 0,
            "cover": cover,
            "src": url_for("pages.player_source", file_id=library_file.id),
            "mimetype": mimetypes.guess_type(library_file.name)[0] or "video/mp4",
        })
    return entries


@bp.route("/playing")
def player():
    """旧播放器入口仅做兼容重定向到客户端解码播放器。"""
    args = request.args.to_dict(flat=True)
    return redirect(url_for("pages.client_player", **args))


def _recent_play_entries(limit: int = 50):
    from app.models import PlaybackHistory

    rows = (
        PlaybackHistory.query.order_by(PlaybackHistory.last_played_at.desc())
        .limit(max(1, min(int(limit), 50)))
        .all()
    )
    entries = []
    for row in rows:
        title = db.session.get(Title, row.title_id)
        if title is None:
            continue
        cover = url_for("pages.media", filename=title.cover_local) if title.cover_local else None
        entries.append({
            "title_id": title.id,
            "dvd_id": title.dvd_id,
            "title": title.display_title(),
            "cover": cover,
            "url": url_for("pages.client_player", title_id=title.id),
            "last_played_at": row.last_played_at.isoformat() if row.last_played_at else None,
            "play_count": row.play_count,
        })
    return entries


@bp.route("/api/player/session", methods=["POST"])
def player_session():
    from app.services.monitor import monitor
    from app.services.playback import playback_sessions

    data = request.get_json(silent=True) or {}
    try:
        title_id = int(data.get("title_id"))
        file_id = int(data.get("file_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "title_id/file_id 无效"}), 400
    title = db.session.get(Title, title_id)
    item = db.session.get(LibraryFile, file_id)
    if title is None or item is None:
        return jsonify({"ok": False, "error": "影片或文件不存在"}), 404
    playback_sessions.update(
        ip=monitor.client_ip(),
        session_id=str(data.get("session_id") or ""),
        title_id=title.id,
        dvd_id=title.dvd_id,
        title_name=title.display_title(),
        file_id=item.id,
        file_name=item.name,
        position=data.get("position") or 0,
        duration=data.get("duration") or 0,
        paused=bool(data.get("paused")),
    )
    return jsonify({"ok": True})


@bp.route("/api/player/recent", methods=["GET"])
def api_recent_plays():
    return jsonify({"ok": True, "items": _recent_play_entries(50)})


@bp.route("/api/player/recent", methods=["POST"])
def api_record_play():
    from app.models import PlaybackHistory

    data = request.get_json(silent=True) or {}
    try:
        title_id = int(data.get("title_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "title_id 无效"}), 400
    title = db.session.get(Title, title_id)
    if title is None:
        return jsonify({"ok": False, "error": "影片不存在"}), 404
    row = PlaybackHistory.query.filter_by(title_id=title.id).first()
    if row is None:
        row = PlaybackHistory(title_id=title.id, play_count=1)
        db.session.add(row)
    else:
        row.play_count = (row.play_count or 0) + 1
    row.last_played_at = utcnow()
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/client-player")
def client_player():
    title_id = request.args.get("title_id", type=int)
    selected_id = request.args.get("file_id", type=int)
    # 从片库详情进入必须带 title_id；直接点导航进入时不加载全库、不自动播放
    if title_id is None and selected_id is not None:
        selected_file = db.session.get(LibraryFile, selected_id)
        if selected_file is not None:
            title_id = selected_file.title_id
    entries = _player_entries(title_id=title_id) if title_id else []
    selected = next((item for item in entries if item["file_id"] == selected_id), None)
    if selected is None and entries:
        selected = entries[0]
    return render_template(
        "pages/client_player.html",
        entries=entries, selected=selected, total=len(entries), title_id=title_id,
    )


@bp.route("/source/<int:file_id>")
def player_source(file_id):
    item = db.session.get(LibraryFile, file_id)
    if item is None or item.deleted_at is not None:
        abort(404)
    if Path(item.name).suffix.lower() not in VIDEO_SUFFIXES:
        abort(404)
    volume = item.volume
    if volume is None:
        abort(404)
    if (volume.kind or "") == "tape":
        from app.config import Config

        base = Path(current_app.config.get("LTFS_MOUNT", Config.LTFS_MOUNT))
    elif volume.ref:
        base = Path(volume.ref)
    else:
        abort(404)
    try:
        base = base.resolve()
        target = (base / item.rel_path).resolve()
        if target != base and base not in target.parents:
            abort(403)
    except OSError:
        abort(404)
    if not target.is_file():
        abort(404)
    mimetype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    response = send_file(target, conditional=True, mimetype=mimetype)
    response.headers["Accept-Ranges"] = "bytes"
    return response


@bp.route("/activity")
def activity():
    return render_template("pages/activity.html")


@bp.route("/api/activity")
def api_activity():
    running = (
        Job.query.filter(Job.status.in_(("running", "paused")))
        .order_by(Job.id.desc()).limit(20).all()
    )
    queued = Job.query.filter(Job.status == "pending").count()
    recent = (
        Job.query.filter(Job.status.in_(["done", "failed", "cancelled"]))
        .order_by(Job.id.desc())
        .limit(20)
        .all()
    )
    tape_running = (
        TapeOperation.query.filter(TapeOperation.status == "running")
        .order_by(TapeOperation.id.desc())
        .limit(10)
        .all()
    )
    tape_ops = []
    for op in tape_running:
        elapsed = None
        if op.started_at:
            elapsed = max(0, int((utcnow() - op.started_at).total_seconds()))
        tape_ops.append(
            {
                "id": op.id,
                "op_type": op.op_type,
                "barcode": op.barcode,
                "message": op.message,
                "elapsed_s": elapsed,
            }
        )
    sources = MediaSource.query.all()
    from app.services import jobs as jobs_service
    from app.services.playback import active_playback

    return jsonify(
        {
            "ok": True,
            "worker": jobs_service.worker_status(),
            "now": utcnow().isoformat(),
            "running": [_job_activity(j) for j in running],
            "queued": queued,
            "recent": [_job_activity(j) for j in recent],
            "tape_ops": tape_ops,
            "playing": active_playback(45),
            "stats": {
                "titles": Title.query.count(),
                "files": LibraryFile.query.filter(LibraryFile.deleted_at.is_(None)).count(),
                "sources": len(sources),
                "sources_ok": len([s for s in sources if s.last_scan_status == "ok"]),
                "sources_error": len([s for s in sources if s.last_scan_status in ("failed", "missing")]),
            },
        }
    )


@bp.route("/scrape")
def scrape():
    stage_rows = (
        db.session.query(ScrapeTask.stage, ScrapeTask.status, func.count())
        .group_by(ScrapeTask.stage, ScrapeTask.status)
        .all()
    )
    stats = {}
    for stage, status, n in stage_rows:
        stats.setdefault(stage, {})[status] = n
    return render_template(
        "pages/scrape.html",
        stats=stats,
        dump_dir=str(current_app.config.get("DUMP_DIR")),
        dump_url=current_app.config.get("R18_DUMP_URL"),
        titles=db.session.query(func.count(Title.id)).scalar(),
        with_cover=db.session.query(func.count(Title.id))
        .filter(Title.cover_local.isnot(None))
        .scalar(),
        no_zh=db.session.query(func.count(Title.id))
        .filter(Title.title_zh.is_(None))
        .scalar(),
        missing_cover=db.session.query(func.count(Title.id))
        .filter(or_(Title.cover_local.is_(None), func.trim(Title.cover_local) == ""))
        .scalar(),
    )


@bp.route("/settings")
def settings():
    from app.services import settings as settings_service

    conf = current_app.config
    proxy = settings_service.get_proxy(current_app._get_current_object())
    safe = {
        "数据目录": str(conf.get("DATA_DIR")),
        "数据库": str(conf.get("DB_PATH")),
        "媒体目录": str(conf.get("MEDIA_DIR")),
        "r18 导入库": str(conf.get("R18_DUMP_DB")),
        "NAS 根目录": "、".join(conf.get("NAS_ROOTS") or []) or "（未配置）",
        "磁带适配器": conf.get("TAPE_ADAPTER"),
        "LTFS 设备": conf.get("LTFS_DEVICE"),
        "LTFS 挂载点": str(conf.get("LTFS_MOUNT")),
        "文件浏览根": "、".join(conf.get("BROWSE_ROOTS") or []),
        "r18 dump 地址": conf.get("R18_DUMP_URL"),
        "JavDB 限速（秒/部）": conf.get("JAVDB_RATE_SECONDS"),
        "JavDB Cookie": "已配置" if conf.get("JAVDB_COOKIE") else "未配置",
        "自动物化阈值": conf.get("CODE_AUTO_MIN"),
    }
    prefs = settings_service.get_preferences(current_app._get_current_object())
    return render_template(
        "pages/settings.html", conf=safe, proxy=proxy, prefs=prefs
    )


@bp.route("/api/settings")
def api_settings():
    from app.services import settings as settings_service

    return jsonify(settings_service.get_all(current_app._get_current_object()))


@bp.route("/api/settings/proxy", methods=["POST"])
def api_save_proxy():
    from app.services import settings as settings_service

    data = request.get_json(silent=True) or {}
    result = settings_service.save_proxy(
        current_app._get_current_object(),
        enabled=bool(data.get("enabled")),
        host=(data.get("host") or ""),
        port=(data.get("port") or ""),
    )
    return jsonify(result), (200 if result.get("ok") else 400)


@bp.route("/api/settings/proxy/test", methods=["POST"])
def api_test_proxy():
    from app.services import settings as settings_service

    result = settings_service.test_proxy(current_app._get_current_object())
    return jsonify(result), (200 if result.get("ok") else 400)


@bp.route("/api/settings/preferences", methods=["POST"])
def api_save_preferences():
    from app.services import settings as settings_service

    data = request.get_json(silent=True) or {}
    auto_asset_poll = data.get("auto_asset_poll")
    result = settings_service.save_preferences(
        current_app._get_current_object(),
        auto_scan_new_source=bool(data.get("auto_scan_new_source")),
        auto_asset_poll=None if auto_asset_poll is None else bool(auto_asset_poll),
        video_frame_count=(data.get("video_frame_count")
                           if data.get("video_frame_count") is not None else None),
        auto_materialize_after_scan=(
            data.get("auto_materialize_after_scan")
            if data.get("auto_materialize_after_scan") is not None else None
        ),
        video_frame_width=(
            data.get("video_frame_width")
            if data.get("video_frame_width") is not None else None
        ),
        auto_refresh_sources=(
            data.get("auto_refresh_sources")
            if data.get("auto_refresh_sources") is not None else None
        ),
        source_refresh_minutes=(
            data.get("source_refresh_minutes")
            if data.get("source_refresh_minutes") is not None else None
        ),
        smb_auto_refresh=(
            data.get("smb_auto_refresh")
            if data.get("smb_auto_refresh") is not None else None
        ),
    )
    return jsonify(result)


@bp.route("/healthz")
def healthz():
    db_ok = True
    try:
        db.session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False
    return jsonify({"status": "ok" if db_ok else "degraded", "db": db_ok})


@bp.route("/api/stats")
def api_stats():
    return jsonify(
        {
            "titles": db.session.query(func.count(Title.id)).scalar(),
            "files": db.session.query(func.count(LibraryFile.id)).scalar(),
            "volumes": db.session.query(func.count(LibraryVolume.id)).scalar(),
            "actresses": db.session.query(func.count(Actress.id)).scalar(),
            "genres": db.session.query(func.count(Genre.id)).scalar(),
            "subtitles": db.session.query(func.count(Subtitle.id)).scalar(),
            "with_chinese_sub": db.session.query(func.count(Title.id))
            .filter(Title.has_chinese_sub.is_(True))
            .scalar(),
            "no_media": db.session.query(func.count(Title.id))
            .filter(
                ~Title.id.in_(
                    db.session.query(LibraryFile.title_id).filter(
                        LibraryFile.title_id.isnot(None)
                    )
                )
            )
            .scalar(),
        }
    )

@bp.route("/media/<path:filename>")
def media(filename):
    from flask import send_from_directory

    return send_from_directory(current_app.config["MEDIA_DIR"], filename)


@bp.route("/collections")
def collections_page():
    """按顶层文件夹聚合的合集（母带/无码流出等）。"""
    from app.services.collections import list_collections

    items = list_collections(current_app._get_current_object())
    return render_template("pages/collections.html", collections=items)


@bp.route("/collections/<path:name>")
def collection_detail(name):
    from app.services.collections import get_collection

    info, rows = get_collection(current_app._get_current_object(), name)
    if info is None:
        abort(404)
    return render_template("pages/collection_detail.html", collection=info, rows=rows)
