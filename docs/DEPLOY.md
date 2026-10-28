# 部署到 Unraid（合并版：一个容器管磁带 + 片库）

> 合并版不再需要 `ltfs-management-webui`。镜像内置开源 LTFS（v2.4.9），
> 自己挂载/卸载/弹出磁带，同时提供片库、元数据、字幕、文件浏览、日志、备份。
> 切换前请先停掉 v1 容器（设备独占）。

## 0. 前置

1. Unraid（x86_64）+ Docker；
2. 磁带机已接在宿主机（SAS HBA），能确认设备号：`ltfs -o device_list` 或 `lsscsi -g`；常见是 `/dev/sg10`；
3. NAS 片库路径（示例 `/mnt/user/av`）；
4. **v1 已停止**（否则双挂载/设备忙）。

## 0.1 两种部署方式

### A. 代码包 + 本地构建（推荐，镜像里会编译 LTFS）

```bash
# 本机打包（含 vendor/ 里的 LTFS 源码，约 2MB）
python -m scripts.make_dist
# 产出 dist/av-archive-<时间>.tar.gz + .sha256

scp dist/av-archive-*.tar.gz unraid:/mnt/user/appdata/av-archive/
ssh unraid
cd /mnt/user/appdata/av-archive
sha256sum -c av-archive-*.tar.gz.sha256
tar xzf av-archive-*.tar.gz --strip-components=1
```

首次构建要编译 LTFS，**大约 3–10 分钟**（之后有缓存）。国内网络：

```bash
export AVLIB_BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim-bookworm
export AVLIB_APT_MIRROR=mirrors.aliyun.com
export AVLIB_PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
docker compose up -d --build
```

网络好则直接 `docker compose up -d --build`。

### B. 离线镜像（不构建，直接 load）

如果已经拿到验证过的 `av-archive-amd64-merge-image.tar.gz`（约 94MB，内置 LTFS）：

```bash
sha256sum -c av-archive-amd64-merge-image.tar.gz.sha256
docker load -i av-archive-amd64-merge-image.tar.gz
docker tag av-archive:amd64-merge av-archive:local
docker compose -f docker-compose.image.yml up -d
```

> Unraid 是 x86_64，必须用 `linux/amd64` 镜像。

## 1. 目录与 compose

```bash
mkdir -p /mnt/user/appdata/av-archive/data
cd /mnt/user/appdata/av-archive
```

编辑 `docker-compose.yml` 三处：

```yaml
    privileged: true                 # LTFS/FUSE 需要
    devices:
      - /dev/sg10:/dev/sg10          # ← 改成你的设备号
    volumes:
      - ./data:/data
      - /mnt/user/av:/mnt/av:ro      # ← 你的 NAS 片库
    environment:
      AVLIB_NAS_ROOTS: /mnt/av
      AVLIB_TAPE_ADAPTER: direct
      LTFS_DEVICE: /dev/sg10         # ← 与 devices 一致
```

**不要**再把宿主目录挂到 `/media/tape`：LTFS 在容器内自己挂载。

## 2. 启动与自检

```bash
docker compose up -d --build
docker exec -it av-archive python -m scripts.selfcheck
# 深度：扫一次 NAS + 物化 100 条
docker exec -it av-archive python -m scripts.selfcheck --deep --dir /mnt/av --limit 100
```

浏览器：`http://<unraid-ip>:5080`

## 3. 首次数据初始化

### 3.1 元数据（r18 dump）

```bash
docker exec -it av-archive python -m scripts.download_dump --out /data/dump
docker exec -it av-archive python -m scripts.import_dump \
  --sql /data/dump/latest.sql --out /data/r18dump.db
```

然后 UI「刮削」页 →「番号 → 元数据物化」。

### 3.2 媒体库（可添加多个 NAS 挂载点，只读扫描）

「介质」页 → **媒体库（NAS 挂载点）** → 填名称 + 容器内路径 → 「新增介质」。

