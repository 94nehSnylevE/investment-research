# investment-research

个人投研、回测与提醒工作台。此项目独立于 OpenBB、Qlib、vn.py、daily_stock_analysis 和 ai-hedge-fund，上游仓库保持原样。

## 当前能力

- 维护美股、A 股、港股观察列表；美股 MVP 预置 SPY、QQQ、IWM、TLT、GLD 五只 ETF。
- 通过只读的 Yahoo Finance Chart API 获取美股 ETF 日频数据，无需 API 密钥。
- 使用 `yfinance` 管理 Yahoo 的会话、cookie 与 crumb；请求串行执行，1.5 秒节流，429 限流时停止后续请求。
- 将每次由 `yfinance` 标准化的 Yahoo 日频返回及元数据缓存至 `data/raw/prices/`，记录来源、抓取时间、时区、频率、币种与复权口径。
- 生成含最新收盘价、成交量、缓存路径和逐标的质量告警的盘后研究报告。
- 将 ETF 官方页面候选资料以 SQLite 审计快照保存到 `data/processed/etf/candidate-history.sqlite3`，支持按标的和抓取时间追溯；候选不会自动写为已核验事实。
- 提供未安装的 macOS `launchd` 每日任务模板。

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

采集 iShares 的 IWM 或 TLT 候选资料时，会保留原始页面和结构化候选文件，并同时写入本地 SQLite 审计库。数据库仅保存 `pending_review` 候选，不会自动修改 `config/etf-profiles.json`。

```bash
PYTHONPATH=src python -m investment_research.cli etf-candidate-review --symbol IWM
```

候选审核报告会读取该 ETF 最近的历史快照；数据库路径为 `data/processed/etf/candidate-history.sqlite3`，与其 WAL/SHM 文件均不提交 Git。

## 分层接入计划

1. `daily_stock_analysis`：盘后复盘、报告、低频提醒。
2. OpenBB：美股、ETF、SEC、宏观的结构化研究数据。
3. Qlib：固定口径历史数据上的日频/低频回测。
4. vn.py：策略通过验证后，仅用于模拟盘、行情、订单状态与风控。

## 定时与实时

`ops/launchd/` 提供每日 18:30 本机时间的模板，尚未安装或启用。启用前需要根据本机用户名、Python 环境与各市场收盘后的数据可用时间修改路径和时间。

定时任务只负责按时间运行；实时提醒需要可信的行情供应商和事件流/WebSocket。免费或延迟数据不能用于自动交易，所有提醒必须人工核验。

详见 [数据口径约定](docs/data-contract.md) 与 [开发路线图](docs/development-plan.md)。
