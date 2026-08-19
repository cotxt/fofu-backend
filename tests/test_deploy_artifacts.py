from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import httpx
import yaml
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE_PATH = ROOT / "Dockerfile"
COMPOSE_PATH = ROOT / "deploy" / "aws" / "compose.production.yaml"
CADDYFILE_PATH = ROOT / "deploy" / "aws" / "Caddyfile"
WRAPPER_PATH = ROOT / "deploy" / "aws" / "compose-with-ssm.sh"
IDENTITY_PREP_PATH = ROOT / "deploy" / "aws" / "prepare-runtime-identity.sh"
HOST_BOOTSTRAP_PATH = ROOT / "deploy" / "aws" / "bootstrap-ec2-host.sh"
HOST_CONFIGURE_PATH = ROOT / "deploy" / "aws" / "configure-and-start-host.sh"
HOST_NETWORK_PATH = ROOT / "deploy" / "aws" / "inspect-api-host-network.sh"
HOST_STACK_PATH = ROOT / "deploy" / "aws" / "fofu-api-host.yaml"
UNIT_PATH = ROOT / "deploy" / "aws" / "fofu.service"


def load_compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE_PATH.read_text())


class CloudFormationLoader(yaml.SafeLoader):
    pass


def construct_cloudformation_tag(
    loader: CloudFormationLoader, suffix: str, node: yaml.Node
) -> dict[str, Any]:
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return {suffix: value}


CloudFormationLoader.add_multi_constructor("!", construct_cloudformation_tag)


def load_host_stack() -> dict[str, Any]:
    return yaml.load(HOST_STACK_PATH.read_text(), Loader=CloudFormationLoader)


def test_production_proxy_network_and_trusted_ip_stay_aligned() -> None:
    compose = load_compose()
    api = compose["services"]["api"]
    proxy = compose["services"]["proxy"]
    networks = compose["networks"]

    assert "ports" not in api
    assert networks["proxy-internal"]["internal"] is True
    assert networks["proxy-internal"]["ipam"]["config"] == [{"subnet": "172.30.0.0/24"}]
    assert api["networks"]["proxy-internal"]["ipv4_address"] == "172.30.0.3"
    assert proxy["networks"]["proxy-internal"]["ipv4_address"] == "172.30.0.2"
    assert set(api["networks"]) & set(proxy["networks"]) == {"proxy-internal"}
    assert "--forwarded-allow-ips=172.30.0.2" in api["command"]
    assert "--forwarded-allow-ips=*" not in api["command"]


def test_caddy_healthcheck_is_bound_to_loopback() -> None:
    compose = load_compose()
    health_test = compose["services"]["proxy"]["healthcheck"]["test"]
    caddyfile = CADDYFILE_PATH.read_text()

    assert health_test[-1] == "http://127.0.0.1:2015/health"
    assert "http://127.0.0.1:2015 {\n\tbind 127.0.0.1" in caddyfile
    assert "\trespond /health 200" in caddyfile


def test_caddy_access_log_cannot_capture_revocable_qr_paths() -> None:
    caddyfile = CADDYFILE_PATH.read_text()

    # Caddy sees the raw URL before the application's Uvicorn redaction filter.
    # Keep access logging off until a tested path-redaction encoder is configured.
    assert "\n\tlog {" not in caddyfile


def test_runtime_wrapper_enforces_safe_secret_handling() -> None:
    wrapper = WRAPPER_PATH.read_text()

    assert "up | create | run)" in wrapper
    assert "refusing to render Compose configuration" in wrapper
    assert "replace every example.com placeholder" in wrapper
    assert "sslmode=require" in wrapper
    assert "sslmode=verify-full" in wrapper
    assert "for attempt in 1 2 3 4 5" in wrapper


def test_systemd_retries_a_failed_initial_start() -> None:
    unit = UNIT_PATH.read_text()

    assert "Restart=on-failure" in unit
    assert "RestartSec=15s" in unit
    assert "ExecReload=" in unit
    assert "--force-recreate" in unit


