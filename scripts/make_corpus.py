#!/usr/bin/env python3
"""生成合成语料目录树（两套：NAS 下载站风格 + LTO 磁带归档风格）。

用途：给扫描器 / 归组 / 刮削队列做端到端测试。**只往 /tmp 写，不写进仓库。**

    python3 scripts/make_corpus.py                       # 默认 /tmp/av-archive-corpus，每套 300 个视频
    python3 scripts/make_corpus.py --out /tmp/x --per-tree 300 --seed 42 --clean

产物：
    <out>/nas/...            NAS 风格：JAV/<maker>/<番号>/<番号>-C.mp4、downloads/ 里各种脏名
    <out>/tape/LTO6-A001/…   磁带风格：barcode/movies/<year>/<番号>.mp4，含 VIDEO_TS 老结构
    <out>/manifest.json      rel_path -> expect_code / expect_flags / kind，供扫描器测试断言

设计约束：
- 每个树正好 `--per-tree` 个**视频**文件（默认 300），其中 10% 是**故意无法识别**的垃圾名。
- 所有可识别文件的 expect_code 必须能被 app/services/code_parser.py 复现（tests 里有校验）。
- 纯标准库，确定性随机（固定 seed 两次生成完全一致）。
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# 名字素材
# ---------------------------------------------------------------------------

# 常用 DMM 系厂牌（生成 `<PREFIX>-<3位数字>` 这种干净番号）
MAIN_PREFIXES = [
    "SSIS", "SSNI", "SONE", "MIDE", "MIAA", "IPX", "ABP", "JUQ", "PRED",
    "STARS", "DASD", "SNIS", "SHKD", "VENU", "WANZ", "ADN", "MIMK",
    "FSDSS", "DLDSS", "HND", "IPZZ", "NIMA", "MEYD", "REBD",
]

# 厂牌 -> 目录名（NAS 里常见的 maker 目录）
MAKER_DIRS = {
    "SSIS": "S1", "SSNI": "S1", "SONE": "S1", "SNIS": "S1", "IPZZ": "S1",
    "MIDE": "MOODYZ", "MIAA": "MOODYZ", "JUQ": "MOODYZ",
    "IPX": "IDEA_POCKET", "ABP": "PRESTIGE", "PRED": "PREMIUM",
    "STARS": "SOD", "DASD": "DAS", "SHKD": "ATTACKERS", "VENU": "VENUS",
    "WANZ": "WANZ", "ADN": "ADN", "MIMK": "MIMK",
    "FSDSS": "FALENO", "DLDSS": "DALETT", "HND": "HONNAKA", "NIMA": "NIMA",
    "MEYD": "MEYD", "REBD": "REBD",
}

# (后缀, 该后缀应带的 flags)
FLAG_SUFFIXES: List[Tuple[str, Tuple[str, ...]]] = [
    ("", ()),
    ("", ()),
    ("", ()),
    ("-C", ("chinese_sub",)),
    ("-CH", ("chinese_sub",)),
    ("-UC", ("chinese_sub", "uncensored")),
    ("-U", ("uncensored",)),
    ("-4K", ("hi_res",)),
    ("-1080p", ("hi_res",)),
    ("-FHD", ("hi_res",)),
    ("-LEAK", ("uncensored",)),
    ("-CD1", ("part",)),
    ("-PART2", ("part",)),
    ("-A", ("part",)),
    ("-B", ("part",)),
]

# 故意无法识别的名字（分辨率/编码/相机/日期/纯中文，解析器必须返回 None）
GARBAGE_NAMES = [
    "1080P.mp4",
    "2160p.UHD.mkv",
    "x264.mkv",
    "h264.mp4",
    "hevc.mkv",
    "aac.m4a",
    "web-dl.mkv",
    "FHD.mp4",
    "UHD.mp4",
    "PART1.mp4",
    "CD1.avi",
    "IMG_1234.MOV",
    "DSC01234.JPG",
    "MVI_0001.MP4",
    "VTS_01_1.VOB",
    "random_movie_2024.mkv",
    "movie2024.mp4",
    "holiday_2023.mp4",
    "未命名.mp4",
    "某某老师合集第三部.mkv",
    "sample.mkv",
    "trailer.mp4",
    "clip (2).mp4",
    "1920x1080.mp4",
    "2024-01-15.mp4",
    "file001.mp4",
    "temp_1080p.mkv",
]

# 视频扩展名池（扫描器要处理的常见容器）
VIDEO_EXTS = [".mp4"] * 8 + [".mkv"] * 4 + [".avi", ".wmv", ".ts", ".rmvb", ".iso", ".m4v"]


# ---------------------------------------------------------------------------
# 小工具（脚本自包含，不 import app.*）
# ---------------------------------------------------------------------------

def _pad(num: int, width: int) -> str:
    n = str(num).lstrip("0") or "0"
    return n.zfill(width) if len(n) < width else n


def _garbage_name(i: int) -> str:
    """取第 i 个垃圾名；用完一轮后加 ` (2)`/` (3)` 后缀保证 rel_path 唯一（仍无法识别）。"""
    base = GARBAGE_NAMES[i % len(GARBAGE_NAMES)]
    round_no = i // len(GARBAGE_NAMES)
    if round_no == 0:
        return base
    p = Path(base)
    return "%s (%d)%s" % (p.stem, round_no + 1, p.suffix)


def _spec(filename: str, code: str, flags: Sequence[str] = ()) -> Dict:
    return {"filename": filename, "code": code, "flags": tuple(flags)}


# ---------------------------------------------------------------------------
# 可识别名字的生成器：每个返回 {"filename","code","flags"}
# ---------------------------------------------------------------------------

def _gen_plain(rng: random.Random) -> Dict:
    """干净写法：SSIS-001.mp4 / SSIS_001-C.mp4（带 maker 目录）。"""
    prefix = rng.choice(MAIN_PREFIXES)
    num = rng.randint(1, 999)
    code = "%s-%03d" % (prefix, num)
    suffix, flags = rng.choice(FLAG_SUFFIXES)
    sep = rng.choice(["-", "_", "-"])
    stem = "%s%s%s" % (prefix, sep, "%03d" % num)
    ext = rng.choice(VIDEO_EXTS)
    maker = MAKER_DIRS.get(prefix, "MISC")
    return {
        **_spec(stem + suffix + ext, code, flags),
        "dir": ["JAV", maker, code],
    }


def _gen_glued(rng: random.Random) -> Dict:
    """下载站粘连写法：ssis001.mp4 / ssis001c.mp4 / SSIS-001-C.srt 等。"""
    prefix = rng.choice(MAIN_PREFIXES)
    num = rng.randint(1, 999)
    code = "%s-%03d" % (prefix, num)
    style = rng.random()
    if style < 0.5:
        stem = "%s%03d" % (prefix.lower(), num)
        flags: Tuple[str, ...] = ()
    elif style < 0.7:
        stem = "%s%03d" % (prefix.lower(), num)
        flags = ("chinese_sub",)
        stem += "c"
    elif style < 0.85:
        stem = "%s_%03d" % (prefix, num)
        flags = ()
    else:
        stem = "%s %03d" % (prefix, num)
        flags = ()
    ext = rng.choice(VIDEO_EXTS)
    return {**_spec(stem + ext, code, flags), "dir": ["downloads"]}


def _gen_ad_prefix(rng: random.Random) -> Dict:
    """广告站前缀：hhd800.com@ssis00001hhb.mp4 / [ThZu.Cc]SSNI-001.mp4 / www.98t.la@ipx001.mp4。"""
    prefix = rng.choice(MAIN_PREFIXES)
    num = rng.randint(1, 999)
    code = "%s-%03d" % (prefix, num)
    style = rng.randrange(3)
    if style == 0:
        name = "hhd800.com@%s%05dhhb" % (prefix.lower(), num)
        flags: Tuple[str, ...] = ()
    elif style == 1:
        name = "[ThZu.Cc]%s-%03d" % (prefix, num)
        flags = ()
    else:
        name = "www.98t.la@%s%03d" % (prefix.lower(), num)
        flags = ()
    ext = rng.choice(VIDEO_EXTS)
    return {**_spec(name + ext, code, flags), "dir": ["downloads", "2024"]}


def _gen_cn_dirty(rng: random.Random) -> Dict:
    """中文字幕/无码标记的中文命名：中文字幕SSIS-001.mp4 / 無碼-IPX-001-UC.mp4 / SSIS-001 中字.mkv。"""
    prefix = rng.choice(MAIN_PREFIXES)
    num = rng.randint(1, 999)
    code = "%s-%03d" % (prefix, num)
    style = rng.randrange(4)
    if style == 0:
        name = "中文字幕%s-%03d" % (prefix, num)
        flags: Tuple[str, ...] = ("chinese_sub",)
    elif style == 1:
        name = "無碼-%s-%03d-UC" % (prefix, num)
        flags = ("chinese_sub", "uncensored")
    elif style == 2:
        name = "%s-%03d 中字" % (prefix, num)
        flags = ("chinese_sub",)
    else:
        name = "%s-%03d-4K-C" % (prefix, num)
        flags = ("hi_res", "chinese_sub")
    ext = rng.choice(VIDEO_EXTS)
    return {**_spec(name + ext, code, flags), "dir": ["中文"]} if style else {
        **_spec(name + ext, code, flags), "dir": ["JAV", "中文字幕"]
    }


def _gen_special(rng: random.Random) -> Dict:
    """特殊厂牌：FC2 / HEYZO / SIRO / Tokyo-Hot / 1pondo / 10musume / Caribbean / 素人数字前缀。"""
    kind = rng.choice(
        ["fc2", "heyzo", "siro", "tokyo", "pondo", "musume", "carib", "paco", "luxu", "mium", "gana", "t28"]
    )
    ext = rng.choice(VIDEO_EXTS)
    if kind == "fc2":
        ident = rng.randint(100000, 9999999)
        style = rng.randrange(3)
        if style == 0:
            name = "FC2-PPV-%d" % ident
        elif style == 1:
            name = "fc2ppv%d" % ident
        else:
            name = "FC2-%d" % ident
        return {**_spec(name + ext, "FC2-PPV-%d" % ident), "dir": ["FC2"]}
    if kind == "heyzo":
        num = rng.randint(1, 9999)
        style = rng.randrange(2)
        name = "HEYZO-%04d" % num if style == 0 else "heyzo_hd_%04d" % num
        flags = ("hi_res",) if style else ()
        return {**_spec(name + ext, "HEYZO-%04d" % num, flags), "dir": ["HEYZO"]}
    if kind == "siro":
        num = rng.randint(1, 9999)
        style = rng.randrange(2)
        name = "SIRO-%04d" % num if style == 0 else "siro%04d" % num
        return {**_spec(name + ext, "SIRO-%04d" % num), "dir": ["SIRO"]}
    if kind == "tokyo":
        num = rng.randint(1, 9999)
        letter = rng.choice(["n", "k"])
        name = "Tokyo-Hot %s%04d" % (letter, num)
        return {**_spec(name + ext, "TOKYO-HOT-%s%04d" % (letter.upper(), num)), "dir": ["Tokyo-Hot"]}
    if kind == "pondo":
        main, part = rng.randint(100000, 999999), rng.randint(1, 999)
        name = "1pondo %d_%03d" % (main, part)
        return {**_spec(name + ext, "1PONDO-%d_%03d" % (main, part)), "dir": ["素人", "1pondo"]}
    if kind == "musume":
        main, part = rng.randint(100000, 999999), rng.randint(1, 99)
        name = "10musume %d_%02d" % (main, part)
        return {**_spec(name + ext, "10MUSUME-%d_%02d" % (main, part)), "dir": ["素人", "10musume"]}
    if kind == "carib":
        main, part = rng.randint(100000, 999999), rng.randint(1, 999)
        style = rng.randrange(2)
        name = "Caribbean %d-%03d" % (main, part) if style == 0 else "carib-%d-%03d" % (main, part)
        return {**_spec(name + ext, "CARIB-%d-%03d" % (main, part)), "dir": ["素人", "Caribbean"]}
    if kind == "paco":
        main, part = rng.randint(100000, 999999), rng.randint(1, 999)
        name = "Pacopacomama %d_%03d" % (main, part)
        return {**_spec(name + ext, "PACOPACOMAMA-%d_%03d" % (main, part)), "dir": ["素人", "Pacopacomama"]}
    if kind == "luxu":
        num = rng.randint(1000, 9999)
        name = "259LUXU-%04d" % num
        return {**_spec(name + ext, "259LUXU-%04d" % num), "dir": ["素人"]}
    if kind == "mium":
        num = rng.randint(1, 999)
        name = "300MIUM-%03d" % num
        return {**_spec(name + ext, "300MIUM-%03d" % num), "dir": ["素人"]}
    if kind == "gana":
        num = rng.randint(1000, 9999)
        name = "200GANA-%04d" % num
        return {**_spec(name + ext, "200GANA-%04d" % num), "dir": ["素人"]}
    num = rng.randint(1, 999)
    name = "T28-%03d" % num
    return {**_spec(name + ext, "T28-%03d" % num), "dir": ["T28"]}


# 生成器池：可识别名字按这个顺序轮转（保证每套语料覆盖所有形态）
PARSEABLE_GENERATORS: List[Callable[[random.Random], Dict]] = [
    _gen_plain,
    _gen_plain,
    _gen_glued,
    _gen_ad_prefix,
    _gen_cn_dirty,
    _gen_special,
]


# ---------------------------------------------------------------------------
# 两套目录树
# ---------------------------------------------------------------------------

def _sidecar_for(entry: Dict, rng: random.Random) -> Optional[Dict]:
    """给一条视频随机生成 sidecar（字幕/封面/nfo），预期番号跟视频一致。"""
    stem = Path(entry["filename"]).stem
    kind = rng.random()
    if kind < 0.45:
        name, flags = stem + "-C.srt", ("chinese_sub",)
    elif kind < 0.65:
        name, flags = stem + ".zh.srt", ("chinese_sub",)
    elif kind < 0.8:
        name, flags = stem + ".nfo", ()
    else:
        name, flags = stem + "-cover.jpg", ()
    return {
        "filename": name,
        "code": entry["code"],
        "flags": flags,
        "dir": list(entry["dir"]),
        "kind": "sidecar",
    }


def _nas_entries(per_tree: int, rng: random.Random) -> List[Dict]:
    videos: List[Dict] = []
    n_garbage = max(1, int(round(per_tree * 0.10)))
    n_parseable = per_tree - n_garbage

    idx = 0
    seen = set()
    while len(videos) < n_parseable:
        gen = PARSEABLE_GENERATORS[idx % len(PARSEABLE_GENERATORS)]
        v = gen(rng)
        idx += 1
        rel = "/".join(v["dir"] + [v["filename"]])
        if rel in seen:  # 同名碰撞（随机号码撞车）→ 重摇
            if idx > per_tree * 200:
                p = Path(v["filename"])
                v["filename"] = "%s (2)%s" % (p.stem, p.suffix)
                rel = "/".join(v["dir"] + [v["filename"]])
            if rel in seen:
                continue
        seen.add(rel)
        videos.append(v)

    # 10% 垃圾名（放进 _未分类，模拟下载站残留）
    for i in range(n_garbage):
        name = _garbage_name(i)
        rel = "/".join(["_未分类", name])
        if rel in seen:
            continue
        seen.add(rel)
        videos.append({
            "filename": name,
            "code": None,
            "flags": (),
            "dir": ["_未分类"],
        })

    rng.shuffle(videos)
    entries: List[Dict] = []
    for v in videos:
        entries.append({
            "kind": "video",
            "rel_path": "/".join(v["dir"] + [v["filename"]]),
            "expect_code": v["code"],
            "expect_flags": list(v["flags"]),
        })

    # sidecar：~12% 的视频带 1-2 个 sidecar
    for v in videos:
        if v["code"] is None:
            continue
        if rng.random() < 0.12:
            s = _sidecar_for(v, rng)
            entries.append({
                "kind": "sidecar",
                "rel_path": "/".join(s["dir"] + [s["filename"]]),
                "expect_code": s["code"],
                "expect_flags": list(s["flags"]),
            })
            if rng.random() < 0.3:
                nfo_name = Path(v["filename"]).stem + ".nfo"
                entries.append({
                    "kind": "sidecar",
                    "rel_path": "/".join(v["dir"] + [nfo_name]),
                    "expect_code": v["code"],
                    "expect_flags": [],
                })

    # 父目录兜底结构：<code>/cover.jpg 与 <code>/CD1/VIDEO_TS/VTS_01_1.VOB
    pick = [v for v in videos if v["code"] is not None]
    rng.shuffle(pick)
    for v in pick[:3]:
        # 下载站常见：<番号>/cover.jpg（文件本身没番号，靠父目录兜底）
        entries.append({
            "kind": "sidecar",
            "rel_path": "/".join(v["dir"] + [v["code"], "cover.jpg"]),
            "expect_code": v["code"],
            "expect_flags": [],
        })
    if pick:
        v = pick[0]
        # 老 DVD 结构：<番号>/CD1/VIDEO_TS/VTS_01_1.VOB（父目录兜底第 3 层）
        entries.append({
            "kind": "sidecar",
            "rel_path": "/".join(v["dir"] + [v["code"], "CD1", "VIDEO_TS", "VTS_01_1.VOB"]),
            "expect_code": v["code"],
            "expect_flags": [],
        })
    return entries


def _tape_entries(per_tree: int, rng: random.Random) -> List[Dict]:
    barcodes = ["LTO6-A001", "LTO6-B002"]
    videos: List[Dict] = []
    n_garbage = max(1, int(round(per_tree * 0.10)))
    n_parseable = per_tree - n_garbage
    idx = 0
    seen = set()
    while len(videos) < n_parseable:
        gen = PARSEABLE_GENERATORS[idx % len(PARSEABLE_GENERATORS)]
        v = gen(rng)
        barcode = barcodes[idx % len(barcodes)]
        year = rng.choice(["2018", "2019", "2020", "2021", "2022", "2023", "2024"])
        # 磁带上的归类目录：categories 比 NAS 扁平
        cat = rng.choice(["movies", "movies", "movies", "uncensored", "fc2"])
        v["dir"] = [barcode, cat, year]
        idx += 1
        rel = "/".join(v["dir"] + [v["filename"]])
        if rel in seen:
            if idx > per_tree * 200:
                p = Path(v["filename"])
                v["filename"] = "%s (2)%s" % (p.stem, p.suffix)
                rel = "/".join(v["dir"] + [v["filename"]])
            if rel in seen:
                continue
        seen.add(rel)
        videos.append(v)

    for i in range(n_garbage):
        name = _garbage_name(i + 7)
        rel = "/".join([barcodes[i % len(barcodes)], "_unknown", name])
        if rel in seen:
            continue
        seen.add(rel)
        videos.append({
            "filename": name,
            "code": None,
            "flags": (),
            "dir": [barcodes[i % len(barcodes)], "_unknown"],
        })

    rng.shuffle(videos)
    entries: List[Dict] = []
    for v in videos:
        entries.append({
            "kind": "video",
            "rel_path": "/".join(v["dir"] + [v["filename"]]),
            "expect_code": v["code"],
            "expect_flags": list(v["flags"]),
        })

    # 磁带上很常见的「盘根 nfo / manifest 附带字幕」
    for v in videos:
        if v["code"] is not None and rng.random() < 0.1:
            s = _sidecar_for(v, rng)
            entries.append({
                "kind": "sidecar",
                "rel_path": "/".join(v["dir"] + [s["filename"]]),
                "expect_code": s["code"],
                "expect_flags": list(s["flags"]),
            })
    return entries


def build_corpus_plan(per_tree: int = 300, seed: int = 42) -> Dict[str, List[Dict]]:
    """纯函数：返回 {"nas": [...], "tape": [...]}，不碰文件系统（测试直接用它）。"""
    if per_tree < 1:
        raise ValueError("per_tree 必须 >= 1")
    plan: Dict[str, List[Dict]] = {}
    for tree, builder in (("nas", _nas_entries), ("tape", _tape_entries)):
        rng = random.Random("%s-%d-%d" % (tree, seed, per_tree))
        plan[tree] = builder(per_tree, rng)
    return plan


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------

_EMPTY_EXTS = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".ts", ".m2ts", ".rmvb",
               ".rm", ".flv", ".webm", ".iso", ".m4v", ".mpg", ".mpeg", ".vob", ".jpg", ".JPG", ".MOV", ".MP4"}


def _write_file(path: Path, rel: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix in (".srt", ".ass", ".ssa", ".nfo"):
        if suffix == ".nfo":
            path.write_text("<?xml version=\"1.0\"?><movie><title>synthetic</title></movie>\n",
                            encoding="utf-8")
        else:
            path.write_text("1\n00:00:01,000 --> 00:00:02,000\n合成字幕\n", encoding="utf-8")
    else:
        path.write_bytes(b"")


def _ensure_not_in_repo(out: Path) -> None:
    try:
        out.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return
    print(
        "拒绝写入仓库内路径：%s（语料必须生成在 /tmp 或仓库外）" % out,
        file=sys.stderr,
    )
    raise SystemExit(2)


def generate_corpus(
    out_dir: Path,
    per_tree: int = 300,
    seed: int = 42,
    clean: bool = False,
) -> Dict:
    """把两套语料树写到 out_dir，返回 summary（也写进 manifest.json）。"""
    out = Path(out_dir)
    _ensure_not_in_repo(out)
    if clean and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    plan = build_corpus_plan(per_tree=per_tree, seed=seed)
    all_entries: List[Dict] = []
    trees_summary: Dict[str, Dict] = {}

    for tree, entries in plan.items():
        tree_root = out / tree
        videos = 0
        sidecars = 0
        unparseable = 0
        for e in entries:
            _write_file(tree_root / e["rel_path"], e["rel_path"])
            all_entries.append({"tree": tree, **e})
            if e["kind"] == "video":
                videos += 1
                if e["expect_code"] is None:
                    unparseable += 1
            else:
                sidecars += 1
        trees_summary[tree] = {
            "root": str(tree_root),
            "videos": videos,
            "sidecars": sidecars,
            "unparseable": unparseable,
        }

    manifest = {
        "seed": seed,
        "videos_per_tree": per_tree,
        "trees": trees_summary,
        "entries": all_entries,
    }
    manifest_path = out / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )

    total_files = sum(len(v) for v in plan.values())
    return {
        "out": str(out),
        "manifest": str(manifest_path),
        "seed": seed,
        "videos_per_tree": per_tree,
        "total_files": total_files,
        "trees": trees_summary,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="生成合成 AV 语料目录树（默认写 /tmp）")
    ap.add_argument("--out", default="/tmp/av-archive-corpus", help="输出目录（必须在仓库外）")
    ap.add_argument("--per-tree", type=int, default=300, help="每套树的视频文件数（默认 300）")
    ap.add_argument("--seed", type=int, default=42, help="随机种子（默认 42）")
    ap.add_argument("--clean", action="store_true", help="先删掉输出目录")
    args = ap.parse_args(argv)

    summary = generate_corpus(Path(args.out), per_tree=args.per_tree, seed=args.seed, clean=args.clean)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
