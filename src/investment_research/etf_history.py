"""ETF 待审核候选资料的本地 SQLite 审计索引。

数据库只保存 ``pending_review`` 候选的可查询索引及证据哈希；原始文件仍为证据原件，
``config/etf-profiles.json`` 仍是人工审核后事实的唯一来源。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from investment_research.etf_profiles import PROJECT_ROOT

DEFAULT_CANDIDATE_HISTORY_DB = PROJECT_ROOT / "data" / "processed" / "etf" / "candidate-history.sqlite3"
_SCHEMA_VERSION = 1


def initialize_history_db(database_path: Path = DEFAULT_CANDIDATE_HISTORY_DB) -> None:
    """创建或升级本地候选审计数据库。"""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS candidate_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                provider TEXT NOT NULL,
                source_id TEXT NOT NULL,
                publisher TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_url TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                raw_path TEXT NOT NULL,
                metadata_path TEXT NOT NULL,
                candidate_path TEXT NOT NULL,
                raw_sha256 TEXT NOT NULL,
                candidate_sha256 TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status = 'pending_review'),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS candidate_facts (
                snapshot_id TEXT NOT NULL REFERENCES candidate_snapshots(snapshot_id) ON DELETE RESTRICT,
                fact_name TEXT NOT NULL,
                value_json TEXT NOT NULL,
                unit TEXT,
                as_of TEXT,
                status TEXT NOT NULL CHECK (status = 'pending_review'),
                PRIMARY KEY (snapshot_id, fact_name)
            );
            CREATE INDEX IF NOT EXISTS idx_candidate_snapshots_symbol_retrieved
                ON candidate_snapshots(symbol, retrieved_at DESC);
            CREATE INDEX IF NOT EXISTS idx_candidate_snapshots_source_retrieved
                ON candidate_snapshots(source_id, retrieved_at DESC);
            CREATE INDEX IF NOT EXISTS idx_candidate_facts_name
                ON candidate_facts(fact_name);
            """
        )
        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")


def record_pending_snapshot(
    candidate: Mapping[str, Any],
    candidate_path: Path,
    database_path: Path = DEFAULT_CANDIDATE_HISTORY_DB,
) -> str:
    """将已落盘的待审核候选及字段写入 SQLite，并返回幂等快照 ID。"""
    if candidate.get("status") != "pending_review":
        raise ValueError("候选审计库只允许写入 pending_review 数据。")

    source = _required_mapping(candidate, "source")
    facts = _required_mapping(candidate, "facts")
    raw_path = _project_path(_required_text(source, "raw_path"))
    metadata_path = _project_path(_required_text(source, "metadata_path"))
    if not raw_path.is_file() or not metadata_path.is_file() or not candidate_path.is_file():
        raise ValueError("候选证据文件不完整，无法写入候选审计库。")

    raw_sha256 = _sha256_file(raw_path)
    candidate_sha256 = _sha256_file(candidate_path)
    snapshot_id = _snapshot_id(candidate, raw_sha256)
    snapshot = (
        snapshot_id,
        _required_text(candidate, "symbol").upper(),
        _required_text(candidate, "market"),
        _required_text(candidate, "provider"),
        _required_text(source, "id"),
        _required_text(source, "publisher"),
        _required_text(source, "source_type"),
        _required_text(source, "source_url"),
        _required_text(source, "retrieved_at"),
        _relative_project_path(raw_path),
        _relative_project_path(metadata_path),
        _relative_project_path(candidate_path),
        raw_sha256,
        candidate_sha256,
        "pending_review",
        _required_text(source, "retrieved_at"),
    )

    initialize_history_db(database_path)
    with _connect(database_path) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO candidate_snapshots (
                snapshot_id, symbol, market, provider, source_id, publisher, source_type, source_url,
                retrieved_at, raw_path, metadata_path, candidate_path, raw_sha256, candidate_sha256,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            snapshot,
        )
        for fact_name, fact in facts.items():
            if not isinstance(fact, Mapping) or "value" not in fact:
                raise ValueError(f"候选字段 {fact_name} 缺少 value。")
            connection.execute(
                """
                INSERT OR IGNORE INTO candidate_facts (
                    snapshot_id, fact_name, value_json, unit, as_of, status
                ) VALUES (?, ?, ?, ?, ?, 'pending_review')
                """,
                (
                    snapshot_id,
                    str(fact_name),
                    json.dumps(fact["value"], ensure_ascii=False, sort_keys=True),
                    fact.get("unit"),
                    fact.get("as_of"),
                ),
            )
    return snapshot_id


def list_pending_snapshots(
    symbol: str,
    limit: int = 20,
    database_path: Path = DEFAULT_CANDIDATE_HISTORY_DB,
) -> list[dict[str, Any]]:
    """按抓取时间倒序读取一个 ETF 的待审核历史候选。"""
    if limit < 1:
        raise ValueError("limit 必须大于 0。")
    if not database_path.exists():
        return []

    with _connect(database_path) as connection:
        snapshots = connection.execute(
            """
            SELECT snapshot_id, symbol, market, provider, source_id, publisher, source_type, source_url,
                   retrieved_at, raw_path, metadata_path, raw_sha256, candidate_sha256, status
            FROM candidate_snapshots
            WHERE symbol = ? AND status = 'pending_review'
            ORDER BY retrieved_at DESC
            LIMIT ?
            """,
            (symbol.upper(), limit),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in snapshots:
            facts = connection.execute(
                """
                SELECT fact_name, value_json, unit, as_of
                FROM candidate_facts
                WHERE snapshot_id = ? AND status = 'pending_review'
                ORDER BY fact_name
                """,
                (row["snapshot_id"],),
            ).fetchall()
            results.append(
                {
                    "snapshot_id": row["snapshot_id"],
                    "symbol": row["symbol"],
                    "market": row["market"],
                    "provider": row["provider"],
                    "status": row["status"],
                    "source": {
                        "id": row["source_id"],
                        "publisher": row["publisher"],
                        "source_type": row["source_type"],
                        "source_url": row["source_url"],
                        "retrieved_at": row["retrieved_at"],
                        "raw_path": row["raw_path"],
                        "metadata_path": row["metadata_path"],
                        "raw_sha256": row["raw_sha256"],
                        "candidate_sha256": row["candidate_sha256"],
                    },
                    "facts": {
                        fact["fact_name"]: {
                            "value": json.loads(fact["value_json"]),
                            "unit": fact["unit"],
                            "as_of": fact["as_of"],
                        }
                        for fact in facts
                    },
                }
            )
    return results


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _snapshot_id(candidate: Mapping[str, Any], raw_sha256: str) -> str:
    source = _required_mapping(candidate, "source")
    identity = {
        "symbol": _required_text(candidate, "symbol").upper(),
        "provider": _required_text(candidate, "provider"),
        "source_url": _required_text(source, "source_url"),
        "retrieved_at": _required_text(source, "retrieved_at"),
        "raw_sha256": raw_sha256,
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _project_path(relative_path: str) -> Path:
    path = PROJECT_ROOT / relative_path
    if path.resolve().is_relative_to(PROJECT_ROOT.resolve()):
        return path
    raise ValueError("候选证据路径必须位于项目目录内。")


def _relative_project_path(path: Path) -> str:
    return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))


def _required_mapping(payload: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    value = payload.get(field_name)
    if not isinstance(value, Mapping):
        raise ValueError(f"候选数据缺少对象字段：{field_name}。")
    return value


def _required_text(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"候选数据缺少文本字段：{field_name}。")
    return value
