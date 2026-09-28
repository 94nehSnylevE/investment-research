# investment-research

个人投研、回测与提醒工作台。此项目独立于 OpenBB、Qlib、vn.py、daily_stock_analysis 和 ai-hedge-fund，上游仓库保持原样。

## 当前能力

- 维护美股、A 股、港股观察列表；美股 MVP 预置 SPY、QQQ、IWM、TLT、GLD 五只 ETF。
- 通过只读的 Yahoo Finance Chart API 获取美股 ETF 日频数据，无需 API 密钥。
- 使用 `yfinance` 管理 Yahoo 的会话、cookie 与 crumb；请求串行执行，1.5 秒节流，429 限流时停止后续请求。
- 将每次由 `yfinance` 标准化的 Yahoo 日频返回及元数据缓存至 `data/raw/prices/`，记录来源、抓取时间、时区、频率、币种与复权口径。
- 生成含最新收盘价、成交量、缓存路径和逐标的质量告警的盘后研究报告。
- 使用 FMP EOD 作为 Yahoo 的独立只读日频校验源；分别缓存并在报告中展示同日同口径的收盘价差异及套餐限制。
- 从 BLS、BEA、Federal Reserve 官方页面采集宏观发布日期候选，保存本地原始证据与独立 SQLite 审计索引；来源失败会明确降级。
- 通过 FRED CSV 自动获取 BLS 发布的 CPI、核心 CPI、非农与失业率最新观测；可在无代理环境运行，仍标记为待审核候选。
- 支持在宏观数据发布前人工保存预期/前值候选，发布后按参考期从 FRED 计算首次捕获值、预期差候选和后续修订，保存到独立 append-only SQLite 审计库。
- 将长历史复权总回报价格冻结为带哈希清单的版本化数据集，并在其上运行买入持有、月度再平衡与趋势规则的朴素基线回测。
- 提供滚动样本外的因子方向评估，与「无条件猜涨」基准对比后给出是否存在预测力的诚实结论。
- 提供近实时报价只读监控：记录供应商时间戳、延迟、交易所与连接状态，并标注单一交易所与非授权来源边界。
- 将 Yahoo/FMP 每次日频价格结果（含实时、缓存降级和失败）写入本地 SQLite 审计库，支持按标的、来源和交易日追溯。
- 将 ETF 官方页面候选资料以 SQLite 审计快照保存到 `data/processed/etf/candidate-history.sqlite3`，支持按标的和抓取时间追溯；候选不会自动写为已核验事实。
- 采集 Federal Reserve、BEA、BLS 官方 RSS 的发布标题与链接，去重后写入独立 append-only 审计库；不保存正文、不做情绪判断。
- 从官方公告识别 CPI、就业、PCE、FOMC、GDP 是否已发布，记录检测延迟并触发实际值同步，降低发布后的获取滞后。
- 提供可安装/可卸载的 macOS `launchd` 定时任务：每日研究报告与宏观发布监视。

数据仅用于研究，不调用 LLM、不发送提醒、不连接券商、不下单。价格以 Yahoo 的未复权 `close` 记录；不得与其他复权口径混合用于回测，所有结果都须人工核验。

## 本地运行

项目支持 Python 3.9+。先创建环境并安装锁定版本的依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install "yfinance==0.2.66"
PYTHONPATH=src python -m investment_research.cli daily-review --dry-run
PYTHONPATH=src python -m investment_research.cli daily-review
```

`--dry-run` 只校验并预览，绝不联网或写入文件。普通命令会请求 Yahoo 日频数据，在 `data/raw/prices/` 保存由 `yfinance` 标准化的 JSON 与元数据，并在 `reports/daily/` 生成当天报告。数据缺失、网络失败、解析异常、供应商错误或陈旧数据都会被明确标记，而非伪造结果。

### Yahoo 网络受限时使用本地代理

若 Yahoo Finance 返回 `403`、`429`，或当前网络无法访问行情接口，可在**当前终端会话**按需设置本地代理后重跑。以下设置不会写入代码、Git、报告或 `launchd` 定时任务；关闭终端后自动失效。

```bash
export https_proxy=http://127.0.0.1:7890
export http_proxy=http://127.0.0.1:7890
export all_proxy=socks5://127.0.0.1:7890
PYTHONPATH=src python -m investment_research.cli daily-review
```

只想对单次命令生效时，可使用：

```bash
https_proxy=http://127.0.0.1:7890 http_proxy=http://127.0.0.1:7890 all_proxy=socks5://127.0.0.1:7890 PYTHONPATH=src python -m investment_research.cli daily-review
```

### ETF 候选资料历史

`etf-candidate-review` 可从 SPY、QQQ、IWM、TLT、GLD 的**预配置发行人官方基金页**采集明确标注的费用率或基准候选。每次采集都会保留原始 HTML、元数据和结构化候选，并写入本地 SQLite 审计库；数据库仅保存 `pending_review`，不会自动修改 `config/etf-profiles.json` 或标记任何事实为 `verified`。

```bash
PYTHONPATH=src python -m investment_research.cli etf-candidate-review --symbol SPY
```

候选审核报告会读取该 ETF 最近的历史快照；数据库路径为 `data/processed/etf/candidate-history.sqlite3`，与其 WAL/SHM 文件均不提交 Git。页面字段缺失、非 HTML 响应、非 HTTPS 或跨允许域重定向会明确失败；不会回退第三方数据或猜测字段。

### 宏观预期差

在 CPI、就业或 PCE 公布前，从人工核验的日历页面录入预期候选。`--scheduled-at` 必须使用带时区的 ISO 8601 时间，并与参考期结束日相隔 1–62 天；程序先确认 FRED 尚无目标期次观测，再在取得数据库写锁后生成不可由 CLI 指定的录入时间。FRED 无法确认或发布时点已到都会拒绝补录。发布时间和预期仍属于人工候选，不等于已验证的 point-in-time 数据。

```bash
PYTHONPATH=src python -m investment_research.cli macro-release expect \
  --event cpi --metric cpi_mom_sa --period 2026-09 \
  --scheduled-at 2026-10-13T08:30:00-04:00 \
  --forecast 0.3 --previous 0.4 \
  --source-label "人工核验来源" --source-url "https://example.com/calendar"
