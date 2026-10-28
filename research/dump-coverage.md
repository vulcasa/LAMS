# r18.dev 每周 dump 覆盖率实测报告

> 目的：量化「r18.dev 全量 dump 能不能覆盖用户 2–4 万部的片库」，为 M0 决策
> （FC2/无码是否单独管道、中文缺口多大、图片能不能直接下）提供**实测数字**，
> 并交付可复用的 staging 导入器。
>
> 数据来源：r18.dev 2026-10-26 的 `latest.sql`（pg_dump plain，解压 1.29 GiB），
> 全部数字来自真实导入后的 `data/r18dump.db` 与 `research/dump-stats.json`，
> **没有一个估算值**（统计口径写在每节末尾，复现命令见 §12）。

---

## 0. 结论速览

| 结论 | 数字 |
|---|---|
| dump 规模 | 19 个 COPY 表 / **18,860,058 行** / 1,388,248,099 B（1.29 GiB）/ sha256 `65903a48…20d742a2` |
| 进 staging 的表 | **15 张 / 16,467,908 行**，其余 4 张（机翻 + DMM 原始表）只计数不建表 |
| 导入耗时 | **55.1 s**（表数据 37.0 s + 索引 16.3 s），远低于 10 分钟门槛；staging 库 1.74 GiB |
| 可用番号 | `derived_video` 1,914,173 部中 **1,526,052 部（79.72%）有 dvd_id**；去重后 **1,170,727 个精确番号键** |
| 标题 | `title_ja` 非空 **97.89%**；`title_en` 只有 **22.52%**（双语 20.41%）；至少一种标题 **100%** |
| 封面/剧照 | 封面 99.77%；剧照 first/last 字段 58.09%（其余 41.91% 为空） |
| 中文内容 | **完全没有**。唯一翻译表是 ja→en；仅简中字全量扫描 1,914,173 行只命中 1 行噪声 |
| 图片 URL | `https://pics.dmm.co.jp/<相对路径>.jpg` **成立**（实测 5/7 返回 200 图片；2 例是下架占位 302） |
| 不覆盖 | **FC2 / HEYZO / 1pondo / Caribbean 全部 0 条**；DMM 素人（`h_` content_id）339,923 部只能按 content_id 匹配 |
| 对用户库的判断 | 主流 DMM/FANZA 有码渠道（DVD/配信番号）→ 数据容量足够（1.17M 番号 ≈ 用户 2–4 万部的 29–58 倍），瓶颈在番号解析；FC2/无码/素人占比高 → dump 缺位，必须单独管道 |

---

## 1. dump 基本信息

| 项 | 值 |
|---|---|
| 文件 | `/tmp/r18dump/latest.sql`（同目录有原始 `latest.sql.gz`，导入器按魔数自动识别 gzip） |
| 大小 | 1,388,248,099 B = 1.29 GiB（gzip 包 274,959,219 B = 262 MiB） |
| 文件 mtime | 2026-10-26 03:15:32 UTC（2026-10-26 11:15 CST） |
| sha256 | `65903a48a0c18a579be2559daefb22038bca7ccfa4895b8b806dcfdd20d742a2` |
| 来源 | PostgreSQL 15.19 的 pg_dump 15.5 plain 格式；19 个 `COPY ... FROM stdin;` 数据块 |
| 总行数 | 18,860,745 行（含 COPY 头、`\.` 终止行、pg_dump 注释等 687 行非数据行） |
| 表/列 | 与任务书给出的列清单一致（`derived_video` 20 列，另加 `derived_site`、`derived_author`、`source_dmm_*`） |

`meta` 表中记录：`dump_file=latest.sql`、`dump_date=2026-10-26`、`dump_sha256`、
`imported_at=2026-10-26T03:30:57+00:00`、`schema_version=1`、`user_version=1`
（`PRAGMA user_version` 同为 1）。

---

## 2. 导入结果与耗时（实测）

