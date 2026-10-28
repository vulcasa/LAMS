# JAV 离线刮削元数据源调研（javbus / javdb / javlibrary / r18.dev / FC2）

> 目标：为「2–4 万部、完全离线、不播放、WebUI 按 演员/导演/番号/片商/系列/题材 筛选、中日双语、要封面+剧照」的归档库选源。
> 结论导向：**推荐源组合 + 每个字段取自哪个源 + 抓取速率 + 反爬绕法**。
> 实测时间：**2026-10-26**（Asia/Shanghai）。标注说明：**实测**＝本次真实发过请求看到响应；**未验证**＝来自文档/源码/社区说明，未亲自请求确认。
> 网络：按工作区 `AGENTS.md` 约定走本机代理（地址不落文档）；本次代理出口地区为**香港**；**不走代理直连时四个站全部超时失败（实测，HTTP 000）**，即当前大陆网络环境必须走代理。

---

## 0. 结论速览（先看这个）

| 角色 | 推荐源 | 理由（一句话） |
|---|---|---|
| **主力元数据（离线，零抓取）** | **r18.dev 每周数据库 dump** | 官方页面明确提供**每周二 03:00–04:00 UTC 更新的 gzip SQL 全量 dump（当前约 262 MiB，CC0 许可）**，`https://r18.dev/dumps/latest` 可直接程序化下载（实测 307 → S3）。日/英双语字段齐全，可整库导入 SQLite 后本地查询，**2–4 万部完全不需要逐部抓网页**。 |
| **中文题材 + 评分 + 中文标签** | **JavDB（HTML）** | 详情页「類別」是繁体中文题材、「評分」带评价人数，还有繁体中文标签体系与系列页；单部详情 = 1 次请求（实测 200），可只对 r18.dev 缺中文/缺评分的条目做补抓。 |
| **兜底 / 中文题材补充 / 演员头像与三围** | **JavBus（HTML）** | 「製作商/發行商/導演/系列/類別」都有，演员头像与女优页（罩杯/三围/爱好）是它独有的；但 **robots.txt 是 `Disallow: /`**，合规风险最高，只作兜底。 |
| **不建议** | **JavLibrary** | 整站 Cloudflare Managed Challenge：连 `robots.txt` 都 403 `cf-mitigated: challenge`（实测），必须 FlareSolverr/真浏览器 cookie，成本高、易封，只适合极少量补评分/评论。 |
| **FC2 / 素人 / 无码** | **javdb（f=fc2）或 javinizer-go 的 fc2 scraper** | **r18.dev 实测不覆盖 FC2**（`combined=fc2ppv1234567/json` → 404），必须单独一条管道。 |

**一句话推荐组合**：`r18.dev 周更 dump 做全库底座 → JavDB 补中文题材/评分（只补缺口） → JavBus 补演员头像/三围/系列兜底 → 中文标题用离线 MT/LLM 生成（JavSP/Javinizer 都有现成翻译管线）`。最省事的落地方式甚至是**直接部署 Javinizer-Go**（MIT、活跃、自带 dump 下载→SQLite、Web UI、NFO、翻译、FlareSolverr），把本档当选型与风控依据。

---

## 1. 实测环境与口径

- 时间：2026-10-26 11:0x（Asia/Shanghai）。所有「实测」结论仅代表该时点、该出口地区（香港）的网络与服务状态。
- 直连（`--noproxy`，6s 超时）：`www.javbus.com`、`javdb.com`、`r18.dev`、`www.javlibrary.com` **全部 HTTP 000（超时）** → 大陆网络裸连不可用，**代理是硬前提**。
- 请求纪律：本次全部为单次/少量探测（每站数次 HEAD/GET），未做批量爬取；下文的速率建议即按「礼貌抓取」设计。
- 关键证据：响应头、状态码、页面字段结构、robots.txt、开源实现源码（已下载仓库核对）。

---

## 2. 逐源分析

### 2.1 r18.dev（首选主力源）—— 有公开 dump + 无鉴权 JSON 接口

**定位**：DMM/FANZA 系「有码」元数据聚合库，站点自述 “provides information about hundreds of thousands of Japanese adult videos”（实测首页 meta）。是本次调研中**唯一同时满足「字段最全 + 有官方批量数据 + 明确许可」**的源。

#### （1）能拿到的字段

主接口（JSON，无需 token / affiliate id，实测 200）：

```
https://r18.dev/videos/vod/movies/detail/-/combined=<content_id>/json      # 推荐，字段最全
https://r18.dev/videos/vod/movies/detail/-/dvd_id=<DVD-ID>/json            # 用番号兜底查（字段较简，英文片名被消音）
```

`combined=ipx00001` / `combined=ssni00001` 实测返回的**完整字段**（逐字段核对）：

