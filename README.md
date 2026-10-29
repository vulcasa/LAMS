# LAMS · Local AV Management System

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.x-000000?logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![SQLite](https://img.shields.io/badge/SQLite-WAL-003B57?logo=sqlite&logoColor=white)](https://www.sqlite.org/)
[![Version](https://img.shields.io/badge/version-1.1.2-blue)]()
[![Tests](https://img.shields.io/badge/tests-869%20passed-brightgreen)](#测试)

**LAMS** 是一个自托管的个人 AV 归档管理系统，用来把 **NAS 热数据** 与 **LTO 磁带冷数据** 统一编目、检索、播放和备份。

它面向局域网、单用户、自托管场景：

- 按番号 / 标题 / 演员 / 导演 / 片商 / 系列 / 题材 / 年份 / 介质 / 中字 / 收藏筛选片库；
- 同一部影片可以同时存在于 NAS 和多盘磁带中，系统记录每个文件的实际位置；
- 内置 LTFS 磁带控制、盘点、manifest、NAS ↔ 磁带文件传输；
- 元数据来自 r18.dev 全量 dump + JavDB 补缺 + NFO；
- 客户端解码播放（WebCodecs / FFmpeg-WASM），**服务器不转码、不生成播放缓存**；
- 完整备份包含数据库、manifest、封面、剧照、视频抽帧，并支持页面还原；
- 纯 Flask + SQLite + 原生 JS，无前端构建、无 CDN 依赖。

> **免责声明**
>
> 本项目只提供媒体库管理工具，不提供、不托管、不下载任何影片内容。
> 请只管理你有权访问的合法内容，并遵守当地法律与平台条款。
> 项目包含成人内容管理场景，仅限 18 岁以上成年人使用。
> 建议仅部署在可信局域网内，不要直接暴露到公网。

---

## 目录

- [功能](#功能)
- [技术栈](#技术栈)
- [整体架构](#整体架构)
- [目录结构](#目录结构)
- [实现方式](#实现方式)
  - [1. Web 与页面层](#1-web-与页面层)
  - [2. 数据模型](#2-数据模型)
  - [3. 扫描与物化](#3-扫描与物化)
  - [4. 元数据与资产](#4-元数据与资产)
  - [5. 后台任务](#5-后台任务)
  - [6. 磁带与介质](#6-磁带与介质)
  - [7. 客户端播放器](#7-客户端播放器)
  - [8. 备份与还原](#8-备份与还原)
  - [9. 移动端与 UI](#9-移动端与-ui)
- [快速开始](#快速开始)
- [一键安装](#一键安装)
- [Docker 部署](#docker-部署)
- [页面与 API](#页面与-api)
- [环境变量](#环境变量)
- [数据目录](#数据目录)
- [测试](#测试)
- [v1.1.0 更新计划](docs/PLAN-v1.1.0.md)
- [更新记录](CHANGELOG.md)
- [后续版本路线图](docs/ROADMAP.md)
- [v1.2.0 非标来源元数据方案](docs/PLAN-metadata-v1.2.md)
- [安全与限制](#安全与限制)
- [第三方组件](#第三方组件)
- [License](#license)

---

## 功能

### 片库

- 番号、日文/中文/英文标题、发行日期、时长、评分；
- 演员、导演、片商、厂牌、系列、题材；
- 封面、剧照、视频抽帧截图；
- 按介质（NAS / 磁带）、中文字幕、收藏状态筛选；
- 收藏星标：详情页一键收藏，片库支持「已收藏 / 未收藏」筛选；
- 合集与合集详情；
- 全文搜索索引（SQLite FTS5）。

### 元数据

- r18.dev 全量 dump 导入，作为番号底座；
- 番号解析器，覆盖常见厂牌、数字/字母番号、FC2、素人等形态；
- 低置信度结果进入「待确认」，人工修正后再物化；
- JavDB 补中文题材 / 评分，带限速、403 冷却、未命中重试；
- NFO 导入优先，手工锁定字段不被自动覆盖；
- 下载缺失封面 / 剧照 / 缩略图，支持自动轮询；
- 视频抽帧截图：默认 24 张/文件，可配置分辨率。

### 媒介与磁带

- NAS / SMB 媒体库登记与增量扫描；
- 扫描只读，不修改、不移动、不删除 NAS 文件；
- LTO 磁带控制：挂载、卸载、弹出、设备状态；
- 条码强校验，防串盘；
- 盘点队列 + 本地 manifest + 数据库双份记录；
- NAS ↔ 磁带文件浏览、复制、移动，路径逃逸校验；
- 磁带盘点、哈希抽检、哈希 + 抽帧；
- 操作日志。

### 播放

- 浏览器内播放，无服务端转码；
- MKV / HEVC / AV1 等由客户端 WebCodecs + FFmpeg-WASM 解码；
- 直接通过 HTTP Range 读取原文件，服务端不落播放缓存；
- 播放列表、最近播放、播放会话、活动页显示正在播放；
- 移动端只开放片库与播放器。

### 运维

- 后台任务队列：暂停 / 继续 / 停止；
- 日志页分离「当前活动任务」与「任务记录」；
- 活动页：任务、磁带操作、正在播放、CPU/内存/网络监控；
- 完整备份 + 数据库快照两套备份；
- 完整备份 zip 可下载，也可在页面中还原；
- 自定义 LAMS 404 页面。

---

## 技术栈

| 层 | 技术 |
|---|---|
| Web 框架 | Flask 3 |
| ORM | SQLAlchemy 2 + Flask-SQLAlchemy |
| 数据库 | SQLite（WAL） |
| 模板 | Jinja2 |
| 前端 | 原生 HTML / CSS / JavaScript，无构建步骤、无 CDN |
| 播放器 | [Movi Player](https://github.com/MrujjwalG/movi-player)（Apache-2.0），客户端 WebCodecs / FFmpeg-WASM |
| 任务 | SQLite jobs 表 + 应用内线程池 |
| 部署 | Gunicorn（1 worker / 12 threads）+ Docker |
| 磁带 | 内置开源 LTFS（编译进镜像） |
| 媒体工具 | ffmpeg / ffprobe（仅用于抽帧、缩略图等，不用于播放转码） |

当前测试：**869 passed**。

---

## 整体架构

```text
                    ┌────────────────────────────────────────────┐
                    │              Browser / Mobile              │
                    │  Jinja 页面 · 原生 JS · Movi Player        │
                    └───────────────┬────────────────────────────┘
                                    │ HTTP / Range
                    ┌───────────────▼────────────────────────────┐
                    │        Flask + Gunicorn (1×12 threads)     │
                    │  routes/  →  services/  →  models/         │
                    └───────┬───────────────┬────────────────────┘
                            │               │
          ┌─────────────────▼───┐     ┌─────▼────────────────────┐
          │ SQLite (WAL)        │     │ 数据目录 data/            │
          │ avlib.db            │     │ manifests / media / ...  │
          └─────────────────────┘     └─────┬────────────────────┘
                                            │
          ┌─────────────────────────────────▼────────────────────┐
          │  NAS / SMB 挂载         │  LTO 磁带（LTFS 直连）      │
          │  /mnt/av ...            │  /dev/sgN → /media/tape    │
          └──────────────────────────────────────────────────────┘

      后台线程：
      - jobs worker pool（扫描 / 物化 / 下载 / 抽帧 / 磁带任务）
      - media_watch（可选媒体库变动轮询）
```

请求路径尽量短：页面、筛选、元数据直接读 SQLite；只有播放/文件传输才触碰 NAS/SMB/磁带。

---

## 目录结构

```text
av-archive/
├── app/
│   ├── __init__.py             # Flask 应用工厂、错误页、移动端门禁、CSRF、Jinja 过滤器
│   ├── config.py               # 所有环境变量与默认值
│   ├── db.py                   # SQLAlchemy 实例、建表/轻量迁移
│   ├── models.py               # 全部数据模型
│   ├── routes/
│   │   ├── library.py          # 片库、详情、收藏、筛选、播放检查
│   │   ├── pages.py            # 活动/日志/设置/介质/刮削/播放器/静态媒体
│   │   ├── tape.py             # 磁带状态、挂载/卸载/弹出
│   │   ├── files.py            # 容器 ↔ 磁带文件浏览与传输
│   │   ├── review.py           # 待确认列表与人工修正
│   │   ├── logs.py             # 磁带操作 / SMB 操作 / 任务记录
│   │   ├── backup.py           # 数据库快照 + 完整备份 + 下载/校验/还原
│   │   └── api.py              # 任务、扫描、资产、搜索、磁带、传输等 JSON API
│   ├── services/
│   │   ├── scanner.py          # NAS / 磁带增量扫描
│   │   ├── code_parser.py      # 番号解析
│   │   ├── materialize.py      # 文件 → Title 物化
│   │   ├── r18.py              # r18.dev dump 导入/查询
│   │   ├── javdb.py            # JavDB 抓取（限速/熔断/重试）
│   │   ├── nfo.py              # NFO 元数据解析与优先应用
│   │   ├── assets.py           # 封面/剧照/缩略图下载
│   │   ├── video_frames.py     # 视频抽帧
│   │   ├── subtitle.py         # 字幕标记 / 抓取
│   │   ├── search.py           # FTS5 全文搜索
│   │   ├── collections.py      # 合集
│   │   ├── jobs.py             # 任务注册、入队、worker 线程
│   │   ├── media_watch.py      # 媒体库变动轮询
│   │   ├── media_sources.py    # 媒体库来源管理
│   │   ├── smb.py              # mount.cifs 挂载/卸载
│   │   ├── tape_adapter.py     # LTFS HTTP/direct 适配器
│   │   ├── tape_ops.py         # 磁带操作互斥、状态
│   │   ├── audit.py            # 盘点、manifest 读写
│   │   ├── transfer.py         # 文件传输
│   │   ├── rebuild.py          # 从 manifest 重建索引（灾难恢复）
│   │   ├── backup.py           # 数据库热备 / 校验 / 轮换
│   │   ├── full_backup.py      # 完整 zip 备份 / 还原
│   │   ├── playback.py         # 内存播放会话
│   │   ├── monitor.py          # 系统监控
│   │   ├── settings.py         # 运行设置
│   │   └── mobile.py           # 移动端白名单
│   ├── templates/
│   │   ├── base.html           # 顶部导航、LAMS 品牌、全局脚本
│   │   ├── 404.html            # 专用 404
│   │   ├── library/            # 片库、详情
│   │   ├── pages/              # 正在播放、活动、刮削、设置、介质、合集
│   │   ├── tape/               # 磁带
│   │   ├── files/              # 文件
│   │   ├── logs/               # 日志
│   │   └── backup/             # 备份与还原
│   └── static/
│       ├── css/app.css         # 全局样式（36px 控件规范）
│       ├── css/client-player.css
│       ├── js/jobs.js          # 入队/轮询/通用弹窗
│       ├── js/client_player.js # 播放列表 / 最近播放 / 会话
│       └── vendor/movi/        # 本地化 Movi Player（JS + wasm + LICENSE）
├── scripts/
│   ├── dev_seed.py             # 本地假数据
│   ├── download_dump.py        # 下载 r18.dev dump
│   ├── import_dump.py          # 导入 staging 库
│   ├── analyze_names.py        # 番号解析基准
│   ├── make_corpus.py          # 合成测试语料
│   ├── backup_db.py            # 数据库备份 CLI
│   ├── rebuild_from_manifest.py# manifest 重建 CLI
│   ├── scan.py                 # 扫描 CLI
│   ├── audit_tape.py           # 盘点 CLI
│   ├── selfcheck.py            # 部署自检
│   ├── worker.py               # 独立 worker 进程
│   └── make_dist.py            # 代码包打包
├── tests/                      # pytest（869 用例）
├── docs/                       # 设计、部署、契约、截图
├── research/                   # 数据源 / 字幕 / 覆盖率调研
├── vendor/                     # LTFS / uthash 源码包（用于镜像构建）
├── Dockerfile                  # 多阶段：编译 LTFS + 运行 Flask
├── docker-compose.yml          # 生产部署（磁带直通）
├── docker-compose.image.yml    # 离线镜像部署
├── wsgi.py                     # Gunicorn 入口
├── requirements.txt            # 开发依赖
└── requirements-prod.txt       # 生产依赖
```

`data/`、`dist/`、`.venv/`、浏览器调试目录和数据库文件都在 `.gitignore` 中，不应提交。

---

## 实现方式

### 1. Web 与页面层

- 应用工厂：`app/__init__.py:create_app()`；
- 所有页面共享 `templates/base.html`：顶部 LAMS 品牌、导航、全局弹窗/任务脚本；
- `before_request` 负责：
  - 移动端白名单；
  - CSRF 双提交 Cookie 校验（浏览器写操作需要 `X-CSRF-Token`）；
  - 请求监控；
- 页面全部服务端渲染，前端不依赖 Node/打包器；
- 表单控件统一 36px 高度（复选框除外）；
- API 与页面共用 SQLAlchemy 模型；
- 自定义 `404.html`；`/api/*` 的 404 返回 JSON。

### 2. 数据模型

`app/models.py` 主要分为两层：

**介质层**

- `MediaSource`：媒体库来源（NAS / SMB）；
- `LibraryVolume`：卷（NAS 根、磁带卷）；
- `LibraryFile`：实际文件（路径、大小、mtime、类型、sidecar、软删）；
- `TapeManifest`：磁带 manifest 记录；
- `TapeOperation` / `SmbOperation`：操作日志。

**影片层**

- `Title`：番号、标题、日期、时长、评分、封面、收藏状态等；
- `Actress` / `Maker` / `Label` / `Series` / `Director` / `Genre` 及关联表；
- `Asset`：封面 / 剧照 / 截图等资产记录；
- `ScrapeTask`：刮削任务状态；
- `Job`：后台任务；
- `PlaybackHistory`、`Collection` 等。

SQLite 使用 WAL，`db.py` 负责建表、轻量列迁移和 FTS5 虚表。

### 3. 扫描与物化

```text
扫描媒体库
  → 读取文件名/大小/mtime
  → 增量比对 LibraryFile
  → 删除的标记软删，新文件入库
  → code_parser 解析番号
  → 高置信度自动关联 Title
  → 低置信度进入待确认
  → 更新封面/截图/字幕等资产
```

扫描只读；不会修改 NAS 或磁带上的源文件。媒体库变动监测是可选的，默认关闭；启用时按分钟轮询，并在抽帧/哈希/物化等任务运行时自动让路。

### 4. 元数据与资产

- r18.dev dump 导入 `data/r18dump.db` 作为 staging 库；
- 物化任务把 dump 元数据写入主库；
- JavDB 仅补中文题材/评分，带速率限制和 403 冷却；
- NFO 优先且可锁定字段；
- 封面、剧照、缩略图、视频截图统一落在 `data/media/` 下；
- 资产下载失败/占位图有冷却，避免自动轮询反复重试。

### 5. 后台任务

- 任务写入 SQLite `job` 表；
- 应用内 3 个 worker 线程（可配置）；
- 支持任务类型：扫描、物化、封面、抽帧、盘点、哈希、传输、索引重建等；
- 支持 `pending / running / paused / done / failed / cancelled`；
- 任务日志页：
  - 「后台任务」只显示 pending / running / paused；
  - 「任务记录」滚动显示最近 1000 条历史，约 20 行高度；
- 任务结果 JSON 存在 `Job.result`，便于页面展示。

### 6. 磁带与介质

- Docker 镜像内置 LTFS，直接操作 `/dev/sgN`；
- 挂载/卸载/弹出由 `tape_adapter.py` 调用 LTFS；
- 操作使用进程内 RLock + 跨进程 flock 互斥；
- 盘点：条码校验 → 扫描 → 生成 manifest → 卸载；
- manifest 同时写 `data/manifests/` 与数据库；
- NAS ↔ 磁带文件移动/复制由 `transfer.py` 完成，带路径逃逸检查；
- 卸载、弹出等危险操作有二次确认。

### 7. 客户端播放器

- 页面：`/client-player`，旧 `/playing` 兼容重定向；
- 播放器：本地内置 Movi Player（无 CDN）；
- 通过 `/source/<file_id>` 提供原文件 Range 响应；
- 客户端 WebCodecs / FFmpeg-WASM 解码；
- 服务端不转码、不生成 MKV 重封装缓存；
- 播放列表、最近播放、进度会话、键盘快捷键、移动端抽屉；
- 直接打开播放页不会自动加载全库或自动播放，必须从影片详情进入或点最近播放。

### 8. 备份与还原

两套备份并存：

1. **数据库快照**：`avlib-YYYYmmdd-HHMMSS.db` + `.sha256`；
2. **完整备份**：`avlib-full-YYYYmmdd-HHMMSS.zip`，包含：
   - `database/avlib.db`
   - `manifests/`
   - `media/covers/`
   - `media/stills/`
   - `media/screenshots/`
   - `backup.json` 元信息（资产计数）

页面支持：

- 立即完整备份 / 仅备份数据库；
- 下载、sha256 校验、删除；
- 从已有完整备份还原；
- 上传 zip 还原；
- 还原时恢复资产并写回数据库。

> 还原会覆盖当前数据库和媒体资产，建议在无任务运行时操作，并先创建一份新备份。

### 9. 移动端与 UI

- 移动端只开放片库、详情、客户端播放器；
- 其他页面统一显示移动端提示页；
- 桌面布局为深色主题，卡片/表格/弹窗统一样式；
- 自定义 404；
- 表单、按钮、下拉框统一 36px 高度；
- 设置页保存后按钮右侧显示「设置已保存」，5 秒后消失；
- 所有危险操作使用同一套二次确认弹窗。

---

## 快速开始

### 本地开发

```bash
cd av-archive
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 可选：造一套假数据
python -m scripts.dev_seed

AVLIB_DATA_DIR=./data .venv/bin/python -m flask --app wsgi run --port 8010
# http://127.0.0.1:8010
```

固定本地预览地址（本仓库开发约定）：

```bash
AVLIB_BROWSE_ROOTS="/tmp,$PWD/data" \
  .venv/bin/python -m flask --app wsgi run --port 8024 --no-reload
```

### 元数据初始化

```bash
# 下载 r18.dev 最新 dump（每周更新，约 262MB）
python -m scripts.download_dump --out data/dump

# 导入 staging 库
python -m scripts.import_dump --sql data/dump/latest.sql --out data/r18dump.db
```

之后在「刮削」页执行「番号 → 元数据物化」，再按需补封面、剧照、截图。

---


## 一键安装

Linux / macOS 原生安装（systemd / launchd）：

```bash
curl -fsSL https://raw.githubusercontent.com/vulcasa/LAMS/main/install.sh -o install.sh
sudo bash install.sh
```

- 程序目录：`/opt/lams`
- 数据目录：`/lams/data`
- 端口：`5080`
- 自动安装依赖、拉取 GitHub 最新代码、注册开机自启、尝试开放防火墙端口
- 提供 `--skip-ltfs`（仅媒体库）和 `--update`（更新已有安装）等选项
- LTFS 默认从官方 `LinearTapeFileSystem/ltfs` 源码编译；网络失败时回退项目 `vendor/` 源码

Docker 部署仍使用 `docker-compose.yml`。

## Docker 部署

```bash
mkdir -p data
docker compose up -d --build
# http://<host>:5080
```

`docker-compose.yml` 需要按环境修改：

- NAS 挂载路径：`/mnt/user/av` → 实际片库路径；
- 磁带设备：`/dev/sg10`（用 `lsscsi -g` 或 `ltfs -o device_list` 确认）；
- `AVLIB_NAS_ROOTS`、`AVLIB_BROWSE_ROOTS`；
- 如需要，再配置代理。

磁带直通需要：

```yaml
privileged: true
devices:
  - /dev/sg10:/dev/sg10
```

SMB 运行时挂载需要 `CAP_SYS_ADMIN` / `cifs-utils`，镜像已包含 `cifs-utils`。

离线镜像部署：

```bash
docker load < av-archive-amd64-merge-image.tar.gz
docker tag av-archive:amd64-merge av-archive:local
docker compose -f docker-compose.image.yml up -d
```

---

## 页面与 API

### 页面

| 页面 | 路径 | 说明 |
|---|---|---|
| 片库 | `/library` | 筛选、排序、搜索、收藏 |
| 详情 | `/library/<dvd_id>` | 元数据、媒体、剧照、抽帧、播放 |
| 正在播放 | `/client-player` | 客户端解码播放器 |
| 活动 | `/activity` | 任务 / 磁带 / 播放 / 系统监控 |
| 磁带 | `/tapes` | 挂载、卸载、弹出、盘点、哈希 |
| 介质 | `/sources` | NAS/SMB 媒体库、SMB 挂载 |
| 待确认 | `/review` | 低置信度与人工修正 |
| 刮削 | `/scrape` | 物化、补元数据、资产、抽帧、索引 |
| 文件 | `/files` | 容器 ↔ 磁带文件传输 |
| 日志 | `/logs` | 磁带/SMB 操作、活动任务、任务记录 |
| 备份 | `/backup` | 完整备份/数据库快照、下载、校验、还原 |
| 设置 | `/settings` | 代理、媒体库监测、截图、SMB 等 |

### API（节选）

```text
GET  /api/stats
GET  /api/jobs?active=1
POST /api/jobs/<id>/pause|resume|stop
POST /api/settings/preferences
POST /api/backup/create
POST /api/backup/create-full
POST /api/backup/restore
GET  /api/backup/full/list
POST /api/assets/sync
POST /api/search/rebuild
POST /api/rebuild/manifest
POST /api/integrity/hash
POST /api/player/session
GET  /api/player/recent
GET  /source/<file_id>          # Range 原文件
```

写操作默认启用 CSRF 双提交 Cookie；无浏览器 Origin/Referer 的自动化请求可绕过首次校验。

---

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `AVLIB_DATA_DIR` | `./data` | 数据根 |
| `AVLIB_DB_PATH` | `$DATA/avlib.db` | SQLite 主库 |
| `AVLIB_MEDIA_DIR` | `$DATA/media` | 封面/剧照/截图 |
| `AVLIB_MANIFEST_DIR` | `$DATA/manifests` | 磁带 manifest |
| `AVLIB_DUMP_DIR` | `$DATA/dump` | r18 dump |
| `AVLIB_R18_DB` | `$DATA/r18dump.db` | staging 库 |
| `AVLIB_NAS_ROOTS` | 空 | 冒号分隔的 NAS 根目录 |
| `AVLIB_TAPE_ADAPTER` | `direct` | `direct` / `http` |
| `LTFS_DEVICE` | `/dev/sg10` | 磁带设备 |
| `LTFS_MOUNT` | `/media/tape` | LTFS 挂载点 |
| `AVLIB_AUTO_EJECT` | `1` | 盘点后是否自动弹出 |
| `AVLIB_BROWSE_ROOTS` | `/mnt,/data` | 文件浏览器根 |
| `AVLIB_WORKER` | `1` | 应用内 worker |
| `AVLIB_WORKER_THREADS` | `3` | worker 线程数 |
| `AVLIB_CSRF` | `1` | CSRF 开关 |
| `AVLIB_JAVDB_RATE` | `1.5` | JavDB 请求间隔（秒） |
| `AVLIB_VIDEO_FRAME_COUNT` | `24` | 默认抽帧数 |
| `AVLIB_VIDEO_FRAME_WIDTH` | `0` | 抽帧宽度（0=原分辨率） |
| `AVLIB_SOURCE_REFRESH_MINUTES` | `120` | 媒体库轮询间隔（分钟） |
| `HTTPS_PROXY` / `HTTP_PROXY` | 空 | 刮削/下载代理 |

完整默认值见 `app/config.py`。

---

## 数据目录

```text
data/
├── avlib.db              # 主库（含 WAL/SHM）
├── r18dump.db            # r18.dev staging 库
├── manifests/            # 磁带 manifest
├── media/
│   ├── covers/           # 封面
│   ├── stills/           # 剧照
│   ├── screenshots/      # 视频抽帧
│   ├── thumbs/           # 缩略图
│   ├── avatars/          # 演员头像
│   └── subtitles/        # 字幕
├── dump/                 # 下载的 r18 dump
└── backups/              # 数据库快照 + 完整备份 zip
```

`data/` 不应提交到 Git。

---

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

当前：**869 passed**。

测试覆盖：

- 番号解析 / 扫描 / 物化 / r18 导入；
- JavDB、资产下载、字幕；
- 任务队列、暂停/停止；
- 磁带 adapter、盘点和 manifest；
- SMB、传输、路径安全；
- 片库筛选、收藏、设置；
- 播放器会话、移动端、404；
- 数据库快照、完整备份与还原。

---

## 安全与限制

### 安全

- **无登录认证**：默认面向可信局域网，不要直接暴露公网；
- 写操作使用 CSRF 双提交 Cookie；
- 文件浏览和传输有路径逃逸校验；
- 备份名、下载名、还原 zip 路径均有目录穿越校验；
- 磁带操作需要 privileged / 设备直通，部署时注意权限隔离；
- 还原完整备份会覆盖数据，必须在维护窗口操作。

### 限制

- **服务器不转码**：浏览器/设备不支持编码时无法播放，可换兼容片源或使用外部播放器；
- 客户端解码性能取决于设备，4K HEVC 在手机上可能发热/掉帧；
- r18 dump 不覆盖 FC2 / 素人等全部内容，需要独立管道或人工补录；
- JavDB 可能 403/限流，需要代理或 Cookie；
- SMB 自动轮询会唤醒休眠硬盘，默认关闭；新增文件建议手动扫描或用主机事件触发；
- LTO 操作依赖具体设备与 LTFS 版本，首次部署需自检；
- 这是单用户个人项目，不保证多租户隔离或公网安全。

---

## 第三方组件

- [Movi Player](https://github.com/MrujjwalG/movi-player) — Apache-2.0，客户端 WebCodecs/FFmpeg-WASM 播放器；本地化文件位于 `app/static/vendor/movi/`，保留上游 LICENSE。
- [FFmpeg](https://ffmpeg.org/) — LGPL/GPL，取决于构建选项；用于抽帧、缩略图等。
- [LTFS](https://github.com/LinearTapeFileSystem/ltfs) — 磁带文件系统，镜像构建时编译。
- r18.dev / JavDB — 元数据来源，分别遵循其站点条款。
- 本项目不附带任何媒体文件、封面或剧照数据。

---

## License

当前仓库未附带开源许可证文件。若要在 GitHub 公开发布，请先确认第三方组件许可证并补充合适的 LICENSE；在此之前，默认保留所有权利，仅供个人自托管使用。