运行环境：macOS + 系统 Python 3.14.7 + SQLite 3.53.4，本机 SSD。
命令：`python3 -m scripts.import_dump --sql /tmp/r18dump/latest.sql --out data/r18dump.db --stats-json research/dump-stats.json`
（导入器只依赖标准库，**不需要** Flask/venv；`python -m scripts.import_dump` 与
`python scripts/import_dump.py` 都可以）。

| 表 | 行数 | 导入耗时 (s) | 状态 |
|---|---:|---:|---|
| derived_actor | 105,833 | 0.19 | 进 staging |
| derived_actress | 102,022 | 0.21 | 进 staging |
| derived_author | 8,189 | 0.01 | 进 staging |
| derived_category | 989 | 0.00 | 进 staging |
| derived_director | 27,973 | 0.05 | 进 staging |
| derived_label | 13,141 | 0.02 | 进 staging |
| derived_maker | 6,874 | 0.01 | 进 staging |
| derived_series | 102,250 | 0.19 | 进 staging |
| derived_site | 2 | 0.00 | 进 staging |
| **derived_video** | **1,914,173** | **13.75** | 进 staging |
| derived_video_actor | 1,173,047 | 2.00 | 进 staging |
| **derived_video_actress** | **2,494,132** | **4.45** | 进 staging |
| derived_video_author | 148,443 | 0.20 | 进 staging |
| **derived_video_category** | **9,551,843** | **14.84** | 进 staging |
| derived_video_director | 818,997 | 1.10 | 进 staging |
| **小计（进 staging）** | **16,467,908** | **37.0** | |
| machine_translation | 560,622 | 0.24 | 仅计数（ja→en，无中文） |
| source_dmm_histrion | 3,554 | 0.00 | 仅计数（DMM 原始表） |
| source_dmm_trailer | 1,813,878 | 0.52 | 仅计数 |
| source_dmm_video_histrion | 14,096 | 0.00 | 仅计数 |
| **全 dump 合计** | **18,860,058** | **55.1 s（含索引）** | |

- **索引**：15 张表 16.33 s（`derived_video` 5 个索引：`dvd_id`、`dvd_id_key`、
  `dvd_id_norm`、`content_id`、`content_id_norm`；两张 100 万+ 级 join 表各 2 个索引）。
  索引在**数据全部灌完后统一建**，比边灌边维护快一个数量级。
- **吞吐** ≈ 29.9 万行/秒（16.47M 行 / 55.1 s）；峰值内存约 800 MB。
- **未超过 10 分钟**（55.1 s），所以不存在必须解决的瓶颈。若未来在弱 CPU 的
  Unraid/Jetson 上跑，可选的优化项：① `--batch-size 20000`（事务数减半）；
  ② `--no-sha256` 省 1 秒；③ 只导 `--tables derived_video,derived_actress,...`
  跳过 9.55M 行的 `derived_video_category`（约省 15 s，代价是暂时没有题材）；
  ④ 真要多机并行再考虑按表切分，本机没必要。
- **幂等**：重复执行 = 重建 staging 库（先写 `<out>.tmp`，完成后 `os.replace`
  原子替换，失败自动清理临时文件）；`meta` 每个 key 只有一行，不会追加。
- **主库零污染**：dump 只写 `data/r18dump.db`，主库 `data/avlib.db`（240 KB）不动。

---

## 3. derived_video 整体覆盖

| 指标 | 数值 | 占 1,914,173 行 |
|---|---:|---:|
| 总行数（每行 = 一部片的某个发行版本） | 1,914,173 | 100% |
| `dvd_id` 为 NULL | 386,959 | 20.21% |
| `dvd_id` 为空字符串 `''` | 1,162 | 0.06% |
| **dvd_id 可用** | **1,526,052** | **79.72%** |
| `content_id` 为 NULL | 0 | 0% |
| `title_ja` 非空 | 1,873,835 | 97.89% |
| `title_en` 非空 | 431,058 | 22.52% |
| ja + en 都有 | 390,720 | 20.41% |
| **ja 或 en 至少一个** | **1,914,173** | **100%** |
| `comment_en` 非空 | 21,409 | 1.12% |
| `comment_ja` 非空 | **0** | **0.00%**（整列全 NULL） |
| `runtime_mins` 非空 | 1,833,509 | 95.79% |
| `release_date` 非空 | 1,914,140 | 99.998% |
| 发行日期范围 | 1968-05-01 ~ 2030-12-31（50 个不同年份） | — |
| `sample_url` 非空 | 418,930 | 21.89% |
| `jacket_full_url` 非空 | 1,909,700 | 99.77% |
| `jacket_thumb_url` 非空 | 1,909,700 | 99.77% |
| `gallery_full_first` 非空 | 1,111,906 | 58.09% |

