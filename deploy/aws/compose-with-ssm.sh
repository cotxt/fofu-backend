#!/usr/bin/env bash
set -Eeuo pipefail

# Keep decrypted values out of the repository and filesystem. Docker stores
# container environment values in its root-only metadata, so access to the
# Docker socket must remain restricted to administrators.

readonly DEPLOY_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly CONFIG_FILE="${FOFU_CONFIG_FILE:-/etc/fofu/production.env}"
readonly COMPOSE_FILE="${FOFU_COMPOSE_FILE:-${DEPLOY_DIR}/compose.production.yaml}"
readonly DATABASE_PARAMETER="${FOFU_DATABASE_URL_PARAMETER:-/fofu/production/database-url}"
readonly JWT_PARAMETER="${FOFU_JWT_SECRET_PARAMETER:-/fofu/production/jwt-secret}"
readonly REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-ap-northeast-2}}"

if (( $# == 0 )); then
  echo "usage: $0 <docker compose arguments...>" >&2
  exit 64
fi

if [[ ! -r "${CONFIG_FILE}" ]]; then
  echo "configuration file is not readable: ${CONFIG_FILE}" >&2
  exit 66
fi

if [[ ! -r "${COMPOSE_FILE}" ]]; then
  echo "Compose file is not readable: ${COMPOSE_FILE}" >&2
  exit 66
fi

for required_command in aws docker; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    echo "required command is missing: ${required_command}" >&2
    exit 69
  fi
done

read_parameter() {
  local parameter_name="$1"
  local attempt value
  for attempt in 1 2 3 4 5; do
    if value="$(
      aws ssm get-parameter \
        --region "${REGION}" \
        --name "${parameter_name}" \
        --with-decryption \
        --query 'Parameter.Value' \
        --output text
    )"; then
      printf '%s' "${value}"
      return 0
    fi
    if (( attempt < 5 )); then
      sleep $((attempt * 2))
    fi
  done
  return 1
}

needs_runtime_secrets=false
case "$1" in
  up | create | run)
    needs_runtime_secrets=true
    ;;
  config)
    if [[ " $* " != *" --quiet "* ]]; then
      echo "refusing to render Compose configuration because it can expose secrets; use config --quiet" >&2
      exit 64
    fi
    ;;
esac

if [[ "${needs_runtime_secrets}" == true ]] && grep -q 'example\.com' "${CONFIG_FILE}"; then
  echo "replace every example.com placeholder in ${CONFIG_FILE} before starting Fofu" >&2
  exit 78
fi

if [[ "${needs_runtime_secrets}" == true ]]; then
  database_url="$(read_parameter "${DATABASE_PARAMETER}")"
  jwt_secret="$(read_parameter "${JWT_PARAMETER}")"
else
  # Compose interpolates required variables even for commands that do not create
  # containers. Harmless placeholders keep ps/logs/exec/down independent of SSM.
  database_url="postgresql://unused:unused@127.0.0.1/unused?sslmode=require"
  jwt_secret="unused-runtime-secret-0000000000000000"
fi

if [[ -z "${database_url}" || "${database_url}" == "None" ]]; then
  echo "SSM parameter is empty: ${DATABASE_PARAMETER}" >&2
  exit 78
fi

if [[ -z "${jwt_secret}" || "${jwt_secret}" == "None" ]]; then
  echo "SSM parameter is empty: ${JWT_PARAMETER}" >&2
  exit 78
fi

if [[ "${database_url}" == *$'\n'* || "${database_url}" == *$'\r'* \
  || "${jwt_secret}" == *$'\n'* || "${jwt_secret}" == *$'\r'* ]]; then
  echo "SSM values must be single-line strings" >&2
  exit 78
fi

if (( ${#jwt_secret} < 32 )); then
  echo "FOFU_JWT_SECRET must contain at least 32 characters" >&2
  exit 78
fi

if [[ "${needs_runtime_secrets}" == true ]]; then
  if [[ "${database_url}" != postgresql://* && "${database_url}" != postgresql+* ]]; then
    echo "FOFU_DATABASE_URL must be a PostgreSQL URL" >&2
    exit 78
  fi
  if [[ "${database_url}" != *"sslmode=require"* \
    && "${database_url}" != *"sslmode=verify-ca"* \
    && "${database_url}" != *"sslmode=verify-full"* ]]; then
    echo "FOFU_DATABASE_URL must require PostgreSQL TLS with sslmode" >&2
    exit 78
  fi
fi

export FOFU_DATABASE_URL="${database_url}"
export FOFU_JWT_SECRET="${jwt_secret}"

exec docker compose \
  --env-file "${CONFIG_FILE}" \
  --file "${COMPOSE_FILE}" \
  "$@"
