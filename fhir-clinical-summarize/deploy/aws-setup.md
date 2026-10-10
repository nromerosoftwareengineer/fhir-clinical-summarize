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

The stack needs ~11 GiB of RAM: HAPI's 3 GB heap, Postgres, Ollama with the
model loaded (~3 GB), plus the API, nginx and the OS.

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
$C exec ollama ollama pull llama3.2:3b
$C exec ollama ollama list
```

It lands on the `ollama-models` named volume and survives restarts.

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

Then open **`http://$IP/ui/`**. Expect 10-30 s per packet on CPU.

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
$C exec ollama ollama run llama3.2:3b --verbose "say hello"
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
