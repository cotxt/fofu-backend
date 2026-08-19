#!/usr/bin/env bash
set -Eeuo pipefail

# Prepare only the identity and Standard Parameter Store resources required by
# the Fofu EC2 runtime. This script deliberately does not create metered
# compute, public IPv4, or networking resources.

readonly REGION="${AWS_REGION:-ap-northeast-2}"
readonly RDS_INSTANCE_ID="fofu-postgres"
readonly DATABASE_PARAMETER="/fofu/production/database-url"
readonly JWT_PARAMETER="/fofu/production/jwt-secret"
readonly ROLE_NAME="fofu-api-ec2-role"
readonly PROFILE_NAME="fofu-api-ec2-profile"
readonly POLICY_NAME="fofu-api-runtime"

usage() {
  cat <<'EOF'
usage: prepare-runtime-identity.sh check|apply

  check  Validate the AWS account, private RDS, and database SSM parameter.
  apply  Run the checks, then create or update only the JWT parameter, EC2 IAM
         role, inline policy, and instance profile. Existing JWT values are
         never read, printed, or overwritten.
EOF
}

if (( $# != 1 )) || [[ "$1" != "check" && "$1" != "apply" ]]; then
  usage >&2
  exit 64
fi
readonly ACTION="$1"

if [[ "${REGION}" != "ap-northeast-2" ]]; then
  echo "refusing to prepare Fofu outside ap-northeast-2 (received ${REGION})" >&2
  exit 78
fi

for required_command in aws openssl mktemp tr; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    echo "required command is missing: ${required_command}" >&2
    exit 69
  fi
done

export AWS_REGION="${REGION}"
export AWS_DEFAULT_REGION="${REGION}"
export AWS_PAGER=""

account_id="$(aws sts get-caller-identity --query Account --output text)"
if [[ ! "${account_id}" =~ ^[0-9]{12}$ ]]; then
  echo "AWS returned an invalid account ID" >&2
  exit 69
fi

rds_details="$(
  aws rds describe-db-instances \
    --db-instance-identifier "${RDS_INSTANCE_ID}" \
    --query 'DBInstances[0].[DBInstanceStatus,PubliclyAccessible,DBSubnetGroup.VpcId,DBSubnetGroup.DBSubnetGroupName,join(`,`,VpcSecurityGroups[].VpcSecurityGroupId)]' \
    --output text
)"
read -r rds_status rds_public vpc_id subnet_group rds_security_groups <<<"${rds_details}"

if [[ "${rds_status}" != "available" ]]; then
  echo "RDS ${RDS_INSTANCE_ID} must be available (current: ${rds_status})" >&2
  exit 69
fi
if [[ "${rds_public}" != "False" && "${rds_public}" != "false" ]]; then
  echo "RDS ${RDS_INSTANCE_ID} must remain private" >&2
  exit 78
fi
if [[ ! "${vpc_id}" =~ ^vpc-[0-9a-f]+$ ]]; then
  echo "could not resolve the RDS VPC" >&2
  exit 69
fi

parameter_count() {
  aws ssm describe-parameters \
    --parameter-filters "Key=Name,Option=Equals,Values=$1" \
    --query 'length(Parameters)' \
    --output text
}

parameter_metadata() {
  aws ssm describe-parameters \
    --parameter-filters "Key=Name,Option=Equals,Values=$1" \
    --query 'Parameters[0].[Type,Tier,Version,KeyId]' \
    --output text
}

require_default_ssm_key() {
  local parameter_name="$1"
  local key_id="$2"
  case "${key_id}" in
    "" | None | null | alias/aws/ssm | "arn:aws:kms:${REGION}:${account_id}:alias/aws/ssm")
      ;;
    *)
      echo "${parameter_name} uses customer-managed KMS key ${key_id}" >&2
      echo "this low-cost runtime role supports only the default alias/aws/ssm key" >&2
      exit 78
      ;;
  esac
}

database_count="$(parameter_count "${DATABASE_PARAMETER}")"
if [[ "${database_count}" != "1" ]]; then
  echo "expected exactly one ${DATABASE_PARAMETER} parameter" >&2
  exit 69
fi
database_metadata="$(parameter_metadata "${DATABASE_PARAMETER}")"
read -r database_type database_tier database_version database_key_id <<<"${database_metadata}"
if [[ "${database_type}" != "SecureString" ]]; then
  echo "${DATABASE_PARAMETER} must be a SecureString" >&2
  exit 78
