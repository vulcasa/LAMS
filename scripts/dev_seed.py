"""本地开发用假数据：建 NAS/磁带卷、影片、文件、封面、少量字幕。

    python -m scripts.dev_seed [--data-dir ./data] [--reset]
"""
import argparse
import random
import shutil
import sys
from datetime import date
from pathlib import Path

# 允许从仓库根直接跑
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402

SAMPLE = [
    # code, title_ja, title_zh, actress, maker, series, genres, release, mins, sub
    ("SSIS-001", "新人NO.1 STYLE 〇〇 AVデビュー", "新人NO.1 〇〇 AV出道", "三上悠亜", "S1", "NO.1 STYLE", ["单体", "出道"], date(2021, 4, 7), 120, True),
    ("SSNI-001", "交わる体液、濃密セックス", "交错的体液、浓密性爱", "三上悠亜", "S1", None, ["单体"], date(2017, 8, 13), 150, False),
    ("IPX-001", "伝説のAV女優 電撃移籍", "传说的AV女优 电击移籍", "桃乃木かな", "IdeaPocket", None, ["单体", "企划"], date(2017, 8, 11), 160, False),
    ("ABP-123", "敏感すぎる彼女と濃厚な時間", "与过于敏感的女友的浓密时光", "深田えいみ", "Prestige", "ABP", ["恋爱", "单体"], date(2019, 3, 2), 130, True),
    ("MIDE-001", "専属NO.1 究極のフェラチオ", "专属NO.1 究极口交", "高橋しょう子", "MOODYZ", None, ["口交", "单体"], date(2018, 6, 1), 140, False),
    ("JUL-001", "人妻、汗だく不倫", "人妻、大汗不伦", "葵つかさ", "Madonna", "人妻", ["人妻", "不伦"], date(2020, 1, 10), 150, True),
    ("STARS-001", "新人 専属デビュー", "新人 专属出道", "河北彩花", "SOD", None, ["单体", "出道"], date(2020, 5, 20), 125, False),
    ("SNIS-001", "No.1 Style 新人", "No.1 Style 新人", "葵つかさ", "S1", None, ["单体"], date(2015, 3, 5), 120, False),
    ("PRED-001", "はじめてのナマ中出し", "第一次生中出", "波多野結衣", "Premium", None, ["中出", "单体"], date(2016, 7, 7), 135, True),
    ("FSDSS-001", "新人 専属 デビュー", "新人 专属 出道", "河北彩花", "FALENO", None, ["单体", "出道"], date(2020, 8, 8), 120, False),
    ("MIAA-001", "巨乳のお姉さん", "巨乳姐姐", "石原莉奈", "MOODYZ", None, ["巨乳", "单体"], date(2019, 11, 11), 145, True),
    ("HND-001", "幼馴染とひと夏の思い出", "与青梅竹马的一个夏天", "涼森れむ", "Honzawa", None, ["恋爱", "单体"], date(2021, 7, 7), 130, False),
    ("CAWD-001", "新人 専属デビュー", "新人 专属出道", "桜もこ", "Kawaii", None, ["单体", "出道"], date(2020, 2, 2), 118, False),
    ("FC2-PPV-1234567", "個人撮影 素人 20歳", "个人拍摄 素人 20岁", None, None, None, ["素人", "个人拍摄"], date(2021, 9, 1), 45, True),
    ("HEYZO-1234", "激選 美少女", "精选 美少女", None, "HEYZO", None, ["单体"], date(2019, 5, 5), 60, False),
    ("259LUXU-1234", "ラグジュTV 素人", "LUXU 素人", None, "LUXU", None, ["素人"], date(2020, 10, 10), 75, False),
]

FEMALE_POOL = ["三上悠亜", "桃乃木かな", "深田えいみ", "高橋しょう子", "葵つかさ",
               "河北彩花", "波多野結衣", "石原莉奈", "涼森れむ", "桜もこ"]
MAKERS = ["S1", "IdeaPocket", "Prestige", "MOODYZ", "Madonna", "SOD", "FALENO", "Kawaii",
          "Premium", "Honzawa", "HEYZO", "LUXU"]
GENRES = ["单体", "出道", "巨乳", "人妻", "不伦", "中出", "口交", "素人", "恋爱", "企划",
          "个人拍摄", "天然", "美少女", "熟女"]


