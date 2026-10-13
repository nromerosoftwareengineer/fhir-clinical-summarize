# Hosting the whole stack on one AWS EC2 instance

Four containers on one host: HAPI FHIR, Postgres, Ollama, the API, with nginx in
front. The `Dockerfile`, `docker-compose.prod.yml` and `deploy/nginx*.conf` are
cloud-agnostic and used unchanged -- only the provisioning differs from
`deploy/vm-setup.md`.

> **Verified locally:** the image builds and runs, the compose files merge, and
> both nginx configs pass `nginx -t`. The `aws` commands have **not** been run
> against a live account.

---

## Instance sizing

The stack needs ~12 GiB of RAM: HAPI's 3 GB heap, Postgres, Ollama with
gemma3:4b loaded (~3.6 GB at `num_ctx=8192`), plus the API, nginx and the OS.
The summary cache adds a table to the existing Postgres, not a service, so it
costs no extra RAM.

| Instance | vCPU | RAM | Notes |
|---|---|---|---|
| **`m7i.xlarge`** | 4 | 16 GiB | **Default.** Intel, DDR5, non-burstable |
| `m7g.xlarge` | 4 | 16 GiB | Graviton (ARM64). Cheapest of the three; all images publish arm64 |
| `c7i.2xlarge` | 8 | 16 GiB | Twice the cores — the fastest CPU inference here |
| `g4dn.xlarge` | 4 | 16 GiB | **NVIDIA T4.** ~1-2 s per summary instead of 10-30 s |
| ~~`t3.xlarge`~~ | 4 | 16 GiB | Avoid: burstable. Sustained inference drains CPU credits and throttles |

Token generation is memory-bandwidth bound, so going from 4 to 8 vCPU helps less
than it looks. Only a GPU removes the regression against an M-series Mac
entirely; see the GPU section at the end.

---

## 1. Configure the CLI

```bash
aws configure           # access key, secret, region (e.g. us-east-1), output json
aws sts get-caller-identity
```

```bash
export REGION=us-east-1
export NAME=fhir-demo
export TYPE=m7i.xlarge
```

## 2. Key pair

```bash
aws ec2 create-key-pair --region $REGION --key-name $NAME \
  --query KeyMaterial --output text > ~/.ssh/$NAME.pem
chmod 400 ~/.ssh/$NAME.pem
```

## 3. Security group

SSH restricted to your own address; only HTTP and HTTPS open to the world.

```bash
export VPC=$(aws ec2 describe-vpcs --region $REGION \
  --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)

export SG=$(aws ec2 create-security-group --region $REGION \
  --group-name $NAME-sg --description "FHIR summarize demo" \
  --vpc-id $VPC --query GroupId --output text)

export MYIP=$(curl -s https://checkip.amazonaws.com)

aws ec2 authorize-security-group-ingress --region $REGION --group-id $SG \
  --protocol tcp --port 22 --cidr $MYIP/32
aws ec2 authorize-security-group-ingress --region $REGION --group-id $SG \
  --protocol tcp --port 80 --cidr 0.0.0.0/0
aws ec2 authorize-security-group-ingress --region $REGION --group-id $SG \
  --protocol tcp --port 443 --cidr 0.0.0.0/0
```

Port 8080 is never opened. Compose still publishes it on the host -- it appends
rather than replaces port lists across files -- so the security group is what
keeps HAPI unreachable from outside.

## 4. Find the Ubuntu AMI

Canonical publishes the current AMI id as a public SSM parameter, so there is no
id to hardcode or look up by hand:

```bash
# x86_64 (m7i, c7i, g4dn)
export AMI=$(aws ssm get-parameters --region $REGION \
  --names /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --query 'Parameters[0].Value' --output text)

# arm64 (m7g) -- use this instead if you chose Graviton
# export AMI=$(aws ssm get-parameters --region $REGION \
#   --names /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id \
#   --query 'Parameters[0].Value' --output text)

echo $AMI
```

