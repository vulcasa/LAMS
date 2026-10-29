"""SQLite 主库备份 / 校验 / 列表 / 删除。

背景与安全边界
--------------
v1（ltfs-webui）的备份页有一个真实事故：「恢复成功但没生效」——它在服务进程还
活着的时候直接覆盖了 ``avlib.db``，进程继续用旧连接 / 旧 WAL 写盘，表面恢复、
实际数据没回来。所以本模块**只做四件事**：

* :func:`create_backup` —— 用 ``sqlite3.Connection.backup()`` 在线热备（WAL 下也
  一致，**绝不直接 cp**），生成 ``avlib-YYYYmmdd-HHMMSS.db`` + ``.sha256`` 边车，
  并按 ``keep`` 轮换旧备份；
* :func:`list_backups` —— 列出备份（名称/大小/时间/sha256）；
* :func:`verify_backup` —— 重算 sha256 对比边车 + ``PRAGMA integrity_check``；
* :func:`delete_backup` —— 删除备份与边车（防目录穿越）。

**在线恢复刻意不提供**：SQLite 不支持安全的热替换，替换文件时活着的连接、WAL、
SHM 都会让新库「看起来恢复了但没生效」。恢复必须由运维按下面的演练流程执行 CLI：

恢复演练流程（停容器 → 覆盖 → 启动）
------------------------------------
1. **确认备份可用**（任意环境，只读，可远程先做）::

       python -m scripts.backup_db --help          # 旧 CLI 仍可创建备份
       # 新服务：POST /api/backup/verify 或 python - <<'PY'
       # from app.services.backup import verify_backup; ...
       # PY

   期望 ``ok=True``、``sha256_ok=True``、``integrity="ok"``。

2. **停掉所有写数据库的进程**（关键！）::

       docker compose stop av-archive          # 或 systemctl stop av-archive
       # 确认没有 gunicorn/worker/python 还在跑，例如：
       docker compose ps

3. **挪走现场**（别直接覆盖，保留回滚点；``-wal/-shm`` 必须一起挪走，否则旧 WAL
   会在启动时回放、把恢复的库改回旧数据）::

       cd /mnt/user/appdata/av-archive/data
       mkdir -p restore-backup-$(date +%Y%m%d-%H%M%S)
       mv avlib.db avlib.db-wal avlib.db-shm restore-backup-*/ 2>/dev/null

4. **覆盖成备份文件**（保持运行用户权限；把路径换成实际备份）::

       cp /mnt/user/appdata/av-archive/data/backups/avlib-YYYYmmdd-HHMMSS.db \\
          /mnt/user/appdata/av-archive/data/avlib.db
       # 容器内通常以非 root 跑，必要时 chown -R 1000:1000 data/

5. **启动并验证**::

       docker compose up -d
       docker compose exec av-archive python -m scripts.selfcheck
       # 打开片库 / 刮削页，确认标题数、封面、盘点记录都在。

6. **回滚**：停容器 → 把第 3 步挪走的三件套移回原位 → 启动。

建议每季度做一次演练：把 ``data/`` 拷到临时目录，在副本上走一遍 2~5 步，确认
流程和备份文件本身都可靠（磁带是唯一副本，元数据库重建很费时间）。
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from app.config import Config

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_KEEP",
    "BACKUP_NAME_RE",
    "create_backup",
    "list_backups",
    "verify_backup",
    "delete_backup",
    "backup_dir",
    "restore_notes",
]

#: 默认保留份数（Config.BACKUP_KEEP 可覆盖；没有该配置时用 14）
DEFAULT_KEEP = 14
#: 备份文件名：avlib-YYYYmmdd-HHMMSS.db，同秒冲突时加 -02/-03 后缀
BACKUP_NAME_RE = re.compile(r"^avlib-\d{8}-\d{6}(?:-\d{2,3})?\.db$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HASH_CHUNK = 1024 * 1024


# ---------------------------------------------------------------------------
# 路径 / 配置
# ---------------------------------------------------------------------------
def backup_dir(app) -> Path:
    """备份目录：优先 app.config['BACKUP_DIR']，否则 ``<DATA_DIR>/backups``。"""
    configured = app.config.get("BACKUP_DIR")
    if configured:
        return Path(configured).expanduser()
    data_dir = app.config.get("DATA_DIR") or Config.DATA_DIR
    return Path(data_dir).expanduser() / "backups"


def _resolve_dir(app, dest_dir=None) -> Path:
    directory = Path(dest_dir).expanduser() if dest_dir else backup_dir(app)
    if directory.exists() and not directory.is_dir():
        raise ValueError(f"备份目录不是目录: {directory}")
    directory.mkdir(parents=True, exist_ok=True)
    return directory.resolve()


def _resolve_keep(app, keep) -> int:
    if keep is not None:
        return max(1, int(keep))
    configured = app.config.get("BACKUP_KEEP")
    if configured is None:
        configured = getattr(Config, "BACKUP_KEEP", DEFAULT_KEEP)
    return max(1, int(configured or DEFAULT_KEEP))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sidecar_path(db_path: Path) -> Path:
    return db_path.parent / (db_path.name + ".sha256")


def _safe_backup_path(app, name: str, dest_dir=None) -> tuple[Path, Path]:
    """把用户给的备份名解析成目录内的路径，拒绝任何目录穿越。

    只接受 ``avlib-YYYYmmdd-HHMMSS.db`` 形态的**纯文件名**；解析后还必须确认
    父目录就是备份目录（``resolve()`` 会展开符号链接，所以指向目录外的软链也拦）。
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("备份名不能为空")
    candidate = name.strip()
    if candidate != Path(candidate).name:
        raise ValueError(f"只允许备份文件名，不允许路径: {name!r}")
    if ".." in candidate or "/" in candidate or "\\" in candidate:
        raise ValueError(f"非法备份名: {name!r}")
    if not BACKUP_NAME_RE.match(candidate):
        raise ValueError(f"不是合法的备份文件名: {name!r}")
    directory = _resolve_dir(app, dest_dir)
    path = (directory / candidate).resolve()
    if path.parent != directory:
        raise ValueError(f"备份路径越界: {name!r}")
    return path, directory