| 字段组 | 字段名（JSON） | 说明 |
|---|---|---|
| 番号/内容 ID | `dvd_id`、`content_id`、`service_code`、`site_id` | `content_id` 是 DMM 内部 ID（如 `ipx00001`），`dvd_id` 是展示番号（`IPX-001`） |
| 标题 | `title_ja`、`title_en`、`title_en_is_machine_translation`、`title_en_uncensored`、`comment_en` | **无中文标题**；`title_en` 是机器翻译（站点由 DeepL 提供机翻，见 dumps 页脚实测） |
| 女优 | `actresses[]`：`id`、`name_kanji`、`name_kana`、`name_romaji`、`image_url` | 多女优是数组；**日文汉字+假名+罗马音三写**，`image_url` 为相对文件名（补 `https://pics.dmm.co.jp/mono/actjpgs/` 前缀） |
| 男优/其他 | `actors[]`、`histrions[]`、`authors[]` | 语义未逐一验证（**未验证**），但结构存在 |
| 导演 | `directors[]`：`id`、`name_kanji`、`name_kana`、`name_romaji` | 实测有（IPX-001：刃修羅 / Shura Jin） |
| 片商 maker | `maker_id`、`maker_name_ja`、`maker_name_en` | 实测 `アイデアポケット` / `Idea Pocket` |
| 厂牌 label | `label_id`、`label_name_ja`、`label_name_en` | 实测 `ティッシュ` / `Dish`；SSNI-001 实测 `S1 NO.1 STYLE` |
| 系列 series | `series_id`、`series_name_ja`、`series_name_en`、`series_name_en_is_machine_translation` | 实测 SSNI-001 返回 `ノンストップ大量射精○○連発 超乱交スペシャル`；无系列时 `series_id: null` |
| 题材 | `categories[]`：`id`、`name_ja`、`name_en`、`name_en_is_machine_translation` | **日英双语题材**，带稳定 ID（可做跨源映射） |
| 日期/时长 | `release_date`、`runtime_mins` | 日期取 DMM 口径（与 javdb/javbus 可能差 1–2 天，见 §3 注） |
| 封面**大图** | `jacket_full_url`、`jacket_thumb_url` | 实测为 `https://pics.dmm.co.jp/mono/movie/adult/ipx001/ipx001pl.jpg`（`pl`=大图） |
| **剧照** | `gallery[]`：`image_full`、`image_thumb` | 实测 IPX-001 返回 **12 张** `https://pics.dmm.co.jp/digital/video/ipx00001/ipx00001-N.jpg`，下载实测 200 |
| 预告片 | `sample_url` | mp4，`https://cc3001.dmm.co.jp/litevideo/...`（归档可不下） |
| 评分 | **无** | 两个接口都没有评分/收藏数字段（实测） |

> 演员头像实测：`image_url` 为 `hiduki_rui.jpg`，与 `https://pics.dmm.co.jp/mono/actjpgs/` 拼接后 HEAD → **200 image/jpeg（实测）**。
> 另一接口 `list2`：首页内联 JS 里有 `https://r18.dev/videos/vod/movies/list2/json?id=<performer_id>&type=<type>&page=N`（每页 100 条、含 `total_results`），用于按演员枚举作品。**接口存在已从页面 JS 读到，未实测响应结构**。
> `https://r18.dev/videos/vod/movies/list/?searchstr=...` 实测**返回的是首页**（参数不对/该列表由 JS 拉 `list2`），不要用它做搜索。

#### （2）访问方式

- 网页 HTML + **隐藏 JSON 接口**（站点前端自己用的，无文档、无需鉴权、无 affiliate id）。
- **官方批量入口（重点）**：
  - `https://r18.dev/dumps`：数据库转储页。实测内容明确写：**每周二 03:00–04:00 UTC 上传新 dump；gzip 的 SQL 文件；旧文件保留 90 天；所有结构化数据为 CC0 许可**；当前最新 `r18dotdev_dump_2026-10-20.sql.gz` **262.2 MiB**。
  - `https://r18.dev/dumps/latest`：程序化下载最新包（实测 HEAD → 307 跳 `https://r18dotdev.s3.eu-west-1.wasabisys.com/dumps/...sql.gz`）。
  - 形式：PostgreSQL `pg_dump`，内含 `public.derived_video` 等 COPY 块（**源码级证据**：javinizer-go `internal/r18devdump/parse.go`；其 `models.DumpMovie` 字段包含 `title_ja/title_en/comment_ja/jacket_full_url/gallery_first/gallery_last/sample_url/series/label/maker/director/actresses/categories/trailer_url`，可视为 dump 字段清单）。

#### （3）反爬

- 站点在 **Cloudflare + Netlify Edge** 之后（实测响应头 `server: cloudflare`、`cache-status: Netlify Edge`）。
- JSON 接口本身**无需 cookie/鉴权**（实测多次 200），但 **WAF 会挑「不像浏览器」的请求**：裸 `curl`（无 Accept 等头）访问 `/dumps` 一次得到 **403 Cloudflare 拦截页**；补齐 Chrome 式头（Accept/Accept-Language/Sec-Fetch-*）后同一 URL **200（实测）**。→ 结论：**统一用真实浏览器头 + 合理的 UA**，不要用默认 curl UA 批量跑。
- `robots.txt` 只有 `Sitemap:` 一行（实测），无 Disallow。
- 限速：接口页脚无说明；参考 javinizer-go 的实现是「带 `Retry-After` 支持 + 可配 `rate_limit`，UA 自报 `Javinizer (+repo URL)`」。建议 **1 req/s 量级**，遇到 403/429 立即退避（见 §5）。
- 「dvd_id 兜底查询」有数据质量坑：javinizer-go 源码注释记录了 r18.dev 的 **ABF-030 之类 fuzzy/mislabel 现象**（`dvd_id=abf030` 返回错标条目），所以**必须校验返回的 `dvd_id` 与目标严格相等**再采用。引用：`internal/scraper/r18dev/r18dev.go` 注释（仓库内）。

#### （4）稳定性 / 维护 / TOS

- 维护活跃：dump 每周更新到 `2026-10-20`（实测列表含 2025-11 至 2026-10 连续周更），说明站点在持续运营。
- 许可最干净：**结构化数据 CC0**（dumps 页原文，实测）；图像仍是 DMM CDN 的版权内容（见 §7）。
- 数据质量：`title_en`/`series_name_en` 带 `is_machine_translation` 标记，说明部分英文字段是机翻，不要把英文当权威；**日文字段是权威**。
- 覆盖面：主体是 DMM 有码；**FC2 实测 404 未覆盖**；素人/无码覆盖情况**未验证**（不要假设全有）。

---

### 2.2 JavDB —— 中文题材 + 评分的最佳补充源（HTML）

**定位**：中文（繁体）成人影片数据库，检索友好、详情页结构化程度高。**没有公开 API**（`/api/v1/...` 实测 404），走 HTML 解析。

#### （1）字段（实测详情页 `https://javdb.com/v/A72m`、`https://javdb.com/v/P906N`）

