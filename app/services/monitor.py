"""实例监控：CPU / 负载 / 内存 / 网络 / 在线访问人数。

轻量实现，不引入 psutil；数据来自 Linux /proc。采样在调用时计算增量，
5 秒一次请求即可，避免额外后台采样线程。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional


class _Monitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._cpu_prev: Optional[tuple[float, float]] = None
        self._net_prev: Optional[tuple[float, float, float]] = None
        self._active = 0
        self._clients: dict[str, float] = {}

    @staticmethod
    def client_ip() -> str:
        try:
            from flask import request, has_request_context

            if has_request_context():
                forwarded = request.headers.get("X-Forwarded-For", "")
                if forwarded:
                    return forwarded.split(",")[0].strip()
                return request.remote_addr or "unknown"
        except Exception:  # noqa: BLE001
            pass
        return "unknown"

    def begin_request(self) -> None:
        ip = self.client_ip()
        with self._lock:
            self._active += 1
            self._clients[ip] = time.monotonic()

    def end_request(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    def _cpu(self) -> Optional[float]:
        try:
            with open("/proc/stat", encoding="utf-8") as handle:
                fields = handle.readline().split()[1:]
            values = [int(value) for value in fields]
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            total = sum(values)
        except (OSError, ValueError, IndexError):
            return None
        with self._lock:
            previous = self._cpu_prev
            self._cpu_prev = (total, idle)
        if previous is None:
            return None
        total_delta = total - previous[0]
        idle_delta = idle - previous[1]
        if total_delta <= 0:
            return 0.0
        return max(0.0, min(100.0, (total_delta - idle_delta) * 100.0 / total_delta))

    def _load(self) -> dict:
        try:
            with open("/proc/loadavg", encoding="utf-8") as handle:
                parts = handle.read().split()
            return {"1": float(parts[0]), "5": float(parts[1]), "15": float(parts[2])}
        except (OSError, ValueError, IndexError):
            return {"1": None, "5": None, "15": None}

    def _memory(self) -> dict:
        info = {}
        try:
            with open("/proc/meminfo", encoding="utf-8") as handle:
                for line in handle:
                    key, _, value = line.partition(":")
                    if value.strip().endswith("kB"):
                        info[key] = int(value.split()[0]) * 1024
        except (OSError, ValueError):
            return {"total": 0, "used": 0, "percent": None}
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", info.get("MemFree", 0))
        used = max(0, total - available)
        percent = round(used * 100.0 / total, 1) if total else None
        return {"total": total, "used": used, "percent": percent}

    def _network(self) -> dict:
        rx = tx = 0
        try:
            with open("/proc/net/dev", encoding="utf-8") as handle:
                for line in handle:
                    if ":" not in line:
                        continue
                    name, _, values = line.partition(":")
                    if name.strip() == "lo":
                        continue
                    fields = values.split()
                    rx += int(fields[0])
                    tx += int(fields[8])
        except (OSError, ValueError, IndexError):
            return {"rx_bps": 0.0, "tx_bps": 0.0, "rx_total": 0, "tx_total": 0}
        now = time.monotonic()
        with self._lock:
            previous = self._net_prev
            self._net_prev = (now, rx, tx)
        if previous is None or now <= previous[0]:
            return {"rx_bps": 0.0, "tx_bps": 0.0, "rx_total": rx, "tx_total": tx}
        elapsed = now - previous[0]
        return {
            "rx_bps": max(0.0, (rx - previous[1]) / elapsed),
            "tx_bps": max(0.0, (tx - previous[2]) / elapsed),
            "rx_total": rx,
            "tx_total": tx,
        }

    def snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            cutoff = now - 120.0
            self._clients = {
                ip: seen for ip, seen in self._clients.items() if seen >= cutoff
            }
            online = len(self._clients)
            active = self._active
        return {
            "ok": True,
            "timestamp": now,
            "uptime_s": int(now - self._started),
            "cpu_percent": self._cpu(),
            "load": self._load(),
            "memory": self._memory(),
            "network": self._network(),
            "users": {"online": online, "active_requests": active},
        }


monitor = _Monitor()


def snapshot() -> dict:
    return monitor.snapshot()
