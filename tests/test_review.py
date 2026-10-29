"""人工修正 / 待确认工作流的测试（临时 avlib.db，不碰 data/avlib.db）。

覆盖：

1. 待确认分组：低置信度、高置信度自动未命中（FC2）、无番号、多卷；排除
   sidecar / 已关联 / 已删除文件；排序与 limit；
2. 置信度窗口参数：``max_confidence`` 只收窄低置信度子集，自动未命中项始终保留；
3. ``assign_code``：CodeAlias upsert、批量改 code/conf=95（含 sidecar）、立即物化、
   幂等；file_ids 分支；目标 Title 已存在时 title_id 正确；参数校验；
4. ``reassign_files``：挂到已有 Title + 学习 alias、幂等；目标 Title 不存在时调用
   materialize 并给出错误提示；
5. 路由：``GET /review``、``GET /api/review/pending``、``POST /api/review/assign``、
   ``POST /api/review/reassign``（同步返回，materialize 失败不 500）。

跑法（项目根 = av-archive/）::

    .venv/bin/python -m pytest tests/test_review.py -q
"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.db import db  # noqa: E402
from app.models import CodeAlias, LibraryFile, LibraryVolume, Title, utcnow  # noqa: E402
from app.services import review as review_service  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures / seed
# ---------------------------------------------------------------------------
@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "DATA_DIR": tmp_path,
            "DB_PATH": tmp_path / "avlib.db",
            "MEDIA_DIR": tmp_path / "media",
            "COVER_DIR": tmp_path / "media" / "covers",
            "STILL_DIR": tmp_path / "media" / "stills",
            "AVATAR_DIR": tmp_path / "media" / "avatars",
            "SUBTITLE_DIR": tmp_path / "media" / "subtitles",
            "THUMB_DIR": tmp_path / "media" / "thumbs",
            "DUMP_DIR": tmp_path / "dump",
            "R18_DUMP_DB": tmp_path / "no-such-dump.db",
            "WORKER_ENABLED": False,
        }
    )
    # Lead 负责注册；测试里保证蓝图可用（重复注册会报错，先判断）
    from app.routes.review import bp as review_bp

    if "review" not in application.blueprints:
        application.register_blueprint(review_bp)
    yield application
    with application.app_context():
        db.session.remove()


@pytest.fixture()
def client(app):
    return app.test_client()


def _volume(kind: str = "nas", ref: str = "/mnt/nas") -> LibraryVolume:
    volume = LibraryVolume(kind=kind, ref=ref, name=(ref if kind == "nas" else None))
    db.session.add(volume)
    db.session.flush()
    return volume


_file_seq = itertools.count(1)


def _file(volume, code, *, confidence=90, is_sidecar=False, title_id=None,
          deleted=False):
    suffix = ".srt" if is_sidecar else ".mp4"
    seq = next(_file_seq)
    name = f"{code or 'unknown'}-{seq}{suffix}"
    library_file = LibraryFile(
        volume_id=volume.id,
        rel_path=f"{code or 'unknown'}/{name}",
        name=name,
        size=1024,
        code_guess=code,
        confidence=confidence,
        is_sidecar=is_sidecar,
        title_id=title_id,
        deleted_at=utcnow() if deleted else None,
    )
    db.session.add(library_file)
    db.session.flush()
    return library_file


def _title(code: str, **kwargs) -> Title:
    title = Title(dvd_id=code, **kwargs)
    db.session.add(title)
    db.session.flush()
    return title


# ---------------------------------------------------------------------------
# 1. 待确认分组
# ---------------------------------------------------------------------------
def test_pending_groups_filter_sort_and_shape(app):
    with app.app_context():
        nas = _volume("nas", "/mnt/nas")
        tape = _volume("tape", "LTO6-0001")
        linked_title = _title("LINKED-001")

        high1 = _file(nas, "SSIS-001", confidence=92)
        high2 = _file(tape, "SSIS-001", confidence=95)
        fc2 = _file(nas, "FC2-PPV-1234567", confidence=96)
        ambiguous = _file(nas, "ambiguous", confidence=45)
        no_code = _file(tape, None, confidence=0)
        # 以下都不该出现
        _file(nas, "SSIS-001", confidence=92, is_sidecar=True)
        _file(nas, "LINKED-001", confidence=92, title_id=linked_title.id)
        _file(nas, "DELETED-001", confidence=92, deleted=True)
        db.session.commit()

        groups = review_service.pending_items(app)

        assert [g.code_guess for g in groups] == [
            "SSIS-001", "FC2-PPV-1234567", "ambiguous", None,
        ]
        first = groups[0]
        assert first.files == 2
        assert (first.confidence_min, first.confidence_max) == (92, 95)
        assert set(first.file_ids) == {high1.id, high2.id}
        assert len(first.samples) == 2
        assert all(sample.startswith("SSIS-001/") for sample in first.samples)
        assert set(first.volumes) == {"NAS /mnt/nas", "磁带 LTO6-0001"}
        assert groups[1].files == 1
        assert groups[-1].code_guess is None and groups[-1].files == 1

        assert len(review_service.pending_items(app, limit=2)) == 2

        summary = review_service.pending_summary(app)
        assert summary.pending_files == 5
        assert summary.pending_groups == 4
        assert summary.low_confidence_files == 2      # ambiguous + 无番号
        assert summary.auto_unmatched_files == 3      # SSIS×2 + FC2
        assert summary.aliases == 0


def test_pending_confidence_window_keeps_auto_unmatched(app):
    with app.app_context():
        volume = _volume()
        _file(volume, "LOW-45", confidence=45)
        _file(volume, "MID-75", confidence=75)
        _file(volume, "HIGH-96", confidence=96)
        db.session.commit()

        default_codes = {g.code_guess for g in review_service.pending_items(app)}
        assert default_codes == {"LOW-45", "MID-75", "HIGH-96"}

        # max_confidence 只收窄低置信度子集；>= CODE_AUTO_MIN 的自动未命中项保留
        codes = {g.code_guess for g in review_service.pending_items(app, max_confidence=60)}
        assert codes == {"LOW-45", "HIGH-96"}

        codes = {g.code_guess for g in review_service.pending_items(app, min_confidence=80)}
        assert codes == {"HIGH-96"}


# ---------------------------------------------------------------------------
# 2. assign_code
# ---------------------------------------------------------------------------
def test_assign_code_updates_files_alias_and_is_idempotent(app, monkeypatch):
    calls: list[str] = []

    def fake_materialize(app_, code):
        calls.append(code)
        return {"considered": 0, "matched": 0, "errors": ["staging 缺失（测试）"]}

    monkeypatch.setattr(review_service, "_run_materialize", fake_materialize)

    with app.app_context():
        volume = _volume()
        first = _file(volume, "garbage name", confidence=40)
        second = _file(volume, "garbage name", confidence=35)
        sidecar = _file(volume, "garbage name", confidence=40, is_sidecar=True)
        db.session.commit()

        result = review_service.assign_code(app, "garbage name", "mide-1")

        assert calls == ["MIDE-001"]
        assert result["code"] == "MIDE-001"
        assert result["files_updated"] == 3          # 含字幕 sidecar
        assert result["alias_created"] is True
        assert result["title_id"] is None
        assert result["materialize"] is not None
        assert result["errors"]                      # dump 没命中 → 提示人工补元数据

        for library_file in (first, second, sidecar):
            db.session.refresh(library_file)
            assert library_file.code_guess == "MIDE-001"
            assert library_file.confidence == 95
        alias = CodeAlias.query.filter_by(raw_key="garbage name").one()
        assert alias.code == "MIDE-001"

        again = review_service.assign_code(app, "garbage name", "MIDE-001")
        assert again["files_updated"] == 0           # 幂等
        assert again["alias_created"] is False
        assert CodeAlias.query.filter_by(raw_key="garbage name").count() == 1
        assert calls == ["MIDE-001", "MIDE-001"]


def test_assign_code_by_file_ids_and_existing_title(app):
    with app.app_context():
        volume = _volume()
        title = _title("SP-001")
        library_file = _file(volume, None, confidence=0)
        other = _file(volume, None, confidence=0)
        db.session.commit()

        result = review_service.assign_code(
            app, "", "sp1", file_ids=[library_file.id], apply_materialize=False,
        )

        assert result["files_updated"] == 1
        assert result["code"] == "SP-001"            # 命中已有 Title 的规范形态
        assert result["title_id"] == title.id
        assert CodeAlias.query.count() == 0          # 无 raw_key 不写 alias
        db.session.refresh(library_file)
        db.session.refresh(other)
        assert library_file.code_guess == "SP-001"
        assert other.code_guess is None              # 未选中的文件不动


def test_assign_code_validates_input(app):
    with app.app_context():
        volume = _volume()
        _file(volume, "some-key", confidence=10)
        db.session.commit()

        with pytest.raises(ValueError):
            review_service.assign_code(app, "some-key", "   ")
        with pytest.raises(ValueError):
            review_service.assign_code(app, "", "SSIS-001")  # 既无 raw_key 也无 file_ids


# ---------------------------------------------------------------------------
# 3. reassign_files
# ---------------------------------------------------------------------------
def test_reassign_files_to_existing_title_learns_alias(app):
    with app.app_context():
        volume = _volume()
        title = _title("MANUAL-001")
        first = _file(volume, "weird_one", confidence=30)
        second = _file(volume, "weird_one", confidence=30)
        db.session.commit()

        result = review_service.reassign_files([first.id, second.id], "manual-001")

        assert result["title_id"] == title.id
        assert result["files_updated"] == 2
        assert result["alias_created"] == 1
        for library_file in (first, second):
            db.session.refresh(library_file)
            assert library_file.title_id == title.id
            assert library_file.code_guess == "MANUAL-001"
            assert library_file.confidence == 95
        alias = CodeAlias.query.filter_by(raw_key="weird_one").one()
        assert alias.code == "MANUAL-001"

        again = review_service.reassign_files([first.id, second.id], "MANUAL-001")
        assert again["files_updated"] == 0
        assert again["alias_created"] == 0


def test_reassign_files_missing_title_calls_materialize(app, monkeypatch):
    calls: list[str] = []

    def fake_materialize(app_, code):
        calls.append(code)
        return {"considered": 1, "matched": 0, "errors": ["dump 没有这个番号"]}

    monkeypatch.setattr(review_service, "_run_materialize", fake_materialize)

    with app.app_context():
        volume = _volume()
        library_file = _file(volume, "weird_two", confidence=30)
        db.session.commit()

        result = review_service.reassign_files([library_file.id], "ZZZ-999")

        assert calls == ["ZZZ-999"]
        assert result["title_id"] is None
        assert result["materialize"] is not None
        assert result["errors"]
        db.session.refresh(library_file)
        assert library_file.code_guess == "ZZZ-999"
        assert library_file.confidence == 95
        assert library_file.title_id is None

        with pytest.raises(ValueError):
            review_service.reassign_files([], "SSIS-001")


# ---------------------------------------------------------------------------
# 4. 路由
# ---------------------------------------------------------------------------
def _seed_one_pending() -> int:
    volume = _volume()
    library_file = _file(volume, "garbage key", confidence=40)
    db.session.commit()
    return library_file.id


def test_page_renders_pending_groups(client, app):
    with app.app_context():
        _seed_one_pending()

    response = client.get("/review")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "待确认" in html
    assert "garbage key" in html
    assert "assign-code" in html          # 每组的「目标番号」输入框
    assert "data-file-ids" in html


def test_page_empty_state(client):
    response = client.get("/review")
    assert response.status_code == 200
    assert "没有待确认文件" in response.get_data(as_text=True)

    data = client.get("/api/review/pending").get_json()
    assert data["groups"] == []
    assert data["summary"]["pending_files"] == 0


def test_api_pending(client, app):
    with app.app_context():
        _seed_one_pending()

    response = client.get("/api/review/pending?limit=10")

    assert response.status_code == 200
    data = response.get_json()
    assert data["limit"] == 10
    assert data["groups"][0]["code_guess"] == "garbage key"
    assert data["groups"][0]["files"] == 1
    assert data["summary"]["pending_files"] == 1
    assert data["truncated"] is False


def test_api_assign_is_synchronous(client, app):
    with app.app_context():
        _seed_one_pending()

    response = client.post(
        "/api/review/assign", json={"raw_key": "garbage key", "code": "MIDE-001"}
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["code"] == "MIDE-001"
    assert data["files_updated"] == 1
    assert data["alias_created"] is True
    # 测试里 R18_DUMP_DB 指向不存在的文件 → materialize 收 errors 返回，不 500
    assert data["materialize"] is not None
    assert data["title_id"] is None

    with app.app_context():
        library_file = LibraryFile.query.one()
        assert library_file.code_guess == "MIDE-001"
        assert library_file.confidence == 95
        assert CodeAlias.query.filter_by(raw_key="garbage key").one().code == "MIDE-001"


def test_api_assign_validation(client):
    assert client.post("/api/review/assign", json={}).status_code == 400
    assert client.post(
        "/api/review/assign", json={"raw_key": "x"}
    ).status_code == 400
    assert client.post(
        "/api/review/assign", json={"raw_key": "x", "code": "SSIS-001", "file_ids": "1,2"}
    ).status_code == 400


def test_api_reassign(client, app):
    with app.app_context():
        volume = _volume()
        title = _title("MANUAL-002")
        library_file = _file(volume, "weird_three", confidence=20)
        db.session.commit()
        file_id, title_id = library_file.id, title.id

    response = client.post(
        "/api/review/reassign",
        json={"file_ids": [file_id], "target_code": "manual-002"},
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["title_id"] == title_id
    assert data["files_updated"] == 1
    with app.app_context():
        library_file = db.session.get(LibraryFile, file_id)
        assert library_file.title_id == title_id
        assert library_file.code_guess == "MANUAL-002"


def test_review_page_paginates_groups(app):
    with app.app_context():
        volume = _volume()
        for index in range(3):
            _file(volume, f"PAGE-{index:03d}", confidence=0)
        db.session.commit()
    client = app.test_client()
    page = client.get("/review?limit=1&page=2")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "第 2 /" in html
    assert "page-jump" in html


def test_target_code_defaults_to_raw_key(app):
    with app.app_context():
        volume = _volume()
        _file(volume, "SSIS-001", confidence=50)
        db.session.commit()
    html = app.test_client().get("/review").get_data(as_text=True)
    assert 'class="assign-code" value="SSIS-001"' in html


def test_batch_assign_endpoint(app):
    with app.app_context():
        volume = _volume()
        first = _file(volume, "BATCH-001", confidence=50)
        second = _file(volume, "BATCH-002", confidence=50)
        db.session.commit()
        first_id, second_id = first.id, second.id

    response = app.test_client().post(
        "/api/review/batch-assign",
        json={
            "items": [
                {"raw_key": "BATCH-001", "code": "BATCH-001", "file_ids": [first_id]},
                {"raw_key": "BATCH-002", "code": "BATCH-002", "file_ids": [second_id]},
            ]
        },
    )
    assert response.status_code == 202
    data = response.get_json()
    assert data["job_id"]
    with app.app_context():
        from app.models import Job
        from app.services import jobs

        job = db.session.get(Job, data["job_id"])
        jobs.run_job(job, app)
        db.session.refresh(job)
        result = __import__("json").loads(job.result)
        assert result["updated"] == 2
        assert result["materialize_job_id"]
        assert db.session.get(LibraryFile, first_id).code_guess == "BATCH-001"
        assert db.session.get(LibraryFile, first_id).confidence == 95


def test_review_page_has_batch_controls(app):
    with app.app_context():
        volume = _volume()
        _file(volume, "BATCH-UI-001", confidence=50)
        db.session.commit()
    html = app.test_client().get("/review").get_data(as_text=True)
    assert 'id="select-all"' in html
    assert 'class="row-select"' in html
    assert "batchAssign()" in html


def test_review_batch_script_select_all_and_right_button(app):
    with app.app_context():
        volume = _volume()
        _file(volume, "BATCH-UI-002", confidence=50)
        db.session.commit()
    html = app.test_client().get("/review").get_data(as_text=True)
    assert "event.target.id !== 'select-all'" in html
    assert 'id="batch-assign-btn"' in html
    # 结果文本在按钮前，按钮靠右
    assert html.index('id="batch-result"') < html.index('id="batch-assign-btn"')


def test_manual_code_creates_stub_for_dump_miss(app):
    with app.app_context():
        volume = _volume()
        item = _file(volume, "HEYZO-2068", confidence=95)
        db.session.commit()
        from app.services.materialize import _materialize_one
        from app.services.review import assign_code

        result = assign_code(app, "HEYZO-2068", "HEYZO-2068", apply_materialize=False)
        assert result["files_updated"] >= 1
        db.session.refresh(item)
        assert item.manual_code is True

        outcome = _materialize_one(None, "HEYZO-2068", [], [item])
        db.session.flush()
        db.session.refresh(item)
        assert outcome.stubbed is True
        assert item.title_id is not None


def test_pending_groups_strict_confidence_and_volume_filter(app):
    with app.app_context():
        nas = _volume("nas", "/mnt/nas")
        tape = _volume("tape", "LTO6-0002")
        _file(nas, "LOW-001", confidence=45)
        _file(nas, "MID-001", confidence=75)
        _file(tape, "HIGH-001", confidence=95)
        db.session.commit()

        low = review_service.pending_items(
            app, min_confidence=0, max_confidence=89, confidence_strict=True
        )
        assert [g.code_guess for g in low] == ["LOW-001", "MID-001"]

        only_tape = review_service.pending_items(app, volume_id=tape.id)
        assert [g.code_guess for g in only_tape] == ["HIGH-001"]

        stats = review_service.pending_summary(
            app, min_confidence=0, max_confidence=89, confidence_strict=True,
            volume_id=nas.id,
        )
        assert stats.pending_files == 2
        assert stats.pending_groups == 2
        labels = [v["label"] for v in review_service.pending_volumes(app)]
        assert "NAS /mnt/nas" in labels