def test_host_stack_is_low_cost_no_ssh_and_rds_scoped() -> None:
    stack = load_host_stack()
    resources = stack["Resources"]
    instance = resources["ApiInstance"]["Properties"]
    ingress = resources["ApiSecurityGroup"]["Properties"]["SecurityGroupIngress"]
    rds_ingress = resources["RdsIngressFromApi"]["Properties"]

    assert {(rule["IpProtocol"], rule["FromPort"], rule["ToPort"]) for rule in ingress} == {
        ("tcp", 80, 80),
        ("tcp", 443, 443),
    }
    assert all(rule["FromPort"] != 22 for rule in ingress)
    assert rds_ingress["FromPort"] == rds_ingress["ToPort"] == 5432
    assert rds_ingress["SourceSecurityGroupId"] == {"GetAtt": "ApiSecurityGroup.GroupId"}
    assert "CidrIp" not in rds_ingress

    assert instance["InstanceType"] == "t4g.micro"
    assert instance["CreditSpecification"] == {"CPUCredits": "standard"}
    assert instance["Monitoring"] is False
    assert instance["IamInstanceProfile"] == {"Ref": "InstanceProfileName"}
    assert instance["MetadataOptions"] == {
        "HttpEndpoint": "enabled",
        "HttpTokens": "required",
        "HttpPutResponseHopLimit": 1,
        "InstanceMetadataTags": "disabled",
    }
    assert instance["NetworkInterfaces"][0]["AssociatePublicIpAddress"] is False
    assert instance["NetworkInterfaces"][0]["GroupSet"] == [{"GetAtt": "ApiSecurityGroup.GroupId"}]
    assert instance["BlockDeviceMappings"][0]["Ebs"] == {
        "DeleteOnTermination": True,
        "Encrypted": True,
        "VolumeSize": 12,
        "VolumeType": "gp3",
    }

    data_volume = resources["ApiDataVolume"]
    data_attachment = resources["ApiDataVolumeAttachment"]["Properties"]
    assert data_volume["DeletionPolicy"] == "RetainExceptOnCreate"
    assert data_volume["UpdateReplacePolicy"] == "Retain"
    assert data_volume["Properties"]["Encrypted"] is True
    assert data_volume["Properties"]["VolumeType"] == "gp3"
    assert data_volume["Properties"]["AvailabilityZone"] == {"Ref": "AvailabilityZone"}
    assert data_attachment["VolumeId"] == {"Ref": "ApiDataVolume"}
    assert data_attachment["InstanceId"] == {"Ref": "ApiInstance"}
    assert instance["AvailabilityZone"] == {"Ref": "AvailabilityZone"}


def test_host_bootstrap_separates_paid_host_creation_from_activation() -> None:
    dockerfile = DOCKERFILE_PATH.read_text()
    compose = load_compose()
    bootstrap = HOST_BOOTSTRAP_PATH.read_text()
    configure = HOST_CONFIGURE_PATH.read_text()
    stack = HOST_STACK_PATH.read_text()

    assert 'readonly COMPOSE_VERSION="5.4.0"' in bootstrap
    assert 'readonly COMPOSE_SHA256="fc5d1371' in bootstrap
    assert "sha256sum --check --status" in bootstrap
    assert "systemctl enable --now docker" in bootstrap
    assert "systemctl enable --now fofu" not in bootstrap
    assert "FOFU_DATABASE_URL" not in bootstrap
    assert "FOFU_JWT_SECRET" not in bootstrap
    assert "NORMALIZED_DATA_VOLUME_ID" in bootstrap
    assert "refusing to use the root EBS device" in bootstrap
    assert "refusing to format a non-empty or partitioned data volume" in bootstrap
    assert "UUID=${filesystem_uuid} ${DATA_MOUNT} xfs" in bootstrap
    assert "RequiresMountsFor=${DATA_MOUNT}" in bootstrap

    api = compose["services"]["api"]
    proxy = compose["services"]["proxy"]
    assert "--uid 10001" in dockerfile
    assert "--gid 10001" in dockerfile
    assert api["user"] == "10001:10001"
    assert api["volumes"] == ["/var/lib/fofu/uploads:/app/var/uploads:Z"]
    assert "/var/lib/fofu/caddy-data:/data:Z" in proxy["volumes"]
    assert "/var/lib/fofu/caddy-config:/config:Z" in proxy["volumes"]
    assert "volumes" not in compose

    assert "getent ahostsv4" in configure
    assert "FOFU_CORS_ORIGINS=[]" in configure
    assert "FOFU_AUTO_CREATE_SCHEMA=false" in configure
    assert "FOFU_SEED_DEMO_DATA=false" in configure
    assert "FOFU_APNS_ENABLED=false" in configure
    assert "systemctl start fofu.service" in configure
    assert "FOFU_DATABASE_URL=" not in configure
    assert "FOFU_JWT_SECRET=" not in configure
    assert '[[ "${ipv4_answers}" == "${public_ip}" ]]' in configure
    assert '[[ -z "${ipv6_answers}" ]]' in configure
    assert "refusing to replace it" in configure

    assert "AssociatePublicIpAddress: false" in stack
    assert "AllowedPattern: ^[0-9a-f]{40}$" in stack
    assert "git clone --filter=blob:none --no-checkout" in stack
    assert "git -C /opt/fofu fetch --depth 1 origin ${RepositoryCommit}" in stack
    assert "package_network_ready=false" in stack
    assert "for attempt in $(seq 1 60)" in stack
    assert "bootstrap-ec2-host.sh '${ApiDataVolume}'" in stack
    assert "configure-and-start-host.sh" not in stack


