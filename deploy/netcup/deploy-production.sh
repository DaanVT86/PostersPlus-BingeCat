#!/usr/bin/env bash
set -Eeuo pipefail

readonly STACK_ROOT="${POSTERSPLUS_STACK_ROOT:-/opt/postersplus}"
readonly REPO_ROOT="${POSTERSPLUS_REPO_ROOT:-${STACK_ROOT}/repo}"
readonly COMPOSE_FILE="${POSTERSPLUS_COMPOSE_FILE:-${REPO_ROOT}/compose.production.yaml}"
readonly CORE_ENV_FILE="${POSTERSPLUS_PRODUCTION_CORE_ENV_FILE:-${STACK_ROOT}/env/postersplus.env}"
readonly PRIVATE_ENV_FILE="${POSTERSPLUS_PRODUCTION_PRIVATE_ENV_FILE:-${STACK_ROOT}/env/postersplus-v2-private.env}"
readonly CACHE_DIR="${POSTERSPLUS_PRODUCTION_CACHE_DIR:-${STACK_ROOT}/cache}"
readonly HEALTH_PORT="${POSTERSPLUS_PRODUCTION_HEALTH_PORT:-18084}"
readonly NETWORK_ALIAS="${POSTERSPLUS_PRODUCTION_NETWORK_ALIAS:-postersplus-v2-core}"
readonly DEPLOY_REF="${1:-origin/dev}"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "missing command: $1"
}

require_command git
require_command curl
require_command sudo

[[ "${POSTERSPLUS_DEPLOY:-0}" == "1" ]] \
  || die "set POSTERSPLUS_DEPLOY=1 to confirm this Core-only deployment"
[[ -d "${REPO_ROOT}/.git" ]] || die "missing PosterPlus checkout: ${REPO_ROOT}"
[[ "${HEALTH_PORT}" =~ ^[0-9]+$ ]] || die "health port must be numeric"
(( HEALTH_PORT >= 1024 && HEALTH_PORT <= 65535 )) || die "health port is outside the unprivileged range"
[[ "${NETWORK_ALIAS}" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die "network alias is invalid"
[[ "${NETWORK_ALIAS}" != "postersplus-v2-private" ]] || die "pre-go deploy may not claim BingeCat's configured live alias"

[[ -z "$(git -C "${REPO_ROOT}" status --porcelain --untracked-files=normal)" ]] \
  || die "production checkout is not clean"
git -C "${REPO_ROOT}" fetch --prune origin
target_sha="$(git -C "${REPO_ROOT}" rev-parse --verify "${DEPLOY_REF}^{commit}")"
[[ -n "${target_sha}" ]] || die "deploy ref did not resolve"
git -C "${REPO_ROOT}" checkout --detach "${target_sha}"

[[ -f "${COMPOSE_FILE}" ]] || die "missing production compose file"
sudo -n test -r "${CORE_ENV_FILE}" || die "core env is unavailable"
sudo -n test -r "${PRIVATE_ENV_FILE}" || die "private env is unavailable"
sudo -n install -d -m 0750 -o "$(id -un)" -g "$(id -gn)" "${CACHE_DIR}"
sudo -n docker network inspect aicat-app-internal >/dev/null 2>&1 \
  || die "external network aicat-app-internal is unavailable"

sudo -n awk -F= '
  /^[[:space:]]*(#|$)/ { next }
  {
    key=$1
    gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
    value=substr($0, index($0, "=") + 1)
    gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
    if (key == "TMDB_API_KEY" && value != "") tmdb=1
    if (key == "MDBLIST_API_KEY" && value != "") mdblist=1
    if (key == "POSTERSPLUS_BINGECAT_REQUEST_SECRET" && value != "") secret=1
  }
  END { exit !(tmdb && mdblist && secret) }
' "${CORE_ENV_FILE}" "${PRIVATE_ENV_FILE}" \
  || die "required Core keys are missing"

compose=(
  sudo -n env
  "POSTERSPLUS_PRODUCTION_CORE_ENV_FILE=${CORE_ENV_FILE}"
  "POSTERSPLUS_PRODUCTION_PRIVATE_ENV_FILE=${PRIVATE_ENV_FILE}"
  "POSTERSPLUS_PRODUCTION_CACHE_DIR=${CACHE_DIR}"
  "POSTERSPLUS_PRODUCTION_HEALTH_PORT=${HEALTH_PORT}"
  "POSTERSPLUS_PRODUCTION_NETWORK_ALIAS=${NETWORK_ALIAS}"
  "POSTERSPLUS_PRODUCTION_IMAGE=postersplus-bingecat:v2-${target_sha}"
  docker compose --project-directory "${REPO_ROOT}" -f "${COMPOSE_FILE}"
)
"${compose[@]}" config --quiet
"${compose[@]}" build --pull app
"${compose[@]}" up -d --no-deps app

deadline=$((SECONDS + 180))
while (( SECONDS < deadline )); do
  status="$(sudo -n docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' postersplus-v2-core 2>/dev/null || true)"
  if [[ "${status}" == "healthy" ]]; then
    break
  fi
  if [[ "${status}" == "unhealthy" || "${status}" == "exited" || "${status}" == "dead" ]]; then
    sudo -n docker logs --tail 80 postersplus-v2-core >&2 || true
    die "PosterPlus Core entered state ${status}"
  fi
  sleep 2
done
[[ "${status:-}" == "healthy" ]] || die "PosterPlus Core health timed out"

curl --fail --silent --show-error --max-time 10 \
  "http://127.0.0.1:${HEALTH_PORT}/health" >/dev/null

attached_aliases="$(sudo -n docker inspect --format '{{range $name, $network := .NetworkSettings.Networks}}{{range $network.Aliases}}{{println .}}{{end}}{{end}}' postersplus-v2-core)"
grep -Fxq "${NETWORK_ALIAS}" <<<"${attached_aliases}" \
  || die "private network alias was not attached"
if grep -Fxq 'postersplus-v2-private' <<<"${attached_aliases}"; then
  die "pre-go container unexpectedly claimed the configured live alias"
fi

printf 'PosterPlus Core ready sha=%s alias=%s health_port=%s\n' \
  "${target_sha}" "${NETWORK_ALIAS}" "${HEALTH_PORT}"
