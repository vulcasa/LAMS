"""容器 ↔ 磁带 文件传输（合并 v1 ltfs-webui 的 file-browser 传输能力）。

对外只有一个入口::

    run_transfer(app, direction, sources, target_dir, *,
                 move=False, progress=None, create_target=True) -> dict

- ``direction``: ``container_to_tape``（NAS/容器 → 磁带挂载点）或
  ``tape_to_container``（磁带挂载点 → 容器）。
- ``sources``: 绝对路径列表（文件或目录；单个字符串也接受）。目录递归传输，
  目标目录下按源 basename 建一层，保留相对目录结构（含空目录）。
- ``target_dir``: 目标目录（绝对路径）。``create_target=True``（默认）时不存在会自动
  创建，但**必须先通过落在允许根内的安全校验**（越界路径连 mkdir 都不会发生）；
  ``create_target=False`` 则要求目录已存在。同名的既有文件会被覆盖。
- ``move=True``: **整单所有文件复制并 rename 成功之后**才删除源（文件 unlink /
  目录 rmtree；源本身是软链时只删链接），失败时一个源都不删。
- ``progress(done_bytes: int, current_file: str)``: 可选进度回调；``current_file``
  是相对 ``target_dir`` 的 POSIX 路径。

返回值::

    {"ok": bool, "transfer_id": int | None, "status": "done" | "failed",
     "direction", "move", "target_dir", "file_count", "total_bytes",
     "done_bytes", "files": [{"src","dst","rel","size"}], "error", "elapsed_s"}

**不抛异常**：参数/路径/挂载校验失败、复制失败都写 ``Transfer.status='failed'`` +
``error``，并返回 ``ok=False``；已经复制完成的文件保留，已写入的 ``Transfer`` 行
（含 ``file_list`` 计划）保留，方便 UI 展示。调用方负责 ``app.app_context()``。

安全模型（路径来自浏览器，一律当不可信输入）：
1. 两侧路径先 ``os.path.realpath`` 再判根：container 侧必须在
   ``Config.BROWSE_ROOTS``（默认 /mnt,/data）内；tape 侧必须在 ``Config.LTFS_MOUNT``
   内，且 ``_tape_is_mounted(app)`` 为真。
2. 符号链接逃逸：源树里每个文件的 realpath 必须仍在该侧根内；目标每个文件/目录的
   真实父路径必须在 ``target_dir`` 内（防目标树里预埋的软链目录把写入引到外面）。
   目录软链不跟随（``os.walk(followlinks=False)``），也不会被创建。
3. ``..`` 由 realpath 消解；消解后越界即拒绝。
4. 源=目标、目标在源目录内部（递归复制自己）直接拒绝。
5. 跨方向混乱：container 侧路径落在 ``LTFS_MOUNT`` 内（或 tape 侧路径跑出挂载点）
   直接拒绝。
6. 多个源在目标目录下重名、源之间互相包含，直接拒绝（避免静默互相覆盖）。

传输语义（与 v1 ``app/utils/file_transfer.py`` 的差异）：
- 分块 ``Config.TRANSFER_CHUNK``（默认 8MiB）读到 ``<dest>.part``，flush+fsync 后
  ``os.replace`` 原子改名；v1 直接写正式文件名，中断会留下半截文件。
- 进度落库每「完成一个文件」一次；v1 每 1MiB 一次 commit（D13 写放大）。
- move 是「整单成功才删源」；v1 逐文件 copy 完立即删，后续文件失败会丢源。
- 复制前后校验源文件大小（并发改写时宁可失败，不留截断文件）。
- 同步函数、不自己起线程（由 jobs worker 调度）；v1 是 daemon 线程 + UUID operation_id。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from app.config import Config
from app.db import db
from app.models import Transfer, utcnow

__all__ = [
    "DIRECTION_CONTAINER_TO_TAPE",
    "DIRECTION_TAPE_TO_CONTAINER",
    "DIRECTIONS",
    "TransferError",
    "run_transfer",
]

log = logging.getLogger(__name__)

DIRECTION_CONTAINER_TO_TAPE = "container_to_tape"
DIRECTION_TAPE_TO_CONTAINER = "tape_to_container"
DIRECTIONS = (DIRECTION_CONTAINER_TO_TAPE, DIRECTION_TAPE_TO_CONTAINER)

#: Transfer.error 里最多存多少字符（异常栈/巨长路径别把 SQLite 撑爆）
MAX_ERROR_TEXT = 4000


class TransferError(Exception):
    """参数 / 路径 / 挂载校验失败：整单拒绝，不开始复制。"""


# ---------------------------------------------------------------------------
# 路径与配置工具
# ---------------------------------------------------------------------------
def _real_path(path) -> str:
    """绝对路径 + 消解符号链接与 `..`（不要求路径存在）。"""
    return os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))


def _is_within(path, root) -> bool:
    """realpath(path) 是否等于根或在根目录之下（二者都先 realpath）。"""
    p = _real_path(path)
    r = _real_path(root)
    if p == r:
        return True
    r = r.rstrip(os.sep)
    if not r:  # 根本身就是 "/"
        return p.startswith(os.sep)
    return p.startswith(r + os.sep)


def _within_any(path, roots) -> bool:
    return any(_is_within(path, root) for root in roots)


def _tape_is_mounted(app) -> bool:
    """磁带是否已挂载。

    只做本地文件系统检查（不跑 ltfs 命令、不阻塞）：挂载点目录存在，并且是
    挂载点（Linux ``os.path.ismount``）或带有 LTFS 写入的 ``.volume_label`` /
    ``.ltfs`` 标记。测试里直接 monkeypatch 本函数即可。
    """
    mount = app.config.get("LTFS_MOUNT", Config.LTFS_MOUNT)
    if not mount or not str(mount).strip():
        return False
    mount_path = str(mount)
    try:
        if not os.path.isdir(mount_path):
            return False
        if os.path.ismount(mount_path):
            return True
        for marker in (".volume_label", ".ltfs"):
            if os.path.isfile(os.path.join(mount_path, marker)):
                return True
    except OSError:  # pragma: no cover - 权限/IO 异常一律当未挂载
        return False
    return False


def _container_roots(app) -> list[str]:
    """BROWSE_ROOTS + 已登记媒体库（MediaSource）路径；realpath 去重保序。"""
    raw = app.config.get("BROWSE_ROOTS", Config.BROWSE_ROOTS)
    if isinstance(raw, str):
        raw = raw.split(",")
    roots: list[str] = []
    for item in raw or []:
        text = str(item).strip()
        if not text:
            continue
        real = _real_path(text)
        if real not in roots:
            roots.append(real)
    # 「介质」页登记的媒体库（NAS 挂载点等）也允许作为源/目标
    try:
        from app.models import MediaSource

        for source in MediaSource.query.filter_by(enabled=True).all():
            real = _real_path(source.path)
            if real not in roots:
                roots.append(real)
    except Exception:  # noqa: BLE001 - 数据库未初始化/测试环境
        pass
    if not roots:
        raise TransferError("未配置 BROWSE_ROOTS（容器侧允许访问的根目录）")
    return roots


def container_roots(app) -> list[str]:
    """公开：当前允许浏览/传输的容器侧根目录（含媒体库）。"""
    return _container_roots(app)


def _tape_root(app) -> str:
    mount = app.config.get("LTFS_MOUNT", Config.LTFS_MOUNT)
    if not mount or not str(mount).strip():
        raise TransferError("未配置 LTFS_MOUNT（磁带挂载点）")
    return _real_path(mount)


def _assert_no_self_copy(*, src_real: str, dst_top: str, is_dir: bool, target_real: str) -> None:
    """拒绝「源=目标」与「目标在源目录内部」（后者会边写边读、递归复制自己）。"""
    if _real_path(dst_top) == _real_path(src_real):
        raise TransferError(f"源=目标，拒绝操作: {src_real}")
    if is_dir and _is_within(target_real, src_real):
        raise TransferError(
            f"目标在源目录内部，会递归复制自己: 源 {src_real} 目标 {target_real}"
        )


def _chunk_size(app) -> int:
    raw = app.config.get("TRANSFER_CHUNK", Config.TRANSFER_CHUNK)
    try:
        size = int(raw)
    except (TypeError, ValueError):
        size = Config.TRANSFER_CHUNK
    return max(1, size)


def _sources_text(sources) -> str:
    try:
        if isinstance(sources, (str, os.PathLike)):
            items = [str(sources)]
        else:
            items = [str(s) for s in sources]
        return json.dumps(items, ensure_ascii=False)[:MAX_ERROR_TEXT]
    except Exception:  # noqa: BLE001 - 只用于审计字段，永不因此失败
        return str(sources)[:MAX_ERROR_TEXT]


def _normalize_direction(direction) -> str:
    value = str(direction or "").strip()
    if value not in DIRECTIONS:
        raise TransferError(
            f"direction 只能是 {' 或 '.join(DIRECTIONS)}，收到 {direction!r}"
        )
    return value


def _normalize_sources(sources) -> list[str]:
    if isinstance(sources, (str, os.PathLike)):
        raw: list = [sources]
    else:
        try:
            raw = list(sources or [])
        except TypeError:
            raise TransferError("sources 必须是路径或路径列表") from None
    if not raw:
        raise TransferError("没有指定要传输的源文件/目录")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, (str, os.PathLike)):
            raise TransferError(f"sources 里含非路径项: {item!r}")
        text = str(item).strip()
        if not text:
            raise TransferError("sources 里含空路径")
        if not os.path.isabs(text):
            raise TransferError(f"源路径必须是绝对路径: {text!r}")
        real = _real_path(text)
        if real in seen:  # 同一路径重复给只传一次
            continue
        seen.add(real)
        out.append(text)
    return out


# ---------------------------------------------------------------------------
# 计划
# ---------------------------------------------------------------------------
@dataclass
class _Unit:
    """一个源（文件或目录）及其目标顶层路径。"""

    src: str          # realpath（读取用，已消解软链/..）
    origin: str       # 用户给的规范化路径（未消解软链；move 删它，删软链本身而不是目标）
    name: str         # basename(origin)，目标目录下的名字
    is_dir: bool
    dst: str          # target_dir/name


@dataclass
class _PlannedFile:
    src: str          # 待读的 realpath
    dst: str          # 目标绝对路径
    rel: str          # 相对 target_dir 的 POSIX 路径（显示/进度用）
    size: int


@dataclass
class _Plan:
    direction: str
    target_real: str
    units: list[_Unit] = field(default_factory=list)
    files: list[_PlannedFile] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)
    total_bytes: int = 0
    target_missing: bool = False   # 目标不存在且 create_target=True：计划通过后再 mkdir


def _plan_file(
    *,
    src_path: str,
    dst: str,
    rel: str,
    source_roots: list[str],
    target_real: str,
) -> Optional[_PlannedFile]:
    """校验单个源文件并生成计划项；非普通文件返回 None（跳过 FIFO/socket 等）。"""
    real = _real_path(src_path)
    if not _within_any(real, source_roots):
        raise TransferError(f"符号链接逃逸：{src_path} -> {real} 不在允许的根内")
    if not os.path.isfile(real):
        return None
    parent_real = _real_path(os.path.dirname(dst))
    if not _is_within(parent_real, target_real):
        raise TransferError(f"目标路径逃逸出目标目录（软链目录？）: {dst}")
    try:
        size = os.path.getsize(real)
    except OSError as exc:
        raise TransferError(f"无法读取源文件 {src_path}: {exc}") from exc
    return _PlannedFile(src=real, dst=dst, rel=rel, size=size)


def _plan_transfer(app, direction, sources, target_dir, *, create_target: bool = True) -> _Plan:
    direction = _normalize_direction(direction)
    container_roots = _container_roots(app)
    tape_root = _tape_root(app)
    if not _tape_is_mounted(app):
        raise TransferError(f"磁带未挂载，拒绝传输（挂载点 {tape_root}）")

    target_raw = str(target_dir or "").strip()
    if not target_raw:
        raise TransferError("target_dir 不能为空")
    if not os.path.isabs(target_raw):
        raise TransferError(f"target_dir 必须是绝对路径: {target_raw!r}")
    target_real = _real_path(target_raw)

    # 先做「目标必须落在允许侧根内」的安全校验，再决定要不要建目录：
    # 越界的目标连 mkdir 都不能发生（否则等于给了任意建目录的能力）。
    if direction == DIRECTION_CONTAINER_TO_TAPE:
        if not _is_within(target_real, tape_root):
            raise TransferError(f"目标目录必须落在磁带挂载点内: {target_real} -> {tape_root}")
        source_roots = container_roots
    else:
        if not _within_any(target_real, container_roots):
            raise TransferError(
                f"目标目录必须落在 BROWSE_ROOTS 内: {target_real} -> {', '.join(container_roots)}"
            )
        if _is_within(target_real, tape_root):
            raise TransferError(f"目标目录落在磁带挂载点内，方向不符（跨方向）: {target_real}")
        source_roots = [tape_root]

    # 目标不存在：校验通过且 create_target=True 才允许创建；真正的 mkdir 放到
    # 源计划全部校验通过之后（见 run_transfer），避免「源非法却先建了个空目录」。
    target_missing = False
    if not os.path.isdir(target_real):
        if os.path.exists(target_real):
            raise TransferError(f"目标路径已存在但不是目录: {target_raw}")
        if not create_target:
            raise TransferError(f"目标目录不存在: {target_raw}（create_target=False）")
        target_missing = True

    plan = _Plan(direction=direction, target_real=target_real, target_missing=target_missing)
    by_name: dict[str, str] = {}

    for raw_source in _normalize_sources(sources):
        origin = os.path.abspath(os.path.expanduser(str(raw_source)))
        src_real = _real_path(raw_source)
        if not os.path.exists(src_real):
            raise TransferError(f"源不存在: {raw_source}")
        is_dir = os.path.isdir(src_real)
        is_file = os.path.isfile(src_real)
        if not (is_dir or is_file):
            raise TransferError(f"源不是普通文件或目录: {raw_source}")

        # --- 侧别 / 跨方向校验（用 realpath，软链逃逸会在这里/计划阶段被拒）---
        if direction == DIRECTION_CONTAINER_TO_TAPE:
            if not _within_any(src_real, container_roots):
                raise TransferError(
                    f"源必须是容器侧路径（BROWSE_ROOTS 内）: {src_real} -> "
                    f"{', '.join(container_roots)}"
                )
            if _is_within(src_real, tape_root):
                raise TransferError(f"源落在磁带挂载点内，方向不符（跨方向）: {src_real}")
        else:
            if not _is_within(src_real, tape_root):
                raise TransferError(f"源必须落在磁带挂载点内: {src_real} -> {tape_root}")

        name = os.path.basename(origin.rstrip(os.sep))
        if not name:
            raise TransferError(f"无法确定源名称（不能直接传文件系统根）: {src_real}")
        dst_top = os.path.join(target_real, name)
        _assert_no_self_copy(
            src_real=src_real, dst_top=dst_top, is_dir=is_dir, target_real=target_real
        )

        previous = by_name.get(name)
        if previous is not None and previous != src_real:
            raise TransferError(
                f"多个源在目标目录下同名，会互相覆盖: {name!r} ({previous} 与 {src_real})"
            )
        by_name[name] = src_real
        plan.units.append(
            _Unit(src=src_real, origin=origin, name=name, is_dir=is_dir, dst=dst_top)
        )

    # 源之间互相包含（一个目录里又单独选了它下面的文件）会让计划语义混乱
    for i, unit in enumerate(plan.units):
        if not unit.is_dir:
            continue
        for j, other in enumerate(plan.units):
            if i != j and _is_within(other.src, unit.src):
                raise TransferError(
                    f"源之间存在包含关系: {other.src} 在目录源 {unit.src} 内"
                )

    for unit in plan.units:
        if unit.is_dir:
            plan.dirs.append(unit.dst)
            walk_errors: list[OSError] = []

            def _on_walk_error(exc: OSError, _errors=walk_errors) -> None:
                _errors.append(exc)

            for dirpath, dirnames, filenames in os.walk(
                unit.src, topdown=True, onerror=_on_walk_error, followlinks=False
            ):
                rel_dir = os.path.relpath(dirpath, unit.src)
                rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
                # 目录软链不跟随、也不在目标建目录（followlinks=False 已经不下钻）
                dirnames.sort()
                for dirname in list(dirnames):
                    full = os.path.join(dirpath, dirname)
                    if os.path.islink(full):
                        dirnames.remove(dirname)
                        continue
                    rel = f"{rel_dir}/{dirname}" if rel_dir else dirname
                    dst_dir = os.path.join(unit.dst, *rel.split("/"))
                    if not _is_within(_real_path(dst_dir), target_real):
                        raise TransferError(f"目标路径逃逸出目标目录（软链目录？）: {dst_dir}")
                    plan.dirs.append(dst_dir)
                for filename in sorted(filenames):
                    full = os.path.join(dirpath, filename)
                    rel = f"{rel_dir}/{filename}" if rel_dir else filename
                    rel = rel.replace(os.sep, "/")
                    dst = os.path.join(unit.dst, *rel.split("/"))
                    planned = _plan_file(
                        src_path=full,
                        dst=dst,
                        rel=f"{unit.name}/{rel}",
                        source_roots=source_roots,
                        target_real=target_real,
                    )
                    if planned is not None:
                        plan.files.append(planned)
            if walk_errors:
                exc = walk_errors[0]
                failed_dir = getattr(exc, "filename", "") or unit.src
                raise TransferError(f"读取源目录失败 {failed_dir}: {exc}")
        else:
            planned = _plan_file(
                src_path=unit.src,
                dst=unit.dst,
                rel=unit.name,
                source_roots=source_roots,
                target_real=target_real,
            )
            if planned is None:
                raise TransferError(f"源不是普通文件: {unit.src}")
            plan.files.append(planned)

    plan.total_bytes = sum(item.size for item in plan.files)
    return plan


# ---------------------------------------------------------------------------
# 复制
# ---------------------------------------------------------------------------
def _emit_progress(progress, done_bytes: int, current_file: str) -> None:
    if progress is None:
        return
    try:
        progress(int(done_bytes), current_file)
    except Exception:  # noqa: BLE001 - UI 回调炸了不能弄丢传输结果
        log.exception("transfer progress callback failed (ignored)")


def _copy_file(src: str, dst: str, chunk_size: int, on_chunk=None) -> int:
    """分块复制 src → ``dst.part``，fsync 后原子 rename；返回字节数。

    失败时删掉 .part 再抛，绝不让半截文件以正式文件名出现。
    """
    tmp = dst + ".part"
    parent = os.path.dirname(dst)
    os.makedirs(parent, exist_ok=True)
    written = 0
    try:
        with open(src, "rb") as fsrc:
            expected = os.fstat(fsrc.fileno()).st_size
            with open(tmp, "wb") as fdst:
                while True:
                    data = fsrc.read(chunk_size)
                    if not data:
                        break
                    fdst.write(data)
                    written += len(data)
                    if on_chunk is not None:
                        on_chunk(written)
                fdst.flush()
                os.fsync(fdst.fileno())
        if written != expected:
            raise OSError(
                f"源文件在复制期间发生变化（打开时 {expected} 字节，实际读 {written} 字节）"
            )
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return written


def _assert_dest_parent_within(dst: str, target_real: str) -> None:
    parent_real = _real_path(os.path.dirname(dst))
    if not _is_within(parent_real, target_real):
        raise TransferError(f"目标路径逃逸出目标目录（软链目录？）: {dst}")


def _prepare_dirs(plan: _Plan) -> None:
    for directory in plan.dirs:
        # 创建前先按 realpath 校验：目标树里预埋的软链目录不能把写入引到外面
        if not _is_within(_real_path(directory), plan.target_real):
            raise TransferError(f"目标路径逃逸出目标目录（软链目录？）: {directory}")
    for directory in plan.dirs:
        os.makedirs(directory, exist_ok=True)


def _delete_source_unit(unit: _Unit) -> None:
    """move 成功后删源：软链只删链接本身，绝不顺着软链删到别处。"""
    if os.path.islink(unit.origin):
        os.unlink(unit.origin)
    elif unit.is_dir:
        shutil.rmtree(unit.origin)
    else:
        os.remove(unit.origin)


# ---------------------------------------------------------------------------
# Transfer 行与返回
# ---------------------------------------------------------------------------
def _create_transfer(direction, sources, target_dir, move) -> Optional[Transfer]:
    try:
        row = Transfer(
            direction=str(direction if direction is not None else "").strip()[:24] or "unknown",
            move=bool(move),
            source_summary=_sources_text(sources),
            target_dir=str(target_dir or ""),
            status="running",
            started_at=utcnow(),
            file_count=0,
            total_bytes=0,
            done_bytes=0,
            file_list="[]",
        )
        db.session.add(row)
        db.session.commit()
        return row
    except Exception:  # noqa: BLE001 - 建行失败也不能把异常抛给 UI
        log.exception("创建 Transfer 记录失败")
        db.session.rollback()
        return None


def _fail(
    transfer: Optional[Transfer],
    error: str,
    started: float,
    *,
    entries: Optional[list[dict]] = None,
) -> dict:
    message = str(error)[:MAX_ERROR_TEXT]
    result = {
        "ok": False,
        "transfer_id": getattr(transfer, "id", None),
        "status": "failed",
        "direction": getattr(transfer, "direction", None),
        "move": bool(getattr(transfer, "move", False)),
        "target_dir": getattr(transfer, "target_dir", None),
        "file_count": int(getattr(transfer, "file_count", 0) or 0),
        "total_bytes": int(getattr(transfer, "total_bytes", 0) or 0),
        "done_bytes": int(getattr(transfer, "done_bytes", 0) or 0),
        "files": entries or [],
        "error": message,
        "elapsed_s": round(time.monotonic() - started, 3),
    }
    if transfer is not None:
        try:
            transfer.status = "failed"
            transfer.error = message
            transfer.finished_at = utcnow()
            db.session.commit()
        except Exception:  # noqa: BLE001
            log.exception("标记 Transfer failed 失败")
            db.session.rollback()
    log.warning("transfer %s failed: %s", result["transfer_id"], message)
    return result


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def tape_is_mounted(app) -> bool:
    """公开的挂载判定（路由/UI 与传输共用同一口径）。"""
    return _tape_is_mounted(app)


def _repoint_moved_files(app, plan_files, direction: str) -> int:
    """移动成功后，把片库里旧位置的行改指到新位置（NAS ↔ 磁带）。

    只处理数据库里已登记过的文件；找不到对应行就跳过。目标位置已有行时合并
    （保留目标行的 title_id/元数据，删掉旧行），避免唯一约束冲突。
    """
    from app.models import LibraryFile, LibraryVolume

    try:
        from app.services.tape_adapter import read_volume_label
    except Exception:  # pragma: no cover
        read_volume_label = lambda *_a, **_k: None  # noqa: E731

    if not plan_files:
        return 0
    mount = Path(_tape_root(app))
    barcode = read_volume_label(mount)
    tape_vol = None
    if barcode:
        tape_vol = LibraryVolume.query.filter_by(kind="tape", ref=barcode).first()
    if tape_vol is None:
        tape_vol = LibraryVolume.query.filter_by(kind="tape", status="mounted").first()
    if tape_vol is None:
        log.warning("repoint 跳过：数据库里没有当前磁带卷（%s）", barcode)
        return 0

    container_vols = LibraryVolume.query.filter(LibraryVolume.kind != "tape").all()

    def locate(path, tape_side: bool):
        real = _real_path(path)
        if tape_side:
            if _is_within(real, mount):
                return tape_vol, os.path.relpath(real, str(mount))
            return None
        for vol in container_vols:
            if _is_within(real, vol.ref):
                return vol, os.path.relpath(real, _real_path(vol.ref))
        return None

    updated = 0
    for item in plan_files:
        try:
            src, dst = str(item.src), str(item.dst)
            if direction == DIRECTION_CONTAINER_TO_TAPE:
                old, new = locate(src, False), locate(dst, True)
            else:
                old, new = locate(src, True), locate(dst, False)
            if old is None or new is None:
                continue
            old_vol, old_rel = old
            new_vol, new_rel = new
            row = (
                LibraryFile.query.filter_by(
                    volume_id=old_vol.id, rel_path=old_rel, deleted_at=None
                ).first()
            )
            if row is None:
                continue
            existing = LibraryFile.query.filter_by(
                volume_id=new_vol.id, rel_path=new_rel
            ).first()
            if existing is not None and existing.id != row.id:
                if existing.title_id is None:
                    existing.title_id = row.title_id
                db.session.delete(row)
            else:
                row.volume_id = new_vol.id
                row.rel_path = new_rel
                row.name = os.path.basename(new_rel)
                row.seen_at = utcnow()
            updated += 1
        except Exception:  # noqa: BLE001 - 元数据更新失败不能影响已完成的传输
            log.exception("repoint 失败: %s", getattr(item, "src", "?"))
            db.session.rollback()
    if updated:
        try:
            db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()
            log.exception("repoint 提交失败")
            return 0
    return updated


def run_transfer(
    app,
    direction,
    sources,
    target_dir,
    *,
    move: bool = False,
    progress: Optional[Callable[[int, str], None]] = None,
    create_target: bool = True,
) -> dict:
    """容器 ↔ 磁带 传输（同步执行，不抛异常，详见模块 docstring）。

    ``create_target=True``（默认）时，目标目录不存在会在**通过安全校验后**自动创建；
    ``False`` 则要求目标目录已存在（批量脚本/只读演练用）。
    """
    started = time.monotonic()
    # 先把 sources 固化成 list，避免调用方传 generator 时被审计字段/校验各消费一次
    if not isinstance(sources, (str, os.PathLike)):
        try:
            sources = list(sources or [])
        except TypeError:
            pass
    transfer = _create_transfer(direction, sources, target_dir, move)
    if transfer is None:
        return {
            "ok": False,
            "transfer_id": None,
            "status": "failed",
            "direction": direction,
            "move": bool(move),
            "target_dir": str(target_dir or ""),
            "file_count": 0,
            "total_bytes": 0,
            "done_bytes": 0,
            "files": [],
            "error": "创建 Transfer 记录失败（数据库不可用）",
            "elapsed_s": round(time.monotonic() - started, 3),
        }

    try:
        plan = _plan_transfer(
            app, direction, sources, target_dir, create_target=bool(create_target)
        )
    except TransferError as exc:
        return _fail(transfer, str(exc), started)
    except Exception as exc:  # noqa: BLE001 - 计划阶段的意外错误也归 failed
        return _fail(transfer, f"计划失败: {type(exc).__name__}: {exc}", started)

    entries = [
        {"src": item.src, "dst": item.dst, "rel": item.rel, "size": item.size}
        for item in plan.files
    ]
    try:
        transfer.direction = plan.direction
        transfer.target_dir = plan.target_real
        transfer.file_count = len(plan.files)
        transfer.total_bytes = plan.total_bytes
        transfer.done_bytes = 0
        transfer.file_list = json.dumps(entries, ensure_ascii=False)
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        return _fail(transfer, f"写 Transfer 计划失败: {exc}", started, entries=entries)

    chunk_size = _chunk_size(app)
    done_bytes = 0
    current_rel = ""
    last_progress_commit = 0.0

    try:
        # 源计划全部通过校验后才真正创建缺失的目标目录（越界路径上面已经拒绝）
        if plan.target_missing:
            os.makedirs(plan.target_real, exist_ok=True)
            if not os.path.isdir(plan.target_real):  # pragma: no cover - 极端并发
                raise TransferError(f"目标目录创建后仍不可用: {plan.target_real}")
        _prepare_dirs(plan)
        for item in plan.files:
            _assert_dest_parent_within(item.dst, plan.target_real)
            current_rel = item.rel
            transfer.current_file = item.rel  # 内存中先标记，失败时 _fail 落库

            def _on_chunk(written: int, _item=item, _base=done_bytes) -> None:
                nonlocal last_progress_commit
                total_done = _base + written
                _emit_progress(progress, total_done, _item.rel)
                # 大文件复制时也把进度落库，文件页面才能显示连续百分比和 ETA。
                now_mono = time.monotonic()
                if now_mono - last_progress_commit >= 1.0:
                    try:
                        transfer.done_bytes = total_done
                        transfer.current_file = _item.rel
                        db.session.commit()
                        last_progress_commit = now_mono
                    except Exception:  # noqa: BLE001 - 进度落库失败不影响实际复制
                        db.session.rollback()

            written = _copy_file(item.src, item.dst, chunk_size, _on_chunk)
            done_bytes += written
            transfer.done_bytes = done_bytes
            transfer.current_file = item.rel
            db.session.commit()  # 每完成一个文件落一次进度（v1 是每 1MiB 一次）
            _emit_progress(progress, done_bytes, item.rel)
    except TransferError as exc:
        return _fail(transfer, str(exc), started, entries=entries)
    except Exception as exc:  # noqa: BLE001
        where = f"（当前文件 {current_rel}）" if current_rel else ""
        return _fail(
            transfer,
            f"复制失败{where}: {type(exc).__name__}: {exc}",
            started,
            entries=entries,
        )

    repointed = 0
    if move:
        delete_errors: list[str] = []
        for unit in plan.units:
            try:
                _delete_source_unit(unit)
            except OSError as exc:
                delete_errors.append(f"{unit.origin}: {exc}")
        if delete_errors:
            return _fail(
                transfer,
                "已全部复制成功，但删除源失败：" + "；".join(delete_errors),
                started,
                entries=entries,
            )
        # 源已删除：把片库里的旧位置改指到新位置（NAS → 磁带 / 磁带 → NAS）
        try:
            repointed = _repoint_moved_files(app, plan.files, plan.direction)
        except Exception:  # noqa: BLE001
            log.exception("移动后更新片库位置失败")
            repointed = 0

    transfer_id = transfer.id
    try:
        transfer.status = "done"
        transfer.done_bytes = plan.total_bytes
        transfer.current_file = None
        transfer.error = None
        transfer.finished_at = utcnow()
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        return _fail(transfer, f"写 Transfer 完成状态失败: {exc}", started, entries=entries)

    _emit_progress(progress, plan.total_bytes, "")
    elapsed = round(time.monotonic() - started, 3)
    log.info(
        "transfer %s done: direction=%s move=%s files=%d bytes=%d elapsed=%.2fs",
        transfer_id,
        plan.direction,
        bool(move),
        len(plan.files),
        plan.total_bytes,
        elapsed,
    )
    return {
        "ok": True,
        "transfer_id": transfer_id,
        "status": "done",
        "direction": plan.direction,
        "move": bool(move),
        "target_dir": plan.target_real,
        "file_count": len(plan.files),
        "total_bytes": plan.total_bytes,
        "done_bytes": plan.total_bytes,
        "files": entries,
        "repointed": repointed,
        "error": None,
        "elapsed_s": elapsed,
    }
