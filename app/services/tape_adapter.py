"""磁带适配器：把 v1（ltfs-webui）或本机 ltfs 命令包成同一个接口。

背景（详见 docs/CONTRACTS.md §4 与 research/existing-repo.md）：

- 合并 v1 之后，本服务自己控设备，生产默认走 ``DirectAdapter``（``TAPE_ADAPTER=direct``）；
  ``HttpAdapter`` 保留给"v1 还在跑、由它独占驱动器"的过渡场景——两种实现不能同时
  挂载同一台驱动器。
- v1 的 ``POST /api/tape/mount`` **没有参数**：挂载"当前插入的磁带"，成功后返回实际
  条码。也就是说 mount 没法指定盘，**比对预期条码是 tape_ops/audit 的职责** —— 适配器
  只如实返回实际条码（解析不出来时返回 ``UNKNOWN``，由上层中止，防串盘）。
- v1 的 ``unmount``/``eject`` 路由内部会 ``request.get_json()``，不带 JSON body 会
  500，所以这里统一带 ``json={}``（v1 的 ``tape_id`` 分支对 None 是安全的）。
- v1 的 ``CommandExecutor`` 超时只 join 线程、**不 kill 子进程**（D6 缺陷，会留下
  幽灵 ``ltfs`` 进程导致双挂载）。``DirectAdapter`` 这里用 ``Popen`` + ``kill()``，
  超时后确认子进程已回收再返回。
- 每次设备操作都会把"命令 + stdout/stderr"记到 ``last_output``（不截断，由
  ``tape_ops`` 截到 8KB 再入库），供操作日志/故障排查用。

返回值约定（**任何方法都不向调用方抛异常**，UI 只需看 ``(ok, message)``）：

    status()            -> TapeStatus
    mount()             -> (ok, message, actual_barcode | None)
    unmount()/eject()   -> (ok, message)
    device_list()       -> list[dict]    # [{path, model, serial_number, firmware_version, raw}]
    device_info()       -> dict          # {model, serial_number, firmware_version, raw, error}
    volume_label()      -> str | None    # 读挂载点的 .volume_label（当前带的标签）
    scan(mount_path, barcode) -> scanner.ScanStats   # 这里允许抛（audit 会兜住）

``scan`` 延迟 import ``app.services.scanner``：并行开发期该模块可能还不存在，测试里
monkeypatch ``_import_scanner`` 即可；生产环境缺失时抛 RuntimeError，audit 记为 failed。
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

import requests

from app.config import Config

logger = logging.getLogger(__name__)

# 条码不可信时的哨兵值（v1 的 _get_barcode 三级兜底失败就返回它）
UNKNOWN_BARCODE = "UNKNOWN"
# 认为"不可用"的条码：空 / UNKNOWN / NONE（大小写不敏感）
_INVALID_BARCODES = {"", UNKNOWN_BARCODE, "NONE", "UNKNOWN_BARCODE"}


def normalize_barcode(value: Any) -> str | None:
    """条码归一化：去空白；空值/UNKNOWN 一律返回 None（调用方据此中止）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def barcode_is_usable(value: Any) -> bool:
    """条码是否可用于盘点（非空且不是 UNKNOWN/NONE）。"""
    text = normalize_barcode(value)
    return bool(text) and text.upper() not in _INVALID_BARCODES


def barcode_matches(actual: Any, expected: Any) -> bool:
    """实际条码是否等于预期条码（大小写/空白不敏感；预期为空时只要求可用）。"""
    actual_n = normalize_barcode(actual)
    expected_n = normalize_barcode(expected)
    if not actual_n or not barcode_is_usable(actual_n):
        return False
    if expected_n is None:
        return True
    return actual_n.upper() == expected_n.upper()


# ---------------------------------------------------------------------------
# 返回值
# ---------------------------------------------------------------------------
@dataclass
class TapeStatus:
    """一盘磁带的当前状态（给 UI/audit 显示用，字段与契约一致）。"""

    mounted: bool = False
    barcode: str | None = None
    device_exists: bool = False
    device_path: str | None = None
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------
def _import_scanner():
    """延迟 import scanner（并行开发隔离；测试里可 monkeypatch 本函数）。"""
    try:
        from app.services import scanner  # noqa: PLC0415 - 故意延迟
    except ImportError as exc:  # pragma: no cover - 生产环境缺模块
        raise RuntimeError(
            "scanner 模块不可用：缺少 app/services/scanner.py，无法扫描磁带"
        ) from exc
    return scanner


