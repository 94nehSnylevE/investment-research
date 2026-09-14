"""只读的美股 ETF 日频价格适配器与本地缓存回退。"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import yfinance as yf
from yfinance.exceptions import YFRateLimitError

PROVIDER = "yahoo_finance"
MARKET = "us"
MARKET_TIMEZONE = "America/New_York"
INTERVAL = "1d"
ADJUSTMENT_METHOD = "unadjusted"
REQUEST_DELAY_SECONDS = 1.5
MAX_ATTEMPTS = 2


@dataclass(frozen=True)
class DailyPrice:
    """按数据契约标准化的最新日频价格及其上一完整交易日。"""

    symbol: str
    market: str
    provider: str
    fetched_at: str
    timestamp: str
    timezone: str
    interval: str
    adjustment_method: str
    currency: str
    open: float
    high: float
    low: float
    close: float
    volume: Optional[int]
    previous_close: Optional[float]
    previous_volume: Optional[int]
    source_url: str

    @property
    def daily_change_pct(self) -> Optional[float]:
        if self.previous_close is None or self.previous_close <= 0:
            return None
        return (self.close / self.previous_close - 1) * 100

    @property
    def volume_change_pct(self) -> Optional[float]:
        if self.volume is None or self.previous_volume is None or self.previous_volume <= 0:
            return None
        return (self.volume / self.previous_volume - 1) * 100


@dataclass(frozen=True)
class PriceFetchResult:
    """单标的结果；实时失败时可指向最近一次成功缓存。"""

    symbol: str
    provider: str
    price: Optional[DailyPrice]
    cache_path: Path
    metadata_path: Path
    warnings: tuple[str, ...]
    error: Optional[str] = None
    data_status: str = "live"


def fetch_us_etf_daily_prices(
    symbols: Iterable[str], data_root: Path, timeout_seconds: int = 20
) -> list[PriceFetchResult]:
    """读取日频价格；限流后停止后续网络请求，改尝试本地成功缓存。"""
    results: list[PriceFetchResult] = []
    symbol_list = [symbol.upper() for symbol in symbols]
    for index, symbol in enumerate(symbol_list):
        if index:
            time.sleep(REQUEST_DELAY_SECONDS)
        result = _fetch_symbol(symbol, data_root, timeout_seconds)
        results.append(result)
        if _is_rate_limited(result):
            results.extend(_skipped_rate_limited_result(item, data_root) for item in symbol_list[index + 1 :])
            break
    return results


def _fetch_symbol(symbol: str, data_root: Path, timeout_seconds: int) -> PriceFetchResult:
    fetched_at = datetime.now(timezone.utc)
    source_url = _source_url(symbol)
    raw_response = ""
    error: Optional[str] = None
    history: Any = None
    metadata: dict[str, Any] = {}

    for attempt in range(MAX_ATTEMPTS):
        try:
            ticker = yf.Ticker(symbol)
            history = ticker.history(
                period="1mo", interval=INTERVAL, auto_adjust=False, actions=False,
                raise_errors=True, timeout=timeout_seconds,
            )
            metadata = ticker.get_history_metadata() or {}
            if history.empty:
                error = "Yahoo Finance 未返回日频记录。"
            break
        except YFRateLimitError:
            error = "Yahoo Finance 返回 429 限流。"
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep((attempt + 1) * REQUEST_DELAY_SECONDS)
        except Exception as exception:
            error = f"获取失败：{type(exception).__name__}。"
            break

    if history is not None and not history.empty:
        raw_response = _serialize_yahoo_history(symbol, history, metadata)
    request_cache_path, request_metadata_path = _write_cache(
        data_root, symbol, fetched_at, source_url, raw_response, error
    )
    if error or history is None or history.empty:
        return _fallback_or_failure(
            symbol, data_root, request_cache_path, request_metadata_path, error or "未返回可用数据。"
        )

    try:
        price, warnings = _latest_price_from_history(symbol, history, metadata, fetched_at, source_url)
    except ValueError as exception:
        return _fallback_or_failure(
            symbol, data_root, request_cache_path, request_metadata_path, f"解析失败：{exception}"
        )
    return PriceFetchResult(symbol, PROVIDER, price, request_cache_path, request_metadata_path, tuple(warnings))


def _skipped_rate_limited_result(symbol: str, data_root: Path) -> PriceFetchResult:
    fetched_at = datetime.now(timezone.utc)
    message = "Yahoo Finance 已触发限流；本次运行停止后续网络请求。"
    request_cache_path, request_metadata_path = _write_cache(
        data_root, symbol, fetched_at, _source_url(symbol), "", message
    )
    return _fallback_or_failure(symbol, data_root, request_cache_path, request_metadata_path, message)


def _fallback_or_failure(
    symbol: str,
    data_root: Path,
    request_cache_path: Path,
    request_metadata_path: Path,
    failure_reason: str,
) -> PriceFetchResult:
    fallback = _load_latest_success_cache(symbol, data_root)
    if fallback is None:
        return PriceFetchResult(
            symbol, PROVIDER, None, request_cache_path, request_metadata_path, (), failure_reason, "error"
        )

    price, cache_path, metadata_path, cache_warnings = fallback
    warnings = (
        f"实时拉取失败：{failure_reason}；已回退至缓存，原始抓取时间（UTC）：{price.fetched_at}。",
        *cache_warnings,
    )
    return PriceFetchResult(
        symbol, PROVIDER, price, cache_path, metadata_path, warnings, None, "cache"
    )


def _load_latest_success_cache(
    symbol: str, data_root: Path
) -> Optional[tuple[DailyPrice, Path, Path, tuple[str, ...]]]:
    """从最近一次完整成功缓存恢复价格；失败记录和损坏文件一律跳过。"""
    cache_directory = data_root / "raw" / "prices" / PROVIDER / MARKET / symbol
    for metadata_path in sorted(cache_directory.glob("*.metadata.json"), reverse=True):
        try:
            metadata_record = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata_record.get("fetch_error") is not None:
                continue
            cache_path = metadata_path.with_name(
                str(metadata_record["raw_response_file"])
            )
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if payload.get("provider") != PROVIDER or payload.get("symbol") != symbol:
                continue
            fetched_at = datetime.fromisoformat(str(metadata_record["fetched_at"]))
            price, warnings = _latest_price_from_cached_payload(
                symbol, payload, fetched_at, str(metadata_record["source_url"])
            )
            return price, cache_path, metadata_path, tuple(warnings)
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return None


def _is_rate_limited(result: PriceFetchResult) -> bool:
    return result.error is not None and "限流" in result.error or any(
        "限流" in warning for warning in result.warnings
    )


def _source_url(symbol: str) -> str:
    return f"https://finance.yahoo.com/quote/{symbol}/history"


def _serialize_yahoo_history(symbol: str, history: Any, metadata: dict[str, Any]) -> str:
    rows = []
    for timestamp, row in history.iterrows():
        rows.append(
            {
                "timestamp": timestamp.isoformat(),
                "open": _json_number(row.get("Open")),
                "high": _json_number(row.get("High")),
                "low": _json_number(row.get("Low")),
                "close": _json_number(row.get("Close")),
                "volume": _json_number(row.get("Volume")),
            }
        )
    return json.dumps(
        {
            "schema_version": 1,
            "provider": PROVIDER,
            "symbol": symbol,
            "response_format": "yfinance_history_normalized",
            "history": rows,
            "metadata": metadata,
        },
        ensure_ascii=False,
        default=str,
        indent=2,
    ) + "\n"


def _json_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _write_cache(
    data_root: Path, symbol: str, fetched_at: datetime, source_url: str,
    raw_response: str, fetch_error: Optional[str],
) -> tuple[Path, Path]:
    """保存成功或失败请求；失败记录不会成为缓存回退来源。"""
    cache_directory = data_root / "raw" / "prices" / PROVIDER / MARKET / symbol
    cache_directory.mkdir(parents=True, exist_ok=True)
    filename = fetched_at.strftime("%Y%m%dT%H%M%S%fZ")
    cache_path = cache_directory / f"{filename}.raw.json"
    metadata_path = cache_directory / f"{filename}.metadata.json"
    cache_path.write_text(raw_response, encoding="utf-8")
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": 1, "symbol": symbol, "market": MARKET, "provider": PROVIDER,
                "fetched_at": fetched_at.isoformat(), "timestamp_timezone": MARKET_TIMEZONE,
                "interval": INTERVAL, "adjustment_method": ADJUSTMENT_METHOD,
                "source_url": source_url, "raw_response_file": cache_path.name,
                "fetch_error": fetch_error,
            },
            ensure_ascii=False, indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return cache_path, metadata_path


def _latest_price_from_history(
    symbol: str, history: Any, metadata: dict[str, Any], fetched_at: datetime, source_url: str
) -> tuple[DailyPrice, list[str]]:
    rows = []
    for timestamp, row in history.iterrows():
        rows.append(
            {
                "timestamp": timestamp.to_pydatetime(), "open": row.get("Open"),
                "high": row.get("High"), "low": row.get("Low"), "close": row.get("Close"),
                "volume": row.get("Volume"),
            }
        )
    return _build_daily_price(symbol, rows, metadata.get("currency"), fetched_at, source_url)


def _latest_price_from_cached_payload(
    symbol: str, payload: dict[str, Any], fetched_at: datetime, source_url: str
) -> tuple[DailyPrice, list[str]]:
    history = payload.get("history")
    metadata = payload.get("metadata")
    if not isinstance(history, list) or not isinstance(metadata, dict):
        raise ValueError("缓存缺少日频数据或元数据。")
    return _build_daily_price(symbol, history, metadata.get("currency"), fetched_at, source_url)


def _build_daily_price(
    symbol: str, rows: list[dict[str, Any]], currency: Any, fetched_at: datetime, source_url: str
) -> tuple[DailyPrice, list[str]]:
    if not isinstance(currency, str) or not currency:
        raise ValueError("供应商未提供币种。")
    bars = _latest_complete_bars(rows, maximum=2)
    if not bars:
        raise ValueError("没有可用的完整 OHLCV 行。")
    latest = bars[0]
    previous = bars[1] if len(bars) > 1 else None
    timestamp = _market_timestamp(latest["timestamp"])
    warnings = ["数据经 yfinance 标准化保存；使用或回测前须人工核验 Yahoo 复权口径。"]
    if previous is None:
        warnings.append("缺少上一完整交易日，无法计算日涨跌与成交量变化。")
    age_days = (fetched_at.astimezone(ZoneInfo(MARKET_TIMEZONE)).date() - timestamp.date()).days
    if age_days > 4:
        warnings.append(f"最新交易日距抓取日已 {age_days} 天；请人工核验交易日和供应商延迟。")
    return (
        DailyPrice(
            symbol=symbol, market=MARKET, provider=PROVIDER, fetched_at=fetched_at.isoformat(),
            timestamp=timestamp.isoformat(), timezone=MARKET_TIMEZONE, interval=INTERVAL,
            adjustment_method=ADJUSTMENT_METHOD, currency=currency, open=latest["open"],
            high=latest["high"], low=latest["low"], close=latest["close"], volume=latest["volume"],
            previous_close=previous["close"] if previous else None,
            previous_volume=previous["volume"] if previous else None, source_url=source_url,
        ),
        warnings,
    )


def _latest_complete_bars(rows: list[dict[str, Any]], maximum: int) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    for row in reversed(rows):
        try:
            candidate = {
                "timestamp": _parse_timestamp(row["timestamp"]),
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
        if len(bars) == maximum:
            break
    return bars


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _market_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=ZoneInfo(MARKET_TIMEZONE))
    return value.astimezone(ZoneInfo(MARKET_TIMEZONE))


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
