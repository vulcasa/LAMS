"""中文字幕 L0/L1 服务测试（全 mock，不依赖网络）。

跑法（项目根 = av-archive/）::

    .venv/bin/python -m pytest tests/test_subtitle.py -q

真实素材（2026-10-26 实测，单次请求）：
* 迅雷字幕库 ``GET /oracle/subtitle?gcid=&cid=&name=SSNI-001`` 的原样返回
  （3 条候选，``duration`` 约 9,851,588 ms ≈ 164.2 min，见 ``XUNLEI_SSNI_001_JSON``）；
* SubtitleCat 搜索页结果行 / 详情页结构（中文只有 JS「Translate」按钮，没有直链）。
  命中率抽样（6 个番号）：SSNI-001→3、ABP-001→8/10、PRED-001→4/4 含番号；
  IPX-001/SSIS-001/STARS-001→0（STARS-001 有 1 条但不含番号，会被番号校验过滤）。

覆盖的绝不妥协项：
1. 候选必须含番号（模糊命中要滤掉）；
2. 时长差 >10% 丢弃（迅雷用接口 duration；SubtitleCat 用 srt 时间轴跨度）；
3. 网络失败/非 200/脏 JSON → 返回 []/None，不抛异常；
4. SubtitleCat 一律 ``quality_flag='machine'``。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 支持 pytest 直接跑（不依赖 cwd）
    sys.path.insert(0, str(ROOT))

from app.services import subtitle  # noqa: E402

# --- 迅雷字幕库真实返回（name=SSNI-001，2026-10-26 单次请求原样）--------------
XUNLEI_SSNI_001_JSON = r"""
{"code":0,"data":[{"gcid":"3E14709F650B1E49E836A436CD9D224B374C930B","cid":"D1B09C34668336627425AAF366FE2586A9002621","url":"https://subtitle.v.geilijiasu.com/D1/B0/D1B09C34668336627425AAF366FE2586A9002621.srt","ext":"srt","name":"SSNI001超级大乱交特别篇24巨根VS葵.srt","duration":9851588,"languages":["默认"],"source":0,"score":0,"fingerprintf_score":0,"extra_name":"（网友上传）","mt":2},{"gcid":"606278D02DF6B875A352E4C17134DBA98C0F911A","cid":"606278D02DF6B875A352E4C17134DBA98C0F911A","url":"https://subtitle.v.geilijiasu.com/60/62/606278D02DF6B875A352E4C17134DBA98C0F911A.srt","ext":"srt","name":"SSNI001超级大乱交特别篇24巨根VS葵.srt","duration":9853000,"languages":[""],"source":0,"score":0,"fingerprintf_score":0,"extra_name":"（网友上传）","mt":2},{"gcid":"F5BAD4B87D07F19D132891EA88836555A3B705FF","cid":"F5BAD4B87D07F19D132891EA88836555A3B705FF","url":"https://subtitle.v.geilijiasu.com/F5/BA/F5BAD4B87D07F19D132891EA88836555A3B705FF.srt","ext":"srt","name":"VN_FdtD5rBsnZgiEL_jeaGzoA1_SSNI001超级大乱交特别篇24巨根VS葵(1).srt","duration":9853000,"languages":[""],"source":0,"score":0,"fingerprintf_score":0,"extra_name":"（网友上传）","mt":2}],"result":"ok"}
"""

# --- SubtitleCat 真实搜索页裁剪（结果行结构）---------------------------------
SUBTITLECAT_SEARCH_HTML = (
    '<html><body><div class="subtitles"><div class="container"><div class="row"><div class="col-12">'
    '<table class="table sub-table"><thead><tr><th colspan="2">'
    '<h2 class="sec-title">5 subtitles found</h2></th></tr></thead><tbody>'
    '<tr><td><a href="subs/1366/ssni-001-unc-sep_2017-gb-nasty-ono_yuuko.html">'
    "ssni-001-unc-sep_2017-gb-nasty-ono_yuuko</a> (translated from Japanese)</td>"
    '<td class="sub-table__stars">&nbsp;</td><td class="sub-table__metric">'
    '<span class="sub-table__metric-label">Downloads</span></td></tr>'
    '<tr><td><a href="subs/275/other-movie.html">other-movie</a></td>'
    '<td>&nbsp;</td><td>&nbsp;</td></tr>'
    "</tbody></table></div></div></div></div></body></html>"
)

# SubtitleCat 详情页：真实 SSNI-001 页的中文是 JS 翻译按钮，没有直链（实测）；
# 下面分别造出「有中文直链」（假设结构，按同一页面模式）和「只有 Translate」两种。
SC_DETAIL_WITH_ZH_SRT = (
    '<html><body><div class="sub-single">'
    '<span><img src="/assets/flags/cn.png" alt="zh-CN" class="flag"></span>'
    "<span>Chinese (Simplified)</span>"
    '<span><a href="/subs/1366/ssni001-zh-CN.srt" class="green-link">Download</a></span>'
    "</div></body></html>"
)
SC_DETAIL_ONLY_TRANSLATE = (
    '<html><body><div class="sub-single">'
    '<span><img src="/assets/flags/cn.png" alt="zh-CN" class="flag"></span>'
    "<span>Chinese (Simplified)</span>"
    '<span><button id="zh-CN" class="yellow-link">Translate</button></span>'
    "</div>"
    '<div class="sub-single"><span>English</span>'
    '<span><a href="/subs/1366/ssni001-en.srt" class="green-link">Download</a></span>'
    "</div></body></html>"
)

SRT_160MIN = (
    "1\n00:00:01,000 --> 00:00:03,000\n你好\n\n"
    "2\n02:39:58,000 --> 02:40:00,000\n结尾\n"
)
SRT_120MIN = (
    "1\n00:00:01,000 --> 00:00:03,000\n你好\n\n"
    "2\n01:59:58,000 --> 02:00:00,000\n结尾\n"
)


# ---------------------------------------------------------------------------
# 假 session / 假响应
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, text="", status=200, url="https://example.invalid/", payload=None, content=None):
        self.text = text
        self.status_code = status
        self.url = url
        self.headers = {}
        self._payload = payload
        if content is None:
            content = text.encode("utf-8") if isinstance(text, str) else b""
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
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


def _xunlei_response():
    return FakeResponse(payload=json.loads(XUNLEI_SSNI_001_JSON))


@pytest.fixture(autouse=True)
def _clear_mocks(monkeypatch):
    yield


# ---------------------------------------------------------------------------
# app fixture
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


def _seed_title(app, dvd_id="SSNI-001", *, runtime_mins=160, has_chinese_sub=False):
    from app.db import db
    from app.models import Title

    title = Title(dvd_id=dvd_id, runtime_mins=runtime_mins, has_chinese_sub=has_chinese_sub)
    db.session.add(title)
    db.session.commit()
    return title


# ---------------------------------------------------------------------------
# 番号校验 / 时长校验
# ---------------------------------------------------------------------------
def test_text_has_code_accepts_standard_spellings():
    assert subtitle.text_has_code("SSNI001超级大乱交.srt", "SSNI-001")
    assert subtitle.text_has_code("SSNI-001-C.torrent", "SSNI-001")
    assert subtitle.text_has_code("ssni00001hhb.srt", "SSNI-001")  # DMM content 5 位补零
    assert subtitle.text_has_code("VN_x_SSNI001xxx(1).srt", "SSNI-001")


def test_text_has_code_rejects_other_numbers_and_junk():
    assert not subtitle.text_has_code("SSNI-011.srt", "SSNI-001")
    assert not subtitle.text_has_code("hhd800.com@ssis00001hhb.srt", "SSNI-001")
    assert not subtitle.text_has_code("", "SSNI-001")
    assert not subtitle.text_has_code("SSNI-001.srt", "")


def test_duration_ok_boundary():
    runtime = 160
    exact = runtime * 60 * 1000
    assert subtitle._duration_ok(runtime, exact)[0] is True
    assert subtitle._duration_ok(runtime, int(exact * 1.10))[0] is True    # 边界 10%
    assert subtitle._duration_ok(runtime, int(exact * 1.11))[0] is False   # 超 10% 丢弃
    assert subtitle._duration_ok(runtime, int(exact * 0.85))[0] is False
    assert subtitle._duration_ok(None, exact)[0] is False                  # 缺 Title 时长 → 宁缺毋滥
    assert subtitle._duration_ok(runtime, None)[0] is False


# ---------------------------------------------------------------------------
# 迅雷搜索
# ---------------------------------------------------------------------------
def test_search_xunlei_real_json_returns_code_matched_candidates():
    session = FakeSession(_xunlei_response())
    candidates = subtitle.search_xunlei("SSNI-001", session=session)

    assert len(candidates) == 3
    assert all(candidate.source == "xunlei" for candidate in candidates)
    assert all(candidate.lang == "zh" for candidate in candidates)
    assert all(candidate.code == "SSNI-001" for candidate in candidates)
    assert all(candidate.duration_ms and candidate.duration_ms > 9_000_000 for candidate in candidates)
    assert all(candidate.download_url.startswith("https://subtitle.v.geilijiasu.com/") for candidate in candidates)
    assert all(0 <= candidate.score <= 100 for candidate in candidates)
    # 接口参数与真实请求一致（空 gcid/cid + name=番号）
    assert session.calls[0][1]["params"] == {"gcid": "", "cid": "", "name": "SSNI-001"}
    # 命中标准紧凑写法 ssni001 → 加了分
    assert candidates[0].score > 60


def test_search_xunlei_filters_mismatched_names():
    session = FakeSession(_xunlei_response())
    assert subtitle.search_xunlei("IPX-001", session=session) == []


def test_search_xunlei_network_failures_return_empty():
    assert subtitle.search_xunlei("SSNI-001", session=FakeSession(ConnectionError("boom"))) == []
    assert subtitle.search_xunlei("SSNI-001", session=FakeSession(FakeResponse("err", status=503))) == []
    assert subtitle.search_xunlei("SSNI-001", session=FakeSession(FakeResponse("not json"))) == []
    assert subtitle.search_xunlei("SSNI-001", session=FakeSession(FakeResponse(payload={"code": 1, "result": "error"}))) == []
    assert subtitle.search_xunlei("", session=FakeSession()) == []


# ---------------------------------------------------------------------------
# SubtitleCat 搜索
# ---------------------------------------------------------------------------
def test_search_subtitlecat_parses_real_rows_and_filters_code():
    session = FakeSession(
        FakeResponse(
            SUBTITLECAT_SEARCH_HTML,
            url="https://www.subtitlecat.com/index.php?search=SSNI-001",
        )
    )
    candidates = subtitle.search_subtitlecat("SSNI-001", session=session)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source == "subtitlecat"
    assert candidate.download_url == (
        "https://www.subtitlecat.com/subs/1366/ssni-001-unc-sep_2017-gb-nasty-ono_yuuko.html"
    )
    assert candidate.duration_ms is None  # SubtitleCat 搜索页不提供时长
    assert candidate.score <= 50

    other = FakeSession(FakeResponse(SUBTITLECAT_SEARCH_HTML))
    assert subtitle.search_subtitlecat("IPX-001", session=other) == []


def test_search_subtitlecat_network_failures_return_empty():
    assert subtitle.search_subtitlecat("SSNI-001", session=FakeSession(ConnectionError("boom"))) == []
    assert subtitle.search_subtitlecat("SSNI-001", session=FakeSession(FakeResponse("x", status=403))) == []


# ---------------------------------------------------------------------------
# L0：文件名标记同步
# ---------------------------------------------------------------------------
def _seed_file(app, *, title, flags, confidence=95, rel_path="a/b.mp4"):
    from app.db import db
    from app.models import LibraryFile, LibraryVolume

    volume = db.session.query(LibraryVolume).first()
    if volume is None:
        volume = LibraryVolume(kind="nas", ref="/tmp/fake-nas")
        db.session.add(volume)
        db.session.commit()
    library_file = LibraryFile(
        volume_id=volume.id,
        rel_path=rel_path,
        name=Path(rel_path).name,
        code_guess=title.dvd_id,
        confidence=confidence,
        flags=flags,
        title_id=title.id,
    )
    db.session.add(library_file)
    db.session.commit()
    return library_file


def test_sync_filename_flags_marks_title(app):
    from app.db import db
    from app.models import Title

    with app.app_context():
        with_flag = _seed_title(app, "SSNI-001")
        without_flag = _seed_title(app, "SSNI-002")
        _seed_file(app, title=with_flag, flags="chinese_sub,hi_res", rel_path="SSNI-001-C.mp4")
        _seed_file(app, title=without_flag, flags="hi_res", rel_path="SSNI-002.mp4")

    stats = subtitle.sync_filename_flags(app)
    assert stats["files_scanned"] == 1
    assert stats["titles_seen"] == 1
    assert stats["titles_updated"] == 1
    assert stats["errors"] == []

    with app.app_context():
        assert db.session.query(Title).filter_by(dvd_id="SSNI-001").one().has_chinese_sub is True
        assert db.session.query(Title).filter_by(dvd_id="SSNI-002").one().has_chinese_sub is False

    # 幂等：再跑一次不再更新
    again = subtitle.sync_filename_flags(app)
    assert again["titles_updated"] == 0


def test_sync_filename_flags_min_confidence_filter(app):
    from app.db import db
    from app.models import Title

    with app.app_context():
        low = _seed_title(app, "SSNI-003")
        _seed_file(app, title=low, flags="chinese_sub", confidence=60, rel_path="x/SSNI-003.mp4")

    stats = subtitle.sync_filename_flags(app, min_confidence=90)
    assert stats["files_scanned"] == 0
    with app.app_context():
        assert db.session.query(Title).filter_by(dvd_id="SSNI-003").one().has_chinese_sub is False


# ---------------------------------------------------------------------------
# L1：fetch_best
# ---------------------------------------------------------------------------
def _good_candidate(code="SSNI-001", duration_ms=9_851_000):
    return subtitle.SubtitleCandidate(
        code=code,
        source="xunlei",
        title="SSNI001超级大乱交.srt",
        lang="zh",
        score=85,
        download_url="https://subtitle.example.invalid/ssni001.srt",
        referer=subtitle.XUNLEI_URL,
        duration_ms=duration_ms,
    )


def test_fetch_best_xunlei_success_writes_file_and_row(app, monkeypatch):
    from app.db import db
    from app.models import Subtitle

    monkeypatch.setattr(subtitle, "search_xunlei", lambda code, session=None: [_good_candidate()])
    session = FakeSession(FakeResponse(SRT_160MIN, url="https://subtitle.example.invalid/ssni001.srt"))

    with app.app_context():
        title = _seed_title(app, "SSNI-001")
        row = subtitle.fetch_best(app, title, session=session)

        assert row is not None
        assert row.id is not None
        assert row.source == "xunlei"
        assert row.quality_flag == "unknown"
        assert row.lang == "zh"
        assert row.format == "srt"
        path = Path(row.local_path)
        assert path.is_file()
        assert Path(app.config["SUBTITLE_DIR"]).resolve() in path.resolve().parents
        assert path.read_text(encoding="utf-8") == SRT_160MIN
        assert row.sha256 == hashlib.sha256(SRT_160MIN.encode("utf-8")).hexdigest()
        assert row.source_url == "https://subtitle.example.invalid/ssni001.srt"
        assert db.session.query(Subtitle).count() == 1
        row_id = row.id

        # 幂等：再跑一次仍只有一行，返回同一行
        again = subtitle.fetch_best(
            app,
            title,
            session=FakeSession(FakeResponse(SRT_160MIN, url="https://subtitle.example.invalid/ssni001.srt")),
        )
        assert again is not None and again.id == row_id
        assert db.session.query(Subtitle).count() == 1


def test_fetch_best_rejects_duration_mismatch(app, monkeypatch):
    from app.db import db
    from app.models import Subtitle

    monkeypatch.setattr(
        subtitle, "search_xunlei", lambda code, session=None: [_good_candidate(duration_ms=120 * 60 * 1000)]
    )
    monkeypatch.setattr(subtitle, "search_subtitlecat", lambda code, session=None: [])
    session = FakeSession()  # 不应该发生任何下载

    with app.app_context():
        title = _seed_title(app, "SSNI-001")
        assert subtitle.fetch_best(app, title, session=session) is None
        assert db.session.query(Subtitle).count() == 0
        assert session.calls == []
        subtitles_dir = Path(app.config["SUBTITLE_DIR"])
        assert not list(subtitles_dir.glob("*.srt"))


def test_fetch_best_rejects_when_runtime_missing(app, monkeypatch):
    monkeypatch.setattr(subtitle, "search_xunlei", lambda code, session=None: [_good_candidate()])
    monkeypatch.setattr(subtitle, "search_subtitlecat", lambda code, session=None: [])

    with app.app_context():
        title = _seed_title(app, "SSNI-002", runtime_mins=None)
        assert subtitle.fetch_best(app, title, session=FakeSession()) is None


def test_fetch_best_rejects_non_subtitle_payload(app, monkeypatch):
    from app.db import db
    from app.models import Subtitle

    monkeypatch.setattr(subtitle, "search_xunlei", lambda code, session=None: [_good_candidate()])
    session = FakeSession(FakeResponse("<html>404</html>", url="https://subtitle.example.invalid/ssni001.srt"))

    with app.app_context():
        title = _seed_title(app, "SSNI-003")
        assert subtitle.fetch_best(app, title, session=session) is None
        assert db.session.query(Subtitle).count() == 0


def test_fetch_best_dry_run_has_no_side_effects(app, monkeypatch):
    from app.db import db
    from app.models import Subtitle

    monkeypatch.setattr(subtitle, "search_xunlei", lambda code, session=None: [_good_candidate()])
    session = FakeSession()  # dry_run 不下载

    with app.app_context():
        title = _seed_title(app, "SSNI-004")
        row = subtitle.fetch_best(app, title, session=session, dry_run=True)
        assert row is not None
        assert row.id is None
        assert row.quality_flag == "unknown"
        assert row.local_path is None
        assert db.session.query(Subtitle).count() == 0
        assert session.calls == []
        assert not list(Path(app.config["SUBTITLE_DIR"]).glob("*"))


def test_fetch_best_subtitlecat_fallback_uses_srt_span_check(app, monkeypatch):
    from app.db import db
    from app.models import Subtitle

    monkeypatch.setattr(subtitle, "search_xunlei", lambda code, session=None: [])
    candidate = subtitle.SubtitleCandidate(
        code="SSNI-001",
        source="subtitlecat",
        title="ssni-001-unc-sep",
        lang="zh",
        score=50,
        download_url="https://www.subtitlecat.com/subs/1366/x.html",
        referer="https://www.subtitlecat.com/index.php?search=SSNI-001",
    )
    monkeypatch.setattr(subtitle, "search_subtitlecat", lambda code, session=None: [candidate])
    session = FakeSession(
        FakeResponse(SC_DETAIL_WITH_ZH_SRT, url="https://www.subtitlecat.com/subs/1366/x.html"),
        FakeResponse(SRT_160MIN, url="https://www.subtitlecat.com/subs/1366/ssni001-zh-CN.srt"),
    )

    with app.app_context():
        title = _seed_title(app, "SSNI-005")
        row = subtitle.fetch_best(app, title, session=session)
        assert row is not None
        assert row.source == "subtitlecat"
        assert row.quality_flag == "machine"  # 契约：SubtitleCat 一律 machine
        assert Path(row.local_path).is_file()
        assert db.session.query(Subtitle).count() == 1


def test_fetch_best_subtitlecat_without_direct_zh_link_returns_none(app, monkeypatch):
    """真实 SSNI-001 页只有 JS「Translate」按钮 → 不能伪造中文 srt，直接放弃。"""
    from app.db import db
    from app.models import Subtitle

    monkeypatch.setattr(subtitle, "search_xunlei", lambda code, session=None: [])
    candidate = subtitle.SubtitleCandidate(
        code="SSNI-001",
        source="subtitlecat",
        title="ssni-001-unc-sep",
        lang="zh",
        score=50,
        download_url="https://www.subtitlecat.com/subs/1366/x.html",
        referer=None,
    )
    monkeypatch.setattr(subtitle, "search_subtitlecat", lambda code, session=None: [candidate])
    session = FakeSession(
        FakeResponse(SC_DETAIL_ONLY_TRANSLATE, url="https://www.subtitlecat.com/subs/1366/x.html"),
    )

    with app.app_context():
        title = _seed_title(app, "SSNI-006")
        assert subtitle.fetch_best(app, title, session=session) is None
        assert db.session.query(Subtitle).count() == 0


def test_fetch_best_never_raises_on_session_errors(app, monkeypatch):
    monkeypatch.setattr(subtitle, "search_xunlei", lambda code, session=None: [_good_candidate()])
    session = FakeSession(ConnectionError("boom"))

    with app.app_context():
        title = _seed_title(app, "SSNI-007")
        assert subtitle.fetch_best(app, title, session=session) is None
