"""磁带盘点：mount → scan → manifest → unmount/eject 的状态机。

设计要点（docs/CONTRACTS.md §4、docs/PLAN.md §4.3）：

- 状态全部落 ``AuditRun``，容器重启不丢，**不依赖 v1 的 ``Tape.status``**
  （v1 启动会把 status 刷成 ejected，见 research/existing-repo.md）。
- **防串盘是硬门槛**：实际条码必须是可用值（非空/非 UNKNOWN）且等于预期条码；
  否则立刻中止并退出磁带，绝不进入扫描。
- 流程：``pending → mounting → scanning → written_manifest → done``；
  条码不符/UNKNOWN 记 ``aborted``；任何异常记 ``failed`` + error，**不会留下看起来
  完成的 run**（done 只在最后一步写入）。
- 结束时严格先 ``unmount`` 再 ``eject``（v1 的 eject 不带挂载点，已挂载状态下行为
  未定义）。卸载失败只记 errors；eject 失败把 run 标 failed（磁带没退出来，下一盘
  没法换）。
- manifest 写到 ``<mount>/._manifest/<barcode>.json``，**临时文件 + fsync + rename**
  原子写；1 万文件量级也只是一次 JSON 序列化（约 1–2 MB），失败不会留下半截文件。
- ``._manifest`` 以点开头：v1 的扫描器只跳过 ``.`` 开头路径，双方互不干扰。

manifest 格式（schema 1）::

    {
      "schema": 1,
      "barcode": "FB1186",
      "generated_at": "2026-10-26T03:45:12",
      "stats": {"volume_id": 3, "kind": "tape", "files_seen": 2, "...": 0},
      "files": [
        {"rel_path": "SSIS-001/SSIS-001.mp4", "size": 1234,
         "mtime": "2026-10-25T10:00:00", "code": "SSIS-001",
         "confidence": 95, "flags": ""}
      ],
      "file_count": 2
    }

``read_manifest`` 对损坏文件/旧格式（无 schema、裸 files 列表）都返回 None 或归一化
后的 dict，**绝不抛异常**。
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import tempfile
from contextlib import nullcontext
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.config import Config
from app.db import db
from app.models import AuditRun, LibraryFile, utcnow

from .tape_adapter import (
    TapeAdapter,
    barcode_is_usable,
    barcode_matches,
    get_adapter,
    normalize_barcode,
    read_volume_label,
)

logger = logging.getLogger(__name__)

# --- 状态机（写入 AuditRun.status） ---------------------------------------
STATUS_PENDING = "pending"
STATUS_MOUNTING = "mounting"
STATUS_SCANNING = "scanning"
STATUS_WRITTEN_MANIFEST = "written_manifest"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_ABORTED = "aborted"

MANIFEST_DIRNAME = "._manifest"
MANIFEST_SCHEMA = 1
_MANIFEST_RESERVED = {"schema", "barcode", "generated_at", "stats", "files", "file_count"}
# 与 Config.SKIP_DIRS 对齐（再额外排除 manifest 自己）
_SKIP_DIRS = {
    MANIFEST_DIRNAME,
    ".@__thumb",
    "@eaDir",
    "#recycle",
    ".Trash",
    "System Volume Information",
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _utcnow_iso() -> str:
    """naive UTC ISO 字符串（与 ``app.models.utcnow`` 同一约定）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _iso_from_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).replace(tzinfo=None).isoformat(
        timespec="seconds"
    )


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _jsonable(value: Any) -> Any:
    """把 dataclass / datetime / Path / set 等转成 JSON 可序列化结构。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return _iso(value)
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "__dict__"):
        return {str(k): _jsonable(v) for k, v in vars(value).items() if not k.startswith("_")}
    return str(value)


def _stats_to_dict(scan: Any) -> dict:
    """ScanStats -> dict（兼容 dataclass / mapping / 普通对象 / None）。"""
    if scan is None:
        return {}
    if isinstance(scan, dict):
        return {str(k): _jsonable(v) for k, v in scan.items()}
    if dataclasses.is_dataclass(scan) and not isinstance(scan, type):
        return {f.name: _jsonable(getattr(scan, f.name)) for f in dataclasses.fields(scan)}
    if hasattr(scan, "__dict__"):
        return {str(k): _jsonable(v) for k, v in vars(scan).items() if not k.startswith("_")}
    return {}


def _normalize_rel(value: Any) -> str | None:
    if value is None:
        return None
    rel = str(value).strip().replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel or None


def _field(row: Any, *names: str) -> Any:
    for name in names:
        if isinstance(row, dict):
            if name in row:
                return row[name]
        elif hasattr(row, name):
            return getattr(row, name)
    return None


def _first_error_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:500]
    return ""


def _maybe_app_context(app):
    """调用方通常已 ``with app.app_context()``；没进上下文时这里补一个（线程安全）。"""
    try:
        from flask import has_app_context
    except Exception:  # pragma: no cover - 理论上 Flask 一定在
        return nullcontext()
    if app is None or has_app_context():
        return nullcontext()
    return app.app_context()


def _make_progress(progress):
    """统一进度回调，吞掉回调自身的异常（进度条坏了不该拖垮盘点）。"""
    if progress is None:
        return lambda done, message="": None

    def emit(done: int, message: str = "") -> None:
        try:
            progress(int(done), message)
        except Exception:  # pragma: no cover - 回调实现问题
            logger.warning("progress 回调异常", exc_info=True)

    return emit


# ---------------------------------------------------------------------------
# manifest：路径 / 原子写 / 读回
# ---------------------------------------------------------------------------
def _safe_filename(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "").strip("._")
    return name or "UNKNOWN"


def manifest_dir(target: Any = None) -> Path:
    """本地 manifest 目录。

    ``target`` 可以是 Flask app（读其 ``MANIFEST_DIR``/``DATA_DIR`` 配置），
    也可以是任意目录路径（测试/迁移脚本用）。
    **默认绝不指向磁带挂载点**——manifest 不再写磁带。
    """
    cfg = getattr(target, "config", None)
    if cfg is not None and hasattr(cfg, "get"):
        base = cfg.get("MANIFEST_DIR")
        if base:
            return Path(str(base))
        data = cfg.get("DATA_DIR")
        if data:
            return Path(str(data)) / "manifests"
    if target is not None:
        return Path(str(target))
    return Path(str(Config.DATA_DIR)) / "manifests"


def manifest_path(target: Any, barcode: str) -> Path:
    """``<本地 manifest 目录>/<barcode>.json``（非法文件名字符转成 _）。"""
    return manifest_dir(target) / f"{_safe_filename(barcode)}.json"


def _atomic_write_json(target: Path, payload: Any) -> None:
    """临时文件 + fsync + os.replace；异常时清掉临时文件，不留半截 manifest。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
        _fsync_dir(target.parent)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _fsync_dir(path: Path) -> None:
    """rename 的持久化需要父目录 fsync；不支持时忽略（部分平台/文件系统）。"""
    try:
        dir_fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _skip_dir(name: str) -> bool:
    # 与 scanner 对齐：点开头目录（._manifest / .@__thumb / .Trash 等）和显式垃圾目录都跳过
    return name.startswith(".") or name in _SKIP_DIRS