def make_cover(path: Path, code: str, seed: int):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    rnd = random.Random(seed)
    w, h = 300, 400
    img = Image.new("RGB", (w, h), (rnd.randint(16, 40), rnd.randint(18, 44), rnd.randint(26, 52)))
    d = ImageDraw.Draw(img)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=(30 + y // 8, 34 + y // 10, 46 + y // 12))
    d.rectangle([10, 10, w - 10, h - 10], outline=(70, 80, 100))
    d.text((22, h - 60), code, fill=(180, 200, 240))
    d.text((22, h - 40), "sample cover", fill=(110, 120, 140))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=82)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(Config.DATA_DIR))
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir)
    if args.reset and data_dir.exists():
        # 只清开发产物，保留体积大的 staging 库和 dump（r18dump.db / dump/）
        for f in data_dir.glob("avlib.db*"):
            f.unlink(missing_ok=True)
        for sub in ("media", "backups"):
            shutil.rmtree(data_dir / sub, ignore_errors=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    import os
    os.environ["AVLIB_DATA_DIR"] = str(data_dir)

    from app import create_app
    from app.db import db
    from app.models import (
        Actress, Asset, Genre, Label, LibraryFile, LibraryVolume, Maker,
        Series, Subtitle, Title, TitleActress, TitleGenre, utcnow,
    )

    app = create_app({"DATA_DIR": data_dir, "DB_PATH": data_dir / "avlib.db"})
    with app.app_context():
        db.drop_all()
        db.create_all()

        nas = LibraryVolume(kind="nas", ref=str(data_dir / "mock_nas"), name="NAS 主片库",
                            last_scan_status="ok", last_scan_at=utcnow())
        tapes = [
            LibraryVolume(kind="tape", ref="LTO6-0001", name="磁带 1", last_scan_status="ok", last_scan_at=utcnow()),
            LibraryVolume(kind="tape", ref="LTO6-0002", name="磁带 2", last_scan_status="ok", last_scan_at=utcnow()),
        ]
        db.session.add_all([nas, *tapes])
        db.session.flush()

        maker_map, genre_map, actress_map = {}, {}, {}
        for name in MAKERS:
            maker_map[name] = Maker(name_ja=name, name_en=name)
        for name in GENRES:
            genre_map[name] = Genre(genre_ja=name, genre_zh=name)
        for name in FEMALE_POOL:
            actress_map[name] = Actress(name_kanji=name)
        db.session.add_all([*maker_map.values(), *genre_map.values(), *actress_map.values()])
        db.session.flush()

        covers_dir = Path(app.config["COVER_DIR"])
        rnd = random.Random(42)
        for i, (code, tja, tzh, actress, maker, series, genres, rel, mins, sub) in enumerate(SAMPLE):
            mid = maker_map[maker].id if maker else None
            sid = None
            if series:
                s = Series(name_ja=series)
                db.session.add(s)
                db.session.flush()
                sid = s.id
            t = Title(
                dvd_id=code, title_ja=tja, title_zh=tzh, release_date=rel,
                runtime_mins=mins, maker_id=mid, series_id=sid,
                has_chinese_sub=sub, rating=round(rnd.uniform(3.6, 4.9), 2),
                rating_votes=rnd.randint(30, 3000), rating_source="javdb",
                source_dump_date="2026-10-20",
            )
            db.session.add(t)
            db.session.flush()
            if actress in actress_map:
                db.session.add(TitleActress(title_id=t.id, actress_id=actress_map[actress].id))
            for g in genres:
                db.session.add(TitleGenre(title_id=t.id, genre_id=genre_map[g].id))
            cover = covers_dir / f"{code}.jpg"
            make_cover(cover, code, i)
            if cover.exists():
                t.cover_local = f"covers/{cover.name}"

            # 文件分布：60% NAS，40% 磁带（部分同时存在）
            if rnd.random() < 0.75:
                db.session.add(LibraryFile(
                    volume_id=nas.id, rel_path=f"{maker or '其他'}/{code}/{code}.mp4",
                    name=f"{code}.mp4", size=rnd.randint(900, 8000) * 1024 * 1024,
                    code_guess=code, confidence=92, title_id=t.id,
                ))
            if rnd.random() < 0.5:
                tv = rnd.choice(tapes)
                db.session.add(LibraryFile(
                    volume_id=tv.id, rel_path=f"{code}.mp4", name=f"{code}.mp4",
                    size=rnd.randint(900, 8000) * 1024 * 1024,
                    code_guess=code, confidence=90, title_id=t.id,
                ))
            if sub:
                db.session.add(Subtitle(
                    title_id=t.id, lang="zh", format="srt",
                    local_path=f"subtitles/{code}.zh.srt", source="filename",
                    quality_flag="good",
                ))
        db.session.commit()

        print(f"seeded: {len(SAMPLE)} titles, "
              f"{LibraryFile.query.count()} files, "
              f"{LibraryVolume.query.count()} volumes -> {data_dir}")
        print(f"db: {app.config['DB_PATH']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
