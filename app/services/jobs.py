"""后台任务队列 + 单 worker 线程。

设计：
- Job 行落库，重启不丢；worker 用 `UPDATE ... WHERE status='pending'` 原子领取，
  所以 gunicorn 多 worker / 多线程也不会重复执行同一条。
- 具体业务函数在 handler 里**延迟 import**，服务模块还没就位时应用也能启动。
- 开发/单容器：create_app() 起一个守护线程；生产也可用 scripts/worker.py 独立进程。
"""
import json
import logging
import threading
import time
import traceback
from dataclasses import asdict, is_dataclass

from sqlalchemy import text

from app.db import db
from app.models import Job, utcnow

log = logging.getLogger(__name__)

_handlers = {}
_started = False


class JobCancelled(Exception):
    """用户点了停止；handler 在下一个进度点中止。"""
_stop = threading.Event()
_threads: list[threading.Thread] = []
_thread = None
_last_beat = 0.0
_AUTO_LOCK = threading.Lock()
_TAPE_JOB_LOCK = threading.RLock()


def register(kind):
    def deco(fn):
        _handlers[kind] = fn
        return fn

    return deco


ASSET_JOB_KINDS = ("cover_fill", "asset_sync")
_last_auto_assets_at = 0.0


def maybe_auto_assets(app, *, force: bool = False):
    """自动轮询：有缺失封面/剧照且队列空闲时，入队下一批 cover_fill。

    - 设置页关闭自动轮询则不做事；
    - 已有资产任务 pending/running 时不重复入队；
    - ``force=True`` 用于「一批刚结束」，立即接下一批（仍要求有活可干）。
    返回新 job id 或 None。不抛异常（worker 循环调用）。
    """
    global _last_auto_assets_at
    if not _AUTO_LOCK.acquire(blocking=False):
        return None
    try:
        from app.services.assets import missing_assets_summary
        from app.services.settings import KEY_AUTO_ASSETS, get_bool

        if not get_bool(KEY_AUTO_ASSETS, True):
            return None
        interval = float(app.config.get("ASSET_AUTO_POLL_SECONDS", 60) or 60)
        now = time.monotonic()
        if not force and now - _last_auto_assets_at < interval:
            return None
        # 只和“资产任务”去重：自动封面/剧照可以排在抽帧、物化等其它任务之后，
        # 但不能因为长时间抽帧导致永远不入队。新任务 id 更大，不会插到用户任务前面。
        active_asset = Job.query.filter(
            Job.kind.in_(ASSET_JOB_KINDS),
            Job.status.in_(("pending", "running")),
        ).first()
        if active_asset is not None:
            return None
        summary = missing_assets_summary(app)
        if int(summary.get("actionable") or 0) <= 0:
            return None
        batch = int(app.config.get("ASSET_AUTO_BATCH", 15) or 15)
        job_id = enqueue("cover_fill", {"limit": batch, "auto": True})
        _last_auto_assets_at = now
        log.info(
            "auto assets: 入队 cover_fill#%s（batch=%s, actionable=%s）",
            job_id, batch, summary.get("actionable"),
        )
        return job_id
    except Exception:  # noqa: BLE001
        log.exception("自动轮询封面/剧照失败")
        return None
    finally:
        _AUTO_LOCK.release()


def enqueue(kind: str, payload: dict | None = None) -> int:
    job = Job(kind=kind, payload=json.dumps(payload or {}, ensure_ascii=False), status="pending")
    db.session.add(job)
    db.session.commit()
    return job.id


def _claim_next():
    row = db.session.execute(
        text("SELECT id FROM job WHERE status='pending' ORDER BY id LIMIT 1")
    ).fetchone()
    if not row:
        return None
    job_id = row[0]
    res = db.session.execute(
        text(
            "UPDATE job SET status='running', started_at=:t "
            "WHERE id=:id AND status='pending'"
        ),
        {"t": utcnow(), "id": job_id},
    )
    db.session.commit()
    if res.rowcount != 1:
        return None
    return db.session.get(Job, job_id)