def _walk_entries(mount: Path) -> list[dict]:
    """遍历挂载点，产出 manifest 的 files 条目（size/mtime 来自真实文件系统）。

    只读、不跟符号链接目录；跳过点开头路径（对齐 scanner/v1 的扫描范围，保证 manifest
    的文件集合与入库集合一致）；单文件 stat 失败就跳过（不让一个坏文件毁掉整盘 manifest）。
    排序让同名盘的 manifest 可 diff。
    """
    root = str(mount)
    entries: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not _skip_dir(name))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            try:
                stat = os.stat(full)
            except OSError:
                logger.warning("manifest: stat 失败，跳过 %s", full)
                continue
            rel_path = os.path.relpath(full, root).replace(os.sep, "/")
            entries.append(
                {
                    "rel_path": rel_path,
                    "size": int(stat.st_size),
                    "mtime": _iso_from_timestamp(stat.st_mtime),
                    "code": None,
                    "confidence": None,
                    "flags": None,
                }
            )
    entries.sort(key=lambda entry: entry["rel_path"])
    return entries


def _enrich_entries(entries: list[dict], rows: Iterable[Any]) -> None:
    """用扫描结果（DB 行）给 manifest 文件条目补 code/confidence/flags。"""
    by_rel: dict[str, Any] = {}
    for row in rows:
        rel = _normalize_rel(_field(row, "rel_path", "path", "file_path"))
        if rel:
            by_rel[rel] = row
    if not by_rel:
        return
    for entry in entries:
        row = by_rel.get(entry["rel_path"])
        if row is None:
            continue
        code = _field(row, "code", "code_guess")
        confidence = _field(row, "confidence")
        flags = _field(row, "flags")
        if isinstance(flags, (list, tuple, set)):
            flags = ",".join(str(f) for f in flags)
        entry["code"] = normalize_barcode(code) if code is not None else None
        entry["confidence"] = _safe_int(confidence) if confidence is not None else None
        entry["flags"] = None if flags in (None, "") else str(flags)