| 字段 | 实测情况 |
|---|---|
| 番号 | 「番號: IPX-001」（页面拆成 `IPX -001` 两段，注意清洗） |
| 日期/时长 | 「日期」「時長」 |
| 导演 | 「導演」 |
| 片商 / 发行(label) | 「片商」「發行」 |
| **系列** | 有则显示（SSNI-001 实测有「系列: ノンストップ大量射精○○連発超乱交スペシャル」；IPX-001 无系列则不显示该行） |
| **评分** | 「評分: 4.52分, 由1080人評價」（含人数）+「想看/看過」计数 —— **本次调研唯一稳定可得的评分源** |
| **题材** | 「類別」为**繁体中文**题材（如 巨乳/淫亂真實/單體作品/顏射…），并链接 `/tags?cX=N`（可反查收藏整个 tag） |
| 演员 | 「演員」多人（含男优），日文名，链接 `/actors/<id>`；女优页（`https://javdb.com/actors/MbzQ` 实测）给出**别名**（妃月るい 的别名「音琴るい」）+ 影片数（641 部），但**无三围/生日** |
| 封面 | `https://c0.jdbstatic.com/covers/<2字符>/<javdb_id>.jpg`（实测 HEAD 200 image/jpeg） |
| **剧照** | 详情页 `https://c0.jdbstatic.com/samples/<2字符>/<id>_s_N.jpg`（缩略）/ `_l_N.jpg`（大图），实测 IPX-001 有 12 张 |
| 预告片/磁力 | 需要登录（`/login` 跳转），匿名拿不到 |
| 列表页 | 搜索列表就带 封面/评分/日期/`含中字磁鏈` 等标记（实测 `https://javdb.com/search?q=SSNI-001&f=all`），适合做「批量比对/找 id」，但**不要拿列表页当爬取入口** |

补充：列表页会标 `中字可播放`/`含中字磁鏈`，**中文字幕信息**是 javdb 的相对优势字段（源数据里没有字幕标记）。

#### （2）访问方式

- 纯 HTML：`https://javdb.com/search?q=<番号>&f=all` → 详情 `https://javdb.com/v/<javdb_id>`；`/actors/<id>`、`/series/<id>`、`/makers/<id>`、`/tags?cX=N`。
- **无公开 JSON API**（`/api/v1/movies?...` 实测 404）。移动端 App 的签名接口存在（社区逆向文章）但**未验证**，不建议依赖。
- 需要 `f=all`（否则可能只搜有码）；页面 `body data-domain="https://javdb580.com"`（实测）说明**主域名会轮换**，抓取前应从真实页面/公告获取当前域名，不要硬编码（JavSP 里同类逻辑见 `javsp/web/javdb.py`）。

#### （3）反爬

- 站点在 Cloudflare 后（实测 `server: cloudflare`，`cf-ray: ...-HKG`），但**匿名 HTML GET 目前直接 200**：首页、搜索、详情、演员页均实测 200，**无需 cookie、无年龄门**。
- 但社区实现普遍不信任匿名访问：JavSP 的 javdb 爬虫用 **cloudscraper + 从本机浏览器导入真实登录 cookie**，并处理「cookie 失效被重定向/临时域名过期」；javinizer-go 提供 **FlareSolverr** 开关。→ 说明**高频或深翻页时会触发验证/封禁**，匿名只适合低频、单部查询。
- 建议：单 worker、**1 部 = 搜索 1 次 + 详情 1 次 = 2 请求**，间隔 ≥1.5s；有账号就带 cookie；遇到验证页/403 立即停（见 §5）。
- `robots.txt`（实测 200）是 Cloudflare 托管的内容信号：`User-agent: *` → `Content-Signal: search=yes,ai-train=no,use=reference`、`Allow: /`（允许检索/引用，禁止 AI 训练）。

#### （4）稳定性 / 维护 / TOS

- 活跃、域名轮换（`javdb580.com` 实测；JavSP 历史数据里是 `javdb521.com`、`javdb368.com`），说明**主站域名会变**，自动化要能跟随。
- 站点性质是磁力索引站，TOS/DMCA 风险中高；`robots` 允许抓但内容信号 `ai-train=no`。
- 中文题材质量好，但**片名仍是日文**（实测）；「中文标题」不在它的字段里。

---

### 2.3 JavBus —— 兜底源（字段实用，但 robots 全面禁止）

**定位**：繁体中文老牌站，字段实用（製作商/發行商/系列/類別/演员头像/女优三围），但**合规风险最高**。

#### （1）字段（实测 `https://www.javbus.com/IPX-001`）

| 字段 | 实测情况 |
|---|---|
| 标题 / 识别码 / 发行日期 / 长度 | `<h3>` 标题（日文）；「識別碼」「發行日期」「長度」 |
| 导演 | 「導演」（IPX-001：刃修羅） |
| 片商 / label | 「製作商」（アイデアポケット）/「發行商」（ティッシュ） |
| **系列** | 「系列:」仅在有条目时输出；IPX-001 无（**实测**）；JavSP 解析器支持该行（源码证据），覆盖率**未验证** |
| 题材 | 「類別」繁体中文（單體作品/高中女生/巨乳/深喉/數位馬賽克…） |
| 演员 | 「演員」多人，日文名；**带演员头像缩略 `/pics/actress/<id>_a.jpg`**，头像大图同路径 |
| **女优资料页** | `https://www.javbus.com/star/py1`（实测 200）：**罩杯 E / 胸圍 92cm / 腰圍 58cm / 臀圍 89cm / 愛好 / 头像 `_a.jpg` / 作品数**；未观察到生日与中文别名（**实测未见**，不代表没有） |
| 封面大图 | 相对路径 `/pics/cover/649d_b.jpg`（`_b`=大图；实测 HEAD 200 **但必须带 Referer**，见反爬） |
| **剧照** | `#sample-waterfall` 内 12 个 `<a class="sample-box" href="https://pics.dmm.co.jp/digital/video/<cid>/<cid>jp-N.jpg">` —— **原图直链即 DMM CDN**，webp 缩略走 `/pics/sample/...` |
| 评分 | **无** |

#### （2）访问方式

