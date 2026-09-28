"""基于冻结数据集的朴素基线回测。

只实现可解释、可复现的规则：买入持有、定期等权再平衡、单标的趋势规则。所有结果都包含
交易成本与换手，并与基准并列展示；本模块不产生交易信号，也不输出下单指令。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from investment_research.backtest.dataset import DEFAULT_DATASET_ROOT, load_frozen_dataset
from investment_research.etf_profiles import PROJECT_ROOT

TRADING_DAYS_PER_YEAR = 252
DEFAULT_COST_BPS = 5.0
DEFAULT_REPORT_DIRECTORY = PROJECT_ROOT / "reports" / "backtest"
STRATEGIES = ("buy_and_hold", "monthly_equal_weight", "sma_trend")


@dataclass(frozen=True)
class BacktestResult:
    strategy: str
    parameters: dict[str, Any]
    dataset_id: str
    start_date: str
    end_date: str
    metrics: dict[str, float]
    benchmark_metrics: dict[str, float]
    equity_curve: list[tuple[str, float]] = field(repr=False, default_factory=list)


def run_baseline_backtests(
    dataset_id: Optional[str] = None,
    benchmark: str = "SPY",
    cost_bps: float = DEFAULT_COST_BPS,
    sma_window: int = 200,
    dataset_root: Path = DEFAULT_DATASET_ROOT,
) -> list[BacktestResult]:
    """在同一冻结数据集上运行三个朴素基线，并统一计算成本后的绩效。"""
    if cost_bps < 0:
        raise ValueError("cost_bps 不能为负。")
    if sma_window < 20:
        raise ValueError("sma_window 至少为 20 个交易日。")
    manifest, dates, columns = load_frozen_dataset(dataset_id, dataset_root)
    symbols = list(manifest["symbols"])
    if benchmark not in symbols:
        raise ValueError(f"基准 {benchmark} 不在数据集中。")

    returns = {symbol: _simple_returns(columns[symbol]) for symbol in symbols}
    benchmark_weights = _constant_weights(len(dates) - 1, {benchmark: 1.0}, symbols)
    benchmark_curve, benchmark_turnover, benchmark_cost = _simulate(returns, benchmark_weights, symbols, cost_bps)
    benchmark_metrics = _metrics(benchmark_curve, benchmark_turnover, benchmark_cost, dates)

    results: list[BacktestResult] = []
    equal_weight = {symbol: 1.0 / len(symbols) for symbol in symbols}
    plans = {
        "buy_and_hold": _buy_and_hold_weights(returns, symbols, equal_weight),
        "monthly_equal_weight": _monthly_rebalance_weights(dates, symbols, equal_weight, returns),
        "sma_trend": _sma_trend_weights(dates, columns, symbols, sma_window),
    }
    for strategy, weights in plans.items():
        curve, turnover, cost = _simulate(returns, weights, symbols, cost_bps)
        parameters: dict[str, Any] = {"cost_bps": cost_bps, "benchmark": benchmark}
        if strategy == "sma_trend":
            parameters["sma_window"] = sma_window
        results.append(
            BacktestResult(
                strategy=strategy,
                parameters=parameters,
                dataset_id=manifest["dataset_id"],
                start_date=dates[0],
                end_date=dates[-1],
                metrics=_metrics(curve, turnover, cost, dates),
                benchmark_metrics=benchmark_metrics,
                equity_curve=list(zip(dates[1:], curve)),
            )
        )
    return results


def write_backtest_report(
    results: list[BacktestResult], output_directory: Path = DEFAULT_REPORT_DIRECTORY
) -> Path:
    """写出可复现的回测报告；明确标注研究用途与成本假设。"""
    if not results:
        raise ValueError("没有回测结果可写入。")
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    output_directory.mkdir(parents=True, exist_ok=True)
    report_path = output_directory / f"{generated_at.date().isoformat()}_baseline_backtest.md"
    first = results[0]
    lines = [
        "# 朴素基线回测报告",
        "",
        f"生成时间（UTC）：{generated_at.isoformat()}",
        f"数据集版本：`{first.dataset_id}`；区间：{first.start_date} 至 {first.end_date}",
        f"成本假设：单边 {first.parameters['cost_bps']:g} bps；基准：{first.parameters['benchmark']}",
        "",
        "| 策略 | 年化收益 | 年化波动 | 最大回撤 | 夏普 | 年化换手 | 成本拖累 | 相对基准年化 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for result in results:
        metrics = result.metrics
        excess = metrics["cagr_pct"] - result.benchmark_metrics["cagr_pct"]
        lines.append(
            f"| {result.strategy} | {metrics['cagr_pct']:+.2f}% | {metrics['volatility_pct']:.2f}% | "
            f"{metrics['max_drawdown_pct']:.2f}% | {metrics['sharpe']:.2f} | "
            f"{metrics['annual_turnover']:.2f}x | {metrics['cost_drag_pct']:.2f}% | {excess:+.2f}% |"
        )
    benchmark = first.benchmark_metrics
    lines.append(
        f"| 基准 {first.parameters['benchmark']}（买入持有） | {benchmark['cagr_pct']:+.2f}% | "
        f"{benchmark['volatility_pct']:.2f}% | {benchmark['max_drawdown_pct']:.2f}% | "
        f"{benchmark['sharpe']:.2f} | {benchmark['annual_turnover']:.2f}x | "
        f"{benchmark['cost_drag_pct']:.2f}% | +0.00% |"
    )
    lines.extend(
        [
            "",
            "## 使用边界",
            "- 结果基于单一冻结数据集与 Yahoo 复权总回报口径；更换数据版本或复权方式结论可能改变。",
            "- 夏普按零无风险利率计算；未计入税费、融资成本、冲击成本与流动性限制。",
            "- 样本内规则展示不等于样本外可重复性；不构成交易信号、投资建议或下单指令。",
            "- 所有策略均为等权、无杠杆、无做空的朴素基线，仅用于建立可比参照。",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def _simple_returns(prices: list[float]) -> list[float]:
    return [prices[index] / prices[index - 1] - 1 for index in range(1, len(prices))]


def _constant_weights(
    periods: int, weights: dict[str, float], symbols: list[str]
) -> list[dict[str, float]]:
    row = {symbol: weights.get(symbol, 0.0) for symbol in symbols}
    return [dict(row) for _ in range(periods)]


def _buy_and_hold_weights(
    returns: dict[str, list[float]], symbols: list[str], initial: dict[str, float]
) -> list[dict[str, float]]:
    periods = len(returns[symbols[0]])
    holdings = {symbol: initial[symbol] for symbol in symbols}
    weights: list[dict[str, float]] = []
    for index in range(periods):
        total = sum(holdings.values())
        weights.append({symbol: holdings[symbol] / total for symbol in symbols})
        holdings = {symbol: holdings[symbol] * (1 + returns[symbol][index]) for symbol in symbols}
    return weights


def _monthly_rebalance_weights(
    dates: list[str],
    symbols: list[str],
    target: dict[str, float],
    returns: dict[str, list[float]],
) -> list[dict[str, float]]:
    """仅在月度首个交易日恢复目标权重，其余交易日按持仓自然漂移。"""
    weights: list[dict[str, float]] = []
    holdings = {symbol: target[symbol] for symbol in symbols}
    for index in range(1, len(dates)):
        if dates[index - 1][:7] != dates[index][:7]:
            holdings = {symbol: target[symbol] for symbol in symbols}
        total = sum(holdings.values())
        current = {symbol: holdings[symbol] / total for symbol in symbols}
        weights.append(current)
        holdings = {symbol: current[symbol] * (1 + returns[symbol][index - 1]) for symbol in symbols}
    return weights


def _sma_trend_weights(
    dates: list[str], columns: dict[str, list[float]], symbols: list[str], window: int
) -> list[dict[str, float]]:
    weights: list[dict[str, float]] = []
    for index in range(1, len(dates)):
        signal_index = index - 1
        holdings: dict[str, float] = {}
        for symbol in symbols:
            if signal_index + 1 < window:
                holdings[symbol] = 0.0
                continue
            window_prices = columns[symbol][signal_index + 1 - window : signal_index + 1]
            average = sum(window_prices) / window
            holdings[symbol] = 1.0 if columns[symbol][signal_index] > average else 0.0
        active = sum(holdings.values())
        if active:
            weights.append({symbol: holdings[symbol] / active for symbol in symbols})
        else:
            weights.append({symbol: 0.0 for symbol in symbols})
    return weights


def _simulate(
    returns: dict[str, list[float]], weights: list[dict[str, float]], symbols: list[str], cost_bps: float
) -> tuple[list[float], float, float]:
    equity = 1.0
    curve: list[float] = []
    previous = {symbol: 0.0 for symbol in symbols}
    total_turnover = 0.0
    total_cost = 0.0
    cost_rate = cost_bps / 10_000
    for index, target in enumerate(weights):
        turnover = sum(abs(target[symbol] - previous[symbol]) for symbol in symbols) / 2
        cost = turnover * cost_rate
        total_turnover += turnover
        total_cost += cost
        gross = sum(target[symbol] * returns[symbol][index] for symbol in symbols)
        equity *= (1 + gross) * (1 - cost)
        curve.append(equity)
        drifted = {symbol: target[symbol] * (1 + returns[symbol][index]) for symbol in symbols}
        total = sum(drifted.values())
        previous = {symbol: (drifted[symbol] / total if total else 0.0) for symbol in symbols}
    return curve, total_turnover, total_cost


def _metrics(curve: list[float], turnover: float, cost: float, dates: list[str]) -> dict[str, float]:
    years = len(curve) / TRADING_DAYS_PER_YEAR
    total_return = curve[-1] - 1
    cagr = (curve[-1] ** (1 / years) - 1) if years > 0 and curve[-1] > 0 else float("nan")
    daily = [curve[0] - 1] + [curve[index] / curve[index - 1] - 1 for index in range(1, len(curve))]
    mean = sum(daily) / len(daily)
    variance = sum((value - mean) ** 2 for value in daily) / (len(daily) - 1)
    volatility = math.sqrt(variance) * math.sqrt(TRADING_DAYS_PER_YEAR)
    peak = curve[0]
    max_drawdown = 0.0
    for value in curve:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1)
    sharpe = (mean * TRADING_DAYS_PER_YEAR) / volatility if volatility > 0 else float("nan")
    return {
        "total_return_pct": total_return * 100,
        "cagr_pct": cagr * 100,
        "volatility_pct": volatility * 100,
        "max_drawdown_pct": max_drawdown * 100,
        "sharpe": sharpe,
        "annual_turnover": turnover / years if years > 0 else float("nan"),
        "cost_drag_pct": cost * 100,
    }