def _normalize_manifest_file(raw: Any) -> dict:
    """归一化单个文件条目（兼容旧格式的 path/file_path、code_guess 等键名）。"""
    if isinstance(raw, (str, Path)):
        rel_path = _normalize_rel(raw)
        if not rel_path:
            raise ValueError("manifest 文件条目缺少 rel_path")
        return {"rel_path": rel_path, "size": None, "mtime": None,
                "code": None, "confidence": None, "flags": None, "sha256": None}
    if not isinstance(raw, dict):
        raise TypeError(f"manifest 文件条目类型不支持: {type(raw).__name__}")
    rel_path = _normalize_rel(_field(raw, "rel_path", "path", "file_path", "name"))
    if not rel_path:
        raise ValueError("manifest 文件条目缺少 rel_path")
    flags = _field(raw, "flags")
    if isinstance(flags, (list, tuple, set)):
        flags = ",".join(str(f) for f in flags)
    confidence = _field(raw, "confidence")
    return {
        "rel_path": rel_path,
        "size": _safe_int(_field(raw, "size")) if _field(raw, "size") is not None else None,
        "mtime": _iso(_field(raw, "mtime", "modified_at")),
        "code": normalize_barcode(_field(raw, "code", "code_guess")),
        "confidence": _safe_int(confidence) if confidence is not None else None,
        "flags": None if flags in (None, "") else str(flags),
        "sha256": _field(raw, "sha256") or None,
    }


def _normalize_manifest(data: Any, fallback_barcode: str | None = None) -> dict:
    """把任意版本的 manifest 归一化成 schema 1 结构；结构不支持时抛异常。"""
    if isinstance(data, list):
        # 最老的格式：裸 files 列表
        schema = 0
        barcode = normalize_barcode(fallback_barcode)
        generated_at = None
        stats: dict = {}
        files: list = data
        extras: dict = {}
    elif isinstance(data, dict):
        schema = _safe_int(data.get("schema"), 0)
        barcode = normalize_barcode(data.get("barcode")) or normalize_barcode(fallback_barcode)
        generated_at = _iso(data.get("generated_at") or data.get("timestamp"))
        stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
        files = data.get("files")
        if files is None:
            files = data.get("entries", [])
        if not isinstance(files, list):
            raise TypeError("manifest.files 不是列表")
        extras = {
            str(key): _jsonable(value)
            for key, value in data.items()
            if key not in _MANIFEST_RESERVED
        }
    else:
        raise TypeError(f"manifest 根节点类型不支持: {type(data).__name__}")

    normalized_files = [_normalize_manifest_file(item) for item in files]
    result = {
        "schema": schema,
        "barcode": barcode,
        "generated_at": generated_at,
        "stats": _jsonable(stats),
        "files": normalized_files,
        "file_count": len(normalized_files),
    }
    if extras:
        result.update(extras)
    return result