- 纯 HTML：`https://www.javbus.com/<番号>`（**番号直连，不需要先搜索**，实测）；搜索 `https://www.javbus.com/search/<关键词>`；女优 `/star/<id>`。
- 存在自托管 REST 包装（javbus-api，见 §6）提供 `/api/movies/{id}`、`/api/stars/{id}` 等，但本质是实时反代 javbus。

#### （3）反爬（本次实测最关键的一条）

1. **裸请求会被导到年龄/人机门**：无浏览器头 GET `/IPX-001` → **302 `https://www.javbus.com/doc/driver-verify?referer=...`**，该页是 `<title>Age Verification JavBus</title>` 的“地區年齡檢測”页；页面内 `POST` 该 URL 会种下 **`age=verified`** cookie（实测：POST `Submit=確認` 响应 `Set-Cookie: age=verified`）。
2. **只要 Request 头像 Chrome，就能直接 200**：补齐 `Accept` / `Accept-Language` / `Sec-Fetch-Mode|Site|Dest` / `Upgrade-Insecure-Requests` 后，`GET /IPX-001` → **200（实测，未带任何 cookie）**。另外社区实现会固定带 `age=verified; dv=1; existmag=mag` 三个 cookie（javinizer-go `internal/scraper/javbus/javbus.go` 源码），建议照抄这套组合更稳。
3. **图片有防盗链**：`/pics/cover/...`、`/pics/actress/...` 裸 HEAD → **403**；带 `Referer: https://www.javbus.com/<页面>` 后 → **200 image/jpeg（实测）**。批量下图必须带 Referer。
4. **地区敏感**：javbus-api README 明确写「**用美国 IP 代理/VPS 会跳登录页，请换其他地区 IP**」（实测我们香港出口正常 200）。→ 出口选 **HK/JP/TW**，避开美国。
5. `robots.txt` = **`User-agent: *` / `Disallow: /`（实测）**。这是本文档里**唯一明确禁止爬虫**的源。
6. 首页/详情页由 PHP 输出、Cloudflare 前置（实测 `x-powered-by: PHP/5.6.40`、`server: cloudflare`），有自建反爬（302→verify、防盗链），但没有 JS 挑战（不像 javlibrary）。

#### （4）稳定性 / 维护 / TOS

- 站点长期运营，但**主域名会换**：JavSP 的 `proxyfree.py` 会去 `https://www.javbus.one/` 页面正则抓「防屏蔽地址」（源码证据）。注意：JavSP 配置里旧的免代理镜像 `https://www.seedmm.help` **实测已是停放域名**（302 → `router.parklogic.com`），镜像列表不能照抄。
- TOS 风险最高：`robots` 全禁 + 内容为盗版索引 + 广告多。**建议只做少量兜底查询（缺口补全），不做全库抓取**，并优先把可替代字段从 r18.dev/javdb 拿。

---

### 2.4 JavLibrary —— 只适合极少量补评分/评论（Cloudflare 全挑战）

- 访问实测：`https://www.javlibrary.com/ja/` → **403 `cf-mitigated: challenge`**，Cloudflare Managed Challenge；连 `https://www.javlibrary.com/robots.txt` 都 **403（Just a moment... 挑战页）**（实测）。
- 结论：**必须**用 FlareSolverr / 真浏览器拿 `cf_clearance`（+`cf_bm`）cookie 才能访问；javinizer-go 配置里就是 `cookies.cf_clearance / cf_bm` + `use_flaresolverr`（源码证据），JavSP 用 cloudscraper 且「**先试不走代理**，因为代理更容易触发 IUAM 保护」（`javsp/web/javlib.py` 注释，源码证据）。
- 语言：javinizer-go 的 javlibrary scraper 支持 `language: en | ja | cn | tw`（配置示例），即**有中文界面/中文标签**；具体字段（评分、评论、演员资料、中文标题是否存在）**未验证**（被挑战挡住，未登录看不了）。
- 建议：**不作为主源**；若非要评分补全，用 FlareSolverr 低频访问（≥3s/req），或直接用 JavDB 的评分替代。

---

### 2.5 官方 / 半官方 API（可选增强）

| 源 | 形态 | 关键点 |
|---|---|---|
| **DMM/FANZA Web 服务 API** | 官方 JSON API，`https://affiliate.dmm.com/api/` | 需要 **API ID + affiliate ID**（DMM アフィリエイト申请）；字段含 女優/監督/シリーズ/ジャンル/サンプル画像，日文权威；**本次未验证**（无 key），且需确认海外访问与成人向 ID 的可用性。图片来源仍是 `pics.dmm.co.jp`（与 r18.dev 同一套 CDN）。 |
| **javstash** | GraphQL API，需 API key（javinizer-go README 明确写 “GraphQL API scraper; requires API key from javstash.org”） | 适合要「正规 API」而不想爬 HTML 的场景；**未验证**覆盖度/费用。 |
| **MGStage 等厂站** | 网页 + 年龄 cookie（如 `adc=1`，JavSP `javsp/web/mgstage.py`、javinizer-go README 均提到） | 只覆盖自家厂牌，适合作为单厂牌补全，不适合通用。 |

---

### 2.6 FC2 / 素人 / 无码（可选，需独立管道）

- **r18.dev 不覆盖 FC2**：`https://r18.dev/videos/vod/movies/detail/-/combined=fc2ppv1234567/json` → **404（实测）**。所以 FC2 必须另找源。
- 现成实现参考：
  - javinizer-go 的 `fc2` scraper，base `https://adult.contents.fc2.com`（源码证据），带 429 限流处理；
  - JavSP 有 `fc2` / `fc2fan` / `fc2ppvdb` / `javmenu` 多个爬虫（`config.yml` 的 `fc2:` 选择列表，源码证据；其中 `fc2fan` 已关站，配置注释里写明要自备镜像）；
  - javdb 有 FC2 分类（`/tags/fc2?c10=1`，实测首页导航出现该链接）。
- 建议：先统计自己库里 FC2/素人占比再决定投入；FC2 字段普遍比 DMM 系贫（演员多为「素人」化名），双语需求也更难满足。

