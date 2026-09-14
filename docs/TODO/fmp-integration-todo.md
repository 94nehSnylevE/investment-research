# FMP 日频交叉校验接入 TODO

> 目的：将 Financial Modeling Prep（FMP）作为 Yahoo Finance 的独立、只读日频校验源；不用于交易、下单或混合回测数据。

## 官方入口

- [注册并获取 API Key](https://site.financialmodelingprep.com/register)
- [开发文档](https://intelligence.financialmodelingprep.com/developer/docs)
- [价格与套餐限制](https://intelligence.financialmodelingprep.com/pricing-plans)

截至本文档整理时，FMP 官方注册页说明免费 API Key 不要求银行卡；官方价格页说明免费方案采用滚动 30 天 **500MB** 带宽限制。端点可用范围、调用限制和授权条件可能调整，实施前必须以注册后的 FMP 控制台、价格页与目标端点文档为准。

## 人工准备（待办）

- [ ] 使用上方官方注册页创建个人免费账户。
- [ ] 在 FMP 控制台生成个人 API Key；不要将 Key 发送到聊天、写入报告、缓存、Git、截图或提交记录。
- [ ] 阅读日终历史价格（EOD historical price）端点的当前文档，确认美股 ETF、`SPY`、`QQQ`、`IWM`、`TLT`、`GLD` 是否在免费权限范围内。
- [ ] 在控制台确认免费档的当前带宽、请求频率、历史范围和个人使用授权；若与本文档不同，以控制台为准。

## 本机使用 Key

本项目不会自动读取 `.env`。推荐在**当前终端会话**设置环境变量：

```bash
export FMP_API_KEY='替换为你的实际 Key'
```

运行命令前可只检查变量是否已设置，不显示其内容：

```bash
python3 -c "import os; print('FMP_API_KEY 已设置' if os.getenv('FMP_API_KEY') else 'FMP_API_KEY 未设置')"
```

关闭终端后该变量失效。若改用本机 `.env` 或密钥管理器，必须保持其不提交 Git；当前 `.gitignore` 已忽略 `.env`。代理变量与 `FMP_API_KEY` 可以在同一终端会话并存，但 Key 不应放进 URL、日志或 Markdown 报告。

## 代码接入待办

- [ ] 新增只读 FMP 日频适配器；从 `FMP_API_KEY` 读取 Key，禁止硬编码。
- [ ] 每次响应记录 `provider=fmp`、拉取时间（UTC）、交易日、市场时区、频率、币种、复权/调整方法和请求结果；Key 必须脱敏。
- [ ] 只抓取观察池的五只 ETF，每日一次；不要为校验目的进行高频重试。
- [ ] 将 FMP 与 Yahoo 的最新**同一交易日、同一币种、同一复权口径**的 `Close` 对比；成交量只作辅助对比。
- [ ] 将“日期不一致、缺失、口径不明、收盘价偏差超过待定阈值”标为数据质量告警；不得自动得出买卖结论。
- [ ] 两个数据源仅作交叉校验，不得直接拼接为回测数据集。

## 待确定的决策

- [ ] 确定是否比较未复权 `Close`，或改为双方均有明确定义的调整后价格。
- [ ] 结合连续两周运行结果，确定收盘价偏差告警阈值（初始建议人工审阅，而非立刻自动阈值）。
- [ ] 记录免费额度实际消耗、供应商延迟、目标 ETF 覆盖情况与验收日期到 `docs/`。

## 验收标准

- FMP Key 仅存在于本机环境或密钥管理器，仓库中不存在真实 Key。
- 每份报告可区分 Yahoo 实时数据、Yahoo 缓存降级、FMP 数据和交叉校验告警。
- 任一数据源失败时，报告明确降级；不伪造价格，也不触发通知或交易行为。

内容依据上述 FMP 官方页面整理并作了转述，以符合许可要求。
