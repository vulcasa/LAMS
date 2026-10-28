"""JavDB 补缺口服务测试（全 mock，不依赖网络）。

跑法（项目根 = av-archive/）::

    .venv/bin/python -m pytest tests/test_javdb.py -q

覆盖：
* 用真实页面裁剪脱敏的 ``tests/fixtures/javdb_sample.html`` 验证详情页解析
  （番號/中文标题/繁中類別/評分+人数/系列/片商/日期/中字标记）；
* 搜索结果页只剩「番号严格相等」才命中（同系列别的片子不误抓）；
* ``fetch`` 的域名轮换（javdb.com → 页面 data-domain javdb580.com）、403 熔断、
  网络异常、无结果 —— 全部返回 None 不抛；
* ``fill_gaps`` 只补缺口、幂等、ScrapeTask 记录、403 立即熔断、manual_lock 跳过。
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 支持 pytest 直接跑（不依赖 cwd）
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from app.services import javdb  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "javdb_sample.html"
FIXTURE_NO_ZH_TITLE = ROOT / "tests" / "fixtures" / "javdb_no_zh_title.html"
FIXTURE_UNTRANSLATED = ROOT / "tests" / "fixtures" / "javdb_untranslated_title.html"

# --- 真实搜索结果页裁剪（2026-10-26 抓取，URL 已脱敏）--------------------------
SEARCH_SSNI_CN = (
    '<html><head><meta charset="utf-8"><title>JavDB 成人影片數據庫</title></head>'
    '<body data-lang="zh" data-domain="https://javdb580.com">'
    '<div class="movie-list h cols-4 vcols-8"><div class="item"> '
    '<a class="box" href="/v/P906N" title="大乱交解禁！！ 超乱交スペシャル"> '
    '<div class="cover"> <span class="tag-can-play cnsub"> 中字可播放 </span> '
    '<img loading="lazy" src="https://img.example.invalid/cover.jpg"/> </div> '
    '<div class="video-title"><strong>SSNI-001</strong> 大乱交解禁！！ 超乱交スペシャル</div> '
    '<div class="score"> <span class="value"><span class="score-stars">'
    '<i class="icon-star"></i><i class="icon-star"></i><i class="icon-star"></i>'
    '<i class="icon-star"></i><i class="icon-star gray"></i></span> 4.52分, 由1080人評價</span> </div> '
    '<div class="meta"> 2017-09-25 </div> '
    '<div class="tags has-addons"> <span class="tag is-warning">含中字磁鏈</span> </div> '
    "</a> </div></div></body></html>"
)
SEARCH_IPX_NO_CN = (
    '<html><head><meta charset="utf-8"><title>JavDB 成人影片數據庫</title></head>'
    '<body data-lang="zh" data-domain="https://javdb580.com">'
    '<div class="movie-list h cols-4 vcols-8"><div class="item"> '
    '<a class="box" href="/v/A72m" title="女子校生便所交際 妃月るい"> '
    '<div class="cover"> <span class="tag-can-play"> 可播放 </span> '
    '<img loading="lazy" src="https://img.example.invalid/cover.jpg"/> </div> '
    '<div class="video-title"><strong>IPX-001</strong> 女子校生便所交際 妃月るい</div> '
    '<div class="score"> <span class="value"><span class="score-stars"></span> 3.77分, 由134人評價</span> </div> '
    '<div class="meta"> 2017-08-11 </div> '
    '<div class="tags has-addons"> <span class="tag is-success">含磁鏈</span> </div> '
    "</a> </div></div></body></html>"
)
SEARCH_WRONG_MOVIE = SEARCH_SSNI_CN.replace("SSNI-001", "SSNI-002")


# ---------------------------------------------------------------------------
# 假 session / 假响应
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, text="", status=200, url="https://javdb.com/search", headers=None, payload=None):
        self.text = text
        self.status_code = status
        self.url = url
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """按顺序返回预置响应；记录每次请求参数。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, **kwargs):
        self.calls.append((url, dict(kwargs)))
        if not self.responses:
            raise AssertionError(f"unexpected request: {url}")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture(autouse=True)
