# 数据口径约定

任何进入研究、提醒或回测的数据都应记录以下字段：

- `symbol`：标的代码与所属市场。
- `provider`：数据供应商。
- `fetched_at`：拉取时间（UTC）。
- `timestamp` 与 `timezone`：市场时间和时区。
- `interval`：例如 `1d`、`1h`。
- `adjustment_method`：原始价格或复权价格。
- `currency`：币种。

不同供应商的数据不得在未统一上述口径前混入同一回测。免费或延迟数据只能用于研究；提醒触发后需在可靠行情源人工复核。
## 静态/低频 ETF 资料

费用率、基准、持仓及行业/国家权重属于带版本的参考资料，必须与日频价格数据分开存放和展示。每项资料至少记录：`symbol`、`provider`/发布方、`source_url`、`as_of`、`retrieved_at`（UTC）、`verification_status`、`source_id` 与证据文件哈希。

- `as_of` 是资料实际截至日；`retrieved_at` 是本地取得时间，两者不得混用。
- 只有 `verification_status=verified`、带来源索引且已人工复核的值可以展示为“结构化事实”。
- `pending_review` 仅展示待核验状态与资料缺口，不展示候选数值；`not_applicable` 必须说明原因。
- 前十大持仓、行业/国家权重必须记录覆盖比例和方法；不得将部分覆盖描述为完整组合。
- 网页搜索、新闻、LLM 输出和未审核下载件只能作为候选证据或观点，不能直接写入事实区。
- ETF 候选快照写入本地 SQLite：`data/processed/etf/candidate-history.sqlite3`。每条快照记录来源、抓取时间、原始/处理文件路径、SHA-256 和候选字段，便于按标的和日期追溯。
- SQLite 中的候选快照和字段状态固定为 `pending_review`，只作审计索引；原始 HTML/JSON 仍是证据原件，`config/etf-profiles.json` 仍是人工审核后 `verified` 事实的唯一来源。
