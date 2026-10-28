# v2 服务模块接口契约（并行开发用）

> 所有模块都跑在 `av-archive` 这个独立服务里。**不要修改**：`app/models.py`、`app/db.py`、
> `app/config.py`、`app/routes/*`、`app/templates/*`、`app/__init__.py`、`Dockerfile`、
> `docker-compose.yml`（这些由 Lead 维护）。只写你 task 里 `write_scopes` 列的文件。

## 0. 运行环境

- 项目根：`av-archive/`，venv：`av-archive/.venv`（已装 flask/sqlalchemy/pytest/requests/bs4/lxml/Pillow）。
- 测试：`.venv/bin/python -m pytest tests/<你的测试>.py -q`
- 代理：按工作区 `AGENTS.md`（`127.0.0.1:7897`），产物/代码里不得写代理地址，用 `HTTPS_PROXY` 环境变量。
- 已有真实资产：
  - `data/r18dump.db`（r18 staging，1.74GB，15 表）
  - `/tmp/r18dump/latest.sql`（原始 dump）
  - `/tmp/av-archive-corpus/{nas,tape}/...` + `manifest.json`（合成语料，每套 300 视频 + sidecar）

## 1. 通用约定

- 用 `app` factory 拿配置：`create_app()` 里已建好表和目录；服务函数接收 `app` 参数（`app.app_context()` 由调用方负责）。
- 时间统一 `app.models.utcnow()`（naive UTC）。
- 错误不要抛到 UI：收集到返回值的 `errors: list[str]`，单文件/单条失败不中断整批。
- 所有写库操作要**幂等**（重复跑结果一致）。
- 进度回调统一签名：`progress(done: int, message: str = "") -> None`，可为 None。
- 日志用 `current_app.logger`（或 `logging.getLogger(__name__)`）。

## 2. `app/services/scanner.py`（scanner-dev）

```python
@dataclass
class ScanStats:
    volume_id: int
    kind: str                 # 'nas' | 'tape'
    root: str
    files_seen: int
    videos: int
    sidecars: int
    new: int
    updated: int
    unchanged: int
    deleted: int              # 本次标记 deleted_at 的旧行
    with_code: int
    low_confidence: int       # 0 < confidence < CODE_AUTO_MIN
    no_code: int
    chinese_sub_files: int
    elapsed_s: float
    errors: list[str]

def scan_nas(app, root_path: str, *, volume_name: str | None = None,
             progress=None) -> ScanStats: ...

def scan_tape(app, mount_path: str, *, barcode: str,
              expected_barcode: str | None = None,
              progress=None) -> ScanStats: ...
```

规则：
1. `LibraryVolume`：`kind='nas'` 用 `str(Path(root_path).resolve())` 作 `ref`；`kind='tape'` 用 `barcode`。不存在就建，存在就复用。更新 `last_scan_at/status/used_bytes`。
2. 遍历：`os.walk`，跳过 `Config.SKIP_DIRS`、不跟符号链接；单文件 `os.stat` 失败只记 errors。
3. 分类：`Config.VIDEO_EXTS` 为视频；`Config.SUBTITLE_EXTS` 为字幕 sidecar；`.nfo` 也是 sidecar。`is_sidecar=True` 的字幕文件也要入库（用于关联字幕），但不计入 `videos`。
4. 番号：`extract_code(rel_path)`（`app.services.code_parser`）。写入 `code_guess`（None 允许）、`confidence`、`flags=",".join(sorted(flags))`。`low_confidence` 按 `0 < conf < Config.CODE_AUTO_MIN` 统计。
5. `rel_path` 用相对 root 的 POSIX 路径（正斜杠）。
6. 增量：`(volume_id, rel_path)` 已存在 → size/mtime 未变则 `unchanged`（只更新 `seen_at`），否则 `updated`；不存在则 `new`。
7. 本轮没见到的旧行 → `deleted_at=utcnow()`（软删）；再次出现则清空 `deleted_at`。
8. `scan_tape`：若 `expected_barcode` 给了且 `barcode != expected_barcode` → 直接抛 `ValueError`；`barcode` 为 `UNKNOWN`/空 → 抛 `ValueError`（防串盘）。
9. 事务：每 500~1000 行 commit 一次；不要一个文件一次 commit。
10. 必须能跑通合成语料，并用 `manifest.json` 断言：可识别文件 `with_code` 数量、30 条垃圾名为 `no_code`/低置信度。

