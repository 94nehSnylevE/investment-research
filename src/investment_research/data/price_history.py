"""日频价格的本地 SQLite 审计索引。

原始 JSON/metadata 仍是证据原件；本库保存 Yahoo/FMP 的可查询快照与最新 OHLCV 投影。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from investment_research.data.daily_prices import PriceFetchResult
from investment_research.etf_profiles import PROJECT_ROOT

DEFAULT_DAILY_PRICE_HISTORY_DB = PROJECT_ROOT / "data" / "processed" / "prices" / "daily-price-history.sqlite3"


def record_price_fetches(
    results: Iterable[PriceFetchResult], database_path: Path = DEFAULT_DAILY_PRICE_HISTORY_DB
) -> list[str]:
    """记录本次 Yahoo/FMP 结果；失败和缓存降级同样保留审计快照。"""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    fetch_ids: list[str] = []
    with _connect(database_path) as connection:
        _initialize(connection)
        for result in results:
            fetch_ids.append(_record_result(connection, result))
    return fetch_ids


def get_latest_daily_price(
    symbol: str, provider: str, database_path: Path = DEFAULT_DAILY_PRICE_HISTORY_DB
) -> Optional[dict[str, Any]]:
    """读取一个来源/标的最新交易日的可用价格投影。"""
    if not database_path.exists():
        return None
    with _connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT observation.symbol, observation.provider, observation.trade_date, observation.market_timestamp,
                   observation.timezone, observation.interval, observation.adjustment_method, observation.currency,
                   observation.open, observation.high, observation.low, observation.close, observation.volume,
                   observation.previous_close, observation.previous_volume, fetch.data_status, fetch.attempted_at,
                   fetch.raw_path, fetch.metadata_path, fetch.raw_sha256, fetch.message
            FROM daily_price_observations AS observation
            JOIN price_fetches AS fetch ON fetch.fetch_id = observation.fetch_id
            WHERE observation.symbol = ? AND observation.provider = ?
            ORDER BY observation.trade_date DESC, fetch.attempted_at DESC
            LIMIT 1
            """,
            (symbol.upper(), provider),
        ).fetchone()
    return dict(row) if row else None


def list_price_fetches(
    symbol: str, provider: Optional[str] = None, limit: int = 20,
    database_path: Path = DEFAULT_DAILY_PRICE_HISTORY_DB,
) -> list[dict[str, Any]]:
    """按尝试时间倒序读取单标的的价格抓取审计历史。"""
    if limit < 1:
        raise ValueError("limit 必须大于 0。")
    if not database_path.exists():
        return []
    query = "SELECT * FROM price_fetches WHERE symbol = ?"
    parameters: list[Any] = [symbol.upper()]
    if provider:
        query += " AND provider = ?"
        parameters.append(provider)
    query += " ORDER BY attempted_at DESC LIMIT ?"
    parameters.append(limit)
    with _connect(database_path) as connection:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]


def _record_result(connection: sqlite3.Connection, result: PriceFetchResult) -> str:
    raw_path = _project_file(result.cache_path)
    metadata_path = _project_file(result.metadata_path)
    raw_sha256 = _sha256(raw_path)
    attempted_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    source_url = result.price.source_url if result.price else _metadata_source_url(metadata_path)
    identity = {
        "symbol": result.symbol.upper(),
        "provider": result.provider,
        "attempted_at": attempted_at,
        "raw_sha256": raw_sha256,
        "data_status": result.data_status,
    }
    fetch_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
    message = result.error or " | ".join(result.warnings) or None
    connection.execute(
        """
        INSERT OR IGNORE INTO price_fetches (
            fetch_id, symbol, provider, source_url, attempted_at, data_status, raw_path, metadata_path,
            raw_sha256, message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fetch_id, result.symbol.upper(), result.provider, source_url, attempted_at, result.data_status,
            _relative(raw_path), _relative(metadata_path), raw_sha256, message,
        ),
    )
    if result.price is not None:
        price = result.price
        connection.execute(
            """
            INSERT OR IGNORE INTO daily_price_observations (
                fetch_id, symbol, provider, trade_date, market_timestamp, timezone, interval,
                adjustment_method, currency, open, high, low, close, volume, previous_close, previous_volume
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fetch_id, price.symbol.upper(), price.provider, price.timestamp[:10], price.timestamp,
                price.timezone, price.interval, price.adjustment_method, price.currency, price.open,
                price.high, price.low, price.close, price.volume, price.previous_close, price.previous_volume,
            ),
        )
    return fetch_id


def _initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS price_fetches (
            fetch_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            provider TEXT NOT NULL,
            source_url TEXT NOT NULL,
            attempted_at TEXT NOT NULL,
            data_status TEXT NOT NULL CHECK(data_status IN ('live', 'cache', 'error')),
            raw_path TEXT NOT NULL,
            metadata_path TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            message TEXT
        );
        CREATE TABLE IF NOT EXISTS daily_price_observations (
            fetch_id TEXT PRIMARY KEY REFERENCES price_fetches(fetch_id) ON DELETE RESTRICT,
            symbol TEXT NOT NULL,
            provider TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            market_timestamp TEXT NOT NULL,
            timezone TEXT NOT NULL,
            interval TEXT NOT NULL CHECK(interval = '1d'),
            adjustment_method TEXT NOT NULL,
            currency TEXT NOT NULL,
            open REAL NOT NULL CHECK(open > 0),
            high REAL NOT NULL CHECK(high > 0),
            low REAL NOT NULL CHECK(low > 0),
            close REAL NOT NULL CHECK(close > 0),
            volume INTEGER CHECK(volume IS NULL OR volume >= 0),
            previous_close REAL,
            previous_volume INTEGER,
            CHECK(low <= open AND low <= close AND high >= open AND high >= close)
        );
        CREATE INDEX IF NOT EXISTS idx_price_fetches_symbol_provider_attempted
            ON price_fetches(symbol, provider, attempted_at DESC);
        CREATE INDEX IF NOT EXISTS idx_price_fetches_status_attempted
            ON price_fetches(data_status, attempted_at DESC);
        CREATE INDEX IF NOT EXISTS idx_daily_price_symbol_provider_date
            ON daily_price_observations(symbol, provider, trade_date DESC);
        """
    )


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _metadata_source_url(metadata_path: Path) -> str:
    try:
        source_url = json.loads(metadata_path.read_text(encoding="utf-8")).get("source_url")
    except (OSError, json.JSONDecodeError):
        source_url = None
    return str(source_url) if source_url else "unknown"


def _project_file(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_file() or not resolved.is_relative_to(PROJECT_ROOT.resolve()):
        raise ValueError("价格证据文件必须位于项目目录内。")
    return resolved


def _relative(path: Path) -> str:
    return str(path.relative_to(PROJECT_ROOT.resolve()))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
