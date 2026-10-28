"""限制同时占用 SMB 的媒体请求，避免挂起的 SMB I/O 耗尽所有 gunicorn 线程。"""
from __future__ import annotations

import threading

_SLOTS = threading.BoundedSemaphore(6)


def acquire() -> bool:
    return _SLOTS.acquire(blocking=False)


def release() -> None:
    try:
        _SLOTS.release()
    except ValueError:
        pass


def busy_message() -> str:
    return "媒体读取繁忙或被 SMB 阻塞，请稍后重试；片库页面不受影响"


def guard_json(view):
    """JSON 接口并发槽；忙碌立即 503，不排队占用 gunicorn 线程。"""
    from functools import wraps

    from flask import jsonify

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not acquire():
            return jsonify({"ok": False, "status": "busy",
                            "message": busy_message()}), 503
        try:
            return view(*args, **kwargs)
        finally:
            release()

    return wrapper
