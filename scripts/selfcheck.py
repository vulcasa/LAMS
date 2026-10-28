"""部署自检：在容器里跑一遍，确认这套服务真的可用。

    python -m scripts.selfcheck                # 快速检查（默认）
    python -m scripts.selfcheck --deep --dir /mnt/av --limit 200
    docker exec -it av-archive python -m scripts.selfcheck

输出 PASS / WARN / FAIL 表格；有 FAIL 时退出码 1。
"""
import argparse
import json
import shutil
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402

RESULTS = []


def _record(name: str, status: str, detail: str = ""):
    RESULTS.append({"name": name, "status": status, "detail": detail})
    icon = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌"}.get(status, "·")
    print(f"{icon} {name:<34} {detail}")


def check_imports():
    try:
        import flask  # noqa: F401
        import sqlalchemy  # noqa: F401
        import requests  # noqa: F401
        import bs4  # noqa: F401
        from PIL import Image  # noqa: F401

        _record("Python 依赖", "PASS", f"python {sys.version.split()[0]}")
    except Exception as exc:  # noqa: BLE001
        _record("Python 依赖", "FAIL", f"{type(exc).__name__}: {exc}")


def check_dirs(app):
    try:
        for key in ("DATA_DIR", "MEDIA_DIR", "COVER_DIR", "STILL_DIR", "AVATAR_DIR",
                    "SUBTITLE_DIR", "THUMB_DIR", "DUMP_DIR"):
            p = Path(app.config[key])
            p.mkdir(parents=True, exist_ok=True)
        probe = Path(app.config["DATA_DIR"]) / ".selfcheck"
        probe.write_text("ok")
        probe.unlink()
        _record("数据目录可写", "PASS", str(app.config["DATA_DIR"]))
    except Exception as exc:  # noqa: BLE001
        _record("数据目录可写", "FAIL", f"{type(exc).__name__}: {exc}")


def check_db(app):
    from app.db import db
    from app.models import Job, LibraryFile, Title

    try:
        with app.app_context():
            db.create_all()
            cols = {c.name for c in LibraryFile.__table__.columns}
            missing = {"flags", "is_sidecar", "code_guess", "confidence"} - cols
            if missing:
                _record("数据库结构", "FAIL", f"library_file 缺列: {missing}（旧库需要删除重建或迁移）")
                return
            db.session.query(Job).count()
            db.session.query(Title).count()
        _record("数据库结构", "PASS", f"{app.config['SQLALCHEMY_DATABASE_URI']}")
    except Exception as exc:  # noqa: BLE001
        _record("数据库结构", "FAIL", f"{type(exc).__name__}: {exc}")


def check_staging(app):
    db_path = Path(app.config.get("R18_DUMP_DB") or Config.R18_DUMP_DB)
    if not db_path.is_file():
        _record("r18 staging 库", "WARN", f"未找到 {db_path}（先跑 scripts.import_dump；元数据功能不可用）")
        return
    try:
        from app.services import r18

        conn = r18.open_dump_db(db_path)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(derived_video)")}
            if "dvd_id_key" not in cols:
                _record("r18 staging 库", "FAIL", "旧版 staging（缺 dvd_id_key），请重新 import_dump")
                return
            n = conn.execute("SELECT COUNT(*) FROM derived_video").fetchone()[0]
            found = r18.lookup_by_dvd_ids(conn, ["SSIS-001"])
            if "SSIS-001" not in found:
                _record("r18 staging 库", "WARN", f"{n} 条，但 SSIS-001 查不到（库可能不完整）")
            else:
                _record("r18 staging 库", "PASS", f"{n} 部，{db_path.stat().st_size/1024/1024/1024:.2f} GB")
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        _record("r18 staging 库", "FAIL", f"{type(exc).__name__}: {exc}")


def check_ltfs():
    import shutil
    import subprocess

    binary = shutil.which("ltfs")
    if not binary:
        _record("LTFS 二进制", "WARN", "未安装：磁带控制不可用（片库/元数据不受影响）")
        return
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=15
        )
        line = (proc.stdout or proc.stderr or "").strip().splitlines()
        _record("LTFS 二进制", "PASS", line[0][:70] if line else binary)
    except Exception as exc:  # noqa: BLE001
        _record("LTFS 二进制", "WARN", f"{type(exc).__name__}: {exc}")


