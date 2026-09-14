"""官方美国宏观发布日期候选采集。

所有结果均为 pending_review 宏观日历候选，与 ETF profile/候选库严格隔离。
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.request import Request, urlopen

from investment_research.etf_profiles import PROJECT_ROOT

DEFAULT_MACRO_CONFIG = PROJECT_ROOT / "config" / "macro-calendar.json"
DEFAULT_MACRO_HISTORY_DB = PROJECT_ROOT / "data" / "processed" / "macro" / "macro-candidate-history.sqlite3"
MONTHS = {name: index for index, name in enumerate(("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), start=1)}


@dataclass(frozen=True)
class MacroCalendarEvent:
    event_key: str
    display_name: str
    scheduled_date: str
    reference_period: Optional[str]
    source_url: str
    raw_path: Path
    status: str = "pending_review"


@dataclass(frozen=True)
class MacroFetchResult:
    event_key: str
    display_name: str
    publisher: str
    events: tuple[MacroCalendarEvent, ...]
    cache_path: Path
    metadata_path: Path
    warnings: tuple[str, ...]
    error: Optional[str] = None


def fetch_official_macro_calendar(
    data_root: Path, config_path: Path = DEFAULT_MACRO_CONFIG, timeout_seconds: int = 20
) -> list[MacroFetchResult]:
    """抓取官方日历页面、保存证据并返回待审核发布日期候选。"""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("schema_version") != 1 or not isinstance(config.get("events"), list):
        raise ValueError("宏观日历配置无效。")
    results: list[MacroFetchResult] = []
    for definition in config["events"]:
        results.append(_fetch_event(definition, data_root, timeout_seconds))
    return results


def _fetch_event(definition: dict[str, Any], data_root: Path, timeout_seconds: int) -> MacroFetchResult:
    event_key = _required_text(definition, "event_key")
    display_name = _required_text(definition, "display_name")
    publisher = _required_text(definition, "publisher")
    source_url = _required_text(definition, "source_url")
    parser = _required_text(definition, "parser")
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0)
    raw_text = ""
    error: Optional[str] = None
    try:
        request = Request(source_url, headers={"User-Agent": "investment-research/0.1 research-only"})
        with urlopen(request, timeout=timeout_seconds) as response:
            raw_text = response.read().decode("utf-8")
    except Exception as exception:
        error = f"官方来源获取失败：{type(exception).__name__}。"

    cache_path, metadata_path = _write_raw_cache(data_root, event_key, publisher, source_url, fetched_at, raw_text, error)
    if error:
        return MacroFetchResult(event_key, display_name, publisher, (), cache_path, metadata_path, (), error)

    try:
        scheduled = _parse_calendar(parser, raw_text, fetched_at.date())
    except ValueError as exception:
        return MacroFetchResult(event_key, display_name, publisher, (), cache_path, metadata_path, (), f"日历解析失败：{exception}")
    events = tuple(
        MacroCalendarEvent(event_key, display_name, item["scheduled_date"], item.get("reference_period"), source_url, cache_path)
        for item in scheduled
    )
    warnings: tuple[str, ...] = ()
    if not events:
        warnings = ("官方页面已缓存，但当前解析器未取得发布日期；请人工查看原始证据。",)
    else:
        candidate_path = _write_candidates(data_root, event_key, fetched_at, publisher, source_url, cache_path, metadata_path, events)
        _record_snapshot(event_key, publisher, source_url, fetched_at, cache_path, metadata_path, candidate_path, events)
    return MacroFetchResult(event_key, display_name, publisher, events, cache_path, metadata_path, warnings)


def _parse_calendar(parser: str, raw_text: str, today: date) -> list[dict[str, Optional[str]]]:
    plain = _plain_text(raw_text)
    if parser == "bea_pce":
        return _parse_bea_pce(plain, today.year)
    if parser == "fomc":
        return _parse_fomc(plain, today.year)
    if parser == "h15":
        return []
    if parser in {"bls_cpi", "bls_employment"}:
        return _parse_bls_dates(plain, today.year)
    raise ValueError(f"未知解析器：{parser}")


def _parse_bea_pce(text: str, year: int) -> list[dict[str, Optional[str]]]:
    pattern = re.compile(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})\s+\d{1,2}:\d{2}\s+(?:AM|PM)\s+N\s*ews\s+Personal Income and Outlays,\s+([A-Za-z]+\s+\d{4})"
    )
    return [
        {"scheduled_date": _date_string(month, day, year), "reference_period": reference}
        for month, day, reference in pattern.findall(text)
    ]


def _parse_fomc(text: str, year: int) -> list[dict[str, Optional[str]]]:
    section = re.search(rf"{year} FOMC Meetings(.*?)(?:{year - 1} FOMC Meetings|$)", text)
    if section is None:
        return []
    pattern = re.compile(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})-(\d{1,2})\*?"
    )
    return [
        {"scheduled_date": _date_string(month, end_day, year), "reference_period": f"{month} {start_day}-{end_day}, {year}"}
        for month, start_day, end_day in pattern.findall(section.group(1))
    ]


def _parse_bls_dates(text: str, year: int) -> list[dict[str, Optional[str]]]:
    pattern = re.compile(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(\d{4})")
    return [
        {"scheduled_date": _date_string(month, day, int(found_year)), "reference_period": None}
        for month, day, found_year in pattern.findall(text)
        if int(found_year) == year
    ]


def _write_raw_cache(
    data_root: Path, event_key: str, publisher: str, source_url: str, fetched_at: datetime, raw_text: str, fetch_error: Optional[str]
) -> tuple[Path, Path]:
    directory = data_root / "raw" / "macro" / event_key
    directory.mkdir(parents=True, exist_ok=True)
    stem = fetched_at.strftime("%Y%m%dT%H%M%SZ")
    cache_path = directory / f"{stem}.html"
    metadata_path = directory / f"{stem}.metadata.json"
    cache_path.write_text(raw_text, encoding="utf-8")
    metadata_path.write_text(json.dumps({"schema_version": 1, "event_key": event_key, "publisher": publisher, "source_url": source_url, "fetched_at": fetched_at.isoformat(), "timezone": "America/New_York", "raw_file": cache_path.name, "fetch_error": fetch_error, "status": "pending_review"}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return cache_path, metadata_path


def _write_candidates(
    data_root: Path, event_key: str, fetched_at: datetime, publisher: str, source_url: str, cache_path: Path, metadata_path: Path, events: tuple[MacroCalendarEvent, ...]
) -> Path:
    directory = data_root / "processed" / "macro" / "candidates" / "us" / event_key
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{fetched_at.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps({"schema_version": 1, "event_key": event_key, "publisher": publisher, "source_url": source_url, "retrieved_at": fetched_at.isoformat(), "status": "pending_review", "raw_path": str(cache_path.relative_to(PROJECT_ROOT)), "metadata_path": str(metadata_path.relative_to(PROJECT_ROOT)), "events": [{"scheduled_date": event.scheduled_date, "reference_period": event.reference_period, "status": event.status} for event in events]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _record_snapshot(
    event_key: str, publisher: str, source_url: str, fetched_at: datetime, cache_path: Path, metadata_path: Path, candidate_path: Path, events: tuple[MacroCalendarEvent, ...]
) -> None:
    DEFAULT_MACRO_HISTORY_DB.parent.mkdir(parents=True, exist_ok=True)
    raw_hash = _sha256(cache_path)
    candidate_hash = _sha256(candidate_path)
    snapshot_id = hashlib.sha256(f"{event_key}|{source_url}|{fetched_at.isoformat()}|{raw_hash}".encode()).hexdigest()
    with sqlite3.connect(DEFAULT_MACRO_HISTORY_DB) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS macro_candidate_snapshots (snapshot_id TEXT PRIMARY KEY, event_key TEXT NOT NULL, publisher TEXT NOT NULL, source_url TEXT NOT NULL, retrieved_at TEXT NOT NULL, raw_path TEXT NOT NULL, metadata_path TEXT NOT NULL, candidate_path TEXT NOT NULL, raw_sha256 TEXT NOT NULL, candidate_sha256 TEXT NOT NULL, status TEXT NOT NULL CHECK(status = 'pending_review'));
            CREATE TABLE IF NOT EXISTS macro_calendar_events (snapshot_id TEXT NOT NULL REFERENCES macro_candidate_snapshots(snapshot_id), scheduled_date TEXT NOT NULL, reference_period TEXT, status TEXT NOT NULL CHECK(status = 'pending_review'), PRIMARY KEY(snapshot_id, scheduled_date));
            CREATE INDEX IF NOT EXISTS idx_macro_events_date ON macro_calendar_events(scheduled_date);
        """)
        connection.execute("INSERT OR IGNORE INTO macro_candidate_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_review')", (snapshot_id, event_key, publisher, source_url, fetched_at.isoformat(), _relative(cache_path), _relative(metadata_path), _relative(candidate_path), raw_hash, candidate_hash))
        connection.executemany("INSERT OR IGNORE INTO macro_calendar_events VALUES (?, ?, ?, 'pending_review')", [(snapshot_id, event.scheduled_date, event.reference_period) for event in events])


def _plain_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value)))


def _date_string(month: str, day: str, year: int) -> str:
    return date(year, MONTHS[month], int(day)).isoformat()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative(path: Path) -> str:
    return str(path.relative_to(PROJECT_ROOT))


def _required_text(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"宏观日历配置缺少 {field_name}。")
    return value
