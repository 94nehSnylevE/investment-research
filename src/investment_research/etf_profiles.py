"""本地 ETF 静态资料快照、校验及研究模板生成。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ETF_PROFILES = PROJECT_ROOT / "config" / "etf-profiles.json"
RESEARCH_TEMPLATE = PROJECT_ROOT / "docs" / "templates" / "etf-research.md"
RESEARCH_REPORT_DIRECTORY = PROJECT_ROOT / "reports" / "research"
FACT_NAMES = {"expense_ratio", "holdings", "sector_weights", "country_weights", "benchmark"}
FACT_STATUSES = {"pending_review", "verified", "not_applicable"}
PROFILE_STATUSES = {"pending_review", "verified", "partially_verified"}


def load_etf_profiles(profile_path: Path = DEFAULT_ETF_PROFILES) -> dict[str, dict[str, Any]]:
    """加载并校验本地 ETF 资料；返回以股票代码索引的 profile。"""
    with profile_path.open(encoding="utf-8") as profile_file:
        payload = json.load(profile_file)
    if payload.get("schema_version") != 1:
        raise ValueError("ETF 资料 schema_version 必须为 1。")
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("ETF 资料必须包含非空 profiles 对象。")
    for symbol, profile in profiles.items():
        _validate_profile(symbol, profile)
    return profiles


def profile_summary(profile: dict[str, Any]) -> tuple[str, int, int]:
    """返回配置状态、已核验事实数和待核验事实数，用于报告渲染。"""
    facts = profile["facts"]
    verified_count = sum(item["status"] == "verified" for item in facts.values())
    pending_count = sum(item["status"] == "pending_review" for item in facts.values())
    return profile["profile_status"], verified_count, pending_count


def generate_research_template(
    symbol: str,
    profile_path: Path = DEFAULT_ETF_PROFILES,
    output_directory: Path = RESEARCH_REPORT_DIRECTORY,
    dry_run: bool = False,
) -> Optional[Path]:
    """为单只 ETF 生成事实/观点严格分离的本地研究模板。"""
    normalized_symbol = symbol.upper()
    profiles = load_etf_profiles(profile_path)
    profile = profiles.get(normalized_symbol)
    if profile is None:
        raise ValueError(f"未找到 ETF 资料：{normalized_symbol}。")

    template = RESEARCH_TEMPLATE.read_text(encoding="utf-8")
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    status, verified_count, pending_count = profile_summary(profile)
    fact_statuses = "、".join(
        f"{name}={fact['status']}" for name, fact in profile["facts"].items()
    )
    profile_context = (
        "## 0. 资料审核状态\n\n"
        f"- 生成时间（UTC）：{generated_at.isoformat()}\n"
        f"- Profile 状态：{status}；已核验事实：{verified_count}；待核验事实：{pending_count}\n"
        f"- 各字段状态：{fact_statuses}\n"
        "- 本模板不填充待核验候选数值；仅人工审核、带来源索引的 `verified` 条目可进入第 1 节。\n\n"
    )
    content = template.replace("{{SYMBOL}}", normalized_symbol).replace(
        "## 1. 结构化事实（仅已核验）", profile_context + "## 1. 结构化事实（仅已核验）"
    )
    if dry_run:
        print(content, end="")
        return None

    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / f"{generated_at.date().isoformat()}_{normalized_symbol}_etf_research.md"
    output_path.write_text(content, encoding="utf-8")
    return output_path


def _validate_profile(symbol: str, profile: Any) -> None:
    if not isinstance(profile, dict):
        raise ValueError(f"ETF 资料 {symbol} 必须为对象。")
    if profile.get("symbol") != symbol or profile.get("market") != "us":
        raise ValueError(f"ETF 资料 {symbol} 的代码或市场不匹配。")
    if profile.get("profile_status") not in PROFILE_STATUSES:
        raise ValueError(f"ETF 资料 {symbol} 的 profile_status 无效。")
    if profile.get("reviewed_at") is not None and not isinstance(profile["reviewed_at"], str):
        raise ValueError(f"ETF 资料 {symbol} 的 reviewed_at 必须是字符串或 null。")
    sources = profile.get("sources")
    if not isinstance(sources, list):
        raise ValueError(f"ETF 资料 {symbol} 的 sources 必须为列表。")
    source_ids = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError(f"ETF 资料 {symbol} 的来源必须为对象。")
        required_fields = {"id", "publisher", "source_type", "source_url", "retrieved_at", "verification_status"}
        if not required_fields.issubset(source) or not all(isinstance(source[field], str) and source[field] for field in required_fields - {"verification_status"}):
            raise ValueError(f"ETF 资料 {symbol} 的来源元数据不完整。")
        if source["verification_status"] not in {"pending_review", "verified", "rejected"}:
            raise ValueError(f"ETF 资料 {symbol} 的来源审核状态无效。")
        if source["id"] in source_ids:
            raise ValueError(f"ETF 资料 {symbol} 的来源 ID 必须唯一。")
        source_ids.add(source["id"])
    if profile["profile_status"] == "verified" and not sources:
        raise ValueError(f"已核验 ETF 资料 {symbol} 必须包含来源。")

    facts = profile.get("facts")
    if not isinstance(facts, dict) or set(facts) != FACT_NAMES:
        raise ValueError(f"ETF 资料 {symbol} 必须包含完整事实字段。")
    for fact_name, fact in facts.items():
        if not isinstance(fact, dict) or fact.get("status") not in FACT_STATUSES:
            raise ValueError(f"ETF 资料 {symbol} 的 {fact_name} 状态无效。")
        if fact["status"] == "verified":
            if not isinstance(fact.get("source_id"), str) or fact["source_id"] not in source_ids:
                raise ValueError(f"ETF 资料 {symbol} 的已核验 {fact_name} 必须引用来源。")
            if not isinstance(fact.get("as_of"), str) or not fact["as_of"]:
                raise ValueError(f"ETF 资料 {symbol} 的已核验 {fact_name} 必须记录 as_of。")
        if fact["status"] == "not_applicable" and not isinstance(fact.get("reason"), str):
            raise ValueError(f"ETF 资料 {symbol} 的不适用 {fact_name} 必须说明原因。")
