#!/usr/bin/env bash
# 安装或卸载本项目的 macOS launchd 定时任务。
#
# 这些任务只做只读数据采集与本地写库，不发送提醒、不连接券商、不下单。
# 安装会在 ~/Library/LaunchAgents 写入用户级 plist；可用 uninstall 完全撤销。
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_DIRECTORY="${HOME}/Library/LaunchAgents"
TEMPLATE_DIRECTORY="${PROJECT_ROOT}/ops/launchd"
LABELS=(
  "com.user.investment-research.daily-review"
  "com.user.investment-research.macro-watch"
)

usage() {
  echo "用法：$0 [install|uninstall|status]"
  exit 64
}

action="${1:-status}"

case "${action}" in
  install)
    mkdir -p "${AGENT_DIRECTORY}" "${PROJECT_ROOT}/logs"
    for label in "${LABELS[@]}"; do
      template="${TEMPLATE_DIRECTORY}/${label}.plist.template"
      target="${AGENT_DIRECTORY}/${label}.plist"
      if [[ ! -f "${template}" ]]; then
        echo "缺少模板：${template}" >&2
        exit 1
      fi
      sed "s|__PROJECT_ROOT__|${PROJECT_ROOT}|g" "${template}" > "${target}"
      plutil -lint "${target}" >/dev/null
      launchctl unload "${target}" 2>/dev/null || true
      launchctl load -w "${target}" 2>/dev/null || true
      if launchctl list "${label}" >/dev/null 2>&1; then
        echo "已安装并注册：${label}"
      else
        echo "安装失败，未注册：${label}" >&2
        exit 1
      fi
    done
    ;;
  uninstall)
    for label in "${LABELS[@]}"; do
      target="${AGENT_DIRECTORY}/${label}.plist"
      if [[ -f "${target}" ]]; then
        launchctl unload "${target}" 2>/dev/null || true
        rm -f "${target}"
        echo "已卸载：${label}"
      fi
    done
    ;;
  status)
    for label in "${LABELS[@]}"; do
      target="${AGENT_DIRECTORY}/${label}.plist"
      if launchctl list "${label}" >/dev/null 2>&1; then
        echo "已注册：${label}"
      elif [[ -f "${target}" ]]; then
        echo "plist 已存在但未注册：${label}"
      else
        echo "未安装：${label}"
      fi
    done
    ;;
  *)
    usage
    ;;
esac
