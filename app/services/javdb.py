"""JavDB 补缺口（中文题材 / 评分 / 中字标记 / 系列 / 片商 / 日期）。

为什么需要这一层
----------------
r18.dev 的 weekly dump 是权威日文源，但**没有中文题材、没有评分、没有「有没有中字版」**。
JavDB 的详情页恰好补这三类缺口（在 `research/metadata-sources.md` 里实测过结构）：

* 「類別」= 繁体中文题材；
* 「評分: 4.52分, 由1080人評價」= 评分 + 人数（本次调研唯一稳定可得的评分源）；
* 磁力条目里的 `span.tag「字幕」` / 列表页「含中字磁鏈」「中字可播放」= 该番号存在中文硬字幕版；
* 「系列」「片商」「日期」作为 r18 缺字段时的兜底。

工程约束（CONTRACTS §5）
-------------------------
* :func:`fetch`：单部查询，任何网络/解析失败都返回 ``None``，绝不抛给调用方；
* :func:`fill_gaps`：只挑「评分缺失」或「没有中文题材」的 Title，单线程 + 限速，
  403/429 立即熔断本轮并记录，单条失败不中断整批；
* JavDB 会轮换域名：从 ``https://javdb.com`` 起步、跟随重定向，读取页面
  ``body[data-domain]`` 校验并把可用域名写 ``AppSetting('javdb_domain')`` 供下次优先使用；
* 写库幂等：重复跑不会产生重复的 Genre/TitleGenre/ScrapeTask。

线程模型：本模块只有「单线程顺序请求」，不要在多线程里共享同一个 session
（:func:`fetch` / :func:`fill_gaps` 各自创建 session）。
"""
from __future__ import annotations

import datetime as _dt
import difflib
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from sqlalchemy import func, or_

from app.config import Config
from app.db import db
from app.models import (
    AppSetting,
    Genre,
    Maker,
    ScrapeTask,
    Series,
    Title,
    TitleGenre,
    utcnow,
)

__all__ = [
    "JavdbDetail",
    "JavdbBlocked",
    "DEFAULT_BASE_URL",
    "DOMAIN_SETTING_KEY",
    "fetch",
    "fill_gaps",
    "parse_detail_html",
    "parse_search_html",
]

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://javdb.com"
DOMAIN_SETTING_KEY = "javdb_domain"
DEFAULT_TIMEOUT = 15
BLOCK_STATUSES = frozenset({403, 429})
# 详情页 / 列表页出现这些字样 = 该番号有中文硬字幕版本（实测结构见 tests/fixtures/javdb_sample.html）
CHINESE_SUB_MARKERS = ("含中字磁鏈", "含中字磁链", "中字可播放")
# 「類別/番號/...」面板的 label 白名单：页面里还有 review-buttons 等 panel-block，不能全收
PANEL_LABELS = (
    "番號", "日期", "時長", "導演", "片商", "發行", "系列", "評分", "類別", "演員",
)
_SCRIPT_TAG = re.compile(r"<script\b.*?</script>", re.S | re.I)
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
#: 平假名 + 片假名：current-title 里还带假名基本说明「没翻译」，仍是日文
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_RATING_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*分")
_VOTES_RE = re.compile(r"由\s*([0-9][0-9,]*)\s*人評價")
_ALNUM_RE = re.compile(r"[^0-9A-Za-z]+")
_SPACE_RE = re.compile(r"\s+")
_SHOW_ORIGIN_RE = re.compile(r"\s*(?:顯示原標題|显示原标题)\s*$")

#: 进程内记住的最近可用域名（fetch 单部调用时也能复用）
_domain_cache: str | None = None


class JavdbBlocked(RuntimeError):
    """JavDB 返回 403/429/Cloudflare 挑战——应熔断本轮。"""


@dataclass
class JavdbDetail:
    """JavDB 详情页解析结果（字段即 CONTRACTS §5 约定）。"""

    code: str
    url: str
    title: str | None = None
    genres_zh: list[str] | None = None
    rating: float | None = None
    votes: int | None = None
    has_chinese_sub: bool = False
    series: str | None = None
    maker: str | None = None
    release_date: str | None = None
    #: JavDB 详情页 ``.current-title`` 的中文标题（繁中/社区译名）；没有/与日文相同时为 None
    title_zh: str | None = None
    #: JavDB 详情页封面大图 URL（DMM 封面缺失/占位时的兜底源）
    cover_url: str | None = None

    def __post_init__(self) -> None:
        if self.genres_zh is None:
            self.genres_zh = []


@dataclass
class _SearchHit:
    href: str
    has_chinese_sub: bool


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _compact(text: str | None) -> str:
    """番号/文件名的紧凑形式（大写、只留字母数字），用于严格比对。"""
    return _ALNUM_RE.sub("", (text or "").upper())