def check_parser():
    try:
        from app.services.code_parser import extract_code

        cases = {
            "SSIS-001.mp4": "SSIS-001",
            "hhd800.com@ssis00001hhb.mp4": "SSIS-001",
            "FC2-PPV-1234567.mp4": "FC2-PPV-1234567",
            "Tokyo-Hot n1234.mp4": "TOKYO-HOT-N1234",
            "100TV-031.mp4": "100TV-031",
            "C-2391.mp4": "C-2391",
            "movie.mkv": None,
        }
        bad = []
        for name, expected in cases.items():
            got = extract_code(name).code
            if got != expected:
                bad.append(f"{name}→{got}（期望 {expected}）")
        if bad:
            _record("番号解析器", "FAIL", "; ".join(bad))
        else:
            _record("番号解析器", "PASS", f"{len(cases)} 个样例")
    except Exception as exc:  # noqa: BLE001
        _record("番号解析器", "FAIL", f"{type(exc).__name__}: {exc}")


def check_config(app):
    cfg = app.config
    try:
        from app.services.settings import get_proxy

        with app.app_context():
            info = get_proxy(app)
        if info.get("url"):
            proxy = f"已配置（{info['source']}: {info['url']}）"
        else:
            proxy = "未配置（JavDB/图片下载会失败；可在 设置 页配置）"
    except Exception:  # noqa: BLE001
        proxy = "已配置" if (cfg.get("HTTP_PROXY") or "") else "未配置"
    adapter = cfg.get("TAPE_ADAPTER")
    _record("运行配置", "PASS", f"tape_adapter={adapter} proxy={proxy}")
    if adapter == "http":
        _record("磁带服务地址", "PASS", str(cfg.get("LTFS_API_BASE")))
    else:
        _record("磁带设备", "PASS", str(cfg.get("LTFS_DEVICE")))


def check_tape(app):
    try:
        from app.services.tape_adapter import get_adapter

        with app.app_context():
            status = get_adapter(app.config, app=app).status()
        if status.device_exists:
            _record("磁带设备", "PASS", f"已连接，当前带={status.barcode or '无'}")
        else:
            _record("磁带设备", "WARN", "未检测到设备（没有插带或 adapter 配置不对，不影响网页/元数据功能）")
    except Exception as exc:  # noqa: BLE001
        _record("磁带设备", "WARN", f"{type(exc).__name__}: {exc}")


def deep_scan(app, directory: str, limit: int):
    try:
        from app.services.scanner import scan_nas

        with app.app_context():
            stats = scan_nas(app, directory, volume_name="selfcheck")
        _record("扫描目录", "PASS", f"{stats.files_seen} 文件 / {stats.with_code} 有番号 / {stats.elapsed_s:.2f}s")
    except Exception as exc:  # noqa: BLE001
        _record("扫描目录", "FAIL", f"{type(exc).__name__}: {exc}")
        return
    try:
        from app.services.materialize import materialize

        with app.app_context():
            ms = materialize(app, dump_db_path=app.config.get("R18_DUMP_DB"), min_confidence=90, limit=limit)
        _record("物化元数据", "PASS", f"命中 {ms.matched} / 未命中 {ms.no_dump_match}（只处理前 {limit} 个）")
    except Exception as exc:  # noqa: BLE001
        _record("物化元数据", "FAIL", f"{type(exc).__name__}: {exc}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--deep", action="store_true", help="额外做一次真实目录扫描 + 物化")
    ap.add_argument("--dir", default="", help="--deep 时扫描的目录")
    ap.add_argument("--limit", type=int, default=200, help="--deep 物化上限")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    print("=" * 66)
    print("av-archive 部署自检")
    print("=" * 66)

    check_imports()
    from app import create_app

    try:
        app = create_app({"WORKER_ENABLED": False})
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        print("❌ 应用启动失败")
        return 1

    check_dirs(app)
    check_db(app)
    check_config(app)
    check_ltfs()
    check_parser()
    check_staging(app)
    check_tape(app)
    if args.deep:
        if not args.dir:
            _record("扫描目录", "WARN", "--deep 需要 --dir")
        else:
            deep_scan(app, args.dir, args.limit)
    else:
        _record("深度扫描", "WARN", "未启用（加 --deep --dir /mnt/av 可测真实目录）")

    failed = [r for r in RESULTS if r["status"] == "FAIL"]
    warned = [r for r in RESULTS if r["status"] == "WARN"]
    print("-" * 66)
    print(f"结果：PASS {len([r for r in RESULTS if r['status']=='PASS'])} / "
          f"WARN {len(warned)} / FAIL {len(failed)}")
    if failed:
        print("有 FAIL：先按上面的提示修，再重新自检。")
    elif warned:
        print("自检通过（有 WARN 项不影响核心功能，按需处理）。")
    else:
        print("自检全部通过。")
    if args.json:
        print(json.dumps(RESULTS, ensure_ascii=False, indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
