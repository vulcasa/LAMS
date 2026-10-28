# 现有仓库分析：`vulcasa/ltfs-management-webui` → AV 归档片库 v2 集成方案

> 基线代码：`/tmp/ltfs-probe/ltfs-management-webui-main`（v1.0.1-beta，README 标注 beta）
> 分析方式：纯静态阅读，未执行任何 LTFS 命令、未改动 /tmp 下文件、未 pip install。
> 引用格式：`文件:行号`，全部行号对应上述目录的实际行。
> 配套文档：`av-archive/docs/PLAN.md`（Lead 的方案草稿）。本文所有建议已对齐其命名（`LibraryFile` / `LibraryTitle` / `ScrapeTask` / `AuditRun`），并在 §16 列出需要修订的地方。

---

## 0. 结论速览（先看这张表）

| # | 问题 | 结论 | 关键依据 |
|---|---|---|---|
| 1 | `File.tape_id NOT NULL`，NAS 文件怎么入库 | **不要让 NAS 文件进 `File` 表**。v1 六张表零改动，新建 `LibraryVolume` + `LibraryFile` 统一介质索引；迁移 = 纯 `CREATE TABLE` + 一次 backfill，不需要 `DROP COLUMN`/重建表 | `app/models.py:39`、`app/routes.py:365-366` |
| 2 | 重复扫描会不会产生重复 Directory/File | **同一盘重扫不会重复**（先按 tape 删再插），但会**打断其它磁带的目录树父子链**；并发/条码解析失败/挂载归属错乱时会产生真重复，且**全库没有任何唯一约束兜底** | `app/routes.py:357,365-366,391`；`app/models.py:14,76`（唯一的两个 unique） |
| 3 | 多磁带目录统合是查询拼还是落库合并 | **落库时合并**：`Directory` 表按 `path` 全局去重共享，`tape_id` = 首次创建它的那盘（会漂移）。所以"同一番号在不同磁带"只能靠 `File.tape_id` 表达，目录节点本身无法表达"哪几盘有它" | `app/routes.py:372,391,411` |
| 4 | 状态机 + 启动重置对批量盘点队列的坑 | **`Tape.status` 不能当队列锁**。每次启动无条件全表置 `ejected`；无 drive 概念；条码解析失败统一落 `UNKNOWN` 且 `barcode` 唯一 → 多盘共用一行；命令超时不 kill 子进程 | `run.py:17-26,47`；`app/utils/ltfs_tool.py:168`；`app/utils/command_executor.py:52-57` |
| 5 | 前端加新页面成本 | **低**（原生 JS + Bootstrap 5，无构建步骤，每个页面 ≈ 1 html + 1 js）。但 `base.html` 只有 `content`/`scripts` 两个 block，**`extra_css` block 被 Jinja 静默丢弃**（现有 `file_browser.css` 其实从未加载）；Bootstrap 走 CDN，内网/Unraid 离线会掉样式 | `app/templates/base.html:160,172`；`file_browser.html:5-7` |
| 6 | 推荐 v2 文件布局 | **Blueprint 划分**（v1 是单文件 `routes.py` 1411 行、无 Blueprint）。新增 `app/models_v2.py` + `app/blueprints/{library,sources,scrape,audit,settings}.py` + `app/services/*`；v1 `routes.py` **改 0 行**（可选 +1 行同步钩子），`base.html` 改约 20 行，`app/__init__.py` +6 行 | §11 / §12 |

**一句话方案**：v1 当"磁带驱动层"原样保留；v2 在旁边长出一套 `LibraryVolume/LibraryFile/LibraryTitle` 介质无关索引，自己扫描、自己 upsert，通过 `LibraryFile(volume_id, rel_path)` 唯一键解决"同番号多盘"，通过 `_manifest/<barcode>.json` 解决 DB 可重建。**不改 v1 表结构 = 迁移风险接近 0。**

---

## 1. 代码地图

### 1.1 规模

| 文件 | 行数 | 职责 |
|---|---|---|
| `app/routes.py` | 1411 | 全部 43 个路由（页面 + API），无 Blueprint、无 service 层 |
| `app/models.py` | 92 | 6 张表：Tape/Directory/File/Operation/SystemError/FileTransfer |
| `app/utils/ltfs_tool.py` | 296 | LTFS 命令封装（mount/umount/eject/device_list/scan_filesystem） |
| `app/utils/file_transfer.py` | 268 | 容器↔磁带 复制/移动，daemon 线程 + 进度写库 |
| `app/utils/command_executor.py` | 128 | subprocess 执行 + 内存进度追踪器 |
| `app/utils/error_monitor.py` | 81 | 异常落 `SystemError` 表 |
| `app/templates/base.html` | 174 | 唯一布局模板（侧边栏 + 内联 CSS） |
| 其余模板 | 1096 | index/tapes/files/logs/backup/errors/file_browser，各自内联 JS |
| `app/static/js/file_browser.js` | 495 | 唯一独立 JS 模块（class FileBrowser） |

### 1.2 启动链（关键：建表与状态重置的位置）

```
run.py:7   from app import app, db
  └─ app/__init__.py:4  app = Flask(__name__)
     app/__init__.py:6  SQLALCHEMY_DATABASE_URI = 'sqlite:///tape_metadata.db'   ← 硬编码，忽略 config
     app/__init__.py:9  db = SQLAlchemy(app)
     app/__init__.py:12 from app import routes        ← 此时 6 张模型才注册进 metadata
run.py:14  db.create_all()                            ← 只建"缺失的表"，不改已有表
run.py:21  Tape.query.update({'status':'ejected'})    ← 无条件全表重置
run.py:47  app.run(..., debug=True)                    ← 硬编码 debug，带 reloader
```

**对 v2 的直接含义**：
- 新增模型只要在 `create_all()` 之前被 import，重启即自动建表 —— 这是"零迁移脚本"的物理基础。**陷阱**：模型必须在 `app/__init__.py` 或 `routes.py` 的 import 链上，否则 `create_all()` 看不见它，静默不建表。
- 仓库里**没有 Alembic / Flask-Migrate**（`requirements.txt:1-4` 只有 flask/flask-sqlalchemy/python-dotenv/pytz），`docs/CHANGELOG.md` 里也没有任何迁移记录。→ v2 的 schema 变更没有现成工具链，推荐自己写幂等脚本（§9.4）。

---

## 2. 数据模型与约束瓶颈

### 2.1 六张表

| 表 | 定义 | 说明 |
|---|---|---|
| `Tape` | `app/models.py:12-24` | `barcode` unique **非空**（14）、`label`、`status`（16，默认 `unmounted`，无 CHECK）、`capacity`/`used_space` 是**字符串**（17-18，`"2.5TB"` 硬编码于 `routes.py:129`） |
| `Directory` | `app/models.py:26-35` | `tape_id` NOT NULL（28）、`parent_id` 自引用（29）、`path` NOT NULL（31，**无 unique**）。目录树是**跨磁带共享**的全局树 |
| `File` | `app/models.py:37-46` | `tape_id` NOT NULL（39）、`directory_id` NOT NULL（40）、`name`、`size`、`mtime/atime/ctime`。**没有 `path` 列、没有 `hash` 列、没有 unique** |
| `Operation` | `app/models.py:48-57` | 操作日志，`tape_id` 可空（50）、`command/stdout/stderr` 全量落库（54-56） |
| `SystemError` | `app/models.py:60-71` | 异常监控 |
| `FileTransfer` | `app/models.py:74-92` | 传输任务，`operation_id` unique（76），进度字段（86-88） |

### 2.2 全库只有 2 个唯一约束

- `tape.barcode`（`app/models.py:14`）
- `file_transfer.operation_id`（`app/models.py:76`）

`directory.path`、`file.tape_id+name` 都**没有**唯一约束，也**没有 `index=True`**。所以：
- "目录按 path 去重"完全是**应用层**行为（`routes.py:391`），并发/半失败时数据可重复；
- 文件表的唯一性只靠"扫描前先删"（`routes.py:365-366`）来保证，属于易碎设计。

### 2.3 外键实际上不被强制执行

`models.py` 里 `db.ForeignKey(...)` 只是 DDL 声明（22-24/28-29/34-35/39-40/50）。SQLite 默认 `PRAGMA foreign_keys=OFF`，SQLAlchemy 也不会自动开启。→ 现状：**删父行不会报错、会留下悬空 `parent_id` / `directory_id`**。这既是现有 bug 的温床（§3.3），也是迁移时必须留意的一点：v2 若打开 `PRAGMA foreign_keys=ON`，v1 的 `Directory.query...delete()`（`routes.py:365`）会因自引用子行而失败（`parent_id` 无 `ON DELETE CASCADE`）。见 §14 R6。

