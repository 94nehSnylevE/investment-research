"""官方发布源新闻采集（只读、可追溯）。

只采集政府/央行官方 RSS。为避免版权与再分发问题，仅保存标题、链接、发布时间与截断摘要，
不保存正文全文。所有条目均为 ``pending_review`` 事实候选，不做情绪判断，也不产生交易信号。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
from xml.etree import ElementTree

from investment_research.etf_profiles import PROJECT_ROOT

DEFAULT_NEWS_CONFIG = PROJECT_ROOT / "config" / "news-feeds.json"
DEFAULT_NEWS_DB = PROJECT_ROOT / "data" / "processed" / "news" / "news-item-history.sqlite3"
USER_AGENT = "investment-research/0.1 (personal research; contact: local-user)"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SUMMARY_CHARACTER_LIMIT = 280
MAX_ITEMS_PER_FEED = 50


@dataclass(frozen=True)
class NewsItem:
    feed_key: str
    publisher: str
    title: str
    link: str
    published_at_utc: Optional[str]
    summary_excerpt: Optional[str]
    item_id: str


@dataclass(frozen=True)
class FeedFetchResult:
    feed_key: str
    display_name: str
    publisher: str
    feed_url: str
    fetched_at_utc: str
    items: tuple[NewsItem, ...]
    new_item_count: int
    raw_path: Optional[Path]
    error: Optional[str] = None


def fetch_official_news(
    config_path: Path = DEFAULT_NEWS_CONFIG,
    database_path: Path = DEFAULT_NEWS_DB,
    data_root: Path = PROJECT_ROOT / "data",
    timeout_seconds: int = 20,
) -> list[FeedFetchResult]:
    """抓取已登记官方源，落原始 XML 证据并写入去重后的条目。"""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("schema_version") != 1 or not isinstance(config.get("feeds"), list):
        raise ValueError("新闻源配置无效。")
    results: list[FeedFetchResult] = []
    for definition in config["feeds"]:
        results.append(_fetch_feed(definition, database_path, data_root, timeout_seconds))
    return results


def list_recent_news(
    limit: int = 20, database_path: Path = DEFAULT_NEWS_DB
) -> list[dict[str, Any]]:
    """只读列出最近条目；不联网、不建库。"""
    if limit < 1:
        raise ValueError("limit 必须大于 0。")
    if not database_path.exists():
        return []
    connection = sqlite3.connect(f"file:{database_path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """SELECT feed_key, publisher, title, link, published_at_utc, first_seen_at_utc, status
               FROM news_items
               ORDER BY COALESCE(published_at_utc, first_seen_at_utc) DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    return [dict(row) for row in rows]


