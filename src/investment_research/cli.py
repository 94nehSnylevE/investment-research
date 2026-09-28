"""命令行入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from investment_research.daily_review import DEFAULT_WATCHLIST, run_daily_review
from investment_research.etf_candidates import collect_official_candidate, write_candidate_review
from investment_research.etf_profiles import generate_research_template, load_etf_profiles
from investment_research.macro_releases import list_release_summaries, record_expectation, sync_due_actuals


def build_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="个人投研工作台")
    subparsers = parser.add_subparsers(dest="command", required=True)

    daily_review = subparsers.add_parser("daily-review", help="生成每日研究报告")
    daily_review.add_argument("--config", type=Path, default=DEFAULT_WATCHLIST, help="观察列表 JSON 路径")
    daily_review.add_argument("--dry-run", action="store_true", help="只校验配置并打印报告，不写入文件")

    etf_template = subparsers.add_parser("etf-research-template", help="生成 ETF 研究模板")
    etf_template.add_argument("--symbol", required=True, help="ETF 代码，例如 SPY")
    etf_template.add_argument("--dry-run", action="store_true", help="只渲染模板，不写入文件")

    candidate_review = subparsers.add_parser("etf-candidate-review", help="采集 ETF 官方待审核候选资料")
    candidate_review.add_argument("--symbol", required=True, help="支持 SPY、QQQ、IWM、TLT 或 GLD")

    macro_release = subparsers.add_parser("macro-release", help="管理宏观发布前预期与发布后实际值")
    macro_actions = macro_release.add_subparsers(dest="macro_action", required=True)
    macro_expect = macro_actions.add_parser("expect", help="在发布时间前追加人工预期快照")
    macro_expect.add_argument("--event", required=True, help="事件键，例如 cpi")
    macro_expect.add_argument("--metric", required=True, help="指标键，例如 cpi_mom_sa")
    macro_expect.add_argument("--period", required=True, help="参考期 YYYY-MM")
    macro_expect.add_argument("--scheduled-at", required=True, help="带时区的发布时间，例如 2026-10-13T08:30:00-04:00")
    macro_expect.add_argument("--forecast", required=True, help="市场预期值")
    macro_expect.add_argument("--previous", help="页面显示的前值")
    macro_expect.add_argument("--source-label", required=True, help="人工来源名称，例如 金十数据")
    macro_expect.add_argument("--source-url", required=True, help="包含发布时间和预期值的来源页面 URL")
    macro_expect.add_argument("--evidence", type=Path, help="项目目录内的截图或导出文件")
    macro_expect.add_argument("--note", help="口径或人工核验备注")
    macro_sync = macro_actions.add_parser("sync-actuals", help="为已发布实例同步 FRED 实际值/修订")
    macro_sync.add_argument("--release-id", help="只同步指定发布实例；默认同步全部到期实例")
    macro_list = macro_actions.add_parser("list", help="只读列出预期、实际、预期差与修订")
    macro_list.add_argument("--limit", type=int, default=20, help="最大行数")
    return parser


def main() -> None:
    """执行命令。"""
    arguments = build_parser().parse_args()
    if arguments.command == "daily-review":
        run_daily_review(arguments.config, arguments.dry_run)
    elif arguments.command == "etf-research-template":
        output_path = generate_research_template(arguments.symbol, dry_run=arguments.dry_run)
        if output_path is not None:
            print(f"已生成 ETF 研究模板：{output_path}")
    elif arguments.command == "etf-candidate-review":
        symbol = arguments.symbol.upper()
        profile = load_etf_profiles().get(symbol)
        if profile is None:
            raise ValueError(f"未找到 ETF profile：{symbol}。")
        candidate = collect_official_candidate(symbol)
        report_path = write_candidate_review(symbol, profile)
        print(f"已生成待审核候选报告：{report_path}")
    elif arguments.command == "macro-release":
        if arguments.macro_action == "expect":
            result = record_expectation(
                event_key=arguments.event,
                metric_key=arguments.metric,
                reference_period=arguments.period,
                scheduled_at=arguments.scheduled_at,
                expected_value=arguments.forecast,
                previous_value=arguments.previous,
                source_label=arguments.source_label,
                source_url=arguments.source_url,
                evidence_path=arguments.evidence,
                note=arguments.note,
            )
            print(json.dumps(result, ensure_ascii=False))
        elif arguments.macro_action == "sync-actuals":
            print(json.dumps(sync_due_actuals(arguments.release_id), ensure_ascii=False, indent=2))
        elif arguments.macro_action == "list":
            print(json.dumps(list_release_summaries(limit=arguments.limit), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