def _reset_domain_cache():
    javdb._domain_cache = None
    yield
    javdb._domain_cache = None


# ---------------------------------------------------------------------------
# app / DB fixture（用 app factory，库和目录都放 tmp_path）
# ---------------------------------------------------------------------------
@pytest.fixture()
def app(tmp_path):
    from app import create_app
    from app.db import db

    data = tmp_path / "data"
    media = data / "media"
    application = create_app(
        {
            "TESTING": True,
            "DATA_DIR": str(data),
            "MEDIA_DIR": str(media),
            "COVER_DIR": str(media / "covers"),
            "STILL_DIR": str(media / "stills"),
            "AVATAR_DIR": str(media / "avatars"),
            "SUBTITLE_DIR": str(media / "subtitles"),
            "THUMB_DIR": str(media / "thumbs"),
            "DUMP_DIR": str(data / "dump"),
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
            "JAVDB_RATE_SECONDS": 0.0,
            "WORKER_ENABLED": False,
        }
    )
    ctx = application.app_context()
    ctx.push()
    try:
        yield application
    finally:
        db.session.remove()
        ctx.pop()


def _seed_title(app, dvd_id, *, rating=None, release_date=None, manual_lock=False):
    from app.db import db
    from app.models import Title

    title = Title(
        dvd_id=dvd_id,
        rating=rating,
        release_date=release_date,
        manual_lock=manual_lock,
    )
    db.session.add(title)
    db.session.commit()
    return title


def _seed_genre(app, name_ja, *, title=None, genre_zh=None):
    from app.db import db
    from app.models import Genre, TitleGenre

    genre = Genre(genre_ja=name_ja, genre_zh=genre_zh)
    db.session.add(genre)
    db.session.flush()
    if title is not None:
        db.session.add(TitleGenre(title_id=title.id, genre_id=genre.id, source="r18"))
    db.session.commit()
    return genre


def _fake_detail(**overrides):
    payload = dict(
        code="SSNI-001",
        url="https://javdb580.com/v/P906N",
        title="SSNI-001 大乱交解禁",
        genres_zh=["巨乳", "美少女"],
        rating=4.52,
        votes=1080,
        has_chinese_sub=True,
        series="ノンストップ大量射精○○連発超乱交スペシャル",
        maker="S1 NO.1 STYLE",
        release_date="2017-09-25",
        title_zh="大亂交解禁！24根肉棒猛肏到噴精25連發 超亂交特別版 葵",
    )
    payload.update(overrides)
    return javdb.JavdbDetail(**payload)


# ---------------------------------------------------------------------------
# 详情页解析（真实 fixture）
# ---------------------------------------------------------------------------
def test_parse_detail_real_fixture():
    html = FIXTURE.read_text(encoding="utf-8")
    detail = javdb.parse_detail_html(html, code="SSNI-001", url="https://javdb.com/v/P906N")

    assert detail is not None
    assert detail.code == "SSNI-001"
    assert detail.url == "https://javdb.com/v/P906N"
    # 标题：真实页面 h2 里 .current-title 是 JavDB 的繁体中文标题（实测），.origin-title 是日文原名
    assert detail.title and "SSNI" not in detail.title
    assert detail.title_zh == "大亂交解禁！24根肉棒猛肏到噴精25連發 超亂交特別版 葵"
    assert detail.title == detail.title_zh
    assert detail.rating == pytest.approx(4.52)
    assert detail.votes == 1080
    assert "巨乳" in detail.genres_zh and "淫亂真實" in detail.genres_zh
    assert detail.series and "ノンストップ" in detail.series
    assert detail.maker == "S1 NO.1 STYLE"
    assert detail.release_date == "2017-09-25"
    # 真实页面的磁力条目里有 span.tag「字幕」→ 有中文字幕版
    assert detail.has_chinese_sub is True


