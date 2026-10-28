# AV 归档片库 v2 —— 设计方案（定稿 v0.3）

> 基线：`vulcasa/ltfs-management-webui` v1.0.1-beta（Flask + SQLite + LTFS）
> 部署：Docker 跑在 Unraid，磁带机手动换带，**纯归档不播放**
> **交付形态（已定）：独立服务**。与 ltfs-management-webui 分离部署，通过「磁带适配器」复用 v1（优先调它的 HTTP API，或直连 LTFS 命令 + 只读它的 SQLite），**不改 v1 任何代码**。
> 依据：`research/existing-repo.md`（代码级）、`research/metadata-sources.md`（实测）、`research/subtitles.md`（实测）

---

## 0. 三个决定性结论（先看这个）

**① 元数据不需要逐部爬 —— r18.dev 每周提供 CC0 全量 SQL dump（约 262MB）。**
`https://r18.dev/dumps/latest` 可直接程序化下载（实测 307 → S3）。整库导入本地 SQLite 后本地查询，字段覆盖日/英标题、女优（汉字+假名+罗马音+头像）、导演、maker/label、系列、日/英题材、发行日期、时长、封面大图、12 张剧照。**2–4 万部零抓取搞定底座**，只有缺中文题材/评分的条目才补 JavDB。
→ 原方案里"25–35 小时刮削"变成"每周导一次 dump + 补 10–30% 缺口"。

**② JavLibrary 放弃，JavDB 补中文，JavBus 只兜底。**
JavLibrary 全站 Cloudflare Managed Challenge（连 robots.txt 都 403），必须 FlareSolverr，性价比极低。JavDB 匿名 HTML 就 200，繁中题材 + 评分 + 中字标记是唯一稳定中文源。JavBus 有独家的女优三围/头像，但 `robots.txt` 是 `Disallow: /`，合规风险最高，只做最低频兜底。FC2 不在 r18 覆盖范围，需独立管道。

**③ 中文字幕是这套系统里唯一"没有好解"的部分（已定 L0+L1）。**
JAV 的中文字幕主流形态是**硬字幕重封装**，不存在"番号 → srt"的可靠索引。实测 11 个源，只有迅雷字幕库非公开接口（免 key、可下 srt，抽查 2/7 命中且会误命中）和 SubtitleCat（机翻链，质量差）勉强可用。全量 Whisper 生成单卡要 125–208 天，**不做**。
→ **L0 标记「有无中字版」+ L1 捡漏下载现成 srt**（命中即用、标注 source/质量、不追求覆盖率）。L2 按需生成作为后续可选扩展，不纳入本次交付。

---

## 1. 范围

| 做 | 不做 |
|---|---|
| NAS + 40~50 盘 LTO-6 统一编目 | 网页播放 / 转码 / 流媒体 |
| 按 演员/导演/番号/片商/系列/题材/日期 筛选 | 外网访问、多用户权限 |
| 封面、剧照、中日双语元数据 | 自动换带 |
| 每部标注"在哪盘磁带 / NAS 哪个路径" | 磁带数据迁移；改 v1 代码 |
| 中文字幕 L0 标记 + L1 捡漏 | 全量字幕生成、Whisper 集成 |
| 磁带盘点队列 + manifest（DB 可由磁带重建） | 磁带内文件整理/重命名 |

## 2. 架构

```
┌──────────── av-archive（独立容器，新） ────────────┐
│ /library /library/<code> /sources /scrape /audit   │
│ /settings                                          │
└──────┬─────────────────┬──────────────────┬────────┘
       │                 │                  │
┌──────▼───────┐ ┌───────▼────────┐ ┌───────▼──────────┐
│ 片库层        │ │ 元数据层        │ │ 盘点/介质层        │
│ title/file   │ │ r18 dump 导入   │ │ LibraryVolume    │
│ 筛选/FTS     │ │ JavDB 补缺口    │ │ LibraryFile      │
│ 封面缩略图    │ │ 图片下载        │ │ AuditRun 队列    │
└──────────────┘ └────────────────┘ └───────┬──────────┘
                                            │ TapeAdapter
                              ┌─────────────┴──────────────┐
                              │ ltfs-webui（现有，独占设备）    │
                              │ mount/unmount/eject · LTFS  │
                              └────────────────────────────┘
```