class TapeAdapter:
    """磁带操作接口。子类实现设备操作；``scan``/输出记录是共用的。

    子类构造时把 ``app`` 传进来（``get_adapter(cfg, app=app)`` 会传），因为
    ``scanner.scan_tape`` 需要 Flask app；``mount_point`` 给 audit/tape_ops 用。

    ``last_output`` 保存**最近一次设备操作**的命令与输出（不截断），
    ``tape_ops`` 会把它截到 8KB 后写 ``TapeOperation``；其它调用方不该依赖它的生命周期。
    """

    def __init__(self, *, app=None, mount_point: str | os.PathLike | None = None):
        self._app = app
        self.mount_point = str(mount_point or Config.LTFS_MOUNT)
        self.last_output: dict = {"command": None, "stdout": "", "stderr": ""}

    # --- 子类必须实现 ---------------------------------------------------
    def status(self) -> TapeStatus:
        raise NotImplementedError

    def mount(self) -> tuple[bool, str, str | None]:
        raise NotImplementedError

    def unmount(self) -> tuple[bool, str]:
        raise NotImplementedError

    def eject(self) -> tuple[bool, str]:
        raise NotImplementedError

    def device_list(self) -> list[dict]:
        """枚举驱动器（``ltfs -o device_list`` / v1 ``/api/tape/device-list``）。

        返回统一的 ``[{path, model, serial_number, firmware_version, raw}]``；失败返回 ``[]``。
        """
        raise NotImplementedError

    def device_info(self) -> dict:
        """读取驱动器信息（Model/Serial/Firmware）。

        返回 ``{model, serial_number, firmware_version, raw, error}``；失败时字段为 None
        且 ``error`` 有说明，**不抛异常**。
        """
        raise NotImplementedError

    def volume_label(self) -> str | None:
        """当前带的标签（挂载点 ``.volume_label``）；读不到返回 None。"""
        raise NotImplementedError

    # --- 共用 -----------------------------------------------------------
    def _record_output(
        self, *, command: str | None, stdout: Any = None, stderr: Any = None
    ) -> None:
        """记录最近一次设备操作的命令/输出（原始、不截断；由 tape_ops 截断入库）。"""
        self.last_output = {
            "command": command,
            "stdout": "" if stdout is None else str(stdout),
            "stderr": "" if stderr is None else str(stderr),
        }

    def last_command(self) -> dict:
        """最近一次设备操作的 ``{command, stdout, stderr}`` 快照（可能是空值）。"""
        return dict(self.last_output or {})

    @property
    def device_path(self) -> str | None:
        """驱动器设备路径（Direct 为 ``LTFS_DEVICE``；HTTP 不知道时为 None）。"""
        device = getattr(self, "device", None)
        return str(device) if device else None

    def scan(self, mount_path: str, barcode: str, *, progress=None):
        """扫描已挂载的磁带，写库交给 scanner；返回其 ScanStats。

        ``barcode`` 必须是 mount 返回的**实际条码**，预期条码由 audit 预先比对。
        """
        scanner = _import_scanner()
        app = self._app
        if app is None:
            raise RuntimeError("TapeAdapter.scan 需要 app：请用 get_adapter(cfg, app=app) 构造")
        return scanner.scan_tape(app, mount_path, barcode=barcode, progress=progress)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__} mount_point={self.mount_point!r}>"


# ---------------------------------------------------------------------------
# HTTP 实现（过渡期兼容）：调 v1 的 API，由 v1 独占驱动器
# ---------------------------------------------------------------------------
def _response_message(data: Any, fallback: str = "") -> str:
    """从 v1 的 JSON 里取 message（v1 成功/失败响应都是 {success, message}）。"""
    if isinstance(data, dict):
        msg = data.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
    return fallback


