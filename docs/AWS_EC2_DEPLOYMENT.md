# AWS EC2 production deployment

This is the low-cost, single-host deployment for the Fofu FastAPI service. One EC2 instance runs
the non-root API container and Caddy. Caddy terminates HTTPS and is the only container with public
ports. The existing private RDS instance remains outside Docker.

## What this deployment protects

- The database URL and JWT secret stay in SSM Parameter Store and are fetched only at runtime.
- The API does not publish port 8000; only Caddy publishes 80/443.
- The API image already runs as the unprivileged `fofu` user. The production container also drops
  Linux capabilities, uses a read-only root filesystem, and enables `no-new-privileges`.
- Caddy and the API share a dedicated internal Docker network. Caddy has the fixed address
  `172.30.0.2`, and Uvicorn trusts forwarded headers from that address only. Separate one-service
  egress networks let the API reach RDS and Caddy reach ACME without adding another API peer.
- Both containers restart after a crash or reboot. Docker's `local` log driver rotates each
  container at 10 MB, retaining five files.
- Caddy access logging is disabled so revocable `/q/<code>` paths cannot bypass the API's access-log
  redaction. Caddy operational/error output and the API's redacted access logs still go to stdout.
- `/health/ready` checks both FastAPI and RDS before Compose reports a successful start.

This is intentionally a one-instance setup. It has no high availability: an EC2 failure or deploy
causes downtime. RDS backups are still required.

## 1. AWS prerequisites

Use `ap-northeast-2` and put EC2 in the same VPC as `fofu-postgres`.

