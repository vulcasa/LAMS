# 磁带初始化（未格式化新带）方案

> 状态：仅方案，不落地
> 目标：新插入的空白磁带首次挂载失败时，识别为“未初始化 LTFS”，由用户在弹窗输入条码并确认后执行 `mkltfs`，成功后自动挂载并登记卷。

---

## 1. 背景与边界

LAMS 当前磁带链路：

```text
磁带页
→ /api/tape/mount
→ tape_ops.mount_tape()
→ DirectAdapter.mount()
→ ltfs -o devname=... <mount_point>
```

当前问题：

- 新带未格式化时，`ltfs` 挂载失败；
- `DirectAdapter.mount()` 只返回一行错误；
- 页面只显示“挂载失败”，没有后续动作；
- 用户无法在 LAMS 中完成“新带初始化”。

目标流程：

```text
插入新带
→ 点击挂载
→ LTFS 返回“不是 LTFS 卷/无法读取标签”
→ 弹窗提示“磁带未初始化”
→ 用户输入条码并确认格式化
→ mkltfs 初始化
→ 自动挂载
→ 登记磁带卷
```

边界：

- 只对 **未格式化的新带/空白带** 提供初始化；
- 不做自动格式化；
- 不把普通挂载失败误判为未初始化；
- 磁带设备仍是独占模式，初始化与挂载/卸载/弹出共享同一把设备锁；
- 仅 `TAPE_ADAPTER=direct` 支持；HTTP 兼容模式不支持初始化。

---

## 2. 参考项目核对

已阅读 `vulcasa/ltfs-management-webui` 当前默认分支。

它已有：

- LTFS 挂载/卸载/弹出；
- `ltfs -o device_list` / `device_info`；
- 从挂载输出解析条码；
- 命令执行和输出记录。

但当前默认分支**没有现成的 `mkltfs` 格式化/初始化实现**；挂载失败时只是返回 `磁带挂载失败: <stderr>`。

因此本方案：

- 复用 ltfs-webui 的条码解析/命令执行/输出日志模式；
- 在 LAMS 的 adapter 层新增 `initialize()`，而不是照搬不存在的格式化代码；
- 初始化流程由 `tape_ops` 统一管理锁、操作日志和卷登记。

---

## 3. 错误识别

### 3.1 触发条件

只在挂载失败输出命中“未格式化/非 LTFS 卷”特征时提示初始化，例如：

```text
LTFS11176E The ANSI label indicates this is not an LTFS volume.
LTFS11170E Failed to read label ...
LTFS11174E Cannot read ANSI label ...
LTFS11175E Cannot read ANSI label: expected 80 bytes ...
not an LTFS volume
not formatted
unformatted cartridge
```

建议分类函数：

```python
classify_mount_failure(stdout, stderr, message) -> {
    "code": "uninitialized" | "busy" | "no_tape" | "write_protected" | "unknown",
    "needs_init": bool,
    "summary": str,
}
```

### 3.2 不要误判的情况

以下情况不应弹初始化：

- 设备不存在；
- 没有插入磁带；
- 磁带被写保护；
- 挂载点被占用；
- 设备忙；
- LTFS 进程超时；
- 普通 I/O 错误；
- 已格式化但索引损坏（应走 `ltfsck`，不是 `mkltfs`）。

只有明确“不是 LTFS 卷/无法读取标签”才进入初始化流程；其他错误保留原始错误信息。

### 3.3 输出保存

`DirectAdapter.mount()` 已经把命令/stdout/stderr 存到 `last_output`。

`mount_tape()` 在返回失败前用它做分类：

未初始化、条码未登记（可以进入初始化）：

```json
{
  "ok": false,
  "needs_init": true,
  "code": "uninitialized",
  "detected_barcode": "FB1186",
  "barcode_registered": false,
  "message": "当前磁带未初始化（不是 LTFS 卷），需要格式化后才能使用",
  "operation_id": 123,
  "barcode": null
}
```

挂载失败但条码已登记（禁止格式化）：

