"""Financial Modeling Prep（FMP）美股 ETF 日频交叉校验适配器。

仅用于与 Yahoo Finance 独立比对；不产生交易信号，也不写入 ETF 静态事实。
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from investment_research.data.daily_prices import DailyPrice, PriceFetchResult
from investment_research.etf_profiles import PROJECT_ROOT

PROVIDER = "fmp"
MARKET = "us"
MARKET_TIMEZONE = "America/New_York"
INTERVAL = "1d"
ADJUSTMENT_METHOD = "unadjusted"
REQUEST_DELAY_SECONDS = 1.0
FMP_EOD_ENDPOINT = "https://financialmodelingprep.com/stable/historical-price-eod/full"


def fetch_us_etf_fmp_daily_prices(
    symbols: Iterable[str], data_root: Path, timeout_seconds: int = 20
) -> list[PriceFetchResult]:
    """读取 FMP EOD 价格；缺少密钥或单标的失败时明确返回错误结果。"""
    api_key = load_fmp_api_key()
    results: list[PriceFetchResult] = []
    for index, symbol in enumerate(symbol.upper() for symbol in symbols):
        if index:
            time.sleep(REQUEST_DELAY_SECONDS)
        results.append(_fetch_symbol(symbol, data_root, api_key, timeout_seconds))
    return results


def load_fmp_api_key() -> Optional[str]:
    """优先从环境变量读取 FMP Key，其次读取项目根目录被忽略的 .env。"""
    return os.environ.get("FMP_API_KEY") or _read_local_env_key()


def _fetch_symbol(
    symbol: str, data_root: Path, api_key: Optional[str], timeout_seconds: int
) -> PriceFetchResult:
    fetched_at = datetime.now(timezone.utc)
    source_url = _source_url(symbol)
    if not api_key:
        return _failure_result(
            symbol, data_root, fetched_at, source_url, "FMP_API_KEY 未配置；已跳过 FMP 交叉校验。"
        )

    raw_response = ""
    error: Optional[str] = None
    payload: Any = None
    try:
        request_url = f"{FMP_EOD_ENDPOINT}?{urlencode({'symbol': symbol, 'apikey': api_key})}"
        request = Request(request_url, headers={"User-Agent": "investment-research/0.1 research-only"})
        with urlopen(request, timeout=timeout_seconds) as response:
            raw_response = response.read().decode("utf-8")
        payload = json.loads(raw_response)
        if not isinstance(payload, list):
            error = "FMP 未返回日频列表。"
        elif not payload:
            error = "FMP 未返回日频记录。"
    except Exception as exception:
        status_code = getattr(exception, "code", None)
        error = (
            f"FMP 请求受当前套餐限制（HTTP {status_code}）。"
            if status_code == 402
            else f"FMP 获取失败：{type(exception).__name__}。"
        )

    cache_path, metadata_path = _write_cache(data_root, symbol, fetched_at, source_url, raw_response, error)
    if error is not None or not isinstance(payload, list):
        return PriceFetchResult(symbol, PROVIDER, None, cache_path, metadata_path, (), error or "FMP 返回不可用数据。", "error")

    try:
        price, warnings = _build_daily_price(symbol, payload, fetched_at, source_url)
    except ValueError as exception:
        return PriceFetchResult(symbol, PROVIDER, None, cache_path, metadata_path, (), f"FMP 解析失败：{exception}", "error")
    return PriceFetchResult(symbol, PROVIDER, price, cache_path, metadata_path, tuple(warnings))


def _failure_result(
    symbol: str, data_root: Path, fetched_at: datetime, source_url: str, error: str
) -> PriceFetchResult:
    cache_path, metadata_path = _write_cache(data_root, symbol, fetched_at, source_url, "", error)
    return PriceFetchResult(symbol, PROVIDER, None, cache_path, metadata_path, (), error, "error")


def _read_local_env_key() -> Optional[str]:
    env_path = PROJECT_ROOT / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        key, separator, value = line.partition("=")
        if key.strip() != "FMP_API_KEY" or not separator:
            continue
        candidate = value.strip().strip("'\"")
        return candidate or None
    return None


def _source_url(symbol: str) -> str:
    """可记录的脱敏端点；实际请求的 Key 不会写入缓存或报告。"""
    return f"{FMP_EOD_ENDPOINT}?{urlencode({'symbol': symbol})}"


def _write_cache(
    data_root: Path, symbol: str, fetched_at: datetime, source_url: str,
    raw_response: str, fetch_error: Optional[str],
) -> tuple[Path, Path]:
    cache_directory = data_root / "raw" / "prices" / PROVIDER / MARKET / symbol
    cache_directory.mkdir(parents=True, exist_ok=True)
    filename = fetched_at.strftime("%Y%m%dT%H%M%S%fZ")
    cache_path = cache_directory / f"{filename}.raw.json"
    metadata_path = cache_directory / f"{filename}.metadata.json"
    cache_path.write_text(raw_response, encoding="utf-8")
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "symbol": symbol,
                "market": MARKET,
                "provider": PROVIDER,
                "fetched_at": fetched_at.isoformat(),
                "timestamp_timezone": MARKET_TIMEZONE,
                "interval": INTERVAL,
                "adjustment_method": ADJUSTMENT_METHOD,
                "currency": "USD",
                "source_url": source_url,
                "raw_response_file": cache_path.name,
                "fetch_error": fetch_error,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return cache_path, metadata_path


def _build_daily_price(
    symbol: str, rows: list[dict[str, Any]], fetched_at: datetime, source_url: str
) -> tuple[DailyPrice, list[str]]:
    bars = _latest_complete_bars(rows, maximum=2)
    if not bars:
        raise ValueError("没有可用的完整 OHLCV 行。")
    latest = bars[0]
    previous = bars[1] if len(bars) > 1 else None
    warnings = [
        "FMP EOD 接口未返回独立币种字段；美股 ETF 交叉校验按 USD 记录，仍须人工核验。",
        "FMP 与 Yahoo 的收盘价仅在同交易日、同未复权口径下比较；不得混合用于回测。",
    ]
    if previous is None:
        warnings.append("缺少上一完整交易日，无法计算日涨跌与成交量变化。")
    latest_timestamp = latest["timestamp"]
    age_days = (fetched_at.astimezone(ZoneInfo(MARKET_TIMEZONE)).date() - latest_timestamp.date()).days
    if age_days > 4:
        warnings.append(f"FMP 最新交易日距抓取日已 {age_days} 天；请人工核验延迟与休市。")
    return (
        DailyPrice(
            symbol=symbol,
            market=MARKET,
            provider=PROVIDER,
            fetched_at=fetched_at.isoformat(),
            timestamp=latest_timestamp.isoformat(),
            timezone=MARKET_TIMEZONE,
            interval=INTERVAL,
            adjustment_method=ADJUSTMENT_METHOD,
            currency="USD",
            open=latest["open"],
            high=latest["high"],
            low=latest["low"],
            close=latest["close"],
            volume=latest["volume"],
            previous_close=previous["close"] if previous else None,
            previous_volume=previous["volume"] if previous else None,
            source_url=source_url,
        ),
        warnings,
    )


def _latest_complete_bars(rows: list[dict[str, Any]], maximum: int) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    for row in rows:
        try:
            candidate = {
                "timestamp": datetime.fromisoformat(str(row["date"])).replace(tzinfo=ZoneInfo(MARKET_TIMEZONE)),
                "open": _positive_float(row["open"], "open"),
                "high": _positive_float(row["high"], "high"),
                "low": _positive_float(row["low"], "low"),
                "close": _positive_float(row["close"], "close"),
                "volume": _nonnegative_int(row["volume"], "volume"),
            }
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if candidate["low"] > min(candidate["open"], candidate["close"]) or candidate["high"] < max(
            candidate["open"], candidate["close"]
        ):
            continue
        bars.append(candidate)
    bars.sort(key=lambda item: item["timestamp"], reverse=True)
    return bars[:maximum]


def _positive_float(value: Any, field_name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{field_name} 必须为正数")
    return number


def _nonnegative_int(value: Any, field_name: str) -> int:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field_name} 不可为负数")
    return int(number)
