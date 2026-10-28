"""容器 ↔ 磁带 传输服务测试（``app/services/transfer.py``）。

全部本地、零网络/零真实设备：用 ``tmp_path`` 造 container / tape 两个根，
``monkeypatch`` 掉 ``_tape_is_mounted`` 模拟磁带已挂载。

覆盖：
- 正常：文件 / 目录递归（含空目录、中文名）、双向、覆盖同名、分块复制、进度回调；
- move：成功才删源（文件/目录），失败时一个源都不删、已完成的复制保留；
- 原子写：``.part`` 失败清理、rename 失败不留正式文件；
- 安全（重点）：``..`` 逃逸、源/目标软链逃逸、目标树预埋软链目录、源=目标、
  目标在源目录内部、跨方向、源同名/包含、未挂载、相对路径、源/目标不存在；
- ``Transfer`` 行状态/计数/file_list；``_tape_is_mounted`` 的真实实现判定。

运行（项目根 = av-archive/）::

    .venv/bin/python -m pytest tests/test_transfer.py -q
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 直接 `pytest tests/...` 时也能 import app
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.db import db  # noqa: E402
from app.models import Transfer  # noqa: E402
from app.services import transfer as transfer_mod  # noqa: E402
from app.services.transfer import (  # noqa: E402
    TransferError,
    _assert_no_self_copy,
    _tape_is_mounted,
    run_transfer,
)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture()
def env(tmp_path):
    """独立 app + 独立 SQLite；container 与 tape 两个互不重叠的根目录。"""
    data_dir = tmp_path / "data"
    container = tmp_path / "container"
    tape = tmp_path / "tape"
    container.mkdir()
    tape.mkdir()
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
        "BROWSE_ROOTS": [str(container)],
        "LTFS_MOUNT": str(tape),
        "TRANSFER_CHUNK": 8,
    }
    application = create_app(overrides)
    with application.app_context():
        yield SimpleNamespace(app=application, container=container, tape=tape, tmp=tmp_path)
        db.session.remove()
        db.engine.dispose()


@pytest.fixture()
def mounted(monkeypatch):
    """默认模拟磁带已挂载；需要未挂载的用例自己再 monkeypatch 成 False。"""
    monkeypatch.setattr(transfer_mod, "_tape_is_mounted", lambda app: True)
    return True


def write(path: Path, data: bytes = b"") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def make_dir(path: Path) -> Path:
    Path(path).mkdir(parents=True, exist_ok=True)
    return Path(path)


def make_symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):  # pragma: no cover - 个别系统不支持
        pytest.skip("系统不支持符号链接")


def target_dir(env) -> Path:
    return make_dir(env.tape / "target")


def row_of(transfer_id: int) -> Transfer:
    row = db.session.get(Transfer, transfer_id)
    assert row is not None
    return row


def assert_no_part_files(root: Path) -> None:
    leftovers = sorted(str(p) for p in root.rglob("*.part"))
    assert leftovers == [], f"残留 .part 临时文件: {leftovers}"


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------
def test_copy_file_container_to_tape(env, mounted):
    src = write(env.container / "a.mp4", b"hello" * 100)
    target = target_dir(env)
    calls: list[tuple[int, str]] = []

    result = run_transfer(
        env.app,
        "container_to_tape",
        [str(src)],
        str(target),
        progress=lambda done, current: calls.append((done, current)),
    )

    assert result["ok"] is True, result
    assert result["status"] == "done"
    assert result["direction"] == "container_to_tape"
    assert result["move"] is False
    assert result["file_count"] == 1
    assert result["total_bytes"] == len(b"hello" * 100)
    assert result["done_bytes"] == result["total_bytes"]
    assert result["error"] is None
    assert isinstance(result["elapsed_s"], float)
    assert set(result) == {
        "ok", "transfer_id", "status", "direction", "move", "target_dir",
        "file_count", "total_bytes", "done_bytes", "files", "repointed",
        "error", "elapsed_s",
    }

    dst = target / "a.mp4"
    assert dst.read_bytes() == b"hello" * 100
    assert src.exists(), "copy 不能删源"
    assert_no_part_files(env.tape)
    assert calls and calls[-1] == (result["total_bytes"], "")
    dones = [done for done, _ in calls]
    assert dones == sorted(dones)

    row = row_of(result["transfer_id"])
    assert row.status == "done"
    assert row.direction == "container_to_tape"
    assert row.move is False
    assert (row.file_count, row.total_bytes, row.done_bytes) == (1, len(b"hello" * 100), len(b"hello" * 100))
    assert row.current_file is None
    assert row.error is None
    assert row.started_at is not None and row.finished_at is not None
    assert row.target_dir == str(target.resolve())

    entries = json.loads(row.file_list)
    assert entries == [
        {
            "src": str(src.resolve()),
            "dst": str(dst.resolve()),
            "rel": "a.mp4",
            "size": len(b"hello" * 100),
        }
    ]
    assert result["files"] == entries


def test_copy_directory_preserves_structure_and_empty_dirs(env, mounted):
    src_dir = make_dir(env.container / "movies")
    write(src_dir / "a.mp4", b"A" * 10)
    write(src_dir / "sub" / "b.srt", b"B" * 3)
    write(src_dir / "sub" / "deeper" / "c.nfo", b"")
    make_dir(src_dir / "empty" / "nested_empty")
    target = target_dir(env)

    result = run_transfer(env.app, "container_to_tape", [str(src_dir)], str(target))

    assert result["ok"] is True, result
    assert result["file_count"] == 3
    assert result["total_bytes"] == 13
    dest = target / "movies"
    assert (dest / "a.mp4").read_bytes() == b"A" * 10
    assert (dest / "sub" / "b.srt").read_bytes() == b"B" * 3
    assert (dest / "sub" / "deeper" / "c.nfo").is_file()
    assert (dest / "empty" / "nested_empty").is_dir(), "空目录也要保留"
    assert_no_part_files(env.tape)
    row = row_of(result["transfer_id"])
    assert row.file_count == 3
    assert len(json.loads(row.file_list)) == 3


def test_copy_tape_to_container(env, mounted):
    src = write(env.tape / "LTO6-A001" / "a.mp4", b"from-tape")
    target = make_dir(env.container / "inbox")

    result = run_transfer(env.app, "tape_to_container", [str(src)], str(target))

    assert result["ok"] is True, result
    assert result["direction"] == "tape_to_container"
    assert (target / "a.mp4").read_bytes() == b"from-tape"
    assert row_of(result["transfer_id"]).direction == "tape_to_container"


def test_unicode_names_roundtrip(env, mounted):
    src_dir = make_dir(env.container / "中文 目录")
    write(src_dir / "字幕 文件.srt", "中文字幕".encode("utf-8"))
    target = target_dir(env)

    result = run_transfer(env.app, "container_to_tape", [str(src_dir)], str(target))

    assert result["ok"] is True, result
    assert (target / "中文 目录" / "字幕 文件.srt").read_text(encoding="utf-8") == "中文字幕"


def test_overwrite_existing_target_file(env, mounted):
    src = write(env.container / "same.mp4", b"new-content")
    target = target_dir(env)
    write(target / "same.mp4", b"old-content")

    result = run_transfer(env.app, "container_to_tape", [str(src)], str(target))

    assert result["ok"] is True, result
    assert (target / "same.mp4").read_bytes() == b"new-content"
    assert_no_part_files(env.tape)


def test_empty_directory_source(env, mounted):
    src_dir = make_dir(env.container / "emptydir")
    target = target_dir(env)

    result = run_transfer(env.app, "container_to_tape", [str(src_dir)], str(target))

    assert result["ok"] is True, result
    assert result["file_count"] == 0
    assert result["total_bytes"] == 0
    assert (target / "emptydir").is_dir()
    assert row_of(result["transfer_id"]).status == "done"


def test_sources_accept_str_and_path_and_dedupe(env, mounted):
    src = write(env.container / "a.mp4", b"x")
    target = target_dir(env)

    first = run_transfer(env.app, "container_to_tape", str(src), str(target))
    assert first["ok"] is True and first["file_count"] == 1

    second = run_transfer(
        env.app,
        "container_to_tape",
        [str(src), str(src), Path(src)],  # 同一路径给三次只传一次
        str(target),
    )
    assert second["ok"] is True, second
    assert second["file_count"] == 1


# ---------------------------------------------------------------------------
# move 语义
# ---------------------------------------------------------------------------
def test_move_file_deletes_source_after_success(env, mounted):
    src = write(env.container / "f.mp4", b"data")
    target = target_dir(env)

    result = run_transfer(env.app, "container_to_tape", [str(src)], str(target), move=True)

    assert result["ok"] is True, result
    assert not src.exists(), "move 成功后源文件要删掉"
    assert (target / "f.mp4").read_bytes() == b"data"
    row = row_of(result["transfer_id"])
    assert row.move is True and row.status == "done"


def test_move_directory_deletes_source_tree(env, mounted):
    src_dir = make_dir(env.container / "batch")
    write(src_dir / "a.mp4", b"a")
    write(src_dir / "sub" / "b.srt", b"b")
    target = target_dir(env)

    result = run_transfer(env.app, "container_to_tape", [str(src_dir)], str(target), move=True)

    assert result["ok"] is True, result
    assert not src_dir.exists(), "move 成功后源目录树要删掉"
    assert (target / "batch" / "a.mp4").read_bytes() == b"a"
    assert (target / "batch" / "sub" / "b.srt").read_bytes() == b"b"


def test_move_symlink_file_deletes_link_not_target(env, mounted):
    """源本身是软链时：move 只删链接，绝不顺着软链删到真实文件。"""
    real = write(env.container / "real.txt", b"real-data")
    link = env.container / "movie.txt"
    make_symlink(link, real)
    target = target_dir(env)

    result = run_transfer(env.app, "container_to_tape", [str(link)], str(target), move=True)

    assert result["ok"] is True, result
    assert not link.exists() and not link.is_symlink(), "软链本身应被删除"
    assert real.read_bytes() == b"real-data", "软链目标绝不能被删"
    # 目标用用户看到的名字（链接名），而不是解析后的真实文件名
    assert (target / "movie.txt").read_bytes() == b"real-data"
    assert not (target / "real.txt").exists()


def test_move_symlink_dir_deletes_link_not_target(env, mounted):
    real_dir = make_dir(env.container / "real_dir")
    write(real_dir / "inside.mp4", b"inside")
    link_dir = env.container / "linked_dir"
    make_symlink(link_dir, real_dir)
    target = target_dir(env)

    result = run_transfer(env.app, "container_to_tape", [str(link_dir)], str(target), move=True)

    assert result["ok"] is True, result
    assert not link_dir.is_symlink(), "目录软链本身应被删除"
    assert real_dir.is_dir(), "软链指向的真实目录绝不能被 rmtree"
    assert (real_dir / "inside.mp4").read_bytes() == b"inside"
    assert (target / "linked_dir" / "inside.mp4").read_bytes() == b"inside"


def test_failed_transfer_keeps_all_sources_and_completed_files(env, mounted, monkeypatch):
    """整单失败：已复制完成的文件保留，但 move 一个源都不删（比 v1 更保守）。"""
    src_dir = make_dir(env.container / "batch")
    write(src_dir / "a.mp4", b"a" * 5)
    write(src_dir / "bad.mp4", b"b" * 7)
    write(src_dir / "z.mp4", b"z" * 9)
    target = target_dir(env)

    real_copy = transfer_mod._copy_file

    def flaky_copy(src, dst, chunk_size, on_chunk=None):
        if src.endswith("bad.mp4"):
            raise OSError("模拟磁盘满")
        return real_copy(src, dst, chunk_size, on_chunk)

    monkeypatch.setattr(transfer_mod, "_copy_file", flaky_copy)

    result = run_transfer(env.app, "container_to_tape", [str(src_dir)], str(target), move=True)

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "bad.mp4" in (result["error"] or "")
    row = row_of(result["transfer_id"])
    assert row.status == "failed"
    assert row.error and "bad.mp4" in row.error
    assert row.done_bytes == 5, "只把成功完成的 a.mp4 记进度"
    assert len(json.loads(row.file_list)) == 3, "失败也要留计划列表给 UI"

    # move=True 但整单失败 → 源全部保留
    assert src_dir.is_dir()
    for name in ("a.mp4", "bad.mp4", "z.mp4"):
        assert (src_dir / name).is_file(), f"失败时不能删源: {name}"
    # 已完成的 a.mp4 保留在目标
    assert (target / "batch" / "a.mp4").read_bytes() == b"a" * 5
    assert not (target / "batch" / "bad.mp4").exists()
    assert_no_part_files(env.tape)


def test_rename_failure_cleans_part_file(env, mounted, monkeypatch):
    src = write(env.container / "a.mp4", b"x" * 50)
    target = target_dir(env)

    def boom(src_path, dst_path):
        raise OSError("模拟 rename 失败")

    monkeypatch.setattr(transfer_mod.os, "replace", boom)

    result = run_transfer(env.app, "container_to_tape", [str(src)], str(target), move=True)

    assert result["ok"] is False
    assert "复制失败" in (result["error"] or "")
    assert_no_part_files(env.tape), "失败必须清掉 .part"
    assert not (target / "a.mp4").exists(), "不能留下半截的正式文件"
    assert src.exists(), "move 失败不能删源"


def test_source_delete_failure_marks_failed_but_copies_kept(env, mounted):
    src_dir = make_dir(env.container / "locked")
    src = write(src_dir / "a.mp4", b"keep-me")
    target = target_dir(env)

    os.chmod(src_dir, 0o555)  # 目录不可写 → unlink 失败
    try:
        result = run_transfer(env.app, "container_to_tape", [str(src)], str(target), move=True)
    finally:
        os.chmod(src_dir, 0o755)

    assert result["ok"] is False
    assert "删除源失败" in (result["error"] or "")
    assert src.exists(), "删源失败时源必须还在（数据不丢）"
    assert (target / "a.mp4").read_bytes() == b"keep-me"
    assert row_of(result["transfer_id"]).status == "failed"


# ---------------------------------------------------------------------------
# 分块 / 进度 / 原子性
# ---------------------------------------------------------------------------
def test_chunked_copy_and_progress_callback(env, mounted):
    env.app.config["TRANSFER_CHUNK"] = 7
    payload = bytes(range(256)) * 900  # 230400 字节，会被切很多块
    src = write(env.container / "big.bin", payload)
    target = target_dir(env)
    calls: list[tuple[int, str]] = []

    result = run_transfer(
        env.app,
        "container_to_tape",
        [str(src)],
        str(target),
        progress=lambda done, current: calls.append((done, current)),
    )

    assert result["ok"] is True, result
    assert (target / "big.bin").read_bytes() == payload
    assert not list(env.tape.rglob("*.part"))
    assert len(calls) > 10, f"8MB 默认值之外，7 字节分块应产生很多次进度回调: {len(calls)}"
    dones = [done for done, _ in calls]
    assert dones == sorted(dones), "progress 的 done_bytes 必须单调不减"
    assert dones[-1] == len(payload)
    assert all(isinstance(current, str) for _, current in calls)
    assert any("big.bin" in current for _, current in calls)


def test_part_file_is_never_visible_as_final_name(env, mounted, monkeypatch):
    """rename 之前磁盘上只有 .part；rename 之后 .part 消失、正式名出现。"""
    src = write(env.container / "a.mp4", b"z" * 33)
    target = target_dir(env)
    snapshot_before_replace: list[list[str]] = []

    real_replace = transfer_mod.os.replace

    def observing_replace(src_path, dst_path):
        # rename 的瞬间：同目录下应该只有 a.mp4.part，没有 a.mp4
        snapshot_before_replace.append(sorted(p.name for p in Path(dst_path).parent.iterdir()))
        return real_replace(src_path, dst_path)

    monkeypatch.setattr(transfer_mod.os, "replace", observing_replace)

    result = run_transfer(env.app, "container_to_tape", [str(src)], str(target))

    assert result["ok"] is True, result
    assert snapshot_before_replace == [["a.mp4.part"]], snapshot_before_replace
    assert sorted(p.name for p in target.iterdir()) == ["a.mp4"]
    assert (target / "a.mp4").read_bytes() == b"z" * 33
    assert_no_part_files(env.tape)


# ---------------------------------------------------------------------------
# 安全：未挂载 / 方向 / 参数
# ---------------------------------------------------------------------------
def test_reject_unmounted(env, monkeypatch):
    src = write(env.container / "a.mp4", b"x")
    target = target_dir(env)
    monkeypatch.setattr(transfer_mod, "_tape_is_mounted", lambda app: False)

    for direction in ("container_to_tape", "tape_to_container"):
        result = run_transfer(env.app, direction, [str(src)], str(target))
        assert result["ok"] is False
        assert "未挂载" in (result["error"] or "")
        row = row_of(result["transfer_id"])
        assert row.status == "failed"
        assert row.finished_at is not None


def test_reject_invalid_direction(env, mounted):
    src = write(env.container / "a.mp4", b"x")
    result = run_transfer(env.app, "sideways", [str(src)], str(target_dir(env)))
    assert result["ok"] is False
    assert "direction" in (result["error"] or "")
    assert row_of(result["transfer_id"]).status == "failed"


def test_reject_relative_paths(env, mounted):
    target = target_dir(env)
    write(env.container / "a.mp4", b"x")
    result = run_transfer(env.app, "container_to_tape", ["a.mp4"], str(target))
    assert result["ok"] is False and "绝对路径" in (result["error"] or "")

    result = run_transfer(env.app, "container_to_tape", [str(env.container / "a.mp4")], "target")
    assert result["ok"] is False and "绝对路径" in (result["error"] or "")


def test_reject_empty_sources(env, mounted):
    result = run_transfer(env.app, "container_to_tape", [], str(target_dir(env)))
    assert result["ok"] is False
    assert "没有指定" in (result["error"] or "")


def test_reject_missing_source(env, mounted):
    result = run_transfer(
        env.app, "container_to_tape", [str(env.container / "nope.mp4")], str(target_dir(env))
    )
    assert result["ok"] is False
    assert "源不存在" in (result["error"] or "")
    row = row_of(result["transfer_id"])
    assert row.status == "failed" and row.error


def test_missing_target_is_created_by_default(env, mounted):
    """实测反馈：目标目录不存在（如 /media/tape/imported）应自动创建再传。"""
    src = write(env.container / "a.mp4", b"abc" * 7)

    # container_to_tape：目标在磁带挂载点内、尚不存在
    tape_target = env.tape / "imported"
    assert not tape_target.exists()
    result = run_transfer(env.app, "container_to_tape", [str(src)], str(tape_target))
    assert result["ok"] is True, result
    assert tape_target.is_dir()
    assert (tape_target / "a.mp4").read_bytes() == b"abc" * 7
    assert row_of(result["transfer_id"]).target_dir == str(tape_target.resolve())

    # tape_to_container：反向目标不存在同样自动创建
    tape_file = write(env.tape / "fromtape.mp4", b"t" * 5)
    container_target = env.container / "incoming" / "nested"
    assert not container_target.exists()
    result = run_transfer(
        env.app, "tape_to_container", [str(tape_file)], str(container_target)
    )
    assert result["ok"] is True, result
    assert (container_target / "fromtape.mp4").read_bytes() == b"t" * 5


def test_missing_target_rejected_when_create_target_false(env, mounted):
    src = write(env.container / "a.mp4", b"x")
    target = env.tape / "nope"
    result = run_transfer(
        env.app, "container_to_tape", [str(src)], str(target), create_target=False
    )
    assert result["ok"] is False
    assert "目标目录不存在" in (result["error"] or "")
    assert not target.exists(), "create_target=False 时不能建目录"


def test_missing_target_not_created_when_source_invalid(env, mounted):
    """源校验失败时不应留下一个空的目标目录（先验证、后 mkdir）。"""
    target = env.tape / "never-created"
    result = run_transfer(
        env.app,
        "container_to_tape",
        [str(env.container / "missing.mp4")],
        str(target),
    )
    assert result["ok"] is False
    assert "源不存在" in (result["error"] or "")
    assert not target.exists()


def test_out_of_scope_target_is_not_created(env, mounted):
    """越界目标必须在 mkdir 之前就拒绝（不能给任意建目录的能力）。"""
    src = write(env.container / "a.mp4", b"x")
    outside = env.tmp / "outside-not-created"

    result = run_transfer(env.app, "container_to_tape", [str(src)], str(outside))
    assert result["ok"] is False
    assert "磁带挂载点内" in (result["error"] or "")
    assert not outside.exists(), "越界目标不能留下目录"

    # 用 .. 逃出磁带挂载点、且目标不存在
    sneaky = env.tape / ".." / "escaped-not-created"
    result = run_transfer(env.app, "container_to_tape", [str(src)], str(sneaky))
    assert result["ok"] is False
    assert not (env.tmp / "escaped-not-created").exists()

    # 反向：目标在 tape 里（容器侧越界），也不能创建
    tape_file = write(env.tape / "b.mp4", b"b")
    tape_target = env.tape / "should-not-exist"
    result = run_transfer(
        env.app, "tape_to_container", [str(tape_file)], str(tape_target)
    )
    assert result["ok"] is False
    assert not tape_target.exists()


def test_target_exists_but_is_a_file(env, mounted):
    src = write(env.container / "a.mp4", b"x")
    blocker = write(env.tape / "blocker", b"i-am-a-file")
    for create_target in (True, False):
        result = run_transfer(
            env.app,
            "container_to_tape",
            [str(src)],
            str(blocker),
            create_target=create_target,
        )
        assert result["ok"] is False
        assert "不是目录" in (result["error"] or "")
    assert blocker.read_bytes() == b"i-am-a-file", "不能把已有文件当目录用"


# ---------------------------------------------------------------------------
# 安全：路径逃逸
# ---------------------------------------------------------------------------
def test_reject_dotdot_source_escape(env, mounted):
    outside = write(env.tmp / "outside.mp4", b"secret")
    target = target_dir(env)
    # realpath 消掉 .. 后落在 BROWSE_ROOTS 之外
    sneaky = str(env.container / ".." / "outside.mp4")

    result = run_transfer(env.app, "container_to_tape", [sneaky], str(target))
    assert result["ok"] is False
    assert "BROWSE_ROOTS" in (result["error"] or "")
    assert not (target / "outside.mp4").exists()
    assert outside.exists()


def test_reject_dotdot_target_escape(env, mounted):
    src = write(env.container / "a.mp4", b"x")
    sneaky_target = str(env.tape / ".." / "container")

    result = run_transfer(env.app, "container_to_tape", [str(src)], sneaky_target)
    assert result["ok"] is False
    assert "磁带挂载点内" in (result["error"] or "")


def test_reject_symlink_source_escape(env, mounted):
    outside = write(env.tmp / "outside.mp4", b"secret")
    link = env.container / "evil.mp4"
    make_symlink(link, outside)
    target = target_dir(env)

    result = run_transfer(env.app, "container_to_tape", [str(link)], str(target))

    assert result["ok"] is False
    assert "BROWSE_ROOTS" in (result["error"] or "")
    assert not (target / "evil.mp4").exists()


def test_reject_symlink_inside_tree_escape(env, mounted):
    outside = write(env.tmp / "outside.mp4", b"secret")
    src_dir = make_dir(env.container / "dir")
    write(src_dir / "a.mp4", b"ok")
    make_symlink(src_dir / "leak.mp4", outside)
    target = target_dir(env)

    result = run_transfer(env.app, "container_to_tape", [str(src_dir)], str(target))

    assert result["ok"] is False
    assert "符号链接逃逸" in (result["error"] or "")
    assert not (target / "dir").exists(), "计划阶段就该拒绝，不能先复制一半"


def test_symlink_file_inside_root_is_copied(env, mounted):
    real_file = write(env.container / "real.txt", b"inside-root")
    src_dir = make_dir(env.container / "dir")
    write(src_dir / "a.mp4", b"a")
    make_symlink(src_dir / "link.txt", real_file)
    target = target_dir(env)

    result = run_transfer(env.app, "container_to_tape", [str(src_dir)], str(target))

    assert result["ok"] is True, result
    # 软链内容被复制成普通文件（仍在允许根内，不算逃逸）
    assert (target / "dir" / "link.txt").read_bytes() == b"inside-root"


def test_directory_symlink_is_not_followed(env, mounted):
    other = make_dir(env.container / "other")
    write(other / "secret.mp4", b"other")
    src_dir = make_dir(env.container / "dir")
    write(src_dir / "a.mp4", b"a")
    make_symlink(src_dir / "linked", env.container / "other")
    target = target_dir(env)

    result = run_transfer(env.app, "container_to_tape", [str(src_dir)], str(target))

    assert result["ok"] is True, result
    assert result["file_count"] == 1
    assert (target / "dir" / "a.mp4").read_bytes() == b"a"
    assert not (target / "dir" / "linked").exists(), "目录软链不跟随、也不在目标建目录"


def test_reject_symlink_target_dir_outside_mount(env, mounted):
    src = write(env.container / "a.mp4", b"x")
    sneaky = env.tape / "sneaky"
    make_symlink(sneaky, env.container)  # 指向磁带挂载点之外

    result = run_transfer(env.app, "container_to_tape", [str(src)], str(sneaky))

    assert result["ok"] is False
    assert "磁带挂载点内" in (result["error"] or "")


def test_reject_symlink_dest_parent_escape(env, mounted):
    """目标树里预埋的软链目录不能把写入引到目标目录之外。"""
    src_dir = make_dir(env.container / "movies")
    write(src_dir / "sub" / "a.mp4", b"x")
    target = target_dir(env)
    outside = make_dir(env.container / "outside_dir")
    make_dir(target / "movies")
    make_symlink(target / "movies" / "sub", outside)

    result = run_transfer(env.app, "container_to_tape", [str(src_dir)], str(target))

    assert result["ok"] is False
    assert "逃逸" in (result["error"] or "")
    assert not (outside / "a.mp4").exists(), "不能写到目标目录之外"


# ---------------------------------------------------------------------------
# 安全：自我复制 / 方向混乱 / 源冲突
# ---------------------------------------------------------------------------
def test_reject_target_inside_source(env, mounted):
    """目标在源目录内部 = 边写边读自己，必须拒绝。"""
    work = make_dir(env.container / "work")
    write(work / "a.mp4", b"a")
    mount = make_dir(work / "tape")  # 把磁带挂载点放到源目录里
    env.app.config["LTFS_MOUNT"] = str(mount)

    result = run_transfer(env.app, "container_to_tape", [str(work)], str(mount))

    assert result["ok"] is False
    assert "目标在源目录内部" in (result["error"] or "")


def test_no_self_copy_helper_unit():
    """源=目标 / 目标在源内部 的纯校验函数（公开 API 下两侧根不相交，单独测）。"""
    with pytest.raises(TransferError, match="源=目标"):
        _assert_no_self_copy(
            src_real="/data/a.mp4", dst_top="/data/a.mp4", is_dir=False, target_real="/data"
        )
    with pytest.raises(TransferError, match="目标在源目录内部"):
        _assert_no_self_copy(
            src_real="/data/dir",
            dst_top="/data/dir/sub/dir",
            is_dir=True,
            target_real="/data/dir/sub",
        )
    # 正常情况不抛
    _assert_no_self_copy(
        src_real="/data/a.mp4", dst_top="/tape/a.mp4", is_dir=False, target_real="/tape"
    )


def test_reject_cross_direction(env, mounted):
    env.app.config["BROWSE_ROOTS"] = [str(env.tmp)]  # 容器根覆盖了 tape 目录
    tape_file = write(env.tape / "on-tape.mp4", b"x")
    target = target_dir(env)

    result = run_transfer(env.app, "container_to_tape", [str(tape_file)], str(target))
    assert result["ok"] is False
    assert "跨方向" in (result["error"] or "")

    result = run_transfer(
        env.app, "tape_to_container", [str(tape_file)], str(make_dir(env.tape / "sub"))
    )
    assert result["ok"] is False
    assert "跨方向" in (result["error"] or "")


def test_reject_target_on_wrong_side(env, mounted):
    src = write(env.container / "a.mp4", b"x")
    container_target = make_dir(env.container / "out")
    result = run_transfer(env.app, "container_to_tape", [str(src)], str(container_target))
    assert result["ok"] is False
    assert "磁带挂载点内" in (result["error"] or "")

    tape_file = write(env.tape / "b.mp4", b"b")
    tape_target = target_dir(env)
    result = run_transfer(env.app, "tape_to_container", [str(tape_file)], str(tape_target))
    assert result["ok"] is False
    assert "BROWSE_ROOTS" in (result["error"] or "")


def test_reject_duplicate_top_level_names(env, mounted):
    make_dir(env.container / "a" / "movies")
    make_dir(env.container / "b" / "movies")
    write(env.container / "a" / "movies" / "x.mp4", b"x")
    write(env.container / "b" / "movies" / "y.mp4", b"y")
    target = target_dir(env)

    result = run_transfer(
        env.app,
        "container_to_tape",
        [str(env.container / "a" / "movies"), str(env.container / "b" / "movies")],
        str(target),
    )

    assert result["ok"] is False
    assert "同名" in (result["error"] or "")


def test_reject_nested_sources(env, mounted):
    src_dir = make_dir(env.container / "dir")
    write(src_dir / "sub" / "a.mp4", b"a")
    target = target_dir(env)

    result = run_transfer(
        env.app,
        "container_to_tape",
        [str(src_dir), str(src_dir / "sub" / "a.mp4")],
        str(target),
    )

    assert result["ok"] is False
    assert "包含关系" in (result["error"] or "")


# ---------------------------------------------------------------------------
# 挂载检测（真实实现，不 monkeypatch）
# ---------------------------------------------------------------------------
def test_tape_is_mounted_detection(env, monkeypatch):
    monkeypatch.setattr(transfer_mod.os.path, "ismount", lambda path: False)
    env.app.config["LTFS_MOUNT"] = str(env.tape)

    assert _tape_is_mounted(env.app) is False  # 空目录，没有 LTFS 标记

    write(env.tape / ".volume_label", b"LTO6-A001")
    assert _tape_is_mounted(env.app) is True, "有 .volume_label 视为已挂载"

    (env.tape / ".volume_label").unlink()
    monkeypatch.setattr(
        transfer_mod.os.path, "ismount", lambda path: str(path).rstrip(os.sep) == str(env.tape)
    )
    assert _tape_is_mounted(env.app) is True, "os.path.ismount 为真也算挂载"

    env.app.config["LTFS_MOUNT"] = str(env.tmp / "missing")
    assert _tape_is_mounted(env.app) is False

    env.app.config["LTFS_MOUNT"] = ""
    assert _tape_is_mounted(env.app) is False


def test_move_nas_to_tape_repoints_library_row(env, mounted):
    """移动完成后，片库里 NAS 位置的行要改指到磁带位置。"""
    from app.models import LibraryFile, LibraryVolume

    app, container, tape = env.app, env.container, env.tape
    (tape / ".volume_label").write_text("FB1186\n", encoding="utf-8")
    src = write(container / "ALDN" / "ALDN-336.mkv", b"x" * 100)
    with app.app_context():
        nas = LibraryVolume(kind="nas", ref=str(container), name="NAS")
        tp = LibraryVolume(kind="tape", ref="FB1186", name="磁带 FB1186")
        db.session.add_all([nas, tp])
        db.session.flush()
        db.session.add(
            LibraryFile(
                volume_id=nas.id,
                rel_path="ALDN/ALDN-336.mkv",
                name="ALDN-336.mkv",
                size=100,
                code_guess="ALDN-336",
                confidence=92,
            )
        )
        db.session.commit()
        nas_id, tape_id = nas.id, tp.id

    result = run_transfer(
        app, "container_to_tape", [str(src)], str(tape / "ALDN"), move=True
    )

    assert result["ok"] is True, result
    assert result["repointed"] == 1
    assert not src.exists()  # 源已删除
    with app.app_context():
        rows = LibraryFile.query.all()
        assert len(rows) == 1
        assert rows[0].volume_id == tape_id
        assert rows[0].rel_path == "ALDN/ALDN-336.mkv"
        assert rows[0].code_guess == "ALDN-336"


def test_container_roots_include_media_sources(env):
    """「介质」页登记的媒体库路径也要允许浏览/传输。"""
    from app.models import MediaSource
    from app.services.transfer import container_roots

    extra = env.tmp / "nas-library"
    extra.mkdir()
    with env.app.app_context():
        db.session.add(MediaSource(name="NAS 片库", kind="mount", path=str(extra)))
        db.session.commit()
        roots = container_roots(env.app)
    assert os.path.realpath(str(extra)) in roots
