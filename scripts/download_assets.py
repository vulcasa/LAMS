"""新增：封面 / 剧照 / 缩略图下载 CLI。

    python -m scripts.download_assets                  # 全库缺图的都下
    python -m scripts.download_assets --limit 20       # 只处理前 20 个 Title
    python -m scripts.download_assets --code SSIS-001 --code STARS-001 --force
    python -m scripts.download_assets --data-dir ./data --code DASD-061

代理按工作区 AGENTS.md：读 ``HTTPS_PROXY`` / ``HTTP_PROXY`` 环境变量，不硬编码。
"""
import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from app.config import Config  # noqa: E402
from app.services.assets import AssetStats, download_for_titles  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="下载封面 / 剧照 / 缩略图（DMM pics），回写 Title.cover_local 与 Asset.local_path",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少个 Title")
    parser.add_argument("--code", action="append", dest="codes", metavar="DVD_ID",
                        help="只处理指定番号，可重复（如 --code SSIS-001）")
    parser.add_argument("--force", action="store_true",
                        help="已有本地文件也重新下载（修复占位图/坏图）")
    parser.add_argument("--data-dir", default=None,
                        help="数据目录（默认 Config.DATA_DIR），库为 <data-dir>/avlib.db")
    parser.add_argument("--workers", type=int, default=None, help="下载并发数（默认 4）")
    parser.add_argument("--min-interval", type=float, default=None,
                        help="全局请求最小间隔秒数（默认 0.15）")
    parser.add_argument("--stats-json", default=None, help="把统计写入该 JSON 文件")
    parser.add_argument("--quiet", action="store_true", help="不打印逐条进度")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # 先检查主库存在再 create_app——create_app 会 db.create_all() 顺手建空库
    if args.data_dir:
        data_dir = Path(args.data_dir)
        db_path = data_dir / "avlib.db"
    else:
        data_dir = None
        db_path = Path(Config.DB_PATH)
    if not db_path.is_file():
        print(f"主库不存在: {db_path}（先跑 scripts/dev_seed 或指定 --data-dir）",
              file=sys.stderr)
        return 2

    overrides: dict = {"WORKER_ENABLED": False}
    if data_dir is not None:
        overrides.update({
            "DATA_DIR": data_dir,
            "DB_PATH": db_path,
            "MEDIA_DIR": data_dir / "media",
            "COVER_DIR": data_dir / "media" / "covers",
            "STILL_DIR": data_dir / "media" / "stills",
            "AVATAR_DIR": data_dir / "media" / "avatars",
            "SUBTITLE_DIR": data_dir / "media" / "subtitles",
            "THUMB_DIR": data_dir / "media" / "thumbs",
            "DUMP_DIR": data_dir / "dump",
        })
    if args.workers is not None:
        overrides["ASSET_WORKERS"] = args.workers
    if args.min_interval is not None:
        overrides["ASSET_MIN_INTERVAL"] = args.min_interval

    app = create_app(overrides)

    def progress(done: int, message: str = "") -> None:
        if not args.quiet:
            print(f"[{done}] {message}", file=sys.stderr, flush=True)

    with app.app_context():
        media_dir = app.config["MEDIA_DIR"]
        try:
            stats: AssetStats = download_for_titles(
                app,
                limit=args.limit,
                only_codes=args.codes,
                force=args.force,
                progress=progress,
            )
        except KeyboardInterrupt:
            print("已中断", file=sys.stderr)
            return 130

    payload = asdict(stats)
    print(
        f"considered={stats.considered} downloaded={stats.downloaded} "
        f"(cover={stats.covers} still={stats.stills} thumb={stats.thumbs}) "
        f"skipped={stats.skipped} failed={stats.failed} "
        f"placeholders={stats.placeholders} bytes={stats.bytes} "
        f"elapsed={stats.elapsed_s:.3f}s"
    )
    if stats.errors:
        print(f"errors={len(stats.errors)}（前 10 条）", file=sys.stderr)
        for message in stats.errors[:10]:
            print(f"  - {message}", file=sys.stderr)
    print(f"媒体目录: {media_dir}")
    if args.stats_json:
        target = Path(args.stats_json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        print(f"统计已写入: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