```

发布后同步 FRED 实际值并查询初始预期差：

```bash
PYTHONPATH=src python -m investment_research.cli macro-release sync-actuals
PYTHONPATH=src python -m investment_research.cli macro-release list
```

首批指标键为 `cpi_mom_sa`、`core_cpi_mom_sa`、`payroll_change_sa`、`unemployment_rate_sa`、`pce_mom_sa`、`core_pce_mom_sa`。CPI/PCE 单位为百分比，非农单位为千人。数据库位于 `data/processed/macro/macro-release-history.sqlite3`；预期、实际版本与每次抓取记录只追加不覆盖。仅发布时间后 4 小时内的首次 FRED 捕获计算 surprise 候选；迟抓只记录值，不冒充官方初值。人工来源不等于官方事实，本地时间戳也不是第三方可信时间证明。

### 官方新闻与宏观发布时效

采集政府/央行官方 RSS，只保存标题、链接、发布时间与短摘要，不保存正文。

```bash
PYTHONPATH=src python -m investment_research.cli news-feeds fetch
PYTHONPATH=src python -m investment_research.cli news-feeds list --limit 10
PYTHONPATH=src python -m investment_research.cli macro-watch detect
PYTHONPATH=src python -m investment_research.cli macro-watch latency
```

已登记 Federal Reserve、BEA、BLS 三个源（BLS 当前返回 403，会明确降级）。`macro-watch detect` 从官方公告中识别 CPI、就业、PCE、FOMC、GDP 是否已发布，并记录检测延迟，用于在发布后立即触发 FRED 同步，而不是盲目轮询。

宏观滞后要区分两类：**参考期滞后**（8 月 CPI 在 9 月中旬才发布）由官方口径决定，无法优化；**获取滞后**（发布后多久拿到）才是本模块优化的对象。

### 定时任务

```bash
bash scripts/install_launchd_jobs.sh install
bash scripts/install_launchd_jobs.sh status
bash scripts/install_launchd_jobs.sh uninstall
```

安装两个用户级 `launchd` 任务：`daily-review`（每日 18:30 生成研究报告）和 `macro-watch`（08:35、08:50、09:30、14:15 抓新闻、识别发布、同步实际值）。任务只做只读采集与本地写库，不发送提醒、不连接券商、不下单；`uninstall` 可完全撤销。

### 近实时行情监控

通过 Yahoo 流式端点接收近实时报价，只用于观察与延迟审计。

```bash
PYTHONPATH=src python -m investment_research.cli realtime-monitor --symbols SPY QQQ GLD --duration-seconds 60
PYTHONPATH=src python -m investment_research.cli realtime-monitor --summary
```

监控记录写入 `data/processed/realtime/intraday-quote-monitor.sqlite3`，保存供应商时间戳、接收时间、延迟、交易所与连接状态，只追加不覆盖。

**口径边界**：报价来自**单一交易所**（实测 SPY/GLD 为 `PCX`、QQQ 为 `NGM`），不是全市场合并最优价（NBBO）；来源为非官方授权端点，不得用于成交判断、自动交易或对外分发。当供应商时间戳早于本机时钟超过 1 秒时标记 `clock_skew_suspected`；超过 60 秒标记 `stale`。这些报价与日频价格库、回测数据集口径不同，不可混用。

### 回测与因子基线

先冻结一份独立的长历史数据集。该数据集使用 Yahoo 复权总回报收盘价，与 `data/raw/prices/` 的未复权价格库口径不同，**不可混用**。

```bash
PYTHONPATH=src python -m investment_research.cli backtest-dataset build --period 10y
PYTHONPATH=src python -m investment_research.cli backtest-dataset list
PYTHONPATH=src python -m investment_research.cli backtest --cost-bps 5
PYTHONPATH=src python -m investment_research.cli factor-baseline --horizon-days 5
```

数据集写入 `data/processed/backtest/datasets/<dataset_id>/`，含价格 CSV、清单（SHA-256）与质量报告；回测前会校验哈希，不一致直接拒绝。回测实现买入持有、月度等权再平衡和 SMA 趋势三个朴素基线，统一计入单边成本与换手，并与基准并列。

因子基线用滚动前向验证评估未来涨跌方向，并强制与「无条件猜涨」基准对比。当前 10 年样本的实测结论是**没有预测力证据**：五只 ETF 的样本外命中率均未超过猜涨基准，IC 接近 0。该模块只用于建立诚实参照，不产生交易信号或目标价。

## 分层接入计划

1. `daily_stock_analysis`：盘后复盘、报告、低频提醒。
2. OpenBB：美股、ETF、SEC、宏观的结构化研究数据。
3. Qlib：固定口径历史数据上的日频/低频回测。
4. vn.py：策略通过验证后，仅用于模拟盘、行情、订单状态与风控。

## 定时与实时

`ops/launchd/` 提供每日 18:30 本机时间的模板，尚未安装或启用。启用前需要根据本机用户名、Python 环境与各市场收盘后的数据可用时间修改路径和时间。

定时任务只负责按时间运行；实时提醒需要可信的行情供应商和事件流/WebSocket。免费或延迟数据不能用于自动交易，所有提醒必须人工核验。

详见 [数据口径约定](docs/data-contract.md) 与 [开发路线图](docs/development-plan.md)。
