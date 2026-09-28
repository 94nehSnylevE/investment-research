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
- ETF 候选快照写入本地 SQLite：`data/processed/etf/candidate-history.sqlite3`。每条快照记录来源、抓取时间、原始/处理文件路径、SHA-256 和候选字段，便于按标的和日期追溯；日报只显示快照数、按发布方/文档类型/规范 URL 去重的候选文档数和最近抓取时间。
- 候选采集只允许预配置的发行人官方 HTTPS 页面；不允许的重定向、非 HTML 响应、字段缺失或无法解析必须明确失败，不得猜测字段或静默回退第三方数据。
- SQLite 中的候选快照和字段状态固定为 `pending_review`，只作审计索引；原始 HTML/JSON 仍是证据原件，`config/etf-profiles.json` 仍是人工审核后 `verified` 事实的唯一来源。

## 宏观发布日期候选

CPI、就业、PCE、FOMC 与利率页面的发布日期属于独立的宏观候选，不得写入 ETF profile 或 ETF 候选 SQLite。每次采集应记录 `event_key`、发布方、`scheduled_date`、`reference_period`、来源 URL、`retrieved_at`、原始证据 SHA-256 和 `pending_review` 状态。

- 官方页面解析出的日期仅作为日历候选；不代表市场预期、实际发布数值、政策解释或交易信号。
- 一致预期不是政府机构发布的官方事实；当前仅支持人工在发布前录入许可范围内查看到的预期，并保留来源标签、URL、录入时间及可选证据哈希。
- 来源失败、解析失败或陈旧缓存必须在报告中明确降级，不得使用第三方数据静默替代。
- CPI、核心 CPI、非农和失业率可通过 FRED 获取 BLS 来源的已发布观测；必须同时记录 FRED、BLS 原始发布方、series ID、观测期、单位、缓存哈希与 `pending_review` 状态。
- FRED 已发布观测不提供完整未来发布日期或市场一致预期；两者仍须使用独立、可追溯来源并分栏展示。

## 宏观预期、实际值与修订

人工预期和 FRED 实际值版本写入独立的 `data/processed/macro/macro-release-history.sqlite3`，不得写入宏观日历候选库、FRED 指标候选库、ETF 库或价格库。

- 发布实例由 `event_key + reference_period` 标识；首次录入固化人工候选的 `scheduled_at_utc` 和预期截止时间，并要求发布时间在月度参考期结束后 1–62 天。录入前必须确认 FRED 尚无该期观测；FRED 不可达时保守拒绝。CLI 不接受自定义 `recorded_at`，取得数据库写锁后才生成系统时间，到达发布时间后拒绝补录。
- 每次预期更新都追加新快照，记录预期、页面前值、单位、人工来源、URL、可选证据路径/SHA-256、系统录入时间及哈希链；禁止 UPDATE/DELETE。
- 实际值必须按参考期读取 FRED 序列并保存转换公式及输入 level。CPI/PCE 月率使用 `pct_change_1`，非农变动使用 `diff_1`，失业率使用 `identity`。
- 首次捕获的 FRED 版本记为 revision 0，并固化其引用的 `expectation_id`、公式版本、单位和 surprise 候选；后续输入变化只追加修订，不重新解释首次结果。只有发布时间后 4 小时内捕获才计算 surprise，迟抓值不得冒充官方初值。
- FRED 是分发渠道，原始发布方仍记录为 BLS 或 BEA；FRED 当前 vintage 不保证等于最初官方发布稿，延迟首次抓取必须标记为 `late_first_capture`。
- 本地 SQLite、系统时钟和哈希链可防止普通误改，但不能提供独立第三方可信时间证明；严肃回测仍需外部时间戳或授权 point-in-time 数据。

## 日频价格历史

Yahoo Finance 与 FMP 的每次日频拉取会保留原始 JSON/metadata，并将来源、抓取时间、`live`/`cache`/`error` 状态、证据文件 SHA-256 和可用的最新交易日 OHLCV 写入 `data/processed/prices/daily-price-history.sqlite3`。

- SQLite 是原始价格文件的审计索引，不替代 `data/raw/prices/` 的证据原件。
- Yahoo 与 FMP 价格按 `provider` 独立保存；不得在数据库或回测中静默合并、覆盖或替代彼此。
- `cache` 表示本次实时请求失败后使用旧成功缓存，`error` 表示本次没有可用价格；两种状态均不应被误标为实时数据。
