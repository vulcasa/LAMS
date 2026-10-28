# JAV 中文字幕：可批量自动化的来源到底有没有？（调研报告）

- 调研人：subtitle-researcher（task-2）
- 日期：2026-10-26
- 场景：2–4 万部 JAV，NAS + LTO-6 纯归档、不播放，但字幕要能随片库检索/管理；已接受刮削，NAS 24h 在线。
- 结论先行：**A 路（找现成外挂中文字幕批量落地）在"全覆盖"意义上不成立——没有任何站点提供"按番号索引"的中文字幕库；但存在 2 个可以半自动化的零散来源（迅雷字幕库公开接口、SubtitleCat），命中率抽查约 2/7 与 5/5（含模糊误命中），只能当"捡漏"。真正可规模化的路是 B 路：本地 Whisper 生成日文字幕 + LLM 翻译，按需/分级跑。**

> 网络：本机走代理探测（代理地址按工作区 AGENTS.md 约定不写进产物，下文命令一律用 `$HTTPS_PROXY`）。**每个来源只发 1–2 个单请求**，未做批量抓取；抽查样本 n≤8，只用于判断"有没有"，不能当统计结论。

---

## 0. 一页结论

| 问题 | 结论 |
|---|---|
| JAV 中文字幕有可靠的外挂 srt 来源吗？ | **没有**。没有站点做"番号 → 全量中文字幕"的索引；能查到的是网友零散上传，覆盖率、命名、质量都不稳定。 |
| 有没有部分可自动化的来源？ | 有 2 个：**迅雷字幕库非公开接口**（JSON，无 key，实测能按番号返回真中文 srt）、**SubtitleCat**（公开 HTML，提供 zhTW 等 MT 变体）。两者都被现成开源插件当作数据源，说明是事实标准。 |
| 覆盖率量级？ | 抽查（n=7~8 番号）：迅雷 2/7 命中、assrt 1/8 命中、SubtitleCat 5/5 有结果但含模糊误命中；SubHD 0/1、OpenSubtitles 基本无 JAV。真实覆盖率需 500–1000 部抽样，见"未解决问题"。 |
| 中文字幕的主流流通形态？ | **硬字幕重封装版本**。JavDB 对这类发布直接打「含中字磁鏈 / 中字可播放」标记——即字幕是烧进视频的，不是独立 srt。 |
| 那 2–4 万部怎么落地？ | **分级策略**：L0 刮"有无中字版"元数据 → L1 对能命中的用迅雷/SubtitleCat 捡漏（人工抽检）→ L2 对要检索的按需 WhisperJAV 生成 ja.srt + LLM 翻译 zh.srt，文本入全文索引。全量生成不现实（见 §2.4 成本表与 §3.2）。 |
| 成本量级（关键数字） | 单部 2h：本机 M2 约 8 min（实测 15x 实时）；RTX 3060 级跑 WhisperJAV Faster 模式 6–10 min。**3 万部 ≈ 3,000–5,000 GPU 小时，单卡连续跑 125–208 天**。翻译只占零头：一部约 2h44m 正片的实际中文字幕仅 **852 条 / 6,170 汉字**（首末 cue 跨度 161 min）。 |

---

## 1. A 路：现成外挂中文字幕源逐个评估

### 1.1 总表（2026-10-26 实测）