1. Create an EC2 IAM role, for example `fofu-api-ec2-role`, with EC2 as its trusted service.
2. For strict Parameter Store isolation, do **not** attach `AmazonSSMManagedInstanceCore`. Its
   current v2 policy grants both `ssm:GetParameter` and `ssm:GetParameters` on `Resource: "*"`, and
   an additional narrow Allow cannot reduce that access. Instead, add the custom inline policy
   below. It grants the AWS-documented Session Manager agent/channel minimum and adds
   `ssm:GetParameter` for Fofu's two values only. Replace the account ID:

   ```json
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
           "arn:aws:ssm:ap-northeast-2:ACCOUNT_ID:parameter/fofu/production/database-url",
           "arn:aws:ssm:ap-northeast-2:ACCOUNT_ID:parameter/fofu/production/jwt-secret"
         ]
       }
     ]
   }
   ```

   Compare the broad
   [AWS-managed policy](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonSSMManagedInstanceCore.html)
   with AWS's
   [custom Session Manager role](https://docs.aws.amazon.com/systems-manager/latest/userguide/getting-started-create-iam-instance-profile.html).
   The default `alias/aws/ssm` key needs no custom key policy. If a customer-managed KMS key is
   used, also grant `kms:Decrypt` for that exact key.
3. Keep the existing `/fofu/production/database-url` SecureString. Create
   `/fofu/production/jwt-secret` as a Standard SecureString containing at least 32 random bytes.
   Generate it locally with `openssl rand -hex 32`; never paste it into Git or this repository.
4. Launch an Arm-based Amazon Linux 2023 `t4g.micro` in a public subnet with the IAM role above.
   Use an encrypted 12 GiB gp3 root disk and Session Manager instead of SSH. Require IMDSv2 and
   set the metadata response hop limit to `1`, so application containers cannot obtain the EC2
   instance role credentials. The Python and Caddy images used here support Arm64.
5. The EC2 security group needs inbound TCP 80 and 443 from the internet and no port 22 rule. Add
   UDP 443 if HTTP/3 is desired. The RDS security group must allow PostgreSQL TCP 5432 **from this
   EC2 security group only**, never from `0.0.0.0/0`.
6. Point the API hostname's DNS A record at the EC2 public address. The address must remain stable
   across reboots (normally an Elastic IP). DNS must resolve before Caddy can issue a certificate.

## 2. Install the host software

In an EC2 Session Manager shell:

```bash
sudo dnf install -y docker git
sudo systemctl enable --now docker
aws --version
sudo docker compose version
```

The last command must report Docker Compose v2.20 or newer. Amazon Linux may not install the
Compose plugin with Docker Engine. If it is absent, install a version-pinned ARM64 plugin into the
system-wide `/usr/local/lib/docker/cli-plugins/docker-compose` path, verify its published checksum,
and rerun `sudo docker compose version`. Follow Docker's official Linux plugin instructions; do not
install the legacy Python `docker-compose` command or put the plugin only in one user's home.

The micro instance has 1 GiB RAM. Add a 2 GiB swap file once so the first local Docker build does
not run out of memory:

```bash
sudo dd if=/dev/zero of=/swapfile bs=1M count=2048 status=progress
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile swap swap defaults 0 0' | sudo tee -a /etc/fstab
free -h
```

Clone the exact repository and keep the deployment path unchanged because the unit file uses it:

```bash
sudo git clone https://github.com/cotxt/fofu-backend.git /opt/fofu
sudo chown -R root:root /opt/fofu
sudo chmod 0755 /opt/fofu/deploy/aws/compose-with-ssm.sh
```

## 3. Configure non-secret settings

```bash
sudo install -d -o root -g root -m 0755 /etc/fofu
sudo install -o root -g root -m 0640 \
  /opt/fofu/deploy/aws/production.env.example \
  /etc/fofu/production.env
sudoedit /etc/fofu/production.env
```

Replace every `example.com` value. `FOFU_DOMAIN` is only the hostname, while the three application
URL/origin values include `https://`. Configure the real Google OAuth audience before enabling
Google login. Leave schema creation, demo seed, and APNs disabled for the first deployment.

The startup wrapper rejects `example.com` placeholders and a PostgreSQL URL that does not contain
`sslmode=require`, `sslmode=verify-ca`, or `sslmode=verify-full`.

Do not put either secret in this file. The app role in the RDS URL should be `fofu_app`, not the RDS
master user. URL-encode special characters in the database password.

## 4. Start at boot

```bash
sudo install -o root -g root -m 0644 \
  /opt/fofu/deploy/aws/fofu.service \
  /etc/systemd/system/fofu.service
sudo systemctl daemon-reload
sudo systemctl enable --now fofu
```

The first image build can take several minutes on a micro instance. Check it with:

```bash
sudo systemctl status fofu --no-pager
sudo journalctl -u fofu -n 100 --no-pager
cd /opt/fofu && sudo ./deploy/aws/compose-with-ssm.sh ps
curl --fail --show-error https://api.example.com/health/ready
```

The final response must contain `"status":"ready"`. Also verify that plain HTTP redirects to
HTTPS and that `https://api.example.com/api/v1/docs` loads.

Docker restarts containers that exit and systemd retries a failed initial deployment. Neither one
restarts a process merely because its health check becomes unhealthy, so configure an external
HTTPS uptime check for `/health/ready` and an alert before relying on this service remotely.

Confirm the proxy health listener is bound to container loopback and not the Docker interfaces:

```bash
cd /opt/fofu
sudo ./deploy/aws/compose-with-ssm.sh exec proxy wget -qO- http://127.0.0.1:2015/health
sudo ./deploy/aws/compose-with-ssm.sh exec proxy sh -c \
  'for ip in $(hostname -i); do ! wget -qO- "http://${ip}:2015/health"; done'
```

The first command succeeds with an empty HTTP 200 response; the second succeeds only when access
through the container's non-loopback address is rejected. If `172.30.0.0/24` overlaps a future VPC
or VPN route, change that subnet and both fixed addresses together before starting the service.

## Operations

Redacted application access logs and Caddy operational/error logs:

```bash
cd /opt/fofu
sudo ./deploy/aws/compose-with-ssm.sh logs --tail 200 api
sudo ./deploy/aws/compose-with-ssm.sh logs --tail 200 proxy
```

The wrapper loads decrypted SSM values only for `up`, `create`, and `run`. Read-only operational
commands and `down` use interpolation placeholders, so they remain available during an SSM outage.
It refuses the output-producing `docker compose config` form because that output could disclose
secrets; only `config --quiet` is allowed.

Deploy a fast-forward Git update:

```bash
cd /opt/fofu
sudo git pull --ff-only
sudo install -o root -g root -m 0644 deploy/aws/fofu.service /etc/systemd/system/fofu.service
sudo systemctl daemon-reload
sudo systemctl reload fofu
```

`reload` retrieves fresh SSM values, rebuilds the image, waits for readiness, and leaves the named
upload and Caddy certificate volumes intact. Rotate either secret in Parameter Store and reload the
service to apply it.

The deployment deliberately does **not** run `alembic upgrade head` on every boot. The restricted
`fofu_app` account must not own or migrate the schema. Review and back up RDS, then run future
migrations as a separate controlled release step with a temporary migration credential.

To stop the API without deleting persistent volumes:

```bash
sudo systemctl stop fofu
```

Never use `docker compose down --volumes` unless the Caddy state and uploaded media are intentionally
being deleted.
