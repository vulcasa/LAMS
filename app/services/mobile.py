"""移动端访问识别与允许的页面范围。"""
from __future__ import annotations

import re

from flask import has_request_context, request

_MOBILE_UA = re.compile(
    r"android|iphone|ipad|ipod|mobile|windows phone|webos|blackberry|"
    r"opera mini|iemobile|bb10|playbook",
    re.I,
)

# 移动端只开放片库（含详情）、正在播放及其播放所需的只读资源。
_MOBILE_ENDPOINTS = {
    "static",
    "pages.home",
    "pages.healthz",
    "library.index",
    "library.detail",
    "library.title_playback",
    "library.toggle_favorite",
    "library.video_frames",
    "pages.player",
    "pages.client_player",
    "pages.player_source",
    "pages.media",
    "pages.api_recent_plays",
    "pages.api_record_play",
    "pages.player_session",
}


def is_mobile_request() -> bool:
    if not has_request_context():
        return False
    if request.headers.get("Sec-CH-UA-Mobile", "").strip() == "?1":
        return True
    ua = request.headers.get("User-Agent", "")
    return bool(_MOBILE_UA.search(ua or ""))


def mobile_endpoint_allowed(endpoint: str | None) -> bool:
    if not endpoint:
        return False
    if endpoint in _MOBILE_ENDPOINTS:
        return True
    return endpoint.startswith("static.")