## 5. Launch

```bash
export ID=$(aws ec2 run-instances --region $REGION \
  --image-id $AMI \
  --instance-type $TYPE \
  --key-name $NAME \
  --security-group-ids $SG \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":128,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" \
  --query 'Instances[0].InstanceId' --output text)

aws ec2 wait instance-running --region $REGION --instance-ids $ID
```

## 6. Elastic IP

Worth doing: a plain public IP **changes every time the instance is stopped and
started**, and stopping it between demos is how this stays cheap. An Elastic IP
keeps the address stable, which matters once a DNS record or `PUBLIC_URL` points
at it.

```bash
export ALLOC=$(aws ec2 allocate-address --region $REGION --domain vpc \
  --query AllocationId --output text)
aws ec2 associate-address --region $REGION --instance-id $ID --allocation-id $ALLOC

export IP=$(aws ec2 describe-addresses --region $REGION --allocation-ids $ALLOC \
  --query 'Addresses[0].PublicIp' --output text)
echo "http://$IP"
```

Save `$ID` and `$ALLOC` somewhere -- you need them to stop, start and tear down.

## 7. Install Docker

```bash
ssh -i ~/.ssh/$NAME.pem ubuntu@$IP

curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker ubuntu
sudo apt-get update && sudo apt-get install -y git python3-venv
exit            # log out and back in so the docker group applies
```

```bash
ssh -i ~/.ssh/$NAME.pem ubuntu@$IP
docker run --rm hello-world
```

The default user on Ubuntu AMIs is `ubuntu`, not `ec2-user` (that is Amazon
Linux) and not `azureuser`.

## 8. Clone and start

```bash
git clone https://github.com/<you>/fhir-clinical-summarize.git
cd fhir-clinical-summarize

docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

HAPI's first boot runs schema creation against Postgres -- two to five minutes:

```bash
until curl -sf http://localhost:8080/fhir/metadata >/dev/null; do sleep 5; echo waiting; done
curl -s localhost/health
```

> nginx resolves `api` and `hapi-fhir` at start-up, so if it wins the race it
> exits; `restart: unless-stopped` recovers it within seconds. If nginx is the
> only thing down, restart just that service.

## 9. Pull the model

```bash
C="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
$C exec ollama ollama pull gemma3:4b
$C exec ollama ollama list
```

It lands on the `ollama-models` named volume and survives restarts.

### Create the summary-cache database

`deploy/init-summaries.sql` handles this automatically on a **fresh** volume. An existing
volume -- one that already has HAPI data -- needs it once by hand, because the postgres image
only runs its init hooks when the data directory is empty:

```bash
$C exec db psql -U hapi -d postgres -c "CREATE DATABASE summaries OWNER hapi;"
$C restart api        # ensure_schema() creates the table on startup
```

Confirm with `curl -s localhost/ready | jq .checks.cache` -- expect `"ok"`. A `cache` of
`unavailable` is **not** fatal: packets are served uncached, just slowly. Leave
`SUMMARY_DB_URL` empty to turn the cache off entirely.

## 10. Load patient data

```bash
mkdir -p data && cd data
curl -LO https://synthetichealth.github.io/synthea-sample-data/downloads/synthea_sample_data_fhir_r4_sep2019.zip
unzip -q synthea_sample_data_fhir_r4_sep2019.zip
cd ..

python3 -m venv .venv
./.venv/bin/pip install -e .
./.venv/bin/python -m scripts.load_synthea -n 300
```

The loader runs on the host against `localhost:8080`, which is its default.

## 11. Turn on citation links and verify

```bash
PUBLIC_URL=http://$IP docker compose \
  -f docker-compose.yml -f docker-compose.prod.yml up -d api