def test_host_network_inspection_is_read_only_and_selects_real_public_routes() -> None:
    script = HOST_NETWORK_PATH.read_text()

    assert "DestinationCidrBlock==`0.0.0.0/0`" in script
    assert '[[ "${default_gateway}" == igw-* ]]' in script
    assert "describe-instance-type-offerings" in script
    assert '[[ "${subnet_az}" == "${rds_az}" ]]' in script
    assert "enableDnsSupport" in script
    assert "enableDnsHostnames" in script
    assert "AvailabilityZone=${recommended_az}" in script
    assert "run prepare-runtime-identity.sh apply first" in script
    assert "run-instances" not in script
    assert "create-security-group" not in script
    assert "authorize-security-group-ingress" not in script
    assert "allocate-address" not in script
    assert "associate-address" not in script
    assert "No AWS resources were changed." in script


def test_identity_preparation_is_narrow_scoped_and_does_not_disclose_jwt() -> None:
    script = IDENTITY_PREP_PATH.read_text()

    assert "usage: prepare-runtime-identity.sh check|apply" in script
    assert "--tier Standard" in script
    assert "--value file:///dev/stdin" in script
    assert "--no-overwrite" in script
    assert "aws configure get cli_history" in script
    assert "ssm:GetParameter" in script
    assert "AmazonSSMManagedInstanceCore" not in script
    assert "--overwrite" not in script
    assert "--with-decryption" not in script
    assert "jwt_secret=" not in script
    assert "fofu-jwt-request" not in script
    assert "run-instances" not in script
    assert "allocate-address" not in script
    assert "create-security-group" not in script
    assert "No metered compute, public IPv4, or networking resource was created." in script


