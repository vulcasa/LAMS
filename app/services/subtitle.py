"""中文字幕：L0 文件名标记 + L1 迅雷/SubtitleCat 捡漏。

背景（`research/subtitles.md` 的结论）
------------------------------------
* 没有任何站点提供「按番号索引」的中文字幕库，L1 只能当**捡漏**：
  * 迅雷字幕库非公开接口：``GET https://api-shoulei-ssl.xunlei.com/oracle/subtitle?gcid=&cid=&name=<番号>``
    返回 JSON，条目含 ``name``（文件名）、``url``（srt 直链）、``duration``（毫秒）、``ext``、
    ``languages``、``extra_name``。**匹配是模糊的**——必须先用番号校验再入库。
  * SubtitleCat：搜索结果页 ``table.sub-table`` 有 ``subs/<id>/<name>.html`` 详情页；中文
    很多时候是站内 JS「Translate」按钮现场机翻（没有直链 srt），所以只当低质兜底，
    命中直链中文字幕才入库，``quality_flag='machine'``。
* 主路（WhisperJAV 本地生成）不在本模块范围。

本模块行为契约（CONTRACTS §5）
------------------------------
* :func:`sync_filename_flags`（L0）：``LibraryFile.flags`` 含 ``chinese_sub`` →
  ``Title.has_chinese_sub=True``（只置 True，不清除，幂等）。
* :func:`search_xunlei` / :func:`search_subtitlecat`：网络不可用返回 ``[]``，绝不抛异常。
* :func:`fetch_best`（L1）：候选必须**同时**满足
  1) 候选文件名/标题含该番号；2) 时长差 ≤10%（迅雷用接口 ``duration``；SubtitleCat 用
  下载后 srt 时间轴跨度校验）才落盘/入库；宁缺毋滥。
* 落盘：``Config.SUBTITLE_DIR/<番号>.zh.<srt|ass>``，写 ``Subtitle`` 行（含 sha256）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from app.config import Config
from app.db import db
from app.models import LibraryFile, Subtitle, Title, utcnow
from app.services.r18 import canonical_dvd_id

__all__ = [
    "SubtitleCandidate",
    "DURATION_TOLERANCE",
    "search_xunlei",
    "search_subtitlecat",
    "sync_filename_flags",
    "fetch_best",
    "text_has_code",
]

log = logging.getLogger(__name__)

XUNLEI_URL = "https://api-shoulei-ssl.xunlei.com/oracle/subtitle"
SUBTITLECAT_SEARCH_URL = "https://www.subtitlecat.com/index.php"
SUBTITLECAT_BASE = "https://www.subtitlecat.com/"
DEFAULT_TIMEOUT = 15
DURATION_TOLERANCE = 0.10  # 时长差上限 10%
_MIN_SUBTITLE_BYTES = 64
_SUBTITLE_EXTS = ("srt", "ass", "ssa", "vtt")

_CODE_RE = re.compile(r"^(?P<prefix>[A-Za-z0-9]*[A-Za-z])[ _\-]*(?P<num>\d+)$")
_ALNUM_RE = re.compile(r"[^0-9A-Za-z]+")
_TS_RE = re.compile(r"(\d{1,3}):(\d{2}):(\d{2})[.,](\d{1,3})")
_ZH_LANG_HINTS = ("zh", "chi", "chinese", "中文", "繁中", "简中", "简体", "繁體", "默认", "默認")


@dataclass
class SubtitleCandidate:
    """一个候选字幕（字段与 CONTRACTS §5 一致）。

    ``duration_ms`` 是**附加可选字段**：迅雷接口自带时长，用于 10% 时长校验；
    契约要求的 7 个字段保持不变，追加在末尾不影响既有调用方。
    """

    code: str
    source: str          # 'xunlei' | 'subtitlecat'
    title: str
    lang: str            # 'zh'
    score: int           # 0-100
    download_url: str
    referer: str | None
    duration_ms: int | None = None


# ---------------------------------------------------------------------------
# 网络小工具
# ---------------------------------------------------------------------------
def _session(proxy: str | None = None) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": Config.USER_AGENT,
            "Accept": "application/json, text/html, */*",
            "Accept-Language": "zh-TW,zh;q=0.9,ja;q=0.8,en;q=0.7",
        }
    )
    if proxy is None:
        try:
            from app.services.settings import proxy_url

            proxy = proxy_url() or ""
        except Exception:  # noqa: BLE001
            proxy = (Config.HTTP_PROXY or "").strip()
    proxy = (proxy or "").strip()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


def _proxy_for(app) -> str | None:
    """按设置页 → 环境变量取代理；app=None 时只走环境变量。"""
    try:
        from app.services.settings import proxy_url

        return proxy_url(app)
    except Exception:  # noqa: BLE001
        return (Config.HTTP_PROXY or "").strip() or None


def _get(session, url: str, *, params=None, headers=None, timeout: float = DEFAULT_TIMEOUT):
    """安全 GET：任何异常/非 200 都返回 ``None``。"""
    try:
        resp = session.get(
            url,
            params=params,
            headers=headers or {},
            timeout=timeout,
            allow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001 - 契约要求不抛
        log.debug("字幕请求失败 %s: %s", url, exc)
        return None
    if getattr(resp, "status_code", 0) != 200:
        return None
    return resp


# ---------------------------------------------------------------------------
# 番号校验
# ---------------------------------------------------------------------------
def _compact(text: str | None) -> str:
    return _ALNUM_RE.sub("", (text or "").upper())


def _code_variants(code: str) -> list[str]:
    """番号在文件名里可能的紧凑写法（宁缺毋滥：只给有限几个标准形态）。

    ``SSNI-001`` → ``ssni001`` / ``ssni00001``（DMM content 5 位补零）/ ``ssni0001``。
    """
    compact = _compact(code)
    if not compact:
        return []
    variants = {compact}
    match = _CODE_RE.match((code or "").strip())
    if match is not None:
        prefix = match.group("prefix").upper()
        num = match.group("num")
        variants.add(f"{prefix}{num}")
        stripped = num.lstrip("0") or "0"
        variants.add(f"{prefix}{stripped}")
        for width in (4, 5):
            variants.add(f"{prefix}{num.zfill(width)}")
    return sorted({variant for variant in variants if len(variant) >= 3})


def text_has_code(text: str | None, code: str | None) -> bool:
    """``text``（文件名/标题）里是否含该番号的某个标准紧凑写法。"""
    if not text or not code:
        return False
    haystack = _compact(text)
    if not haystack:
        return False
    return any(variant in haystack for variant in _code_variants(code))


def _duration_ok(runtime_mins: int | None, duration_ms: int | None) -> tuple[bool, float | None]:
    """时长校验：要求候选时长与 Title 时长差 ≤10%。

    返回 ``(ok, ratio)``；``ratio=None`` 表示无法比较（缺数据）——此时按
    「宁缺毋滥」判为不通过。
    """
    if runtime_mins is None or duration_ms is None:
        return False, None
    try:
        runtime_minutes = float(runtime_mins)
        candidate_minutes = float(duration_ms) / 60000.0
    except (TypeError, ValueError):
        return False, None
    if runtime_minutes <= 0 or candidate_minutes <= 0:
        return False, None
    ratio = abs(candidate_minutes - runtime_minutes) / runtime_minutes
    return ratio <= DURATION_TOLERANCE, ratio


def _subtitle_span_seconds(content: bytes) -> float | None:
    """字幕文件里最后一个时间码的秒数（srt/ass 通用近似）；解析不出返回 None。"""
    if not content:
        return None
    text = content.decode("utf-8", "ignore")
    matches = _TS_RE.findall(text)
    if not matches:
        try:
            text = content.decode("gb18030", "ignore")
            matches = _TS_RE.findall(text)
        except Exception:  # pragma: no cover
            matches = []
    if not matches:
        return None
    best = 0.0
    for hours, minutes, seconds, frac in matches:
        value = int(hours) * 3600 + int(minutes) * 60 + int(seconds)
        if len(frac) == 3:
            value += int(frac) / 1000.0
        elif len(frac) == 2:
            value += int(frac) / 100.0
        best = max(best, value)
    return best or None


def _looks_like_subtitle(content: bytes) -> bool:
    head = content[:8192]
    if len(head) < _MIN_SUBTITLE_BYTES:
        return False
    lowered = head.lstrip().lower()
    if lowered.startswith(b"webvtt") or b"[script info]" in lowered or b"dialogue:" in lowered:
        return True
    return b"-->" in head


def _guess_ext(url: str | None) -> str:
    path = (urlparse(url or "").path or "").lower()
    for ext in _SUBTITLE_EXTS:
        if path.endswith("." + ext):
            return ext
    return "srt"


# ---------------------------------------------------------------------------
# L1-A：迅雷字幕库（非公开接口，实测 JSON 见 tests/test_subtitle.py 的 XUNLEI_SAMPLE）
# ---------------------------------------------------------------------------
def search_xunlei(code: str, *, session=None) -> list[SubtitleCandidate]:
    """按番号查迅雷字幕库；返回**番号校验通过**的候选（按 score 降序）。"""
    code = (code or "").strip()
    if not code:
        return []
    session = session or _session(_proxy_for(app))
    resp = _get(
        session,
        XUNLEI_URL,
        params={"gcid": "", "cid": "", "name": code},
        headers={"Accept": "application/json"},
    )
    if resp is None:
        return []
    try:
        payload = resp.json()
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    if str(payload.get("code")) not in ("0", "0.0"):
        log.debug("迅雷接口异常返回: %r", payload.get("result"))
        return []
    items = payload.get("data")
    if not isinstance(items, list):
        return []
    canonical = canonical_dvd_id(code) or code.strip().upper()
    exact = _compact(canonical)
    out: list[SubtitleCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        url = str(item.get("url") or "")
        if not url or not text_has_code(name, code):
            continue
        duration = item.get("duration")
        try:
            duration_ms = int(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration_ms = None
        score = 60
        if exact and exact in _compact(name):
            score += 15          # 命中标准紧凑写法（ssni001 这种最可靠）
        if duration_ms:
            score += 5
        if _guess_ext(url) == "srt":
            score += 5
        languages = item.get("languages")
        lang_text = " ".join(str(x) for x in languages) if isinstance(languages, list) else str(languages or "")
        if any(hint.lower() in lang_text.lower() for hint in _ZH_LANG_HINTS):
            score += 5
        out.append(
            SubtitleCandidate(
                code=canonical,
                source="xunlei",
                title=name or url.rsplit("/", 1)[-1],
                lang="zh",
                score=max(0, min(100, score)),
                download_url=url,
                referer=XUNLEI_URL,
                duration_ms=duration_ms,
            )
        )
    out.sort(key=lambda candidate: (-candidate.score, candidate.title))
    return out


# ---------------------------------------------------------------------------
# L1-B：SubtitleCat（HTML 兜底，中文多为站内 JS 机翻 → quality=machine）
# ---------------------------------------------------------------------------
def search_subtitlecat(code: str, *, session=None) -> list[SubtitleCandidate]:
    """按番号查 SubtitleCat；返回番号校验通过的候选（中文多为机翻，分数较低）。"""
    code = (code or "").strip()
    if not code:
        return []
    session = session or _session()
    resp = _get(session, SUBTITLECAT_SEARCH_URL, params={"search": code})
    if resp is None:
        return []
    canonical = canonical_dvd_id(code) or code.strip().upper()
    try:
        soup = BeautifulSoup(resp.text or "", "lxml")
    except Exception:
        return []
    rows = soup.select("table.sub-table tbody tr")
    anchors = []
    if rows:
        for row in rows:
            anchor = row.find("a", href=re.compile(r"^/?subs/"))
            if anchor is not None:
                anchors.append((anchor, row.get_text(" ", strip=True)))
    if not anchors:  # 页面结构变了也尽量兜住
        for anchor in soup.find_all("a", href=re.compile(r"^/?subs/")):
            anchors.append((anchor, anchor.get_text(" ", strip=True)))
    out: list[SubtitleCandidate] = []
    for anchor, row_text in anchors:
        name = anchor.get_text(" ", strip=True)
        if not (text_has_code(name, code) or text_has_code(row_text, code)):
            continue
        href = anchor.get("href") or ""
        url = urljoin(SUBTITLECAT_BASE, href.lstrip("/"))
        lowered = url.lower()
        score = 40
        if any(hint in lowered for hint in ("zh-", "_zh", "-zh", "zh_", "chinese")):
            score += 10
        out.append(
            SubtitleCandidate(
                code=canonical,
                source="subtitlecat",
                title=name,
                lang="zh",
                score=max(0, min(100, score)),
                download_url=url,
                referer=resp.url,
                duration_ms=None,
            )
        )
    out.sort(key=lambda candidate: (-candidate.score, candidate.title))
    return out


# ---------------------------------------------------------------------------
# L0：文件名标记 → Title.has_chinese_sub
# ---------------------------------------------------------------------------
def sync_filename_flags(app, *, min_confidence: int | None = None) -> dict:
    """L0：扫描文件名里的 ``chinese_sub`` flag，把对应 Title 标成有中字。

    只置 True、不清除（其它来源也可能标记），重复跑幂等。
    """
    stats: dict = {"files_scanned": 0, "titles_seen": 0, "titles_updated": 0, "errors": []}
    try:
        query = db.session.query(LibraryFile).filter(
            LibraryFile.deleted_at.is_(None),
            LibraryFile.title_id.isnot(None),
            LibraryFile.flags.like("%chinese_sub%"),
        )
        if min_confidence is not None:
            query = query.filter(LibraryFile.confidence >= int(min_confidence))
        files = query.all()
    except Exception as exc:  # pragma: no cover - DB 异常
        stats["errors"].append(f"查询带中字标记的文件失败: {exc}")
        return stats
    stats["files_scanned"] = len(files)
    title_ids = sorted({int(item.title_id) for item in files if item.title_id is not None})
    stats["titles_seen"] = len(title_ids)
    if not title_ids:
        return stats
    try:
        titles = db.session.query(Title).filter(Title.id.in_(title_ids)).all()
        for title in titles:
            if not title.has_chinese_sub:
                title.has_chinese_sub = True
                title.updated_at = utcnow()
                stats["titles_updated"] += 1
        db.session.commit()
    except Exception as exc:  # pragma: no cover
        db.session.rollback()
        stats["errors"].append(f"写 has_chinese_sub 失败: {exc}")
    return stats


# ---------------------------------------------------------------------------
# 落盘 / Subtitle 行
# ---------------------------------------------------------------------------
def _safe_code(dvd_id: str) -> str:
    text = _ALNUM_RE.sub("-", (dvd_id or "").strip()).strip("-")
    return (text or "UNKNOWN").upper()


def _write_file(app, title: Title, content: bytes, ext: str) -> tuple[Path, str]:
    directory = Path(app.config["SUBTITLE_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_safe_code(title.dvd_id)}.zh.{ext}"
    sha256 = hashlib.sha256(content).hexdigest()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(content)
    os.replace(tmp, path)
    return path, sha256


def _upsert_subtitle(
    *,
    title: Title,
    candidate: SubtitleCandidate,
    quality_flag: str,
    content: bytes,
    ext: str,
    app,
) -> Subtitle:
    """写/更新 ``Subtitle`` 行（同 Title+lang 只留一行；manual/filename 不覆盖）。"""
    path, sha256 = _write_file(app, title, content, ext)
    existing = (
        db.session.query(Subtitle)
        .filter(Subtitle.title_id == title.id, Subtitle.lang == "zh")
        .order_by(Subtitle.id)
        .first()
    )
    if existing is not None and (
        existing.source in ("manual", "filename") or (existing.quality_flag or "") == "good"
    ):
        log.info("%s 已有 %s 字幕（%s），不覆盖", title.dvd_id, existing.source, existing.quality_flag)
        return existing
    if existing is None:
        row = Subtitle(title_id=title.id, lang="zh")
        db.session.add(row)
    else:
        row = existing
    row.format = ext
    row.local_path = str(path)
    row.source = candidate.source
    row.source_url = candidate.download_url
    row.quality_flag = quality_flag
    row.sha256 = sha256
    row.created_at = utcnow()
    db.session.commit()
    return row


def _candidate_summary(title: Title, candidate: SubtitleCandidate, quality_flag: str) -> Subtitle:
    """dry_run 用：不落盘、不写库，只回一个未持久化的 Subtitle 视图。"""
    return Subtitle(
        title_id=title.id,
        lang="zh",
        format=_guess_ext(candidate.download_url),
        local_path=None,
        source=candidate.source,
        source_url=candidate.download_url,
        quality_flag=quality_flag,
        sha256=None,
    )


def _resolve_subtitlecat_srt(session, candidate: SubtitleCandidate) -> str | None:
    """在 SubtitleCat 详情页里找**直链中文 srt**（没有直链就放弃，宁缺毋滥）。

    实测（2026-10-26，SSNI-001）：中文只有 ``translate_from_server_folder`` JS 按钮，
    没有直链 → 这种情况返回 None，由上层跳过。英文/印尼语等有直链，但我们只要中文。
    """
    resp = _get(
        session,
        candidate.download_url,
        headers={"Referer": candidate.referer or SUBTITLECAT_BASE},
    )
    if resp is None:
        return None
    try:
        soup = BeautifulSoup(resp.text or "", "lxml")
    except Exception:
        return None
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href") or ""
        lowered = href.lower()
        if not lowered.endswith(".srt"):
            continue
        context = (anchor.get_text(" ", strip=True) + " " + (anchor.parent.get_text(" ", strip=True) if anchor.parent else "")).lower()
        if not any(hint in context for hint in ("chinese", "中文", "繁中", "简中", "繁體", "简体")):
            continue
        return urljoin(candidate.download_url, href)
    # 退一步：直接匹配 href 里带 zh 的 srt 直链
    match = re.search(r'href="([^"]*zh[^"]*\.srt[^"]*)"', resp.text or "", re.I)
    if match:
        return urljoin(candidate.download_url, match.group(1))
    return None


# ---------------------------------------------------------------------------
# L1 主入口
# ---------------------------------------------------------------------------
def _download_subtitle(session, url: str, *, referer: str | None = None, timeout: float = DEFAULT_TIMEOUT):
    resp = _get(session, url, headers={"Referer": referer} if referer else None, timeout=timeout)
    if resp is None:
        return None
    content = getattr(resp, "content", b"") or b""
    if not _looks_like_subtitle(content):
        log.debug("下载内容不像字幕: %s", url)
        return None
    final_url = str(getattr(resp, "url", "") or url)
    return content, final_url


def _ranked_xunlei(code: str, *, session, runtime_mins) -> list[SubtitleCandidate]:
    """迅雷候选按「番号分 + 时长校验」过滤排序。"""
    candidates = search_xunlei(code, session=session)
    valid: list[tuple[int, float, SubtitleCandidate]] = []
    for candidate in candidates:
        ok, ratio = _duration_ok(runtime_mins, candidate.duration_ms)
        if not ok:
            log.debug(
                "迅雷候选时长不合 %s（%s vs %s min, ratio=%s）",
                candidate.title, candidate.duration_ms, runtime_mins, ratio,
            )
            continue
        valid.append((candidate.score, -float(ratio or 0.0), candidate))
    valid.sort(key=lambda item: (-item[0], -item[1], item[2].title))
    return [item[2] for item in valid]


def _fetch_xunlei(app, title: Title, candidate: SubtitleCandidate, *, session) -> Subtitle | None:
    downloaded = _download_subtitle(session, candidate.download_url, referer=candidate.referer)
    if downloaded is None:
        return None
    content, final_url = downloaded
    ext = _guess_ext(candidate.download_url) or _guess_ext(final_url)
    return _upsert_subtitle(
        title=title,
        candidate=candidate,
        quality_flag="unknown",  # 迅雷网友上传，质量未知（契约规定）
        content=content,
        ext=ext,
        app=app,
    )


def _fetch_subtitlecat(app, title: Title, candidate: SubtitleCandidate, *, session) -> Subtitle | None:
    srt_url = _resolve_subtitlecat_srt(session, candidate)
    if not srt_url:
        log.debug("SubtitleCat 无中文直链，跳过: %s", candidate.download_url)
        return None
    downloaded = _download_subtitle(session, srt_url, referer=candidate.download_url)
    if downloaded is None:
        return None
    content, final_url = downloaded
    # SubtitleCat 拿不到候选时长 → 用下下来的 srt 时间轴跨度做 ±10% 校验
    span = _subtitle_span_seconds(content)
    span_minutes = (span / 60.0) if span else None
    ok, ratio = _duration_ok(title.runtime_mins, int(span_minutes * 60000) if span_minutes else None)
    if not ok:
        log.debug(
            "SubtitleCat srt 时长不合 %s（span=%s min vs %s, ratio=%s）",
            title.dvd_id, span_minutes, title.runtime_mins, ratio,
        )
        return None
    ext = _guess_ext(final_url) or "srt"
    return _upsert_subtitle(
        title=title,
        candidate=candidate,
        quality_flag="machine",  # 契约：SubtitleCat 一律 machine
        content=content,
        ext=ext,
        app=app,
    )


def fetch_best(app, title, *, session=None, dry_run: bool = False) -> "Subtitle | None":
    """给一个 Title 找一条中文字幕（迅雷优先，SubtitleCat 兜底）。

    行为：
    * 候选必须含番号（搜索层已过滤）且时长差 ≤10%（迅雷用接口 ``duration``，
      SubtitleCat 用 srt 时间轴跨度）；任一不满足 → ``None``；
    * ``dry_run=True`` 不下载、不落盘、不写库，只返回未持久化的 Subtitle 视图；
    * 任何异常都吞掉返回 ``None``（字幕是锦上添花，不能拖垮片库任务）。
    """
    if title is None or not getattr(title, "dvd_id", None):
        return None
    session = session or _session()
    try:
        runtime = getattr(title, "runtime_mins", None)
        for candidate in _ranked_xunlei(title.dvd_id, session=session, runtime_mins=runtime):
            if dry_run:
                return _candidate_summary(title, candidate, "unknown")
            result = _fetch_xunlei(app, title, candidate, session=session)
            if result is not None:
                return result
            # 该候选下载/校验失败，继续试下一个
        for candidate in search_subtitlecat(title.dvd_id, session=session):
            if dry_run:
                return _candidate_summary(title, candidate, "machine")
            result = _fetch_subtitlecat(app, title, candidate, session=session)
            if result is not None:
                return result
        return None
    except Exception as exc:  # noqa: BLE001 - 契约要求不抛
        log.warning("fetch_best 失败 %s: %s", getattr(title, "dvd_id", "?"), exc)
        try:
            db.session.rollback()
        except Exception:  # pragma: no cover
            pass
        return None
