"""按文件夹聚合的合集页 + 待确认里的合集归属。"""
from app import create_app
from app.db import db
from app.models import LibraryFile, LibraryVolume
from app.services import review as review_service
from app.services.collections import collection_of, get_collection, list_collections, Collection


def test_collection_of_path():
    assert collection_of("蚊香社无码流出/合集重编码/清城ゆき/20170621.mkv") == "蚊香社无码流出"
    assert collection_of("movies/foo/bar.mp4") == "foo"
    assert collection_of("single.mp4") is None


def test_collections_page_and_detail(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path / "data",
            "DB_PATH": tmp_path / "data" / "test.db",
            "WORKER_ENABLED": False,
        }
    )
    with app.app_context():
        volume = LibraryVolume(kind="tape", ref="FB1186")
        db.session.add(volume)
        db.session.flush()
        for rel in (
            "蚊香社无码流出/合集重编码/清城ゆき/20170621.mkv",
            "蚊香社无码流出/合集重编码/清城ゆき/20170622.mkv",
        ):
            db.session.add(
                LibraryFile(
                    volume_id=volume.id,
                    rel_path=rel,
                    name=rel.rsplit("/", 1)[-1],
                    size=100,
                    code_guess=None,
                    confidence=0,
                )
            )
        db.session.commit()

        items = list_collections(app)
        assert len(items) == 1
        assert items[0].name == "蚊香社无码流出"
        assert items[0].files == 2
        info, rows = get_collection(app, "蚊香社无码流出")
        assert info.files == 2 and len(rows) == 2

        client = app.test_client()
        assert client.get("/collections").status_code == 200
        page = client.get("/collections/蚊香社无码流出")
        assert page.status_code == 200
        assert "清城ゆき" in page.get_data(as_text=True)

        groups = review_service.pending_items(app)
        assert groups[0].collection == "蚊香社无码流出"
