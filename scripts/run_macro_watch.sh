#!/usr/bin/env bash
# 宏观发布监视：抓官方新闻源 → 识别发布公告 → 同步到期实际值。
#
# 发布日感知加密轮询：先检查纽约时间今天是否有已用 `macro-release expect`
# 录入过预期的发布实例。命中时，在本次调用内以 MACRO_WATCH_DENSE_INTERVAL_SECONDS
# （默认 300 秒，即 5 分钟）为间隔连续轮询，最长持续
# MACRO_WATCH_DENSE_DURATION_SECONDS（默认 5400 秒，即 90 分钟）；
# 未命中则只运行一次，交给 launchd 现有的稀疏时点。
#
# 供手动执行或 launchd 调用；不创建虚拟环境、不安装依赖、不下单。
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi

export PYTHONPATH="${PROJECT_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

DENSE_INTERVAL_SECONDS="${MACRO_WATCH_DENSE_INTERVAL_SECONDS:-300}"
DENSE_DURATION_SECONDS="${MACRO_WATCH_DENSE_DURATION_SECONDS:-5400}"

run_once() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] 抓取官方新闻源"
  "${PYTHON_BIN}" -m investment_research.cli news-feeds fetch

  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] 识别宏观发布公告"
  "${PYTHON_BIN}" -m investment_research.cli macro-watch detect

  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] 同步到期实际值"
  "${PYTHON_BIN}" -m investment_research.cli macro-release sync-actuals
}

if "${PYTHON_BIN}" -m investment_research.cli macro-release today > /tmp/macro_watch_today.json 2>/dev/null; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] 检测到今天有已录入预期的发布实例；启动加密轮询"
  cat /tmp/macro_watch_today.json
  deadline=$(( $(date -u +%s) + DENSE_DURATION_SECONDS ))
  while true; do
    run_once
    now=$(date -u +%s)
    if (( now >= deadline )); then
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] 加密轮询窗口结束"
      break
    fi
    sleep "${DENSE_INTERVAL_SECONDS}"
  done
else
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] 今天没有已录入预期的发布实例；按常规频率单次运行"
  run_once
fi
