"""番号解析器 —— 从任意脏文件名里抽出「番号」(dvd_id / content code)。

设计目标
--------
1. **纯标准库**，不 import flask / sqlalchemy / app.db，可独立 `python -c` / pytest 导入。
2. **宁可漏也别错**：错配会污染后续 r18 元数据归组，所以拿不准时给低 confidence
   甚至 `code=None`；不追求"任何名字都能出番号"。
3. 对用户库 2–4 万个完全不可控的名字（NAS 下载站风格、磁带归档风格、各种前缀广告、
   中文字幕标记、多版本后缀）做**多层剥离 + 多候选打分**。

对外接口（其它模块只依赖这些）
--------------------------------
    @dataclass CodeMatch: code / confidence / source / raw / flags
    extract_code(path) -> CodeMatch        # 主入口，吃 basename（可选含父目录）
    normalize_code(raw) -> str | None      # 单个番号串归一化
    is_chinese_sub_marker(text) -> bool    # 中字标记判断（L0 字幕层用）
    extract_flags(text) -> set[str]        # 附赠：单独取 flag

归一化后的规范形态（重要，matcher / manifest 都要按这个对）
--------------------------------------------------------
    SSIS-001                   普通 DMM 系：大写前缀 + '-' + 3 位补零
    MIDE-1234                  数字本身 >= 3 位时不截断（只去前导零再补足 3 位）
    FC2-PPV-1234567            FC2（`FC2-1234567`、`fc2ppv1234567` 全归成这个）
    HEYZO-1234                 HEYZO（补 4 位）
    SIRO-1234                  SIRO（补 4 位）
    TOKYO-HOT-N1234            Tokyo-Hot（也支持 K 系列 -> TOKYO-HOT-K1234）
    1PONDO-123456_001          1pondo（主号 6 位 + 下划线 + 分集 3 位）
    10MUSUME-123456_01         10musume（分集 2 位）
    CARIB-123456-001           Caribbean / caribbeancom
    PACOPACOMAMA-123456_001    Pacopacomama
    259LUXU-1234 / 300MIUM-123  带数字前缀的素人厂牌，保留数字前缀（javbus 就是这么用的）
    T28-123                    少数字母+数字前缀（白名单）
    C-2391 / M-823 / H-1238    老 DMM 单字母厂牌（需显式分隔符 + ≥3 位数字；H-264 这类编码除外）
    100TV-031 / 3DSVR-0067     数字/混排前缀 + 短标签（CD5-005 / S4LW-001 / 3DBD-003 同族）
    SD-1106 / SD-0512          老厂牌（SD 不再被画质黑名单误杀；4 位补零号保留前导零）
    DMM-070802 / DM-080302     6 位老番号（允许，但置信度比 3–5 位低）
    UD321R / UD-330R / IBW-123Z  字母前缀 + 数字 + 单字母后缀（R/Z/T 老无码号，整串算番号）

flags 约定
-----------
    chinese_sub  中文字幕版（中文字幕/中字/-C/-CH/-UC/ch/chs/zh ...）
    uncensored   无码/破解/流出（無碼/无码/U/UC/LEAK/流出/破解 ...）
    hi_res       高清/4K/1080p/FHD/UHD/HDR ...
    part         分片/多碟（CD1/PART1/DISC2/-A/-B ...）

注意 `-UC` 在老司机命名里同时表示「无码破解」和「内嵌中字」，所以两个 flag 都打。

confidence 分档建议（调用方按需设阈值）
----------------------------------------
    90–100  干净番号（基名直接命中，前缀已知 + 显式分隔符）
    70–89   大概率正确（粘连写法、特殊厂牌、5 位前导零变体）
    75–82   老 DMM 单字母号（C-2391 = 82；其它字母 = 76）
    72–78   数字/混排前缀（100TV-031 = 78；6 位老番号 DMM-070802 = 72）
    50–69   可疑（未知前缀粘连、多候选冲突、带数字前缀的素人厂牌）
    30–49   只当线索（裸 `n1234`、父目录兜底较深）
    0–10    没有番号 / 垃圾名

一个文件名里出现多个**不同**番号（合集/混剪）时，分数会被压到 68 以下 —— 这类必须进
人工待确认，自动入库只会给其它片子挂错元数据。

`raw` 字段 = 命中的原始子串（保留原始大小写、是入参 path 的真子串；`code is None` 时为空串），
便于 alias 学习与 UI 调试。噪声词只在匹配用的临时副本里被抹成空格，raw 始终回原文取。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Set, Tuple

__all__ = [
    "CodeMatch",
    "extract_code",
    "normalize_code",
    "is_chinese_sub_marker",
    "extract_flags",
    "FLAG_CHINESE_SUB",
    "FLAG_UNCENSORED",
    "FLAG_HI_RES",
    "FLAG_PART",
]

# ---------------------------------------------------------------------------
# flags
# ---------------------------------------------------------------------------

FLAG_CHINESE_SUB = "chinese_sub"
FLAG_UNCENSORED = "uncensored"
FLAG_HI_RES = "hi_res"
FLAG_PART = "part"

# ---------------------------------------------------------------------------
# 已知 DMM 系番号前缀（只用来抬 confidence，不做硬门槛）
# 未知前缀不是错误 —— 每月都有新厂牌，但未知前缀的粘连写法会被降分。
# ---------------------------------------------------------------------------

KNOWN_PREFIXES = frozenset(
    """
    SSIS SSNI SSIS S1 SONE SONE SONE SONE SOE SNIS SIVR SIVR
    ABP ABW ABX ADN ALDN ATOM AUKG AUKS
    IPX IPZ IPTD IPZZ IPXR IPT
    MIDE MIDV MIAA MIMK MIRD MISM MIZD MIGD MIAE MIKA MKMP MMT MNG MUDR
    JUQ JUL JUX JULIA JBS JKNK
    PRED PRTD PRST PRBY
    STARS STAR START
    DASD DASS DLDSS DVAJ DVDMS DVRT
    EBOD EKDV EKDK
    FSDSS FTHT FSET
    GIGL GVH GVG GMEM
    HND HODV HOMA HONB HUNTA HOIZ
    IENE IENF
    KAM KAVR KBI KMI KSB KUSE KIR
    MDB MDTM MEYD MHND
    NATR NEO NGOD NHDTA NITR NSPS NTR NKKD NIMA NNPJ
    OFJE ONSD
    PPPD PPPE PXH PGD
    RBD RCT REBD REAL ROYD RKI
    SDDE SDJS SDMU SGK SHKD SIRD SKMJ SNIS SUPD SW
    T28 T26 T27 T29 T38 TAMO TEK TKI TMRD TPPN TUE TYOD
    UMSO URE
    VAGU VEC VENU VRTM VDD
    WANZ WSA
    XRW
    YMDD YRH
    ZUKO ZEX ZMEN
    AARM ABSS ACMD AQSH AROMA AVOP AVSA
    BBIK BOKD BF
    CHN CEMD CHRV
    DDT DMOW
    EBWH EYAN
    GENM
    HAVD HHH HITMA HTMS HUNT
    IBW IPIT IRO
    JFB JYMA
    KAAD KATU KH KIRAY
    LULU LUXU
    MBR MCON MFIN MGR MIA MIFD MIMK MIST MIZU MOGI MOKO MSG MSZ MUKD
    NASK NEO NHDTA NIKO NKT
    OKS OKSN OY
    PKPD PLOT
    REBDB RIXA RUMI
    SABA SACU SAN SAVA SGA SGR SGS SIS SONE SOR SSNI SUKE
    TD TIKB TINY TKA TOEN TOMN TOTTE TPPN TSF TSUB
    UMD USBA
    VEMA VNDS VRTM
    WAAP WAAA
    XS
    YAG YST
    ZAM
    """.split()
)

# 粘连写法的兜底白名单：字母+数字前缀（T28-123 这类）。必须显式列出，否则 X264 会被误判。
ALNUM_PREFIXES = frozenset({"t26", "t27", "t28", "t29", "t30", "t38"})

# 带数字前缀的素人厂牌（259LUXU / 300MIUM / 200GANA ...）
KNOWN_NUMERIC_LABELS = frozenset(
    {"LUXU", "MIUM", "GANA", "ARA", "EVA", "KNB", "DCV", "JAC", "MFC", "MBR", "GACHI", "CANDY", "PACO"}
)

# 老 DMM 单字母番号（C-2391 / M-823 / H-1238）。真实 dump 里 C/M/H 最常见。
SINGLE_LETTER_PREFIXES = frozenset({"C", "M", "H"})
SINGLE_LETTER_MIN_DIGITS = 3
# 单字母 + 264/265/263 是编码器写法（H-264 / X-265），不是番号
_SINGLE_LETTER_CODEC_NUMBERS = frozenset({"263", "264", "265"})

# 分辨率数字：画质前缀 + 这些数字（SD-1080）是噪声而不是番号
RESOLUTION_NUMBERS = frozenset(
    {"240", "360", "480", "540", "576", "720", "1080", "1440", "2160", "4320"}
)
QUALITY_PREFIXES = frozenset({"SD", "HD", "FHD", "UHD", "HDR", "SDR", "WEB", "BD", "DVD", "4K", "8K"})

# 6 位数字（`DMM-070802` 这类老 DMM 日期型）允许，但要降置信度、且不允许粘连写法
MAX_NUMBER_DIGITS = 6

# 数字/混排前缀（100TV-031 / 3DSVR-0067 / 3DBD-003 / CD5-005 / S4LW-001）里
# 这些 token 是画质/编码/音频/分辨率标记，不是番号
_MIXED_NOISE_TOKEN_RE = re.compile(
    r"^(?:"
    r"[248]k|"                    # 4k / 8k / 2k
    r"\d{3,4}[pi]|"               # 1080p / 720i
    r"\d{2,3}fps|"                # 24fps / 60fps
    r"\d{1,2}bit|"                # 10bit / 8bit
    r"[xh]26[45]|"                 # x264 / h265
    r"(?:aac|ac3|eac3|dts|flac|mp3|opus|truehd)\d?|"
    r"(?:av1|vp9|xvid|divx)|"
    r"(?:web|webrip|hdtv|bdrip)|"
    r"(?:hdr|sdr)\d*|"
    r"(?:dsc|dscf|img|mvi|vid|pxl)\d*|"
    r"\d{1,2}ch|"
    r"(?:2d|3d|uhd|fhd|fullhd|hd|sd|bd|dvd|cd|cm|new|old|avc|hevc)"
    r")$",
    re.IGNORECASE,
)

# 分辨率写法 `1920x1080` / `1920×1080`：整段抹掉，防止被混排规则拆成 `1920X-1080`
_RESOLUTION_RE = re.compile(r"\d{3,4}\s*[x×*]\s*\d{3,4}", re.IGNORECASE)

# 这些「前缀 + 数字」的组合是噪声，不是番号（分辨率/编码/文件名/广告站/磁带卷名/相机名）。
DROP_PREFIXES = frozenset(
    """
    HHD HHB THZU JAVDB JAVBUS AVMOO AVSOX SUKEBEI SEHUA T66Y NYAP
    VTS VOB BDMV IMG DSC DSCF PXL MVI VID TAPE LTO VOL DISC DISK PART CD PT
    EP SP OP ED NC PV TAK CHAPTER TITLE BONUS EXTRA EXTENDED UNCUT REPACK
    PROPER REMUX HDTV HDRIP DVDRIP BDRIP BRRIP BLURAY BD DVD RIP
    WEB WEBRIP WEBDL FHD UHD HD HDR SDR
    X264 X265 H264 H265 HEVC AVC AV1 XVID DIVX VP9
    AAC AC3 EAC3 DTS FLAC MP3 OPUS TRUEHD
    MP4 MKV AVI WMV MOV RMVB RM FLV WEBM M4V MPG MPEG TS M2TS ISO
    MOVIE FILM VIDEO CLIP SAMPLE TRAILER PREVIEW TEASER FPS BIT BITRATE
    FILE TEMP TMP DUP COPY OLD
    AUDIO SUB SUBS SRT ASS SSA NFO JPG JPEG PNG GIF
    CH CHS CHT CHI ENG JPN JPSC
    """.split()
)

# 常见下载站域名后缀（用于剥 `hhd800.com@` 这类前缀）
_DOMAIN_TLDS = (
    "com|net|org|cc|tv|me|xyz|top|info|vip|biz|club|site|online|la|ws|jp|cn|io|to|"
    "co|mm|us|ru|pw|sh|ly|so|in|hk|tw|app|link|pro|moe|fun|live|store|work|space|"
    "website|asia|de|uk|ru|su|nu|bid|win|review|download|stream|porn|sex|adult"
)

_URL_RE = re.compile(r"(?:https?://|ftp://)\S+", re.IGNORECASE)
_DOMAIN_RE = re.compile(
    r"(?<![a-z0-9])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:%s)(?![a-z0-9])" % _DOMAIN_TLDS,
    re.IGNORECASE,
)

# 左/右边界用 ASCII 判断（不用 \b，因为中文是 Unicode word char，会让 `中字4K` 剥不掉）
_L = r"(?<![a-z0-9])"
_R = r"(?![a-z0-9])"


def _word_re(words: str, extra: str = "") -> re.Pattern:
    """左边界 + 词表 + 右边界(+附加约束)，统一 IGNORECASE。"""
    return re.compile(r"%s(?:%s)%s%s" % (_L, words, _R, extra), re.IGNORECASE)


# 纯噪声词：出现即替换成空格，避免它们组合出假番号
_NOISE_WORD_RE = _word_re(
    r"4k|8k|2160p|1440p|1080p|1080i|720p|576p|480p|uhd|fhd|hdr10\+?|hdr|sdr|"
    r"fullhd|full-hd|web[-. ]?dl|webrip|web-?rip|bluray|blu[-. ]?ray|bdrip|brrip|"
    r"dvdrip|hdtv|hdrip|remux|repack|proper|"
    r"hevc|avc|av1|xvid|divx|vp9|x264|x265|h\.?264|h\.?265|10bit|8bit|hi10p|"
    r"aac|ac3|eac3|dts|dtshd|truehd|flac|mp3|opus|"
    r"uncensored|decensored|censored|leaked|leak|mosaic|"
    r"chinese|japanese|english|subbed|dubbed|multi|dual|"
    r"mp4|mkv|avi|wmv|mov|rmvb|flv|webm|m4v|mpg|mpeg|m2ts|vob|iso|"
    r"sample|trailer|preview|teaser|bonus|extra|feature|"
    r"hd"
)

# CD1 / PART2 / DISC3 / PT1（也允许紧贴在番号数字后面：SSIS-001CD1）
_NOISE_DISC_RE = _word_re(
    r"(?:cd|part|pt|disc|disk)[-_. ]?0?[1-9]", r"(?![0-9])(?![-_. ]?[0-9])"
)
_NOISE_DISC_GLUED_RE = re.compile(
    r"(?<=[0-9])(?:cd|part|pt)[-_. ]?0?[1-9](?![0-9])(?![-_. ]?[0-9])", re.IGNORECASE
)

# 点 / 加号 / @ 统一当分隔符（`_` 保留给 raw 原貌，各正则里显式接受）
_SEP_RE = re.compile(r"[.+@]+")

# 番号里可能出现的分隔符（`SSIS-001` / `SSIS_001` / `SSIS 001` 都算显式分隔）
_S = r"[-_\s]"


def _rx(body: str) -> "re.Pattern":
    """用 {L}/{R}/{S} 占位符拼正则，统一 IGNORECASE。"""
    raw = body.replace("{L}", _L).replace("{R}", _R).replace("{S}", _S)
    return re.compile(raw, re.IGNORECASE)


# ---------------------------------------------------------------------------
# 特殊厂牌（必须排在普通正则前面）
# ---------------------------------------------------------------------------

# FC2：FC2-PPV-1234567 / FC2PPV-1234567 / fc2ppv1234567 / FC2-1234567
_FC2_RE = _rx(r"{L}fc2{S}*(?:ppv)?{S}*(\d{5,8})(?![0-9])")
_FC2_PART_RE = re.compile(r"[-_\s]([1-9])(?![0-9])")

# HEYZO：HEYZO-1234 / heyzo 1234 / heyzo_hd_1234
_HEYZO_RE = _rx(r"{L}heyzo{S}*(?:hd{S}*)?(\d{2,5}){R}")
# SIRO：SIRO-1234 / siro1234
_SIRO_RE = _rx(r"{L}siro{S}*(\d{2,5}){R}")

# Tokyo-Hot：`Tokyo-Hot n1234` / `tokyohot_n1234`；裸 `n1234` 另算低置信
_TOKYO_CONTEXT_RE = re.compile(r"(?:tokyo[-_\s]?hot|tokyohot|東京熱|东热|東熱)", re.IGNORECASE)
_TOKYO_ID_RE = _rx(r"{L}([nk]){S}*(\d{3,4}){R}")

# Caribbean：Caribbean 123456-001 / carib-123456-001 / caribbeancom_123456_001
_CARIB_RE = _rx(r"{L}(?:caribbean(?:com)?|carib|カリビアン){S}*(\d{6}){S}*(\d{2,3})?{R}")
# 1pondo：1pondo 123456_001 / 1pondo-123456-001
_PONDO_RE = _rx(r"{L}(?:1{S}*pondo|1pon|いちぽんど){S}*(\d{6}){S}*(\d{2,3})?{R}")
# 10musume：10musume 123456_01
_MUSUME_RE = _rx(r"{L}(?:10{S}*musume|10musume){S}*(\d{6}){S}*(\d{1,2})?{R}")
# Pacopacomama：Pacopacomama 123456_001 / paco-123456-001
_PACO_RE = _rx(r"{L}(?:pacopacomama|paco){S}*(\d{6}){S}*(\d{1,3})?{R}")

# 数字前缀素人厂牌：259LUXU-1234 / 300MIUM-123
_NUMLABEL_RE = _rx(r"{L}(\d{2,3}){S}*([a-z]{2,5}){S}*(\d{2,5}){R}")

# 字母+数字前缀白名单（T28-123）
_ALNUM_RE = _rx(r"{L}([a-z]\d{2}){S}*(\d{2,4}){R}")

# 单字母厂牌：`C-2391` / `M-823` / `H-1238`。必须显式分隔符 + >=3 位数字，
# 这样 `X-1` / `CD-2`（分片）/ `A-1` 不会被误判。
_SINGLE_LETTER_RE = _rx(r"{L}([a-z]){S}+(\d{3,6})(?![0-9a-z])")

# 数字/混排前缀：`100TV-031` / `3DSVR-0067` / `3DBD-003` / `CD5-005` / `S4LW-001`。
# 必须显式分隔符（`DSC01234`、`AB12345` 这类粘连名因此不会被拆成番号）。
_MIXED_PREFIX_RE = _rx(r"{L}([a-z0-9]{2,7}){S}+(\d{2,6})(?![0-9])")

# 老无码/素人号：`UD321R` / `UD-330R` / `IBW-123Z` —— 2~3 字母 + 3~4 位数字 +
# 单字母后缀（R/Z/T），整串是一个番号，不能拆成 `UD-321`（拆了 r18 dump 匹配不上）。
# 排除 A/B/C/U（那是分片/中字/无码后缀，不是番号的一部分）。
_TRAILING_LETTER_RE = _rx(r"{L}([a-z]{2,3}){S}?(\d{3,4})([rzt])(?![0-9a-z])")

# 普通番号：2-6 个字母 + 可选分隔符 + 2-6 位数字
# 末尾只排除数字，允许粘着广告字母（hhd800.com@ssis00001hhb 里的 `ssis00001`）
_GENERIC_RE = _rx(r"{L}([a-z]{2,6}){S}*(\d{2,6})(?![0-9])")

# 补零宽度：特殊厂牌各自不同
_WIDTH_GENERIC = 3
_WIDTH_HEYZO = 4
_WIDTH_SIRO = 4
_WIDTH_TOKYO = 4

_ACCEPT_BASENAME = 40  # 基名候选低于这个分直接当没番号
_PARENT_KNOWN_MIN = 62  # 已知前缀 / 特殊厂牌走父目录兜底的最低分
_PARENT_UNKNOWN_MIN = 82  # 未知前缀（粘连写法）父目录兜底要更高分


@dataclass
class CodeMatch:
    """一次解析的结果。"""

    code: Optional[str]
    confidence: int
    source: str  # 'basename' | 'parent' | 'none'
    raw: str
    flags: Set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.confidence = int(self.confidence)
        if self.code is not None:
            self.code = self.code.strip() or None
        if not isinstance(self.flags, set):
            self.flags = set(self.flags)

    # 便捷判断（UI / 扫描器直接用）
    @property
    def is_confident(self) -> bool:
        return self.code is not None and self.confidence >= 70

    def has_flag(self, flag: str) -> bool:
        return flag in self.flags


@dataclass(frozen=True)
class _Candidate:
    code: str
    raw: str
    start: int
    end: int
    score: int
    kind: str
    prefix: str = ""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _pad(num: str, width: int) -> str:
    """去前导零后补足 width 位；本身已 >= width 位则原样（不截断）。

    特例：位数 > width 且带前导零的「老番号」保留原样（``SD-0512`` / ``DMM-070802``），
    因为 r18 dump 的 dvd_id 就是这么写的，抹掉前导零会匹配不上；
    而 ``ssis00001`` 这种「把 3 位号补到 5 位」的写法仍会被还原成 ``SSIS-001``。
    """
    n = num.lstrip("0") or "0"
    if len(n) < width:
        return n.zfill(width)
    # 4 位带前导零（SD-0512）/ 6 位带前导零（DMM-070802）保留原样；
    # 5 位前导零则视为「把 3 位号补到 5 位」（00578 -> 578），照旧还原。
    if len(num) == 4 and num.startswith("0") and len(n) >= 3:
        return num
    if len(num) >= 6 and num.startswith("0"):
        return num
    return n


def _pad_mixed(num: str, width: int = 3) -> str:
    """混排前缀厂牌（100TV/3DSVR/CD5/S4LW）用的补零规则。

    这类厂牌的真实 dvd_id 常是 4 位补零（``3DSVR-0067``），所以原样保留 >=4 位的
    前导零写法；3 位以内仍按普通规则补到 3 位。
    """
    if len(num) >= 4 and num.startswith("0"):
        return num
    n = num.lstrip("0") or "0"
    if len(n) < width:
        return n.zfill(width)
    return n


def _split_path(path: str) -> Tuple[str, List[str]]:
    """拆成 (basename, [父目录链])，兼容 POSIX / Windows 分隔符和尾斜杠。"""
    raw = str(path or "")
    parts = [p for p in re.split(r"[\\/]+", raw) if p not in ("", ".", "..")]
    if not parts:
        return "", []
    return parts[-1], parts[:-1]


def _blank(m: "re.Match") -> str:
    """把匹配段替换成等长空格 —— 保证清洗后下标与原文 1:1，raw 才能取回原名子串。"""
    return " " * len(m.group(0))


def _clean(text: str) -> str:
    """把一段名字（basename 或目录名）清成"只剩可能构成番号的字符 + 空格"。

    所有替换都是**等长**的（噪声变空格），所以候选的 start/end 可以直接映射回原文本。
    """
    t = _URL_RE.sub(_blank, text)
    t = _DOMAIN_RE.sub(_blank, t)
    t = _SEP_RE.sub(_blank, t)  # . + @ -> 空格（`_` 保留，各正则显式接受）
    t = _RESOLUTION_RE.sub(_blank, t)  # 1920x1080 整段抹掉
    t = _NOISE_WORD_RE.sub(_blank, t)
    t = _NOISE_DISC_GLUED_RE.sub(_blank, t)
    t = _NOISE_DISC_RE.sub(_blank, t)
    return t


def _strip_ext(basename: str) -> str:
    """去最后一层扩展名；`.part` / 多级扩展（SSIS-001.chs.srt）保留前面的部分。"""
    stem, _ext = os.path.splitext(basename)
    return stem or basename


def _ctx_has_tokyo(text: str) -> bool:
    return bool(_TOKYO_CONTEXT_RE.search(text))


# ---------------------------------------------------------------------------
# 候选生成
# ---------------------------------------------------------------------------


def _mk(
    code: str,
    raw: str,
    m: "re.Match",
    score: int,
    kind: str,
    prefix: str = "",
) -> _Candidate:
    return _Candidate(code=code, raw=raw, start=m.start(), end=m.end(), score=score, kind=kind, prefix=prefix)


def _special_candidates(text: str, ctx: str) -> List[_Candidate]:
    out: List[_Candidate] = []

    # --- FC2 ---
    for m in _FC2_RE.finditer(text):
        num = m.group(1).lstrip("0") or "0"
        score = 96
        tail = text[m.end() : m.end() + 4]
        pm = _FC2_PART_RE.match(tail)
        if pm:
            score -= 1
        out.append(_mk("FC2-PPV-" + num, m.group(0), m, score, "fc2"))

    # --- HEYZO ---
    for m in _HEYZO_RE.finditer(text):
        num = _pad(m.group(1), _WIDTH_HEYZO)
        out.append(_mk("HEYZO-" + num, m.group(0), m, 92, "heyzo"))

    # --- SIRO ---
    for m in _SIRO_RE.finditer(text):
        num = _pad(m.group(1), _WIDTH_SIRO)
        out.append(_mk("SIRO-" + num, m.group(0), m, 90, "siro"))

    # --- Caribbean ---
    for m in _CARIB_RE.finditer(text):
        main = m.group(1)
        part = m.group(2)
        code = "CARIB-" + main + ("-" + _pad(part, 3) if part else "")
        out.append(_mk(code, m.group(0), m, 92, "carib"))

    # --- 1pondo ---
    for m in _PONDO_RE.finditer(text):
        main = m.group(1)
        part = m.group(2)
        code = "1PONDO-" + main + ("_" + _pad(part, 3) if part else "")
        out.append(_mk(code, m.group(0), m, 92, "pondo"))

    # --- 10musume ---
    for m in _MUSUME_RE.finditer(text):
        main = m.group(1)
        part = m.group(2)
        code = "10MUSUME-" + main + ("_" + _pad(part, 2) if part else "")
        out.append(_mk(code, m.group(0), m, 92, "musume"))

    # --- Pacopacomama ---
    for m in _PACO_RE.finditer(text):
        main = m.group(1)
        part = m.group(2)
        code = "PACOPACOMAMA-" + main + ("_" + _pad(part, 3) if part else "")
        out.append(_mk(code, m.group(0), m, 92, "paco"))

    # --- Tokyo-Hot ---
    tokyo_ctx = _ctx_has_tokyo(text) or _ctx_has_tokyo(ctx)
    for m in _TOKYO_ID_RE.finditer(text):
        letter = m.group(1).upper()
        num = _pad(m.group(2), _WIDTH_TOKYO)
        code = "TOKYO-HOT-%s%s" % (letter, num)
        if tokyo_ctx:
            out.append(_mk(code, m.group(0), m, 90, "tokyo_hot"))
        else:
            # 裸 n1234：只当线索。整段基本就是它（允许 (2)/空格等非字母尾巴）→ 45；否则 34
            whole = re.fullmatch(r"[^a-z]*[nk][-\s]?\d{3,4}[^a-z]*", text, re.IGNORECASE)
            score = 45 if whole else 34
            out.append(_mk(code, m.group(0), m, score, "tokyo_hot_bare"))

    # --- 带数字前缀的素人厂牌（259LUXU-1234 / 300MIUM-123） ---
    for m in _NUMLABEL_RE.finditer(text):
        num_prefix, label, num = m.group(1), m.group(2).upper(), m.group(3)
        if label in ("PONDO", "MUSUME") or label in DROP_PREFIXES or len(label) < 3:
            # `24FPS-030` / `12BIT-8` 这类不是番号
            continue
        known = label in KNOWN_NUMERIC_LABELS
        # 已知素人厂牌要压过普通规则里的「LUXU-1234」候选（javbus 用带数字前缀的全称）
        score = 94 if known else 76
        code = "%s%s-%s" % (num_prefix, label, _pad(num, _WIDTH_GENERIC))
        out.append(_mk(code, m.group(0), m, score, "numlabel_known" if known else "numlabel"))

    # --- 字母+数字前缀白名单（T28-123） ---
    for m in _ALNUM_RE.finditer(text):
        prefix = m.group(1).lower()
        if prefix not in ALNUM_PREFIXES:
            continue
        code = "%s-%s" % (prefix.upper(), _pad(m.group(2), _WIDTH_GENERIC))
        out.append(_mk(code, m.group(0), m, 84, "alnum", prefix=prefix.upper()))

    return out


def _single_letter_candidates(text: str) -> List[_Candidate]:
    """老 DMM 单字母番号：`C-2391` / `M-823` / `H-1238`。

    置信度 82（C/M/H，真实 dump 里最常见的三个）/ 76（其它单字母）。
    显式排除 `H-264` / `X-265` 这类编码器写法（看数字而不是看字母）。
    """
    out: List[_Candidate] = []
    for m in _SINGLE_LETTER_RE.finditer(text):
        letter = m.group(1).upper()
        raw_num = m.group(2)
        if raw_num.lstrip("0") in _SINGLE_LETTER_CODEC_NUMBERS:
            continue  # H-264 / X-265 / H-263：编码器，不是番号
        score = 82 if letter in SINGLE_LETTER_PREFIXES else 76
        out.append(_mk("%s-%s" % (letter, _pad(raw_num, _WIDTH_GENERIC)), m.group(0), m, score, "single_letter"))
    return out


def _mixed_prefix_candidates(text: str) -> List[_Candidate]:
    """数字/字母混排前缀：`100TV-031` / `3DSVR-0067` / `3DBD-003` / `CD5-005` / `S4LW-001`。

    - token 必须同时含字母和数字（纯字母走普通规则，纯数字不是番号）；
    - 必须显式分隔符（`DSC01234` / `AB12345` 这类粘连名不会被拆）；
    - 画质/编码/音频/分辨率 token 一律拒绝（1080P / X264 / 4K / 24FPS / HDR10 ...）；
    - 已知素人厂牌（259LUXU/300MIUM/200GANA）走 94 分，其余 78 分；
    - 6 位数字（老 DMM 日期型）降 8 分。
    """
    out: List[_Candidate] = []
    for m in _MIXED_PREFIX_RE.finditer(text):
        token = m.group(1)
        raw_num = m.group(2)
        if not any(ch.isdigit() for ch in token) or not any(ch.isalpha() for ch in token):
            continue  # 纯字母 / 纯数字交给别的规则
        upper = token.upper()
        if upper in DROP_PREFIXES or _MIXED_NOISE_TOKEN_RE.match(upper):
            continue
        label = "".join(ch for ch in upper if ch.isalpha())
        known = label in KNOWN_NUMERIC_LABELS
        score = 94 if known else 78
        if len(raw_num) >= 6:
            score -= 8
        code = "%s-%s" % (upper, _pad_mixed(raw_num))
        out.append(_mk(code, m.group(0), m, score, "numlabel_known" if known else "mixed"))
    return out


def _trailing_letter_candidates(text: str) -> List[_Candidate]:
    """`UD321R` / `UD330R` 这类「字母前缀 + 数字 + 单字母后缀」的整串番号。

    置信度 76：比普通粘连规则高（整串形态很特殊），但低于标准 `PREFIX-NNN`。
    """
    out: List[_Candidate] = []
    for m in _TRAILING_LETTER_RE.finditer(text):
        prefix = m.group(1).upper()
        number = m.group(2)
        suffix = m.group(3).upper()
        if prefix in DROP_PREFIXES:
            continue
        if number.lstrip("0") in RESOLUTION_NUMBERS:
            continue
        # 已知前缀（IBW 等）要能压过普通规则判出的 `IBW-123`；未知前缀（UD 等）
        # 也要压过 `UD-330`，否则后缀字母 R/Z/T 会被丢掉、r18 匹配不上。
        score = 93 if prefix in KNOWN_PREFIXES else 86
        out.append(
            _mk(prefix + number + suffix, m.group(0), m, score, "trailing_letter", prefix=prefix)
        )
    return out


def _generic_candidates(text: str) -> List[_Candidate]:
    out: List[_Candidate] = []
    for m in _GENERIC_RE.finditer(text):
        prefix = m.group(1).upper()
        raw_num = m.group(2)
        if prefix in DROP_PREFIXES:
            continue
        # 分隔符：`SSIS-001` / `SSIS 001` vs 粘连 `ssis001`
        between = text[m.start(1) + len(m.group(1)) : m.start(2)]
        has_sep = between != "" and re.fullmatch(r"[-_\s]+", between) is not None

        if prefix in QUALITY_PREFIXES and raw_num.lstrip("0") in RESOLUTION_NUMBERS:
            continue  # `SD-1080` 这类画质标记不是番号
        if len(raw_num) >= MAX_NUMBER_DIGITS and not has_sep:
            continue  # 6 位数字只认显式分隔写法，粘连 6 位更像随机串

        known = prefix in KNOWN_PREFIXES
        score = 86 if known else 74
        score += 6 if has_sep else -10
        # 基名开头就是 `PREFIX-<3位数字>`，且同串里带明显视频发布信息（1080p/WEB-DL/
        # DMM/年份等）：即使前缀没收录也应当能自动入库（ALDN-336.2024.1080p.DMM...），
        # 而不是被 90 阈值挡掉。没有这些上下文时仍按未知前缀 80 分处理。
        has_av_context = bool(
            re.search(
                r"(?i)(1080p|2160p|720p|480p|4k|web[-_. ]?dl|dmm|h\.?26[45]|x26[45]|"
                r"aac|hevc|fhd|uhd|uncensored|chinese|中字|中文字幕|無碼|无码|破解|流出)",
                text,
            )
        )
        if (
            has_sep
            and m.start(1) == 0
            and prefix.isalpha()
            and len(raw_num) == 3
            and (known or has_av_context)
        ):
            score = max(score, 92)
        over_padded = (
            len(raw_num) in (5, 6)
            and raw_num.startswith("0")
            and len(raw_num.lstrip("0")) <= 3
        )
        if over_padded:
            score += 6  # 3 位号补到 5/6 位的变体（ssis00001）是很强的 JAV 信号
        elif not known and len(raw_num) == 4 and 1900 <= int(raw_num) <= 2099:
            score -= 28  # 未知前缀 + 年份 → 多半是 "movie 2024" 这类名字
        if not known and not has_sep and len(raw_num) >= 4:
            score -= 12  # 未知前缀 + 粘连 + 4 位以上 → 磁带条码 / 随机串
        if len(raw_num) >= MAX_NUMBER_DIGITS:
            score -= 8  # 6 位老番号（DMM-070802）能用，但置信度低一点
        num = _pad(raw_num, _WIDTH_GENERIC)
        out.append(_mk("%s-%s" % (prefix, num), m.group(0), m, score, "generic", prefix=prefix))
    return out


def _dedupe(cands: List[_Candidate]) -> List[_Candidate]:
    """同一段文字被多个规则命中时，保留分高/更长的那个（避免 FC2 再被拆成子串）。"""
    ordered = sorted(cands, key=lambda c: (-c.score, -(c.end - c.start), c.start))
    kept: List[_Candidate] = []
    for c in ordered:
        if any(not (c.end <= k.start or c.start >= k.end) for k in kept):
            continue
        kept.append(c)
    return kept


def _find_candidates(text: str, ctx: str = "") -> List[_Candidate]:
    cleaned = _clean(text)
    if not cleaned.strip():
        return []
    cands = _special_candidates(cleaned, _clean(ctx))
    cands.extend(_single_letter_candidates(cleaned))
    cands.extend(_mixed_prefix_candidates(cleaned))
    cands.extend(_trailing_letter_candidates(cleaned))
    cands.extend(_generic_candidates(cleaned))
    cands = _dedupe(cands)
    # 清洗是等长替换（噪声 -> 空格），所以下标能 1:1 映射回原文：raw 取原文子串
    if len(cleaned) == len(text):
        cands = [
            _Candidate(c.code, text[c.start : c.end], c.start, c.end, c.score, c.kind, c.prefix)
            for c in cands
        ]
    return cands


def _best(text: str, ctx: str = "") -> Optional[_Candidate]:
    cands = _find_candidates(text, ctx)
    if not cands:
        return None
    cands.sort(key=lambda c: (-c.score, c.start, -(c.end - c.start)))
    best = cands[0]
    # 同 code 的重复命中不算冲突（`SSIS-001 SSIS-001.mp4`、方括号里又写一遍）
    rivals = [c for c in cands[1:] if c.code != best.code and c.score >= best.score - 10]
    if rivals:
        # 一个文件名里出现多个不同番号 = 合集/混剪，选谁都有一半错。
        # 压到 68 以下，让它进「待确认」，绝不自动入库。
        score = min(best.score, 68) - 3 * (len(rivals) - 1)
        best = _Candidate(
            code=best.code,
            raw=best.raw,
            start=best.start,
            end=best.end,
            score=max(5, score),
            kind=best.kind,
            prefix=best.prefix,
        )
    return best


# ---------------------------------------------------------------------------
# flags
# ---------------------------------------------------------------------------

_CHINESE_SUB_RES = [
    re.compile(
        r"中文字幕|中字版|中字|中文|简中|簡中|繁中|繁体|繁體|简体|簡體|内嵌字幕|内挂字幕|"
        r"内嵌中字|有字幕|字幕|中文配音|中文字?幕"
    ),
    re.compile(r"%s(?:chs|cht|chi|chinese|chsub|ch[-\s]?sub|subbed|zh)(?:[-\s]?(?:cn|tw|hans|hant|hk))?%s" % (_L, _R), re.IGNORECASE),
    re.compile(r"%sch%s" % (_L, _R), re.IGNORECASE),
    # 单 `c` 必须是分隔符开头的标记（`SSIS-001-C`），不能是 `C-2391` 的厂牌
    re.compile(r"(?<=[^a-z0-9])c(?![a-z0-9])", re.IGNORECASE),
    # 粘连写法 ssis001c / abc-123c（排除 x264c / h264c 这类编码尾巴）
    re.compile(r"(?<![a-z])(?<=[0-9])c(?![a-z0-9])", re.IGNORECASE),
    re.compile(r"%suc%s" % (_L, _R), re.IGNORECASE),
]

_UNCENSORED_RES = [
    re.compile(
        r"無碼|无码|無修正|无修正|無碼破解|无码破解|破解|流出|無修正版|"
        r"uncensored|decensored|leaked|leak",
        re.IGNORECASE,
    ),
    # 单/双字母无码标记要求前面是分隔符（`SSIS-001-U`），
    # 避免把 `U-123` 这种单字母厂牌当无码标记
    re.compile(r"(?<=[^a-z0-9])(?:u|uc|un|unc|uncen)(?![a-z0-9])", re.IGNORECASE),
]

_HI_RES_RES = [
    re.compile(
        r"%s(?:4k|8k|2160p|1440p|1080p|1080i|720p|uhd|fhd|hdr10\+?|hdr|hd|fullhd|bluray|blu-ray|remux)%s"
        % (_L, _R),
        re.IGNORECASE,
    ),
]

_PART_RES = [
    # 只有「后面不跟数字」时才当分片：`SSIS-001-CD1` 是分片，`CD5-005` 是番号
    re.compile(
        r"%s(?:cd|part|pt|disc|disk)[-_. ]?0?[1-9]%s%s"
        % (_L, r"(?![0-9])(?![-_. ]?[0-9])", ""),
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<=[0-9])(?:cd|part|pt)[-_. ]?0?[1-9](?![0-9])(?![-_. ]?[0-9])", re.IGNORECASE
    ),
    # 单字母分片要求前面是分隔符（`-A`/`-B`），避免把 `A-123` 的厂牌当分片
    re.compile(r"(?<=[^a-z0-9])[ab](?![a-z0-9])", re.IGNORECASE),
]


def extract_flags(text: str) -> Set[str]:
    """从任意文本（文件名 / 目录名 / 标签串）里抽 flags。"""
    if not text:
        return set()
    flags: Set[str] = set()
    if any(rx.search(text) for rx in _CHINESE_SUB_RES):
        flags.add(FLAG_CHINESE_SUB)
    if any(rx.search(text) for rx in _UNCENSORED_RES):
        flags.add(FLAG_UNCENSORED)
    if any(rx.search(text) for rx in _HI_RES_RES):
        flags.add(FLAG_HI_RES)
    if any(rx.search(text) for rx in _PART_RES):
        flags.add(FLAG_PART)
    return flags


def is_chinese_sub_marker(text: str) -> bool:
    """`中文字幕 / 中字 / -C / -CH / UC / chs / zh` 等中字标记判断（L0 字幕层用）。"""
    if not text:
        return False
    return any(rx.search(text) for rx in _CHINESE_SUB_RES)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def _parent_candidate(parents: Sequence[str], ctx: str) -> Tuple[Optional[_Candidate], int]:
    """从最近的上层目录向上找（最多 3 层），返回 (候选, 层深)。"""
    for depth, name in enumerate(reversed(list(parents)[-3:])):
        cand = _best(name, ctx)
        if cand is not None:
            return cand, depth
    return None, 0


def _parent_ok(cand: _Candidate) -> bool:
    if cand.kind == "generic" and cand.prefix not in KNOWN_PREFIXES:
        return cand.score >= _PARENT_UNKNOWN_MIN
    return cand.score >= _PARENT_KNOWN_MIN


def extract_code(path: str) -> CodeMatch:
    """从路径（或单个文件名）解析番号。

    - 先看 basename；没有可信番号再看父目录（最多 3 层，用于
      `SSIS-001/cover.jpg`、`SSIS-001/CD1/VIDEO_TS/VTS_01_1.VOB` 这类目录结构）。
    - flags 只从 basename 抽（避免整个「中文字幕」根目录把所有文件都标成中字）。
    """
    basename, parents = _split_path(path)
    if not basename:
        return CodeMatch(code=None, confidence=0, source="none", raw="", flags=set())

    stem = _strip_ext(basename)
    flags = extract_flags(stem)
    ctx = " ".join(parents[-2:])

    stem_cand = _best(stem, ctx)
    parent_cand, parent_depth = _parent_candidate(parents, ctx)
    parent_ok = parent_cand is not None and _parent_ok(parent_cand)

    selected: Optional[_Candidate] = None
    source = "none"
    conf = 0

    stem_score = stem_cand.score if stem_cand else 0
    # 什么时候宁可信父目录、不信文件名？
    #  1) 文件名完全没有番号；
    #  2) 文件名只有弱线索（如裸 n1234 <55），而父目录是强番号（>=75）；
    #  3) 文件名判出的番号与父目录不一致，且文件名自身并不可靠（<60）。
    prefer_parent = parent_ok and (
        stem_cand is None
        or (stem_score < 55 and parent_cand.score >= 75)
        or (stem_cand.code != parent_cand.code and stem_score < 60)
    )

    if stem_cand is not None and stem_score >= _ACCEPT_BASENAME and not prefer_parent:
        selected, source = stem_cand, "basename"
        conf = min(98, stem_score)
        # 目录与文件名是同一个番号 → 强确认（磁带/下载站常见 AGAV-101/AGAV-101.mp4）。
        # 这种结构下即使前缀没收录也按高置信度处理；没有目录确认时仍保持原分。
        if parent_cand is not None and parent_cand.code == stem_cand.code:
            conf = min(98, conf + 2)
            if conf >= 72 and conf < 92:
                conf = 92
        # 父目录恰好是厂牌前缀（NMSL/NMSL-019.mp4、MIGD/MIGD649.HD.mkv）：
        # 这是归档目录的常见组织方式，前缀+目录双重确认，也抬到可自动入库。
        elif stem_cand.prefix:
            parent_token = ""
            if parents:
                last_parent = parents[-1]
                parent_token = re.sub(r"[^A-Za-z]+", "", last_parent).upper()
            code_digits = str(stem_cand.code).rsplit("-", 1)[-1]
            if (
                parent_token
                and len(parent_token) >= 2
                and parent_token == stem_cand.prefix.upper()
                and len(code_digits) == 3
                and conf >= 60
            ):
                conf = 92
    elif parent_ok:
        selected, source = parent_cand, "parent"
        conf = max(30, min(70, parent_cand.score - 10) - 4 * parent_depth)
    elif stem_cand is not None and stem_score >= _ACCEPT_BASENAME:
        selected, source = stem_cand, "basename"
        conf = min(98, stem_score)

    if selected is None:
        return CodeMatch(code=None, confidence=0, source="none", raw="", flags=flags)

    return CodeMatch(code=selected.code, confidence=conf, source=source, raw=selected.raw, flags=flags)


def normalize_code(raw: str) -> Optional[str]:
    """把单个番号串归一化成 `PREFIX-NNN` 形式；识别不出返回 None。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    cand = _best(_strip_ext(text))
    if cand is None or cand.score < _ACCEPT_BASENAME:
        return None
    return cand.code
