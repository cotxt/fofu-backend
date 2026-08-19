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
COMPOSE_PATH = ROOT / "deploy" / "aws" / "compose.production.yaml"
CADDYFILE_PATH = ROOT / "deploy" / "aws" / "Caddyfile"
WRAPPER_PATH = ROOT / "deploy" / "aws" / "compose-with-ssm.sh"
IDENTITY_PREP_PATH = ROOT / "deploy" / "aws" / "prepare-runtime-identity.sh"
UNIT_PATH = ROOT / "deploy" / "aws" / "fofu.service"


def load_compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE_PATH.read_text())


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
