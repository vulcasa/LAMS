"""从磁带 manifest 重建数据库的测试。

目标场景（任务 task-12）：主库文件全丢 / 换机器，只剩磁带上的
``._manifest/<barcode>.json``，要能重建 ``library_volume`` + ``library_file``，
之后 ``materialize`` 仍能按番号重新关联。

覆盖：

- 用 :func:`app.services.audit.write_manifest` 造**真实格式** manifest（Unicode 名、
  深层路径、字幕 sidecar），导入临时 DB 后逐字段断言；
- 路径形态：挂载根目录 / ``._manifest`` 目录 / 备份目录（json 直接散放）/ 显式文件，
  以及同一路径重复传入去重；
- 幂等：重复导入不重复行、不改 seen_at；
- 失效行更新（size/code 变化）与软删行被 manifest 复活；
- 不软删其它卷 / 本卷多出来的旧行；
- 同一条码多份 manifest：取最新 generated_at 的卷级元数据，文件取并集、同路径以新为准；
- 旧格式兼容（无 schema / 裸 list / ``path``+``code_guess`` 键名）；
- dry-run 不写库；损坏 / 非 manifest 的 json / 不存在的路径只记错误不中断；
- 条码 UNKNOWN 的 manifest 跳过（防串盘）；
- mtime 各种写法（naive ISO / Z / epoch / None）解析；
- 1 万文件重建耗时与二次导入幂等耗时。

运行（项目根 = av-archive/）::

    .venv/bin/python -m pytest tests/test_rebuild.py -q
"""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import create_app
from app.db import db
from app.models import LibraryFile, LibraryVolume
from app.services import audit
from app.services.audit import MANIFEST_DIRNAME, read_manifest, write_manifest
from app.services.rebuild import (
    RebuildStats,
    discover_manifests,
    rebuild_from_manifests,
)

PERF_FILES = 10_000
PERF_CREATE_LIMIT_S = 20.0
PERF_SECOND_RUN_LIMIT_S = 15.0


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture()
def app(tmp_path):
    """独立 app + 独立 SQLite；不起后台 worker，不碰仓库 data/。"""
    data_dir = tmp_path / "data"
    application = create_app(
        {
            "TESTING": True,
            "WORKER_ENABLED": False,
            "DATA_DIR": data_dir,
            "DB_PATH": data_dir / "avlib.db",
            "MEDIA_DIR": data_dir / "media",
            "COVER_DIR": data_dir / "media" / "covers",
            "STILL_DIR": data_dir / "media" / "stills",
            "AVATAR_DIR": data_dir / "media" / "avatars",
            "SUBTITLE_DIR": data_dir / "media" / "subtitles",
            "THUMB_DIR": data_dir / "media" / "thumbs",
            "DUMP_DIR": data_dir / "dump",
        }
    )
    with application.app_context():
        yield application
        db.session.remove()
        db.engine.dispose()