- 像 Plex 加库一样，可以加多个挂载点（`/mnt/av`、`/mnt/av2`…），各自独立扫描；
- 默认**新增后自动扫描**，可在「设置 → 媒体库 → 新增介质后自动扫描」关闭；
- 每个媒体库可单独「扫描 / 停用 / 启用 / 移除 / 清索引并移除」；
- 扫描是**只读**的：只读取文件名/大小/修改时间并写本地索引，不会修改、移动或删除
  NAS 上的任何文件（自动化测试保证：扫描前后文件内容/mtime 不变、不新建文件）。

CLI 等价：

```bash
docker exec -it av-archive python -m scripts.scan --nas /mnt/av
```

> 旧的 `AVLIB_NAS_ROOTS` 环境变量仍兼容：首次启动会自动导入成媒体库，之后以页面配置为准。

### 3.3 磁带（合并后的核心）

1. 插一盘磁带；
2. 打开「磁带」页 → 填预期条码（可选，防串盘）→「挂载」；
   - 成功后会显示实际条码、登记磁带卷；
   - 条码不符会自动卸载并弹出；
3. 点该盘的「盘点」→ 自动 挂载→扫描→**写本地 manifest（`data/manifests/`，不写磁带）**→卸载（按 `AVLIB_AUTO_EJECT` 决定是否弹出）；
4. 完成后换下一盘。40–50 盘约 1–3 小时（含换带）。

CLI 等价：

```bash
docker exec -it av-archive python -m scripts.audit_tape --barcode FB1186 --expected FB1186
```

> 条码格式：真实 LTO 条码通常是 6–8 位字母数字（如 `FB1186`）。如果你贴的是
> `LTO6-0001` 这种带连字符的标签，解析器已放宽支持，但仍建议用标准条码。

### 3.3.1 代理（设置页）

如果容器不能直连外网：打开「设置」页 → 代理：

1. 勾选「启用代理」，填地址与端口（如 `192.168.2.66` / `7890`），保存；
2. 点「测试连通性」——会通过代理请求一次 `https://javdb.com`，拿到响应（含 403）即链路通；
3. 保存后**立即生效**：JavDB 补缺、封面/剧照下载、字幕抓取、`download_dump` 全部走这个代理；
4. 不启用时回退到环境变量 `HTTPS_PROXY`；两者都没有则直连。

代理配置存在数据库里（`AppSetting`），重启容器不丢。设置页的「运行信息」只读展示其余
环境变量配置（不再显示 v1 的 `LTFS_API_BASE`）。

### 3.4 中文 / 评分 / 封面 / 字幕

- 「刮削」页：「JavDB 补中文/评分」每批 40–50 条，间隔 30–60 分钟（403 会冷却 30 分钟，冷却期内零请求）；
- **缺封面**：片库可按「缺封面」筛选，页头显示缺封面数量；「刮削」页有「补齐缺封面」按钮——
  先给没有封面 URL 的条目从 JavDB 抓封面，再下载到本地。DMM 老片返回占位图时仍会缺，
  卡片上会标「缺封面」，可重复跑或用「待确认」页人工处理；
- 「下载封面/剧照」分批跑；
- 「字幕 L0」按文件名标记中字；L1 迅捷接口捡漏在服务层，暂未开放手动按钮。

### 3.5 人工修正

「待确认」页：FC2/素人/低置信度/合集按 `code_guess` 分组，填目标番号点「指派」，
会写 alias、改番号并立即物化。

## 4. 文件浏览与传输

「文件」页：左侧容器目录（默认允许 `/mnt,/data`），右侧磁带挂载点。

- 勾选文件 → 选方向 → 填目标目录 → 开始传输；
- 目标目录不存在会自动创建（在允许根内）；
- 大文件走后台任务，`.part` 临时名写完再 rename，中途失败不留半截正式文件；
- **磁带→容器写回**默认只能写到 `/data`（可写卷）：NAS 是 `:ro` 挂载。要直接写回 NAS，把 compose 的 `/mnt/user/av:/mnt/av:ro` 改成 `:rw`；
- 路径逃逸（`..`、软链、越界根）会被拒绝。