def _norm_base(url: str | None) -> str | None:
    """把 ``https://javdb.com/`` 归一成 ``https://javdb.com``。"""
    if not url:
        return None
    text = str(url).strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    if not parsed.netloc:
        return None
    return f"{parsed.scheme or 'https'}://{parsed.netloc}".rstrip("/")


def _normalize_code(code: str | None) -> str:
    """展示用番号：去掉 ``IPX -001`` 里 '-' 两侧的空格。"""
    text = (code or "").strip()
    return re.sub(r"\s*-\s*", "-", text)


def _parse_release_date(text: str | None) -> str | None:
    if not text:
        return None
    match = _DATE_RE.search(text)
    if match is None:
        return None
    try:
        return _dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return None


def _new_session(proxy: str | None = None) -> requests.Session:
    """带 UA / Accept-Language / 可选代理的 requests.Session。

    ``proxy=None`` 表示按「设置页 → 环境变量」的顺序自动取；传空串表示强制直连。
    """
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": Config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,ja;q=0.8,en;q=0.7",
        }
    )
    if proxy is None:
        try:
            from app.services.settings import proxy_url

            proxy = proxy_url() or ""
        except Exception:  # noqa: BLE001 - 无 app context 时回退环境变量
            proxy = (Config.HTTP_PROXY or "").strip()
    proxy = (proxy or "").strip()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


def _candidate_bases(preferred: str | None) -> list[str]:
    """候选入口域名：AppSetting/上次成功 → 进程内缓存 → javdb.com。"""
    bases: list[str] = []
    for value in (preferred, _domain_cache, DEFAULT_BASE_URL):
        base = _norm_base(value)
        if base and base not in bases:
            bases.append(base)
    return bases


def _page_domain(html: str) -> str | None:
    """从 ``<body data-domain="https://javdb580.com">`` 取当前官方域名。

    只接受 host 里带 ``javdb`` 的值——镜像/停放域名会被站点轮换公告页带偏，
    解析结果不可信（调研里见过 ``seedmm.help`` 已被停放成 parklogic）。
    """
    soup = BeautifulSoup(html or "", "lxml")
    body = soup.find("body")
    if body is None:
        return None
    value = body.get("data-domain")
    base = _norm_base(value)
    if base is None:
        return None
    host = urlparse(base).netloc.lower()
    if "javdb" not in host:
        return None
    return base


BLOCKED_UNTIL_KEY = "javdb_blocked_until"


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)


def _stored_blocked_until():
    """读冷却截止时间（AppSetting，naive UTC）。不在 app context 里返回 None。"""
    try:
        row = db.session.get(AppSetting, BLOCKED_UNTIL_KEY)
        if row is None or not row.value:
            return None
        from datetime import datetime

        return datetime.fromisoformat(row.value)
    except Exception:  # noqa: BLE001
        return None


def cooldown_remaining(app=None) -> int:
    """距离 JavDB 冷却结束还有多少秒；0 表示可跑。"""
    until = _stored_blocked_until()
    if until is None:
        return 0
    delta = int((until - _now()).total_seconds())
    return max(0, delta)


def _set_cooldown(seconds: int) -> int:
    """写入冷却截止时间（幂等 upsert），返回实际秒数。"""
    seconds = max(60, int(seconds))
    from datetime import datetime, timedelta

    until = _now() + timedelta(seconds=seconds)
    try:
        row = db.session.get(AppSetting, BLOCKED_UNTIL_KEY)
        if row is None:
            db.session.add(AppSetting(key=BLOCKED_UNTIL_KEY, value=until.isoformat()))
        else:
            row.value = until.isoformat()
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
    return seconds


def _clear_cooldown() -> None:
    try:
        row = db.session.get(AppSetting, BLOCKED_UNTIL_KEY)
        if row is not None:
            db.session.delete(row)
            db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()


def _remember(base: str | None) -> None:
    global _domain_cache
    base = _norm_base(base)
    if base and "javdb" in urlparse(base).netloc.lower():
        _domain_cache = base


def _looks_blocked(resp) -> bool:
    """403/429 或 Cloudflare/人机验证页 → 熔断信号。"""
    status = getattr(resp, "status_code", 0) or 0
    if status in BLOCK_STATUSES:
        return True
    headers = getattr(resp, "headers", None) or {}
    try:
        if str(headers.get("cf-mitigated", "")).lower() == "challenge":
            return True
    except Exception:  # pragma: no cover - 防御非 dict 的 headers
        pass
    text = (getattr(resp, "text", "") or "")[:4000]
    if "Just a moment" in text or "Attention Required" in text:
        return True
    if "人機驗證" in text or "人机验证" in text:
        return True
    return False


def _guess_ext(url: str | None) -> str:
    path = (urlparse(url or "").path or "").lower()
    for ext in ("srt", "ass", "ssa", "vtt"):
        if path.endswith("." + ext):
            return ext
    return "srt"


