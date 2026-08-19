#!/usr/bin/env bash
set -Eeuo pipefail

# Idempotent host bootstrap for the Fofu Amazon Linux 2023 Arm64 instance.
# This deliberately does not write application secrets or start the API. The
# API is activated only after its public HTTPS hostname resolves to the EIP.

readonly COMPOSE_VERSION="5.4.0"
readonly COMPOSE_SHA256="fc5d1371f1ec7987e703da94ede49af3fbfb240b83f22991a98511de7bc4b93b"
readonly COMPOSE_URL="https://github.com/docker/compose/releases/download/v${COMPOSE_VERSION}/docker-compose-linux-aarch64"
readonly COMPOSE_PATH="/usr/local/lib/docker/cli-plugins/docker-compose"
readonly REPOSITORY_DIR="/opt/fofu"
readonly DATA_MOUNT="/var/lib/fofu"
readonly API_UID="10001"
readonly API_GID="10001"

if (( EUID != 0 )); then
  echo "run this script as root" >&2
  exit 77
fi

if [[ "$(uname -m)" != "aarch64" ]]; then
  echo "this bootstrap supports only the Arm64 Fofu host" >&2
  exit 78
fi

if [[ ! -d "${REPOSITORY_DIR}/.git" ]]; then
  echo "repository is missing: ${REPOSITORY_DIR}" >&2
  exit 66
fi