**交叉覆盖（只在 1,526,052 行「dvd_id 可用」里算，即真正能按番号命中的部分）**：

| 具备的字段 | 行数 | 占可用番号 |
|---|---:|---:|
| `title_ja` | 1,486,399 | 97.40% |
| `title_en` | 400,416 | 26.24% |
| `jacket_full_url` | 1,525,363 | 99.95% |
| `gallery_full_first` | 866,437 | 56.78% |
| `runtime_mins` | 1,448,726 | 94.93% |
| `release_date` | 1,526,033 | 99.999% |
| 有女优关联（`derived_video_actress`） | 774,527 | 50.75% |
| 有题材关联（`derived_video_category`） | 1,384,277 | 90.71% |

**站点 / 渠道分布**（`site_id` 与 `service_code` 是 r18 的「站点/售卖形态」维度）：

| site_id | 行数 | 占比 | | service_code | 行数 | 占比 |
|---|---:|---:|---|---|---:|---:|
| 2 | 1,591,376 | 83.14% | | mono | 849,919 | 44.40% |
| 1 | 322,797 | 16.86% | | digital | 643,738 | 33.63% |
| | | | | rental | 308,955 | 16.14% |
| | | | | ebook | 111,561 | 5.83% |

**dvd_id 为空的 386,959 行按渠道拆分**：digital 242,160 / ebook 111,561 /
mono 33,238（rental 0）。按站点：site_id=2 有 365,235 行、site_id=1 有 21,724 行。
也就是说「ebook（电子书籍）」几乎全是没有 dvd_id 的条目。

> 口径：SQL `COUNT(*)` + `TRIM(col) <> ''`（非 NULL 且非空串）；日期按 TEXT 存
> `YYYY-MM-DD`。

---

## 4. dvd_id 结构、去重与「多版本」

| 指标 | 数值 |
|---|---:|
| dvd_id 可用的行数 | 1,526,052 |
| 去重后的原始 dvd_id 字符串（`COUNT(DISTINCT dvd_id)`） | 1,288,431 |
| **精确匹配键 `dvd_id_key` 去重数**（小写+去分隔符，不补零） | **1,170,727** |
| 补零键 `dvd_id_norm` 去重数 | 1,165,312 |
| 精确键重复（同一番号多行）的组数 | 273,922 |
| 其中「同一番号、多个 content_id」的组数（= 同一部片有多个发行版本） | 260,106，覆盖 600,512 行（31.44% 的可用番号行） |
| 补零把「不同原始番号」并成同键的组数 | 5,338 组 / 16,368 行（1.07% 的可用番号行） |

**重复的来源（实测 top 组合）**：同一部片在 dump 里同时有 digital / mono / rental
三个售卖版本，各有独立 `content_id`，但 `dvd_id` 相同。组内 service_code 组合：

| service 组合 | 组数 |
|---|---:|
| digital + mono | 148,633 |
| digital + mono + rental | 60,611 |
| mono + rental | 20,667 |
| digital + rental | 13,858 |
| mono + digital（顺序不同） | 6,921 |
| digital + rental + mono（顺序不同） | 6,202 |

**为什么需要两级键（重要设计结论）**：dump 里存在「数字部分补零后相同、但其实是
两部不同片」的数据，例如：

