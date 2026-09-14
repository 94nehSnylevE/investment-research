"""命令行入口。"""

from __future__ import annotations

import argparse
from pathlib import Path

from investment_research.daily_review import DEFAULT_WATCHLIST, run_daily_review
from investment_research.etf_candidates import collect_ishares_candidate, write_candidate_review
from investment_research.etf_profiles import generate_research_template, load_etf_profiles


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

    candidate_review = subparsers.add_parser("etf-candidate-review", help="采集 ETF 待审核候选资料")
    candidate_review.add_argument("--symbol", required=True, help="当前支持 IWM 或 TLT")
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
        candidate = collect_ishares_candidate(symbol)
        profile = load_etf_profiles().get(symbol)
        if profile is None:
            raise ValueError(f"未找到 ETF profile：{symbol}。")
        report_path = write_candidate_review(symbol, profile)
        print(f"已生成待审核候选报告：{report_path}")


if __name__ == "__main__":
    main()
