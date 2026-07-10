#!/usr/bin/env bash
# 停止由 setup_proxy.sh 启动的 mihomo(Clash) 代理。
# 读 pid 文件优雅 kill；无 pid 文件则静默跳过。CI 里用 if: always() 调用。

set -uo pipefail

WORK_DIR="${RUNNER_TEMP:-/tmp}/mihomo"
PID_FILE="${WORK_DIR}/mihomo.pid"

log() { printf '[stop_proxy] %s\n' "$*"; }

if [ ! -f "${PID_FILE}" ]; then
  log "无 pid 文件，代理未启动或已停止，跳过。"
  exit 0
fi

PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
if [ -z "${PID}" ]; then
  log "pid 文件为空，跳过。"
  rm -f "${PID_FILE}"
  exit 0
fi

if kill -0 "${PID}" 2>/dev/null; then
  kill "${PID}" 2>/dev/null || true
  # 等待优雅退出，最多 ~5s
  for _ in $(seq 1 5); do
    kill -0 "${PID}" 2>/dev/null || break
    sleep 1
  done
  # 仍存活则强杀
  if kill -0 "${PID}" 2>/dev/null; then
    kill -9 "${PID}" 2>/dev/null || true
    log "已强制停止 mihomo (pid=${PID})"
  else
    log "已停止 mihomo (pid=${PID})"
  fi
else
  log "进程 (pid=${PID}) 不存在，可能已退出。"
fi

rm -f "${PID_FILE}"
exit 0