```json
{
  "ok": false,
  "needs_init": true,
  "code": "registered_tape",
  "detected_barcode": "FB1186",
  "barcode_registered": true,
  "message": "该磁带已在库中登记，不能格式化；请检查磁带索引或挂载状态",
  "operation_id": 123,
  "barcode": "FB1186"
}
```

> 前端必须先检查 `barcode_registered`：为 true 时即使 `needs_init=true` 也不显示格式化按钮/选项。

### 3.4 Barcode 对比与展示条件

初始化弹窗必须同时满足：

```text
挂载失败
且识别为“不是 LTFS 卷/无法读取标签”
且当前未挂载
且没有可用的已登记条码
```

对比规则：

| 场景 | 是否显示格式化按钮/选项 |
|---|---|
| 挂载成功 | 否，任何情况下都不显示 |
| 挂载失败，但读取到 barcode，且库中已登记 | 否，提示“已登记，请检查磁带到挂载状态”，不提供格式化 |
| 挂载失败，读取到 barcode，库中未登记 | 是，可进入初始化弹窗 |
| 挂载失败，未读取到 barcode | 是，进入弹窗要求手工输入条码 |
| 用户输入的 barcode 在库中已登记 | 否，服务端拒绝初始化 |
| 选中强制格式化 `-f` | 仍不允许覆盖已登记 barcode |
| 挂载成功后 | 否，只显示卸载/弹出等正常操作 |

注意：

- `needs_init=true` 只表示“挂载失败且像未初始化”，不等于“允许初始化”；
- 是否显示格式化按钮还必须同时检查 `barcode_registered=false`；
- 这里“已登记”指 `LibraryVolume(kind='tape', ref=barcode)` 存在，不论它的 status 是什么；
- 已登记磁带即使挂载失败，也不能通过初始化按钮格式化；
- `-f` 只影响 `mkltfs` 是否强制覆盖，不绕过“已登记 barcode”保护；
- 成功挂载后，页面只保留卸载/弹出，不出现初始化入口。

### 3.5 先尝试 ltfsck 索引恢复，再进入格式化

对于：

```text
挂载失败
且读取到 barcode
且该 barcode 在 LibraryVolume 中未登记
```

不要直接弹格式化，先弹出“索引恢复”选择：

```text
标题：磁带回读失败

检测到条码：FB1186（未登记）

可能原因：
- 磁带索引损坏
- 上次写入未正常卸载
- 磁带确实是空白/非 LTFS

是否先尝试 ltfsck 索引恢复？

[取消] [不恢复，格式化…] [尝试索引恢复]
```

规则：

- **默认推荐先恢复**，不推荐直接格式化；
- 用户选择「不恢复，格式化…」后，才进入格式化弹窗；
- 用户选择「尝试索引恢复」后，执行只读检查/恢复流程；
- 恢复成功并挂载成功 → 结束，不出现格式化选项；
- 恢复失败 → 显示失败原因，再提供「继续格式化」入口；
- 恢复过程中始终持有设备锁。

### 3.6 ltfsck 恢复策略

先只读列出可回滚点：

```bash
ltfsck -l /dev/sg10
```

如果存在回滚点，用户选择最新或指定 generation：

```bash
ltfsck -r -g <generation> /dev/sg10
```

可选：

```bash
ltfsck -n /dev/sg10          # 不执行回滚，检查指定点
ltfsck -z /dev/sg10          # 深度恢复（仅高级选项，需额外确认）
```

注意：

- `ltfsck -l` 只读，优先执行；
- `-r` 会修改磁带索引，必须用户确认；
- `-z` / `-f` 深度/完整恢复可能产生 `_ltfs_lostandfound`，第一版不自动执行；
- 对真正的空白带，ltfsck 通常也会失败，失败后再引导格式化；
- ltfsck 可能耗时较长，需要独立超时；
- 恢复失败不能自动格式化，必须再次由用户确认。

### 3.7 已登记条码的失败处理

如果读取到的条码已在库中登记：

```text
挂载失败 + barcode_registered=true
```

则：

- 不显示格式化按钮/选项；
- 不显示 `-f` 勾选项；
- 可以显示「运行 ltfsck 索引检查/恢复」；
- 恢复仍失败时，只提示检查磁带/索引，不提供格式化；
- 如果用户坚持格式化，必须走独立的高级危险操作，不属于本自动流程。