### 2.4 `File.tape_id NOT NULL` 的影响面（为什么难改）

三条独立证据说明"给 v1 的 `File` 打补丁"是最差选项：

1. **`File.id` 不稳定，不能当业务主键**。每次扫描 `File.query.filter_by(tape_id=...).delete()` 然后全量重插（`routes.py:365-366,439-448`），`File.id` 每次重扫全部变化。**任何影片层表只要 `title_id ← file_id` 外键指向 `File.id`，重扫一次就断链**。这一条就否决了"在 `File` 上加 `title_id`"的路线。
2. **`File` 没有路径列**。路径要从 `Directory.path` 拼（`routes.py:569-573`），而 `Directory` 是跨磁带的共享节点、`tape_id` 会漂移（§4），无法作为 NAS 侧的稳定键。
3. **SQLite 唯一索引对 NULL 不生效**。就算把 `tape_id` 改成 nullable，`UNIQUE(tape_id, name)` 里 `tape_id IS NULL` 的行在 SQLite 中互不相等（NULL ≠ NULL），NAS 文件的去重形同虚设。必须改成 `UNIQUE(volume_id, rel_path)` 且 `volume_id NOT NULL` 才可靠 —— 这已经等价于新建一张表了。

**SQLite 的能力边界（结论必须准确）**：
- 不支持 `ALTER COLUMN`（改 NOT NULL / 类型 / 默认值）→ 必须重建表；
- `DROP COLUMN` 需 SQLite ≥ 3.35 且列**不能**出现在索引/UNIQUE/PK/FK/CHECK 中 —— `file.tape_id` 带 FK 且被应用大量引用 → **不可 DROP**；
- 加列 `ADD COLUMN` 可以（但加 `NOT NULL` 列必须带默认值）；
- `CREATE INDEX` / `CREATE UNIQUE INDEX` 可以，不重建表；
- 重建表的标准做法（官方推荐）：`PRAGMA foreign_keys=OFF` → 开事务 → 建新表 → `INSERT SELECT` → `DROP` 旧表 → `RENAME` 新表 → 重建索引/触发器 → `PRAGMA foreign_key_check` → `COMMIT`。

### 2.5 其它数据层事实

- **无时间序列/无软删除**：`File` 没有 `deleted_at`、`seen_at`，所以"磁带上的文件被删了"只能靠整表重扫发现 → 2~4 万条 NAS 文件不可行。
- **`File` 无内容指纹**：做不了"同一个文件在 NAS 和磁带各一份"的判定，也做不了秒传/校验。v2 的 `LibraryFile` 建议加 `sha256`（可选、懒计算，2~4 万条全量哈希代价高，先只对 <100MB 的文件算或按需算）。
- **容量是字符串**（`models.py:17-18`）且格式由 `routes.py:451-461` 手写换算 → v2 统计容量要用 `LibraryFile.size` 求和，别解析 `used_space`。

---

## 3. 扫描入库流程与"重复扫描"真相

### 3.1 完整调用链

```
前端 files.html:281-309  fetch GET /api/tape/filesystem
→ routes.py:346  @app.route('/api/tape/filesystem', methods=['GET'])   ← 破坏性操作挂在 GET 上
→ routes.py:351  ltfs.scan_filesystem()
    → ltfs_tool.py:252  os.path.ismount(self.mount_point) 不通过就返回失败
    → ltfs_tool.py:261  os.walk 遍历，跳过 '.' 开头的目录/文件（266,274）
    → ltfs_tool.py:286  每个文件都调一次 _get_barcode()  ← O(n) 次 open(.volume_label)，4 万文件会明显拖慢
→ routes.py:355  with app.app_context():                              ← 嵌套 app_context，不必要
→ routes.py:357  tape = Tape.query.filter_by(status='mounted').first()  ← 归属靠"库里的第一行 mounted"
→ routes.py:365  Directory.query.filter_by(tape_id=tape.id).delete()   ← 只删本盘的目录
→ routes.py:366  File.query.filter_by(tape_id=tape.id).delete()        ← 只删本盘的文件
→ routes.py:372  root_dir = Directory.query.filter_by(path='').first()  ← 全局唯一根（path=''）
→ routes.py:385  按 '/' 数量排序，保证父目录先建
→ routes.py:391  existing_dir = Directory.query.filter_by(path=...)    ← 全局按 path 复用
→ routes.py:411  parent_dir = Directory.query.filter_by(path=parent_path).first()  ← 全局找父
→ routes.py:425-448  逐文件 add(File(...))，路径仅用于查 directory_id，不落库
→ routes.py:451-461  更新 tape.used_space
→ routes.py:466-473  写一条 Operation
```

**路径主键怎么定的**：`Directory.path` 是相对挂载根的 POSIX 相对路径（`ltfs_tool.py:262-264` 的 `os.path.relpath`），根目录用空串 `''` 表示（`routes.py:372`、`ltfs_tool.py:263-264`）。`File` **没有存路径**，只存 `name + directory_id`。所以"文件的完整路径"永远是 `Directory.path + '/' + File.name` 现拼（`routes.py:569-573`、`files.html:70-74`）。

### 3.2 同一盘重复扫描：不会重复，但也不安全

- 同一盘第二次扫：`routes.py:365-366` 已把本盘的 `Directory`/`File` 删干净，然后重建 → **不会产生重复行**。
- 但注意副作用：**根目录 `path=''` 那行的 `tape_id` 也可能被删掉**（如果它是本盘创建的），此时 `routes.py:372` 会重建一个新根 → **其它磁带挂在这个旧根下的子树立刻变成悬空节点**（`parent_id` 指向已删除的 id）。因为 FK 未强制（§2.3），不会报错，只会**从目录树上静默消失**：`files.html:130` 只渲染 `parent_id === null` 的节点，`/api/directories`（`routes.py:510-518`）也只会按传入的 `parent_id` 过滤，悬空子树既不是根也查不到。
- 结论：**"重扫同一盘"对*本盘*是幂等的，对*其它盘*是有破坏性的**。这是 v1 最隐蔽的数据缺陷。

### 3.3 真正会产生重复/错乱的四种情况

| 场景 | 机制 | 后果 |
|---|---|---|
| **A. 并发扫描**（双击按钮、两个标签页、慢扫描时刷新） | `routes.py:365-366` 的 delete 与 439-448 的 insert 之间没有锁；两个请求都先删后插 → 交错插入 | `Directory` 同 path 两行（`routes.py:391` 的 `.first()` 检查不是原子操作）、`File` 成群重复；`/files` 页面直接显示重复 |
| **B. 条码解析失败** | `ltfs_tool.py:168` 兜底返回 `'UNKNOWN'`；`tape.barcode` 唯一（`models.py:14`）→ `routes.py:122` 查到同一个 UNKNOWN 行并复用 | **两盘不同的磁带共用一条 Tape 行**，扫描 A 盘删掉的是 B 盘的文件、写入的也进同一行 → 串盘。且 `routes.py:122-134` 不会纠正已有 barcode |
| **C. 库里有多条 `status='mounted'`** | 卸载/弹出不带 `tape_id` 时只改"第一条 mounted"（`routes.py:200-203`、`261-264`），残留行长期脏；`routes.py:357` 取 `.first()` | 扫到的文件被记到错的磁带上 |
| **D. 目录树跨盘漂移**（§4） | 共享 `Directory` 的 `tape_id` 只记录"首建者" | `Directory.tape_id` 与"哪些盘有这个目录"完全不等价，任何按 `directory.tape_id` 做的统计都是错的 |

### 3.4 给 v2 的结论（重要）

**不要继承"delete + 全量重插"这个模式。** 2~4 万条 NAS 文件下它不可行，而且它会摧毁一切指向 `File.id` 的外键。v2 必须：

1. `LibraryFile` 的唯一键是 **`(volume_id, rel_path)`**（`volume_id NOT NULL`）——与 `File.id` 完全解耦；
2. 扫描用 **upsert + `seen_at` 时间戳 + 软删除**：本次没出现的行 `seen_at` 不更新，事后把 `seen_at < scan_start` 的行标记 `deleted_at`，而不是物理删；
3. 扫描接口一律 **POST**（v1 用了 GET，`routes.py:346`）；
4. 扫描期间用**单例锁**（进程内 `threading.Lock` + DB 里的 `scan_job` 行）防并发；
5. **信任 `File.tape_id`，不要信任 `Directory.tape_id`** —— 若要从 v1 存量物化，join 必须走 `file.tape_id`（§9.4 的 SQL）。