def test_parse_detail_without_current_title_has_no_title_zh():
    """只有 .origin-title（日文原名）→ title_zh 必须为 None（回退日文，不硬造）。"""
    html = FIXTURE_NO_ZH_TITLE.read_text(encoding="utf-8")
    detail = javdb.parse_detail_html(html, code="SSNI-001")

    assert detail is not None
    assert detail.title_zh is None
    assert "チ●ポ" in detail.title  # 展示标题回退到日文原名
    assert detail.rating == pytest.approx(4.52)  # 其它字段照常解析


def test_parse_detail_untranslated_current_title_returns_none():
    """.current-title 内容与日文原名相同（没翻译）→ 不算中文标题。"""
    html = FIXTURE_UNTRANSLATED.read_text(encoding="utf-8")
    detail = javdb.parse_detail_html(html, code="SSNI-001")

    assert detail is not None
    assert detail.title_zh is None


def test_parse_detail_empty_or_english_current_title_returns_none():
    html = FIXTURE.read_text(encoding="utf-8")
    empty = re.sub(
        r'(<strong class="current-title">).*?(</strong>)',
        r"\1   \2",
        html,
        count=1,
        flags=re.S,
    )
    assert javdb.parse_detail_html(empty, code="SSNI-001").title_zh is None

    english = re.sub(
        r'(<strong class="current-title">).*?(</strong>)',
        r"\1Big Orgy Special\2",
        html,
        count=1,
        flags=re.S,
    )
    detail = javdb.parse_detail_html(english, code="SSNI-001")
    assert detail is not None
    assert detail.title_zh is None  # 纯英文不是中文标题
    assert detail.title == "Big Orgy Special"  # 展示标题仍可用


def test_parse_detail_japanese_current_title_rejected():
    """实测回归：ABP-203 的 .current-title 直接是日文（含假名且与日文原名不同）→ 不算中文。"""
    html = FIXTURE.read_text(encoding="utf-8")
    japanese = re.sub(
        r'(<strong class="current-title">).*?(</strong>)',
        r"\1制服お嬢様の卑猥なる飼育 雲乃亜美\2",
        html,
        count=1,
        flags=re.S,
    )
    detail = javdb.parse_detail_html(japanese, code="SSNI-001")
    assert detail is not None
    assert detail.title_zh is None  # 有假名 → 仍是日文，不能写进 title_zh
    assert detail.title == "制服お嬢様の卑猥なる飼育 雲乃亜美"  # 展示标题仍可用


def test_pick_title_zh_similarity_guard():
    # 只是标点/后缀差异（≥85% 相似）→ 视为未翻译
    assert javdb._pick_title_zh(
        "制服お嬢様の卑猥なる飼育 雲乃亜美",
        "制服お嬢様の卑猥なる飼育 雲乃亜美 【完全版】",
    ) is None
    # 真中文（无假名）且与日文差异大 → 接受
    assert javdb._pick_title_zh(
        "兩天一夜，美少女完全預約制。 第二章 ～相樂彩陽的場合～",
        "一泊二日、美少女完全予約制。 第二章 〜相楽いろの場合〜",
    ) is not None


def test_parse_detail_code_mismatch_returns_none():
    html = FIXTURE.read_text(encoding="utf-8")
    assert javdb.parse_detail_html(html, code="IPX-001") is None


def test_parse_detail_not_a_detail_page_returns_none():
    assert javdb.parse_detail_html("<html><body><h1>Just a moment</h1></body></html>") is None
    assert javdb.parse_detail_html("") is None


def test_parse_detail_without_chinese_tag():
    html = FIXTURE.read_text(encoding="utf-8").replace("字幕", "注释")
    detail = javdb.parse_detail_html(html, code="SSNI-001")
    assert detail is not None and detail.has_chinese_sub is False