### 3.8 挂载成功

挂载成功：

- 不显示索引恢复弹窗；
- 不显示格式化按钮/选项；
- 不显示 `-f` 勾选项；
- 只保留正常卸载/弹出/盘点操作。

---

## 4. 初始化命令

使用 LTFS 自带的 `mkltfs`：

默认：

```bash
mkltfs -d /dev/sg10 -s FB1186 -n FB1186
```

强制格式化：

```bash
mkltfs -d /dev/sg10 -s FB1186 -n FB1186 -f
```

说明：

- `-d` 设备路径：`LTFS_DEVICE`；
- `-s` tape serial / barcode：用户输入；
- `-n` volume name：默认与 barcode 相同；
- 默认不加 `-f`，优先保护已有 LTFS 数据；
- 弹窗提供「使用强制格式化 `-f`」勾选项；
- 勾选后才在命令中追加 `-f`；
- `mkltfs` 返回“already formatted”且未勾选 `-f` 时，提示用户直接挂载，不自动覆盖；
- 勾选 `-f` 属于明确覆盖操作，仍需要二次确认和完整警告。

barcode 限制：

- `mkltfs -s` 按 LTFS 文档为 6 位 ASCII 字母数字；
- 建议前端限制 `^[A-Za-z0-9]{6}$`，统一转大写；
- 如果实际环境条码长度不同，需要先做兼容性测试再放宽。

---

## 5. 后端设计

### 5.1 Adapter 新增 `initialize()`

基类：

```python
def initialize(self, barcode: str, *, force: bool = False) -> tuple[bool, str, str | None]:
    raise NotImplementedError
```

`DirectAdapter.initialize()`：

```text
1. 检查设备存在；
2. 检查 mkltfs 可执行；
3. 运行 mkltfs -d <device> -s <barcode> -n <barcode>；
   如果 force=True，追加 -f；
4. 记录 command/stdout/stderr；
5. 检查返回码；
6. 成功后不自动挂载（由 tape_ops 继续）；
7. 返回 (ok, message, barcode)
```

`HttpAdapter.initialize()`：

```text
返回 unsupported：
"当前为 HTTP 兼容模式，初始化需要 TAPE_ADAPTER=direct"
```

新增 ltfsck 方法：

```python
def recover_list(self) -> tuple[bool, str, list[dict]]:
    """只读执行 ltfsck -l，返回可回滚点。"""

def recover_index(self, generation: str | None = None, *, deep: bool = False) -> tuple[bool, str]:
    """执行 ltfsck -r -g <generation> 或深度恢复；必须由用户确认。"""
```

`DirectAdapter` 使用 `LTFSCK_BIN`（默认 `ltfsck`），记录 command/stdout/stderr。
`HttpAdapter` 若 v1 没有对应接口，返回 unsupported。

新增配置：

```text
MKLTFS_BIN=mkltfs
TAPE_INIT_TIMEOUT=600
```

构造函数增加 `mkltfs_bin` 参数，便于测试注入假命令。

### 5.2 `tape_ops.initialize_tape()`

新增：

```python
@_serialized
def initialize_tape(app, barcode, *, confirmed=False, force=False, adapter=None) -> dict:
```

流程：

```text
1. 参数校验：confirmed 必须为 True；barcode 格式合法；
2. 获取设备锁（与 mount/unmount/eject 共用）；
3. 拒绝已挂载状态；
4. 检查直接适配器；HTTP 返回 unsupported；
5. 检查设备存在，mkltfs 存在；
6. 安全检查：如果 barcode 已存在于 LibraryVolume 且该卷有文件 → 拒绝；
7. 写 TapeOperation(op_type='init', status='running')；
8. adapter.initialize(barcode, force=force)；
9. 成功后调用 adapter.mount()；
10. 校验实际挂载条码与输入 barcode 一致；
11. 登记/更新 LibraryVolume；
12. 写 TapeOperation 终态；
13. 返回 ok/message/operation_id/volume。
```

新增：

```python
@_serialized
def recover_tape_index(app, *, generation=None, deep=False, confirmed=False, adapter=None) -> dict:
```