| 源 | 按番号可检索? | 需登录? | 有 API? | 实测可达? | 抽查覆盖率 | 质量 | 自动化难度 / TOS 风险 |
|---|---|---|---|---|---|---|---|
| **迅雷字幕库**（`api-shoulei-ssl.xunlei.com/oracle/subtitle`） | 部分可以（模糊名匹配，非精确番号索引） | 否 | 有，但**非公开开放**（插件逆向所得 GET 接口） | ✅ HTTP 200，返回 JSON + 可直接下 srt | 2/7（SSNI-001→3 条、ABP-001→10 条；SSIS-001/MIDE-001/JUQ-001/IPX-001→0） | 未知。标注"（网友上传）"，文件里有 `mt` 字段但语义未公开；抽到的 srt 是正常中文、852 条/161 min，可读性尚可 | 低（一个 GET）。**风险高**：未公开接口随时变更；无授权、限速未知；内容版权 |
| **SubtitleCat**（subtitlecat.com） | 部分可以（模糊搜索） | 否（"无需注册、无需 API 密钥"——被 PotPlayer 插件 README 明确写明） | 无公开 API，HTML 抓取 | ✅ HTTP 200；站点自报 16,872,371 条字幕 | 5/5 有结果（SSIS-001 4 条 / MIDE-001 2 / ABP-001 6 / JUQ-001 10 / NIMA-014 1）；**相关度未逐条验证** | 差–中。结果名形如 `ssis-001-zhTW`、`SSIS-001 eng`，页面标注 "translated from English" → **机翻链**（EN→zh），不是日文原声直译 | 中（要解析 HTML、去重、判相关度）。TOS 未授权批量抓取；成人内容 |
| **assrt.net（射手网·伪）** | 可以按关键词（番号）搜，但命中极少 | API 需注册拿 32 位 Token（免费）；网页下载有登录入口 | ✅ 正式公开 API：`api.assrt.net/v1/sub/{search,detail}` 等 | ✅ 首页/api 文档 200 | 1/8（仅 SSNI-001 命中 1 条：SSA、简体、2020-03-13、345 次下载；SSIS-001/MIDE-001/IPX-001/STARS-001/JUQ-001/ABP-001/PRED-001 均 0 条） | 该条为转载；整体"零星、偏老（2020 年前后）" | 中（要 Token + detail 再拿带时效下载地址）。**配额：20 次/分钟，且 token 与 IP 共享**。非商用免费，需署名 |
| **SubHD**（subhd.tv） | 搜得到番号但 0 结果（目录是电影/剧集） | 下载需登录（页面有登录/注册） | 无官方 API | ✅ HTTP 200；`/search/SSIS-001` → "共 0 条" | 0/1 | — | 不适合 JAV。站点自己提示"可能被墙"，备用域名 subhd.me / subhd.one |
| **OpenSubtitles** | 理论可（query 检索） | 下载/查询需 Api-Key + 账号 | ✅ 官方 REST API | ⚠️ `/api/v1/subtitles` 无 key **403**；`opensubtitles.org` 307 跳转 | 基本为 0（JAV 不在其作品库；需 key 才能进一步验证） | — | 高门槛（key 申请 + 限次）。JAV 无实际价值 |
| **SubScene** | — | — | — | ❌ 403（站点长期不稳定/已不再服务，**未独立核实关站事实**） | 0 | — | 放弃 |
| **字幕库（zimuku 类）** | — | — | — | ❌ 根域 404（域名已变/停运，未核实新域名） | 0 | — | 放弃 |
| **JavDB** | 可按番号查（元数据），**不提供字幕文件** | 否（网页） | 无官方 API（有社区爬虫） | ✅ HTTP 200 | 不是字幕源；但会标「含中字磁鏈 / 中字可播放」 | — | 用来判断"该番号有没有中字硬字幕版"，不是拿 srt |
| **论坛 / Telegram 频道 / 网盘合集**（色花堂、1024、各类"中文字幕合集"） | 靠人肉/合集包，无索引 | 是 | 否 | 未探测（需账号，不适合脚本探测） | 不可估 | 参差 | **只能人工**。邀请码/验证码/网盘限速，无法规模化 |
| **流媒体站（MissAV/Jable 等中文字幕版）** | 站点内有中字版本 | 否 | 否 | 未探测 | — | — | 有社区脚本从其页面取字幕（JavSubtitleScraper README 引用的 SleazyFork 脚本）；**未验证**，且站点反爬强 |

### 1.2 逐项要点

**① 迅雷字幕库接口（最实用的"捡漏"源，但别当基础设施）**

PotPlayer 插件源码里写死了接口（`SubtitleSearch - SubtitleCat.as`）：

```
GET https://api-shoulei-ssl.xunlei.com/oracle/subtitle?gcid=&cid=&name=<关键词>
```

