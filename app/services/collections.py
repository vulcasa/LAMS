"""文件夹合集（母带/无码流出等按目录整理好的内容）。

不建额外数据表：直接按 LibraryFile.rel_path 的顶层目录聚合，正好复用用户已经
手工分好的文件夹结构（如 ``蚊香社无码流出/蚊香社无码流出合集重编码/.../a.mkv``）。
只统计未挂 Title 的文件，避免把正常番号目录也列成合集。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable

from app.db import db
from app.models import LibraryFile, LibraryVolume

__all__ = ["Collection", "collection_of", "list_collections", "get_collection"]

_GENERIC_TOP = {
    "movies", "movie", "jav", "downloads", "download", "media", "video",
    "videos", "_未分类", "未分类", "new", "temp", "tmp",
}


@dataclass
class Collection:
    name: str
    files: int = 0
    total_bytes: int = 0
    volumes: list[str] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)


def collection_of(rel_path: str | None) -> str | None:
    """从相对路径取合集名（顶层目录；通用目录向下退一层）。"""
    parts = [part for part in (rel_path or "").replace("\\", "/").split("/") if part]
    if len(parts) < 2:
        return None
    index = 0
    if parts[0].startswith(".") or parts[0].lower() in _GENERIC_TOP:
        index = 1
    if index >= len(parts) - 1:
        return None
    return parts[index]


def _rows():
    return (
        db.session.query(LibraryFile, LibraryVolume)
        .outerjoin(LibraryVolume, LibraryVolume.id == LibraryFile.volume_id)
        .filter(
            LibraryFile.deleted_at.is_(None),
            LibraryFile.is_sidecar.is_(False),
            LibraryFile.title_id.is_(None),
        )
        .order_by(LibraryFile.id)
        .all()
    )


def list_collections(app=None, *, min_files: int = 2, limit: int = 200) -> list[Collection]:
    """列出未挂 Title 文件按顶层目录聚合的合集。"""
    grouped: dict[str, Collection] = {}
    for library_file, volume in _rows():
        name = collection_of(library_file.rel_path)
        if name is None:
            continue
        bucket = grouped.setdefault(name, Collection(name=name))
        bucket.files += 1
        bucket.total_bytes += int(library_file.size or 0)
        label = volume.name if volume is not None and volume.name else (
            volume.ref if volume is not None else "—"
        )
        if label not in bucket.volumes:
            bucket.volumes.append(label)
        if len(bucket.samples) < 3:
            bucket.samples.append(library_file.rel_path)
    items = [item for item in grouped.values() if item.files >= max(1, int(min_files))]
    items.sort(key=lambda item: (-item.files, item.name))
    return items[:limit]


def get_collection(app=None, name: str | None = None) -> tuple[Collection | None, list[dict]]:
    """返回 (合集信息, 文件行)；收藏夹不存在时返回 (None, [])。"""
    name = (name or "").strip().strip("/")
    if not name:
        return None, []
    info = Collection(name=name)
    rows: list[dict] = []
    for library_file, volume in _rows():
        if collection_of(library_file.rel_path) != name:
            continue
        info.files += 1
        info.total_bytes += int(library_file.size or 0)
        label = volume.name if volume is not None and volume.name else (
            volume.ref if volume is not None else "—"
        )
        if label not in info.volumes:
            info.volumes.append(label)
        if len(info.samples) < 3:
            info.samples.append(library_file.rel_path)
        rows.append(
            {
                "id": library_file.id,
                "rel_path": library_file.rel_path,
                "name": library_file.name,
                "size": library_file.size,
                "mtime": library_file.mtime,
                "code_guess": library_file.code_guess,
                "confidence": library_file.confidence,
                "volume": label,
            }
        )
    if info.files == 0:
        return None, []
    rows.sort(key=lambda item: item["rel_path"])
    return info, rows