# ---------------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------------
def _backup_name_taken(directory: Path, name: str) -> bool:
    return (directory / name).exists() or _sidecar_path(directory / name).exists()


def _make_backup_name(directory: Path) -> str:
    base = f"avlib-{time.strftime('%Y%m%d-%H%M%S')}"
    candidate = f"{base}.db"
    if not _backup_name_taken(directory, candidate):
        return candidate
    for index in range(2, 100):
        candidate = f"{base}-{index:02d}.db"
        if not _backup_name_taken(directory, candidate):
            return candidate
    raise RuntimeError("同一秒内备份文件过多，请重试")


def create_backup(app, *, dest_dir=None, keep=None) -> dict:
    """在线热备主库（WAL 安全）+ sha256 边车 + 轮换旧备份。

    * 源库：``app.config['DB_PATH']``；
    * 目标目录：``dest_dir`` / ``app.config['BACKUP_DIR']`` / ``<DATA_DIR>/backups``；
    * ``keep``：保留份数（默认 Config.BACKUP_KEEP / 14），超出的最旧备份连同
      ``.sha256`` 边车一起删除。

    返回 ``{name, path, bytes, sha256, sidecar, created_at, keep, removed, elapsed_s}``。
    源库不存在抛 ``FileNotFoundError``；目录不可写等 IO 错误照常抛出。
    """
    started = time.perf_counter()
    src = Path(app.config["DB_PATH"]).expanduser()
    if not src.is_file():
        raise FileNotFoundError(f"主库不存在: {src}")

    directory = _resolve_dir(app, dest_dir)
    keep_value = _resolve_keep(app, keep)
    name = _make_backup_name(directory)
    dst = directory / name
    sidecar = _sidecar_path(dst)

    # sqlite backup API：源库有 -wal 时也能拿到一致快照（不要用 shutil.copy）
    try:
        src_conn = sqlite3.connect(str(src))
    except sqlite3.Error as exc:  # pragma: no cover - 权限等极端情况
        raise RuntimeError(f"打开主库失败: {exc}") from exc
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    except Exception:
        # 失败的半成品不要留在目录里
        dst.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        raise
    finally:
        src_conn.close()

    digest = _sha256_file(dst)
    sidecar.write_text(f"{digest}  {name}\n", encoding="utf-8")
    created_at = datetime.fromtimestamp(dst.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")

    removed = _rotate(directory, keep_value)
    result = {
        "name": name,
        "path": str(dst),
        "bytes": dst.stat().st_size,
        "sha256": digest,
        "sidecar": str(sidecar),
        "created_at": created_at,
        "keep": keep_value,
        "removed": removed,
        "elapsed_s": round(time.perf_counter() - started, 3),
    }
    logger.info(
        "备份完成 %s (%.1f MB, %.3fs, keep=%s, 清理 %s 个)",
        name, result["bytes"] / 1024 / 1024, result["elapsed_s"], keep_value,
        len(removed),
    )
    return result


def _list_backup_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return [
        path for path in directory.iterdir()
        if path.is_file() and BACKUP_NAME_RE.match(path.name)
    ]


def _rotate(directory: Path, keep: int) -> list[str]:
    """按修改时间从新到旧保留 ``keep`` 份，返回被删掉的备份名。"""
    files = _list_backup_files(directory)
    files.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    removed: list[str] = []
    for path in files[keep:]:
        try:
            # 只删除旧备份文件，保留 sha256 边车
            path.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - 权限等
            logger.warning("清理旧备份失败 %s: %s", path, exc)
            continue
        removed.append(path.name)
    return removed


# ---------------------------------------------------------------------------
# 列表 / 校验
# ---------------------------------------------------------------------------
def _read_sidecar(db_path: Path) -> tuple[str | None, str | None]:
    """读 ``.sha256`` 边车：返回 (digest, error)。

    兼容 ``sha256sum`` 格式（``<hex>  <filename>``）和只写了一行 hex 的文件。
    """
    sidecar = _sidecar_path(db_path)
    if not sidecar.is_file():
        return None, "缺少 sha256 边车文件"
    try:
        text = sidecar.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        return None, f"读取 sha256 边车失败: {exc}"
    token = text.split()[0] if text else ""
    if not _SHA256_RE.match(token.lower()):
        return None, "sha256 边车格式非法"
    return token.lower(), None


def list_backups(app, dest_dir=None) -> list[dict]:
    """列出备份（最新在前）：name/size/mtime/sha256/完整路径。

    ``sha256`` 只在边车存在且格式合法时有值，否则为 ``None``。
    """
    directory = _resolve_dir(app, dest_dir)
    entries: list[dict] = []
    files = _list_backup_files(directory)
    files.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    for path in files:
        stat = path.stat()
        digest, sidecar_error = _read_sidecar(path)
        entries.append({
            "name": path.name,
            "path": str(path),
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "mtime_ts": stat.st_mtime,
            "sha256": digest,
            "has_sidecar": sidecar_error is None,
        })
    return entries


def _connect_ro(path: Path) -> sqlite3.Connection:
    """只读打开 SQLite（用 as_uri 处理路径里的空格/中文，避免 URI 拼接坏掉）。"""
    uri = path.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _integrity_check(path: Path) -> tuple[str, str | None, int]:
    """返回 (状态, 详情, 表数量)；状态 = ok | invalid | error。"""
    try:
        conn = _connect_ro(path)
    except sqlite3.Error as exc:
        return "error", f"打开失败: {exc}", 0
    try:
        tables = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        row = conn.execute("PRAGMA integrity_check").fetchone()
        result = str(row[0]) if row else ""
        if result.lower() != "ok":
            return "invalid", f"integrity_check: {result}", int(tables)
        return "ok", None, int(tables)
    except sqlite3.DatabaseError as exc:
        return "invalid", f"不是合法 SQLite 数据库: {exc}", 0
    except sqlite3.Error as exc:  # pragma: no cover - 极端 IO
        return "error", f"校验失败: {exc}", 0
    finally:
        conn.close()


def verify_backup(app, name: str, dest_dir=None) -> dict:
    """校验备份：sha256 对比边车 + ``PRAGMA integrity_check``。

    返回 ``{name, path, size, sha256, expected_sha256, sha256_ok, integrity,
    integrity_detail, tables, ok, errors}``；文件不存在/损坏都返回 ``ok=False``
    而不是抛异常（方便页面直接显示）。**非法文件名（``../x``）抛 ValueError**。
    """
    path, directory = _safe_backup_path(app, name, dest_dir)
    result: dict = {
        "name": name,
        "path": str(path),
        "size": 0,
        "sha256": None,
        "expected_sha256": None,
        "sha256_ok": None,
        "integrity": "missing",
        "integrity_detail": None,
        "tables": 0,
        "ok": False,
        "errors": [],
    }
    if not path.is_file():
        result["errors"].append("备份文件不存在")
        return result

    stat = path.stat()
    result["size"] = stat.st_size
    if stat.st_size == 0:
        result["integrity"] = "invalid"
        result["integrity_detail"] = "空文件"
        result["errors"].append("备份文件为空（0 字节）")
        return result

    digest = _sha256_file(path)
    result["sha256"] = digest
    expected, sidecar_error = _read_sidecar(path)
    result["expected_sha256"] = expected
    if sidecar_error:
        result["errors"].append(sidecar_error)
        result["sha256_ok"] = False
    else:
        result["sha256_ok"] = digest == expected
        if not result["sha256_ok"]:
            result["errors"].append("sha256 与边车不一致（备份被改动或边车损坏）")

    integrity, detail, tables = _integrity_check(path)
    result["integrity"] = integrity
    result["integrity_detail"] = detail
    result["tables"] = tables
    if integrity != "ok":
        result["errors"].append(detail or f"integrity_check 失败: {integrity}")
    elif tables == 0:
        result["integrity"] = "invalid"
        result["errors"].append("SQLite 里没有任何表（不是 avlib 主库）")

    result["ok"] = (
        result["sha256_ok"] is True
        and result["integrity"] == "ok"
        and result["size"] > 0
        and not result["errors"]
    )
    return result


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------
def delete_backup(app, name: str, dest_dir=None) -> dict:
    """删除备份文件；保留 ``.sha256`` 边车用于校验/审计。

    只接受备份目录内的合法备份文件名（``../x``、绝对路径、软链越界都抛
    ``ValueError``）。文件不存在返回 ``deleted=False`` + 错误信息，不抛。
    """
    path, _directory = _safe_backup_path(app, name, dest_dir)
    result = {
        "name": name,
        "path": str(path),
        "deleted": False,
        "bytes": 0,
        "sidecar_deleted": False,
        "sidecar_kept": True,
        "errors": [],
    }
    if not path.is_file():
        result["errors"].append("备份文件不存在")
        return result

    result["bytes"] = path.stat().st_size
    sidecar = _sidecar_path(path)
    try:
        path.unlink()
        result["deleted"] = True
    except OSError as exc:
        result["errors"].append(f"删除备份失败: {exc}")
        return result
    # 保留 sidecar（sha256 校验文件），不随备份文件一起删除。
    result["sidecar_kept"] = sidecar.is_file()
    logger.info("删除备份 %s（%s 字节，保留 sha256 边车）", name, result["bytes"])
    return result


# ---------------------------------------------------------------------------
# 恢复说明
# ---------------------------------------------------------------------------
def restore_notes() -> list[str]:
    """恢复演练步骤（服务不会自动恢复；返回给 UI/文档直接展示）。"""
    return [
        "1. 先验证备份：verify_backup() / 页面「校验」，确认 ok=True、sha256 一致、integrity=ok。",
        "2. 停掉所有会写库的进程：docker compose stop av-archive（确认 worker 也停了）。",
        "3. 挪走现场：把 avlib.db 和 avlib.db-wal / avlib.db-shm 一起移到 restore-backup-<时间>/ 目录（别直接覆盖，留回滚点）。",
        "4. 覆盖：cp data/backups/avlib-YYYYmmdd-HHMMSS.db data/avlib.db，必要时 chown 运行用户。",
        "5. 启动：docker compose up -d，跑 python -m scripts.selfcheck，打开片库/刮削页确认数据。",
        "6. 回滚：停容器 → 把第 3 步移走的三件套移回 → 启动。",
        "7. 在线恢复被刻意禁用：SQLite 热替换 + 旧 WAL/-shm 回放会导致 v1 那种「恢复了但没生效」，必须停进程再覆盖。",
    ]