def _set_total(job_id: int, total: int) -> None:
    try:
        db.session.execute(
            text("UPDATE job SET total=:t WHERE id=:id"), {"t": int(total), "id": job_id}
        )
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()


def _job_control(job_id: int):
    """读取控制状态；独立连接避免 handler 未提交事务影响。"""
    try:
        with db.engine.connect() as conn:
            return conn.execute(
                text("SELECT status, cancel_requested FROM job WHERE id=:id"),
                {"id": job_id},
            ).fetchone()
    except Exception:  # noqa: BLE001
        return None


def _job_progress(job_id: int, done: int, message: str = ""):
    while True:
        row = _job_control(job_id)
        if row is None:
            return
        if bool(row[1]) or row[0] == "cancelled":
            raise JobCancelled
        if row[0] == "paused":
            time.sleep(0.6)
            continue
        break
    _progress(job_id, done, message)


def _progress(job_id: int, done: int, message: str = ""):
    for attempt in range(4):
        try:
            db.session.execute(
                text("UPDATE job SET progress=:p, message=:m WHERE id=:id"),
                {"p": int(done or 0), "m": message, "id": job_id},
            )
            db.session.commit()
            return
        except Exception:  # noqa: BLE001 - 并发写锁，进度不是关键路径
            db.session.rollback()
            if attempt == 3:
                return
            time.sleep(0.2 * (attempt + 1))


