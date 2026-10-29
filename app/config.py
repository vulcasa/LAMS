"""av-archive 配置。

所有路径/开关都从环境变量读，默认值面向本地开发；
部署到 Unraid 时通过 docker-compose 注入。不做隐式 .env 魔法（v1 的坑）。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes", "on")


class Config:
    # --- 基础 ---
    SECRET_KEY = os.environ.get("AVLIB_SECRET_KEY", "dev-only-change-me")
    # 写操作 CSRF 校验（双提交 Cookie）；测试/自动化可设 AVLIB_CSRF=0 关闭
    CSRF_ENABLED = _bool("AVLIB_CSRF", True)
    DATA_DIR = Path(os.environ.get("AVLIB_DATA_DIR", str(BASE_DIR / "data")))
    DB_PATH = Path(os.environ.get("AVLIB_DB_PATH", str(DATA_DIR / "avlib.db")))
    MEDIA_DIR = Path(os.environ.get("AVLIB_MEDIA_DIR", str(DATA_DIR / "media")))
    COVER_DIR = MEDIA_DIR / "covers"
    STILL_DIR = MEDIA_DIR / "stills"
    AVATAR_DIR = MEDIA_DIR / "avatars"
    SUBTITLE_DIR = MEDIA_DIR / "subtitles"
    THUMB_DIR = MEDIA_DIR / "thumbs"
    DUMP_DIR = Path(os.environ.get("AVLIB_DUMP_DIR", str(DATA_DIR / "dump")))
    R18_DUMP_DB = Path(os.environ.get("AVLIB_R18_DB", str(DATA_DIR / "r18dump.db")))
    # 盘点 manifest 统一存本地（不写磁带、不写媒体库）；数据库里也存一份
    MANIFEST_DIR = Path(os.environ.get("AVLIB_MANIFEST_DIR", str(DATA_DIR / "manifests")))
    # 自动物化阈值：>= auto 自动关联，>= review 自动但待抽检，< review 待人工
    CODE_AUTO_MIN = int(os.environ.get("AVLIB_CODE_AUTO_MIN", "90"))
    CODE_REVIEW_MIN = int(os.environ.get("AVLIB_CODE_REVIEW_MIN", "70"))
    # 后台 worker（应用内线程；生产也可用 scripts/worker.py）
    WORKER_ENABLED = _bool("AVLIB_WORKER", True)
    WORKER_POLL_SECONDS = float(os.environ.get("AVLIB_WORKER_POLL", "1.5"))

    # --- 文件源 ---
    # 冒号分隔的 NAS 根目录列表，容器内路径
    NAS_ROOTS = [p for p in os.environ.get("AVLIB_NAS_ROOTS", "").split(":") if p]
    # 视频扩展名
    VIDEO_EXTS = {
        ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".ts", ".m2ts",
        ".rmvb", ".rm", ".flv", ".webm", ".iso", ".m4v", ".mpg", ".mpeg",
        ".vob", ".divx", ".asf", ".f4v",
    }
    SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub", ".vtt"}
    # 扫描时忽略的目录名
    SKIP_DIRS = {".@__thumb", "@eaDir", "#recycle", ".Trash", "System Volume Information"}

    # --- 磁带适配器 ---
    # http: 调 ltfs-webui 的 API（默认，设备只归 v1）
    # direct: 直接调 ltfs 命令
    # 合并后默认直连设备（不再依赖 ltfs-webui）；过渡期仍可设 http 双跑
    TAPE_ADAPTER = os.environ.get("AVLIB_TAPE_ADAPTER", "direct")
    LTFS_API_BASE = os.environ.get("AVLIB_LTFS_API_BASE", "http://ltfs-webui:5001")
    LTFS_DEVICE = os.environ.get("LTFS_DEVICE", "/dev/sg10")
    LTFS_MOUNT = Path(os.environ.get("LTFS_MOUNT", "/media/tape"))
    LTFS_TIMEOUT = int(os.environ.get("LTFS_TIMEOUT", "300"))
    # 机柜/磁带库会挡门时置 0：盘点结束只卸载，不弹出；/tapes 的「弹出」也会被拒绝
    AUTO_EJECT = _bool("AVLIB_AUTO_EJECT", True)
    # 设备信息类查询（device_info/device_list）的短超时：真机上可能卡住
    LTFS_INFO_TIMEOUT = int(os.environ.get("AVLIB_LTFS_INFO_TIMEOUT", "15"))
    MKLTFS_BIN = os.environ.get("AVLIB_MKLTFS_BIN", "mkltfs")
    LTFSCK_BIN = os.environ.get("AVLIB_LTFSCK_BIN", "ltfsck")
    TAPE_INIT_TIMEOUT = int(os.environ.get("AVLIB_TAPE_INIT_TIMEOUT", "600"))
    TAPE_RECOVER_TIMEOUT = int(os.environ.get("AVLIB_TAPE_RECOVER_TIMEOUT", "1800"))
    # 只读打开 v1 的 SQLite（backfill 用），可选
    V1_DB_PATH = os.environ.get("AVLIB_V1_DB_PATH", "")
    # 文件浏览器允许访问的容器内根目录（逗号分隔）
    BROWSE_ROOTS = [p for p in os.environ.get("AVLIB_BROWSE_ROOTS", "/mnt,/data").split(",") if p]
    TRANSFER_CHUNK = int(os.environ.get("AVLIB_TRANSFER_CHUNK", str(8 * 1024 * 1024)))

    # --- 元数据 ---
    R18_DUMP_URL = os.environ.get("AVLIB_R18_DUMP_URL", "https://r18.dev/dumps/latest")
    HTTP_PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
    USER_AGENT = os.environ.get(
        "AVLIB_UA",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    )
    JAVDB_RATE_SECONDS = float(os.environ.get("AVLIB_JAVDB_RATE", "1.5"))
    JAVDB_COOKIE = os.environ.get("AVLIB_JAVDB_COOKIE", "")
    JAVDB_COOLDOWN_SECONDS = int(os.environ.get("AVLIB_JAVDB_COOLDOWN", "1800"))
    # JavDB 明确未命中的条目，多少天内不再重复抓（后台标记，UI 不可见）
    JAVDB_MISS_RETRY_DAYS = int(os.environ.get("AVLIB_JAVDB_MISS_RETRY_DAYS", "30"))
    # 自动轮询下载缺失封面/剧照：检查间隔（秒）与每批条数
    ASSET_AUTO_POLL_SECONDS = int(os.environ.get("AVLIB_ASSET_AUTO_POLL", "60"))
    ASSET_AUTO_BATCH = int(os.environ.get("AVLIB_ASSET_AUTO_BATCH", "15"))
    # 后台任务并行度（物化/扫描/封面可并行；磁带类任务仍互斥）
    WORKER_THREADS = int(os.environ.get("AVLIB_WORKER_THREADS", "3"))
    # 图片下载失败/占位图的冷却天数（自动轮询不再重复尝试；强制重下可绕过）
    ASSET_FAILED_RETRY_DAYS = int(os.environ.get("AVLIB_ASSET_FAILED_RETRY_DAYS", "30"))
    # 剧照落盘最小宽度（DMM/JavDB 原图只有 800px，下载时 Lanczos 放大到该宽度）
    STILL_TARGET_WIDTH = int(os.environ.get("AVLIB_STILL_WIDTH", "1920"))
    # 视频截图默认张数（设置页可改；按视频时长平均间隔）
    VIDEO_FRAME_COUNT = int(os.environ.get("AVLIB_VIDEO_FRAME_COUNT", "24"))
    VIDEO_FRAME_COUNT_MAX = int(os.environ.get("AVLIB_VIDEO_FRAME_MAX", "60"))
    # 0 = 保持源分辨率；>0 时按该宽度等比缩放
    VIDEO_FRAME_WIDTH = int(os.environ.get("AVLIB_VIDEO_FRAME_WIDTH", "0"))
    SOURCE_REFRESH_MINUTES = int(os.environ.get("AVLIB_SOURCE_REFRESH_MINUTES", "120"))
    # 拿不到时长时的兜底：按文件大小/假定码率估算；再不行按 2 小时假设均匀取帧
    VIDEO_ASSUMED_MBPS = float(os.environ.get("AVLIB_VIDEO_ASSUMED_MBPS", "8"))
    VIDEO_FALLBACK_DURATION = int(os.environ.get("AVLIB_VIDEO_FALLBACK_DURATION", "7200"))
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

    # --- 扫描/任务 ---
    SCAN_WORKERS = int(os.environ.get("AVLIB_SCAN_WORKERS", "1"))
    AUDIT_CHUNK = int(os.environ.get("AVLIB_AUDIT_CHUNK", "1000"))