流程：

```text
1. 检查当前未挂载、设备存在、ltfsck 可用；
2. 获取设备锁；
3. adapter.recover_list() 只读列出回滚点；
4. 用户选择 generation 后确认；
5. adapter.recover_index(generation, deep=deep)；
6. 成功后 adapter.mount()；
7. 实际条码校验；
8. 记录 TapeOperation(op_type='ltfsck'/'recover')；
9. 成功则登记卷；失败则保留错误，回到格式化选择。
```

关键安全点：

- ltfsck 可能修改索引，必须用户确认；
- 先 `-l` 只读，再决定是否 `-r`；
- 深度恢复/完整恢复默认不开放；
- 恢复失败不能自动格式化；
- 初始化是破坏性操作，必须 `confirmed=True`；
- 不能因为条码已存在就静默覆盖；
- 如果格式化成功但挂载失败，记录“初始化成功、挂载失败”，不要登记为已挂载；
- 失败时保存原始输出，便于判断写保护/设备错误；
- 同一时间只允许一个磁带操作。

### 5.3 API

新增：

```text
POST /api/tape/init
Content-Type: application/json

{
  "barcode": "FB1186",
  "confirmed": true,
  "force": false
}
```

返回：

```json
{
  "ok": true,
  "message": "磁带初始化并挂载成功",
  "operation_id": 123,
  "barcode": "FB1186",
  "volume": {...}
}
```

服务端初始化前必须重新校验：

```text
1. 当前未挂载；
2. barcode 合法；
3. barcode 未在 LibraryVolume 登记；
4. 如果最近一次挂载操作读取到了 detected_barcode，
   输入的 barcode 必须与它一致；
5. 已登记条码即使 force=True 也拒绝；
6. 初始化后挂载成功，实际条码必须与输入一致，否则卸载并失败。
```

错误：

```json
{
  "ok": false,
  "needs_init": false,
  "message": "条码格式必须是 6 位字母或数字",
  "code": "invalid_barcode"
}
```

```json
{
  "ok": false,
  "needs_init": false,
  "message": "该条码已在库中登记，禁止格式化",
  "code": "already_registered"
}
```

```json
{
  "ok": false,
  "needs_init": false,
  "message": "输入条码与读取到的磁带条码不一致",
  "code": "barcode_mismatch"
}
```

```json
{
  "ok": false,
  "needs_init": false,
  "message": "当前磁带已挂载，不能执行初始化",
  "code": "already_mounted"
}
```

新增恢复 API：

```text
POST /api/tape/recover/list
{}

POST /api/tape/recover
{
  "generation": "123",   # 可选；空则用最新
  "deep": false,
  "confirmed": true
}
```

`/api/tape/recover/list` 只读；`/api/tape/recover` 必须 `confirmed=true`。
已登记条码允许恢复，但恢复后仍不允许通过本流程格式化。

`/api/tape/mount` 失败时增加 `needs_init`/`code` 字段；现有调用兼容。

---

## 6. 前端交互

### 6.1 挂载失败后的弹窗

在磁带页 `tapeOp('mount')` 中：

```text
POST /api/tape/mount
→ 如果 data.ok == true
   → 不显示任何格式化入口，只刷新挂载状态
→ 如果 data.needs_init != true
   → 普通挂载失败提示，不显示格式化
→ 如果 data.needs_init == true 且 data.barcode_registered == true
   → 只提示“已登记，可尝试 ltfsck 检查/恢复”，不显示格式化
→ 如果 data.needs_init == true 且未登记
   → 打开“磁带回读失败”弹窗
   → 优先显示「尝试索引恢复」
   → 用户选择不恢复后，才打开格式化弹窗
```

### 6.1.1 恢复弹窗

```text
标题：磁带回读失败

检测到条码：FB1186（未登记）

可能原因：索引损坏 / 上次未正常卸载 / 空白带

[取消] [不恢复，格式化…] [尝试索引恢复]
```

- 「尝试索引恢复」先调用 `/api/tape/recover/list`；
- 有回滚点则展示 generation 列表，默认最新；
- 用户确认后调用 `/api/tape/recover`；
- 成功挂载 → 结束，不显示格式化；
- 失败 → 显示错误，并提供「继续格式化」；
- 只有用户明确不恢复或恢复失败后，才进入第 6.1.2 节格式化弹窗。

