"""近实时行情只读监控。

通过 Yahoo 流式端点接收报价，只用于观察与延迟审计。数据为**单一交易所**报价（例如 Nasdaq
或 NYSE Arca），不是全市场合并最优价（NBBO），且来源为非官方授权端点；因此不得用于成交判断、
自动交易或对外分发。本模块与日频价格库、回测数据集完全独立，口径不可混用。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from investment_research.etf_profiles import PROJECT_ROOT

PROVIDER = "yahoo_finance_stream"
VENUE_SCOPE = "single_venue_not_nbbo"
LICENSE_STATUS = "unlicensed_research_only"
DEFAULT_MONITOR_DB = PROJECT_ROOT / "data" / "processed" / "realtime" / "intraday-quote-monitor.sqlite3"
STREAM_URL = "wss://streamer.finance.yahoo.com/?version=2"
STALE_THRESHOLD_SECONDS = 60.0
CLOCK_SKEW_TOLERANCE_SECONDS = 1.0


@dataclass(frozen=True)
class QuoteTick:
    symbol: str
    exchange: Optional[str]
    market_state: Optional[str]
    price: Optional[float]
    provider_timestamp_utc: Optional[str]
    received_at_utc: str
    latency_seconds: Optional[float]
    freshness: str


@dataclass(frozen=True)
class MonitorSession:
    session_id: str
    started_at_utc: str
    ended_at_utc: str
    requested_symbols: tuple[str, ...]
    tick_count: int
    connection_status: str
    error: Optional[str]
    ticks: tuple[QuoteTick, ...]


def monitor_realtime_quotes(
    symbols: Iterable[str],
    duration_seconds: int = 60,
    database_path: Path = DEFAULT_MONITOR_DB,
) -> MonitorSession:
    """在限定时长内接收流式报价并写入只读审计库；不产生交易信号。"""
    symbol_list = tuple(sorted({symbol.upper() for symbol in symbols}))
    if not symbol_list:
        raise ValueError("至少需要一个标的。")
    if not 1 <= duration_seconds <= 3600:
        raise ValueError("duration_seconds 必须在 1 至 3600 之间。")

    import yfinance as yf

    started_at = datetime.now(timezone.utc)
    ticks: list[QuoteTick] = []
    error: Optional[str] = None
    client: Any = None

    def handle(message: dict[str, Any]) -> None:
        tick = _normalize_message(message)
        if tick is not None:
            ticks.append(tick)

    try:
        client = yf.WebSocket(url=STREAM_URL, verbose=False)
        client.subscribe(list(symbol_list))
        timer = threading.Timer(duration_seconds, lambda: _safe_close(client))
        timer.daemon = True
        timer.start()
        try:
            client.listen(handle)
        finally:
            timer.cancel()
        connection_status = "closed_after_duration"
    except Exception as exception:
        error = f"流式连接失败：{type(exception).__name__}。"
        connection_status = "failed"
    finally:
        _safe_close(client)

    ended_at = datetime.now(timezone.utc)
    session_id = started_at.strftime("%Y%m%dT%H%M%S%fZ")
    if not ticks and error is None:
        connection_status = "connected_no_ticks"
    session = MonitorSession(
        session_id=session_id,
        started_at_utc=started_at.isoformat(),
        ended_at_utc=ended_at.isoformat(),
        requested_symbols=symbol_list,
        tick_count=len(ticks),
        connection_status=connection_status,
        error=error,
        ticks=tuple(ticks),
    )
    _record_session(session, database_path)
    return session


def latency_summary(
    database_path: Path = DEFAULT_MONITOR_DB, limit: int = 10
) -> list[dict[str, Any]]:
    """只读汇总最近监控会话的报价数量与延迟分布。"""
    if limit < 1:
        raise ValueError("limit 必须大于 0。")
    if not database_path.exists():
        return []
    connection = sqlite3.connect(f"file:{database_path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """SELECT s.session_id, s.started_at_utc, s.connection_status, s.tick_count,
                      COUNT(t.rowid) AS recorded_ticks,
                      AVG(t.latency_seconds) AS mean_latency_seconds,
                      MAX(t.latency_seconds) AS max_latency_seconds,
                      SUM(CASE WHEN t.freshness = 'stale' THEN 1 ELSE 0 END) AS stale_ticks,
                      SUM(CASE WHEN t.freshness = 'clock_skew_suspected' THEN 1 ELSE 0 END) AS clock_skew_ticks
               FROM monitor_sessions s LEFT JOIN quote_ticks t ON t.session_id = s.session_id
               GROUP BY s.session_id ORDER BY s.started_at_utc DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    return [dict(row) for row in rows]


