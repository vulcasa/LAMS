"""磁带高层操作：mount / unmount / eject + 操作日志 + 卷状态流转。

合并 ltfs-webui 之后，本服务自己控设备。职责划分：

- ``tape_adapter``：只负责"怎么调设备"（ltfs 命令 / 过渡期 v1 HTTP），以及最新的
  ``last_output``（命令 + stdout/stderr）；
- **本模块（tape_ops）**：负责"操作前后的数据库状态"——
  - 每次操作写一条 ``TapeOperation``（running → success/failed；command/stdout/stderr
    截到 8KB 再入库，v1 的 Operation 表语义）；
  - 成功 mount 后按**实际条码** upsert ``LibraryVolume(kind='tape')``，置
    ``status='mounted'`` + ``last_mounted_at`` + ``device_path``；同一驱动器只能挂一盘，
    挂新带时把库里其它"残留 mounted"的卷标成 unknown（v1 多行 mounted 的老坑）；
  - unmount → 当前带卷 ``status='unmounted'``；eject → ``status='ejected'``；
    失败时把原因写进卷的 ``last_error``；
  - **防串盘**：mount 后实际条码为空/UNKNOWN 或与 ``expected_barcode`` 不符 →
    立即 unmount + eject（尽力而为）并记 failed，**不登记卷**，与 audit 的口径一致。
- 所有对外函数**不抛异常**，统一返回 ``{ok: bool, message: str, ...}``，供
  ``app/routes/tape.py`` 直接 jsonify。

返回字段（常用）：
    mount_tape      -> {ok, message, barcode, status, operation_id, volume_id, volume}
    unmount_tape    -> {ok, message, barcode, status, operation_id, volume}
    eject_tape      -> {ok, message, barcode, status, operation_id, volume}
    current_status  -> {ok, message, mounted, barcode, device_exists, device_path,
                        volume_label, current_volume, device_info, device_list,
                        status:{...}, adapter, mount_point, errors}
    device_snapshot -> 同 current_status（路由 /api/tape/device 用这个别名）
    list_tape_volumes / list_operations -> list[dict]（JSON 友好）
"""
from __future__ import annotations

import functools
import logging
import os
import re
from pathlib import Path
import time
from contextlib import nullcontext
from typing import Any

from sqlalchemy import func

from app.db import db
from app.models import LibraryFile, LibraryVolume, TapeOperation, utcnow
from app.services.tape_adapter import (
    TapeAdapter,
    barcode_is_usable,
    barcode_matches,
    get_adapter,
    normalize_barcode,
    parse_barcode_from_text,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_OUTPUT_BYTES",
    "classify_mount_failure",
    "current_status",
    "device_snapshot",
    "eject_tape",
    "initialize_tape",
    "list_operations",
    "list_tape_volumes",
    "mount_tape",
    "recover_list_tape",
    "recover_tape_index",
    "unmount_tape",
]

#: command/stdout/stderr 入库前的截断上限（v1 的 Operation 表也这么干）
MAX_OUTPUT_BYTES = 8 * 1024
_TRUNCATE_NOTICE = "\n...（输出超过 8KB，已截断）"

_TAPE_KIND = "tape"
_BARCODE_INIT_RE = re.compile(r"^[A-Za-z0-9]{6}$")
_UNINITIALIZED_MARKERS = (
    "not an ltfs volume",
    "cannot read ansi label",
    "failed to read label",
    "unformatted cartridge",
    "not formatted",
    "medium is not partitioned",
    "not partitioned",
    "cannot read volume: medium",
    "ltfs17168e",
    "ltfs11170e",
    "ltfs11174e",
    "ltfs11175e",
    "ltfs11176e",
)


def _output_text(output: dict | None) -> str:
    if not isinstance(output, dict):
        return ""
    return "\n".join(
        str(output.get(key) or "") for key in ("command", "stdout", "stderr")
    )


def classify_mount_failure(output: dict | None, message: str = "") -> str:
    """把挂载失败分类：uninitialized / already_registered / unknown。

    只认明确的“不是 LTFS 卷/无法读取标签”，不能把设备忙、写保护、超时误判为未初始化。
    """
    text = (_output_text(output) + "\n" + str(message or "")).lower()
    if any(marker in text for marker in _UNINITIALIZED_MARKERS):
        return "uninitialized"
    return "unknown"


def _normalize_init_barcode(value: str | None) -> str | None:
    barcode = normalize_barcode(value)
    if not barcode or not _BARCODE_INIT_RE.match(barcode):
        return None
    return barcode.upper()


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _maybe_app_context(app):
    """调用方通常已 ``with app.app_context()``；没进上下文时补一个（路由/线程都安全）。"""
    try:
        from flask import has_app_context
    except Exception:  # pragma: no cover - 理论上 Flask 一定在
        return nullcontext()
    if app is None or has_app_context():
        return nullcontext()
    return app.app_context()


def _adapter(app, adapter: TapeAdapter | None = None) -> TapeAdapter:
    if adapter is not None:
        return adapter
    return get_adapter(getattr(app, "config", None), app=app)


def _truncate(value: Any, limit: int = MAX_OUTPUT_BYTES) -> str | None:
    """按 UTF-8 字节截断（不会切坏多字节字符），返回 None 表示原值为 None。"""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        text = bytes(value).decode("utf-8", "replace")
    else:
        text = str(value)
    if text == "":
        return ""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore") + _TRUNCATE_NOTICE


def _iso(value) -> str | None:
    if value is None:
        return None
    try:
        return value.isoformat(timespec="seconds")
    except (AttributeError, TypeError):  # pragma: no cover - 防御
        return str(value)


