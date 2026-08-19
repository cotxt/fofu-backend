# AWS EC2 production deployment

This is the authoritative low-cost deployment path for the Fofu FastAPI backend. It keeps the
existing `fofu-postgres` RDS instance private and runs the API plus Caddy on one Arm-based EC2
host. The public API hostname is `api.fofu.me`.

Run Sections 1–3 in **AWS Console → CloudShell** in `ap-northeast-2`. Do not run those commands in
the local Mac terminal unless it is separately authenticated to the intended AWS account.

## What gets created and billed

The identity helper creates a least-privilege EC2 role/profile and a Standard SecureString JWT
parameter. The CloudFormation stack then creates:

- one `t4g.micro` EC2 instance with a disposable encrypted 12 GiB gp3 root volume;
- one Elastic IP;
- one security group exposing only TCP 80/443, with no SSH rule;
- one RDS security-group rule allowing TCP 5432 only from the API security group; and
- one encrypted 10 GiB gp3 data volume for uploads and Caddy certificate state.

The metered host resources start at the `cloudformation deploy` command. EC2, both EBS volumes,
the public IPv4 address, data transfer, RDS, and any optional monitoring are billed under the
account's current prices and credits. The separate 10 GiB data volume has its own gp3 storage
charge for as long as it exists; see [Amazon EBS pricing](https://aws.amazon.com/ebs/pricing/).
Check AWS Billing rather than treating an estimate as a spending limit.

This is a single-host deployment and has no high availability. An EC2 replacement or deployment
causes downtime. RDS backups remain a separate responsibility.

## 1. Prepare runtime identity and secrets

Open CloudShell and obtain the repository. It is currently public and can be cloned anonymously;
GitHub credentials or a personal access token are not required.

```bash
cd ~

if [ -d fofu-backend/.git ]; then
  cd fofu-backend
  git pull --ff-only
else
  git clone https://github.com/cotxt/fofu-backend.git
  cd fofu-backend
fi

export AWS_REGION=ap-northeast-2
```

First run the read-only check:

```bash
bash deploy/aws/prepare-runtime-identity.sh check
```

It must report the expected 12-digit account, private `fofu-postgres` instance, VPC, RDS security
group, and `/fofu/production/database-url` SecureString. Then apply the IAM/JWT preparation:

```bash
bash deploy/aws/prepare-runtime-identity.sh apply
```

`apply` creates or verifies `fofu-api-ec2-role`, `fofu-api-ec2-profile`, the narrow
`fofu-api-runtime` policy, and `/fofu/production/jwt-secret`. It never prints or overwrites an
existing JWT value. It does not create EC2, EBS, Elastic IP, or other hourly resources.

If it says AWS CLI history is enabled, follow the displayed command to disable that history and
rerun `apply`. Do not paste the database URL or JWT value into Git, CloudFormation parameters, or
`/etc/fofu/production.env`.

## 2. Discover the safe network parameters

Run the read-only network inspector:

```bash
bash deploy/aws/inspect-api-host-network.sh
```

It accepts only an available subnet that has an active IPv4 default route to an internet gateway,
supports `t4g.micro`, and belongs to the RDS VPC. It prefers the RDS Availability Zone so the API
and database do not incur cross-AZ traffic. It also prints the current Arm64 Amazon Linux AMI root
device.

Copy the five recommended values into variables, replacing every example below with the inspector
output. If RDS lists more than one security group, choose the one that should own the Fofu ingress
rule.

```bash
export VPC_ID=vpc-xxxxxxxx
export PUBLIC_SUBNET_ID=subnet-xxxxxxxx
export AVAILABILITY_ZONE=ap-northeast-2a
export RDS_SECURITY_GROUP_ID=sg-xxxxxxxx
export ROOT_DEVICE_NAME=/dev/xvda
```

`PUBLIC_SUBNET_ID` and `AVAILABILITY_ZONE` must describe the same subnet. The retained EBS data
volume is created in that Availability Zone and cannot be attached across Availability Zones.

## 3. Validate and create the host stack

Make sure CloudShell has the newest tested repository commit, then validate the template:

```bash
git pull --ff-only
export REPOSITORY_COMMIT="$(git rev-parse HEAD)"

aws cloudformation validate-template \
  --template-body file://deploy/aws/fofu-api-host.yaml
```

Validation is read-only. Review the variables once, then create the paid host resources:

```bash
aws cloudformation deploy \
  --stack-name fofu-api-host \
  --template-file deploy/aws/fofu-api-host.yaml \
  --parameter-overrides \
    "VpcId=${VPC_ID}" \
    "PublicSubnetId=${PUBLIC_SUBNET_ID}" \
    "AvailabilityZone=${AVAILABILITY_ZONE}" \
    "RdsSecurityGroupId=${RDS_SECURITY_GROUP_ID}" \
    "RootDeviceName=${ROOT_DEVICE_NAME}" \
    "RepositoryCommit=${REPOSITORY_COMMIT}" \
  --no-fail-on-empty-changeset
```

The template checks out that exact 40-character Git commit. No SSH key pair is created. Host
bootstrap installs Docker and a checksum-pinned Arm64 Docker Compose plugin, adds swap, formats and
mounts the separate data volume at `/var/lib/fofu`, and installs the systemd unit. It deliberately
does **not** start Fofu before DNS is ready.

Display and save the stack outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name fofu-api-host \
  --query 'Stacks[0].Outputs[].{Key:OutputKey,Value:OutputValue}' \
  --output table

export INSTANCE_ID="$(aws cloudformation describe-stacks \
  --stack-name fofu-api-host \
  --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue | [0]" \
  --output text)"
