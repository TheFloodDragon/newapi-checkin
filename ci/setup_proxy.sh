#!/usr/bin/env bash
# 启动本地 mihomo(Clash) 代理，供部分站点过阿里云 WAF 使用。
#
# 用法（CI）：把完整 mihomo/Clash 配置文件内容放进 Secret CLASH_CONFIG，
# 在签到前执行本脚本。脚本会：
#   1. 未设置 CLASH_CONFIG -> 打印跳过并 exit 0（不影响直连站点）。
#   2. 设置了 -> 下载 mihomo、写 config.yaml（剥离用户配置里的顶层端口/控制器 key，
#      再强制注入 mixed-port=7897、关闭 external-controller）、后台启动并做健康检查。
#
# 站点侧：在 ACCOUNTS.json 里给需要走代理的站点填 "proxy": "http://127.0.0.1:7897"。
# 其它站点留空即直连。
#
# 环境变量：
#   CLASH_CONFIG    完整 mihomo 配置文件内容（含 proxies/proxy-groups/rules）。
#   PROXY_REQUIRED  设为 true 时，代理起不来则 exit 1；否则仅告警并跳过（默认 false）。
#   MIHOMO_VERSION  可选，覆盖回退版本（默认 v1.19.28）。

set -euo pipefail

# ---- 常量 ----
PROXY_PORT=7897
WORK_DIR="${RUNNER_TEMP:-/tmp}/mihomo"
CONFIG_FILE="${WORK_DIR}/config.yaml"
BIN_FILE="${WORK_DIR}/mihomo"
PID_FILE="${WORK_DIR}/mihomo.pid"
LOG_FILE="${WORK_DIR}/mihomo.log"
FALLBACK_VERSION="${MIHOMO_VERSION:-v1.19.28}"
PROXY_REQUIRED="${PROXY_REQUIRED:-false}"

log() { printf '[setup_proxy] %s\n' "$*"; }

# 代理起不来时的统一处理：required 则失败，否则跳过。
give_up() {
  local msg="$1"
  if [ "${PROXY_REQUIRED}" = "true" ]; then
    log "❌ ${msg}（PROXY_REQUIRED=true，终止）"
    [ -f "${LOG_FILE}" ] && { log "---- mihomo.log 尾部 ----"; tail -n 40 "${LOG_FILE}" || true; }
    exit 1
  fi
  log "⚠️  ${msg}（PROXY_REQUIRED!=true，跳过代理，站点将直连）"
  exit 0
}

# ---- 1. 未配置则跳过 ----
if [ -z "${CLASH_CONFIG:-}" ]; then
  log "未设置 Secret CLASH_CONFIG，跳过代理启动（站点直连）。"
  exit 0
fi

mkdir -p "${WORK_DIR}"

# ---- 2. 下载 mihomo ----
detect_asset() {
  # mihomo release 资产名形如 mihomo-linux-amd64-v1.19.28.gz
  local arch
  arch="$(uname -m)"
  case "${arch}" in
    x86_64|amd64) echo "linux-amd64" ;;
    aarch64|arm64) echo "linux-arm64" ;;
    *) echo "linux-amd64" ;;  # 默认 amd64
  esac
}

resolve_version() {
  # 取最新 release tag，失败则回退固定版本。
  local v=""
  v="$(curl -fsSL --max-time 15 \
        https://api.github.com/repos/MetaCubeX/mihomo/releases/latest 2>/dev/null \
        | grep -o '"tag_name": *"[^"]*"' | head -n1 | sed 's/.*"tag_name": *"\([^"]*\)".*/\1/')" || true
  if [ -z "${v}" ]; then
    v="${FALLBACK_VERSION}"
    log "获取最新版本失败，回退到 ${v}"
  fi
  echo "${v}"
}

if [ ! -x "${BIN_FILE}" ]; then
  ASSET="$(detect_asset)"
  VERSION="$(resolve_version)"
  URL="https://github.com/MetaCubeX/mihomo/releases/download/${VERSION}/mihomo-${ASSET}-${VERSION}.gz"
  log "下载 mihomo ${VERSION} (${ASSET})..."
  if ! curl -fsSL --max-time 120 -o "${BIN_FILE}.gz" "${URL}"; then
    give_up "mihomo 下载失败: ${URL}"
  fi
  gunzip -f "${BIN_FILE}.gz" || give_up "mihomo 解压失败"
  chmod +x "${BIN_FILE}"
fi

# ---- 3. 写 config.yaml 并强制端口约定 ----
# mihomo rejects duplicate top-level keys, so we cannot just append overrides.
# Strip any top-level (no-indent) copies of the keys we force, then append ours.
# Only lines with no leading whitespace are removed, so nested/indented keys of
# the same name (inside proxies/rules/etc.) are preserved untouched.
STRIP_KEYS='mixed-port|port|socks-port|redir-port|tproxy-port|allow-lan|bind-address|external-controller'
printf '%s\n' "${CLASH_CONFIG}" | sed -E "/^(${STRIP_KEYS})[[:space:]]*:/d" > "${CONFIG_FILE}"
{
  echo ""
  echo "# ---- forced by setup_proxy.sh (top-level overrides) ----"
  echo "mixed-port: ${PROXY_PORT}"
  echo "allow-lan: false"
  echo "bind-address: '127.0.0.1'"
  echo "external-controller: ''"
} >> "${CONFIG_FILE}"

# ---- 4. 校验配置 ----
if ! "${BIN_FILE}" -t -d "${WORK_DIR}" -f "${CONFIG_FILE}" >/dev/null 2>&1; then
  log "配置校验未通过，输出详情："
  "${BIN_FILE}" -t -d "${WORK_DIR}" -f "${CONFIG_FILE}" 2>&1 | tail -n 30 || true
  give_up "CLASH_CONFIG 配置校验失败"
fi

# ---- 5. 后台启动 ----
nohup "${BIN_FILE}" -d "${WORK_DIR}" -f "${CONFIG_FILE}" > "${LOG_FILE}" 2>&1 &
echo $! > "${PID_FILE}"
log "mihomo 已启动 (pid=$(cat "${PID_FILE}"))，端口 ${PROXY_PORT}"

# ---- 6. 健康检查 ----
log "健康检查中（最多 ~90s）..."
OK=false
for i in $(seq 1 30); do
  if curl -fsS --max-time 8 -x "http://127.0.0.1:${PROXY_PORT}" \
       -o /dev/null https://www.gstatic.com/generate_204 2>/dev/null; then
    OK=true
    break
  fi
  # 进程若已退出，提前结束等待
  if [ -f "${PID_FILE}" ] && ! kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
    log "mihomo 进程已退出"
    break
  fi
  sleep 3
done

if [ "${OK}" = "true" ]; then
  log "✅ 代理就绪：http://127.0.0.1:${PROXY_PORT}"
  exit 0
fi

log "健康检查失败，mihomo.log 尾部："
tail -n 40 "${LOG_FILE}" 2>/dev/null || true
give_up "代理健康检查失败"