if (( $# != 1 )); then
  echo "usage: $0 <data-volume-id>" >&2
  exit 64
fi
readonly DATA_VOLUME_ID="$1"
if [[ ! "${DATA_VOLUME_ID}" =~ ^vol-[0-9a-f]+$ ]]; then
  echo "invalid EBS data volume ID: ${DATA_VOLUME_ID}" >&2
  exit 64
fi

# Amazon Linux 2023 ships curl-minimal, which provides the curl command but
# conflicts with the full curl package. --allowerasing lets dnf swap it for
# full curl instead of aborting the whole transaction on that conflict.
dnf upgrade -y --allowerasing
dnf install -y --allowerasing curl docker git util-linux xfsprogs

systemctl enable --now amazon-ssm-agent
systemctl enable --now docker

compose_tmp_dir="$(mktemp -d /tmp/fofu-compose.XXXXXX)"
cleanup() {
  rm -rf -- "${compose_tmp_dir}"
}
trap cleanup EXIT

curl \
  --fail \
  --show-error \
  --silent \
  --location \
  --proto '=https' \
  --tlsv1.2 \
  --output "${compose_tmp_dir}/docker-compose" \
  "${COMPOSE_URL}"
printf '%s  %s\n' \
  "${COMPOSE_SHA256}" \
  "${compose_tmp_dir}/docker-compose" \
  | sha256sum --check --status
install -d -o root -g root -m 0755 "$(dirname -- "${COMPOSE_PATH}")"
install -o root -g root -m 0755 \
  "${compose_tmp_dir}/docker-compose" \
  "${COMPOSE_PATH}"

installed_compose_version="$(docker compose version --short)"
if [[ "${installed_compose_version#v}" != "${COMPOSE_VERSION}" ]]; then
  echo "unexpected Docker Compose version: ${installed_compose_version}" >&2
  exit 69
fi

# Nitro instances expose EBS volumes as NVMe devices whose kernel names are not
# derived from the requested /dev/sdX attachment name. Resolve the device only
# through the udev symlink that contains the exact CloudFormation volume ID.
readonly NORMALIZED_DATA_VOLUME_ID="${DATA_VOLUME_ID//-/}"
data_device=""
for _attempt in $(seq 1 120); do
  for candidate in \
    "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${NORMALIZED_DATA_VOLUME_ID}" \
    "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${DATA_VOLUME_ID}"; do
    if [[ -b "${candidate}" ]]; then
      data_device="$(readlink -f -- "${candidate}")"
      break 2
    fi
  done
  sleep 5
done
if [[ -z "${data_device}" || ! -b "${data_device}" ]]; then
  echo "timed out waiting for EBS data volume ${DATA_VOLUME_ID}" >&2
  exit 69
fi

root_source="$(findmnt --noheadings --output SOURCE --target /)"
root_parent="$(lsblk --noheadings --output PKNAME "${root_source}" 2>/dev/null | head -n 1)"
if [[ "$(readlink -f -- "${root_source}")" == "${data_device}" \
  || ( -n "${root_parent}" && "/dev/${root_parent}" == "${data_device}" ) ]]; then
  echo "refusing to use the root EBS device as the Fofu data volume" >&2
  exit 78
fi

existing_mounts="$(findmnt --raw --noheadings --source "${data_device}" --output TARGET || true)"
if [[ -n "${existing_mounts}" && "${existing_mounts}" != "${DATA_MOUNT}" ]]; then
  echo "data volume is already mounted at an unexpected path: ${existing_mounts}" >&2
  exit 78
fi

filesystem_type="$(blkid -s TYPE -o value "${data_device}" || true)"
if [[ -z "${filesystem_type}" ]]; then
  signature_types="$(wipefs --noheadings --output TYPE "${data_device}" | awk 'NF { print }')"
  device_node_count="$(lsblk --raw --noheadings --paths --output NAME "${data_device}" | awk 'END { print NR }')"
  if [[ -n "${signature_types}" || "${device_node_count}" != "1" ]]; then
    echo "refusing to format a non-empty or partitioned data volume" >&2
    exit 78
  fi
  mkfs.xfs -L fofu-data "${data_device}"
  filesystem_type="xfs"
elif [[ "${filesystem_type}" != "xfs" ]]; then
  echo "unsupported data volume filesystem: ${filesystem_type} (expected xfs)" >&2
  exit 78
fi

filesystem_uuid="$(blkid -s UUID -o value "${data_device}")"
if [[ -z "${filesystem_uuid}" ]]; then
  echo "the Fofu data volume has no filesystem UUID" >&2
  exit 69
fi

install -d -o root -g root -m 0750 "${DATA_MOUNT}"
readonly FSTAB_ENTRY="UUID=${filesystem_uuid} ${DATA_MOUNT} xfs defaults,nofail,x-systemd.device-timeout=120s 0 2"
mapfile -t existing_fstab_entries < <(
  awk -v target="${DATA_MOUNT}" '$2 == target { print }' /etc/fstab
)
if (( ${#existing_fstab_entries[@]} == 0 )); then
  printf '%s\n' "${FSTAB_ENTRY}" >>/etc/fstab
elif (( ${#existing_fstab_entries[@]} != 1 )) \
  || [[ "${existing_fstab_entries[0]}" != "${FSTAB_ENTRY}" ]]; then
  echo "refusing to replace an unexpected ${DATA_MOUNT} entry in /etc/fstab" >&2
  exit 78
fi

systemctl daemon-reload
if ! mountpoint --quiet "${DATA_MOUNT}"; then
  if find "${DATA_MOUNT}" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    echo "refusing to hide files beneath the unmounted ${DATA_MOUNT}" >&2
    exit 78
  fi
  mount "${DATA_MOUNT}"
fi
mounted_uuid="$(findmnt --noheadings --output UUID --target "${DATA_MOUNT}")"
if [[ "${mounted_uuid,,}" != "${filesystem_uuid,,}" ]]; then
  echo "the filesystem mounted at ${DATA_MOUNT} is not ${DATA_VOLUME_ID}" >&2
  exit 78
fi

chmod 0750 "${DATA_MOUNT}"
install -d -o "${API_UID}" -g "${API_GID}" -m 0700 "${DATA_MOUNT}/uploads"
install -d -o root -g root -m 0700 \
  "${DATA_MOUNT}/caddy-data" \
  "${DATA_MOUNT}/caddy-config"

if [[ ! -e /swapfile ]]; then
  dd if=/dev/zero of=/swapfile bs=1M count=2048 status=progress
  chmod 0600 /swapfile
  mkswap /swapfile
fi
chmod 0600 /swapfile
if ! swapon --show=NAME --noheadings | grep -Fxq /swapfile; then
  swapon /swapfile
fi
if ! grep -Eq '^/swapfile[[:space:]]+swap[[:space:]]' /etc/fstab; then
  printf '%s\n' '/swapfile swap swap defaults 0 0' >>/etc/fstab
fi

chown -R root:root "${REPOSITORY_DIR}"
chmod 0755 "${REPOSITORY_DIR}/deploy/aws/compose-with-ssm.sh"
chmod 0755 "${REPOSITORY_DIR}/deploy/aws/configure-and-start-host.sh"
install -d -o root -g root -m 0755 /etc/fofu
install -o root -g root -m 0644 \
  "${REPOSITORY_DIR}/deploy/aws/fofu.service" \
  /etc/systemd/system/fofu.service
install -d -o root -g root -m 0755 /etc/systemd/system/fofu.service.d
printf '%s\n' \
  '[Unit]' \
  "RequiresMountsFor=${DATA_MOUNT}" \
  >/etc/systemd/system/fofu.service.d/data-volume.conf
chmod 0644 /etc/systemd/system/fofu.service.d/data-volume.conf
systemctl daemon-reload

printf 'completed_at=%s\ncompose_version=%s\n' \
  "$(date --utc +%Y-%m-%dT%H:%M:%SZ)" \
  "${COMPOSE_VERSION}" \
  >"${DATA_MOUNT}/bootstrap-complete"
chmod 0644 "${DATA_MOUNT}/bootstrap-complete"

echo "Fofu host bootstrap complete; the API has not been started."
