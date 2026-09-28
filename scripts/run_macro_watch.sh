#!/usr/bin/env bash
# 宏观发布监视：抓官方新闻源 → 识别发布公告 → 同步到期实际值。
# 供手动执行或 launchd 调用；不创建虚拟环境、不安装依赖、不下单。
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi

export PYTHONPATH="${PROJECT_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] 抓取官方新闻源"
"${PYTHON_BIN}" -m investment_research.cli news-feeds fetch

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] 识别宏观发布公告"
"${PYTHON_BIN}" -m investment_research.cli macro-watch detect

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] 同步到期实际值"
"${PYTHON_BIN}" -m investment_research.cli macro-release sync-actuals