fi
require_default_ssm_key "${DATABASE_PARAMETER}" "${database_key_id}"
if [[ "${database_tier}" != "Standard" ]]; then
  echo "warning: ${DATABASE_PARAMETER} tier is ${database_tier}; Standard avoids parameter storage charges" >&2
fi

existing_jwt=false
jwt_count="$(parameter_count "${JWT_PARAMETER}")"
if [[ "${jwt_count}" == "1" ]]; then
  existing_jwt=true
  jwt_metadata="$(parameter_metadata "${JWT_PARAMETER}")"
  read -r jwt_type jwt_tier jwt_version jwt_key_id <<<"${jwt_metadata}"
  if [[ "${jwt_type}" != "SecureString" ]]; then
    echo "${JWT_PARAMETER} already exists but is not a SecureString" >&2
    exit 78
  fi
  require_default_ssm_key "${JWT_PARAMETER}" "${jwt_key_id}"
  if [[ "${jwt_tier}" != "Standard" ]]; then
    echo "warning: ${JWT_PARAMETER} tier is ${jwt_tier}; Standard avoids parameter storage charges" >&2
  fi
elif [[ "${jwt_count}" != "0" ]]; then
  echo "could not determine whether ${JWT_PARAMETER} exists" >&2
  exit 69
fi

echo "AWS preflight passed"
echo "  account: ${account_id}"
echo "  region: ${REGION}"
echo "  RDS: ${RDS_INSTANCE_ID} (${rds_status}, private)"
echo "  VPC: ${vpc_id}"
echo "  DB subnet group: ${subnet_group}"
echo "  RDS security groups: ${rds_security_groups}"
echo "  DB parameter: ${DATABASE_PARAMETER} (${database_type}, version ${database_version})"
if [[ "${existing_jwt}" == true ]]; then
  echo "  JWT parameter: ${JWT_PARAMETER} (existing SecureString; it will not be overwritten)"
else
  echo "  JWT parameter: ${JWT_PARAMETER} (missing; apply will create it)"
fi

if [[ "${ACTION}" == "check" ]]; then
  echo "No AWS resources were changed. Run again with 'apply' to prepare IAM and JWT."
  exit 0
fi

if [[ "${existing_jwt}" == false ]]; then
  cli_history="$(aws configure get cli_history 2>/dev/null || true)"
  case "${cli_history}" in
    enabled | Enabled | ENABLED)
      echo "refusing to generate JWT while AWS CLI history is enabled" >&2
      echo "run 'aws configure set cli_history disabled', then rerun apply" >&2
      exit 78
      ;;
  esac
fi

umask 077
trust_file="$(mktemp /tmp/fofu-ec2-trust.XXXXXX)"
policy_file="$(mktemp /tmp/fofu-ec2-policy.XXXXXX)"
cleanup() {
  rm -f -- "${trust_file}" "${policy_file}"
}
trap cleanup EXIT

iam_role_exists() {
  local result
  if result="$(aws iam get-role --role-name "${ROLE_NAME}" 2>&1)"; then
    return 0
  fi
  if [[ "${result}" == *NoSuchEntity* ]]; then
    return 1
  fi
  echo "failed to inspect IAM role ${ROLE_NAME}: ${result}" >&2
  exit 69
}

instance_profile_exists() {
  local result
  if result="$(
    aws iam get-instance-profile --instance-profile-name "${PROFILE_NAME}" 2>&1
  )"; then
    return 0
  fi
  if [[ "${result}" == *NoSuchEntity* ]]; then
    return 1
  fi
  echo "failed to inspect instance profile ${PROFILE_NAME}: ${result}" >&2
  exit 69
}

require_owned_role() {
  local project environment managed_count inline_count inline_names
  project="$(
    aws iam list-role-tags \
      --role-name "${ROLE_NAME}" \
      --query "Tags[?Key=='Project'].Value | [0]" \
      --output text
  )"
  environment="$(
    aws iam list-role-tags \
      --role-name "${ROLE_NAME}" \
      --query "Tags[?Key=='Environment'].Value | [0]" \
      --output text
  )"
  if [[ "${project}" != "fofu" || "${environment}" != "production" ]]; then
    echo "refusing to modify unowned IAM role ${ROLE_NAME}; expected Project=fofu and Environment=production tags" >&2
    exit 78
  fi
  managed_count="$(
    aws iam list-attached-role-policies \
      --role-name "${ROLE_NAME}" \
      --query 'length(AttachedPolicies)' \
      --output text
  )"
  inline_count="$(
    aws iam list-role-policies \
      --role-name "${ROLE_NAME}" \
      --query 'length(PolicyNames)' \
      --output text
  )"
  inline_names="$(
    aws iam list-role-policies \
      --role-name "${ROLE_NAME}" \
      --query 'PolicyNames' \
      --output text
  )"
  if [[ "${managed_count}" != "0" || "${inline_count}" -gt 1 ]]; then
    echo "refusing role ${ROLE_NAME}: unexpected extra policies are attached" >&2
    exit 78
  fi
  if [[ "${inline_count}" == "1" && "${inline_names}" != "${POLICY_NAME}" ]]; then
    echo "refusing role ${ROLE_NAME}: unexpected inline policy ${inline_names}" >&2
    exit 78
  fi
}

