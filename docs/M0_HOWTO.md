# M0：用真实文件名做命中率评估（5 分钟）

这一步决定后面所有工作是否值得做，**必须用你的真实文件，不能用合成样本**。

## 1. 在 Unraid 上导出文件名

```bash
find /mnt/user/av -type f \( -iname '*.mp4' -o -iname '*.mkv' -o -iname '*.avi' \
  -o -iname '*.wmv' -o -iname '*.ts' -o -iname '*.m2ts' -o -iname '*.rmvb' -o -iname '*.mov' \) \
  | head -2000 > /tmp/av_names.txt
wc -l /tmp/av_names.txt
```

500–2000 行足够；越多越准。**不需要上传文件本身**，只要路径清单。

## 2. 跑分析

```bash
# 把清单拷到装了本项目的机器，或者直接在项目目录下
python -m scripts.analyze_names --paths-file /tmp/av_names.txt \
    --r18db data/r18dump.db --out m0-report.md
```

输出一份 Markdown 报告，包含：

| 指标 | 意义 | 决策阈值 |
|---|---|---|
| 可解析率 | 文件名里能认出番号的比例 | ≥95% 直接进；85–95% 加 alias/规则；<85% 先补解析器 |
| r18 覆盖率 | 认出的番号里有多少能在 r18 dump 命中 | ≥90% 元数据够用；<75% 要补 JavDB/其它源 |
| FC2/素人占比 | 特殊厂牌占文件比例 | ≥10% 必须建独立刮削管道 |

报告还会给出：confidence 分布、前缀 Top 30、特殊厂牌明细、未解析样本 50 条。

## 3. 看什么

1. **未解析样本 50 条**——直接暴露命名盲区（比如你用的是 `ABC123` 还是 `ABC-123`、有没有奇怪的广告前缀）。
2. **前缀 Top 30**——如果大量前缀是 r18 不覆盖的（FC2/HEYZO/CARIB/200GANA/LUXU……），就要为它们单独做管道。
3. **可解析率与 confidence 分布**——70–89 的比例高说明命名比较脏，需要人工抽检队列。

## 4. 之后

- 报告出来我会根据三个数字决定：直接全量、先补 parser、还是先做 FC2/素人管道。
- 工具是只读的（`--r18db` 用只读连接），不会改你的库。