export ELASTIC_IP="$(aws cloudformation describe-stacks \
  --stack-name fofu-api-host \
  --query "Stacks[0].Outputs[?OutputKey=='ElasticIp'].OutputValue | [0]" \
  --output text)"
export DATA_VOLUME_ID="$(aws cloudformation describe-stacks \
  --stack-name fofu-api-host \
  --query "Stacks[0].Outputs[?OutputKey=='DataVolumeId'].OutputValue | [0]" \
  --output text)"

printf 'instance=%s\neip=%s\ndata_volume=%s\n' \
  "$INSTANCE_ID" "$ELASTIC_IP" "$DATA_VOLUME_ID"
```

In AWS Console, open **EC2 → Instances → fofu-api → Connect → Session Manager → Connect**. Wait
until Session Manager is available, then check bootstrap:

```bash
sudo cloud-init status --wait
sudo tail -n 200 /var/log/fofu-bootstrap.log
sudo test -f /var/lib/fofu/bootstrap-complete && echo "bootstrap ready"
sudo findmnt /var/lib/fofu
sudo docker compose version
```

Do not continue until `bootstrap ready` appears. If `cloud-init` reports an error, keep the stack
and inspect the bootstrap log rather than creating a second instance.

## 4. Point Spaceship DNS at the Elastic IP

In Spaceship, open DNS records for `fofu.me` and create this record:

| Type | Host/name | Value | TTL |
|---|---|---|---|
| A | `api` | the stack's `ElasticIp` value | automatic or 300 seconds |

For `api.fofu.me`, keep exactly one A record and make it the stack Elastic IP. Remove any AAAA,
extra A, or conflicting CNAME record. If a DNS service offers a proxy/CDN switch, use DNS-only/no
proxy for this first deployment. Existing root-domain records for `fofu.me` can remain unchanged.

From CloudShell, wait until the first command prints only the Elastic IP and the other two commands
print nothing:

```bash
dig +short A api.fofu.me
dig +short AAAA api.fofu.me
dig +short CNAME api.fofu.me
```

DNS propagation can take several minutes. Do not activate Caddy while the name still points
somewhere else.

## 5. Activate Fofu through Session Manager

Return to the existing Session Manager shell. Substitute a real contact email used for TLS
certificate notices; it is not an application login or secret.

```bash
sudo /opt/fofu/deploy/aws/configure-and-start-host.sh \
  api.fofu.me \
  YOUR_TLS_CONTACT_EMAIL
```

The script verifies that `api.fofu.me` resolves only to this instance's Elastic IP and has no AAAA
record. It then writes the non-secret production settings, enables `fofu.service`, builds the API
image, starts Caddy, obtains HTTPS, and checks FastAPI plus RDS readiness. The first build on a
micro instance can take several minutes. Success ends with:

```text
Fofu is ready at https://api.fofu.me/api/v1
```

If it fails, inspect only service and container logs; do not print SSM parameters:

```bash
sudo systemctl status fofu --no-pager
sudo journalctl -u fofu -n 100 --no-pager
cd /opt/fofu
sudo ./deploy/aws/compose-with-ssm.sh ps
sudo ./deploy/aws/compose-with-ssm.sh logs --tail 200 api
sudo ./deploy/aws/compose-with-ssm.sh logs --tail 200 proxy
```

## 6. Verify HTTPS and real catalog data

Run these from CloudShell or the Mac terminal:

```bash
curl --fail --show-error --silent https://api.fofu.me/health/ready
curl --head http://api.fofu.me/health/ready

curl --fail --show-error --silent --get \
  'https://api.fofu.me/api/v1/restaurants' \
  --data 'lat=33.4996' \
  --data 'lng=126.5312' \
  --data 'radius_m=50000' \
  --data 'limit=1' \
  | jq '{total,has_more,first:(.items[0] | {name,latitude,longitude})}'