| dvd_id | `dvd_id_key`（精确） | `dvd_id_norm`（补零） |
|---|---|---|
| `DOG-01`（おじさんがイイコト…） | `dog01` | `dog001` |
| `DOG-001`（【盗撮】逗●海岸周辺） | `dog001` | `dog001` ← 被补零合并 |
| `20ID-016` / `20ID-16` / `20ID016` | `20id016` / `20id16` / `20id016` | 都是 `20id016` |
| `AA-002` / `AA002` / `AA-2` | `aa002` / `aa002` / `aa2` | 都是 `aa002` |

所以 staging 同时保留两列，**匹配先精确 `dvd_id_key`，没命中再退回补零
`dvd_id_norm`**（`r18.lookup_by_dvd_ids` 就是这么实现的），避免把 DOG-01 和
DOG-001 混成一部。补零歧义只占 1.07% 的可用行，但真实存在。

**无 `-` 的番号形态**：313,154 个（16.4% 的可用量，`dvd_id_no_dash`），其中
letters+digits（如 `AA001`、`SSIS001`）187,192；纯数字开头（如 `1000819240`，
老 DMM 目录号）116,961；带下划线（如 `FT_010`）282；其它（如 `AARM001BOD`）8,719。
这些都能被 `dvd_id_key` 覆盖；纯数字串在「前缀 Top 30」里归到 `(other)`。

---

## 5. dvd_id 前缀 Top 30

口径：取「dvd_id 可用」的 1,526,052 行；前缀 = `-` 之前的部分（大写）；没有 `-`
的取开头连续字母；两者都没有（纯数字等）归 `(other)`。

| # | 前缀 | 行数 | 占比 | # | 前缀 | 行数 | 占比 |
|---:|---|---:|---:|---:|---|---:|---:|
| 1 | (other：无 `-` 且非字母开头) | 116,966 | 7.66% | 16 | ABP | 3,729 | 0.24% |
| 2 | PCBE | 5,522 | 0.36% | 17 | ARM | 3,522 | 0.23% |
| 3 | VNDS | 5,138 | 0.34% | 18 | SPZ | 3,356 | 0.22% |
| 4 | DSE | 5,019 | 0.33% | 19 | GS | 3,354 | 0.22% |
| 5 | SPRD | 4,757 | 0.31% | 20 | NHDTB | 3,125 | 0.20% |
| 6 | KBKD | 4,741 | 0.31% | 21 | MXGS | 3,053 | 0.20% |
| 7 | GNBF | 4,401 | 0.29% | 22 | GVG | 2,997 | 0.20% |
| 8 | PCBP | 4,164 | 0.27% | 23 | XRW | 2,961 | 0.19% |
| 9 | BCBA | 4,084 | 0.27% | 24 | JRZD | 2,874 | 0.19% |
| 10 | HODV | 4,021 | 0.26% | 25 | VENU | 2,804 | 0.18% |
| 11 | NACR | 3,929 | 0.26% | 26 | C | 2,758 | 0.18% |
| 12 | KIBF | 3,874 | 0.25% | 27 | CESD | 2,724 | 0.18% |
| 13 | TCED | 3,872 | 0.25% | 28 | RD | 2,653 | 0.17% |
| 14 | TDV | 3,760 | 0.25% | 29 | STARS | 2,598 | 0.17% |
| 15 | DSTD | 3,742 | 0.25% | 30 | PCBG | 2,579 | 0.17% |

> 说明：分布非常扁平（top 前缀只占 0.2–0.4%），因为 dump 里有 1,000+ 个厂牌；
> `(other)` 主要是纯数字老目录号与 `AARM001BOD` 这类特殊形态，并非解析失败。
> 用户 2–4 万部的常见主流厂牌（SSIS/SNIS/IPX/ABP/STARS/MIDV/PRED/JUL/…）都在
> 长尾里，见 §10 的单独计数。

---

## 6. 女优 / 导演 / 题材 / 厂牌覆盖

### derived_actress（102,022 人）

