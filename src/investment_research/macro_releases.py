"""宏观发布的人工预期快照、FRED 实际值版本与预期差审计。

预期只允许在发布时间前追加；实际值和修订只追加、不覆盖。FRED 是分发渠道，
市场预期来源始终保留为人工录入的第三方来源，不冒充官方数据。
"""

from __future__ import annotations

from calendar import monthrange
import csv
import hashlib
import json
import re
import sqlite3
import subprocess
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import StringIO
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from investment_research.etf_profiles import PROJECT_ROOT

DEFAULT_RELEASE_CONFIG = PROJECT_ROOT / "config" / "macro-releases.json"
DEFAULT_RELEASE_DB = PROJECT_ROOT / "data" / "processed" / "macro" / "macro-release-history.sqlite3"
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data"
_PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_TRANSFORMS = {"identity", "diff_1", "pct_change_1"}


def load_release_config(config_path: Path = DEFAULT_RELEASE_CONFIG) -> dict[str, Any]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("metrics"), dict):
        raise ValueError("宏观发布配置无效。")
    for metric_key, metric in payload["metrics"].items():
        if not isinstance(metric, dict):
            raise ValueError(f"宏观指标 {metric_key} 配置必须为对象。")
        required = {"event_key", "display_name", "series_id", "transform", "unit", "precision", "original_publisher"}
        if not required.issubset(metric):
            raise ValueError(f"宏观指标 {metric_key} 配置不完整。")
        if metric["transform"] not in _TRANSFORMS:
            raise ValueError(f"宏观指标 {metric_key} 的 transform 无效。")
        if not isinstance(metric["precision"], int) or not 0 <= metric["precision"] <= 6:
            raise ValueError(f"宏观指标 {metric_key} 的 precision 无效。")
    return payload