## 5. 备份与恢复

UI「备份」页可创建/校验/下载/删除（SQLite backup API，WAL 安全）。
自动备份（宿主机 cron）：

```bash
0 3 * * * docker exec av-archive python -m scripts.backup_db --dest /data/backups --keep 14
```

**恢复必须停机**（不做在线替换，避免 v1 那种「恢复了但没生效」）：

1. `docker compose stop av-archive`；
2. 把 `data/avlib.db`、`data/avlib.db-wal`、`data/avlib.db-shm` 一起移到 `restore-backup-<时间>/`；
3. `cp data/backups/avlib-YYYYmmdd-HHMMSS.db data/avlib.db`；
4. `docker compose up -d` → `python -m scripts.selfcheck`；
5. 回滚：停容器，把第 2 步三件套移回。

manifest 不写磁带：每盘盘点后写本地 `data/manifests/<条码>.json`，并在数据库 `tape_manifest` 表存一份。
数据库/文件丢失时两种重建方式：

```bash
# 从本地 manifests 目录（默认 data/manifests）
docker exec -it av-archive python -m scripts.rebuild_from_manifest
# 从数据库副本
docker exec -it av-archive python -m scripts.rebuild_from_manifest --from-db
```

> 旧磁带里已有的 `._manifest/` 仍能读（兼容），但新盘点不会再往磁带写。

## 6. 从 v1 切换 / 回退

切换：

1. 记录 v1 的设备号、条码规则；
2. `docker stop ltfs-webui`（**必须**）；
3. 起合并版（上面步骤）；
4. 插一盘试挂载 + 盘点，确认条码、文件数、manifest 正常；
5. 稳定 1–2 周后删除 v1 容器与镜像。

回退：`docker stop av-archive` → 启 v1；或把合并版 `AVLIB_TAPE_ADAPTER=http` 指回 v1（过渡模式，双跑时设备仍只归一边）。

详见 `docs/MERGE_PLAN.md`。

## 7. 常见问题

| 症状 | 原因 | 处理 |
|---|---|---|
| 镜像构建失败 apt/pip 超时 | 网络 | 加 `AVLIB_APT_MIRROR` / `AVLIB_PIP_INDEX_URL` |
| `/tapes` 显示未检测到设备 | 设备号错/未直通/未 privileged | 改 `devices`、确认 `docker exec av-archive ltfs -o device_list` |
| 挂载报设备忙 | v1 还在跑/上次 ltfs 未退出 | 停 v1、`umount /media/tape`、重启容器 |
| 条码不符自动弹出 | 插错带/条码解析失败 | 按提示换正确盘；必要时手动填实际条码 |
| 扫描 0 文件 | 挂载点空/权限 | `docker exec av-archive ls /media/tape` |
| 传输失败"目标目录不存在" | 旧版本未自动建目录 | 升级到最新版；或手动 mkdir |
| 物化大量无封面 | r18 无图/老片 | 正常 |
| JavDB 403 | 风控 | 等冷却（30 分钟）或配 `AVLIB_JAVDB_COOKIE` |
| 字幕少 | JAV 硬字幕为主 | 只做标记与捡漏，不追求覆盖率 |


---

## 9. 真机部署记录（测试机 192.168.2.24）

这是第一次在真实 HP Ultrium 6-SCSI（`/dev/sg10`）上的部署记录，供以后避坑：

### 9.1 环境

- Debian 13 (trixie)，Python 3.13，**没有 systemd / docker / cron**（PID1 是 bash）；
- 已装 LTFS 2.5.0；`/dev/sg10` 与 `/dev/nst0` 都直通；
- 部署方式：venv + gunicorn（不是 Docker），目录 `/opt/av-archive`，监听 `0.0.0.0:5080`；
- 依赖版本按 Python 3.13 的 wheel 装：Flask 3.1 / SQLAlchemy 2.1 / Pillow 13 / lxml 6。

### 9.2 实测结果