def _last_output(adapter: Any) -> dict:
    """适配器最近一次设备操作的 {command, stdout, stderr}（可能为空）。"""
    output = getattr(adapter, "last_output", None)
    if not isinstance(output, dict):
        return {"command": None, "stdout": "", "stderr": ""}
    return {
        "command": output.get("command"),
        "stdout": output.get("stdout") or "",
        "stderr": output.get("stderr") or "",
    }


def _volume_dict(volume: LibraryVolume, *, files: int | None = None) -> dict:
    """``LibraryVolume`` → JSON 友好 dict（also 可被 Jinja 点号访问）。"""
    return {
        "id": volume.id,
        "kind": volume.kind,
        "ref": volume.ref,
        "barcode": volume.ref,
        "name": volume.name,
        "status": volume.status,
        "device_path": volume.device_path,
        "capacity_bytes": volume.capacity_bytes,
        "used_bytes": volume.used_bytes,
        "last_mounted_at": _iso(volume.last_mounted_at),
        "last_scan_at": _iso(volume.last_scan_at),
        "last_scan_status": volume.last_scan_status,
        "last_error": volume.last_error,
        "enabled": bool(volume.enabled),
        "created_at": _iso(volume.created_at),
        "files": files,
    }


def _volume_file_count(volume: LibraryVolume) -> int:
    try:
        return (
            db.session.query(func.count(LibraryFile.id))
            .filter(LibraryFile.volume_id == volume.id, LibraryFile.deleted_at.is_(None))
            .scalar()
            or 0
        )
    except Exception:  # pragma: no cover - 防御
        return 0


def _find_volume(barcode: str | None) -> LibraryVolume | None:
    barcode_n = normalize_barcode(barcode)
    if not barcode_n:
        return None
    return LibraryVolume.query.filter_by(kind=_TAPE_KIND, ref=barcode_n).first()


def _mounted_volumes() -> list[LibraryVolume]:
    return LibraryVolume.query.filter_by(kind=_TAPE_KIND, status="mounted").all()


#: 初始化只接受近期一次“未初始化挂载失败”产生的上下文，避免直接调用 API 误格式化。
_INIT_CONTEXT_MAX_AGE = 15 * 60


def _recent_uninitialized_mount(operation_id: int | None) -> TapeOperation | None:
    """校验初始化上下文：必须引用近期一次 uninitialized 的 mount 失败操作。"""
    if operation_id is None:
        return None
    try:
        operation = db.session.get(TapeOperation, int(operation_id))
    except (TypeError, ValueError):
        return None
    if operation is None or operation.op_type != "mount" or operation.status != "failed":
        return None
    # 上下文必须仍是最近一次 mount 操作，避免挂载成功后继续用旧失败上下文格式化。
    latest_mount = (
        TapeOperation.query.filter_by(op_type="mount")
        .order_by(TapeOperation.id.desc()).first()
    )
    if latest_mount is None or latest_mount.id != operation.id:
        return None
    started = operation.started_at or operation.created_at
    if started is not None:
        try:
            age = (utcnow() - started).total_seconds()
        except TypeError:
            age = 0
        if age > _INIT_CONTEXT_MAX_AGE:
            return None
    output = {
        "stdout": operation.stdout or "",
        "stderr": operation.stderr or "",
    }
    if classify_mount_failure(output, operation.message or "") != "uninitialized":
        return None
    return operation


def _upsert_volume(barcode: str) -> tuple[LibraryVolume, bool]:
    volume = _find_volume(barcode)
    if volume is not None:
        return volume, False
    volume = LibraryVolume(
        kind=_TAPE_KIND,
        ref=barcode,
        name=f"磁带 {barcode}",
        enabled=True,
    )
    db.session.add(volume)
    db.session.commit()  # 先落库固定 (kind, ref) 唯一键
    return volume, True


def _set_volume_status(volume: LibraryVolume, status: str, *, error: str | None = None) -> None:
    volume.status = status
    if error is not None:
        volume.last_error = _truncate(error) or ""
    elif status != "mounted":
        volume.last_error = None


def _mark_volume_error(
    volume: LibraryVolume | None, message: str, *, status: str | None = None
) -> None:
    if volume is None:
        return
    try:
        volume.last_error = _truncate(message) or ""
        if status is not None:
            volume.status = status
        db.session.commit()
    except Exception:  # pragma: no cover - 记录错误失败不该掩盖主错误
        logger.warning("写卷 last_error 失败", exc_info=True)
        db.session.rollback()


def _start_operation(
    op_type: str,
    *,
    barcode: str | None = None,
    volume_id: int | None = None,
) -> TapeOperation:
    operation = TapeOperation(
        volume_id=volume_id,
        barcode=barcode,
        op_type=op_type,
        status="running",
        started_at=utcnow(),
    )
    db.session.add(operation)
    db.session.commit()
    return operation


def _finish_operation(
    operation: TapeOperation,
    *,
    ok: bool,
    message: str,
    started: float,
    output: dict | None = None,
    volume_id: int | None = None,
) -> TapeOperation:
    """写终态（running → success/failed），command/stdout/stderr 截断到 8KB。"""
    out = output if output is not None else {"command": None, "stdout": "", "stderr": ""}
    operation.status = "success" if ok else "failed"
    operation.message = _truncate(message) or ""
    operation.command = _truncate(out.get("command"))
    operation.stdout = _truncate(out.get("stdout"))
    operation.stderr = _truncate(out.get("stderr"))
    if volume_id is not None:
        operation.volume_id = volume_id
    operation.finished_at = utcnow()
    operation.duration_ms = max(0, int((time.monotonic() - started) * 1000))
    try:
        db.session.commit()
    except Exception:  # pragma: no cover - 日志写失败不能盖掉操作结果
        logger.exception("TapeOperation 落库失败")
        db.session.rollback()
    return operation


