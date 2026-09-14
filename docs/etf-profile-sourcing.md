# ETF 静态资料来源审核清单

> 本清单只登记官方资料入口和人工审核步骤。链接、搜索摘要或网页内容本身不是已核验事实；在 `config/etf-profiles.json` 中录入数值前，必须完成本清单。

## 官方资料入口

| ETF | 发行人 | 官方资料页 | 计划审核字段 | 当前状态 |
| --- | --- | --- | --- | --- |
| SPY | State Street Global Advisors | [SPY 官方页](https://www.ssga.com/us/en/intermediary/capabilities/spdr-core-equity-etfs/spy-sp-500) | 费用率、S&P 500 基准、持仓/行业/国家权重 | 待人工审核 |
| QQQ | Invesco | [QQQ 官方页](https://www.invesco.com/qqq-etf/en/home.html) | 费用率、Nasdaq-100 基准、持仓/行业/国家权重 | 待人工审核 |
| IWM | iShares by BlackRock | [IWM 官方页](https://www.ishares.com/us/products/239710/ishares-russell-2000-etf) | 费用率、Russell 2000 基准、持仓/行业/国家权重 | 待人工审核 |
| TLT | iShares by BlackRock | [TLT 官方页](https://www.ishares.com/us/products/239454/ishares-20-year-treasury-bond-etf) | 费用率、长久期美国国债基准、发行人/期限暴露 | 待人工审核 |
| GLD | State Street Global Advisors | [GLD 官方页](https://www.ssga.com/us/en/intermediary/etfs/spdr-gold-shares-gld) | 费用率、黄金参考基准、黄金持有结构 | 待人工审核 |

## 每只 ETF 的人工审核步骤

- [ ] 打开上表官方资料页，并优先查找当前 prospectus、factsheet 或官方 holdings 下载文件。
- [ ] 记录文档标题、发布方、URL、资料实际截至日（`as_of`）和本地取得时间（`retrieved_at`）。
- [ ] 下载原始证据到 `data/raw/etf/<publisher>/us/<symbol>/<utc>/`，计算 SHA-256；不要将证据或 Key 写入 Git。
- [ ] 确认费用率口径（总费用率、净费用率或管理费）及基准名称的原文定义。
- [ ] 对持仓和权重确认资料截至日、前十大覆盖比例、行业/国家分类方法；不得将前十大当成完整组合。
- [ ] 将来源索引的 `verification_status` 和事实字段状态从 `pending_review` 更新为 `verified`，仅在来源、`as_of` 和人工审核齐全时填入数值。
- [ ] 为 GLD 保留行业/国家权重的 `not_applicable` 说明；不要用零权重替代不适用。

## 资料审核后的最低记录格式

```json
{
  "status": "verified",
  "value_pct": 0.00,
  "as_of": "YYYY-MM-DD",
  "source_id": "official_source_id"
}
```

持仓/权重资料还必须额外包含覆盖比例、分类方法和资料截至日。当前 `etf-profiles.json` 中没有任何事实处于 `verified` 状态。

来源链接均指向 ETF 发行人官方页面；本文内容仅作流程性转述，不复制页面内容。
