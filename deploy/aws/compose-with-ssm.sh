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
  aws ssm get-parameter \
    --region "${REGION}" \
    --name "${parameter_name}" \
    --with-decryption \
    --query 'Parameter.Value' \
    --output text
}

database_url="$(read_parameter "${DATABASE_PARAMETER}")"
jwt_secret="$(read_parameter "${JWT_PARAMETER}")"

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

export FOFU_DATABASE_URL="${database_url}"
export FOFU_JWT_SECRET="${jwt_secret}"

exec docker compose \
  --env-file "${CONFIG_FILE}" \
  --file "${COMPOSE_FILE}" \
  "$@"