| 项 | 结果 |
|---|---|
| 局域网访问 | `http://192.168.2.24:5080` 外部机器全部页面 200 |
| 挂载真磁带 | 成功，条码 **FB1186**，耗时 **109.6 秒**（LTO 装载 + LTFS 索引） |
| 磁带内容 | **864 文件 / 468 视频 / 396 字幕 / 627 个可识别番号** |
| 卸载 | 成功（25ms），**未弹出**（AUTO_EJECT=0 生效；手动弹出被拒绝） |
| 物化 | 110 部命中（阈值 90）；449 个低置信度进「待确认」 |
| 封面/剧照 | 15 封面 + 300 剧照 / 6.7MB |
| 代理 | 设置页配 192.168.2.66:7890，javdb 测试 200 / 686ms |
| r18 dump | 262MB 下载（23MB/s）+ 导入 1.8GB / 约 2 分钟 |

### 9.3 文件位置解析（置信度）

- 父目录=厂牌前缀（`NMSL/NMSL-019.mp4`、`MIGD/MIGD649.HD.mkv`）→ 92 分；
- 目录名=番号（`AGAV-101/AGAV-101.mp4`）→ 92 分；
- dump 没收录但高置信度的正常番号会建**占位条目**（如 NMSL-019），文件先进片库；
  详情页可直接上传封面/剧照补图。FC2/HEYZO/LUXU 等私有番号仍不建条目。
- 实测 FB1186：标题 219 → 291（匹配）+ 22（占位），低置信度剩余 19 个文件。

- 干净命名 `PREFIX-三位数字`（如 `ALDN-336.2024.1080p.DMM.WEB-DL...`）即使前缀未收录，
  带 1080p/DMM/WEB-DL 等发布信息时也给 92 分，可自动入库；
- 目录名=番号（`AGAV-101/AGAV-101.mp4`）是强确认，抬到 92；
- 增量扫描会检测解析器变化并刷新 `code_guess/confidence/flags`（不需要换盘重扫）；
- 实测 FB1186：物化标题 110 → 219 部，低置信度待确认从 449 → 102 个文件。

### 9.4 踩坑

1. **驱动器未 LOAD 时 ltfs 会卡在 `Loading cartridge`**：先 `sg_start --load /dev/sg10`，`mt -f /dev/nst0 status` 看到 `BOT ONLINE` 再挂载；
2. **不要把 `device_info/device_list` 放进页面状态路径**：真机上可能卡住并占着 `/dev/sg10`（我们因此遇到 EBUSY）。现在默认状态只查 `status()`（本地节点/挂载点），详情查询有 15s 短超时；
3. **gunicorn 与旧进程抢 5080**：重启前先 `pkill -f "gunicorn.*5080"` 并等端口释放；
4. 首次挂载超 300s 的失败记录可以忽略（当时介质未 LOAD）；介质到位后 110s 成功；
5. 这台机器没有 systemd/cron：重启后要手动 `/opt/av-archive/start.sh`（如需开机自启，可加 rc/init 脚本）。

### 9.5 JavDB 未命中标记

- `title` 表新增后台字段 `javdb_miss_at` / `javdb_miss_count`（UI 不显示）；
- `fill_gaps` 明确未命中的番号会打标记，默认 **30 天**内不再进入候选
  （`AVLIB_JAVDB_MISS_RETRY_DAYS` 可调）；
- 之后成功命中会清空标记；`only_codes` 人工指定番号可绕过标记强制重试；
- 目的：反复点「JavDB 补中文/评分」不会每次重跑同一批未命中的老内容。

### 9.6 自动轮询下载缺失封面/剧照

- 设置页开关：「自动轮询下载缺失封面/剧照」，默认开启；
- 后台 worker 空闲时检查缺口并自动入队 `cover_fill`；一批结束立即接下一批；
- 片库新增影片/扫描新盘后，下一轮轮询会自动启动；
- 有任务 pending/running 时不会重复入队；
- JavDB 403 冷却或未命中标记期间不会空转；
- 调参环境变量：`AVLIB_ASSET_AUTO_POLL`（检查间隔秒，默认 60）、
  `AVLIB_ASSET_AUTO_BATCH`（每批条数，默认 15）。

