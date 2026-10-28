"""番号 → r18 staging 物化的测试（真实 ``data/r18dump.db`` + 临时 avlib.db）。

覆盖（契约 docs/CONTRACTS.md §3）：

1. 命中：Title 的日文标题/英标题/发售日/时长/厂牌/封面、女优、题材、剧照 URL；
   视频与字幕 sidecar 都回写 ``title_id``；``flags=chinese_sub`` → ``has_chinese_sub``。
2. 同番号多版本：按 ``dvd_id_key`` 去重 + ``site_id=2``/``title_ja`` 优先，只建 1 个
   Title（``FSDSS-001`` 在 dump 里有 digital / mono / rental 三条）。
3. 未命中：FC2 / HEYZO / LUXU 这类私有番号进 ``no_dump_match`` 与
   ``unmatched_codes``，不建 Title、不关联文件。
4. ``manual_lock=True``：不覆盖人工字段，只补空，统计 ``manual_locked_skipped``。
5. 幂等：同一批跑两次，第二次 ``titles_created == 0``；``refresh=True`` 再跑一次
   ``titles_updated == 0`` / ``files_linked == 0``。
6. 候选过滤：低置信度跳过（``low_confidence_skipped``）、只有字幕 sidecar 的番号
   不参与、``codes`` / ``limit`` 参数生效。
7. ``title_zh`` 默认留空；只有调用方显式给 ``translator`` 才填。
8. 小工具：封面 URL 拼接、剧照最多 20 张、``-0`` 占位不出剧照、发售日解析、
   多版本分组的 key 精确优先。

跑法（项目根 = av-archive/）::

    .venv/bin/python -m pytest tests/test_materialize.py -q
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 允许从任意 cwd 跑
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.db import db  # noqa: E402
from app.models import (  # noqa: E402
    Asset,
    LibraryFile,
    LibraryVolume,
    Title,
    TitleActress,
    TitleGenre,
)
from app.services.materialize import (  # noqa: E402
    _cover_url,
    _parse_release_date,
    _pick_versions,
    _still_urls,
    materialize,
)

DUMP_DB = ROOT / "data" / "r18dump.db"

pytestmark = pytest.mark.skipif(
    not DUMP_DB.is_file(), reason="需要真实 staging 库 data/r18dump.db"
)


# ---------------------------------------------------------------------------
# fixture / 小工具
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
            "R18_DUMP_DB": DUMP_DB,
            "WORKER_ENABLED": False,
        }
    )
    yield application
    with application.app_context():
        db.session.remove()


def _volume(name: str = "nas-test") -> LibraryVolume:
    volume = LibraryVolume(kind="nas", ref=str(Path("/tmp") / name), name=name)
    db.session.add(volume)
    db.session.flush()
    return volume


def _file(volume, code, *, confidence=92, flags=None, is_sidecar=False,
          title_id=None, suffix=".mp4"):
    name = f"{code}{suffix}"
    library_file = LibraryFile(
        volume_id=volume.id,
        rel_path=f"{code}/{name}" if not is_sidecar else f"{code}/sub/{name}",
        name=name,
        size=1024,
        code_guess=code,
        confidence=confidence,
        flags=flags,
        is_sidecar=is_sidecar,
        title_id=title_id,
    )
    db.session.add(library_file)
    return library_file


def _create_title(**kwargs) -> Title:
    title = Title(**kwargs)
    db.session.add(title)
    db.session.flush()
    return title


def _duplicate_dvd_ids() -> list:
    from sqlalchemy import func

    return (
        db.session.query(Title.dvd_id, func.count(Title.id))
        .group_by(Title.dvd_id)
        .having(func.count(Title.id) > 1)
        .all()
    )


# ---------------------------------------------------------------------------
# 1. 命中：元数据 / 关系 / 剧照 / 文件回写
# ---------------------------------------------------------------------------
def test_hit_materializes_title_relations_assets_and_links(app):
    with app.app_context():
        volume = _volume()
        video = _file(volume, "SSIS-001", flags="chinese_sub")
        sidecar = _file(volume, "SSIS-001", is_sidecar=True, suffix=".srt")
        db.session.commit()

        stats = materialize(app)

        assert stats.codes_considered == 1
        assert stats.matched == 1
        assert stats.no_dump_match == 0
        assert stats.titles_created == 1
        assert stats.files_linked == 2  # 视频 + 字幕 sidecar
        assert stats.errors == []

        title = Title.query.filter_by(dvd_id="SSIS-001").one()
        assert title.title_ja and "禁欲" in title.title_ja
        assert title.title_en
        assert title.release_date == date(2021, 2, 19)
        assert title.runtime_mins == 147
        assert title.content_id == "ssis00001"
        assert title.cover_url == (
            "https://pics.dmm.co.jp/digital/video/ssis00001/ssis00001pl.jpg"
        )
        assert title.has_chinese_sub is True
        assert title.title_zh is None  # 中文留空，不自动翻译
        assert title.source_dump_date  # 记录了 staging dump 日期
        assert title.maker_id is not None
        assert title.label_id is not None
        assert title.director_id is not None

        actresses = {
            ta.actress.name_kanji
            for ta in TitleActress.query.filter_by(title_id=title.id)
        }
        assert {"葵つかさ", "乙白さやか"} <= actresses

        genres = {
            tg.genre.genre_en
            for tg in TitleGenre.query.filter_by(title_id=title.id)
        }
        assert {"Beautiful Tits", "Drama"} <= genres

        stills = Asset.query.filter_by(title_id=title.id, kind="still").order_by(
            Asset.idx
        ).all()
        assert len(stills) == 10
        assert [asset.idx for asset in stills] == list(range(1, 11))
        assert stills[0].url == (
            "https://pics.dmm.co.jp/digital/video/ssis00001/ssis00001jp-1.jpg"
        )
        assert all(asset.local_path is None for asset in stills)  # 只建行不下载

        assert video.title_id == title.id
        assert sidecar.title_id == title.id


# ---------------------------------------------------------------------------
# 2. 同番号多版本：只建一个 Title
# ---------------------------------------------------------------------------
def test_multi_version_dedupes_to_single_title(app):
    with app.app_context():
        volume = _volume()
        _file(volume, "FSDSS-001")
        db.session.commit()

        stats = materialize(app, codes=["FSDSS-001"])

        assert stats.matched == 1
        assert stats.titles_created == 1
        assert Title.query.count() == 1  # dump 里 3 条版本，只物化 1 个 Title

        title = Title.query.filter_by(dvd_id="FSDSS-001").one()
        assert "生田みなみ" in (title.title_ja or "")
        assert title.title_en  # digital 版本带英文标题
        assert title.content_id == "1fsdss00001"  # 主版本 = site_id=2 + digital
        assert title.cover_url
        assert _duplicate_dvd_ids() == []


def test_unhyphenated_dump_id_uses_canonical_title_id(app):
    """dump 里 ``FSDSS001``（无连字符）也要落到规范 Title ``FSDSS-001``。"""
    with app.app_context():
        volume = _volume()
        _file(volume, "FSDSS001")
        db.session.commit()

        stats = materialize(app)

        assert stats.matched == 1
        assert Title.query.filter_by(dvd_id="FSDSS-001").count() == 1
        assert Title.query.filter_by(dvd_id="FSDSS001").count() == 0


def test_pick_versions_keeps_distinct_dvd_id_keys_apart():
    """``DOG-01`` 与 ``DOG-001`` 是不同 key，聚合时不能混成一组。"""
    def row(key, dvd_id, title_ja):
        return {
            "dvd_id_key": key,
            "dvd_id": dvd_id,
            "content_id": dvd_id.lower().replace("-", ""),
            "title_ja": title_ja,
            "site_id": 2,
            "service_code": "digital",
            "jacket_full_url": None,
            "gallery_full_first": None,
            "gallery_full_last": None,
        }

    rows = [
        row("dog01", "DOG-01", "一日目"),
        row("dog001", "DOG-001", "別作品"),
        row("dog001", "DOG-001", "別作品（mono）"),
    ]
    chosen = _pick_versions(rows, "DOG-001")
    assert {item["dvd_id"] for item in chosen} == {"DOG-001"}
    chosen = _pick_versions(rows, "DOG-01")
    assert {item["dvd_id"] for item in chosen} == {"DOG-01"}


# ---------------------------------------------------------------------------
# 3. 未命中：私有番号
# ---------------------------------------------------------------------------
def test_private_codes_are_reported_unmatched(app):
    with app.app_context():
        volume = _volume()
        private_codes = ["HEYZO-1234", "259LUXU-1234"]
        video = [_file(volume, code) for code in private_codes]
        db.session.commit()

        stats = materialize(app)

        assert stats.codes_considered == 2
        assert stats.matched == 0
        assert stats.titles_created == 0
        assert stats.no_dump_match == 2
        assert sorted(stats.unmatched_codes) == sorted(private_codes)
        assert stats.errors == []
        assert Title.query.count() == 0
        assert [f.title_id for f in video] == [None, None]


# ---------------------------------------------------------------------------
# 4. manual_lock：只补空，不覆盖
# ---------------------------------------------------------------------------
def test_manual_lock_only_fills_blanks(app):
    with app.app_context():
        volume = _volume()
        _file(volume, "JUL-001")
        title = _create_title(
            dvd_id="JUL-001", title_ja="人工锁定的日文标题", title_zh="人工中文",
            manual_lock=True,
        )
        db.session.commit()

        stats = materialize(app)

        assert stats.matched == 1
        assert stats.titles_created == 0
        assert stats.manual_locked_skipped == 1
        assert stats.titles_updated == 1  # 只补了封面等空字段

        db.session.refresh(title)
        assert title.title_ja == "人工锁定的日文标题"  # 未被 dump 覆盖
        assert title.title_zh == "人工中文"
        assert title.cover_url  # 空字段被补上
        assert title.director_id is not None
        actresses = {
            ta.actress.name_kanji
            for ta in TitleActress.query.filter_by(title_id=title.id)
        }
        assert "水戸かな" in actresses

        # 再 refresh 一次：已经补满，不应再产生更新
        stats2 = materialize(app, refresh=True)
        assert stats2.manual_locked_skipped == 1
        assert stats2.titles_updated == 0
        assert stats2.files_linked == 0
        assert title.title_ja == "人工锁定的日文标题"


# ---------------------------------------------------------------------------
# 5. 幂等
# ---------------------------------------------------------------------------
def test_idempotent_runs(app):
    codes = ["SSIS-001", "JUL-001", "FSDSS-001"]
    with app.app_context():
        volume = _volume()
        for code in codes:
            _file(volume, code)
        db.session.commit()

        first = materialize(app)
        assert first.titles_created == len(codes)
        title_count = Title.query.count()
        actress_count = TitleActress.query.count()
        genre_count = TitleGenre.query.count()
        asset_count = Asset.query.count()

        # 第二次默认跑：文件都已关联（或未命中），没有候选
        second = materialize(app)
        assert second.titles_created == 0
        assert Title.query.count() == title_count

        # 第二次 refresh 跑：字段/关联/剧照都不许变，也不许重复
        third = materialize(app, refresh=True)
        assert third.titles_created == 0
        assert third.titles_updated == 0
        assert third.files_linked == 0
        assert Title.query.count() == title_count
        assert TitleActress.query.count() == actress_count
        assert TitleGenre.query.count() == genre_count
        assert Asset.query.count() == asset_count
        assert _duplicate_dvd_ids() == []


def test_refresh_updates_stale_metadata(app):
    with app.app_context():
        volume = _volume()
        stale = _create_title(dvd_id="STARS-001", title_ja="旧标题", manual_lock=False)
        _file(volume, "STARS-001", title_id=stale.id)
        db.session.commit()

        # 已关联 + 未 refresh → 不是候选
        untouched = materialize(app)
        assert untouched.codes_considered == 0

        stats = materialize(app, refresh=True)
        assert stats.codes_considered == 1
        assert stats.matched == 1
        assert stats.titles_created == 0
        assert stats.titles_updated == 1
        assert "飛鳥りん" in stale.title_ja
        assert _duplicate_dvd_ids() == []


# ---------------------------------------------------------------------------
# 6. 候选过滤：低置信度 / 只有字幕 / codes / limit
# ---------------------------------------------------------------------------
def test_low_confidence_and_sidecar_only_are_skipped(app):
    with app.app_context():
        volume = _volume()
        low = _file(volume, "SSIS-001", confidence=50)
        # 只有字幕 sidecar、没有视频文件的番号不参与物化
        _file(volume, "JUL-001", confidence=92, is_sidecar=True)
        db.session.commit()

        stats = materialize(app)

        assert stats.codes_considered == 0
        assert stats.low_confidence_skipped == 1
        assert stats.matched == 0
        assert Title.query.count() == 0
        assert low.title_id is None


def test_codes_and_limit_filters(app):
    with app.app_context():
        volume = _volume()
        for code in ("SSIS-001", "IPX-001", "JUL-001"):
            _file(volume, code)
        db.session.commit()

        stats = materialize(app, codes=["SSIS-001", "IPX-001"])
        assert stats.codes_considered == 2
        assert stats.titles_created == 2
        assert Title.query.count() == 2
        jul = LibraryFile.query.filter_by(code_guess="JUL-001").one()
        assert jul.title_id is None

        limited = materialize(app, refresh=True, limit=1)
        assert limited.codes_considered == 1
        assert limited.matched == 1


# ---------------------------------------------------------------------------
# 7. title_zh 仅由可选 translator 填充
# ---------------------------------------------------------------------------
def test_translator_is_opt_in_and_runs_once(app):
    calls: list[str] = []

    def translator(title_ja: str) -> str:
        calls.append(title_ja)
        return f"中文：{title_ja}"

    with app.app_context():
        volume = _volume()
        _file(volume, "JUL-001")
        db.session.commit()

        materialize(app, translator=translator)
        title = Title.query.filter_by(dvd_id="JUL-001").one()
        assert title.title_zh == f"中文：{title.title_ja}"
        assert calls == [title.title_ja]

        # refresh 时 title_zh 已存在 → 不再调用翻译
        stats = materialize(app, refresh=True, translator=translator)
        assert len(calls) == 1
        assert stats.titles_updated == 0


# ---------------------------------------------------------------------------
# 8. 男优：derived_video_actor → Actress(role='male')
# ---------------------------------------------------------------------------
def test_male_actor_gets_role_male(app, monkeypatch):
    """真实 dump 的 site_id=2 数据没有男优行，用 fake 关联行覆盖管道本身。"""
    from app.services import materialize as materialize_module

    real_related = materialize_module._related
    fake_actor = {"id": 990001, "name_kanji": "男優テスト", "name_kana": "ダンユウテスト"}

    def fake_related(conn, sql, content_ids):
        if sql is materialize_module._ACTOR_SQL:
            return [fake_actor]
        return real_related(conn, sql, content_ids)

    monkeypatch.setattr(materialize_module, "_related", fake_related)
    with app.app_context():
        volume = _volume()
        _file(volume, "SSIS-001")
        db.session.commit()

        stats = materialize(app)

        assert stats.matched == 1
        title = Title.query.filter_by(dvd_id="SSIS-001").one()
        roles = {
            ta.actress.name_kanji: ta.role
            for ta in TitleActress.query.filter_by(title_id=title.id)
        }
        assert roles.get("男優テスト") == "male"
        assert roles.get("葵つかさ") == "female"


# ---------------------------------------------------------------------------
# 9. 异常 / 小工具
# ---------------------------------------------------------------------------
def test_missing_staging_db_is_reported_not_raised(app, tmp_path):
    with app.app_context():
        volume = _volume()
        _file(volume, "SSIS-001")
        db.session.commit()

        stats = materialize(app, dump_db_path=str(tmp_path / "missing.db"))

        assert stats.codes_considered == 0
        assert stats.matched == 0
        assert stats.errors  # staging 不可用 → 收集到 errors，不抛到 UI
        assert LibraryFile.query.one().title_id is None


def test_garbage_staging_db_is_reported_not_raised(app, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a sqlite database")
    with app.app_context():
        volume = _volume()
        _file(volume, "SSIS-001")
        db.session.commit()

        stats = materialize(app, dump_db_path=str(bad))

        assert stats.codes_considered == 0
        assert stats.matched == 0
        assert stats.errors
        assert LibraryFile.query.one().title_id is None


def test_cover_url_and_still_urls():
    assert _cover_url("digital/video/x/xpl") == (
        "https://pics.dmm.co.jp/digital/video/x/xpl.jpg"
    )
    assert _cover_url("/mono/movie/adult/x/xpl") == (
        "https://pics.dmm.co.jp/mono/movie/adult/x/xpl.jpg"
    )
    assert _cover_url(None) is None

    # 上限 20 张
    assert len(_still_urls("digital/video/a/ajp-1", "digital/video/a/ajp-25")) == 20
    # -0 占位 / basename 不一致 → 不生成剧照
    assert _still_urls("digital/video/a/ajp-1", "digital/video/a/ajp-0") == []
    assert _still_urls("digital/video/a/ajp-1", "digital/video/b/bjp-5") == []
    # 首尾相同 basename 时正常生成
    urls = _still_urls("digital/video/a/ajp-1", "digital/video/a/ajp-3")
    assert [idx for idx, _ in urls] == [1, 2, 3]
    assert urls[0][1] == "https://pics.dmm.co.jp/digital/video/a/ajp-1.jpg"


def test_parse_release_date():
    assert _parse_release_date("2021-02-19") == date(2021, 2, 19)
    assert _parse_release_date("2019-07") == date(2019, 7, 1)
    assert _parse_release_date("2019") == date(2019, 1, 1)
    assert _parse_release_date(None) is None
    assert _parse_release_date("") is None
    assert _parse_release_date("not-a-date") is None


def test_high_confidence_unmatched_code_creates_stub_title(app):
    """dump 没收录但置信度足够的正常番号（NMSL-019）建占位条目，文件先入片库。"""
    with app.app_context():
        volume = _volume()
        row = _file(volume, "NMSL-019")
        db.session.commit()

        stats = materialize(app)

        assert stats.no_dump_match == 1
        assert stats.titles_stubbed == 1
        assert stats.titles_created == 1
        assert stats.files_linked == 1
        title = db.session.query(Title).filter_by(dvd_id="NMSL-019").one()
        assert title.title_ja is None
        assert row.title_id == title.id


def test_private_codes_do_not_create_stub_titles(app):
    with app.app_context():
        volume = _volume()
        _file(volume, "HEYZO-1234")
        db.session.commit()

        stats = materialize(app)

        assert stats.titles_stubbed == 0
        assert db.session.query(Title).count() == 0


def test_fc2_code_creates_stub_for_url_import(app):
    """FC2 高置信度也建占位条目，详情页可从 FC2 官方页导入元数据。"""
    with app.app_context():
        volume = _volume()
        row = _file(volume, "FC2-PPV-1234567")
        db.session.commit()

        stats = materialize(app)

        assert stats.titles_stubbed == 1
        title = db.session.query(Title).filter_by(dvd_id="FC2-PPV-1234567").one()
        assert row.title_id == title.id


def test_materialize_reports_total(app):
    with app.app_context():
        volume = _volume()
        _file(volume, "SSIS-001")
        _file(volume, "SSIS-002")
        db.session.commit()
        totals = []
        materialize(app, on_total=totals.append)
        assert totals and totals[0] >= 1