def news_feed_summary(database_path: Path = DEFAULT_NEWS_DB) -> list[dict[str, Any]]:
    """只读汇总各来源的条目数与最近发布时间。"""
    if not database_path.exists():
        return []
    connection = sqlite3.connect(f"file:{database_path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """SELECT feed_key, publisher, COUNT(*) AS item_count,
                      MAX(published_at_utc) AS latest_published_at_utc,
                      MAX(first_seen_at_utc) AS latest_seen_at_utc
               FROM news_items GROUP BY feed_key ORDER BY feed_key"""
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    return [dict(row) for row in rows]


def _fetch_feed(
    definition: dict[str, Any], database_path: Path, data_root: Path, timeout_seconds: int
) -> FeedFetchResult:
    feed_key = _required_text(definition, "feed_key")
    display_name = _required_text(definition, "display_name")
    publisher = _required_text(definition, "publisher")
    feed_url = _required_text(definition, "feed_url")
    allowed_host = _required_text(definition, "allowed_host")
    fetched_at = datetime.now(timezone.utc)

    try:
        raw_xml = _download(feed_url, allowed_host, timeout_seconds)
    except ValueError as error:
        return FeedFetchResult(feed_key, display_name, publisher, feed_url, fetched_at.isoformat(), (), 0, None, str(error))

    raw_path = _write_raw(data_root, feed_key, fetched_at, raw_xml)
    try:
        items = _parse_items(feed_key, publisher, raw_xml)
    except ValueError as error:
        return FeedFetchResult(feed_key, display_name, publisher, feed_url, fetched_at.isoformat(), (), 0, raw_path, str(error))

    new_count = _record_items(database_path, feed_key, publisher, feed_url, fetched_at, raw_path, items)
    return FeedFetchResult(
        feed_key, display_name, publisher, feed_url, fetched_at.isoformat(), items, new_count, raw_path
    )


def _download(feed_url: str, allowed_host: str, timeout_seconds: int) -> str:
    parsed = urlsplit(feed_url)
    if parsed.scheme != "https" or parsed.hostname != allowed_host:
        raise ValueError("新闻源地址必须是预配置的 HTTPS 主机。")
    request = Request(feed_url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml"})
    opener = build_opener()
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            final = urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname != allowed_host:
                raise ValueError("新闻源重定向至未允许地址。")
            raw_bytes = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise ValueError(f"来源返回 HTTP {error.code}；已明确降级，不使用替代数据。") from error
    except Exception as error:
        raise ValueError(f"来源获取失败（{type(error).__name__}）。") from error
    if len(raw_bytes) > MAX_RESPONSE_BYTES:
        raise ValueError("新闻源响应超过 4 MiB 限制。")
    return raw_bytes.decode("utf-8", errors="replace")


def _parse_items(feed_key: str, publisher: str, raw_xml: str) -> tuple[NewsItem, ...]:
    try:
        root = ElementTree.fromstring(raw_xml)
    except ElementTree.ParseError as error:
        raise ValueError("新闻源 XML 解析失败。") from error

    namespaces = {"atom": "http://www.w3.org/2005/Atom"}
    nodes = root.findall(".//item") or root.findall(".//atom:entry", namespaces)
    if not nodes:
        raise ValueError("新闻源未解析出条目。")

    items: list[NewsItem] = []
    for node in nodes[:MAX_ITEMS_PER_FEED]:
        title = _node_text(node, ("title", "atom:title"), namespaces)
        link = _node_text(node, ("link", "guid"), namespaces) or _atom_link(node, namespaces)
        if not title or not link:
            continue
        published = _parse_datetime(
            _node_text(node, ("pubDate", "published", "updated", "atom:published", "atom:updated"), namespaces)
        )
        summary = _node_text(node, ("description", "summary", "atom:summary"), namespaces)
        items.append(
            NewsItem(
                feed_key=feed_key,
                publisher=publisher,
                title=_clean_text(title)[:500],
                link=link.strip(),
                published_at_utc=published.isoformat() if published else None,
                summary_excerpt=_excerpt(summary),
                item_id=hashlib.sha256(f"{feed_key}|{link.strip()}".encode("utf-8")).hexdigest(),
            )
        )
    if not items:
        raise ValueError("新闻源条目缺少标题或链接。")
    return tuple(items)


def _record_items(
    database_path: Path,
    feed_key: str,
    publisher: str,
    feed_url: str,
    fetched_at: datetime,
    raw_path: Path,
    items: tuple[NewsItem, ...],
) -> int:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    raw_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS news_fetches (
                fetch_id TEXT PRIMARY KEY,
                feed_key TEXT NOT NULL,
                feed_url TEXT NOT NULL,
                fetched_at_utc TEXT NOT NULL,
                raw_path TEXT NOT NULL,
                raw_sha256 TEXT NOT NULL,
                item_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS news_items (
                item_id TEXT PRIMARY KEY,
                feed_key TEXT NOT NULL,
                publisher TEXT NOT NULL,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                published_at_utc TEXT,
                summary_excerpt TEXT,
                first_seen_at_utc TEXT NOT NULL,
                first_seen_fetch_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status = 'pending_review')
            );
            CREATE INDEX IF NOT EXISTS idx_news_items_published
                ON news_items(published_at_utc DESC);
            CREATE TRIGGER IF NOT EXISTS news_items_no_update BEFORE UPDATE ON news_items
                BEGIN SELECT RAISE(ABORT, 'news items are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS news_items_no_delete BEFORE DELETE ON news_items
                BEGIN SELECT RAISE(ABORT, 'news items are append-only'); END;
            """
        )
        fetch_id = hashlib.sha256(f"{feed_key}|{fetched_at.isoformat()}|{raw_hash}".encode("utf-8")).hexdigest()
        connection.execute(
            """INSERT OR IGNORE INTO news_fetches VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                fetch_id, feed_key, feed_url, fetched_at.isoformat(),
                str(raw_path.resolve().relative_to(PROJECT_ROOT.resolve())), raw_hash, len(items),
            ),
        )
        before = connection.execute("SELECT COUNT(*) FROM news_items WHERE feed_key = ?", (feed_key,)).fetchone()[0]
        connection.executemany(
            """INSERT OR IGNORE INTO news_items (
                item_id, feed_key, publisher, title, link, published_at_utc,
                summary_excerpt, first_seen_at_utc, first_seen_fetch_id, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_review')""",
            [
                (
                    item.item_id, item.feed_key, item.publisher, item.title, item.link,
                    item.published_at_utc, item.summary_excerpt, fetched_at.isoformat(), fetch_id,
                )
                for item in items
            ],
        )
        after = connection.execute("SELECT COUNT(*) FROM news_items WHERE feed_key = ?", (feed_key,)).fetchone()[0]
    return after - before


def _write_raw(data_root: Path, feed_key: str, fetched_at: datetime, raw_xml: str) -> Path:
    directory = data_root / "raw" / "news" / feed_key
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{fetched_at.strftime('%Y%m%dT%H%M%S%fZ')}.xml"
    path.write_text(raw_xml, encoding="utf-8")
    return path


def _node_text(node: Any, tags: tuple[str, ...], namespaces: dict[str, str]) -> Optional[str]:
    for tag in tags:
        found = node.find(tag, namespaces) if ":" in tag else node.find(tag)
        if found is not None and found.text and found.text.strip():
            return found.text
    return None


def _atom_link(node: Any, namespaces: dict[str, str]) -> Optional[str]:
    link = node.find("atom:link", namespaces)
    if link is not None:
        return link.attrib.get("href")
    return None


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _excerpt(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = _clean_text(value)
    if len(text) <= SUMMARY_CHARACTER_LIMIT:
        return text
    return text[:SUMMARY_CHARACTER_LIMIT].rstrip() + "…"


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def _required_text(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"新闻源配置缺少 {field_name}。")
    return value