def _plain(obj):
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, (list, tuple)):
        return [_plain(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    return obj


def run_job(job: Job, app):
    kind = job.kind
    payload = json.loads(job.payload or "{}")
    handler = _handlers.get(kind)
    if handler is None:
        _finish(job.id, "failed", error=f"未知任务类型: {kind}")
        return
    try:
        result = handler(app, job.id, payload, lambda done, msg="": _job_progress(job.id, done, msg))
        if (lambda r: bool(r) and bool(r[1]))(_job_control(job.id)):
            raise JobCancelled
        plain = _plain(result)
        # 业务函数可以返回 {"ok": False, ...} 表示"跑完了但没成功"（如盘点挂载失败）
        if isinstance(plain, dict) and plain.get("ok") is False:
            _finish(
                job.id,
                "failed",
                result=plain,
                error=str(plain.get("message") or plain.get("error") or "任务未成功"),
            )
        else:
            _finish(job.id, "done", result=plain)
    except JobCancelled:
        log.info("job %s (%s) cancelled by user", job.id, kind)
        _finish(job.id, "cancelled", error="用户停止")
    except Exception as exc:  # noqa: BLE001
        log.exception("job %s (%s) failed", job.id, kind)
        _finish(job.id, "failed", error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-3000:]}")


def _finish(job_id: int, status: str, *, result=None, error: str | None = None):
    # SQLite 并发写偶发 database is locked：重试，避免已完成的任务卡在 running
    for attempt in range(5):
        job = db.session.get(Job, job_id)
        if job is None:
            return
        job.status = status
        job.finished_at = utcnow()
        if result is not None:
            job.result = json.dumps(result, ensure_ascii=False, default=str)
        if error:
            job.error = error
        try:
            db.session.commit()
            return
        except Exception:  # noqa: BLE001
            db.session.rollback()
            if attempt == 4:
                log.exception("写任务终态失败 job=%s", job_id)
                return
            time.sleep(0.3 * (attempt + 1))


# ---------------------------------------------------------------------------
# handlers：延迟 import 业务模块
# ---------------------------------------------------------------------------
def _maybe_enqueue_materialize(app) -> int | None:
    """扫描/盘点后自动物化（可在设置关闭）；已有物化任务则不重复入队。"""
    try:
        from app.services.settings import KEY_AUTO_MATERIALIZE, get_bool

        if not get_bool(KEY_AUTO_MATERIALIZE, True):
            return None
        active = Job.query.filter(
            Job.kind == "materialize", Job.status.in_(("pending", "running"))
        ).first()
        if active is not None:
            return None
        return enqueue("materialize", {"auto": True})
    except Exception:  # noqa: BLE001
        log.exception("自动物化入队失败")
        return None


@register("scan_nas")
def _scan_nas(app, job_id, payload, progress):
    from app.services import media_sources
    from app.services.scanner import scan_nas

    source_id = payload.get("source_id")
    if source_id:
        media_sources.update_scan_status(app, int(source_id), "running", job_id=job_id)
    try:
        stats = scan_nas(app, payload["root"], volume_name=payload.get("name"), progress=progress)
    except Exception as exc:
        if source_id:
            media_sources.update_scan_status(
                app, int(source_id), "failed", job_id=job_id, error=f"{type(exc).__name__}: {exc}"
            )
        raise
    if source_id:
        media_sources.update_scan_status(app, int(source_id), "ok", job_id=job_id)
    try:
        from app.services.nfo import apply_nfo_for_volume

        apply_nfo_for_volume(app, stats.volume_id)
    except Exception:  # noqa: BLE001 - NFO 失败不影响扫描
        log.exception("导入 NFO 失败")
    materialize_job = _maybe_enqueue_materialize(app) if getattr(stats, "with_code", 0) else None
    if materialize_job is not None:
        progress(100, f"扫描完成，已自动入队物化 job #{materialize_job}")
    return stats


@register("scan_tape")
def _scan_tape(app, job_id, payload, progress):
    from app.services.scanner import scan_tape

    with _TAPE_JOB_LOCK:
        stats = scan_tape(
            app,
            payload["mount"],
            barcode=payload["barcode"],
            expected_barcode=payload.get("expected_barcode"),
            progress=progress,
        )
    try:
        from app.services.nfo import apply_nfo_for_volume

        apply_nfo_for_volume(app, stats.volume_id)
    except Exception:  # noqa: BLE001
        log.exception("导入 NFO 失败")
    if getattr(stats, "with_code", 0):
        _maybe_enqueue_materialize(app)
    return stats


@register("materialize")
def _materialize(app, job_id, payload, progress):
    from app.config import Config
    from app.services.materialize import materialize

    result = materialize(
        app,
        dump_db_path=str(Config.R18_DUMP_DB),
        min_confidence=payload.get("min_confidence"),
        limit=payload.get("limit"),
        refresh=bool(payload.get("refresh")),
        on_total=lambda total: _set_total(job_id, total),
        progress=progress,
    )
    try:
        from app.services.search import rebuild_title_fts

        rebuild_title_fts(app)
    except Exception:  # noqa: BLE001 - 搜索索引失败不影响物化
        pass
    try:
        from app.routes.library import _clear_cache

        _clear_cache()
    except Exception:  # noqa: BLE001
        pass
    return result


@register("audit_tape")
def _audit_tape(app, job_id, payload, progress):
    from app.services.audit import run_audit
    from app.services.tape_adapter import get_adapter
    from app.services.tape_ops import acquire_drive, release_drive

    with _TAPE_JOB_LOCK:
        if not acquire_drive():
            return {
                "ok": False,
                "busy": True,
                "message": "另一个磁带操作正在进行（挂载/卸载/弹出/盘点），盘点已跳过",
            }
        try:
            report = run_audit(
                app,
                payload["barcode"],
                expected_barcode=payload.get("expected_barcode"),
                adapter=get_adapter(app.config, app=app),
                write_manifest=payload.get("write_manifest", True),
                eject=False,
                progress=progress,
            )
            if report.get("files_seen"):
                _maybe_enqueue_materialize(app)
            return report
        finally:
            release_drive()


@register("javdb_fill")
def _javdb_fill(app, job_id, payload, progress):
    from app.services.javdb import fill_gaps

    result = fill_gaps(app, limit=payload.get("limit", 100), progress=progress)
    try:
        from app.routes.library import _clear_cache

        _clear_cache()
    except Exception:  # noqa: BLE001
        pass
    return result


@register("transfer")
def _transfer(app, job_id, payload, progress):
    from app.services.transfer import run_transfer

    with _TAPE_JOB_LOCK:
        return _transfer_locked(app, payload, progress)


def _transfer_locked(app, payload, progress):
    from app.services.transfer import run_transfer

    def cb(done, current=""):
        progress(int(done or 0), current)

    return run_transfer(
        app,
        payload["direction"],
        payload["sources"],
        payload["target"],
        move=bool(payload.get("move")),
        progress=cb,
    )


@register("review_batch_assign")
def _review_batch_assign(app, job_id, payload, progress):
    from app.services.review import assign_code

    items = payload.get("items") or []
    total = len(items)
    _set_total(job_id, total)
    updated = 0
    errors = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            errors.append(f"第 {index} 项格式错误")
            continue
        raw_key = str(item.get("raw_key") or "").strip()
        code = str(item.get("code") or "").strip()
        file_ids = item.get("file_ids")
        if not code:
            errors.append(f"第 {index} 项目标番号为空")
            continue
        if file_ids is not None and not isinstance(file_ids, list):
            errors.append(f"第 {index} 项 file_ids 不是数组")
            continue
        try:
            assign_code(
                app, raw_key, code,
                file_ids=[int(fid) for fid in file_ids] if file_ids else None,
                apply_materialize=False,
            )
            updated += 1
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            errors.append(f"{raw_key or code}: {type(exc).__name__}: {exc}")
        progress(index, f"批量指派 {index}/{total}")
    materialize_job = _maybe_enqueue_materialize(app) if updated else None
    return {
        "ok": True,
        "updated": updated,
        "errors": errors,
        "materialize_job_id": materialize_job,
    }


@register("nfo_import")
def _nfo_import(app, job_id, payload, progress):
    from app.services.nfo import apply_nfo_for_all

    progress(0, "读取 NFO…")
    stats = apply_nfo_for_all(app, limit=payload.get("limit"))
    materialize_job = _maybe_enqueue_materialize(app) if stats.get("titles") else None
    return {**stats, "materialize_job_id": materialize_job}


@register("fts_rebuild")
def _fts_rebuild(app, job_id, payload, progress):
    from app.services.search import rebuild_title_fts

    return rebuild_title_fts(app)


@register("hash_verify")
def _hash_verify(app, job_id, payload, progress):
    from app.models import LibraryVolume
    from app.services.integrity import hash_library_files

    volume_id = payload.get("volume_id")
    volume = db.session.get(LibraryVolume, int(volume_id)) if volume_id else None
    if volume is not None and (volume.kind or "") == "tape":
        with _TAPE_JOB_LOCK:
            return _hash_verify_run(app, job_id, payload, progress, hash_library_files)
    return _hash_verify_run(app, job_id, payload, progress, hash_library_files)


def _hash_verify_run(app, job_id, payload, progress, hash_library_files):
    return hash_library_files(
        app,
        volume_id=payload.get("volume_id"),
        title_id=payload.get("title_id"),
        limit=payload.get("limit"),
        verify=bool(payload.get("verify", True)),
        with_frames=bool(payload.get("with_frames", False)),
        max_videos=payload.get("max_videos"),
        frame_count=payload.get("frame_count"),
        on_total=lambda total: _set_total(job_id, total),
        progress=progress,
    )


@register("video_frames_batch")
def _video_frames_batch(app, job_id, payload, progress):
    from app.models import Asset, FrameFailure, LibraryFile
    from app.services.video_frames import (
        extract_frames, frame_count_setting, titles_missing_screenshots,
    )

    def record_failures(title_id, failed_files):
        if not failed_files:
            return
        for file_id, error in failed_files.items():
            row = FrameFailure.query.filter_by(file_id=int(file_id)).first()
            if row is None:
                row = FrameFailure(
                    file_id=int(file_id), title_id=int(title_id),
                    path=None, error=str(error)[:1000],
                )
                db.session.add(row)
            else:
                row.error = str(error)[:1000]
                row.failed_at = utcnow()
                row.fail_count = int(row.fail_count or 0) + 1
            source = db.session.get(LibraryFile, int(file_id))
            if source is not None:
                row.path = source.rel_path
        db.session.commit()

    # 已有另一个批量截图任务在跑时不重复开批，避免同一批影片被生成两次
    active = (
        Job.query.filter(
            Job.kind == "video_frames_batch",
            Job.status.in_(("pending", "running")),
            Job.id != job_id,
        ).first()
    )
    if active is not None:
        return {"ok": True, "skipped": True, "message": f"已有批量截图任务 #{active.id} 在运行"}
    limit = int(payload.get("limit") or 50)
    count = frame_count_setting(app, payload.get("frame_count"))
    titles = titles_missing_screenshots(app, limit=limit)
    _set_total(job_id, len(titles))
    frames = 0
    errors = []
    failed_titles = 0
    done = 0
    for title in titles:
        # 任务开始后可能已被其它任务生成截图；逐部再确认，绝不重复生成
        if Asset.query.filter_by(title_id=title.id, kind="screenshot").count() > 0:
            done += 1
            progress(done, f"跳过已有截图 {done}/{len(titles)} · {title.dvd_id}")
            continue
        done += 1
        result = extract_frames(app, title.id, count=count, allow_tape=False)
        frames += int(result.get("frames") or 0)
        failed_files = result.get("failed_files") or {}
        if failed_files:
            record_failures(title.id, failed_files)
            failed_titles += 1
        if result.get("errors"):
            errors.extend(f"{title.dvd_id}: {err}" for err in result["errors"][:2])
        progress(done, f"批量截图 {done}/{len(titles)} · {title.dvd_id} · {frames} 张")

    next_job = None
    # 单文件错误只记录并跳过；只要本批有成功帧且还有剩余标题，就继续下一批。
    # 全部失败（frames=0）或系统性错误时停止，避免无限循环。
    if frames > 0:
        has_more = bool(titles_missing_screenshots(app, limit=1))
        if has_more:
            next_job = enqueue("video_frames_batch", {"limit": limit})
    return {
        "ok": True,
        "titles": len(titles),
        "frames": frames,
        "failed_titles": failed_titles,
        "errors": errors[:100],
        "next_job_id": next_job,
    }


@register("video_frames")
def _video_frames(app, job_id, payload, progress):
    from app.services.video_frames import extract_frames, frame_count_setting

    count = frame_count_setting(app, payload.get("frame_count"))
    return extract_frames(
        app, int(payload["title_id"]), count=count,
        width=payload.get("frame_width"),
        on_total=lambda total: _set_total(job_id, total),
        progress=progress,
    )


@register("assets_upscale")
def _assets_upscale(app, job_id, payload, progress):
    from app.services.assets import upscale_existing_stills

    return upscale_existing_stills(
        app, limit=payload.get("limit"), progress=progress
    )


@register("cover_fill")
def _cover_fill(app, job_id, payload, progress):
    """缺封面补齐：先 JavDB 补封面 URL（仅缺 cover_url 的），再下载本地缺图。"""
    from app.services.assets import download_for_titles
    from app.services.javdb import fill_gaps, refresh_covers

    limit = int(payload.get("limit") or 30)
    javdb_stats = fill_gaps(
        app, limit=limit, only_missing_cover=True, progress=progress
    )
    # DMM 占位图导致的缺封面：重新抓 JavDB 封面 URL 再下载一次
    cover_stats = refresh_covers(app, limit=limit, progress=progress)
    codes = []
    if cover_stats.get("updated"):
        from app.models import Title
        from app.db import db

        rows = (
            db.session.query(Title.dvd_id)
            .filter(
                Title.cover_local.is_(None),
                Title.cover_url.isnot(None),
                Title.cover_url != "",
            )
            .order_by(Title.id)
            .limit(limit)
            .all()
        )
        codes = [row[0] for row in rows]
    assets_stats = download_for_titles(
        app, limit=limit, only_codes=codes or None, progress=progress
    )
    return {"javdb": javdb_stats, "covers": cover_stats, "assets": assets_stats}


@register("asset_sync")
def _asset_sync(app, job_id, payload, progress):
    from app.services.assets import download_for_titles

    return download_for_titles(
        app,
        limit=payload.get("limit"),
        only_codes=payload.get("codes"),
        force=bool(payload.get("force")),
        progress=progress,
    )


@register("manifest_rebuild")
def _manifest_rebuild(app, job_id, payload, progress):
    from app.services.rebuild import rebuild_from_db, rebuild_from_manifests

    if payload.get("from_db"):
        return rebuild_from_db(app, progress=progress)
    return rebuild_from_manifests(app, payload.get("paths") or None, progress=progress)


@register("subtitle_sync")
def _subtitle_sync(app, job_id, payload, progress):
    from app.services.subtitle import sync_filename_flags

    return sync_filename_flags(app)


@register("subtitle_fetch")
def _subtitle_fetch(app, job_id, payload, progress):
    from app.models import Title
    from app.services.subtitle import fetch_best

    codes = payload.get("codes") or []
    out = {"found": 0, "missed": 0}
    for i, code in enumerate(codes, 1):
        title = Title.query.filter_by(dvd_id=code).first()
        if not title:
            out["missed"] += 1
            continue
        sub = fetch_best(app, title)
        if sub:
            out["found"] += 1
        else:
            out["missed"] += 1
        progress(i, f"{code}")
    return out


# ---------------------------------------------------------------------------
# worker 线程
# ---------------------------------------------------------------------------
def recover_stale_jobs(app) -> int:
    """服务重启后把遗留的 running 任务标记为中断，避免活动页永远显示进度。"""
    try:
        rows = Job.query.filter(Job.status.in_(("running", "paused"))).all()
        for job in rows:
            job.status = "cancelled" if job.cancel_requested else "failed"
            job.finished_at = utcnow()
            job.error = "服务重启，任务已中断"
        if rows:
            db.session.commit()
        return len(rows)
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.exception("恢复遗留任务失败")
        return 0


def start_worker(app):
    global _started, _threads, _thread
    if _started:
        return
    if not app.config.get("WORKER_ENABLED", True):
        log.info("worker disabled by config")
        return
    poll = float(app.config.get("WORKER_POLL_SECONDS", 1.5))
    threads_count = max(1, min(int(app.config.get("WORKER_THREADS", 3) or 3), 8))
    with app.app_context():
        recovered = recover_stale_jobs(app)
        if recovered:
            log.warning("恢复遗留任务：%s 个 running 已标记为中断", recovered)
    _started = True

    def loop(name):
        global _last_beat

        while not _stop.is_set():
            _last_beat = time.monotonic()
            try:
                with app.app_context():
                    job = _claim_next()
                    if job is not None:
                        run_job(job, app)
                        # 自动封面只在队列空闲时补批，避免挡住用户手动任务
                        maybe_auto_assets(app, force=job.kind in ASSET_JOB_KINDS)
                        continue
                    maybe_auto_assets(app)
            except Exception:  # noqa: BLE001
                log.exception("worker loop error (%s)", name)
            time.sleep(poll)

    _threads = []
    for index in range(threads_count):
        thread_name = f"avlib-worker-{index + 1}"
        thread = threading.Thread(
            target=loop, args=(thread_name,), name=thread_name, daemon=True
        )
        thread.start()
        _threads.append(thread)
    _thread = _threads[0]
    try:
        from app.services import media_watch

        media_watch.start(app)
    except Exception:  # noqa: BLE001
        log.exception("启动媒体库变动监测失败")
    log.info("worker threads started: %s (poll=%.1fs)", threads_count, poll)


def stop_worker():
    _stop.set()


def worker_status() -> dict:
    """给「活动」页/自检用的 worker 存活信息。"""
    alive = bool(_threads and any(thread.is_alive() for thread in _threads))
    ago = None
    if _last_beat:
        ago = round(max(0.0, time.monotonic() - _last_beat), 1)
    return {
        "started": _started,
        "alive": alive,
        "last_beat_ago_s": ago,
    }
