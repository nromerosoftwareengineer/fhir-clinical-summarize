# Hosting the whole stack on one Azure VM

Four containers on one host: HAPI FHIR, Postgres, Ollama, the API, with nginx in
front terminating HTTP(S) and reverse-proxying. The compose files are reused
as-is, so there is nothing to translate.

Why this rather than Container Apps, for a demo: the compose stack already works,
the Ollama model persists on a real disk, citation links resolve because nginx can
proxy `/fhir/` on the same hostname, and `az vm deallocate` stops the bill between
demos. The trade is a single point of failure, OS patching, and manual TLS
renewal — all acceptable for something shown once, none acceptable for real PHI.

> **Verified locally:** the image builds and runs, the compose files merge, both
> nginx configs pass `nginx -t`, and `FHIR_PUBLIC_URL` interpolates correctly.
> The `az` commands have **not** been run against a live subscription.

---

## 1. Create the VM

```bash
az login
az account set --subscription "<your subscription>"

export RG=rg-fhir-demo
export LOC=eastus
export VM=vm-fhir

az group create -n $RG -l $LOC

az vm create -g $RG -n $VM \
  --image Ubuntu2404 \
  --size Standard_D4s_v5 \
  --admin-username azureuser \
  --generate-ssh-keys \
  --public-ip-sku Standard \
  --os-disk-size-gb 128

az vm open-port -g $RG -n $VM --port 80 --priority 1001
az vm open-port -g $RG -n $VM --port 443 --priority 1002

export IP=$(az vm show -d -g $RG -n $VM --query publicIps -o tsv)
echo "http://$IP"
```

`Standard_D4s_v5` (4 vCPU / 16 GiB), **not** a burstable B-series: Ollama needs
sustained CPU and a B-series throttles mid-inference. The 16 GiB covers HAPI's
3 GB heap, Postgres, the model's working set, and the API.

Only 80 and 443 are opened. HAPI still publishes 8080 inside the host because
Compose appends port lists across files rather than replacing them — the network
security group is what keeps it unreachable.

## 2. Install Docker

```bash
ssh azureuser@$IP

curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker azureuser
sudo apt-get update && sudo apt-get install -y git
exit            # log out and back in so the docker group applies
```

```bash
ssh azureuser@$IP
docker run --rm hello-world     # confirm it works without sudo
```

## 3. Get the code

```bash
git clone <your-repo-url> fhir-clinical-summarize
cd fhir-clinical-summarize
```

## 4. Start the stack

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

The `--build` builds the API image on the VM. `data/` is in `.dockerignore`, so
the build context stays small.

HAPI's first boot runs Hibernate schema creation against Postgres — two to five
minutes. Wait for it:

```bash
until curl -sf http://localhost:8080/fhir/metadata >/dev/null; do sleep 5; echo waiting; done
echo "HAPI ready"
```

**Checkpoint:** `curl -s localhost/health` should return `{"status":"ok"}`.

> nginx resolves `api` and `hapi-fhir` at startup, so if it comes up first it will
> exit. `restart: unless-stopped` recovers it within a few seconds. If nginx is
> the only thing down, `docker compose ... restart nginx`.

## 5. Pull the model

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  exec ollama ollama pull llama3.2:3b

docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  exec ollama ollama list
```

It lands on the `ollama-models` named volume, so it survives restarts and
rebuilds. Only `docker compose down -v` would remove it.

## 6. Load patient data

```bash
mkdir -p data && cd data
curl -LO https://synthetichealth.github.io/synthea-sample-data/downloads/synthea_sample_data_fhir_r4_sep2019.zip
unzip -q synthea_sample_data_fhir_r4_sep2019.zip
cd ..

sudo apt-get install -y python3-venv
python3 -m venv .venv
./.venv/bin/pip install -e .
./.venv/bin/python -m scripts.load_synthea -n 300
```

The loader runs on the host against `localhost:8080`, which is the default — no
`--base` needed. 1.3 GB extracted, ~2 s per bundle.

**Checkpoint:**

```bash
curl -s "localhost:8080/fhir/Patient?_summary=count&_total=accurate" \
  -H 'Cache-Control: no-cache' | python3 -c 'import json,sys; print(json.load(sys.stdin)["total"])'
```

## 7. Verify from your laptop

```bash
curl -s "http://$IP/health" | jq .
curl -s "http://$IP/ready"  | jq .      # want {"fhir":"ok","model":"ok"}
curl -s "http://$IP/patients?limit=3" | jq '.total'
curl -s "http://$IP/fhir/metadata" | jq -r '.software.version'
```

Then open **`http://$IP/ui/`**, search for a patient, and build a packet. Expect
10–30 s on CPU rather than the 3–8 s you get locally.

## 8. Citation links

Set `PUBLIC_URL` so the dashboard links each source to the record, through the
`/fhir/` path nginx proxies:

```bash
PUBLIC_URL=http://$IP docker compose \
  -f docker-compose.yml -f docker-compose.prod.yml up -d api
```

Left unset, the dashboard renders `Condition/abc123` as plain text instead of a
dead link. With it set, clicking a citation opens the FHIR record — the most
convincing thing this project does in a demo.

## 9. TLS (optional)

Only worth it if you have a domain. Point an A record at `$IP`, then:

```bash
sudo apt-get install -y certbot
docker compose -f docker-compose.yml -f docker-compose.prod.yml stop nginx
sudo certbot certonly --standalone -d YOUR_DOMAIN
sudo sed -i 's/DOMAIN/YOUR_DOMAIN/g' deploy/nginx-tls.conf

NGINX_CONF=./deploy/nginx-tls.conf PUBLIC_URL=https://YOUR_DOMAIN \
  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d nginx api
```

Renewal, since nginx holds port 80:

```bash
sudo crontab -e
# 0 3 * * * certbot renew --webroot -w /var/www/certbot --quiet && docker restart nginx
```

For synthetic data, plain HTTP on the IP is a defensible choice and one less
moving part.

---

## Day-to-day

```bash
C="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

$C ps                      # what is running
$C logs -f api             # API logs (JSON lines)
$C logs -f hapi-fhir
$C restart api
git pull && $C up -d --build api    # deploy a change

az vm deallocate -g $RG -n $VM      # stop the bill, keep the disk
az vm start      -g $RG -n $VM      # back up; containers restart themselves
```

`restart: unless-stopped` on every service means the stack comes back by itself
after a reboot or a `vm start`.

## Tear down

```bash
az group delete -n $RG --yes --no-wait
```

A `D4s_v5` left running bills continuously — roughly a few dollars a day, plus
disk. Deallocate between demos and delete the group when you are done. Check the
pricing calculator for your region rather than trusting that estimate.

## What this is not

Production. Single host, no redundancy, OS patching is yours, the Postgres
password is in `docker-compose.yml`, and `/fhir/` is publicly readable (GET-only,
but public). That last one is fine for Synthea data and not fine for PHI. The
path for real data is `deploy/README.md`: managed FHIR behind a private endpoint,
Entra ID auth via `FHIR_AUTH_SCOPE`, and no publicly reachable FHIR endpoint at
all.
