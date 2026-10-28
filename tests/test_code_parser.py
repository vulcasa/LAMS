"""番号解析器测试 —— table-driven，真实世界脏名字为主。

运行：
    cd av-archive && .venv/bin/python -m pytest tests/test_code_parser.py -q
    # 或任意装了 pytest 的 python3：python3 -m pytest tests/test_code_parser.py -q

===============================================================================
【边界 case 清单】—— 哪些名字**不能**可靠解析，以及为什么
===============================================================================
下面这些是刻意「不解决」或「只能给低 confidence」的类别。原则：宁可漏也别错，
错配会把 r18 元数据挂到错片子上，比漏抓难查得多。

A. 完全没有番号的素人/无码散片
   `1pondo` / `10musume` / `Caribbean` / `Pacopacomama` / `Tokyo-Hot` 这些厂牌
   的原始站内命名（`123456_001.mp4`、`n1234.mp4`）不带厂牌字串，纯数字/单字母。
   本解析器只认「带厂牌字串」或「整串就是 n1234/k1234」的形态，其它一律 None。
   裸 `n1234` 只能给 45 分（不排除是别的片子），全库扫描时应该走待确认。

B. 纯中文 / 纯日文 / 纯描述性命名的文件
   `未命名合集.mp4`、`某某老师合集第三部.mkv`、`初撮り.mp4`。没有 ASCII 番号可言，
   code=None。这类只能靠人工标注或 NFO/目录结构补。

C. FC2 卖家自定名、素人自压名
   `fc2-ppv-1234567-素人名.mp4` 能出番号；但 `FC2 1234567 ハメ撮り.mp4` 之外的
   卖家乱码名（`fc2 マンコ 3.mp4`）没有 ID，无解。

D. 多片合集 / 混剪 / 大杂烩
   `SSIS-001_SSNI-002_合集.mp4` 这种一个文件装多个番号：只能选一个（取位置最靠前、
   分最高者），并且**必然**给其它片子挂错。本解析器会扣 12 分（ambiguity penalty）
   让这类落进待确认，但默认仍返回第一个——调用方应把 confidence<70 的进人工队列。
   `-A`/`-B`/`CD1` 之类多碟后缀是「同一部的分片」，不是合集，按 part flag 处理。

E. 番号后缀 vs 版本后缀的冲突
   `SSIS-001-A` 里的 `-A` 90% 是分片，但极少数是卖家自定版本号；`-C` 在中字语境
   是 chinese_sub，在个别压制组里是 "censored"（有码）。本解析器按中文圈主流习惯
   解释（`-A/-B`=part，`-C/-CH`=中字），这两类 flag 允许错，但**番号本身**不受影响。

F. `-UC` 的双重语义
   无码破解盘通常「既无码又内嵌中字」，所以 `-UC` 同时打 uncensored + chinese_sub。
   如果实际只是无码无字，chinese_sub 会假阳性 —— 这是刻意的偏置（字幕层 L0 允许
   「疑似中字」再交叉验证 JavDB 标记）。

G. 4 位数字年份 / 分辨率 / 编码 与真番号的同形
   `movie-2024.mp4`（年份）、`2160p`、`x265`、`h264`、`web-dl`、`fhd` 都做过黑名单/
   正则剥离。但「未知前缀 + 4 位年份」只要不是知名前缀就扣 28 分，仍可能漏杀：
   比如 `ABC-2024`（假设真有个 ABC 厂牌出 2024 号）会被打成低置信。
   4 位号的真厂牌（HEYZO-1234 / SIRO-1234 / 259LUXU-1234）走的是专用规则，不受影响。

H. 磁带条码 / 相机文件名 / 播放器产物
   `AB12345/cover.jpg`（条码目录）、`IMG_1234.MOV`、`DSC01234.JPG`、`VTS_01_1.VOB`、
   `MVI_0001.MP4`：全部黑名单掉。但条码形态本身无法与「未知 2 字母厂牌 + 5 位号」
   完全区分（`AB12345` 长得就像番号），所以父目录兜底对未知前缀要求 score>=82。

I. 番号被广告前缀/后缀切碎
   `hhd800.com@ssis00001hhb.mp4`：域名剥掉 + 5 位前导零识别后能出 SSIS-001。
   但 `hhd800@ssis001`（无 .com）依赖前缀黑名单 HHD；如果换成没见过的广告站前缀
   （如 `zzz999@ssis001.mp4`）就会变成两个候选、扣分，可能选错。
   后缀同理：`ssis001hhb` 里的 `hhb` 被忽略，但如果广告字母恰好构成字母+数字
   （`ssis001 hhd800`）就会多一个候选。

J. 目录结构与真实归属矛盾
   `SSIS-001/CD1/VIDEO_TS/VTS_01_1.VOB` 按目录兜底到 SSIS-001，但如果目录名本身
   是错的（下载站把多部片塞进一个「SSIS-001」目录），所有文件都会挂错。
   父目录兜底最多上溯 3 层，且 confidence 封顶 70 / 按层递减。

K. 单字母/双字母厂的未知前缀
   `xyz-123`、`abc 123`：未知前缀 + 显式分隔符给 80 分（可能对，可能是垃圾）。
   低于 70 的都应该进待确认；只有 90+ 才建议自动入库。

L. 不处理的形态（明确不做）
   - 只有「系列名+序号」没有番号的：`S-Cute 123`、`SIRO シロウト` 类站内编号
   - 磁力链 hash、`[javdb]` 元数据文件、nfo 里的 `<num>`（那是 xml 的事）
   - `-1`/`-2` 纯数字分集后缀（`FC2-PPV-1234567-1` 识别 part 但没有 flag 断言）
   - 跨文件推理（同一目录下 A.mp4 有番号、B.srt 无番号，不做启发式传播；
     只按 basename → 父目录 兜底）

M. 多候选时的确定性
   同分候选按「起始位置靠前」取，再扣 ambiguity penalty。库扫描重跑结果必须稳定，
   所以解析过程不带随机、不依赖 dict 顺序（测试里有 determinism 用例）。

N. 老 DMM 单字母号（`C-2391`/`M-823`/`H-1238`）—— task-14 新增
   真实 r18 dump 里 C/M/H 是三个最常见的单字母厂牌，但单字母规则天生容易误伤：
   * 硬条件：显式分隔符 + 数字 ≥3 位 → `X-1`、`CD-2`、`C-12` 不会变成番号；
   * `H-264`/`H-265`/`X-263` 明确是编码器写法（看数字不看字母）→ 拒绝；
   * 其它单字母（`A-123` 等）给 76 分而不是 82：真实数据里没见过，可能是分片/版本
     标记，入库前应抽检（<80 的进待确认）。

O. 数字/混排前缀（`100TV-031`/`3DSVR-0067`/`CD5-005`/`S4LW-001`）—— task-14 新增
   硬条件同样是**显式分隔符**：`DSC01234`、`AB12345` 这类粘连名不会被拆成番号。
   token 守卫拒绝画质/编码/音频/分辨率写法：`1080P-60`、`X264-001`、`4K-001`、
   `24FPS-030`、`10BIT-8`、`HDR10-4000`、`3D-001`、`MP4-001`。
   `CD5-005` 与 `SSIS-001-CD5` 的歧义靠"分片标记后面没跟数字"区分：前者是番号，
   后者剥掉 CD5 当分片。

P. 老厂牌 4 位补零 / 6 位日期号 —— task-14 新增
   `SD-0512`、`DMM-070802` 这种**必须保留前导零**（r18 dump 的 dvd_id 就这么写，
   抹掉零会匹配不上）；`ssis00001` 这种"3 位号补到 5 位"仍还原成 `SSIS-001`。
   `SD-1080`/`SD-720` 这类"画质前缀 + 分辨率数字"仍然拒绝。6 位号置信度低 8 分。

Q. 后缀字母老无码号（`UD321R`/`UD-330R`/`IBW-123Z`）—— task-14 新增
   2~3 字母 + 3~4 位数字 + 单字母后缀（R/Z/T）整串是一个番号，**不能**拆成
   `UD-321`（拆了 dump 匹配不上）。输出统一成无连字符形态（`UD330R`），因为
   `r18.dvd_id_norm` 对 `UD-330R`/`UD330R` 两种写法归一化结果相同。
   A/B/C/U 后缀被排除（那是分片/中字/无码标记）；极少数 `ABP-123-R` 会被当成
   后缀号 `ABP123R` —— R/Z/T 不是常见版本后缀，接受这个风险。

R. 明确不支持的残留（task-14 benchmark 覆盖）
   `ADV-R0632` 型（字母前缀 + `R` + 4 位数字，2000 条真实 dump 里 2 条 ≈0.1%）：
   要支持它就得放宽"-字母-数字"后缀规则，会牺牲 `-A/-B` 分片语义，不划算。
   benchmark 的主变体可解析率 99.90%，失败样本就是它。

===============================================================================
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app" / "services"))

import code_parser as cp  # noqa: E402

extract_code = cp.extract_code
normalize_code = cp.normalize_code
is_chinese_sub_marker = cp.is_chinese_sub_marker
extract_flags = cp.extract_flags
CodeMatch = cp.CodeMatch


# =============================================================================
# 1. 主表：extract_code(path) -> (code, source, confidence 区间, 必须有的 flags,
#    必须没有的 flags)
#    每行: (说明, path, 期望 code, 期望 source, 最低分, 最高分, 必须有, 必须没有)
# =============================================================================

CODE_CASES = [
    # ---------------- 常规 DMM 写法 ----------------
    ("常规-连字符", "SSIS-001.mp4", "SSIS-001", "basename", 90, 98, set(), set()),
    ("常规-全小写粘连", "ssis001.mp4", "SSIS-001", "basename", 70, 85, set(), set()),
    ("常规-下划线", "SSIS_001.mp4", "SSIS-001", "basename", 88, 98, set(), set()),
    ("常规-空格分隔", "SSIS 001.mp4", "SSIS-001", "basename", 88, 98, set(), set()),
    ("常规-方括号广告站", "[ThZu.Cc]SSNI-001.mp4", "SSNI-001", "basename", 88, 98, set(), set()),
    ("常规-域名@前缀+5位前导零", "hhd800.com@ssis00001hhb.mp4", "SSIS-001", "basename", 75, 90, set(), set()),
    ("常规-域名@前缀", "hhd800.com@SSIS-001.mp4", "SSIS-001", "basename", 88, 98, set(), set()),
    ("常规-www域名@前缀", "www.98t.la@ipx001.mp4", "IPX-001", "basename", 70, 90, set(), set()),
    ("常规-4K 前置", "4K_SSIS-001.mp4", "SSIS-001", "basename", 85, 98, {"hi_res"}, set()),
    ("常规-日文厂牌", "MIDE-001.mp4", "MIDE-001", "basename", 90, 98, set(), set()),
    ("常规-三字母厂牌", "ABP-123.mp4", "ABP-123", "basename", 90, 98, set(), set()),
    ("常规-长厂牌", "STARS-001.mp4", "STARS-001", "basename", 90, 98, set(), set()),
    ("常规-竖线/引号包裹", "[FHD]SSIS-001[中字].mp4", "SSIS-001", "basename", 88, 98,
     {"hi_res", "chinese_sub"}, set()),
    ("常规-两段番号文件名", "SSIS-001-sample.mp4", "SSIS-001", "basename", 88, 98, set(), set()),
    ("常规-番号带中文说明", "SSIS-001 巨乳人妻.mp4", "SSIS-001", "basename", 88, 98, set(), set()),
    ("常规-Windows 反斜杠", r"D:\JAV\S1\SSIS-001.mp4", "SSIS-001", "basename", 88, 98, set(), set()),
    # ---------------- 后缀噪声 + flags ----------------
    ("后缀-C=中字", "SSIS-001-C.mp4", "SSIS-001", "basename", 88, 98, {"chinese_sub"}, set()),
    ("后缀-CH=中字", "SSIS-001-CH.mp4", "SSIS-001", "basename", 88, 98, {"chinese_sub"}, set()),
    ("后缀-UC=中字+无码", "SSIS-001-UC.mp4", "SSIS-001", "basename", 88, 98,
     {"chinese_sub", "uncensored"}, set()),
    ("后缀-U=无码", "SSIS-001-U.mp4", "SSIS-001", "basename", 88, 98, {"uncensored"}, {"chinese_sub"}),
    ("后缀-4K=高清", "SSIS-001-4K.mp4", "SSIS-001", "basename", 88, 98, {"hi_res"}, set()),
    ("后缀-1080p=高清", "SSIS-001-1080p.mp4", "SSIS-001", "basename", 88, 98, {"hi_res"}, set()),
    ("后缀-FHD=高清", "SSIS-001-FHD.mp4", "SSIS-001", "basename", 88, 98, {"hi_res"}, set()),
    ("后缀-LEAK=无码", "SSIS-001-LEAK.mp4", "SSIS-001", "basename", 88, 98, {"uncensored"}, set()),
    ("后缀-CD1=分片", "SSIS-001-CD1.mp4", "SSIS-001", "basename", 88, 98, {"part"}, set()),
    ("后缀-PART1=分片", "SSIS-001-PART1.mp4", "SSIS-001", "basename", 88, 98, {"part"}, set()),
    ("后缀-点分part", "SDDE-001.part2.mkv", "SDDE-001", "basename", 88, 98, {"part"}, set()),
    ("后缀-A=分片", "SSIS-001-A.mp4", "SSIS-001", "basename", 88, 98, {"part"}, set()),
    ("后缀-B=分片", "SSIS-001-B.mp4", "SSIS-001", "basename", 88, 98, {"part"}, set()),
    ("后缀-多噪声叠加", "ABP-123-4K-C.mp4", "ABP-123", "basename", 85, 98,
     {"hi_res", "chinese_sub"}, set()),
    ("中字-简体前缀", "中文字幕SSIS-001.mp4", "SSIS-001", "basename", 88, 98, {"chinese_sub"}, set()),
    ("中字-后缀", "SSIS-001中字.mp4", "SSIS-001", "basename", 88, 98, {"chinese_sub"}, set()),
    ("中字-繁体方括号", "[無碼]SSIS-001.mp4", "SSIS-001", "basename", 88, 98, {"uncensored"}, set()),
    ("中字-无码中文", "无码SSIS-001.mp4", "SSIS-001", "basename", 88, 98, {"uncensored"}, set()),
    ("中字-粘连c", "ssis001c.mp4", "SSIS-001", "basename", 70, 85, {"chinese_sub"}, set()),
    ("中字-中英混合", "SSIS-001 无码 中字.mp4", "SSIS-001", "basename", 85, 98,
     {"uncensored", "chinese_sub"}, set()),
    # ---------------- 特殊厂牌 ----------------
    ("FC2-标准", "FC2-PPV-1234567.mp4", "FC2-PPV-1234567", "basename", 93, 98, set(), set()),
    ("FC2-全小写粘连", "fc2ppv1234567.mp4", "FC2-PPV-1234567", "basename", 93, 98, set(), set()),
    ("FC2-大写无分隔", "FC2PPV-1234567.mp4", "FC2-PPV-1234567", "basename", 93, 98, set(), set()),
    ("FC2-省略PPV", "FC2-1234567.mp4", "FC2-PPV-1234567", "basename", 93, 98, set(), set()),
    ("FC2-空格分隔", "fc2 ppv 1234567.mp4", "FC2-PPV-1234567", "basename", 93, 98, set(), set()),
    ("FC2-带分集尾巴", "FC2-PPV-1234567-1.mp4", "FC2-PPV-1234567", "basename", 90, 98, set(), set()),
    ("FC2-粘字母尾巴", "FC2PPV-1234567a.mp4", "FC2-PPV-1234567", "basename", 90, 98, set(), set()),
    ("HEYZO-标准", "HEYZO-1234.mp4", "HEYZO-1234", "basename", 90, 98, set(), set()),
    ("HEYZO-hd 变体", "heyzo_hd_1234.mp4", "HEYZO-1234", "basename", 88, 98, {"hi_res"}, set()),
    ("HEYZO-空格", "heyzo 1234.mp4", "HEYZO-1234", "basename", 88, 98, set(), set()),
    ("SIRO-标准", "SIRO-1234.mp4", "SIRO-1234", "basename", 88, 98, set(), set()),
    ("SIRO-粘连", "siro1234.mp4", "SIRO-1234", "basename", 88, 98, set(), set()),
    ("TokyoHot-连字符", "Tokyo-Hot n1234.mp4", "TOKYO-HOT-N1234", "basename", 85, 98, set(), set()),
    ("TokyoHot-下划线", "Tokyo_Hot_k1234.mp4", "TOKYO-HOT-K1234", "basename", 85, 98, set(), set()),
    ("TokyoHot-裸号低置信", "n1234.mp4", "TOKYO-HOT-N1234", "basename", 30, 55, set(), set()),
    ("TokyoHot-裸号带分集括号", "n1234 (2).mp4", "TOKYO-HOT-N1234", "basename", 30, 55, set(), set()),
    ("1pondo-下划线", "1pondo 123456_001.mp4", "1PONDO-123456_001", "basename", 88, 98, set(), set()),
    ("1pondo-连字符", "1pondo-123456-001.mp4", "1PONDO-123456_001", "basename", 88, 98, set(), set()),
    ("10musume-下划线", "10musume 123456_01.mp4", "10MUSUME-123456_01", "basename", 88, 98, set(), set()),
    ("Caribbean-全称", "Caribbean 123456-001.mp4", "CARIB-123456-001", "basename", 88, 98, set(), set()),
    ("Caribbean-缩写", "carib-123456-001.mp4", "CARIB-123456-001", "basename", 88, 98, set(), set()),
    ("Pacopacomama-全称", "Pacopacomama 123456_001.mp4", "PACOPACOMAMA-123456_001", "basename", 88, 98, set(), set()),
    ("素人-数字前缀LUXU", "259LUXU-1234.mp4", "259LUXU-1234", "basename", 90, 98, set(), set()),
    ("素人-数字前缀LUXU空格", "259 luxu 1234.mp4", "259LUXU-1234", "basename", 90, 98, set(), set()),
    ("素人-数字前缀MIUM", "300MIUM-123.mp4", "300MIUM-123", "basename", 90, 98, set(), set()),
    ("素人-数字前缀GANA", "200GANA-1234.mp4", "200GANA-1234", "basename", 90, 98, set(), set()),
    ("字母数字厂牌-T28", "T28-123.mp4", "T28-123", "basename", 80, 95, set(), set()),
    # ---------------- 老 DMM 单字母厂牌（task-14） ----------------
    ("单字母-C", "C-2391.mp4", "C-2391", "basename", 80, 85, set(), {"chinese_sub"}),
    ("单字母-C 小写", "c-2332.mp4", "C-2332", "basename", 80, 85, set(), set()),
    ("单字母-M", "M-823.mp4", "M-823", "basename", 80, 85, set(), set()),
    ("单字母-H", "H-1238.mp4", "H-1238", "basename", 80, 85, set(), set()),
    ("单字母-下划线", "C_2391.mp4", "C-2391", "basename", 80, 85, set(), set()),
    ("单字母-空格", "C 2391.mp4", "C-2391", "basename", 80, 85, set(), set()),
    ("单字母-中字后缀", "C-2391-C.mp4", "C-2391", "basename", 80, 85, {"chinese_sub"}, set()),
    ("单字母-未知字母低分", "V-1234.mp4", "V-1234", "basename", 75, 80, set(), set()),
    # ---------------- 数字/混排前缀（task-14） ----------------
    ("混排-100TV", "100TV-031.mp4", "100TV-031", "basename", 76, 80, set(), set()),
    ("混排-3DSVR", "3DSVR-0067.mp4", "3DSVR-0067", "basename", 76, 80, set(), set()),
    ("混排-3DBD", "3DBD-003.mp4", "3DBD-003", "basename", 76, 80, set(), set()),
    ("混排-CD5", "CD5-005.mp4", "CD5-005", "basename", 76, 80, set(), {"part"}),
    ("混排-S4LW", "S4LW-001.mp4", "S4LW-001", "basename", 76, 80, set(), set()),
    ("混排-CD17", "CD17-005.mp4", "CD17-005", "basename", 76, 80, set(), set()),
    ("混排-下划线", "3DSVR_0067.mp4", "3DSVR-0067", "basename", 76, 80, set(), set()),
    # ---------------- 老厂牌补零 / 6 位日期号（task-14） ----------------
    ("老番号-SD", "SD-1106.mp4", "SD-1106", "basename", 78, 82, set(), set()),
    ("老番号-SD 补零", "SD-0512.mp4", "SD-0512", "basename", 78, 82, set(), set()),
    ("老番号-6位DMM", "DMM-070802.mp4", "DMM-070802", "basename", 70, 75, set(), set()),
    ("老番号-6位DM", "DM-080302.mp4", "DM-080302", "basename", 70, 75, set(), set()),
    ("老番号-CM", "CM-1022.mp4", "CM-1022", "basename", 78, 82, set(), set()),
    ("老番号-NEW 补零", "NEW-04.mp4", "NEW-004", "basename", 78, 82, set(), set()),
    # ---------------- 后缀字母老无码号（task-14） ----------------
    ("后缀号-UD 粘连", "UD321R.mp4", "UD321R", "basename", 84, 90, set(), set()),
    ("后缀号-UD 连字符", "UD-330R.mp4", "UD330R", "basename", 84, 90, set(), set()),
    ("后缀号-UD 小写", "ud-330r.mp4", "UD330R", "basename", 84, 90, set(), set()),
    ("后缀号-IBW", "IBW-123Z.mp4", "IBW123Z", "basename", 90, 95, set(), set()),
    ("后缀号-RAN", "RAN-456T.mp4", "RAN456T", "basename", 84, 90, set(), set()),
    # 后缀 A/B/C/U 仍然是分片/中字/无码，不能被吞进番号
    ("后缀号-A 不吞", "ABP-123-A.mp4", "ABP-123", "basename", 90, 98, {"part"}, set()),
    ("后缀号-C 不吞", "ABP-123-C.mp4", "ABP-123", "basename", 90, 98, {"chinese_sub"}, set()),
    ("分片-CD5 不误判", "SSIS-001-CD5.mp4", "SSIS-001", "basename", 90, 98, {"part"}, set()),
    # ---------------- 陷阱：不能被当番号 ----------------
    ("陷阱-1080P", "1080P.mp4", None, "none", 0, 0, set(), set()),
    ("陷阱-X264", "X264.mkv", None, "none", 0, 0, set(), set()),
    ("陷阱-HEVC", "HEVC.mkv", None, "none", 0, 0, set(), set()),
    ("陷阱-H264", "H264.mp4", None, "none", 0, 0, set(), set()),
    ("陷阱-AAC", "AAC.m4a", None, "none", 0, 0, set(), set()),
    ("陷阱-WEB-DL", "WEB-DL.mkv", None, "none", 0, 0, set(), set()),
    ("陷阱-FHD", "FHD.mp4", None, "none", 0, 0, set(), set()),
    ("陷阱-UHD", "UHD.mp4", None, "none", 0, 0, set(), set()),
    ("陷阱-PART1", "PART1.mp4", None, "none", 0, 0, set(), set()),
    ("陷阱-CD1", "CD1.mp4", None, "none", 0, 0, set(), set()),
    ("陷阱-相机文件名", "IMG_1234.MOV", None, "none", 0, 0, set(), set()),
    ("陷阱-DVD VTS", "VTS_01_1.VOB", None, "none", 0, 0, set(), set()),
    ("陷阱-单反", "DSC01234.JPG", None, "none", 0, 0, set(), set()),
    ("陷阱-年份电影名", "random_movie_2024.mkv", None, "none", 0, 0, set(), set()),
    ("陷阱-年份粘连", "movie2024.mp4", None, "none", 0, 0, set(), set()),
    ("陷阱-未命名", "未命名.mp4", None, "none", 0, 0, set(), set()),
    ("陷阱-sample", "sample.mkv", None, "none", 0, 0, set(), set()),
    ("陷阱-trailer", "trailer.mp4", None, "none", 0, 0, set(), set()),
    ("陷阱-clip括号数字", "clip (1).mp4", None, "none", 0, 0, set(), set()),
    ("陷阱-只有分辨率", "1920x1080.mp4", None, "none", 0, 0, set(), set()),
    ("陷阱-分辨率编码混排", "1080p-x264-aac.mkv", None, "none", 0, 0, set(), set()),
    ("陷阱-广告站无番号", "hhd800.com@1080p.mp4", None, "none", 0, 0, set(), set()),
    ("陷阱-日期名", "2024-01-15.mp4", None, "none", 0, 0, set(), set()),
    # --- task-14 新规则的反例（每类 >=5 条）---
    ("反例-单字母1位数字", "X-1.mp4", None, "none", 0, 0, set(), set()),
    ("反例-分片CD-2", "CD-2.mp4", None, "none", 0, 0, set(), set()),
    ("反例-分片PART-1", "PART-1.mp4", None, "none", 0, 0, set(), set()),
    ("反例-编码X-264", "X-264.mp4", None, "none", 0, 0, set(), set()),
    ("反例-编码H-264", "H-264.mp4", None, "none", 0, 0, set(), set()),
    ("反例-编码H-265", "H-265.mp4", None, "none", 0, 0, set(), set()),
    ("反例-单字母2位数字", "C-12.mp4", None, "none", 0, 0, set(), set()),
    ("反例-分辨率前缀号", "1080P-60.mp4", None, "none", 0, 0, set(), set()),
    ("反例-编码前缀号", "X264-001.mp4", None, "none", 0, 0, set(), set()),
    ("反例-4K前缀号", "4K-001.mp4", None, "none", 0, 0, set(), set()),
    ("反例-fps 前缀号", "24FPS-030.mp4", None, "none", 0, 0, set(), set()),
    ("反例-bit 前缀号", "10BIT-8.mp4", None, "none", 0, 0, set(), set()),
    ("反例-相机连字符", "DSC012-034.mp4", None, "none", 0, 0, set(), set()),
    ("反例-相机粘连", "DSC01234.JPG", None, "none", 0, 0, set(), set()),
    ("反例-条形码粘连", "AB12345/cover.jpg", None, "none", 0, 0, set(), set()),
    ("反例-MP4前缀号", "MP4-001.mp4", None, "none", 0, 0, set(), set()),
    ("反例-画质数字SD", "SD-1080.mp4", None, "none", 0, 0, set(), set()),
    ("反例-画质数字SD2", "SD-720.mp4", None, "none", 0, 0, set(), set()),
    ("反例-画质数字SD3", "SD-2160.mp4", None, "none", 0, 0, set(), set()),
    ("反例-6位超长", "DMM-0708021.mp4", None, "none", 0, 0, set(), set()),
    ("陷阱-Part1 带点", "file.mp4.part1", None, "none", 0, 0, set(), set()),
    # ---------------- 非视频文件（字幕/封面/nfo 归属） ----------------
    ("非视频-srt", "SSIS-001.srt", "SSIS-001", "basename", 88, 98, set(), set()),
    ("非视频-中字srt", "SSIS-001-C.srt", "SSIS-001", "basename", 88, 98, {"chinese_sub"}, set()),
    ("非视频-zh 字幕", "SSIS-001.zh.srt", "SSIS-001", "basename", 88, 98, {"chinese_sub"}, set()),
    ("非视频-chs 字幕", "SSIS-001.chs.ass", "SSIS-001", "basename", 88, 98, {"chinese_sub"}, set()),
    ("非视频-nfo", "SSIS-001.nfo", "SSIS-001", "basename", 88, 98, set(), set()),
    ("非视频-封面", "SSIS-001-cover.jpg", "SSIS-001", "basename", 88, 98, set(), set()),
    ("非视频-无番号封面", "poster.jpg", None, "none", 0, 0, set(), set()),
    # ---------------- 父目录兜底 ----------------
    ("父目录-简单", "SSIS-001/cover.jpg", "SSIS-001", "parent", 60, 70, set(), set()),
    ("父目录-VOB 结构", "SSIS-001/CD1/VIDEO_TS/VTS_01_1.VOB", "SSIS-001", "parent", 55, 70, set(), set()),
    ("父目录-道具目录", "SSIS-001/extras/making.mp4", "SSIS-001", "parent", 55, 70, set(), set()),
    ("父目录-目录同名文件", "SSIS-001/SSIS-001.mp4", "SSIS-001", "basename", 90, 98, set(), set()),
    ("父目录-条码目录不误判", "AB12345/cover.jpg", None, "none", 0, 0, set(), set()),
    ("父目录-不能上溯太远", "SSIS-001/a/b/c/cover.jpg", None, "none", 0, 0, set(), set()),
    # 目录名里的「中文字幕」不传染给文件（避免整库假阳性）
    ("父目录-中字目录不传染", "中文字幕/SSIS-001.mp4", "SSIS-001", "basename", 88, 98, set(), {"chinese_sub"}),
]


@pytest.mark.parametrize(
    "label,path,code,source,min_conf,max_conf,must_flags,must_not_flags",
    CODE_CASES,
    ids=[c[0] for c in CODE_CASES],
)
def test_extract_code(label, path, code, source, min_conf, max_conf, must_flags, must_not_flags):
    m = extract_code(path)
    assert isinstance(m, CodeMatch)
    assert m.code == code, f"{label}: {path} -> {m.code!r}, want {code!r}"
    assert m.source == source, f"{label}: source={m.source!r}, want {source!r}"
    assert min_conf <= m.confidence <= max_conf, (
        f"{label}: confidence={m.confidence}, want [{min_conf},{max_conf}]"
    )
    assert must_flags <= m.flags, f"{label}: flags={m.flags}, want superset {must_flags}"
    assert not (must_not_flags & m.flags), f"{label}: flags={m.flags}, 不应包含 {must_not_flags}"
    # 无番号时 raw 必须是空串；有番号时 raw 必须是名字里的子串
    if code is None:
        assert m.raw == ""
    else:
        assert m.raw and m.raw in path, f"{label}: raw={m.raw!r} 不是 {path!r} 的子串"


# =============================================================================
# 2. 常见视频容器后缀都能解析
# =============================================================================

VIDEO_EXTS = [
    ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".ts", ".m2ts", ".rmvb",
    ".rm", ".flv", ".webm", ".iso", ".m4v", ".mpg", ".mpeg",
]


@pytest.mark.parametrize("ext", VIDEO_EXTS)
def test_all_video_extensions(ext):
    m = extract_code("SSIS-001" + ext)
    assert m.code == "SSIS-001"
    assert m.confidence >= 85


# =============================================================================
# 3. normalize_code
# =============================================================================

NORMALIZE_CASES = [
    ("已规范", "SSIS-001", "SSIS-001"),
    ("小写粘连", "ssis001", "SSIS-001"),
    ("下划线", "SSIS_001", "SSIS-001"),
    ("5位前导零+广告尾巴", "ssis00001hhb", "SSIS-001"),
    ("4K 后缀", "ABP-123-4K", "ABP-123"),
    ("中字后缀", "SSIS-001-C", "SSIS-001"),
    ("UC 后缀", "SSIS-001-UC", "SSIS-001"),
    ("FC2 粘连", "FC2PPV1234567", "FC2-PPV-1234567"),
    ("FC2 带连字符", "FC2-PPV-1234567", "FC2-PPV-1234567"),
    ("HEYZO hd", "heyzo_hd_1234", "HEYZO-1234"),
    ("Tokyo-Hot", "Tokyo-Hot n1234", "TOKYO-HOT-N1234"),
    ("1pondo", "1pondo 123456_001", "1PONDO-123456_001"),
    ("Caribbean", "carib-123456-001", "CARIB-123456-001"),
    ("数字前缀素人", "259LUXU-1234", "259LUXU-1234"),
    ("带扩展名", "SSIS-001.mp4", "SSIS-001"),
    ("老DMM单字母", "C-2391", "C-2391"),
    ("数字前缀短标签", "100TV-031", "100TV-031"),
    ("混排前缀", "S4LW-001", "S4LW-001"),
    ("混排前缀下划线", "3DSVR_0067", "3DSVR-0067"),
    ("SD 4位补零", "SD-0512", "SD-0512"),
    ("6位老番号", "DMM-070802", "DMM-070802"),
    ("后缀字母老无码", "UD-330R", "UD330R"),
    ("编码不是番号", "X264-001", None),
    ("分辨率不是番号", "1080P-60", None),
    ("分片不是番号", "CD-2", None),
    ("分辨率", "1080p", None),
    ("编码", "X264", None),
    ("广告前缀", "hhd800", None),
    ("纯中文", "未命名", None),
    ("空串", "", None),
    ("None", None, None),
]


@pytest.mark.parametrize("label,raw,want", NORMALIZE_CASES, ids=[c[0] for c in NORMALIZE_CASES])
def test_normalize_code(label, raw, want):
    assert normalize_code(raw) == want, f"{label}: normalize_code({raw!r})"


# =============================================================================
# 4. is_chinese_sub_marker（字幕层 L0 用）
# =============================================================================

CSUB_TRUE = [
    "SSIS-001-C.mp4",
    "SSIS-001-CH.mp4",
    "SSIS-001-UC.mp4",
    "ssis001c.mp4",
    "SSIS-001.chs.srt",
    "SSIS-001.zh.srt",
    "中文字幕SSIS-001.mp4",
    "SSIS-001中字.mp4",
    "[简中]SSIS-001.mp4",
    "SSIS-001 中文.mp4",
]
CSUB_FALSE = [
    "SSIS-001.mp4",
    "SSIS-001-1080p.mp4",
    "SSIS-001-4K.mp4",
    "SSIS-001-U.mp4",
    "無碼SSIS-001.mp4",
    "X264.mkv",
    "",
]


@pytest.mark.parametrize("text", CSUB_TRUE)
def test_chinese_sub_true(text):
    assert is_chinese_sub_marker(text) is True


@pytest.mark.parametrize("text", CSUB_FALSE)
def test_chinese_sub_false(text):
    assert is_chinese_sub_marker(text) is False


# =============================================================================
# 5. extract_flags 单项
# =============================================================================

@pytest.mark.parametrize(
    "text,flag,expected",
    [
        ("SSIS-001-C", cp.FLAG_CHINESE_SUB, True),
        ("SSIS-001-UC", cp.FLAG_CHINESE_SUB, True),
        ("SSIS-001-UC", cp.FLAG_UNCENSORED, True),
        ("SSIS-001 無碼", cp.FLAG_UNCENSORED, True),
        ("SSIS-001 流出", cp.FLAG_UNCENSORED, True),
        ("SSIS-001-4K", cp.FLAG_HI_RES, True),
        ("SSIS-001-2160p", cp.FLAG_HI_RES, True),
        ("SSIS-001-CD1", cp.FLAG_PART, True),
        ("SSIS-001-PART1", cp.FLAG_PART, True),
        ("SSIS-001-A", cp.FLAG_PART, True),
        ("SSIS-001-B", cp.FLAG_PART, True),
        ("SSIS-001", cp.FLAG_CHINESE_SUB, False),
        ("SSIS-001", cp.FLAG_UNCENSORED, False),
        ("SSIS-001", cp.FLAG_PART, False),
        ("", cp.FLAG_HI_RES, False),
    ],
)
def test_extract_flags(text, flag, expected):
    assert (flag in extract_flags(text)) is expected


# =============================================================================
# 6. confidence 语义 / 排序
# =============================================================================

def test_confidence_clean_beats_glued_unknown():
    clean = extract_code("SSIS-001.mp4")
    glued = extract_code("xyz123.mp4")
    assert clean.code == "SSIS-001"
    assert glued.code == "XYZ-123"
    assert clean.confidence > glued.confidence


def test_confidence_known_prefix_beats_unknown():
    known = extract_code("SSIS-001.mp4")
    unknown = extract_code("ZZZ-001.mp4")
    assert unknown.code == "ZZZ-001"
    assert known.confidence > unknown.confidence


def test_confidence_none_is_zero():
    m = extract_code("1080P.mp4")
    assert m.code is None and m.confidence == 0 and m.source == "none" and m.raw == ""


def test_confidence_parent_capped():
    m = extract_code("SSIS-001/cover.jpg")
    assert m.source == "parent"
    assert m.confidence <= 70


def test_ambiguity_penalty_and_determinism():
    single = extract_code("SSIS-001.mp4")
    multi = extract_code("SSIS-001_SSNI-002.mp4")
    # 多候选：取靠前的，扣分，但仍是合法番号
    assert multi.code == "SSIS-001"
    assert multi.confidence < single.confidence
    # 确定性：重复解析结果一致
    assert extract_code("SSIS-001_SSNI-002.mp4") == multi


def test_repeated_same_code_not_penalized():
    once = extract_code("SSIS-001.mp4")
    twice = extract_code("SSIS-001 SSIS-001.mp4")
    assert twice.code == "SSIS-001"
    assert twice.confidence == once.confidence


def test_multi_code_collection_goes_to_review():
    # 一个文件塞多个番号 = 合集，选谁都错一半 -> 必须 <70 进待确认
    m = extract_code("SSIS-001_SSNI-002_合集.mp4")
    assert m.code == "SSIS-001"  # 取靠前的，保证确定性
    assert m.confidence < 70
    assert m.is_confident is False


def test_is_confident_property():
    assert extract_code("SSIS-001.mp4").is_confident is True
    assert extract_code("n1234.mp4").is_confident is False
    assert extract_code("1080P.mp4").is_confident is False


def test_flags_default_is_independent_set():
    a = CodeMatch(code="A-001", confidence=1, source="none", raw="")
    b = CodeMatch(code="B-001", confidence=1, source="none", raw="")
    a.flags.add("x")
    assert b.flags == set()


# =============================================================================
# 7. 路径边界条件
# =============================================================================

@pytest.mark.parametrize("path", ["", "/", ".", "..", "///", "C:\\", "..."])
def test_degenerate_paths(path):
    m = extract_code(path)
    assert m.code is None
    assert m.source == "none"


def test_trailing_slash_and_deep_path():
    m = extract_code("/mnt/user/av/S1/SSIS-001/SSIS-001-C.mp4")
    assert m.code == "SSIS-001"
    assert m.source == "basename"
    assert "chinese_sub" in m.flags


def test_parent_fallback_uses_nearest_ancestor():
    # 3 层祖先也能用，但必须比 1 层祖先低分
    near = extract_code("SSIS-001/cover.jpg")
    far = extract_code("SSIS-001/CD1/VIDEO_TS/VTS_01_1.VOB")
    assert near.code == far.code == "SSIS-001"
    assert near.confidence > far.confidence


def test_tape_barcode_path_not_mistaken():
    # 磁带根：barcode + 目录层级，文件本身有番号
    m = extract_code("/media/tape/LTO6-A001/2019/SSIS-001.mp4")
    assert m.code == "SSIS-001"
    assert m.source == "basename"


# =============================================================================
# 8. 纯度：不能依赖 flask / sqlalchemy / app.db
# =============================================================================

def test_module_has_no_web_or_db_imports():
    import ast

    src = (ROOT / "app" / "services" / "code_parser.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0].lower() for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0].lower())
    forbidden = {"flask", "sqlalchemy", "flask_sqlalchemy", "app", "requests"}
    assert not (imported & forbidden), f"code_parser 不应 import {sorted(imported & forbidden)}"
    # 只允许标准库（这里只做个下限检查，防止有人 copy 进来第三方依赖）
    assert imported <= {"os", "re", "dataclasses", "typing", "__future__"}, (
        f"code_parser 只应依赖标准库，实际：{sorted(imported)}"
    )


def test_module_public_api():
    for name in ("CodeMatch", "extract_code", "normalize_code", "is_chinese_sub_marker"):
        assert hasattr(cp, name)


# =============================================================================
# 9. 合成语料脚本（scripts/make_corpus.py）
# =============================================================================

def _load_make_corpus():
    path = ROOT / "scripts" / "make_corpus.py"
    spec = importlib.util.spec_from_file_location("make_corpus_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_corpus_plan_counts_and_parseability():
    mod = _load_make_corpus()
    plan = mod.build_corpus_plan(per_tree=300, seed=42)
    assert set(plan) == {"nas", "tape"}

    for tree, entries in plan.items():
        videos = [e for e in entries if e["kind"] == "video"]
        assert len(videos) == 300, f"{tree}: 视频数 {len(videos)}"
        unparseable = [e for e in videos if e["expect_code"] is None]
        ratio = len(unparseable) / len(videos)
        assert 0.07 <= ratio <= 0.13, f"{tree}: 无法识别比例 {ratio:.3f} 应在 10% 左右"
        # 目录树里不允许出现仓库路径（语料必须能在 /tmp 生成）
        paths = [e["rel_path"] for e in entries]
        assert len(paths) == len(set(paths)), f"{tree}: rel_path 有重复"
        for e in entries:
            assert not e["rel_path"].startswith("/")
            assert ".." not in e["rel_path"].split("/")


def test_corpus_plan_matches_parser():
    mod = _load_make_corpus()
    plan = mod.build_corpus_plan(per_tree=300, seed=42)  # 全量语义校验（纯计算，毫秒级）
    mismatches = []
    for tree, entries in plan.items():
        for e in entries:
            got = extract_code(e["rel_path"])
            if got.code != e["expect_code"]:
                mismatches.append((tree, e["rel_path"], e["expect_code"], got.code))
            if e["expect_code"] is not None:
                missing = set(e["expect_flags"]) - got.flags
                if missing:
                    mismatches.append((tree, e["rel_path"], sorted(missing), sorted(got.flags)))
    assert not mismatches, f"语料与解析器不一致 {len(mismatches)} 条，前 10: {mismatches[:10]}"


def test_corpus_writes_two_trees(tmp_path):
    mod = _load_make_corpus()
    out = tmp_path / "corpus"
    summary = mod.generate_corpus(out, per_tree=30, seed=11, clean=True)
    assert summary["videos_per_tree"] == 30
    for tree in ("nas", "tape"):
        root = out / tree
        assert root.is_dir()
        files = [p for p in root.rglob("*") if p.is_file()]
        assert len(files) >= 30, f"{tree}: 只生成 {len(files)} 个文件"
    manifest_path = out / "manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["seed"] == 11
    assert manifest["videos_per_tree"] == 30
    assert len(manifest["entries"]) == sum(len(v) for v in mod.build_corpus_plan(30, 11).values())
    # 语料里每个视频的番号都能被解析器复现
    for e in manifest["entries"]:
        if e["kind"] != "video":
            continue
        assert extract_code(e["rel_path"]).code == e["expect_code"], e["rel_path"]


def test_corpus_refuses_repo_output():
    mod = _load_make_corpus()
    with pytest.raises(SystemExit):
        mod.generate_corpus(ROOT / "corpus-should-not-exist", per_tree=1, seed=1)
    assert not (ROOT / "corpus-should-not-exist").exists()

# =============================================================================
# 10. 真实 dvd_id 基准脚本（scripts/parser_benchmark.py）—— task-14
# =============================================================================

def _load_benchmark():
    name = "parser_benchmark_under_test"
    if name in sys.modules:
        return sys.modules[name]
    path = ROOT / "scripts" / "parser_benchmark.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bm = _load_benchmark()

BENCH_IDS = ["SSIS-001", "C-2391", "100TV-031", "3DSVR-0067", "SD-1106", "DMM-070802", "ADV-R0632"]


def _make_bench_db(path: Path, dvd_ids) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE derived_video (dvd_id TEXT)")
    conn.executemany("INSERT INTO derived_video (dvd_id) VALUES (?)", [(x,) for x in dvd_ids])
    conn.commit()
    conn.close()
    return path


def test_benchmark_fake_db_numbers_match_parser(tmp_path):
    db = _make_bench_db(tmp_path / "r18.db", BENCH_IDS)
    assert bm.sample_dvd_ids(str(db), limit=10) == BENCH_IDS

    report = bm.run_benchmark(BENCH_IDS)
    assert report["primary"]["total"] == len(BENCH_IDS)
    assert report["primary"]["parseable"] == 6  # 只有 ADV-R0632 解析不出
    assert report["primary"]["parse_rate"] == pytest.approx(6 / 7)
    assert report["primary"]["passed"] is False  # 85.7% < 97%
    assert report["dvd_ids"] == len(BENCH_IDS)

    # 分变体数字逐条与解析器对账
    expected = {}
    for dvd_id in BENCH_IDS:
        for item in bm.build_variants(dvd_id):
            stats = expected.setdefault(
                item["variant"], {"total": 0, "parseable": 0}
            )
            stats["total"] += 1
            if extract_code(item["filename"]).code is not None:
                stats["parseable"] += 1
    for stats in report["variants"]:
        exp = expected[stats["variant"]]
        assert stats["total"] == exp["total"]
        assert stats["parseable"] == exp["parseable"]
        assert stats["parse_rate"] == pytest.approx(exp["parseable"] / exp["total"])

    # 失败样本：解析不出的只有 ADV-R0632
    no_parse = report["failures"]["no_parse"]
    assert no_parse and all(item["dvd_id"] == "ADV-R0632" for item in no_parse)
    assert report["failure_prefixes"] == [{"prefix": "ADV", "count": len(no_parse)}]
    # 这份假数据里不该有"解析出但归一化不一致"
    assert report["failures"]["mismatch"] == []

    markdown = bm.render_markdown(report, db_path=str(db))
    for needle in ("番号解析器基准", "## 0. 结论", "未达标", "## 2. 失败样本", "## 3. 失败前缀", "ADV"):
        assert needle in markdown
    assert json.loads(json.dumps(report, ensure_ascii=False))["primary"]["parseable"] == 6


def test_benchmark_passes_on_full_coverage(tmp_path):
    ids = ["SSIS-001", "C-2391", "100TV-031", "SD-1106", "DMM-070802", "UD-330R"]
    report = bm.run_benchmark(ids)
    assert report["primary"]["parseable"] == len(ids)
    assert report["primary"]["passed"] is True
    assert "PASS" in bm.render_markdown(report)
    # 归一化一致：UD-330R -> UD330R 也应对得上
    assert report["primary"]["match_rate"] == pytest.approx(1.0)


def test_benchmark_cli(tmp_path, capsys):
    db = _make_bench_db(tmp_path / "r18.db", BENCH_IDS)
    md = tmp_path / "bench.md"
    js = tmp_path / "bench.json"
    code = bm.main(["--db", str(db), "--limit", "10", "--out", str(md), "--json", str(js)])
    assert code == 0
    assert md.is_file() and js.is_file()
    payload = json.loads(js.read_text(encoding="utf-8"))
    assert payload["primary"]["total"] == len(BENCH_IDS)
    assert "番号解析器基准" in md.read_text(encoding="utf-8")

    # --fail-under：两个方向
    assert bm.main(["--db", str(db), "--limit", "10", "--fail-under", "99",
                    "--out", str(tmp_path / "x.md")]) == 1
    assert "未达标" in capsys.readouterr().err
    assert bm.main(["--db", str(db), "--limit", "10", "--fail-under", "50",
                    "--out", str(tmp_path / "y.md")]) == 0

    # 缺库 / 非法参数
    assert bm.main(["--db", str(tmp_path / "nope.db")]) == 2
    assert "错误" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        bm.main(["--db", str(db), "--limit", "0"])


@pytest.mark.skipif(not (ROOT / "data" / "r18dump.db").is_file(), reason="没有 data/r18dump.db")
def test_benchmark_real_dump_smoke():
    dvd_ids = bm.sample_dvd_ids(str(ROOT / "data" / "r18dump.db"), limit=300)
    assert len(dvd_ids) == 300
    report = bm.run_benchmark(dvd_ids)
    assert report["primary"]["parse_rate"] >= 0.97, report["primary"]
    assert report["primary"]["match_rate"] >= 0.95, report["primary"]
    assert report["primary"]["passed"] is True
    json.dumps(report, ensure_ascii=False)



def test_folder_confirmation_boosts_unknown_prefix():
    """目录名和文件名是同一番号时，未知前缀也应达到自动入库阈值。"""
    from app.services.code_parser import extract_code

    assert extract_code("AGAV-101/AGAV-101.mp4").confidence >= 90
    assert extract_code("AP-324-C/AP-324-C.mp4").confidence >= 90
    # 没有目录确认的未知前缀仍保持低置信度，避免误入库
    assert extract_code("ABC-123 random.mkv").confidence < 90


def test_prefix_folder_confirmation_boosts_confidence():
    """父目录=厂牌前缀（NMSL/NMSL-019.mp4、MIGD/MIGD649.HD.mkv）应可自动入库。"""
    from app.services.code_parser import extract_code

    assert extract_code("NMSL/NMSL-019.mp4").code == "NMSL-019"
    assert extract_code("NMSL/NMSL-019.mp4").confidence >= 90
    assert extract_code("MIGD/MIGD649.HD.mkv").code == "MIGD-649"
    assert extract_code("MIGD/MIGD649.HD.mkv").confidence >= 90
    # 没有父目录确认时仍保守
    assert extract_code("random/NMSL-019.mp4").confidence < 90