### 6.1.2 格式化弹窗

格式化弹窗只在恢复被拒绝/失败后出现。

弹窗内容：

```text
标题：磁带未初始化
说明：当前插入的磁带不是可用的 LTFS 卷，可能尚未初始化。
      格式化会清除磁带上的全部数据，且不可撤销。

磁带条码：[ FB1186 ]   （必填，6 位字母/数字）

☐ 我确认这是空白/可格式化的磁带，并且条码与磁带标签一致
☐ 使用强制格式化 `mkltfs -f`（仅当磁带已有 LTFS 数据或普通格式化失败时）

[取消] [格式化并挂载]
```

按钮：

- 默认禁用「格式化并挂载」；
- 条码合法 + 勾选「我确认」后才启用；
- 勾选「使用强制格式化 `-f`」后，按钮文案变为「强制格式化并挂载」；
- 强制勾选项旁显示红色警告：会覆盖已有 LTFS 数据，不可撤销；
- 按钮使用红色危险样式；
- 提交后按钮禁用，显示“正在格式化并挂载…”；
- 成功：提示成功并刷新磁带页；
- 失败：保留弹窗和输入，显示首行错误 + 可展开完整输出。

### 6.2 条码一致性

- 如果挂载失败输出里解析到 `detected_barcode`，弹窗默认填入该条码；
- 如果该条码已在库中登记，弹窗不打开，直接提示不可格式化；
- 如果用户手工输入条码，提交前服务端再次查库；
- 输入条码与 `detected_barcode` 不一致时拒绝；
- 前端不能仅依赖本地列表，服务端校验是最终权威。

### 6.3 样式复用

使用现有：

```text
.avlib-modal
.avlib-modal-box
.avlib-modal-head
.avlib-modal-actions
.btn.danger
```

需要在 `jobs.js` 的通用弹窗基础上增加一个支持输入框的 `avlib.prompt`，或在磁带页单独实现同款 DOM；建议统一扩展 `avlib.prompt`，避免复制样式。

### 6.4 其他入口

- 不提供“无条件格式化”按钮；
- 仅在挂载失败且识别为未初始化时出现；
- 如果用户取消，保留原挂载失败提示；
- 如果当前是 HTTP 适配器，挂载失败不弹初始化，改为提示“当前为兼容模式，初始化需 direct 适配器”。

---

## 7. 数据与日志

### TapeOperation

新增操作类型：

```text
init
ltfsck
recover
```

记录：

- barcode；
- command（`mkltfs ...`）；
- stdout/stderr；
- 开始/结束时间；
- 成功/失败消息；
- volume_id（成功后关联）。

磁带页「最近操作」直接展示 `init`；后台活动/日志页也能看到。

### 卷登记

初始化并挂载成功后：

```text
LibraryVolume(kind='tape', ref=barcode)
  status = mounted
  last_mounted_at = now
  device_path = LTFS_DEVICE
  last_error = None
```

如果卷已存在且无文件，可更新；有文件则拒绝覆盖。

---

## 8. 安全与失败处理

必须遵守：

- 不自动格式化；
- 挂载成功后不显示任何格式化入口；
- 读取到条码且库中已登记 → 不显示格式化按钮/选项，服务端也拒绝；
- 未登记但挂载失败 → 先提示 ltfsck 恢复，用户不恢复后再格式化；
- ltfsck 只读列表优先；恢复/深度恢复必须额外确认；
- 不在普通挂载失败时猜测并格式化；
- 必须用户输入条码 + 勾选确认；
- `-f` 只由弹窗中的强制格式化勾选项触发；
- 强制格式化必须额外确认，不能默认开启；
- 条码必须与磁带物理标签一致；
- 已有文件记录的条码只有在用户明确勾选强制格式化后才允许覆盖，并保留操作日志；
- 写保护磁带 → 明确报错，不重试；
- 已格式化但损坏 → 提示 `ltfsck`，不提供 `mkltfs`；
- 机柜门/设备独占 → 初始化期间锁住；
- 超时必须 kill `mkltfs` 子进程并记录；
- 失败保留原始输出，不吞错。

