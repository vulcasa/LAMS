"""完整备份归档：数据库 + manifest + 封面/剧照/视频抽帧，打包为 zip。

数据库仍用 sqlite3 backup API 生成一致快照；资产目录按原相对路径打包。
还原时资产写回原目录，数据库通过 sqlite backup API 写回运行库（会短暂中断写入）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from app.config import Config

logger = logging.getLogger(__name__)

FULL_NAME_RE = re.compile(r"^avlib-full-\d{8}-\d{6}(?:-\d{2,3})?\.zip$")
FULL_KEEP_DEFAULT = 3
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_ROOTS = ("database/", "manifests/", "media/covers/",
                  "media/stills/", "media/screenshots/", "backup.json")
_CONTENT_ROOTS = ("manifests/", "media/covers/", "media/stills/", "media/screenshots/")


def _backup_dir(app) -> Path:
    from app.services.backup import backup_dir

    path = backup_dir(app)
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _cfg_dir(app, key: str, fallback: Path) -> Path:
    value = app.config.get(key)
    return Path(value).expanduser() if value else Path(fallback).expanduser()


def asset_dirs(app) -> dict[str, Path]:
    media = _cfg_dir(app, "MEDIA_DIR", Config.MEDIA_DIR)
    return {
        "manifests": _cfg_dir(app, "MANIFEST_DIR", Config.MANIFEST_DIR),
        "covers": _cfg_dir(app, "COVER_DIR", Config.COVER_DIR),
        "stills": _cfg_dir(app, "STILL_DIR", Config.STILL_DIR),
        "screenshots": media / "screenshots",
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".sha256")


def _unique_name(directory: Path) -> str:
    base = f"avlib-full-{time.strftime('%Y%m%d-%H%M%S')}"
    for suffix in ("", *[f"-{i:02d}" for i in range(2, 100)]):
        name = f"{base}{suffix}.zip"
        if not (directory / name).exists() and not _sidecar(directory / name).exists():
            return name
    raise RuntimeError("同一秒内完整备份过多，请重试")


def _iter_files(root: Path):
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def create_full_backup(app, *, dest_dir=None, keep: int | None = None) -> dict:
    """创建完整 zip；返回文件名/大小/资产计数。"""
    started = time.perf_counter()
    db_path = Path(app.config["DB_PATH"]).expanduser()
    if not db_path.is_file():
        raise FileNotFoundError(f"主库不存在: {db_path}")
    directory = Path(dest_dir).expanduser().resolve() if dest_dir else _backup_dir(app)
    directory.mkdir(parents=True, exist_ok=True)
    name = _unique_name(directory)
    target = directory / name
    sidecar = _sidecar(target)
    dirs = asset_dirs(app)
    counts = {key: 0 for key in dirs}

    temp_dir = Path(tempfile.mkdtemp(prefix=".full-backup-", dir=directory))
    temp_db = temp_dir / "avlib.db"
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(temp_db))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.write(temp_db, "database/avlib.db")
            for key, root in dirs.items():
                if not root.is_dir():
                    continue
                prefix = {"covers": "media/covers", "stills": "media/stills",
                          "screenshots": "media/screenshots",
                          "manifests": "manifests"}[key]
                for path in _iter_files(root):
                    zf.write(path, f"{prefix}/{path.relative_to(root).as_posix()}")
                    counts[key] += 1
            meta = {
                "format": "lams-full-backup",
                "version": 1,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "db_name": "avlib.db",
                "counts": counts,
                "dirs": {key: str(value) for key, value in dirs.items()},
            }
            zf.writestr("backup.json", json.dumps(meta, ensure_ascii=False, indent=2))
    except Exception:
        target.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    digest = _sha256_file(target)
    sidecar.write_text(f"{digest}  {name}\n", encoding="utf-8")
    keep_value = max(1, int(keep if keep is not None else app.config.get("FULL_BACKUP_KEEP", FULL_KEEP_DEFAULT)))
    removed = _rotate_full(directory, keep_value)
    stat = target.stat()
    return {
        "ok": True,
        "name": name,
        "path": str(target),
        "bytes": stat.st_size,
        "sha256": digest,
        "created_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "counts": counts,
        "keep": keep_value,
        "removed": removed,
        "elapsed_s": round(time.perf_counter() - started, 3),
    }


def _full_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return [p for p in directory.iterdir() if p.is_file() and FULL_NAME_RE.match(p.name)]


def _rotate_full(directory: Path, keep: int) -> list[str]:
    files = sorted(_full_files(directory), key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
    removed = []
    for path in files[keep:]:
        try:
            # 只清理旧的备份文件；.sha256 边车保留，供校验/审计使用
            path.unlink(missing_ok=True)
            removed.append(path.name)
        except OSError as exc:
            logger.warning("清理完整备份失败 %s: %s", path, exc)
    return removed


def list_full_backups(app, dest_dir=None) -> list[dict]:
    directory = Path(dest_dir).expanduser().resolve() if dest_dir else _backup_dir(app)
    entries = []
    for path in sorted(_full_files(directory), key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True):
        stat = path.stat()
        digest = None
        sidecar = _sidecar(path)
        if sidecar.is_file():
            try:
                token = sidecar.read_text(encoding="utf-8").split()[0].lower()
                if _SHA256_RE.match(token):
                    digest = token
            except (OSError, IndexError):
                pass
        entries.append({
            "name": path.name,
            "path": str(path),
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "sha256": digest,
            "type": "full",
        })
    return entries


def _safe_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError("备份名不能为空")
    candidate = name.strip()
    if candidate != Path(candidate).name or ".." in candidate or "/" in candidate or "\\" in candidate:
        raise ValueError("非法备份名")
    if not FULL_NAME_RE.match(candidate):
        raise ValueError("不是合法的完整备份文件名")
    return candidate


def full_backup_path(app, name: str, dest_dir=None) -> Path:
    directory = Path(dest_dir).expanduser().resolve() if dest_dir else _backup_dir(app)
    path = (directory / _safe_name(name)).resolve()
    if path.parent != directory:
        raise ValueError("备份路径越界")
    return path


def delete_full_backup(app, name: str, dest_dir=None) -> dict:
    path = full_backup_path(app, name, dest_dir)
    if not path.is_file():
        return {"ok": False, "error": "备份不存在"}
    size = path.stat().st_size
    path.unlink()
    # sha256 边车不删除：它是备份的校验/审计记录，保留下来。
    return {"ok": True, "name": name, "bytes": size, "sidecar_kept": True}


def verify_full_backup(app, name: str, dest_dir=None) -> dict:
    path = full_backup_path(app, name, dest_dir)
    result = {"ok": False, "name": name, "sha256_ok": False, "zip_ok": False, "errors": []}
    if not path.is_file():
        result["errors"].append("备份不存在")
        return result
    digest = _sha256_file(path)
    expected = None
    sidecar = _sidecar(path)
    if sidecar.is_file():
        token = sidecar.read_text(encoding="utf-8").split()[0].lower()
        if _SHA256_RE.match(token):
            expected = token
    result["sha256"] = digest
    result["expected_sha256"] = expected
    result["sha256_ok"] = expected is not None and digest == expected
    if not result["sha256_ok"]:
        result["errors"].append("sha256 不匹配或缺少边车")
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            names = set(zf.namelist())
        result["zip_ok"] = bad is None
        result["has_database"] = "database/avlib.db" in names
        if bad:
            result["errors"].append(f"zip 内文件损坏: {bad}")
        if not result["has_database"]:
            result["errors"].append("zip 缺少 database/avlib.db")
    except zipfile.BadZipFile as exc:
        result["errors"].append(f"不是有效 zip: {exc}")
    result["ok"] = result["sha256_ok"] and result["zip_ok"] and result.get("has_database", False)
    return result


def _valid_arcname(name: str) -> bool:
    if not name or name.startswith("/") or "\\" in name:
        return False
    parts = PurePosixPath(name).parts
    if ".." in parts:
        return False
    return name == "backup.json" or any(name.startswith(root) for root in _ALLOWED_ROOTS)


def restore_full_backup(app, path: Path) -> dict:
    """从完整备份还原资产和数据库；调用期间不要有其他写操作。"""
    from app.db import db

    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"备份不存在: {path}")
    dirs = asset_dirs(app)
    media = _cfg_dir(app, "MEDIA_DIR", Config.MEDIA_DIR)
    # 目标目录映射
    targets = {
        "manifests": dirs["manifests"],
        "covers": dirs["covers"],
        "stills": dirs["stills"],
        "screenshots": media / "screenshots",
    }
    temp_dir = Path(tempfile.mkdtemp(prefix=".restore-", dir=path.parent))
    restored = {"assets": 0, "database": False}
    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if not _valid_arcname(info.filename):
                    raise ValueError(f"备份包含非法路径: {info.filename}")
            zf.extractall(temp_dir)
        staged_db = temp_dir / "database" / "avlib.db"
        if not staged_db.is_file():
            raise ValueError("备份缺少 database/avlib.db")
        # 先验证备份库
        src = sqlite3.connect(str(staged_db))
        try:
            if src.execute("PRAGMA integrity_check").fetchone()[0].lower() != "ok":
                raise ValueError("备份数据库 integrity_check 失败")
            tables = src.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            if tables == 0:
                raise ValueError("备份数据库没有表")
            # 资产先恢复
            for key, prefix in (("manifests", "manifests"), ("covers", "media/covers"),
                                ("stills", "media/stills"),
                                ("screenshots", "media/screenshots")):
                source_root = temp_dir / prefix
                if source_root.is_dir():
                    target_root = targets[key]
                    target_root.mkdir(parents=True, exist_ok=True)
                    for file_path in _iter_files(source_root):
                        rel = file_path.relative_to(source_root)
                        dest = target_root / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(file_path, dest)
                        restored["assets"] += 1
            # 数据库写回运行库（sqlite backup API；无需替换 inode）
            live = Path(app.config["DB_PATH"]).expanduser()
            db.session.remove()
            db.engine.dispose()
            dst = sqlite3.connect(str(live))
            try:
                src.backup(dst)
                dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                dst.close()
            db.engine.dispose()
            restored["database"] = True
        finally:
            src.close()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    logger.info("完整备份还原完成 %s: %s", path.name, restored)
    return {"ok": True, "name": path.name, **restored}