| 字段 | 非空行数 | 占比 |
|---|---:|---:|
| `name_kanji`（汉字名） | 66,233 | 64.92% |
| `name_kana`（假名） | 65,774 | 64.47% |
| `name_romaji`（罗马音） | 82,661 | 81.02% |
| `image_url`（头像） | **39,582** | **38.80%** |
| 三个名字全空 | 0 | 0% |
| 汉字+假名都有 | 65,774 | 64.47% |
| 只有汉字、没有假名 | 459 | 0.45% |

重点：**name_kanji 有 35% 左右为空**（素人/新人用罗马音），所以主库显示名必须
`kanji or kana or romaji` 回退；头像只有 38.8%，要补演员头像还得靠 JavBus/JavDB。

### derived_director（27,973 人）

| 字段 | 非空 | 占比 |
|---|---:|---:|
| `name_kanji` | 27,973 | 100% |
| `name_kana` | 27,973 | 100% |
| `name_romaji` | 4,594 | 16.42% |

### derived_category（989 个题材）

| 字段 | 非空 | 占比 |
|---|---:|---:|
| `name_ja` | 970 | 98.08% |
| `name_en` | 471 | 47.62% |

### maker / label / series

| 表 | 行数 | `name_ja` 非空 | `name_en` 非空 |
|---|---:|---:|---:|
| derived_maker | 6,874 | 6,850（99.65%） | 4,883（71.04%） |
| derived_label | 13,141 | 12,789（97.32%） | 9,327（70.98%） |
| derived_series | 102,250 | 101,003（98.78%） | **38,798（37.94%）** |

日文名基本全覆盖；英文名能看不能用（series 只有 38%）。

---

## 7. gallery（剧照）字段形态与张数分布

`gallery_*` 四个字段都是**相对路径**（不带协议/域名/`.jpg` 后缀），first/last 是
同一组图的起止序号：

- `gallery_full_first` / `gallery_full_last`：大图，形如
  `digital/video/100tv00031/100tv00031jp-1` ~ `…jp-5`
- `gallery_thumb_first` / `gallery_thumb_last`：小图，形如
  `digital/video/100tv00031/100tv00031-1` ~ `…-5`
- 非空 1,111,906 行（58.09%），**802,267 行（41.91%）整组为空**。
- 相对路径有两种形态：带 `jp-` 的 533,705 行（digital 系），不带 `jp-` 的
  578,201 行（`digital/amateur/…-1` 等素人系）；两种都按「相对路径 + `.jpg`」拼。
- `first == last` 表示 1 张；`last` 序号为 `-0` 的行有 **12,768 行**，实测是
  「占位（没有剧照）」，应记 0 张。
- 无解析失败的 pair（unparsable = 0）。

**张数分布（`last - first + 1`，实测）**：

| 张数区间 | 行数 | 说明 |
|---|---:|---|
| 0 张 | 12,768 | `last` 是 `-0` 占位 |
| 1–5 张 | 83,864 | |
| 6–10 张 | 219,607 | 峰值在 10 张（181,359 行） |
| 11–15 张 | 117,854 | 峰值在 12 张（50,127 行） |
| 16–20 张 | **592,053** | 峰值在 20 张（545,022 行，DMM 常见上限） |
| 21+ 张 | 85,760 | 峰值在 40 张（83,668 行，双页/合集） |

样例（可直接拼 URL 验证）：

| first | last | 张数 |
|---|---|---:|
| `digital/video/100tv00031/100tv00031jp-1` | `digital/video/100tv00031/100tv00031jp-5` | 5 |
| `digital/video/100tv00059/100tv00059-1` | `digital/video/100tv00059/100tv00059-0` | 0（占位） |
| `digital/video/140m00919/140m00919jp-1` | `digital/video/140m00919/140m00919jp-12` | 12 |

> 结论：剧照最多 40 张，且 58% 的条目才有序号；下载器要按「实际存在的张数」
> 下载，别按固定 20 张死循环。

---

## 8. 图片 URL 拼法验证（实测 HEAD）

规则验证：**`https://pics.dmm.co.jp/<dump 里的相对路径>.jpg`**（相对路径已含
`digital/...` 或 `mono/...`，不补前导斜杠）。