def _cleanup_tape(adapter: TapeAdapter) -> str:
    """防串盘失败/初始化后校验失败时只卸载，绝不自动弹出。返回问题描述。"""
    problems: list[str] = []
    try:
        ok, message = adapter.unmount()
        if not ok:
            problems.append(f"卸载失败（{message}）")
    except Exception as exc:  # pragma: no cover - 适配器不应抛
        problems.append(f"卸载异常（{type(exc).__name__}: {exc}）")
    return "；".join(problems)



def _fail_operation(operation_id: int | None, message: str, started: float) -> None:
    """兜底：意外异常时把仍停在 running 的操作日志标 failed（不抛）。"""
    if not operation_id:
        return
    try:
        row = db.session.get(TapeOperation, operation_id)
        if row is None or row.status != "running":
            return
        row.status = "failed"
        row.message = _truncate(message) or ""
        row.finished_at = utcnow()
        row.duration_ms = max(0, int((time.monotonic() - started) * 1000))
        db.session.commit()
    except Exception:  # pragma: no cover - 兜底不能再抛
        logger.exception("兜底标记 TapeOperation failed 失败")
        try:
            db.session.rollback()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 设备互斥：单驱动器不能被并发操作（v1 的双挂载教训）
# ---------------------------------------------------------------------------
import threading as _threading

try:  # 跨进程文件锁（CLI 与 web 同时操作设备时也能互斥）
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None

_DRIVE_LOCK = _threading.RLock()
_LOCK_STATE = {"fd": None, "depth": 0}
_LOCK_FILE = "tape.lock"


def _lock_path():
    from app.config import Config

    try:
        base = Path(Config.DATA_DIR)
    except Exception:  # pragma: no cover
        base = Path("/tmp")
    base.mkdir(parents=True, exist_ok=True)
    return base / _LOCK_FILE


def acquire_drive() -> bool:
    """非阻塞获取设备锁：进程内 RLock + 跨进程 flock，本线程可重入。"""
    if not _DRIVE_LOCK.acquire(blocking=False):
        return False
    if _LOCK_STATE["depth"] > 0:  # 同线程重入
        _LOCK_STATE["depth"] += 1
        return True
    if _fcntl is not None:
        try:
            fd = os.open(str(_lock_path()), os.O_CREAT | os.O_RDWR, 0o644)
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            _DRIVE_LOCK.release()
            return False
        except Exception:  # pragma: no cover
            _DRIVE_LOCK.release()
            return False
        _LOCK_STATE["fd"] = fd
    _LOCK_STATE["depth"] = 1
    return True


def release_drive() -> None:
    if _LOCK_STATE["depth"] > 0:
        _LOCK_STATE["depth"] -= 1
        if _LOCK_STATE["depth"] == 0:
            fd = _LOCK_STATE.get("fd")
            if fd is not None and _fcntl is not None:
                try:
                    _fcntl.flock(fd, _fcntl.LOCK_UN)
                except Exception:  # pragma: no cover
                    pass
                try:
                    os.close(fd)
                except Exception:  # pragma: no cover
                    pass
            _LOCK_STATE["fd"] = None
    try:
        _DRIVE_LOCK.release()
    except RuntimeError:  # pragma: no cover - 未持有时忽略
        pass


def drive_busy() -> bool:
    """锁是否被其它线程占用（同线程重入不算忙）。"""
    if not acquire_drive():
        return True
    release_drive()
    return False