- 返回 `{"code":0,"result":"ok","data":[{...}]}`；每条含 `url`（`.srt`/`.ass`，托管在 `subtitle.v.geilijiasu.com`）、`name`、`duration`（毫秒）、`languages`、`source`、`mt`、`extra_name`（如"（网友上传）"）。
- 实测 `name=SSNI-001` → 3 条；`name=ABP-001` → 10 条；`name=SSIS-001` / `MIDE-001` / `JUQ-001` / `IPX-001` → 0 条。
- 下载下来的 srt 是真中文（示例首行 "超级大乱交特别篇"），**852 条字幕、6,170 汉字、覆盖 161 分钟**。
- **匹配是模糊的**：`name=SSIS-001-C_c` 返回了一条 `hhd800.com@ssis00001hhb.srt`（明显不相关）→ 必须做番号正则校验 + 时长校验（插件里也有"时长过滤"开关，默认关）。
- 注意：这是**未公开接口**，不是迅雷开放平台产品；无文档、无限速承诺，随时可能失效。合规上属于灰色。

**② SubtitleCat（覆盖面最广，但质量最差）**

- 公开、免注册、免 key，被 `736368217/PotplayerJavChineseSubs_subtitlecat`（139★）直接当数据源。
- 实测 `index.php?search=<番号>`：SSIS-001→4 条（`ssis-001-zhTW` / `SSIS-001 T` / `SSIS-001 eng` / `SSIS-001`）、MIDE-001→2、ABP-001→6、JUQ-001→10、NIMA-014→1。
- 结果里明确写 "translated from English" → 主链是 **英文字幕再机翻成 50 种语言**。对 JAV 而言 = 日文语音 →（别人）英译/ASR → 机翻中文，**双重损耗**，术语和语气基本不可用，只适合"知道大概在说什么"。
- 无 API，HTML 结构易变；抓取需节流。

**③ assrt.net（唯一"正规"API，但 JAV 只是顺带）**

- 官方 API 文档：<https://assrt.net/api/doc>。要点（已从文档核实）：
  - 域名 `api.assrt.net`（备用 `api.makedie.me`），Token 32 位，注册后免费获取；
  - **配额 20 次/分钟，且同一 IP 与同一 token 共享**；
  - `sub/search` 参数 `q`（≥3 字符）、`pos`、`cnt`（最多 15）、`is_file`（按视频文件名搜）、`no_muxer`（忽略压制组/参数）；
  - `sub/detail` 返回**带时效的下载地址**（文档明确"每次生成唯一、有有效期，不要保存"）。
- 实测：`SSNI-001` 命中 1 条（SSA、简体、转载、2020-03-13、2471 次查阅/345 次下载）；另外 7 个常见番号 0 条 → **JAV 覆盖极低且有"年代断层"**。
- 优点：唯一有文档、有配额、可写进流水线的源；缺点：对 JAV 就是低命中。

**④ SubHD / OpenSubtitles / SubScene / 字幕库**

- SubHD 目录是电影剧集，`SSIS-001` → "共 0 条"；页面自带"可能被墙"提示。
- OpenSubtitles 无 key 直接 403；即便有 key，JAV 目录基本不存在（日文字幕都罕见）。
- SubScene 403、字幕库根域 404 → 直接放弃。

**⑤ JavDB：不是字幕源，但是最好的"有没有中字"标记源**

- 实测搜索结果页里对《SSIS-001》直接打标：「含中字磁鏈」「中字可播放」。
- 意义：**它标的是"存在中文字幕版发布"，而不是给你 srt**。这些中字版通常是硬字幕。
- 用途：给片库做"是否已有中字"的元数据列，避免对已有中字版的片子重复生成。

### 1.3 为什么"批量外挂字幕"这条路在结构上不成立