红线：v1 一个字节不改；磁带设备只允许 v1 操作；v1 的 `File.id` 会漂移，任何外键都不挂它；介质归属只信 `file.tape_id`。

## 3. 数据模型

### 3.1 介质层

```python
library_volume(id, kind['tape'|'nas'], ref, name, capacity_bytes,
               enabled, last_scan_at, last_scan_status)
library_file(id, volume_id NOT NULL, rel_path NOT NULL, name, size, mtime,
             code_guess, confidence, title_id NULL, sha256 NULL,
             seen_at, deleted_at)          -- UNIQUE(volume_id, rel_path)
audit_run(id, volume_id, status, started_at, finished_at,
          files_seen, files_new, error)
```
- `UNIQUE(volume_id, rel_path)` 两列均 NOT NULL → 绕开 SQLite NULL 唯一索引失效，且天然支持"同一番号在多盘"。
- `rel_path` 用 POSIX 相对路径（磁带 = 目录 path + '/' + 文件名；NAS = 相对共享根）。
- v2 自己扫（`os.walk`），不依赖 v1 的扫描结果。

### 3.2 影片层

```python
title(id, dvd_id UNIQUE, content_id, title_ja, title_zh, title_en,
      release_date, runtime_mins, rating, rating_votes, rating_source,
      maker_id, label_id, series_id, director_id,
      has_chinese_sub, cover_url, cover_local, work_url,
      source_dump_date, updated_at, manual_lock)
actress(id, name_kanji, name_kana, name_romaji, image_url, local_avatar)
title_actress(title_id, actress_id, role['female'|'male'])
director / maker / label / series(id, name_ja, name_en|kana)
genre(id, genre_ja, genre_zh, genre_en, source_tag_id)
title_genre(title_id, genre_id, source['r18'|'javdb'|'javbus'])
asset(title_id, kind['cover'|'still'|'avatar'], url, local_path, bytes)
subtitle(id, title_id, lang, format, local_path, source, source_url,
         quality_flag, sha256, created_at)
scrape_task(id, title_id, stage['dump'|'javdb_gap'|'asset'], status,
            attempts, error, updated_at)
```
- 双语：`显示名 = zh or ja`，无中文回退日文，不存空值。
- 题材双写：`genre_ja`（r18 权威）+ `genre_zh`（JavDB 繁中），对齐复用 JavSP 的 `genre_javdb.csv`。
- 评分只有 JavDB 有（r18 dump 无评分）。
- FTS5 虚表：`title_fts(code, title_ja, title_zh, actress_names)`，中文按字面切分。

## 4. 关键流水线

### 4.1 番号识别（决定成败）
三级：正则命中（`[A-Z]{2,6}[-_ ]?\d{2,5}`，先剥 `-C/-CH/-UC/-4K/-1080p/cd1` 等噪声）→ 归一化（补零、大写、特殊厂牌 FC2/HEYZO/SIRO/TOKYO-HOT/1pondo/Caribbean/10musume）→ 人工确认（低置信度进待确认，修正写入 alias 表供下次复用）。
**M0 硬门槛：拿真实文件名样本跑命中率，不达标不进入下一阶段。**

### 4.2 元数据
1. 每周下载 r18 dump → 导入本地 SQLite（`PRAGMA user_version` 记版本）
2. 本地匹配：番号 → title/演员/导演/厂牌/系列/题材/封面/剧照
3. 缺中文题材或评分的条目 → JavDB 补（预计 10–30%）
4. 图片本地化：封面 + 剧照压到 800px 宽，存数据卷（**不放 DB 目录**）
5. 中文标题：离线 MT（复用 JavSP `translate.py` 的 provider 抽象），结果缓存入 `title_zh`

存储：封面+剧照 ≈ 2.5–4 MB/部 → 2 万部 50–80GB，4 万部 100–160GB（压到 800px 后约减半）。