# ---------------------------------------------------------------------------
# 搜索结果解析
# ---------------------------------------------------------------------------
def test_parse_search_matches_exact_code_and_cn_flag():
    hit = javdb.parse_search_html(SEARCH_SSNI_CN, "SSNI-001")
    assert hit is not None
    assert hit.href == "/v/P906N"
    assert hit.has_chinese_sub is True


def test_parse_search_plain_item_has_no_cn_flag():
    hit = javdb.parse_search_html(SEARCH_IPX_NO_CN, "IPX-001")
    assert hit is not None and hit.href == "/v/A72m"
    assert hit.has_chinese_sub is False


def test_parse_search_ignores_other_codes():
    assert javdb.parse_search_html(SEARCH_WRONG_MOVIE, "SSNI-001") is None
    assert javdb.parse_search_html("", "SSNI-001") is None


# ---------------------------------------------------------------------------
# fetch：域名轮换 / 熔断 / 网络异常 / 无结果
# ---------------------------------------------------------------------------
def test_fetch_follows_data_domain_and_parses():
    html = FIXTURE.read_text(encoding="utf-8")
    session = FakeSession(
        FakeResponse(SEARCH_SSNI_CN, url="https://javdb.com/search?q=SSNI-001&f=all"),
        FakeResponse(html, url="https://javdb580.com/v/P906N"),
    )
    detail = javdb.fetch("ssni-001", session=session)

    assert detail is not None and detail.code == "SSNI-001"
    assert detail.has_chinese_sub is True
    # 详情页 URL 跟随「实际响应域名」（javdb.com 本身可用就不强切到 data-domain）
    assert session.calls[1][0] == "https://javdb.com/v/P906N"
    # 但页面 data-domain 被记住，下次优先用 javdb580.com
    assert javdb._domain_cache == "https://javdb580.com"
    # 搜索请求带了番号与 f=all
    assert session.calls[0][1]["params"] == {"q": "ssni-001", "f": "all"}

    session2 = FakeSession(
        FakeResponse(SEARCH_SSNI_CN, url="https://javdb580.com/search?q=SSNI-001&f=all"),
        FakeResponse(html, url="https://javdb580.com/v/P906N"),
    )
    assert javdb.fetch("SSNI-001", session=session2) is not None
    assert session2.calls[0][0] == "https://javdb580.com/search"


def test_fetch_passes_cookie_header():
    html = FIXTURE.read_text(encoding="utf-8")
    session = FakeSession(
        FakeResponse(SEARCH_SSNI_CN, url="https://javdb.com/search?q=SSNI-001&f=all"),
        FakeResponse(html, url="https://javdb.com/v/P906N"),
    )
    assert javdb.fetch("SSNI-001", session=session, cookie="over18=1") is not None
    assert session.calls[0][1]["headers"]["Cookie"] == "over18=1"


def test_fill_gaps_progress_callback(app, monkeypatch):
    with app.app_context():
        _seed_title(app, "SSNI-001")
        _seed_title(app, "SSNI-002")

    monkeypatch.setattr(javdb, "_fetch_impl", lambda code, **kwargs: _fake_detail(code=code))
    events = []
    javdb.fill_gaps(app, rate=0, progress=lambda done, message="": events.append((done, message)))

    assert [done for done, _ in events] == [1, 2]
    assert all(isinstance(message, str) and message for _, message in events)


def test_fetch_returns_none_on_403():
    session = FakeSession(FakeResponse("blocked", status=403, url="https://javdb.com/search"))
    assert javdb.fetch("SSNI-001", session=session) is None


def test_fetch_returns_none_on_429_and_stops():
    session = FakeSession(FakeResponse("rate limited", status=429))
    assert javdb.fetch("SSNI-001", session=session) is None
    assert len(session.calls) == 1  # 熔断，不换域名重试


