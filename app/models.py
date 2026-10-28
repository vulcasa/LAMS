"""av-archive 数据模型。

设计要点（来自 research/existing-repo.md 的代码级结论）：
- 介质与文件分离：LibraryVolume + LibraryFile，UNIQUE(volume_id, rel_path)；
- 影片层只按番号归一化后建 Title，不依赖任何外部系统的内部 ID；
- r18 dump 的 191 万部不会进主库，主库只物化用户实际拥有的 ~2-4 万部；
- 双语字段 *_ja / *_zh，展示时 zh or ja 回退。
"""
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.db import db


def utcnow():
    """存 naive UTC，展示层再转本地时区。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------
# 介质层
# --------------------------------------------------------------------------
class LibraryVolume(db.Model):
    __tablename__ = "library_volume"

    id = Column(Integer, primary_key=True)
    kind = Column(String(8), nullable=False)          # tape | nas
    ref = Column(String(512), nullable=False)         # tape: barcode; nas: 根路径
    name = Column(String(255))
    capacity_bytes = Column(BigInteger)
    used_bytes = Column(BigInteger)
    enabled = Column(Boolean, default=True, nullable=False)
    # 磁带运行时状态（v1 的 Tape.status 语义，合并后由本服务维护）
    status = Column(String(16), default="unknown", nullable=False)  # mounted|unmounted|ejected|unknown
    device_path = Column(String(64))
    last_mounted_at = Column(DateTime)
    last_error = Column(Text)
    last_scan_at = Column(DateTime)
    last_scan_status = Column(String(32))
    created_at = Column(DateTime, default=utcnow, nullable=False)

    files = relationship("LibraryFile", back_populates="volume", lazy="dynamic")

    __table_args__ = (
        UniqueConstraint("kind", "ref", name="uq_volume_kind_ref"),
    )

    def __repr__(self):
        return f"<LibraryVolume {self.kind}:{self.ref}>"


class LibraryFile(db.Model):
    __tablename__ = "library_file"

    id = Column(Integer, primary_key=True)
    volume_id = Column(Integer, ForeignKey("library_volume.id"), nullable=False)
    rel_path = Column(Text, nullable=False)
    name = Column(String(512), nullable=False)
    size = Column(BigInteger)
    mtime = Column(DateTime)
    code_guess = Column(String(64))
    confidence = Column(Integer, default=0)
    # 人工在「待确认」里确认过番号；物化时即使 dump 未命中也可建占位 Title
    manual_code = Column(Boolean, default=False, nullable=False)
    flags = Column(String(64))            # 逗号分隔：chinese_sub,uncensored,hi_res,part
    is_sidecar = Column(Boolean, default=False, nullable=False)
    title_id = Column(Integer, ForeignKey("title.id"))
    sha256 = Column(String(64))
    seen_at = Column(DateTime, default=utcnow, nullable=False)
    deleted_at = Column(DateTime)

    volume = relationship("LibraryVolume", back_populates="files")
    title = relationship("Title", back_populates="files")

    __table_args__ = (
        UniqueConstraint("volume_id", "rel_path", name="uq_file_volume_relpath"),
        Index("ix_file_rel_path", "rel_path"),
        Index("ix_file_code", "code_guess"),
        Index("ix_file_title", "title_id"),
        Index("ix_file_volume", "volume_id"),
    )


class AuditRun(db.Model):
    __tablename__ = "audit_run"

    id = Column(Integer, primary_key=True)
    volume_id = Column(Integer, ForeignKey("library_volume.id"))
    expected_barcode = Column(String(64))
    actual_barcode = Column(String(64))
    status = Column(String(24), nullable=False, default="pending")
    # pending | mounting | scanning | written_manifest | done | failed | aborted
    started_at = Column(DateTime)
    finished_at = Column(DateTime)
    files_seen = Column(Integer, default=0)
    files_new = Column(Integer, default=0)
    error = Column(Text)
    created_at = Column(DateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------
# 影片层
# --------------------------------------------------------------------------
class Title(db.Model):
    __tablename__ = "title"

    id = Column(Integer, primary_key=True)
    dvd_id = Column(String(64), unique=True, nullable=False, index=True)  # 归一化番号
    content_id = Column(String(64), index=True)                           # r18 content_id
    title_ja = Column(Text)
    title_zh = Column(Text)
    title_en = Column(Text)
    release_date = Column(Date, index=True)
    runtime_mins = Column(Integer)
    rating = Column(Float)
    rating_votes = Column(Integer)
    rating_source = Column(String(32))
    maker_id = Column(Integer, ForeignKey("maker.id"), index=True)
    label_id = Column(Integer, ForeignKey("label.id"), index=True)
    series_id = Column(Integer, ForeignKey("series.id"), index=True)
    director_id = Column(Integer, ForeignKey("director.id"), index=True)
    has_chinese_sub = Column(Boolean, default=False, nullable=False)
    is_favorite = Column(Boolean, default=False, nullable=False, index=True)
    cover_url = Column(Text)
    cover_local = Column(Text)
    work_url = Column(Text)
    source_dump_date = Column(String(16))
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    manual_lock = Column(Boolean, default=False, nullable=False)
    # JavDB 刮削未命中的后台标记（用户不可见）；冷却期过后才重新尝试
    javdb_miss_at = Column(DateTime)
    javdb_miss_count = Column(Integer, default=0)
    # 封面下载失败/占位图：自动轮询在冷却期内不再重复尝试（手动强制重下可绕过）
    cover_failed_at = Column(DateTime)
    cover_fail_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    files = relationship("LibraryFile", back_populates="title", lazy="dynamic")
    actresses = relationship("TitleActress", back_populates="title", lazy="dynamic")
    genres = relationship("TitleGenre", back_populates="title", lazy="dynamic")
    maker = relationship("Maker")
    label = relationship("Label")
    series = relationship("Series")
    director = relationship("Director")

    def display_title(self):
        return self.title_zh or self.title_ja or self.title_en or self.dvd_id


class Actress(db.Model):
    __tablename__ = "actress"

    id = Column(Integer, primary_key=True)
    r18_id = Column(Integer, index=True)
    name_kanji = Column(String(128), index=True)
    name_kana = Column(String(128))
    name_romaji = Column(String(128), index=True)
    aliases = Column(Text)          # JSON array
    image_url = Column(Text)
    local_avatar = Column(Text)

    def display_name(self):
        return self.name_kanji or self.name_kana or self.name_romaji or f"#{self.id}"


class TitleActress(db.Model):
    __tablename__ = "title_actress"

    title_id = Column(Integer, ForeignKey("title.id"), primary_key=True)
    actress_id = Column(Integer, ForeignKey("actress.id"), primary_key=True)
    role = Column(String(16), default="female", nullable=False)  # female | male

    title = relationship("Title", back_populates="actresses")
    actress = relationship("Actress")

    __table_args__ = (Index("ix_title_actress_actress", "actress_id"),)


class Director(db.Model):
    __tablename__ = "director"

    id = Column(Integer, primary_key=True)
    r18_id = Column(Integer, index=True)
    name_kanji = Column(String(128), index=True)
    name_kana = Column(String(128))
    name_romaji = Column(String(128))

    def display_name(self):
        return self.name_kanji or self.name_kana or self.name_romaji or f"#{self.id}"


class Maker(db.Model):
    __tablename__ = "maker"

    id = Column(Integer, primary_key=True)
    r18_id = Column(Integer, index=True)
    name_ja = Column(String(128))
    name_en = Column(String(128))

    def display_name(self):
        return self.name_ja or self.name_en or f"#{self.id}"


class Label(db.Model):
    __tablename__ = "label"

    id = Column(Integer, primary_key=True)
    r18_id = Column(Integer, index=True)
    name_ja = Column(String(128))
    name_en = Column(String(128))

    def display_name(self):
        return self.name_ja or self.name_en or f"#{self.id}"


class Series(db.Model):
    __tablename__ = "series"

    id = Column(Integer, primary_key=True)
    r18_id = Column(Integer, index=True)
    name_ja = Column(String(255))
    name_en = Column(String(255))

    def display_name(self):
        return self.name_ja or self.name_en or f"#{self.id}"


class Genre(db.Model):
    __tablename__ = "genre"

    id = Column(Integer, primary_key=True)
    r18_id = Column(Integer, index=True)
    genre_ja = Column(String(128), index=True)
    genre_zh = Column(String(128), index=True)
    genre_en = Column(String(128))

    def display_name(self):
        return self.genre_zh or self.genre_ja or self.genre_en or f"#{self.id}"


class TitleGenre(db.Model):
    __tablename__ = "title_genre"

    title_id = Column(Integer, ForeignKey("title.id"), primary_key=True)
    genre_id = Column(Integer, ForeignKey("genre.id"), primary_key=True)
    source = Column(String(16), default="r18")

    title = relationship("Title", back_populates="genres")
    genre = relationship("Genre")

    __table_args__ = (Index("ix_title_genre_genre", "genre_id"),)


class Asset(db.Model):
    __tablename__ = "asset"

    id = Column(Integer, primary_key=True)
    title_id = Column(Integer, ForeignKey("title.id"), index=True)
    kind = Column(String(16), nullable=False)   # cover | still | avatar
    idx = Column(Integer)                        # 剧照序号
    url = Column(Text)
    local_path = Column(Text)
    bytes = Column(BigInteger)
    failed_at = Column(DateTime)
    fail_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class Subtitle(db.Model):
    __tablename__ = "subtitle"

    id = Column(Integer, primary_key=True)
    title_id = Column(Integer, ForeignKey("title.id"), index=True)
    lang = Column(String(8), default="zh")
    format = Column(String(8))                  # srt | ass
    local_path = Column(Text)
    source = Column(String(32))                 # xunlei | subtitlecat | filename | manual
    source_url = Column(Text)
    quality_flag = Column(String(16))           # good | machine | unknown
    sha256 = Column(String(64))
    created_at = Column(DateTime, default=utcnow, nullable=False)


class ScrapeTask(db.Model):
    __tablename__ = "scrape_task"

    id = Column(Integer, primary_key=True)
    title_id = Column(Integer, ForeignKey("title.id"), index=True)
    stage = Column(String(24), nullable=False)   # dump | javdb_gap | asset | subtitle
    status = Column(String(16), nullable=False, default="pending")
    attempts = Column(Integer, default=0, nullable=False)
    error = Column(Text)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (Index("ix_scrape_stage_status", "stage", "status"),)


class CodeAlias(db.Model):
    """人工修正过的番号映射：原始字符串 -> 归一化番号。"""

    __tablename__ = "code_alias"

    id = Column(Integer, primary_key=True)
    raw_key = Column(String(512), unique=True, nullable=False)
    code = Column(String(64), nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class AppSetting(db.Model):
    __tablename__ = "app_setting"

    key = Column(String(64), primary_key=True)
    value = Column(Text)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class Job(db.Model):
    """后台任务队列（扫描/物化/抓取/盘点）。单 worker 原子领取，重启不丢。"""

    __tablename__ = "job"

    id = Column(Integer, primary_key=True)
    kind = Column(String(32), nullable=False)      # scan_nas|scan_tape|materialize|audit_tape|javdb_fill|subtitle_fetch
    payload = Column(Text)                          # JSON
    status = Column(String(16), nullable=False, default="pending")
    cancel_requested = Column(Boolean, default=False, nullable=False)
    progress = Column(Integer, default=0)
    total = Column(Integer, default=0)
    message = Column(Text)
    result = Column(Text)                           # JSON
    error = Column(Text)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)

    __table_args__ = (Index("ix_job_status_id", "status", "id"),)


class SmbOperation(db.Model):
    """SMB 挂载/卸载操作日志。"""

    __tablename__ = "smb_operation"

    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, ForeignKey("media_source.id"))
    action = Column(String(16), nullable=False)      # add|mount|unmount
    host = Column(String(255))
    share = Column(String(512))
    mount_path = Column(Text)
    status = Column(String(16), nullable=False)      # success|failed
    message = Column(Text)
    started_at = Column(DateTime, default=utcnow, nullable=False)
    finished_at = Column(DateTime)
    duration_ms = Column(Integer)


class TapeOperation(db.Model):
    """磁带操作日志（mount/unmount/eject/device_info/scan）。"""

    __tablename__ = "tape_operation"

    id = Column(Integer, primary_key=True)
    volume_id = Column(Integer, ForeignKey("library_volume.id"))
    barcode = Column(String(64))
    op_type = Column(String(32), nullable=False)
    status = Column(String(16), nullable=False)      # running|success|failed
    message = Column(Text)
    command = Column(Text)
    stdout = Column(Text)
    stderr = Column(Text)
    started_at = Column(DateTime, default=utcnow, nullable=False)
    finished_at = Column(DateTime)
    duration_ms = Column(Integer)

    __table_args__ = (Index("ix_tape_op_time", "started_at"),)


class Transfer(db.Model):
    """容器 <-> 磁带 文件传输任务（v1 的 FileTransfer 合并版）。"""

    __tablename__ = "transfer"

    id = Column(Integer, primary_key=True)
    direction = Column(String(24), nullable=False)   # container_to_tape | tape_to_container
    move = Column(Boolean, default=False, nullable=False)
    source_summary = Column(Text)
    target_dir = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, default="pending")  # pending|running|done|failed|cancelled
    file_count = Column(Integer, default=0)
    total_bytes = Column(BigInteger, default=0)
    done_bytes = Column(BigInteger, default=0)
    current_file = Column(Text)
    file_list = Column(Text)          # JSON
    error = Column(Text)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)

    __table_args__ = (Index("ix_transfer_status", "status", "id"),)


class TitleEditHistory(db.Model):
    """手动编辑历史（快照），支持一键回滚。"""

    __tablename__ = "title_edit_history"

    id = Column(Integer, primary_key=True)
    title_id = Column(Integer, ForeignKey("title.id"), index=True, nullable=False)
    field = Column(String(64))
    before_json = Column(Text)
    after_json = Column(Text)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class PlaybackHistory(db.Model):
    """最近播放的影片（按 Title 去重，最多保留查询时需要的数量）。"""

    __tablename__ = "playback_history"

    id = Column(Integer, primary_key=True)
    title_id = Column(Integer, ForeignKey("title.id"), unique=True, nullable=False, index=True)
    last_played_at = Column(DateTime, default=utcnow, nullable=False)
    play_count = Column(Integer, default=1, nullable=False)


class TapeManifest(db.Model):
    """盘点 manifest 的数据库副本（文件副本在 Config.MANIFEST_DIR）。"""

    __tablename__ = "tape_manifest"

    id = Column(Integer, primary_key=True)
    barcode = Column(String(64), unique=True, nullable=False)
    volume_id = Column(Integer, ForeignKey("library_volume.id"))
    generated_at = Column(String(32))
    file_count = Column(Integer, default=0)
    payload = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class MediaSource(db.Model):
    """用户添加的媒体库（挂载点）。

    目前只支持 NAS 路径；磁带卷由挂载/盘点自动登记。一个挂载点一条记录，
    扫描后对应的 LibraryVolume(kind='nas', ref=path) 会被创建/复用。
    """

    __tablename__ = "media_source"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)
    kind = Column(String(16), nullable=False, default="nas")
    path = Column(Text, nullable=False, unique=True)
    enabled = Column(Boolean, default=True, nullable=False)
    auto_scan = Column(Boolean, default=True, nullable=False)
    origin = Column(String(16), default="manual")  # manual | env | smb
    # SMB 运行时挂载参数（origin=smb 时使用；密码只存本地库，不通过 API 返回）
    smb_host = Column(String(255))
    smb_share = Column(String(512))
    smb_username = Column(String(255))
    smb_password = Column(Text)
    smb_options = Column(Text)
    smb_mounted_at = Column(DateTime)
    last_scan_at = Column(DateTime)
    last_scan_status = Column(String(32))
    last_error = Column(Text)
    last_job_id = Column(Integer)
    created_at = Column(DateTime, default=utcnow, nullable=False)