---

## 3. 字段 → 源 映射表（给落库用）

> 原则：**日文以 r18.dev(dump) 为权威**；**中文题材以 JavDB 为权威**；JavBus 只补 r18/javdb 没有的（演员头像、三围、偶尔的系列/中文题材）；所有字段落库时保留 `source` 与抓取时间，便于回溯。

| 字段 | 首选源（权威） | 次选/兜底 | 中日双语策略 |
|---|---|---|---|
| 番号 `dvd_id` | r18.dev dump `dvd_id` | javdb「番號」、javbus「識別碼」 | 统一归一化为 `PREFIX-NNN` + 另存 `content_id` 作匹配键 |
| 标题（日） | r18.dev `title_ja` | javdb 页面标题、javbus `<h3>` | **存日文原名** `title_ja` |
| 标题（中） | **无现成源**（三站标题均为日文，实测） | 离线 MT：JavSP `translate.py`（Baidu/Bing/Google/OpenAI/Claude）或 Javinizer `metadata.translation`（openai/anthropic/deepl/google） | 生成 `title_zh` 并缓存；**无中文名就用日文**（字段回退，不是空值） |
| 女优 | r18.dev `actresses[]`（汉字/假名/罗马音 + id + 头像文件名） | javdb 演員、javbus 演員+女优页 | 女优主键用 r18 `id`；显示名 = `name_kanji`；中文名多数**没有**，用 JavSP `data/actress_alias.json`（含繁中别名，源码证据）做补充，仍缺则回退日文 |
| 别名 | javdb 女优页「別名」（如 音琴るい，实测） | JavSP `actress_alias.json`（多语别名表） | 单独表 `actress_alias(actress_id, name, lang)` |
| 男优/共演 | r18.dev `actors[]`（结构存在，语义**未验证**） | javdb 演員（含男优，实测） | 单独标记 `gender=male`，别混进女优筛选 |
| 导演 | r18.dev `directors[]`（汉字/假名/罗马音） | javdb「導演」、javbus「導演」 | 存名 + r18 `id`（javdb/javbus 只有名，用姓名匹配） |
| 片商 maker | r18.dev `maker_name_ja/en` + `maker_id` | javdb「片商」、javbus「製作商」 | `maker_ja` / `maker_en` 双语落库 |
| 厂牌 label | r18.dev `label_name_ja/en` + `label_id` | javdb「發行」、javbus「發行商」 | 同上；注意「片商 vs label」是两级（如 S1 / S1 NO.1 STYLE） |
| 系列 series | r18.dev `series_name_ja/en` + `series_id` | javdb「系列」、javbus「系列」 | `series_ja` 权威；中文名一般没有，必要时 MT 或回退日文 |
| 题材 | **JavDB「類別」= 繁中** | r18.dev `categories[].name_ja`（日文权威）、javbus「類別」（繁中） | **双写**：`genre_ja`（r18 `name_ja`/id）+ `genre_zh`（javdb tag 的繁中名/id）；跨源对齐可用 JavSP 的 `data/genre_javdb.csv`（列：`id,url,zh_tw,en,translate`）与 `genre_javbus.csv`（`id,url,zh_tw,ja,en,translate`）（源码证据） |
| 发行日期 | r18.dev `release_date`（DMM 口径） | javdb「日期」、javbus「發行日期」 | 只存一个权威日期并记录来源；**实测同片会差 1–2 天**（IPX-001：javdb/javbus `2017-08-11` vs r18 `2017-08-13`） |
| 时长 | r18.dev `runtime_mins` | javdb「時長」、javbus「長度」 | 分钟整数 |
| 封面大图 | r18.dev `jacket_full_url`（`pics.dmm.co.jp/...pl.jpg`） | javdb `c0.jdbstatic.com/covers/...jpg`、javbus `/pics/cover/..._b.jpg` | 本地下载存 `cover.jpg`，DB 存源 URL + 本地路径。注意 JavSP 注释：javdb 封面可能带水印，**只作 fallback**（`use_javdb_cover: fallback`） |
| 剧照 | r18.dev `gallery[]`（实测 12 张/片，`pics.dmm.co.jp/digital/video/<cid>/<cid>-N.jpg`） | javbus `sample-waterfall`（同一批 DMM 图）、javdb `/samples/..._l_N.jpg` | 本地存 `stills/01..12.jpg`；`gallery_first/gallery_last` 范围式编号可直接循环 |
| 演员头像 | r18.dev `actresses[].image_url` + `https://pics.dmm.co.jp/mono/actjpgs/`（实测 200） | javbus `/pics/actress/<id>_a.jpg`（需 Referer） | 每个女优 1 张，按女优 id 存 |
| 预告片 | r18.dev `sample_url`（mp4） | — | 120GB 级库建议**不下**，或只下 720p |
| 评分 | **JavDB**「評分 X.XX 分, 由 N 人評價」（实测） | JavLibrary（需 CF 绕过，**未验证**） | 存 `rating` + `rating_votes` + `rating_source` |
| 中字/字幕标记 | JavDB 列表/详情标记（`含中字磁鏈` 等） | — | `has_chinese_sub` 布尔，方便筛「有中字」 |
| 磁力/下载 | javdb（需登录）、javbus（磁力页） | — | **本次归档不播放、不下载正片，可不抓**；只留影片页 URL 备查 |

---

## 4. 推荐落地架构（2–4 万部，完全离线）

### 4.1 数据管线（按顺序）

1. **底座：导入 r18.dev dump（零抓取）**
   - `GET https://r18.dev/dumps/latest`（307 → S3 `.sql.gz`，实测）→ 下载（建议 Range 断点续传 + 校验大小）→ 解压 → 解析 `COPY public.*` 块 → 写入本地 SQLite/PostgreSQL。
   - 参考实现：javinizer-go `internal/r18devdump/download.go`（`LatestDumpURL = "https://r18.dev/dumps/latest"`、Range 续传、变更检测）+ `internal/scraper/r18dev/dump_result.go`（**“零 HTTP”路径**：dump 命中后不再请求 r18.dev，直接构造元数据结果）。
   - 频次：**每周一次**（周二 03:00–04:00 UTC 后更新，实测规则），增量 diff 出新增番号，只处理新增条目。
