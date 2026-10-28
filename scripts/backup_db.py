"""SQLite 备份（在线安全 + sha256 + 轮换）。

    python -m scripts.backup_db --dest /data/backups --keep 14

用 sqlite3 backup API（WAL 下也一致），不要直接 cp。
"""
import argparse
import hashlib
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def backup(src: Path, dest_dir: Path, keep: int) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = dest_dir / f"avlib-{stamp}.db"
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    digest = sha256(dst)
    (dst.with_suffix(".db.sha256")).write_text(f"{digest}  {dst.name}\n")
    print(f"备份完成: {dst}  {dst.stat().st_size / 1024 / 1024:.1f} MB  sha256={digest[:16]}…")

    olds = sorted(dest_dir.glob("avlib-*.db"), reverse=True)
    for old in olds[keep:]:
        old.unlink(missing_ok=True)
        old.with_suffix(".db.sha256").unlink(missing_ok=True)
        print(f"清理旧备份: {old.name}")
    return dst


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(Config.DB_PATH))
    ap.add_argument("--dest", default=str(Config.DATA_DIR / "backups"))
    ap.add_argument("--keep", type=int, default=14)
    args = ap.parse_args(argv)
    src = Path(args.src)
    if not src.is_file():
        print(f"数据库不存在: {src}", file=sys.stderr)
        return 1
    backup(src, Path(args.dest), args.keep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