def read_manifest(target: Any, barcode: str | None = None) -> dict | None:
    """读回 manifest；不存在/损坏/格式不认识时返回 **None**，从不抛异常。

    - ``target`` 是 json 文件 → 直接读；
    - 是目录 → 先找 ``<dir>/<barcode>.json``，再兼容旧磁带布局
      ``<dir>/._manifest/<barcode>.json``，最后取最新的 json；
    - 是 Flask app → 读本地 manifest 目录。
    """
    try:
        root = Path(str(target)) if not hasattr(target, "config") else manifest_dir(target)
        candidate: Path | None = None
        if root.is_file():
            candidate = root
        else:
            wanted = normalize_barcode(barcode)
            if wanted:
                for base in (root, root / MANIFEST_DIRNAME):
                    exact = base / f"{_safe_filename(wanted)}.json"
                    if exact.is_file():
                        candidate = exact
                        break
            if candidate is None:
                json_files = []
                for base in (root, root / MANIFEST_DIRNAME):
                    if base.is_dir():
                        json_files.extend(p for p in base.glob("*.json") if p.is_file())
                if not json_files:
                    return None
                candidate = max(json_files, key=lambda path: path.stat().st_mtime)
        with candidate.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return _normalize_manifest(data, fallback_barcode=candidate.stem)
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("read_manifest 放弃 %r: %s", target, exc)
        return None


# ---------------------------------------------------------------------------
# write_manifest / _write_manifest
# ---------------------------------------------------------------------------
def write_manifest(
    target: Any,
    barcode: str,
    scan: Any,
    extra: dict | None = None,
) -> str:
    """把 manifest 写到**本地** ``data/manifests/<barcode>.json``（同时写数据库一份）。

    ``target`` 传 Flask app 时写应用的 ``MANIFEST_DIR`` 并 upsert ``TapeManifest``；
    传目录路径时只写文件（测试/迁移用）。**不会写磁带，也不会写媒体库。**

    ``extra``：
    - ``{"files": [...]}`` —— 扫描得到的文件行（含 code/confidence/flags），
      直接作为 manifest 的 files，不再 walk 磁带；
    - 其它键作为顶层附加字段（不覆盖契约字段）。
    """
    return _write_manifest(target, barcode, scan, extra)


def _write_manifest(
    target: Any,
    barcode: str,
    scan: Any,
    extra: dict | None = None,
) -> str:
    is_app = hasattr(target, "config")
    root = manifest_dir(target)
    root.mkdir(parents=True, exist_ok=True)

    barcode_n = normalize_barcode(barcode) or "UNKNOWN"
    extras = dict(extra or {})
    file_rows = extras.pop("files", None)

    if is_app:
        # 生产路径：DB 行就是权威（scanner 已写入 size/mtime/code/flags）
        entries = [_normalize_manifest_file(item) for item in (file_rows or [])]
        entries = [e for e in entries if e.get("rel_path")]
    else:
        # 目录路径（测试/迁移）：walk 磁盘拿 size/mtime，再用给定行补 code/flags
        walk_root = Path(str(target))
        entries = _walk_entries(walk_root) if walk_root.is_dir() else []
        # 不要把自己（以及同目录的其它 manifest json）当成媒体文件写进 manifest
        own_name = manifest_path(target, barcode_n).name
        entries = [
            e for e in entries
            if not (str(e.get("rel_path") or "") == own_name)
        ]
        if file_rows:
            _enrich_entries(entries, file_rows)

    manifest: dict = {
        "schema": MANIFEST_SCHEMA,
        "barcode": barcode_n,
        "generated_at": _utcnow_iso(),
        "stats": _stats_to_dict(scan),
        "files": entries,
        "file_count": len(entries),
    }
    for key, value in extras.items():
        if key in _MANIFEST_RESERVED:
            continue
        manifest[str(key)] = _jsonable(value)

    path = manifest_path(target, barcode_n)
    _atomic_write_json(path, manifest)

    if is_app:
        # 数据库里也存一份，文件丢了仍可从 DB 重建
        try:
            from app.models import TapeManifest

            row = db.session.query(TapeManifest).filter_by(barcode=barcode_n).first()
            payload = json.dumps(manifest, ensure_ascii=False)
            if row is None:
                row = TapeManifest(barcode=barcode_n, payload=payload)
                db.session.add(row)
            else:
                row.payload = payload
            row.generated_at = str(manifest.get("generated_at") or "")
            row.file_count = int(manifest.get("file_count") or 0)
            db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()

    return str(path)


def read_manifest_from_db(app, barcode: str | None = None) -> dict | None:
    """从数据库读 manifest（文件丢了的兜底）。"""
    try:
        from app.models import TapeManifest
        from app.db import db as _db

        query = _db.session.query(TapeManifest)
        if barcode:
            row = query.filter_by(barcode=normalize_barcode(barcode)).first()
        else:
            row = query.order_by(TapeManifest.updated_at.desc()).first()
        if row is None or not row.payload:
            return None
        data = json.loads(row.payload)
        return _normalize_manifest(data, fallback_barcode=row.barcode)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# run_audit