---

## 4. 多磁带目录统合：落库时合并，不是查询拼

### 4.1 证据

- `routes.py:372`：根目录按 `path=''` 全局查找，找到就复用 —— **不看 tape**。
- `routes.py:391`：每个目录按 `path` 全局查找，找到就复用 —— **不看 tape**。
- `routes.py:411`：父目录同样按 `path` 全局查找。
- 反向证据：`/api/directories`（`routes.py:503-542`）只是可选地按 `tape_id`/`parent_id` 过滤，**没有任何 join/union 或"合并同名节点"的查询逻辑**。
- 文档确认：`docs/FEATURES.md:79`、`docs/CHANGELOG.md:90,95` 都写着"统合多张磁带的目录结构""统一目录结构（多张磁带合并显示）"。

→ 一句话：**`Directory` 是一棵物理全局树，多盘共享同一批节点；`tape_id` 只是"首次创建它的那盘"的残留标记，语义上已经失效。** 唯一的正确信息在 `File.tape_id`。

### 4.2 "同一个番号在不同磁带"怎么表达

| 层 | v1 能不能表达 | 说明 |
|---|---|---|
| 文件级 | ✅ 能 | `File` 每行带 `tape_id`，同名文件在 A/B 两盘 = 两行不同 `File`（`routes.py:439-448`） |
| 目录级 | ❌ 不能 | 共享节点，没有 `directory↔tape` 多对多表；无法回答"哪些盘有这个目录" |
| 影片级 | ❌ 不存在 | v1 没有影片概念 |

### 4.3 对 v2 的建模要求

- 一个 `LibraryTitle`（一个番号）↔ N 个 `LibraryFile`，**N 天然支持"同一部在多盘/多版本/同时在 NAS 和磁带"**。这正是 `PLAN.md:80` 的意图，代码事实支持它。
- `LibraryTitle` 上的"可播放/有副本"这类聚合字段必须**物化缓存**（denormalized），否则每次列表页都要 join 聚合 2~4 万行。建议 `LibraryTitle.file_count / tape_count / nas_count / last_seen_at`。
- 同一个内容在 NAS 和磁带各一份：靠 `LibraryFile.volume_id` 区分（两个 volume），可选再用 `sha256` 交叉校验；**不要**做全局 `UNIQUE(sha256)`（磁带是唯一副本 + 允许重复收藏）。
- 若要把 v1 的 `File` 物化成 `LibraryFile`，**join 路径必须走 `file.tape_id`**，绝不能用 `directory.tape_id`（§4.1）。

---

## 5. 磁带状态机与启动重置（批量盘点的坑）

### 5.1 实际状态机

```
status ∈ { 'unmounted'(默认) , 'mounted' , 'ejected' }   # 无 CHECK 约束，字符串随意写
```

| 迁移 | 触发点 | 代码 |
|---|---|---|
| → `mounted` | `POST /api/tape/mount` 成功 | `routes.py:120-134`（首次挂载还会新建 Tape，`capacity="2.5TB"` 硬编码 129） |
| → `unmounted` | `POST /api/tape/unmount` 成功 | `routes.py:192-203` |
| → `ejected` | `POST /api/tape/eject` 成功 | `routes.py:253-264` |
| **全部 → `ejected`** | **每次进程启动，无条件、无差别** | `run.py:17-26`（`Tape.query.update({'status':'ejected'})`） |

### 5.2 与"批量盘点队列"直接冲突的点

1. **启动即清空状态**：`run.py:21` 把所有磁带（含真实在挂的）刷成 `ejected`。容器重启后 `/api/tape/current`（`routes.py:605-608`：先找 `mounted`，再找"非 ejected 的最新一条"，否则 404）必然 404 → 前端 `tapes.html:104-137` 落到"未挂载"分支，给用户显示**"挂载"+"弹出"两个按钮**，此时点弹出就是对着空 drive 发 `ltfs -o release_device`。**盘点队列绝不能拿 `Tape.status` 判断"当前盘"**，必须自己维护 `audit_run.current_tape` 并做挂载期校验（`os.path.ismount` + barcode 二次确认）。
2. **没有 drive/设备资源概念**：设备路径来自 `config.py:9`（`LTFS_DEVICE`）或 `LTFS_DEVICE_PATH` 属性，全局唯一，`ltfs_tool.py:8-13` 每次实例化都读同一个。`routes.py` 里 `get_device_list`（321-344）**从未被用于选设备**。→ 批量盘点只能"单 drive 串行"，需要显式互斥；未来上自动换带机（`mtx`）时也没有挂载点。
3. **条码 `UNKNOWN` 会串盘**：`ltfs_tool.py:144-168` 三级兜底（ltfs 输出正则 → `.volume_label` 文件 → 设备序列号），全失败返回 `'UNKNOWN'`；`tape.barcode` 唯一（`models.py:14`）→ 多盘落到同一行。**盘点队列必须在入库前强制确认条码**（扫描前 `_get_barcode()` 再取一次并和 UI 提交的预期条码比对，不一致就中止并告警），否则 40 盘盘点会互相覆盖。
4. **命令超时不 kill 子进程**：`command_executor.py:52-59` 用 `thread.join(self.timeout)`，超时后返回 `(1, '', '命令执行超时')`，但 `Popen` 出来的 `ltfs` **还在跑**，可能几十秒后成功挂载。队列若据此判失败继续下一盘 → 双挂载/设备忙。**队列实现必须：失败后先 `umount` 且 `os.path.ismount` 复查，确认干净再继续。**
5. **`unmounted` 是伪状态**：只有 unmount 后存在，重启即消失；`ejected` 才是持久态。`tapes.html:106` 也只在 `mounted|unmounted` 时渲染当前盘。→ 任何"重新挂载上次的盘"的断点续跑逻辑不能依赖 `unmounted`。
6. **状态写入与操作日志用"副作用"关联**：`routes.py:206`（卸载后取"最新 unmounted 的盘"写操作日志）、`267`（取"最新 ejected 的盘"）——不是用请求里的 `tape_id`，多盘并存时会记到错的盘上。
7. **进度只存内存**：`command_executor.py:74-126`，进程结束即丢；`PLAN.md:108` 要求"断点续跑"，**必须落库**（建议 `AuditRun` + `LibraryFile.seen_at` 天然幂等，重跑只补不重）。
8. **单盘 4 步顺序**：`mount → scan → umount → eject`。v1 的 UI 顺序也是先卸载再弹出（`tapes.html:146-157`）；`ltfs_tool.py:58` 的 eject 命令 `ltfs -o devname=... -o release_device` 没有指定挂载点，**在已挂载状态下直接 eject 行为未定义** → 队列必须严格四步、每步校验。
9. **`debug=True` 会起两份进程**：`run.py:47` 硬编码 `debug=True`，Werkzeug reloader 会 fork 一个子进程。**启动期拉起的后台盘点/刮削 worker 会跑两份。** v2 若需要在应用内起 worker：要么改 `use_reloader=False`（动 `run.py`），要么把 worker 拆成独立进程（推荐，`scripts/worker.py` + 独立容器/supervisor，`run.py` 零改动）。
10. **无并发保护**：`POST /api/tape/mount` 没有任何锁（`routes.py:101-180`），两个请求会同时对同一个挂载点 `ltfs` → 第二份失败或产生脏挂载。v2 队列需全局锁。

---

## 6. LTFS 封装细节（`app/utils/ltfs_tool.py`）

