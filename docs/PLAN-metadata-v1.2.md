# v1.2.0 非标来源元数据方案

> 版本：v1.2.0（规划中）
> 范围：FC2PPV、HeyDouga、金髪天国（KIN8）、欧美片等非标准番号元数据
> 状态：方案，不落地

---

## 1. 背景

LAMS 当前元数据底座主要覆盖：

- 商业番号：r18.dev dump；
- JavDB 补中文题材/评分；
- NFO/手工修正；
- FC2PPV 只有基础 URL 导入能力，覆盖面不足。

缺口较大的来源：

| 来源 | 特点 | 现有问题 |
|---|---|---|
| FC2PPV | 无统一 dump，作品页独立 | 只有标题/单封面，缺 sample 剧照、seller、tags |
| HeyDouga | 站点/频道型，编号不统一 | 番号解析和元数据来源不完整 |
| 金髪天国（KIN8） | 系列/站点型，编号与标题混合 | 缺统一 provider，封面/剧照覆盖不足 |
| 欧美片 | 无日本番号体系，studio/scene 命名 | 无法靠番号自动匹配，需外部元数据源 |

目标不是“全部自动抓取”，而是：

```text
可识别的来源自动抓
不可识别的来源支持手工 URL / NFO
所有来源统一落到 Title + Asset
明确标记元数据来源和完整度
```

---

## 2. 总体架构

不要把 FC2、HeyDouga、KIN8、欧美片分别写死在 JavDB 流程里。

建议新增统一 provider 层：

```text
services/metadata/
  __init__.py
  provider.py          # 统一接口、注册表、结果结构
  fc2.py               # FC2PPV
  heydouga.py          # HeyDouga
  kin8.py               # 金髪天国 / KIN8
  western.py            # 欧美片
  thirdparty.py         # JavDB / IAFD / AdultDVDEmpire / StashDB 等 fallback
  normalize.py          # 番号/外部 ID 归一化
```

统一接口：

```python
detect(filename, path) -> ProviderHit | None
fetch(hit) -> Metadata | None
parse(html/json, url) -> Metadata
download_assets(metadata) -> Asset[]
```

统一结果：

```python
{
    "provider": "fc2",
    "external_id": "FC2-PPV-1234567",
    "title": "...",
    "release_date": "...",
    "runtime_mins": 120,
    "maker": "...",
    "seller": "...",
    "tags": [...],
    "cover_url": "...",
    "still_urls": [...],
    "source_url": "...",
    "confidence": 90,
}
```

---

## 3. 各来源方案

### 3.1 FC2PPV

主来源：

```text
https://adult.contents.fc2.com/article/<id>/
```

解析：

- JSON-LD / 内嵌 JSON / OpenGraph；
- 标题、日期、seller、tags、简介；
- 封面；
- sample 剧照列表。

兜底：

- FC2 seller 页面；
- 第三方 FC2 数据库（如 fc2ppvdb，先确认条款）；
- JavDB；
- 手工 URL。

标识：

```text
FC2-PPV-<id>
```

### 3.2 HeyDouga

主来源：

- HeyDouga 官方作品/频道页；
- 官方页面的 OpenGraph / JSON-LD / sample gallery。

需要实现：

- 文件名/路径识别 HeyDouga 编号；
- 官方 URL 构造；
- 标题、日期、演员/频道、tags、封面、sample 剧照；
- 找不到官方页时走第三方/手工 URL。

标识建议：

```text
HEYDOUGA-<id>
```

注意：

- HeyDouga 的编号形态不统一，必须先收集真实文件名样本；
- 不能把任意数字串直接当 HeyDouga 编号；
- 低置信度进入待确认。

### 3.3 金髪天国（KIN8）

主来源：

- 金髪天国 / KIN8 官方作品页；
- 官方页面的标题、演员、日期、tags、封面、sample。

实现：

- 识别 `kin8tengoku` / `kin8` / 金髪天国相关文件名；
- 统一外部 ID；
- 官方页优先；
- JavDB / 第三方页面 fallback；
- 缺失字段进人工补录。

标识建议：

```text
KIN8-<id>
```

注意：

- 这是一个系列/站点型来源，不一定有与传统番号等价的 ID；
- 需要保留 `source_url` 和 `external_id`，不强行编造番号。

### 3.4 欧美片

欧美片没有日本番号体系，不能只靠文件名匹配。

主来源：