---

## 9. 测试计划

### 单元测试

- 错误分类：
  - `LTFS11176E` → uninitialized；
  - `not an LTFS volume` → uninitialized；
  - `no medium` / `write protected` → 不进入初始化；
- barcode 校验：长度/字符/大小写；
- adapter initialize 参数正确（假 `mkltfs` 可执行脚本）；
- `force=False` 时命令不含 `-f`；
- `force=True` 时命令包含 `-f`；
- 强制格式化必须 `confirmed=True`；
- 已有卷有文件时，未勾选 force 拒绝；勾选 force 才允许；
- 挂载成功时返回 `needs_init=false`，前端无格式化入口；
- 挂载失败且 detected_barcode 已登记 → `barcode_registered=true`，不弹初始化；
- 初始化接口对已登记条码即使 `force=true` 也返回 `already_registered`；
- 输入条码与 detected_barcode 不一致 → `barcode_mismatch`；
- 初始化成功但实际挂载条码不一致 → 卸载并失败；
- 挂载失败未登记 → 先进入恢复选择，不直接格式化；
- 用户选择不恢复 → 才调用初始化；
- ltfsck -l 无回滚点 → 提示无法恢复，再允许格式化；
- ltfsck 恢复成功 → 自动挂载，不出现格式化；
- ltfsck 失败 → 保留错误，要求用户再次确认格式化；
- 已登记条码 → 可恢复，但 recover/init 均不允许格式化。
- 锁冲突返回 busy；
- 格式化成功但挂载失败 → 不登记 mounted；
- HTTP 适配器返回 unsupported。

### 集成测试

使用 fake `mkltfs` + fake `ltfs`，模拟：

```text
新带挂载失败
→ needs_init
→ /api/tape/init
→ 格式化成功
→ 挂载成功
→ 卷登记
```

### 真机测试

- 空白 LTO 带；
- 写保护带；
- 已格式化带（不能弹初始化）；
- 非 LTFS 数据带；
- 机柜门无法弹出场景；
- 6 位条码与现有标签一致性。

---

## 10. 分阶段实施

### 阶段 1：只做识别（低风险）

- 错误分类；
- mount 失败返回 `needs_init` / `detected_barcode` / `barcode_registered`；
- 页面只在满足条件时弹提示；已登记条码和挂载成功都不显示格式化入口。

### 阶段 2：完整恢复/初始化

- adapter.recover_list / recover_index；
- /api/tape/recover/list、/api/tape/recover；
- 恢复选择弹窗；
- 用户不恢复后再进入初始化弹窗；
- adapter.initialize；
- /api/tape/init；
- 输入条码弹窗；
- 自动挂载与卷登记。

### 阶段 3：增强

- 完整输出查看；
- 写保护/损坏引导；
- `ltfsck` 修复流程；
- 操作审计与重试策略。

---

## 11. 风险

- LTFS 错误文本版本差异，分类可能漏判；
- 条码长度/字符限制与真实标签不一致；
- 用户输错条码会写错标签；
- 误把“索引损坏”当“未格式化”会破坏数据；
- `mkltfs` 耗时较长，HTTP 请求需要足够超时；
- 设备独占下，初始化被其他操作阻塞；
- HTTP 兼容模式无法初始化；
- ltfsck 也可能失败或进一步损坏索引；
- 恢复时间可能很长，需超时和进度提示；
- 空白带执行 ltfsck 可能只是浪费一次读带时间。

---

## 12. 推荐结论

采用：

```text
挂载失败
→ 严格匹配“不是 LTFS 卷/无法读取标签”
→ 弹窗要求输入条码 + 二次确认
→ mkltfs 初始化
→ 自动挂载
→ 登记卷
```

仅支持 `TAPE_ADAPTER=direct`；不自动格式化；不覆盖已有文件卷；所有命令和输出写入 `TapeOperation`。

这套方案可以复用 ltfs-webui 的条码解析/命令执行模式，但 `mkltfs` 初始化和安全确认需要作为 LAMS 的新能力实现。