2. **补中文题材/评分：JavDB 只补缺口**
   - 对比本地库：`genre_zh` 缺失 或 `rating` 缺失 或 `series` 缺失的条目 → 走 JavDB `search + /v/<id>`，写回中文题材/系列/评分/想看数/中字标记。
   - 这是「2–4 万部里只抓 10–30%」的省钱关键；全量抓 javdb 封禁风险高且没必要。
3. **补演员头像/三围/独有字段：JavBus 仅兜底**
   - 女优头像缺 → `https://www.javbus.com/star/<id>` 或详情页演员头像（记得带 Referer）。
   - 只在 r18+javdb 都没有时访问，频率设最低。
4. **中文标题：离线机翻 + 人工抽查**
   - Javinizer-Go：`metadata.translation`（provider: openai/openai-compatible/anthropic/deepl/google，`source_language: ja` → `target_language: zh`，可作用于 title/director/maker/label/series/genres/actresses）。或直接用 JavSP 的 `translate.py`（Baidu/Bing/Google/OpenAI/Claude）。
   - 落库规则：`title_zh = MT结果，若为空则 = title_ja`（对应用户「无中文名用日文」）。
5. **图片下载**：封面（r18 `jacket_full_url`）、剧照（r18 `gallery[]`）、女优头像（`actjpgs` / javbus `_a.jpg`）。全部落本地，WebUI 用本地路径+生成缩略图，不做实时反代。
6. **NFO（可选）**：若要兼容 Jellyfin/Emby/Plex，可复用 Javinizer-Go/JavSP 的 NFO 生成（含中文标题/题材/演员）。

### 4.2 建议的最小 schema（支持按 演员/导演/番号/片商/系列/题材 筛选）

```
title(id, dvd_id UNIQUE, content_id, title_ja, title_zh, title_en,
      release_date, runtime_mins, rating, rating_votes, rating_source,
      maker_id, label_id, series_id, director_id, has_chinese_sub,
      cover_url, cover_local, source_dump_date, updated_at)
actress(id, name_kanji, name_kana, name_romaji, image_url, local_avatar)
title_actress(title_id, actress_id, role)          -- 多女优/多男优, role=female|male
director(id, name_ja, name_kana, name_romaji)
maker(id, name_ja, name_en) / label(id, name_ja, name_en)
series(id, name_ja, name_en)
genre(id, genre_ja, genre_zh, genre_en, source_tag_id)   -- 双语题材, 跨源映射
title_genre(title_id, genre_id, source)             -- source=r18|javdb|javbus
asset(title_id, kind, url, local_path, bytes)       -- kind=cover|still|avatar|trailer
```
索引：`title(dvd_id)`、`title(content_id)`、`title_actress(actress_id)`、`title_genre(genre_id)`、`title(release_date)`、`maker/label/series/director` 外键。WebUI 的六个筛选维度一一对应。

### 4.3 存储体积估算（2–4 万部）

- 封面 ~0.3–0.6 MB/部、剧照 10–12 张 × ~0.15–0.3 MB ≈ ~2–3 MB/部、头像忽略不计 → **约 2.5–4 MB/部**，**2 万部 ≈ 50–80 GB，4 万部 ≈ 100–160 GB**（不含预告片；不含正片）。建议 `stills` 只存 `jp`/中尺寸，或统一二次压缩到 800px 宽（可省 50%+）。
- dump 本身 262 MB gz（实测），导入后 SQLite 约 1–3 GB 量级（取决于字段/索引），可接受。

---

## 5. 抓取速率与风控建议（按源）

> 通用：单站点单 worker（不要并发同一站点）；请求间加 0.5–1s 随机抖动；指数退避（3 次后放弃并记录）；**遇到 403/429/验证页立即停该源 30–60 分钟**，不要重试轰炸；所有请求带真实浏览器头与可识别 UA（r18.dev 社区先例：`Javinizer (+https://github.com/javinizer/javinizer-go)`）。

| 源 | 建议速率 | 每日上限 | 触发风控的信号 | 备注 |
|---|---|---|---|---|
| **r18.dev dump** | 每周 1 次，下载限速 ~2–5 MB/s，支持 Range 续传 | 1 次/周 | 403 Cloudflare（头不像浏览器时，实测） | 262 MB/周 ≈ 每月 ~1 GB 流量 |
| **r18.dev JSON（补漏）** | 1–2 req/s（建议 1） | 5k–10k | 403/429 | 支持 `Retry-After`（javinizer 实现）；`dvd_id` 查询要校验严格匹配 |
| **JavDB** | 1 req / 1.5–2.5s；1 部 = 2 req（search+detail） | 3k–5k（保守） | 403、验证页、被重定向到登录/临时域名 | 优先带登录 cookie；只补缺口，别全量爬 |
| **JavBus** | 1 req / 1–2s；图片并发 ≤4（带 Referer） | 5k | 302→`/doc/driver-verify`（说明头/地区不对）、403（图片无 Referer）、跳登录页（美国 IP） | 出口选 HK/JP/TW；带 `age=verified; dv=1; existmag=mag` |
| **JavLibrary** | FlareSolverr，1 req / 3–5s | 1k–2k | 全站 CF 挑战（实测 403 `cf-mitigated: challenge`） | `cf_clearance`/`cf_bm` 会过期，需刷新；不建议做主力 |
| **图片 CDN**（pics.dmm.co.jp / c0.jdbstatic.com） | 4–8 并发 | 不限流但别打爆 | 404/403 | javbus 图片例外：**必须带 Referer**，并发降到 ≤4 |