1. **没有番号索引**：上传者用中文译名/罗马字/网站前缀命名。实测迅雷返回过 `hhd800.com@ssis00001hhb.srt` 这种名字，番号都被拆散了，精确匹配无从谈起。
2. **中文主要以"硬字幕版视频"流通**：JavDB 的「含中字磁鏈/中字可播放」标记就是证据——字幕烧在片子里，不单独发 srt。想从硬字幕里拿文本只能 OCR（需先有中字版视频 + 字幕区定位 + 高错误率），比重新 ASR 贵且差。
3. **主流字幕站不收录 JAV**：SubHD 0 条、射手（assrt）低命中、OpenSubtitles 基本空。它们的目录体系（电影/剧集/IMDb）天然排除 JAV。
4. **剩下的只有"网友零散上传"**：迅雷字幕库、SubtitleCat。无索引、无保证、可能被删；站点还可能随时关（SubScene 403、字幕库 404 就是前车之鉴）。
5. **能自动化的部分别人已经做了**：Emby 插件 `ipx-666/JavSubtitleScraper`、PotPlayer 插件、GitHub 上的 `Jendag/JavSubtitle` 都是「番号 → 迅雷/SubtitleCat」这一套。**没有第三个更好的源**——这就是天花板。

> 结论：A 路只能作为"命中即用"的捡漏层，必须配人工抽检；**不能承诺 2–4 万部的字幕覆盖**。

---

## 2. B 路：本地生成（推荐主路）

### 2.1 不要从零拼，直接用 WhisperJAV