def test_identity_preparation_check_and_apply_are_idempotent(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    state_dir = tmp_path / "state"
    bin_dir.mkdir()
    state_dir.mkdir()
    call_log = tmp_path / "aws-calls.log"
    fake_aws = bin_dir / "aws"
    fake_aws.write_text(
        r"""#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >>"${FAKE_AWS_LOG:?}"
state="${FAKE_AWS_STATE:?}"
service="${1:-}"
operation="${2:-}"

case "${service}:${operation}" in
  sts:get-caller-identity)
    printf '123456789012\n'
    ;;
  rds:describe-db-instances)
    printf 'available\tFalse\tvpc-abc123\tfofu-subnets\tsg-abc123\n'
    ;;
  configure:get)
    printf '%s\n' "${FAKE_AWS_CLI_HISTORY:-disabled}"
    ;;
  ssm:get-parameter)
    if [[ "$*" == *'/fofu/production/database-url'* ]]; then
      printf 'SecureString\t7\n'
    elif [[ -f "${state}/jwt" ]]; then
      printf 'SecureString\n'
    else
      exit 254
    fi
    ;;
  ssm:describe-parameters)
    if [[ "$*" == *'length(Parameters)'* ]]; then
      if [[ "$*" == *'/fofu/production/jwt-secret'* && ! -f "${state}/jwt" ]]; then
        printf '0\n'
      else
        printf '1\n'
      fi
    elif [[ "$*" == *'/fofu/production/jwt-secret'* ]]; then
      [[ -f "${state}/jwt" ]] || exit 254
      printf 'SecureString\tStandard\t1\talias/aws/ssm\n'
    else
      printf 'SecureString\tStandard\t7\talias/aws/ssm\n'
    fi
    ;;
  ssm:put-parameter)
    cat >/dev/null
    touch "${state}/jwt"
    printf '1\tStandard\n'
    ;;
  iam:get-role)
    if [[ ! -f "${state}/role" ]]; then
      echo 'An error occurred (NoSuchEntity) when calling the GetRole operation' >&2
      exit 254
    fi
    printf 'fofu-api-ec2-role\n'
    ;;
  iam:create-role)
    touch "${state}/role"
    printf 'fofu-api-ec2-role\n'
    ;;
  iam:update-assume-role-policy | iam:wait)
    ;;
  iam:put-role-policy)
    touch "${state}/policy"
    ;;
  iam:list-role-tags)
    if [[ "$*" == *"Project"* ]]; then
      printf 'fofu\n'
    else
      printf 'production\n'
    fi
    ;;
  iam:list-attached-role-policies)
    printf '0\n'
    ;;
  iam:list-role-policies)
    if [[ "$*" == *'length(PolicyNames)'* ]]; then
      [[ -f "${state}/policy" ]] && printf '1\n' || printf '0\n'
    elif [[ -f "${state}/policy" ]]; then
      printf 'fofu-api-runtime\n'
    else
      printf 'None\n'
    fi
    ;;
  iam:get-instance-profile)
    if [[ ! -f "${state}/profile" ]]; then
      echo 'An error occurred (NoSuchEntity) when calling the GetInstanceProfile operation' >&2
      exit 254
    fi
    if [[ -f "${state}/attached" ]]; then
      printf 'fofu-api-ec2-role\n'
    else
      printf 'None\n'
    fi
    ;;
  iam:create-instance-profile)
    touch "${state}/profile"
    printf 'fofu-api-ec2-profile\n'
    ;;
  iam:list-instance-profile-tags)
    if [[ "$*" == *"Project"* ]]; then
      printf 'fofu\n'
    else
      printf 'production\n'
    fi
    ;;
  iam:add-role-to-instance-profile)
    touch "${state}/attached"
    ;;
  *)
    echo "unexpected fake AWS call: $*" >&2
    exit 99
    ;;
esac
"""
    )
    fake_aws.chmod(0o755)
    environment = {
        **os.environ,
        "AWS_REGION": "ap-northeast-2",
        "FAKE_AWS_LOG": str(call_log),
        "FAKE_AWS_STATE": str(state_dir),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }

    checked = subprocess.run(
        ["bash", str(IDENTITY_PREP_PATH), "check"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert checked.returncode == 0, checked.stderr
    assert "No AWS resources were changed" in checked.stdout
    assert "iam " not in call_log.read_text()
    assert "ssm put-parameter" not in call_log.read_text()

    history_refusal = subprocess.run(
        ["bash", str(IDENTITY_PREP_PATH), "apply"],
        check=False,
        capture_output=True,
        text=True,
        env={**environment, "FAKE_AWS_CLI_HISTORY": "enabled"},
    )
    assert history_refusal.returncode == 78
    assert "AWS CLI history is enabled" in history_refusal.stderr
    assert "iam " not in call_log.read_text()
    assert "ssm put-parameter" not in call_log.read_text()

    first_apply = subprocess.run(
        ["bash", str(IDENTITY_PREP_PATH), "apply"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert first_apply.returncode == 0, first_apply.stderr
    assert "Runtime identity preparation complete" in first_apply.stdout

    second_apply = subprocess.run(
        ["bash", str(IDENTITY_PREP_PATH), "apply"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert second_apply.returncode == 0, second_apply.stderr
    calls = call_log.read_text()
    assert calls.count("ssm put-parameter") == 1
    assert calls.count("iam create-role") == 1
    assert calls.count("iam create-instance-profile") == 1
    assert calls.count("iam add-role-to-instance-profile") == 1
    assert not re.search(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", calls)
    assert not re.search(
        r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])",
        checked.stdout + first_apply.stdout + second_apply.stdout,
    )


async def proxy_scope(client_ip: str) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        captured.update(scheme=scope["scheme"], client=scope["client"])
        body = json.dumps(captured).encode()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})

    wrapped = ProxyHeadersMiddleware(app, trusted_hosts="172.30.0.2")
    transport = httpx.ASGITransport(app=wrapped, client=(client_ip, 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        response = await client.get(
            "/",
            headers={
                "X-Forwarded-For": "203.0.113.8",
                "X-Forwarded-Proto": "https",
            },
        )
    assert response.status_code == 200
    return response.json()


def test_only_caddy_ip_can_supply_forwarded_headers() -> None:
    trusted = asyncio.run(proxy_scope("172.30.0.2"))
    untrusted = asyncio.run(proxy_scope("172.30.0.99"))

    assert trusted == {"scheme": "https", "client": ["203.0.113.8", 0]}
    assert untrusted == {"scheme": "http", "client": ["172.30.0.99", 12345]}
