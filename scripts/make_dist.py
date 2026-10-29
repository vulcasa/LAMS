"""打包可部署 tarball（不含 .venv / data / 缓存），拷到 Unraid 直接 build。

    python -m scripts.make_dist [--out dist]

产物：dist/av-archive-<时间戳>.tar.gz + .sha256
"""
import argparse
import hashlib
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCLUDE = [
    "app",
    "scripts",
    "tests",
    "docs",
    "research",
    "vendor",
    "wsgi.py",
    "Dockerfile",
    ".dockerignore",
    "docker-compose.yml",
    "docker-compose.image.yml",
    "requirements.txt",
    "requirements-prod.txt",
    "README.md",
    "VERSION",
    "install.sh",
]
EXCLUDE_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".venv",
    ".chrome-profile",
    "data",
    "dist",
    ".git",
}
REQUIRED = ["Dockerfile", "docker-compose.yml",
    "docker-compose.image.yml", "requirements-prod.txt", "app/__init__.py", "wsgi.py"]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist"))
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    dst = out_dir / f"av-archive-{stamp}.tar.gz"
    arc_root = ROOT.name  # av-archive

    count = 0
    with tarfile.open(dst, "w:gz") as tar:
        for name in INCLUDE:
            src = ROOT / name
            if not src.exists():
                continue
            if src.is_dir():
                for f in sorted(src.rglob("*")):
                    if not f.is_file():
                        continue
                    if any(part in EXCLUDE_DIRS for part in f.parts):
                        continue
                    tar.add(f, arcname=f"{arc_root}/{f.relative_to(ROOT)}")
                    count += 1
            else:
                tar.add(src, arcname=f"{arc_root}/{src.relative_to(ROOT)}")
                count += 1

        # 空 data 目录，保持 compose 的 ./data 挂载可创建
        data_keep = ROOT / "data" / ".gitkeep"
        if data_keep.exists():
            tar.add(data_keep, arcname=f"{arc_root}/data/.gitkeep")
            count += 1

    with tarfile.open(dst, "r:gz") as tar:
        names = set(tar.getnames())
    missing = [r for r in REQUIRED if f"{arc_root}/{r}" not in names]
    if missing:
        dst.unlink(missing_ok=True)
        raise SystemExit(f"打包失败，缺少必需文件: {missing}")

    digest = sha256(dst)
    (Path(str(dst) + ".sha256")).write_text(f"{digest}  {dst.name}\n")
    size_mb = dst.stat().st_size / 1024 / 1024
    print(f"打包完成: {dst}  {size_mb:.2f} MB  {count} 个文件")
    print(f"sha256: {digest}")
    print("\n拷到 Unraid 后：")
    print(f"  scp {dst.name} unraid:/mnt/user/appdata/av-archive/")
    print(f"  cd /mnt/user/appdata/av-archive && tar xzf {dst.name} --strip-components=1")
    print("  docker compose up -d --build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
