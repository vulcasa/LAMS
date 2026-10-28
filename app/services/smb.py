"""运行时 SMB/CIFS 挂载（不重启容器即可新增媒体库）。

在容器内执行 mount.cifs，把 ``//host/share`` 挂到本地目录，然后登记成
MediaSource（kind=nas，origin=smb）并自动扫描。需要容器有 CAP_SYS_ADMIN
（通常 privileged 或 --cap-add SYS_ADMIN）以及 cifs-utils。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from app.config import Config
from app.db import db
from app.models import MediaSource, utcnow

__all__ = ["add_smb_source", "mount_source", "unmount_source", "smb_status", "smb_available"]

BASE_DIR = Path(os.environ.get("AVLIB_SMB_BASE", "/mnt/smb"))
DEFAULT_OPTIONS = "vers=3.0,iocharset=utf8,uid=0,gid=0,file_mode=0664,dir_mode=0775"
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,253}$")


def smb_available() -> bool:
    return bool(
        shutil.which("mount.cifs")
        or Path("/sbin/mount.cifs").exists()
        or Path("/usr/sbin/mount.cifs").exists()
    )


def _mount_bin() -> str:
    found = shutil.which("mount.cifs")
    if found:
        return found
    for candidate in ("/sbin/mount.cifs", "/usr/sbin/mount.cifs"):
        if Path(candidate).exists():
            return candidate
    return "mount.cifs"


def _safe_fragment(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip()).strip("._") or "share"


def _normalize_share(raw: str) -> str:
    share = (raw or "").strip().strip("/")
    if share.lower().startswith("//"):
        share = share[2:]
    if not share or "/" in share.split("/", 1)[0] and share.count("/") > 8:
        return ""
    if ".." in share.split("/") or not share:
        return ""
    return share


def default_mount_path(host: str, share: str) -> Path:
    return BASE_DIR / f"{_safe_fragment(host)}_{_safe_fragment(share.split('/')[0])}"


def _cred_path(source_id: int, app=None) -> Path:
    base = app.config.get("DATA_DIR", Config.DATA_DIR) if app is not None else Config.DATA_DIR
    directory = Path(base) / "smb"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"source_{int(source_id)}.cred"


def _write_credential(path: Path, username: str, password: str) -> Path:
    path.write_text(f"username={username}\npassword={password}\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def migrate_plaintext_credentials(app) -> int:
    """把旧版存在 SQLite 里的明文 SMB 密码迁移到 data/smb/*.cred 并清空 DB。"""
    from app.models import MediaSource

    migrated = 0
    for row in MediaSource.query.filter(MediaSource.origin == "smb").all():
        if not row.smb_password:
            continue
        _write_credential(_cred_path(row.id, app), row.smb_username or "", row.smb_password)
        row.smb_password = None
        migrated += 1
    if migrated:
        db.session.commit()
    return migrated


def _run_mount(host: str, share: str, mount_path: Path, username: str,
               password: str, options: str, *,
               cred_path: Path | None = None) -> tuple[bool, str]:
    if not smb_available():
        return False, "容器内缺少 mount.cifs，请安装 cifs-utils"
    if not username:
        return False, "SMB 用户名不能为空"
    temporary = cred_path is None
    if temporary:
        directory = Path(Config.DATA_DIR) / "smb"
        directory.mkdir(parents=True, exist_ok=True)
        cred_path = _write_credential(
            directory / f"tmp_{uuid.uuid4().hex[:10]}.cred", username, password
        )
    # 注意：mount.cifs 不接受 -t cifs（那是 mount 的语法）；
    # 传了它 mount.cifs 会打印帮助并返回 0，看起来“成功”但实际没挂载。
    command = [
        _mount_bin(), f"//{host}/{share}", str(mount_path),
        "-o", f"credentials={cred_path},{options or DEFAULT_OPTIONS}",
    ]
    try:
        mount_path.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(command, capture_output=True, text=True, timeout=45)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if temporary:
            try:
                cred_path.unlink()
            except OSError:
                pass
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "mount.cifs 失败").strip()[:800]
        if "unknown filesystem type" in detail.lower() or "no such device" in detail.lower():
            detail += "；宿主机可能未加载 cifs 模块，请在宿主机执行 modprobe cifs"
        return False, detail
    if not _is_mounted(mount_path):
        detail = (result.stderr or result.stdout or "").strip()[:500]
        return False, (
            "mount.cifs 返回成功，但未检测到真实 cifs 挂载"
            + (f"：{detail}" if detail else "")
        )
    return True, "挂载成功"


def _log_operation(action: str, *, source_id=None, host="", share="", mount_path="",
                  status="success", message="", started=None):
    """记录 SMB 操作日志（失败也记，便于在日志页排查）。"""
    try:
        from app.models import SmbOperation

        now = utcnow()
        started_at = started or now
        duration = int(max(0, (now - started_at).total_seconds() * 1000))
        db.session.add(
            SmbOperation(
                source_id=source_id, action=action, host=host, share=share,
                mount_path=mount_path, status=status, message=(message or "")[:4000],
                started_at=started_at, finished_at=now, duration_ms=duration,
            )
        )
        db.session.commit()
    except Exception:  # noqa: BLE001 - 日志失败不影响主流程
        db.session.rollback()


def _unescape_mountinfo(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def parse_mountinfo(text: str, target: str) -> bool:
    """从 /proc/self/mountinfo 判断 target 是否为真实 cifs/smb 挂载。"""
    wanted = os.path.realpath(str(target))
    for line in (text or "").splitlines():
        try:
            left, right = line.split(" - ", 1)
            fields = left.split()
            fstype = right.split()[0] if right.split() else ""
            mount_point = _unescape_mountinfo(fields[4])
        except (IndexError, ValueError):
            continue
        if os.path.realpath(mount_point) == wanted and fstype in (
            "cifs", "smb3", "smbfs",
        ):
            return True
    return False


def _is_mounted(path: Path) -> bool:
    """只认内核 mountinfo 里的 cifs 挂载，避免 os.path.ismount 把 bind/普通目录误判。"""
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as handle:
            return parse_mountinfo(handle.read(), str(path))
    except OSError:
        return False


def add_smb_source(app, *, name: str, host: str, share: str, mount_path: str,
                   username: str, password: str, options: str = "",
                   auto_scan: bool = True) -> dict:
    """挂载 SMB 并登记为媒体库；返回 {ok, error, source, mount_path, mounted}。"""
    from app.services import jobs, media_sources

    host = (host or "").strip()
    share = _normalize_share(share)
    if not _HOST_RE.match(host or ""):
        return {"ok": False, "error": "IP/主机名格式不正确"}
    if not share:
        return {"ok": False, "error": "共享名不能为空"}
    target_raw = (mount_path or "").strip()
    target = Path(target_raw).expanduser() if target_raw else default_mount_path(host, share)
    target = target.absolute()
    if target == Path("/") or str(target) in ("/dev", "/proc", "/sys", "/boot", "/etc"):
        return {"ok": False, "error": "挂载点不能是系统关键目录"}
    if MediaSource.query.filter_by(path=str(target)).first():
        return {"ok": False, "error": f"该挂载点已存在媒体库记录：{target}"}
    if target.exists() and any(target.iterdir()) and not _is_mounted(target):
        return {"ok": False, "error": f"挂载点非空且未挂载：{target}"}
    if not smb_available():
        _log_operation("add", host=host, share=share, mount_path=str(target),
                       status="failed", message="容器缺少 cifs-utils")
        return {"ok": False,
                "error": "容器缺少 cifs-utils（mount.cifs）；请安装后重试，或重建镜像"}
    started = utcnow()
    ok, message = _run_mount(host, share, target, username, password, options)
    if not ok:
        _log_operation("add", host=host, share=share, mount_path=str(target),
                       status="failed", message=message, started=started)
        return {"ok": False, "error": message, "mount_path": str(target)}

    row = MediaSource(
        name=(name or f"SMB {host}/{share}").strip()[:128],
        kind="nas",
        path=str(target),
        enabled=True,
        auto_scan=bool(auto_scan),
        origin="smb",
        smb_host=host,
        smb_share=share,
        smb_username=username,
        smb_password=None,
        smb_options=options or DEFAULT_OPTIONS,
        smb_mounted_at=utcnow(),
        last_scan_status="mounted",
    )
    db.session.add(row)
    db.session.commit()
    _write_credential(_cred_path(row.id, app), username, password)
    _log_operation("add", source_id=row.id, host=host, share=share,
                   mount_path=str(target), status="success", message="挂载成功并登记媒体库",
                   started=started)

    job_id = None
    if auto_scan:
        job_id = jobs.enqueue("scan_nas", {"root": str(target), "name": row.name,
                                           "source_id": row.id})
        row.last_job_id = job_id
        row.last_scan_status = "queued"
        db.session.commit()
    return {"ok": True, "source": media_sources._source_dict(app, row),
            "mount_path": str(target), "mounted": True, "job_id": job_id}


def mount_source(app, source_id: int) -> dict:
    row = db.session.get(MediaSource, source_id)
    if row is None:
        return {"ok": False, "error": "媒体库不存在"}
    if not row.smb_host or not row.smb_share:
        return {"ok": False, "error": "该媒体库不是 SMB 挂载"}
    target = Path(row.path)
    if _is_mounted(target):
        return {"ok": True, "mounted": True, "message": "已经挂载"}
    try:
        if os.path.ismount(str(target)):
            return {
                "ok": False,
                "error": f"挂载点已被其他文件系统占用，不能挂载 SMB：{target}",
            }
    except OSError:
        pass
    credential = _cred_path(row.id, app)
    if not credential.is_file():
        if not row.smb_password:
            return {"ok": False, "error": "缺少 SMB 凭据文件，请重新挂载并输入密码"}
        _write_credential(credential, row.smb_username or "", row.smb_password)
        row.smb_password = None
        db.session.commit()
    started = utcnow()
    ok, message = _run_mount(row.smb_host, row.smb_share, target,
                             row.smb_username or "", "", row.smb_options or DEFAULT_OPTIONS,
                             cred_path=credential)
    if ok:
        row.smb_mounted_at = utcnow()
        db.session.commit()
    _log_operation("mount", source_id=row.id, host=row.smb_host, share=row.smb_share,
                   mount_path=str(target), status="success" if ok else "failed",
                   message=message, started=started)
    return {"ok": ok, "mounted": ok, "message": message}


def unmount_source(app, source_id: int) -> dict:
    row = db.session.get(MediaSource, source_id)
    if row is None:
        return {"ok": False, "error": "媒体库不存在"}
    target = Path(row.path)
    if not _is_mounted(target):
        return {"ok": True, "mounted": False, "message": "未挂载"}
    started = utcnow()
    try:
        result = subprocess.run(["umount", str(target)], capture_output=True, text=True,
                                timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if result.returncode != 0:
        message = (result.stderr or "umount 失败").strip()[:500]
        _log_operation("unmount", source_id=row.id, host=row.smb_host,
                       share=row.smb_share, mount_path=str(target),
                       status="failed", message=message, started=started)
        return {"ok": False, "error": message}
    try:
        row.smb_mounted_at = None
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
    _log_operation("unmount", source_id=row.id, host=row.smb_host,
                   share=row.smb_share, mount_path=str(target),
                   status="success", message="已卸载", started=started)
    return {"ok": True, "mounted": False, "message": "已卸载"}


def smb_status(row: MediaSource) -> dict:
    return {
        "host": row.smb_host,
        "share": row.smb_share,
        "username": row.smb_username,
        "mounted": _is_mounted(Path(row.path)) if row.path else False,
        "has_password": bool(row.smb_password),
    }
