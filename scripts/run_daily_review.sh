#!/usr/bin/env bash
# 供手动执行或 launchd 调用。不会自动创建虚拟环境或安装依赖。
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi

export PYTHONPATH="${PROJECT_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
exec "${PYTHON_BIN}" -m investment_research.cli daily-review
