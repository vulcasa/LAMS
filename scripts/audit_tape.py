"""磁带盘点 CLI：mount → scan → manifest → eject 一条命令跑完。

用法（项目根 = av-archive/）::

    # 默认走 v1 的 HTTP API（v1 独占驱动器时唯一安全的方式）
    python -m scripts.audit_tape --barcode FB1186

    # 条码强校验（推荐；--expected 省略时默认等于 --barcode）
    python -m scripts.audit_tape --barcode FB1186 --expected FB1186

    # v1 没跑时直连 ltfs（自己独占设备，别和 v1 同时用！）
    python -m scripts.audit_tape --barcode FB1186 --adapter direct --device /dev/sg10

    # 调试：不写 manifest、只输出 JSON
    python -m scripts.audit_tape --barcode FB1186 --no-manifest --json

退出码：0 = done；1 = failed/aborted（看输出里的 status 与 errors）。

注意：``--barcode`` 是"用户声明的当前插入磁带条码"，会与 mount 返回的实际条码比对；
不一致或实际条码为 UNKNOWN 时**立刻中止并退出磁带**，防止把 A 盘的文件记到 B 盘。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许 `python scripts/audit_tape.py` 直接跑（也支持 `python -m scripts.audit_tape`）
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.services.audit import run_audit  # noqa: E402
from app.services.tape_adapter import get_adapter  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audit_tape",
        description="盘点一盘已插入的磁带：强校验条码 → 挂载 → 扫描 → 写 manifest → 弹出",
    )
    parser.add_argument(
        "--barcode",
        required=True,
        help="当前插入磁带的条码（必填，用于和 mount 返回的实际条码比对，防串盘）",
    )
    parser.add_argument(
        "--expected",
        default=None,
        help="实际条码必须等于此值；省略时默认等于 --barcode；显式传空串表示只要求非 UNKNOWN",
    )
    parser.add_argument(
        "--adapter",
        choices=["http", "direct"],
        default=None,
        help="磁带适配器：http=调 ltfs-webui API（默认）；direct=直接调 ltfs 命令",
    )
    parser.add_argument(
        "--no-manifest",
        action="store_true",
        help="不写 <mount>/._manifest/<barcode>.json（调试/只读盘用）",
    )
    parser.add_argument("--api-base", default=None, help="覆盖 LTFS_API_BASE，如 http://127.0.0.1:5001")
    parser.add_argument("--device", default=None, help="覆盖 LTFS_DEVICE（direct 用），如 /dev/sg10")
    parser.add_argument("--mount", default=None, help="覆盖 LTFS_MOUNT（LTFS 挂载点）")
    parser.add_argument("--timeout", type=float, default=None, help="覆盖 LTFS_TIMEOUT（秒）")
    parser.add_argument("--json", action="store_true", help="只输出机器可读的 JSON 结果")
    parser.add_argument("--quiet", action="store_true", help="不打印进度")
    return parser


def _progress_printer(enabled: bool):
    if not enabled:
        return None

    def progress(done: int, message: str = "") -> None:
        sys.stderr.write(f"\r[{done:3d}%] {message:<40}")
        sys.stderr.flush()

    return progress


def _print_human(result: dict) -> None:
    print(f"状态: {result.get('status')}")
    print(f"条码: 预期 {result.get('expected_barcode')} / 实际 {result.get('actual_barcode')}")
    if result.get("run_id") is not None:
        print(f"AuditRun: #{result['run_id']}")
    print(f"文件数: 共 {result.get('files_seen', 0)}，新增 {result.get('files_new', 0)}")
    manifest = result.get("manifest_path")
    print(f"manifest: {manifest or '（未写）'}")
    stats = result.get("stats") or {}
    if stats:
        print(
            "扫描统计: "
            f"videos={stats.get('videos', 0)} sidecars={stats.get('sidecars', 0)} "
            f"with_code={stats.get('with_code', 0)} no_code={stats.get('no_code', 0)} "
            f"updated={stats.get('updated', 0)} unchanged={stats.get('unchanged', 0)}"
        )
    for err in result.get("errors") or []:
        print(f"错误: {err}", file=sys.stderr)
    print(f"结论: {result.get('message')}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    overrides = {}
    if args.adapter:
        overrides["TAPE_ADAPTER"] = args.adapter
    if args.api_base:
        overrides["LTFS_API_BASE"] = args.api_base
    if args.device:
        overrides["LTFS_DEVICE"] = args.device
    if args.mount:
        overrides["LTFS_MOUNT"] = args.mount
    if args.timeout is not None:
        overrides["LTFS_TIMEOUT"] = args.timeout

    try:
        app = create_app(overrides=overrides)
    except Exception as exc:  # 例如路由模块还没就绪
        print(f"创建应用失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    with app.app_context():
        try:
            adapter = get_adapter(app.config, app=app)
            result = run_audit(
                app,
                args.barcode,
                expected_barcode=args.expected,
                adapter=adapter,
                write_manifest=not args.no_manifest,
                progress=_progress_printer(not args.quiet and not args.json),
            )
        except Exception as exc:  # run_audit 理论上不抛，这里兜底
            print(f"盘点失败: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

    if not args.json:
        sys.stderr.write("\r" + " " * 60 + "\r")
        sys.stderr.flush()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_human(result)
    return 0 if result.get("status") == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