| 能力 | 命令（源码行） | 退出码/超时处理 | 备注 |
|---|---|---|---|
| 挂载 | `ltfs -o devname={device} {mount_point}`（`ltfs_tool.py:18`） | `rc==0` → success，否则把 `stderr or stdout` 塞进 message（30-36） | 先 `os.makedirs(mount_point, exist_ok=True)`（16）；挂载成功后 `_get_barcode` 三级兜底（144-168） |
| 卸载 | `umount {mount_point}`（39） | 同上（42-55） | 不检查是否真的 mount，失败信息直接透传 |
| 弹出 | `ltfs -o devname={device} -o release_device`（58） | 同上（61-74） | **不带挂载点**；依赖先 umount |
| 设备列表 | `ltfs -o device_list`（77） | 正则 `/dev/sg\d+` 解析（205-224） | 解析失败时**伪造**一条 `HP LTO-6 Drive`（219-224）→ 前端看到的是假数据 |
| 设备信息 | `ltfs -o devname={device} -o device_info`（97） | 解析 Model/Serial/Firmware（226-249），失败返回 `UNKNOWN` | 被 `_get_barcode` 方式3 复用（162-164） |
| 设备存在 | `os.path.exists(device_path)`（117） | — | 仅查 `/dev/sgNN` 文件，不代表磁带在机/在线 |
| 挂载状态 | `os.path.ismount(mount_point)`（131） | — | **v1 的 routes 从未调用**（grep 无引用），扫描时才用（252） |
| 扫描 | `os.walk`（261-287） | 不捕获异常 | 跳过 `.` 开头（266,274）；**每个文件**调 `_get_barcode()`（286）；不排序、不处理符号链接/权限错误（一个 `os.stat` 抛 `PermissionError` 会让整个接口 500） |

**入口配置来源（三个不一致，v2 必须统一）**：

| 期望变量 | 代码实际读取 | 现状 |
|---|---|---|
| `LTFS_DEVICE_PATH` | `config.py:9` → `os.environ.get('LTFS_DEVICE')` | **compose 里设的 `LTFS_DEVICE_PATH` 完全不生效**（`docker-compose.yml:14`），只因为默认值恰好相同才没暴露 |
| `LTFS_MOUNT_POINT` | `config.py:10` → `os.environ.get('LTFS_MOUNT')` | 同上（`docker-compose.yml:15`、`.env.production:6` 均无效） |
| `LTFS_TIMEOUT` | `config.py:11` 定义了，但 `routes.py:108` 只写 `LTFSProvider()`，`ltfs_tool.py:8` 默认 `timeout=300` | **`LTFS_TIMEOUT` 死代码** |
| `.env` 文件 | `requirements.txt:3` 装了 `python-dotenv`，但全仓 **没有任何 `load_dotenv()` 调用** | `.env` / `.env.example` / `.env.production` 全部是摆设 |

**v2 要做的**：新增 `config.py` 里的 NAS/刮削配置时，**同一套变量名在 compose、代码、文档三处对齐**；顺手把 `LTFS_DEVICE_PATH`/`LTFS_MOUNT_POINT` 两种名字都读（向后兼容），并让 `LTFS_TIMEOUT` 真正传到 `LTFSProvider(timeout=...)`。这两处属于"改 bug"，但要单独提交、单独验证，别和 v2 功能混在一起。

---

## 7. Docker 部署事实

| 项 | 内容（行号） |
|---|---|
| 基础镜像 | `debian:stable`（`Dockerfile:2`），单阶段 |
| LTFS 安装 | **从源码编译**：装 ≈20 个 build 依赖（`Dockerfile:15-41`）→ `git clone https://github.com/LinearTapeFileSystem/ltfs.git`（44）→ `./autogen.sh && ./configure --prefix=/usr && make && make install && ldconfig`（47-53）→ `ltfs --version` 自检（56） |
| 其它 apt | `sg3-utils`、`mergerfs`、`sqlite3`、`libfuse2`、`udev`（16-24）—— `sg3-utils`/`mergerfs` 装了但代码里**没用到**（自动换带/合并没有实现） |
| Python | `pip3 install --break-system-packages -r requirements.txt`（60） |
| 目录 | `/app/instance/backups`、`/app/app/data/virtual_tape`、`/media/tape`（64-66） |
| 启动 | `CMD python3 run.py`（73），`EXPOSE 5001`（68） |
| 宿主权限 | `privileged: true`（`docker-compose.yml:9`）+ `devices: /dev/sg10`（20-21）；`docs/DEPLOYMENT.md:349-365` 说明需要 `chmod 666 /dev/sg*` 或 privileged |
| 卷 | `./instance:/app/instance`（17）、`/mnt:/mnt`（18）、命名卷 `tape-media:/media/tape`（19） |
| 健康检查 | `curl -f http://localhost:5001/health`（23-28）→ 命中 `routes.py:1105-1120` |
| 平台 | `platform: linux/amd64`（7，Apple Silicon 上走模拟） |

**对 v2 的含义**：
- NAS 要挂进容器：`docker-compose.yml:16-19` 加 `- /mnt/user/av:/mnt/av:ro`（Unraid 路径）。**关键红利**：`/api/browser/list` 的安全白名单是 `path.startswith('/mnt')`（`routes.py:1227`），**把 NAS 挂到 `/mnt/...` 下，v1 的文件浏览器 + 容器↔磁带传输立刻就能浏览 NAS、并且能直接把 NAS 文件拷进磁带，零代码改动**（`routes.py:1299-1320` 的传输接口不做路径校验，`file_transfer.py` 只是 `os.walk` + 分块 copy）。这是"NAS 文件源"的最小可用切入点，建议先验证它，再谈 v2 索引。
- 刮削要出网：容器默认继承宿主网络；Unraid 上需确认容器能出外网/走代理（`PLAN.md:153` 的待确认项）。
- 封面存储：建议放宿主机挂载卷（如 `/mnt/user/av/.art`），**不要**放 `./instance`（备份脚本会连 DB 一起 copy，见 §13）。
- 编译 LTFS 的镜像很大且依赖网络（GitHub clone，`Dockerfile:44`）→ v2 改 Dockerfile 要小心别把已有的可复现构建搞坏；建议**不改 Dockerfile**，NAS 相关全走 compose 挂载 + 环境变量。

---

## 8. 前端结构与"加新页面"成本

### 8.1 `base.html` 的真实结构

- 只有一个布局模板 `app/templates/base.html`（174 行）：CDN 引 Bootstrap 5.3 CSS（7）与 JS（165）、引 `slide-dialog.css/js`（8,166）。
- **只有两个 block**：`{% block content %}`（160）、`{% block scripts %}`（172）。
- 内联 `<style>`（9-116）定义了侧边栏、`.card`、`.status-badge` 及 3 个状态色（74-87）、`.progress-bar`、`.table-hover`。
- 侧边栏导航是**手写 6 个 `<li>`**（125-156），每项用 `{% if request.path == '/xxx' %}active{% endif %}` 判断高亮：`/`、`/tapes`、`/files`、`/logs`、`/backup`、`/file-browser`。
- 主区 `col-md-9/col-lg-10`（159）。

### 8.2 已存在的 bug（加页面之前必须知道）

- `file_browser.html:5-7` 用了 `{% block extra_css %}` 引 `css/file_browser.css`，但 **`base.html` 没有定义 `extra_css` block** → Jinja 静默忽略 → **`file_browser.css`（66 行）从未被加载**，右侧面板样式实际是浏览器默认。
- `files.html:3` 的 `{% block title %}` 同样无效（base 没有 title block），全站 `<title>` 永远是 "LTO-6 磁带管理系统"（`base.html:6`）。
- → **v2 加页面前，先给 base.html 补 2 个 block（约 2 行）**，成本极低、收益立竿见影，且能顺手修复 v1 的这个隐性 bug（属于"不破坏现有功能"的范围内）。

### 8.3 可复用度评估

| 组件 | 可复用度 | 说明 |
|---|---|---|
| 侧边栏 / 卡片 / 表格 / 徽章 / 进度条 / 弹窗 | ★★★★☆ | 纯 Bootstrap 类 + base 内联 CSS，直接抄 class 即可 |
| 状态色 | ★★☆☆☆ | 只有 `mounted/unmounted/ejected` 三色（`base.html:74-87`）；`scrape_status` 有 4~5 态，需新增 CSS |
| 页面级布局 | ★★★☆☆ | 每页 `h2` + `card` + `table` 的套路一致，但没有 macro/partial，复制粘贴为主 |
| 分页 / 筛选侧栏 / 封面网格 / 无限滚动 | ☆ | **完全没有**。现有列表全是 `File.query.all()`（`routes.py:70-72`）一次性全渲染，2~4 万条会炸；v2 必须自己写 `limit/offset + count` |
| 前端技术栈 | ★★★★★ | 原生 `fetch` + 模板字符串 + 事件绑定，无打包、无框架。加页面 ≈ 1 个 html + 1 个 js，**不需要 Node 工具链** |
| 独立 JS 模块范式 | ★★★☆☆ | `static/js/file_browser.js`（495 行）是唯一的"class + 生命周期"范式，可照抄结构 |

### 8.4 现有页面里的硬编码（扩展成本点）