def _serialized(fn):
    """给 mount/unmount/eject 加设备锁：占不到就返回 busy，不排队、不抛。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not acquire_drive():
            return {
                "ok": False,
                "busy": True,
                "message": "另一个磁带操作正在进行（挂载/卸载/弹出/盘点），请稍后再试",
            }
        try:
            return fn(*args, **kwargs)
        finally:
            release_drive()

    return wrapper


# ---------------------------------------------------------------------------
# mount / unmount / eject
# ---------------------------------------------------------------------------
@_serialized
def mount_tape(
    app,
    expected_barcode: str | None = None,
    *,
    manual_barcode: str | None = None,
    manual_confirm: bool = False,
    adapter: TapeAdapter | None = None,
) -> dict:
    """挂载当前插入的磁带（mount 不能选盘），成功后登记卷。

    ``expected_barcode`` 给了就做防串盘校验：实际条码必须非 UNKNOWN 且与它一致；
    否则只卸载、绝不自动弹出，并记 failed。

    ``manual_barcode`` 是例外通道：用于磁带上贴了物理条码、但 LTFS 初始化时
    没写入 barcode、挂载后读不到条码的磁带。必须已经 mounted 且实际条码不可用，
    用户确认后才能按手输条码登记；已有可读条码的磁带不允许用它绕过校验。
    """
    started = time.monotonic()
    operation: TapeOperation | None = None
    try:
        with _maybe_app_context(app):
            tape_adapter = _adapter(app, adapter)
            expected = normalize_barcode(expected_barcode)
            if expected_barcode is not None and not barcode_is_usable(expected):
                return {
                    "ok": False,
                    "message": f"预期条码不可用（{expected_barcode!r}）：拒绝挂载（防串盘）",
                    "barcode": None,
                    "status": None,
                }
            manual_n = _normalize_init_barcode(manual_barcode)
            if manual_barcode is not None:
                if not manual_confirm:
                    return {
                        "ok": False, "code": "manual_not_confirmed",
                        "message": "手动条码必须确认后才能登记",
                    }
                if manual_n is None:
                    return {
                        "ok": False, "code": "invalid_barcode",
                        "message": "手动条码必须是 6 位字母或数字",
                    }

            if manual_n is not None:
                # 手动条码只用于“已经挂载、但 LTFS 读不到条码”的例外磁带。
                current_status = None
                try:
                    current_status = tape_adapter.status()
                except Exception:  # noqa: BLE001
                    current_status = None
                mounted_now = bool(getattr(current_status, "mounted", False))
                actual_now = normalize_barcode(getattr(current_status, "barcode", None))
                if not barcode_is_usable(actual_now):
                    label_fn = getattr(tape_adapter, "volume_label", None)
                    if callable(label_fn):
                        try:
                            actual_now = normalize_barcode(label_fn())
                        except Exception:  # noqa: BLE001
                            pass
                if not mounted_now:
                    return {
                        "ok": False, "code": "not_mounted",
                        "message": "当前没有已挂载的磁带，无法手动登记条码",
                    }
                if barcode_is_usable(actual_now):
                    return {
                        "ok": False, "code": "barcode_readable",
                        "message": "当前磁带条码可正常读取，不允许手动覆盖",
                    }
                if expected is not None and not barcode_matches(manual_n, expected):
                    return {
                        "ok": False, "code": "barcode_mismatch",
                        "message": f"手动条码 {manual_n} 与预期条码 {expected} 不一致",
                    }
                operation = _start_operation("mount", barcode=manual_n)
                volume, created = _upsert_volume(manual_n)
                for other in _mounted_volumes():
                    if other.id != volume.id:
                        _set_volume_status(other, "unknown")
                volume.status = "mounted"
                volume.device_path = getattr(tape_adapter, "device_path", None) or volume.device_path
                volume.last_mounted_at = utcnow()
                volume.last_error = None
                db.session.commit()
                detail = f"磁带已挂载，按手动条码 {manual_n} 登记"
                _finish_operation(operation, ok=True, message=detail,
                                  started=started, volume_id=volume.id)
                return {
                    "ok": True,
                    "message": detail,
                    "barcode": manual_n,
                    "status": "mounted",
                    "manual_barcode": True,
                    "operation_id": operation.id,
                    "volume_id": volume.id,
                    "volume": _volume_dict(volume),
                    "created_volume": created,
                }
            operation = _start_operation("mount", barcode=expected)
            try:
                ok, message, actual = tape_adapter.mount()
            except Exception as exc:  # 适配器理论上不抛，兜底
                logger.exception("adapter.mount 异常")
                _finish_operation(
                    operation,
                    ok=False,
                    message=f"挂载异常：{type(exc).__name__}: {exc}",
                    started=started,
                )
                return {
                    "ok": False,
                    "message": f"挂载失败：{type(exc).__name__}: {exc}",
                    "operation_id": operation.id,
                }

            mount_output = _last_output(tape_adapter)  # 先存 mount 自己的输出，防被 cleanup 覆盖
            actual_n = normalize_barcode(actual)
            operation.barcode = actual_n or expected
            device_path = getattr(tape_adapter, "device_path", None)

            if not ok:
                detail = str(message or "未知原因").strip()
                _finish_operation(
                    operation,
                    ok=False,
                    message=detail,
                    started=started,
                    output=mount_output,
                )
                volume = _find_volume(expected or actual_n)
                if volume is not None:
                    _mark_volume_error(volume, detail)
                detected = parse_barcode_from_text(_output_text(mount_output))
                registered = _find_volume(detected)
                failure_code = classify_mount_failure(mount_output, detail)
                return {
                    "ok": False,
                    "message": detail,
                    "barcode": actual_n,
                    "operation_id": operation.id,
                    "volume": _volume_dict(volume) if volume is not None else None,
                    "needs_init": failure_code == "uninitialized",
                    "code": failure_code,
                    "detected_barcode": detected,
                    "barcode_registered": registered is not None,
                }

            # --- 防串盘：条码不可用 → 保持挂载，交给用户手动登记例外通道 ------
            if not barcode_is_usable(actual_n):
                detail = f"实际条码不可用（{actual_n or '空'}）：磁带保持挂载，可手动输入物理条码登记（不会自动弹出）"
                _finish_operation(
                    operation,
                    ok=False,
                    message=detail,
                    started=started,
                    output=mount_output,
                )
                volume = _find_volume(expected)
                _mark_volume_error(volume, detail, status="unknown")
                return {
                    "ok": False,
                    "message": detail,
                    "code": "barcode_unreadable",
                    "needs_manual_barcode": True,
                    "detected_barcode": None,
                    "barcode": None,
                    "operation_id": operation.id,
                    "volume": _volume_dict(volume) if volume is not None else None,
                }
            if expected is not None and not barcode_matches(actual_n, expected):
                problems = _cleanup_tape(tape_adapter)
                detail = f"条码不符：预期 {expected}，实际 {actual_n}；已卸载（未自动弹出）"
                if problems:
                    detail += f"；{problems}"
                _finish_operation(
                    operation,
                    ok=False,
                    message=detail,
                    started=started,
                    output=mount_output,
                )
                volume = _find_volume(expected)
                _mark_volume_error(volume, detail, status="unknown")
                return {
                    "ok": False,
                    "message": detail,
                    "barcode": actual_n,
                    "operation_id": operation.id,
                    "volume": _volume_dict(volume) if volume is not None else None,
                }

            # --- 成功：登记卷 + 状态流转 ---------------------------------
            volume, created = _upsert_volume(actual_n)
            now = utcnow()
            # 单驱动器：把其它"残留 mounted"的卷标 unknown（v1 多行 mounted 的坑）
            for other in _mounted_volumes():
                if other.id != volume.id:
                    _set_volume_status(other, "unknown")
            volume.status = "mounted"
            volume.device_path = device_path or volume.device_path
            volume.last_mounted_at = now
            volume.last_error = None
            db.session.commit()
            _finish_operation(
                operation,
                ok=True,
                message=message,
                started=started,
                output=mount_output,
                volume_id=volume.id,
            )
            return {
                "ok": True,
                "message": message,
                "barcode": actual_n,
                "status": "mounted",
                "operation_id": operation.id,
                "volume_id": volume.id,
                "volume": _volume_dict(volume),
                "created_volume": created,
            }
    except Exception as exc:
        logger.exception("mount_tape 失败")
        try:
            db.session.rollback()
        except Exception:  # pragma: no cover
            pass
        _fail_operation(getattr(operation, "id", None), f"挂载异常：{type(exc).__name__}: {exc}", started)
        return {"ok": False, "message": f"挂载失败：{type(exc).__name__}: {exc}"}


@_serialized
def initialize_tape(
    app,
    barcode: str,
    *,
    confirmed: bool = False,
    force: bool = False,
    mount_operation_id: int | None = None,
    adapter: TapeAdapter | None = None,
) -> dict:
    """格式化未初始化磁带并挂载。

    安全规则：
    - 必须 confirmed=True；
    - barcode 必须是 6 位字母/数字；
    - 已挂载/已登记卷拒绝（force 也不能绕过已登记保护）；
    - 只有 direct adapter 支持；
    - 初始化成功后必须实际挂载且条码一致才登记。
    """
    started = time.monotonic()
    operation: TapeOperation | None = None
    try:
        with _maybe_app_context(app):
            if not confirmed:
                return {"ok": False, "code": "not_confirmed",
                        "message": "必须明确确认格式化操作"}
            barcode_n = _normalize_init_barcode(barcode)
            if barcode_n is None:
                return {"ok": False, "code": "invalid_barcode",
                        "message": "条码必须是 6 位字母或数字"}
            context = _recent_uninitialized_mount(mount_operation_id)
            if context is None:
                return {"ok": False, "code": "missing_mount_context",
                        "message": "初始化必须基于近期一次“磁带回读失败/未初始化”的挂载结果"}
            tape_adapter = _adapter(app, adapter)
            try:
                is_mounted = tape_adapter.status().mounted or os.path.ismount(tape_adapter.mount_point)
            except OSError:
                is_mounted = True
            if is_mounted:
                return {"ok": False, "code": "already_mounted",
                        "message": "当前磁带已挂载，不能执行初始化"}
            if _find_volume(barcode_n) is not None:
                return {"ok": False, "code": "already_registered",
                        "message": f"条码 {barcode_n} 已在库中登记，禁止格式化"}
            detected = parse_barcode_from_text(
                "\n".join((context.stdout or "", context.stderr or ""))
            )
            if detected and normalize_barcode(detected) and detected.upper() != barcode_n:
                return {"ok": False, "code": "barcode_mismatch",
                        "message": f"输入条码 {barcode_n} 与读取到的条码 {detected} 不一致"}

            operation = _start_operation("init", barcode=barcode_n)
            try:
                ok, message, _actual = tape_adapter.initialize(barcode_n, force=force)
            except Exception as exc:  # noqa: BLE001
                logger.exception("adapter.initialize 异常")
                _finish_operation(operation, ok=False,
                                  message=f"初始化异常：{type(exc).__name__}: {exc}",
                                  started=started)
                return {"ok": False, "code": "init_error",
                        "message": f"初始化失败：{type(exc).__name__}: {exc}",
                        "operation_id": operation.id}
            init_output = _last_output(tape_adapter)
            if not ok:
                _finish_operation(operation, ok=False, message=message,
                                  started=started, output=init_output)
                return {"ok": False, "code": "init_failed", "message": message,
                        "operation_id": operation.id}

            # 格式化成功后自动挂载
            try:
                mounted, mount_message, actual = tape_adapter.mount()
            except Exception as exc:  # noqa: BLE001
                logger.exception("初始化后挂载异常")
                mounted, mount_message, actual = False, f"挂载异常：{type(exc).__name__}: {exc}", None
            mount_output = _last_output(tape_adapter)
            actual_n = normalize_barcode(actual)
            if not mounted:
                _finish_operation(operation, ok=False,
                                  message=f"初始化成功但挂载失败：{mount_message}",
                                  started=started, output=mount_output)
                return {"ok": False, "code": "mount_after_init_failed",
                        "message": f"初始化成功但挂载失败：{mount_message}",
                        "operation_id": operation.id}
            if not barcode_matches(actual_n, barcode_n):
                problems = _cleanup_tape(tape_adapter)
                detail = f"初始化后条码不一致：输入 {barcode_n}，实际 {actual_n or '空'}"
                if problems:
                    detail += f"；{problems}"
                _finish_operation(operation, ok=False, message=detail,
                                  started=started, output=mount_output)
                return {"ok": False, "code": "barcode_mismatch",
                        "message": detail, "operation_id": operation.id}

            volume, created = _upsert_volume(actual_n)
            for other in _mounted_volumes():
                if other.id != volume.id:
                    _set_volume_status(other, "unknown")
            volume.status = "mounted"
            volume.device_path = getattr(tape_adapter, "device_path", None) or volume.device_path
            volume.last_mounted_at = utcnow()
            volume.last_error = None
            db.session.commit()
            _finish_operation(operation, ok=True, message=f"磁带初始化并挂载成功（{actual_n}）",
                              started=started, output=mount_output, volume_id=volume.id)
            return {
                "ok": True,
                "message": f"磁带初始化并挂载成功（{actual_n}）",
                "barcode": actual_n,
                "operation_id": operation.id,
                "volume_id": volume.id,
                "volume": _volume_dict(volume),
                "created_volume": created,
            }
    except Exception as exc:  # noqa: BLE001
        logger.exception("initialize_tape 失败")
        try:
            db.session.rollback()
        except Exception:  # pragma: no cover
            pass
        _fail_operation(getattr(operation, "id", None),
                        f"初始化异常：{type(exc).__name__}: {exc}", started)
        return {"ok": False, "code": "init_error",
                "message": f"初始化失败：{type(exc).__name__}: {exc}"}


@_serialized
def recover_list_tape(app, *, adapter: TapeAdapter | None = None) -> dict:
    """只读列出 ltfsck 回滚点，不修改磁带。"""
    try:
        with _maybe_app_context(app):
            tape_adapter = _adapter(app, adapter)
            if tape_adapter.status().mounted:
                return {"ok": False, "message": "当前磁带已挂载，不能执行索引检查", "points": []}
            ok, message, points = tape_adapter.recover_list()
            return {"ok": bool(ok), "message": message, "points": points}
    except Exception as exc:  # noqa: BLE001
        logger.exception("recover_list_tape 失败")
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}", "points": []}


@_serialized
def recover_tape_index(
    app,
    *,
    generation: str | None = None,
    deep: bool = False,
    confirmed: bool = False,
    adapter: TapeAdapter | None = None,
) -> dict:
    """执行 ltfsck 恢复并尝试挂载；恢复失败不自动格式化。"""
    started = time.monotonic()
    operation: TapeOperation | None = None
    try:
        with _maybe_app_context(app):
            if not confirmed:
                return {"ok": False, "code": "not_confirmed",
                        "message": "必须确认索引恢复操作"}
            if not generation and not deep:
                return {"ok": False, "code": "missing_generation",
                        "message": "必须指定 ltfsck 回滚点（或明确选择深度恢复）"}
            tape_adapter = _adapter(app, adapter)
            if tape_adapter.status().mounted:
                return {"ok": False, "code": "already_mounted",
                        "message": "当前磁带已挂载，不能执行索引恢复"}
            operation = _start_operation("recover", barcode=_current_barcode())
            try:
                ok, message = tape_adapter.recover_index(generation, deep=deep)
            except Exception as exc:  # noqa: BLE001
                ok, message = False, f"{type(exc).__name__}: {exc}"
            output = _last_output(tape_adapter)
            if not ok:
                _finish_operation(operation, ok=False, message=message,
                                  started=started, output=output)
                return {"ok": False, "code": "recover_failed",
                        "message": message, "operation_id": operation.id}

            mounted, mount_message, actual = tape_adapter.mount()
            mount_output = _last_output(tape_adapter)
            actual_n = normalize_barcode(actual)
            if not mounted or not barcode_is_usable(actual_n):
                problems = _cleanup_tape(tape_adapter) if mounted else ""
                detail = f"恢复成功但挂载/条码校验失败：{mount_message}"
                if problems:
                    detail += f"；{problems}"
                _finish_operation(operation, ok=False, message=detail,
                                  started=started, output=mount_output)
                return {"ok": False, "code": "mount_after_recover_failed",
                        "message": detail, "operation_id": operation.id}
            volume, _created = _upsert_volume(actual_n)
            for other in _mounted_volumes():
                if other.id != volume.id:
                    _set_volume_status(other, "unknown")
            volume.status = "mounted"
            volume.last_mounted_at = utcnow()
            volume.last_error = None
            db.session.commit()
            _finish_operation(operation, ok=True,
                              message=f"索引恢复并挂载成功（{actual_n}）",
                              started=started, output=mount_output, volume_id=volume.id)
            return {"ok": True, "message": f"索引恢复并挂载成功（{actual_n}）",
                    "barcode": actual_n, "operation_id": operation.id,
                    "volume_id": volume.id, "volume": _volume_dict(volume)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("recover_tape_index 失败")
        try:
            db.session.rollback()
        except Exception:  # pragma: no cover
            pass
        _fail_operation(getattr(operation, "id", None),
                        f"索引恢复异常：{type(exc).__name__}: {exc}", started)
        return {"ok": False, "code": "recover_error",
                "message": f"索引恢复失败：{type(exc).__name__}: {exc}"}


@_serialized
def unmount_tape(app, *, adapter: TapeAdapter | None = None) -> dict:
    """卸载当前带：adapter.unmount() + 卷 status='unmounted'。不抛异常。"""
    started = time.monotonic()
    operation: TapeOperation | None = None
    try:
        with _maybe_app_context(app):
            tape_adapter = _adapter(app, adapter)
            operation = _start_operation("unmount", barcode=_current_barcode())
            try:
                ok, message = tape_adapter.unmount()
            except Exception as exc:
                logger.exception("adapter.unmount 异常")
                _finish_operation(
                    operation,
                    ok=False,
                    message=f"卸载异常：{type(exc).__name__}: {exc}",
                    started=started,
                )
                return {"ok": False, "message": f"卸载失败：{type(exc).__name__}: {exc}",
                        "operation_id": operation.id}

            output = _last_output(tape_adapter)
            volume = _current_volume()
            if ok:
                for item in _volumes_by_status("mounted"):
                    _set_volume_status(item, "unmounted")
                db.session.commit()
                _finish_operation(operation, ok=True, message=message, started=started,
                                  output=output, volume_id=volume.id if volume else None)
                return {
                    "ok": True,
                    "message": message or "磁带卸载成功",
                    "barcode": volume.ref if volume is not None else None,
                    "status": "unmounted",
                    "operation_id": operation.id,
                    "volume": _volume_dict(volume) if volume is not None else None,
                }

            detail = str(message or "未知原因").strip()
            if volume is not None:
                _mark_volume_error(volume, detail)
            _finish_operation(operation, ok=False, message=detail, started=started,
                              output=output, volume_id=volume.id if volume else None)
            return {
                "ok": False,
                "message": detail,
                "barcode": volume.ref if volume is not None else None,
                "operation_id": operation.id,
                "volume": _volume_dict(volume) if volume is not None else None,
            }
    except Exception as exc:
        logger.exception("unmount_tape 失败")
        try:
            db.session.rollback()
        except Exception:  # pragma: no cover
            pass
        _fail_operation(getattr(operation, "id", None), f"卸载异常：{type(exc).__name__}: {exc}", started)
        return {"ok": False, "message": f"卸载失败：{type(exc).__name__}: {exc}"}


@_serialized
def eject_tape(app, *, adapter: TapeAdapter | None = None) -> dict:
    """弹出当前带：必要时先自动卸载（v1 的 eject 在挂载状态行为未定义）。不抛异常。"""
    started = time.monotonic()
    operation: TapeOperation | None = None
    try:
        with _maybe_app_context(app):
            tape_adapter = _adapter(app, adapter)
            operation = _start_operation("eject", barcode=_current_barcode())
            prefix = ""
            try:
                status = tape_adapter.status()
                if getattr(status, "mounted", False):
                    un_ok, un_message = tape_adapter.unmount()
                    if not un_ok:
                        detail = f"弹出前自动卸载失败：{un_message}"
                        _finish_operation(operation, ok=False, message=detail, started=started)
                        volume = _current_volume()
                        _mark_volume_error(volume, detail)
                        return {"ok": False, "message": detail, "operation_id": operation.id}
                    prefix = "（已先自动卸载）"
                    for item in _volumes_by_status("mounted"):
                        _set_volume_status(item, "unmounted")
                    db.session.commit()
                ok, message = tape_adapter.eject()
            except Exception as exc:
                logger.exception("adapter.eject 异常")
                _finish_operation(
                    operation,
                    ok=False,
                    message=f"弹出异常：{type(exc).__name__}: {exc}",
                    started=started,
                )
                return {"ok": False, "message": f"弹出失败：{type(exc).__name__}: {exc}",
                        "operation_id": operation.id}

            output = _last_output(tape_adapter)
            volume = _current_volume()
            if ok:
                for item in _volumes_by_status("mounted", "unmounted"):
                    _set_volume_status(item, "ejected")
                db.session.commit()
                detail = f"{message or '磁带弹出成功'}{prefix}"
                _finish_operation(operation, ok=True, message=detail, started=started,
                                  output=output, volume_id=volume.id if volume else None)
                return {
                    "ok": True,
                    "message": detail,
                    "barcode": volume.ref if volume is not None else None,
                    "status": "ejected",
                    "operation_id": operation.id,
                    "volume": _volume_dict(volume) if volume is not None else None,
                }

            detail = str(message or "未知原因").strip()
            if volume is not None:
                _mark_volume_error(volume, detail)
            _finish_operation(operation, ok=False, message=detail, started=started,
                              output=output, volume_id=volume.id if volume else None)
            return {
                "ok": False,
                "message": detail,
                "barcode": volume.ref if volume is not None else None,
                "operation_id": operation.id,
                "volume": _volume_dict(volume) if volume is not None else None,
            }
    except Exception as exc:
        logger.exception("eject_tape 失败")
        try:
            db.session.rollback()
        except Exception:  # pragma: no cover
            pass
        _fail_operation(getattr(operation, "id", None), f"弹出异常：{type(exc).__name__}: {exc}", started)
        return {"ok": False, "message": f"弹出失败：{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# 状态查询 / 列表
# ---------------------------------------------------------------------------
def _current_volume() -> LibraryVolume | None:
    """当前"带"卷：优先 DB 里最后一个 mounted，其次 unmounted（eject 前的过渡态）。"""
    volume = (
        LibraryVolume.query.filter_by(kind=_TAPE_KIND, status="mounted")
        .order_by(LibraryVolume.last_mounted_at.desc(), LibraryVolume.id.desc())
        .first()
    )
    if volume is not None:
        return volume
    return (
        LibraryVolume.query.filter_by(kind=_TAPE_KIND, status="unmounted")
        .order_by(LibraryVolume.last_mounted_at.desc(), LibraryVolume.id.desc())
        .first()
    )


def _current_barcode() -> str | None:
    volume = _current_volume()
    return volume.ref if volume is not None else None


def _volumes_by_status(*statuses: str) -> list[LibraryVolume]:
    return LibraryVolume.query.filter(
        LibraryVolume.kind == _TAPE_KIND, LibraryVolume.status.in_(statuses)
    ).all()


def current_status(
    app, *, adapter: TapeAdapter | None = None, include_device_details: bool = False
) -> dict:
    """当前设备/磁带/操作状态（只读，不写库）。不抛异常。

    ``include_device_details=False``（默认）时**不执行** device_info/device_list：
    这两条 ltfs 命令在真机上可能卡住/占设备，页面状态只依赖 status()
    （设备节点是否存在、是否挂载、挂载点里的条码）。需要型号/序列号时显式传 True。
    """
    try:
        with _maybe_app_context(app):
            tape_adapter = _adapter(app, adapter)
            status = tape_adapter.status()
            device_info: dict = {}
            device_list: list = []
            if include_device_details:
                try:
                    device_info = tape_adapter.device_info()
                except Exception as exc:  # pragma: no cover - 适配器不应抛
                    device_info = {"model": None, "serial_number": None,
                                   "firmware_version": None, "raw": None,
                                   "error": f"{type(exc).__name__}: {exc}"}
                try:
                    device_list = tape_adapter.device_list()
                except Exception as exc:  # pragma: no cover
                    logger.warning("device_list 失败: %s", exc)
                    device_list = []
            try:
                volume_label = tape_adapter.volume_label()
            except Exception:  # pragma: no cover
                volume_label = None

            mounted = bool(getattr(status, "mounted", False))
            barcode = normalize_barcode(getattr(status, "barcode", None))
            volume = _find_volume(barcode) if barcode else None
            if volume is None:
                volume = _current_volume()
            current_barcode = barcode or (volume.ref if volume is not None else None)

            device_exists = bool(getattr(status, "device_exists", False))
            device_path = (
                getattr(status, "device_path", None)
                or getattr(tape_adapter, "device_path", None)
                or (device_list[0]["path"] if device_list else None)
            )
            if device_exists and mounted and current_barcode:
                message = f"已挂载：{current_barcode}"
            elif device_exists and mounted:
                message = "已挂载（条码未知）"
            elif device_exists:
                message = "设备就绪，当前未挂载磁带"
            else:
                message = "未检测到磁带设备"

            errors: list[str] = []
            raw = getattr(status, "raw", None)
            if isinstance(raw, dict) and raw.get("errors"):
                errors = [str(item) for item in raw["errors"]]
            if isinstance(device_info, dict) and device_info.get("error"):
                errors.append(str(device_info["error"]))

            result = {
                "ok": True,
                "message": message,
                "mounted": mounted,
                "barcode": current_barcode,
                "device_exists": device_exists,
                "device_path": str(device_path) if device_path else None,
                "volume_label": volume_label,
                "current_volume": _volume_dict(volume) if volume is not None else None,
                "device_info": device_info,
                "device_list": device_list,
                "adapter": type(tape_adapter).__name__,
                "mount_point": str(getattr(tape_adapter, "mount_point", "")),
                "status": {
                    "mounted": mounted,
                    "barcode": current_barcode,
                    "device_exists": device_exists,
                    "device_path": str(device_path) if device_path else None,
                    "volume_label": volume_label,
                },
                "errors": errors,
            }
            if volume is not None:
                result["files"] = _volume_file_count(volume)
            return result
    except Exception as exc:
        logger.exception("current_status 失败")
        try:
            db.session.rollback()
        except Exception:  # pragma: no cover
            pass
        return {
            "ok": False,
            "message": f"状态读取失败：{type(exc).__name__}: {exc}",
            "mounted": False,
            "barcode": None,
            "device_exists": False,
            "device_path": None,
            "status": {"mounted": False, "barcode": None, "device_exists": False},
            "device_info": {},
            "device_list": [],
            "errors": [f"{type(exc).__name__}: {exc}"],
        }


def device_snapshot(app, *, adapter: TapeAdapter | None = None) -> dict:
    """``/api/tape/device`` 用：设备列表 + 设备信息 + 当前状态（显式查详情）。"""
    return current_status(app, adapter=adapter, include_device_details=True)


def list_tape_volumes(app) -> list[dict]:
    """DB 里所有磁带卷 + 未软删文件数（JSON 友好）。不抛异常。"""
    try:
        with _maybe_app_context(app):
            rows = (
                db.session.query(LibraryVolume, func.count(LibraryFile.id))
                .outerjoin(
                    LibraryFile,
                    (LibraryFile.volume_id == LibraryVolume.id)
                    & (LibraryFile.deleted_at.is_(None)),
                )
                .filter(LibraryVolume.kind == _TAPE_KIND)
                .group_by(LibraryVolume.id)
                .order_by(LibraryVolume.ref)
                .all()
            )
            return [_volume_dict(volume, files=int(count or 0)) for volume, count in rows]
    except Exception as exc:
        logger.exception("list_tape_volumes 失败")
        try:
            db.session.rollback()
        except Exception:  # pragma: no cover
            pass
        return []


def list_operations(app, limit: int = 100, op_type: str | None = None) -> list[dict]:
    """磁带操作日志（新→旧），可按 op_type 过滤。不抛异常。"""
    try:
        with _maybe_app_context(app):
            try:
                capped = max(1, min(int(limit or 100), 1000))
            except (TypeError, ValueError):
                capped = 100
            query = TapeOperation.query
            if op_type:
                query = query.filter(TapeOperation.op_type == str(op_type))
            rows = query.order_by(TapeOperation.id.desc()).limit(capped).all()
            return [
                {
                    "id": row.id,
                    "volume_id": row.volume_id,
                    "barcode": row.barcode,
                    "op_type": row.op_type,
                    "status": row.status,
                    "message": row.message,
                    "command": row.command,
                    "stdout": row.stdout,
                    "stderr": row.stderr,
                    "started_at": _iso(row.started_at),
                    "finished_at": _iso(row.finished_at),
                    "duration_ms": row.duration_ms,
                }
                for row in rows
            ]
    except Exception as exc:
        logger.exception("list_operations 失败")
        try:
            db.session.rollback()
        except Exception:  # pragma: no cover
            pass
        return []