require_owned_profile() {
  local project environment
  project="$(
    aws iam list-instance-profile-tags \
      --instance-profile-name "${PROFILE_NAME}" \
      --query "Tags[?Key=='Project'].Value | [0]" \
      --output text
  )"
  environment="$(
    aws iam list-instance-profile-tags \
      --instance-profile-name "${PROFILE_NAME}" \
      --query "Tags[?Key=='Environment'].Value | [0]" \
      --output text
  )"
  if [[ "${project}" != "fofu" || "${environment}" != "production" ]]; then
    echo "refusing to modify unowned instance profile ${PROFILE_NAME}" >&2
    exit 78
  fi
}

instance_profile_role() {
  aws iam get-instance-profile \
    --instance-profile-name "${PROFILE_NAME}" \
    --query 'InstanceProfile.Roles[0].RoleName' \
    --output text
}

retryable_iam_error() {
  [[ "$1" == *NoSuchEntity* || "$1" == *ConcurrentModification* ]]
}

runtime_role_ready() {
  local project environment managed_count inline_count inline_names result

  if ! project="$(
    aws iam list-role-tags \
      --role-name "${ROLE_NAME}" \
      --query "Tags[?Key=='Project'].Value | [0]" \
      --output text 2>&1
  )"; then
    result="${project}"
    if retryable_iam_error "${result}"; then
      return 1
    fi
    echo "failed to verify IAM role tags: ${result}" >&2
    exit 69
  fi
  if ! environment="$(
    aws iam list-role-tags \
      --role-name "${ROLE_NAME}" \
      --query "Tags[?Key=='Environment'].Value | [0]" \
      --output text 2>&1
  )"; then
    result="${environment}"
    if retryable_iam_error "${result}"; then
      return 1
    fi
    echo "failed to verify IAM role tags: ${result}" >&2
    exit 69
  fi
  if [[ -z "${project}" || "${project}" == "None" || "${project}" == "null" \
    || -z "${environment}" || "${environment}" == "None" \
    || "${environment}" == "null" ]]; then
    return 1
  fi
  if [[ "${project}" != "fofu" || "${environment}" != "production" ]]; then
    echo "refusing unexpected IAM role tags on ${ROLE_NAME}" >&2
    exit 78
  fi

  if ! managed_count="$(
    aws iam list-attached-role-policies \
      --role-name "${ROLE_NAME}" \
      --query 'length(AttachedPolicies)' \
      --output text 2>&1
  )"; then
    result="${managed_count}"
    if retryable_iam_error "${result}"; then
      return 1
    fi
    echo "failed to verify managed IAM policies: ${result}" >&2
    exit 69
  fi
  if [[ "${managed_count}" != "0" ]]; then
    echo "refusing role ${ROLE_NAME}: unexpected managed policies are attached" >&2
    exit 78
  fi

  if ! inline_count="$(
    aws iam list-role-policies \
      --role-name "${ROLE_NAME}" \
      --query 'length(PolicyNames)' \
      --output text 2>&1
  )"; then
    result="${inline_count}"
    if retryable_iam_error "${result}"; then
      return 1
    fi
    echo "failed to verify inline IAM policies: ${result}" >&2
    exit 69
  fi
  if [[ "${inline_count}" == "0" ]]; then
    return 1
  fi
  if [[ "${inline_count}" != "1" ]]; then
    echo "refusing role ${ROLE_NAME}: unexpected extra inline policies are attached" >&2
    exit 78
  fi
  if ! inline_names="$(
    aws iam list-role-policies \
      --role-name "${ROLE_NAME}" \
      --query 'PolicyNames' \
      --output text 2>&1
  )"; then
    result="${inline_names}"
    if retryable_iam_error "${result}"; then
      return 1
    fi
    echo "failed to verify inline IAM policy name: ${result}" >&2
    exit 69
  fi
  if [[ "${inline_names}" != "${POLICY_NAME}" ]]; then
    echo "refusing role ${ROLE_NAME}: unexpected inline policy ${inline_names}" >&2
    exit 78
  fi
  return 0
}

