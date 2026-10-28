"""从官方/第三方页面导入元数据（FC2、CosplayJAV、欧美厂牌等的通用兜底）。

思路：这些来源没有统一的番号数据库，但都有自己的作品页。用户把作品页 URL 粘进来，
本模块抓 OpenGraph / JSON-LD 里的标题、封面、发布日期、简介，写回 Title，
后续再走自动封面下载。对 FC2 还能按番号直接拼官方 article URL。
"""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from app.config import Config

__all__ = ["fetch_url_metadata", "parse_url_metadata", "fc2_article_url"]

_FC2_CODE_RE = re.compile(r"^FC2(?:-PPV)?-(\d{5,})$", re.I)
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")


def fc2_article_url(code: str) -> str | None:
    """FC2-PPV-1234567 → https://adult.contents.fc2.com/article/1234567/"""
    match = _FC2_CODE_RE.match((code or "").strip())
    if not match:
        return None
    return f"https://adult.contents.fc2.com/article/{match.group(1)}/"


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _image_url(value) -> str | None:
    if isinstance(value, dict):
        return value.get("url") or value.get("contentUrl")
    if isinstance(value, list):
        return _image_url(_first(value))
    return value if isinstance(value, str) else None


def parse_url_metadata(html: str, url: str = "") -> dict:
    """解析页面里的 OpenGraph / JSON-LD；返回 ``{title,image,description,date,site}``。"""
    soup = BeautifulSoup(html or "", "lxml")
    meta: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or "").strip().lower()
        content = (tag.get("content") or "").strip()
        if key and content and key not in meta:
            meta[key] = content

    ld: dict = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "{}")
        except (ValueError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if isinstance(item, dict) and (item.get("name") or item.get("headline")):
                ld = item
                break
        if ld:
            break

    title = (
        meta.get("og:title")
        or ld.get("name")
        or ld.get("headline")
        or (soup.title.get_text(strip=True) if soup.title else "")
    )
    image = (
        meta.get("og:image")
        or meta.get("og:image:secure_url")
        or _image_url(ld.get("image"))
    )
    description = meta.get("og:description") or meta.get("description") or ld.get("description")
    published = (
        meta.get("article:published_time")
        or meta.get("og:release_date")
        or meta.get("date")
        or ld.get("datePublished")
    )
    date_text = None
    if published:
        match = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", str(published))
        if match:
            date_text = f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    if image:
        image = urljoin(url or "", image)
    return {
        "title": (title or "").strip() or None,
        "image": image,
        "description": (description or "").strip() or None,
        "date": date_text,
        "site": (urlparse(url).netloc if url else "") or None,
        "url": url or None,
    }


def fetch_url_metadata(app, url: str, *, timeout: float = 25.0) -> dict:
    """带代理抓页面并解析；失败不抛，返回 ``{ok: False, error}``。"""
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "URL 必须以 http:// 或 https:// 开头"}
    try:
        from app.services.settings import request_proxies

        session = requests.Session()
        session.trust_env = False
        session.headers.update({"User-Agent": Config.USER_AGENT})
        proxies = request_proxies(app)
        if proxies:
            session.proxies.update(proxies)
        response = session.get(url, timeout=timeout, allow_redirects=True)
        if response.status_code >= 400:
            return {"ok": False, "error": f"页面返回 HTTP {response.status_code}", "url": url}
        data = parse_url_metadata(response.text, response.url or url)
        if not data.get("title"):
            return {"ok": False, "error": "页面里没有解析到标题", "url": url}
        return {"ok": True, **data}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "url": url}