- `files.html:263` `colspan="6"`、`files.html:56-77` 服务端渲染 6 列、`files.html:269-277` JS 又渲染一遍 6 列 → **同一张表两套渲染路径**，加列要改两处。v2 不要沿用。
- `/api/files` 每个文件一次 `Tape.query.get()`（`routes.py:584`）→ N+1；`/api/directories` 每个目录一次 `has_children` 查询（`routes.py:523`）→ N+1。目录树大了会明显卡。v2 的接口一次查完再组装。
- `files.html:281-283` 依赖隐式全局 `event`（`event.target.closest`），非标准写法。
- `/api/tape/filesystem` 是 **GET**（`routes.py:346`）却有删除副作用 → v2 扫描一律 POST。

### 8.5 成本估算（以现有风格纯手写计）

| 页面 | 内容 | 估时 |
|---|---|---|
| `/library` 列表 | 封面网格 + 筛选侧栏（演员/片商/系列/题材/年份/介质/字幕/刮削状态）+ 搜索 + 分页 | 1.5 → 2 人日 |
| `/library/<code>` 详情 | 封面/剧照、双语字段、所在介质（磁带条码 / NAS 路径）、字幕、重新刮削、手工编辑 | 1 人日 |
| `/scrape` 队列 | 进度条 + 统计 + 失败重试 + 限速配置（可照抄 `logs.html` 的轮询范式，`logs.html:59-228`） | 1 人日 |
| `/audit` 盘点向导 | 步骤条（挂载→扫描→换带提示）+ 历史记录 | 1.5 人日 |
| `/settings` | 表单 | 0.5 人日 |
| `base.html` 改造（2 个 block + 5 个 nav + 状态色） | | 0.5 人日 |

→ **前端总成本 ≈ 6~7 人日**，其中约 0.5 天是"修 base.html 的 block 缺失"。Bootstrap 组件不需要新引入任何库。

---

## 9. 【重点问题 1】NAS 文件入库：三个方案与推荐

### 9.1 方案 A：把 NAS 伪装成一条 Tape（"虚拟磁带"）

- 做法：给 NAS 根目录建 `Tape(barcode='NAS-UNRAID', status='<什么？>')`，`Directory.tape_id` / `File.tape_id` 指向它。
- 代码零改动，`/api/browser/list` 甚至已经能用（挂到 `/mnt` 下）。
- **否决理由**：① `run.py:21` 启动把它的 `status` 刷成 `ejected`，语义彻底崩；② `/api/tapes`（`routes.py:89-99`）会把它当磁带列出来，`/api/tape/current`（605-608）可能把它当"当前磁带"返回，前端 `tapes.html` 会出现一条假磁带；③ 容量是字符串、`used_space` 由磁带扫描逻辑写；④ 它仍无法表达"同名文件在 NAS 与磁带各一份"的区分（虽然可以用不同 tape_id 区分，语义上只是"另一盘"）。
- 唯一适用场景：**今天先用它验证"NAS 挂载 + 浏览 + 拷进磁带"链路**（不改一行代码），验证完就删掉这条假磁带。

### 9.2 方案 B：`File.tape_id` 改 nullable + 加 `location` 表（单表统一）

- SQLite 必须**重建表**（§2.4），迁移步骤见 §9.5。改完 v1 代码大部分能继续跑（`routes.py:584` 有 `if file.tape_id` 守卫、`files.html:61` 有 `if file.tape`），但：
  - `UNIQUE(location_id, name)` 在 `location_id IS NULL` 时失效（SQLite NULL 语义）→ 还得给 NAS 文件也塞一个 location，那 `tape_id` 的 nullable 就白改了；
  - 仍然没有 `rel_path`、没有 `hash`、`File.id` 仍会重扫漂移 → 影片层仍然不能安全外键；
  - 语义污染：`File` 变成"三种来源混装"，v1 的 `/files` 页会把 NAS 文件按"所在磁带 = -"显示，用户困惑。
- 适用：如果**坚决要"一个文件表"**，且愿意接受上面三条。迁移风险中等（§9.5 有完整步骤）。

### 9.3 方案 C（**推荐**）：v1 表零改动 + 新增 `LibraryVolume` / `LibraryFile` 统一介质索引

```
LibraryVolume   id, kind('tape'|'nas'), ref(barcode 或 'nas:<id>'), name,
                nas_root_id, capacity_bytes, enabled, last_scan_at, last_scan_status
LibraryFile     id, volume_id NOT NULL → LibraryVolume, rel_path NOT NULL,
                name, size, mtime, sha256(可空), code_guess, confidence,
                seen_at, deleted_at(软删), created_at
                UNIQUE(volume_id, rel_path)          ← 唯一键，NULL 不参与
                INDEX(rel_path), INDEX(code_guess)
LibraryTitle    id, code UNIQUE, ...            ← 影片层
（其余 Person/Studio/Series/Tag/TitleTag/Subtitle/ScrapeTask/AuditRun 见 §11）
```

- **唯一键 `(volume_id, rel_path)` 两列都 NOT NULL** → 彻底绕开 SQLite NULL 唯一索引问题；
- `rel_path` 用 POSIX 相对路径（磁带侧 = `Directory.path + '/' + File.name`；NAS 侧 = 相对共享根的路径），**与 v1 的 `Directory.path` 同一套写法**，可直接互转；
- `LibraryFile.id` 稳定（upsert 不换行）→ 影片层、字幕表可以安全外键；
- 迁移成本 = **纯 `CREATE TABLE` + 一次 backfill**，v1 表一个字节不动，回滚 = `DROP` 新表。**这是"不破坏现有功能"的最强保证。**

### 9.4 迁移步骤（方案 C，零 ALTER）

```python
# scripts/migrate_v2.py —— 幂等，可重复执行
import sqlite3, sys
from app import app, db
import app.models_v2          # 关键：必须在 create_all 前 import，否则不建表

V2_VERSION = 2

def migrate():
    with app.app_context():
        db.create_all()       # ① 只建缺失的新表；对已有表"静默跳过"，不会碰 v1 数据
    path = db.engine.url.database
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    v = cur.execute("PRAGMA user_version").fetchone()[0]
    if v >= V2_VERSION:
        print("已是最新"); return
    cur.executescript("""
      -- ② 磁带介质登记：每盘一条 LibraryVolume
      INSERT OR IGNORE INTO library_volume(kind, ref, name, enabled, created_at)
      SELECT 'tape', t.barcode, COALESCE(t.label, t.barcode), 1, CURRENT_TIMESTAMP
      FROM tape t;

      -- ③ 物化：路径必须用 file.tape_id 归属，绝不能用 directory.tape_id
      INSERT OR IGNORE INTO library_file(volume_id, rel_path, name, size, mtime, seen_at)
      SELECT v.id,
             CASE WHEN d.path IS NULL OR d.path = '' THEN f.name
                  ELSE d.path || '/' || f.name END,
             f.name, f.size, f.mtime, CURRENT_TIMESTAMP
      FROM "file" f
      JOIN tape t          ON t.id = f.tape_id
      JOIN library_volume v ON v.kind='tape' AND v.ref = t.barcode
      LEFT JOIN directory d ON d.id = f.directory_id;

      -- ④ v1 侧加固（可选，见 §10）
      CREATE UNIQUE INDEX IF NOT EXISTS ux_directory_path ON directory(path);
      CREATE UNIQUE INDEX IF NOT EXISTS ux_file_tape_dir_name ON "file"(tape_id, directory_id, name);
    """)
    cur.execute(f"PRAGMA user_version={V2_VERSION}")
    conn.commit(); conn.close()
```

要点：
- `db.create_all()` **不会**修改已有表 —— 这正是方案 C 安全的原因，也是"加列"类需求的死刑判决书，必须靠脚本重建；
- `INSERT OR IGNORE` + 唯一索引 = 可重复执行；
- `PRAGMA user_version` 做版本账本，避免引入 Flask-Migrate 依赖；
- ④ 的两个唯一索引**在加之前必须先清理历史重复**（§10.3），否则建索引会失败；
- 建议脚本跑完打印 `library_file` 行数 vs `SELECT COUNT(*) FROM "file"` 做对账。

### 9.5 方案 B 的迁移步骤（若坚持单表，SQLite 重建表完整流程）

**关键前提**：`file.tape_id` 有 FK、被大量 SQL 引用，SQLite **不能** `DROP COLUMN`、**不能**改 NOT NULL → 只能重建。

**方式 1：Alembic batch（若引入 Flask-Migrate）**