def initialize_release_db(database_path: Path = DEFAULT_RELEASE_DB) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS release_instances (
                release_id TEXT PRIMARY KEY,
                event_key TEXT NOT NULL,
                reference_period TEXT NOT NULL,
                scheduled_at_utc TEXT NOT NULL,
                expectation_cutoff_at_utc TEXT NOT NULL,
                timezone TEXT NOT NULL,
                schedule_source_url TEXT NOT NULL,
                schedule_evidence_sha256 TEXT,
                schedule_status TEXT NOT NULL CHECK(schedule_status = 'manual_schedule_candidate'),
                created_at_utc TEXT NOT NULL,
                UNIQUE(event_key, reference_period)
            );
            CREATE TABLE IF NOT EXISTS expectation_snapshots (
                expectation_id TEXT PRIMARY KEY,
                release_id TEXT NOT NULL REFERENCES release_instances(release_id) ON DELETE RESTRICT,
                metric_key TEXT NOT NULL,
                expected_value_text TEXT NOT NULL,
                previous_value_text TEXT,
                unit TEXT NOT NULL,
                source_label TEXT NOT NULL,
                source_url TEXT NOT NULL,
                evidence_path TEXT,
                evidence_sha256 TEXT,
                note TEXT,
                recorded_at_utc TEXT NOT NULL,
                verification_status TEXT NOT NULL CHECK(verification_status = 'manual_pre_release_candidate'),
                supersedes_expectation_id TEXT REFERENCES expectation_snapshots(expectation_id),
                payload_sha256 TEXT NOT NULL,
                chain_hash TEXT NOT NULL,
                UNIQUE(release_id, metric_key, recorded_at_utc)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_expectation_one_root
                ON expectation_snapshots(release_id, metric_key)
                WHERE supersedes_expectation_id IS NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_expectation_one_successor
                ON expectation_snapshots(supersedes_expectation_id)
                WHERE supersedes_expectation_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS actual_vintages (
                actual_id TEXT PRIMARY KEY,
                release_id TEXT NOT NULL REFERENCES release_instances(release_id) ON DELETE RESTRICT,
                metric_key TEXT NOT NULL,
                series_id TEXT NOT NULL,
                observation_date TEXT NOT NULL,
                transform_key TEXT NOT NULL,
                formula_version TEXT NOT NULL,
                input_values_json TEXT NOT NULL,
                actual_value_raw_text TEXT NOT NULL,
                actual_value_display_text TEXT NOT NULL,
                unit TEXT NOT NULL,
                expectation_id TEXT NOT NULL REFERENCES expectation_snapshots(expectation_id),
                surprise_value_text TEXT,
                fetched_at_utc TEXT NOT NULL,
                provider TEXT NOT NULL CHECK(provider = 'fred'),
                original_publisher TEXT NOT NULL,
                source_url TEXT NOT NULL,
                raw_path TEXT NOT NULL,
                raw_sha256 TEXT NOT NULL,
                revision_number INTEGER NOT NULL CHECK(revision_number >= 0),
                capture_status TEXT NOT NULL CHECK(capture_status IN ('prompt_first_capture', 'late_first_capture', 'revision_seen')),
                supersedes_actual_id TEXT REFERENCES actual_vintages(actual_id),
                UNIQUE(release_id, metric_key, revision_number)
            );
            CREATE TABLE IF NOT EXISTS actual_captures (
                capture_id TEXT PRIMARY KEY,
                release_id TEXT NOT NULL REFERENCES release_instances(release_id) ON DELETE RESTRICT,
                metric_key TEXT NOT NULL,
                actual_id TEXT NOT NULL REFERENCES actual_vintages(actual_id) ON DELETE RESTRICT,
                fetched_at_utc TEXT NOT NULL,
                raw_path TEXT NOT NULL,
                metadata_path TEXT NOT NULL,
                raw_sha256 TEXT NOT NULL,
                capture_result TEXT NOT NULL CHECK(capture_result IN ('new_vintage', 'unchanged', 'stale_capture'))
            );
            CREATE INDEX IF NOT EXISTS idx_expectation_release_metric
                ON expectation_snapshots(release_id, metric_key, recorded_at_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_actual_release_metric
                ON actual_vintages(release_id, metric_key, revision_number);
            CREATE TRIGGER IF NOT EXISTS release_no_update BEFORE UPDATE ON release_instances
                BEGIN SELECT RAISE(ABORT, 'release instances are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS release_no_delete BEFORE DELETE ON release_instances
                BEGIN SELECT RAISE(ABORT, 'release instances are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS expectation_no_update BEFORE UPDATE ON expectation_snapshots
                BEGIN SELECT RAISE(ABORT, 'expectation snapshots are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS expectation_no_delete BEFORE DELETE ON expectation_snapshots
                BEGIN SELECT RAISE(ABORT, 'expectation snapshots are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS actual_no_update BEFORE UPDATE ON actual_vintages
                BEGIN SELECT RAISE(ABORT, 'actual vintages are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS actual_no_delete BEFORE DELETE ON actual_vintages
                BEGIN SELECT RAISE(ABORT, 'actual vintages are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS capture_no_update BEFORE UPDATE ON actual_captures
                BEGIN SELECT RAISE(ABORT, 'actual captures are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS capture_no_delete BEFORE DELETE ON actual_captures
                BEGIN SELECT RAISE(ABORT, 'actual captures are append-only'); END;
            """
        )
        connection.execute("PRAGMA user_version = 2")


def record_expectation(
    event_key: str,
    metric_key: str,
    reference_period: str,
    scheduled_at: str,
    expected_value: str,
    source_label: str,
    source_url: str,
    previous_value: Optional[str] = None,
    evidence_path: Optional[Path] = None,
    note: Optional[str] = None,
    database_path: Path = DEFAULT_RELEASE_DB,
    config_path: Path = DEFAULT_RELEASE_CONFIG,
) -> dict[str, str]:
    """在发布截止时间前追加一条人工预期；录入时间只能取系统当前时间。"""
    config = load_release_config(config_path)
    metric = _metric_definition(config, metric_key, event_key)
    period = _validate_period(reference_period)
    scheduled_utc = _parse_scheduled_at(scheduled_at)
    _validate_release_timing(period, scheduled_utc)
    if _fred_observation_exists(metric["series_id"], period):
        raise ValueError("FRED 已存在该参考期观测；拒绝补录为发布前预期。")
    expected = _decimal_text(expected_value, "forecast")
    previous = _decimal_text(previous_value, "previous") if previous_value is not None else None
    label = source_label.strip()
    if not label:
        raise ValueError("source_label 不能为空。")
    if urlparse(source_url).scheme not in {"http", "https"}:
        raise ValueError("source_url 必须是 HTTP(S) 地址。")
    evidence_relative, evidence_hash = _evidence(evidence_path)
    release_id = f"us:{event_key}:{period}"

    initialize_release_db(database_path)
    with _connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        now = datetime.now(timezone.utc)
        if now >= scheduled_utc:
            raise ValueError("发布时间已到或已过；拒绝补录为发布前预期。")
        recorded_at = now.isoformat()
        existing_release = connection.execute(
            "SELECT scheduled_at_utc, expectation_cutoff_at_utc FROM release_instances WHERE release_id = ?",
            (release_id,),
        ).fetchone()
        if existing_release is None:
            connection.execute(
                """INSERT INTO release_instances (
                    release_id, event_key, reference_period, scheduled_at_utc, expectation_cutoff_at_utc,
                    timezone, schedule_source_url, schedule_evidence_sha256, schedule_status, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual_schedule_candidate', ?)""",
                (
                    release_id, event_key, period, scheduled_utc.isoformat(), scheduled_utc.isoformat(),
                    config["timezone"], source_url, evidence_hash, recorded_at,
                ),
            )
        elif existing_release["scheduled_at_utc"] != scheduled_utc.isoformat():
            raise ValueError("该发布实例的 scheduled_at 已固化；不得后移或改写截止时间。")

        previous_snapshot = connection.execute(
            """SELECT expectation_id, chain_hash FROM expectation_snapshots
               WHERE release_id = ? AND metric_key = ? ORDER BY recorded_at_utc DESC LIMIT 1""",
            (release_id, metric_key),
        ).fetchone()
        payload = {
            "release_id": release_id,
            "metric_key": metric_key,
            "expected_value": expected,
            "previous_value": previous,
            "unit": metric["unit"],
            "source_label": label,
            "source_url": source_url,
            "evidence_path": evidence_relative,
            "evidence_sha256": evidence_hash,
            "note": note,
            "recorded_at_utc": recorded_at,
            "verification_status": "manual_pre_release_candidate",
        }
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        prior_chain = previous_snapshot["chain_hash"] if previous_snapshot else ""
        chain_hash = hashlib.sha256(f"{prior_chain}|{payload_hash}".encode("utf-8")).hexdigest()
        expectation_id = hashlib.sha256(f"{release_id}|{metric_key}|{recorded_at}|{payload_hash}".encode("utf-8")).hexdigest()
        connection.execute(
            """INSERT INTO expectation_snapshots (
                expectation_id, release_id, metric_key, expected_value_text, previous_value_text, unit,
                source_label, source_url, evidence_path, evidence_sha256, note, recorded_at_utc,
                verification_status, supersedes_expectation_id, payload_sha256, chain_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual_pre_release_candidate', ?, ?, ?)""",
            (
                expectation_id, release_id, metric_key, expected, previous, metric["unit"], label,
                source_url, evidence_relative, evidence_hash, note, recorded_at,
                previous_snapshot["expectation_id"] if previous_snapshot else None, payload_hash, chain_hash,
            ),
        )
    return {"release_id": release_id, "expectation_id": expectation_id, "recorded_at_utc": recorded_at}


def sync_due_actuals(
    release_id: Optional[str] = None,
    database_path: Path = DEFAULT_RELEASE_DB,
    config_path: Path = DEFAULT_RELEASE_CONFIG,
    data_root: Path = DEFAULT_DATA_ROOT,
    timeout_seconds: int = 30,
) -> list[dict[str, str]]:
    """为已到发布时间且已有预期的指标抓取 FRED 实际值，并追加修订版本。"""
    config = load_release_config(config_path)
    if not database_path.exists():
        return []
    now = datetime.now(timezone.utc)
    query = """
        SELECT DISTINCT r.release_id, r.event_key, r.reference_period, r.scheduled_at_utc, e.metric_key
        FROM release_instances r JOIN expectation_snapshots e ON e.release_id = r.release_id
        WHERE r.scheduled_at_utc <= ?
    """
    parameters: list[Any] = [now.isoformat()]
    if release_id:
        query += " AND r.release_id = ?"
        parameters.append(release_id)
    query += " ORDER BY r.scheduled_at_utc, e.metric_key"
    with _connect(database_path) as connection:
        due = connection.execute(query, parameters).fetchall()

    results: list[dict[str, str]] = []
    for row in due:
        metric = _metric_definition(config, row["metric_key"], row["event_key"])
        try:
            fetched = _fetch_fred_metric(metric, row["metric_key"], row["reference_period"], data_root, timeout_seconds)
            status = _record_actual_vintage(database_path, row, metric, fetched, now)
            results.append({"release_id": row["release_id"], "metric_key": row["metric_key"], "status": status})
        except (ValueError, sqlite3.Error) as error:
            results.append({"release_id": row["release_id"], "metric_key": row["metric_key"], "status": f"error: {error}"})
    return results


def list_release_summaries(
    database_path: Path = DEFAULT_RELEASE_DB,
    config_path: Path = DEFAULT_RELEASE_CONFIG,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """只读列出发布前候选、首次捕获、surprise 候选与最新修订。"""
    if limit < 1:
        raise ValueError("limit 必须大于 0。")
    if not database_path.exists():
        return []
    config = load_release_config(config_path)
    connection = _connect_readonly(database_path)
    try:
        pairs = connection.execute(
            """SELECT DISTINCT r.release_id, r.event_key, r.reference_period, r.scheduled_at_utc, e.metric_key
               FROM release_instances r JOIN expectation_snapshots e ON e.release_id = r.release_id
               ORDER BY r.scheduled_at_utc DESC, e.metric_key LIMIT ?""",
            (limit,),
        ).fetchall()
        summaries: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        for pair in pairs:
            metric = _metric_definition(config, pair["metric_key"], pair["event_key"])
            first_actual = connection.execute(
                """SELECT actual_value_display_text, fetched_at_utc, expectation_id,
                          surprise_value_text, capture_status, unit, formula_version
                   FROM actual_vintages
                   WHERE release_id = ? AND metric_key = ? AND revision_number = 0""",
                (pair["release_id"], pair["metric_key"]),
            ).fetchone()
            if first_actual:
                expectation = connection.execute(
                    """SELECT expected_value_text, previous_value_text, recorded_at_utc, source_label,
                              source_url, verification_status, unit
                       FROM expectation_snapshots WHERE expectation_id = ?""",
                    (first_actual["expectation_id"],),
                ).fetchone()
            else:
                expectation = connection.execute(
                    """SELECT expected_value_text, previous_value_text, recorded_at_utc, source_label,
                              source_url, verification_status, unit
                       FROM expectation_snapshots WHERE release_id = ? AND metric_key = ?
                       ORDER BY recorded_at_utc DESC LIMIT 1""",
                    (pair["release_id"], pair["metric_key"]),
                ).fetchone()
            latest_actual = connection.execute(
                """SELECT actual_value_display_text, revision_number FROM actual_vintages
                   WHERE release_id = ? AND metric_key = ? ORDER BY revision_number DESC LIMIT 1""",
                (pair["release_id"], pair["metric_key"]),
            ).fetchone()
            scheduled = datetime.fromisoformat(pair["scheduled_at_utc"])
            status = "upcoming" if scheduled > now else "awaiting_fred" if first_actual is None else first_actual["capture_status"]
            summaries.append(
                {
                    "release_id": pair["release_id"],
                    "event_key": pair["event_key"],
                    "reference_period": pair["reference_period"],
                    "scheduled_at_utc": pair["scheduled_at_utc"],
                    "metric_key": pair["metric_key"],
                    "display_name": metric["display_name"],
                    "unit": expectation["unit"],
                    "forecast": expectation["expected_value_text"],
                    "previous": expectation["previous_value_text"],
                    "expectation_recorded_at_utc": expectation["recorded_at_utc"],
                    "source_label": expectation["source_label"],
                    "source_url": expectation["source_url"],
                    "expectation_status": expectation["verification_status"],
                    "first_captured_actual": first_actual["actual_value_display_text"] if first_actual else None,
                    "surprise": first_actual["surprise_value_text"] if first_actual else None,
                    "capture_status": first_actual["capture_status"] if first_actual else None,
                    "formula_version": first_actual["formula_version"] if first_actual else f"{metric['transform']}:v1",
                    "latest_actual": latest_actual["actual_value_display_text"] if latest_actual else None,
                    "revision_number": latest_actual["revision_number"] if latest_actual else None,
                    "status": status,
                }
            )
        return summaries
    finally:
        connection.close()


def _fred_observation_exists(series_id: str, period: str, timeout_seconds: int = 15) -> bool:
    source_url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        response = subprocess.run(
            [
                "curl", "--http1.1", "--fail", "--silent", "--show-error", "--max-time",
                str(timeout_seconds), "--retry", "1", "--retry-all-errors", source_url,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds * 2,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("无法确认 FRED 目标期次是否已发布；为防止事后补录，本次拒绝写入。") from error
    if response.returncode:
        raise ValueError("无法确认 FRED 目标期次是否已发布；为防止事后补录，本次拒绝写入。")
    rows = list(csv.DictReader(StringIO(response.stdout)))
    if not response.stdout.strip() or not rows:
        raise ValueError("FRED 响应为空；无法证明目标期次尚未发布，本次拒绝写入。")
    required_columns = {"observation_date", series_id}
    if not required_columns.issubset(rows[0]):
        raise ValueError("FRED 响应缺少必需列；无法证明目标期次尚未发布，本次拒绝写入。")
    target_date = f"{period}-01"
    valid_dates = [
        row["observation_date"]
        for row in rows
        if row.get("observation_date") and row.get(series_id, "").strip() not in {"", "."}
    ]
    if not valid_dates:
        raise ValueError("FRED 响应没有可解析观测；无法证明目标期次尚未发布，本次拒绝写入。")
    if target_date in valid_dates:
        return True
    if target_date <= max(valid_dates):
        raise ValueError("FRED 序列在目标期次之后已有观测但目标期缺失；本次保守拒绝写入。")
    return False


def _fetch_fred_metric(
    metric: Mapping[str, Any], metric_key: str, reference_period: str, data_root: Path, timeout_seconds: int
) -> dict[str, Any]:
    series_id = metric["series_id"]
    source_url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    fetched_at = datetime.now(timezone.utc)
    try:
        response = subprocess.run(
            ["curl", "--http1.1", "--fail", "--silent", "--show-error", "--max-time", str(timeout_seconds), "--retry", "2", "--retry-all-errors", source_url],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds * 3,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("FRED 获取失败：curl 不可用或超时。") from error
    if response.returncode:
        raise ValueError(f"FRED 获取失败：curl exit {response.returncode}。")
    raw_csv = response.stdout
    inputs, actual_raw = _transform_series(raw_csv, series_id, reference_period, metric["transform"])
    actual_display = _quantize(actual_raw, metric["precision"])
    directory = data_root / "raw" / "macro" / "releases" / "fred" / metric_key
    directory.mkdir(parents=True, exist_ok=True)
    stem = fetched_at.strftime("%Y%m%dT%H%M%S%fZ")
    raw_path = directory / f"{stem}.csv"
    metadata_path = directory / f"{stem}.metadata.json"
    raw_path.write_text(raw_csv, encoding="utf-8")
    raw_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provider": "fred",
                "original_publisher": metric["original_publisher"],
                "metric_key": metric_key,
                "series_id": series_id,
                "reference_period": reference_period,
                "transform": metric["transform"],
                "source_url": source_url,
                "fetched_at": fetched_at.isoformat(),
                "raw_path": str(raw_path.resolve().relative_to(PROJECT_ROOT.resolve())),
                "raw_sha256": raw_hash,
                "status": "pending_review",
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return {
        "series_id": series_id,
        "observation_date": f"{reference_period}-01",
        "inputs": inputs,
        "actual_raw": _normalize_decimal(actual_raw),
        "actual_display": actual_display,
        "fetched_at_utc": fetched_at.isoformat(),
        "source_url": source_url,
        "raw_path": str(raw_path.resolve().relative_to(PROJECT_ROOT.resolve())),
        "metadata_path": str(metadata_path.resolve().relative_to(PROJECT_ROOT.resolve())),
        "raw_sha256": raw_hash,
    }


def _transform_series(raw_csv: str, series_id: str, period: str, transform: str) -> tuple[dict[str, str], Decimal]:
    rows = list(csv.DictReader(StringIO(raw_csv)))
    if not rows or "observation_date" not in rows[0] or series_id not in rows[0]:
        raise ValueError("FRED CSV 缺少预期列。")
    values = {
        row["observation_date"]: Decimal(row[series_id])
        for row in rows
        if row.get(series_id, "").strip() not in {"", "."}
    }
    current_date = f"{period}-01"
    if current_date not in values:
        raise ValueError(f"FRED 尚无参考期 {period} 的有效观测。")
    current = values[current_date]
    inputs = {current_date: _normalize_decimal(current)}
    if transform == "identity":
        return inputs, current
    previous_date = f"{_previous_month(period)}-01"
    if previous_date not in values:
        raise ValueError(f"FRED 缺少变换所需前期 {previous_date}。")
    previous = values[previous_date]
    inputs[previous_date] = _normalize_decimal(previous)
    if transform == "diff_1":
        return inputs, current - previous
    if previous == 0:
        raise ValueError("FRED 前期值为零，无法计算环比。")
    return inputs, (current / previous - Decimal("1")) * Decimal("100")


def _record_actual_vintage(
    database_path: Path, release: sqlite3.Row, metric: Mapping[str, Any], fetched: Mapping[str, Any], now: datetime
) -> str:
    input_json = json.dumps(fetched["inputs"], sort_keys=True, separators=(",", ":"))
    formula_version = f"{metric['transform']}:v1"
    with _connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        latest = connection.execute(
            """SELECT actual_id, revision_number, input_values_json, actual_value_raw_text,
                      expectation_id, surprise_value_text, fetched_at_utc
               FROM actual_vintages WHERE release_id = ? AND metric_key = ?
               ORDER BY revision_number DESC LIMIT 1""",
            (release["release_id"], release["metric_key"]),
        ).fetchone()
        fetched_at = datetime.fromisoformat(fetched["fetched_at_utc"])
        if latest and fetched_at <= datetime.fromisoformat(latest["fetched_at_utc"]):
            _record_actual_capture(connection, release, fetched, latest["actual_id"], "stale_capture")
            return "stale_capture"
        if latest and latest["input_values_json"] == input_json and latest["actual_value_raw_text"] == fetched["actual_raw"]:
            _record_actual_capture(connection, release, fetched, latest["actual_id"], "unchanged")
            return "unchanged"

        revision_number = 0 if latest is None else latest["revision_number"] + 1
        scheduled = datetime.fromisoformat(release["scheduled_at_utc"])
        if latest is not None:
            capture_status = "revision_seen"
            expectation_id = latest["expectation_id"]
            surprise = latest["surprise_value_text"]
        else:
            expectation = connection.execute(
                """SELECT expectation_id, expected_value_text FROM expectation_snapshots
                   WHERE release_id = ? AND metric_key = ? AND recorded_at_utc < ?
                   ORDER BY recorded_at_utc DESC LIMIT 1""",
                (release["release_id"], release["metric_key"], release["scheduled_at_utc"]),
            ).fetchone()
            if expectation is None:
                raise ValueError("没有合格的发布前预期，拒绝写入配对实际值。")
            expectation_id = expectation["expectation_id"]
            delay = fetched_at - scheduled
            capture_status = "prompt_first_capture" if timedelta(0) <= delay <= timedelta(hours=4) else "late_first_capture"
            surprise = (
                _subtract_text(fetched["actual_display"], expectation["expected_value_text"], metric["precision"])
                if capture_status == "prompt_first_capture"
                else None
            )

        identity = f"{release['release_id']}|{release['metric_key']}|{revision_number}|{input_json}|{fetched['raw_sha256']}"
        actual_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        connection.execute(
            """INSERT INTO actual_vintages (
                actual_id, release_id, metric_key, series_id, observation_date, transform_key,
                formula_version, input_values_json, actual_value_raw_text, actual_value_display_text,
                unit, expectation_id, surprise_value_text, fetched_at_utc, provider, original_publisher,
                source_url, raw_path, raw_sha256, revision_number, capture_status, supersedes_actual_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'fred', ?, ?, ?, ?, ?, ?, ?)""",
            (
                actual_id, release["release_id"], release["metric_key"], fetched["series_id"],
                fetched["observation_date"], metric["transform"], formula_version, input_json,
                fetched["actual_raw"], fetched["actual_display"], metric["unit"], expectation_id,
                surprise, fetched["fetched_at_utc"], metric["original_publisher"], fetched["source_url"],
                fetched["raw_path"], fetched["raw_sha256"], revision_number, capture_status,
                latest["actual_id"] if latest else None,
            ),
        )
        _record_actual_capture(connection, release, fetched, actual_id, "new_vintage")
    return capture_status


def _record_actual_capture(
    connection: sqlite3.Connection,
    release: sqlite3.Row,
    fetched: Mapping[str, Any],
    actual_id: str,
    result: str,
) -> None:
    identity = (
        f"{release['release_id']}|{release['metric_key']}|{fetched['fetched_at_utc']}|"
        f"{fetched['raw_sha256']}|{result}"
    )
    capture_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    connection.execute(
        """INSERT OR IGNORE INTO actual_captures (
            capture_id, release_id, metric_key, actual_id, fetched_at_utc, raw_path, metadata_path,
            raw_sha256, capture_result
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            capture_id, release["release_id"], release["metric_key"], actual_id,
            fetched["fetched_at_utc"], fetched["raw_path"], fetched["metadata_path"],
            fetched["raw_sha256"], result,
        ),
    )


def _metric_definition(config: Mapping[str, Any], metric_key: str, event_key: str) -> Mapping[str, Any]:
    metric = config["metrics"].get(metric_key)
    if not isinstance(metric, Mapping):
        raise ValueError(f"未知宏观指标：{metric_key}。")
    if metric["event_key"] != event_key:
        raise ValueError(f"指标 {metric_key} 不属于事件 {event_key}。")
    return metric


def _parse_scheduled_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("scheduled_at 必须是带时区的 ISO 8601 时间。") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("scheduled_at 必须包含 UTC 偏移或 Z。")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _validate_release_timing(period: str, scheduled_at_utc: datetime) -> None:
    year, month = (int(part) for part in period.split("-"))
    period_end = date(year, month, monthrange(year, month)[1])
    lag_days = (scheduled_at_utc.date() - period_end).days
    if lag_days < 1 or lag_days > 62:
        raise ValueError("scheduled_at 与参考期不匹配；月度数据应在参考期结束后 1 至 62 天发布。")


def _validate_period(value: str) -> str:
    if not _PERIOD_PATTERN.fullmatch(value):
        raise ValueError("reference_period 必须是 YYYY-MM。")
    return value


def _decimal_text(value: str, field_name: str) -> str:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError) as error:
        raise ValueError(f"{field_name} 必须是有限十进制数。") from error
    if not parsed.is_finite():
        raise ValueError(f"{field_name} 必须是有限十进制数。")
    return _normalize_decimal(parsed)


def _normalize_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f") if normalized != 0 else "0"


def _quantize(value: Decimal, precision: int) -> str:
    quantum = Decimal("1").scaleb(-precision)
    return format(value.quantize(quantum, rounding=ROUND_HALF_UP), f".{precision}f")


def _subtract_text(actual: str, expected: str, precision: int) -> str:
    return _quantize(Decimal(actual) - Decimal(expected), precision)


def _previous_month(period: str) -> str:
    year, month = (int(part) for part in period.split("-"))
    if month == 1:
        return f"{year - 1}-12"
    return f"{year}-{month - 1:02d}"


def _evidence(path: Optional[Path]) -> tuple[Optional[str], Optional[str]]:
    if path is None:
        return None, None
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError("evidence_path 不存在或不是文件。")
    try:
        relative = resolved.relative_to(PROJECT_ROOT.resolve())
    except ValueError as error:
        raise ValueError("evidence_path 必须位于项目目录内。") from error
    return str(relative), hashlib.sha256(resolved.read_bytes()).hexdigest()


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _connect_readonly(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{database_path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection
