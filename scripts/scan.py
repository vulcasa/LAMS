#!/usr/bin/env python3
"""扫描 CLI：NAS 目录 / 磁带挂载点 → ``library_file``，或导出卷/片库统计。

用法（项目根 = av-archive/）::

    python -m scripts.scan --nas /mnt/user/av --name NAS-AV
    python -m scripts.scan --tape /media/tape --barcode LTO6-A001
    python -m scripts.scan --tape /media/tape --barcode LTO6-A001 --expected LTO6-A001
    python -m scripts.scan --list
    python -m scripts.scan --stats

所有模式都往 stdout 输出 JSON（进度写 stderr，不污染 stdout）；错误以
``{"error": "..."}`` 输出并以退出码 2 结束。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func  # noqa: E402

from app import create_app  # noqa: E402
from app.config import Config  # noqa: E402
from app.db import db  # noqa: E402
from app.models import LibraryFile, LibraryVolume  # noqa: E402
from app.services.scanner import scan_nas, scan_tape  # noqa: E402


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _progress_printer(quiet: bool):
    if quiet:
        return None

    def _show(done: int, message: str = "") -> None:
        print(f"\r[{done}] {message}", end="", file=sys.stderr, flush=True)

    return _show


def _volume_list() -> list[dict]:
    """卷列表 + 未软删文件数/视频数/有番号数/字节数。"""
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
    videos = dict(
        db.session.query(LibraryFile.volume_id, func.count(LibraryFile.id))
        .filter(
            LibraryFile.deleted_at.is_(None),
            LibraryFile.is_sidecar.is_(False),
        )
        .group_by(LibraryFile.volume_id)
        .all()
    )
    with_code = dict(
        db.session.query(LibraryFile.volume_id, func.count(LibraryFile.id))
        .filter(
            LibraryFile.deleted_at.is_(None),
            func.coalesce(LibraryFile.code_guess, "") != "",
        )
        .group_by(LibraryFile.volume_id)
        .all()
    )
    out = []
    for volume, n_files, n_bytes in rows:
        out.append(
            {
                "id": volume.id,
                "kind": volume.kind,
                "ref": volume.ref,
                "name": volume.name,
                "enabled": bool(volume.enabled),
                "last_scan_at": _iso(volume.last_scan_at),
                "last_scan_status": volume.last_scan_status,
                "used_bytes": volume.used_bytes,
                "files": int(n_files),
                "videos": int(videos.get(volume.id, 0)),
                "with_code": int(with_code.get(volume.id, 0)),
                "bytes": int(n_bytes),
            }
        )
    return out


def _library_stats() -> dict:
    """片库文件层统计（软删行不计入）。"""
    alive = LibraryFile.deleted_at.is_(None)
    has_code = func.coalesce(LibraryFile.code_guess, "") != ""
    auto_min = int(Config.CODE_AUTO_MIN)

    def _count(*criteria) -> int:
        q = db.session.query(func.count(LibraryFile.id)).filter(alive)
        for c in criteria:
            q = q.filter(c)
        return int(q.scalar() or 0)

    files = _count()
    videos = _count(LibraryFile.is_sidecar.is_(False))
    with_code = _count(has_code)
    low_confidence = _count(
        has_code,
        LibraryFile.confidence > 0,
        LibraryFile.confidence < auto_min,
    )
    by_kind = dict(
        db.session.query(LibraryVolume.kind, func.count(LibraryVolume.id))
        .group_by(LibraryVolume.kind)
        .all()
    )
    return {
        "db": str(db.engine.url.database or Config.DB_PATH),
        "volumes": int(db.session.query(func.count(LibraryVolume.id)).scalar() or 0),
        "volumes_by_kind": {k: int(v) for k, v in by_kind.items()},
        "files": files,
        "videos": videos,
        "sidecars": files - videos,
        "with_code": with_code,
        "no_code": files - with_code,
        "low_confidence": low_confidence,
        "chinese_sub_files": _count(LibraryFile.flags.contains("chinese_sub")),
        "deleted_rows": int(
            db.session.query(func.count(LibraryFile.id))
            .filter(LibraryFile.deleted_at.isnot(None))
            .scalar()
            or 0
        ),
        "bytes": int(
            db.session.query(func.coalesce(func.sum(LibraryFile.size), 0))
            .filter(alive)
            .scalar()
            or 0
        ),
        "auto_min": auto_min,
    }


def _run(app, args) -> dict | list:
    if args.list:
        return _volume_list()
    if args.stats:
        return _library_stats()
    if args.nas:
        return asdict(
            scan_nas(
                app,
                args.nas,
                volume_name=args.name,
                progress=_progress_printer(args.quiet),
            )
        )
    return asdict(
        scan_tape(
            app,
            args.tape,
            barcode=args.barcode,
            expected_barcode=args.expected,
            progress=_progress_printer(args.quiet),
        )
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="扫描 NAS / 磁带进片库（输出 JSON；进度走 stderr）",
    )
    ap.add_argument("--nas", metavar="PATH", help="扫描 NAS 根目录")
    ap.add_argument("--name", help="NAS 卷显示名（默认用目录名）")
    ap.add_argument("--tape", metavar="MOUNT", help="扫描已挂载的磁带目录")
    ap.add_argument("--barcode", help="磁带条码（--tape 必填，UNKNOWN 会拒绝）")
    ap.add_argument("--expected", help="预期条码；与实际不符时中止（防串盘）")
    ap.add_argument("--list", action="store_true", help="列出所有卷及其文件数")
    ap.add_argument("--stats", action="store_true", help="输出片库文件层统计")
    ap.add_argument("--db", help="覆盖主库路径（默认 $AVLIB_DB_PATH 或 data/avlib.db）")
    ap.add_argument("--quiet", action="store_true", help="不打印扫描进度")
    args = ap.parse_args(argv)

    modes = [bool(args.nas), bool(args.tape), args.list, args.stats]
    if sum(modes) != 1:
        ap.error("必须且只能指定 --nas / --tape / --list / --stats 之一")
    if args.tape and not args.barcode:
        ap.error("--tape 必须同时给 --barcode")
    if args.barcode and not args.tape:
        ap.error("--barcode 只能与 --tape 一起用")
    if args.expected and not args.tape:
        ap.error("--expected 只能与 --tape 一起用")
    if args.name and not args.nas:
        ap.error("--name 只能与 --nas 一起用")

    overrides: dict = {"WORKER_ENABLED": False}
    if args.db:
        db_path = Path(args.db).expanduser().resolve()
        overrides["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"

    app = create_app(overrides)
    with app.app_context():
        try:
            payload = _run(app, args)
        except (ValueError, FileNotFoundError) as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 2
        if not args.quiet and (args.nas or args.tape):
            print(file=sys.stderr)  # 结束进度行
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