1. 官方 studio 页面；
2. IAFD；
3. AdultDVDEmpire；
4. StashDB（如条款允许）；
5. 手工 URL / NFO。

字段：

- 标题；
- studio；
- 演员；
- 日期/年份；
- 场景/系列；
- 封面；
- sample/stills；
- tags。

标识：

```text
WESTERN-<studio>-<scene-id>
```

如果无法稳定识别：

- 不自动建 Title；
- 进入待确认/手工导入；
- 允许用户粘贴官方 URL；
- 保留 NFO 优先。

注意：

- TMDb 等主流影视库不适合成人内容；
- 第三方成人元数据站点的条款、API、稳定性需逐个确认；
- 不要绕过登录、付费墙或 DRM。

---

## 4. 数据模型

建议扩展 `Title` 或新增 `ExternalMetadata`：

```text
metadata_provider      # fc2 / heydouga / kin8 / western / javdb / nfo
external_id            # 外部 ID
source_url             # 官方页
metadata_status        # complete / partial / missing / manual
metadata_updated_at
```

资产仍使用：

```text
Asset(kind='cover')
Asset(kind='still', idx=N)
Asset(kind='screenshot')   # 视频抽帧，与剧照分开
```

`LibraryFile` 解析结果：

```text
code_guess / confidence / provider
```

低置信度不自动关联，进入「待确认」。

---

## 5. 抓取与兜底

统一优先级：

```text
官方页
→ 官方 API（如有）
→ 第三方元数据库
→ JavDB
→ 本地 NFO / 手工 URL
→ 待确认
```

通用规则：

- 单域名限速 2–5 秒/请求；
- HTML/JSON 缓存 7–30 天；
- 404/下架/403 冷却；
- 支持代理；
- 同一作品只抓一次；
- 批量任务低并发；
- 失败不自动重试无限循环。

---

## 6. UI

### 元数据完整度

片库/详情增加：

```text
元数据来源：FC2 / HeyDouga / KIN8 / Western / JavDB / NFO
完整度：完整 / 部分 / 缺失 / 手工
```

筛选：

```text
元数据来源
缺失封面
缺失剧照
缺失简介
待人工补录
```

### 手工导入

详情页保留：

- 粘贴官方 URL；
- 重新抓取；
- 手工编辑标题/日期/studio/tags；
- 上传封面/剧照。

### 批量任务

```text
metadata/fetch
metadata/fetch_batch
metadata/refresh
```

任务页显示每个来源的成功/失败数量。

---

## 7. 分阶段实施

### v1.2.0（第一批）

- provider 框架；
- FC2PPV 完整元数据 + 剧照；
- HeyDouga 官方页；
- KIN8 官方页；
- 欧美片：先支持手工 URL/NFO，不强行自动匹配；
- 元数据完整度筛选；
- 限速/缓存/失败冷却。

### v1.2.x（第二批）

- KIN8 / HeyDouga 第三方 fallback；
- 欧美片 IAFD/AdultDVDEmpire/StashDB 适配（逐站确认条款）；
- 批量补全任务；
- 元数据覆盖率报表。

### v1.3+

- 更多区域/厂牌 provider；
- 自动匹配质量评分；
- 人工校对工作流。

---

## 8. 测试计划

- FC2 页面 fixture 解析；
- HeyDouga fixture；
- KIN8 fixture；
- 欧美片标题/studio 解析；
- 无 ID 文件名 → 不自动建 Title；
- 低置信度 → 待确认；
- 官方页失败 → 第三方 fallback；
- 限速/冷却；
- 图片下载失败不影响元数据；
- 手工 URL 解析。

---

## 9. 风险与合规

- 各站点结构变化频繁，需要 fixture 和容错；
- 第三方元数据站点条款/API 不稳定；
- 欧美片没有统一 ID，自动匹配误判风险高；
- 成人内容可能触发在线翻译/抓取安全限制；
- 图片版权归原作者/平台；
- 只抓公开元数据，不绕过付费/登录/DRM；
- 仅用于个人归档，不公开再分发。

---

## 10. 结论

v1.2.0 的元数据范围从单一 FC2PPV 扩展为：

```text
FC2PPV
HeyDouga
金髪天国 / KIN8
欧美片
```

采用统一 provider 架构：

```text
官方页优先
→ 第三方/JavDB fallback
→ NFO/手工 URL
→ 待确认
```

FC2、HeyDouga、KIN8 可优先自动抓取；欧美片第一版以手工 URL/NFO 为主，避免低质量自动匹配污染片库。
