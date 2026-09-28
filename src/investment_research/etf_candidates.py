"""ETF 官方静态资料候选采集与交叉校验。

网络结果始终为 ``pending_review`` 候选，只能由人工审核后写入 ETF profile 的 verified 事实。
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from investment_research.etf_history import list_pending_snapshots, record_pending_snapshot
from investment_research.etf_profiles import PROJECT_ROOT

_MAX_RESPONSE_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class OfficialSource:
    symbol: str
    provider: str
    publisher: str
    source_id: str
    source_url: str
    allowed_host: str
    parser: Callable[[str], dict[str, dict[str, Any]]]


OFFICIAL_SOURCES: dict[str, OfficialSource] = {}


def collect_official_candidate(symbol: str, timeout_seconds: int = 20) -> dict[str, Any]:
    """抓取预配置的发行人页面，生成可追溯的待人工审核候选资料。"""
    normalized_symbol = symbol.upper()
    source = OFFICIAL_SOURCES.get(normalized_symbol)
    if source is None:
        supported = ", ".join(sorted(OFFICIAL_SOURCES))
        raise ValueError(f"暂不支持官方 ETF 候选采集：{normalized_symbol}；支持：{supported}。")

    fetched_at = datetime.now(timezone.utc).replace(microsecond=0)
    raw_html, final_url, document_title = _fetch_official_page(source, timeout_seconds)
    raw_path, metadata_path = _write_raw_page(source, fetched_at, raw_html, final_url, document_title)
    facts = source.parser(raw_html)
    if not facts:
        raise ValueError(
            f"{normalized_symbol} 官方页面未解析出支持的候选字段；已保留原始证据，请人工使用 prospectus 或 factsheet 审核。"
        )

    candidate = {
        "schema_version": 1,
        "symbol": normalized_symbol,
        "market": "us",
        "provider": source.provider,
        "source": {
            "id": source.source_id,
            "publisher": source.publisher,
            "source_type": "official_fund_page",
            "source_url": final_url,
            "document_title": document_title,
            "document_as_of": None,
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


def collect_ishares_candidate(symbol: str, timeout_seconds: int = 20) -> dict[str, Any]:
    """兼容旧入口；仅允许 iShares 标的并委托通用官方采集器。"""
    normalized_symbol = symbol.upper()
    source = OFFICIAL_SOURCES.get(normalized_symbol)
    if source is None or source.provider != "ishares":
        raise ValueError(f"暂不支持 iShares 候选采集：{normalized_symbol}。")
    return collect_official_candidate(normalized_symbol, timeout_seconds)


def build_candidate_review(profile: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    """渲染候选与已核验 profile 的差异清单，不改变 profile。"""
    symbol = profile["symbol"]
    lines = [
        f"# {symbol} ETF 候选资料交叉校验",
        "",
        "## 审核边界",
        "- 下列均为 `pending_review` 候选资料，不能自动成为结构化事实或交易依据。",
        "- 需要至少两个独立、可比来源且口径/截至日一致，才可进入人工确认；最终仍由人工将 profile 更新为 `verified`。",
        "",
        "## 候选来源",
        "",
        "| 来源 | 文档标题 | 抓取时间（UTC） | 原始缓存 | 证据哈希 | 状态 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for candidate in candidates:
        source = candidate["source"]
        lines.append(
            f"| {source['publisher']} | {source.get('document_title', 'N/A')} | {source['retrieved_at']} | "
            f"`{source['raw_path']}` | `{source.get('raw_sha256', 'N/A')[:12]}` | {candidate['status']} |"
        )

    field_names = sorted({name for candidate in candidates for name in candidate["facts"]})
    lines.extend(["", "## 候选字段与比较", "", "| 字段 | 候选值 | 已核验 profile | 比较状态 |", "| --- | --- | --- | --- |"])
    for field_name in field_names:
        values = [candidate["facts"][field_name]["value"] for candidate in candidates if field_name in candidate["facts"]]
        profile_fact = profile["facts"][field_name]
        profile_value = "N/A" if profile_fact["status"] != "verified" else "已核验值存在"
        comparison = "不足两个独立来源，待人工复核" if _unique_source_count(candidates, field_name) < 2 else _comparison_status(values)
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


class _NoRedirectHandler(HTTPRedirectHandler):
    """禁止 urllib 自动跳转，以便逐跳检查预配置发行人域。"""

    def redirect_request(self, request: Request, fp: Any, code: int, message: str, headers: Any, newurl: str) -> None:
        return None


def _fetch_official_page(source: OfficialSource, timeout_seconds: int) -> tuple[str, str, str]:
    current_url = source.source_url
    opener = build_opener(_NoRedirectHandler())
    for _ in range(4):
        _validate_source_url(current_url, source)
        request = Request(current_url, headers={"User-Agent": "investment-research/0.1 research-only"})
        try:
            response = opener.open(request, timeout=timeout_seconds)
        except HTTPError as error:
            if error.code not in {301, 302, 303, 307, 308}:
                raise
            location = error.headers.get("Location")
            if not location:
                raise ValueError("官方来源重定向缺少 Location。") from error
            current_url = urljoin(current_url, location)
            _validate_source_url(current_url, source)
            continue
        with response:
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise ValueError(f"官方来源响应类型不受支持：{content_type}")
            raw_bytes = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw_bytes) > _MAX_RESPONSE_BYTES:
            raise ValueError("官方页面超过 5 MiB 限制，未写入候选。")
        raw_html = raw_bytes.decode("utf-8", errors="replace")
        return raw_html, current_url, _document_title(raw_html)
    raise ValueError("官方来源重定向超过 3 跳限制。")


def _validate_source_url(url: str, source: OfficialSource) -> None:
    parsed_url = urlsplit(url)
    if parsed_url.scheme != "https" or parsed_url.hostname != source.allowed_host:
        raise ValueError(f"官方来源重定向至未允许地址：{url}")


def _parse_ishares_facts(raw_html: str) -> dict[str, dict[str, Any]]:
    text = _plain_text(raw_html)
    facts = _expense_ratio_fact(text)
    benchmark_match = re.search(
        r"Benchmark Index\s*([^\n]{3,120}?)(?:Shares Outstanding|Fund Inception|Asset Class)", text, flags=re.IGNORECASE
    )
    if benchmark_match:
        facts["benchmark"] = {"value": benchmark_match.group(1).strip(), "unit": "text", "as_of": None}
    return facts


def _parse_spy_facts(raw_html: str) -> dict[str, dict[str, Any]]:
    text = _plain_text(raw_html)
    facts = _expense_ratio_fact(text)
    benchmark_match = re.search(r"(?:benchmarked to|tracks? the performance of)\s+the\s+(S&P\s*500(?:®)?(?:\s+Index)?)", text, flags=re.IGNORECASE)
    if benchmark_match:
        facts["benchmark"] = {"value": _clean_index_name(benchmark_match.group(1)), "unit": "text", "as_of": None}
    return facts


def _parse_qqq_facts(raw_html: str) -> dict[str, dict[str, Any]]:
    text = _plain_text(raw_html)
    facts = _expense_ratio_fact(text)
    benchmark_match = re.search(r"(Nasdaq[-\s]?100(?:®)?(?:\s+Index)?)", text, flags=re.IGNORECASE)
    if benchmark_match:
        facts["benchmark"] = {"value": _clean_index_name(benchmark_match.group(1)), "unit": "text", "as_of": None}
    return facts


def _parse_gld_facts(raw_html: str) -> dict[str, dict[str, Any]]:
    text = _plain_text(raw_html)
    facts = _expense_ratio_fact(text)
    objective_match = re.search(r"investment objective.*?reflect the performance of\s+(the price of gold bullion)", text, flags=re.IGNORECASE)
    if objective_match:
        facts["benchmark"] = {"value": objective_match.group(1).strip(), "unit": "text", "as_of": None}
    return facts


def _expense_ratio_fact(text: str) -> dict[str, dict[str, Any]]:
    match = re.search(r"(?:Gross\s+)?Expense Ratio(?:.{0,300}?)([0-9]+(?:\.[0-9]+)?)%", text, flags=re.IGNORECASE)
    if not match:
        return {}
    value = float(match.group(1))
    if not 0 <= value <= 10:
        return {}
    return {"expense_ratio": {"value": value, "unit": "percent", "as_of": None}}


def _clean_index_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("®", "")).strip()


def _plain_text(raw_html: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw_html)))


def _document_title(raw_html: str) -> str:
    match = re.search(r"<title[^>]*>\s*(.*?)\s*</title>", raw_html, flags=re.IGNORECASE | re.DOTALL)
    return _plain_text(match.group(1)) if match else "N/A"


def _unique_source_count(candidates: list[dict[str, Any]], field_name: str) -> int:
    return len(
        {
            _source_document_key(candidate["source"])
            for candidate in candidates
            if field_name in candidate["facts"]
        }
    )


def _source_document_key(source: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(source["publisher"]),
        str(source["source_type"]),
        _canonical_source_url(str(source["source_url"])),
    )


def _canonical_source_url(url: str) -> str:
    parsed_url = urlsplit(url)
    path = parsed_url.path.rstrip("/") or "/"
    return urlunsplit((parsed_url.scheme.lower(), (parsed_url.hostname or "").lower(), path, "", ""))


def _comparison_status(values: list[Any]) -> str:
    return "候选值一致，仍待人工确认" if len({str(value) for value in values}) == 1 else "候选值存在差异，需人工决定口径"


def _write_raw_page(source: OfficialSource, fetched_at: datetime, raw_html: str, final_url: str, document_title: str) -> tuple[Path, Path]:
    directory = PROJECT_ROOT / "data" / "raw" / "etf" / source.provider / "us" / source.symbol
    directory.mkdir(parents=True, exist_ok=True)
    stem = fetched_at.strftime("%Y%m%dT%H%M%SZ")
    raw_path = directory / f"{stem}.html"
    metadata_path = directory / f"{stem}.metadata.json"
    raw_path.write_text(raw_html, encoding="utf-8")
    metadata_path.write_text(
        json.dumps(
            {
                "symbol": source.symbol,
                "provider": source.provider,
                "source_url": final_url,
                "fetched_at": fetched_at.isoformat(),
                "document_title": document_title,
                "raw_sha256": hashlib.sha256(raw_html.encode("utf-8")).hexdigest(),
                "status": "pending_review",
                "raw_file": raw_path.name,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return raw_path, metadata_path


def _write_candidate(symbol: str, fetched_at: datetime, candidate: dict[str, Any]) -> Path:
    directory = PROJECT_ROOT / "data" / "processed" / "etf" / "candidates" / "us" / symbol
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{fetched_at.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


OFFICIAL_SOURCES.update(
    {
        "SPY": OfficialSource("SPY", "ssga", "State Street Global Advisors", "ssga_spy_fund_page", "https://www.ssga.com/us/en/intermediary/capabilities/spdr-core-equity-etfs/spy-sp-500", "www.ssga.com", _parse_spy_facts),
        "QQQ": OfficialSource("QQQ", "invesco", "Invesco", "invesco_qqq_fund_page", "https://www.invesco.com/qqq-etf/en/home.html", "www.invesco.com", _parse_qqq_facts),
        "IWM": OfficialSource("IWM", "ishares", "iShares by BlackRock", "ishares_iwm_fund_page", "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf", "www.ishares.com", _parse_ishares_facts),
        "TLT": OfficialSource("TLT", "ishares", "iShares by BlackRock", "ishares_tlt_fund_page", "https://www.ishares.com/us/products/239454/ishares-20-year-treasury-bond-etf", "www.ishares.com", _parse_ishares_facts),
        "GLD": OfficialSource("GLD", "ssga", "State Street Global Advisors", "ssga_gld_fund_page", "https://www.ssga.com/us/en/intermediary/etfs/spdr-gold-shares-gld", "www.ssga.com", _parse_gld_facts),
    }
)