def test_fetch_returns_none_on_network_error():
    session = FakeSession(requests.ConnectionError("boom"), requests.ConnectionError("boom"))
    assert javdb.fetch("SSNI-001", session=session) is None


def test_fetch_returns_none_when_no_result():
    session = FakeSession(FakeResponse(SEARCH_WRONG_MOVIE, url="https://javdb.com/search?q=SSNI-001"))
    assert javdb.fetch("SSNI-001", session=session) is None
    assert len(session.calls) == 1  # 没命中就不去请求详情


# ---------------------------------------------------------------------------
# fill_gaps
# ---------------------------------------------------------------------------
def test_fill_gaps_fills_missing_fields_and_is_idempotent(app, monkeypatch):
    from app.db import db
    from app.models import ScrapeTask, Title, TitleGenre

    with app.app_context():
        target = _seed_title(app, "SSNI-001")
        complete = _seed_title(app, "IPX-001", rating=3.77)
        complete.title_zh = "已有中文标题"
        db.session.commit()
        _seed_genre(app, "巨乳", title=complete, genre_zh="巨乳")

    monkeypatch.setattr(javdb, "_fetch_impl", lambda code, **kwargs: _fake_detail(code=code))

    stats = javdb.fill_gaps(app, rate=0)

    assert stats["considered"] == 1 and stats["filled"] == 1
    assert stats["blocked"] is False and stats["errors"] == []
    with app.app_context():
        title = db.session.query(Title).filter_by(dvd_id="SSNI-001").one()
        assert title.rating == pytest.approx(4.52)
        assert title.rating_votes == 1080
        assert title.rating_source == "javdb"
        assert title.release_date == dt.date(2017, 9, 25)
        assert title.series is not None and "ノンストップ" in title.series.name_ja
        assert title.maker is not None and title.maker.name_en == "S1 NO.1 STYLE"
        assert title.has_chinese_sub is True
        zh_names = {
            row.genre.genre_zh
            for row in db.session.query(TitleGenre).filter_by(title_id=title.id)
            if row.genre is not None and row.genre.genre_zh
        }
        assert {"巨乳", "美少女"} <= zh_names
        task = db.session.query(ScrapeTask).filter_by(title_id=title.id, stage="javdb_gap").one()
        assert task.status == "ok" and task.attempts == 1

    # 第二次跑：已经没有缺口了
    second = javdb.fill_gaps(app, rate=0)
    assert second["considered"] == 0 and second["filled"] == 0

    # 人为清掉 rating 再跑一次：genre/series/maker 不应重复建、不应重复关联
    with app.app_context():
        title = db.session.query(Title).filter_by(dvd_id="SSNI-001").one()
        title.rating = None
        db.session.commit()
        genre_rows_before = db.session.query(TitleGenre).filter_by(title_id=title.id).count()
        maker_id_before = title.maker_id
    stats2 = javdb.fill_gaps(app, rate=0)
    assert stats2["filled"] == 1
    with app.app_context():
        title = db.session.query(Title).filter_by(dvd_id="SSNI-001").one()
        assert db.session.query(TitleGenre).filter_by(title_id=title.id).count() == genre_rows_before
        assert title.maker_id == maker_id_before


def test_fill_gaps_blocks_on_403(app, monkeypatch):
    from app.db import db
    from app.models import ScrapeTask

    with app.app_context():
        _seed_title(app, "SSNI-001")
        _seed_title(app, "SSNI-002")

    def _blocked(code, **kwargs):
        raise javdb.JavdbBlocked("403")

    monkeypatch.setattr(javdb, "_fetch_impl", _blocked)
    stats = javdb.fill_gaps(app, rate=0)

    assert stats["blocked"] is True
    assert stats["filled"] == 0
    assert stats["errors"] and "熔断" in stats["errors"][0]
    with app.app_context():
        failed = db.session.query(ScrapeTask).filter_by(status="failed").count()
        assert failed == 1  # 只尝试了第一条就熔断


