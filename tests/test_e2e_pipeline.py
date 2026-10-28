"""端到端流水线测试：扫描合成语料 → 物化真实 r18 元数据。

依赖两个本机资产（缺失时自动 skip，CI 上不会红）：
- av-archive/data/r18dump.db（由 scripts/import_dump 生成，1.7GB）
- /tmp/av-archive-corpus/nas（由 scripts/make_corpus.py 生成）
"""
from pathlib import Path

import pytest

from app import create_app
from app.config import Config
from app.db import db
from app.models import LibraryFile, Title

REPO = Path(__file__).resolve().parents[1]
DUMP_DB = REPO / "data" / "r18dump.db"
CORPUS = Path("/tmp/av-archive-corpus/nas")

pytestmark = pytest.mark.skipif(
    not (DUMP_DB.exists() and CORPUS.exists()),
    reason="需要 data/r18dump.db 与 /tmp/av-archive-corpus/nas",
)


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "DATA_DIR": tmp_path,
            "DB_PATH": tmp_path / "e2e.db",
            "MEDIA_DIR": tmp_path / "media",
            "COVER_DIR": tmp_path / "media" / "covers",
            "STILL_DIR": tmp_path / "media" / "stills",
            "AVATAR_DIR": tmp_path / "media" / "avatars",
            "SUBTITLE_DIR": tmp_path / "media" / "subtitles",
            "THUMB_DIR": tmp_path / "media" / "thumbs",
            "DUMP_DIR": tmp_path / "dump",
            "WORKER_ENABLED": False,
        }
    )
    yield app
    with app.app_context():
        db.session.remove()


def test_scan_then_materialize(app):
    from app.services.materialize import materialize
    from app.services.scanner import scan_nas

    with app.app_context():
        st = scan_nas(app, str(CORPUS), volume_name="corpus")
        assert st.videos > 250, st
        assert st.with_code > 250, st
        assert st.no_code <= 40, st
        assert st.deleted == 0

        # 增量：重扫不新增、不重复
        st2 = scan_nas(app, str(CORPUS))
        assert st2.new == 0 and st2.updated == 0 and st2.deleted == 0

        ms = materialize(app, dump_db_path=str(DUMP_DB), min_confidence=90)
        assert ms.titles_created > 50, ms
        assert ms.files_linked > 50, ms

        t = Title.query.filter(Title.cover_url.isnot(None)).order_by(Title.id).first()
        assert t is not None
        assert t.title_ja or t.title_en
        assert (t.cover_url or "").startswith("https://pics.dmm.co.jp/")
        assert t.actresses.count() >= 1

        linked = LibraryFile.query.filter(LibraryFile.title_id.isnot(None)).count()
        assert linked == ms.files_linked

        # 幂等
        ms2 = materialize(app, dump_db_path=str(DUMP_DB), min_confidence=90)
        assert ms2.titles_created == 0
        assert Title.query.filter_by(dvd_id=t.dvd_id).count() == 1


def test_low_confidence_not_materialized(app):
    from app.services.materialize import materialize
    from app.services.scanner import scan_nas

    with app.app_context():
        scan_nas(app, str(CORPUS))
        materialize(app, dump_db_path=str(DUMP_DB), min_confidence=90)
        # 低于"待确认阈值"(70)的文件绝不能被关联；
        # 70-89 的子文件可以跟随同番号的高置信视频一起挂上 title（例如 cover.jpg 靠父目录）
        low = (
            LibraryFile.query.filter(
                LibraryFile.code_guess.isnot(None),
                LibraryFile.confidence < Config.CODE_REVIEW_MIN,
                LibraryFile.title_id.isnot(None),
            ).count()
        )
        assert low == 0
        # 只有低置信度文件的番号不应该产生 Title
        from app.db import db as _db
        risky = (
            _db.session.query(LibraryFile.code_guess, _db.func.max(LibraryFile.confidence))
            .filter(LibraryFile.code_guess.isnot(None))
            .group_by(LibraryFile.code_guess)
            .having(_db.func.max(LibraryFile.confidence) < Config.CODE_AUTO_MIN)
            .all()
        )
        risky_codes = {c for c, _ in risky}
        assert not (set(t.dvd_id for t in Title.query.all()) & risky_codes)