runtime_profile_owned() {
  local project environment result

  if ! project="$(
    aws iam list-instance-profile-tags \
      --instance-profile-name "${PROFILE_NAME}" \
      --query "Tags[?Key=='Project'].Value | [0]" \
      --output text 2>&1
  )"; then
    result="${project}"
    if retryable_iam_error "${result}"; then
      return 1
    fi
    echo "failed to verify instance profile tags: ${result}" >&2
    exit 69
  fi
  if ! environment="$(
    aws iam list-instance-profile-tags \
      --instance-profile-name "${PROFILE_NAME}" \
      --query "Tags[?Key=='Environment'].Value | [0]" \
      --output text 2>&1
  )"; then
    result="${environment}"
    if retryable_iam_error "${result}"; then
      return 1
    fi
    echo "failed to verify instance profile tags: ${result}" >&2
    exit 69
  fi
  if [[ -z "${project}" || "${project}" == "None" || "${project}" == "null" \
    || -z "${environment}" || "${environment}" == "None" \
    || "${environment}" == "null" ]]; then
    return 1
  fi
  if [[ "${project}" != "fofu" || "${environment}" != "production" ]]; then
    echo "refusing unexpected tags on instance profile ${PROFILE_NAME}" >&2
    exit 78
  fi
  return 0
}

cat >"${trust_file}" <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {"Service": "ec2.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }
  ]
}
JSON

cat >"${policy_file}" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SessionManagerCore",
      "Effect": "Allow",
      "Action": [
        "ssm:UpdateInstanceInformation",
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ReadFofuRuntimeParameters",
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": [
        "arn:aws:ssm:${REGION}:${account_id}:parameter/fofu/production/database-url",
        "arn:aws:ssm:${REGION}:${account_id}:parameter/fofu/production/jwt-secret"
      ]
    }
  ]
}
JSON

if iam_role_exists; then
  require_owned_role
  aws iam update-assume-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-document "file://${trust_file}"
else
  aws iam create-role \
    --role-name "${ROLE_NAME}" \
    --assume-role-policy-document "file://${trust_file}" \
    --description "Fofu production API EC2 runtime" \
    --tags Key=Project,Value=fofu Key=Environment,Value=production \
    --query 'Role.RoleName' \
    --output text
fi
aws iam wait role-exists --role-name "${ROLE_NAME}"

put_policy_succeeded=false
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if put_policy_result="$(
    aws iam put-role-policy \
      --role-name "${ROLE_NAME}" \
      --policy-name "${POLICY_NAME}" \
      --policy-document "file://${policy_file}" 2>&1
  )"; then
    put_policy_succeeded=true
    break
  fi
  if ! retryable_iam_error "${put_policy_result}"; then
    echo "failed to store ${POLICY_NAME}: ${put_policy_result}" >&2
    exit 69
  fi
  if (( attempt < 10 )); then
    sleep 3
  fi
done
if [[ "${put_policy_succeeded}" != true ]]; then
  echo "IAM policy did not become writable in time; rerun this script with apply" >&2
  exit 75
fi

role_ready=false
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if runtime_role_ready; then
    role_ready=true
    break
  fi
  if (( attempt < 10 )); then
    sleep 3
  fi
done
if [[ "${role_ready}" != true ]]; then
  echo "IAM role changes did not become visible in time; rerun this script with apply" >&2
  exit 75
fi

if instance_profile_exists; then
  require_owned_profile
else
  aws iam create-instance-profile \
    --instance-profile-name "${PROFILE_NAME}" \
    --tags Key=Project,Value=fofu Key=Environment,Value=production \
    --query 'InstanceProfile.InstanceProfileName' \
    --output text
fi
aws iam wait instance-profile-exists \
  --instance-profile-name "${PROFILE_NAME}"

profile_owned=false
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if runtime_profile_owned; then
    profile_owned=true
    break
  fi
  if (( attempt < 10 )); then
    sleep 3
  fi
done
if [[ "${profile_owned}" != true ]]; then
  echo "instance profile tags did not become visible in time; rerun apply" >&2
  exit 75
fi

if ! attached_role="$(instance_profile_role)"; then
  echo "failed to inspect instance profile ${PROFILE_NAME}" >&2
  exit 69
