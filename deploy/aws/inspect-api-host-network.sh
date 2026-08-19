#!/usr/bin/env bash
set -Eeuo pipefail

# Read-only discovery for the Fofu API EC2 host. It accepts only subnets with
# an active IPv4 default route to an internet gateway and t4g.micro support.

readonly REGION="${AWS_REGION:-ap-northeast-2}"
readonly RDS_INSTANCE_ID="fofu-postgres"
readonly PROFILE_NAME="fofu-api-ec2-profile"
readonly ROLE_NAME="fofu-api-ec2-role"
readonly AMI_PARAMETER="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"

if (( $# != 0 )); then
  echo "usage: inspect-api-host-network.sh" >&2
  exit 64
fi
if [[ "${REGION}" != "ap-northeast-2" ]]; then
  echo "refusing to inspect Fofu outside ap-northeast-2" >&2
  exit 78
fi
for required_command in aws head sort; do
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
  echo "could not resolve a valid 12-digit AWS account ID" >&2
  exit 69
fi
rds_details="$(
  aws rds describe-db-instances \
    --db-instance-identifier "${RDS_INSTANCE_ID}" \
    --query 'DBInstances[0].[DBInstanceStatus,PubliclyAccessible,AvailabilityZone,DBSubnetGroup.VpcId,join(`,`,VpcSecurityGroups[].VpcSecurityGroupId)]' \
    --output text
)"
read -r rds_status rds_public rds_az vpc_id rds_security_groups <<<"${rds_details}"

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
if [[ ! "${rds_az}" =~ ^ap-northeast-2[a-z]$ ]]; then
  echo "could not resolve a valid Seoul Availability Zone for RDS" >&2
  exit 69
fi
if [[ ! "${rds_security_groups}" =~ ^sg-[0-9a-f]+(,sg-[0-9a-f]+)*$ ]]; then
  echo "could not resolve the RDS security groups" >&2
  exit 69
fi

dns_support="$(
  aws ec2 describe-vpc-attribute \
    --vpc-id "${vpc_id}" \
    --attribute enableDnsSupport \
    --query 'EnableDnsSupport.Value' \
    --output text
)"
dns_hostnames="$(
  aws ec2 describe-vpc-attribute \
    --vpc-id "${vpc_id}" \
    --attribute enableDnsHostnames \
    --query 'EnableDnsHostnames.Value' \
    --output text
)"
if [[ "${dns_support}" != "True" && "${dns_support}" != "true" ]]; then
  echo "VPC ${vpc_id} must have enableDnsSupport=true" >&2
  exit 78
fi
if [[ "${dns_hostnames}" != "True" && "${dns_hostnames}" != "true" ]]; then
  echo "VPC ${vpc_id} must have enableDnsHostnames=true" >&2
  exit 78
fi

ami_id="$(
  aws ssm get-parameter \
    --name "${AMI_PARAMETER}" \
    --query 'Parameter.Value' \
    --output text
)"
ami_details="$(
  aws ec2 describe-images \
    --image-ids "${ami_id}" \
    --query 'Images[0].[State,Architecture,RootDeviceName]' \
    --output text
)"
read -r ami_state ami_architecture root_device_name <<<"${ami_details}"
if [[ "${ami_state}" != "available" || "${ami_architecture}" != "arm64" ]]; then
  echo "latest AL2023 AMI is not an available Arm64 image" >&2
  exit 69
fi

profile_state="missing (run prepare-runtime-identity.sh apply)"
profile_lookup_output=""
if profile_lookup_output="$(
  aws iam get-instance-profile \
    --instance-profile-name "${PROFILE_NAME}" \
    --query 'InstanceProfile.Roles[0].RoleName' \
    --output text 2>&1
)"; then
  profile_role="${profile_lookup_output}"
  if [[ "${profile_role}" != "${ROLE_NAME}" ]]; then
    echo "instance profile contains unexpected role: ${profile_role}" >&2
    exit 78
  fi
  profile_state="ready (${profile_role})"
elif [[ "${profile_lookup_output}" != *"(NoSuchEntity)"* ]]; then
  echo "failed to inspect instance profile ${PROFILE_NAME}" >&2
  echo "${profile_lookup_output}" >&2
  exit 69