# ---------------------------------------------------------------------------
def _release(adapter: TapeAdapter, errors: list[str], *, eject: bool = True) -> dict:
    """先 unmount；``eject=True`` 时再 eject（best-effort，绝不抛）。"""
    result = {"unmount_ok": False, "unmount_message": "", "eject_ok": False, "eject_message": ""}
    try:
        ok, message = adapter.unmount()
    except Exception as exc:  # pragma: no cover - 防御性
        ok, message = False, f"{type(exc).__name__}: {exc}"
    result["unmount_ok"], result["unmount_message"] = bool(ok), str(message)
    if not ok:
        errors.append(f"卸载失败：{message}")

    if not eject:
        result["eject_ok"] = True
        result["eject_message"] = "已禁用自动弹出（AVLIB_AUTO_EJECT=0）"
        result["eject_skipped"] = True
        return result

    try:
        ok, message = adapter.eject()
    except Exception as exc:  # pragma: no cover - 防御性
        ok, message = False, f"{type(exc).__name__}: {exc}"
    result["eject_ok"], result["eject_message"] = bool(ok), str(message)
    if not ok:
        errors.append(f"弹出失败：{message}")
    return result


def _db_file_entries(volume_id: int | None) -> list[dict]:
    """取本次扫描写入的文件行，用于 manifest 的 code/confidence/flags 补充。"""
    if not volume_id:
        return []
    try:
        rows = (
            db.session.query(LibraryFile)
            .filter(LibraryFile.volume_id == volume_id, LibraryFile.deleted_at.is_(None))
            .all()
        )
    except Exception:  # pragma: no cover - DB 查询异常不该让 manifest 写不出来
        logger.warning("读取 LibraryFile 失败（manifest 将不含 code 信息）", exc_info=True)
        return []
    entries = []
    for row in rows:
        entries.append(
            {
                "rel_path": row.rel_path,
                "size": row.size,
                "mtime": row.mtime,
                "code": row.code_guess,
                "confidence": row.confidence,
                "flags": row.flags,
                "sha256": row.sha256,
            }
        )
    return entries


def _finish(run: AuditRun, report: dict, status: str, message: str, errors: list[str]) -> None:
    """收尾：写 AuditRun 终态 + 汇总 report。"""
    run.status = status
    run.finished_at = utcnow()
    if errors:
        run.error = "; ".join(errors)[:4000]
    db.session.commit()
    report.update(ok=status == STATUS_DONE, status=status, message=message)
    if errors:
        for item in errors:
            if item not in report["errors"]:
                report["errors"].append(item)


def _safe_commit() -> None:
    try:
        db.session.commit()
    except Exception:  # pragma: no cover - DB 崩了只能记日志
        logger.exception("AuditRun commit 失败")
        db.session.rollback()


