"""运行时设置（存数据库，UI 可改；环境变量作为回退）。

目前只有代理设置，后续可扩展限速/刮削源开关等。
设计取舍：
- DB 设置优先于环境变量；UI 里"启用"后立即对所有联网操作生效（刮削/图片/字幕/下载）。
- 不用 requests 的 ``trust_env`` 隐式行为，调用方显式把 ``request_proxies()`` 塞进 Session。
- 不在代码/日志里回显密码类信息；这里只有 host/port。
"""
from __future__ import annotations

import time
from typing import Any

import requests

from app.config import Config
from app.db import db
from app.models import AppSetting

KEY_PROXY_ENABLED = "proxy_enabled"
KEY_AUTO_SCAN = "auto_scan_new_source"
KEY_SOURCES_BOOTSTRAPPED = "sources_bootstrapped"
KEY_AUTO_ASSETS = "auto_asset_poll"
KEY_VIDEO_FRAME_COUNT = "video_frame_count"
KEY_VIDEO_FRAME_WIDTH = "video_frame_width"
KEY_AUTO_REFRESH_SOURCES = "auto_watch_media"
KEY_SOURCE_REFRESH_MINUTES = "source_watch_minutes"
KEY_SMB_AUTO_REFRESH = "smb_auto_refresh"
KEY_AUTO_MATERIALIZE = "auto_materialize_after_scan"
KEY_PROXY_HOST = "proxy_host"
KEY_PROXY_PORT = "proxy_port"

_TRUE = {"1", "true", "yes", "on"}


def _get(key: str, default: str = "") -> str:
    try:
        row = db.session.get(AppSetting, key)
        if row is not None and row.value is not None:
            return str(row.value)
    except Exception:  # noqa: BLE001 - 无 app context/DB 未初始化时回退
        pass
    return default


def _set(key: str, value: Any) -> None:
    row = db.session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=str(value))
        db.session.add(row)
    else:
        row.value = str(value)
    db.session.commit()


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in _TRUE


def get_value(key: str, default: str = "") -> str:
    """通用读取（无 app context 时返回默认值）。"""
    return _get(key, default)


def set_value(key: str, value: Any) -> None:
    _set(key, value)


def get_bool(key: str, default: bool = False) -> bool:
    return _as_bool(_get(key, "1" if default else "0"))


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(str(_get(key, str(default))).strip())
    except (TypeError, ValueError):
        return default


def set_bool(key: str, value: bool) -> None:
    _set(key, "1" if value else "0")


def get_preferences(app=None) -> dict:
    return {
        "auto_scan_new_source": get_bool(KEY_AUTO_SCAN, True),
        # 自动轮询下载缺失封面/剧照（批次结束自动接下一批；新增影片后自动启动）
        "auto_asset_poll": get_bool(KEY_AUTO_ASSETS, True),
        "video_frame_count": get_int(
            KEY_VIDEO_FRAME_COUNT, int(getattr(Config, "VIDEO_FRAME_COUNT", 6))
        ),
        "auto_materialize_after_scan": get_bool(KEY_AUTO_MATERIALIZE, True),
        "video_frame_width": get_int(
            KEY_VIDEO_FRAME_WIDTH, int(getattr(Config, "VIDEO_FRAME_WIDTH", 0))
        ),
        "auto_refresh_sources": get_bool(KEY_AUTO_REFRESH_SOURCES, False),
        "source_refresh_minutes": get_int(
            KEY_SOURCE_REFRESH_MINUTES,
            int(getattr(Config, "SOURCE_REFRESH_MINUTES", 120)),
        ),
        "smb_auto_refresh": get_bool(KEY_SMB_AUTO_REFRESH, False),
    }


def save_preferences(
    app,
    *,
    auto_scan_new_source: bool,
    auto_asset_poll: bool | None = None,
    video_frame_count: int | None = None,
    auto_materialize_after_scan: bool | None = None,
    video_frame_width: int | None = None,
    auto_refresh_sources: bool | None = None,
    source_refresh_minutes: int | None = None,
    smb_auto_refresh: bool | None = None,
) -> dict:
    set_bool(KEY_AUTO_SCAN, bool(auto_scan_new_source))
    if auto_asset_poll is not None:
        set_bool(KEY_AUTO_ASSETS, bool(auto_asset_poll))
    if video_frame_count is not None:
        from app.config import Config as _Config

        frame_max = int(getattr(_Config, "VIDEO_FRAME_COUNT_MAX", 60) or 60)
        set_value(KEY_VIDEO_FRAME_COUNT, max(1, min(int(video_frame_count), frame_max)))
    if auto_materialize_after_scan is not None:
        set_bool(KEY_AUTO_MATERIALIZE, bool(auto_materialize_after_scan))
    if video_frame_width is not None:
        value = int(video_frame_width)
        set_value(KEY_VIDEO_FRAME_WIDTH, 0 if value <= 0 else min(value, 3840))
    if auto_refresh_sources is not None:
        set_bool(KEY_AUTO_REFRESH_SOURCES, bool(auto_refresh_sources))
    if source_refresh_minutes is not None:
        set_value(KEY_SOURCE_REFRESH_MINUTES,
                  max(1, min(int(source_refresh_minutes), 1440)))
    if smb_auto_refresh is not None:
        set_bool(KEY_SMB_AUTO_REFRESH, bool(smb_auto_refresh))
    return {"ok": True, "preferences": get_preferences(app)}