### 4.3 磁带盘点（存量 40~50 盘）
`AuditRun` 队列：选盘 → **强校验实际条码 ≠ UNKNOWN 且等于预期** → `TapeAdapter.mount()` → 只读递归扫描 → upsert `library_file` → 写 `._manifest/<barcode>.json` 到磁带根 → `eject()` → 提示换下一盘。
- 队列状态全落库，容器重启不丢（不依赖 `Tape.status`）。
- 单盘 1–5 分钟，40 盘含换带约 1–3 小时。
- `._manifest` 用点开头，避开 v1 扫描器（v1 只跳过 `.` 开头）。
- 扫描异常单文件不中断整盘（v1 的 D11 缺陷，v2 必须自己做对）。

### 4.4 字幕（L0+L1）
- **L0**：文件名/版本标记识别中字版（`-C`/`中字`/`UC`）→ `has_chinese_sub`；JavDB 的"含中字"标记作交叉验证。
- **L1**：迅雷字幕库接口按番号查（**必须加番号正则 + 时长校验**防误命中），SubtitleCat 低质兜底；下载后 sha256 + 记录 source/quality。
- 不追求覆盖率，命中即用；未命中就是没有。

## 5. UI

| 页面 | 内容 |
|---|---|
| `/library` | 封面网格 + 筛选（演员/导演/片商/系列/题材/年份/介质/有无中字）+ FTS 搜索（番号/中日标题/演员） |
| `/library/<dvd_id>` | 封面、剧照、完整双语元数据、**所在介质（磁带条码 / NAS 路径）**、字幕、重新刮削、手工编辑 |
| `/sources` | 磁带卷 + NAS 根目录、容量、最近扫描、导入 v1 索引 |
| `/scrape` | dump 版本/导入状态、JavDB 补缺口队列、失败重试、限速/代理 |
| `/audit` | 盘点向导：当前盘 → 扫描 → 换盘提示 → 历史 |
| `/settings` | 源开关与优先级、NAS 根、缩略图规格、Adapter 配置 |

技术栈：Flask + Jinja2 + Bootstrap 5（**本地 vendor，不用 CDN**，Unraid 离线可用）+ 原生 JS，无前端构建步骤——和 v1 同一套风格，你自己能维护。

## 6. 与 v1 的集成（独立服务模式）

不改 v1，靠 `TapeAdapter` 抽象解耦：

```python
class TapeAdapter:
    def status() -> list[Drive/Tape]     # 当前挂载盘、设备状态
    def mount(barcode) -> None
    def unmount() -> None
    def eject() -> None
    def scan(mount_point) -> [FileEntry] # 自己 os.walk
```

| 实现 | 做法 | 适用 |
|---|---|---|
| `HttpAdapter`（默认） | 调 v1 的 `POST /api/tape/{mount,unmount,eject}`、`GET /api/tape/{current,device-status}`；扫描直接读挂载点 `/media/tape` | v1 在跑，由它独占驱动器，不会双挂载 |
| `DirectAdapter` | 直接调 `ltfs` 命令；可只读 v1 SQLite（join 必须用 `file.tape_id`） | v1 没跑时；或导入历史索引 |

部署关系：

```
docker-compose（新增 av-archive 服务）
  av-archive ──HTTP──► ltfs-webui（现有容器，独占 /dev/sg*）
      ├── /mnt/user/av : ro      # NAS 片库
      ├── /media/tape  : shared  # LTFS 挂载点（v1 挂载后双方可见）
      └── /data/avlib  : rw      # SQLite + 封面缩略图
```

**6.1 历史数据导入（可选）**：首次上线跑一次性 `import_v1_index`，读 v1 SQLite → 写 `library_volume/library_file`（`INSERT OR IGNORE`）→ 对账行数。之后以自己扫为准。

**6.2 v1 缺陷规避清单**：备份恢复路径是坏的（别用它的 restore）、`.env` 未加载（显式 `load_dotenv`）、命令超时不 kill（自己用 subprocess timeout+kill）、条码 UNKNOWN 会串盘（强校验）、Bootstrap CDN（vendor 化）、开 WAL 但**不全局开 foreign_keys**。

## 7. 里程碑