## 3. `app/services/materialize.py`（materialize-dev）

```python
@dataclass
class MaterializeStats:
    codes_considered: int
    matched: int
    titles_created: int
    titles_updated: int
    files_linked: int
    no_dump_match: int
    low_confidence_skipped: int
    manual_locked_skipped: int
    errors: list[str]

def materialize(app, *, dump_db_path: str | None = None,
                min_confidence: int | None = None, limit: int | None = None,
                codes: Iterable[str] | None = None,
                refresh: bool = False, progress=None) -> MaterializeStats: ...
```

规则：
1. 输入：`LibraryFile`（`deleted_at IS NULL`, `is_sidecar=False`）中 `code_guess` 非空、`confidence >= CODE_AUTO_MIN`、且（`title_id IS NULL` 或 `refresh=True`）的番号去重。
2. 用 `app.services.r18.lookup_by_dvd_ids(conn, codes)` 查 staging；未命中计入 `no_dump_match`（把番号收集起来，便于报告）。
3. 同番号多版本：优先 `site_id=2`（mono）+ `title_ja` 非空；合并时以日文标题为准。
4. 建/更新 `Title`：`dvd_id`（归一化）、`content_id`、`title_ja/en`、`release_date`、`runtime_mins`、`maker_id/label_id/series_id/director_id`（staging 里对应表，`r18_id` 存 staging id，`name_ja/en` 落库；已存在按 `r18_id` 复用）、`cover_url`。
5. 关联：`derived_video_actress`→`Actress`（role=female），`derived_video_actor`→`Actress`（role=male）；`derived_video_category`→`Genre`（`genre_en` 有，`genre_ja` 可能有）；`derived_video_director`→`Director`。
6. 图片：`cover_url` 存 `https://pics.dmm.co.jp/<jacket_full_url>.jpg`；剧照按 `gallery_full_first/last` 生成 `Asset(kind='still', idx=N)`（数量用 `r18.gallery_image_count`，上限 20）。不要真的下载图片（后续资产任务负责）。
7. `has_chinese_sub`：该番号任一文件 `flags` 含 `chinese_sub` → True。
8. `title_zh`：dump 无中文，这里留空；提供可选 `translator: Callable[[str], str | None] = None` 参数（默认 None，不调用）。
9. 更新 `library_file.title_id`（含该番号所有文件，包括 `is_sidecar=True` 的字幕文件）。
10. `manual_lock=True` 的 Title 不覆盖字段（只补空）。
11. 用真实 `data/r18dump.db` 跑：用 `scripts/dev_seed` 造的 16 部里，SSIS-001/IPX-001/ABP-123/STARS-001 等应命中；FC2-PPV-1234567 应进 `no_dump_match`。

## 4. `app/services/tape_adapter.py` + `app/services/audit.py`（tape-dev）

```python
@dataclass
class TapeStatus:
    mounted: bool
    barcode: str | None
    device_exists: bool
    device_path: str | None
    raw: dict

class TapeAdapter:
    def status(self) -> TapeStatus: ...
    def mount(self) -> tuple[bool, str, str | None]:      # (ok, message, barcode)
    def unmount(self) -> tuple[bool, str]: ...
    def eject(self) -> tuple[bool, str]: ...
    def scan(self, mount_path: str, barcode: str) -> "ScanStats": ...   # 调 scanner.scan_tape

class HttpAdapter(TapeAdapter):    # 走 ltfs-webui API（默认）
class DirectAdapter(TapeAdapter):  # 直接调 ltfs 命令
def get_adapter(cfg) -> TapeAdapter: ...
```

HttpAdapter 端点（v1 实测）：
- `POST {base}/api/tape/mount`（**无参数**，挂载插入的磁带；返回 `{success, message, tape:{barcode,status}}`）
- `POST {base}/api/tape/unmount`、`POST {base}/api/tape/eject`（`{success,message}`）
- `GET {base}/api/tape/current`（`{success, tape:{barcode,status,...}}`，无磁带时 404）
- `GET {base}/api/tape/device-status`（`{success, device_exists, device_path}`）
- mount 是"挂载当前插入的盘"，**不能指定条码**；`mount()` 返回实际条码，audit 负责比对 expected。

