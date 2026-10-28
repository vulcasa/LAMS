"""下载 r18.dev 每周 dump（支持断点续传）。

用法：
    python -m scripts.download_dump --out data/dump
"""
import argparse
import gzip
import os
import sys
import time
from pathlib import Path

import requests

DEFAULT_URL = "https://r18.dev/dumps/latest"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    proxies = None
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if not proxy:
        # 环境变量没有时，尝试读 UI 设置页保存的代理（需要 app context）
        try:
            from app import create_app
            from app.services.settings import proxy_url

            _app = create_app({"WORKER_ENABLED": False})
            with _app.app_context():
                proxy = proxy_url(_app)
        except Exception:  # noqa: BLE001
            proxy = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    if proxies:
        sess.proxies.update(proxies)

    with sess.get(url, stream=True, timeout=60, allow_redirects=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        # 从 Content-Disposition 或最终 URL 里取文件名
        name = dest.name
        final = r.url.split("?")[0].rsplit("/", 1)[-1]
        if final.lower().endswith(".sql.gz"):
            name = final
        dest = dest.with_name(name)
        done = dest.stat().st_size if dest.exists() else 0
        if total and done >= total:
            print(f"已存在且完整: {dest} ({done} bytes)")
            return dest
        mode = "ab" if done else "wb"
        t0 = time.time()
        with open(dest, mode) as f:
            for chunk in r.iter_content(1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 / total
                    speed = done / max(time.time() - t0, 0.001) / 1024 / 1024
                    sys.stdout.write(f"\r{done/1024/1024:7.1f} MB / {total/1024/1024:.1f} MB  {pct:5.1f}%  {speed:5.1f} MB/s")
                    sys.stdout.flush()
        print()
    return dest


def decompress(src: Path) -> Path:
    out = src.with_suffix("")  # x.sql.gz -> x.sql
    if out.exists() and out.stat().st_size > 0:
        print(f"已解压: {out}")
        return out
    print(f"解压 -> {out}")
    with gzip.open(src, "rb") as fin, open(out, "wb") as fout:
        while True:
            block = fin.read(64 * 1024 * 1024)
            if not block:
                break
            fout.write(block)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--out", default="data/dump", help="落盘目录")
    ap.add_argument("--no-decompress", action="store_true")
    args = ap.parse_args(argv)

    out = Path(args.out)
    gz = download(args.url, out / "latest.sql.gz")
    print(f"下载完成: {gz}  {gz.stat().st_size/1024/1024:.1f} MB")
    if not args.no_decompress:
        sql = decompress(gz)
        print(f"SQL: {sql}  {sql.stat().st_size/1024/1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