fi
if [[ -z "${attached_role}" || "${attached_role}" == "None" || "${attached_role}" == "null" ]]; then
  add_role_succeeded=false
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if add_role_result="$(
      aws iam add-role-to-instance-profile \
        --instance-profile-name "${PROFILE_NAME}" \
        --role-name "${ROLE_NAME}" 2>&1
    )"; then
      add_role_succeeded=true
      break
    fi
    if [[ "${add_role_result}" != *NoSuchEntity* \
      && "${add_role_result}" != *ConcurrentModification* ]]; then
      echo "failed to add ${ROLE_NAME} to ${PROFILE_NAME}: ${add_role_result}" >&2
      exit 69
    fi
    if (( attempt < 10 )); then
      sleep 3
    fi
  done
  if [[ "${add_role_succeeded}" != true ]]; then
    echo "IAM role did not become available in time; rerun this script with apply" >&2
    exit 75
  fi
elif [[ "${attached_role}" != "${ROLE_NAME}" ]]; then
  echo "instance profile ${PROFILE_NAME} already contains unexpected role ${attached_role}" >&2
  exit 78
fi

if [[ "${existing_jwt}" == false ]]; then
  openssl rand -hex 32 \
    | tr -d '\n' \
    | aws ssm put-parameter \
      --name "${JWT_PARAMETER}" \
      --description "Fofu production JWT signing secret" \
      --value file:///dev/stdin \
      --type SecureString \
      --tier Standard \
      --no-overwrite \
      --tags Key=Project,Value=fofu Key=Environment,Value=production \
      --query '{Version:Version,Tier:Tier}' \
      --output table
fi

jwt_ready=false
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if ! jwt_count="$(parameter_count "${JWT_PARAMETER}" 2>&1)"; then
    echo "failed to verify JWT parameter: ${jwt_count}" >&2
    exit 69
  fi
  if [[ "${jwt_count}" == "1" ]]; then
    if ! jwt_metadata="$(parameter_metadata "${JWT_PARAMETER}" 2>&1)"; then
      echo "failed to verify JWT parameter metadata: ${jwt_metadata}" >&2
      exit 69
    fi
    read -r jwt_type jwt_tier jwt_version jwt_key_id <<<"${jwt_metadata}"
    if [[ "${jwt_type}" == "SecureString" ]]; then
      require_default_ssm_key "${JWT_PARAMETER}" "${jwt_key_id}"
      jwt_ready=true
      break
    fi
    if [[ -n "${jwt_type}" && "${jwt_type}" != "None" && "${jwt_type}" != "null" ]]; then
      echo "JWT parameter verification failed: unexpected type ${jwt_type}" >&2
      exit 69
    fi
  elif [[ "${jwt_count}" != "0" ]]; then
    echo "JWT parameter verification failed: expected exactly one parameter" >&2
    exit 69
  fi
  if (( attempt < 10 )); then
    sleep 3
  fi
done
if [[ "${jwt_ready}" != true ]]; then
  echo "JWT parameter did not become visible in time; rerun this script with apply" >&2
  exit 75
fi
if [[ "${jwt_tier}" != "Standard" ]]; then
  echo "warning: ${JWT_PARAMETER} tier is ${jwt_tier}; Standard avoids parameter storage charges" >&2
fi

verified_role=""
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if profile_result="$(instance_profile_role 2>&1)"; then
    verified_role="${profile_result}"
    if [[ "${verified_role}" == "${ROLE_NAME}" ]]; then
      break
    fi
    if [[ -n "${verified_role}" && "${verified_role}" != "None" \
      && "${verified_role}" != "null" ]]; then
      break
    fi
  elif [[ "${profile_result}" != *NoSuchEntity* ]]; then
    echo "failed to verify instance profile ${PROFILE_NAME}: ${profile_result}" >&2
    exit 69
  fi
  if (( attempt < 10 )); then
    sleep 3
  fi
done
if [[ "${verified_role}" != "${ROLE_NAME}" ]]; then
  echo "instance profile verification failed; rerun this script with apply" >&2
  exit 75
fi

echo "Runtime identity preparation complete"
echo "  role: ${ROLE_NAME}"
echo "  instance profile: ${PROFILE_NAME}"
echo "  inline policy: ${POLICY_NAME}"
echo "  JWT parameter: ${JWT_PARAMETER} (${jwt_type}, ${jwt_tier}, version ${jwt_version})"
echo "No metered compute, public IPv4, or networking resource was created."
