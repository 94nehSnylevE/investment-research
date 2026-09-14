"""FRED 宏观指标的只读、可缓存适配器。

通过 macOS curl 的 HTTP/1.1 模式读取 FRED CSV，避免当前代理与 urllib 的连接兼容问题。
所有值均为 pending_review 候选，不替代 BLS 原始发布文件。
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Optional

from investment_research.etf_profiles import PROJECT_ROOT

PROVIDER = "fred"
DEFAULT_HISTORY_DB = PROJECT_ROOT / "data" / "processed" / "macro" / "fred-indicator-history.sqlite3"
REQUEST_DELAY_SECONDS = 0.5
SERIES = (
    ("cpi", "CPI（季调）", "CPIAUCSL", "Index 1982-1984=100, seasonally adjusted", "U.S. Bureau of Labor Statistics"),
    ("core_cpi", "核心 CPI（季调）", "CPILFESL", "Index 1982-1984=100, seasonally adjusted", "U.S. Bureau of Labor Statistics"),
    ("nonfarm_payrolls", "非农就业人数（季调）", "PAYEMS", "Thousands of persons, seasonally adjusted", "U.S. Bureau of Labor Statistics"),
    ("unemployment_rate", "失业率", "UNRATE", "Percent, seasonally adjusted", "U.S. Bureau of Labor Statistics"),
)


@dataclass(frozen=True)
class FredIndicatorResult:
    indicator_key: str
    display_name: str
    series_id: str
    unit: str
    original_publisher: str
    observation_date: Optional[str]
    value: Optional[float]
    cache_path: Path
    metadata_path: Path
    error: Optional[str] = None


def fetch_fred_indicators(data_root: Path, timeout_seconds: int = 30) -> list[FredIndicatorResult]:
    """取得四个 BLS 来源的 FRED 最新观测，并缓存原始 CSV/审计记录。"""
    results: list[FredIndicatorResult] = []
    for index, definition in enumerate(SERIES):
        if index:
            time.sleep(REQUEST_DELAY_SECONDS)
        results.append(_fetch_indicator(data_root, *definition, timeout_seconds))
    return results


def _fetch_indicator(
    data_root: Path, indicator_key: str, display_name: str, series_id: str, unit: str,
    original_publisher: str, timeout_seconds: int,
) -> FredIndicatorResult:
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0)
    source_url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    raw_csv = ""
    error: Optional[str] = None
    try:
        response = subprocess.run(
            ["curl", "--http1.1", "--fail", "--silent", "--show-error", "--max-time", str(timeout_seconds), "--retry", "2", "--retry-all-errors", source_url],
            check=False, capture_output=True, text=True, timeout=timeout_seconds * 3,
        )
        if response.returncode:
            error = f"FRED 获取失败：curl exit {response.returncode}。"
        else:
            raw_csv = response.stdout
    except (OSError, subprocess.TimeoutExpired):
        error = "FRED 获取失败：本机 curl 不可用或超时。"

    cache_path, metadata_path = _write_cache(data_root, indicator_key, series_id, source_url, fetched_at, raw_csv, error)
    if error:
        return FredIndicatorResult(indicator_key, display_name, series_id, unit, original_publisher, None, None, cache_path, metadata_path, error)
    try:
        observation_date, value = _latest_observation(raw_csv, series_id)
    except ValueError as exception:
        return FredIndicatorResult(indicator_key, display_name, series_id, unit, original_publisher, None, None, cache_path, metadata_path, f"FRED 解析失败：{exception}")

    _record_snapshot(indicator_key, series_id, source_url, fetched_at, observation_date, value, cache_path, metadata_path)
    return FredIndicatorResult(indicator_key, display_name, series_id, unit, original_publisher, observation_date, value, cache_path, metadata_path)


def _latest_observation(raw_csv: str, series_id: str) -> tuple[str, float]:
    rows = list(csv.DictReader(StringIO(raw_csv)))
    if not rows or "observation_date" not in rows[0] or series_id not in rows[0]:
        raise ValueError("CSV 缺少预期列。")
    for row in reversed(rows):
        value = row.get(series_id, "").strip()
        if value and value != ".":
            return row["observation_date"], float(value)
    raise ValueError("CSV 没有有效观测。")


def _write_cache(
    data_root: Path, indicator_key: str, series_id: str, source_url: str, fetched_at: datetime,
    raw_csv: str, fetch_error: Optional[str],
) -> tuple[Path, Path]:
    directory = data_root / "raw" / "macro" / "fred" / indicator_key
    directory.mkdir(parents=True, exist_ok=True)
    stem = fetched_at.strftime("%Y%m%dT%H%M%SZ")
    cache_path = directory / f"{stem}.csv"
    metadata_path = directory / f"{stem}.metadata.json"
    cache_path.write_text(raw_csv, encoding="utf-8")
    metadata_path.write_text(json.dumps({"schema_version": 1, "provider": PROVIDER, "original_publisher": "U.S. Bureau of Labor Statistics", "indicator_key": indicator_key, "series_id": series_id, "source_url": source_url, "fetched_at": fetched_at.isoformat(), "raw_file": cache_path.name, "fetch_error": fetch_error, "status": "pending_review"}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return cache_path, metadata_path


def _record_snapshot(
    indicator_key: str, series_id: str, source_url: str, fetched_at: datetime, observation_date: str,
    value: float, cache_path: Path, metadata_path: Path,
) -> None:
    DEFAULT_HISTORY_DB.parent.mkdir(parents=True, exist_ok=True)
    raw_hash = hashlib.sha256(cache_path.read_bytes()).hexdigest()
    snapshot_id = hashlib.sha256(f"{series_id}|{observation_date}|{raw_hash}".encode()).hexdigest()
    with sqlite3.connect(DEFAULT_HISTORY_DB) as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS fred_indicator_snapshots (snapshot_id TEXT PRIMARY KEY, indicator_key TEXT NOT NULL, series_id TEXT NOT NULL, source_url TEXT NOT NULL, fetched_at TEXT NOT NULL, observation_date TEXT NOT NULL, value REAL NOT NULL, raw_path TEXT NOT NULL, metadata_path TEXT NOT NULL, raw_sha256 TEXT NOT NULL, status TEXT NOT NULL CHECK(status = 'pending_review'))""")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_fred_indicator_date ON fred_indicator_snapshots(indicator_key, observation_date DESC)")
        connection.execute("INSERT OR IGNORE INTO fred_indicator_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_review')", (snapshot_id, indicator_key, series_id, source_url, fetched_at.isoformat(), observation_date, value, str(cache_path.relative_to(PROJECT_ROOT)), str(metadata_path.relative_to(PROJECT_ROOT)), raw_hash))