参考基线（源码/配置证据）：
- JavSP：`sleep_after_scraping: PT1S`（每部刮完后等待 1 秒）、`retry: 3`、`timeout: PT10S`（`config.yml`）。
- Javinizer-Go：`rate_limit` 以毫秒计（示例 1000ms）；r18dev 默认 `rate_limit: 0` 但支持 `respect_retry_after`（`configs/config.yaml.example`）。
- javbus-api：README 明确「美国 IP 会被跳登录页」——**地区**比速率更容易被忽视。

示例抓取命令（不写入代理地址，用环境变量）：

```bash
# 统一走环境变量代理（按 AGENTS.md 约定；不要把地址写进脚本/文档）
export HTTPS_PROXY="$HTTPS_PROXY"   # 由运行环境注入
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'

# r18.dev JSON（单次）
curl -sS -A "$UA" -H 'Accept: application/json, text/html, */*' \
  'https://r18.dev/videos/vod/movies/detail/-/combined=ipx00001/json'

# r18.dev dump（每周一次；-C - 支持断点续传）
curl -sSL -A "$UA" -C - -o r18dump.sql.gz 'https://r18.dev/dumps/latest'

# javbus 详情（必须浏览器头；图片另加 -e 作 Referer）
curl -sS -A "$UA" -H 'Accept: text/html,application/xhtml+xml' \
  -H 'Accept-Language: zh-TW,zh;q=0.9' -H 'Sec-Fetch-Mode: navigate' \
  -b 'age=verified; dv=1; existmag=mag' 'https://www.javbus.com/IPX-001'
curl -sS -A "$UA" -e 'https://www.javbus.com/IPX-001' -O 'https://www.javbus.com/pics/cover/649d_b.jpg'

# javdb 详情（匿名低频；高频/深翻页再加登录 cookie）
curl -sS -A "$UA" -H 'Accept-Language: zh-TW,zh;q=0.9' 'https://javdb.com/v/A72m'
```

---

## 6. 可复用开源实现（谁值得抄 / 谁值得直接用）