```python
# migrations/versions/xxxx_file_nullable.py
def upgrade():
    with op.batch_alter_table('file', recreate='always') as b:   # recreate 必填，SQLite 才会重建
        b.alter_column('tape_id', existing_type=sa.Integer(), nullable=True)
        b.add_column(sa.Column('location_id', sa.Integer(),
                               sa.ForeignKey('library_volume.id'), nullable=True))
        b.add_column(sa.Column('rel_path', sa.Text(), nullable=True))
        b.create_unique_constraint('uq_file_volume_relpath',
                                   ['location_id', 'rel_path'])   # 注意 NULL 语义失效
    # 数据回填：给现有磁带文件补 rel_path、补 location_id
```
配置必须是 `context.configure(render_as_batch=True)`（SQLite 必需）。`recreate='always'` 会走“建新表→拷数据→换名”，本质就是 §2.4 那段流程的自动化。

**方式 2：不引入 Alembic，用 `RENAME` + `create_all`（更省事，推荐给本仓库）**

```sql
-- 0) 停机（先备份 instance/tape_metadata.db）
PRAGMA foreign_keys=OFF;
BEGIN;
-- 1) 旧表改名（SQLite ≥3.25 会同步更新其它表对它的 FK 引用；此处无表引用 file，安全）
ALTER TABLE "file" RENAME TO file_v1_legacy;
-- 2) 此时把 models.py 里 File.tape_id 改成 nullable=True、加 location_id/rel_path，
--    然后执行 python3 -c "from app import app,db; from app import models; \
--        app.app_context().push(); db.create_all()"  → 自动建出新结构的 file
-- 3) 拷数据（显式列名，别用 SELECT *）
INSERT INTO "file"(id, tape_id, directory_id, name, size, mtime, atime, ctime, created_at)
SELECT id, tape_id, directory_id, name, size, mtime, atime, ctime, created_at
FROM file_v1_legacy;
-- 4) 补 location_id
UPDATE "file" SET location_id = (SELECT v.id FROM library_volume v
                                 JOIN tape t ON t.barcode = v.ref
                                 WHERE t.id = "file".tape_id);
-- 5) 校验
SELECT (SELECT COUNT(*) FROM file_v1_legacy) AS old_n, (SELECT COUNT(*) FROM "file") AS new_n;
-- 6) 确认无误后
DROP TABLE file_v1_legacy;
CREATE UNIQUE INDEX ux_file_volume_relpath ON "file"(location_id, rel_path);
COMMIT;
PRAGMA foreign_key_check;   -- 必须空结果
```

**注意**：这种方案下 v1 的 `File.tape_id` 语义从"必有"变"可选"，`routes.py` 里所有 `filter_by(tape_id=...)` 的删/查逻辑仍然成立（磁带文件都有 tape_id），风险可控但**远大于方案 C**，且要连带改 `models.py`（v1 唯一需要改 schema 的地方）。**只在"必须单表"时选它。**

---

## 10. 【重点问题 2 续】唯一约束与去重方案

### 10.1 现状

- 全库仅 `tape.barcode`（`models.py:14`）、`file_transfer.operation_id`（`models.py:76`）两个 unique，`directory.path` / `file.*` 全裸（§2.2）。
- 目录去重靠应用层 `.first()`（`routes.py:391`），非原子。

### 10.2 给 v1 加约束（DDL 即可，不重建表）

```sql
-- 目录：path 全局唯一（与"统一根目录 path=''"的设计一致）
CREATE UNIQUE INDEX ux_directory_path ON directory(path);
-- 文件：同一盘、同一目录下同名唯一
CREATE UNIQUE INDEX ux_file_tape_dir_name ON "file"(tape_id, directory_id, name);
```

- **收益**：把 §3.3 的"并发双扫产生重复"从**静默脏数据**变成**显式 `IntegrityError`（500）**，至少能发现；目录树不可能再长出同 path 双节点。
- **代价/前提**：
  1. 建索引前必须先清理历史重复（`directory` 按 path 合并：`UPDATE "file" SET directory_id=<keep> WHERE directory_id IN (<dup>)`、`UPDATE directory SET parent_id=<keep> WHERE parent_id IN (<dup>)`、再删重复行；`file` 按 `(tape_id,directory_id,name)` 去重）；
  2. 加索引后，**并发扫描会从"重复"变成"500"** —— 需要配套单例锁或把扫描改成 upsert；
  3. 若将来开 `PRAGMA foreign_keys=ON`，`routes.py:365` 的自引用批量删除会失败（§14 R6）。
- **安全做法**：v2 迁移脚本里"先 dedupe、后建索引"，并且只在**确认没有并发扫描**的窗口执行。

### 10.3 从根上解决：upsert 替代 delete+insert

v2 扫描器（`app/services/media_index.py`）必须这样写，而不是复刻 `routes.py:365-366`：

```python
from sqlalchemy.dialects.sqlite import insert

def upsert_file(session, volume_id, rel_path, name, size, mtime):
    stmt = insert(LibraryFile).values(
        volume_id=volume_id, rel_path=rel_path, name=name,
        size=size, mtime=mtime, seen_at=now())
    stmt = stmt.on_conflict_do_update(
        index_elements=['volume_id', 'rel_path'],
        set_={'name': stmt.excluded.name, 'size': stmt.excluded.size,
              'mtime': stmt.excluded.mtime, 'seen_at': stmt.excluded.seen_at,
              'deleted_at': None})
    session.execute(stmt)
```

配套：扫描开始记 `scan_started_at`；结束后
`UPDATE library_file SET deleted_at=now() WHERE volume_id=? AND seen_at < scan_started_at AND deleted_at IS NULL`。
→ 幂等、可断点续跑、不丢 `id`、天然支持增量。

---

## 11. 【重点问题 6】推荐的 v2 文件布局

### 11.1 为什么用 Blueprint

v1 的 43 个路由全挤在 `app/routes.py`（1411 行），全部 `@app.route`、无 Blueprint、无 service 层。v2 再加片库/筛选/详情/刮削/盘点/设置 ≈ 30+ 路由 → 单文件会到 3000 行。Blueprint 与 v1 的 `@app.route` **可以共存**（`app.register_blueprint` 与 `@app.route` 不冲突，只要 URL 前缀不撞）。v1 的模板 `url_for('static', ...)` 在 Blueprint 页面上照常工作（static 是 app 级端点）。

### 11.2 目录树

```
av-archive/
  app/
    __init__.py              【改 +6 行】import models_v2 + register_blueprints(app)
    models.py                【不动】v1 六表原样
    models_v2.py             【新】LibraryVolume / LibraryFile / LibraryTitle /
                                   Person / TitleCredit / Studio / Series / Tag /
                                   TitleTag / Subtitle / ScrapeTask / AuditRun / AppSetting
    routes.py                【不动】（可选 +1 行同步钩子，见 §12）
    blueprints/
      __init__.py            【新】register_blueprints(app): 
                                   library / sources / scrape / audit / settings
      library.py             【新】/library, /library/<code>, /api/library/list|detail|filters
      sources.py             【新】/sources, /api/sources/list|scan|progress
      scrape.py              【新】/scrape, /api/scrape/start|status|retry|providers
      audit.py               【新】/audit, /api/audit/start|status|next|finish
      settings.py            【新】/settings, /api/settings
    services/
      __init__.py
      media_index.py         统一 upsert（§10.3）+ 音量/路径归一化
      tape_source.py         适配 v1：调用 LTFSTool.scan_filesystem()（只读复用）
      nas_source.py          os.walk 增量扫描（mtime/size 短路）
      code_parser.py         番号抽取 + 归一化 + confidence
      matcher.py             LibraryFile → LibraryTitle 挂接
      manifest.py            _manifest/<barcode>.json 读写（DB 可重建）
      inventory.py           盘点队列 worker：状态机 + 全局锁 + 四步校验
      art.py                 封面/剧照下载、缩略图、路径缓存
      scrape/
        __init__.py          注册表
        base.py              Provider 抽象（rate limit / retry / 熔断）
        javbus.py javdb.py ... 【新】具体实现
    templates/
      base.html              【改 ~20 行】+block title/extra_css +5 个 nav + 状态色
      library/list.html  detail.html  _card.html  _filters.html   【新】
      sources/list.html                                         【新】
      scrape/queue.html                                         【新】
      audit/wizard.html                                         【新】
      settings/index.html                                       【新】
    static/
      js/library.js  sources.js  scrape.js  audit.js            【新，照抄 file_browser.js 范式】
      css/library.css                                           【新】
  scripts/
    migrate_v2.py            【新】§9.4
    worker.py                【新】独立进程跑盘点/刮削队列（不改 run.py 的关键）
  config.py                  【改】+NAS_ROOTS / ART_DIR / SCRAPER_* / 修 LTFS 变量名
  requirements.txt           【改】+requests +beautifulsoup4 +lxml +Pillow
  docker-compose.yml         【改】+NAS 卷 +环境变量 +worker 服务
  Dockerfile                 【不动】
```

