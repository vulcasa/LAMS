"""片库排序 / 分页大小的回归测试。"""
import re
from datetime import date

import pytest

from app import create_app
from app.db import db
from app.models import Title


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path / "data",
            "DB_PATH": tmp_path / "data" / "test.db",
            "WORKER_ENABLED": False,
            "LTFS_MOUNT": str(tmp_path / "tape"),
        }
    )
    (tmp_path / "tape").mkdir()
    with app.app_context():
        db.session.add_all(
            [
                Title(dvd_id="ZZZ-001", release_date=date(2024, 6, 1)),
                Title(dvd_id="AAA-002", release_date=date(2023, 1, 1)),
                Title(dvd_id="MMM-003", release_date=None),
            ]
        )
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()


@pytest.fixture()
def client(app):
    return app.test_client()


def _codes_in_order(html: str) -> list[str]:
    return re.findall(r'<div class="code">([^<]+)</div>', html)


def test_sort_by_code_asc_and_desc(client):
    assert _codes_in_order(client.get("/library?sort=code_asc").get_data(as_text=True)) == [
        "AAA-002", "MMM-003", "ZZZ-001"
    ]
    assert _codes_in_order(client.get("/library?sort=code_desc").get_data(as_text=True)) == [
        "ZZZ-001", "MMM-003", "AAA-002"
    ]


def test_sort_by_release_date(client):
    # 新→旧：无日期的排最后
    assert _codes_in_order(client.get("/library?sort=date_desc").get_data(as_text=True)) == [
        "ZZZ-001", "AAA-002", "MMM-003"
    ]
    # 旧→新：无日期的排最后
    assert _codes_in_order(client.get("/library?sort=date_asc").get_data(as_text=True)) == [
        "AAA-002", "ZZZ-001", "MMM-003"
    ]


def test_default_sort_is_date_desc(client):
    assert _codes_in_order(client.get("/library").get_data(as_text=True)) == [
        "ZZZ-001", "AAA-002", "MMM-003"
    ]


def test_page_size_is_49_and_pager_keeps_sort(client, app):
    with app.app_context():
        db.session.add_all(
            [Title(dvd_id=f"TST-{i:03d}", release_date=date(2020, 1, (i % 28) + 1))
             for i in range(60)]
        )
        db.session.commit()
    html = client.get("/library?sort=code_asc").get_data(as_text=True)
    assert len(re.findall(r'class="card"', html)) == 49
    assert "sort=code_asc" in html  # 下一页链接保留排序


def test_archived_filter_and_badge(app):
    from app.models import LibraryFile, LibraryVolume

    with app.app_context():
        tape = LibraryVolume(kind="tape", ref="FB1186")
        nas = LibraryVolume(kind="nas", ref="/mnt/nas")
        db.session.add_all([tape, nas])
        db.session.flush()
        for code, volume in (("ZZZ-001", tape), ("AAA-002", nas)):
            title = Title.query.filter_by(dvd_id=code).one()
            db.session.add(
                LibraryFile(
                    volume_id=volume.id,
                    rel_path=f"{code}/{code}.mp4",
                    name=f"{code}.mp4",
                    size=10,
                    title_id=title.id,
                )
            )
        db.session.commit()

    client = app.test_client()
    archived = client.get("/library?archived=1").get_data(as_text=True)
    assert "ZZZ-001" in archived and "AAA-002" not in archived
    assert "已归档" in archived

    unarchived = client.get("/library?archived=0").get_data(as_text=True)
    assert "AAA-002" in unarchived and "ZZZ-001" not in unarchived


def test_pagination_has_page_links_and_jump(client, app):
    with app.app_context():
        db.session.add_all(
            [Title(dvd_id=f"PG-{i:03d}", release_date=date(2020, 1, 1)) for i in range(400)]
        )
        db.session.commit()
    html = client.get("/library").get_data(as_text=True)
    assert "page=2" in html
    assert 'class="page-jump"' in html
    assert 'name="page"' in html
    assert ">Go</button>" in html
    # 页数超过窗口时出现省略号
    assert "page-gap" in html or "…" in html


def test_fts_search_matches_title_and_code(app):
    from app.services.search import rebuild_title_fts

    with app.app_context():
        db.session.add(All := Title(dvd_id="FTS-999", title_ja="特别搜索标题"))
        db.session.commit()
        rebuild_title_fts(app)
    client = app.test_client()
    html = client.get("/library?q=FTS-999").get_data(as_text=True)
    assert "FTS-999" in html
    html = client.get("/library?q=特别搜索").get_data(as_text=True)
    assert "FTS-999" in html