def _normalize_host(raw: str) -> str:
    raw = (raw or "").strip()
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    raw = raw.strip("/")
    # 允许用户把 "127.0.0.1:7897" 整个粘进地址栏
    if ":" in raw and raw.rsplit(":", 1)[-1].isdigit() and not raw.startswith("["):
        host, _, port = raw.rpartition(":")
        return f"{host}|{port}"
    return raw


def get_proxy(app=None) -> dict:
    """返回生效中的代理配置。

    ``source``: settings（UI 配置生效）| env（环境变量回退）| none
    """
    enabled = _as_bool(_get(KEY_PROXY_ENABLED, "0"))
    raw_host = _get(KEY_PROXY_HOST, "")
    port = _get(KEY_PROXY_PORT, "").strip()
    host = _normalize_host(raw_host)
    if "|" in host:  # 用户把 host:port 贴在地址栏
        host, pasted_port = host.split("|", 1)
        if not port:
            port = pasted_port
    host = host.strip()
    settings_url = f"http://{host}:{port}" if (host and port.isdigit()) else None

    env_url = (Config.HTTP_PROXY or "").strip()
    if enabled:
        url, source = settings_url, ("settings" if settings_url else "settings-invalid")
    else:
        url, source = (env_url or None), ("env" if env_url else "none")

    return {
        "enabled": enabled,
        "host": host,
        "port": port,
        "url": url,
        "source": source,
        "env_available": bool(env_url),
        "env_url": env_url,
    }


def proxy_url(app=None) -> str | None:
    return get_proxy(app).get("url")


def request_proxies(app=None) -> dict | None:
    url = proxy_url(app)
    if not url:
        return None
    return {"http": url, "https": url}


def apply_proxy(session: requests.Session, app=None) -> requests.Session:
    proxies = request_proxies(app)
    if proxies:
        session.proxies.update(proxies)
    return session


def save_proxy(app, *, enabled: bool, host: str, port: Any) -> dict:
    """保存代理设置（校验后落库）。返回 ``{ok, errors, proxy}``。"""
    errors: list[str] = []
    host_n = _normalize_host(host)
    if "|" in host_n:
        host_n, _, pasted_port = host_n.partition("|")
        if not str(port or "").strip():
            port = pasted_port
    host_n = host_n.strip()
    port_s = str(port if port is not None else "").strip()

    if enabled:
        if not host_n:
            errors.append("启用代理时地址不能为空")
        elif any(ch.isspace() for ch in host_n):
            errors.append("代理地址不能包含空格")
        if not port_s:
            errors.append("启用代理时端口不能为空")
        elif not port_s.isdigit() or not (1 <= int(port_s) <= 65535):
            errors.append("端口必须是 1–65535 的数字")

    if errors:
        return {"ok": False, "errors": errors, "proxy": get_proxy(app)}

    _set(KEY_PROXY_ENABLED, "1" if enabled else "0")
    _set(KEY_PROXY_HOST, host_n)
    _set(KEY_PROXY_PORT, port_s)
    return {"ok": True, "errors": [], "proxy": get_proxy(app)}


def test_proxy(app=None, target: str = "https://www.google.com", timeout: float = 10.0) -> dict:
    """用当前代理做一次真实请求，返回连通性与耗时。

    不判断 HTTP 状态码是否为 200，能拿到响应即说明代理链路通。
    """
    proxy = proxy_url(app)
    if not proxy:
        return {"ok": False, "error": "没有启用代理（既无 UI 配置，也无环境变量）"}
    session = requests.Session()
    session.headers.update({"User-Agent": Config.USER_AGENT})
    session.trust_env = False
    session.proxies.update({"http": proxy, "https": proxy})
    started = time.monotonic()
    try:
        resp = session.get(target, timeout=timeout, allow_redirects=False)
        elapsed = int((time.monotonic() - started) * 1000)
        return {
            "ok": True,
            "proxy": proxy,
            "target": target,
            "status_code": resp.status_code,
            "elapsed_ms": elapsed,
        }
    except Exception as exc:  # noqa: BLE001
        elapsed = int((time.monotonic() - started) * 1000)
        return {
            "ok": False,
            "proxy": proxy,
            "target": target,
            "elapsed_ms": elapsed,
            "error": f"{type(exc).__name__}: {exc}",
        }


def get_all(app=None) -> dict:
    """给设置页展示的完整快照。"""
    return {
        "proxy": get_proxy(app),
        "preferences": get_preferences(app),
    }
