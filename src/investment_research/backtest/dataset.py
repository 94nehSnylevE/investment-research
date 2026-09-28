"""回测专用的版本冻结日频数据集。

数据集使用 Yahoo 复权（总回报）收盘价，与 ``data/raw/prices`` 的未复权日频价格库完全分开；
两者不得混用。每个版本都写入不可再修改的 CSV、清单与数据质量报告，便于按版本复现回测。
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from investment_research.etf_profiles import PROJECT_ROOT

PROVIDER = "yahoo_finance"
MARKET = "us"
MARKET_TIMEZONE = "America/New_York"
INTERVAL = "1d"
ADJUSTMENT_METHOD = "yahoo_auto_adjusted_total_return"
REQUEST_DELAY_SECONDS = 1.5
DEFAULT_DATASET_ROOT = PROJECT_ROOT / "data" / "processed" / "backtest" / "datasets"
MAX_GAP_TRADING_DAYS = 5


@dataclass(frozen=True)
class DatasetBuildResult:
    dataset_id: str
    dataset_directory: Path
    prices_path: Path
    manifest_path: Path
    quality_report_path: Path
    symbols: tuple[str, ...]
    start_date: str
    end_date: str
    row_count: int
    blocking_issues: tuple[str, ...]
    warnings: tuple[str, ...]


def build_frozen_dataset(
    symbols: Iterable[str],
    period: str = "10y",
    dataset_root: Path = DEFAULT_DATASET_ROOT,
    timeout_seconds: int = 30,
) -> DatasetBuildResult:
    """抓取长历史复权收盘价并冻结为带清单和质量报告的数据集版本。"""
    symbol_list = tuple(sorted({symbol.upper() for symbol in symbols}))
    if not symbol_list:
        raise ValueError("至少需要一个标的。")
    built_at = datetime.now(timezone.utc).replace(microsecond=0)
    series: dict[str, dict[str, float]] = {}
    failures: list[str] = []
    for index, symbol in enumerate(symbol_list):
        if index:
            time.sleep(REQUEST_DELAY_SECONDS)
        try:
            series[symbol] = _fetch_adjusted_close(symbol, period, timeout_seconds)
        except ValueError as error:
            failures.append(f"{symbol}：{error}")
    if failures:
        raise ValueError("数据集未冻结，以下标的获取失败：" + "；".join(failures))

    dates = sorted(set().union(*(set(values) for values in series.values())))
    if not dates:
        raise ValueError("未取得任何交易日。")
    rows, missing = _align_rows(symbol_list, series, dates)
    quality = _quality_report(symbol_list, rows, missing)
    dataset_id = built_at.strftime("%Y%m%dT%H%M%SZ")
    directory = dataset_root / dataset_id
    directory.mkdir(parents=True, exist_ok=True)
    prices_path = directory / "adjusted_close.csv"
    prices_path.write_text(_render_csv(symbol_list, rows), encoding="utf-8")
    prices_sha256 = hashlib.sha256(prices_path.read_bytes()).hexdigest()

    manifest = {
        "schema_version": 1,
        "dataset_id": dataset_id,
        "built_at": built_at.isoformat(),
        "provider": PROVIDER,
        "market": MARKET,
        "timezone": MARKET_TIMEZONE,
        "interval": INTERVAL,
        "adjustment_method": ADJUSTMENT_METHOD,
        "currency": "USD",
        "requested_period": period,
        "symbols": list(symbol_list),
        "start_date": rows[0]["date"],
        "end_date": rows[-1]["date"],
        "row_count": len(rows),
        "prices_file": prices_path.name,
        "prices_sha256": prices_sha256,
        "status": "frozen",
        "usage_boundary": "仅用于研究回测；与未复权日频价格库口径不同，不得混用或用于自动交易。",
    }
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    quality_report_path = directory / "quality-report.json"
    quality_report_path.write_text(json.dumps(quality, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return DatasetBuildResult(
        dataset_id=dataset_id,
        dataset_directory=directory,
        prices_path=prices_path,
        manifest_path=manifest_path,
        quality_report_path=quality_report_path,
        symbols=symbol_list,
        start_date=rows[0]["date"],
        end_date=rows[-1]["date"],
        row_count=len(rows),
        blocking_issues=tuple(quality["blocking_issues"]),
        warnings=tuple(quality["warnings"]),
    )


def load_frozen_dataset(
    dataset_id: Optional[str] = None, dataset_root: Path = DEFAULT_DATASET_ROOT
) -> tuple[dict[str, Any], list[str], dict[str, list[float]]]:
    """按版本读取冻结数据集，并校验证据哈希；返回清单、日期与各标的价格序列。"""
    directory = _resolve_dataset(dataset_id, dataset_root)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") != "frozen" or manifest.get("schema_version") != 1:
        raise ValueError("数据集清单无效或未冻结。")
    prices_path = directory / manifest["prices_file"]
    actual_hash = hashlib.sha256(prices_path.read_bytes()).hexdigest()
    if actual_hash != manifest["prices_sha256"]:
        raise ValueError("数据集文件哈希与清单不一致；拒绝用于回测。")

    lines = prices_path.read_text(encoding="utf-8").strip().splitlines()
    header = lines[0].split(",")
    symbols = header[1:]
    if symbols != list(manifest["symbols"]):
        raise ValueError("数据集列与清单标的不一致。")
    dates: list[str] = []
    columns: dict[str, list[float]] = {symbol: [] for symbol in symbols}
    for line in lines[1:]:
        parts = line.split(",")
        dates.append(parts[0])
        for symbol, value in zip(symbols, parts[1:]):
            if not value:
                raise ValueError(f"数据集存在缺失值：{parts[0]} {symbol}。")
            columns[symbol].append(float(value))
    return manifest, dates, columns


def list_frozen_datasets(dataset_root: Path = DEFAULT_DATASET_ROOT) -> list[dict[str, Any]]:
    """只读列出本地冻结数据集版本。"""
    if not dataset_root.exists():
        return []
    summaries: list[dict[str, Any]] = []
    for directory in sorted(dataset_root.iterdir()):
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        summaries.append(
            {
                "dataset_id": manifest["dataset_id"],
                "symbols": manifest["symbols"],
                "start_date": manifest["start_date"],
                "end_date": manifest["end_date"],
                "row_count": manifest["row_count"],
                "adjustment_method": manifest["adjustment_method"],
            }
        )
    return summaries


def _fetch_adjusted_close(symbol: str, period: str, timeout_seconds: int) -> dict[str, float]:
    import yfinance as yf

    try:
        history = yf.Ticker(symbol).history(
            period=period,
            interval=INTERVAL,
            auto_adjust=True,
            actions=False,
            raise_errors=True,
            timeout=timeout_seconds,
        )
    except Exception as error:
        raise ValueError(f"获取失败（{type(error).__name__}）") from error
    if history.empty:
        raise ValueError("未返回日频记录")
    values: dict[str, float] = {}
    for timestamp, row in history.iterrows():
        close = row.get("Close")
        try:
            price = float(close)
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and price > 0:
            values[timestamp.date().isoformat()] = price
    if not values:
        raise ValueError("没有有效的复权收盘价")
    return values


def _align_rows(
    symbols: tuple[str, ...], series: dict[str, dict[str, float]], dates: list[str]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows: list[dict[str, Any]] = []
    missing = {symbol: 0 for symbol in symbols}
    for day in dates:
        values = {symbol: series[symbol].get(day) for symbol in symbols}
        if any(value is None for value in values.values()):
            for symbol, value in values.items():
                if value is None:
                    missing[symbol] += 1
            continue
        rows.append({"date": day, **values})
    if not rows:
        raise ValueError("没有所有标的都有价格的共同交易日。")
    return rows, missing


def _quality_report(
    symbols: tuple[str, ...], rows: list[dict[str, Any]], missing: dict[str, int]
) -> dict[str, Any]:
    warnings: list[str] = []
    blocking: list[str] = []
    for symbol, count in missing.items():
        if count:
            warnings.append(f"{symbol}：有 {count} 个交易日缺少价格，已从共同交易日中剔除。")

    gaps: list[dict[str, Any]] = []
    for previous, current in zip(rows, rows[1:]):
        previous_date = datetime.fromisoformat(previous["date"]).date()
        current_date = datetime.fromisoformat(current["date"]).date()
        calendar_gap = (current_date - previous_date).days
        if calendar_gap > MAX_GAP_TRADING_DAYS + 2:
            gaps.append({"from": previous["date"], "to": current["date"], "calendar_days": calendar_gap})

    extreme_moves: list[dict[str, Any]] = []
    for symbol in symbols:
        for previous, current in zip(rows, rows[1:]):
            change = current[symbol] / previous[symbol] - 1
            if abs(change) >= 0.25:
                extreme_moves.append(
                    {"symbol": symbol, "date": current["date"], "change_pct": round(change * 100, 4)}
                )
    if gaps:
        warnings.append(f"存在 {len(gaps)} 处超过一周的日期间隔，需人工核对停牌或假期。")
    if extreme_moves:
        warnings.append(f"存在 {len(extreme_moves)} 个单日 ±25% 以上变动，需人工核对复权与公司行为。")
    if len(rows) < 500:
        blocking.append(f"共同交易日仅 {len(rows)} 天，样本过短，不足以支撑样本外结论。")

    return {
        "schema_version": 1,
        "row_count": len(rows),
        "symbols": list(symbols),
        "missing_by_symbol": missing,
        "date_gaps": gaps[:50],
        "extreme_moves": extreme_moves[:50],
        "warnings": warnings,
        "blocking_issues": blocking,
        "status": "pending_review",
    }


def _render_csv(symbols: tuple[str, ...], rows: list[dict[str, Any]]) -> str:
    lines = ["date," + ",".join(symbols)]
    for row in rows:
        lines.append(row["date"] + "," + ",".join(f"{row[symbol]:.6f}" for symbol in symbols))
    return "\n".join(lines) + "\n"


def _resolve_dataset(dataset_id: Optional[str], dataset_root: Path) -> Path:
    if not dataset_root.exists():
        raise ValueError("本地没有冻结数据集；请先运行 backtest-dataset build。")
    if dataset_id:
        directory = dataset_root / dataset_id
        if not (directory / "manifest.json").is_file():
            raise ValueError(f"未找到数据集版本：{dataset_id}。")
        return directory
    candidates = [item for item in sorted(dataset_root.iterdir()) if (item / "manifest.json").is_file()]
    if not candidates:
        raise ValueError("本地没有冻结数据集；请先运行 backtest-dataset build。")
    return candidates[-1]
