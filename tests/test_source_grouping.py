"""无番号文件按来源分类（CosplayJAV / FC2 / 欧美厂牌）测试。"""
from app import create_app
from app.db import db
from app.models import LibraryFile, LibraryVolume
from app.services import review as review_service
from app.services.source_classify import detect_source


def test_detect_source_labels():
    assert detect_source("cosplayjav/some/scene.mp4")[1] == "CosplayJAV"
    assert detect_source("FC2-PPV-1234567/foo.mp4")[1] == "FC2-PPV"
    assert detect_source("Blacked/2024/scene.mp4")[1] == "欧美 · Blacked"
    assert detect_source("Marc Dorcel/foo.mp4")[1] == "欧美 · Marc Dorcel（啄木鸟）"
    assert detect_source("Vixen/foo.mp4")[1] == "欧美 · Vixen"
    assert detect_source("random/foo.mp4")[1] == "其他无番号"


def test_pending_no_code_split_by_source(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path / "data",
            "DB_PATH": tmp_path / "data" / "test.db",
            "WORKER_ENABLED": False,
        }
    )
    with app.app_context():
        volume = LibraryVolume(kind="nas", ref="/mnt/nas")
        db.session.add(volume)
        db.session.flush()
        paths = [
            "cosplayjav/CPJ001/scene.mp4",
            "Blacked/2024/scene.mp4",
            "Marc Dorcel/foo.mp4",
            "Vixen/bar.mp4",
            "random/baz.mp4",
        ]
        for index, rel in enumerate(paths):
            db.session.add(
                LibraryFile(
                    volume_id=volume.id,
                    rel_path=rel,
                    name=rel.rsplit("/", 1)[-1],
                    size=10,
                    code_guess=None,
                    confidence=0,
                )
            )
        db.session.commit()

        groups = review_service.pending_items(app)
        labels = {g.source_label for g in groups}
        assert labels == {
            "CosplayJAV",
            "欧美 · Blacked",
            "欧美 · Marc Dorcel（啄木鸟）",
            "欧美 · Vixen",
            "其他无番号",
        }
        assert all(g.code_guess is None for g in groups)


def test_cosplayjav_files_in_different_folders_are_separate_groups(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATA_DIR": tmp_path / "data",
            "DB_PATH": tmp_path / "data" / "test.db",
            "WORKER_ENABLED": False,
        }
    )
    with app.app_context():
        volume = LibraryVolume(kind="nas", ref="/mnt/nas")
        db.session.add(volume)
        db.session.flush()
        paths = [
            "cosplayjav/site/film-a/a.mp4",
            "cosplayjav/site/film-b/b.mp4",
            "cosplayjav/site/film-b/b-extra.mp4",
        ]
        for rel in paths:
            db.session.add(
                LibraryFile(
                    volume_id=volume.id,
                    rel_path=rel,
                    name=rel.rsplit("/", 1)[-1],
                    size=10,
                    code_guess=None,
                    confidence=0,
                )
            )
        db.session.commit()

        groups = review_service.pending_items(app)
        by_folder = {g.folder: g for g in groups}
        assert set(by_folder) == {"cosplayjav/site/film-a", "cosplayjav/site/film-b"}
        assert by_folder["cosplayjav/site/film-a"].files == 1
        assert by_folder["cosplayjav/site/film-b"].files == 2
        # 总览分组数要和列表一致
        summary = review_service.pending_summary(app)
        assert summary.pending_groups == len(groups) == 2