从 dump 抽取样本，逐个单独发 `HEAD` 请求（走本机代理、Chrome UA、不批量）：

| # | 字段 / 相对路径 | HTTP | 返回 |
|---:|---|---:|---|
| 1 | `jacket_full_url` = `digital/video/100tv00031/100tv00031pl` | **200** | image/jpeg, 52,720 B |
| 2 | `jacket_full_url` = `digital/amateur/bzdc020/bzdc020jp`（素人系，尾部 `jp`） | **200** | image/jpeg, 37,578 B |
| 3 | `jacket_thumb_url` = `digital/video/100tv00031/100tv00031ps` | **200** | image/jpeg, 9,197 B |
| 4 | `gallery_full_first` = `digital/video/111mh00066/111mh00066jp-1`（2024 年片） | **200** | image/jpeg, 62,492 B |
| 5 | `gallery_full_last` = `digital/video/111mh00066/111mh00066jp-20` | **200** | image/jpeg, 55,029 B |
| 6 | `gallery_full_first` = `digital/video/100tv00031/100tv00031jp-1`（2019 年片） | **302** | `location: https://pics.dmm.com/mono/movie/n/now_printing/now_printing.jpg`（占位图=CDN 上已无此图） |
| 7 | `gallery_full_first` = `digital/video/118ppt00032/118ppt00032jp-1` | **302** | 同上 |

结论：

1. **URL 拼法成立**：`https://pics.dmm.co.jp/<相对路径>.jpg`，封面 full/thumb、
   剧照 full/thumb 全部同一套规则（实测 5 个真实图片 200，content-type 均为
   image/jpeg）。
2. `jacket_full_url` 尾部实测有三种：`pl` 1,781,606 行、`jp` 74,240 行
   （`digital/amateur` 素人系）、`ps` 53,854 行；`jacket_thumb_url` 尾部
   `ps` 1,835,460、`js` 38,705、`jm` 25,998、`jp` 9,537 —— **都不影响拼法**，
   一律 `.jpg`。