```

From your laptop:

```bash
curl -s "http://$IP/health" | jq .
curl -s "http://$IP/ready"  | jq .     # want {"fhir":"ok","model":"ok"}
curl -s "http://$IP/patients?limit=3" | jq .total
curl -s "http://$IP/fhir/metadata" | jq -r .software.version
```

Check that the prose is cited, not just the tables:

```bash
curl -s "http://$IP/patients/78378d37-92b5-4d2e-8f6c-b8f5e79f3999/packet" \
  | jq -r '.citations[] | "\(.text)  ->  \(.source)"'
```

Expect one line per term the summary named. An empty list means the summary came
back empty (check `/ready`), since `app/link.py` needs no configuration of its
own -- it runs in-process, in under a millisecond, with no model call.

Then open **`http://$IP/ui/`**. Expect 10-30 s per packet on CPU, and ~1 s for a
patient whose summary is already cached. In the summary, the underlined terms
jump to the row they came from.

## 12. TLS (optional)

Needs a domain pointing at the Elastic IP.

```bash
sudo apt-get install -y certbot
docker compose -f docker-compose.yml -f docker-compose.prod.yml stop nginx
sudo certbot certonly --standalone -d YOUR_DOMAIN
sudo sed -i 's/DOMAIN/YOUR_DOMAIN/g' deploy/nginx-tls.conf

NGINX_CONF=./deploy/nginx-tls.conf PUBLIC_URL=https://YOUR_DOMAIN \
  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d nginx api
```

---

## Troubleshooting

Every problem below was hit while actually doing this.

### Shorthand

The compose prefix appears in every command. Set it once per session, or append
it to `~/.bashrc`:

```bash
C="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
$C ps
$C logs -f api
```