### 11.3 磁带侧扫描如何"零改动复用" v1

**不要在 v2 里复刻 mount/eject 逻辑，也不要改 `routes.py`。** 直接：

```python
# app/services/tape_source.py
from app.utils.ltfs_tool import LTFSTool
from config import Config

def scan_mounted_tape():
    tool = LTFSTool(device_path=Config.LTFS_DEVICE_PATH,
                    mount_point=Config.LTFS_MOUNT_POINT,
                    timeout=Config.LTFS_TIMEOUT)   # 顺手修好 LTFS_TIMEOUT 死代码
    res = tool.scan_filesystem()                # 只读，返回 dict，不碰 DB
    if not res['success']:
        raise RuntimeError(res['message'])
    return res                                  # 直接喂给 media_index.upsert
```

`ltfs_tool.scan_filesystem()`（`ltfs_tool.py:251-296`）是**只读**的（只有 `os.walk` + `stat`），v2 复用它对 v1 零影响；v1 自己的 `/api/tape/filesystem` 继续写自己的 `Directory/File`，两条路互不干扰。
**建议**：v2 的挂载/弹出仍然走 v1 的 API（`POST /api/tape/mount|unmount|eject`），前端复用 `tapes.html` 的按钮逻辑，避免两套 LTFS 调用互相打架。

### 11.4 不需要动的文件清单（承诺不破坏 v1 的边界）

| 类别 | 文件 |
|---|---|
| v1 后端逻辑 | `app/routes.py`（0 行）、`app/models.py`（0 行）、`app/utils/ltfs_tool.py`、`app/utils/command_executor.py`、`app/utils/file_transfer.py`、`app/utils/error_monitor.py`、`app/utils/timezone.py` |
| v1 前端 | `templates/index.html` `tapes.html` `files.html` `logs.html` `backup.html` `errors.html` `file_browser.html`、`static/css/file_browser.css` `slide-dialog.css`、`static/js/file_browser.js` `slide-dialog.js` |
| v1 运维 | `Dockerfile`、`init_db.py`、`init_prod_db.py`、`run.py`（见 §12 的唯一例外）、`docs/*.md` 全部 |
| 数据 | `instance/tape_metadata.db` 的六张表结构 |

---

## 12. 需要改动 v1 的精确清单（最小 diff）

按侵入性从低到高；**只有 ①②④ 是必须的**，合计 ≈ 30 行纯增量。

| # | 文件:行号 | 改动 | 行数 | 必要性 |
|---|---|---|---|---|
| ① | `app/__init__.py:9` 之后（`db = SQLAlchemy(app)` 与 `from app import routes` 之间） | `from app import models_v2` + `from app.blueprints import register_blueprints` + `register_blueprints(app)` | +6 | **必须**（否则 `create_all` 不建新表，且页面 404） |
| ② | `app/templates/base.html:6` | 把硬编码 title 改为 `{% block title %}LTO-6 磁带管理系统{% endblock %}` | 1 | **必须**（否则所有新页面标题都一样，且修掉 `files.html:3` 的失效 block） |
| ③ | `app/templates/base.html:8` 附近 | 加 `{% block extra_css %}{% endblock %}` | 1 | **强烈建议**（顺带修复 `file_browser.html:5-7` 从未生效的问题） |
| ④ | `app/templates/base.html:125-156` | 侧边栏 `<li>` 追加 4~5 项：片库/文件源/刮削/盘点/设置 | +15~20 | **必须** |
| ⑤ | `config.py:9-11` | 新增 `NAS_ROOTS` / `ART_DIR` / `SCRAPER_*`；`LTFS_DEVICE_PATH` 同时兼容读 `LTFS_DEVICE` 与 `LTFS_DEVICE_PATH`；`LTFS_TIMEOUT` 传给 `LTFSProvider` | +10 | 建议（配置项） |
| ⑥ | `requirements.txt:1-4` | `+requests` `+beautifulsoup4` `+lxml` `+Pillow`（`+alembic` 仅在选方案 B 时） | +4 | **必须**（刮削） |
| ⑦ | `docker-compose.yml:16-19` | 加 `- /mnt/user/av:/mnt/av:ro`（NAS）、封面卷、环境变量；**注意变量名用 `LTFS_DEVICE` 而非 compose 现在的 `LTFS_DEVICE_PATH`** | +6 | **必须**（部署） |
| ⑧ | `app/routes.py:473` 之后（扫描成功写 Operation 的后面） | **可选** 加 `library_sync.sync_tape(tape.id)` 一行，让 v1 按钮扫描后自动同步到 v2 | +2 | 可选（推荐用 POST 钩子/定时任务替代，保持 0 改动） |
| ⑨ | `run.py:47` | **仅在 v2 要在应用内起后台 worker 时**：`debug=True` → `debug=False` 或 `use_reloader=False`（否则 worker 跑两份） | 1 | 条件必需。**更推荐**：不改 `run.py`，用 `scripts/worker.py` 独立进程/compose 第二服务 |

> 推荐路线：**只做 ①②④⑥⑦ + ③⑤**，`routes.py` 与 `run.py` 都属于"原则上不动"。这样 v1 的 43 个端点、6 张表、所有页面行为完全不变，出问题时的回滚方式 = 撤掉 Blueprint 注册 + `DROP` 新表。

---

## 13. 已发现的 v1 缺陷（v2 必须知道，不一定要修）

| # | 缺陷 | 位置 | 影响 |
|---|---|---|---|
| D1 | **备份恢复写错路径**：备份从 `/app/instance/tape_metadata.db` 复制（`routes.py:670`），恢复却写 `/app/tape_metadata.db`（`routes.py:787`、`868`） | `routes.py:653-891` | 恢复接口提示"成功"但**实际数据库没变**，还多出一个无人使用的文件。**v2 做迁移/备份前必须绕过这个接口，直接 `cp` instance 下的 db** |
| D2 | `DATABASE_URL` 环境变量无效：`app/__init__.py:6` 硬编码 `sqlite:///tape_metadata.db`，从不读 `config.py:5` | `app/__init__.py:4-9` | 迁移脚本不能假设 DB 路径，必须 `db.engine.url.database` 反查 |
| D3 | `.env` 全链路不生效（装了 `python-dotenv` 但没有 `load_dotenv()`） | `requirements.txt:3`，全仓 grep 为空 | 所有 `.env*` 文件都是文档性质；v2 若要读 env，必须显式 `load_dotenv()` 或只用 compose `environment` |
| D4 | compose 的环境变量名与代码不匹配（`LTFS_DEVICE_PATH` vs 代码读的 `LTFS_DEVICE`） | `docker-compose.yml:14-15` vs `config.py:9-10` | 靠默认值相同才没炸；v2 加 NAS 配置时若照抄这个模式会踩同一个坑 |
| D5 | `LTFS_TIMEOUT` 是死代码 | `config.py:11` vs `routes.py:108` | 磁带慢时固定 300s 超时，无法调 |
| D6 | 命令超时不杀子进程 | `command_executor.py:52-57` | 假失败、残留 `ltfs` 进程、可能双挂载 |
| D7 | 扫描接口是 GET 且有删除副作用 | `routes.py:346` | 监控/预取可能触发数据删除；v2 必须 POST |
| D8 | 嵌套 `app.app_context()` 会分裂 SQLAlchemy session scope | `routes.py:355` | 目前靠显式 commit 侥幸正确；v2 不要抄 |
| D9 | `_parse_device_list` 解析失败时**伪造**硬件信息 | `ltfs_tool.py:219-224` | 前端显示的设备型号是假的 |
| D10 | `_get_barcode()` 在扫描循环里逐文件调用 | `ltfs_tool.py:286` | 4 万文件 = 4 万次 `open`，明显拖慢 |
| D11 | `scan_filesystem` 不捕获 `os.stat`/权限异常、不处理符号链接 | `ltfs_tool.py:261-287` | 一个坏文件让整次扫描 500，且可能导致 walk 死循环 |
| D12 | 操作日志用"副作用"归属磁带 | `routes.py:206,267` | 多盘时日志记错盘 |
| D13 | `FileTransfer` 每 1MB 一次 `db.session.commit()` | `file_transfer.py:182,212` | 大文件传输时 SQLite 写放大严重；v2 拷大文件到磁带会感受到（进度可改为每 16~64MB 提交） |
| D14 | 无 CSRF、无鉴权、传输接口不校验路径 | `routes.py:1299-1320` | 内网工具可接受；v2 若加上"删除/移动 NAS 文件"必须补校验（`/api/browser/list` 有 `/mnt` 白名单 `routes.py:1227`，传输接口没有） |
| D15 | `operation_id` 只存唯一约束，`Operation` 没有索引 | `models.py:48-57` | 日志表 1000 行分页（`routes.py:77,1149`）暂时没问题 |