def run_audit(
    app,
    barcode: str,
    *,
    expected_barcode: str | None = None,
    adapter: TapeAdapter | None = None,
    write_manifest: bool = True,
    eject: bool = True,
    progress=None,
) -> dict:
    """完整盘点一盘磁带，返回结果 dict（不抛异常，UI 看 ``ok/status/errors``）。

    参数：
    - ``barcode``：**用户声明的**当前插入磁带条码（用于和 mount 返回的实际条码比对；
      UNKNOWN/空直接拒绝，防串盘）；
    - ``expected_barcode``：不传时默认同 ``barcode``；显式传空串表示"只校验非 UNKNOWN"；
    - ``adapter``：不传则按 ``app.config`` 用 :func:`get_adapter` 建（默认 HTTP）；
    - ``write_manifest``：False 时跳过写盘（调试/只读盘用）；
    - ``eject``：False 时结束只卸载不弹出（机柜门会被挡住的场景）；
    - ``progress(done, message="")``：阶段进度 0–100。

    返回 dict 字段：``ok, status, run_id, barcode, expected_barcode, actual_barcode,
    volume_id, files_seen, files_new, manifest_path, stats, errors, message``。
    """
    emit = _make_progress(progress)
    barcode_input = normalize_barcode(barcode)
    expected = (
        normalize_barcode(expected_barcode) if expected_barcode is not None else barcode_input
    )

    report: dict = {
        "ok": False,
        "status": STATUS_PENDING,
        "run_id": None,
        "barcode": barcode_input,
        "expected_barcode": expected,
        "actual_barcode": None,
        "volume_id": None,
        "files_seen": 0,
        "files_new": 0,
        "manifest_path": None,
        "stats": None,
        "errors": [],
        "message": "",
    }
    errors: list[str] = report["errors"]

    with _maybe_app_context(app):
        if adapter is None:
            adapter = get_adapter(getattr(app, "config", None), app=app)
        mount_path = str(
            getattr(adapter, "mount_point", None)
            or getattr(app, "config", {}).get("LTFS_MOUNT")
            or Config.LTFS_MOUNT
        )

        # 先建 run（pending）并立刻落库：容器重启/中途崩溃也能留下"未完成"的痕迹
        run = AuditRun(status=STATUS_PENDING, expected_barcode=expected, started_at=utcnow())
        db.session.add(run)
        db.session.commit()
        report["run_id"] = run.id
        emit(0, "创建盘点任务")

        try:
            # --- 0. 参数预校验：条码不可用就不碰设备 ----------------------
            if not barcode_is_usable(barcode_input):
                errors.append(
                    f"条码不可用（{barcode_input or '空'}）：拒绝盘点，请确认插入的磁带并核对条码"
                )
                _finish(run, report, STATUS_ABORTED, "条码未通过校验，未操作磁带", errors)
                emit(100, "已中止")
                return report
            if expected is not None and not barcode_is_usable(expected):
                errors.append(f"预期条码不可用（{expected or '空'}），拒绝盘点")
                _finish(run, report, STATUS_ABORTED, "预期条码未通过校验，未操作磁带", errors)
                emit(100, "已中止")
                return report

            # --- 1. 挂载 --------------------------------------------------
            run.status = STATUS_MOUNTING
            db.session.commit()
            emit(1, "正在挂载磁带")
            try:
                mount_ok, mount_message, actual = adapter.mount()
            except Exception as exc:
                mount_ok, mount_message, actual = False, f"{type(exc).__name__}: {exc}", None
            actual_n = normalize_barcode(actual)
            run.actual_barcode = actual_n
            db.session.commit()
            report["actual_barcode"] = actual_n

            if not mount_ok:
                detail = str(mount_message or "").strip() or "未知原因"
                # 适配器通常自带"挂载失败："前缀，别把同一句话叠两遍
                if not any(word in detail for word in ("失败", "超时", "错误", "不存在")):
                    detail = f"挂载失败：{detail}"
                errors.append(detail)
                _finish(run, report, STATUS_FAILED, "磁带挂载失败", errors)
                emit(100, "挂载失败")
                return report
            emit(5, f"已挂载：{actual_n or '拿不到条码'}")

            # --- 2. 强校验实际条码（防串盘） ------------------------------
            if not barcode_is_usable(actual_n):
                errors.append(
                    f"实际条码不可用（{actual_n or '空'}）：无法确认是哪盘磁带，中止并退出"
                )
                _release(adapter, errors)
                _finish(run, report, STATUS_ABORTED, "实际条码不可用，已中止", errors)
                emit(100, "已中止")
                return report
            if not barcode_matches(actual_n, expected):
                errors.append(f"条码不符：预期 {expected or '(任意可用条码)'}，实际 {actual_n}，已中止")
                _release(adapter, errors)
                _finish(run, report, STATUS_ABORTED, "实际条码与预期不符，已中止", errors)
                emit(100, "已中止")
                return report
            report["barcode"] = actual_n

            # --- 3. 扫描 --------------------------------------------------
            run.status = STATUS_SCANNING
            db.session.commit()
            emit(10, "正在扫描磁带内容")
            try:
                scan = adapter.scan(mount_path, actual_n)
            except ValueError as exc:
                # scanner 自身的防串盘/参数校验：归为 aborted
                errors.append(f"扫描中止：{exc}")
                _release(adapter, errors)
                _finish(run, report, STATUS_ABORTED, "扫描中止", errors)
                emit(100, "已中止")
                return report
            except Exception as exc:
                errors.append(f"扫描失败：{type(exc).__name__}: {exc}")
                _release(adapter, errors)
                _finish(run, report, STATUS_FAILED, "磁带扫描失败", errors)
                emit(100, "扫描失败")
                return report

            stats = _stats_to_dict(scan)
            report["stats"] = stats
            volume_id = _safe_int(getattr(scan, "volume_id", None) or stats.get("volume_id"), 0) or None
            files_seen = _safe_int(
                getattr(scan, "files_seen", None) or stats.get("files_seen"), 0
            )
            files_new = _safe_int(getattr(scan, "new", None) or stats.get("new"), 0)
            run.volume_id = volume_id
            run.files_seen = files_seen
            run.files_new = files_new
            db.session.commit()
            report.update(volume_id=volume_id, files_seen=files_seen, files_new=files_new)
            emit(85, f"扫描完成：{files_seen} 个文件")

            # --- 4. manifest（先写盘，再 unmount 刷缓冲，最后 eject） ------
            if write_manifest:
                emit(90, "正在写入 manifest")
                file_rows = _db_file_entries(volume_id)
                if not file_rows:
                    # DB 里没有文件行（例如测试/旧数据）时退化为只读 walk 挂载点
                    try:
                        file_rows = _walk_entries(mount_path)
                    except Exception:  # noqa: BLE001
                        file_rows = []
                try:
                    path = _write_manifest(
                        app,
                        actual_n,
                        scan,
                        {"files": file_rows} if file_rows else None,
                    )
                    readback = read_manifest(path)
                    if readback is None:
                        raise OSError("manifest 写入后读回校验失败")
                except Exception as exc:
                    errors.append(f"写 manifest 失败：{type(exc).__name__}: {exc}")
                    _release(adapter, errors)
                    _finish(run, report, STATUS_FAILED, "manifest 写入失败", errors)
                    emit(100, "manifest 写入失败")
                    return report
                run.status = STATUS_WRITTEN_MANIFEST
                db.session.commit()
                report["manifest_path"] = path
                emit(95, "manifest 已写入")
            else:
                emit(95, "已跳过 manifest")

            # --- 5. 收带：unmount（→ eject，可禁用） -----------------------
            emit(97, "正在卸载并弹出磁带" if eject else "正在卸载磁带（不弹出）")
            release = _release(adapter, errors, eject=eject)
            # 卸载失败但弹出成功：eject 会带走挂载，按原语义容忍
            if not release["unmount_ok"] and not (eject and release["eject_ok"]):
                _finish(run, report, STATUS_FAILED, "盘点数据已写入，但磁带卸载失败", errors)
                emit(100, "卸载失败")
                return report
            if eject and not release["eject_ok"]:
                errors.append("磁带未能弹出，请人工处理后再换盘")
                _finish(run, report, STATUS_FAILED, "盘点数据已写入，但磁带弹出失败", errors)
                emit(100, "弹出失败")
                return report

            done_message = "盘点完成" if eject else "盘点完成（已卸载，未弹出）"
            _finish(run, report, STATUS_DONE, done_message, errors)
            emit(100, done_message)
            return report

        except Exception as exc:  # 任何未预料异常：确保 run 不是"看起来完成"
            logger.exception("run_audit 意外中断（run_id=%s）", report["run_id"])
            _safe_commit()
            try:
                db.session.rollback()
            except Exception:  # pragma: no cover
                pass
            message = f"盘点异常中断：{type(exc).__name__}: {exc}"
            errors.append(message)
            try:
                current = db.session.get(AuditRun, report["run_id"]) if report["run_id"] else None
            except Exception:  # pragma: no cover
                current = None
            if current is None:  # pragma: no cover - 极端情况
                current = AuditRun(
                    status=STATUS_FAILED,
                    expected_barcode=expected,
                    actual_barcode=report.get("actual_barcode"),
                    started_at=utcnow(),
                    finished_at=utcnow(),
                    error=message,
                )
                db.session.add(current)
                db.session.commit()
                report["run_id"] = current.id
            else:
                current.status = STATUS_FAILED
                current.finished_at = utcnow()
                current.error = message[:4000]
                db.session.commit()
            report.update(ok=False, status=STATUS_FAILED, message=message)
            emit(100, "盘点异常")
            return report