def _clean_info_value(value: Any) -> str | None:
    """设备信息值归一化：去空白；v1 的 'Unknown'/'UNKNOWN' 之类占位值当 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in ("UNKNOWN", "NONE", "N/A", "NA", "-"):
        return None
    return text


_DEVICE_INFO_FIELDS = (
    ("model", re.compile(r"model", re.IGNORECASE)),
    ("serial_number", re.compile(r"serial", re.IGNORECASE)),
    ("firmware_version", re.compile(r"firmware", re.IGNORECASE)),
)
_DEVICE_PATH_RE = re.compile(r"(/dev/sg\d+)")


def parse_device_info_text(text: str | None) -> dict:
    """解析 ``ltfs -o devname=... -o device_info`` 输出（对齐 v1 的 Model/Serial/Firmware 行）。

    真实输出形如::

        Device: /dev/sg10
          Vendor: HP
          Model: Ultrium 6-HH
          Serial: HU1234
          Firmware: 3.1.0
    """
    info = {"model": None, "serial_number": None, "firmware_version": None}
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for field, pattern in _DEVICE_INFO_FIELDS:
            if info[field] is not None or not pattern.search(stripped):
                continue
            match = re.search(r"[:=]\s*(.+)$", stripped)
            if match:
                info[field] = _clean_info_value(match.group(1))
            break
    return info


def parse_device_list_text(text: str | None) -> list[dict]:
    """解析 ``ltfs -o device_list`` 输出里的 ``/dev/sgN``（对齐 v1 的正则提取）。"""
    devices: list[dict] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("ltfs"):
            continue
        match = _DEVICE_PATH_RE.search(stripped)
        if not match:
            continue
        path = match.group(1)
        if path in seen:
            continue
        seen.add(path)
        devices.append(
            {
                "path": path,
                "model": None,
                "serial_number": None,
                "firmware_version": None,
                "raw": stripped,
            }
        )
    return devices


class HttpAdapter(TapeAdapter):
    """过渡期兼容：走 ltfs-webui 的 HTTP API；扫描仍直接读共享挂载点（只读 os.walk）。"""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | int | str | None = None,
        *,
        app=None,
        session: requests.Session | None = None,
        mount_point: str | os.PathLike | None = None,
    ):
        super().__init__(app=app, mount_point=mount_point)
        raw_base = str(base_url or Config.LTFS_API_BASE or "").strip()
        self.base_url = raw_base.rstrip("/")
        self.timeout = _coerce_timeout(timeout if timeout is not None else Config.LTFS_TIMEOUT)
        # 状态查询是轻量 GET，别用 mount 的 300s 超时把 UI 卡死
        self.status_timeout = min(self.timeout, 15.0)
        self._session = session

    # --- 内部：统一请求/错误处理（绝不抛） -------------------------------
    @property
    def session(self) -> requests.Session:
        if self._session is None:
            sess = requests.Session()
            # v1 在 docker 内网/localhost，不应走用户代理；避免公司代理把本机请求绕出去
            sess.trust_env = False
            self._session = sess
        return self._session

    def _call(
        self,
        method: str,
        path: str,
        *,
        action: str,
        payload: dict | None = None,
        timeout: float | None = None,
    ) -> tuple[dict | None, str | None]:
        """发一次请求，返回 ``(json_dict, error_message)``；错误只记在 message 里。"""
        if not self.base_url:
            return None, f"{action}失败：未配置 LTFS_API_BASE"
        url = f"{self.base_url}{path}"
        req_timeout = self.timeout if timeout is None else timeout
        try:
            if method.upper() == "GET":
                resp = self.session.get(url, timeout=req_timeout)
            else:
                # v1 的 unmount/eject 路由会调 request.get_json()，必须带 JSON body
                resp = self.session.post(url, json=payload if payload is not None else {}, timeout=req_timeout)
        except requests.Timeout:
            return None, f"{action}超时（>{req_timeout:g}s）：{url}"
        except requests.RequestException as exc:
            return None, f"{action}失败：连不上 {url}（{type(exc).__name__}: {exc}）"
        except Exception as exc:  # pragma: no cover - 防御性兜底
            return None, f"{action}失败：{type(exc).__name__}: {exc}"

        data: Any = None
        try:
            data = resp.json()
        except ValueError:
            data = None

        if resp.status_code >= 400:
            msg = _response_message(data)
            if not msg:
                msg = (resp.text or "").strip()[:200]
            suffix = f"：{msg}" if msg else ""
            return None, f"{action}失败：HTTP {resp.status_code}{suffix}"
        if not isinstance(data, dict):
            return None, f"{action}失败：响应不是 JSON 对象（HTTP {resp.status_code}）"
        return data, None

    @staticmethod
    def _tape_from(data: dict) -> dict:
        tape = data.get("tape")
        return tape if isinstance(tape, dict) else {}

    # --- 对外接口 -------------------------------------------------------
    def status(self) -> TapeStatus:
        raw: dict = {"adapter": "http", "base_url": self.base_url}
        errors: list[str] = []

        device_exists = False
        device_path: str | None = None
        device, err = self._call(
            "GET", "/api/tape/device-status", action="查询设备状态", timeout=self.status_timeout
        )
        if err:
            errors.append(err)
        elif device is not None:
            device_exists = bool(device.get("device_exists"))
            device_path = device.get("device_path") or None
            raw["device"] = device

        mounted = False
        barcode: str | None = None
        current, err = self._call(
            "GET", "/api/tape/current", action="查询当前磁带", timeout=self.status_timeout
        )
        if err:
            # 404 是"没有磁带"的正常情况，不算错误
            if "HTTP 404" not in err:
                errors.append(err)
        elif current is not None:
            raw["current"] = current
            tape = self._tape_from(current)
            if tape.get("status") == "mounted" or tape.get("barcode"):
                # v1 的 status 会被重启刷成 ejected，不能只信它；有 barcode 就报出来
                mounted = tape.get("status") == "mounted"
                barcode = normalize_barcode(tape.get("barcode"))
        if errors:
            raw["errors"] = errors
        return TapeStatus(
            mounted=mounted,
            barcode=barcode,
            device_exists=device_exists,
            device_path=device_path,
            raw=raw,
        )

    def _record_http(self, method: str, path: str, data: Any, err: str | None) -> None:
        """把 v1 的 HTTP 调用记成 v1 Operation 风格的 command/stdout/stderr。"""
        command = f"{method.upper()} {self.base_url}{path}"
        stdout = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else ""
        self._record_output(command=command, stdout=stdout, stderr=err or "")

    def mount(self) -> tuple[bool, str, str | None]:
        data, err = self._call("POST", "/api/tape/mount", action="挂载磁带")
        self._record_http("POST", "/api/tape/mount", data, err)
        if err:
            return False, err, None
        assert data is not None
        if not data.get("success"):
            return False, _response_message(data, "挂载失败"), None
        barcode = normalize_barcode(self._tape_from(data).get("barcode") or data.get("barcode"))
        return True, _response_message(data, "磁带挂载成功"), barcode

    def unmount(self) -> tuple[bool, str]:
        data, err = self._call("POST", "/api/tape/unmount", action="卸载磁带", payload={})
        self._record_http("POST", "/api/tape/unmount", data, err)
        if err:
            return False, err
        assert data is not None
        if not data.get("success"):
            return False, _response_message(data, "卸载失败")
        return True, _response_message(data, "磁带卸载成功")

    def eject(self) -> tuple[bool, str]:
        data, err = self._call("POST", "/api/tape/eject", action="弹出磁带", payload={})
        self._record_http("POST", "/api/tape/eject", data, err)
        if err:
            return False, err
        assert data is not None
        if not data.get("success"):
            return False, _response_message(data, "弹出失败")
        return True, _response_message(data, "磁带弹出成功")

    # --- 设备信息（v1: /api/tape/device-info、/api/tape/device-list） -----
    def device_info(self) -> dict:
        data, err = self._call(
            "GET", "/api/tape/device-info", action="读取设备信息", timeout=self.status_timeout
        )
        self._record_http("GET", "/api/tape/device-info", data, err)
        info: dict = {
            "model": None,
            "serial_number": None,
            "firmware_version": None,
            "raw": data,
            "error": None,
        }
        if err:
            info["error"] = err
            return info
        assert data is not None
        source = data.get("info") if isinstance(data.get("info"), dict) else data
        info.update(
            {
                "model": _clean_info_value(source.get("model")),
                "serial_number": _clean_info_value(
                    source.get("serial_number") or source.get("serial")
                ),
                "firmware_version": _clean_info_value(
                    source.get("firmware_version") or source.get("firmware")
                ),
            }
        )
        if not data.get("success", True):
            info["error"] = _response_message(data, "读取设备信息失败")
        return info

    def device_list(self) -> list[dict]:
        data, err = self._call(
            "GET", "/api/tape/device-list", action="枚举设备", timeout=self.status_timeout
        )
        self._record_http("GET", "/api/tape/device-list", data, err)
        if err or data is None:
            return []
        raw_devices = data.get("devices")
        if not isinstance(raw_devices, list):
            return []
        devices: list[dict] = []
        for item in raw_devices:
            if not isinstance(item, dict):
                continue
            path = item.get("path") or item.get("device_path")
            if not path:
                continue
            devices.append(
                {
                    "path": str(path),
                    "model": _clean_info_value(item.get("model")),
                    "serial_number": _clean_info_value(item.get("serial_number")),
                    "firmware_version": _clean_info_value(item.get("firmware_version")),
                    "raw": item,
                }
            )
        return devices

    def volume_label(self) -> str | None:
        """v1 挂载到共享挂载点后，本服务直接读同一份 ``.volume_label``。"""
        return read_volume_label(self.mount_point)


# ---------------------------------------------------------------------------
# 直连实现（合并后默认）：本服务自己跑 ltfs 命令
# ---------------------------------------------------------------------------
#: 条码允许的字符：字母/数字/连字符。真实 LTO 条码常见无连字符（``FB1186``），但
#: 用户自己贴标签可能写成 ``LTO6-0001``，所以不能把 ``-`` 截断（否则 ``LTO6-TEST``
#: 会解析成 ``LTO6``，防串盘会误杀）。
_BARCODE_PATTERNS = (
    # v1 实测格式: "1c LTFS17227I Tape attribute: Barcode = FB1186"
    re.compile(r"Tape attribute:\s*Barcode\s*=\s*([-A-Za-z0-9]+)", re.IGNORECASE),
    re.compile(r"\bbarcode\s*[:=]\s*([-A-Za-z0-9]+)", re.IGNORECASE),
    re.compile(r"volume\s*(?:label)?\s*[:=]\s*([-A-Za-z0-9]+)", re.IGNORECASE),
)

# 与 Config.SKIP_DIRS 对齐（这里不 import Config 常量，避免 scanner 侧改配置时行为漂移）
_SKIP_DIRS = {
    "._manifest",
    ".@__thumb",
    "@eaDir",
    "#recycle",
    ".Trash",
    "System Volume Information",
}


def _coerce_timeout(value: float | int | str | None, default: float = 300.0) -> float:
    try:
        timeout = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return timeout if timeout > 0 else default


def parse_barcode_from_text(text: str | None) -> str | None:
    """从 ltfs 命令输出里解析条码（对齐 v1 的三种格式，最后兜底校验）。

    条码允许连字符（``LTO6-0001``）；匹配到的首尾 ``-`` 会去掉（``AB-123-`` → ``AB-123``）。
    """
    if not text:
        return None
    for pattern in _BARCODE_PATTERNS:
        match = pattern.search(text)
        if match:
            found = normalize_barcode(match.group(1))
            if found:
                found = found.strip("-")
            if found and barcode_is_usable(found):
                return found
    return None


def read_volume_label(mount_point: str | os.PathLike) -> str | None:
    """从 LVFS 根目录的 ``.volume_label`` 读条码（v1 的兜底来源之一）。"""
    label_path = os.path.join(str(mount_point), ".volume_label")
    try:
        with open(label_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read(4096)
    except OSError:
        return None
    for line in content.splitlines():
        found = normalize_barcode(line)
        if found:
            return found
    return None


class DirectAdapter(TapeAdapter):
    """合并 v1 后的生产实现：直接跑 ``ltfs`` / ``umount`` 命令。

    重点：命令超时 **kill 子进程并回收**（v1 D6 的坑：只 join 线程不 kill，
    超时后 ltfs 还在后台挂载 → 双挂载/设备忙）。
    """

    def __init__(
        self,
        device: str | None = None,
        mount_point: str | os.PathLike | None = None,
        timeout: float | int | str | None = None,
        *,
        app=None,
        ltfs_bin: str = "ltfs",
        umount_bin: str = "umount",
        info_timeout: float | int | str | None = None,
    ):
        super().__init__(app=app, mount_point=mount_point)
        self.device = str(device or Config.LTFS_DEVICE)
        self.timeout = _coerce_timeout(timeout if timeout is not None else Config.LTFS_TIMEOUT)
        # 设备信息/设备列表是"锦上添花"的查询：真机上可能卡住，给短超时，
        # 绝不能让它占着驱动器 5 分钟（页面状态只走 status()，不查详情）。
        default_info = getattr(Config, "LTFS_INFO_TIMEOUT", 15)
        self.info_timeout = _coerce_timeout(
            info_timeout if info_timeout is not None else default_info
        )
        self.ltfs_bin = ltfs_bin
        self.umount_bin = umount_bin

    # --- 内部：安全的子进程执行 ----------------------------------------
    def _exec(self, argv: list[str], timeout: float | None = None) -> tuple[int, str, str, bool]:
        """跑命令，返回 ``(returncode, stdout, stderr, timed_out)``；不抛异常。

        退出码约定：127=命令不存在，126=无法执行，124=超时（与 GNU timeout 一致）。
        """
        logger.debug("执行命令: %s", argv)
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
            )
        except FileNotFoundError:
            return 127, "", f"命令不存在: {argv[0]}", False
        except OSError as exc:
            return 126, "", f"无法执行 {argv[0]}: {exc}", False

        effective_timeout = self.timeout if timeout is None else timeout
        try:
            out, err = proc.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            # D6：必须 kill 并 wait，否则幽灵 ltfs 会继续挂载
            proc.kill()
            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - 极端情况
                out, err = "", ""
            message = f"命令超时（>{effective_timeout:g}s），已终止: {argv[0]}"
            merged = ((err or "") + "\n" + message).strip()
            return 124, out or "", merged, True
        except Exception as exc:  # pragma: no cover - 防御性兜底
            proc.kill()
            return 1, "", f"命令执行异常: {exc}", False
        return proc.returncode or 0, out or "", err or "", False

    # --- 对外接口 -------------------------------------------------------
    def status(self) -> TapeStatus:
        raw: dict = {
            "adapter": "direct",
            "device": self.device,
            "mount_point": self.mount_point,
            "ltfs_bin": self.ltfs_bin,
        }
        try:
            device_exists = os.path.exists(self.device)
        except OSError:  # pragma: no cover
            device_exists = False
        try:
            mounted = os.path.ismount(self.mount_point)
        except OSError:  # pragma: no cover
            mounted = False
        barcode = read_volume_label(self.mount_point) if mounted else None
        return TapeStatus(
            mounted=mounted,
            barcode=barcode,
            device_exists=device_exists,
            device_path=self.device,
            raw=raw,
        )

    def mount(self) -> tuple[bool, str, str | None]:
        mount_point = str(self.mount_point)
        try:
            os.makedirs(mount_point, exist_ok=True)
        except OSError as exc:
            return False, f"无法创建挂载点 {mount_point}: {exc}", None

        argv = [self.ltfs_bin, "-o", f"devname={self.device}", mount_point]
        rc, out, err, timed_out = self._exec(argv)
        self._record_output(command=" ".join(argv), stdout=out, stderr=err)
        if timed_out:
            return False, f"挂载超时：{_first_line(err)}", None
        if rc != 0:
            return False, f"挂载失败：{_first_line(err) or _first_line(out)}", None

        # 命令成功不代表真挂上了（v1 曾出现假 success），复查 ismount 防双挂载
        try:
            really_mounted = os.path.ismount(mount_point)
        except OSError:  # pragma: no cover
            really_mounted = False
        if not really_mounted:
            return False, f"挂载命令返回成功但挂载点未挂载: {mount_point}", None

        barcode = (
            parse_barcode_from_text((out or "") + "\n" + (err or ""))
            or read_volume_label(mount_point)
            or UNKNOWN_BARCODE
        )
        return True, f"磁带挂载成功（条码 {barcode}）", barcode

    def unmount(self) -> tuple[bool, str]:
        argv = [self.umount_bin, str(self.mount_point)]
        rc, out, err, timed_out = self._exec(argv)
        self._record_output(command=" ".join(argv), stdout=out, stderr=err)
        if timed_out:
            return False, f"卸载超时：{_first_line(err)}"
        if rc != 0:
            return False, f"卸载失败：{_first_line(err) or _first_line(out)}"
        return True, "磁带卸载成功"

    def eject(self) -> tuple[bool, str]:
        # 与 v1 一致：release_device 不带挂载点（要求先 unmount）
        argv = [self.ltfs_bin, "-o", f"devname={self.device}", "-o", "release_device"]
        rc, out, err, timed_out = self._exec(argv)
        self._record_output(command=" ".join(argv), stdout=out, stderr=err)
        if timed_out:
            return False, f"弹出超时：{_first_line(err)}"
        if rc != 0:
            return False, f"弹出失败：{_first_line(err) or _first_line(out)}"
        return True, "磁带弹出成功"

    # --- 设备信息（ltfs -o device_list / -o device_info） ---------------
    def device_list(self) -> list[dict]:
        argv = [self.ltfs_bin, "-o", "device_list"]
        rc, out, err, timed_out = self._exec(argv, timeout=self.info_timeout)
        self._record_output(command=" ".join(argv), stdout=out, stderr=err)
        if timed_out or rc != 0:
            logger.warning("ltfs device_list 失败（rc=%s）: %s", rc, _first_line(err))
            return []
        return parse_device_list_text((out or "") + "\n" + (err or ""))

    def device_info(self) -> dict:
        argv = [self.ltfs_bin, "-o", f"devname={self.device}", "-o", "device_info"]
        rc, out, err, timed_out = self._exec(argv, timeout=self.info_timeout)
        self._record_output(command=" ".join(argv), stdout=out, stderr=err)
        info = parse_device_info_text((out or "") + "\n" + (err or ""))
        info["raw"] = out or ""
        if timed_out:
            info["error"] = f"读取设备信息超时：{_first_line(err)}"
        elif rc != 0:
            info["error"] = f"读取设备信息失败：{_first_line(err) or _first_line(out)}"
        else:
            info["error"] = None
        return info

    def volume_label(self) -> str | None:
        return read_volume_label(self.mount_point)


def _first_line(text: str | None) -> str:
    """错误信息取第一行（避免把整坨 stderr 塞进 UI）。"""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:500]
    return ""


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def _cfg_get(cfg: Any, key: str, default: Any = None) -> Any:
    """cfg 可以是 dict / Flask config / Config 类 / 任意带属性的对象。"""
    if cfg is None:
        return getattr(Config, key, default)
    if hasattr(cfg, "get"):
        try:
            return cfg.get(key, default)  # type: ignore[union-attr]
        except TypeError:  # pragma: no cover - 少见：get 签名不兼容
            pass
    return getattr(cfg, key, default)


def get_adapter(cfg: Any = None, *, app=None) -> TapeAdapter:
    """按配置建适配器：``TAPE_ADAPTER=direct|http``（合并 v1 后默认 direct）。

    合并后设备由本服务独占，``direct`` 是生产默认；``http`` 只留给"v1 还在跑、
    由它独占驱动器"的过渡场景。``app`` 只用于 ``scan()`` 调 scanner；
    纯挂载/弹出场景可省略。可选配置 ``LTFS_BIN``/``UMOUNT_BIN``
    覆盖命令路径（打包/测试注入用）。
    """
    default_kind = getattr(Config, "TAPE_ADAPTER", "direct")
    kind = str(_cfg_get(cfg, "TAPE_ADAPTER", default_kind) or default_kind).strip().lower()
    timeout = _cfg_get(cfg, "LTFS_TIMEOUT", Config.LTFS_TIMEOUT)
    mount_point = _cfg_get(cfg, "LTFS_MOUNT", Config.LTFS_MOUNT)
    if kind in ("http", "ltfs-webui", "webui"):
        return HttpAdapter(
            base_url=str(_cfg_get(cfg, "LTFS_API_BASE", Config.LTFS_API_BASE) or ""),
            timeout=timeout,
            app=app,
            mount_point=mount_point,
        )
    if kind in ("direct", "ltfs", "local"):
        return DirectAdapter(
            device=str(_cfg_get(cfg, "LTFS_DEVICE", Config.LTFS_DEVICE) or Config.LTFS_DEVICE),
            mount_point=mount_point,
            timeout=timeout,
            app=app,
            ltfs_bin=str(_cfg_get(cfg, "LTFS_BIN", "ltfs") or "ltfs"),
            umount_bin=str(_cfg_get(cfg, "UMOUNT_BIN", "umount") or "umount"),
        )
    raise ValueError(f"未知的 TAPE_ADAPTER 配置: {kind!r}（支持 http/direct）")
