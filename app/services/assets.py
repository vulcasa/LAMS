"""封面 / 剧照 / 缩略图下载器。

职责（对应 docs/CONTRACTS.md 与 task-10）
----------------------------------------
``materialize`` 只往主库写了 ``Title.cover_url`` 和 ``Asset(kind='still', url=...)``
URL，本地没有图片。本模块按这些 URL 把图片下载到 ``Config.MEDIA_DIR`` 下：

======  =====================================  ==================================
类型    落盘位置                                数据库回写
======  =====================================  ==================================
封面    ``<COVER_DIR>/<dvd_id>.jpg``            ``Title.cover_local``（相对 MEDIA_DIR）
剧照    ``<STILL_DIR>/<dvd_id>/<idx:02d>.jpg``  ``Asset.local_path`` / ``Asset.bytes``
缩略图  ``<THUMB_DIR>/<dvd_id>_cover.jpg``      ——（宽 400，JPEG quality 82）
======  =====================================  ==================================

设计要点
--------
1. **幂等**：本地文件已存在且大小 > 0 就跳过（除非 ``force=True``）；跳过的文件也会
   顺手把 ``Title.cover_local`` / ``Asset.local_path`` 补上（相对 MEDIA_DIR 的
   POSIX 路径），所以手工放进去的图也能被认领。
2. **占位图当缺失**：DMM 老片会把 ``pics.dmm.co.jp`` 302 到
   ``.../now_printing/now_printing.jpg``（实测 MIDE-001 等）。这类响应是合法
   ``image/jpeg``，只能靠最终 URL / Content-Type / 大小判断——一律**不落盘、
   不回写 local_path**，计入 ``failed`` 与 ``placeholders``。
3. **限速 + 并发**：网络下载用线程池（默认 4 并发），每次请求前过全局令牌桶
   （默认 0.15s/请求）；**写文件、改 ORM、commit 全部在主线程串行**，避免
   SQLite 写竞争。
4. **单条失败不中断整批**：超时/404/坏图/占位图都收进 ``errors``，继续下一张。
5. **代理**：按工作区 AGENTS.md 读 ``HTTPS_PROXY`` / ``HTTP_PROXY``（Config.HTTP_PROXY
   已做环境变量映射），显式设置到 ``requests.Session.proxies``；不硬编码地址。
   测试用 fake session 注入，不联网。
6. 缩略图只由封面生成，路径是约定（``thumbs/<dvd_id>_cover.jpg``），数据库没有
   字段，UI 以后可按约定拼 URL。

CLI 见 ``scripts/download_assets.py``；队列入口是 ``jobs`` 的 ``asset_sync``。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterable

import requests
from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, or_
from sqlalchemy.orm import Query

from app.config import Config
from app.db import db
from app.models import Asset, Title, utcnow

logger = logging.getLogger(__name__)

__all__ = ["AssetStats", "download_for_titles", "missing_assets_summary"]

# ---------------------------------------------------------------------------
# 常量 / 默认值（可用 app.config 覆盖，便于测试与部署调参）
# ---------------------------------------------------------------------------
DEFAULT_WORKERS = 4
#: 全局最小请求间隔（秒）≈ 6.7 req/s；对 pics.dmm.co.jp 足够客气
DEFAULT_MIN_INTERVAL = 0.15
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 20.0
DEFAULT_RETRIES = 1

THUMB_WIDTH = 400
THUMB_QUALITY = 82
STILL_NAME_FORMAT = "{idx:02d}.jpg"
_COMMIT_EVERY = 20
#: DMM 占位图（老片下架/无封面）最终 URL 里带这个片段
_PLACEHOLDER_MARK = "now_printing"
#: 文件名安全化：dvd_id 里理论上只有 [A-Za-z0-9._-]，保险起见替换其余字符
_UNSAFE_FILENAME_RE = re.compile(r"[^0-9A-Za-z._-]+")

_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
@dataclass
class AssetStats:
    """一次资产下载的统计（task-10 要求的字段 + 便于报告的细分项）。"""

    considered: int = 0      # 参与本次的 Title 数
    downloaded: int = 0      # 成功落盘的远程图片数（封面 + 剧照）
    skipped: int = 0         # 本地已有、跳过的文件数（封面 + 剧照 + 缩略图）
    failed: int = 0          # 失败/缺失的图片数（网络错误、404、坏图、占位图）
    bytes: int = 0           # 本次写盘总字节（含缩略图）
    elapsed_s: float = 0.0
    errors: list[str] = field(default_factory=list)
    # 细分（方便 UI / CLI 展示）
    covers: int = 0          # 成功下载的封面数
    stills: int = 0          # 成功下载的剧照数
    thumbs: int = 0          # 生成的缩略图数
    placeholders: int = 0    # 命中 DMM now_printing 占位图的数量


# ---------------------------------------------------------------------------
# 文件名 / 路径
# ---------------------------------------------------------------------------
def _safe_name(dvd_id: str | None) -> str:
    return _UNSAFE_FILENAME_RE.sub("_", (dvd_id or "").strip()) or "unknown"


def _cover_rel_path(dvd_id: str) -> str:
    return f"covers/{_safe_name(dvd_id)}.jpg"


def _still_rel_path(dvd_id: str, idx: int) -> str:
    return f"stills/{_safe_name(dvd_id)}/{STILL_NAME_FORMAT.format(idx=idx)}"


def _thumb_rel_path(dvd_id: str) -> str:
    return f"thumbs/{_safe_name(dvd_id)}_cover.jpg"


def _exists_nonempty(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:  # pragma: no cover - 权限等极端情况
        return False


def _write_atomic(path: Path, data: bytes) -> int:
    """写 ``<path>``（先写 .tmp 再 replace），返回字节数。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return len(data)


