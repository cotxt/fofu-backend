#!/usr/bin/env bash
set -Eeuo pipefail

# Configure the non-secret production settings only after the public hostname
# resolves to this instance's Elastic IP, then start and verify Fofu.

readonly REPOSITORY_DIR="/opt/fofu"
readonly CONFIG_DIR="/etc/fofu"
readonly CONFIG_FILE="${CONFIG_DIR}/production.env"
readonly METADATA_URL="http://169.254.169.254/latest"

usage() {
  echo "usage: configure-and-start-host.sh <api-hostname> <tls-email>" >&2
}

if (( EUID != 0 )); then
  echo "run this script as root" >&2
  exit 77
fi
if (( $# != 2 )); then
  usage
  exit 64
fi

readonly API_HOSTNAME="$1"
readonly TLS_EMAIL="$2"

for required_command in awk cat chmod chown curl getent install mktemp mv \
  python3 rm sleep sort systemctl; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    echo "required command is missing: ${required_command}" >&2
    exit 69
  fi
done

if [[ ! "${API_HOSTNAME}" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ \
  || "${API_HOSTNAME}" != *.* \
  || "${API_HOSTNAME}" == *..* \
  || "${API_HOSTNAME}" == *://* \
  || "${API_HOSTNAME}" == */* \
  || ${#API_HOSTNAME} -gt 253 ]]; then
  echo "invalid API hostname: ${API_HOSTNAME}" >&2
  exit 78
fi
if [[ ! "${TLS_EMAIL}" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; then
  echo "invalid TLS contact email" >&2
  exit 78
fi
if [[ ! -f /var/lib/fofu/bootstrap-complete ]]; then
  echo "host bootstrap has not completed" >&2
  exit 69
fi
if [[ ! -x "${REPOSITORY_DIR}/deploy/aws/compose-with-ssm.sh" ]]; then
  echo "Fofu deployment wrapper is missing" >&2
  exit 66
fi

metadata_token="$(
  curl \
    --fail \
    --show-error \
    --silent \
    --request PUT \
    --header 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
    "${METADATA_URL}/api/token"
)"
public_ip="$(
  curl \
    --fail \
    --show-error \
    --silent \
    --header "X-aws-ec2-metadata-token: ${metadata_token}" \
    "${METADATA_URL}/meta-data/public-ipv4"
)"
if [[ ! "${public_ip}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  echo "could not determine the instance public IPv4 address" >&2
  exit 69
fi

resolve_ipv4_addresses() {
  local lookup_output

  if ! lookup_output="$(getent ahostsv4 "${API_HOSTNAME}" 2>/dev/null)"; then
    return 0
  fi
  awk '{print $1}' <<<"${lookup_output}" | LC_ALL=C sort -u
}

resolve_ipv6_addresses() {
  python3 - "${API_HOSTNAME}" <<'PY'
import socket
import sys

hostname = sys.argv[1]
try:
    answers = socket.getaddrinfo(
        hostname,
        None,
        family=socket.AF_INET6,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
        flags=0,
    )
except socket.gaierror as error:
    no_answer_errors = {socket.EAI_NONAME}
    if hasattr(socket, "EAI_NODATA"):
        no_answer_errors.add(socket.EAI_NODATA)
    if error.errno in no_answer_errors:
        raise SystemExit(0)
    raise SystemExit(1)

for address in sorted({answer[4][0].split("%", 1)[0] for answer in answers}):
    print(address)
PY
}

hostname_ready=false
for ((attempt = 1; attempt <= 40; attempt++)); do
  ipv4_answers="$(resolve_ipv4_addresses)"
  if ipv6_answers="$(resolve_ipv6_addresses)" \
    && [[ "${ipv4_answers}" == "${public_ip}" ]] \
    && [[ -z "${ipv6_answers}" ]]; then
    hostname_ready=true
    break
  fi
  if (( attempt < 40 )); then
    sleep 15
  fi
done
if [[ "${hostname_ready}" != true ]]; then
  echo "${API_HOSTNAME} must resolve only to this instance EIP (${public_ip}) and have no AAAA record" >&2
  exit 75
fi

install -d -o root -g root -m 0755 "${CONFIG_DIR}"
read_single_env_value() {
  local key="$1"
  local file="$2"

  awk -v key="${key}" '
    index($0, key "=") == 1 {
      count += 1
      value = substr($0, length(key) + 2)
    }
    END {
      if (count != 1) {
        exit 1
      }
      print value
    }
  ' "${file}"
}

if [[ -e "${CONFIG_FILE}" || -L "${CONFIG_FILE}" ]]; then
  if [[ ! -f "${CONFIG_FILE}" || -L "${CONFIG_FILE}" ]]; then
    echo "existing production configuration is not a regular file; refusing to replace it" >&2
    exit 78
  fi
  existing_hostname=""
  existing_tls_email=""
  if ! existing_hostname="$(read_single_env_value FOFU_DOMAIN "${CONFIG_FILE}")" \
    || ! existing_tls_email="$(read_single_env_value FOFU_TLS_EMAIL "${CONFIG_FILE}")"; then
    echo "existing production configuration has missing or duplicate host/email settings; refusing to replace it" >&2
    exit 78
  fi
  if [[ "${existing_hostname}" != "${API_HOSTNAME}" \
    || "${existing_tls_email}" != "${TLS_EMAIL}" ]]; then
    echo "existing production configuration uses a different host or TLS email; refusing to replace it" >&2
    exit 78
  fi
else
  config_tmp="$(mktemp "${CONFIG_DIR}/production.env.XXXXXX")"
  cleanup() {
    rm -f -- "${config_tmp}"
  }
  trap cleanup EXIT
  cat >"${config_tmp}" <<EOF
FOFU_DOMAIN=${API_HOSTNAME}
FOFU_TLS_EMAIL=${TLS_EMAIL}

FOFU_ENVIRONMENT=production
FOFU_CORS_ORIGINS=[]
FOFU_WEB_APP_BASE_URL=https://${API_HOSTNAME}
FOFU_PUBLIC_API_BASE_URL=https://${API_HOSTNAME}
FOFU_GOOGLE_OAUTH_CLIENT_IDS=[]

FOFU_UPLOAD_DIR=/app/var/uploads
FOFU_AUTO_CREATE_SCHEMA=false
FOFU_SEED_DEMO_DATA=false

FOFU_APNS_ENABLED=false
FOFU_APNS_ENVIRONMENT=production
FOFU_APNS_TEAM_ID=
FOFU_APNS_KEY_ID=
FOFU_APNS_BUNDLE_ID=
FOFU_APNS_PRIVATE_KEY_PATH=

FOFU_PUSH_WORKER_POLL_SECONDS=2
FOFU_PUSH_WORKER_BATCH_SIZE=20
FOFU_PUSH_DELIVERY_MAX_ATTEMPTS=8
FOFU_PUSH_DELIVERY_LEASE_SECONDS=60
FOFU_PUSH_DELIVERY_RETENTION_DAYS=30
FOFU_PUSH_MAX_ACTIVE_DEVICES_PER_USER=10
EOF
  chown root:root "${config_tmp}"
  chmod 0640 "${config_tmp}"
  mv -- "${config_tmp}" "${CONFIG_FILE}"
  trap - EXIT
fi
chown root:root "${CONFIG_FILE}"
chmod 0640 "${CONFIG_FILE}"

systemctl enable fofu.service
if systemctl is-active --quiet fofu.service; then
  systemctl reload fofu.service
else
  systemctl start fofu.service
fi

ready_url="https://${API_HOSTNAME}/health/ready"
ready_response="$(
  curl \
    --fail \
    --show-error \
    --silent \
    --retry 12 \
    --retry-all-errors \
    --retry-delay 5 \
    "${ready_url}"
)"
if [[ "${ready_response}" != *'"status":"ready"'* ]]; then
  echo "unexpected readiness response: ${ready_response}" >&2
  exit 69
fi

echo "Fofu is ready at https://${API_HOSTNAME}/api/v1"