curl --fail --show-error --silent --get \
  'https://api.fofu.me/api/v1/search' \
  --data-urlencode 'q=김치' \
  --data 'lat=33.4996' \
  --data 'lng=126.5312' \
  --data 'radius_m=50000' \
  --data 'limit=1' \
  | jq '{query,item_count,restaurant_count}'
```

The production catalog bounds search to an explicit area, so `lat`, `lng`, and
`radius_m` are required; omitting them returns `422 search_area_required`.

The readiness response must contain `"status":"ready"`, plain HTTP must redirect to HTTPS, and
the Jeju restaurant request must return a nonzero `total`. Also open
`https://api.fofu.me/api/v1/docs` in a browser.

## 7. Set the iOS API base URL

The backend host/origin values intentionally have **no** API path:

```text
FOFU_DOMAIN=api.fofu.me
FOFU_WEB_APP_BASE_URL=https://api.fofu.me
FOFU_PUBLIC_API_BASE_URL=https://api.fofu.me
```

The activation script writes those values automatically. In contrast, the iOS build setting must
include the API version path for both the intended Debug configuration and Release:

```text
FOFU_API_BASE_URL = https://api.fofu.me/api/v1
```

Do not append another `/api/v1` in request code. Build the app, run it on a physical device away
from the development network, move the map to Jeju, and confirm restaurant details and menus load.

## Operations

### Deploy an application update

Use Session Manager; SSH remains closed. Only deploy a reviewed commit that is already on the
configured upstream branch:

```bash
cd /opt/fofu
sudo git pull --ff-only
sudo install -o root -g root -m 0644 \
  deploy/aws/fofu.service /etc/systemd/system/fofu.service
sudo systemctl daemon-reload
sudo systemctl reload fofu
```

`reload` reads the current SSM values, rebuilds the image, force-recreates the containers, and
waits for readiness. It preserves `/var/lib/fofu/uploads` and Caddy state on the data volume.

The deployment does not run `alembic upgrade head` automatically. The restricted `fofu_app` role
must not own or migrate the schema. Back up RDS and perform future migrations separately with a
temporary migration credential.

### Stop and inspect

```bash
sudo systemctl stop fofu
sudo systemctl start fofu
sudo journalctl -u fofu -n 100 --no-pager
```

Stopping the service does not stop EC2, RDS, EBS, or public IPv4 charges. The Compose wrapper
fetches decrypted SSM values only for commands that create containers. It rejects the
output-producing `docker compose config` command because rendered output could expose secrets.

### Retained data volume and stack deletion

`ApiDataVolume` stores these paths and has both `DeletionPolicy: Retain` and
`UpdateReplacePolicy: Retain`:

```text
/var/lib/fofu/uploads
/var/lib/fofu/caddy-data
/var/lib/fofu/caddy-config
```

Consequences:

- replacing the EC2 instance within the same stack/AZ can reattach the same data volume;
- deleting the stack leaves the data volume behind and its EBS storage charge continues;
- recreating the stack does not automatically discover or attach that retained volume;
- changing `AvailabilityZone` replaces the data volume, retains the old one, and can create two
  billed volumes; and
- an EBS volume can be attached only to an EC2 instance in the same Availability Zone.

Record `DataVolumeId` before deleting a stack. Snapshot or otherwise back up required uploads.
Delete a retained volume manually only after its data is confirmed unnecessary; that deletion is
irreversible. The 12 GiB root volume is different: it is deleted with the EC2 instance.

## Audit appendix: manual equivalent

The automated files are the reviewable source of truth:

| File | Responsibility |
|---|---|
| `prepare-runtime-identity.sh` | Verify private RDS/SSM and create the narrow IAM role/profile plus JWT |
| `inspect-api-host-network.sh` | Read-only VPC, public-route, AZ, instance-offering, AMI, and root-device discovery |
| `fofu-api-host.yaml` | EC2, Elastic IP, security groups, RDS ingress, root EBS, and retained data EBS |
| `bootstrap-ec2-host.sh` | Host packages, pinned Compose, swap, XFS mount, permissions, and systemd installation |
| `configure-and-start-host.sh` | Exact DNS check, non-secret environment, service start/reload, and readiness check |
| `compose-with-ssm.sh` | Runtime-only retrieval of the database URL and JWT from SSM |

A manual console build must be equivalent: Arm64 Amazon Linux 2023 `t4g.micro`, standard CPU
credits, encrypted storage, IMDSv2 required with hop limit 1, no SSH/key pair, only TCP 80/443
public ingress, RDS 5432 sourced only from the API security group, the exact instance profile, a
stable Elastic IP, and the separate same-AZ retained data volume. Do not attach the broad
`AmazonSSMManagedInstanceCore` policy in addition to the generated narrow policy, because that
would broaden Parameter Store read access.