Watch for commands split across lines with a trailing `\` -- copying only the
second line gives errors like `bash: exec: api: not found`.

### The deployment files are missing after cloning

```bash
$ ls
README.md  app  docker-compose.yml  pyproject.toml  scripts  tests  ui
# no Dockerfile, no docker-compose.prod.yml, no deploy/
```

The clone reflects what is **pushed**, not what is on your laptop. Commit and
push locally, then `git pull` on the instance.

Also check where you are: cloning into a directory of the same name gives
`~/fhir-clinical-summarize/fhir-clinical-summarize`. Harmless, but every `cd` in
this guide assumes the repo root.

### Nothing responds from outside, but localhost works on the instance

If `curl -s localhost/health` works on the instance and nothing works from your
laptop, nginx is fine and the problem is network. In order of likelihood:

**1. Security group.** Check what is actually allowed -- this recovers `$SG`
even if you lost the variable:

```bash
export SG=$(aws ec2 describe-instances --region $REGION \
  --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' --output text)

aws ec2 describe-security-groups --region $REGION --group-ids $SG \
  --query 'SecurityGroups[0].IpPermissions[].{proto:IpProtocol,from:FromPort,to:ToPort,cidrs:IpRanges[].CidrIp}' \
  --output json
```

You want entries for 22, 80 and 443. If only 22 is there:

```bash
aws ec2 authorize-security-group-ingress --region $REGION --group-id $SG \
  --protocol tcp --port 80 --cidr 0.0.0.0/0
aws ec2 authorize-security-group-ingress --region $REGION --group-id $SG \
  --protocol tcp --port 443 --cidr 0.0.0.0/0
```

`InvalidPermission.Duplicate` means that rule already exists -- harmless. Changes
apply immediately; nothing needs restarting.

**Read the failure mode**, because it localises the fault:

```bash
curl -sv --max-time 10 "http://$IP/health" 2>&1 | tail -5
```

| Result | Meaning |
|---|---|
| `{"status":"ok"}` | fixed |
| `Connection timed out` | packets dropped -- security group or NACL |
| `Connection refused` | reached the host, nothing listening -- **not** a firewall problem |

A security group drops traffic silently, so it always presents as a timeout.
"Refused" points at nginx instead.

**2. Host firewall.** On the instance:

```bash
sudo ufw status          # expect: inactive
```

Ubuntu EC2 images ship with it off. If something turned it on:

```bash
sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
```

**3. Network ACL.** Subnet-level, defaults to allow-all, but worth ruling out:

```bash
export SUBNET=$(aws ec2 describe-instances --region $REGION --instance-ids $ID \
  --query 'Reservations[0].Instances[0].SubnetId' --output text)
aws ec2 describe-network-acls --region $REGION \
  --filters "Name=association.subnet-id,Values=$SUBNET" \
  --query 'NetworkAcls[0].Entries' --output table
```

Look for rule 100 allowing all traffic inbound and outbound.

**4. nginx not bound to the host.** On the instance:

```bash
sudo ss -tlnp | grep ':80'      # expect 0.0.0.0:80 via docker-proxy
$C ps                            # is nginx even up?
```

nginx resolves `api` and `hapi-fhir` at start-up, so if it wins the race against
them it exits. `restart: unless-stopped` recovers it within seconds; if it is the
only thing down, `$C restart nginx`.

### Citation links are plain text, or FHIR_PUBLIC_URL is blank

```bash
$C exec api printenv FHIR_PUBLIC_URL     # blank
$C config | grep FHIR_PUBLIC_URL         # FHIR_PUBLIC_URL: ""
```

Compose reads `.env` from the **project directory** -- the folder holding the
compose file passed to `-f`, which with relative paths is your current directory.
A `.env` one level up is never seen.

```bash
cd ~/fhir-clinical-summarize          # the repo root, wherever that is
echo "PUBLIC_URL=http://$IP" > .env
cat .env

$C up -d --force-recreate api
```

`--force-recreate` matters: writing `.env` does not change a running container,
and without it Compose may decide nothing needs replacing.

Verify through the app rather than the environment, since that is what the
dashboard reads:

```bash
curl -s localhost/config
# {"fhir_base_url":"http://100.62.174.138/fhir","model":"gemma3:4b"}
```

Set `PUBLIC_URL` to the bare origin -- scheme and host only. Compose appends
`/fhir` itself (`FHIR_PUBLIC_URL=${PUBLIC_URL:+${PUBLIC_URL}/fhir}`):

| Wrong | Result |
|---|---|
| `http://1.2.3.4/fhir` | `/fhir/fhir` -- every citation 404s |
| `1.2.3.4` | no scheme; the browser reads it as a relative path |
| `https://1.2.3.4` | no certificate until step 12 |
| `http://172.31.x.x` | the private address -- unreachable from a browser |

### ifconfig shows 172.31.x.x, not the public IP

AWS never puts the public address on the interface; it is NAT'd in front of the
instance. `ifconfig` (and `ip addr`, since `net-tools` is not installed on Ubuntu
24.04) only ever shows the private address -- which is also encoded in the shell
prompt, e.g. `ip-172-31-45-112`.

From the instance:

```bash
TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/public-ipv4
```

From your laptop:

```bash
aws ec2 describe-instances --region $REGION \
  --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].PublicIpAddress' --output text
```

### Recovering a lost shell

```bash
export REGION=us-east-1 NAME=fhir-demo
export ID=$(aws ec2 describe-instances --region $REGION \
  --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
export IP=$(aws ec2 describe-instances --region $REGION --instance-ids $ID \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
export ALLOC=$(aws ec2 describe-addresses --region $REGION \
  --query "Addresses[?InstanceId=='$ID'].AllocationId|[0]" --output text)
echo "id=$ID ip=$IP alloc=$ALLOC"
```

Empty output usually means the instance is **stopped**, not gone -- the
`running` filter hides it. Drop the filter to see everything. A stopped instance
has no public IP unless an Elastic IP is attached, which is why step 6 exists.

### /ready reports a dependency down

```bash
curl -s "http://$IP/ready" | jq .
```

| Report | Fix |
|---|---|
| `model: unavailable` or `loaded models do not include ...` | `$C exec ollama ollama pull gemma3:4b` |
| `fhir: unavailable` | HAPI still booting -- `$C logs hapi-fhir`, wait for "Started Application" |
| `/patients` returns `total: 0` | data not loaded -- step 10 |

### The first packet is very slow

20-30 s on the first request while the model loads into memory, then 10-20 s on
this CPU. Both figures were measured under `llama3.2:3b`; `gemma3:4b` is the
larger model, so expect somewhat longer until they are re-measured on this host.

The load cost is paid *per load*, not once. Ollama unloads an idle model after
five minutes by default, so a demo that pauses for questions pays it again --
and the load alone is longer than the inference it precedes. The compose overlay
sets `OLLAMA_KEEP_ALIVE=-1`, which pins the model for the life of the container.
Confirm it took effect:

```bash
$C exec ollama env | grep KEEP_ALIVE     # OLLAMA_KEEP_ALIVE=-1
$C exec ollama ollama ps                 # UNTIL reads "Forever"
```

An empty `ollama ps` means nothing is resident yet: the model loads on the first
packet and stays from then on, so build one packet after `up -d` and every
request after it is inference only.

With the summary cache enabled this cost is paid **once per patient** rather than once per
request: a second request for the same patient skips the model entirely and returns in well
under a second, until that patient's clinical facts change. Warming the handful of patients
a demo will actually visit turns the worst-case first impression into the best case.

A resident gemma3:4b holds **3.6 GB** at `num_ctx=8192`. That is inside the
~12 GiB the stack needs on a 16 GiB host, but on a smaller instance trade it back
for the default:

```bash
OLLAMA_KEEP_ALIVE=5m $C up -d ollama
```

---

## Upgrading an existing instance

For an instance already running an older revision -- moving to `gemma3:4b` and adding the
summary cache. Order matters in two places, noted below.

```bash
ssh -i ~/.ssh/$KEY.pem ubuntu@$IP
cd ~/fhir-clinical-summarize
C="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

git pull
```

**1. Recreate `db` first.** `docker-compose.yml` changed (a loopback-only port binding and
the init-script mount), so Compose replaces the container. HAPI restarts with it because of
`depends_on`; the data is on the `hapi-postgres-data` named volume and survives.

```bash
$C up -d db
until $C exec db pg_isready -U hapi -d hapi; do sleep 3; done
```

**2. Create the cache database before the API starts.** `deploy/init-summaries.sql` only
runs on a *fresh* volume, and this one has HAPI's data in it, so the hook will not fire:

```bash
$C exec db psql -U hapi -d postgres -c "CREATE DATABASE summaries OWNER hapi;"
```

Harmless to re-run -- it fails with `already exists` and changes nothing. Do this *before*
the API boots, so `ensure_schema()` finds the database on its first attempt instead of
logging a warning and running uncached until the next restart.

**3. Pull the new model.** ~3.3 GB; the old one can stay for now.

```bash
$C exec ollama ollama pull gemma3:4b
```

**4. Rebuild the API image.** `psycopg` is a new dependency, so a plain `restart` would run
the old image and the cache would report `unavailable` no matter what else is right. This is
the step that is easy to skip:

```bash
$C up -d --build api
```

**5. Verify.** All three dependencies, then a real packet:

```bash
curl -s localhost/ready | jq .
# {"status":"ready","checks":{"fhir":"ok","model":"ok","cache":"ok"}}

ID=$(curl -s "localhost/patients?limit=1" | jq -r '.patients[0].id')
time curl -s -o /dev/null "localhost/patients/$ID/packet"   # cold: generates
time curl -s -o /dev/null "localhost/patients/$ID/packet"   # warm: cache hit
```

The second call should return in well under a second. `cache: ok` with a slow second call
means the row is not being written -- check `$C logs api | grep "summary cache"`.

Nothing supplies `SUMMARY_DB_URL` by hand here: `docker-compose.prod.yml` defaults it to
`postgresql://hapi:hapi@db:5432/summaries` over the internal network. The `127.0.0.1:55432`
binding in the base file is only for a laptop running uvicorn outside Docker, and binding it
to loopback keeps Postgres off the instance's public interface regardless of what the
security group allows.

### Reclaiming the old model

Optional. Keep it if you still want `scripts/eval_sample.py --model llama3.2:3b` comparisons:

```bash
$C exec ollama ollama list
$C exec ollama ollama rm llama3.2:3b      # frees ~2 GB on the 128 GB volume
```

### Rolling back

The cache is additive, so turning it off needs no code change:

```bash
SUMMARY_DB_URL= $C up -d api              # cache disabled, packets generate fresh
OLLAMA_MODEL=llama3.2:3b $C up -d api     # previous model, if it is still pulled
```

Switching the model back does **not** invalidate cached prose -- the hash covers the
patient's facts, not the model -- so clear it if you want the old model's summaries back:

```bash
$C exec db psql -U hapi -d summaries -c "TRUNCATE summary_cache;"
```

`scripts/` is not copied into the API image (the Dockerfile ships `app/` and `ui/` only), so
`scripts.cache_admin` is a laptop tool -- point it at the instance or use the `psql` above.

---

## Day-to-day

```bash
C="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
$C ps
$C logs -f api
git pull && $C up -d --build api

# stop the compute bill, keep the disk and the IP
aws ec2 stop-instances  --region $REGION --instance-ids $ID
aws ec2 start-instances --region $REGION --instance-ids $ID
```

`restart: unless-stopped` brings every container back after a start, so there is
nothing to re-run.

## Tear down

```bash
aws ec2 terminate-instances --region $REGION --instance-ids $ID
aws ec2 wait instance-terminated --region $REGION --instance-ids $ID
aws ec2 release-address --region $REGION --allocation-id $ALLOC
aws ec2 delete-security-group --region $REGION --group-id $SG
aws ec2 delete-key-pair --region $REGION --key-name $NAME
```

Release the Elastic IP explicitly -- AWS bills for public IPv4 addresses whether
or not they are attached to anything.

Stopped instances still bill for the 128 GB gp3 volume. Terminate when done.

---

## GPU variant

If you want ~1-2 s summaries instead of 10-30 s, `g4dn.xlarge` carries an NVIDIA
T4 (16 GB VRAM) with the same 4 vCPU / 16 GiB.

```bash
export TYPE=g4dn.xlarge
```

Check the service quota first -- new accounts often have zero vCPUs for G
instances:

```bash
aws service-quotas get-service-quota --region $REGION \
  --service-code ec2 --quota-code L-DB2E81BA \
  --query 'Quota.{name:QuotaName, value:Value}'
```

Then, on the instance, two extra steps beyond the CPU path:

```bash
# NVIDIA driver
sudo apt-get update && sudo apt-get install -y nvidia-driver-550
sudo reboot
# after reconnecting:
nvidia-smi

# NVIDIA Container Toolkit, so Docker can see the GPU
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

And reserve the GPU for the model container, or it silently runs on CPU:

```yaml
# docker-compose.gpu.yml
services:
  ollama:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  -f docker-compose.gpu.yml up -d
```

Confirm it is actually on the GPU -- expect 60+ tokens/sec rather than 10-20:

```bash
$C exec ollama ollama run gemma3:4b --verbose "say hello"
```

Then drop `REQUEST_TIMEOUT_S` back to 60 and `proxy_read_timeout` to 60s.

---

## What this is not

Production. Single host, no redundancy, OS patching is yours, the Postgres
password sits in `docker-compose.yml`, and `/fhir/` is publicly readable
(GET-only, but public). Fine for Synthea data; not fine for PHI.

The managed path on AWS, equivalent to what `deploy/README.md` describes for
Azure: **AWS HealthLake** for FHIR (SigV4-signed requests rather than the Entra
ID tokens `fhir_client.py` supports today), **RDS for PostgreSQL**, **ECS
Fargate** or **App Runner** for the API, **Bedrock** for inference, and private
subnets with VPC endpoints throughout.