# ---------------------------------------------------------------------------
# HTML 解析（纯函数，测试直接用 tests/fixtures/javdb_sample.html 跑）
# ---------------------------------------------------------------------------
def parse_search_html(html: str, code: str) -> _SearchHit | None:
    """在搜索结果页里找**番号严格相等**的那一条，返回 (详情链接, 是否有中字标记)。

    找不到（含「有结果但没有同番号」）返回 ``None``——宁缺毋滥，避免把同系列
    的其它片子当成本片。
    """
    target = _compact(code)
    if not target:
        return None
    soup = BeautifulSoup(html or "", "lxml")
    items = soup.select("div.movie-list div.item") or soup.select("div.movie-list > div")
    for item in items:
        anchor = item.select_one("a.box[href^='/v/']") or item.find("a", href=re.compile(r"^/v/"))
        if anchor is None:
            continue
        strong = item.select_one(".video-title strong")
        found_code = strong.get_text(strip=True) if strong is not None else ""
        if _compact(found_code) != target:
            continue
        item_text = item.get_text(" ", strip=True)
        has_cn = any(marker in item_text for marker in CHINESE_SUB_MARKERS)
        if not has_cn:
            has_cn = item.select_one(".tag-can-play.cnsub") is not None
        return _SearchHit(href=anchor.get("href") or "", has_chinese_sub=has_cn)
    return None


def _panel_map(soup: BeautifulSoup) -> dict[str, str]:
    """把 ``div.panel-block`` 的 ``<strong>label</strong> + value`` 收成 dict。"""
    panels: dict[str, str] = {}
    for block in soup.select("div.panel-block"):
        strong = block.find("strong")
        if strong is None:
            continue
        label = strong.get_text(strip=True).rstrip(":：").strip()
        if label not in PANEL_LABELS or label in panels:
            continue
        value_node = block.select_one("span.value")
        text = (
            value_node.get_text(" ", strip=True)
            if value_node is not None
            else block.get_text(" ", strip=True)
        )
        text = re.sub(rf"^{re.escape(label)}\s*[:：]?\s*", "", text).strip()
        if text:
            panels[label] = text
    return panels


def _panel_links(soup: BeautifulSoup, label: str) -> list[str]:
    """取某个面板内的所有链接文字（用于「類別」这种列表型字段）。"""
    for block in soup.select("div.panel-block"):
        strong = block.find("strong")
        if strong is None:
            continue
        if strong.get_text(strip=True).rstrip(":：").strip() != label:
            continue
        out: list[str] = []
        for anchor in block.find_all("a"):
            text = anchor.get_text(" ", strip=True)
            if text and text not in out:
                out.append(text)
        return out
    return []


def _clean_title_text(text: str | None) -> str | None:
    """标题文本归一：压空白、去首尾；空串→None。"""
    cleaned = _SPACE_RE.sub(" ", text or "").strip()
    return cleaned or None


def _title_key(text: str | None) -> str:
    """标题比较键：NFKC + 小写 + 去空白/标点（用于判断「中文标题」是否只是日文原名）。"""
    import unicodedata

    normalized = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[\s\W_]+", "", normalized, flags=re.UNICODE)


def _pick_title_zh(current: str | None, origin: str | None) -> str | None:
    """从 ``.current-title`` / ``.origin-title`` 判定真正的中文标题。

    实测（2026-10-26）JavDB 的 ``.current-title`` 是「当前语言标题」，但**不保证是中文**：
    有的页面直接放日文原名（甚至没有 ``.origin-title`` 节点）。以下情况一律不算中文标题、
    返回 None（按需求「无中文名用日文」回退）：

    * 空 / 不含 CJK（纯英文、罗马音）；
    * 含平假名/片假名——基本可判定仍是日文（实测 ABP-203：current-title 就是日文原名）;
    * 与 ``.origin-title`` 实质相同，或高度相似（≥85%，只是标点/前后缀差异）。
    """
    current = _clean_title_text(current)
    if not current or not _CJK_RE.search(current):
        return None
    if _KANA_RE.search(current):
        return None
    origin = _clean_title_text(origin)
    if origin:
        current_key = _title_key(current)
        origin_key = _title_key(origin)
        if current_key == origin_key:
            return None
        if difflib.SequenceMatcher(None, current_key, origin_key).ratio() >= 0.85:
            return None
    return current


