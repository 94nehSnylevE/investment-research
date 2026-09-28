"""朴素因子与方向预测基线（严格样本外）。

模块只回答一个研究问题：简单价格因子在样本外能否比「无条件猜涨」更好地判断未来涨跌。
所有评估都用滚动前向验证，并与朴素基准并列；命中率不足或样本过小时必须明确拒绝结论。
本模块不产生交易信号、目标价或下单指令。
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from investment_research.backtest.dataset import DEFAULT_DATASET_ROOT, load_frozen_dataset
from investment_research.etf_profiles import PROJECT_ROOT
from investment_research.macro_releases import DEFAULT_RELEASE_DB

DEFAULT_REPORT_DIRECTORY = PROJECT_ROOT / "reports" / "backtest"
FEATURE_NAMES = ("momentum_20", "momentum_60", "momentum_120", "sma_200_gap", "volatility_20")
MIN_TRAIN_ROWS = 500
MIN_EVALUATION_ROWS = 250
RIDGE_PENALTY = 1e-3


@dataclass(frozen=True)
class DirectionEvaluation:
    symbol: str
    horizon_days: int
    evaluated_periods: int
    hit_rate_pct: float
    baseline_up_rate_pct: float
    edge_pct: float
    information_coefficient: float
    binomial_z: float
    verdict: str


@dataclass(frozen=True)
class MacroEventStudy:
    metric_key: str
    symbol: str
    observations: int
    positive_surprise_mean_pct: Optional[float]
    negative_surprise_mean_pct: Optional[float]
    verdict: str


def evaluate_direction_baseline(
    dataset_id: Optional[str] = None,
    horizon_days: int = 5,
    dataset_root: Path = DEFAULT_DATASET_ROOT,
) -> tuple[dict[str, Any], list[DirectionEvaluation]]:
    """滚动样本外评估简单因子对未来涨跌的判别力。"""
    if horizon_days < 1:
        raise ValueError("horizon_days 必须大于 0。")
    manifest, dates, columns = load_frozen_dataset(dataset_id, dataset_root)
    evaluations: list[DirectionEvaluation] = []
    for symbol in manifest["symbols"]:
        prices = columns[symbol]
        samples = _build_samples(dates, prices, horizon_days)
        if len(samples) < MIN_TRAIN_ROWS + MIN_EVALUATION_ROWS:
            evaluations.append(
                DirectionEvaluation(
                    symbol=symbol,
                    horizon_days=horizon_days,
                    evaluated_periods=len(samples),
                    hit_rate_pct=float("nan"),
                    baseline_up_rate_pct=float("nan"),
                    edge_pct=float("nan"),
                    information_coefficient=float("nan"),
                    binomial_z=float("nan"),
                    verdict="样本不足，拒绝给出预测结论",
                )
            )
            continue
        evaluations.append(_walk_forward(symbol, horizon_days, samples))
    return manifest, evaluations


def study_macro_surprise_reaction(
    dataset_id: Optional[str] = None,
    symbols: tuple[str, ...] = ("SPY", "GLD"),
    dataset_root: Path = DEFAULT_DATASET_ROOT,
    release_database_path: Path = DEFAULT_RELEASE_DB,
) -> list[MacroEventStudy]:
    """对已记录的宏观预期差做事件研究；样本不足时不给出方向结论。"""
    manifest, dates, columns = load_frozen_dataset(dataset_id, dataset_root)
    surprises = _load_surprises(release_database_path)
    date_index = {day: index for index, day in enumerate(dates)}
    studies: list[MacroEventStudy] = []
    for metric_key, events in sorted(surprises.items()):
        for symbol in symbols:
            if symbol not in manifest["symbols"]:
                continue
            positive: list[float] = []
            negative: list[float] = []
            for release_date, surprise in events:
                index = _next_trading_index(release_date, dates, date_index)
                if index is None or index + 1 >= len(dates):
                    continue
                reaction = (columns[symbol][index + 1] / columns[symbol][index] - 1) * 100
                (positive if surprise > 0 else negative).append(reaction)
            observations = len(positive) + len(negative)
            studies.append(
                MacroEventStudy(
                    metric_key=metric_key,
                    symbol=symbol,
                    observations=observations,
                    positive_surprise_mean_pct=_mean(positive),
                    negative_surprise_mean_pct=_mean(negative),
                    verdict=(
                        "样本不足，仅作记录，不构成方向结论"
                        if observations < 20
                        else "样本仍偏小，需人工核验后再解读"
                    ),
                )
            )
    return studies


def write_factor_report(
    manifest: dict[str, Any],
    evaluations: list[DirectionEvaluation],
    studies: list[MacroEventStudy],
    output_directory: Path = DEFAULT_REPORT_DIRECTORY,
) -> Path:
    """写出方向预测与宏观事件研究报告，并保留统计与使用边界。"""
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    output_directory.mkdir(parents=True, exist_ok=True)
    report_path = output_directory / f"{generated_at.date().isoformat()}_factor_direction_baseline.md"
    horizon = evaluations[0].horizon_days if evaluations else 0
    lines = [
        "# 因子方向预测基线（严格样本外）",
        "",
        f"生成时间（UTC）：{generated_at.isoformat()}",
        f"数据集版本：`{manifest['dataset_id']}`；区间：{manifest['start_date']} 至 {manifest['end_date']}",
        f"预测目标：未来 {horizon} 个交易日的涨跌方向；特征：{', '.join(FEATURE_NAMES)}",
        "",
        "| 标的 | 样本外期数 | 命中率 | 猜涨基准 | 超出基准 | IC | z 值 | 结论 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in evaluations:
        if math.isnan(item.hit_rate_pct):
            lines.append(f"| {item.symbol} | {item.evaluated_periods} | N/A | N/A | N/A | N/A | N/A | {item.verdict} |")
            continue
        lines.append(
            f"| {item.symbol} | {item.evaluated_periods} | {item.hit_rate_pct:.2f}% | "
            f"{item.baseline_up_rate_pct:.2f}% | {item.edge_pct:+.2f}pp | "
            f"{item.information_coefficient:+.3f} | {item.binomial_z:+.2f} | {item.verdict} |"
        )

    lines.extend(["", "## 宏观预期差事件研究", ""])
    if not studies:
        lines.append("- 本地尚无带 surprise 的宏观发布记录；请先用 `macro-release expect` 与 `sync-actuals` 积累样本。")
    else:
        lines.extend(
            [
                "| 指标 | 标的 | 事件数 | 正预期差次日均值 | 负预期差次日均值 | 结论 |",
                "| --- | --- | ---: | ---: | ---: | --- |",
            ]
        )
        for study in studies:
            positive = "N/A" if study.positive_surprise_mean_pct is None else f"{study.positive_surprise_mean_pct:+.2f}%"
            negative = "N/A" if study.negative_surprise_mean_pct is None else f"{study.negative_surprise_mean_pct:+.2f}%"
            lines.append(
                f"| {study.metric_key} | {study.symbol} | {study.observations} | {positive} | {negative} | {study.verdict} |"
            )

    lines.extend(
        [
            "",
            "## 使用边界",
            "- 命中率必须与「猜涨基准」对比；仅高于 50% 不代表有效，需同时看超出基准幅度与 z 值。",
            "- |z| < 2 时不能排除随机性；本基线不做多重检验校正，也未扣除交易成本与滑点。",
            "- 评估为滚动前向、仅用历史数据训练；但仍可能存在数据版本、复权口径与幸存者偏差。",
            "- 宏观事件研究样本极小，只记录历史反应分布，不构成任何发布后方向预测。",
            "- 本报告不产生交易信号、目标价或下单指令；所有结论需人工核验。",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def _build_samples(dates: list[str], prices: list[float], horizon: int) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for index in range(200, len(prices) - horizon):
        window = prices[index - 199 : index + 1]
        average = sum(window) / len(window)
        daily = [window[step] / window[step - 1] - 1 for step in range(len(window) - 20, len(window))]
        mean = sum(daily) / len(daily)
        variance = sum((value - mean) ** 2 for value in daily) / (len(daily) - 1)
        features = (
            prices[index] / prices[index - 20] - 1,
            prices[index] / prices[index - 60] - 1,
            prices[index] / prices[index - 120] - 1,
            prices[index] / average - 1,
            math.sqrt(variance) * math.sqrt(252),
        )
        forward = prices[index + horizon] / prices[index] - 1
        samples.append({"date": dates[index], "features": features, "target": forward})
    return samples


def _walk_forward(symbol: str, horizon: int, samples: list[dict[str, Any]]) -> DirectionEvaluation:
    import numpy as np

    matrix = np.array([sample["features"] for sample in samples], dtype=float)
    targets = np.array([sample["target"] for sample in samples], dtype=float)
    predictions: list[float] = []
    realized: list[float] = []
    for index in range(MIN_TRAIN_ROWS, len(samples), horizon):
        train_end = index - horizon
        if train_end <= 0:
            continue
        train_x = matrix[:train_end]
        train_y = targets[:train_end]
        mean = train_x.mean(axis=0)
        std = train_x.std(axis=0)
        std[std == 0] = 1.0
        scaled = (train_x - mean) / std
        design = np.hstack([np.ones((scaled.shape[0], 1)), scaled])
        penalty = RIDGE_PENALTY * np.eye(design.shape[1])
        penalty[0, 0] = 0.0
        coefficients = np.linalg.solve(design.T @ design + penalty, design.T @ train_y)
        scaled_test = (matrix[index] - mean) / std
        prediction = float(coefficients[0] + scaled_test @ coefficients[1:])
        predictions.append(prediction)
        realized.append(float(targets[index]))

    hits = sum(1 for predicted, actual in zip(predictions, realized) if (predicted > 0) == (actual > 0))
    periods = len(predictions)
    hit_rate = hits / periods * 100
    baseline = sum(1 for value in realized if value > 0) / periods * 100
    correlation = float(np.corrcoef(predictions, realized)[0, 1]) if periods > 2 else float("nan")
    z_value = (hits - periods * 0.5) / math.sqrt(periods * 0.25)
    verdict = (
        "命中率未超过猜涨基准，无证据支持预测力"
        if hit_rate <= baseline
        else "略高于基准但 z 值不显著，视为随机"
        if abs(z_value) < 2
        else "高于基准且 z 值显著，仍需样本外复核与成本检验"
    )
    return DirectionEvaluation(
        symbol=symbol,
        horizon_days=horizon,
        evaluated_periods=periods,
        hit_rate_pct=hit_rate,
        baseline_up_rate_pct=baseline,
        edge_pct=hit_rate - baseline,
        information_coefficient=correlation,
        binomial_z=z_value,
        verdict=verdict,
    )


def _load_surprises(database_path: Path) -> dict[str, list[tuple[str, float]]]:
    if not database_path.exists():
        return {}
    connection = sqlite3.connect(f"file:{database_path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """SELECT a.metric_key, r.scheduled_at_utc, a.surprise_value_text
               FROM actual_vintages a JOIN release_instances r ON r.release_id = a.release_id
               WHERE a.revision_number = 0 AND a.surprise_value_text IS NOT NULL"""
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    grouped: dict[str, list[tuple[str, float]]] = {}
    for row in rows:
        release_date = row["scheduled_at_utc"][:10]
        grouped.setdefault(row["metric_key"], []).append((release_date, float(row["surprise_value_text"])))
    return grouped


def _next_trading_index(
    release_date: str, dates: list[str], date_index: dict[str, int]
) -> Optional[int]:
    if release_date in date_index:
        return date_index[release_date]
    for index, day in enumerate(dates):
        if day >= release_date:
            return index
    return None


def _mean(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None
