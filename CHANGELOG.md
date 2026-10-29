# Changelog

## v1.1.3 — 2026-10-29

### 安装器增强

- 安装前交互询问是否配置代理；
- 支持代理地址、端口、用户名、密码（密码不回显）；
- 代理自动应用到 apt/dnf/pacman/zypper/pip/git/curl，并写入服务环境；
- 支持 `--proxy URL` 和 `--no-proxy` 非交互配置；
- Debian minimal 镜像自动补齐 `systemd`、`systemd-sysv`、`procps`、`psmisc`、`iproute2`、`hostname`、`lsscsi` 等基础包；
- 服务注册前检查 PID 1 是否为 systemd，不是时给出明确错误；
- 增加 `wget`、`gnupg` 等基础网络工具依赖。


## v1.1.2 — 2026-10-29

### 一键安装的 LTFS 编译修正

- 按 `ltfs-webui` 已验证的流程，优先从官方 `LinearTapeFileSystem/ltfs` clone 源码；
- 使用 `./autogen.sh && ./configure --prefix=/usr && make install` 编译；
- 增加 `lsscsi` 等磁带设备依赖；
- GitHub 不可达时回退项目 `vendor/` 中的 LTFS 源码；
- 保留 `--skip-ltfs`、`--ltfs-source vendor` 等离线/仅媒体库选项。


## v1.1.1 — 2026-10-29

### 一键安装

- 新增 `install.sh`，支持 Debian/Ubuntu、RHEL/Fedora/Rocky/Alma、Arch、openSUSE 和 macOS；
- 自动安装 Python、ffmpeg、CIFS、SMB、磁带工具及构建依赖；
- 自动从 GitHub 拉取最新代码；
- 默认程序目录 `/opt/lams`，默认数据目录 `/lams/data`；
- 自动创建 Python venv 并安装生产依赖；
- Linux 自动编译 vendor 中的 LTFS，macOS 跳过 LTFS；
- Linux 注册 systemd 服务，macOS 注册 launchd 服务，支持开机自启；
- 尝试通过 ufw/firewalld/nftables/iptables 开放 TCP 5080；
- 首次安装不会覆盖已有数据目录。


## v1.1.0 — 2026-10-29

### 磁带初始化与恢复

- 新增新磁带初始化流程：挂载失败识别 `LTFS17168E / medium is not partitioned` 等未格式化特征；
- 新增 `mkltfs` 初始化，支持可选的 `-f` 强制格式化勾选；
- 新增 `ltfsck` 索引恢复：先只读列出回滚点，用户确认后恢复；
- 初始化必须引用近期一次“未初始化挂载失败”上下文；
- 已登记 barcode 禁止格式化，`-f` 也不能绕过；
- 初始化前校验挂载状态、条码格式、库中登记、条码一致性；
- 初始化成功后自动挂载并登记卷，失败保留完整命令输出；
- 新增磁带初始化/恢复弹窗，复用项目统一弹窗样式。

### 盘点流程

- 盘点不再自动弹出磁带，流程固定为：扫描 → manifest → 卸载 → 结束；
- 已挂载磁带盘点时复用现有挂载，不再重复 `ltfs mount`；
- `.volume_label` 不可读时使用库中已校验 barcode 兜底；
- 盘点收带后同步 `LibraryVolume.status`，修复“顶部未挂载、卷仍 mounted”矛盾；
- `eject=false` 时不再误调用弹出；
- 手动弹出按钮始终可用；
- 盘点历史状态与实体操作结果保持一致。

### 视频抽帧

- 新增 `frame_failure` 失败标记表；
- 损坏/无法打开的视频文件自动标记并跳过；
- 单文件错误不再中断整批抽帧；
- 有成功帧时自动继续下一批；
- ISO BD 不进入普通抽帧队列。

### 资产自动下载

- 自动封面/剧照任务不再要求整个任务队列空闲；
- 可与抽帧等任务并行，只与其它资产任务去重；
- 保持新任务排在用户任务之后，不插队。

### 测试

- 测试总数：**869 passed**。