def _page_cover_url(soup: BeautifulSoup) -> str | None:
    """从详情页取封面图 URL（JavDB 的 ``/covers/`` 图或 og:image）。"""
    candidates: list[str] = []
    for img in soup.select("img"):
        src = str(img.get("src") or "").strip()
        if not src:
            continue
        if "covers/" in src or "cover" in (img.get("class") or []):
            candidates.append(src)
    meta = soup.select_one('meta[property="og:image"]')
    if meta is not None:
        candidates.append(str(meta.get("content") or "").strip())
    for raw in candidates:
        if not raw:
            continue
        if raw.startswith("//"):
            raw = "https:" + raw
        if raw.startswith("http://"):
            raw = "https://" + raw[len("http://"):]
        if raw.startswith("https://") and ("javdb" in raw or "jdbstatic" in raw):
            return raw
    return None


def _page_titles(soup: BeautifulSoup, page_code: str | None) -> tuple[str | None, str | None]:
    """返回 ``(展示标题, 中文标题)``。

    展示标题优先 ``.current-title``（JavDB 中文标题），退回 ``.origin-title``（日文原名）、
    再退回 ``<title>``；中文标题只认 :func:`_pick_title_zh` 的判定。
    """
    current = origin = None
    h2 = soup.select_one("h2.title") or soup.find("h2")
    if h2 is not None:
        current_node = h2.select_one(".current-title")
        origin_node = h2.select_one(".origin-title")
        current = _clean_title_text(
            current_node.get_text(" ", strip=True) if current_node is not None else None
        )
        origin = _clean_title_text(
            origin_node.get_text(" ", strip=True) if origin_node is not None else None
        )
        display = current or origin
        if display is None:
            text = h2.get_text(" ", strip=True)
            if page_code:
                text = re.sub(rf"^{re.escape(page_code)}\s*", "", text, flags=re.I)
            display = _clean_title_text(_SHOW_ORIGIN_RE.sub("", text))
        return display, _pick_title_zh(current, origin)
    title_tag = soup.find("title")
    if title_tag is not None:
        text = title_tag.get_text(" ", strip=True)
        if not text.lower().startswith("javdb"):
            display = _clean_title_text(re.sub(r"\s*\|\s*JavDB.*$", "", text))
            return display, None
    return None, None


def _page_has_chinese_sub(soup: BeautifulSoup) -> bool:
    """详情页的中字信号：列表页 tag / 磁力条目里的 ``span.tag「字幕」``。"""
    for span in soup.find_all("span"):
        text = span.get_text(strip=True)
        if not text:
            continue
        if text in CHINESE_SUB_MARKERS or "中字磁鏈" in text or "中字磁链" in text:
            return True
        classes = span.get("class") or []
        if text == "字幕" and "tag" in classes:
            return True
    return False


def parse_detail_html(
    html: str,
    *,
    code: str | None = None,
    url: str = "",
    has_chinese_sub: bool = False,
) -> JavdbDetail | None:
    """解析 JavDB 详情页。

    返回 ``None`` 的条件（都按「不能污染数据」处理）：

    * 不是详情页（没有 ``div.panel-block`` 或没有「番號」面板）；
    * 页面「番號」与请求的 ``code`` 不一致（拿错页面）。
    """
    if not html:
        return None
    # 先砍掉脚本，减少 BeautifulSoup 解析噪声（详情页内联 JSON-LD 很大）
    soup = BeautifulSoup(_SCRIPT_TAG.sub("", html), "lxml")
    if soup.find("body") is None:
        return None
    panels = _panel_map(soup)
    page_code_raw = panels.get("番號")
    if not page_code_raw:
        return None
    page_code = _normalize_code(page_code_raw)
    wanted = _compact(code) if code else _compact(page_code)
    if wanted and _compact(page_code) != wanted:
        log.debug("javdb 页面番号 %s != 请求 %s，丢弃", page_code, code)
        return None

    genres = _panel_links(soup, "類別")
    if not genres:
        raw_genres = panels.get("類別") or ""
        genres = [part.strip() for part in re.split(r"[,，、]", raw_genres) if part.strip()]

    rating = None
    votes = None
    rating_text = panels.get("評分") or ""
    rating_match = _RATING_RE.search(rating_text)
    if rating_match is not None:
        try:
            rating = float(rating_match.group(1))
        except ValueError:
            rating = None
    votes_match = _VOTES_RE.search(rating_text)
    if votes_match is not None:
        try:
            votes = int(votes_match.group(1).replace(",", ""))
        except ValueError:
            votes = None

    display_title, title_zh = _page_titles(soup, page_code)
    cover_url = _page_cover_url(soup)
    detail = JavdbDetail(
        code=page_code,
        url=url or "",
        title=display_title,
        title_zh=title_zh,
        genres_zh=genres,
        rating=rating,
        votes=votes,
        has_chinese_sub=bool(has_chinese_sub) or _page_has_chinese_sub(soup),
        series=panels.get("系列"),
        maker=panels.get("片商"),
        release_date=_parse_release_date(panels.get("日期")),
        cover_url=cover_url,
    )
    return detail


