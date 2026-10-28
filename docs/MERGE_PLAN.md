# 合并 ltfs-webui 进 av-archive —— 设计与切换方案

> 目标：不再跑两个容器。av-archive 自己管理 LTFS 设备，同时保留原有片库/元数据/字幕能力。
> 依据：`research/existing-repo.md`（v1 代码分析）+ 本轮对 v1 `ltfs_tool.py` / `Dockerfile` 的复核。

## 1. 边界与假设

- **设备独占**：合并后 `/dev/sg*` 只允许 av-archive 使用；切换时必须先 `docker stop ltfs-webui`，否则双挂载/设备忙。
- **数据不复用 v1 的库**：v1 的 SQLite（Tape/Directory/File）不迁移。磁带内容通过「挂载 → 扫描 → 盘点」重新登记；旧磁带里已有的 `._manifest` 仍可读（兼容），新盘点一律写本地 `data/manifests/`。
- **过渡期可回退**：`AVLIB_TAPE_ADAPTER=http` 仍保留，指回正在跑的 v1 即可；确认稳定后再停 v1。
- **LTFS 版本**：镜像内编译开源 LTFS v2.4.9.0（vendor/ltfs-v2.4.9.0-10523.tar.gz），与 v1 用的 2.5.0-pre 基本兼容；真实挂载行为需上机验证。

## 2. 功能对照

| v1 功能 | 合并后实现 | 状态 |
|---|---|---|
| 挂载 / 卸载 / 弹出 | `tape_ops.mount_tape/unmount_tape/eject_tape` + `/tapes` 页面 | 开发中 |
| 设备列表 / 设备信息 / 条码 | `DirectAdapter.device_list/device_info` + `.volume_label` 兜底 | 开发中 |
| 磁带文件扫描 | 已有 `scanner.scan_tape`（比 v1 更稳：增量/软删/防串盘） | ✅ |
| 文件浏览器（容器↔磁带） | `/files` 双面板 + `transfer.run_transfer`（带路径逃逸校验、`.part` 原子写） | 开发中 |
| 容器↔磁带复制/移动进度 | `Transfer` 表 + Job 队列 + 任务面板 | 开发中 |
| 操作日志 | `TapeOperation` 表 + `/logs` 页面 | 开发中 |
| 备份/恢复 | `backup` 服务 + `/backup` 页面；**不做在线恢复**（停容器→覆盖→启动） | 开发中 |
| 错误监控 | 暂不单独做：Job 失败、TapeOperation 失败、selfcheck 覆盖主要场景 | 不合并 |
| 虚拟磁带模拟 | 不合并：用假 `ltfs` 脚本 + 合成语料做测试 | 不合并 |

## 3. 镜像与设备

- `Dockerfile` 多阶段编译 LTFS（bookworm 基础镜像 + `vendor/` 源码），运行镜像装 `libfuse2/libxml2/libsnmp40/libicu72/libssl3/sg3-utils/mt-st`。
- compose 需要：
  - `privileged: true`（v1 同样要求，LTFS/FUSE 需要）
  - `devices: ["/dev/sg10:/dev/sg10"]`（按实际设备号改，`ltfs -o device_list` 可查）
  - **不要**再把宿主目录绑定到 `/media/tape`（LTFS 在容器内自己挂载）
  - `AVLIB_TAPE_ADAPTER=direct`
- 基础镜像 pin 到 `python:3.12-slim-bookworm`：`trixie` 的包名变了（libicu72 不存在），会导致构建失败。

## 4. 切换步骤（Unraid）

1. 备份 v1 数据（可选）：`docker cp ltfs-webui:/app/instance ./ltfs-v1-backup`
2. 停 v1：`docker compose -f <v1>/docker-compose.yml down`（**必须**，释放设备）
3. 部署合并版：`docker compose -f docker-compose.yml up -d --build`
4. 打开 `/tapes` → 插带 → 「挂载」→ 确认条码 → 「盘点」
5. 全部盘盘完后，在 `/scrape` 跑「番号 → 元数据物化」
6. 观察 1–2 周稳定后，删除 v1 容器/镜像

回退：`docker stop av-archive` → 启 v1 → 把 `AVLIB_TAPE_ADAPTER` 改回 `http`（或干脆不启合并版）。

## 5. 风险

| 风险 | 对策 |
|---|---|
| 内核 FUSE 权限/设备直通失败 | `privileged: true` + 设备直通；先用 `docker run --rm image ltfs --version` 与 `ltfs -o device_list` 验证 |
| 真实 LTFS 命令输出/条码格式与 v1 不一致 | 适配器保留三级条码解析 + `.volume_label` 兜底；未知就中止，不硬猜 |
| 长传输中断 | `.part` 临时名 + 完成后 rename；失败保留已完成文件并记录 Transfer.error |
| 设备号变化（重启后 /dev/sgX 漂移） | 文档要求用 `ltfs -o device_list` 确认后改 compose；后续可加 udev/序列号发现 |
| 与 v1 双跑抢设备 | 文档强制先停 v1；adapter=http 仅作回退模式 |

## 6. 验收标准

- 容器内 `ltfs --version` 正常；
- `/tapes` 能显示设备状态，挂载/卸载/弹出产生 TapeOperation 记录；
- 插一盘真实磁带：挂载 → 条码校验 → 盘点 → 写本地 manifest → 卸载（不弹出）；
- `/files` 能浏览容器与磁带、复制小文件并看到进度；
- `/backup` 能创建/校验/下载备份；
- 全量 pytest 通过；`scripts.selfcheck` 在容器内 PASS。


## 7. 验收记录（2026-10-26）

用「假 ltfs 脚本 + `mount --bind` 目录」在容器内模拟真机（特权容器）：

- 挂载：`ltfs` 输出 `Tape attribute: Barcode = LTO6-0001` → 解析成功、建卷、status=mounted、TapeOperation success；
- 防串盘：输出 FB1186 但预期 LTO6-0001 → 自动卸载+弹出、卷不登记、op failed；
- 盘点：mount→scan（6 文件/含 srt sidecar）→写本地 manifest→unmount（eject 可禁用），`ok=true`；
- 传输：容器→磁带 `/media/tape/newdir`（不存在自动建）成功；磁带→容器 `/data/from_tape/deep` 成功；未挂载时明确拒绝；
- 设备锁：`docker exec` 另起进程持锁 → API 返回 `busy=true`；释放后可正常挂载；
- 备份：容器内创建备份成功（823KB，sha256 生成）；
- 页面：`/tapes /files /logs /backup /library /review /sources` 全 200；
- selfcheck：PASS 8 / WARN 2（无真实设备、未开深度扫描）；
- **amd64 镜像**（Unraid 架构）构建成功并在 Rosetta 下跑通：`ltfs --version` 正常、6 个页面 200，已导出离线包（94MB）。

未验证：真实 LTFS 输出格式/耗时、`/dev/sg*` 直通、release_device 行为、写保护/介质错误。
