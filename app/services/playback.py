"""内存中的「正在播放」会话表（活动页展示 IP / 影片 / 进度）。"""
from __future__ import annotations

import threading
import time
from typing import Optional


class _PlaybackSessions:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}

    def update(self, *, ip: str, session_id: str, title_id: int, dvd_id: str,
               title_name: str, file_id: int, file_name: str,
               position: float, duration: float, paused: bool) -> None:
        now = time.monotonic()
        key = f"{ip}|{session_id or 'default'}"
        with self._lock:
            previous = self._sessions.get(key)
            self._sessions[key] = {
                "key": key,
                "ip": ip or "unknown",
                "title_id": title_id,
                "dvd_id": dvd_id or "",
                "title_name": title_name or "",
                "file_id": file_id,
                "file_name": file_name or "",
                "position": max(0.0, float(position or 0)),
                "duration": max(0.0, float(duration or 0)),
                "paused": bool(paused),
                "started_at": previous["started_at"] if previous else now,
                "last_seen": now,
            }

    def active(self, ttl: float = 45.0) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            expired = [key for key, row in self._sessions.items() if now - row["last_seen"] > ttl]
            for key in expired:
                self._sessions.pop(key, None)
            rows = sorted(self._sessions.values(), key=lambda row: row["last_seen"], reverse=True)
            result = []
            for row in rows:
                duration = row["duration"]
                position = row["position"]
                percent = round(position * 100 / duration, 1) if duration > 0 else None
                result.append({
                    "ip": row["ip"],
                    "title_id": row["title_id"],
                    "dvd_id": row["dvd_id"],
                    "title_name": row["title_name"],
                    "file_id": row["file_id"],
                    "file_name": row["file_name"],
                    "position": round(position, 1),
                    "duration": round(duration, 1),
                    "percent": percent,
                    "paused": row["paused"],
                    "elapsed_s": int(now - row["started_at"]),
                    "last_seen_s": round(now - row["last_seen"], 1),
                })
            return result


playback_sessions = _PlaybackSessions()


def active_playback(ttl: float = 45.0) -> list[dict]:
    return playback_sessions.active(ttl=ttl)