def test_fill_gaps_records_failed_tasks(app, monkeypatch):
    from app.db import db
    from app.models import ScrapeTask

    with app.app_context():
        _seed_title(app, "SSNI-001")
        _seed_title(app, "SSNI-002")

    calls = {"n": 0}

    def _flaky(code, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        raise RuntimeError("db exploded")

    monkeypatch.setattr(javdb, "_fetch_impl", _flaky)
    stats = javdb.fill_gaps(app, rate=0)

    assert stats["no_result"] == 1
    assert stats["failed"] == 1
    assert len(stats["errors"]) == 1
    with app.app_context():
        statuses = sorted(task.status for task in db.session.query(ScrapeTask).all())
        assert statuses == ["failed", "failed"]  # 契约：未命中/解析失败统一记 failed


def test_fill_gaps_writes_title_zh_only_when_empty(app, monkeypatch):
    from app.db import db
    from app.models import Title

    zh_title = "大亂交解禁！24根肉棒猛肏到噴精25連發 超亂交特別版 葵"
    with app.app_context():
        empty = _seed_title(app, "SSNI-001")
        already = _seed_title(app, "SSNI-002")
        already.title_zh = "人工中文标题"
        db.session.commit()

    monkeypatch.setattr(
        javdb, "_fetch_impl", lambda code, **kwargs: _fake_detail(code=code, title_zh=zh_title)
    )
    stats = javdb.fill_gaps(app, rate=0)

    assert stats["title_zh_filled"] == 1  # 只写空的那条
    with app.app_context():
        filled = db.session.query(Title).filter_by(dvd_id="SSNI-001").one()
        kept = db.session.query(Title).filter_by(dvd_id="SSNI-002").one()
        assert filled.title_zh == zh_title
        assert kept.title_zh == "人工中文标题"  # 已有中文不覆盖

    # 幂等：再跑一轮 title_zh_filled 应为 0（都已填过）
    with app.app_context():
        target = db.session.query(Title).filter_by(dvd_id="SSNI-001").one()
        target.rating = None  # 制造缺口，让它再被选中
        kept = db.session.query(Title).filter_by(dvd_id="SSNI-002").one()
        kept.rating = None
        db.session.commit()
    again = javdb.fill_gaps(app, rate=0)
    assert again["filled"] == 2
    assert again["title_zh_filled"] == 0


def test_fill_gaps_manual_lock_title_zh_untouched(app, monkeypatch):
    from app.db import db
    from app.models import Title

    with app.app_context():
        locked = _seed_title(app, "SSNI-003", manual_lock=True)
        open_title = _seed_title(app, "SSNI-004")

    monkeypatch.setattr(
        javdb, "_fetch_impl", lambda code, **kwargs: _fake_detail(code=code, title_zh="JavDB中文标题")
    )
    stats = javdb.fill_gaps(app, rate=0)

    assert stats["title_zh_filled"] == 1  # 只有非锁定的那条
    with app.app_context():
        assert db.session.query(Title).filter_by(dvd_id="SSNI-003").one().title_zh is None
        assert db.session.query(Title).filter_by(dvd_id="SSNI-004").one().title_zh == "JavDB中文标题"


def test_fill_gaps_only_codes_and_manual_lock(app, monkeypatch):
    from app.db import db

    with app.app_context():
        _seed_title(app, "SSNI-001")
        _seed_title(app, "IPX-001")
        _seed_title(app, "MIDE-001", manual_lock=True)

    seen = []

    def _record(code, **kwargs):
        seen.append(code)
        return _fake_detail(code=code)

    monkeypatch.setattr(javdb, "_fetch_impl", _record)
    stats = javdb.fill_gaps(app, rate=0, only_codes=["SSNI-001"], limit=10)

    assert seen == ["SSNI-001"]
    assert stats["considered"] == 1 and stats["filled"] == 1

    seen.clear()
    stats = javdb.fill_gaps(app, rate=0)
    assert "MIDE-001" not in seen  # manual_lock 跳过
    assert stats["skipped_locked"] >= 1


def test_fill_gaps_limit(app, monkeypatch):
    with app.app_context():
        for code in ("SSNI-001", "SSNI-002", "SSNI-003"):
            _seed_title(app, code)

    seen = []

    def _record(code, **kwargs):
        seen.append(code)
        return _fake_detail(code=code)

    monkeypatch.setattr(javdb, "_fetch_impl", _record)
    stats = javdb.fill_gaps(app, rate=0, limit=2)
    assert stats["considered"] == 2 and len(seen) == 2


# ---------------------------------------------------------------------------
# AppSetting 域名持久化
# ---------------------------------------------------------------------------
def test_domain_setting_roundtrip(app):
    from app.db import db
    from app.models import AppSetting

    with app.app_context():
        assert javdb._stored_domain() is None
        javdb._store_domain("https://javdb580.com/")
        assert javdb._stored_domain() == "https://javdb580.com"
        # 幂等 upsert
        javdb._store_domain("https://javdb.com")
        assert db.session.query(AppSetting).count() == 1
        assert javdb._stored_domain() == "https://javdb.com"


def test_domain_rejects_non_javdb_hosts():
    assert javdb._page_domain('<html><body data-domain="https://parklogic.example"></body></html>') is None
    assert javdb._page_domain('<html><body data-domain="https://javdb580.com"></body></html>') == "https://javdb580.com"


# ---------------------------------------------------------------------------
# 403 冷却持久化（Lead 追加）
# ---------------------------------------------------------------------------
def test_cooldown_helpers_roundtrip(app):
    with app.app_context():
        javdb._clear_cooldown()
        assert javdb.cooldown_remaining() == 0
        assert javdb._set_cooldown(120) == 120
        remaining = javdb.cooldown_remaining()
        assert 0 < remaining <= 120
        javdb._clear_cooldown()
        assert javdb.cooldown_remaining() == 0


def test_fill_gaps_sets_and_honors_cooldown_after_403(app, monkeypatch):
    with app.app_context():
        _seed_title(app, "SSNI-001")
        _seed_title(app, "SSNI-002")
        javdb._clear_cooldown()

    calls = {"n": 0}

    def _blocked(code, **kwargs):
        calls["n"] += 1
        raise javdb.JavdbBlocked("403")

    monkeypatch.setattr(javdb, "_fetch_impl", _blocked)
    stats = javdb.fill_gaps(app, rate=0)
    assert stats["blocked"] is True
    assert stats["cooldown_remaining_s"] > 0
    with app.app_context():
        assert javdb.cooldown_remaining() > 0

    # 冷却期内再跑：不发请求，直接 blocked
    calls["n"] = 0
    stats2 = javdb.fill_gaps(app, rate=0)
    assert stats2["blocked"] is True
    assert stats2["cooldown_remaining_s"] > 0
    assert calls["n"] == 0
    assert any("冷却" in e for e in stats2["errors"])


def test_fill_gaps_runs_after_cooldown_expired(app, monkeypatch):
    from app.db import db
    from app.models import AppSetting

    with app.app_context():
        _seed_title(app, "SSNI-001")
        javdb._set_cooldown(60)
        row = db.session.get(AppSetting, javdb.BLOCKED_UNTIL_KEY)
        row.value = (javdb._now() - dt.timedelta(seconds=5)).isoformat()
        db.session.commit()
        assert javdb.cooldown_remaining() == 0

    seen = {"n": 0}

    def _fetch(code, **kwargs):
        seen["n"] += 1
        return None

    monkeypatch.setattr(javdb, "_fetch_impl", _fetch)
    stats = javdb.fill_gaps(app, rate=0)
    assert seen["n"] == 1
    assert stats["blocked"] is False
