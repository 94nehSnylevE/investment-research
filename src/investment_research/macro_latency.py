"""宏观发布时效优化：基于官方发布公告触发同步并度量延迟。

两类滞后必须区分：
- 参考期滞后：例如 8 月 CPI 在 9 月中旬才发布，由官方统计口径决定，任何数据源都无法消除。
- 获取滞后：官方发布后本机多久拿到数据。这一项可以优化，本模块只优化这一项。

做法是把官方新闻发布源当作「已发布」信号，检测到相关公告后再触发 FRED 同步，避免盲目轮询。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from investment_research.etf_profiles import PROJECT_ROOT
from investment_research.news_feeds import DEFAULT_NEWS_DB

DEFAULT_TRIGGER_CONFIG = PROJECT_ROOT / "config" / "macro-release-triggers.json"
DEFAULT_LATENCY_DB = PROJECT_ROOT / "data" / "processed" / "macro" / "macro-latency-history.sqlite3"
RECENT_WINDOW_HOURS = 36


@dataclass(frozen=True)
class ReleaseSignal:
    trigger_key: str
    display_name: str
    matched_title: str
    publisher: str
    link: str
    published_at_utc: Optional[str]
    detected_at_utc: str
    detection_latency_seconds: Optional[float]


def detect_release_signals(
    config_path: Path = DEFAULT_TRIGGER_CONFIG,
    news_database_path: Path = DEFAULT_NEWS_DB,
    latency_database_path: Path = DEFAULT_LATENCY_DB,
    window_hours: int = RECENT_WINDOW_HOURS,
) -> list[ReleaseSignal]:
    """从官方新闻条目中识别宏观发布公告，并记录检测延迟。"""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("schema_version") != 1 or not isinstance(config.get("triggers"), list):
        raise ValueError("宏观触发配置无效。")
    if not news_database_path.exists():
        return []

    detected_at = datetime.now(timezone.utc)
    cutoff = (detected_at - timedelta(hours=window_hours)).isoformat()
    connection = sqlite3.connect(f"file:{news_database_path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """SELECT title, publisher, link, published_at_utc, first_seen_at_utc
               FROM news_items
               WHERE COALESCE(published_at_utc, first_seen_at_utc) >= ?
               ORDER BY COALESCE(published_at_utc, first_seen_at_utc) DESC""",
            (cutoff,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()

    signals: list[ReleaseSignal] = []
    for trigger in config["triggers"]:
        keywords = [keyword.lower() for keyword in trigger["title_keywords"]]
        for row in rows:
            title = row["title"].lower()
            if not all(keyword in title for keyword in keywords):
                continue
            published = row["published_at_utc"]
            latency = None
            if published:
                latency = (detected_at - datetime.fromisoformat(published)).total_seconds()
            signals.append(
                ReleaseSignal(
                    trigger_key=trigger["trigger_key"],
                    display_name=trigger["display_name"],
                    matched_title=row["title"],
                    publisher=row["publisher"],
                    link=row["link"],
                    published_at_utc=published,
                    detected_at_utc=detected_at.isoformat(),
                    detection_latency_seconds=latency,
                )
            )
            break
    _record_signals(signals, latency_database_path)
    return signals


def latency_report(database_path: Path = DEFAULT_LATENCY_DB, limit: int = 20) -> list[dict[str, Any]]:
    """只读列出历史发布检测延迟。"""
    if limit < 1:
        raise ValueError("limit 必须大于 0。")
    if not database_path.exists():
        return []
    connection = sqlite3.connect(f"file:{database_path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """SELECT trigger_key, publisher, published_at_utc, detected_at_utc,
                      detection_latency_seconds, matched_title
               FROM release_signals ORDER BY detected_at_utc DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    return [dict(row) for row in rows]


def _record_signals(signals: list[ReleaseSignal], database_path: Path) -> None:
    if not signals:
        return
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS release_signals (
                signal_id TEXT PRIMARY KEY,
                trigger_key TEXT NOT NULL,
                display_name TEXT NOT NULL,
                publisher TEXT NOT NULL,
                matched_title TEXT NOT NULL,
                link TEXT NOT NULL,
                published_at_utc TEXT,
                detected_at_utc TEXT NOT NULL,
                detection_latency_seconds REAL,
                status TEXT NOT NULL CHECK(status = 'pending_review')
            );
            CREATE INDEX IF NOT EXISTS idx_release_signals_trigger
                ON release_signals(trigger_key, detected_at_utc DESC);
            CREATE TRIGGER IF NOT EXISTS release_signals_no_update BEFORE UPDATE ON release_signals
                BEGIN SELECT RAISE(ABORT, 'release signals are append-only'); END;
            """
        )
        import hashlib

        connection.executemany(
            """INSERT OR IGNORE INTO release_signals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_review')""",
            [
                (
                    hashlib.sha256(f"{signal.trigger_key}|{signal.link}".encode("utf-8")).hexdigest(),
                    signal.trigger_key, signal.display_name, signal.publisher, signal.matched_title,
                    signal.link, signal.published_at_utc, signal.detected_at_utc,
                    signal.detection_latency_seconds,
                )
                for signal in signals
            ],
        )
