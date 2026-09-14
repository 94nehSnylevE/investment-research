"""生成带日频数据、ETF 资料状态与质量告警的每日研究报告。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from investment_research.data.daily_prices import DailyPrice, PriceFetchResult, fetch_us_etf_daily_prices
from investment_research.etf_profiles import load_etf_profiles, profile_summary

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WATCHLIST = PROJECT_ROOT / "config" / "watchlists.json"
DATA_ROOT = PROJECT_ROOT / "data"


def load_watchlists(config_path: Path) -> dict[str, Any]:
    """读取并校验观察列表配置，兼容旧版字符串标的和新版对象标的。"""
    with config_path.open(encoding="utf-8") as config_file:
        config = json.load(config_file)

    markets = config.get("markets")
    if not isinstance(markets, dict) or not markets:
        raise ValueError("观察列表必须包含非空的 markets 对象。")
    for market, details in markets.items():
        if not isinstance(details, dict):
            raise ValueError(f"市场 {market} 的配置必须为对象。")
        if not isinstance(details.get("timezone"), str) or not details["timezone"]:
            raise ValueError(f"市场 {market} 必须配置 timezone。")
        symbols = details.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            raise ValueError(f"市场 {market} 必须至少配置一个标的。")
        for item in symbols:
            if isinstance(item, str) and item:
                continue
            if isinstance(item, dict) and isinstance(item.get("symbol"), str) and item["symbol"]:
                continue
            raise ValueError(f"市场 {market} 的每个标的必须是非空代码或包含 symbol 的对象。")
    return config


def build_daily_review(
    config: dict[str, Any],
    generated_at: datetime,
    price_results: Optional[list[PriceFetchResult]] = None,
    etf_profiles: Optional[dict[str, dict[str, Any]]] = None,
) -> str:
    """构建每日研究 Markdown，并清晰区分价格、资料状态、事实和待办。"""
    lines = ["# 每日研究报告", "", f"生成时间（UTC）：{generated_at.isoformat()}", "", "## 本次范围"]
    for market, details in config["markets"].items():
        symbols = ", ".join(_symbol_code(item) for item in details["symbols"])
        lines.append(f"- {market}（{details['timezone']}）：{symbols}")

    if etf_profiles is not None:
        lines.extend(["", "## ETF 静态资料状态", *_profile_status_lines(config, etf_profiles)])

    if price_results is None:
        lines.extend(["", "## 美股 ETF 日频价格", "- `--dry-run`：已校验配置，未联网抓取或写入价格数据缓存。"])
    else:
        lines.extend(["", "## 美股 ETF 观察池概览", *_market_overview(price_results)])
        lines.extend(
            [
                "",
                "## 美股 ETF 日频价格",
                "",
                "| 标的 | 交易日（纽约） | 收盘价 | 日涨跌 | 成交量 | 成交量变化 | 来源 | 原始缓存 | 状态 |",
                "| --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- |",
            ]
        )
        lines.extend(_render_price_row(result) for result in price_results)
        lines.extend(["", "## 研究待办", *_research_todos(price_results)])
        quality_messages = _quality_messages(price_results)
        lines.extend(["", "## 数据质量告警"])
        lines.extend(f"- {message}" for message in quality_messages) if quality_messages else lines.append(
            "- 未发现阻断性错误；仍须人工核验复权口径。"
        )

    lines.extend(
        [
            "",
            "## 使用边界",
            "- 数据仅供研究；免费或延迟数据不得用于自动交易。",
            "- 当前记录 Yahoo 的未复权收盘价；不同复权口径不得混入回测。",
            "- ETF 静态资料只有 `verified` 状态且带来源的条目才能作为事实；当前待核验条目不展示候选数值。",
            "- 观察池概览为等权描述性统计，不是指数、预测、投资建议或交易信号。",
            "- 所有结论与提醒均需人工核验；本报告不产生交易指令或下单请求。",
        ]
    )
    return "\n".join(lines) + "\n"


def run_daily_review(config_path: Path = DEFAULT_WATCHLIST, dry_run: bool = False) -> Optional[Path]:
    """生成报告；dry_run 只校验配置及预览，绝不联网或写入价格缓存。"""
    config = load_watchlists(config_path)
    etf_profiles = load_etf_profiles()
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    if dry_run:
        print(build_daily_review(config, generated_at, etf_profiles=etf_profiles), end="")
        return None

    price_results = fetch_us_etf_daily_prices(_us_etf_symbols(config), DATA_ROOT)
    report = build_daily_review(config, generated_at, price_results, etf_profiles)
    report_directory = PROJECT_ROOT / "reports" / "daily"
    report_directory.mkdir(parents=True, exist_ok=True)
    report_path = report_directory / f"{generated_at.date().isoformat()}_daily_review.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"已生成研究报告：{report_path}")
    return report_path


def _us_etf_symbols(config: dict[str, Any]) -> list[str]:
    us_market = config["markets"].get("us", {})
    return [
        item["symbol"].upper()
        for item in us_market.get("symbols", [])
        if isinstance(item, dict) and item.get("asset_class") == "ETF"
    ]


def _symbol_code(item: Any) -> str:
    return item if isinstance(item, str) else item["symbol"]


def _profile_status_lines(config: dict[str, Any], profiles: dict[str, dict[str, Any]]) -> list[str]:
    lines = [
        "",
        "| 标的 | 资料状态 | 已核验事实 | 待核验事实 | 审核时间 |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for symbol in _us_etf_symbols(config):
        profile = profiles.get(symbol)
        if profile is None:
            lines.append(f"| {symbol} | 缺失 | 0 | N/A | N/A |")
            continue
        status, verified_count, pending_count = profile_summary(profile)
        reviewed_at = profile["reviewed_at"] or "N/A"
        lines.append(f"| {symbol} | {status} | {verified_count} | {pending_count} | {reviewed_at} |")
    lines.append("- 静态资料模板：`docs/templates/etf-research.md`；待核验状态不会把候选数值写入事实区。")
    return lines


def _market_overview(price_results: list[PriceFetchResult]) -> list[str]:
    valid_prices = [result.price for result in price_results if result.price and result.price.daily_change_pct is not None]
    if not valid_prices:
        return ["- 暂无可计算的日涨跌数据；请检查前一完整交易日是否可得。"]
    prices: list[DailyPrice] = [price for price in valid_prices if price is not None]
    changes = [price.daily_change_pct for price in prices if price.daily_change_pct is not None]
    average_change = sum(changes) / len(changes)
    gainers, decliners = sum(change > 0 for change in changes), sum(change < 0 for change in changes)
    strongest = max(prices, key=lambda price: price.daily_change_pct if price.daily_change_pct is not None else float("-inf"))
    weakest = min(prices, key=lambda price: price.daily_change_pct if price.daily_change_pct is not None else float("inf"))
    live_count = sum(result.data_status == "live" and result.price is not None for result in price_results)
    cached_count = sum(result.data_status == "cache" and result.price is not None for result in price_results)
    return [
        f"- 可比较标的：{len(changes)}/{len(price_results)}；等权平均日涨跌：{average_change:+.2f}%。",
        f"- 上涨 {gainers}，下跌 {decliners}，平盘 {len(changes) - gainers - decliners}；仅反映此观察池，不代表市场指数。",
        f"- 当日相对最强：{strongest.symbol}（{strongest.daily_change_pct:+.2f}%）；最弱：{weakest.symbol}（{weakest.daily_change_pct:+.2f}%）。",
        f"- 数据状态：实时拉取 {live_count}，缓存降级 {cached_count}；缓存数据的抓取时间见下表。",
    ]


def _research_todos(price_results: list[PriceFetchResult]) -> list[str]:
    movable = [result for result in price_results if result.price and result.price.daily_change_pct is not None]
    moves = sorted(movable, key=lambda result: abs(result.price.daily_change_pct or 0), reverse=True)
    todos = ["- [ ] 核验经济日历、利率与 ETF 相关宏观事件（当前未接入宏观数据源）。"]
    if moves:
        leading = moves[0].price
        assert leading is not None
        todos.append(
            f"- [ ] 核验 {leading.symbol} 日涨跌 {leading.daily_change_pct:+.2f}% 的新闻、公告或成分暴露变化（当前未接入新闻数据源）。"
        )
    todos.append("- [ ] 使用官方资料人工审核 ETF 费用率、基准和持仓/权重，再将带来源的事实写入 profile。")
    todos.append("- [ ] 人工复核 Yahoo 未复权收盘价、交易日及异常成交量，再形成研究结论。")
    return todos


def _render_price_row(result: PriceFetchResult) -> str:
    cache_path = result.cache_path.relative_to(PROJECT_ROOT)
    if result.price is None:
        return f"| {result.symbol} | N/A | N/A | N/A | N/A | N/A | {result.provider} | `{cache_path}` | 失败：{result.error} |"
    price = result.price
    volume = f"{price.volume:,}" if price.volume is not None else "N/A"
    status = "缓存降级（需人工核验）" if result.data_status == "cache" else "需人工核验" if result.warnings else "可用"
    return (
        f"| {price.symbol} | {price.timestamp[:10]} | {price.close:.2f} | {_format_pct(price.daily_change_pct)} | "
        f"{volume} | {_format_pct(price.volume_change_pct)} | {price.provider} | `{cache_path}` | {status} |"
    )


def _format_pct(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:+.2f}%"


def _quality_messages(price_results: list[PriceFetchResult]) -> list[str]:
    messages: list[str] = []
    for result in price_results:
        if result.error:
            messages.append(f"{result.symbol}：{result.error}")
        messages.extend(f"{result.symbol}：{warning}" for warning in result.warnings)
    return messages