def scan_stats(**overrides) -> SimpleNamespace:
    payload = {
        "volume_id": None,
        "kind": "tape",
        "root": "",
        "files_seen": 0,
        "videos": 0,
        "sidecars": 0,
        "new": 0,
        "updated": 0,
        "unchanged": 0,
        "deleted": 0,
        "with_code": 0,
        "low_confidence": 0,
        "no_code": 0,
        "chinese_sub_files": 0,
        "elapsed_s": 0.0,
        "errors": [],
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def make_manifest(
    root: Path,
    barcode: str,
    entries: list[dict],
    *,
    name: str | None = None,
) -> Path:
    """在 root 下造真实媒体文件，并用 audit.write_manifest 写正式 manifest。

    ``entries`` 每项：``{"rel_path", "size", "code", "confidence", "flags"}``。
    """
    for entry in entries:
        path = root / entry["rel_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * int(entry.get("size") or 1))
    extra = {
        "files": [
            {
                "rel_path": entry["rel_path"],
                "code": entry.get("code"),
                "confidence": entry.get("confidence"),
                "flags": entry.get("flags"),
            }
            for entry in entries
        ]
    }
    if name:
        extra["name"] = name
    manifest_path = write_manifest(
        root, barcode, scan_stats(files_seen=len(entries)), extra
    )
    return Path(manifest_path)


def rewrite_manifest(path: Path, **updates) -> Path:
    """直接改 manifest 字段（造旧格式 / 指定 generated_at / 改条码）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(updates)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def volume_of(barcode: str) -> LibraryVolume | None:
    return LibraryVolume.query.filter_by(kind="tape", ref=barcode).first()


def files_of(barcode: str) -> dict[str, LibraryFile]:
    volume = volume_of(barcode)
    assert volume is not None
    return {row.rel_path: row for row in LibraryFile.query.filter_by(volume_id=volume.id)}


# ===========================================================================
# 1. 基本重建：2 盘、Unicode、深层路径、sidecar、字段完整
# ===========================================================================
def test_rebuild_two_tapes_creates_volumes_and_files(app, tmp_path):
    tape1 = tmp_path / "tape1"
    tape2 = tmp_path / "tape2"
    make_manifest(
        tape1,
        "FB1186",
        [
            {
                "rel_path": "中文/深/层/目录/SSIS-001-C.mp4",
                "size": 1234,
                "code": "SSIS-001",
                "confidence": 95,
                "flags": "chinese_sub,hi_res",
            },
            {
                "rel_path": "中文/深/层/目录/SSIS-001.srt",
                "size": 456,
                "code": "SSIS-001",
                "confidence": 95,
                "flags": None,
            },
            {
                "rel_path": "無碼/ABP-123-UC.mkv",
                "size": 999,
                "code": "ABP-123",
                "confidence": 93,
                "flags": "uncensored",
            },
        ],
    )
    make_manifest(
        tape2,
        "FB2186",
        [{"rel_path": "SSNI-777.mp4", "size": 2000, "code": "SSNI-777",
          "confidence": 96, "flags": None}],
    )

    stats = rebuild_from_manifests(app, [tape1, tape2])

    assert isinstance(stats, RebuildStats)
    assert (stats.manifests_found, stats.manifests_imported, stats.manifests_failed) == (2, 2, 0)
    assert (stats.volumes_created, stats.volumes_updated) == (2, 0)
    assert (stats.files_created, stats.files_updated, stats.files_unchanged) == (4, 0, 0)
    assert (stats.videos, stats.sidecars) == (3, 1)
    assert stats.with_code == 4
    assert stats.total_bytes == 1234 + 456 + 999 + 2000
    assert stats.per_volume == {"FB1186": 3, "FB2186": 1}
    assert stats.errors == []

    vol1 = volume_of("FB1186")
    vol2 = volume_of("FB2186")
    assert vol1 is not None and vol2 is not None
    assert vol1.kind == "tape" and vol1.ref == "FB1186"
    assert vol1.name == "磁带 FB1186"
    assert vol1.used_bytes == 1234 + 456 + 999
    assert vol1.last_scan_status == "manifest"
    assert vol1.last_scan_at is not None
    assert vol1.enabled is True

    entries = files_of("FB1186")
    assert set(entries) == {
        "中文/深/层/目录/SSIS-001-C.mp4",
        "中文/深/层/目录/SSIS-001.srt",
        "無碼/ABP-123-UC.mkv",
    }
    video = entries["中文/深/层/目录/SSIS-001-C.mp4"]
    assert video.name == "SSIS-001-C.mp4"
    assert video.size == 1234
    assert isinstance(video.mtime, datetime) and video.mtime.tzinfo is None
    assert video.code_guess == "SSIS-001"
    assert video.confidence == 95
    assert video.flags == "chinese_sub,hi_res"
    assert video.is_sidecar is False
    assert video.deleted_at is None
    assert video.seen_at is not None

    sidecar = entries["中文/深/层/目录/SSIS-001.srt"]
    assert sidecar.is_sidecar is True
    assert sidecar.flags is None

    # 之后 materialize 的候选条件必须成立（非 sidecar / 有番号 / 置信度够 / 未失效）
    candidates = LibraryFile.query.filter(
        LibraryFile.deleted_at.is_(None),
        LibraryFile.is_sidecar.is_(False),
        LibraryFile.code_guess.isnot(None),
        LibraryFile.code_guess != "",
        LibraryFile.confidence >= 90,
    ).count()
    assert candidates == 3


# ===========================================================================
# 2. 幂等
# ===========================================================================
def test_rebuild_is_idempotent(app, tmp_path):
    root = tmp_path / "tape"
    make_manifest(
        root,
        "FB1186",
        [
            {"rel_path": "A/SSIS-001.mp4", "size": 10, "code": "SSIS-001",
             "confidence": 95, "flags": None},
            {"rel_path": "A/SSIS-001.srt", "size": 5, "code": "SSIS-001",
             "confidence": 95, "flags": None},
        ],
    )

    first = rebuild_from_manifests(app, [root])
    seen_before = {rel: row.seen_at for rel, row in files_of("FB1186").items()}
    volume_id = volume_of("FB1186").id

    second = rebuild_from_manifests(app, [root])

    assert (first.files_created, first.files_updated) == (2, 0)
    assert (second.files_created, second.files_updated) == (0, 0)
    assert second.files_unchanged == 2
    assert (second.volumes_created, second.volumes_updated) == (0, 0)
    assert LibraryVolume.query.filter_by(ref="FB1186").count() == 1
    assert LibraryFile.query.count() == 2
    assert volume_of("FB1186").id == volume_id
    # 内容没变就不碰行（seen_at 也不动）
    assert {rel: row.seen_at for rel, row in files_of("FB1186").items()} == seen_before


# ===========================================================================
# 3. 变化更新 / 软删复活
# ===========================================================================
def test_rebuild_updates_changed_rows_and_revives_soft_deleted(app, tmp_path):
    root = tmp_path / "tape"
    manifest = make_manifest(
        root,
        "FB1186",
        [
            {"rel_path": "A/SSIS-001.mp4", "size": 10, "code": "SSIS-001",
             "confidence": 95, "flags": None},
            {"rel_path": "B/ABP-123.mp4", "size": 20, "code": "ABP-123",
             "confidence": 90, "flags": None},
        ],
    )
    rebuild_from_manifests(app, [root])

    # 模拟：一行被别人软删/改脏，另一行的文件在磁带上变了（size 变化后重新盘点写 manifest）
    changed = LibraryFile.query.filter_by(rel_path="A/SSIS-001.mp4").one()
    changed.code_guess = None
    changed.confidence = 0
    changed.flags = "manual"
    deleted = LibraryFile.query.filter_by(rel_path="B/ABP-123.mp4").one()
    deleted.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.commit()

    (root / "B" / "ABP-123.mp4").write_bytes(b"y" * 333)
    make_manifest(
        root,
        "FB1186",
        [
            {"rel_path": "A/SSIS-001.mp4", "size": 10, "code": "SSIS-001",
             "confidence": 95, "flags": None},
            {"rel_path": "B/ABP-123.mp4", "size": 333, "code": "ABP-123",
             "confidence": 92, "flags": "chinese_sub"},
        ],
    )
    assert manifest.is_file()

    stats = rebuild_from_manifests(app, [root])

    assert stats.files_created == 0
    assert stats.files_updated == 2
    assert stats.files_unchanged == 0
    assert LibraryFile.query.count() == 2
    entries = files_of("FB1186")
    fixed = entries["A/SSIS-001.mp4"]
    assert (fixed.code_guess, fixed.confidence, fixed.flags) == ("SSIS-001", 95, None)
    revived = entries["B/ABP-123.mp4"]
    assert revived.deleted_at is None
    assert revived.size == 333
    assert revived.confidence == 92
    assert revived.flags == "chinese_sub"


# ===========================================================================
# 4. 不软删：其它卷 / 本卷多出来的行都不动
# ===========================================================================
def test_rebuild_never_soft_deletes_other_rows(app, tmp_path):
    # 库里已有 NAS 卷 + 另一盘磁带 + 本盘一条 manifest 不会列出的旧行
    nas = LibraryVolume(kind="nas", ref="/mnt/user/av", name="NAS")
    other = LibraryVolume(kind="tape", ref="OLD999", name="旧磁带")
    db.session.add_all([nas, other])
    db.session.commit()
    db.session.add_all(
        [
            LibraryFile(volume_id=nas.id, rel_path="nas/SSIS-001.mp4", name="SSIS-001.mp4"),
            LibraryFile(volume_id=other.id, rel_path="old/SSIS-002.mp4", name="SSIS-002.mp4"),
        ]
    )
    db.session.commit()

    root = tmp_path / "tape"
    make_manifest(
        root,
        "FB1186",
        [
            {"rel_path": "A/SSIS-001.mp4", "size": 10, "code": "SSIS-001",
             "confidence": 95, "flags": None},
            {"rel_path": "A/SSIS-001.srt", "size": 5, "code": "SSIS-001",
             "confidence": 95, "flags": None},
        ],
    )
    rebuild_from_manifests(app, [root])

    # 再写一份“少了 srt”的 manifest（模拟文件被移走/只扫到部分），重建也不能删旧行
    make_manifest(
        root,
        "FB1186",
        [{"rel_path": "A/SSIS-001.mp4", "size": 10, "code": "SSIS-001",
          "confidence": 95, "flags": None}],
    )
    stats = rebuild_from_manifests(app, [root])

    assert stats.files_skipped == 0
    assert LibraryFile.query.count() == 4  # 2 原有 + 2 本卷
    assert LibraryFile.query.filter(LibraryFile.deleted_at.isnot(None)).count() == 0
    assert nas.files.count() == 1
    assert other.files.count() == 1
    assert set(files_of("FB1186")) == {"A/SSIS-001.mp4", "A/SSIS-001.srt"}


# ===========================================================================
# 5. 路径形态：挂载根 / ._manifest / 备份目录 / 显式文件 / 去重
# ===========================================================================
def test_rebuild_accepts_all_path_kinds_and_dedupes(app, tmp_path):
    mount = tmp_path / "tape1"
    manifest_a = make_manifest(
        mount,
        "FB1186",
        [{"rel_path": "a.mp4", "size": 1, "code": "SSIS-001", "confidence": 95, "flags": None}],
    )
    # 造一个旧磁带布局：manifest 在 <mount>/._manifest/ 下（兼容测试）
    legacy_dir = mount / MANIFEST_DIRNAME
    legacy_dir.mkdir()
    manifest_a = Path(shutil.move(str(manifest_a), str(legacy_dir / manifest_a.name)))
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    source_b = make_manifest(
        tmp_path / "tape2",
        "FB2186",
        [{"rel_path": "b.mp4", "size": 2, "code": "ABP-123", "confidence": 95, "flags": None}],
    )
    (backup_dir / source_b.name).write_bytes(source_b.read_bytes())

    found, errors, _explicit = discover_manifests(
        [mount, legacy_dir, backup_dir, manifest_a]
    )
    assert errors == []
    assert len(found) == 2  # 同一个 manifest 传 3 遍只算一次

    stats = rebuild_from_manifests(
        app, [mount, legacy_dir, backup_dir, manifest_a]
    )
    assert stats.manifests_found == 2
    assert stats.manifests_imported == 2
    assert set(stats.barcodes) == {"FB1186", "FB2186"}
    assert files_of("FB1186")["a.mp4"].code_guess == "SSIS-001"
    # 备份目录里的 json 通过了“像 manifest”校验
    assert files_of("FB2186")["b.mp4"].code_guess == "ABP-123"


def test_rebuild_accepts_single_path_not_wrapped_in_list(app, tmp_path):
    """容错：直接传一个 Path/str 也能用（不用手动包成 list）。"""
    root = tmp_path / "tape"
    make_manifest(
        root,
        "FB1186",
        [{"rel_path": "a.mp4", "size": 1, "code": "SSIS-001", "confidence": 95, "flags": None}],
    )

    stats = rebuild_from_manifests(app, root)

    assert stats.files_created == 1
    assert files_of("FB1186")["a.mp4"].code_guess == "SSIS-001"


def test_discover_manifests_reports_bad_paths(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    found, errors, _explicit = discover_manifests(
        [tmp_path / "does-not-exist", empty_dir]
    )

    assert found == []
    assert any("不存在" in message for message in errors)
    assert any("没有 manifest" in message for message in errors)


# ===========================================================================
# 6. 旧格式兼容
# ===========================================================================
def test_rebuild_old_format_manifest(app, tmp_path):
    mount = tmp_path / "tape"
    manifest_dir = mount / MANIFEST_DIRNAME
    manifest_dir.mkdir(parents=True)
    # 旧格式 A：没有 schema，文件条目用 path/code_guess（read_manifest 归一化）
    old_dict = {
        "barcode": "OLD01",
        "files": [
            {"path": "A/OLD-001.mp4", "size": 5, "code_guess": "OLD-001", "confidence": 88},
            {"path": "A/OLD-001.srt", "size": 2, "flags": "chinese_sub"},
        ],
    }
    (manifest_dir / "OLD01.json").write_text(json.dumps(old_dict), encoding="utf-8")

    stats = rebuild_from_manifests(app, [mount])

    assert (stats.manifests_imported, stats.files_created) == (1, 2)
    entries = files_of("OLD01")
    assert entries["A/OLD-001.mp4"].code_guess == "OLD-001"
    assert entries["A/OLD-001.mp4"].confidence == 88
    assert entries["A/OLD-001.mp4"].mtime is None
    assert entries["A/OLD-001.srt"].is_sidecar is True
    assert entries["A/OLD-001.srt"].flags == "chinese_sub"


def test_rebuild_old_format_bare_list_as_explicit_file(app, tmp_path):
    # 旧格式 B：裸 files 列表，用户显式点名的文件 → 信任并导入（条码用文件名兜底）
    legacy = tmp_path / "TAPE77.json"
    legacy.write_text(
        json.dumps([{"rel_path": "x/ABC-001.mp4", "size": 7, "code": "ABC-001",
                     "confidence": 91}]),
        encoding="utf-8",
    )

    stats = rebuild_from_manifests(app, [legacy])

    assert (stats.manifests_imported, stats.files_created) == (1, 1)
    entries = files_of("TAPE77")
    assert entries["x/ABC-001.mp4"].code_guess == "ABC-001"


# ===========================================================================
# 7. 非 manifest / 损坏 / 不可用条码
# ===========================================================================
def test_rebuild_ignores_non_manifest_json_and_corrupt_files(app, tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    # 类似 /tmp/av-archive-corpus/manifest.json 的无关 json：不能造出“manifest”卷
    (backup / "corpus.json").write_text(
        json.dumps({"seed": 42, "entries": [{"rel_path": "x.mp4"}]}), encoding="utf-8"
    )
    # 损坏 json 放在 ._manifest 里（受信任 → 尝试解析，失败记错误）
    broken_root = tmp_path / "broken"
    (broken_root / MANIFEST_DIRNAME).mkdir(parents=True)
    (broken_root / MANIFEST_DIRNAME / "BROKEN1.json").write_text("{oops", encoding="utf-8")
    # 同目录里还有一个好 manifest
    good_root = tmp_path / "good"
    make_manifest(
        good_root,
        "FB1186",
        [{"rel_path": "ok.mp4", "size": 3, "code": "SSIS-001", "confidence": 95, "flags": None}],
    )

    stats = rebuild_from_manifests(app, [backup, broken_root, good_root])

    assert stats.manifests_found == 3
    assert stats.manifests_imported == 1
    assert stats.manifests_failed == 2
    assert any("不是磁带 manifest" in message for message in stats.errors)
    assert any("无法解析" in message for message in stats.errors)
    assert volume_of("corpus") is None
    assert volume_of("BROKEN1") is None
    assert files_of("FB1186")["ok.mp4"].code_guess == "SSIS-001"


def test_rebuild_skips_manifest_with_unusable_barcode(app, tmp_path):
    root = tmp_path / "tape"
    manifest = make_manifest(
        root,
        "UNKNOWN",
        [{"rel_path": "x.mp4", "size": 1, "code": None, "confidence": 0, "flags": None}],
    )

    stats = rebuild_from_manifests(app, [manifest])

    assert stats.manifests_failed == 1
    assert stats.files_created == 0
    assert any("条码不可用" in message for message in stats.errors)
    assert LibraryVolume.query.count() == 0


# ===========================================================================
# 8. 同条码多份 manifest：最新为准 + 文件并集
# ===========================================================================
def test_rebuild_duplicate_barcode_latest_wins(app, tmp_path):
    old_root = tmp_path / "old-tape"
    new_root = tmp_path / "new-tape"
    old_manifest = make_manifest(
        old_root,
        "FB1186",
        [
            {"rel_path": "same.mp4", "size": 1, "code": "OLD-001",
             "confidence": 80, "flags": None},
            {"rel_path": "only-in-old.mp4", "size": 2, "code": "OLD-002",
             "confidence": 81, "flags": None},
        ],
    )
    rewrite_manifest(old_manifest, generated_at="2024-01-01T00:00:00")
    new_manifest = make_manifest(
        new_root,
        "FB1186",
        [
            {"rel_path": "same.mp4", "size": 9, "code": "NEW-001",
             "confidence": 99, "flags": "chinese_sub"},
            {"rel_path": "only-in-new.mp4", "size": 8, "code": "NEW-002",
             "confidence": 99, "flags": None},
        ],
    )
    rewrite_manifest(new_manifest, generated_at="2026-01-01T00:00:00")

    stats = rebuild_from_manifests(app, [old_root, new_root])

    assert stats.volumes_created == 1
    assert LibraryVolume.query.filter_by(ref="FB1186").count() == 1
    entries = files_of("FB1186")
    # 文件并集；同 rel_path 以最新 manifest 为准
    assert set(entries) == {"same.mp4", "only-in-old.mp4", "only-in-new.mp4"}
    assert entries["same.mp4"].size == 9
    assert entries["same.mp4"].code_guess == "NEW-001"
    assert entries["same.mp4"].flags == "chinese_sub"
    # 卷级元数据取最新 generated_at
    assert volume_of("FB1186").last_scan_at == datetime(2026, 1, 1, 0, 0, 0)
    assert volume_of("FB1186").used_bytes == 9 + 2 + 8


# ===========================================================================
# 9. dry-run
# ===========================================================================
def test_rebuild_dry_run_does_not_write(app, tmp_path):
    root = tmp_path / "tape"
    make_manifest(
        root,
        "FB1186",
        [
            {"rel_path": "A/SSIS-001.mp4", "size": 10, "code": "SSIS-001",
             "confidence": 95, "flags": None},
            {"rel_path": "A/SSIS-001.srt", "size": 5, "code": "SSIS-001",
             "confidence": 95, "flags": None},
        ],
    )

    dry = rebuild_from_manifests(app, [root], dry_run=True)

    assert dry.dry_run is True
    assert dry.files_created == 2
    assert dry.volumes_created == 1
    assert LibraryVolume.query.count() == 0
    assert LibraryFile.query.count() == 0

    # 再 dry-run 一次结果一致（因为库里仍然什么都没有）
    dry2 = rebuild_from_manifests(app, [root], dry_run=True)
    assert (dry2.files_created, dry2.files_updated) == (2, 0)

    # 真正写库
    real = rebuild_from_manifests(app, [root])
    assert (real.files_created, real.files_updated) == (2, 0)
    assert LibraryVolume.query.count() == 1
    assert LibraryFile.query.count() == 2


# ===========================================================================
# 10. mtime 解析 / 空盘 / 卷名 / progress / stats 序列化
# ===========================================================================
def test_rebuild_parses_mtime_variants(app, tmp_path):
    manifest_path = tmp_path / "M1.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "barcode": "M1",
                "files": [
                    {"rel_path": "naive.mp4", "size": 1, "mtime": "2024-01-02T03:04:05"},
                    {"rel_path": "zulu.mp4", "size": 1, "mtime": "2024-01-02T03:04:05Z"},
                    {"rel_path": "epoch.mp4", "size": 1, "mtime": 1700000000.5},
                    {"rel_path": "none.mp4", "size": 1, "mtime": None},
                ],
            }
        ),
        encoding="utf-8",
    )

    stats = rebuild_from_manifests(app, [manifest_path])

    assert stats.files_created == 4
    entries = files_of("M1")
    assert entries["naive.mp4"].mtime == datetime(2024, 1, 2, 3, 4, 5)
    assert entries["zulu.mp4"].mtime == datetime(2024, 1, 2, 3, 4, 5)
    assert entries["epoch.mp4"].mtime == datetime.fromtimestamp(
        1700000000.5, timezone.utc
    ).replace(tzinfo=None)
    assert entries["none.mp4"].mtime is None


def test_rebuild_empty_manifest_creates_empty_volume(app, tmp_path):
    root = tmp_path / "tape"
    root.mkdir()
    manifest = write_manifest(root, "EMPTY01", scan_stats(files_seen=0))

    stats = rebuild_from_manifests(app, [manifest])

    assert stats.volumes_created == 1
    assert stats.files_created == 0
    volume = volume_of("EMPTY01")
    assert volume is not None
    assert volume.used_bytes == 0
    assert volume.files.count() == 0


def test_rebuild_uses_manifest_extra_name(app, tmp_path):
    root = tmp_path / "tape"
    make_manifest(
        root,
        "FB1186",
        [{"rel_path": "a.mp4", "size": 1, "code": None, "confidence": 0, "flags": None}],
        name="S1-2024-冬",
    )

    rebuild_from_manifests(app, [root])

    assert volume_of("FB1186").name == "S1-2024-冬"


def test_rebuild_progress_and_stats_serializable(app, tmp_path):
    root = tmp_path / "tape"
    make_manifest(
        root,
        "FB1186",
        [{"rel_path": f"A/SSIS-{i:03d}.mp4", "size": i, "code": f"SSIS-{i:03d}",
          "confidence": 95, "flags": None} for i in range(3)],
    )
    events: list[tuple[int, str]] = []

    def progress(done: int, message: str = "") -> None:
        events.append((done, message))

    stats = rebuild_from_manifests(app, [root], progress=progress)

    assert events[0][0] == 0
    assert events[-1][0] == 3
    assert [done for done, _ in events] == sorted(done for done, _ in events)
    assert all(message for _done, message in events)
    assert stats.elapsed_s >= 0
    payload = json.dumps(stats.as_dict(), ensure_ascii=False)
    assert '"files_created": 3' in payload


# ===========================================================================
# 11. 1 万文件重建性能 + 二次幂等
# ===========================================================================
def test_rebuild_10k_files_perf_and_idempotent(app, tmp_path, monkeypatch, capsys):
    """大卷（1 万文件）重建耗时；二次导入应几乎只读、不再写行。"""
    root = tmp_path / "big-tape"
    root.mkdir()

    def fake_walk(_mount):
        return [
            {
                "rel_path": f"dir-{i // 1000:02d}/SUB/SSIS-{i:05d}.mp4",
                "size": i + 1,
                "mtime": "2026-01-01T00:00:00",
                "code": f"SSIS-{i:05d}",
                "confidence": 95,
                "flags": None,
            }
            for i in range(PERF_FILES)
        ]

    monkeypatch.setattr(audit, "_walk_entries", fake_walk)
    manifest = audit.write_manifest(root, "PERF10K", scan_stats(files_seen=PERF_FILES))
    # 让 manifest 的 code 信息来自真实 manifest 内容（write_manifest 用 extra 补）
    data = json.loads(Path(manifest).read_text(encoding="utf-8"))
    assert data["file_count"] == PERF_FILES

    started = time.time()
    first = rebuild_from_manifests(app, [Path(manifest)])
    create_elapsed = time.time() - started

    assert first.files_created == PERF_FILES
    assert first.volumes_created == 1
    assert LibraryFile.query.count() == PERF_FILES
    assert LibraryFile.query.filter_by(volume_id=volume_of("PERF10K").id).count() == PERF_FILES

    started = time.time()
    second = rebuild_from_manifests(app, [Path(manifest)])
    second_elapsed = time.time() - started

    assert second.files_created == 0
    assert second.files_updated == 0
    assert second.files_unchanged == PERF_FILES
    assert LibraryFile.query.count() == PERF_FILES

    print(
        f"\n[rebuild perf] {PERF_FILES} 文件：首次 {create_elapsed:.2f}s，"
        f"二次 {second_elapsed:.2f}s"
    )
    assert create_elapsed < PERF_CREATE_LIMIT_S
    assert second_elapsed < PERF_SECOND_RUN_LIMIT_S


# ===========================================================================
# 12. 与 Job 队列的配合：progress 回调自己 commit（jobs._progress 就是这样）
# ===========================================================================
def test_rebuild_progress_callback_that_commits(app, tmp_path, monkeypatch):
    """jobs 的 progress 回调每次都会 db.session.commit()，不能打断重建事务。"""
    from app.services.jobs import _plain

    count = 2500
    root = tmp_path / "tape"
    root.mkdir()

    def fake_walk(_mount):
        return [
            {"rel_path": f"D/SSIS-{i:05d}.mp4", "size": 1,
             "mtime": "2026-01-01T00:00:00", "code": f"SSIS-{i:05d}",
             "confidence": 95, "flags": None}
            for i in range(count)
        ]

    monkeypatch.setattr(audit, "_walk_entries", fake_walk)
    manifest = audit.write_manifest(root, "JOB01", scan_stats(files_seen=count))

    commits: list[int] = []

    def committing_progress(done: int, message: str = "") -> None:
        commits.append(done)
        db.session.commit()  # 模拟 jobs._progress

    stats = rebuild_from_manifests(app, [Path(manifest)], progress=committing_progress)

    assert stats.files_created == count
    assert LibraryFile.query.count() == count
    assert commits and commits[-1] == count
    # Job 结果落库前会走 jobs._plain（dataclass → dict）+ json.dumps
    stored = json.dumps(_plain(stats), ensure_ascii=False)
    assert '"files_created": 2500' in stored


def test_rebuild_from_db_manifests(app, tmp_path):
    """文件副本丢失时，用数据库里的 TapeManifest 也能重建。"""
    from app.services.rebuild import rebuild_from_db

    with app.app_context():
        write_manifest(
            app,
            "FB1186",
            scan_stats(files_seen=2),
            {
                "files": [
                    {"rel_path": "a.mp4", "size": 10, "code": "SSIS-001",
                     "confidence": 95, "flags": None},
                    {"rel_path": "b.srt", "size": 2, "code": "SSIS-001",
                     "confidence": 95, "flags": "chinese_sub"},
                ]
            },
        )
        stats = rebuild_from_db(app)
        assert stats.manifests_imported == 1
        assert stats.files_created == 2
        assert stats.total_bytes == 12
        assert files_of("FB1186")["a.mp4"].code_guess == "SSIS-001"