fi
if [[ "${profile_state}" == missing* ]]; then
  echo "instance profile ${PROFILE_NAME} is missing; run prepare-runtime-identity.sh apply first" >&2
  exit 69
fi

declare -a candidates=()
while IFS=$'\t' read -r subnet_id subnet_az available_ips map_public_ip; do
  [[ -n "${subnet_id}" ]] || continue

  route_table_id="$(
    aws ec2 describe-route-tables \
      --filters "Name=association.subnet-id,Values=${subnet_id}" \
      --query 'RouteTables[0].RouteTableId' \
      --output text
  )"
  if [[ -z "${route_table_id}" || "${route_table_id}" == "None" \
    || "${route_table_id}" == "null" ]]; then
    route_table_id="$(
      aws ec2 describe-route-tables \
        --filters \
          "Name=vpc-id,Values=${vpc_id}" \
          'Name=association.main,Values=true' \
        --query 'RouteTables[0].RouteTableId' \
        --output text
    )"
  fi

  default_gateway="$(
    aws ec2 describe-route-tables \
      --route-table-ids "${route_table_id}" \
      --query 'RouteTables[0].Routes[?DestinationCidrBlock==`0.0.0.0/0` && State==`active`].GatewayId | [0]' \
      --output text
  )"
  [[ "${default_gateway}" == igw-* ]] || continue

  offering_count="$(
    aws ec2 describe-instance-type-offerings \
      --location-type availability-zone \
      --filters \
        'Name=instance-type,Values=t4g.micro' \
        "Name=location,Values=${subnet_az}" \
      --query 'length(InstanceTypeOfferings)' \
      --output text
  )"
  [[ "${offering_count}" == "1" ]] || continue
  (( available_ips > 0 )) || continue

  priority=1
  if [[ "${subnet_az}" == "${rds_az}" ]]; then
    priority=0
  fi
  candidates+=(
    "${priority}|${subnet_id}|${subnet_az}|${available_ips}|${map_public_ip}|${route_table_id}|${default_gateway}"
  )
done < <(
  aws ec2 describe-subnets \
    --filters "Name=vpc-id,Values=${vpc_id}" 'Name=state,Values=available' \
    --query 'Subnets[].[SubnetId,AvailabilityZone,AvailableIpAddressCount,MapPublicIpOnLaunch]' \
    --output text
)

if (( ${#candidates[@]} == 0 )); then
  echo "no public subnet with t4g.micro support was found in ${vpc_id}" >&2
  exit 69
fi

sorted_candidates="$(printf '%s\n' "${candidates[@]}" | sort -t '|' -k1,1n -k3,3 -k2,2)"
recommended_record="$(printf '%s\n' "${sorted_candidates}" | head -n 1)"
IFS='|' read -r _ recommended_subnet recommended_az _ _ _ _ <<<"${recommended_record}"

echo "Fofu API host network preflight passed"
echo "  account: ${account_id}"
echo "  region: ${REGION}"
echo "  VPC: ${vpc_id} (DNS support and hostnames enabled)"
echo "  RDS: ${RDS_INSTANCE_ID} (${rds_az}, private)"
echo "  RDS security groups: ${rds_security_groups}"
echo "  instance profile: ${profile_state}"
echo "  AMI: ${ami_id} (${ami_architecture}, root ${root_device_name})"
echo
echo "Eligible public subnets (* is in the same AZ as RDS):"
while IFS='|' read -r priority subnet_id subnet_az available_ips map_public_ip route_table_id gateway; do
  marker=" "
  [[ "${priority}" == "0" ]] && marker="*"
  printf '  %s %s  AZ=%s  free_ips=%s  map_public=%s  route=%s -> %s\n' \
    "${marker}" "${subnet_id}" "${subnet_az}" "${available_ips}" \
    "${map_public_ip}" "${route_table_id}" "${gateway}"
done <<<"${sorted_candidates}"
echo
echo "Recommended CloudFormation parameters:"
echo "  VpcId=${vpc_id}"
echo "  PublicSubnetId=${recommended_subnet}  # ${recommended_az}"
echo "  AvailabilityZone=${recommended_az}"
echo "  RdsSecurityGroupId=<choose one ID from the RDS list above>"
echo "  RootDeviceName=${root_device_name}"
echo "No AWS resources were changed."
