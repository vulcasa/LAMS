"""NAS / 磁带扫描器测试。

覆盖（契约 docs/CONTRACTS.md §2）：

- ``ScanStats`` 字段与语义：分类 / 番号 / flags / 置信度 / sidecar / 中文标记；
- 卷复用、增量（new/updated/unchanged）、软删与复活；
- 单文件 ``os.stat`` 失败不中断整批、不误软删；
- ``scan_tape`` 条码强校验（空 / UNKNOWN / 与 expected 不符）；
- 批量事务（按 CHUNK_ROWS 分桶 commit，不是一文件一 commit）；
- 真实合成语料 ``/tmp/av-archive-corpus`` 与 ``manifest.json`` 逐条对照；
- 1 万文件扫描 < 10s（临时目录，不写仓库）。

运行（项目根 = av-archive/）::

    .venv/bin/python -m pytest tests/test_scanner.py -q
    .venv/bin/python -m pytest tests/test_scanner.py -q -s   # 看性能打印

``/tmp/av-archive-corpus`` 不存在时语料相关用例自动 skip（不会把测试搞红）::

    python scripts/make_corpus.py --out /tmp/av-archive-corpus --clean
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 直接 `pytest tests/...` 时也能 import app
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.db import db  # noqa: E402
from app.models import LibraryFile, LibraryVolume  # noqa: E402
from app.services import scanner as scanner_mod  # noqa: E402
from app.services.code_parser import extract_code  # noqa: E402
from app.services.scanner import ScanStats, scan_nas, scan_tape  # noqa: E402

CORPUS = Path("/tmp/av-archive-corpus")
MANIFEST_PATH = CORPUS / "manifest.json"
needs_corpus = pytest.mark.skipif(
    not MANIFEST_PATH.exists(),
    reason="合成语料不存在：先跑 python scripts/make_corpus.py --out /tmp/av-archive-corpus",
)

PERF_DIRS = 100
PERF_PER_DIR = 100
PERF_FILES = PERF_DIRS * PERF_PER_DIR
PERF_LIMIT_S = 10.0
PERF_PREFIXES = ("SSIS", "SSNI", "IPX", "ABP", "MIDE", "STARS", "PRED", "JUQ", "WANZ", "HND")


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture()
def app(tmp_path):
    """独立 app + 独立 SQLite（不碰仓库 data/avlib.db，也不起后台 worker）。"""
    data_dir = tmp_path / "data"
    overrides = {
        "TESTING": True,
        "WORKER_ENABLED": False,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'avlib.db'}",
        "DATA_DIR": str(data_dir),
        "MEDIA_DIR": str(data_dir / "media"),
        "COVER_DIR": str(data_dir / "media" / "covers"),
        "STILL_DIR": str(data_dir / "media" / "stills"),
        "AVATAR_DIR": str(data_dir / "media" / "avatars"),
        "SUBTITLE_DIR": str(data_dir / "media" / "subtitles"),
        "THUMB_DIR": str(data_dir / "media" / "thumbs"),
        "DUMP_DIR": str(data_dir / "dump"),
    }
    application = create_app(overrides)
    with application.app_context():
        yield application
        db.session.remove()
        db.engine.dispose()


def write_file(root: Path, rel: str, content: bytes = b"") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def rows_by_rel(volume_id: int) -> dict[str, LibraryFile]:
    return {
        row.rel_path: row
        for row in LibraryFile.query.filter_by(volume_id=volume_id).all()
    }


def make_nas_sample(root: Path) -> dict[str, bytes]:
    """小型 NAS 树：正常 / 中字 / 无码 / 垃圾名 / 父目录兜底 / 各种必须跳过的目录。"""
    files = {
        "JAV/S1/SSIS-001/SSIS-001.mp4": b"\x00" * 100,
        "JAV/S1/SSIS-001/SSIS-001-C.srt": "1\n00:00:01,000 --> 00:00:02,000\n中字\n".encode("utf-8"),
        "JAV/S1/SSIS-001/SSIS-001.nfo": b'<?xml version="1.0"?><movie/>',
        "JAV/S1/SSIS-001/cover.jpg": b"\xff\xd8\xff",  # 文件本身无番号，靠父目录兜底
        "downloads/ssni002-4K-C.mp4": b"",
        "中文/無碼-IPX-003-UC.mkv": b"",
        "_未分类/1080P.mp4": b"",
        "_未分类/某某老师合集第三部.mkv": b"",
    }
    for rel, content in files.items():
        write_file(root, rel, content)
    # 这些目录/文件必须被跳过（SKIP_DIRS + 点开头）
    for rel in ("@eaDir/thumb.mp4", "#recycle/x.mp4", ".hidden/secret.mp4", ".DS_Store"):
        write_file(root, rel, b"junk")
    return files


def make_tape_tree(mount: Path) -> dict[str, bytes]:
    files = {
        "movies/2019/SSIS-001.mp4": b"",
        "movies/2019/SSIS-001-C.srt": b"1\n",
        "uncensored/IPX-003-UC.mp4": b"",
        "_unknown/1080P.mp4": b"",
    }
    for rel, content in files.items():
        write_file(mount, rel, content)
    # 盘点时写回磁带的 manifest 不能当媒体文件入库
    write_file(mount, "._manifest/LTO6-A001.json", b"{}")
    return files


def _make_perf_tree(root: Path) -> None:
    for d in range(PERF_DIRS):
        sub = root / f"d{d:03d}"
        sub.mkdir(parents=True, exist_ok=True)
        prefix = PERF_PREFIXES[d % len(PERF_PREFIXES)]
        for i in range(PERF_PER_DIR):
            (sub / f"{prefix}-{i + 1:03d}.mp4").write_bytes(b"")


def _video_exts(app) -> set[str]:
    return {ext.lower() for ext in app.config["VIDEO_EXTS"]}


# ---------------------------------------------------------------------------
# ScanStats 契约
# ---------------------------------------------------------------------------
def test_scanstats_contract_fields(app, tmp_path):
    root = tmp_path / "one"
    write_file(root, "SSIS-001.mp4")
    stats = scan_nas(app, str(root))
    assert list(asdict(stats)) == [
        "volume_id",
        "kind",
        "root",
        "files_seen",
        "videos",
        "sidecars",
        "new",
        "updated",
        "unchanged",
        "deleted",
        "with_code",
        "low_confidence",
        "no_code",
        "chinese_sub_files",
        "elapsed_s",
        "errors",
    ]
    assert isinstance(stats.errors, list)
    assert isinstance(stats.elapsed_s, float) and stats.elapsed_s >= 0
    assert isinstance(stats.volume_id, int)


# ---------------------------------------------------------------------------
# NAS：分类 / 番号 / flags / sidecar
# ---------------------------------------------------------------------------
def test_scan_nas_basic(app, tmp_path):
    root = tmp_path / "nas"
    files = make_nas_sample(root)
    # 顺便验证不跟「指向目录的符号链接」
    try:
        os.symlink(root / "JAV", root / "jav-link")
    except OSError:  # pragma: no cover - 个别系统不允许
        pass

    # 传个带尾斜杠的路径，卷 ref 必须是 resolve 后的绝对路径
    stats = scan_nas(app, f"{root}{os.sep}", volume_name="NAS-AV")

    assert isinstance(stats, ScanStats)
    assert stats.kind == "nas"
    assert stats.root == str(root.resolve())
    assert stats.files_seen == 8          # 跳过目录里的 4 个文件不算
    assert stats.videos == 5
    assert stats.sidecars == 3
    assert stats.new == 8
    assert (stats.updated, stats.unchanged, stats.deleted) == (0, 0, 0)
    assert stats.with_code == 6
    assert stats.no_code == 2
    assert stats.low_confidence == 2      # cover.jpg 父目录兜底 70 + ssni002 粘连 76
    assert stats.chinese_sub_files == 3
    assert stats.errors == []
    assert stats.elapsed_s >= 0

    volume = LibraryVolume.query.one()
    assert volume.id == stats.volume_id
    assert (volume.kind, volume.ref, volume.name) == ("nas", str(root.resolve()), "NAS-AV")
    assert volume.last_scan_status == "ok"
    assert volume.last_scan_at is not None
    assert volume.used_bytes == sum(len(content) for content in files.values())
    assert volume.enabled is True

    rows = rows_by_rel(volume.id)
    assert set(rows) == set(files)  # 跳过的目录/点文件一条都不许进库

    main = rows["JAV/S1/SSIS-001/SSIS-001.mp4"]
    assert main.name == "SSIS-001.mp4"
    assert main.is_sidecar is False
    assert main.code_guess == "SSIS-001"
    assert main.confidence == 94
    assert main.flags == ""
    assert main.size == 100
    assert main.mtime is not None
    assert main.seen_at is not None
    assert main.deleted_at is None

    sub = rows["JAV/S1/SSIS-001/SSIS-001-C.srt"]
    assert sub.is_sidecar is True
    assert sub.code_guess == "SSIS-001"
    assert sub.flags == "chinese_sub"

    nfo = rows["JAV/S1/SSIS-001/SSIS-001.nfo"]
    assert nfo.is_sidecar is True
    assert nfo.code_guess == "SSIS-001"

    # 父目录兜底：cover.jpg 自身没有番号，靠 <番号> 目录拿到，置信度封顶 < auto_min
    cover = rows["JAV/S1/SSIS-001/cover.jpg"]
    assert cover.is_sidecar is True
    assert cover.code_guess == "SSIS-001"
    assert 0 < cover.confidence < app.config["CODE_AUTO_MIN"]

    glued = rows["downloads/ssni002-4K-C.mp4"]
    assert glued.code_guess == "SSNI-002"
    assert glued.flags == "chinese_sub,hi_res"

    uncensored = rows["中文/無碼-IPX-003-UC.mkv"]
    assert uncensored.code_guess == "IPX-003"
    assert uncensored.flags == "chinese_sub,uncensored"

    for rel in ("_未分类/1080P.mp4", "_未分类/某某老师合集第三部.mkv"):
        row = rows[rel]
        assert row.code_guess is None
        assert row.confidence == 0


def test_scan_nas_rescan_is_idempotent(app, tmp_path):
    root = tmp_path / "nas"
    make_nas_sample(root)
    first = scan_nas(app, str(root))
    second = scan_nas(app, str(root), volume_name="renamed")

    assert second.volume_id == first.volume_id
    assert LibraryVolume.query.count() == 1
    assert (second.new, second.updated, second.deleted) == (0, 0, 0)
    assert second.unchanged == first.files_seen
    assert LibraryVolume.query.one().name == "renamed"  # --name 可以改名
    assert LibraryVolume.query.one().last_scan_status == "ok"


# ---------------------------------------------------------------------------
# NAS：increment / soft delete / revive
# ---------------------------------------------------------------------------
def test_scan_nas_updated_deleted_and_revived(app, tmp_path):
    root = tmp_path / "lib"
    path_a = write_file(root, "a/SSIS-001.mp4", b"a" * 10)
    path_b = write_file(root, "b/SSNI-002.mp4", b"b" * 20)
    first = scan_nas(app, str(root))
    assert (first.new, first.videos, first.no_code) == (2, 2, 0)
    rows = rows_by_rel(first.volume_id)
    a_id = rows["a/SSIS-001.mp4"].id
    b_id = rows["b/SSNI-002.mp4"].id
    before_b = path_b.stat()

    # 1) 内容变大 → updated
    path_a.write_bytes(b"a" * 99)
    second = scan_nas(app, str(root))
    assert (second.new, second.updated, second.unchanged, second.deleted) == (0, 1, 1, 0)
    assert rows_by_rel(first.volume_id)["a/SSIS-001.mp4"].size == 99

    # 2) size 不变、只改 mtime → updated
    os.utime(path_a, (before_b.st_atime, before_b.st_mtime + 10))
    third = scan_nas(app, str(root))
    assert (third.updated, third.unchanged) == (1, 1)

    # 3) 删掉一个 → 软删：行保留、deleted 计数 1
    os.remove(path_b)
    fourth = scan_nas(app, str(root))
    assert (fourth.files_seen, fourth.unchanged, fourth.deleted) == (1, 1, 1)
    deleted_row = db.session.get(LibraryFile, b_id)
    assert deleted_row.deleted_at is not None
    assert deleted_row.code_guess == "SSNI-002"

    # 4) 原样放回（size 相同 + 恢复 mtime）→ 同一行复活：unchanged、不新增行
    path_b.write_bytes(b"b" * 20)
    os.utime(path_b, ns=(before_b.st_atime_ns, before_b.st_mtime_ns))
    fifth = scan_nas(app, str(root))
    assert (fifth.new, fifth.updated, fifth.unchanged, fifth.deleted) == (0, 0, 2, 0)
    revived = db.session.get(LibraryFile, b_id)
    assert revived is not None
    assert revived.deleted_at is None
    assert LibraryFile.query.count() == 2
    assert {row.id for row in LibraryFile.query.all()} == {a_id, b_id}


def test_stat_failure_records_error_and_keeps_old_row(app, tmp_path):
    root = tmp_path / "err"
    bad = write_file(root, "a/SSIS-001.mp4", b"x")
    write_file(root, "b/SSNI-002.mp4", b"y")
    first = scan_nas(app, str(root))
    assert (first.files_seen, first.new, first.errors) == (2, 2, [])
    bad_id = rows_by_rel(first.volume_id)["a/SSIS-001.mp4"].id

    # 把文件换成「断链符号链接」：os.stat 会失败，但不能中断整批、也不能误软删旧行
    bad.unlink()
    try:
        os.symlink(root / "does-not-exist.mp4", bad)
    except OSError:  # pragma: no cover
        pytest.skip("系统不支持 symlink")

    second = scan_nas(app, str(root))
    assert second.files_seen == 1
    assert (second.new, second.updated, second.unchanged, second.deleted) == (0, 0, 1, 0)
    assert len(second.errors) == 1
    assert "a/SSIS-001.mp4" in second.errors[0]
    kept = db.session.get(LibraryFile, bad_id)
    assert kept is not None and kept.deleted_at is None
    assert LibraryVolume.query.one().last_scan_status == "partial"


def test_scan_nas_missing_root_raises_without_creating_volume(app, tmp_path):
    with pytest.raises(ValueError):
        scan_nas(app, str(tmp_path / "not-mounted"))
    file_path = tmp_path / "a-file"
    file_path.write_bytes(b"x")
    with pytest.raises(ValueError):
        scan_nas(app, str(file_path))
    assert LibraryVolume.query.count() == 0
    assert LibraryFile.query.count() == 0


# ---------------------------------------------------------------------------
# 磁带
# ---------------------------------------------------------------------------
def test_scan_tape_creates_tape_volume(app, tmp_path):
    mount = tmp_path / "tape"
    files = make_tape_tree(mount)
    stats = scan_tape(app, str(mount), barcode="LTO6-A001", expected_barcode="LTO6-A001")

    assert stats.kind == "tape"
    assert stats.root == str(mount.resolve())
    assert stats.files_seen == len(files)  # ._manifest/ 被跳过
    assert (stats.videos, stats.sidecars) == (3, 1)
    assert (stats.with_code, stats.no_code) == (3, 1)
    assert stats.chinese_sub_files == 2
    assert stats.new == len(files)
    assert stats.errors == []

    volume = LibraryVolume.query.one()
    assert (volume.kind, volume.ref, volume.name) == ("tape", "LTO6-A001", "LTO6-A001")
    assert volume.last_scan_status == "ok"
    rows = rows_by_rel(volume.id)
    assert set(rows) == set(files)
    assert rows["movies/2019/SSIS-001.mp4"].code_guess == "SSIS-001"
    assert rows["movies/2019/SSIS-001-C.srt"].is_sidecar is True
    assert rows["movies/2019/SSIS-001-C.srt"].flags == "chinese_sub"

    # 同一盘重复扫 → 幂等
    again = scan_tape(app, str(mount), barcode="LTO6-A001")
    assert again.volume_id == stats.volume_id
    assert (again.new, again.updated, again.deleted) == (0, 0, 0)
    assert again.unchanged == stats.files_seen


@pytest.mark.parametrize(
    "barcode, expected",
    [
        ("", None),            # 空条码
        ("   ", None),         # 全空白
        ("UNKNOWN", None),     # 没读到条码
        ("unknown", None),     # 大小写变体
        ("LTO6-A001", "LTO6-A002"),  # 与预期不符 = 串盘
    ],
)
def test_scan_tape_barcode_guards(app, tmp_path, barcode, expected):
    mount = tmp_path / "tape"
    write_file(mount, "movies/SSIS-001.mp4", b"")
    with pytest.raises(ValueError):
        scan_tape(app, str(mount), barcode=barcode, expected_barcode=expected)
    # 校验失败必须在建卷/写文件之前发生
    assert LibraryVolume.query.count() == 0
    assert LibraryFile.query.count() == 0


# ---------------------------------------------------------------------------
# 事务批量化 / 进度回调
# ---------------------------------------------------------------------------
def test_scan_commits_in_batches(app, tmp_path, monkeypatch):
    flushes: list[int] = []
    real_flush = scanner_mod._Batch.flush

    def spy_flush(self, **kwargs):
        if self.pending:
            flushes.append(self.pending)
        return real_flush(self, **kwargs)

    monkeypatch.setattr(scanner_mod._Batch, "flush", spy_flush)
    monkeypatch.setattr(scanner_mod, "CHUNK_ROWS", 50)  # 缩小批量便于观察

    root = tmp_path / "batch"
    for i in range(120):
        write_file(root, f"d{i:03d}/SSIS-{i + 1:03d}.mp4")

    stats = scan_nas(app, str(root))
    assert stats.new == 120
    assert sum(flushes) == 120           # 所有行都经过批量缓冲
    assert len(flushes) >= 2             # 不是「一文件一 commit」
    assert max(flushes) <= 50            # 每批不超过 CHUNK_ROWS


def test_progress_callback(app, tmp_path, monkeypatch):
    monkeypatch.setattr(scanner_mod, "PROGRESS_FILES", 3)
    root = tmp_path / "progress"
    for i in range(10):
        write_file(root, f"SSIS-{i + 1:03d}.mp4")

    calls: list[tuple[int, str]] = []

    def progress(done: int, message: str = "") -> None:
        calls.append((done, message))

    stats = scan_nas(app, str(root), progress=progress)
    assert calls, "progress 回调一次都没被调用"
    dones = [done for done, _ in calls]
    assert dones == sorted(dones), f"progress 的 done 必须单调不减: {dones}"
    assert dones[-1] == stats.files_seen == 10
    assert all(isinstance(message, str) for _, message in calls)


def test_walk_error_keeps_rows_under_failed_dir(app, tmp_path, monkeypatch):
    """目录读失败只记 error，不能把该目录下的旧行软删（NAS 抖动/权限/LTFS 卡带）。"""
    root = tmp_path / "nas"
    write_file(root, "ok/SSIS-001.mp4")
    write_file(root, "locked/SSNI-002.mp4")
    first = scan_nas(app, str(root))
    assert first.files_seen == 2
    ids = {rel: row.id for rel, row in rows_by_rel(first.volume_id).items()}

    real_walk = scanner_mod.os.walk

    def walk_with_locked_error(top, topdown=True, onerror=None, followlinks=False):
        for dirpath, dirnames, filenames in real_walk(
            top, topdown=topdown, onerror=onerror, followlinks=followlinks
        ):
            if dirpath == str(root) and "locked" in dirnames:
                dirnames.remove("locked")  # 模拟这个目录读失败
                exc = OSError("模拟目录读取失败")
                exc.filename = os.path.join(dirpath, "locked")
                if onerror is not None:
                    onerror(exc)
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(scanner_mod.os, "walk", walk_with_locked_error)
    second = scan_nas(app, str(root))
    assert second.files_seen == 1
    assert len(second.errors) == 1 and "locked" in second.errors[0]
    assert second.deleted == 0, "目录读失败时锁住的旧行不能被软删"
    assert db.session.get(LibraryFile, ids["locked/SSNI-002.mp4"]).deleted_at is None
    assert db.session.get(LibraryFile, ids["ok/SSIS-001.mp4"]).deleted_at is None

    # root 整棵都读不到（挂载点还在但没内容）→ 更保守：一行都不软删
    def walk_all_failed(top, topdown=True, onerror=None, followlinks=False):
        exc = OSError("模拟 root 读取失败")
        exc.filename = str(top)
        if onerror is not None:
            onerror(exc)
        return iter(())

    monkeypatch.setattr(scanner_mod.os, "walk", walk_all_failed)
    third = scan_nas(app, str(root))
    assert third.files_seen == 0
    assert third.deleted == 0
    assert LibraryFile.query.filter(LibraryFile.deleted_at.is_(None)).count() == 2
    assert LibraryVolume.query.one().last_scan_status == "partial"


# ---------------------------------------------------------------------------
# 性能：1 万文件 < 10s
# ---------------------------------------------------------------------------
def test_scan_10k_files_under_10s(app, tmp_path):
    root = tmp_path / "perf"
    _make_perf_tree(root)

    started = time.monotonic()
    stats = scan_nas(app, str(root))
    elapsed = time.monotonic() - started
    print(
        f"\n[perf] 1 万文件首次扫描: {elapsed:.2f}s（scanner 内部 {stats.elapsed_s:.2f}s）"
    )
    assert stats.files_seen == PERF_FILES
    assert stats.videos == PERF_FILES
    assert stats.new == PERF_FILES
    assert stats.with_code == PERF_FILES
    assert stats.errors == []
    assert elapsed < PERF_LIMIT_S, f"1 万文件扫描耗时 {elapsed:.2f}s ≥ {PERF_LIMIT_S}s"

    started = time.monotonic()
    again = scan_nas(app, str(root))
    elapsed2 = time.monotonic() - started
    print(f"[perf] 1 万文件重复扫描: {elapsed2:.2f}s")
    assert (again.new, again.updated, again.deleted) == (0, 0, 0)
    assert again.unchanged == PERF_FILES
    assert elapsed2 < PERF_LIMIT_S


# ---------------------------------------------------------------------------
# 真实合成语料：与 manifest.json 逐条对照
# ---------------------------------------------------------------------------
def _corpus_entries(manifest: dict, tree: str) -> list[dict]:
    return [entry for entry in manifest["entries"] if entry["tree"] == tree]


def _assert_corpus_tree(app, stats: ScanStats, entries: list[dict]) -> None:
    """扫描结果 vs manifest.json（fails 时把 rel_path 打出来）。"""
    video_exts = _video_exts(app)
    auto_min = int(app.config["CODE_AUTO_MIN"])

    rows = rows_by_rel(stats.volume_id)
    expected_rels = {entry["rel_path"] for entry in entries}
    assert set(rows) == expected_rels

    expected_videos = sum(
        1 for entry in entries
        if os.path.splitext(entry["rel_path"])[1].lower() in video_exts
    )
    expected_with_code = [entry for entry in entries if entry["expect_code"]]
    expected_no_code = [entry for entry in entries if not entry["expect_code"]]
    expected_low = expected_chinese = 0

    for entry in entries:
        rel = entry["rel_path"]
        match = extract_code(rel)
        if 0 < match.confidence < auto_min:
            expected_low += 1
        if "chinese_sub" in match.flags:
            expected_chinese += 1

        row = rows[rel]
        # 分类：Config.VIDEO_EXTS 是视频，其余（srt/nfo/jpg/vob/...）都是 sidecar
        expected_sidecar = os.path.splitext(rel)[1].lower() not in video_exts
        assert row.is_sidecar is expected_sidecar, rel
        assert row.name == rel.rsplit("/", 1)[-1], rel

        if entry["expect_code"]:
            assert row.code_guess == entry["expect_code"], rel
            if entry["kind"] == "video":
                got_flags = row.flags.split(",") if row.flags else []
                assert got_flags == sorted(entry["expect_flags"]), rel
        else:
            # 30 条故意垃圾名必须 no_code + 置信度 0
            assert row.code_guess is None, rel
            assert row.confidence == 0, rel

    assert stats.files_seen == len(entries)
    assert stats.videos == expected_videos
    assert stats.sidecars == len(entries) - expected_videos
    assert stats.with_code == len(expected_with_code)
    assert stats.no_code == len(expected_no_code)
    assert stats.low_confidence == expected_low
    assert stats.chinese_sub_files == expected_chinese
    assert stats.new == len(entries)
    assert (stats.updated, stats.unchanged, stats.deleted) == (0, 0, 0)
    assert stats.errors == []

    # 语料固定 10% 垃圾名：每套 300 视频里 30 条
    assert stats.no_code == 30
    assert stats.with_code == len(entries) - 30


@needs_corpus
def test_corpus_nas_matches_manifest(app):
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    entries = _corpus_entries(manifest, "nas")
    stats = scan_nas(app, str(CORPUS / "nas"), volume_name="corpus-nas")
    _assert_corpus_tree(app, stats, entries)

    # 真实语料重复扫描：new=0、unchanged 全量、deleted=0
    again = scan_nas(app, str(CORPUS / "nas"))
    assert again.volume_id == stats.volume_id
    assert (again.new, again.updated, again.deleted) == (0, 0, 0)
    assert again.unchanged == stats.files_seen


@needs_corpus
def test_corpus_tape_matches_manifest(app):
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    entries = _corpus_entries(manifest, "tape")
    stats = scan_tape(app, str(CORPUS / "tape"), barcode="LTO6-A001")
    _assert_corpus_tree(app, stats, entries)

    again = scan_tape(app, str(CORPUS / "tape"), barcode="LTO6-A001")
    assert again.volume_id == stats.volume_id
    assert (again.new, again.updated, again.deleted) == (0, 0, 0)
    assert again.unchanged == stats.files_seen


@needs_corpus
def test_corpus_tape_per_barcode_volumes(app):
    """磁带挂载点下按 <barcode>/ 分目录时，每盘单独扫描不串盘、rel_path 相对挂载点。"""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    tape_root = CORPUS / "tape"
    all_tape_entries = _corpus_entries(manifest, "tape")
    seen_volume_ids = set()

    for barcode in ("LTO6-A001", "LTO6-B002"):
        stats = scan_tape(app, str(tape_root / barcode), barcode=barcode)
        entries = [
            entry for entry in all_tape_entries
            if entry["rel_path"].startswith(f"{barcode}/")
        ]
        assert stats.files_seen == len(entries)
        rows = rows_by_rel(stats.volume_id)
        expected_rels = {
            entry["rel_path"][len(barcode) + 1:] for entry in entries
        }
        assert set(rows) == expected_rels
        # 每盘只归自己的文件，rel_path 里不会带条码前缀
        assert all(not rel.startswith("LTO6-") for rel in rows)
        seen_volume_ids.add(stats.volume_id)

    assert len(seen_volume_ids) == 2
    assert LibraryVolume.query.filter_by(kind="tape").count() == 2


def test_rescan_updates_code_confidence_when_parser_changes(app, tmp_path):
    """文件没变、但解析器升级了：增量扫描也要刷新 code/confidence/flags。"""
    root = tmp_path / "parser-upgrade"
    write_file(root, "ALDN/ALDN-336.2024.1080p.DMM.WEB-DL.AAC2.0.H.264-CTRL.mkv", b"x")
    first = scan_nas(app, str(root))
    with app.app_context():
        row = LibraryFile.query.filter_by(volume_id=first.volume_id).one()
        row.code_guess = "OLD-000"
        row.confidence = 80
        db.session.commit()

    second = scan_nas(app, str(root))

    assert second.new == 0
    assert second.updated == 1
    assert second.unchanged == 0
    with app.app_context():
        row = LibraryFile.query.filter_by(volume_id=first.volume_id).one()
        assert row.code_guess == "ALDN-336"
        assert row.confidence >= 90