### 9.7 时间 / 来源分组 / 非 JAV 内容刮削

- 系统时区：容器 `/etc/localtime` → `Asia/Shanghai`，`start.sh` 里也 `TZ=Asia/Shanghai`；
  UI 里所有 UTC 时间用 `bjtime` 过滤器显示为北京时间。
- 待确认页无番号文件按「来源 + 父文件夹」分组（同一目录视为同一部作品）：CosplayJAV、FC2-PPV、Marc Dorcel（啄木鸟）、
  Blacked、Vixen、Tushy、Brazzers、Naughty America、素人/无码、其他；不再全部塞进
  「无番号」一组。
- 非 JAV 内容刮削：
  1. FC2：高置信度 FC2 番号会建占位条目；详情页有「从 FC2 官方页抓取」按钮，
     自动拼 `adult.contents.fc2.com/article/<id>/`；
  2. CosplayJAV / 欧美厂牌：详情页「从网址导入」粘贴官方作品页 URL，
     解析 OpenGraph/JSON-LD 的标题、封面、发布日期并下载封面；
  3. 抓不到的字段（演员/题材/片商）继续用逐字段解锁编辑；
  4. 批量欧美片后续可另接 StashDB/IAFD（需要 API key 或专用解析器）。

### 9.8 合集 / 母带（文件夹分类）

- 新增「合集」页：未挂 Title 的文件按层文件夹聚合，例如
  `蚊香社无码流出/…/清城ゆき/20170621.mkv` → 合集 `蚊香社无码流出`；
- 不建额外表、不改文件归属；合集详情保留完整路径、大小、置信度、所在卷；
- 待确认页的无番号分组显示所属合集，并链接到该合集；
- 蚊香社无码流出会单独识别为来源（不再混进普通「素人/无码」）。

### 9.9 封面/剧照下载失败冷却

- `title.cover_failed_at` / `asset.failed_at` 记录下载失败/占位图；
- 失败后默认 **30 天**内自动轮询不再重试（`AVLIB_ASSET_FAILED_RETRY_DAYS` 可调），
  避免 DMM 老片占位图导致 `cover_fill` 无限循环；
- 成功下载会清空失败标记；手动「强制重下」绕过标记立即重试；
- 自动轮询统计 `missing_assets_summary` 已排除冷却期内的失败项。

### 9.10 高清剧照 / 视频截图 / SMB 运行时挂载

- 剧照：DMM/JavDB 原图最大 800×450，下载时统一 Lanczos 放大到 1920 宽；
  已下载旧图可跑「升级已有剧照到高清」（`AVLIB_STILL_WIDTH` 可调）；
- 视频截图（预留）：详情页「扫描视频截图」入队 ffmpeg 抽帧，输出到
  `media/screenshots/<番号>/`，写 `asset(kind='screenshot')`。抽帧数量在设置页
  「视频截图数量」配置（默认 6，1–20），按视频时长平均间隔；时长探测顺序
  ffprobe → ffmpeg 输出 → 文件大小/假定码率估算 → 2 小时兜底，保证不会只截片头。
  仅对 NAS/本地文件启用，
  LTO 上 seek 太慢会拒绝；需要 ffmpeg/ffprobe；
- SMB：介质页新增「SMB 挂载」表单，运行时 `mount.cifs` 到容器内目录，
  避免了重启容器加 volume。需要镜像装 `cifs-utils`、容器有 `CAP_SYS_ADMIN`
  （privileged 或 --cap-add SYS_ADMIN）。凭据存本地 `data/smb/*.cred`，
  API 不返回密码。

### 9.11 已归档标记

- 片库卡片右上角：该 Title 有未删除的磁带文件时显示「已归档」；
- 筛选「归档：已归档（在磁带）/ 未归档（不在磁带）」；
- 提示用户：归档在磁带上的影片不能像 NAS 一样随机读取，需要先挂载/回迁。

### 9.12 安全与性能升级

