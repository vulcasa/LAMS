"""从磁带 manifest 重建数据库索引（灾难恢复 CLI）。

场景：主库文件丢了 / 换机器，手头只有磁带里盘点时写的
``._manifest/<barcode>.json``。用本脚本扫 manifest 重建 ``library_volume`` +
``library_file``，之后正常跑 ``materialize`` 就能按番号重新关联元数据。

用法（项目根 = av-archive/）::

    # 直接从插着的磁带挂载点读（只读 manifest，不碰文件内容）
    python -m scripts.rebuild_from_manifest --path /media/tape --dry-run

    # 多盘 = 多个 --path；也可以是备份目录（<barcode>.json 直接放里面）
    python -m scripts.rebuild_from_manifest \\
        --path /media/tape --path /backup/manifests --path /mnt/tape2

    # 真的写库；--json 给脚本/运维用
    python -m scripts.rebuild_from_manifest --path /backup/manifests --json

注意事项：

- 幂等：重复跑不会重复插行；已存在且内容一致的文件行会被跳过（不改 seen_at）。
- **不删数据**：manifest 里没列出的卷/行一律不动（重建只补不删）。
- ``--dry-run`` 只统计“将新增/更新多少”，一个字节也不写库。
- manifest 可以是 schema=1，也可以是旧格式（裸 files 列表 / ``path``+``code_guess``
  键名），格式归一化走 ``app.services.audit.read_manifest``。
- 普通目录里不像 manifest 的 json（如语料的 ``manifest.json``）会被忽略，避免造垃圾卷。

退出码：0 = 没有失败项（哪怕导入 0 盘但目录里确实没有 manifest？也算失败）；
1 = 有 manifest 解析失败 / 路径不存在 / 没找到任何 manifest。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许 `python scripts/rebuild_from_manifest.py` 直接跑（也支持 `-m scripts.xxx`）
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.services.rebuild import rebuild_from_db, rebuild_from_manifests  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rebuild_from_manifest",
        description="从磁带上的 ._manifest/<barcode>.json 重建 library_volume/library_file",
    )
    parser.add_argument(
        "--path",
        action="append",
        help="manifest 文件 / 本地 manifests 目录 / 旧磁带 ._manifest 目录；不传则用 data/manifests",
    )
    parser.add_argument(
        "--from-db",
        action="store_true",
        help="从数据库里的 TapeManifest 重建（文件副本丢失时用）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计将新增/更新多少行，不写库",
    )
    parser.add_argument("--json", action="store_true", help="只输出机器可读的 JSON")
    parser.add_argument("--quiet", action="store_true", help="不打印进度")
    return parser


def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"  # pragma: no cover


def _progress_printer(enabled: bool):
    if not enabled:
        return None

    def progress(done: int, message: str = "") -> None:
        sys.stderr.write(f"\r[{done:6d}] {message:<60}")
        sys.stderr.flush()

    return progress


def _print_human(stats) -> None:
    if stats.dry_run:
        print("== dry-run：不写库，以下为预计结果 ==")
    print(
        f"manifest：发现 {stats.manifests_found} 个，导入 {stats.manifests_imported} 个，"
        f"失败 {stats.manifests_failed} 个"
    )
    if stats.per_volume:
        print("按磁带：")
        for barcode, count in stats.per_volume.items():
            print(f"  {barcode}: {count} 个文件")
    print(
        f"卷：新建 {stats.volumes_created} / 更新 {stats.volumes_updated}；"
        f"文件：新增 {stats.files_created} / 更新 {stats.files_updated} / "
        f"未变 {stats.files_unchanged} / 跳过 {stats.files_skipped}"
    )
    print(
        f"分类：视频 {stats.videos} / sidecar {stats.sidecars} / 有番号 {stats.with_code}；"
        f"合计 {_human_bytes(stats.total_bytes)}"
    )
    print(f"耗时：{stats.elapsed_s:.2f}s")
    for error in stats.errors:
        print(f"错误: {error}", file=sys.stderr)
    if stats.dry_run:
        print("（dry-run 未写入任何数据）")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        app = create_app()
    except Exception as exc:
        print(f"创建应用失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    with app.app_context():
        try:
            progress = _progress_printer(not args.quiet and not args.json)
            if args.from_db:
                stats = rebuild_from_db(app, dry_run=args.dry_run, progress=progress)
            else:
                stats = rebuild_from_manifests(
                    app, args.path, dry_run=args.dry_run, progress=progress
                )
        except Exception as exc:  # rebuild 理论上不抛，这里兜底
            print(f"重建失败: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

    if not args.json and not args.quiet:
        sys.stderr.write("\r" + " " * 80 + "\r")
        sys.stderr.flush()
    if args.json:
        print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_human(stats)

    failed = stats.manifests_failed > 0 or stats.manifests_found == 0
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