| 项目 | 状态（GitHub API 实测 2026-10-26） | 抓取方式 | 可借鉴点 |
|---|---|---|---|
| **[javinizer-go](https://github.com/javinizer/javinizer-go)** | MIT，166★，**最近 push 2026-10-26**（非常活跃） | Go：r18dev **JSON + 本地 dump 零 HTTP**；javdb/javlibrary 可挂 **FlareSolverr**；javbus 带固定 cookie；dmm 可选浏览器模式；javstash GraphQL（需 key）；fc2/mgstage/tokyohot 等 14 个源 | **最接近本需求的整体参考**：`javinizer dump download/update/status/search`（从 `r18.dev/dumps/latest` 下载→导入 SQLite sidecar，Range 续传）、`metadata.translation`（openai/anthropic/deepl/google，ja→zh，可翻 title/genres/actresses/series）、genre 替换库、NFO、Web UI/REST API、docker-compose 自带 FlareSolverr。**甚至可直接部署它当刮削后端**。 |
| **[JavSP](https://github.com/Yuukiy/JavSP)** | GPL-3.0，5203★，最后 push **2025-02-04**（维护放缓） | Python：多站聚合（20 个 crawler），javdb 用 **cloudscraper + 导入本机浏览器 cookie**，javlib 用 cloudscraper，**自动发现免代理镜像** | **中文/双语资产库**：`data/genre_javdb.csv`（`zh_tw,en,translate`）、`data/genre_javbus.csv`（`zh_tw,ja,en`）、`data/actress_alias.json`（多语别名）；`translate.py` 多翻译引擎；`proxyfree.py` 会从 `javbus.one`「防屏蔽地址」和 `github.com/javlibcom` 抓当前域名——**域名漂移的现成解法**。注意其 `proxy_free` 配置里部分镜像已失效（`seedmm.help` 实测停放）。 |
| **[javbus-api](https://github.com/ovnrain/javbus-api)** | TypeScript，407★，push 2026-10-11（活跃） | HTML 解析（cheerio）实时反代 javbus；Docker/Vercel 部署；Basic/Token 鉴权 | 只做 javbus 的薄 API：`/api/movies`、`/api/movies/search`、`/api/movies/{id}`、`/api/magnets/{id}`、`/api/stars/{id}`——**接口形状可直接照抄成内部服务**；README 的「美国 IP 会跳登录」是地区风控的实证。 |
| **Movie Data Capture（MDC）** | 主仓 README 现仅剩官网 `mvdc.top` 与「核心代码开源」字样（**实测 raw README**），不再适合当主要参考 | — | 历史项目，仅作背景；不建议依赖。 |
| **[xbvr](https://github.com/xbapps/xbvr)** | Go，486★，push 2026-10-20 | 含 `r18dev` scraper（曾提交 “Added DVD-ID support for the R18dev scraper” commit） | 参考 r18.dev 在 Go 里的 URL/ID 处理（[commit](https://github.com/xbapps/xbvr/commit/49555264a646c98497edc6b5a8789fd8da7783da)）。 |
| **FlareSolverr** | 独立服务（javinizer-go docker-compose 内置） | 起一个 headless 浏览器解 CF 挑战，返回 cookie/HTML | 解 **JavLibrary / JavDB 挑战**的现成方案；用 HTTP 代理调用，别自己写挑战破解。 |

---

## 7. 风险与合规提示（务必读）

1. **robots/内容信号**：JavBus `Disallow: /`（实测）→ 严格说**不应**抓；JavDB `Allow: /` 但 `ai-train=no`；r18.dev 基本无限制且 **dump 明确 CC0**。→ 工程上「合规排序」= r18.dev >> javdb > javbus ≈ javlibrary。
2. **图像版权不在 CC0 范围内**：dump/元数据可 CC0，但封面/剧照/预告片仍是 DMM/CDN 的版权内容（`pics.dmm.co.jp`、`c0.jdbstatic.com`、`cc3001.dmm.co.jp`）。个人离线归档风险低，但**不要再分发/公开托管**这些图片；WebUI 只在内网用。
3. **成年人内容合规**：JavDB/JavBus 页面含 RTA/2257 声明；自建 WebUI 建议加年龄门、只绑内网、不做公网分享。
4. **账号/cookie 是敏感信息**：javdb/javlibrary 的登录 cookie 等同账号权限，不要写进仓库文档/脚本（参考 AGENTS.md 的凭据规则）；用环境变量或 secrets 注入。
5. **域名漂移**：javdb（`javdb580.com` 实测）、javbus 都会换域名，且镜像站可能是停放/钓鱼页（`seedmm.help` 实测停放）。**只从可信入口拿当前域名**，并在程序里校验页面标题/`data-domain` 再采信。
6. **数据质量**：r18.dev 存在 fuzzy 匹配错标（源码注释记录 ABF-030 类问题）、机翻英文字段带标记、日期口径与 javdb/javbus 有 1–2 天差异。落库时保留 `source`+`raw`，便于纠错。

---

## 8. 未解决问题清单（留给下一步）

1. **中文标题到底有没有现成源？** 本次实测 javdb/javbus/r18.dev 标题均为日文；javlibrary 被 CF 挡住。→ 需要一次「浏览器 + 登录」验证 javlibrary cn/tw 是否有中文标题，或确认只能靠 MT。
2. **javdb 是否有中文女优名/演员头像直链？** 女优页实测只有日文名 + 别名 + 影片数，无头像字段（可能头像在演员页 XHR/登录后）。未验证。
3. **javlibrary 的字段与可用性**：cf_clearance 存活时间、是否支持中文标签/评分/评论、限速阈值——全部未验证（需 FlareSolverr 实测）。
4. **r18.dev dump 的真实表结构与字段完整度**：需实际下载 262 MB 包并导入验证（尤其 `gallery` 是否全量、有无 rating、（若有）中文/字幕字段），以及 `derived_video` 之外还有哪些表。
5. **r18.dev 覆盖边界**：FC2 已实测 404；**素人/无码/中字版/合集**是否覆盖未验证——直接决定用户 2–4 万部里「非 DMM 番号」的命中率。
6. **用户实际库的番号分布**：如果没有样本，无法量化 r18 dump 命中率；建议先拿 500–1000 个文件名跑一遍匹配率再定方案。
7. **javdb 封禁阈值**：匿名/登录、不同出口地区的实际限速阈值未做压力测试（本次严禁批量），3–5k/日 只是保守估计。
8. **javbus 系列字段覆盖率**：实测样本无「系列」，JavSP 代码支持该行——需抽样统计有系列的片商（如 S1/Prestige）占比。
9. **DMM/FANZA 官方 API 现状**：API ID + affiliate ID 申请门槛、海外可用性、成人向字段、费率——未验证。
10. **代理出口地区策略**：本次仅香港出口实测；javbus 对 US IP 会跳登录（README 证据），JP/HK/TW 的差异、以及 javlibrary 挑战强度是否受地区影响，均需系统测试。
11. **存储与缩略图方案**：100–160 GB 图片库的目录分片、去重（同一封面被多部合集复用）、WebUI 生成缩略图的耗时，未做基准。
12. **是否直接用 Javinizer-Go 成品**：需评估它对 2–4 万部的性能、DB 迁移、以及是否满足「按六个维度筛选 + 中日双语」的界面需求（它自带 Web UI，但筛选维度是否齐全未验证）。

---

## 9. 参考链接（本文所有事实的来源）

**目标站点（实测）**
- r18.dev 首页：https://r18.dev/ ｜ 接口示例：https://r18.dev/videos/vod/movies/detail/-/combined=ipx00001/json ｜ 番号查询：https://r18.dev/videos/vod/movies/detail/-/dvd_id=IPX-001/json
- r18.dev 数据库转储：https://r18.dev/dumps ｜ 最近一包：https://r18.dev/dumps/latest ｜ robots：https://r18.dev/robots.txt
- JavDB 搜索：https://javdb.com/search?q=IPX-001&f=all ｜ 详情：https://javdb.com/v/A72m 、https://javdb.com/v/P906N ｜ 女优页：https://javdb.com/actors/MbzQ ｜ robots：https://javdb.com/robots.txt
- JavBus 详情：https://www.javbus.com/IPX-001 ｜ 女优页：https://www.javbus.com/star/py1 ｜ 年龄门：https://www.javbus.com/doc/driver-verify ｜ robots：https://www.javbus.com/robots.txt
- JavLibrary（CF 挑战）：https://www.javlibrary.com/ja/
- DMM/FANZA 官方 API 入口：https://affiliate.dmm.com/api/

**开源实现**
- Javinizer-Go：https://github.com/javinizer/javinizer-go （README 支持源表；`configs/config.yaml.example`；`docs/03-cli-reference.md` 的 `javinizer dump download`；`docs/05-genre-management.md`；`docs/18-translations.md`；`docs/19-offline-actress-name-sync.md`；源码 `internal/scraper/javbus/javbus.go`、`internal/scraper/r18dev/r18dev.go`、`internal/r18devdump/download.go`、`internal/scraper/r18dev/dump_result.go`、`internal/models/r18dev_dump.go`）
- JavSP：https://github.com/Yuukiy/JavSP （`config.yml`、`javsp/web/{javdb,javbus,javlib,mgstage,proxyfree,translate}.py`、`data/genre_{javbus,javdb,javlib,avsox}.csv`、`data/actress_alias.json`）
- javbus-api：https://github.com/ovnrain/javbus-api （README；`api/javbus-parser.ts`、`api/app.ts`）
- MDC：https://github.com/yoshiko2/Movie_Data_Capture
- xbvr（r18dev scraper）：https://github.com/xbapps/xbvr ｜ R18 DVD-ID commit：https://github.com/xbapps/xbvr/commit/49555264a646c98497edc6b5a8789fd8da7783da
- FlareSolverr：https://github.com/FlareSolverr/FlareSolverr

**社区资料（辅助判断，非本次实测）**
- «从 APK 到完整 API：逆向工程 JavDB 移动端»：https://juejin.cn/post/7617004481947172874 （javdb App 签名接口，未验证）