---

## 14. 风险与对策

| # | 风险 | 触发点 | 对策 |
|---|---|---|---|
| R1 | 迁移脚本在已有重复数据上 `CREATE UNIQUE INDEX` 失败 | `directory`/`file` 历史重复（§10.2 前提） | 先 dedupe 再建索引；脚本分两步、每步可单独回滚 |
| R2 | 开 `PRAGMA foreign_keys=ON` 后 v1 扫描崩 | `routes.py:365` 自引用 delete，无 `ON DELETE CASCADE` | **v2 不要全局打开 FK**；要开就给 `directory.parent_id` / `file.directory_id` 补 `ondelete='CASCADE'`（需建索引，见 R1）|
| R3 | 容器重启把所有磁带刷成 `ejected`，盘点队列"当前盘"丢失 | `run.py:17-26` | 队列状态全部落库（`AuditRun`），重启后进"待人工确认"态，别自动续 |
| R4 | 条码解析到 `UNKNOWN`，多盘共用一行 → 盘点串盘 | `ltfs_tool.py:168` + `models.py:14` | 盘点前强制"预期条码 vs 实际条码"比对，不等则中止；`UNKNOWN` 只允许 1 盘并显式告警 |
| R5 | 应用内后台 worker 被 Flask reloader 起两份 | `run.py:47` `debug=True` | worker 独立进程（`scripts/worker.py`），`run.py` 零改动 |
| R6 | SQLite 单写者：盘点扫描（大量 upsert）+ 刮削写库 + 页面读并发 | — | 开 `PRAGMA journal_mode=WAL`、`busy_timeout=5000`；所有写操作串行化到单一 worker；列表页只读 |
| R7 | 2~4 万条 `LibraryFile` 的 upsert 性能 | — | 每 500~1000 行一个事务、`executemany`；扫描期间不写 `Operation` 明细（只在结束时写一条） |
| R8 | 封面 12GB 占满 | `PLAN.md:145` | 缩略图落盘 + 原图可选；路径放 NAS 卷，别放 `instance/`（见 D1，备份会连 DB 一起 copy） |
| R9 | 磁带是唯一副本，DB 丢失 | — | 每盘根目录写 `_manifest/<barcode>.json`（`PLAN.md:19`）。注意：v1 扫描只跳过 `.` 开头（`ltfs_tool.py:266`），`_manifest` 目录**会被 v1 索引**成普通文件 → 建议用 `.manifest/`（点开头）避开，或接受它出现在 v1 文件列表里 |
| R10 | 前端 Bootstrap 走 CDN，Unraid 无外网时整站掉样式 | `base.html:7,165` | v2 把 bootstrap 5.3 拷进 `static/vendor/`，改 2 行引用（顺带修 v1 隐患） |

---

## 15. 实施顺序（建议）

| 阶段 | 内容 | 验收 | 估时 |
|---|---|---|---|
| M0 | 只读验证：compose 加 NAS 卷到 `/mnt/nas`，用 v1 `/file-browser` 浏览 NAS 并拷 1 个文件到磁带（**零代码**） | 链路通，确认 NAS 挂载/权限/速度 | 0.5d |
| M1 | `models_v2.py` + `scripts/migrate_v2.py` + backfill + `base.html` 两个 block；跑通"新表存在、v1 全页面不报错" | v1 全端点回归 + backfill 行数对账 | 1d |
| M2 | `nas_source.py` 增量扫描 + `media_index` upsert + `/library` 列表（先只显示文件，无影片层） | 1 万条规模下列表/搜索 < 1s（分页） | 2d |
| M3 | 番号解析（`code_parser`，先用真实目录抽样测命中率）+ `LibraryTitle` + 详情页 | 命中率报告 ≥ 目标值（`PLAN.md:89`） | 2d |
| M4 | 刮削链路（provider 抽象 + 1 个源 + 离线落库 + 封面） | 100 部跑通，失败可重试 | 2~3d |
| M5 | 盘点队列（`AuditRun` + 四步校验 + 条码强确认 + 落库断点） | 扫 3 盘真实磁带，中途 Ctrl+C 可续跑 | 2d |
| M6 | 筛选/双语/字幕/设置/部署文档 | 全量回归 | 2~3d |

---

## 16. 对 `PLAN.md` 的校准建议（基于代码事实）

1. **`PLAN.md:49` "从 v1 扫描结果物化（File+Directory join → LibraryFile）"—— join 路径必须用 `file.tape_id`**，不能用 `directory.tape_id`（后者语义已失效，§4.1）。SQL 见 §9.4。
2. **`PLAN.md:59` 的唯一键 `(source, coalesce(tape_barcode,nas_root_id), rel_path)`**：SQLite 支持表达式索引，但**推荐改成 `volume_id NOT NULL + UNIQUE(volume_id, rel_path)`**。理由：① 避免 `ifnull` 表达式索引在备份/恢复/工具链里的兼容问题；② 你需要"介质列表/容量/最近扫描时间"这些字段，反正要建 `LibraryVolume`；③ `tape_barcode` 存在 `'UNKNOWN'` 碰撞风险（`ltfs_tool.py:168`），有独立 volume 表才能人工纠正。
3. **`PLAN.md:49` "避免双扫描"**：如果 v2 从 v1 的 `File/Directory` 物化，就**必须先有人在 v1 点扫描**，而 v1 扫描的归盘依据是"库里第一条 marked"（`routes.py:357`）——不可靠。**建议 v2 自己扫**（`tape_source.py` 直接复用 `LTFSProvider().scan_filesystem()`，只读），v1 的扫描按钮仅作为"给 v1 页面看"的兼容路径。这样 v2 不依赖 v1 的脏逻辑。
4. **`PLAN.md:77` `AuditRun.tape_barcode`**：保留，但再加 `volume_id`、`scan_started_at`/`scan_finished_at`、`files_seen`、`files_new`、`status`，并用它做断点依据（不要用 `Tape.status`）。
5. **`PLAN.md:144` "SQLite 并发写 → 单写队列 + WAL"**：完全正确，补一句**不要开 `PRAGMA foreign_keys=ON`**（R2）。
6. **`PLAN.md:29` 页面 `/audit`**：v1 的 `/backup`、`/logs`、`/errors` 已经占用了运维类入口，`/audit` 与它们并列即可；新增的 5 个 nav 项要放在 v1 的 6 项之后，避免打乱用户肌肉记忆。
7. **`PLAN.md:124` "原有路由全部保留"**：技术上成立，因为 v2 全部走 Blueprint，v1 的 `@app.route` 不动。

---

## 附：一页速查（给实现者）

```
v1 能复用（不要重写）
  mount/unmount/eject/device-info   → POST /api/tape/{mount,unmount,eject}   routes.py:101,182,243
  只读扫描                          → LTFSTool().scan_filesystem()          ltfs_tool.py:251
  容器↔介质 分块复制                 → transfer_manager                       file_transfer.py:12
  操作日志表/页面                    → Operation + /logs                     models.py:48
  备份文件列表/下载                  → /api/backup/{list,download}（别用 restore，见 D1）
  文件浏览器（NAS 挂到 /mnt 下即可）  → /api/browser/list                      routes.py:1220

v1 不能复用（要新写）
  文件身份（id 会漂移）              → LibraryFile(volume_id, rel_path)
  目录统合（tape_id 语义失效）        → 扁平 rel_path，不要目录树
  磁带状态（重启被刷）                → AuditRun 状态机
  分页/筛选/封面/番号                 → 全新

红线
  1. 不改 v1 六表（方案 C）
  2. 影片层不外键 File.id
  3. 扫描用 POST + upsert + seen_at
  4. 扫描前确认 barcode ≠ UNKNOWN
  5. 开 WAL，不开 foreign_keys
```