- **CSRF**：写操作需携带 `X-CSRF-Token`（页面 JS 自动注入；Cookie 名 `csrf_token`）。
  外部脚本可先 GET 拿 Cookie，或设 `AVLIB_CSRF=0` 关闭（不推荐）。
- **SMB 凭据**：密码不再写 SQLite，改存 `data/smb/source_<id>.cred`（0600）；
  旧明文密码启动时自动迁移并清空 DB 字段。
- **活动进度**：哈希/抽帧等进度任务会写入 `total`，活动页显示百分比和进度条；
- **内容哈希**：`LibraryFile.sha256`，manifest 写入 `sha256`；「磁带」页有「哈希抽检」
  （100 文件/次），可用 `/api/integrity/hash` 指定 volume_id/limit/verify；
  另有「哈希+抽帧」：同一次挂载会话里哈希完顺手抽帧（`with_frames/max_videos/frame_count`），
  会二次读带/seek，较慢，建议只对少量重点片使用。
- **编辑历史**：详情页「编辑历史」可逐条撤销（恢复快照，含演员/题材关联）。
- **搜索**：`q` 优先走 SQLite FTS5（title_fts），无结果/索引异常回退 LIKE；
  刮削页有「重建搜索索引」，物化结束自动重建。
- **片库性能**：筛选项/缺封面数/总数短 TTL 缓存，卡片 maker 使用 joinedload，
  图片 lazy + async decoding。

### 9.13 分页 / 统计 / 任务状态修正

- 待确认分组支持分页（默认 200 组/页），带页码、省略号、下拉跳转；
- 介质页「媒体库」统计 = NAS/SMB 媒体库 + 磁带卷数量；
- 磁带页预期条码改为下拉选择（仅曾挂载过的磁带，默认空=不校验）；
- 元数据物化任务上报 total，活动页显示完成百分比；
- 活动页恢复遗留 running 任务（服务重启 후标记中断），任务终态写入失败会自动重试，避免“已完成但仍在动画”。

### 9.14 多任务并行

- 后台任务默认 **3 个 worker 线程**并行执行，可用 `AVLIB_WORKER_THREADS` 调整（1–8）；
- 物化/扫描/封面/抽帧等可同时跑；磁带盘点、扫描、传输、磁带哈希仍用互斥锁串行，避免抢设备；
- 自动封面只在队列完全空闲时补批，不会挡住手动任务；
- SQLite 已开 WAL，任务进度/终态写入冲突会自动重试。

### 9.15 多文件影片抽帧

- 一个标题关联多个视频文件时，**每个文件各抽设定数量**（设置页数字 = 每个文件的数量）；
- 任务 total = 文件数 × 每文件数量，活动页显示总进度；
- 截图序号全局递增，详情页按序号展示。

### 9.16 批量生成视频截图

- 「刮削 → 操作」新增「批量生成截图」（50 部/批）；
- 只处理**非磁带**且还没有截图的可访问视频文件；
- 每部影片的每个视频文件按设置数量生成（默认 24 张/文件）；
- 任务结束可在活动页看标题/帧数统计。

### 9.17 NFO 元数据优先

- 扫描 NAS/SMB/磁带时，自动读取影片目录内的 `*.nfo`（`movie.nfo` / `folder.nfo` / 同名 `.nfo`）；
- NFO 的标题、发行日期、时长、评分、片商、导演、系列、题材、演员写入 Title，并设置 `manual_lock`；
- 后续 dump 物化只补空字段，不覆盖 NFO；
- 已有库可在「刮削 → 操作 → 导入 NFO 元数据」补处理；
- 解析失败只记录，不影响扫描/物化。

### 9.18 媒体库自动刷新策略

- SMB 媒体库默认**不自动轮询**（避免唤醒 Unraid 硬盘）；可在设置页开启，但会唤醒硬盘；
- 自动监测到抽帧/哈希/物化/传输等任务时会让路，不并发抢占 worker；
- 新增文件建议手动点媒体库「扫描」，或用 Unraid 主机事件脚本触发扫描。