| 阶段 | 内容 | 验收 |
|---|---|---|
| **M0** | 真实文件名抽样 + r18 dump 实际导入验证 | 命中率报告、dump 覆盖率报告（决定 FC2/无码管道） |
| M1 | 服务骨架：Flask app + models + 迁移 + Dockerfile/compose + 空页面 | 容器起来，DB 建表，v1 未受影响 |
| M2 | 番号解析器 + 单元测试 + 合成语料 | 解析器测试全绿；真实样本命中率 ≥ 目标 |
| M3 | NAS/磁带扫描 + LibraryFile + `/library` 列表（文件级） | 1 万条分页 < 1s |
| M4 | r18 dump 导入 + title 归组 + 详情页 + 封面 | 500 部真实条目完整度 ≥ 90% |
| M5 | 磁带盘点队列 + manifest + 介质标注 | 模拟/真实磁带可中断续跑 |
| M6 | JavDB 补缺口 + 字幕 L0/L1 + FTS + 全维筛选 | 筛选全覆盖，中字标记准确 |
| M7 | Unraid 部署文档 + 备份/重建演练 | 从零部署成功，DB 可由 manifest 重建 |

## 8. 风险

| 风险 | 对策 |
|---|---|
| 番号命中率低（素人/无码/FC2 占比高） | M0 先量化；FC2/素人独立管道；人工确认 + alias 学习 |
| r18 覆盖不到的部分 | dump 导入后统计未命中清单，再决定补源 |
| 磁带唯一副本、DB 丢失 | 每盘写 manifest；DB 每日备份到 NAS + 独立盘 |
| 图片 50–160GB | 压到 800px，只存必要尺寸 |
| 手动换带中断 | 队列落库，按 barcode 断点续跑 |
| 中文标题只能机翻 | 人工抽查 + `manual_lock`，不覆盖人工修改 |
| JavDB 域名轮换/封禁 | 只补缺口、低频、cookie 可配；失效时片库仍可用 |
| v1 与本服务抢磁带设备 | 默认 HttpAdapter，设备只归 v1 |

## 9. 已确认决策

1. 字幕：**L0 标记 + L1 捡漏**（不做 L2 生成）
2. 交付：**独立服务**，与磁带管理分离
3. 开发：**本机开发 + 模拟数据/虚拟磁带**，产出镜像，你部署到 Unraid
4. 元数据：**自研轻量 r18 dump 入库**（不部署 Javinizer-Go）

## 10. 还需要你提供的数据

1. **200–1000 个真实文件名样本**（`find /mnt/user/av -type f | head -500` 即可）——没有它无法量化命中率、无法决定 FC2/无码管道
2. NAS 片库根路径 + 大致条目数
3. 磁带盘数 + 任意一盘的目录结构示例
4. Unraid 上磁带机怎么接的（SAS 直通？）＋ 现有 compose 配置
5. 库里"中字版/硬字幕版"大概占比

---

## 12. 实施进度（截至本轮）

| 里程碑 | 状态 | 验证 |
|---|---|---|
| M0 dump 验证 | ✅ | 18,860,058 行导入 55s；1,170,727 去重番号；覆盖报告已出 |
| M1 骨架 | ✅ | 独立服务、Dockerfile/compose、6 页面 |
| M2 扫描器 | ✅ | 合成语料 346 文件/297 视频/49 sidecar；1 万文件目标 <10s 已验证 |
| M3 番号解析 | ✅ | 194 用例；语料 540/540；合集强制降置信 |
| M4 物化 | ✅ | 真实 dump：201 码→134 命中/67 未命中（FC2/素人），0.58s |
| M5 磁带盘点 | ✅（模拟） | 假 v1 API + 假挂载目录全流程；真实设备待联调 |
| M6 JavDB/字幕 | ✅ | JavDB 实测 12/12；迅雷字幕 SSNI-001 实测落盘 52KB |
| M7 部署 | ⏳ | 文档完成；本机 Docker daemon 未运行，镜像未构建 |


### 第二轮补充（assets / rebuild / 双语 / M0 工具）

