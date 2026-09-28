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

## 官方发布源新闻

新闻条目写入独立的 `data/processed/news/news-item-history.sqlite3`，只追加，且与宏观、ETF、价格库隔离。

- 只允许预配置的政府/央行官方 HTTPS RSS 源；非 HTTPS、跨域重定向或超限响应必须失败。
- 为避免版权与再分发问题，只保存标题、链接、发布时间与不超过 280 字符的摘要，不保存正文全文。
- 条目按 `feed_key + link` 去重，状态固定 `pending_review`；不做情绪打分、不生成观点，也不产生交易信号。
- 来源失败（例如 BLS 当前返回 403）必须显式降级并记录，不得用第三方媒体内容替代官方发布。

## 宏观发布时效

宏观滞后分两类，必须分别记录，不可混为一谈：

- **参考期滞后**：例如 8 月 CPI 在 9 月中旬发布，由官方统计口径决定，任何数据源都无法消除。
- **获取滞后**：官方发布后本机多久取得数据。只有这一项可优化。

优化方式是把官方新闻公告作为「已发布」信号，检测到后再触发 FRED 同步；检测结果写入 `data/processed/macro/macro-latency-history.sqlite3`。

- 发布公告只证明数据已发布，不代表已取得数值；数值仍必须来自 FRED 或官方文件。
- 必须记录公告发布时间、本地检测时间与检测延迟，便于评估定时任务是否足够及时。
- FRED 属于二次分发，永远不早于官方发布；需要发布瞬间数值必须使用官方发布页或有授权的实时日历。
- `macro-release today` 只读判断本地是否已为「今天」录入过发布前预期，供轮询脚本决定是否加密；判断依据是人工录入的 `release_instances`，不额外联网，也不代表官方确认今天必然发布。
- 加密轮询只压缩获取滞后，不改变 BLS 官方页面/API 因边缘网络拦截而不可用的现状；不得为此更换代理或伪装请求头以绕过限制。

## 近实时报价监控

流式报价写入独立的 `data/processed/realtime/intraday-quote-monitor.sqlite3`，只追加，且与日频价格库、回测数据集隔离。

- 每条报价必须记录 `provider`、交易所、供应商时间戳、本地接收时间、延迟与新鲜度状态。
- 数据为单一交易所报价（`single_venue_not_nbbo`），不得描述为全市场最优价或成交价。
- 来源为非官方授权端点（`unlicensed_research_only`）：仅限个人研究，不得用于自动交易、对外分发或商业用途。
- 延迟为负且超过 1 秒记为 `clock_skew_suspected`，超过 60 秒记为 `stale`；两者都不得当作可用实时数据。
- 连接失败或未收到报价必须显式记录状态，不得用历史价格或日频收盘价填充。

## 回测数据集与因子评估

回测使用独立冻结的数据集，存放于 `data/processed/backtest/datasets/<dataset_id>/`，口径为 Yahoo 复权总回报收盘价（`yahoo_auto_adjusted_total_return`）。

- 该数据集与未复权日频价格库、ETF 候选库、宏观库相互独立；不同复权口径不得混入同一回测。
- 每个版本必须包含价格 CSV、清单（含 SHA-256、标的、区间、行数）与质量报告；加载时校验哈希，不一致或存在缺失值必须拒绝使用。
- 数据集一经冻结不得修改；更换标的、区间或复权方式必须生成新版本，回测结论必须引用具体 `dataset_id`。
- 回测必须同时展示基准、交易成本与换手；缺少成本假设的收益不得作为结论。
- 方向预测必须使用滚动前向验证，并与「无条件猜涨」基准和样本量、z 值一同展示。命中率高于 50% 但未超过基准，或 |z| < 2，一律视为无预测力证据。
- 回测与因子结果只是研究参照，不得作为交易信号、目标价或下单依据。

## 日频价格历史

Yahoo Finance 与 FMP 的每次日频拉取会保留原始 JSON/metadata，并将来源、抓取时间、`live`/`cache`/`error` 状态、证据文件 SHA-256 和可用的最新交易日 OHLCV 写入 `data/processed/prices/daily-price-history.sqlite3`。

- SQLite 是原始价格文件的审计索引，不替代 `data/raw/prices/` 的证据原件。
- Yahoo 与 FMP 价格按 `provider` 独立保存；不得在数据库或回测中静默合并、覆盖或替代彼此。
- `cache` 表示本次实时请求失败后使用旧成功缓存，`error` 表示本次没有可用价格；两种状态均不应被误标为实时数据。
