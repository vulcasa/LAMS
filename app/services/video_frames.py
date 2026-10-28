"""预留：用 ffmpeg 从视频抽帧，生成「视频截图」。

设计：
- 只对已关联到 Title 的视频文件抽帧；优先 NAS 上的文件，磁带需已挂载；
- 输出 ``media/screenshots/<番号>/NN.jpg``，写 ``Asset(kind='screenshot')``；
- ffmpeg / ffprobe 不存在或视频不可访问时返回 ok=False，不抛异常；
- 当前为预留功能，UI 只显示已生成的截图（kind='screenshot'）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from app.db import db
from app.models import Asset, LibraryFile, Title, utcnow

__all__ = [
    "ffmpeg_available", "find_video_file", "find_video_files",
    "titles_missing_screenshots", "extract_frames", "frame_count_setting",
    "frame_width_setting", "frame_timestamps",
]

_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".ts", ".flv", ".webm"}


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg"))


def _safe_stem(value: str) -> str:
    import re

    return re.sub(r"[^0-9A-Za-z._-]+", "_", value or "").strip("._") or "title"


def frame_width_setting(app, override: int | None = None) -> int:
    """截图输出宽度；0 = 保持源分辨率。"""
    if override is not None:
        return max(0, min(int(override), 3840))
    try:
        from app.services.settings import KEY_VIDEO_FRAME_WIDTH, get_int

        return max(0, min(get_int(KEY_VIDEO_FRAME_WIDTH, 0), 3840))
    except Exception:  # noqa: BLE001
        from app.config import Config

        return max(0, min(int(getattr(Config, "VIDEO_FRAME_WIDTH", 0) or 0), 3840))


def frame_count_setting(app, override: int | None = None) -> int:
    """当前设置的抽帧数量（设置页 video_frame_count，1–60）。"""
    from app.config import Config as _Config

    frame_max = int(getattr(_Config, "VIDEO_FRAME_COUNT_MAX", 60) or 60)
    if override is not None:
        return max(1, min(int(override), frame_max))
    try:
        from app.services.settings import KEY_VIDEO_FRAME_COUNT, get_int

        value = get_int(KEY_VIDEO_FRAME_COUNT, 0)
        if value > 0:
            return max(1, min(value, frame_max))
    except Exception:  # noqa: BLE001
        pass
    from app.config import Config

    return max(1, min(int(getattr(Config, "VIDEO_FRAME_COUNT", 24) or 24), frame_max))


def frame_timestamps(
    duration: float | None, count: int, *, fallback_duration: float = 7200.0
) -> list[float]:
    """按视频时长平均分布取 count 个时间点（首尾各留半格）。

    时长不可用时**不能**只取前几十秒（片头通常没有内容）；退化为按
    ``fallback_duration``（默认 2 小时）覆盖全片均匀取帧。
    """
    from app.config import Config as _Config

    count = max(1, min(int(count), int(getattr(_Config, "VIDEO_FRAME_COUNT_MAX", 60) or 60)))
    span = duration if (duration and duration > 0) else max(60.0, float(fallback_duration))
    return [max(0.0, span * (index + 0.5) / count) for index in range(count)]


def find_video_files(app, title: Title) -> list[Path]:
    """找该 Title 当前可访问的所有视频文件（按入库顺序；磁带需挂载）。"""
    from app.config import Config

    mount = Path(app.config.get("LTFS_MOUNT", Config.LTFS_MOUNT))
    rows = (
        LibraryFile.query.filter_by(title_id=title.id, deleted_at=None)
        .filter(LibraryFile.is_sidecar.is_(False))
        .order_by(LibraryFile.id)
        .all()
    )
    candidates: list[Path] = []
    for row in rows:
        suffix = Path(row.rel_path).suffix.lower()
        if suffix not in _VIDEO_EXTS:
            continue
        volume = row.volume
        if volume is None:
            continue
        if (volume.kind or "") == "tape":
            candidates.append(mount / row.rel_path)
        elif volume.ref:
            candidates.append(Path(volume.ref) / row.rel_path)
    found: list[Path] = []
    for path in candidates:
        try:
            if path.is_file():
                found.append(path)
        except OSError:
            continue
    return found


def find_video_file(app, title: Title) -> Path | None:
    videos = find_video_files(app, title)
    return videos[0] if videos else None


def titles_missing_screenshots(app, limit: int = 50) -> list[Title]:
    """需要生成视频截图的标题：有非磁带视频文件，且还没有 screenshot。"""
    from sqlalchemy import exists, or_

    from app.models import LibraryVolume

    video_filter = or_(
        *[LibraryFile.name.ilike(f"%{ext}") for ext in sorted(_VIDEO_EXTS)]
    )
    screenshot_exists = exists().where(
        Asset.title_id == Title.id, Asset.kind == "screenshot"
    )
    query = (
        db.session.query(Title)
        .join(LibraryFile, LibraryFile.title_id == Title.id)
        .join(LibraryVolume, LibraryVolume.id == LibraryFile.volume_id)
        .filter(
            LibraryVolume.kind != "tape",
            LibraryFile.deleted_at.is_(None),
            LibraryFile.is_sidecar.is_(False),
            video_filter,
            ~screenshot_exists,
        )
        .distinct()
        .order_by(Title.id)
    )
    return query.limit(max(1, min(int(limit), 500))).all()


def _parse_ffmpeg_duration(text: str) -> float | None:
    import re

    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text or "")
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return max(1.0, int(hours) * 3600 + int(minutes) * 60 + float(seconds))


def _duration(path: Path) -> float | None:
    """时长探测：ffprobe → ffmpeg -i 输出解析；失败返回 None。"""
    probe = shutil.which("ffprobe")
    if probe:
        try:
            result = subprocess.run(
                [probe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            return max(1.0, float(result.stdout.strip()))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-i", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            parsed = _parse_ffmpeg_duration(result.stderr or result.stdout or "")
            if parsed:
                return parsed
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def resolve_duration(app, path: Path) -> tuple[float, str]:
    """返回 (估算时长, 来源)。拿不到时长时按文件大小/假定码率估算，再退 2 小时。"""
    duration = _duration(path)
    if duration:
        return duration, "probe"
    try:
        size = path.stat().st_size
        mbps = float(app.config.get("VIDEO_ASSUMED_MBPS", 8) or 8)
        if size > 0 and mbps > 0:
            estimate = size * 8 / (mbps * 1_000_000)
            if estimate >= 60:
                return min(estimate, 4 * 3600), "size"
    except OSError:
        pass
    fallback = float(app.config.get("VIDEO_FALLBACK_DURATION", 7200) or 7200)
    return max(60.0, fallback), "fallback"


def extract_frames(
    app, title_id: int, *, count: int | None = None, width: int | None = None,
    allow_tape: bool = False, progress=None, on_total=None,
) -> dict:
    """对每个可访问视频文件各抽 count 帧，写 Asset(kind='screenshot')。"""
    stats = {"ok": False, "title_id": title_id, "frames": 0, "errors": []}
    title = db.session.get(Title, title_id)
    if title is None:
        stats["errors"].append("影片不存在")
        return stats
    if not ffmpeg_available():
        stats["errors"].append("ffmpeg 未安装")
        return stats
    videos = find_video_files(app, title)
    if not videos:
        stats["errors"].append("当前没有可访问的视频文件（磁带需先挂载）")
        return stats
    # 预留功能只在 NAS/本地文件上启用：LTO 上逐帧 seek 极慢，容易长时间占用磁带
    try:
        from app.config import Config

        mount = Path(app.config.get("LTFS_MOUNT", Config.LTFS_MOUNT)).resolve()
        videos = [
            video for video in videos
            if allow_tape
            or (Path(video).resolve() != mount and mount not in Path(video).resolve().parents)
        ]
    except OSError:
        pass
    if not videos:
        stats["errors"].append("磁带上的视频抽帧过慢，暂不启用；请先复制到 NAS 再扫描")
        return stats

    count = frame_count_setting(app, count)
    output_width = frame_width_setting(app, width)
    total_planned = count * len(videos)
    stats["count"] = count
    stats["files"] = len(videos)
    stats["frames_per_file"] = count
    stats["total_planned"] = total_planned
    stats["width"] = output_width
    if on_total is not None:
        try:
            on_total(total_planned)
        except Exception:  # noqa: BLE001
            pass

    out_dir = Path(app.config["MEDIA_DIR"]) / "screenshots" / _safe_stem(title.dvd_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    existing = {
        int(asset.idx)
        for asset in Asset.query.filter_by(title_id=title.id, kind="screenshot").all()
        if asset.idx is not None
    }
    idx = max(existing, default=0)
    processed = 0
    fallback = float(app.config.get("VIDEO_FALLBACK_DURATION", 7200) or 7200)
    for file_no, video in enumerate(videos, start=1):
        duration, duration_source = resolve_duration(app, video)
        timestamps = frame_timestamps(duration, count, fallback_duration=fallback)
        if file_no == 1:
            stats["duration_s"] = round(duration, 2)
            stats["duration_source"] = duration_source
            stats["interval_s"] = round(duration / count, 2)
        for position, timestamp in enumerate(timestamps, start=1):
            idx += 1
            processed += 1
            target = out_dir / f"{idx:02d}.jpg"
            command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-ss", f"{timestamp:.2f}", "-i", str(video),
                "-frames:v", "1",
            ]
            if output_width > 0:
                command += ["-vf", f"scale={output_width}:-2"]
            command += ["-q:v", "2", "-y", str(target)]
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=120)
            except (OSError, subprocess.SubprocessError) as exc:
                stats["errors"].append(f"{Path(video).name}: {type(exc).__name__}: {exc}")
                if progress is not None:
                    try:
                        progress(processed, f"文件 {file_no}/{len(videos)} · 截图 {position}/{count}")
                    except Exception:  # noqa: BLE001
                        pass
                continue
            if result.returncode != 0 or not target.is_file():
                stats["errors"].append(
                    f"{Path(video).name}: {(result.stderr or 'ffmpeg 失败').strip()[:300]}"
                )
                if progress is not None:
                    try:
                        progress(processed, f"文件 {file_no}/{len(videos)} · 截图 {position}/{count}")
                    except Exception:  # noqa: BLE001
                        pass
                continue
            rel = os.path.relpath(target, Path(app.config["MEDIA_DIR"])).replace(os.sep, "/")
            db.session.add(
                Asset(title_id=title.id, kind="screenshot", idx=idx, local_path=rel,
                      bytes=target.stat().st_size)
            )
            stats["frames"] += 1
            if progress is not None:
                try:
                    progress(
                        processed,
                        f"文件 {file_no}/{len(videos)} · 截图 {position}/{count}",
                    )
                except Exception:  # noqa: BLE001
                    pass
    if stats["frames"]:
        stats["ok"] = True
        db.session.commit()
    return stats