# ---------------------------------------------------------------------------
# 域名持久化（AppSetting）
# ---------------------------------------------------------------------------
def _stored_domain() -> str | None:
    """读 ``AppSetting('javdb_domain')``；不在 app context 里返回 None。"""
    try:
        from flask import has_app_context

        if not has_app_context():
            return None
        row = db.session.get(AppSetting, DOMAIN_SETTING_KEY)
        return _norm_base(row.value) if row is not None else None
    except Exception:  # pragma: no cover - 数据库未初始化/无 app context 时静默
        return None


def _store_domain(base: str | None) -> str | None:
    """把可用域名写进 ``AppSetting``（幂等 upsert）。"""
    base = _norm_base(base)
    if base is None:
        return None
    try:
        from flask import has_app_context

        if not has_app_context():
            return base
        row = db.session.get(AppSetting, DOMAIN_SETTING_KEY)
        if row is None:
            db.session.add(AppSetting(key=DOMAIN_SETTING_KEY, value=base))
        elif row.value != base:
            row.value = base
        db.session.commit()
    except Exception as exc:  # pragma: no cover
        db.session.rollback()
        log.warning("写入 javdb_domain 失败: %s", exc)
    return base


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------
def _fetch_impl(
    code: str,
    *,
    session=None,
    cookie: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    preferred_base: str | None = None,
) -> JavdbDetail | None:
    """实际抓取（:func:`fetch` 的无异常内核；:func:`fill_gaps` 需要感知熔断时直接调它）。"""
    code = _normalize_code(code)
    if not code:
        return None
    session = session or _new_session()
    headers = {}
    if cookie:
        headers["Cookie"] = cookie
    for base in _candidate_bases(preferred_base):
        search_url = f"{base}/search"
        try:
            resp = session.get(
                search_url,
                params={"q": code, "f": "all"},
                headers=headers,
                timeout=timeout,
                allow_redirects=True,
            )
        except Exception as exc:
            log.debug("javdb 搜索请求失败 %s: %s", base, exc)
            continue
        if _looks_blocked(resp):
            raise JavdbBlocked(f"javdb 返回 {getattr(resp, 'status_code', '?')}（{base}）")
        if getattr(resp, "status_code", 0) != 200:
            continue

        html = getattr(resp, "text", "") or ""
        hit = parse_search_html(html, code)
        final_base = _page_domain(html) or _norm_base(getattr(resp, "url", "") or base)
        # 有效 JavDB 页面但没搜到同番号 → 就是「没有」，不要换域名重复请求
        if hit is None:
            if "JavDB" in html or "movie-list" in html:
                _remember(final_base)
                return None
            continue
        _remember(final_base)

        detail_url = urljoin(getattr(resp, "url", "") or base + "/", hit.href)
        try:
            resp2 = session.get(
                detail_url, headers=headers, timeout=timeout, allow_redirects=True
            )
        except Exception as exc:
            log.debug("javdb 详情请求失败 %s: %s", detail_url, exc)
            continue
        if _looks_blocked(resp2):
            raise JavdbBlocked(f"javdb 详情返回 {getattr(resp2, 'status_code', '?')}")
        if getattr(resp2, "status_code", 0) != 200:
            continue
        detail = parse_detail_html(
            getattr(resp2, "text", "") or "",
            code=code,
            url=str(getattr(resp2, "url", "") or detail_url),
            has_chinese_sub=hit.has_chinese_sub,
        )
        if detail is not None:
            return detail
    return None