| 能力 | 状态 | 证据 |
|---|---|---|
| 封面/剧照/缩略图下载 | ✅ | 120 部实测：1128 文件 / 62.8MB / 191s；125 封面 / 120 缩略图 / 1212 剧照；老片 302 占位图正确跳过 |
| 中文标题 title_zh | ✅ | JavDB `.current-title` 实测可用；累计 48/150 写入；含假名日文误命中守卫；403 熔断已实测 |
| 从 manifest 重建 DB | ✅ | CLI dry-run + 真导入 4 文件（含中文路径/sidecar/flags）验证 |
| M0 文件名命中率工具 | ✅ | 合成语料报告：可解析率 90.6%、r18 覆盖 70%、特殊厂牌 14%（合成样本，非用户真实库） |
| 分发打包 | ✅ | `scripts.make_dist` → 246KB tarball + sha256，含 Dockerfile/compose |

### 第三轮补充（解析器真实基准 / 灾难恢复 / 分发）

| 能力 | 状态 | 证据 |
|---|---|---|
| 番号解析器真实基准 | ✅ | 2000 条真实 dvd_id 可解析率 **99.9%**（原 93.8%）；全变体 99.91%；刻意保留的盲区已列明 |
| 灾难恢复重建 | ✅ | manifest→DB 真实导入验证；10k 文件 0.18s；重建后 materialize 能重新关联 |
| 分发打包 | ✅ | `scripts/make_dist` → 约 250KB tarball + sha256 |
| UI 本地封面 | ✅ | 首页 42 张缩略图实测渲染；详情页本地封面 200 |
| 待办 | ⏳ | 「70–89 置信度待抽检」角标；JavDB 冷却时间持久化；真实设备联调 |

### 第四轮补充（容器验证 / 人工修正 / 自检）

| 能力 | 状态 | 证据 |
|---|---|---|
| Docker 镜像 | ✅ | arm64 `av-archive:local` 296MB + **amd64 离线包 62MB**（Unraid 架构）均构建/运行验证；容器内 6 页面 + API 200；自检 PASS |
| 人工修正工作流 | ✅ | `/review`：137 组待确认；assign 写 CodeAlias + 改番号 + 立即物化；幂等；FC2 未命中不 500 |
| JavDB 冷却 | ✅ | 403 后写 AppSetting `javdb_blocked_until`，冷却期内零请求（31 个 javdb 测试） |
| 置信度抽检 | ✅ | 70–89 角标 + `/library?review=1` 筛选 |
| 部署自检 | ✅ | `scripts/selfcheck`（依赖/目录/DB/staging/解析器/磁带/深度扫描）|
| 打包产物独立运行 | ✅ | dist 解压到干净目录 + gunicorn（容器同款 CMD）+ 新库扫描物化 134 部 |

### 第五轮：合并 ltfs-webui（单容器）

| 能力 | 状态 | 证据 |
|---|---|---|
| 直连 LTFS | ✅ | DirectAdapter 默认；镜像内编译 LTFS 2.4.9（v2.4.9.0 + uthash）通过 |
| 磁带控制页 | ✅ | `/tapes`：设备信息/挂载/卸载/弹出/卷列表/操作日志；`/api/tape/*` |
| 挂载→盘点→manifest | ✅ | 假 ltfs + bind mount 实测：条码 LTO6-0001 挂载成功→盘点 6 文件→写 `._manifest/LTO6-0001.json`→弹出 |
| 防串盘 | ✅ | 条码不符/LTO6-TEST 截断场景：自动卸载+弹出、不建卷、TapeOperation failed |
| 设备互斥 | ✅ | 进程内 RLock + 跨进程 flock；实测 `docker exec` 占锁时 API 返回 busy，释放后可挂载 |
| 文件浏览/传输 | ✅ | `/files` 双面板；双向传输目标自动创建、`.part` 原子写；路径逃逸/软链/跨方向全部拒绝（41 测试） |
| 操作日志 | ✅ | `/logs`：TapeOperation + Job，stdout/stderr 截断展示 |
| 备份 | ✅ | `/backup`：创建/校验/下载/删除；SQLite backup API（WAL 安全），不做在线恢复 |
| 镜像（arm64） | ✅ | `av-archive:merge` 构建、容器 10 页面 200、selfcheck PASS 8/WARN 2 |
| 镜像（amd64，Unraid） | ⏳ | 构建中，完成后导出离线包 |
| 真实磁带机 | ⏳ | 仍需上机验证（假 ltfs 已覆盖应用层） |