`audit.py`：
```python
def run_audit(app, barcode: str, *, expected_barcode: str | None = None,
              adapter: TapeAdapter | None = None, write_manifest: bool = True,
              progress=None) -> dict: ...
def write_manifest(mount_path: str, barcode: str, scan: ScanStats, extra: dict | None = None) -> str: ...
def read_manifest(mount_path: str) -> dict | None: ...
```
- 流程写 `AuditRun`（挂载前创建，状态 pending→mounting→scanning→written_manifest→done；异常 failed + error）；结束 eject。
- manifest 写 `<mount>/._manifest/<barcode>.json`，格式 `{schema:1, barcode, generated_at, stats:{...}, files:[{rel_path,size,mtime,code,confidence,flags}]}`；`read_manifest` 能解析旧格式并返回 None 而不是抛错。
- 可中断：中途抛异常不能留下"看起来完成"的 run。
- 测试：用本地 `http.server` 假 v1 API + `tmp_path` 假挂载目录，覆盖 mount→scan→manifest→eject 全流程、条码不符中止、UNKNOWN 中止。

## 5. `app/services/javdb.py` + `app/services/subtitle.py`（enrich-dev）

```python
@dataclass
class JavdbDetail:
    code: str
    url: str
    title: str | None
    genres_zh: list[str]
    rating: float | None
    votes: int | None
    has_chinese_sub: bool
    series: str | None
    maker: str | None
    release_date: str | None

def fetch(code: str, *, session=None, cookie: str | None = None, timeout: int = 15) -> JavdbDetail | None: ...
def fill_gaps(app, *, limit: int = 100, rate: float | None = None,
              only_codes: Iterable[str] | None = None, progress=None) -> dict: ...
```
- javdb 会轮换域名：从 `https://javdb.com` 起步，跟随重定向，校验页面 `data-domain`；成功域名写 `AppSetting(key='javdb_domain')`，下次优先用。
- 只补缺口：`rating IS NULL` 或该 Title 没有中文题材（Genre.genre_zh 为空）的条目。
- 速率默认 `Config.JAVDB_RATE_SECONDS`，单线程；403/429 立即熔断本轮并记录。
- 解析失败返 None 并写 `ScrapeTask(stage='javdb_gap', status='failed')`，不抛。
- 测试：用 `tests/fixtures/javdb_sample.html`（自己从真实页面存一份去掉敏感内容的）跑解析；fetch 用 mock session。

```python
@dataclass
class SubtitleCandidate:
    code: str
    source: str          # 'xunlei' | 'subtitlecat'
    title: str
    lang: str            # 'zh'
    score: int           # 0-100
    download_url: str
    referer: str | None

def search_xunlei(code: str, *, session=None) -> list[SubtitleCandidate]: ...
def search_subtitlecat(code: str, *, session=None) -> list[SubtitleCandidate]: ...
def sync_filename_flags(app, *, min_confidence: int | None = None) -> dict: ...
def fetch_best(app, title, *, session=None, dry_run: bool = False) -> "Subtitle | None": ...
```
- L0 `sync_filename_flags`：扫描 `LibraryFile.flags` 含 `chinese_sub` → `Title.has_chinese_sub=True`。
- L1 迅雷接口：`https://api-shoulei-ssl.xunlei.com/oracle/subtitle?gcid=&cid=&name=<code>`（无 key，返回 JSON；研究记录抽样 2/7 命中且会误命中）。**必须**：番号正则校验（候选标题/文件名必须含该番号）+ 时长校验（±10%）才能入库；命中存 `Subtitle(source='xunlei', quality_flag='unknown')`，文件落 `Config.SUBTITLE_DIR`。
- subtitlecat 只作低质兜底，`quality_flag='machine'`。
- 网络不可用时返回空列表，不抛异常。

## 6. 联调与验收（Lead 负责）

- Lead 写 `app/services/jobs.py`（Job 队列 + worker 线程）、`app/routes/api.py`、UI 按钮、`scripts/worker.py`。
- 你不需要改路由；只要保证函数签名和行为符合本文档。
- 交付前自测：`pytest tests/ -q` 全绿（含别人的测试），不破坏现有 269 个用例。