# ---------------------------------------------------------------------------
# 图片处理
# ---------------------------------------------------------------------------
def _is_valid_image(data: bytes) -> bool:
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
        return True
    except (UnidentifiedImageError, OSError, ValueError):
        return False


def make_thumbnail(data: bytes, *, width: int = THUMB_WIDTH,
                   quality: int = THUMB_QUALITY) -> bytes:
    """把封面字节缩成宽 ``width`` 的 JPEG（等比），返回 JPEG 字节。"""
    with Image.open(BytesIO(data)) as image:
        image = image.convert("RGB")
        src_width, src_height = image.size
        if src_width != width and src_width > 0:
            height = max(1, round(src_height * width / src_width))
            image = image.resize((width, height), Image.Resampling.LANCZOS)
        buf = BytesIO()
        image.save(buf, "JPEG", quality=quality, optimize=True)
        return buf.getvalue()


# ---------------------------------------------------------------------------
# HTTP（可注入 session，测试全 mock）
# ---------------------------------------------------------------------------
@dataclass
class _FetchResult:
    ok: bool = False
    data: bytes | None = None
    reason: str = ""
    placeholder: bool = False


class _RateLimiter:
    """全局请求节流：保证两次请求开始时间至少间隔 ``min_interval`` 秒。"""

    def __init__(self, min_interval: float):
        self.min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._next_at - now
            self._next_at = max(now, self._next_at) + self.min_interval
        if delay > 0:
            time.sleep(delay)