def fetch(
    code: str,
    *,
    session=None,
    cookie: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> JavdbDetail | None:
    """抓取单部 JavDB 详情；任何失败（网络/403/解析）都返回 ``None``，绝不抛异常。"""
    try:
        return _fetch_impl(
            code,
            session=session,
            cookie=cookie,
            timeout=timeout,
            preferred_base=_stored_domain(),
        )
    except JavdbBlocked as exc:
        log.warning("javdb 熔断：%s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - 契约要求永不向上抛
        log.warning("javdb fetch 失败 %s: %s", code, exc)
        return None


# ---------------------------------------------------------------------------
# 补缺口
# ---------------------------------------------------------------------------
def _mark_javdb_miss(title: Title) -> None:
    """后台标记：JavDB 明确查不到该番号。UI 不显示，冷却期内不再重复抓。"""
    title.javdb_miss_at = utcnow()
    title.javdb_miss_count = int(title.javdb_miss_count or 0) + 1


def _clear_javdb_miss(title: Title) -> None:
    if title.javdb_miss_at is not None or (title.javdb_miss_count or 0):
        title.javdb_miss_at = None
        title.javdb_miss_count = 0


def _gap_titles(
    *,
    limit: int | None,
    only_codes: Iterable[str] | None,
    only_missing_cover: bool = False,
) -> list[Title]:
    """只挑缺口：``rating IS NULL`` / 没有中文题材 / ``title_zh`` 为空。

    ``title_zh`` 也算缺口：这样「上一轮已经补过题材+评分、但标题还是日文」的片子
    这轮才会被重新选中补中文标题（双语闭环）。代价是 JavDB 本来就没有中文标题的
    片子每轮都会重试一次——用 ``limit`` / ``only_codes`` 控制批次即可。
    """
    zh_genre_titles = (
        db.session.query(TitleGenre.title_id)
        .join(Genre, Genre.id == TitleGenre.genre_id)
        .filter(Genre.genre_zh.isnot(None), func.trim(Genre.genre_zh) != "")
        .subquery()
    )
    query = db.session.query(Title).filter(
        or_(
            Title.rating.is_(None),
            ~Title.id.in_(db.session.query(zh_genre_titles.c.title_id)),
            Title.title_zh.is_(None),
            func.trim(Title.title_zh) == "",
        )
    )
    if only_missing_cover:
        query = query.filter(
            or_(Title.cover_url.is_(None), func.trim(Title.cover_url) == "")
        )
    if not only_codes:
        # 明确未命中的条目不重复轰炸：默认 30 天后再试（后台标记，用户不可见）
        try:
            retry_days = int(Config.JAVDB_MISS_RETRY_DAYS)
        except (TypeError, ValueError):
            retry_days = 30
        cutoff = utcnow() - _dt.timedelta(days=max(0, retry_days))
        query = query.filter(
            or_(Title.javdb_miss_at.is_(None), Title.javdb_miss_at < cutoff)
        )
    if only_codes:
        codes = [_normalize_code(code) for code in only_codes if code and str(code).strip()]
        if not codes:
            return []
        query = query.filter(Title.dvd_id.in_(codes))
    query = query.order_by(Title.id)
    if limit is not None and limit > 0:
        query = query.limit(int(limit))
    return query.all()


def _find_or_create_maker(name: str) -> Maker | None:
    name = (name or "").strip()
    if not name:
        return None
    lowered = name.lower()
    maker = (
        db.session.query(Maker)
        .filter(or_(func.lower(Maker.name_en) == lowered, func.lower(Maker.name_ja) == lowered))
        .first()
    )
    if maker is not None:
        return maker
    maker = Maker(name_ja=name) if _CJK_RE.search(name) else Maker(name_en=name)
    db.session.add(maker)
    db.session.flush()
    return maker


def _find_or_create_series(name: str) -> Series | None:
    name = (name or "").strip()
    if not name:
        return None
    lowered = name.lower()
    series = (
        db.session.query(Series)
        .filter(or_(func.lower(Series.name_en) == lowered, func.lower(Series.name_ja) == lowered))
        .first()
    )
    if series is not None:
        return series
    series = Series(name_ja=name) if _CJK_RE.search(name) else Series(name_en=name)
    db.session.add(series)
    db.session.flush()
    return series


def _link_genre(title: Title, name: str, stats: dict) -> None:
    """确保 Genre(genre_zh=name) 存在并与 Title 关联（幂等）。"""
    name = (name or "").strip()
    if not name:
        return
    genre = db.session.query(Genre).filter(Genre.genre_zh == name).first()
    if genre is None:
        genre = Genre(genre_zh=name)
        db.session.add(genre)
        db.session.flush()
        stats["genres_created"] = stats.get("genres_created", 0) + 1
    exists = (
        db.session.query(TitleGenre)
        .filter(TitleGenre.title_id == title.id, TitleGenre.genre_id == genre.id)
        .first()
    )
    if exists is None:
        db.session.add(TitleGenre(title_id=title.id, genre_id=genre.id, source="javdb"))
        stats["genres_added"] = stats.get("genres_added", 0) + 1


def _apply_detail(title: Title, detail: JavdbDetail, stats: dict) -> bool:
    """把 JavDB 详情写进 Title（只补空字段，幂等）。返回是否有改动。"""
    changed = False
    if detail.rating is not None and title.rating is None:
        title.rating = detail.rating
        title.rating_source = "javdb"
        changed = True
    if detail.votes is not None and title.rating_votes is None:
        title.rating_votes = detail.votes
        changed = True
    if title.release_date is None and detail.release_date:
        title.release_date = _dt.date.fromisoformat(detail.release_date)
        changed = True
    if title.maker_id is None and detail.maker:
        maker = _find_or_create_maker(detail.maker)
        if maker is not None:
            title.maker_id = maker.id
            changed = True
    if title.series_id is None and detail.series:
        series = _find_or_create_series(detail.series)
        if series is not None:
            title.series_id = series.id
            changed = True
    if detail.has_chinese_sub and not title.has_chinese_sub:
        title.has_chinese_sub = True
        changed = True
    if detail.cover_url and not (title.cover_url or "").strip():
        title.cover_url = detail.cover_url
        stats["cover_url_filled"] = stats.get("cover_url_filled", 0) + 1
        changed = True
    # title_zh 只填空：已有（含人工/MT 结果）不覆盖，manual_lock 在 fill_gaps 循环外已跳过
    if _clean_title_text(title.title_zh) is None and detail.title_zh:
        title.title_zh = detail.title_zh
        stats["title_zh_filled"] = stats.get("title_zh_filled", 0) + 1
        changed = True
    before = stats.get("genres_added", 0)
    for genre_name in detail.genres_zh:
        _link_genre(title, genre_name, stats)
    if stats.get("genres_added", 0) > before:
        changed = True
    if changed:
        title.updated_at = utcnow()
    return changed


def _record_task(title: Title, status: str, error: str | None = None) -> None:
    """维护 ``ScrapeTask(stage='javdb_gap')``：同一 Title 只保留一行，幂等。"""
    task = (
        db.session.query(ScrapeTask)
        .filter(ScrapeTask.title_id == title.id, ScrapeTask.stage == "javdb_gap")
        .order_by(ScrapeTask.id)
        .first()
    )
    if task is None:
        task = ScrapeTask(title_id=title.id, stage="javdb_gap", status=status, attempts=1)
        db.session.add(task)
    else:
        task.status = status
        task.attempts = int(task.attempts or 0) + 1
    task.error = (error or "")[:500] or None
    task.updated_at = utcnow()


def fill_gaps(
    app,
    *,
    limit: int = 100,
    rate: float | None = None,
    only_codes: Iterable[str] | None = None,
    only_missing_cover: bool = False,
    progress: Callable[[int, str], None] | None = None,
) -> dict:
    """给缺口 Title 补 JavDB 字段（中文标题 / 中文题材 / 评分 / 中字标记 / 系列 / 片商 / 日期）。

    返回统计 dict（供 jobs.py 直接塞 Job.result / UI 展示）::

        {
          "considered": 候选数, "filled": 成功数, "no_result": JavDB 无此片,
          "failed": 失败数, "skipped_locked": manual_lock 跳过数,
          "blocked": False, "domain": "https://javdb580.com",
          "rate": 1.5, "genres_added": n, "genres_created": n,
          "title_zh_filled": n,   # 本轮新写入 Title.title_zh 的条数
          "cooldown_remaining_s": n,  # >0 表示处于 403 冷却期，本轮没发请求
          "errors": ["..."]
        }

    规则：
    * 单线程 + ``rate`` 秒/部（默认 ``Config.JAVDB_RATE_SECONDS``，测试传 0）；
    * 403/429 立即熔断本轮（``blocked=True``），不重试轰炸；
    * 单条失败只记 ``failed`` / ``errors``，不中断整批；
    * 写库幂等（Genre/TitleGenre/ScrapeTask 都不会重复）。
    """
    stats: dict = {
        "considered": 0,
        "filled": 0,
        "no_result": 0,
        "failed": 0,
        "skipped_locked": 0,
        "blocked": False,
        "domain": None,
        "rate": None,
        "genres_added": 0,
        "genres_created": 0,
        "title_zh_filled": 0,
        "cover_url_filled": 0,
        "miss_marked": 0,
        "errors": [],
    }
    if rate is None:
        rate = app.config.get("JAVDB_RATE_SECONDS", Config.JAVDB_RATE_SECONDS)
    try:
        rate = max(0.0, float(rate))
    except (TypeError, ValueError):
        rate = max(0.0, float(Config.JAVDB_RATE_SECONDS))
    stats["rate"] = rate
    cookie = (app.config.get("JAVDB_COOKIE") or "").strip() or None

    # 403/429 熔断后的冷却期：不再发请求，直接告诉调用方还要等多久
    remaining = cooldown_remaining(app)
    stats["cooldown_remaining_s"] = remaining
    if remaining > 0:
        stats["blocked"] = True
        stats["errors"].append(f"javdb 冷却中，剩余 {remaining}s（等 403 风控解除）")
        return stats

    try:
        titles = _gap_titles(
            limit=limit, only_codes=only_codes, only_missing_cover=only_missing_cover
        )
    except Exception as exc:  # pragma: no cover - DB 异常
        log.exception("javdb 查询缺口失败")
        stats["errors"].append(f"查询缺口失败: {exc}")
        return stats
    stats["considered"] = len(titles)
    if not titles:
        stats["domain"] = _domain_cache or _stored_domain()
        return stats

    try:
        from app.services.settings import proxy_url as _proxy_url

        _proxy = _proxy_url(app) or ""
    except Exception:  # noqa: BLE001
        _proxy = ""
    session = _new_session(_proxy)
    total = len(titles)
    last_request = 0.0
    for index, title in enumerate(titles, start=1):
        if getattr(title, "manual_lock", False):
            stats["skipped_locked"] += 1
            if progress is not None:
                progress(index, f"javdb 跳过人工锁定 {title.dvd_id}")
            continue
        if rate > 0:
            remaining = rate - (time.monotonic() - last_request)
            if remaining > 0:
                time.sleep(remaining)
        last_request = time.monotonic()
        try:
            detail = _fetch_impl(
                title.dvd_id,
                session=session,
                cookie=cookie,
                preferred_base=_domain_cache or _stored_domain(),
            )
        except JavdbBlocked as exc:
            stats["blocked"] = True
            cooldown = int(app.config.get("JAVDB_COOLDOWN_SECONDS", Config.JAVDB_COOLDOWN_SECONDS) or 1800)
            stats["cooldown_remaining_s"] = _set_cooldown(cooldown)
            stats["errors"].append(
                f"javdb 熔断（403/429）: {exc}；已冷却 {stats['cooldown_remaining_s']}s"
            )
            try:
                _record_task(title, "failed", str(exc))
                db.session.commit()
            except Exception:  # pragma: no cover
                db.session.rollback()
            break
        except Exception as exc:  # noqa: BLE001 - 单条失败不中断
            stats["failed"] += 1
            stats["errors"].append(f"{title.dvd_id}: {exc}")
            try:
                _record_task(title, "failed", str(exc))
                db.session.commit()
            except Exception:  # pragma: no cover
                db.session.rollback()
            continue

        if detail is None:
            # 契约：解析失败/未命中都记 ScrapeTask(stage='javdb_gap', status='failed')
            stats["no_result"] += 1
            _mark_javdb_miss(title)
            stats["miss_marked"] += 1
            try:
                _record_task(title, "failed", f"JavDB 未命中或详情页解析失败: {title.dvd_id}")
                db.session.commit()
            except Exception:  # pragma: no cover
                db.session.rollback()
            if progress is not None:
                progress(index, f"javdb 未命中 {title.dvd_id}")
            continue

        try:
            _apply_detail(title, detail, stats)
            _clear_javdb_miss(title)
            _record_task(title, "ok")
            db.session.commit()
            stats["filled"] += 1
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            stats["failed"] += 1
            stats["errors"].append(f"{title.dvd_id}: 写库失败 {exc}")
            try:
                _record_task(title, "failed", f"写库失败: {exc}")
                db.session.commit()
            except Exception:  # pragma: no cover
                db.session.rollback()
        if progress is not None:
            progress(index, f"javdb 补缺口 {index}/{total}（{title.dvd_id}）")

    domain = _domain_cache or _stored_domain()
    stats["domain"] = _store_domain(domain) if domain else None
    return stats


def refresh_covers(
    app,
    *,
    limit: int = 10,
    progress: Callable[[int, str], None] | None = None,
) -> dict:
    """给**本地缺封面**的条目重新抓 JavDB 封面 URL（DMM 占位图的兜底）。

    只更新 ``cover_url``，不动其它元数据；单条失败只记录，不抛。
    """
    stats = {"considered": 0, "updated": 0, "failed": 0, "errors": []}
    try:
        retry_days = int(Config.JAVDB_MISS_RETRY_DAYS)
    except (TypeError, ValueError):
        retry_days = 30
    miss_cutoff = utcnow() - _dt.timedelta(days=max(0, retry_days))
    try:
        fail_days = max(0, int(Config.ASSET_FAILED_RETRY_DAYS))
    except (TypeError, ValueError):
        fail_days = 30
    fail_cutoff = utcnow() - _dt.timedelta(days=fail_days)
    titles = (
        db.session.query(Title)
        .filter(
            or_(Title.cover_local.is_(None), func.trim(Title.cover_local) == ""),
            Title.dvd_id.isnot(None),
            or_(Title.javdb_miss_at.is_(None), Title.javdb_miss_at < miss_cutoff),
            or_(Title.cover_failed_at.is_(None), Title.cover_failed_at < fail_cutoff),
        )
        .order_by(Title.id)
        .limit(max(1, int(limit)))
        .all()
    )
    stats["considered"] = len(titles)
    for index, title in enumerate(titles, 1):
        detail = fetch(title.dvd_id)
        if detail is None:
            stats["failed"] += 1
            _mark_javdb_miss(title)
            try:
                db.session.commit()
            except Exception:  # noqa: BLE001
                db.session.rollback()
            continue
        _clear_javdb_miss(title)
        if detail.cover_url and detail.cover_url != (title.cover_url or ""):
            title.cover_url = detail.cover_url
            stats["updated"] += 1
            try:
                db.session.commit()
            except Exception as exc:  # noqa: BLE001
                db.session.rollback()
                stats["errors"].append(f"{title.dvd_id}: {type(exc).__name__}")
        if progress:
            progress(index, f"补封面 {index}/{len(titles)}")
    return stats