3. **坑**：剧照字段非空 ≠ 图一定在。旧片/下架片的剧照会 302 到
   `now_printing.jpg` 占位图（`content-type: text/html`）。图片下载器必须把
   「302 + location 含 now_printing」或「content-type 不是 image/*」判定为缺失，
   否则会把占位图存成封面/剧照。
4. 封面几乎不会缺（99.77% 有字段，实测 200），剧照缺失风险明显更高（41.91% 无字段
   + 下架 302）。

---

## 9. 有没有中文内容？——没有

| 检查项 | 实测结果 |
|---|---|
| dump 的 19 张表里有没有 zh 字段/中文列 | 没有。只有日文原文（`*_ja`）与英文（`*_en`） |
| 唯一的翻译表 `machine_translation`（560,622 行） | 只有 `source_ja → target_en`（日→英），**没有中文** |
| `machine_translation.target_en` 里含 CJK 的行 | 308 / 560,622（0.055%），都是机翻没翻干净的日文残留（如 `《中出し》`、`痴女`） |
| `machine_translation.source_ja` 含仅简中字 | 0 |
| staging 全量 1,914,173 行 `title_ja`/`title_en` 扫「仅简中字」集合（这/个/说/时/对/发/种/样/还/进/动/长/门/问/间/给/从/马/龙/龟/尔） | **1 行**（`s862aztro00040` 的日文标题里出现 `个`），可视为噪声/错标 |
| `title_en` 含 CJK（日文假名/汉字混入） | 34 / 1,914,173（0.0018%），全是英文标题里嵌的日文人名（如 `[[[朝日奈あかり]]]`、`川 Sayuri Minami`） |
| `comment_en` 含 CJK | 1 行 |
| `category`/`maker`/`label` 的 `name_en` 含 CJK | 0 / 0 / 0 |
| `series.name_en` 含 CJK | 1 行（`PERFECT CHOICE RIBON組`） |
| `derived_category.name_ja` | 970/989 非空，日文题材权威可用 |

结论：**r18 dump 提供「日文原文 + 英文（少量）」；中文标题、繁中题材、评分都
必须另外从 JavDB 拿（与 `research/metadata-sources.md` 的结论一致）**。中文标题
只能机翻/人工，dump 帮不上。英文标题只有 22.52%，也不能当主标题。

---

## 10. 191 万部 vs 用户 2–4 万部：命中潜力评估

### 10.1 容量

| 指标 | 数值 | 相对用户 2–4 万部 |
|---|---:|---|
| dump 影片总数 | 1,914,173 | 48–96 倍 |
| 有 dvd_id 可匹配 | 1,526,052（79.72%） | 38–76 倍 |
| **去重精确番号数** | **1,170,727** | **29–58 倍** |
| release_date ≥ 2010 的片 | 1,574,169 | 39–79 倍 |
| release_date ≥ 2010 的去重番号 | 899,696 | 22–45 倍 |
| release_date ≥ 2015 / ≥ 2020 | 1,204,148 / 738,266 | — |

现代片库（用户大概率是 2010 后为主）的番号容量是用户规模的 20 倍以上 → **数据侧
不存在「量不够」的问题**。

### 10.2 主流厂牌在 dump 里的存在量（直接证明覆盖主流有码）

| 番号前缀 | 条数 | | 番号前缀 | 条数 |
|---|---:|---|---|---:|
| SSIS | 1,839 | | MEYD | 1,864 |
| SSNI | 2,242 | | MIDE | 2,005 |
| IPX | 2,061 | | SNIS | 2,011 |
| ABP | 3,768 | | JUQ | 947 |
| STARS | 2,654 | | DASD | 2,064 |
| MIDV | 1,365 | | ADN | 1,205 |
| PRED | 1,201 | | HND | 2,708 |
| JUL | 2,012 | | T28 | 1,471 |
| WAAA | 719 | | SONE | 1,349 |

上述是「前缀精确匹配」的下限（不含 `SSIS001` 这类无横线写法，也不含子厂牌），
实际覆盖只会更多。**主流 DVD/配信渠道的有码番号基本齐全。**

### 10.3 明确的覆盖盲区（必须单独管道）

| 类别 | dump 里条数 | 判断 |
|---|---:|---|
| FC2（`FC2-PPV-*`） | **0**（dvd_id 与 content_id 双查） | r18 不收录，必须独立管道 |
| HEYZO | **0** | 同上 |
| 1pondo / 10musume | **0** | 同上 |
| Caribbean | **0** | 同上（无码） |
| DMM 素人（content_id `h_*`） | 339,923 部 | 有数据，但**大部分 dvd_id 为空**，只能按 content_id 匹配 |
| SIRO / TOKYO-HOT | 120 / 605 部 | 少量收录 |
| ebook 渠道（无 dvd_id） | 111,561 部 | 只能按 content_id |

### 10.4 结论（给决策用）

1. **数据容量足够**：1,170,727 个去重番号 vs 用户 2–4 万部，主流厂牌齐全；
   只要用户的片子是「DMM/FANZA 有码流通的 DVD/配信番号」，dump 底座能覆盖，
   不需要逐部爬网页。
2. **命中率上限取决于「番号解析器 + 文件里的番号形态」，不是 dump**：
   - 两级键（精确 `dvd_id_key` → 补零 `dvd_id_norm` → `content_id_norm` 兜底）
     已实测可吃 `SSIS-001` / `ssis001` / `SSIS001` / `ssis-001` / `IHKD-6` 等写法；
   - 但**拿不到用户真实文件名样本前，无法量化百分比的命中率**（PLAN M0 的
     200–1000 个文件名样本就是这个目的）。本报告只给容量与盲区，不给估算百分比。
3. **覆盖判断**：如果用户库以有码主流（DVD/配信）为主 → **r18 dump 覆盖他的库，
   可以直接在本地建底座**；如果他的库中 FC2/无码/素人占比高（自购素人、FC2 收藏
   党）→ dump 对这部分**完全不覆盖**，必须按 PLAN 走 FC2/无码独立管道，dump
   只负责有码部分。
4. **实测 lookup（真实 `data/r18dump.db`）**：

| 输入 | 命中 | 说明 |
|---|---:|---|
| `SSIS-001` / `ssis001` / `SSIS001` | 2 行 | 同一番号的 digital + mono 两个版本（`ssis00001`、`ssis001`） |
| `IPX-001` | 2 行 | `ipx00001`、`ipx001` |
| `ABP-123` | 1 行 | `118abp00123` |
| `MIDV-001` | 5 行 | 含 `MIDV001` 无横线写法 |
| `STARS-001` | 2 行 | `1stars00001`、`1stars001` |
| `FC2-PPV-1234567` | **0 行** | 预期内：dump 不覆盖 FC2 |
| `SSIS-99999`（不存在的号） | 0 行 | 不误命中 |

8 个番号一次批量查询耗时 **5 ms**。

---

## 11. 对主库物化（M4）的直接建议

1. **只物化命中的片子**：用户 2–4 万 → 主库 `title` 最多 2–4 万行；staging 1.74 GiB
   独立存放，主库保持轻量（当前 `data/avlib.db` 240 KB）。
2. **去重规则**：同一精确番号在 dump 里有 260,106 组「多版本」（digital/mono/rental
   各一个 `content_id`），物化时按 `dvd_id_key` 聚合，建议优先级：
   `site_id=2` 且 `service_code='digital'` → `title_ja` 非空 → `jacket` 非空 →
   `release_date` 较新；不要为三个版本建三条 `title`。
3. **显示名**：`title_zh or title_ja or title_en or dvd_id`；英文只有 22.5%，中文
   需要 JavDB/MT，别指望 `title_en`。
4. **演员名**：`name_kanji or name_kana or name_romaji`（kanji 只有 64.9%）；
   头像只有 38.8%，JavBus/JavDB 补。
5. **题材**：`derived_category.name_ja` 98.08% 可用，直接做 `genre_ja`；繁中题材
   从 JavDB 补。
6. **图片**：封面直接 `pics.dmm.co.jp/<jacket_full_url>.jpg`；剧照按
   `gallery_image_count` 算出的张数逐张下，且把 302/now_printing 判定为缺失。
7. **匹配接口**：`app/services/r18.py` 已提供 `open_dump_db()` /
   `lookup_by_dvd_ids()` / `dvd_id_key()` / `dvd_id_norm()` / `content_id_norm()` /
   `gallery_image_count()`，M4 直接复用即可。

---

## 12. 复现方法与统计口径

```sh
# 1) 导入（系统 python3 即可，导入器只用标准库）
cd av-archive
python3 -m scripts.import_dump \
  --sql /tmp/r18dump/latest.sql \
  --out data/r18dump.db \
  --stats-json research/dump-stats.json

# 2) 只对已有 staging 库重算覆盖率
python3 -m scripts.import_dump --stats-only data/r18dump.db --stats-json research/dump-stats.json

# 3) 测试
.venv/bin/python -m pytest tests/test_import_dump.py -q     # 75 passed
```

- 所有行数 = `SELECT COUNT(*)`；非空 = `IS NOT NULL AND TRIM(col) <> ''`；
  比例保留 4 位小数。原始 JSON：`research/dump-stats.json`。
- 前缀分布、gallery 张数分布、CJK/简中字扫描是 Python 流式遍历（不是抽样），
  遍历全表。
- `meta` 表记录了 `dump_file` / `dump_date` / `dump_sha256` / `imported_at` /
  `schema_version` / `user_version` 及每张表 `rows:*` / `seconds:*` / `imported:*`，
  可随时用 `SELECT key, value FROM meta` 复核。
- 本次未跑 `ANALYZE`（可选 `--analyze`，大库约 +10–30 s）；如需让 SQLite 生成
  统计信息再跑一次即可，不影响上面任何数字。
