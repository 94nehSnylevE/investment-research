"""ETF 静态资料候选采集与交叉校验。

本模块的网络结果永远是 pending_review 候选，不会写入 etf-profiles 的 verified 事实。
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from investment_research.etf_history import list_pending_snapshots, record_pending_snapshot
from investment_research.etf_profiles import PROJECT_ROOT

ISHARES_URLS = {
    "IWM": "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf",
    "TLT": "https://www.ishares.com/us/products/239454/ishares-20-year-treasury-bond-etf",
}
PROVIDER = "ishares"


def collect_ishares_candidate(symbol: str, timeout_seconds: int = 20) -> dict[str, Any]:
    """抓取 iShares 官方页面并生成待人工审核候选资料。"""
    normalized_symbol = symbol.upper()
    source_url = ISHARES_URLS.get(normalized_symbol)
    if source_url is None:
        raise ValueError(f"暂不支持 iShares 候选采集：{normalized_symbol}。")

    fetched_at = datetime.now(timezone.utc).replace(microsecond=0)
    request = Request(source_url, headers={"User-Agent": "investment-research/0.1 research-only"})
    with urlopen(request, timeout=timeout_seconds) as response:
        raw_html = response.read().decode("utf-8")

    raw_path, metadata_path = _write_raw_page(normalized_symbol, source_url, fetched_at, raw_html)
    facts = _parse_ishares_facts(raw_html)
    if not facts:
        raise ValueError("官方页面未解析出支持的候选字段；请人工使用资料页或 prospectus 审核。")

    candidate = {
        "schema_version": 1,
        "symbol": normalized_symbol,
        "market": "us",
        "provider": PROVIDER,
        "source": {
            "id": f"ishares_{normalized_symbol.lower()}_candidate_page",
            "publisher": "iShares by BlackRock",
            "source_type": "official_fund_page",
            "source_url": source_url,
            "retrieved_at": fetched_at.isoformat(),
            "verification_status": "pending_review",
            "raw_path": str(raw_path.relative_to(PROJECT_ROOT)),
            "metadata_path": str(metadata_path.relative_to(PROJECT_ROOT)),
        },
        "facts": facts,
        "status": "pending_review",
    }
    candidate_path = _write_candidate(normalized_symbol, fetched_at, candidate)
    record_pending_snapshot(candidate, candidate_path)
    return candidate


def build_candidate_review(profile: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    """渲染候选与已核验 profile 的差异清单，不改变 profile。"""
    symbol = profile["symbol"]
    lines = [
        f"# {symbol} ETF 候选资料交叉校验",
        "",
        "## 审核边界",
        "- 下列均为 `pending_review` 候选资料，不能自动成为结构化事实或交易依据。",
        "- 需要至少两个可比来源且口径/截至日一致，才可进入人工确认；最终仍由人工将 profile 更新为 `verified`。",
        "",
        "## 候选来源",
        "",
        "| 来源 | 抓取时间（UTC） | 原始缓存 | 证据哈希 | 状态 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for candidate in candidates:
        source = candidate["source"]
        lines.append(
            f"| {source['publisher']} | {source['retrieved_at']} | `{source['raw_path']}` | `{source.get('raw_sha256', 'N/A')[:12]}` | {candidate['status']} |"
        )

    field_names = sorted({name for candidate in candidates for name in candidate["facts"]})
    lines.extend(["", "## 候选字段与比较", "", "| 字段 | 候选值 | 已核验 profile | 比较状态 |", "| --- | --- | --- | --- |"])
    for field_name in field_names:
        values = [candidate["facts"][field_name]["value"] for candidate in candidates if field_name in candidate["facts"]]
        profile_fact = profile["facts"][field_name]
        profile_value = "N/A" if profile_fact["status"] != "verified" else "已核验值存在"
        comparison = "不足两个来源，待人工复核" if len(values) < 2 else _comparison_status(values)
        lines.append(f"| {field_name} | {' / '.join(str(value) for value in values)} | {profile_value} | {comparison} |")

    lines.extend(
        [
            "",
            "## 人工确认待办",
            "- [ ] 核对候选字段的 document title、`as_of`、费用/指数口径与原始缓存。",
            "- [ ] 增加独立第二来源（例如官方 prospectus 或监管文件）后再判断差异。",
            "- [ ] 仅在来源、截至日、证据哈希和数值已确认时，将事实状态改为 `verified`。",
        ]
    )
    return "\n".join(lines) + "\n"


def write_candidate_review(symbol: str, profile: dict[str, Any]) -> Path:
    """从本地审计库读取历史候选，写入待确认报告。"""
    candidates = list_pending_snapshots(symbol)
    if not candidates:
        raise ValueError(f"候选审计库中没有 {symbol.upper()} 的待审核快照。")
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    report_directory = PROJECT_ROOT / "reports" / "research"
    report_directory.mkdir(parents=True, exist_ok=True)
    report_path = report_directory / f"{generated_at.date().isoformat()}_{symbol.upper()}_candidate_review.md"
    report_path.write_text(build_candidate_review(profile, candidates), encoding="utf-8")
    return report_path


def _parse_ishares_facts(raw_html: str) -> dict[str, dict[str, Any]]:
    text = _plain_text(raw_html)
    facts: dict[str, dict[str, Any]] = {}
    expense_match = re.search(r"Expense Ratio.{0,300}?([0-9]+\.[0-9]+)%", text, flags=re.IGNORECASE)
    if expense_match:
        facts["expense_ratio"] = {"value": float(expense_match.group(1)), "unit": "percent", "as_of": None}
    benchmark_match = re.search(r"Benchmark Index\s*([^\n]{3,120}?)(?:Shares Outstanding|Fund Inception|Asset Class)", text)
    if benchmark_match:
        facts["benchmark"] = {"value": benchmark_match.group(1).strip(), "unit": "text", "as_of": None}
    return facts


def _plain_text(raw_html: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw_html)))


def _comparison_status(values: list[Any]) -> str:
    return "候选值一致，仍待人工确认" if len({str(value) for value in values}) == 1 else "候选值存在差异，需人工决定口径"


def _write_raw_page(symbol: str, source_url: str, fetched_at: datetime, raw_html: str) -> tuple[Path, Path]:
    directory = PROJECT_ROOT / "data" / "raw" / "etf" / PROVIDER / "us" / symbol
    directory.mkdir(parents=True, exist_ok=True)
    stem = fetched_at.strftime("%Y%m%dT%H%M%SZ")
    raw_path = directory / f"{stem}.html"
    metadata_path = directory / f"{stem}.metadata.json"
    raw_path.write_text(raw_html, encoding="utf-8")
    metadata_path.write_text(
        json.dumps(
            {"symbol": symbol, "provider": PROVIDER, "source_url": source_url, "fetched_at": fetched_at.isoformat(), "status": "pending_review", "raw_file": raw_path.name},
            ensure_ascii=False, indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return raw_path, metadata_path


def _write_candidate(symbol: str, fetched_at: datetime, candidate: dict[str, Any]) -> Path:
    directory = PROJECT_ROOT / "data" / "processed" / "etf" / "candidates" / "us" / symbol
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{fetched_at.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