def _normalize_message(message: dict[str, Any]) -> Optional[QuoteTick]:
    symbol = message.get("id") or message.get("symbol")
    if not symbol:
        return None
    received_at = datetime.now(timezone.utc)
    price = _optional_float(message.get("price"))
    provider_timestamp = _provider_timestamp(message.get("time"))
    latency: Optional[float] = None
    freshness = "unknown_provider_timestamp"
    if provider_timestamp is not None:
        latency = (received_at - provider_timestamp).total_seconds()
        if latency < -CLOCK_SKEW_TOLERANCE_SECONDS:
            freshness = "clock_skew_suspected"
        elif latency > STALE_THRESHOLD_SECONDS:
            freshness = "stale"
        else:
            freshness = "fresh"
    return QuoteTick(
        symbol=str(symbol).upper(),
        exchange=message.get("exchange"),
        market_state=message.get("marketHours") or message.get("market_hours"),
        price=price,
        provider_timestamp_utc=provider_timestamp.isoformat() if provider_timestamp else None,
        received_at_utc=received_at.isoformat(),
        latency_seconds=latency,
        freshness=freshness,
    )


def _provider_timestamp(value: Any) -> Optional[datetime]:
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return None
    seconds = raw / 1000 if raw > 10_000_000_000 else raw
    try:
        return datetime.fromtimestamp(seconds, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _optional_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _safe_close(client: Any) -> None:
    if client is None:
        return
    try:
        client.close()
    except Exception:
        return


def _record_session(session: MonitorSession, database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS monitor_sessions (
                session_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                venue_scope TEXT NOT NULL,
                license_status TEXT NOT NULL,
                requested_symbols TEXT NOT NULL,
                started_at_utc TEXT NOT NULL,
                ended_at_utc TEXT NOT NULL,
                tick_count INTEGER NOT NULL,
                connection_status TEXT NOT NULL,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS quote_ticks (
                session_id TEXT NOT NULL REFERENCES monitor_sessions(session_id) ON DELETE RESTRICT,
                symbol TEXT NOT NULL,
                exchange TEXT,
                market_state TEXT,
                price REAL,
                provider_timestamp_utc TEXT,
                received_at_utc TEXT NOT NULL,
                latency_seconds REAL,
                freshness TEXT NOT NULL CHECK(freshness IN ('fresh', 'stale', 'clock_skew_suspected', 'unknown_provider_timestamp'))
            );
            CREATE INDEX IF NOT EXISTS idx_quote_ticks_symbol
                ON quote_ticks(symbol, received_at_utc DESC);
            CREATE TRIGGER IF NOT EXISTS monitor_sessions_no_update BEFORE UPDATE ON monitor_sessions
                BEGIN SELECT RAISE(ABORT, 'monitor sessions are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS quote_ticks_no_update BEFORE UPDATE ON quote_ticks
                BEGIN SELECT RAISE(ABORT, 'quote ticks are append-only'); END;
            """
        )
        connection.execute(
            """INSERT OR IGNORE INTO monitor_sessions (
                session_id, provider, venue_scope, license_status, requested_symbols,
                started_at_utc, ended_at_utc, tick_count, connection_status, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session.session_id, PROVIDER, VENUE_SCOPE, LICENSE_STATUS,
                json.dumps(list(session.requested_symbols)), session.started_at_utc,
                session.ended_at_utc, session.tick_count, session.connection_status, session.error,
            ),
        )
        connection.executemany(
            """INSERT INTO quote_ticks (
                session_id, symbol, exchange, market_state, price,
                provider_timestamp_utc, received_at_utc, latency_seconds, freshness
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    session.session_id, tick.symbol, tick.exchange, tick.market_state, tick.price,
                    tick.provider_timestamp_utc, tick.received_at_utc, tick.latency_seconds, tick.freshness,
                )
                for tick in session.ticks
            ],
        )