[`meizhong986/WhisperJAV`](https://github.com/meizhong986/WhisperJAV)（2,376★，2026-10 仍在活跃更新）是专为 JAV 设计的中/日字幕生成器，正好覆盖本场景：

- **JAV 音频的三个失真点**（README 原话）：低信噪比 + 大量非语言发声（喘息/气声，频谱像 `fu` 等音节，会被"听成词"）、音量剧变、长时注意力崩塌导致重复/幻觉。
- 对策：场景切分（scene segmentation）、VAD 夹紧（只喂有人声的片段）、防御式解码 + 日语后处理；还有 `--skip-existing`、批量文件夹、GUI、Colab/Kaggle 笔记本。
- **自带翻译**：`whisperjav-translate`，provider 支持 Ollama（本地）、Local LLM（llama.cpp）、DeepSeek、Gemini、Claude、GPT、OpenRouter、GLM、Groq、任意 OpenAI 兼容端点；tone 里有专门的 **Adult-Explicit（针对 JAV 对白调过）**。文档还给了 Ollama 显存对应模型建议：8 GB→`qwen2.5:7b`，12 GB→`gemma3:12b`，16 GB+→`qwen2.5:14b`。
- macOS/Apple Silicon 有专门安装文档（本机是 M2 8 GB，可跑但纯 CPU 会慢；速度不确定，见 §2.2 与"未解决问题"第 7 条）。
- 输出 `.srt`，可两段式：先只做 ASR 得 `ja.srt`，之后再批量翻译（省得改了翻译要重转写）。

### 2.2 本机实测速度（硬证据）

工作区 `语音转文字/` 的 `transcribe.py`（mlx-whisper + `whisper-large-v3-turbo`，Apple M2 / 8 GB）实测：

| 音频 | 时长 | 耗时 | 实时倍率 |
|---|---|---|---|
| 不当好人当情人1 | 30:31 | 2:01 | **15.0x** |
| 不当好人当情人2 | 56:08 | 3:47 | **14.8x** |
| 不当好人当情人3 | 3:12 | 0:13 | **14.4x** |

证据：`语音转文字/输出/transcribe.log`。注意这是**中文 + 干净人声**的成绩；换成 JAV 日语 + BGM/气声，倍率会掉（WhisperJAV 文档说"难音频再乘 1.2–1.5 倍耗时"）。

模型事实：`whisper-large-v3-turbo` 把解码层从 32 层砍到 4 层，809M 参数，"way faster, minor quality degradation" —— <https://huggingface.co/openai/whisper-large-v3-turbo>。

### 2.3 外部基准：WhisperJAV 官方耗时表（RTX 3060 12 GB / Colab T4）

来源：<https://github.com/meizhong986/WhisperJAV/blob/main/docs/en/faq.md>

| 模式 | 30 min 片段 | 2h 正片 | 说明 |
|---|---|---|---|
| Faster | 2–4 min | **6–10 min** | turbo-int8-batch8，最快 |
| Fast | 3–5 min | 8–15 min | turbo-int8 |
| Balanced（推荐默认） | 4–7 min | 15–25 min | large-v2-int8 |
| Fidelity | 10–15 min | 40–50 min | large-v2-fp16，抖动大 |
| Ensemble | 15–25 min | 60–90 min | 两遍合并 |

- RTX 3090/4080/4090 再快 1.5–3x；纯 CPU "30 分钟片段要 30–60 分钟"，多小时内容不实用。
- 交叉验证：3060 的 Faster 模式（6–10 min / 2h ≈ 12–20x）与本机 M2 实测 15x 同一个量级 → 两套数据互相印证，可作为估算基线。

### 2.4 全量成本：3 万部 / 4 万部（每部按 2h）

音频总量：3 万部 × 2h = **60,000 小时**；4 万部 = **80,000 小时**。

| 方案 | 单部 2h 耗时 | 3 万部 GPU 小时 | 单卡连续天数 | 4 万部 GPU 小时 | 单卡连续天数 |
|---|---|---|---|---|---|
| 本机 M2 8 GB（mlx turbo，实测 15x） | ~8 min | ~4,000 h | **~167 天** | ~5,330 h | ~222 天 |
| RTX 3060 级 · WhisperJAV **Faster** | 6–10 min | 3,000–5,000 h | **125–208 天** | 4,000–6,670 h | 167–278 天 |
| RTX 3060 级 · WhisperJAV **Balanced** | 15–25 min | 7,500–12,500 h | **312–521 天** | 10,000–16,670 h | 417–694 天 |
| RTX 4090 级 · Faster（按官方"高端卡快 1.5–3x"折算） | 2–7 min | 1,000–3,300 h | **42–139 天** | 1,330–4,440 h | 55–185 天 |
| RTX 3060 级 · **Ensemble**（质量最好） | 60–90 min | 30,000–45,000 h | **3.4–5.1 年** | 40,000–60,000 h | 4.5–6.8 年 |

结论：
- **全量 Ensemble 不现实**；全量 Faster 单卡也要 4–7 个月，且还没算人工抽检。
- 若"30 天内出全量" → 3 万部需 1,000–3,300 机器时 ÷ 720 h ≈ **2–5 张 4090 级并行**（实际留余量按 4 张起算），或租云 GPU（机器时按上表折算，自行乘单价）。
- 电费量级：3060 跑 4,000 h × ~0.17 kW ≈ 680 kWh（估 ¥400–500，按当地电价核对）。相比人工/云租可忽略。
- **隐性成本**：即使全自动，也要抽检。按每部 5 min 抽检，3 万部 = 2,500 小时人工（约 1.5 人年）——这是比 GPU 更贵的部分。所以更要"按需"，别全量。

### 2.5 翻译成本（实测字幕量级 → 几乎可忽略）

实测一部正片（文件时长 164 min，首末 cue 跨度 161 min）的中文字幕：**852 条 cue / 1,087 行 / 6,170 汉字**（数据来自迅雷字幕库返回的真实 srt，`SSNI-001`）。

| 项 | 数值 |
|---|---|
| 每部日文字幕 token（估） | ~7k 输入 + ~7k 输出 ≈ 14k |
| 3 万部总量 | ≈ 4.2 亿 token |
| 云 API 费用（按 ¥1–10/百万 token 折算） | **≈ ¥420–4,200 总量**，即 **¥0.01–0.14/部** |
| 本地 Ollama | **0 元**；8 GB→`qwen2.5:7b` 约 5–15 min/部（估，未实测）；12 GB→`gemma3:12b` 更快 |
| 占 ASR 耗时比例 | **<5%**（ASR 才是瓶颈） |

> 也就是说：**贵的是"把日文听出来"，不是"翻成中文"**。所以策略上应该先解决 ASR 吞吐，翻译可以慢慢补、可以换便宜模型、可以以后重翻。

### 2.6 质量现实（别期待"可直接发布的字幕"）

- WhisperJAV 自己的定性描述：JAV 音频会让 Whisper 家族"听到不存在的词"、长时重复/幻觉。它靠 VAD + 场景切分 + 置信度过滤压制，但"结果仍随音源质量波动"（原文：*Results still vary with source audio quality.*）。
- 日语 ASR 本身的同音/专有名词错误 → 翻译会放大。**必须抽检**；本机 `transcribe.py` 已有"专有名词提示词 + OpenCC 繁转简 + 标点归一"的可复用套路，可搬到日语（`--language ja`）。
- 但本场景是**纯归档 + 检索**，不是给人看的成品字幕：只要"这句话大概在说什么"能被检索到，质量门槛比"发行级字幕"低得多。这一点会大幅降低返工成本。

---

## 3. 推荐方案（针对"不播放、纯归档、字幕要能随片库检索"）

### 3.1 三层策略：L0 标记 → L1 捡漏 → L2 按需生成

```
                    ┌─ L0 元数据层（便宜、必做）
                    │  刮 JavDB：番号、标题、发行日、女优、以及「含中字磁鏈/中字可播放」标记
                    │  → 写入片库 DB：has_chinese_sub_release = true/false
                    │
番号 ───────────────┼─ L1 捡漏层（低成本、可选、需抽检）
                    │  迅雷字幕库接口（模糊名匹配 + 番号正则 + 时长校验）
                    │  SubtitleCat（HTML 抓取，只取 zh 系，标记 quality=mt）
                    │  assrt API（Token，20 req/min，低命中但合规）
                    │  → 命中即落盘 <番号>.zh.srt，标记 source + 置信度
                    │
                    └─ L2 生成层（主力、按需/夜间队列）
                        WhisperJAV：ASR → <番号>.ja.srt → LLM 翻译 → <番号>.zh.srt
                        再抽纯文本 → 全文索引
```

**关键设计：字幕文件只是副产品，真正要的是"可检索的文本"。** 用户不播放，那么每部片子真正被消费的是：番号 + 标题 + 日期 + 女优 + **台词中文文本**。所以：

- 落盘规范建议（与现有片库刮削命名对齐）：
  - `<番号>.ja.srt`（ASR 原始，保留，便于日后重翻）
  - `<番号>.zh.srt`（中文，供检索/未来播放器用）
  - `<番号>.zh.txt`（srt 去时间轴的纯文本，索引用）
  - `<番号>.sub.json`（来源、模型、模式、耗时、抽检结论）
- 索引：SQLite FTS5 最省事；量大用 Meilisearch/Elasticsearch。NAS 24h 在线正好常驻一个索引服务，字幕文本随片入库即可搜。

### 3.2 为什么"按需"而不是全量

1. 全量 Faster 单卡 4–7 个月（§2.4），期间机器不能干别的；按需则随时可停。
2. 抽检人工成本才是大头（全量 ≈2,500 h）。
3. 纯归档场景，绝大多数片子永远不会被检索；需求集中在"某女优 / 某题材 / 某关键词"时再生成，命中率更高。
4. **建议的落地节奏**：
   - 第 1 周：抽 **100 部**（覆盖不同年代/厂牌/音质）跑 WhisperJAV，**先定模式**（看 Faster 是否够用，还是必须 Balanced），同时用这 100 部校准"抽检 1 部要几分钟"。
   - 第 2 周起：常驻夜间队列（`--skip-existing`），每晚跑 8h → 约 50–80 部/晚（Faster/3060 级），一年约 1.8–2.9 万部。
   - 并行：L0 元数据 + L1 捡漏，先把"已有中字版"的片子标出来，避免重复生成。
5. 若必须短期内全量：租 2–5 张（留余量按 4–8 张）4090 级云 GPU 跑 Faster，按 §2.4 折算机器时（3 万部约 1,000–3,300 机器时）。

### 3.3 现成可复用的轮子（别自己造）

| 工具 | 用途 | 地址 |
|---|---|---|
| **WhisperJAV** | JAV ASR + AI 翻译（GUI/CLI/批量/`--skip-existing`） | <https://github.com/meizhong986/WhisperJAV> |
| **JavSubtitleScraper**（Emby 插件） | 番号 → 迅雷/SubtitleCat 自动下载中文字幕；可作 L1 参考实现 | <https://github.com/ipx-666/JavSubtitleScraper> |
| **PotplayerJavChineseSubs_subtitlecat** | 迅雷 + SubtitleCat 的接口/解析参考（含接口 URL、时长过滤） | <https://github.com/736368217/PotplayerJavChineseSubs_subtitlecat> |
| **本工作区 `语音转文字/`** | 已跑通的 mlx-whisper 批量转写（15x 实时、srt+txt+时间轴+json 四件套） | 本地 `语音转文字/transcribe.py` |
| JavDB 社区爬虫 | L0 元数据/中字标记 | 例：<https://github.com/TongWu/JAVDB_AutoSpider> |

### 3.4 明确不推荐

- **全量 Ensemble 跑 3 万部**：3.4–5.1 年，得不偿失。
- **对硬字幕做 OCR**：得先有中字版视频，还要字幕区定位 + 时间轴重建，错误率高于重跑 ASR，纯属绕远路。
- **买"字幕合集"网盘**：来源不明、命名混乱、无法验证、时效差，且涉及侵权传播风险。
- **指望"再等等就有人做全集"**：这类资源十年了还是零散上传，结构性原因（§1.3）不会变。

---

## 4. 风险与合规

- **版权**：字幕是对原作对白的翻译，仍属衍生作品；迅雷/SubtitleCat/assrt 上的 JAV 字幕基本都是未授权转载。个人归档风险自担，**不要商用、不要再分发**。
- **接口稳定性**：迅雷接口未公开、SubtitleCat 无 API、assrt 有配额——L1 层必须设计成"失败即跳过"，绝不能让它成为阻塞依赖。
- **成人内容**：本报告只讨论技术可得性；抓取/存储需遵守当地法律与站点条款。
- **代理**：按工作区 AGENTS.md，探测走本机代理；**代理地址/端口不写入任何产物**，脚本里用环境变量 + 默认值。

---

## 5. 未解决问题清单

1. **用户已有片库中，多大比例已经是硬字幕中字版？** 这直接决定"还需不需要生成"。建议先抽 200 部人工看：有中字版的标 `has_chinese_sub_release=true` 直接跳过（不 OCR，理由见 §3.4），无的进生成队列。（未评估）
2. **迅雷字幕库的真实覆盖率**：本报告只抽了 7 个番号（2 命中）。要得到可信区间，需抽 500–1,000 部（含不同年代、厂牌、人气度），并区分"精确命中/模糊误命中"。**这是决定 L1 是否值得做的关键数字，目前没有。**
3. **迅雷返回字幕是否机翻**：字段 `mt`（抽到的值都是 2）语义未公开；需要抽检 10–20 条，与人工字幕对比才能判断。同样，`source` 字段含义未知。
4. **SubtitleCat 的 zh-CN/zhTW 变体来源**：页面写 "translated from English"，但基准是人工英译还是 ASR+MT 未知；相关度（模糊搜索是否命中同番号）也未逐条核验。
5. **assrt 对 JAV 的覆盖上限与活跃度**：抽样 1/8，且命中为 2020 年上传。需要按"近年番号"再抽一批，确认是否已停止收录。
6. **OpenSubtitles 是否真的完全没有 JAV**：无 Api-Key 无法查证（403），结论是基于目录体系的推断。
7. **WhisperJAV 在 Apple Silicon 上的实际速度**：官方 FAQ 只给 NVIDIA/CPU；本机 M2 8 GB 只测过 mlx-whisper 中文（15x），**日语 + JAV 音频 + VAD 前处理**的倍率未测。估 8–12x，未验证。
8. **日语 ASR 在 JAV 上的 CER/可用率**：WhisperJAV 有带 ground-truth 的回归基准（`bench/`），但**未公开数值**；需要自己在 100 部样本上建基线。
9. **L0 元数据落地方式未定**：JavDB 的「含中字磁鏈」是否逐片可靠、爬取频率与反爬策略、以及和现有刮削器（是否已有 JavDB/JavBus 元数据源）如何对接，需要和"刮削方案"那份调研对齐。
10. **存储与索引选址**：字幕/文本/索引放 NAS 哪个卷、LTO-6 上是否要一并归档字幕文件、索引服务跑在哪台机器（NAS 本身算力是否够 SQLite FTS5 + Meilisearch），未评估。
11. **是否需要说话人/片段级检索**：若只做全文检索，纯文本足够；若要"按场景检索"，需要额外场景切分元数据。需求未确认。

---

## 附录 A：实测记录（2026-10-26，单请求，经本机代理）

| 目标 | 请求 | 结果 |
|---|---|---|
| assrt.net | `GET /` | 200 |
| assrt.net | `GET /api/doc` | 200（文档完整：token、20 次/分、search/detail） |
| assrt.net | `GET /sub/?searchword=SSNI-001` | "找到 1 条匹配"，SSA/简体/转载/2020-03-13/345 次下载 |
| assrt.net | 同接口 ×7（SSIS-001, MIDE-001, IPX-001, STARS-001, JUQ-001, ABP-001, PRED-001） | 均"找到 0 条匹配" |
| subhd.tv | `GET /search/SSIS-001` | 200，"共 0 条" |
| api.opensubtitles.com | `GET /api/v1/subtitles` | **403**（无 Api-Key） |
| opensubtitles.org | `GET /` | 307 |
| subscene.com | `GET /` | 403 |
| zimuku.org | `GET /` | 404 |
| javdb.com | `GET /search?q=SSIS-001` | 200；结果项标注「含中字磁鏈」「中字可播放」 |
| subtitlecat.com | `GET /index.php?search=<番号>` ×5 | SSIS-001→4、MIDE-001→2、ABP-001→6、JUQ-001→10、NIMA-014→1 |
| api-shoulei-ssl.xunlei.com | `GET /oracle/subtitle?gcid=&cid=&name=<番号>` ×7 | SSNI-001→3、ABP-001→10；SSIS-001/MIDE-001/JUQ-001/IPX-001→0；`SSIS-001-C_c`→1（不相关） |
| subtitle.v.geilijiasu.com | `GET <srt>` | 52,302 B；852 条 cue / 1,087 行 / 6,170 汉字 / 覆盖 161 min |

## 附录 B：参考链接

- assrt API 文档：<https://assrt.net/api/doc>
- SubtitleCat：<https://www.subtitlecat.com/>（搜索示例 `https://www.subtitlecat.com/index.php?search=SSIS-001`）
- SubHD：<https://subhd.tv/>
- OpenSubtitles API：<https://api.opensubtitles.com/api/v1/subtitles>（需 Api-Key）
- JavDB：<https://javdb.com/>
- WhisperJAV：<https://github.com/meizhong986/WhisperJAV> · 耗时/显存 FAQ：<https://github.com/meizhong986/WhisperJAV/blob/main/docs/en/faq.md> · 翻译指南：<https://github.com/meizhong986/WhisperJAV/blob/main/docs/en/user-guide/translation.md>
- Whisper large-v3-turbo 模型卡（32→4 解码层，更快）：<https://huggingface.co/openai/whisper-large-v3-turbo>
- faster-whisper 基准（RTX 3070 Ti 8 GB，large-v2，13 min → 17 s @ batch8）：<https://github.com/SYSTRAN/faster-whisper#benchmark>
- Emby 字幕插件（迅雷 API + SubtitleCat 参考实现）：<https://github.com/ipx-666/JavSubtitleScraper>
- PotPlayer 插件（同上，含接口 URL）：<https://github.com/736368217/PotplayerJavChineseSubs_subtitlecat>
- JavDB 元数据爬虫示例：<https://github.com/TongWu/JAVDB_AutoSpider>
- 本机实测证据：`语音转文字/输出/transcribe.log`、`语音转文字/方案.md`