class _SessionPool:
    """每线程一个 ``requests.Session``（Session 不保证线程安全）。"""

    def __init__(self, factory: Callable[[], requests.Session]):
        self._factory = factory
        self._local = threading.local()
        self._created: list[requests.Session] = []
        self._lock = threading.Lock()

    def get(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._factory()
            self._local.session = session
            with self._lock:
                self._created.append(session)
        return session

    def close(self) -> None:
        with self._lock:
            sessions, self._created = self._created, []
        for session in sessions:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - 关闭失败无所谓
                pass


def _make_session(app) -> requests.Session:
    """创建带 UA / 代理的 requests session。

    代理只认环境变量（``HTTPS_PROXY`` / ``HTTP_PROXY``，Config 已映射），
    不在代码里写死地址。
    """
    session = requests.Session()
    session.trust_env = False  # 显式控制代理，避免环境/测试里的隐式干扰
    session.headers.update({
        "User-Agent": str(app.config.get("USER_AGENT") or _DEFAULT_UA),
        "Accept": "image/jpeg,image/*;q=0.8,*/*;q=0.5",
    })
    try:
        from app.services.settings import proxy_url

        proxy = proxy_url(app) or ""
    except Exception:  # noqa: BLE001
        proxy = (
            app.config.get("HTTP_PROXY")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("HTTP_PROXY")
            or ""
        )
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def _is_placeholder_response(response) -> bool:
    """识别 DMM 的 ``now_printing.jpg`` 占位图。"""
    final_url = str(getattr(response, "url", "") or "")
    if _PLACEHOLDER_MARK in final_url:
        return True
    headers = getattr(response, "headers", None) or {}
    try:
        content_type = str(headers.get("Content-Type") or "").lower()
    except AttributeError:  # pragma: no cover - 奇怪 header 实现
        content_type = ""
    if content_type and not content_type.startswith("image/"):
        return True
    return False


def _fetch_image(session: requests.Session, limiter: _RateLimiter, url: str, *,
                 timeout, retries: int = DEFAULT_RETRIES) -> _FetchResult:
    """下载单张图片并做占位图 / 有效性校验；任何异常都转成 ``_FetchResult``。"""
    reason = "下载失败"
    for attempt in range(max(0, retries) + 1):
        limiter.wait()
        try:
            response = session.get(url, timeout=timeout)
        except requests.Timeout:
            reason = "超时"
        except requests.ConnectionError:
            reason = "连接失败"
        except requests.RequestException as exc:
            reason = f"请求失败: {type(exc).__name__}"
        except Exception as exc:  # noqa: BLE001 - 不能让线程里的异常炸整批
            return _FetchResult(False, reason=f"未预期异常: {type(exc).__name__}: {exc}")
        else:
            status = int(getattr(response, "status_code", 0) or 0)
            if status == 404:
                return _FetchResult(False, reason="HTTP 404（源文件不存在）")
            if _is_placeholder_response(response):
                return _FetchResult(False, reason="DMM 占位图 now_printing", placeholder=True)
            if status >= 500:
                reason = f"HTTP {status}（服务端错误）"
            elif status >= 400:
                return _FetchResult(False, reason=f"HTTP {status}")
            else:
                data = response.content or b""
                if not data:
                    return _FetchResult(False, reason="空响应")
                if not _is_valid_image(data):
                    return _FetchResult(False, reason="响应不是有效图片")
                return _FetchResult(True, data=data)
        if attempt < retries:
            time.sleep(min(1.0, 0.2 * (attempt + 1)))
    return _FetchResult(False, reason=reason)


# ---------------------------------------------------------------------------
# 标题筛选
# ---------------------------------------------------------------------------
def _select_titles(*, only_codes: Iterable[str] | None) -> list[Title]:
    """选出「有封面 URL 或剧照 URL」的 Title（不在这里截断，limit 见主流程）。"""
    has_stills = (
        db.session.query(Asset.id)
        .filter(Asset.title_id == Title.id, Asset.kind == "still",
                Asset.url.isnot(None))
        .exists()
    )
    query: Query = Title.query.filter(
        or_(Title.cover_url.isnot(None), has_stills)
    )
    if only_codes is not None:
        codes = [code for code in only_codes if code]
        if not codes:
            return []
        query = query.filter(Title.dvd_id.in_(codes))
    return list(query.order_by(Title.id).all())


def _still_assets_by_title(title_ids: list[int]) -> dict[int, list[Asset]]:
    """一次取回参与标题的剧照 Asset，按 title_id 分组。"""
    grouped: dict[int, list[Asset]] = {}
    for start in range(0, len(title_ids), 400):
        chunk = title_ids[start:start + 400]
        assets = (
            Asset.query.filter(
                Asset.title_id.in_(chunk),
                Asset.kind == "still",
                Asset.url.isnot(None),
            )
            .order_by(Asset.title_id, Asset.idx, Asset.id)
            .all()
        )
        for asset in assets:
            grouped.setdefault(asset.title_id, []).append(asset)
    return grouped


# ---------------------------------------------------------------------------
# 内部：单个 Title 的下载计划
# ---------------------------------------------------------------------------
@dataclass
class _TitlePlan:
    title_id: int
    dvd_id: str
    cover_url: str | None
    cover_rel: str
    cover_abs: Path
    thumb_rel: str
    thumb_abs: Path
    force: bool = False
    cover_exists: bool = False
    thumb_exists: bool = False
    pending: int = 0
    cover_skipped: bool = False
    cover_downloaded: bool = False
    #: 本地已有、只需认领进 DB 的剧照：(asset_id, idx, rel, size)
    stills_skipped: list[tuple] = field(default_factory=list)
    jobs: list["_Job"] = field(default_factory=list)

    @property
    def needs_work(self) -> bool:
        """是否还有要做的事（网络下载 或 缩略图补生成）。"""
        if self.pending:
            return True
        return bool(
            self.cover_url and self.cover_exists
            and (self.force or not self.thumb_exists)
        )


@dataclass
class _Job:
    plan: _TitlePlan
    kind: str                 # cover | still
    url: str
    rel: str
    abs: Path
    asset_id: int | None = None
    idx: int | None = None


def _upscale_still_bytes(data: bytes, target_width: int = 1920) -> bytes:
    """把低分辨率剧照放大到 target_width（Lanczos）；失败返回原图。"""
    if not target_width or target_width <= 0:
        return data
    try:
        image = Image.open(BytesIO(data))
        image.load()
        if image.width >= target_width:
            return data
        height = max(1, round(image.height * target_width / image.width))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image = image.resize((int(target_width), height), Image.LANCZOS)
        output = BytesIO()
        image.save(output, "JPEG", quality=92, optimize=True)
        return output.getvalue()
    except Exception:  # noqa: BLE001 - 放大失败就用原图
        return data


def _set_asset_local(asset: Asset, rel_path: str, size: int) -> None:
    asset.local_path = rel_path
    asset.bytes = int(size)


def _retry_cutoff(app):
    """失败冷却截止时间：早于它的失败记录才重新尝试。"""
    from datetime import timedelta

    try:
        days = max(0, int(app.config.get("ASSET_FAILED_RETRY_DAYS", 30)))
    except (TypeError, ValueError):
        days = 30
    return utcnow() - timedelta(days=days)


def _failed_recently(value, cutoff) -> bool:
    return value is not None and value >= cutoff


def _mark_cover_failed(title: Title) -> None:
    title.cover_failed_at = utcnow()
    title.cover_fail_count = int(title.cover_fail_count or 0) + 1


def _clear_cover_failed(title: Title) -> None:
    if title.cover_failed_at is not None or (title.cover_fail_count or 0):
        title.cover_failed_at = None
        title.cover_fail_count = 0


def _mark_still_failed(asset: Asset | None) -> None:
    if asset is None:
        return
    asset.failed_at = utcnow()
    asset.fail_count = int(asset.fail_count or 0) + 1


def _clear_still_failed(asset: Asset | None) -> None:
    if asset is None:
        return
    if asset.failed_at is not None or (asset.fail_count or 0):
        asset.failed_at = None
        asset.fail_count = 0


# ---------------------------------------------------------------------------
# 下载主流程
# ---------------------------------------------------------------------------
def download_for_titles(
    app,
    *,
    limit: int | None = None,
    only_codes: Iterable[str] | None = None,
    force: bool = False,
    progress: Callable[[int, str], None] | None = None,
) -> AssetStats:
    """下载封面 / 剧照 / 缩略图，回写 local_path 与 bytes。

    参数
    ----
    app : Flask
        调用方负责 ``app.app_context()``（契约 §1）。
    limit : int | None
        本批最多处理多少个 Title。给定时**优先挑还有缺图的**（按 Title.id 升序），
        所以 UI 反复点「50 部/批」能不断推进；都下完了再跑就是 considered=0。
        None 表示不截断（会把已有文件也计入 ``skipped``）。
    only_codes : Iterable[str] | None
        只处理这些 ``dvd_id``（精确匹配）；None 表示全部。
    force : bool
        True 时已有本地文件也重新下载（占位图/坏图修复用）。
    progress : callable | None
        ``progress(done, message="")``，每完成一个 Title 一次 + 结束时一次。

    返回 :class:`AssetStats`；单张图片失败只记 ``errors``，不抛异常。
    """
    started = time.perf_counter()
    stats = AssetStats()

    media_dir = Path(app.config["MEDIA_DIR"])
    cover_dir = Path(app.config["COVER_DIR"])
    still_dir = Path(app.config["STILL_DIR"])
    thumb_dir = Path(app.config["THUMB_DIR"])
    for directory in (cover_dir, still_dir, thumb_dir):
        directory.mkdir(parents=True, exist_ok=True)

    workers = max(1, int(app.config.get("ASSET_WORKERS", DEFAULT_WORKERS)))
    min_interval = float(app.config.get("ASSET_MIN_INTERVAL", DEFAULT_MIN_INTERVAL))
    timeout = (
        float(app.config.get("ASSET_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT)),
        float(app.config.get("ASSET_READ_TIMEOUT", DEFAULT_READ_TIMEOUT)),
    )
    retries = max(0, int(app.config.get("ASSET_RETRIES", DEFAULT_RETRIES)))

    # ---- 0) 候选标题 ----
    candidates = _select_titles(only_codes=only_codes)
    if not candidates:
        stats.elapsed_s = round(time.perf_counter() - started, 3)
        if progress is not None:
            progress(0, "没有需要下载资产的标题")
        return stats

    assets_by_title = _still_assets_by_title([title.id for title in candidates])
    failure_cutoff = _retry_cutoff(app)

    # ---- 1) 制定计划（纯本地判断，不碰网络、不动 ORM）----
    plans: list[_TitlePlan] = []
    for title in candidates:
        cover_abs = media_dir / _cover_rel_path(title.dvd_id)
        thumb_abs = media_dir / _thumb_rel_path(title.dvd_id)
        plan = _TitlePlan(
            title_id=title.id,
            dvd_id=title.dvd_id,
            cover_url=title.cover_url,
            cover_rel=_cover_rel_path(title.dvd_id),
            cover_abs=cover_abs,
            thumb_rel=_thumb_rel_path(title.dvd_id),
            thumb_abs=thumb_abs,
            force=force,
            cover_exists=bool(title.cover_url) and _exists_nonempty(cover_abs),
            thumb_exists=_exists_nonempty(thumb_abs),
        )
        plans.append(plan)

        if title.cover_url:
            if plan.cover_exists and not force:
                plan.cover_skipped = True  # 本地已有封面
                _clear_cover_failed(title)
            elif not force and _failed_recently(title.cover_failed_at, failure_cutoff):
                pass  # 失败冷却期内：自动/普通任务不再重试，等过期或强制重下
            else:
                plan.pending += 1
                plan.jobs.append(_Job(plan=plan, kind="cover", url=title.cover_url,
                                      rel=plan.cover_rel, abs=cover_abs))

        for position, asset in enumerate(assets_by_title.get(title.id, ()), start=1):
            idx = asset.idx if asset.idx is not None else position
            rel = _still_rel_path(title.dvd_id, idx)
            abs_path = media_dir / rel
            if not force and _exists_nonempty(abs_path):
                plan.stills_skipped.append((asset.id, idx, rel, abs_path.stat().st_size))
            elif not force and _failed_recently(asset.failed_at, failure_cutoff):
                pass  # 失败冷却期内
            else:
                plan.pending += 1
                plan.jobs.append(_Job(plan=plan, kind="still", url=asset.url, rel=rel,
                                      abs=abs_path, asset_id=asset.id, idx=idx))

    # ---- 2) 选本批要处理的 Title（limit 优先挑还有缺图的，保证每批能推进）----
    if limit is not None:
        selected = [plan for plan in plans if plan.needs_work]
        selected = selected[: max(int(limit), 0)]
    else:
        selected = plans
    stats.considered = len(selected)
    if not selected:
        stats.elapsed_s = round(time.perf_counter() - started, 3)
        if progress is not None:
            progress(0, "所有标题的封面/剧照都已存在")
        return stats

    title_by_id = {title.id: title for title in candidates}
    # 认领本地已有文件（只对选中的标题）
    for plan in selected:
        if plan.cover_skipped:
            stats.skipped += 1
        for asset_id, idx, rel, size in plan.stills_skipped:
            asset = db.session.get(Asset, asset_id) if asset_id else None
            if asset is not None:
                if asset.idx is None:
                    asset.idx = idx
                _set_asset_local(asset, rel, size)
            stats.skipped += 1

    jobs = [job for plan in selected for job in plan.jobs]

    # ---- 3) 已全部跳过的 Title 直接收尾（缩略图 / 认领本地封面）----
    done_titles = 0
    for plan in selected:
        if plan.pending == 0:
            done_titles += 1
            _finalize_plan(stats, title_by_id[plan.title_id], plan,
                           media_dir=media_dir, force=force)
            if progress is not None:
                progress(done_titles, plan.dvd_id)
            if done_titles % _COMMIT_EVERY == 0:
                _safe_commit(stats)

    # ---- 4) 并发下载，主线程串行落盘 / 更新 ORM ----
    if jobs:
        session_pool = _SessionPool(lambda: _make_session(app))
        limiter = _RateLimiter(min_interval)
        remaining = {plan.title_id: plan.pending for plan in selected if plan.pending}

        def _work(job: _Job) -> tuple[_Job, _FetchResult]:
            session = session_pool.get()
            try:
                result = _fetch_image(session, limiter, job.url,
                                      timeout=timeout, retries=retries)
            except Exception as exc:  # noqa: BLE001 - 极端情况兜底
                result = _FetchResult(False, reason=f"下载异常: {type(exc).__name__}: {exc}")
            return job, result

        try:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(_work, job): job for job in jobs}
                for future in as_completed(futures):
                    job, result = future.result()
                    plan = job.plan
                    title = title_by_id[plan.title_id]
                    if result.ok and result.data is not None:
                        try:
                            payload = result.data
                            if job.kind == "still":
                                payload = _upscale_still_bytes(
                                    payload,
                                    int(app.config.get("STILL_TARGET_WIDTH", 1920) or 0),
                                )
                            size = _write_atomic(job.abs, payload)
                        except OSError as exc:
                            stats.failed += 1
                            stats.errors.append(f"{plan.dvd_id} {job.kind}"
                                                f"{'#' + str(job.idx) if job.idx else ''}: "
                                                f"写文件失败: {exc}")
                            if job.kind == "cover":
                                _mark_cover_failed(title)
                            else:
                                _mark_still_failed(
                                    db.session.get(Asset, job.asset_id) if job.asset_id else None
                                )
                        else:
                            stats.downloaded += 1
                            stats.bytes += size
                            if job.kind == "cover":
                                stats.covers += 1
                                plan.cover_downloaded = True
                                _clear_cover_failed(title)
                            else:
                                stats.stills += 1
                                asset = db.session.get(Asset, job.asset_id) if job.asset_id else None
                                if asset is not None:
                                    if asset.idx is None:
                                        asset.idx = job.idx
                                    _set_asset_local(asset, job.rel, size)
                                    _clear_still_failed(asset)
                    else:
                        stats.failed += 1
                        if result.placeholder:
                            stats.placeholders += 1
                        if job.kind == "cover":
                            _mark_cover_failed(title)
                        else:
                            _mark_still_failed(
                                db.session.get(Asset, job.asset_id) if job.asset_id else None
                            )
                        label = job.kind + (f"#{job.idx}" if job.idx is not None else "")
                        stats.errors.append(f"{plan.dvd_id} {label}: {result.reason}")

                    remaining[plan.title_id] -= 1
                    if remaining[plan.title_id] == 0:
                        done_titles += 1
                        _finalize_plan(stats, title, plan,
                                       media_dir=media_dir, force=force)
                        if progress is not None:
                            progress(done_titles, plan.dvd_id)
                        if done_titles % _COMMIT_EVERY == 0:
                            _safe_commit(stats)
        finally:
            session_pool.close()

    _safe_commit(stats)
    stats.elapsed_s = round(time.perf_counter() - started, 3)
    logger.info(
        "asset_sync 完成: considered=%s downloaded=%s skipped=%s failed=%s "
        "bytes=%s covers=%s stills=%s thumbs=%s placeholders=%s errors=%s elapsed=%.3fs",
        stats.considered, stats.downloaded, stats.skipped, stats.failed,
        stats.bytes, stats.covers, stats.stills, stats.thumbs,
        stats.placeholders, len(stats.errors), stats.elapsed_s,
    )
    if progress is not None:
        progress(
            stats.considered,
            f"完成：下载 {stats.downloaded}（封面 {stats.covers}/剧照 {stats.stills}），"
            f"缩略图 {stats.thumbs}，跳过 {stats.skipped}，失败 {stats.failed}",
        )
    return stats


# ---------------------------------------------------------------------------
# 收尾：封面认领 / 缩略图
# ---------------------------------------------------------------------------
def _finalize_plan(stats: AssetStats, title: Title, plan: _TitlePlan, *,
                   media_dir: Path, force: bool) -> None:
    """补齐 Title.cover_local 与缩略图（在下载完成后调用）。"""
    try:
        with db.session.begin_nested():
            if title.cover_url and _exists_nonempty(plan.cover_abs):
                _clear_cover_failed(title)
                if title.cover_local != plan.cover_rel:
                    title.cover_local = plan.cover_rel

                # 缩略图：缺失 / force / 本次新下载了封面时生成
                need_thumb = (
                    force
                    or plan.cover_downloaded
                    or not _exists_nonempty(plan.thumb_abs)
                )
                if need_thumb:
                    try:
                        thumb_data = make_thumbnail(plan.cover_abs.read_bytes())
                    except (OSError, UnidentifiedImageError, ValueError) as exc:
                        stats.failed += 1
                        stats.errors.append(f"{plan.dvd_id} thumb: 缩略图生成失败: {exc}")
                    else:
                        size = _write_atomic(plan.thumb_abs, thumb_data)
                        stats.thumbs += 1
                        stats.bytes += size
                else:
                    stats.skipped += 1
    except Exception as exc:  # noqa: BLE001 - 单标题收尾失败不影响整批
        logger.exception("收尾标题 %s 失败", plan.dvd_id)
        stats.errors.append(f"{plan.dvd_id} finalize: {type(exc).__name__}: {exc}")


def _safe_commit(stats: AssetStats) -> None:
    try:
        db.session.commit()
    except Exception as exc:  # noqa: BLE001 - 提交失败记录后回滚，继续下一批
        logger.exception("资产元数据提交失败")
        db.session.rollback()
        stats.errors.append(f"commit 失败: {type(exc).__name__}: {exc}")


def missing_assets_summary(app) -> dict:
    """统计还有多少可下载的缺失封面/剧照（自动轮询用，只读 DB，不联网）。

    返回 ``actionable`` = 本轮自动任务值得处理的条数：
    - 有封面 URL 但本地没封面；
    - 有剧照 URL 但本地没剧照；
    - 没封面 URL、且 JavDB 还没在冷却期内标记过未命中（可尝试从 JavDB 抓封面）。
    JavDB 处于 403 冷却时不计入第三类，避免自动轮询空转。
    """
    try:
        from datetime import timedelta

        missing_cover = or_(Title.cover_local.is_(None), func.trim(Title.cover_local) == "")
        cover_url_missing = or_(
            Title.cover_url.is_(None), func.trim(Title.cover_url) == ""
        )
        try:
            fail_days = max(0, int(app.config.get("ASSET_FAILED_RETRY_DAYS", 30)))
        except (TypeError, ValueError):
            fail_days = 30
        fail_cutoff = utcnow() - timedelta(days=fail_days)
        cover_with_url = (
            Title.query.filter(
                missing_cover,
                ~cover_url_missing,
                or_(Title.cover_failed_at.is_(None), Title.cover_failed_at < fail_cutoff),
            ).count()
        )

        try:
            retry_days = max(0, int(Config.JAVDB_MISS_RETRY_DAYS))
        except (TypeError, ValueError):
            retry_days = 30
        cutoff = utcnow() - timedelta(days=retry_days)
        javdb_cooldown = 0
        try:
            from app.services.javdb import cooldown_remaining

            javdb_cooldown = int(cooldown_remaining(app) or 0)
        except Exception:  # noqa: BLE001
            javdb_cooldown = 0
        cover_from_javdb = 0
        if javdb_cooldown <= 0:
            cover_from_javdb = (
                Title.query.filter(
                    missing_cover,
                    cover_url_missing,
                    or_(Title.javdb_miss_at.is_(None), Title.javdb_miss_at < cutoff),
                ).count()
            )

        stills_missing = (
            Asset.query.filter(
                Asset.kind == "still",
                Asset.url.isnot(None),
                func.trim(Asset.url) != "",
                or_(Asset.local_path.is_(None), func.trim(Asset.local_path) == ""),
                or_(Asset.failed_at.is_(None), Asset.failed_at < fail_cutoff),
            ).count()
        )
        return {
            "covers_with_url": cover_with_url,
            "covers_from_javdb": cover_from_javdb,
            "stills": stills_missing,
            "actionable": cover_with_url + cover_from_javdb + stills_missing,
            "javdb_cooldown_s": javdb_cooldown,
        }
    except Exception:  # noqa: BLE001 - 自动轮询不能因统计失败而中断
        logger.exception("统计缺失封面/剧照失败")
        return {"covers_with_url": 0, "covers_from_javdb": 0, "stills": 0,
                "actionable": 0, "javdb_cooldown_s": 0}


def upscale_existing_stills(app, *, limit: int | None = None,
                            progress: Callable[[int, str], None] | None = None) -> dict:
    """把已下载的低分辨率剧照原地升级到 STILL_TARGET_WIDTH。"""
    target = int(app.config.get("STILL_TARGET_WIDTH", 1920) or 0)
    media_dir = Path(app.config["MEDIA_DIR"])
    stats = {"considered": 0, "upscaled": 0, "skipped": 0, "failed": 0, "bytes": 0}
    query = Asset.query.filter(
        Asset.kind == "still",
        Asset.local_path.isnot(None),
        func.trim(Asset.local_path) != "",
    ).order_by(Asset.id)
    if limit:
        query = query.limit(int(limit))
    assets = query.all()
    stats["considered"] = len(assets)
    for index, asset in enumerate(assets, start=1):
        path = media_dir / str(asset.local_path)
        try:
            if not path.is_file():
                stats["failed"] += 1
                continue
            original = path.read_bytes()
            upgraded = _upscale_still_bytes(original, target)
            if upgraded is original or len(upgraded) == len(original):
                stats["skipped"] += 1
            else:
                size = _write_atomic(path, upgraded)
                asset.bytes = int(size)
                stats["upscaled"] += 1
                stats["bytes"] += size - len(original)
        except Exception:  # noqa: BLE001
            stats["failed"] += 1
        if progress is not None:
            progress(index, f"剧照升级 {index}/{len(assets)}")
        if index % _COMMIT_EVERY == 0:
            _safe_commit(AssetStats())
    _safe_commit(AssetStats())
    return stats
